# bebop architecture

This document is the map. If you're trying to understand how the pieces fit
together — the Python package, the Rust crate, the C++ AU plugin, what runs
on which thread, what's testable without Logic — start here.

## TL;DR

bebop has **three layers** that all use the same algorithms (the Python
package is the source of truth for every musical decision) but are dispatched
differently:

1. **Offline pipeline** — `bebop` Python package + `bebop` CLI: takes a WAV
   in, writes a comp .mid out. The fastest way to iterate on the algorithms.
2. **Standalone realtime mode** — `bebop live` CLI: listens to a virtual
   audio cable (BlackHole), generates comp, streams it back via the IAC
   Driver bus. Predates the AU; still maintained as the no-AU alternative.
3. **Audio Unit plugin** — `bebop.component`: Logic-loadable AU that
   listens to a bus and emits MIDI. Production target.

```
                                   ┌─────────────────────────────────────────┐
                                   │  bebop/         (Python package)        │
                                   │  ────────────                            │
                  ┌────────────── ▶│  - chord recognition (CQT + chroma)     │
                  │                │  - voicing engine (rootless/evans/...)  │
                  │                │  - rhythm engine (12 templates)         │
                  │                │  - reharm engine (10 substitution rules)│
                  │                │  - eval bench (tests/eval/)             │
                  │                └─────────────────────────────────────────┘
                  │                                ▲
                  │                                │  PyO3 calls into the
                  │                                │  same public API
                  │                                │
   ┌──────────────┴────────────────┐   ┌───────────┴─────────────────────────┐
   │ bebop CLI (offline)           │   │ bebop-rs/  (Rust crate)             │
   │ bebop live (standalone)       │   │ ───────────                         │
   │                               │   │ - PyO3 bindings into Python         │
   │ uses bebop/ directly via      │   │ - C FFI for the C++ AU shell        │
   │ normal `import bebop`         │   │ - Phase A CLI binary                │
   │                               │   │ - 5 test suites                     │
   └───────────────────────────────┘   └─────────────────────────────────────┘
                                                   ▲
                                                   │ static link:
                                                   │ libbebop_rs.a
                                                   │
                                       ┌───────────┴─────────────────────────┐
                                       │ bebop-au/  (C++ AUv2 plugin)        │
                                       │ ──────────                          │
                                       │ - AUEffectBase subclass             │
                                       │ - real-time audio + MIDI            │
                                       │ - SPSC ring buffers                 │
                                       │ - 4 integration test suites         │
                                       └─────────────────────────────────────┘
                                                   ▲
                                                   │ ~/Library/Audio/Plug-Ins/
                                                   │ Components/bebop.component
                                                   │
                                       ┌───────────┴─────────────────────────┐
                                       │  Logic Pro / GarageBand / any AU host
                                       └─────────────────────────────────────┘
```

The single-source-of-truth rule: any algorithmic decision (which voicing
notes to pick, which rhythm hit lands when, which substitution to apply) is
made by Python code in [bebop/](bebop/). Both the Rust FFI and the C++ AU
call into Python for these decisions; neither reimplements them.

## Repository layout

```
bebop/
├── bebop/                          # Python package (the algorithms)
│   ├── io/                         #   chord input: chart, MIDI, audio (CQT)
│   ├── voicing/                    #   rootless / evans / drop2 / quartal
│   ├── rhythm/                     #   12 rhythm templates
│   ├── reharm/                     #   substitution rules + spice knob
│   ├── render/                     #   write_midi, fluidsynth → WAV, HTML
│   └── live/                       #   `bebop live` standalone realtime mode
├── tests/
│   └── eval/                       # benchmarks (recognition / comp / coherence)
│       ├── synth.py                #   ground-truth fixtures
│       ├── recognition_bench.py    #   chord ID accuracy
│       ├── comp_bench.py           #   voicing properties (membership, lead)
│       ├── coherence_bench.py      #   chroma similarity comp vs song
│       └── run_all.py              #   orchestrator → output/eval/*.csv
│
├── bebop-rs/                       # Rust crate
│   ├── src/
│   │   ├── ffi.rs                  #   C ABI: init/audio/chord/MIDI/params
│   │   ├── python.rs               #   PyO3 bindings + venv path bootstrap
│   │   └── bin/bebop_cli.rs        #   Phase A CLI binary
│   ├── python/
│   │   ├── bebop_native_analyze.py #   embedded helper: samples → chord
│   │   └── bebop_native_comp.py    #   embedded helper: chord → MIDI events
│   ├── include/bebop_rs.h          #   hand-written C header
│   └── tests/                      #   PyO3 smoke / FFI smoke / chord recog /
│                                   #   comp generation / Rust↔Python diff
├── bebop-au/                       # C++ AUv2 plugin
│   ├── Sources/
│   │   ├── BebopAU.cpp             #   AU class + kernel + worker + MIDI
│   │   ├── AudioRingBuffer.h       #   SPSC: audio thread → worker
│   │   └── MidiEventQueue.h        #   SPSC: worker → audio thread
│   ├── Resources/Info.plist
│   ├── Tests/                      #   ring buffer / engine / v2 MIDI /
│   │                               #   params / fixture-driven
│   ├── vendor/AudioUnitSDK/        #   Apple's v2-bridge code
│   └── Makefile                    #   build / sign / install / validate
│
├── ARCHITECTURE.md                 # ← you are here
└── README.md                       # quick start + recipes
```

## The realtime / non-realtime separation

The AU plugin exists in two thread contexts. Understanding which work runs
where is the most important architectural concept:

```
┌────────────────────────────────────────────────────────────────────┐
│ AUDIO THREAD (real-time priority, sub-millisecond budget)          │
│ ─────────────────────────────────────────────────────────          │
│ - kernel.Process(in, out, frames)                                  │
│     memmove(out, in, frames)             ← passthrough audio       │
│     ringBuffer.write(in, frames)         ← lock-free, no alloc     │
│                                                                    │
│ - flushMidi() in ProcessBufferLists                                │
│     midiQueue.pop() in a loop            ← lock-free, no alloc     │
│     MIDIPacketListAdd to stack buffer    ← stack-allocated         │
│     host.midiOutputCallback(packetList)  ← short MIDI messages     │
│                                                                    │
│ NEVER does: malloc/new, lock, syscall, Python, anything that       │
│ could pause for unpredictable time.                                │
└────────────────────────────────────────────────────────────────────┘
              │ AudioRingBuffer        ▲ MidiEventQueue
              ▼ (SPSC, lock-free)      │ (SPSC, lock-free)
┌────────────────────────────────────────────────────────────────────┐
│ WORKER THREAD (non-realtime, ~10 Hz)                               │
│ ──────────────────────────────────                                 │
│ - sleep(100ms)                                                     │
│ - audio = ringBuffer.read(chunk)                                   │
│ - bebop_process_audio(audio):                                      │
│     - accumulate in Rust-side rolling buffer                       │
│     - if enough new audio: PyO3 call into Python                   │
│         - librosa CQT + chroma + template match                    │
│         - chord changed? → run comp_for_chord                      │
│           - voice_chord (rootless/evans/drop2/quartal)             │
│           - render_rhythm (12 templates)                           │
│           - reharm.reharmonize (spice 0..1)                        │
│         - schedule each event with wall-clock deadline             │
│ - bebop_pull_midi_events(events):                                  │
│     drain events whose deadline <= now                             │
│ - midiQueue.push(events)                                           │
└────────────────────────────────────────────────────────────────────┘
```

The two threads communicate exclusively through SPSC lock-free queues
([AudioRingBuffer.h](bebop-au/Sources/AudioRingBuffer.h) and
[MidiEventQueue.h](bebop-au/Sources/MidiEventQueue.h)). The worker thread
can take arbitrarily long without disturbing audio.

## Inherent latency

The recognition path takes ~1–2 seconds end-to-end:

| Stage | Time |
|---|---|
| Audio fills the rolling buffer | ~1.0 s (analysis_window) |
| Stability filter (2 consecutive analyses agree) | +0.3 s (analysis_period) |
| CQT + chord ID compute | ~50–100 ms |
| Worker → audio thread queue handoff | < 1 ms |
| Audio thread emits MIDI to host | < 1 ms |

Net: the comp lands ~1 beat behind each chord change at typical jazz tempos.
This is fundamental to chroma-based recognition; the only way to halve it
would be to switch to a learned model (basic-pitch, autochord — already
integrated as offline-only options) or add a beat-tracker.

The `live` Python implementation pre-bakes this into the disk-MIDI it
writes (back-dating events by the lag) so users who want a tighter take can
drag the .mid into Logic afterwards. The AU plugin doesn't do this — comp
fires when it fires.

## Phase progression

| Phase | Scope | Status |
|---|---|---|
| **A** | Rust + PyO3 CLI binary that drives bebop's Python pipeline; differential test against the existing offline pipeline | done |
| **B** | Empty AU shell loads in `auval` + `AVAudioEngine` (passthrough only, no FFI yet) | done |
| **C** | Wire B's render block to A's Rust core via FFI; SPSC queues; worker thread; full comp generation (voicing + rhythm + reharm); 4 AU parameters | done |
| **D** | Drop the AU in Logic; iterate on Logic-specific quirks (UI, automation, freezing, latency reporting) | pending |

## What's autonomously testable

| Layer | Tests | Runs without |
|---|---|---|
| Python algorithms | `tests/eval/run_all.py` (recognition / comp / coherence benches) | anything |
| Python CLI | implicit via `differential.rs` | anything |
| Rust → Python FFI | `cargo test --release` (5 test files) | Python-aware build env only |
| C++ ring buffer | `make test-rb` (5 unit tests + 100k-sample concurrent stress) | the AU bundle |
| AU framework | `make validate` (`auval -strict`) | anything besides macOS + AU SDK |
| AU + AVAudioEngine | `make test-engine` | anything besides macOS |
| AU + MIDI output | `make test-v2midi` (direct AUv2 callback path) | anything besides macOS |
| AU parameters | `make test-params` (round-trip + propagation) | anything besides macOS |
| End-to-end fixture | `make test-fixture` (audio in → captured MIDI vs ground truth) | anything besides macOS |
| **Logic-specific** | needs a human to drop the AU in Logic | Logic |

The "Logic-specific" gap is roughly 5% of the surface area: per-track
latency reporting, parameter automation curves, project-level freezing /
bounce-in-place, the plugin browser display, and various Logic edge cases
that `auval` doesn't exercise.

## Cross-layer guarantees

These are properties the test suite enforces:

1. **The Rust FFI and the Python CLI produce bit-stable identical comp
   MIDI** for the same inputs ([bebop-rs/tests/differential.rs](bebop-rs/tests/differential.rs)).
2. **The AU's emitted MIDI matches what bebop-rs produces** for the same
   audio fixture ([bebop-au/Tests/au_fixture_test.swift](bebop-au/Tests/au_fixture_test.swift)).
3. **The AU's parameters, when changed, alter the comp output** in
   measurable ways ([bebop-au/Tests/au_params_test.swift](bebop-au/Tests/au_params_test.swift)).
4. **The Python eval bench's metrics don't regress** across changes
   ([tests/eval/run_all.py --diff baseline](tests/eval/run_all.py)).

Together these mean: a change to bebop's algorithms (anywhere) is detected
by at least one layer's tests. A change to the FFI is detected by the
differential and fixture tests. A change to the AU bundle/render/MIDI path
is detected by the AU integration tests.

## How to extend

If you want to add a new feature, the right layer to start in:

| Want to add | Layer | Notes |
|---|---|---|
| New chord recognizer (e.g. a deep model) | Python (`bebop/io/`) | Plug into the ensemble voter; the rest of the stack picks it up automatically |
| New voicing style | Python (`bebop/voicing/`) | Add to the voicings registry; expose by index in the AU's voicing parameter |
| New rhythm template | Python (`bebop/rhythm/`) | Add to `TEMPLATES` dict; show up automatically in `all_rhythm_names()` |
| New reharm substitution | Python (`bebop/reharm/`) | Add to the substitution pipeline; the spice knob exposes it |
| Auto-rhythm-from-audio | Python (new module) + AU param | Use `librosa.beat.tempo` + groove analysis; add a parameter index that means "auto" |
| Sample-accurate rhythm timing | C++ (`BebopAU.cpp`) | Currently events fire at the next render block; would require scheduling with sample offsets |
| Real-time AU UI | Swift (new) | Phase D work; build a Cocoa view via `kAudioUnitProperty_CocoaUI` |

The general rule: **add to Python first**. The eval bench validates it.
Then expose to the AU via a parameter or FFI extension only if there's an
audible reason to.

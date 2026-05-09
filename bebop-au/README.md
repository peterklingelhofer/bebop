# bebop-au

C++ AUv2 Audio Unit plugin. Listens to audio on whatever bus the host
inserts it on, runs real-time chord recognition + comp generation through
the embedded Python interpreter (via [bebop-rs](../bebop-rs)), and emits
MIDI to whatever destination the host is configured to route the AU's
MIDI output to.

This is the deliverable of Phase A → B → C. The plugin is fully wired:
chord recognition → voicing → rhythm hits → MIDI events. What's left is
Phase D, where you (the human) drop it into Logic and listen.

## Build, install, validate

```bash
make            # full pipeline: SDK build → compile → bundle → sign → install → auval
make build      # just compile + bundle (no install)
make sdk        # build the vendored AudioUnitSDK static lib (one-time)
make install    # copy .component to ~/Library/Audio/Plug-Ins/Components/
make validate   # arch -arm64 auval -strict -v aufx BBOP Bbop
make test       # build + install + run the AVAudioEngine + v2-MIDI tests
make clean      # remove build/ (keeps the vendored SDK)
```

## What this validates

`auval -strict -v aufx BBOP Bbop` runs the same battery of compliance
checks Logic uses internally before loading a plugin:

- Format negotiation (mono/stereo, multiple sample rates: 11025–192000 Hz)
- Parameter tree validity
- Render-block correctness (multiple block sizes 64–4096 frames)
- Channel handling (1-1, 2-2, 4-4, 6-6, 7-7, 8-8 mappings)
- Bypass behavior, class info, host callbacks
- MIDI output capability declaration
- Bad-max-frames stress

The integration tests in [Tests/](Tests/) go further:

- [au_engine_test.swift](Tests/au_engine_test.swift) — loads the AU into
  `AVAudioEngine` (the API Logic uses) and renders 2 s of audio offline,
  asserting the audio path stays clean (no NaNs, peak amplitude matches
  passthrough expectations).
- [au_v2_midi_test.swift](Tests/au_v2_midi_test.swift) — exercises the
  raw AUv2 C API directly (the path Logic actually uses for MIDI output):
  registers a `MIDIOutputCallback`, renders audio through the AU, and
  asserts the captured MIDI looks like real comp output (bass + multiple
  piano voicing notes per chord).
- [ring_buffer_test.cpp](Tests/ring_buffer_test.cpp) — SPSC ring buffer
  unit tests including a 100k-sample concurrent producer/consumer stress.

This is the closest thing to a Logic integration test that doesn't need
Logic. The remaining 5% is Logic-specific UI/automation/freeze-bounce
behavior that only Logic itself exercises.

## Stack

| Layer | Tech |
|-------|------|
| Audio Unit base class | Apple `AudioUnitSDK` (`ausdk::AUEffectBase`) |
| Plugin source | C++23 ([Sources/BebopAU.cpp](Sources/BebopAU.cpp)) |
| Realtime audio thread | Per-channel kernel + lock-free SPSC ring (writer) |
| Worker thread (non-realtime) | Pulls audio, calls `bebop-rs` for chord recognition + comp generation, queues MIDI events |
| Realtime MIDI delivery | `kAudioUnitProperty_MIDIOutputCallback` from the audio thread, drained from the lock-free MIDI queue |
| Bundle format | AUv2 `.component`, ad-hoc signed |

We picked AUv2 + AUSDK over AUv3 `.appex` because:

- AUv2 isn't sandboxed, which keeps embedded Python viable
  (AUv3's sandbox makes embedded interpreters fragile).
- AUSDK ships the v2 component-manager bridge code we'd otherwise have
  to write ourselves (~hundreds of lines of selector dispatch).
- Logic loads both formats; v2 is still the dominant production format.

## File layout

```
bebop-au/
├── Sources/
│   ├── BebopAU.cpp            # AU class + kernel + worker + MIDI plumbing
│   ├── AudioRingBuffer.h      # SPSC: audio thread → worker
│   └── MidiEventQueue.h       # SPSC: worker → audio thread
├── Resources/
│   └── Info.plist             # AudioComponent registration
├── Tests/
│   ├── au_engine_test.swift   # AVAudioEngine integration
│   ├── au_v2_midi_test.swift  # direct AUv2 + MIDI capture
│   └── ring_buffer_test.cpp   # SPSC unit tests
├── vendor/
│   └── AudioUnitSDK/          # vendored Apple SDK
├── Makefile                   # build + install + validate pipeline
├── build/                     # gitignored
└── README.md
```

## Realtime / non-realtime separation

The audio thread runs on a hard real-time priority. Anything that allocates,
locks, or makes a syscall there causes audible glitches. The plugin keeps
the audio thread purely mechanical:

```
audio thread (real-time):
    kernel.Process(in, out, frames)
        memmove(out, in, frames)              ← passthrough audio
        ring.write(in, frames)                ← lock-free, no alloc
    flushMidi()
        midi_queue.pop() in a loop            ← lock-free, no alloc
        MIDIPacketListAdd to stack buffer     ← stack allocated
        host MIDIOutputCallback(packetList)   ← short MIDI messages

worker thread (non-realtime, ~10 Hz):
    audio = ring.read(chunk)
    bebop_process_audio(audio)                ← Python interpreter, GIL,
                                                CQT, chord recognition,
                                                voicing, rhythm scheduling
    bebop_pull_midi_events(events)
    for ev in events: midi_queue.push(ev)
```

The two threads communicate exclusively through SPSC lock-free queues
([AudioRingBuffer.h](Sources/AudioRingBuffer.h) and
[MidiEventQueue.h](Sources/MidiEventQueue.h)). The worker thread can take
arbitrarily long without disturbing audio.

## Plugin metadata

| Field | Value |
|-------|-------|
| Type | `aufx` (Audio Effect — passthrough audio + side-channel MIDI output) |
| Subtype | `BBOP` |
| Manufacturer | `Bbop` |
| Display name | "Bbop: bebop comp" |
| MIDI channels | piano on ch 1, bass on ch 2 |

Logic users insert the AU on a bus, route the AU's MIDI output to
whichever instrument track they want comping (the AU emits piano on ch 1
and bass on ch 2 — split tracks via the channel filter, or send both to a
single multi-timbral instrument).

## Phase A → B → C → D progression

| Phase | Scope | Status |
|-------|-------|--------|
| A | Rust + PyO3 CLI binary that drives bebop's Python pipeline | done |
| B | Empty AU shell loads in auval + AVAudioEngine | done |
| C | Wire B's render block to A's Rust core via FFI; add MIDI output; full comp generation (voicing + rhythm) | done |
| D | Drop the AU in Logic; iterate on Logic-specific quirks | pending |

See [ARCHITECTURE.md](../ARCHITECTURE.md) at the project root for the full
picture across all three crates.

# bebop-rs

Native Rust core for the bebop AU plugin. Embeds Python via PyO3, wraps the
existing `bebop` Python package behind a stable C ABI, and is statically
linked into the C++ AU shell at [bebop-au/](../bebop-au/).

The crate has two roles:

1. **Phase A reference binary**: a CLI (`bebop_cli`) that drives bebop's
   Python pipeline end-to-end and writes a comp .mid. Used to differentially
   test the FFI against the Python implementation.

2. **C ABI for the C++ AU shell**: the FFI in [src/ffi.rs](src/ffi.rs)
   exposes chord recognition + comp-event generation as plain C functions
   linkable from C/C++/Swift. `bebop-au` links the resulting `libbebop_rs.a`
   and calls it from a worker thread.

## C ABI surface

Declared in [include/bebop_rs.h](include/bebop_rs.h); implemented in
[src/ffi.rs](src/ffi.rs). All callable from any non-realtime thread; not
realtime-safe (Python interpreter, GIL acquisition, allocation).

| Function | Role |
|---|---|
| `bebop_init` / `bebop_destroy` | Lifecycle. Spins up the embedded Python interpreter and imports `bebop`. |
| `bebop_last_error` | Per-thread last-error for diagnostics. |
| `bebop_process_audio(handle, samples, n, sr)` | Push a chunk of mono audio. Internally accumulates a rolling buffer and runs CQT chord recognition once per `ANALYSIS_PERIOD_SECONDS`. New chords trigger comp-event generation (voicing + rhythm) and queue results for `bebop_pull_midi_events`. |
| `bebop_pull_chord(handle, out, len)` | Drain the most recently detected chord symbol. |
| `bebop_pull_midi_events(handle, out, max)` | Drain MIDI events whose deadlines have passed. The C++ AU's worker polls this. |
| `bebop_set_param(handle, param, value)` | Set BPM / voicing / rhythm / spice. |

The `BebopParam` enum in the header lists supported parameters; voicings are
indexed (0=rootless, 1=evans, 2=drop2, 3=quartal) and rhythms are indexed
into `bebop.rhythm.all_rhythm_names()`.

## Build and test

```bash
# build everything (lib, staticlib, bebop_cli binary)
cargo build --release

# full test suite — runs single-threaded because PyO3 auto-initialize
# can't tolerate concurrent Python interpreter init
cargo test --release

# individual tests with stdout
cargo test --release --test python_smoke -- --nocapture
cargo test --release --test ffi_smoke -- --nocapture
cargo test --release --test ffi_chord_recognition -- --nocapture
cargo test --release --test ffi_comp_generation -- --nocapture
cargo test --release --test differential -- --nocapture
```

The crate's [.cargo/config.toml](.cargo/config.toml) pins:

- `PYO3_PYTHON` to the project's uv-managed venv at
  `/Users/peterklingelhofer/Dev/bebop/.venv/bin/python3` so PyO3 links
  against the right libpython and the embedded interpreter sees bebop.
- `RUST_TEST_THREADS=1` because PyO3's auto-initialize feature can't tolerate
  concurrent first-call attempts from multiple test threads.

## CLI binary (Phase A)

```bash
cargo run --release --bin bebop_cli -- \
    --in ../output/eval/ii_V_I_C.wav \
    --out /tmp/comp.mid \
    --bpm 100 \
    --spice 0.0 \
    --voicing rootless \
    --rhythm charleston
```

Equivalent to running `uv run bebop ...` against the same WAV. Used by the
[differential test](tests/differential.rs) which renders the same fixture
through both pipelines and asserts the MIDI is bit-stable identical.

## Tests

| Test | What it validates |
|---|---|
| [python_smoke.rs](tests/python_smoke.rs) | PyO3 init succeeds, `import bebop` resolves, can call into submodules. |
| [ffi_smoke.rs](tests/ffi_smoke.rs) | A C program can link `libbebop_rs.a` and call `bebop_init/process_audio/destroy` cleanly. |
| [ffi_chord_recognition.rs](tests/ffi_chord_recognition.rs) | Feeds an eval fixture WAV through the FFI and asserts the captured chord roots match ground truth (Dm, G, C for ii-V-I). |
| [ffi_comp_generation.rs](tests/ffi_comp_generation.rs) | Feeds an eval fixture and asserts the FFI produces voiced comp events: bass + multiple piano notes per chord, distinct pitch classes spanning the progression. |
| [differential.rs](tests/differential.rs) | Renders 5 ground-truth fixtures through both the Python CLI and the Rust pipeline; asserts the comp MIDI is bit-stable identical (modulo track-name metadata). |

## File layout

```
bebop-rs/
├── Cargo.toml
├── .cargo/config.toml         # PYO3_PYTHON pin, single-threaded test config
├── src/
│   ├── lib.rs                 # public API surface
│   ├── python.rs              # PyO3 bindings into bebop.{io,reharm,render}
│   ├── ffi.rs                 # C ABI: handle lifecycle, audio pipeline,
│   │                          # comp-event scheduler, parameter dispatch
│   ├── wav.rs                 # WAV → mono f32 helper
│   ├── midi.rs                # canonicalized MIDI note loader (for diffs)
│   └── bin/
│       └── bebop_cli.rs       # Phase A reference binary
├── python/
│   ├── bebop_native_analyze.py  # samples → chord symbol via existing
│   │                             # bebop.io.audio_in primitives
│   └── bebop_native_comp.py    # chord symbol → scheduled MIDI events via
│                               # existing bebop.voicing + bebop.rhythm
├── include/
│   └── bebop_rs.h              # hand-written C header (cbindgen-free)
└── tests/
    ├── python_smoke.rs
    ├── ffi_smoke.rs            # + tests/ffi_smoke.c (C program)
    ├── ffi_chord_recognition.rs
    ├── ffi_comp_generation.rs
    └── differential.rs
```

## Position in the project

```
bebop/                          ← Python package (chord parsing, voicing,
                                  rhythm, reharm — the source of truth for
                                  every algorithmic choice)
    │
    ▼ (PyO3 calls into bebop's existing public API + two thin helper
       scripts in bebop-rs/python/ that flatten its async live API)
    │
bebop-rs/                       ← THIS CRATE
    │   - Phase A: CLI binary that drives the Python pipeline
    │   - Phase C: stable C ABI for the C++ AU shell
    │
    ▼ (static link: libbebop_rs.a)
    │
bebop-au/                       ← C++ AUv2 plugin (this is what Logic loads)
    │
    ▼ (.component bundle in ~/Library/Audio/Plug-Ins/Components/)
    │
Logic / GarageBand / any AU host
```

Each layer is independently testable. See [ARCHITECTURE.md](../ARCHITECTURE.md)
in the project root for the full picture.

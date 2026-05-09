//! End-to-end test of the FFI chord-recognition path:
//!
//!   1. Open one of the eval fixtures (`output/eval/ii_V_I_C.wav`)
//!   2. Feed it to `bebop_process_audio` in 1024-sample chunks (mimicking
//!      the C++ AU's worker thread cadence)
//!   3. Drain `bebop_pull_chord` between calls
//!   4. Assert the captured chord sequence's roots match ground truth
//!
//! Same shape as `tests/eval/recognition_bench.py`'s `score_live`, but
//! through the C ABI — verifies the embedded-Python pipeline produces
//! the same chord sequence whether we call it from Python or from C
//! (and therefore from the C++ AU).

use std::ffi::{c_char, CStr};
use std::path::Path;

use bebop_rs::ffi::{bebop_destroy, bebop_init, bebop_process_audio, bebop_pull_chord};
use bebop_rs::wav::load_mono_f32;

fn pull_chord_string(handle: *mut bebop_rs::ffi::BebopHandle) -> Option<String> {
    let mut buf = [0u8; 64];
    let n = unsafe {
        bebop_pull_chord(
            handle,
            buf.as_mut_ptr() as *mut c_char,
            buf.len(),
        )
    };
    if n <= 0 {
        return None;
    }
    let cs = unsafe { CStr::from_ptr(buf.as_ptr() as *const c_char) };
    Some(cs.to_string_lossy().into_owned())
}

/// Map any chord-symbol the recognizer might emit to its root pitch class.
/// Mirrors `tests/eval/synth.py::chord_root_pc` so we score the same way.
fn chord_root_pc(symbol: &str) -> Option<u8> {
    if symbol.is_empty() {
        return None;
    }
    let bytes = symbol.as_bytes();
    let (root_chars, _suffix) = if bytes.len() >= 2 && (bytes[1] == b'#' || bytes[1] == b'b') {
        (&symbol[..2], &symbol[2..])
    } else {
        (&symbol[..1], &symbol[1..])
    };
    let normalized = match root_chars {
        "Db" => "C#", "Eb" => "D#", "Gb" => "F#", "Ab" => "G#", "Bb" => "A#",
        s => s,
    };
    let names = [
        "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
    ];
    names.iter().position(|n| *n == normalized).map(|i| i as u8)
}

#[test]
fn ffi_recognizes_chord_progression_from_eval_fixture() {
    // Locate the fixture.
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let wav = manifest.join("../output/eval/ii_V_I_C.wav");
    assert!(
        wav.exists(),
        "missing fixture {} — run the Phase A bench first to render it",
        wav.display()
    );

    // ii_V_I_C: Dm7 G7 Cmaj7 Cmaj7. Root sequence on chord boundaries:
    // expect at least Dm, G, C in the captured stream. (The recognizer
    // emits triads, not full 7ths — same chroma ceiling we documented
    // in tests/eval/recognition_bench.py.)
    let expected_roots = ["D", "G", "C"];

    let (samples, sr) = load_mono_f32(&wav).expect("load wav");
    eprintln!("[ffi-chord] loaded {} samples @ {} Hz", samples.len(), sr);

    let handle = bebop_init();
    assert!(!handle.is_null(), "bebop_init returned null");

    // Feed in 1024-sample chunks (the same block size the AU's audio
    // thread uses) so the cadence is realistic.
    let mut captured: Vec<String> = Vec::new();
    let chunk_size = 1024usize;
    let mut pos = 0;
    while pos < samples.len() {
        let end = (pos + chunk_size).min(samples.len());
        let rc = unsafe {
            bebop_process_audio(
                handle,
                samples[pos..end].as_ptr(),
                end - pos,
                sr as f32,
            )
        };
        assert_eq!(rc, 0, "bebop_process_audio failed at pos={pos}");
        pos = end;
        if let Some(c) = pull_chord_string(handle) {
            captured.push(c);
        }
    }
    // One last drain in case a chord was detected on the final pass.
    if let Some(c) = pull_chord_string(handle) {
        captured.push(c);
    }
    unsafe { bebop_destroy(handle) };

    eprintln!("[ffi-chord] captured: {captured:?}");
    assert!(
        !captured.is_empty(),
        "no chords detected — pipeline broken"
    );

    // Root-only check: every expected root should appear at least once
    // in the captured sequence (in any order; the bench's score_live
    // does proper alignment, but for FFI smoke we just want to confirm
    // chord-recognition is alive and producing the right roots).
    let captured_roots: Vec<u8> = captured
        .iter()
        .filter_map(|s| chord_root_pc(s))
        .collect();
    for expected in expected_roots {
        let pc = chord_root_pc(expected).unwrap();
        assert!(
            captured_roots.contains(&pc),
            "expected root {expected} (pc={pc}) not in captured {captured:?}"
        );
    }
}

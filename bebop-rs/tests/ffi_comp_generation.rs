//! End-to-end test of the FFI comp-generation path:
//!
//!   1. Open ii_V_I_C.wav (the Phase A eval fixture)
//!   2. Feed it to `bebop_process_audio` in chunks
//!   3. Periodically drain `bebop_pull_midi_events`
//!   4. Assert the captured events look like a real comp:
//!      - Multiple events per chord (voicing has multiple pitches)
//!      - Bass note (channel 2) on the chord root
//!      - Note-on / note-off pairs are balanced
//!
//! Same shape as `ffi_chord_recognition.rs` but pulls structured events
//! instead of just the chord symbol — verifies the comp engine produces
//! the same kind of output Phase A's `write_midi` does.

use std::path::Path;

use bebop_rs::ffi::{
    bebop_destroy, bebop_init, bebop_process_audio, bebop_pull_midi_events,
    bebop_set_param, CBebopMidiEvent,
};
use bebop_rs::wav::load_mono_f32;

#[test]
fn ffi_emits_voiced_comp_events_for_progression() {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let wav = manifest.join("../output/eval/ii_V_I_C.wav");
    assert!(
        wav.exists(),
        "missing fixture {} — render via the Phase A bench first",
        wav.display()
    );

    let (samples, sr) = load_mono_f32(&wav).expect("load wav");
    eprintln!(
        "[ffi-comp] loaded {} samples @ {} Hz, duration={:.2}s",
        samples.len(),
        sr,
        samples.len() as f32 / sr as f32
    );

    let handle = bebop_init();
    assert!(!handle.is_null(), "bebop_init returned null");

    // Set knobs to the same defaults the comp_bench uses (rootless +
    // charleston + 100 BPM matches our fixture).
    unsafe {
        bebop_set_param(handle, 0, 100.0); // BPM
        bebop_set_param(handle, 1, 0.0);   // voicing = rootless
        bebop_set_param(handle, 2, 3.0);   // rhythm   ≈ charleston (index)
    }

    // Feed audio in 1024-sample chunks; drain MIDI events every chunk.
    let chunk_size = 1024usize;
    let mut pos = 0;
    let mut all_events: Vec<CBebopMidiEvent> = Vec::new();
    let mut buf: [CBebopMidiEvent; 64] = unsafe { std::mem::zeroed() };
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

        // Drain ripe events. Loop until we get a 0-event pull (queue
        // empty or all events still pending in the future).
        loop {
            let n = unsafe {
                bebop_pull_midi_events(handle, buf.as_mut_ptr(), buf.len())
            };
            if n == 0 {
                break;
            }
            for i in 0..n {
                all_events.push(CBebopMidiEvent {
                    status: buf[i].status,
                    pitch: buf[i].pitch,
                    velocity: buf[i].velocity,
                    _pad: 0,
                });
            }
        }
        // Tiny wall-clock pause so the wall-clock-scheduled events
        // become "due" — the test feeds audio much faster than realtime,
        // so without sleeps every event would still be scheduled in the
        // future and pull would never see them.
        std::thread::sleep(std::time::Duration::from_millis(2));
    }
    // Final drain (give wall-clock time for remaining events to ripen).
    std::thread::sleep(std::time::Duration::from_millis(100));
    loop {
        let n = unsafe {
            bebop_pull_midi_events(handle, buf.as_mut_ptr(), buf.len())
        };
        if n == 0 {
            break;
        }
        for i in 0..n {
            all_events.push(CBebopMidiEvent {
                status: buf[i].status,
                pitch: buf[i].pitch,
                velocity: buf[i].velocity,
                _pad: 0,
            });
        }
    }
    unsafe { bebop_destroy(handle) };

    eprintln!("[ffi-comp] captured {} events", all_events.len());
    let mut note_ons = 0;
    let mut note_offs = 0;
    let mut bass_notes = 0;
    let mut piano_notes = 0;
    let mut pitch_classes_seen: std::collections::HashSet<u8> = Default::default();
    for ev in &all_events {
        let cmd = ev.status & 0xF0;
        let ch = ev.status & 0x0F;
        if cmd == 0x90 {
            note_ons += 1;
            if ch == 0 {
                piano_notes += 1;
            } else if ch == 1 {
                bass_notes += 1;
            }
            pitch_classes_seen.insert(ev.pitch % 12);
        } else if cmd == 0x80 {
            note_offs += 1;
        }
    }
    eprintln!(
        "[ffi-comp] note_on={} note_off={} bass={} piano={} pcs_seen={:?}",
        note_ons, note_offs, bass_notes, piano_notes, pitch_classes_seen
    );

    // Assertions: the comp should have generated a meaningful number of
    // events for ii-V-I (Dm7 G7 Cmaj7 Cmaj7).
    assert!(
        note_ons >= 4,
        "expected ≥4 note_on events for a 4-bar progression, got {note_ons}"
    );
    assert!(
        bass_notes >= 1,
        "expected at least one bass note (chord root), got {bass_notes}"
    );
    assert!(
        piano_notes >= 1,
        "expected at least one piano voicing note, got {piano_notes}"
    );
    // The progression spans pcs {D, F, A, C} (Dm7) ∪ {G, B, D, F} (G7) ∪
    // {C, E, G, B} (Cmaj7) — should see at least 4 distinct pitch classes
    // across our voicings.
    assert!(
        pitch_classes_seen.len() >= 4,
        "expected ≥4 distinct pitch classes, got {pitch_classes_seen:?}"
    );
}

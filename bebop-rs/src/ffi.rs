//! C FFI surface for the bebop-rs core.
//!
//! Consumed by the C++ AU shell in [`../../bebop-au`](../../bebop-au) via the
//! companion header at `include/bebop_rs.h`. Functions here are called from
//! the AU's worker thread (NEVER from the realtime audio thread) — they
//! initialize the Python interpreter, run chord recognition, and produce
//! MIDI events that the AU's render block then dispatches to the host.
//!
//! Phase C scaffolding (this file's current scope):
//!   - `bebop_init` / `bebop_destroy` — interpreter lifecycle
//!   - `bebop_last_error` — last-error reporting for the C++ side
//!
//! Phase C v2 will add:
//!   - `bebop_process_audio` (push samples for analysis)
//!   - `bebop_pull_midi` (drain queued MIDI events)
//!   - `bebop_set_param` (live knobs)

use std::cell::RefCell;
use std::collections::VecDeque;
use std::ffi::{c_char, c_int, CString};
use std::panic::AssertUnwindSafe;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::python::ensure_venv_on_path;

/// Opaque handle the C++ side carries around.
///
/// Holds a rolling audio buffer (~5 seconds) plus the latest detected chord
/// and the gating state for the analysis cadence. Mutex-guarded — the C++
/// AU's worker thread is the sole caller in production, but the Mutex is
/// cheap and keeps things robust.
pub struct BebopHandle {
    state: Mutex<BebopState>,
}

struct BebopState {
    /// Rolling mono audio at the host's sample rate. Truncated to
    /// `MAX_BUFFER_SECONDS * sample_rate` after each push.
    audio: VecDeque<f32>,
    /// Sample rate of the audio above. Tracks whatever the host hands us.
    sample_rate: f32,
    /// Total samples received since init — drives the analysis cadence
    /// gate (run analysis once per `ANALYSIS_PERIOD_SAMPLES` of new audio).
    samples_received: u64,
    /// Total samples received as of the last analysis pass.
    last_analysis_at: u64,
    /// Most recent committed chord (set by `analyze_now`).
    last_chord: Option<String>,
    /// Most recent detected key (used as a prior for next analysis).
    last_key: String,

    /// Stability filter state. Each analysis pass returns a candidate
    /// chord; we only COMMIT when the same chord has been the candidate
    /// for `STABILITY_FRAMES` consecutive analyses. Filters out the
    /// single-frame oscillations between similar chords (Cmaj7 ↔ C6 ↔ C)
    /// that chroma is prone to.
    candidate_chord: Option<String>,
    candidate_count: u32,

    /// Comp-engine state.
    /// Most recently committed chord that's been turned into MIDI events.
    /// Distinct from `last_chord` (which is the "raw recognizer output"
    /// gated by `bebop_pull_chord`). When this differs from `last_chord`,
    /// we run the comp generator on the new chord.
    last_comped_chord: Option<String>,
    /// Last voicing the comp engine produced — fed back into `comp_for_chord`
    /// for voice-leading on the next call.
    last_voicing_bass: Option<i32>,
    last_voicing_pitches: Option<Vec<i32>>,
    /// Pending comp events with absolute wall-clock deadlines. Sorted by
    /// `due_at`. The C++ side polls `bebop_pull_midi_events` to drain
    /// events whose deadline has passed.
    pending: VecDeque<PendingMidiEvent>,
    /// Pending events scheduled by host beat (used when `rhythm_sync` is
    /// on). The C++ worker drains these unconditionally via
    /// `bebop_drain_beat_events` and forwards into a lockfree SPSC
    /// queue; the audio thread maintains the time-sorted view and emits
    /// when current_beat crosses each event's `due_at_beat`
    pending_beat: VecDeque<PendingBeatEvent>,
    /// Monotonic counter incremented on every comp burst. Tagged onto
    /// emitted beat events so the audio thread can cheaply discard a
    /// previous chord's stale events when a new chord lands
    comp_gen: u32,
    /// Current knob values (writable from C++ via bebop_set_param).
    bpm: f32,
    voicing: String,
    rhythm: String,
    spice: f32,
    /// Octave shift for emitted MIDI pitches; applied at comp generation.
    /// Range typically -3..+3.
    octave_shift: i32,
    /// When true, comp events are delayed to align with the host's bar
    /// grid instead of firing at chord-onset wall-clock. Updated by the
    /// AU param surface; consumed at comp-generation time
    rhythm_sync: bool,
    /// Latest host beat snapshot pushed by the AU shell. Used (with
    /// `taken_at`) to extrapolate the current host beat at comp time
    /// for bar-grid alignment when `rhythm_sync` is on
    host_beat: Option<HostBeat>,
    /// Voicing-update queue for sync mode. Comp generation pushes the
    /// just-computed voicing here; the AU's worker drains it via
    /// `bebop_drain_voicing_updates` and forwards to the audio thread,
    /// which uses it to emit current-voicing pitches at each rhythm hit
    voicing_pending: VecDeque<PendingVoicing>,
    /// Monotonic gen counter for voicing updates (lets the audio thread
    /// detect "no new voicing" cheaply)
    voicing_gen: u32,
}

#[derive(Clone, Copy)]
struct HostBeat {
    beat: f64,
    bpm: f64,
    taken_at: Instant,
}

#[derive(Clone)]
struct PendingMidiEvent {
    due_at: Instant,
    status: u8,
    pitch: u8,
    velocity: u8,
}

/// Voicing snapshot enqueued on each comp commit when sync mode is on.
/// The AU shell's worker drains these and forwards to the audio thread,
/// which uses them to emit current-voicing pitches at each rhythm hit
#[derive(Clone)]
struct PendingVoicing {
    bass_pitch: i32,        // -1 if no bass
    chord_pitches: Vec<u8>, // 0..7 pitches typically
    gen_id: u32,
}

/// C-ABI mirror of [`PendingVoicing`]. Layout MUST match `BebopVoicing`
/// in `include/bebop_rs.h`. Fixed-size buffer (7 pitches max + 1 pad)
/// keeps the FFI struct trivially copyable. 16 bytes total
#[repr(C)]
pub struct CBebopVoicing {
    pub gen_id:      u32,    // monotonic; advances on each comp commit
    pub bass_pitch:  i8,     // -1 if no bass
    pub pitch_count: u8,     // 0..7
    pub _pad:        [u8; 2],
    pub pitches:     [u8; 8], // up to 7 used; pitch_count is authoritative
}

#[derive(Clone)]
struct PendingBeatEvent {
    due_at_beat: f64,
    status: u8,
    pitch: u8,
    velocity: u8,
    /// Comp-burst generation id. The C++ audio thread tracks the highest
    /// gen it's seen and discards any locally-buffered events from older
    /// gens — this is how we cancel a previous chord's beat-scheduled
    /// events without locking the audio thread on the Rust mutex
    gen_id: u32,
}

/// C-ABI mirror of [`PendingBeatEvent`]. Layout MUST match the
/// `BebopBeatEvent` struct in `include/bebop_rs.h`. 24 bytes
/// (struct alignment forces an 8-byte gap before due_at_beat)
#[repr(C)]
pub struct CBebopBeatEvent {
    pub status: u8,
    pub pitch: u8,
    pub velocity: u8,
    pub _pad: u8,
    pub gen_id: u32,
    pub due_at_beat: f64,
}

/// Mirror of the C struct returned to the C++ side. The fields and layout
/// must match `BebopMidiEvent` in `include/bebop_rs.h`.
#[repr(C)]
pub struct CBebopMidiEvent {
    pub status: u8,
    pub pitch: u8,
    pub velocity: u8,
    pub _pad: u8,
}

const MAX_BUFFER_SECONDS: f32 = 5.0;
const ANALYSIS_WINDOW_SECONDS: f32 = 1.0;
const ANALYSIS_PERIOD_SECONDS: f32 = 0.3;
/// Number of consecutive analyses that must agree before committing a
/// new chord. Higher = more stable but adds (N-1) * ANALYSIS_PERIOD to
/// total recognition latency. Eval bench shows N=3 → ~100% triad accuracy.
const STABILITY_FRAMES: u32 = 3;

/// Bar length in beats. 4/4 only for now (matches everywhere else)
const BEATS_PER_BAR: f64 = 4.0;

/// Worker→audio handoff lag estimate, in beats. Conservatively covers
/// `bebop_drain_beat_events` poll latency (~100 ms) + audio block
/// latency (~12 ms) at the slowest realistic project tempo. Used as a
/// safety margin in [`sync_beat_anchor`] so the anchor we pick can't
/// slip into the past by audio fire time.
///
/// At 60 BPM, 0.5 beats = 500 ms of headroom. At 240 BPM, 0.5 beats =
/// 125 ms. Either way well above worker poll latency
const ANCHOR_SAFETY_BEATS: f64 = 0.5;

/// Compute the bar-aligned anchor beat at which the next comp should
/// start. Returns `Some(anchor_beat)` when sync is on AND we have a
/// usable host-beat snapshot, else `None` (caller falls back to
/// wall-clock scheduling).
///
/// Uses ONLY host-beat math (no PDC subtraction) — the audio thread
/// fires beat-scheduled events when current_beat crosses each event's
/// due_at_beat, so timing accuracy is bounded by the audio thread, not
/// by the worker poll cadence
fn sync_beat_anchor(
    rhythm_sync: bool,
    host_beat: Option<HostBeat>,
) -> Option<f64> {
    if !rhythm_sync { return None; }
    let hb = host_beat?;
    if hb.bpm <= 0.0 { return None; }
    let spb = 60.0 / hb.bpm;
    let now_beat = hb.beat + hb.taken_at.elapsed().as_secs_f64() / spb;
    // Next bar boundary at or after (now + safety). The safety margin
    // absorbs worker→audio propagation lag — without it, an anchor
    // computed right before a bar boundary can land in the audio
    // thread's past, causing the comp to fire immediately at sample
    // offset 0 instead of waiting for the next clean bar
    let target = now_beat + ANCHOR_SAFETY_BEATS;
    let next_bar_beat = (target / BEATS_PER_BAR).ceil() * BEATS_PER_BAR;
    Some(next_bar_beat)
}

thread_local! {
    /// Buffer for the most recent error message from this thread, kept
    /// alive across the FFI call so C can read it via `bebop_last_error()`.
    static LAST_ERROR: RefCell<Option<CString>> = RefCell::new(None);
}

fn record_error<E: std::fmt::Display>(e: E) {
    let s = format!("{}", e);
    let cs = CString::new(s).unwrap_or_else(|_| CString::new("<bad utf8>").unwrap());
    LAST_ERROR.with(|slot| *slot.borrow_mut() = Some(cs));
}

/// Initialize the embedded Python interpreter and ensure the bebop venv is
/// reachable. Returns an opaque handle on success, NULL on failure (the
/// C++ side can call `bebop_last_error()` for diagnostic text).
///
/// Safe to call from any thread, but the resulting handle is NOT thread-
/// safe; the AU shell must serialize access through its worker queue.
///
/// # Safety
/// The returned pointer must be released with [`bebop_destroy`].
#[no_mangle]
pub extern "C" fn bebop_init() -> *mut BebopHandle {
    let r = std::panic::catch_unwind(AssertUnwindSafe(|| -> PyResult<()> {
        Python::with_gil(|py| ensure_venv_on_path(py))?;
        // Pre-import bebop on this thread so Phase-C runtime calls don't
        // pay the import cost on the worker thread's first iteration.
        Python::with_gil(|py| -> PyResult<()> {
            let _ = py.import_bound("bebop")?;
            Ok(())
        })?;
        Ok(())
    }));
    match r {
        Ok(Ok(())) => {
            let handle = Box::new(BebopHandle {
                state: Mutex::new(BebopState {
                    audio: VecDeque::new(),
                    sample_rate: 0.0,
                    samples_received: 0,
                    last_analysis_at: 0,
                    last_chord: None,
                    last_key: "C".to_string(),
                    candidate_chord: None,
                    candidate_count: 0,
                    last_comped_chord: None,
                    last_voicing_bass: None,
                    last_voicing_pitches: None,
                    pending: VecDeque::new(),
                    pending_beat: VecDeque::new(),
                    comp_gen: 0,
                    bpm: 120.0,
                    voicing: "rootless".to_string(),
                    rhythm: "charleston".to_string(),
                    spice: 0.0,
                    octave_shift: 0,
                    rhythm_sync: false,
                    host_beat: None,
                    voicing_pending: VecDeque::new(),
                    voicing_gen: 0,
                }),
            });
            Box::into_raw(handle)
        }
        Ok(Err(e)) => {
            record_error(e);
            std::ptr::null_mut()
        }
        Err(panic) => {
            let msg = panic
                .downcast_ref::<&'static str>()
                .map(|s| s.to_string())
                .or_else(|| panic.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "panic during bebop_init".to_string());
            record_error(msg);
            std::ptr::null_mut()
        }
    }
}

/// Release a handle obtained from [`bebop_init`]. Safe to pass NULL.
///
/// # Safety
/// The handle must have come from `bebop_init` and must not be used after
/// this returns.
#[no_mangle]
pub unsafe extern "C" fn bebop_destroy(handle: *mut BebopHandle) {
    if handle.is_null() {
        return;
    }
    drop(Box::from_raw(handle));
}

/// Return the last error message produced on this thread, or NULL if none.
/// The returned pointer is valid until the next FFI call on this thread.
#[no_mangle]
pub extern "C" fn bebop_last_error() -> *const c_char {
    LAST_ERROR.with(|slot| match slot.borrow().as_ref() {
        Some(cs) => cs.as_ptr(),
        None => std::ptr::null(),
    })
}

/// Push a chunk of mono audio samples into the analysis buffer.
/// Once enough audio has accumulated since the last analysis pass, runs
/// chord recognition (CQT + chroma + template matching) via the embedded
/// Python interpreter. Newly detected chords are buffered; the C++ side
/// drains them via [`bebop_pull_chord`].
///
/// Returns 0 on success, non-zero on error.
///
/// # Safety
/// `samples` must point to `n_samples` `f32` values readable for the
/// duration of the call. `handle` must be a live handle from [`bebop_init`].
#[no_mangle]
pub unsafe extern "C" fn bebop_process_audio(
    handle: *mut BebopHandle,
    samples: *const f32,
    n_samples: usize,
    sample_rate: f32,
) -> c_int {
    if handle.is_null() {
        record_error("bebop_process_audio: null handle");
        return -1;
    }
    if samples.is_null() || n_samples == 0 {
        return 0;
    }
    let handle: &BebopHandle = &*handle;
    let slice = std::slice::from_raw_parts(samples, n_samples);

    let r = std::panic::catch_unwind(AssertUnwindSafe(|| -> PyResult<()> {
        // Phase 1: ingest new audio under lock.
        {
            let mut state = handle.state.lock().expect("BebopHandle state lock");
            state.sample_rate = sample_rate;
            state.audio.extend(slice.iter().copied());

            // Cap the buffer at MAX_BUFFER_SECONDS — drop the oldest samples
            // if we're over. Common in practice the C++ worker drains at
            // ~10 Hz so the buffer rarely exceeds 1.5 seconds, but bursty
            // upstream (test fixtures, CPU contention) can stuff multiple
            // seconds in one call; the cap protects against unbounded growth.
            let max_samples = (MAX_BUFFER_SECONDS * sample_rate) as usize;
            while state.audio.len() > max_samples {
                state.audio.pop_front();
            }

            state.samples_received = state
                .samples_received
                .saturating_add(n_samples as u64);
        }

        let period_samples = (ANALYSIS_PERIOD_SECONDS * sample_rate) as u64;
        let window_samples = (ANALYSIS_WINDOW_SECONDS * sample_rate) as usize;

        // Cadence loop — run an analysis at every period-boundary crossed by
        // this batch of audio. Steady real-time flow loops once; a bursty
        // worker that just drained a 1-2 s backlog loops several times so
        // the stability filter sees one prediction per period instead of
        // a single prediction over the entire backlog (which would skip
        // chord transitions and starve STABILITY_FRAMES)
        loop {
            // Phase 2: pick the next analysis target + extract its window.
            let (window, last_key) = {
                let mut state = handle.state.lock().expect("BebopHandle state lock");
                if state.audio.len() < window_samples {
                    return Ok(()); // not enough audio yet
                }
                let target_at = state.last_analysis_at.saturating_add(period_samples);
                if target_at > state.samples_received {
                    return Ok(()); // no new period boundary reached
                }
                // Map target_at (absolute sample index) to a buffer offset.
                let oldest_pos = state.samples_received - state.audio.len() as u64;
                let win_end = (target_at - oldest_pos) as usize;
                if win_end < window_samples {
                    // Window starts before our oldest retained sample — we
                    // can't reconstruct it. Skip past this period boundary
                    state.last_analysis_at = target_at;
                    continue;
                }
                let win_start = win_end - window_samples;
                let window: Vec<f32> = state
                    .audio
                    .iter()
                    .skip(win_start)
                    .take(window_samples)
                    .copied()
                    .collect();
                state.last_analysis_at = target_at;
                (window, state.last_key.clone())
            };

            // Phase 3: run Python analysis without the state lock held —
            // takes ~50 ms; don't block other FFI callers.
            let analysis = Python::with_gil(|py| -> PyResult<Option<(String, String)>> {
                ensure_venv_on_path(py)?;
                run_analysis(py, &window, sample_rate, &last_key)
            })?;

            let Some((chord, key)) = analysis else { continue; };

            // Phase 4: stability filter + commit decision under lock.
            //
            // Stability filter — eliminate single-frame chord oscillations.
            // The recognizer might return Cmaj7, then C6, then Cmaj7, then C
            // across consecutive 0.3s analyses of the same audio region (this
            // is chroma ambiguity, not real chord changes). We require N
            // consecutive analyses to agree on the same chord before we
            // commit. Total recognition latency:
            //   ANALYSIS_WINDOW_SECONDS + (STABILITY_FRAMES - 1) * ANALYSIS_PERIOD_SECONDS
            //   = 1.0 + 2 * 0.3 = 1.6 s
            // PDC latency in the AU is set to match this so MIDI lands
            // sample-aligned with chord positions in the master mix
            let comp_args = {
                let mut state = handle.state.lock().expect("BebopHandle state lock");
                state.last_key = key;

                if state.candidate_chord.as_deref() == Some(chord.as_str()) {
                    state.candidate_count = state.candidate_count.saturating_add(1);
                } else {
                    state.candidate_chord = Some(chord.clone());
                    state.candidate_count = 1;
                }

                let stable = state.candidate_count >= STABILITY_FRAMES;
                let new_commit = state.last_chord.as_deref() != Some(chord.as_str());
                if !stable || !new_commit {
                    continue; // candidate not confirmed yet, OR same as last commit
                }

                // Stable + new — promote the candidate to the committed chord.
                state.last_chord = Some(chord.clone());

                // Generate comp events ONCE per distinct chord — re-detecting
                // the same chord doesn't re-emit a fresh round of voicings.
                if state.last_comped_chord.as_deref() == Some(chord.as_str()) {
                    continue;
                }

                Some((
                    state.bpm,
                    state.voicing.clone(),
                    state.rhythm.clone(),
                    state.spice,
                    state.octave_shift,
                    state.last_voicing_bass,
                    state.last_voicing_pitches.clone(),
                ))
            };

            // Phase 5: generate comp events without the state lock held.
            let Some((bpm, voicing, rhythm, spice, octave_shift, prev_bass, prev_pitches)) =
                comp_args else { continue; };
            let comp = Python::with_gil(|py| -> PyResult<Option<CompResult>> {
                run_comp(py, &chord, bpm, &voicing, &rhythm, spice,
                         octave_shift, prev_bass, prev_pitches.as_deref())
            })?;

            // Phase 6: cancel pending events from the previous chord and
            // schedule the new chord's events under lock.
            if let Some(c) = comp {
                let mut state = handle.state.lock().expect("BebopHandle state lock");
                let now = Instant::now();

                // Cancel previous chord's still-pending events. Two cases
                // per pending note_off:
                //
                //   - Matching note_on STILL in pending → neither has
                //     fired yet. The note never sounded. Drop both.
                //   - Matching note_on already drained → the note IS
                //     currently sounding (note_on fired, note_off in
                //     the future). Force the note_off to fire NOW so
                //     the previous chord's bass / piano notes don't ring
                //     through the new chord's start
                //
                // We collapse beat-scheduled cancellations into wall-clock
                // immediate releases so they fire right away regardless of
                // the host's beat position — note_off should never wait
                let old_wall: Vec<PendingMidiEvent> =
                    state.pending.drain(..).collect();
                let old_beat: Vec<PendingBeatEvent> =
                    state.pending_beat.drain(..).collect();
                let mut next_pending: VecDeque<PendingMidiEvent> =
                    VecDeque::new();
                for ev in &old_wall {
                    if (ev.status & 0xF0) != 0x80 { continue; }
                    let on_still_pending = old_wall.iter().any(|other| {
                        (other.status & 0xF0) == 0x90
                            && (other.status & 0x0F) == (ev.status & 0x0F)
                            && other.pitch == ev.pitch
                    });
                    if on_still_pending { continue; }
                    next_pending.push_back(PendingMidiEvent {
                        due_at: now,
                        status: ev.status,
                        pitch: ev.pitch,
                        velocity: ev.velocity,
                    });
                }
                for ev in &old_beat {
                    if (ev.status & 0xF0) != 0x80 { continue; }
                    let on_still_pending = old_beat.iter().any(|other| {
                        (other.status & 0xF0) == 0x90
                            && (other.status & 0x0F) == (ev.status & 0x0F)
                            && other.pitch == ev.pitch
                    });
                    if on_still_pending { continue; }
                    next_pending.push_back(PendingMidiEvent {
                        due_at: now,
                        status: ev.status,
                        pitch: ev.pitch,
                        velocity: ev.velocity,
                    });
                }

                // Schedule the new chord's events. If sync is on AND we
                // have a host-beat snapshot, route through pending_beat
                // (audio thread emits sample-accurately). Otherwise use
                // the wall-clock pending queue (existing behavior).
                let beat_anchor = sync_beat_anchor(
                    state.rhythm_sync, state.host_beat);
                if let Some(_anchor_beat) = beat_anchor {
                    // Sync mode: the audio thread drives rhythm hits from
                    // the rhythm template + current voicing. Rust just
                    // publishes the latest voicing — no per-event
                    // scheduling. This decouples chord recognition from
                    // rhythm timing: a chord change updates "what plays
                    // on the next hit" without disturbing the rhythm phase
                    //
                    // Drop any wall-clock note_off cancels we collected
                    // (the audio thread emits its own note_offs at each
                    // hit transition based on what it's currently
                    // playing) — keeping them would double-release notes
                    next_pending.clear();
                    state.pending.clear();
                    state.pending_beat.clear();

                    state.voicing_gen = state.voicing_gen.wrapping_add(1);
                    let gen_id = state.voicing_gen;
                    state.voicing_pending.push_back(PendingVoicing {
                        bass_pitch: c.bass_pitch,
                        chord_pitches: c.chord_pitches.iter()
                            .map(|&p| (p as i32).clamp(0, 127) as u8)
                            .collect(),
                        gen_id,
                    });
                } else {
                    // Wall-clock path (current behavior)
                    for (t_seconds, status, pitch, vel) in c.events {
                        next_pending.push_back(PendingMidiEvent {
                            due_at: now + Duration::from_secs_f64(
                                t_seconds.max(0.0)),
                            status,
                            pitch,
                            velocity: vel,
                        });
                    }
                    let mut as_vec: Vec<PendingMidiEvent> = next_pending.into();
                    as_vec.sort_by_key(|e| e.due_at);
                    state.pending = as_vec.into();
                }
                state.last_voicing_bass = Some(c.bass_pitch);
                state.last_voicing_pitches = Some(c.chord_pitches);
                state.last_comped_chord = Some(chord);
            }
        }
    }));
    match r {
        Ok(Ok(())) => 0,
        Ok(Err(e)) => {
            record_error(e);
            -2
        }
        Err(_) => {
            record_error("panic in bebop_process_audio");
            -3
        }
    }
}

/// Pull the most recently detected chord. If no new chord since the last
/// call to this function, copies an empty string and returns 0. Otherwise
/// copies the chord symbol (NUL-terminated) into `out` (capped at `out_len`)
/// and returns the number of bytes written (excluding the trailing NUL).
///
/// # Safety
/// `out` must point to at least `out_len` writable bytes.
#[no_mangle]
pub unsafe extern "C" fn bebop_pull_chord(
    handle: *mut BebopHandle,
    out: *mut c_char,
    out_len: usize,
) -> c_int {
    if handle.is_null() || out.is_null() || out_len == 0 {
        return 0;
    }
    let handle: &BebopHandle = &*handle;
    let mut state = match handle.state.lock() {
        Ok(s) => s,
        Err(_) => return 0,
    };
    let chord = match state.last_chord.take() {
        Some(c) => c,
        None => {
            *out = 0;
            return 0;
        }
    };
    let bytes = chord.as_bytes();
    let n = bytes.len().min(out_len.saturating_sub(1));
    std::ptr::copy_nonoverlapping(bytes.as_ptr() as *const c_char, out, n);
    *out.add(n) = 0; // NUL terminator
    n as c_int
}

/// Result of `comp_for_chord` Python helper — the events to schedule plus
/// the voicing pitches we threaded through (so the next call can do
/// voice-leading).
struct CompResult {
    events: Vec<(f64, u8, u8, u8)>, // (time_seconds, status, pitch, velocity)
    bass_pitch: i32,
    chord_pitches: Vec<i32>,
}

/// Pull the most recently detected chord. If no new chord since the last
/// call to this function, copies an empty string and returns 0. Otherwise
/// copies the chord symbol (NUL-terminated) into `out` (capped at `out_len`)
/// and returns the number of bytes written (excluding the trailing NUL).
fn run_comp(
    py: Python<'_>,
    chord_symbol: &str,
    bpm: f32,
    voicing: &str,
    rhythm: &str,
    spice: f32,
    octave_shift: i32,
    prev_bass: Option<i32>,
    prev_pitches: Option<&[i32]>,
) -> PyResult<Option<CompResult>> {
    let helper_src = include_str!("../python/bebop_native_comp.py");
    let module = PyModule::from_code_bound(
        py,
        helper_src,
        "bebop_native_comp.py",
        "bebop_native_comp",
    )?;
    let kwargs = PyDict::new_bound(py);
    kwargs.set_item("bpm", bpm as f64)?;
    kwargs.set_item("voicing", voicing)?;
    kwargs.set_item("rhythm", rhythm)?;
    // 1 bar of comp per chord change. Was 4 bars but that left long
    // hanging tails — bass would sustain 16 beats (~10s at 100 BPM) on
    // any chord that didn't quickly transition, making recordings look
    // "perpetual." 1 bar releases cleanly and the next chord change
    // (or the panic flush on transport stop) restarts the comp
    kwargs.set_item("n_bars", 1i64)?;
    kwargs.set_item("spice", spice as f64)?;
    kwargs.set_item("octave_shift", octave_shift)?;
    if let Some(b) = prev_bass {
        kwargs.set_item("prev_bass_pitch", b)?;
    }
    if let Some(p) = prev_pitches {
        kwargs.set_item("prev_chord_pitches", p)?;
    }
    let result = module
        .getattr("comp_for_chord")?
        .call((chord_symbol,), Some(&kwargs))?;
    // Returns (events, bass_pitch, chord_pitches)
    let tuple: (Vec<(f64, i64, i64, i64)>, i32, Vec<i32>) = result.extract()?;
    let events = tuple
        .0
        .into_iter()
        .map(|(t, st, p, v)| (t, st as u8, p as u8, v as u8))
        .collect();
    Ok(Some(CompResult {
        events,
        bass_pitch: tuple.1,
        chord_pitches: tuple.2,
    }))
}

/// Drain MIDI events whose deadlines have passed. Writes up to `max_events`
/// into `out` and returns the count actually written.
///
/// # Safety
/// `out` must point to at least `max_events` `CBebopMidiEvent` slots.
#[no_mangle]
pub unsafe extern "C" fn bebop_pull_midi_events(
    handle: *mut BebopHandle,
    out: *mut CBebopMidiEvent,
    max_events: usize,
) -> usize {
    if handle.is_null() || out.is_null() || max_events == 0 {
        return 0;
    }
    let handle: &BebopHandle = &*handle;
    let mut state = match handle.state.lock() {
        Ok(s) => s,
        Err(_) => return 0,
    };
    let now = Instant::now();
    let mut count = 0;
    while count < max_events {
        let due = match state.pending.front() {
            Some(ev) if ev.due_at <= now => true,
            _ => false,
        };
        if !due {
            break;
        }
        let ev = state.pending.pop_front().unwrap();
        let dst = out.add(count);
        (*dst).status = ev.status;
        (*dst).pitch = ev.pitch;
        (*dst).velocity = ev.velocity;
        (*dst)._pad = 0;
        count += 1;
    }
    count
}

/// Panic-flush: drain ALL pending events (wall-clock AND beat-scheduled),
/// returning note_offs for any currently-sounding notes (note_on already
/// drained, note_off still pending). Note_offs whose paired note_on hasn't
/// fired yet are dropped (the note never sounded — no need to release it).
///
/// Called by the AU shell on transport-stop edge so the recording region
/// captures real note_offs before Logic stops capturing.
///
/// # Safety
/// `out` must point to at least `max_events` `CBebopMidiEvent` slots.
#[no_mangle]
pub unsafe extern "C" fn bebop_panic_flush(
    handle: *mut BebopHandle,
    out: *mut CBebopMidiEvent,
    max_events: usize,
) -> usize {
    if handle.is_null() || out.is_null() || max_events == 0 {
        return 0;
    }
    let handle: &BebopHandle = &*handle;
    let mut state = match handle.state.lock() {
        Ok(s) => s,
        Err(_) => return 0,
    };
    let old_wall: Vec<PendingMidiEvent> = state.pending.drain(..).collect();
    let old_beat: Vec<PendingBeatEvent> = state.pending_beat.drain(..).collect();
    let mut count = 0;

    let mut emit = |status: u8, pitch: u8, velocity: u8| {
        if count >= max_events { return false; }
        let dst = out.add(count);
        (*dst).status = status;
        (*dst).pitch = pitch;
        (*dst).velocity = velocity;
        (*dst)._pad = 0;
        count += 1;
        true
    };

    for ev in &old_wall {
        if (ev.status & 0xF0) != 0x80 { continue; }
        let on_still_pending = old_wall.iter().any(|other| {
            (other.status & 0xF0) == 0x90
                && (other.status & 0x0F) == (ev.status & 0x0F)
                && other.pitch == ev.pitch
        });
        if on_still_pending { continue; }
        if !emit(ev.status, ev.pitch, ev.velocity) { break; }
    }
    for ev in &old_beat {
        if (ev.status & 0xF0) != 0x80 { continue; }
        let on_still_pending = old_beat.iter().any(|other| {
            (other.status & 0xF0) == 0x90
                && (other.status & 0x0F) == (ev.status & 0x0F)
                && other.pitch == ev.pitch
        });
        if on_still_pending { continue; }
        if !emit(ev.status, ev.pitch, ev.velocity) { break; }
    }
    // last_comped_chord stays set so we don't immediately re-comp the same
    // chord on play resume; the new chord recognition cycle will pick up
    // naturally
    count
}

/// Drain ALL pending beat-scheduled events. Each event carries its
/// `due_at_beat` and a `gen_id` (comp-burst generation counter — the
/// audio thread uses this to discard stale events from a previous chord
/// without locking).
///
/// The audio thread is the only one that knows the host beat at sample
/// resolution, so it owns the firing decision; this FFI just transports
/// the schedule across the worker→audio boundary
///
/// # Safety
/// `out` must point to at least `max_events` `CBebopBeatEvent` slots.
#[no_mangle]
pub unsafe extern "C" fn bebop_drain_beat_events(
    handle: *mut BebopHandle,
    out: *mut CBebopBeatEvent,
    max_events: usize,
) -> usize {
    if handle.is_null() || out.is_null() || max_events == 0 {
        return 0;
    }
    let handle: &BebopHandle = &*handle;
    let mut state = match handle.state.lock() {
        Ok(s) => s,
        Err(_) => return 0,
    };
    let mut count = 0;
    while count < max_events {
        let Some(ev) = state.pending_beat.pop_front() else { break; };
        let dst = out.add(count);
        (*dst).status = ev.status;
        (*dst).pitch = ev.pitch;
        (*dst).velocity = ev.velocity;
        (*dst)._pad = 0;
        (*dst).gen_id = ev.gen_id;
        (*dst).due_at_beat = ev.due_at_beat;
        count += 1;
    }
    count
}

/// Drain ALL pending voicing updates. Each carries the bass + chord
/// pitches a comp commit just produced, plus a monotonic gen id. The
/// AU's worker drains via this and forwards into a SPSC the audio
/// thread reads — the audio thread keeps the latest voicing as
/// "current" and emits its pitches at every rhythm hit.
///
/// Used only when Rhythm Sync is on; in free mode comp generation
/// schedules events directly via `bebop_pull_midi_events`.
///
/// # Safety
/// `out` must point to at least `max_events` `CBebopVoicing` slots.
#[no_mangle]
pub unsafe extern "C" fn bebop_drain_voicing_updates(
    handle: *mut BebopHandle,
    out: *mut CBebopVoicing,
    max_events: usize,
) -> usize {
    if handle.is_null() || out.is_null() || max_events == 0 {
        return 0;
    }
    let handle: &BebopHandle = &*handle;
    let mut state = match handle.state.lock() {
        Ok(s) => s,
        Err(_) => return 0,
    };
    let mut count = 0;
    while count < max_events {
        let Some(v) = state.voicing_pending.pop_front() else { break; };
        let dst = out.add(count);
        (*dst).gen_id      = v.gen_id;
        (*dst).bass_pitch  = v.bass_pitch.clamp(-1, 127) as i8;
        let n = v.chord_pitches.len().min(7);
        (*dst).pitch_count = n as u8;
        for i in 0..7 { (*dst).pitches[i] = 0; }
        (*dst).pitches[7]  = 0;
        for i in 0..n {
            (*dst).pitches[i] = v.chord_pitches[i];
        }
        (*dst)._pad = [0; 2];
        count += 1;
    }
    count
}

/// Parameter IDs for [`bebop_set_param`]. Must stay in sync with the C header.
#[repr(C)]
pub enum BebopParam {
    Bpm = 0,
    /// Voicing style: 0=rootless, 1=evans, 2=drop2, 3=quartal.
    Voicing = 1,
    /// Rhythm template (encoded by index in `bebop.rhythm.all_rhythm_names()`).
    Rhythm = 2,
    /// Reharm intensity 0..1.
    Spice = 3,
    /// Integer octave shift applied to all emitted MIDI pitches. Positive
    /// shifts up, negative down. Clamped at the Python helper to keep
    /// pitches in [0..127].
    OctaveShift = 4,
    /// When non-zero, comp events are delayed to land on the host's bar
    /// boundary instead of firing at chord-onset wall-clock. Requires
    /// `bebop_set_host_beat` to be called periodically with current
    /// host beat info.
    RhythmSync = 5,
}

/// Set a knob value. `param` is one of `BebopParam`'s values cast to int.
/// Type of `value` depends on the param: BPM is float, Voicing/Rhythm are
/// integer indices.
#[no_mangle]
pub unsafe extern "C" fn bebop_set_param(
    handle: *mut BebopHandle,
    param: c_int,
    value: f64,
) -> c_int {
    if handle.is_null() {
        return -1;
    }
    let handle: &BebopHandle = &*handle;
    let mut state = match handle.state.lock() {
        Ok(s) => s,
        Err(_) => return -1,
    };
    match param {
        0 => {
            state.bpm = (value as f32).clamp(40.0, 240.0);
            0
        }
        1 => {
            let names = ["rootless", "evans", "drop2", "quartal"];
            let idx = (value as usize).min(names.len() - 1);
            state.voicing = names[idx].to_string();
            0
        }
        2 => {
            // Resolve via the same Python helper that defines names —
            // do this lazily so we don't pay it on every set.
            let names = match Python::with_gil(|py| -> PyResult<Vec<String>> {
                ensure_venv_on_path(py)?;
                let m = py.import_bound("bebop.rhythm")?;
                let v: Vec<String> = m.getattr("all_rhythm_names")?
                    .call0()?
                    .extract()?;
                Ok(v)
            }) {
                Ok(n) => n,
                Err(_) => return -1,
            };
            let idx = (value as usize).min(names.len().saturating_sub(1));
            state.rhythm = names[idx].clone();
            0
        }
        3 => {
            state.spice = (value as f32).clamp(0.0, 1.0);
            0
        }
        4 => {
            state.octave_shift = (value as i32).clamp(-3, 3);
            0
        }
        5 => {
            state.rhythm_sync = value > 0.5;
            0
        }
        _ => -2,
    }
}

/// Push the host's current beat position into the FFI so the bar-grid
/// alignment logic can extrapolate the current beat at comp-generation
/// time. The AU shell calls this from the worker thread once per poll
/// using its cached `mBeatLastValue` + tempo. No-op when handle is null.
#[no_mangle]
pub unsafe extern "C" fn bebop_set_host_beat(
    handle: *mut BebopHandle,
    beat: f64,
    bpm: f64,
) -> c_int {
    if handle.is_null() { return -1; }
    let handle: &BebopHandle = &*handle;
    let mut state = match handle.state.lock() {
        Ok(s) => s,
        Err(_) => return -1,
    };
    state.host_beat = Some(HostBeat {
        beat,
        bpm,
        taken_at: Instant::now(),
    });
    0
}

/// Run one chord-recognition pass on a flat audio buffer. Returns
/// `Some((chord_symbol, key))` if a chord could be identified, `None`
/// if the audio was silent / unable to identify.
fn run_analysis(
    py: Python<'_>,
    samples: &[f32],
    sample_rate: f32,
    last_key: &str,
) -> PyResult<Option<(String, String)>> {
    // Convert samples to a Python list — we let numpy convert from there.
    // Slightly inefficient (a copy) but fine at our cadence (~3 Hz).
    // Phase C.5b can switch to numpy.frombuffer for zero-copy if needed.
    let samples_list = PyList::new_bound(py, samples.iter().map(|&s| s as f64));

    let helper_src = include_str!("../python/bebop_native_analyze.py");
    let module = PyModule::from_code_bound(
        py,
        helper_src,
        "bebop_native_analyze.py",
        "bebop_native_analyze",
    )?;
    let kwargs = PyDict::new_bound(py);
    kwargs.set_item("sample_rate", sample_rate as f64)?;
    kwargs.set_item("last_key", last_key)?;
    let result = module
        .getattr("analyze_block")?
        .call((samples_list,), Some(&kwargs))?;
    if result.is_none() {
        return Ok(None);
    }
    let (chord, key): (String, String) = result.extract()?;
    Ok(Some((chord, key)))
}

/* bebop-rs C FFI header.
 *
 * Hand-written to keep the build simple — no cbindgen dependency. Must
 * stay in lockstep with [src/ffi.rs](../src/ffi.rs); the Rust functions
 * declared `#[no_mangle] extern "C"` below are the canonical contract.
 *
 * Consumed by the C++ AU shell at ../../bebop-au/Sources/BebopAU.cpp.
 *
 * Threading: callable from any thread, but a single BebopHandle is NOT
 * thread-safe — the AU's worker queue must serialize access.
 *
 * Realtime safety: NONE of these functions are realtime-safe. The audio
 * thread must never call them; only the worker thread does.
 */
#ifndef BEBOP_RS_H
#define BEBOP_RS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Opaque handle. Allocated by bebop_init, freed by bebop_destroy. */
typedef struct BebopHandle BebopHandle;

/* Initialize the embedded Python interpreter, import bebop, and return
 * an opaque handle. Returns NULL on failure; bebop_last_error() then
 * yields a human-readable message. */
BebopHandle* bebop_init(void);

/* Release a handle. Safe to call with NULL. */
void bebop_destroy(BebopHandle* handle);

/* Last error from the calling thread, or NULL if none. The returned
 * C string remains valid until the next FFI call on this thread. */
const char* bebop_last_error(void);

/* Push a chunk of mono audio for chord recognition. bebop-rs accumulates
 * an internal rolling buffer (~5s) and re-runs analysis once per
 * ANALYSIS_PERIOD_SECONDS of new audio. Detected chords are queued for
 * `bebop_pull_chord` to consume.
 *
 * Returns 0 on success, non-zero on error (call bebop_last_error). */
int bebop_process_audio(BebopHandle* handle,
                        const float* samples,
                        size_t      n_samples,
                        float       sample_rate);

/* Pull the most recently detected chord. Returns the number of bytes
 * written to `out` (excluding the trailing NUL). Returns 0 if no new
 * chord since the last call (calling pulls and clears the slot —
 * subsequent calls return 0 until a new chord is detected).
 *
 * `out` must point to at least `out_len` writable bytes; the result
 * is NUL-terminated. */
int bebop_pull_chord(BebopHandle* handle, char* out, size_t out_len);

/* One scheduled MIDI event ready to be sent to the host. Layout must match
 * the Rust `CBebopMidiEvent` struct in src/ffi.rs. */
typedef struct {
    uint8_t status;     /* 0x9X = note_on (channel X+1), 0x8X = note_off, etc. */
    uint8_t pitch;      /* 0..127 */
    uint8_t velocity;   /* 0..127 */
    uint8_t _pad;
} BebopMidiEvent;

/* Drain MIDI events whose scheduled time has passed. Writes up to
 * `max_events` events into `out` and returns the count actually written.
 * The C++ AU's worker thread polls this; events are emitted on the
 * audio thread on the next render block. */
size_t bebop_pull_midi_events(BebopHandle* handle,
                               BebopMidiEvent* out,
                               size_t max_events);

/* Panic flush: drain ALL pending events and return note_offs for any
 * currently-sounding notes (note_on already drained, note_off still
 * pending). Note_offs whose paired note_on never fired are dropped.
 *
 * The AU shell calls this on transport-stop so the recording region
 * captures real note_offs before Logic stops capturing — without this,
 * a note_on near the end of the region pairs with a note_off scheduled
 * after stop and Logic's recorded MIDI shows the note hanging */
size_t bebop_panic_flush(BebopHandle* handle,
                         BebopMidiEvent* out,
                         size_t max_events);

/* Parameter IDs for `bebop_set_param`. */
enum BebopParam {
    BEBOP_PARAM_BPM           = 0,  /* float, BPM (clamped to 40..240) */
    BEBOP_PARAM_VOICING       = 1,  /* int 0..3: rootless | evans | drop2 | quartal */
    BEBOP_PARAM_RHYTHM        = 2,  /* int, index into all_rhythm_names() */
    BEBOP_PARAM_SPICE         = 3,  /* float 0..1, reharm intensity */
    BEBOP_PARAM_OCTAVE_SHIFT  = 4,  /* int -3..+3, octaves to shift emitted MIDI */
};

/* Set a knob value. Returns 0 on success, non-zero on error. */
int bebop_set_param(BebopHandle* handle, int param, double value);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* BEBOP_RS_H */

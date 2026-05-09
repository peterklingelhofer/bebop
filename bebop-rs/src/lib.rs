//! bebop-rs: native Rust core for the bebop AU plugin.
//!
//! Architecture
//! ------------
//! The `python` module embeds a Python interpreter (via PyO3) and exposes a
//! narrow API into the existing `bebop` Python package:
//!
//!   * [`process_wav`] — offline path: read a WAV, run it through bebop's
//!     chord recognition + comp engine, return MIDI events. Used by the
//!     [`crate::bin::bebop_cli`] binary and by Phase-A differential tests.
//!
//!   * (later) [`audio::RingBuffer`] + [`worker::Worker`] — live path:
//!     the audio thread writes samples; the worker thread pulls 1-second
//!     windows and asks Python to identify the chord. Same shape as
//!     `bebop/live/chord_stream.py` but driven from Rust so the AU shell
//!     can plug it in directly.
//!
//! Phase A scope: just the offline path is enough to validate that
//! embedded Python works end-to-end with bebop's existing code.

pub mod python;
pub mod wav;
pub mod midi;
pub mod ffi;

pub use python::{process_wav_offline, MidiHit};

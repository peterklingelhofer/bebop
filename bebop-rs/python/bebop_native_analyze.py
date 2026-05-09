"""Helper for the bebop-rs FFI's chord-recognition path.

Embedded into the Rust binary at compile time via `include_str!`. Defines
`analyze_block(samples, sample_rate, last_key)` which mirrors what
`bebop.live.chord_stream.analyze_ring` does — but takes a flat list of
samples instead of an `AudioCaptureRing`, since the buffering happens
on the Rust side.

Returns `(chord_symbol, key)` on success, `None` if the buffer is
silent or below the recognition threshold.
"""

from __future__ import annotations

import numpy as np

from bebop.io.audio_in import _identify_audio_chord, _cqt_chroma  # private but stable
from bebop.io.midi_in import detect_key

import librosa


_SILENCE_RMS = 0.005


def analyze_block(
    samples,
    *,
    sample_rate: float,
    last_key: str = "C",
    prefer_flats: bool = False,
) -> tuple[str, str] | None:
    y = np.asarray(samples, dtype=np.float32)
    if y.size < int(sample_rate * 0.25):
        return None

    rms = float(np.sqrt(np.mean(y * y) + 1e-12))
    if rms < _SILENCE_RMS:
        return None

    # HPSS is moderately expensive but matches what live.chord_stream does
    # — keeping the algorithm identical is the whole point of going through
    # the same Python code path.
    try:
        y_h = librosa.effects.harmonic(y, margin=2.0)
    except Exception:
        y_h = y

    chroma_full, bass_chroma = _cqt_chroma(y_h, int(sample_rate))
    if chroma_full.size == 0 or chroma_full.sum() < 1e-6:
        return None

    chroma_vec = chroma_full.mean(axis=1)
    bass_vec = bass_chroma.mean(axis=1)

    # Update key estimate from this block — same approach as ChordStream
    key, _conf = detect_key(chroma_vec)
    if not key:
        key = last_key

    ident = _identify_audio_chord(chroma_vec, bass_vec, key, prefer_flats)
    if ident is None:
        return None
    chord_symbol, _score = ident
    return (chord_symbol, key)

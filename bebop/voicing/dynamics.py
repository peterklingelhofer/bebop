"""Loudness envelope from an audio file, sampled per beat.

Used by the MIDI writer to scale comp velocities so the comp swells with the
song instead of playing at constant intensity. Computed via librosa RMS:
    1. load + downmix to mono
    2. compute frame-wise RMS at hop=512
    3. sample the RMS at each beat boundary
    4. smooth across `smooth_beats` for slow swell rather than per-hit jitter
    5. normalize to [0, 1]

Apply via `velocity_multiplier(envelope, beat) -> float` which maps the
normalized loudness to a multiplier in [_VEL_FLOOR, _VEL_CEIL].
"""

from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np

# Velocity multiplier range. We don't want full silence in quiet sections, so
# floor at 0.55. Ceiling at 1.2 keeps loud sections from clipping past 127.
_VEL_FLOOR = 0.55
_VEL_CEIL = 1.20


def compute_loudness_envelope(
    audio_path: str | Path,
    *,
    bpm: float,
    smooth_beats: float = 2.0,
    sr: int = 22050,
    hop: int = 512,
) -> list[float]:
    """Return per-beat normalized loudness in [0, 1] for the entire audio file."""
    y, _sr = librosa.load(str(audio_path), sr=sr, mono=True)
    if y.size == 0:
        return []
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]    # (n_frames,)
    seconds_per_frame = hop / _sr
    seconds_per_beat = 60.0 / bpm
    duration_seconds = len(y) / _sr
    n_beats = int(duration_seconds / seconds_per_beat) + 1

    env = np.zeros(n_beats, dtype=np.float32)
    for b in range(n_beats):
        f = int(b * seconds_per_beat / seconds_per_frame)
        if 0 <= f < len(rms):
            env[b] = rms[f]

    # smooth across `smooth_beats` window so we follow phrase-level dynamics, not transients
    smooth_n = max(1, int(round(smooth_beats)))
    if smooth_n > 1:
        kernel = np.ones(smooth_n, dtype=np.float32) / smooth_n
        env = np.convolve(env, kernel, mode="same")

    peak = env.max()
    if peak > 0:
        env = env / peak
    return [float(v) for v in env]


def velocity_multiplier(envelope: list[float] | None, beat: float) -> float:
    """Map the envelope value at `beat` to a velocity multiplier in [_VEL_FLOOR, _VEL_CEIL]."""
    if not envelope:
        return 1.0
    idx = int(beat)
    if idx < 0 or idx >= len(envelope):
        return 1.0
    n = envelope[idx]
    return _VEL_FLOOR + (_VEL_CEIL - _VEL_FLOOR) * n

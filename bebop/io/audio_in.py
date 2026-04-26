"""Audio (.wav) -> ChordSequence using librosa CQT chromagrams + HPSS + tuning.

Improvements over the v1 scipy-only recognizer:
    - Constant-Q transform (CQT) instead of linear-frequency STFT — pitch
      bins are equally spaced on a log scale, which matches musical intervals.
    - Harmonic-Percussive Source Separation (HPSS) before chromagram —
      strips the percussive transients (drums, plucks) that smear chroma.
    - Tuning detection (librosa.estimate_tuning) — songs that aren't pinned
      at A=440 still get accurate pitch-class assignment.
    - Bass-aware chord detection: a separate low-register chromagram identifies
      the most likely root, used as a strong prior in scoring.
    - Triad-over-sus bias: among nearly-equal scores, prefer triads, then 7ths,
      then sus chords (sus is often a misread of a triad with passing notes).
"""

from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np

from bebop.io.midi_in import (
    _PITCH_NAMES_FLAT,
    _PITCH_NAMES_SHARP,
    _TEMPLATES,
    _diatonic_pcs,
    detect_key,
)
from bebop.types import Chord, ChordSequence, KeyChange


def _load_audio_mono(path: str | Path, target_sr: int = 22050) -> tuple[np.ndarray, int]:
    y, sr = librosa.load(str(path), sr=target_sr, mono=True)
    return y, sr


def _cqt_chroma(y: np.ndarray, sr: int, hop_length: int = 512,
                fmin_hz: float = librosa.note_to_hz("C2"),
                bins_per_octave: int = 36, n_octaves: int = 6) -> tuple[np.ndarray, np.ndarray]:
    """Return (full_chroma, bass_chroma) — both shape (12, n_frames).

    `bass_chroma` only includes energy from the lowest 1.5 octaves (C2..F#3 area),
    which lets us identify the bass note for slash chords / inversions.
    """
    n_bins = bins_per_octave * n_octaves
    cqt = np.abs(librosa.cqt(y, sr=sr, hop_length=hop_length, fmin=fmin_hz,
                             n_bins=n_bins, bins_per_octave=bins_per_octave))
    # full chroma: fold all octaves
    chroma = librosa.feature.chroma_cqt(C=cqt, bins_per_octave=bins_per_octave,
                                        hop_length=hop_length)
    # bass chroma: only the lowest 1.5 octaves
    bass_bins = bins_per_octave * 3 // 2
    bass_cqt = cqt[:bass_bins]
    bass_chroma = librosa.feature.chroma_cqt(C=bass_cqt, bins_per_octave=bins_per_octave,
                                             hop_length=hop_length)
    return chroma, bass_chroma


def _harmonic_only(y: np.ndarray) -> np.ndarray:
    """Strip the percussive component (drums, transients) so chroma is cleaner."""
    return librosa.effects.harmonic(y, margin=4.0)


def _smooth_chroma(chroma: np.ndarray, window_frames: int = 9) -> np.ndarray:
    if window_frames < 2:
        return chroma
    pad = window_frames // 2
    padded = np.pad(chroma, ((0, 0), (pad, pad)), mode="edge")
    out = np.empty_like(chroma)
    for f in range(chroma.shape[1]):
        out[:, f] = np.median(padded[:, f:f + window_frames], axis=1)
    # renormalize per frame
    sums = out.sum(axis=0, keepdims=True)
    sums[sums < 1e-9] = 1.0
    return out / sums


# Triad-over-sus bias weights: applied to per-template scores.
# Order: maj/min/dim/aug triad > 7th chord > 6th > sus chord.
_TEMPLATE_PRIOR: dict[str, float] = {
    "": 0.04,        # bare triad
    "m": 0.04,
    "maj7": 0.025,
    "m7": 0.025,
    "7": 0.025,
    "m7b5": 0.02,
    "dim7": 0.015,
    "dim": 0.01,
    "aug": -0.01,    # aug is rare; only pick when the score really wins
    "6": 0.015,
    "m6": 0.015,
    "sus4": -0.02,   # actively penalize sus — usually a triad with a passing tone
    "sus2": -0.02,
}


def _chord_score_chroma(chroma_vec: np.ndarray, bass_vec: np.ndarray | None,
                        root: int, template: frozenset[int],
                        key_root_pc: int | None, key_is_major: bool,
                        suffix: str) -> float:
    """Score a (root, template) candidate against frame chroma + bass chroma."""
    template_pcs = np.zeros(12, dtype=np.float64)
    for iv in template:
        template_pcs[(root + iv) % 12] = 1.0
    template_pcs /= template_pcs.sum()

    a = chroma_vec / (np.linalg.norm(chroma_vec) + 1e-9)
    b = template_pcs / (np.linalg.norm(template_pcs) + 1e-9)
    score = float(np.dot(a, b))

    # bass-aware bonus: if the BASS chroma peaks at the proposed root, +bonus
    if bass_vec is not None and bass_vec.sum() > 1e-9:
        bass_normed = bass_vec / bass_vec.sum()
        bass_at_root = bass_normed[root]
        score += 0.18 * bass_at_root  # up to +0.18 if bass is fully on the root

    # template prior
    score += _TEMPLATE_PRIOR.get(suffix, 0.0)

    # key bias
    if key_root_pc is not None:
        diatonic = _diatonic_pcs(key_root_pc, key_is_major)
        if root in diatonic:
            score += 0.06
        if root == key_root_pc:
            score += 0.04
        if root == (key_root_pc + 7) % 12:
            score += 0.03

    return score


def _identify_audio_chord(chroma_vec: np.ndarray, bass_vec: np.ndarray | None,
                          key: str | None, prefer_flats: bool) -> tuple[str, float] | None:
    if float(chroma_vec.sum()) < 1e-6:
        return None

    key_root_pc: int | None = None
    key_is_major = True
    if key:
        is_minor = key.endswith("m")
        tonic_str = key[:-1] if is_minor else key
        try:
            from music21 import pitch as m21pitch
            key_root_pc = m21pitch.Pitch(tonic_str).pitchClass
            key_is_major = not is_minor
        except Exception:
            pass

    best: tuple[float, int, str] | None = None
    for root in range(12):
        for suffix, template in _TEMPLATES:
            # audio side: skip 9/11/13 — chroma can't disambiguate without melody
            if any(c.isdigit() and c not in "67" for c in suffix):
                continue
            score = _chord_score_chroma(chroma_vec, bass_vec, root, template,
                                        key_root_pc, key_is_major, suffix)
            if best is None or score > best[0]:
                best = (score, root, suffix)
    assert best is not None
    score, root_pc, suffix = best
    name_table = _PITCH_NAMES_FLAT if prefer_flats else _PITCH_NAMES_SHARP
    return f"{name_table[root_pc]}{suffix}", score


def _coalesce(chords: list[Chord]) -> list[Chord]:
    if not chords:
        return chords
    out = [chords[0]]
    for c in chords[1:]:
        prev = out[-1]
        if c.symbol == prev.symbol and abs(c.start_beat - prev.end_beat) < 1e-6:
            out[-1] = Chord(
                symbol=prev.symbol,
                start_beat=prev.start_beat,
                duration_beats=prev.duration_beats + c.duration_beats,
                confidence=min(prev.confidence, c.confidence),
            )
        else:
            out.append(c)
    return out


def parse_audio(
    path: str | Path,
    *,
    bpm: float,
    windows_per_bar: int = 2,
    beats_per_bar: int = 4,
    prefer_flats: bool = False,
    region_beats: float = 32.0,
    use_hpss: bool = True,
) -> ChordSequence:
    """Audio chord recognition with CQT chroma, HPSS, and tuning correction."""
    y, sr = _load_audio_mono(path)
    if use_hpss:
        y = _harmonic_only(y)

    chroma, bass_chroma = _cqt_chroma(y, sr)
    chroma = _smooth_chroma(chroma, window_frames=11)
    bass_chroma = _smooth_chroma(bass_chroma, window_frames=11)

    seconds_per_beat = 60.0 / bpm
    seconds_per_window = (beats_per_bar / windows_per_bar) * seconds_per_beat
    beats_per_window = beats_per_bar / windows_per_bar
    hop_seconds = 512 / sr
    n_frames = chroma.shape[1]
    duration_seconds = n_frames * hop_seconds

    region_seconds = region_beats * seconds_per_beat
    n_regions = max(1, int(duration_seconds / region_seconds) + 1)
    region_chroma = np.zeros((n_regions, 12), dtype=np.float64)
    for r in range(n_regions):
        f_start = int(r * region_seconds / hop_seconds)
        f_end = min(int((r + 1) * region_seconds / hop_seconds), n_frames)
        if f_end > f_start:
            region_chroma[r] = chroma[:, f_start:f_end].sum(axis=1)
    key_map: list[KeyChange] = []
    for r in range(n_regions):
        key, conf = detect_key(region_chroma[r])
        beat = r * region_beats
        if not key_map or (key_map[-1].key != key and conf > 0.45):
            key_map.append(KeyChange(start_beat=beat, key=key))

    n_windows = int(duration_seconds / seconds_per_window) + 1
    chords: list[Chord] = []
    for w in range(n_windows):
        w_start_sec = w * seconds_per_window
        w_end_sec = w_start_sec + seconds_per_window
        f_start = int(w_start_sec / hop_seconds)
        f_end = min(int(w_end_sec / hop_seconds), n_frames)
        if f_end <= f_start:
            continue
        chroma_vec = chroma[:, f_start:f_end].mean(axis=1)
        bass_vec = bass_chroma[:, f_start:f_end].mean(axis=1)
        win_beat = w * beats_per_window
        active_key = None
        for kc in key_map:
            if kc.start_beat <= win_beat:
                active_key = kc.key
            else:
                break
        ident = _identify_audio_chord(chroma_vec, bass_vec, active_key, prefer_flats=prefer_flats)
        if ident is None:
            continue
        sym, conf = ident
        chords.append(Chord(symbol=sym, start_beat=win_beat,
                            duration_beats=beats_per_window, confidence=conf))

    return ChordSequence(
        chords=_coalesce(chords),
        bpm=bpm,
        time_signature=(beats_per_bar, 4),
        key_map=key_map,
    )

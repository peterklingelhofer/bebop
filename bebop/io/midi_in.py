"""Polyphonic MIDI -> ChordSequence with robust noise filtering and key detection.

Pipeline:
    1. Load notes via pretty_midi.
    2. Filter noise: drop very-quiet notes, very-short notes, and notes in
       windows where the total sounding mass is too low.
    3. Detect key globally + per-region using Krumhansl-Schmuckler (pitch-class
       histogram correlated against major/minor key profiles).
    4. Per window, build a duration-weighted pitch-class histogram, identify
       a robust bass (median of bottom 3 pitches, not absolute lowest),
       and match against chord templates with a key-aware prior bonus.
    5. Coalesce adjacent identical chord segments.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pretty_midi

from bebop.types import Chord, ChordSequence, KeyChange

_PITCH_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_PITCH_NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

_TEMPLATES: list[tuple[str, frozenset[int]]] = [
    ("maj7", frozenset({0, 4, 7, 11})),
    ("m7", frozenset({0, 3, 7, 10})),
    ("7", frozenset({0, 4, 7, 10})),
    ("m7b5", frozenset({0, 3, 6, 10})),
    ("dim7", frozenset({0, 3, 6, 9})),
    ("6", frozenset({0, 4, 7, 9})),
    ("m6", frozenset({0, 3, 7, 9})),
    ("", frozenset({0, 4, 7})),
    ("m", frozenset({0, 3, 7})),
    ("dim", frozenset({0, 3, 6})),
    ("aug", frozenset({0, 4, 8})),
    ("sus4", frozenset({0, 5, 7})),
    ("sus2", frozenset({0, 2, 7})),
]

# Quality prior — sus chords are usually a triad with a passing tone, so we
# subtract from their score; richer chords (7ths, 6ths) get a small nudge to
# prefer them when they fit equally well as a bare triad.
_TEMPLATE_PRIOR: dict[str, float] = {
    "":     0.04,    # bare major triad
    "m":    0.04,    # bare minor triad
    "maj7": 0.025,
    "m7":   0.025,
    "7":    0.025,
    "m7b5": 0.02,
    "dim7": 0.015,
    "dim":  0.01,
    "aug": -0.01,
    "6":    0.015,
    "m6":   0.015,
    "sus4": -0.04,   # actively penalize — ringing sus chords are common acoustic guitar artifacts
    "sus2": -0.04,
}

# Krumhansl-Kessler 1982 key profiles, normalized.
_KRUMHANSL_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KRUMHANSL_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


@dataclass
class _Window:
    start_beat: float
    end_beat: float
    pitches: list[int] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    velocities: list[int] = field(default_factory=list)


def _pitch_class_name(pc: int, prefer_flats: bool) -> str:
    return (_PITCH_NAMES_FLAT if prefer_flats else _PITCH_NAMES_SHARP)[pc % 12]


def detect_key(pc_weights: np.ndarray) -> tuple[str, float]:
    """Return (key_string, confidence) using Krumhansl correlation.

    `pc_weights` is a length-12 array of pitch-class mass.
    Returns e.g. ("D", 0.78) for D major or ("Am", 0.72) for A minor.
    Confidence is the correlation of the winning key.
    """
    if pc_weights.sum() <= 0:
        return ("C", 0.0)
    pc_weights = pc_weights / pc_weights.sum()
    best_key, best_score = "C", -1e9
    for tonic in range(12):
        for is_minor, profile in ((False, _KRUMHANSL_MAJOR), (True, _KRUMHANSL_MINOR)):
            rotated = np.roll(profile, tonic)
            # Pearson correlation
            r = np.corrcoef(pc_weights, rotated)[0, 1]
            if r > best_score:
                best_score = r
                best_key = _PITCH_NAMES_SHARP[tonic] + ("m" if is_minor else "")
    return best_key, float(best_score)


def _chord_score(observed_pc_weights: Counter[float], root: int, template: frozenset[int],
                 key_root_pc: int | None, key_is_major: bool, suffix: str) -> float:
    """Weighted F1 with key, template, and quality priors."""
    template_pcs = {(root + iv) % 12 for iv in template}
    observed_pcs = {pc for pc, w in observed_pc_weights.items() if w > 0}
    if not observed_pcs or not template_pcs:
        return 0.0
    matched = template_pcs & observed_pcs
    matched_weight = sum(observed_pc_weights[pc] for pc in matched)
    total_observed = sum(observed_pc_weights.values())
    precision = matched_weight / total_observed
    recall = len(matched) / len(template_pcs)
    if precision + recall == 0:
        return 0.0
    f1 = 2 * precision * recall / (precision + recall)

    # extras penalty: pitch classes that are observed but not in the template
    extras = observed_pcs - template_pcs
    extras_weight = sum(observed_pc_weights[pc] for pc in extras)
    extras_penalty = 0.5 * (extras_weight / total_observed) if total_observed > 0 else 0.0

    # key-aware prior
    key_bonus = 0.0
    if key_root_pc is not None:
        diatonic = _diatonic_pcs(key_root_pc, key_is_major)
        if root in diatonic:
            key_bonus += 0.10
        if root == key_root_pc:
            key_bonus += 0.05
        if root == (key_root_pc + 7) % 12:
            key_bonus += 0.04
        if root == (key_root_pc + 5) % 12:
            key_bonus += 0.03

    # quality prior: prefer triads/7ths over sus
    quality_bonus = _TEMPLATE_PRIOR.get(suffix, 0.0)

    return f1 - extras_penalty + key_bonus + quality_bonus


def _diatonic_pcs(tonic: int, is_major: bool) -> frozenset[int]:
    intervals = (0, 2, 4, 5, 7, 9, 11) if is_major else (0, 2, 3, 5, 7, 8, 10)
    return frozenset((tonic + i) % 12 for i in intervals)


def _robust_bass(pitches: list[int]) -> int:
    """Return the modal pitch-class of the lowest few notes (rejects single-note drones)."""
    if not pitches:
        return 0
    sorted_p = sorted(pitches)
    # take bottom 25% (at least 1, at most 4) and pick most common pitch class
    take = max(1, min(4, len(sorted_p) // 4 or 1))
    bottom = sorted_p[:take]
    pc_counts: Counter[int] = Counter(p % 12 for p in bottom)
    return pc_counts.most_common(1)[0][0]


def _identify_chord(window: _Window, key: str | None,
                    prefer_flats: bool, min_mass: float) -> tuple[str, str | None, float] | None:
    """Return (symbol, bass, confidence) or None if window has insufficient mass."""
    if not window.pitches:
        return None

    # weight pitch classes by sounded duration AND velocity (loud + long = chord-defining)
    pc_weights: Counter[int] = Counter()
    total_mass = 0.0
    for pitch, dur, vel in zip(window.pitches, window.durations, window.velocities):
        weight = dur * (vel / 127.0)
        pc_weights[pitch % 12] += weight
        total_mass += weight

    if total_mass < min_mass:
        return None

    bass_pc = _robust_bass(window.pitches)

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
            score = _chord_score(pc_weights, root, template, key_root_pc, key_is_major, suffix)
            # bass-as-root nudge (smaller than v1 to avoid open-string drone bias)
            if root == bass_pc:
                score += 0.07
            if best is None or score > best[0]:
                best = (score, root, suffix)

    assert best is not None
    confidence, root_pc, suffix = best
    root_name = _pitch_class_name(root_pc, prefer_flats)
    symbol = f"{root_name}{suffix}"
    bass = _pitch_class_name(bass_pc, prefer_flats) if bass_pc != root_pc else None
    return symbol, bass, max(0.0, min(1.0, confidence))


def _coalesce(chords: list[Chord]) -> list[Chord]:
    if not chords:
        return chords
    out = [chords[0]]
    for c in chords[1:]:
        prev = out[-1]
        if c.symbol == prev.symbol and c.bass == prev.bass and abs(c.start_beat - prev.end_beat) < 1e-6:
            out[-1] = Chord(
                symbol=prev.symbol,
                start_beat=prev.start_beat,
                duration_beats=prev.duration_beats + c.duration_beats,
                bass=prev.bass,
                confidence=min(prev.confidence, c.confidence),
            )
        else:
            out.append(c)
    return out


def _filter_notes(
    notes: list[pretty_midi.Note],
    *,
    min_velocity: int,
    min_duration_seconds: float,
) -> list[pretty_midi.Note]:
    return [n for n in notes if n.velocity >= min_velocity and (n.end - n.start) >= min_duration_seconds]


def parse_midi(
    path: str | Path,
    *,
    bpm: float | None = None,
    windows_per_bar: int = 2,
    beats_per_bar: int = 4,
    prefer_flats: bool = False,
    min_velocity: int = 25,
    min_note_seconds: float = 0.05,
    min_window_mass: float = 0.30,
    region_beats: float = 32.0,    # for piecewise key detection (8 bars at 4/4)
) -> ChordSequence:
    """Infer a `ChordSequence` from a polyphonic MIDI file with key detection."""
    pm = pretty_midi.PrettyMIDI(str(path))
    inferred_bpm = (pm.estimate_tempo() if bpm is None else bpm) or 120.0

    notes: list[pretty_midi.Note] = []
    for inst in pm.instruments:
        notes.extend(inst.notes)
    notes = _filter_notes(notes, min_velocity=min_velocity, min_duration_seconds=min_note_seconds)
    if not notes:
        return ChordSequence(bpm=inferred_bpm, time_signature=(beats_per_bar, 4))

    seconds_per_beat = 60.0 / inferred_bpm
    seconds_per_window = (beats_per_bar / windows_per_bar) * seconds_per_beat
    beats_per_window = beats_per_bar / windows_per_bar
    region_seconds = region_beats * seconds_per_beat

    end_time = max(n.end for n in notes)
    n_windows = int(end_time / seconds_per_window) + 1

    # ---- piecewise key detection ----
    # build pitch-class histograms per region (weighted by velocity * duration)
    n_regions = max(1, int(end_time / region_seconds) + 1)
    region_hists = np.zeros((n_regions, 12), dtype=np.float64)
    for note in notes:
        region_idx = min(n_regions - 1, int(note.start / region_seconds))
        weight = (note.end - note.start) * (note.velocity / 127.0)
        region_hists[region_idx, note.pitch % 12] += weight

    key_map: list[KeyChange] = []
    for r in range(n_regions):
        key, conf = detect_key(region_hists[r])
        beat = r * region_beats
        # only emit a key change if it differs from the previous one (and confidence is reasonable)
        if not key_map or (key_map[-1].key != key and conf > 0.45):
            key_map.append(KeyChange(start_beat=beat, key=key))

    # ---- chord identification per window ----
    chords: list[Chord] = []
    for w in range(n_windows):
        w_start = w * seconds_per_window
        w_end = w_start + seconds_per_window
        win = _Window(start_beat=w * beats_per_window, end_beat=(w + 1) * beats_per_window)
        for note in notes:
            overlap = min(note.end, w_end) - max(note.start, w_start)
            if overlap > 0:
                win.pitches.append(note.pitch)
                win.durations.append(overlap)
                win.velocities.append(note.velocity)
        # active key for this window
        active_key = None
        win_beat = w * beats_per_window
        for kc in key_map:
            if kc.start_beat <= win_beat:
                active_key = kc.key
            else:
                break
        ident = _identify_chord(win, active_key, prefer_flats=prefer_flats, min_mass=min_window_mass)
        if ident is None:
            continue
        sym, bass, conf = ident
        chords.append(Chord(symbol=sym, start_beat=win.start_beat,
                            duration_beats=beats_per_window, bass=bass, confidence=conf))

    return ChordSequence(
        chords=_coalesce(chords),
        bpm=inferred_bpm,
        time_signature=(beats_per_bar, 4),
        key_map=key_map,
    )

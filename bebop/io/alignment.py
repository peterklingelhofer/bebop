"""Temporal alignment between a MIDI source and an audio source.

Without alignment, both `parse_midi` and `parse_audio` assume time 0 in their
file = beat 0 of the song. If the MIDI was exported from a different starting
point than the WAV (e.g. trimmed leading silence), every MIDI-derived chord
ends up off by a fixed beat offset relative to the audio sources, and the
ensemble vote silently picks against the MIDI for every bar.

This module exposes two ways to control for that:
    - manual offset: shift the MIDI by a known number of beats
    - first-onset alignment: detect the first significant note in MIDI and the
      first onset in audio, compute the lag, and apply it.

Both utilities operate on a ChordSequence — they don't touch the source files.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import librosa
import numpy as np
import pretty_midi

from bebop.types import Chord, ChordSequence, KeyChange


def find_audio_first_onset(audio_path: str | Path, *, sr: int = 22050) -> float | None:
    """Return time (seconds) of first significant audio onset, or None if none found."""
    y, _sr = librosa.load(str(audio_path), sr=sr, mono=True)
    if y.size == 0:
        return None
    # default backtrack=True snaps to the local energy minimum just before each onset,
    # which gives us the actual chord start instead of the attack peak
    onset_frames = librosa.onset.onset_detect(y=y, sr=_sr, units="frames", backtrack=True)
    if len(onset_frames) == 0:
        return None
    return float(librosa.frames_to_time(onset_frames[0], sr=_sr))


def find_midi_first_onset(midi_path: str | Path, *, min_velocity: int = 25) -> float | None:
    """Return time (seconds) of first MIDI note above `min_velocity`, or None if none."""
    pm = pretty_midi.PrettyMIDI(str(midi_path))
    starts: list[float] = []
    for inst in pm.instruments:
        for n in inst.notes:
            if n.velocity >= min_velocity:
                starts.append(n.start)
    if not starts:
        return None
    return min(starts)


def compute_first_onset_offset_beats(
    midi_path: str | Path,
    audio_path: str | Path,
    bpm: float,
) -> tuple[float, float, float] | None:
    """Return (offset_beats, midi_onset_sec, audio_onset_sec) or None if either onset missing.

    Positive offset means the MIDI should be SHIFTED LATER (audio's first event
    is later than MIDI's, so MIDI was assumed-too-early).
    """
    midi_onset = find_midi_first_onset(midi_path)
    audio_onset = find_audio_first_onset(audio_path)
    if midi_onset is None or audio_onset is None:
        return None
    offset_seconds = audio_onset - midi_onset
    offset_beats = offset_seconds * bpm / 60.0
    return offset_beats, midi_onset, audio_onset


def shift_sequence(seq: ChordSequence, offset_beats: float) -> ChordSequence:
    """Return a copy of `seq` with all chord and key-change start_beats shifted by `offset_beats`.

    Chords that would land before beat 0 are preserved (negative start_beat); downstream
    code can handle them or filter, but we don't drop them silently here.
    """
    if offset_beats == 0.0:
        return seq
    new_chords = [
        replace(c, start_beat=c.start_beat + offset_beats)
        for c in seq.chords
    ]
    new_key_map = [
        KeyChange(start_beat=kc.start_beat + offset_beats, key=kc.key)
        for kc in seq.key_map
    ]
    return ChordSequence(
        chords=new_chords,
        bpm=seq.bpm,
        time_signature=seq.time_signature,
        key_map=new_key_map,
    )

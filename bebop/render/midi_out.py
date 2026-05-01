"""Write a comping ChordSequence to a MIDI file using pretty_midi.

We emit two instrument tracks:
    - "Comp Bass":  GM program 33 (Acoustic Bass), either one sustained note per
                    chord (default) or a quarter-note walking line. Empty when
                    `piano_bass=True` because the piano takes over the bassline.
    - "Comp Piano": GM program 1 (Acoustic Grand Piano), the rhythmic upper-structure
                    voicings, plus the bassline in the LH register when
                    `piano_bass=True` (for the solo-piano feel).
"""

from __future__ import annotations

from pathlib import Path

import pretty_midi

from bebop.rhythm import render_rhythm
from bebop.types import ChordSequence
from bebop.voicing import voice_chord, VoicedChord
from bebop.voicing.dynamics import velocity_multiplier
from bebop.voicing.walking_bass import walking_bass_line


def _vel(base: int, envelope: list[float] | None, beat: float) -> int:
    """Clamp a velocity-multiplier-modulated value into the MIDI 1..127 range."""
    if envelope is None:
        return base
    return max(1, min(127, int(round(base * velocity_multiplier(envelope, beat)))))


def write_midi(
    seq: ChordSequence,
    output_path: str | Path,
    *,
    rhythm: str = "charleston",
    include_bass: bool = True,
    voicing: str = "rootless",
    walking_bass: bool = False,
    dynamics_envelope: list[float] | None = None,
    piano_bass: bool = False,
) -> Path:
    """Render a comping MIDI file from a (reharmonized) ChordSequence.

    `voicing` chooses the piano voicing style: rootless, evans, drop2, quartal.
    `walking_bass`: if True, the bassline is a quarter-note walking line; else
        sustained roots.
    `piano_bass`: if True, the piano carries the bassline (LH) and the bass
        instrument is silent. If False, the bass instrument plays the bassline
        and the piano only plays chord voicings.
    """
    pm = pretty_midi.PrettyMIDI(initial_tempo=seq.bpm)
    seconds_per_beat = 60.0 / seq.bpm

    piano = pretty_midi.Instrument(program=0, name=f"Comp Piano ({voicing})")
    bass = pretty_midi.Instrument(
        program=32,
        name=f"Comp Bass{' (walking)' if walking_bass else ''}",
    )

    # ── decide who carries the bassline ──
    bass_to_track = bass if (include_bass and not piano_bass) else (piano if (include_bass and piano_bass) else None)
    # bass_to_track is the instrument that gets the bassline notes:
    #   include_bass=False           → None (no bassline at all)
    #   include_bass + piano_bass=F  → bass instrument (default; full trio)
    #   include_bass + piano_bass=T  → piano (solo-piano feel; bass instrument empty)

    if bass_to_track is not None:
        if walking_bass:
            for note in walking_bass_line(seq):
                bass_to_track.notes.append(pretty_midi.Note(
                    velocity=_vel(note.velocity, dynamics_envelope, note.start_beat),
                    pitch=note.pitch,
                    start=note.start_beat * seconds_per_beat,
                    end=(note.start_beat + note.duration_beats) * seconds_per_beat,
                ))
        else:
            for chord in seq.chords:
                v = voice_chord(chord, previous=None, style=voicing)
                start = chord.start_beat * seconds_per_beat
                bass_to_track.notes.append(pretty_midi.Note(
                    velocity=_vel(80, dynamics_envelope, chord.start_beat),
                    pitch=v.bass_pitch,
                    start=start,
                    end=start + chord.duration_beats * seconds_per_beat,
                ))

    # ── piano track (chord voicings + rhythm) ──
    prev_voicing: VoicedChord | None = None
    for chord in seq.chords:
        v = voice_chord(chord, previous=prev_voicing, style=voicing)
        prev_voicing = v
        if not v.chord_pitches:
            continue
        for hit in render_rhythm(rhythm, chord.duration_beats):
            hit_beat = chord.start_beat + hit.offset_beats
            hit_start_sec = hit_beat * seconds_per_beat
            hit_end_sec = hit_start_sec + hit.duration_beats * seconds_per_beat
            scaled_vel = _vel(hit.velocity, dynamics_envelope, hit_beat)
            for p in v.chord_pitches:
                piano.notes.append(pretty_midi.Note(
                    velocity=scaled_vel,
                    pitch=p,
                    start=hit_start_sec,
                    end=hit_end_sec,
                ))

    pm.instruments.append(bass)
    pm.instruments.append(piano)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pm.write(str(output_path))
    return output_path

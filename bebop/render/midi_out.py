"""Write a comping ChordSequence to a MIDI file using pretty_midi.

We emit two instrument tracks:
    - "Comp Bass":  GM program 33 (Acoustic Bass), either one sustained note per
                    chord (default) or a quarter-note walking line.
    - "Comp Piano": GM program 1 (Acoustic Grand Piano), the rhythmic upper-structure voicings.
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
) -> Path:
    """Render a comping MIDI file from a (reharmonized) ChordSequence.

    `voicing` chooses the piano voicing style: rootless, evans, drop2, quartal.
    `walking_bass`: if True, emit quarter-note walking bass instead of sustained roots.
    """
    pm = pretty_midi.PrettyMIDI(initial_tempo=seq.bpm)
    seconds_per_beat = 60.0 / seq.bpm

    piano = pretty_midi.Instrument(program=0, name=f"Comp Piano ({voicing})")
    bass = pretty_midi.Instrument(
        program=32,
        name=f"Comp Bass{' (walking)' if walking_bass else ''}",
    )

    # ── bass track ──
    if include_bass:
        if walking_bass:
            for note in walking_bass_line(seq):
                bass.notes.append(pretty_midi.Note(
                    velocity=_vel(note.velocity, dynamics_envelope, note.start_beat),
                    pitch=note.pitch,
                    start=note.start_beat * seconds_per_beat,
                    end=(note.start_beat + note.duration_beats) * seconds_per_beat,
                ))
        else:
            for chord in seq.chords:
                v = voice_chord(chord, previous=None, style=voicing)
                start = chord.start_beat * seconds_per_beat
                bass.notes.append(pretty_midi.Note(
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

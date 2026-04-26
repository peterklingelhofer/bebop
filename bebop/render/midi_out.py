"""Write a comping ChordSequence to a MIDI file using pretty_midi.

We emit two instrument tracks:
    - "Comp Bass":  GM program 33 (Acoustic Bass), one note per chord at the bass register.
    - "Comp Piano": GM program 1 (Acoustic Grand Piano), the rhythmic upper-structure voicings.
"""

from __future__ import annotations

from pathlib import Path

import pretty_midi

from bebop.rhythm import render_rhythm
from bebop.types import ChordSequence
from bebop.voicing import voice_chord, VoicedChord


def write_midi(
    seq: ChordSequence,
    output_path: str | Path,
    *,
    rhythm: str = "charleston",
    include_bass: bool = True,
    voicing: str = "rootless",
) -> Path:
    """Render a comping MIDI file from a (reharmonized) ChordSequence.

    `voicing` chooses the piano voicing style: rootless, evans, drop2, quartal.
    """
    pm = pretty_midi.PrettyMIDI(initial_tempo=seq.bpm)
    seconds_per_beat = 60.0 / seq.bpm

    piano = pretty_midi.Instrument(program=0, name=f"Comp Piano ({voicing})")
    bass = pretty_midi.Instrument(program=32, name="Comp Bass")  # acoustic bass

    prev_voicing: VoicedChord | None = None
    for chord in seq.chords:
        v = voice_chord(chord, previous=prev_voicing, style=voicing)
        prev_voicing = v

        chord_start_sec = chord.start_beat * seconds_per_beat

        # bass: one note per chord, sustained for the whole duration
        if include_bass:
            bass.notes.append(
                pretty_midi.Note(
                    velocity=80,
                    pitch=v.bass_pitch,
                    start=chord_start_sec,
                    end=chord_start_sec + chord.duration_beats * seconds_per_beat,
                )
            )

        # piano: rhythmic hits of the upper-structure voicing
        if not v.chord_pitches:
            continue
        for hit in render_rhythm(rhythm, chord.duration_beats):
            hit_start_sec = (chord.start_beat + hit.offset_beats) * seconds_per_beat
            hit_end_sec = hit_start_sec + hit.duration_beats * seconds_per_beat
            for p in v.chord_pitches:
                piano.notes.append(
                    pretty_midi.Note(
                        velocity=hit.velocity,
                        pitch=p,
                        start=hit_start_sec,
                        end=hit_end_sec,
                    )
                )

    pm.instruments.append(bass)
    pm.instruments.append(piano)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pm.write(str(output_path))
    return output_path

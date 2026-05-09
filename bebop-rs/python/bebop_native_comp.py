"""Comp event generator for the bebop-rs FFI's Phase C.7 pipeline.

Embedded into the Rust binary at compile time via `include_str!`. Defines
`comp_for_chord(symbol, ...)` which mirrors what `bebop.render.write_midi`
does internally — but returns a flat list of (time_seconds, status, pitch,
velocity) tuples instead of writing a .mid file. The Rust side schedules
these events as wall-clock deadlines and the C++ AU emits them on the
audio thread when their deadlines pass.

Time = 0 means "fire immediately at chord onset"; positive values are
seconds after chord onset.
"""

from __future__ import annotations

from bebop.reharm import reharmonize
from bebop.rhythm import render_rhythm, resolve_rhythm
from bebop.types import Chord, ChordSequence
from bebop.voicing import VoicedChord, voice_chord


_PIANO_CH = 0   # MIDI channel 1 (status nibble: 0x9X for note_on, 0x8X for note_off)
_BASS_CH = 1    # MIDI channel 2


def comp_for_chord(
    symbol: str,
    *,
    bpm: float = 120.0,
    voicing: str = "rootless",
    rhythm: str = "charleston",
    n_bars: int = 4,
    spice: float = 0.0,
    octave_shift: int = 0,
    prev_bass_pitch: int | None = None,
    prev_chord_pitches: tuple[int, ...] | None = None,
) -> tuple[list[tuple[float, int, int, int]], int, tuple[int, ...]]:
    """Return (events, voiced_bass_pitch, voiced_chord_pitches).

    `events` is a list of (time_seconds, status_byte, pitch, velocity)
    sorted by time. `voiced_*` are the resulting voicing — the caller
    threads them back as `prev_*` on the next call so voice-leading
    works across chord changes.

    `spice` (0..1) drives the reharm engine. Many substitutions need
    chord-pair context (ii-V insertion, tritone-sub of an upcoming
    dominant, etc.) and effectively no-op on a single-chord sequence,
    but extension/dominant alterations still fire — so high spice
    still adds color, just not full reharm. (Phase A's offline
    pipeline gets the full benefit since it sees the whole progression.)
    """
    spb = 60.0 / bpm
    chord_duration_beats = 4.0 * n_bars

    # Build a fake "previous voicing" for voice-leading continuity
    prev_voicing: VoicedChord | None = None
    if prev_bass_pitch is not None and prev_chord_pitches is not None:
        prev_voicing = VoicedChord(
            bass_pitch=int(prev_bass_pitch),
            chord_pitches=tuple(int(p) for p in prev_chord_pitches),
            source=Chord(symbol="prev", start_beat=0.0, duration_beats=4.0),
        )

    chord_obj = Chord(
        symbol=symbol, start_beat=0.0,
        duration_beats=chord_duration_beats,
    )

    # Reharm: apply spice to a single-chord sequence. Limited effect
    # without context, but extensions and dominant alterations still fire.
    if spice > 0.0:
        try:
            seq = ChordSequence(chords=[chord_obj], bpm=bpm,
                                  time_signature=(4, 4))
            reharmed = reharmonize(seq, spice=spice, seed=0)
            if reharmed.chords:
                chord_obj = reharmed.chords[0]
        except Exception as e:
            print(f"[bebop_native_comp] reharm failed for {symbol!r}: {e}")

    try:
        v = voice_chord(chord_obj, previous=prev_voicing, style=voicing)
    except Exception as e:
        # Degraded fallback: empty voicing, emit nothing
        print(f"[bebop_native_comp] voicing failed for {symbol!r}: {e}")
        return ([], 0, ())

    try:
        template_name, shift_beats = resolve_rhythm(rhythm)
    except ValueError as e:
        print(f"[bebop_native_comp] {e}")
        template_name, shift_beats = "charleston", 0.0

    events: list[tuple[float, int, int, int]] = []

    # Octave shift: bias every emitted pitch by 12 * octaves. Clamped to
    # the legal MIDI range [0..127] so a wild shift can't crash the host.
    semis = int(octave_shift) * 12
    def shift(p: int) -> int:
        return max(0, min(127, int(p) + semis))

    # Bass: one sustained note for the whole duration.
    bass_pitch_shifted = shift(v.bass_pitch)
    bass_start = shift_beats * spb
    bass_end = bass_start + chord_duration_beats * spb
    if bass_start >= 0:
        events.append((bass_start, 0x90 | _BASS_CH, bass_pitch_shifted, 80))
        events.append((bass_end,   0x80 | _BASS_CH, bass_pitch_shifted, 0))

    # Piano: rhythm-pattern hits playing each chord pitch.
    chord_pitches_shifted = [shift(p) for p in v.chord_pitches]
    if chord_pitches_shifted:
        for hit in render_rhythm(template_name, chord_duration_beats):
            hit_start = (hit.offset_beats + shift_beats) * spb
            hit_end = hit_start + hit.duration_beats * spb
            if hit_start < 0:
                continue
            for p in chord_pitches_shifted:
                events.append((hit_start, 0x90 | _PIANO_CH, p, int(hit.velocity)))
                events.append((hit_end,   0x80 | _PIANO_CH, p, 0))

    events.sort(key=lambda e: e[0])
    # Return the un-shifted pitches as the "voicing" — this is what gets
    # threaded back as `prev_*` for voice-leading on the next call. Voice
    # leading should reason about the underlying voicing, not the shifted
    # output (otherwise rapid octave changes would scramble continuity).
    return (events, int(v.bass_pitch), tuple(int(p) for p in v.chord_pitches))

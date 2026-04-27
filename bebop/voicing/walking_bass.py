"""Walking bass line generator.

Quarter-note bass lines that connect chord changes via approach notes, in the
style of an upright jazz bassist comping behind a piano. Replaces the default
"sustained root for the whole chord" bass with a moving line.

Algorithm per chord (chord lasting N beats, with next chord's root known):
    beat 1:        root, in bass register, near previous note for smooth motion
    beat 2:        chord 5th
    beat 3:        chord 3rd
    beats 4..N-1:  cycle through 5th, 7th (if extant), root
    beat N:        chromatic approach to next chord's root (whichever direction
                   keeps the leap small) — falls back to chord 5th on the last
                   chord of the song

For chords shorter than a beat, we emit a single note covering the full duration.
"""

from __future__ import annotations

from dataclasses import dataclass

from music21 import pitch as m21pitch

from bebop.reharm.substitutions import parse_root, quality_of
from bebop.types import Chord, ChordSequence

_BASS_LOW = 28      # E1
_BASS_HIGH = 50     # D3
_DEFAULT_VEL = 84


@dataclass(frozen=True, slots=True)
class BassNote:
    pitch: int
    start_beat: float
    duration_beats: float
    velocity: int = _DEFAULT_VEL


def _root_pc(chord: Chord) -> int:
    """Sounding bass-note pitch class — slash-bass override if present, else chord root."""
    if chord.bass:
        try:
            return m21pitch.Pitch(chord.bass).pitchClass
        except Exception:
            pass
    pc, _ = parse_root(chord.symbol)
    return pc


def _chord_root_pc(chord: Chord) -> int:
    """The chord's true root, ignoring slash bass — for computing 3rds/5ths/7ths."""
    pc, _ = parse_root(chord.symbol)
    return pc


def _third_offset(chord: Chord) -> int:
    q = quality_of(chord.symbol)
    return 3 if q in ("min", "dim") else 4


def _fifth_offset(chord: Chord) -> int:
    sym = chord.symbol.lower()
    if "m7b5" in sym or "dim" in sym:
        return 6
    if sym.endswith("aug") or "+" in chord.symbol or "#5" in sym:
        return 8
    return 7


def _seventh_offset(chord: Chord) -> int | None:
    sym = chord.symbol.lower()
    if "maj7" in sym or "maj9" in sym or "maj13" in sym or sym.endswith("m7"):
        if "maj7" in sym or "maj9" in sym or "maj13" in sym:
            return 11
        return 10  # m7
    if "dim7" in sym:
        return 9
    if sym.endswith("dim"):
        return None
    # bare digit suffixes after the root → dominant 7th flavor
    _, rest = parse_root(chord.symbol)
    if rest and rest[0] in ("7", "9", "1"):
        return 10
    return None


def _snap_to_bass(p: int) -> int:
    while p < _BASS_LOW:
        p += 12
    while p > _BASS_HIGH:
        p -= 12
    return p


def _nearest_pitch(target_pc: int, near_pitch: int) -> int:
    """Nearest MIDI pitch with `target_pc` to `near_pitch`."""
    base = (near_pitch // 12) * 12 + target_pc
    candidates = [base - 12, base, base + 12]
    return min(candidates, key=lambda p: abs(p - near_pitch))


def _walking_pattern(chord: Chord, prev_pitch: int | None,
                     next_root_pc: int | None, beats: int) -> list[int]:
    """Pick `beats` quarter-note pitches for `chord`. Built in chronological
    order so the approach note voice-leads from whichever pitch immediately
    precedes it, not from the root."""
    if beats <= 0:
        return []

    bass_root_pc = _root_pc(chord)
    chord_root_pc = _chord_root_pc(chord)
    fifth_pc = (chord_root_pc + _fifth_offset(chord)) % 12
    third_pc = (chord_root_pc + _third_offset(chord)) % 12

    # beat 1 — the bass note
    if prev_pitch is None:
        line: list[int] = [_snap_to_bass(_nearest_pitch(bass_root_pc, 36))]   # near C2
    else:
        line = [_snap_to_bass(_nearest_pitch(bass_root_pc, prev_pitch))]
    if beats == 1:
        return line

    # build the middle of the line in chronological order before the approach
    if beats >= 3:
        # beat 2 — fifth
        line.append(_snap_to_bass(_nearest_pitch(fifth_pc, line[-1])))
    if beats >= 4:
        # beat 3 — third
        line.append(_snap_to_bass(_nearest_pitch(third_pc, line[-1])))
    # >4 beats — extend by cycling through chord tones
    if beats > 4:
        cycle_offsets = [_fifth_offset(chord)]
        seventh_off = _seventh_offset(chord)
        if seventh_off is not None:
            cycle_offsets.append(seventh_off)
        cycle_offsets.extend([_third_offset(chord), 0])
        i = 0
        while len(line) < beats - 1:
            off = cycle_offsets[i % len(cycle_offsets)]
            pc = (chord_root_pc + off) % 12
            line.append(_snap_to_bass(_nearest_pitch(pc, line[-1])))
            i += 1

    # last beat — approach to next chord's root (chromatic, picking the
    # neighbor that minimizes leap from the immediately previous note)
    last_prev = line[-1]
    if next_root_pc is not None:
        above_pc = (next_root_pc + 1) % 12
        below_pc = (next_root_pc - 1) % 12
        above_pitch = _snap_to_bass(_nearest_pitch(above_pc, last_prev))
        below_pitch = _snap_to_bass(_nearest_pitch(below_pc, last_prev))
        last = min((above_pitch, below_pitch), key=lambda p: abs(p - last_prev))
    else:
        last = _snap_to_bass(_nearest_pitch(fifth_pc, last_prev))

    line.append(last)
    return line


def walking_bass_line(seq: ChordSequence) -> list[BassNote]:
    """Generate a walking bass line for `seq`. One quarter note per beat."""
    out: list[BassNote] = []
    prev_pitch: int | None = None
    chords = list(seq.chords)
    for i, chord in enumerate(chords):
        next_root_pc: int | None = None
        if i + 1 < len(chords):
            next_root_pc = _root_pc(chords[i + 1])

        # how many quarter notes does this chord get? cap at floor(duration).
        # for sub-beat chords (rare, from heavy reharm), emit one note covering the full duration.
        n_quarters = int(chord.duration_beats)
        if n_quarters <= 0:
            pc = _root_pc(chord)
            near = prev_pitch if prev_pitch is not None else 36
            pitch_val = _snap_to_bass(_nearest_pitch(pc, near))
            out.append(BassNote(pitch=pitch_val, start_beat=chord.start_beat,
                                duration_beats=chord.duration_beats))
            prev_pitch = pitch_val
            continue

        pitches = _walking_pattern(chord, prev_pitch, next_root_pc, n_quarters)
        for j, p in enumerate(pitches):
            # last note holds any leftover fractional duration for swing-feel sustain
            beat_dur = 1.0
            if j == len(pitches) - 1:
                beat_dur = chord.duration_beats - (len(pitches) - 1)
            out.append(BassNote(pitch=p, start_beat=chord.start_beat + j,
                                duration_beats=beat_dur))
        if pitches:
            prev_pitch = pitches[-1]
    return out

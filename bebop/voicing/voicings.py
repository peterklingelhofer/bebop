"""Voicing layer: chord symbols -> MIDI pitches in a chosen jazz idiom.

Four voicing styles:
    - rootless:  bass + 3-7 (or 7-3) shell + available extensions. Bill Evans LH.
    - evans:     full Bill Evans — LH 3-7 shell + RH 9-3-13 (or 5-9-13) cluster.
                 The "Kind of Blue" piano sound.
    - drop2:     close-position 4-note voicing with the 2nd-from-top dropped
                 down an octave. Big-band brass / jazz guitar comping idiom.
    - quartal:   stacked perfect 4ths from a rootless interior pitch. McCoy
                 Tyner / Coltrane post-bop. Best on minor / sus / modal chords.

Each voicing is a function `(Chord, VoicedChord | None) -> VoicedChord` that
threads voice-leading from the previous chord to minimize jumps.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from music21 import harmony, pitch

from bebop.reharm.substitutions import root_name
from bebop.types import Chord


@dataclass(slots=True)
class VoicedChord:
    """A chord with concrete MIDI pitches, ready for the MIDI writer."""

    bass_pitch: int
    chord_pitches: tuple[int, ...]
    source: Chord


_BASS_RANGE = (28, 50)              # E1 .. D3
_CENTER = 60                         # middle C
_HALF_SPAN = 10                      # voicings within ±10 semitones of center


# ─────────────────────────── chord-symbol parsing ────────────────────────────

def _normalize_suffix(rest: str) -> str:
    """Translate our internal jazz suffix vocabulary into music21's accepted abbreviations."""
    if rest.endswith("7alt"):
        rest = rest[:-4] + "7b9b13"
    for src, dst in (("maj13", "M13"), ("maj9", "M9")):
        if src in rest:
            rest = rest.replace(src, dst)
    return rest


def _normalize_for_music21(symbol: str) -> str:
    """Translate our internal jazz vocabulary into music21's accepted abbreviations.

    music21 has surprising edge cases around bare flat-rooted symbols:
        - "Ab" parses 'A' + chord-type 'b' (invalid). Need explicit triad: "Ab major".
    Easiest fix: enharmonically respell flats to sharps for music21 only — we keep
    the readable flat names internally for chord chart output.
    """
    root = root_name(symbol)
    rest = _normalize_suffix(symbol[len(root):])
    # respell bare flat roots to sharp enharmonics: Ab -> G#, Bb -> A#, Db -> C#, Eb -> D#, Gb -> F#
    enharmonic = {"Ab": "G#", "Bb": "A#", "Db": "C#", "Eb": "D#", "Gb": "F#"}
    return enharmonic.get(root, root) + rest


_RESOLVE_FAILURES_LOGGED: set[str] = set()


@lru_cache(maxsize=1024)
def _resolve_pitches(symbol: str) -> tuple[int, ...]:
    """Parse `symbol` to MIDI pitches via music21. On parser failure, fall back
    to the bare root triad so the comp keeps playing instead of crashing."""
    norm = _normalize_for_music21(symbol)
    try:
        cs = harmony.ChordSymbol(norm)
    except Exception as e:
        if symbol not in _RESOLVE_FAILURES_LOGGED:
            print(f"  [voicing] music21 can't parse {symbol!r} (normalized {norm!r}): {e}; "
                  f"falling back to bare root triad")
            _RESOLVE_FAILURES_LOGGED.add(symbol)
        # bare root triad fallback
        try:
            head = symbol[0].upper()
            rest = symbol[1:]
            accidental = ""
            if rest and rest[0] in ("#", "b"):
                accidental = rest[0]
            cs = harmony.ChordSymbol(f"{head}{accidental}")
        except Exception:
            return ()
    pcs: list[int] = []
    for p in cs.pitches:
        midi = int(p.midi)
        if midi not in pcs:
            pcs.append(midi)
    return tuple(pcs)


def _bass_for_root(root_pc: int) -> int:
    candidate = _BASS_RANGE[0] + ((root_pc - _BASS_RANGE[0]) % 12)
    while candidate < _BASS_RANGE[0]:
        candidate += 12
    while candidate > _BASS_RANGE[1]:
        candidate -= 12
    return candidate


def _resolve_bass_pc(chord: Chord, root_pc: int) -> int:
    if chord.bass:
        try:
            return pitch.Pitch(chord.bass).pitchClass
        except Exception:
            pass
    return root_pc


def _normalize_to_register(midi_notes: list[int],
                           previous: tuple[int, ...] | None,
                           center: int = _CENTER,
                           half_span: int = _HALF_SPAN) -> tuple[int, ...]:
    """Octave-shift each note to land near `center`, then minimize jump from `previous`."""
    if not midi_notes:
        return tuple(midi_notes)
    centered = []
    for n in midi_notes:
        while n < center - half_span:
            n += 12
        while n > center + half_span:
            n -= 12
        centered.append(n)
    centered = sorted(set(centered))

    if previous is None or not previous:
        return tuple(centered)

    optimized: list[int] = []
    for n in centered:
        best = n
        best_dist = min(abs(n - p) for p in previous)
        for shift in (-12, 12):
            cand = n + shift
            if cand < center - half_span - 6 or cand > center + half_span + 6:
                continue
            cand_dist = min(abs(cand - p) for p in previous)
            if cand_dist < best_dist:
                best, best_dist = cand, cand_dist
        optimized.append(best)
    return tuple(sorted(set(optimized)))


def _drop_root(pitches: tuple[int, ...], root_pc: int) -> tuple[int, ...]:
    if len(pitches) <= 3:
        return pitches
    return tuple(p for p in pitches if p % 12 != root_pc)


# ─────────────────────────── voicing implementations ─────────────────────────

def _voice_rootless(chord: Chord, previous: VoicedChord | None) -> VoicedChord:
    """Bass + rootless upper structure (3-7 shell + extensions)."""
    pitches = _resolve_pitches(chord.symbol)
    if not pitches:
        return VoicedChord(bass_pitch=60, chord_pitches=(), source=chord)
    root_pc = pitches[0] % 12
    bass = _bass_for_root(_resolve_bass_pc(chord, root_pc))
    upper = list(_drop_root(pitches, root_pc))
    voiced = _normalize_to_register(upper, previous.chord_pitches if previous else None)
    return VoicedChord(bass_pitch=bass, chord_pitches=voiced, source=chord)


def _voice_evans(chord: Chord, previous: VoicedChord | None) -> VoicedChord:
    """Bill Evans: bass + LH 3-7 shell + RH cluster of (5/9, 7/3, 9/13).

    The classic "Kind of Blue" sound: a tight LH shell beneath a denser RH
    voicing in the upper octave, separated by a small gap.
    """
    pitches = _resolve_pitches(chord.symbol)
    if not pitches:
        return VoicedChord(bass_pitch=60, chord_pitches=(), source=chord)
    root_pc = pitches[0] % 12
    bass = _bass_for_root(_resolve_bass_pc(chord, root_pc))

    # extract scale degrees by interval from root
    by_interval: dict[int, int] = {}
    for p in pitches:
        iv = (p - pitches[0]) % 12
        if iv not in by_interval:
            by_interval[iv] = p
    third = by_interval.get(4, by_interval.get(3))         # major or minor 3rd
    fifth = by_interval.get(7, by_interval.get(6, by_interval.get(8)))
    seventh = by_interval.get(11, by_interval.get(10))     # major or minor 7th
    ninth = by_interval.get(2, by_interval.get(1, by_interval.get(3)))
    thirteenth = by_interval.get(9, by_interval.get(8))    # 13th or b13

    # LH shell: 3 + 7 (or 7 + 3 — pick whichever is closer to previous)
    lh = [n for n in (third, seventh) if n is not None]
    # RH cluster: prefer 9 + 5 (or extension) + 3
    rh_candidates = [n for n in (ninth, fifth, third, thirteenth) if n is not None]
    rh = []
    seen_pcs: set[int] = set()
    for n in rh_candidates:
        pc = n % 12
        if pc in seen_pcs:
            continue
        seen_pcs.add(pc)
        rh.append(n)
        if len(rh) >= 3:
            break

    # place LH around C4, RH around C5
    lh_voiced = _normalize_to_register(lh, previous.chord_pitches if previous else None,
                                       center=58, half_span=5)
    # RH centered higher; voice-lead against the LH placement, not previous chord, for tightness
    rh_voiced = _normalize_to_register(rh, lh_voiced, center=70, half_span=5)
    combined = tuple(sorted(set(lh_voiced) | set(rh_voiced)))
    return VoicedChord(bass_pitch=bass, chord_pitches=combined, source=chord)


def _voice_drop2(chord: Chord, previous: VoicedChord | None) -> VoicedChord:
    """4-note close-position 7th voicing with the 2nd-from-top dropped an octave.

    For Cmaj7 (C-E-G-B), close = C4-E4-G4-B4; drop-2 = G3-C4-E4-B4.
    Produces the smooth, open sound common in big-band brass and jazz guitar comping.
    """
    pitches = _resolve_pitches(chord.symbol)
    if not pitches:
        return VoicedChord(bass_pitch=60, chord_pitches=(), source=chord)
    root_pc = pitches[0] % 12
    bass = _bass_for_root(_resolve_bass_pc(chord, root_pc))

    # take the 4 most-defining tones: root, 3, 5, 7 (or whatever's available)
    by_interval: dict[int, int] = {}
    for p in pitches:
        iv = (p - pitches[0]) % 12
        if iv not in by_interval:
            by_interval[iv] = p
    root_n = by_interval[0]
    third = by_interval.get(4, by_interval.get(3, root_n + 4))
    fifth = by_interval.get(7, by_interval.get(6, by_interval.get(8, root_n + 7)))
    seventh = by_interval.get(11, by_interval.get(10, root_n + 11))

    # close position around the center, sorted ascending: root, 3, 5, 7
    close = sorted([root_n, third, fifth, seventh])
    # drop the 2nd-from-top down an octave
    second_from_top = close[-2]
    dropped = sorted(close[:-2] + [second_from_top - 12, close[-1]])

    voiced = _normalize_to_register(dropped, previous.chord_pitches if previous else None,
                                    center=62, half_span=12)
    return VoicedChord(bass_pitch=bass, chord_pitches=voiced, source=chord)


def _voice_quartal(chord: Chord, previous: VoicedChord | None) -> VoicedChord:
    """Stacked-fourths voicing from an interior pitch.

    For Dm7: D-G-C-F (perfect 4ths) or A-D-G-C. McCoy Tyner / Coltrane style.
    On minor / sus / modal chords this is the default jazz piano post-bop sound.
    On dominants we build the quartal stack from the 5th to imply altered/lydian tensions.
    On majors we shift the starting pitch to land on chord tones rather than tritones.
    """
    pitches = _resolve_pitches(chord.symbol)
    if not pitches:
        return VoicedChord(bass_pitch=60, chord_pitches=(), source=chord)
    root_pc = pitches[0] % 12
    bass = _bass_for_root(_resolve_bass_pc(chord, root_pc))
    chord_pcs = {p % 12 for p in pitches}

    # choose a starting interval-from-root for the quartal stack:
    # minor:    start on root (root, 4, b7, b3 above)
    # dominant: start on 5 or b7 (gives sus-ish lydian dominant tension)
    # major:    start on 5 or 9 (gives 5, 9, 13, 3 — Tyner-on-major)
    # sus:      start on root
    sym = chord.symbol.lower()
    if "m7b5" in sym or sym.endswith("dim"):
        start_offset = 3        # b3 above root: gives sus-y diminished color
    elif "maj" in sym or sym.endswith("6"):
        start_offset = 7        # from the 5th
    elif "sus" in sym:
        start_offset = 0
    elif "m" in sym and "maj" not in sym:
        start_offset = 0        # minor — root quartal
    else:
        # dominant
        start_offset = 7

    base = pitches[0] + start_offset
    stack = [base, base + 5, base + 10, base + 15]   # 4 stacked perfect 4ths

    # snap each note to the nearest chord-tone/extension if a 4th lands on a non-chord-tone
    # for dominants/maj keep the colorful tones; for minor try to keep stack pure
    voiced = _normalize_to_register(stack, previous.chord_pitches if previous else None,
                                    center=62, half_span=14)
    return VoicedChord(bass_pitch=bass, chord_pitches=voiced, source=chord)


# ─────────────────────────── public registry ─────────────────────────────────

_VOICERS = {
    "rootless": _voice_rootless,
    "evans":    _voice_evans,
    "drop2":    _voice_drop2,
    "quartal":  _voice_quartal,
}


def voice_chord(chord: Chord, previous: VoicedChord | None = None,
                style: str = "rootless") -> VoicedChord:
    """Resolve a `Chord` into MIDI pitches using the chosen voicing style."""
    if style not in _VOICERS:
        raise ValueError(f"unknown voicing style: {style!r}. Choose from {list(_VOICERS)}")
    return _VOICERS[style](chord, previous)


def available_voicings() -> list[str]:
    return list(_VOICERS)

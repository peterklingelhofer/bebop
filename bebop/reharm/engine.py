"""Spice-knob orchestration: applies tiered substitutions to a ChordSequence.

Each pass walks the chord list, asks each substitution whether to fire, and
either keeps the chord, replaces it, or splits it into multiple chords.
A pass that splits a chord does NOT recursively re-process the new pieces in
the same pass — that prevents one substitution from cascading wildly.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from bebop.reharm.substitutions import (
    ReharmContext,
    add_extensions,
    add_sevenths,
    alter_dominant,
    coltrane_changes,
    diminished_passing,
    insert_secondary_dominant,
    insert_two_five,
    modal_interchange,
    parse_root,
    prefer_flats_for_key,
    side_slip,
    tritone_sub,
)
from bebop.types import Chord, ChordSequence

Substitution = Callable[[Chord, ReharmContext], "Chord | list[Chord]"]

# Order matters: 7ths/9ths first (so later subs operate on enriched chords),
# then insertions, then dominant alterations, then exotic stuff.
_PIPELINE: list[Substitution] = [
    add_sevenths,
    add_extensions,
    insert_two_five,
    insert_secondary_dominant,
    alter_dominant,
    tritone_sub,
    modal_interchange,
    diminished_passing,
    side_slip,
    coltrane_changes,
]


def _parse_key_string(key: str | None) -> tuple[int | None, bool]:
    """Return (tonic_pc, is_major). 'D' -> (2, True), 'Am' -> (9, False)."""
    if not key:
        return None, True
    is_minor = key.endswith("m")
    tonic_str = key[:-1] if is_minor else key
    try:
        pc, _ = parse_root(tonic_str)
        return pc, not is_minor
    except Exception:
        return None, True


def reharmonize(seq: ChordSequence, *, spice: float = 0.5, seed: int | None = 0) -> ChordSequence:
    """Return a new ChordSequence with reharmonization applied.

    Per chord, the engine looks up the *active* key (from the sequence's key
    map) and threads it into the substitution context — so songs that modulate
    pick up the right modal interchange / secondary-dominant targets.
    """
    spice = max(0.0, min(1.0, spice))
    rng = random.Random(seed)
    ctx = ReharmContext(spice=spice, rng=rng)

    chords = list(seq.chords)
    for sub in _PIPELINE:
        next_chords: list[Chord] = []
        for c in chords:
            ctx.key_pc, ctx.key_is_major = _parse_key_string(seq.key_at(c.start_beat))
            ctx.prefer_flats = prefer_flats_for_key(seq.key_at(c.start_beat))
            result = sub(c, ctx)
            if isinstance(result, list):
                next_chords.extend(result)
            elif result is not None:
                next_chords.append(result)
            else:
                next_chords.append(c)
        chords = next_chords

    return ChordSequence(
        chords=chords,
        bpm=seq.bpm,
        time_signature=seq.time_signature,
        key_map=list(seq.key_map),
    )

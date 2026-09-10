"""Individual reharmonization substitutions, organized by tier.

Each substitution is a pure function `(Chord, ReharmContext) -> Chord | list[Chord] | None`.
- Returning the same `Chord` (or None) means "don't apply".
- Returning a new `Chord` replaces the original 1:1.
- Returning a list splits the original chord's duration across the returned chords.

The engine in `engine.py` orchestrates which subs run at which spice level.
This module is the "music theory" — keep it pure and side-effect free.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from bebop.types import Chord

# Pitch-class names with both spellings — we choose based on root context.
SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


@dataclass
class ReharmContext:
    """Per-call context the engine threads through every substitution.

    `key_pc` is mutable: the engine updates it per chord based on the
    sequence's key map so substitutions like modal interchange use the
    *currently active* tonic, not whichever key the song happened to start in.
    """

    spice: float
    rng: random.Random
    key_pc: int | None = None       # active tonic pitch class
    key_is_major: bool = True       # active mode
    prefer_flats: bool = False      # active key's preferred spelling for inserted roots


# ---------- chord symbol parsing helpers ----------

_QUALITY_MARKERS = ("maj7", "maj9", "maj13", "maj", "m7b5", "m7", "m9", "m11", "m13",
                    "m6", "m", "dim7", "dim", "aug", "7", "9", "11", "13", "6", "sus4", "sus2")


def parse_root(symbol: str) -> tuple[int, str]:
    """Split a chord symbol into (root_pc, remainder). Tolerant of flats/sharps."""
    if not symbol:
        return 0, ""
    head = symbol[0].upper()
    rest = symbol[1:]
    accidental = ""
    if rest and rest[0] in ("#", "b"):
        accidental = rest[0]
        rest = rest[1:]
    root_str = head + accidental
    pc_map = {n: i for i, n in enumerate(SHARP_NAMES)}
    pc_map.update({n: i for i, n in enumerate(FLAT_NAMES)})
    root_pc = pc_map.get(root_str, 0)
    return root_pc, rest


def root_name(symbol: str) -> str:
    """Return the chord symbol's own root spelling (letter + accidental).

    E.g. "Bb" from "Bbmaj7", "F#" from "F#m7b5": the literal text, not a
    pitch-class respelling, so a chart's own flat/sharp choice survives subs
    that don't change the root.
    """
    if not symbol:
        return "C"
    head = symbol[0].upper()
    rest = symbol[1:]
    accidental = rest[0] if rest and rest[0] in ("#", "b") else ""
    return head + accidental


def pc_to_name(pc: int, prefer_flats: bool = False) -> str:
    return (FLAT_NAMES if prefer_flats else SHARP_NAMES)[pc % 12]


FLAT_KEYS_MAJOR = {"F", "Bb", "Eb", "Ab", "Db", "Gb", "Cb"}
FLAT_KEYS_MINOR = {"D", "G", "C", "F", "Bb", "Eb", "Ab"}


def prefer_flats_for_key(key: str | None) -> bool:
    """True when `key` is a flat-side major/minor key. Trailing "m" is the only minor marker."""
    if not key:
        return False
    is_minor = key.endswith("m")
    tonic = key[:-1] if is_minor else key
    return tonic in (FLAT_KEYS_MINOR if is_minor else FLAT_KEYS_MAJOR)


def is_dominant(symbol: str) -> bool:
    """True if the chord is functioning as a dominant (V-style)."""
    _, rest = parse_root(symbol)
    if not rest or rest.startswith("maj") or rest.startswith("m"):
        return False
    # A bare extension (7, 9, 11, 13) with no maj/m prefix is dominant
    return rest.startswith(("7", "9", "11", "13"))


def quality_of(symbol: str) -> str:
    """Returns a coarse quality bucket: 'maj', 'min', 'dom', 'dim', 'sus', 'other'."""
    _, rest = parse_root(symbol)
    if rest.startswith("maj"):
        return "maj"
    if rest.startswith("m7b5") or rest.startswith("dim"):
        return "dim"
    if rest.startswith("m"):
        return "min"
    if rest.startswith("sus"):
        return "sus"
    if rest.startswith(("7", "9", "11", "13")):
        return "dom"
    if not rest or rest[0] in ("6",):
        return "maj"
    return "other"


# ---------- TIER 1 (0.0–0.2): add diatonic 7ths / 9ths ----------

def add_sevenths(c: Chord, ctx: ReharmContext) -> Chord:
    """Major triad -> maj7, minor triad -> m7, dominant triad with no extension -> 7."""
    if ctx.rng.random() > _tier_strength(ctx.spice, 0.0, 0.4):
        return c
    _, rest = parse_root(c.symbol)
    if rest == "":
        return c.with_symbol(f"{root_name(c.symbol)}maj7")
    if rest == "m":
        return c.with_symbol(f"{root_name(c.symbol)}m7")
    if rest in ("sus4", "sus2"):
        return c.with_symbol(f"{root_name(c.symbol)}{rest[:3]}7" if rest.startswith("sus") else c.symbol)
    return c


def add_extensions(c: Chord, ctx: ReharmContext) -> Chord:
    """Promote 7-chords to 9 or 13 chords (probabilistically)."""
    p = _tier_strength(ctx.spice, 0.05, 0.5)
    if ctx.rng.random() > p:
        return c
    _, rest = parse_root(c.symbol)
    promotions = {
        "maj7": ["maj9", "maj13"],
        "m7":   ["m9", "m11"],
        "7":    ["9", "13"],
    }
    for src, choices in promotions.items():
        if rest == src:
            return c.with_symbol(f"{root_name(c.symbol)}{ctx.rng.choice(choices)}")
    return c


# ---------- TIER 2 (0.2–0.4): secondary dominants & inserted ii–Vs ----------

def insert_secondary_dominant(c: Chord, ctx: ReharmContext) -> list[Chord] | Chord:
    """Insert V7/X in front of any chord, splitting the duration in half.

    Only fires on chords lasting ≥2 beats (otherwise it gets too cluttered).
    """
    if c.duration_beats < 2.0:
        return c
    p = _tier_strength(ctx.spice, 0.2, 0.6)
    if ctx.rng.random() > p:
        return c
    root_pc, _ = parse_root(c.symbol)
    v_pc = (root_pc + 7) % 12
    half = c.duration_beats / 2
    v_chord = Chord(symbol=f"{pc_to_name(v_pc, prefer_flats=ctx.prefer_flats)}7",
                    start_beat=c.start_beat, duration_beats=half)
    return [v_chord, Chord(symbol=c.symbol, start_beat=c.start_beat + half, duration_beats=half, bass=c.bass)]


def insert_two_five(c: Chord, ctx: ReharmContext) -> list[Chord] | Chord:
    """Insert ii-7 V7 in front of the chord (each takes a quarter of the duration)."""
    if c.duration_beats < 4.0:
        return c
    p = _tier_strength(ctx.spice, 0.25, 0.6)
    if ctx.rng.random() > p:
        return c
    root_pc, _ = parse_root(c.symbol)
    ii_pc = (root_pc + 2) % 12
    v_pc = (root_pc + 7) % 12
    quarter = c.duration_beats / 4
    return [
        Chord(symbol=f"{pc_to_name(ii_pc, prefer_flats=ctx.prefer_flats)}m7",
              start_beat=c.start_beat, duration_beats=quarter),
        Chord(symbol=f"{pc_to_name(v_pc, prefer_flats=ctx.prefer_flats)}7",
              start_beat=c.start_beat + quarter, duration_beats=quarter),
        Chord(symbol=c.symbol, start_beat=c.start_beat + 2 * quarter, duration_beats=2 * quarter, bass=c.bass),
    ]


# ---------- TIER 3 (0.4–0.6): tritone subs & altered dominants ----------

def tritone_sub(c: Chord, ctx: ReharmContext) -> Chord:
    """Replace a dominant with the dominant a tritone away (e.g. G7 -> Db7)."""
    if not is_dominant(c.symbol):
        return c
    p = _tier_strength(ctx.spice, 0.4, 0.7)
    if ctx.rng.random() > p:
        return c
    root_pc, rest = parse_root(c.symbol)
    sub_pc = (root_pc + 6) % 12
    return c.with_symbol(f"{pc_to_name(sub_pc, prefer_flats=True)}{rest}")


def alter_dominant(c: Chord, ctx: ReharmContext) -> Chord:
    """Add b9 / #9 / #11 / b13 alterations to a dominant chord."""
    if not is_dominant(c.symbol):
        return c
    p = _tier_strength(ctx.spice, 0.4, 0.7)
    if ctx.rng.random() > p:
        return c
    _, rest = parse_root(c.symbol)
    # don't double-alter
    if any(alt in rest for alt in ("b9", "#9", "#11", "b13", "alt")):
        return c
    alteration = ctx.rng.choice(["b9", "#9", "#11", "b13", "alt"])
    return c.with_symbol(f"{root_name(c.symbol)}7{alteration}")


# ---------- TIER 4 (0.6–0.8): diminished passing chords & modal interchange ----------

def diminished_passing(c: Chord, ctx: ReharmContext) -> list[Chord] | Chord:
    """Insert a #i°7 passing chord into the second half of a long chord (resolves up by half-step)."""
    if c.duration_beats < 4.0:
        return c
    p = _tier_strength(ctx.spice, 0.55, 0.6)
    if ctx.rng.random() > p:
        return c
    root_pc, _ = parse_root(c.symbol)
    pass_pc = (root_pc + 1) % 12
    half = c.duration_beats / 2
    return [
        Chord(symbol=c.symbol, start_beat=c.start_beat, duration_beats=half, bass=c.bass),
        Chord(symbol=f"{pc_to_name(pass_pc, prefer_flats=ctx.prefer_flats)}dim7",
              start_beat=c.start_beat + half, duration_beats=half),
    ]


def modal_interchange(c: Chord, ctx: ReharmContext) -> Chord:
    """Borrow from the parallel minor of the *active key*.

    Targets diatonic IV (-> iv) and I (-> bVII rarely). Without a known key,
    falls back to "any major triad -> m7" with low probability so we don't
    wreck songs we have no key map for.
    """
    p = _tier_strength(ctx.spice, 0.6, 0.4)
    if ctx.rng.random() > p:
        return c
    q = quality_of(c.symbol)
    root_pc, _ = parse_root(c.symbol)
    if ctx.key_pc is not None and ctx.key_is_major:
        interval = (root_pc - ctx.key_pc) % 12
        # IV (interval 5, major) -> iv (minor)
        if interval == 5 and q == "maj":
            return c.with_symbol(f"{root_name(c.symbol)}m7")
        # II (interval 2, minor) -> II7 (Lydian dominant tinge) — sparingly
        if interval == 2 and q == "min" and ctx.rng.random() < 0.4:
            return c.with_symbol(f"{root_name(c.symbol)}7")
        return c
    # no key context: only soft, occasional major->minor
    if q == "maj" and ctx.rng.random() < 0.3:
        return c.with_symbol(f"{root_name(c.symbol)}m7")
    return c


# ---------- TIER 5 (0.8–1.0): coltrane / planing / side-slipping ----------

def side_slip(c: Chord, ctx: ReharmContext) -> list[Chord] | Chord:
    """Insert a chord a half-step above for the last beat of a long chord (chromatic upper neighbor)."""
    if c.duration_beats < 4.0:
        return c
    p = _tier_strength(ctx.spice, 0.75, 0.5)
    if ctx.rng.random() > p:
        return c
    root_pc, rest = parse_root(c.symbol)
    slip_pc = (root_pc + 1) % 12
    main_dur = c.duration_beats - 1.0
    return [
        Chord(symbol=c.symbol, start_beat=c.start_beat, duration_beats=main_dur, bass=c.bass),
        Chord(symbol=f"{pc_to_name(slip_pc, prefer_flats=True)}{rest or 'maj7'}",
              start_beat=c.start_beat + main_dur, duration_beats=1.0),
    ]


def coltrane_changes(c: Chord, ctx: ReharmContext) -> list[Chord] | Chord:
    """Apply a Giant Steps-style major-third cycle: replace one long maj chord with maj-(V7-maj)-(V7-maj)."""
    if c.duration_beats < 4.0:
        return c
    if quality_of(c.symbol) not in ("maj",):
        return c
    p = _tier_strength(ctx.spice, 0.85, 0.4)
    if ctx.rng.random() > p:
        return c
    root_pc, rest = parse_root(c.symbol)
    quality_suffix = "maj7"
    third_up = (root_pc + 4) % 12        # +M3
    sixth_up = (root_pc + 8) % 12        # +m6
    v_of_third = (third_up + 7) % 12     # V7 leading to the M3-up tonic
    v_of_sixth = (sixth_up + 7) % 12
    sl = c.duration_beats / 6  # 6 chord changes evenly
    pf = ctx.prefer_flats
    return [
        Chord(symbol=f"{root_name(c.symbol)}{quality_suffix}", start_beat=c.start_beat + 0 * sl, duration_beats=sl),
        Chord(symbol=f"{pc_to_name(v_of_sixth, prefer_flats=pf)}7",
              start_beat=c.start_beat + 1 * sl, duration_beats=sl),
        Chord(symbol=f"{pc_to_name(sixth_up, prefer_flats=pf)}{quality_suffix}",
              start_beat=c.start_beat + 2 * sl, duration_beats=sl),
        Chord(symbol=f"{pc_to_name(v_of_third, prefer_flats=pf)}7",
              start_beat=c.start_beat + 3 * sl, duration_beats=sl),
        Chord(symbol=f"{pc_to_name(third_up, prefer_flats=pf)}{quality_suffix}",
              start_beat=c.start_beat + 4 * sl, duration_beats=sl),
        Chord(symbol=f"{pc_to_name((root_pc + 7) % 12, prefer_flats=pf)}7",
              start_beat=c.start_beat + 5 * sl, duration_beats=sl),
    ]


# ---------- helpers ----------

def _tier_strength(spice: float, threshold: float, span: float) -> float:
    """Probability ramp: 0 below `threshold`, climbing linearly across `span`, capped at 1.0."""
    if spice <= threshold:
        return 0.0
    return min(1.0, (spice - threshold) / span)

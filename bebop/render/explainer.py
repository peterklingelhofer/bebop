"""Explainer / theory annotation helpers for the HTML report.

For each (reharmonized) variant, we want the report to show:
    - bar-by-bar comparison of the original chord chart vs. the variant's progression
    - a heuristic theory note explaining what changed (added 7th, tritone sub, etc.)
    - a voicing breakdown per chord — which pitches are sounding and what scale
      degrees they represent

This module contains pure functions that operate on ChordSequence objects.
Renderer-side concerns (HTML, CSS, JS) live in `report.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from bebop.reharm.substitutions import parse_root, pc_to_name, quality_of
from bebop.types import ChordSequence
from bebop.voicing import voice_chord


# ──────────────────────────── theory annotation ─────────────────────────────

# Interval (in semitones from chord root) → musical name.
# For a few intervals we offer multiple labels; the right one depends on chord
# quality, so the renderer should pick by context. We pick the most common.
_INTERVAL_LABEL: dict[int, str] = {
    0: "root", 1: "♭9", 2: "9", 3: "♭3 / ♯9", 4: "3",
    5: "11", 6: "♯11 / ♭5", 7: "5", 8: "♯5 / ♭13",
    9: "13 / 6", 10: "♭7", 11: "7",
}

# Quality bucket → friendly name (for prose).
_QUALITY_NAME = {
    "maj": "major", "min": "minor", "dom": "dominant",
    "dim": "diminished", "sus": "suspended", "other": "",
}


def _interval(a: int, b: int) -> int:
    """Return (b − a) mod 12."""
    return (b - a) % 12


def _has_extensions_above(symbol: str, threshold_steps: int) -> bool:
    """True if `symbol` references chord extensions of `threshold_steps`-th or higher.

    `threshold_steps` is in stepwise terms (7 means 7th, 9 means 9th, etc.).
    Crude — looks for the digit substring in the chord-quality remainder.
    """
    _, rest = parse_root(symbol)
    return any(s in rest for s in (str(threshold_steps), str(threshold_steps + 2),
                                    str(threshold_steps + 4), str(threshold_steps + 6)))


def explain_substitution(original_symbols: list[str], new_symbol: str,
                         next_original: str | None = None) -> str:
    """Heuristic short description of how `new_symbol` differs from the originals
    that would have been sounding at the same beat.

    `original_symbols` is the list of original chords whose duration covers the
    new chord's start beat — usually one entry, sometimes two if a bar already
    had multiple chords.
    """
    if not original_symbols:
        return "inserted (no original at this beat)"

    # Pick the closest match — usually just the first/only one
    primary_original = original_symbols[0]
    if primary_original == new_symbol:
        return ""  # no change

    o_root, o_rest = parse_root(primary_original)
    n_root, n_rest = parse_root(new_symbol)
    o_quality = quality_of(primary_original)
    n_quality = quality_of(new_symbol)

    # ── same root ──
    if o_root == n_root:
        if o_quality == n_quality:
            # quality bucket same, just more extensions — "added X"
            return _describe_extension_change(primary_original, new_symbol)
        # quality changed: modal interchange or parallel transformation
        if o_quality == "maj" and n_quality == "min":
            return "modal interchange — major → minor (parallel)"
        if o_quality == "min" and n_quality == "maj":
            return "parallel — minor → major"
        if o_quality in ("maj", "min") and n_quality == "dom":
            return f"turned into a dominant 7"
        return f"changed quality: {_QUALITY_NAME.get(o_quality, '')} → {_QUALITY_NAME.get(n_quality, '')}"

    # ── different root ──
    # Insert into next-chord context if we have it
    if next_original is not None:
        next_root, _ = parse_root(next_original)
        # Inserted V7 of the next chord (secondary dominant)
        if n_quality == "dom" and _interval(n_root, next_root) == 5:
            # n_root is a 5th below next root, i.e., n_root is V of next
            return f"V7 of {pc_to_name(next_root)} (secondary dominant)"
        # Inserted ii of the next chord
        if n_quality == "min" and _interval(n_root, next_root) == 10:
            return f"ii of {pc_to_name(next_root)} (inserted in ii-V)"
        # Tritone sub of the V7 of next chord
        if n_quality == "dom" and _interval(n_root, next_root) == 1:
            return f"tritone sub: {pc_to_name((next_root - 5) % 12)}7 → {new_symbol} (resolves down a half-step to {pc_to_name(next_root)})"

    # Distance from original by interval
    interval = _interval(o_root, n_root)
    if interval == 6:
        return f"tritone sub of {primary_original}"
    if interval == 7:
        return f"V7 — root up a 5th from {primary_original}"
    if interval == 5:
        return f"root up a 4th from {primary_original}"
    if interval == 1:
        return f"chromatic neighbor — half-step above {primary_original}"
    if interval == 11:
        return f"chromatic neighbor — half-step below {primary_original}"
    if interval == 4:
        return f"root up a major 3rd (Coltrane-style)"
    if interval == 8:
        return f"root up a minor 6th (Coltrane-style)"
    if interval == 2:
        return f"root up a whole step"
    if interval == 3:
        return f"root up a minor 3rd"

    return f"resubstituted (was {primary_original})"


def _describe_extension_change(original: str, new: str) -> str:
    """Same root, quality bucket roughly the same — describe added extensions."""
    _, o_rest = parse_root(original)
    _, n_rest = parse_root(new)
    if not o_rest and "maj7" in n_rest:
        return "added maj7"
    if not o_rest and n_rest == "m":
        return ""
    if not o_rest and "7" in n_rest and "maj" not in n_rest:
        return "added dominant 7th"
    # detect added extensions
    extensions_added: list[str] = []
    for token in ("maj9", "maj13", "9", "11", "13", "6", "b9", "#9", "#11", "b13", "alt"):
        if token in n_rest and token not in o_rest:
            extensions_added.append(token)
    if extensions_added:
        return "added " + " + ".join(extensions_added)
    return f"upgraded {original} → {new}"


# ──────────────────────────── voicing breakdown ─────────────────────────────


@dataclass(frozen=True, slots=True)
class VoicingBreakdown:
    chord_symbol: str
    bass_pitch: int
    bass_interval: str           # interval label for the bass note from chord root
    chord_pitches: tuple[int, ...]
    intervals: tuple[str, ...]   # interval label per chord_pitch, in order
    summary: str                 # e.g. "rootless 3-7-9 shell"


def _interval_label(interval: int) -> str:
    return _INTERVAL_LABEL.get(interval, f"{interval}st")


def _voicing_summary(intervals: tuple[str, ...], style: str) -> str:
    """Short prose describing the voicing in jazz-pedagogy terms."""
    # remove duplicates to describe the structure
    unique_intervals = tuple(dict.fromkeys(intervals))
    has_root = "root" in unique_intervals
    if style == "rootless" and not has_root:
        return f"rootless {'-'.join(unique_intervals)} (Bill Evans LH)"
    if style == "drop2":
        return f"drop-2: {'-'.join(unique_intervals)}"
    if style == "quartal":
        return f"stacked 4ths (McCoy Tyner): {'-'.join(unique_intervals)}"
    if style == "evans":
        return f"Evans cluster: {'-'.join(unique_intervals)}"
    return "-".join(unique_intervals)


def voicing_breakdown(reharmed: ChordSequence, voicing_style: str) -> list[VoicingBreakdown]:
    """For each chord in `reharmed`, return its voiced pitches + interval labels.

    `bass_interval` is the interval from the chord's root to the bass note
    (usually "root", but for slash chords like Dm7/F it's "♭3" because F is
    the minor third of Dm).
    """
    out: list[VoicingBreakdown] = []
    prev = None
    for chord in reharmed.chords:
        v = voice_chord(chord, previous=prev, style=voicing_style)
        prev = v
        try:
            root_pc, _ = parse_root(chord.symbol)
        except Exception:
            root_pc = 0
        intervals = tuple(_interval_label((p - root_pc) % 12) for p in v.chord_pitches)
        bass_interval = _interval_label((v.bass_pitch - root_pc) % 12)
        out.append(VoicingBreakdown(
            chord_symbol=chord.symbol,
            bass_pitch=v.bass_pitch,
            bass_interval=bass_interval,
            chord_pitches=v.chord_pitches,
            intervals=intervals,
            summary=_voicing_summary(intervals, voicing_style),
        ))
    return out


# ──────────────────────────── chart-comparison data ─────────────────────────


@dataclass(frozen=True, slots=True)
class ChartRow:
    bar: int
    beat: float                 # absolute beat position in song
    duration_beats: float
    original_symbol: str | None
    new_symbol: str
    new_bass: str | None
    theory_note: str


def _originals_at_beat(original: ChordSequence, beat: float) -> list[str]:
    """Return original-chart symbol(s) sounding at `beat` (just before, inclusive)."""
    out = []
    for c in original.chords:
        if c.start_beat <= beat < c.end_beat + 1e-6:
            out.append(c.symbol)
    return out


def chart_comparison(original: ChordSequence, reharmed: ChordSequence,
                     beats_per_bar: int = 4) -> list[ChartRow]:
    """Produce per-(reharmed-chord) rows with original chord context + theory notes."""
    rows: list[ChartRow] = []
    for i, chord in enumerate(reharmed.chords):
        bar = int(chord.start_beat // beats_per_bar) + 1
        originals = _originals_at_beat(original, chord.start_beat)
        next_orig: str | None = None
        # for the theory annotation, look at what the original had AFTER this chord
        if i + 1 < len(reharmed.chords):
            next_chord = reharmed.chords[i + 1]
            next_originals = _originals_at_beat(original, next_chord.start_beat)
            if next_originals:
                next_orig = next_originals[0]
        note = explain_substitution(originals, chord.symbol, next_orig)
        rows.append(ChartRow(
            bar=bar,
            beat=chord.start_beat,
            duration_beats=chord.duration_beats,
            original_symbol=originals[0] if originals else None,
            new_symbol=chord.symbol,
            new_bass=chord.bass,
            theory_note=note,
        ))
    return rows

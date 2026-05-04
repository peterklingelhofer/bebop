"""Rhythmic comping templates.

Each template maps a chord's duration into a list of (offset_beats, duration_beats,
velocity) hits. We tile the template across each chord's duration in 1-bar (4-beat)
chunks. If a chord doesn't span a full bar, the template is truncated.

Pattern families included:
    Charleston family — beat 1 + "and of 3", plus +1/+2/+3-beat shifts so you
        can A/B the same syncopation landing on different parts of the bar.
        The shifts wrap modulo 4, so e.g. `charleston_+2` puts the original
        beat-1 hit on beat 3 and the original "and-of-3" hit on "and of 1".

    Quarter-note patterns — `freddie_green` (Count Basie-style relentless
        quarter chunk), `two_and_four` (Freddie Green minus the downbeats).

    Pads — `sustained` (one whole-bar held chord — ballad pad).

    Syncopated comping — `anticipations` (push on "and of 4"),
        `reverse_charleston` ("and of 1" + beat 3, the answer to Charleston),
        `ahmad_jamal` (1 + 2 + "and of 3"), `bossa` (1 + "and of 2" + 4,
        latin-flavored 3-2 shape), `kenny_barron` (3 hits per bar in a
        dotted-quarter polyrhythm).

The `render_rhythm` function tiles a template across a chord's duration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RhythmHit:
    offset_beats: float
    duration_beats: float
    velocity: int


# Each template is one bar long (4 beats). Tiled by the renderer.
TEMPLATES: dict[str, list[RhythmHit]] = {
    # ── Charleston family ──────────────────────────────────────────────────
    # Original Charleston: dotted-quarter on beat 1 + dotted-quarter on "and of 3"
    "charleston": [
        RhythmHit(0.0, 1.5, 90),     # beat 1
        RhythmHit(2.5, 1.5, 78),     # "and" of 3 (classic Charleston push)
    ],
    # NB: charleston_+1, charleston_+2, charleston_+3 are not rhythm templates
    # — they're handled by `resolve_rhythm()` below as "use the `charleston`
    # template, then apply a +N-beat GLOBAL shift to the entire output." This
    # replicates the effect of dragging the rendered comp N beats later in a
    # DAW: chord identities relative to the audio shift, producing
    # cool-but-dissonant harmonic re-mappings (Bill Evans-style wrong-bar comp).

    # ── Quarter-note family ────────────────────────────────────────────────
    # Freddie Green-style "two and four" chunks, no downbeats
    "two_and_four": [
        RhythmHit(1.0, 0.9, 70),     # beat 2
        RhythmHit(3.0, 0.9, 75),     # beat 4
    ],
    # Full Count Basie chunk: every quarter, with 2 and 4 emphasized (back-beat)
    "freddie_green": [
        RhythmHit(0.0, 0.9, 65),     # beat 1
        RhythmHit(1.0, 0.9, 78),     # beat 2 — emphasized
        RhythmHit(2.0, 0.9, 65),     # beat 3
        RhythmHit(3.0, 0.9, 78),     # beat 4 — emphasized
    ],

    # ── Pads ───────────────────────────────────────────────────────────────
    # One held chord per bar — for ballads or where comp should be furniture
    "sustained": [
        RhythmHit(0.0, 4.0, 72),
    ],

    # ── Syncopated comping ─────────────────────────────────────────────────
    # Anticipations: pushes on "and of 4" — forward-leaning, restless
    "anticipations": [
        RhythmHit(0.0, 1.5, 84),     # beat 1
        RhythmHit(2.0, 0.5, 68),     # beat 3 (light)
        RhythmHit(3.5, 0.5, 90),     # "and" of 4 — pushes into next chord
    ],
    # Reverse Charleston: "and of 1" + beat 3 — call-and-response w/ Charleston
    "reverse_charleston": [
        RhythmHit(0.5, 1.5, 78),     # "and" of 1
        RhythmHit(2.0, 1.5, 84),     # beat 3 — emphasized
    ],
    # Ahmad Jamal-style: 1 + 2 + "and of 3" — front-loaded then a push
    "ahmad_jamal": [
        RhythmHit(0.0, 0.9, 82),     # beat 1
        RhythmHit(1.0, 0.9, 70),     # beat 2
        RhythmHit(2.5, 1.5, 78),     # "and" of 3
    ],
    # Bossa-flavored 3-2 shape: 1 + "and of 2" + 4
    "bossa": [
        RhythmHit(0.0, 1.5, 80),     # beat 1
        RhythmHit(1.5, 1.0, 72),     # "and" of 2
        RhythmHit(3.0, 1.0, 76),     # beat 4
    ],
    # Kenny Barron-style polyrhythmic dotted-quarter pulse: 3 hits per bar
    # (4 beats / 1.5-beat spacing ≈ 3 hits per bar — produces a 3-against-4 feel)
    "kenny_barron": [
        RhythmHit(0.0, 1.5, 82),     # beat 1
        RhythmHit(1.5, 1.5, 76),     # "and" of 2
        RhythmHit(3.0, 1.0, 80),     # beat 4
    ],
}


_GLOBAL_SHIFT_RHYTHMS: dict[str, tuple[str, float]] = {
    # name → (underlying_template, global_shift_beats)
    "charleston_+1": ("charleston", 1.0),
    "charleston_+2": ("charleston", 2.0),
    "charleston_+3": ("charleston", 3.0),
}


def resolve_rhythm(name: str) -> tuple[str, float]:
    """Map a rhythm name to (template_name_to_use, global_shift_beats).

    For most names the global_shift_beats is 0. For special-cased names like
    `charleston_+1`, the underlying template is `charleston` and the shift is
    applied AFTER all rendering — which produces the dragged-region-in-DAW
    effect where chord identities re-map relative to the audio.
    """
    if name in TEMPLATES:
        return name, 0.0
    if name in _GLOBAL_SHIFT_RHYTHMS:
        return _GLOBAL_SHIFT_RHYTHMS[name]
    raise ValueError(f"unknown rhythm: {name!r}. Choices: {sorted(all_rhythm_names())}")


def all_rhythm_names() -> list[str]:
    """Every accepted rhythm name (template-backed plus global-shift variants)."""
    return sorted(set(TEMPLATES) | set(_GLOBAL_SHIFT_RHYTHMS))


def render_rhythm(template_name: str, chord_duration_beats: float) -> list[RhythmHit]:
    """Tile a 1-bar template across the chord's duration; clip the last bar if partial."""
    template = TEMPLATES.get(template_name)
    if template is None:
        raise ValueError(f"unknown rhythm template: {template_name!r}. "
                         f"Choices: {sorted(TEMPLATES)}")

    bar_len = 4.0
    out: list[RhythmHit] = []
    bar_start = 0.0
    while bar_start < chord_duration_beats - 1e-6:
        for hit in template:
            hit_start = bar_start + hit.offset_beats
            if hit_start >= chord_duration_beats - 1e-6:
                continue
            duration = min(hit.duration_beats, chord_duration_beats - hit_start)
            out.append(RhythmHit(offset_beats=hit_start, duration_beats=duration,
                                 velocity=hit.velocity))
        bar_start += bar_len
    return out

"""Rhythmic comping templates.

Each template maps a chord's duration into a list of (offset_beats, duration_beats,
velocity) hits. We tile the template across each chord's duration.

Templates included for v1:
    - 'charleston': "dah . . dah-dah" — beat 1 + the "and" of beat 2 (the classic)
    - 'two_and_four': simple 2-and-4 chunks (think Freddie Green big-band guitar)
    - 'sustained': one whole-bar pad (good for ballads / sparse comping)
    - 'anticipations': syncopated push on the "and" of beat 4 (anticipates next chord)

The `render_rhythm` function tiles a template across a chord's duration in
1-bar chunks. If a chord doesn't span a full bar, the template is truncated.
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
    "charleston": [
        RhythmHit(0.0, 1.5, 90),    # beat 1, dotted quarter
        RhythmHit(2.5, 1.5, 78),    # "and" of 3 (classic Charleston push)
    ],
    "two_and_four": [
        RhythmHit(1.0, 0.9, 70),    # beat 2
        RhythmHit(3.0, 0.9, 75),    # beat 4
    ],
    "sustained": [
        RhythmHit(0.0, 4.0, 72),
    ],
    "anticipations": [
        RhythmHit(0.0, 1.5, 84),
        RhythmHit(2.0, 0.5, 68),
        RhythmHit(3.5, 0.5, 90),    # the "and" of 4 — pushes into next chord
    ],
}


def render_rhythm(template_name: str, chord_duration_beats: float) -> list[RhythmHit]:
    """Tile a 1-bar template across the chord's duration; clip the last bar if partial."""
    template = TEMPLATES.get(template_name)
    if template is None:
        raise ValueError(f"unknown rhythm template: {template_name!r}")

    bar_len = 4.0
    out: list[RhythmHit] = []
    bar_start = 0.0
    while bar_start < chord_duration_beats - 1e-6:
        for hit in template:
            hit_start = bar_start + hit.offset_beats
            if hit_start >= chord_duration_beats - 1e-6:
                break
            duration = min(hit.duration_beats, chord_duration_beats - hit_start)
            out.append(RhythmHit(offset_beats=hit_start, duration_beats=duration, velocity=hit.velocity))
        bar_start += bar_len
    return out

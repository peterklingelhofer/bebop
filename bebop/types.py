"""Core data types shared across the pipeline.

A `Chord` is a symbolic chord with a duration in beats.
A `KeyChange` marks a key transition at a beat boundary.
A `ChordSequence` carries chords + a *piecewise* key map so songs that modulate
mid-piece (verse in D, bridge in F, etc.) are first-class — every downstream
stage looks up `seq.key_at(beat)` instead of assuming one global key.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Chord:
    """A symbolic chord placed on a beat grid.

    `symbol` is parsed lazily by music21 (e.g. "Cmaj7", "F#7b9", "Db13#11").
    `start_beat` is the absolute beat index from song start (0-indexed).
    `duration_beats` is in quarter-note beats.
    `bass` lets us encode slash chords like "C/G" without polluting `symbol`.
    """

    symbol: str
    start_beat: float
    duration_beats: float
    bass: str | None = None
    confidence: float = 1.0     # 0..1, used by ensemble voter to break ties

    @property
    def end_beat(self) -> float:
        return self.start_beat + self.duration_beats

    def with_symbol(self, new_symbol: str, *, bass: str | None = None) -> "Chord":
        return replace(self, symbol=new_symbol, bass=bass if bass is not None else self.bass)


@dataclass(frozen=True, slots=True)
class KeyChange:
    """A key transition. `key` is a tonic-centric string like "D", "Am", "Bb"."""

    start_beat: float
    key: str       # e.g. "D" major, "Am" minor (we use the trailing 'm' as the only minor marker)


@dataclass(slots=True)
class ChordSequence:
    """Time-ordered chords + tempo + a piecewise key map."""

    chords: list[Chord] = field(default_factory=list)
    bpm: float = 120.0
    time_signature: tuple[int, int] = (4, 4)
    key_map: list[KeyChange] = field(default_factory=list)  # sorted by start_beat

    @property
    def total_beats(self) -> float:
        return self.chords[-1].end_beat if self.chords else 0.0

    @property
    def beats_per_bar(self) -> int:
        return self.time_signature[0]

    @property
    def key(self) -> str | None:
        """Convenience: the *first* key, for backwards-compatible callers."""
        return self.key_map[0].key if self.key_map else None

    def key_at(self, beat: float) -> str | None:
        """Look up the active key at `beat`. Returns None if no key map declared."""
        active: str | None = None
        for kc in self.key_map:
            if kc.start_beat <= beat:
                active = kc.key
            else:
                break
        return active

    def __iter__(self) -> Iterable[Chord]:
        return iter(self.chords)

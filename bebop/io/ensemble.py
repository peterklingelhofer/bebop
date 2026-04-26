"""Multi-source chord-recognition consensus voter.

Take 2+ ChordSequences (e.g. one from MIDI, one from audio chromagram, one from
a hand chart) covering the same song and produce a single best-guess sequence.

Algorithm — for each beat-window of the canonical grid:
    1. Collect what each source says is sounding at that beat.
    2. Score candidate chords by:
        - vote count (sources that agree on root + quality bucket)
        - sum of source confidences
        - bonus when the chord is diatonic to the active key (taken from
          whichever source provides a key map; we prefer charts > MIDI > audio)
        - bonus for matching the previous accepted chord (favors stability /
          avoids per-beat thrash from one noisy source)
    3. Pick the highest-scoring candidate; coalesce identical adjacent picks.

This is intentionally not a Hidden Markov Model — that level of sophistication
gives marginal returns over the heuristic vote when you only have 2-3 sources.
"""

from __future__ import annotations

from dataclasses import dataclass

from bebop.reharm.substitutions import parse_root, quality_of
from bebop.types import Chord, ChordSequence, KeyChange


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A normalized vote: (root_pc, quality_bucket, full_symbol, bass, confidence)."""

    root_pc: int
    quality: str
    symbol: str
    bass: str | None
    confidence: float


def _normalize(c: Chord) -> _Candidate:
    root_pc, _ = parse_root(c.symbol)
    return _Candidate(
        root_pc=root_pc,
        quality=quality_of(c.symbol),
        symbol=c.symbol,
        bass=c.bass,
        confidence=c.confidence,
    )


def _chord_at(seq: ChordSequence, beat: float) -> Chord | None:
    """Return the chord sounding at `beat` in `seq`, or None if past the end."""
    for c in seq.chords:
        if c.start_beat <= beat < c.end_beat:
            return c
    return None


def _quality_diatonic(quality: str, root_pc: int, key_root_pc: int, key_is_major: bool) -> bool:
    """Is (root, quality) a "natural" chord in the given key?"""
    interval = (root_pc - key_root_pc) % 12
    if key_is_major:
        # I ii iii IV V vi vii°  -> intervals 0 2 4 5 7 9 11 with qualities maj min min maj maj/dom min dim
        expected = {0: ("maj",), 2: ("min",), 4: ("min",), 5: ("maj",),
                    7: ("maj", "dom"), 9: ("min",), 11: ("dim",)}
    else:
        # natural minor: i ii° III iv v VI VII -> 0 2 3 5 7 8 10
        expected = {0: ("min",), 2: ("dim",), 3: ("maj",), 5: ("min",),
                    7: ("min", "dom"), 8: ("maj",), 10: ("maj",)}
    return quality in expected.get(interval, ())


def _parse_key(key: str | None) -> tuple[int | None, bool]:
    if not key:
        return None, True
    is_minor = key.endswith("m")
    tonic_str = key[:-1] if is_minor else key
    try:
        from music21 import pitch as m21pitch
        return m21pitch.Pitch(tonic_str).pitchClass, not is_minor
    except Exception:
        return None, True


def _pick_key_map(sources: list[ChordSequence]) -> list[KeyChange]:
    """Prefer charts > MIDI > audio key maps. Whichever is non-empty wins."""
    for s in sources:
        if s.key_map:
            return s.key_map
    return []


def vote(
    *sources: ChordSequence,
    beats_per_window: float = 1.0,
    prev_chord_bonus: float = 0.15,
    diatonic_bonus: float = 0.10,
) -> ChordSequence:
    """Vote across `sources` to produce a single ChordSequence.

    `sources` should be ordered by trust (highest first), e.g.:
        vote(chart, midi_seq, audio_seq)
    """
    if not sources:
        return ChordSequence()

    # canonical bpm/time from first source (they should match if user set BPM consistently)
    first = sources[0]
    bpm = first.bpm
    time_sig = first.time_signature
    beats_per_bar = time_sig[0]
    key_map = _pick_key_map(list(sources))

    # canonical grid: start at 0, end at the latest end_beat across sources, step by `beats_per_window`
    end = max((s.total_beats for s in sources), default=0.0)
    if end <= 0:
        return ChordSequence(bpm=bpm, time_signature=time_sig, key_map=key_map)

    # source weights — prior trust by index (chart > midi > audio)
    source_weights = [1.0 / (1 + i * 0.5) for i in range(len(sources))]

    n_steps = int(end / beats_per_window) + 1
    out_chords: list[Chord] = []
    prev_pick: _Candidate | None = None

    for step in range(n_steps):
        beat = step * beats_per_window
        # collect candidates from each source
        cands: list[tuple[_Candidate, float]] = []  # (candidate, source_weight)
        for s, w in zip(sources, source_weights):
            ch = _chord_at(s, beat)
            if ch is None:
                continue
            cands.append((_normalize(ch), w))

        if not cands:
            continue

        # score every distinct (root_pc, quality) tuple
        # group cands by (root_pc, quality)
        scored: dict[tuple[int, str], dict] = {}
        for cand, w in cands:
            key_t = (cand.root_pc, cand.quality)
            entry = scored.setdefault(key_t, {"score": 0.0, "best_symbol": cand.symbol,
                                              "best_bass": cand.bass, "best_conf": 0.0})
            entry["score"] += w * (0.5 + 0.5 * cand.confidence)  # base + confidence-weighted
            # prefer the richer chord symbol when symbols agree on bucket
            if cand.confidence > entry["best_conf"]:
                entry["best_symbol"] = cand.symbol
                entry["best_bass"] = cand.bass
                entry["best_conf"] = cand.confidence

        # apply key + previous-chord bonuses
        active_key = None
        for kc in key_map:
            if kc.start_beat <= beat:
                active_key = kc.key
            else:
                break
        key_root_pc, key_is_major = _parse_key(active_key)

        for (root_pc, quality), entry in scored.items():
            if key_root_pc is not None and _quality_diatonic(quality, root_pc, key_root_pc, key_is_major):
                entry["score"] += diatonic_bonus
            if prev_pick is not None and prev_pick.root_pc == root_pc and prev_pick.quality == quality:
                entry["score"] += prev_chord_bonus

        # pick best
        (root_pc, quality), entry = max(scored.items(), key=lambda kv: kv[1]["score"])
        winner = _Candidate(
            root_pc=root_pc, quality=quality,
            symbol=entry["best_symbol"], bass=entry["best_bass"],
            confidence=min(1.0, entry["score"] / (sum(source_weights) + 0.5)),
        )
        prev_pick = winner

        out_chords.append(Chord(
            symbol=winner.symbol,
            start_beat=beat,
            duration_beats=beats_per_window,
            bass=winner.bass,
            confidence=winner.confidence,
        ))

    # coalesce identical adjacent windows
    coalesced: list[Chord] = []
    for c in out_chords:
        if coalesced and coalesced[-1].symbol == c.symbol and coalesced[-1].bass == c.bass \
                and abs(c.start_beat - coalesced[-1].end_beat) < 1e-6:
            prev = coalesced[-1]
            coalesced[-1] = Chord(
                symbol=prev.symbol,
                start_beat=prev.start_beat,
                duration_beats=prev.duration_beats + c.duration_beats,
                bass=prev.bass,
                confidence=min(prev.confidence, c.confidence),
            )
        else:
            coalesced.append(c)

    return ChordSequence(
        chords=coalesced,
        bpm=bpm,
        time_signature=time_sig,
        key_map=key_map,
    )

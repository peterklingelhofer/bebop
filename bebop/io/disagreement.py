"""Source-disagreement reporting + auto-suggested chord chart writer.

Given multiple ChordSequence inputs from different recognizers, produce:
    - A side-by-side per-bar table for human auditing
    - A "disagreement score" per bar to highlight where to listen carefully
    - A suggested chord chart file in our own format, derived from the
      ensemble vote, that you can drop into charts/ and edit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from bebop.reharm.substitutions import parse_root, quality_of
from bebop.types import ChordSequence


@dataclass
class _Cell:
    label: str
    root_pc: int | None
    quality: str | None


def _cell_at(seq: ChordSequence, beat: float) -> _Cell:
    for c in seq.chords:
        if c.start_beat <= beat < c.end_beat:
            label = c.symbol + (f"/{c.bass}" if c.bass else "")
            try:
                root_pc, _ = parse_root(c.symbol)
                return _Cell(label=label, root_pc=root_pc, quality=quality_of(c.symbol))
            except Exception:
                return _Cell(label=label, root_pc=None, quality=None)
    return _Cell(label="--", root_pc=None, quality=None)


def disagreement_table(
    sources: dict[str, ChordSequence],
    *,
    beats_per_bar: int = 4,
    sample_offset_in_bar: float = 1.0,
) -> list[tuple[int, dict[str, str], int]]:
    """Return a list of (bar, source_label_map, disagreement_score) per bar.

    `disagreement_score` = number of distinct (root, quality) tuples among the
    sources at that bar. 1 = unanimous; len(sources) = total chaos.
    """
    end_bar = max(int(s.total_beats // beats_per_bar) + 1 for s in sources.values())
    rows: list[tuple[int, dict[str, str], int]] = []
    for bar in range(end_bar):
        beat = bar * beats_per_bar + sample_offset_in_bar
        labels = {name: _cell_at(s, beat) for name, s in sources.items()}
        signatures = {(c.root_pc, c.quality) for c in labels.values() if c.root_pc is not None}
        score = len(signatures) if signatures else 1
        rows.append((bar + 1, {name: c.label for name, c in labels.items()}, score))
    return rows


def print_disagreement_report(sources: dict[str, ChordSequence]) -> None:
    """Print the per-bar agreement table, sorted by disagreement score (descending)."""
    rows = disagreement_table(sources)
    if not rows:
        return
    names = list(sources)
    header = f"{'bar':>3}  {'agree':>5}  " + "  ".join(f"{n[:8]:<8}" for n in names)
    print("\n=== DISAGREEMENT REPORT (sorted: noisiest bars first) ===")
    print(header)
    print("-" * len(header))
    rows_sorted = sorted(rows, key=lambda r: (-r[2], r[0]))
    for bar, labels, score in rows_sorted[:20]:
        agree_str = f"{len(names)-score+1}/{len(names)}" if score > 1 else "all"
        chord_cells = "  ".join(f"{labels.get(n, '--'):<8}" for n in names)
        print(f"{bar:>3}  {agree_str:>5}  {chord_cells}")
    if len(rows_sorted) > 20:
        print(f"... +{len(rows_sorted) - 20} more bars (most agreed-upon hidden)")


def write_suggested_chart(
    consensus: ChordSequence,
    output_path: str | Path,
    *,
    title: str = "auto-generated from ensemble consensus",
) -> Path:
    """Write a human-editable chord chart in our own format from a consensus sequence."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("# Edit freely — this is a starting point from ensemble vote.")
    lines.append("")
    lines.append(f"bpm: {consensus.bpm:.0f}")
    lines.append(f"time: {consensus.time_signature[0]}/{consensus.time_signature[1]}")
    if consensus.key_map:
        lines.append(f"key: {consensus.key_map[0].key}")
    lines.append("")

    # bin chords into bars; emit one bar per pipe-segment
    bpb = consensus.beats_per_bar
    end_bar = int(consensus.total_beats // bpb) + 1
    last_key: str | None = consensus.key_map[0].key if consensus.key_map else None
    bars_buffer: list[str] = []

    for bar in range(end_bar):
        # mid-song key change?
        for kc in consensus.key_map:
            kc_bar = int(kc.start_beat // bpb)
            if kc_bar == bar and kc.key != last_key:
                # flush current line, then write key change, then continue
                if bars_buffer:
                    lines.append("| " + " | ".join(bars_buffer) + " |")
                    bars_buffer = []
                lines.append(f"key: {kc.key}")
                last_key = kc.key

        # collect chords sounding in this bar, ordered by start_beat
        bar_start = bar * bpb
        bar_end = bar_start + bpb
        chords_in_bar = [c for c in consensus.chords
                         if c.start_beat < bar_end and c.end_beat > bar_start]
        if not chords_in_bar:
            bars_buffer.append("-")
        else:
            chord_strs = []
            for c in chords_in_bar:
                sym = c.symbol + (f"/{c.bass}" if c.bass else "")
                chord_strs.append(sym)
            bars_buffer.append(" ".join(chord_strs))

        # flush every 4 bars for readability
        if len(bars_buffer) == 4:
            lines.append("| " + " | ".join(bars_buffer) + " |")
            bars_buffer = []

    if bars_buffer:
        lines.append("| " + " | ".join(bars_buffer) + " |")

    output_path.write_text("\n".join(lines) + "\n")
    return output_path

"""Run the full bench suite, write a CSV summary, and print headlines.

The CSV is what makes this tool useful for autonomous iteration: bebop changes
can be A/B'd by diffing two CSVs (`output/eval/baseline.csv` vs
`output/eval/current.csv`), with no human in the loop required.

Usage:
    uv run python -m tests.eval.run_all                # write current.csv
    uv run python -m tests.eval.run_all --label baseline   # snapshot a baseline
    uv run python -m tests.eval.run_all --diff baseline    # current vs baseline.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from tests.eval.coherence_bench import run_coherence_bench
from tests.eval.comp_bench import run_comp_bench
from tests.eval.recognition_bench import run_recognition_bench
from tests.eval.synth import EVAL_DIR


def _flatten_to_rows(*, recognition: dict, comp: dict, coherence: dict) -> list[dict]:
    """Flatten the three bench results into one CSV-friendly row list:
        bench, fixture, key, metric, value
    """
    rows: list[dict] = []
    for fixture, modes in recognition.items():
        for mode in ("offline", "live"):
            d = modes.get(mode)
            if not d:
                continue
            for k, v in d.items():
                rows.append({"bench": "recognition", "fixture": fixture,
                             "config": mode, "metric": k, "value": v})
        if "live_lag_s" in modes:
            rows.append({"bench": "recognition", "fixture": fixture,
                         "config": "live", "metric": "lag_s",
                         "value": modes["live_lag_s"]})

    for fixture, cells in comp.items():
        for cfg, metrics in cells.items():
            for k, v in metrics.items():
                rows.append({"bench": "comp", "fixture": fixture,
                             "config": cfg, "metric": k, "value": v})

    for fixture, cells in coherence.items():
        for cfg, metrics in cells.items():
            for k, v in metrics.items():
                rows.append({"bench": "coherence", "fixture": fixture,
                             "config": cfg, "metric": k, "value": v})
    return rows


def _write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["bench", "fixture", "config", "metric", "value"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _read_csv(path: Path) -> dict[tuple, float]:
    """Return dict keyed by (bench, fixture, config, metric) → value."""
    out: dict[tuple, float] = {}
    with path.open() as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                out[(row["bench"], row["fixture"], row["config"], row["metric"])] = float(row["value"])
            except ValueError:
                pass
    return out


def _diff(current: dict, baseline: dict, *, threshold: float = 0.01) -> list[tuple]:
    """Return [(key, current, baseline, delta)] sorted by absolute delta desc.
    Skips changes smaller than `threshold` so the report stays readable."""
    out: list[tuple] = []
    keys = sorted(set(current) | set(baseline))
    for k in keys:
        c = current.get(k)
        b = baseline.get(k)
        if c is None or b is None:
            continue
        delta = c - b
        if abs(delta) >= threshold:
            out.append((k, c, b, delta))
    out.sort(key=lambda x: -abs(x[3]))
    return out


def _print_summary_headlines(*, recognition: dict, comp: dict, coherence: dict) -> None:
    """Print a one-line-per-bench top-of-funnel summary."""
    if recognition:
        triads = []
        live_triads = []
        for fix, modes in recognition.items():
            if modes.get("offline"):
                triads.append(modes["offline"]["triad"])
            if modes.get("live"):
                live_triads.append(modes["live"]["triad"])
        avg_off = sum(triads) / max(1, len(triads))
        avg_live = sum(live_triads) / max(1, len(live_triads))
        print(f"  recognition triad accuracy: offline {avg_off*100:.1f}% · live {avg_live*100:.1f}%")
    if comp:
        memberships = []
        for fix, cells in comp.items():
            for cfg, m in cells.items():
                memberships.append(m["chord_membership"])
        avg = sum(memberships) / max(1, len(memberships))
        print(f"  comp chord-membership avg: {avg*100:.1f}%  ({len(memberships)} cells)")
    if coherence:
        subsets = []
        for fix, cells in coherence.items():
            for cfg, m in cells.items():
                subsets.append(m["subset_score"])
        avg = sum(subsets) / max(1, len(subsets))
        print(f"  comp/song chroma subset avg: {avg:.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="bebop-eval")
    parser.add_argument("--label", default="current",
                        help="CSV filename stem under output/eval/. "
                             "Use 'baseline' to snapshot. Default: 'current'.")
    parser.add_argument("--diff", default=None,
                        help="Compare results against output/eval/<DIFF>.csv after running. "
                             "e.g. --diff baseline.")
    parser.add_argument("--diff-threshold", type=float, default=0.01,
                        help="Skip diffs smaller than this absolute value. Default 0.01.")
    parser.add_argument("--skip", default="",
                        help="Comma-separated benches to skip (recognition,comp,coherence)")
    args = parser.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    recognition: dict = {} if "recognition" in skip else run_recognition_bench()
    comp: dict = {} if "comp" in skip else run_comp_bench()
    coherence: dict = {} if "coherence" in skip else run_coherence_bench()

    rows = _flatten_to_rows(recognition=recognition, comp=comp, coherence=coherence)
    out_csv = EVAL_DIR / f"{args.label}.csv"
    _write_csv(rows, out_csv)

    print("=" * 50)
    print(f"summary  ({len(rows)} metrics → {out_csv})")
    _print_summary_headlines(recognition=recognition, comp=comp, coherence=coherence)

    if args.diff is not None:
        baseline_path = EVAL_DIR / f"{args.diff}.csv"
        if not baseline_path.exists():
            print(f"\n  no baseline at {baseline_path} — run with --label {args.diff} first.")
            return 0
        current = _read_csv(out_csv)
        baseline = _read_csv(baseline_path)
        diffs = _diff(current, baseline, threshold=args.diff_threshold)
        print(f"\n=== diff vs {args.diff}.csv ({len(diffs)} changes >= {args.diff_threshold}) ===")
        for (bench, fix, cfg, metric), c, b, delta in diffs[:30]:
            sign = "+" if delta > 0 else ""
            print(f"  [{bench:<11}] {fix:<14} {cfg:<22} {metric:<18}  "
                  f"{b:>7.3f} → {c:>7.3f}  ({sign}{delta:+.3f})")
        if len(diffs) > 30:
            print(f"  … {len(diffs) - 30} more (see {out_csv})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

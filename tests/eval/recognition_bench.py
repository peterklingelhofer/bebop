"""Recognition bench: how accurately do parse_audio + the live ChordStream
identify chords from synthesized audio?

For each fixture we:
    1. Render block-chord audio via fluidsynth (deterministic)
    2. Run BOTH the offline parse_audio AND the live chord stream over it
    3. Score against the ground truth at 4 levels of strictness:

           exact    — predicted symbol equals ground-truth symbol
           pcs      — predicted pitch-class set equals ground truth's
           root     — predicted root equals ground-truth root
           triad    — predicted root + 3rd-quality (maj/min/dim/aug) match

`exact` and `pcs` will be low (typically 0-50%) and that is *not a bug*.
Chroma can't reliably distinguish Cmaj7 from C6 from bare C — the 7th's
contribution to the chromagram is small and easily lost in CQT noise + HPSS
leakage. Realistic targets here are root (100%) and triad-quality (>90%);
the comp engine's voicing layer (rootless / evans / drop2 / quartal) adds
its own 7ths from the chord symbol regardless of whether the recognizer
identified one, so this is rarely audible in practice.

Also reports detection latency for the live stream: seconds elapsed from
chord onset (in the audio) to when the recognizer committed to it.
"""

from __future__ import annotations

from dataclasses import dataclass

from bebop.io.audio_in import parse_audio

from tests.eval.synth import (
    CapturedRun,
    chord_pcs,
    chord_root_pc,
    feed_wav_to_chord_stream,
    fixtures,
    fmt_n,
    fmt_pct,
    synthesize,
)


@dataclass
class Score:
    exact: float
    pcs: float
    root: float
    triad: float
    n: int

    @classmethod
    def from_pairs(cls, predicted: list[str], truth: list[str]) -> "Score":
        n = min(len(predicted), len(truth))
        if n == 0:
            return cls(0, 0, 0, 0, 0)
        ex = pcs_eq = root = triad = 0
        for p, t in zip(predicted[:n], truth[:n]):
            if p == t:
                ex += 1
            try:
                if chord_pcs(p) == chord_pcs(t):
                    pcs_eq += 1
                if chord_root_pc(p) == chord_root_pc(t):
                    root += 1
                    if _triad_quality(p) == _triad_quality(t):
                        triad += 1
            except Exception:
                pass
        return cls(ex / n, pcs_eq / n, root / n, triad / n, n)


def _triad_quality(symbol: str) -> str:
    """min/maj/dim/aug — collapses 7th-chord variants down to triad family."""
    if len(symbol) >= 2 and symbol[1] in "#b":
        suffix = symbol[2:]
    else:
        suffix = symbol[1:]
    if suffix.startswith("dim"):
        return "dim"
    if suffix.startswith("aug"):
        return "aug"
    if suffix.startswith("m") and not suffix.startswith("maj"):
        return "min"
    return "maj"


def _align_to_truth(pred_symbols: list[str], pred_starts: list[float],
                     truth_starts: list[float], truth_symbols: list[str]) -> list[str]:
    """Pick one predicted chord per truth-chord-window (whichever pred chord
    occupies the most time in that window)."""
    out: list[str] = []
    for i, t_start in enumerate(truth_starts):
        t_end = truth_starts[i + 1] if i + 1 < len(truth_starts) else float("inf")
        # pick predictions whose start falls in [t_start, t_end)
        candidates = [s for s, st in zip(pred_symbols, pred_starts)
                      if t_start <= st < t_end]
        out.append(candidates[0] if candidates else (pred_symbols[0] if pred_symbols else ""))
    return out


def score_offline(fixture_name: str, seq, wav) -> Score:
    detected = parse_audio(wav, bpm=seq.bpm,
                           windows_per_bar=1, beats_per_bar=4,
                           prefer_flats=False)
    pred_starts = [c.start_beat for c in detected.chords]
    pred_syms = [c.symbol for c in detected.chords]
    truth_starts = [c.start_beat for c in seq.chords]
    truth_syms = [c.symbol for c in seq.chords]
    aligned = _align_to_truth(pred_syms, pred_starts, truth_starts, truth_syms)
    return Score.from_pairs(aligned, truth_syms)


def score_live(fixture_name: str, seq, wav) -> tuple[Score, float, CapturedRun]:
    """Returns (score, mean_detection_latency_seconds, captured_run).

    Uses the audio-time sync feeder, so event timestamps and truth times are
    in the same scale. `mean_detection_latency` is averaged per-truth-chord:
    for each truth onset, how long after it did the recognizer commit the
    matching chord? (Skipped truth chords don't penalize this metric.)
    """
    spb = 60.0 / seq.bpm
    truth_starts_sec = [c.start_beat * spb for c in seq.chords]
    truth_ends_sec = [t + 4 * spb for t in truth_starts_sec]
    truth_syms = [c.symbol for c in seq.chords]

    run = feed_wav_to_chord_stream(wav)
    if not run.events:
        return Score(0, 0, 0, 0, len(truth_syms)), float("nan"), run

    # event.detected_at is in audio seconds (set by the sync feeder)
    pred_starts = [ev.detected_at for ev in run.events]
    pred_syms = [ev.symbol for ev in run.events]

    # one prediction per truth chord: pick the FIRST event whose onset falls
    # in [truth_start, truth_end). If none, fall back to the most-recent
    # earlier prediction (= the chord that was "still sounding" at the truth
    # onset).
    aligned: list[str] = []
    for t_start, t_end in zip(truth_starts_sec, truth_ends_sec):
        in_window = [s for s, t in zip(pred_syms, pred_starts)
                     if t_start <= t < t_end]
        if in_window:
            aligned.append(in_window[0])
            continue
        carryover = [s for s, t in zip(pred_syms, pred_starts) if t < t_start]
        aligned.append(carryover[-1] if carryover else "")

    score = Score.from_pairs(aligned, truth_syms)

    # latency: per-truth-chord, the gap between truth onset and the first
    # matching prediction (root-level match). Captures responsiveness.
    from tests.eval.synth import chord_root_pc
    lags: list[float] = []
    for t_start, t_sym in zip(truth_starts_sec, truth_syms):
        for s, t in zip(pred_syms, pred_starts):
            if t < t_start:
                continue
            try:
                if chord_root_pc(s) == chord_root_pc(t_sym):
                    lags.append(t - t_start)
                    break
            except Exception:
                continue
    mean_lag = sum(lags) / max(1, len(lags)) if lags else float("nan")
    return score, mean_lag, run


def run_recognition_bench() -> dict:
    """Run the bench, print a table, return results as a dict for run_all to
    aggregate."""
    print("\n=== recognition bench ===")
    print(f"{'fixture':<18}  {'mode':<8}  {'n':>3}  {'exact':>6}  {'pcs':>6}  {'root':>6}  {'triad':>6}  {'lag(s)':>7}")
    print("-" * 76)
    results: dict[str, dict] = {}
    for name, seq in fixtures().items():
        wav = synthesize(seq, name)
        try:
            off = score_offline(name, seq, wav)
        except Exception as e:
            print(f"{name:<18}  offline   ERR  {e}")
            off = None
        try:
            live, lag, _ = score_live(name, seq, wav)
        except Exception as e:
            print(f"{name:<18}  live      ERR  {e}")
            live = None
            lag = float("nan")

        if off is not None:
            print(f"{name:<18}  {'offline':<8}  {off.n:>3}  {fmt_pct(off.exact)}  "
                  f"{fmt_pct(off.pcs)}  {fmt_pct(off.root)}  {fmt_pct(off.triad)}  {'—':>7}")
        if live is not None:
            print(f"{name:<18}  {'live':<8}  {live.n:>3}  {fmt_pct(live.exact)}  "
                  f"{fmt_pct(live.pcs)}  {fmt_pct(live.root)}  {fmt_pct(live.triad)}  "
                  f"{fmt_n(lag, w=7, p=2)}")
        results[name] = {
            "offline": off.__dict__ if off else None,
            "live": live.__dict__ if live else None,
            "live_lag_s": lag,
        }
    print()
    return results


if __name__ == "__main__":
    run_recognition_bench()

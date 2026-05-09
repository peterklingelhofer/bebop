"""Sweep `stability_frames` and report per-fixture accuracy + lag.

Picks the best tradeoff between flicker-suppression and detection latency.
"""

from __future__ import annotations

from tests.eval.recognition_bench import score_live
from tests.eval.synth import feed_wav_to_chord_stream, fixtures, synthesize


def _patched_feeder(stability_frames: int):
    """Return a feeder closure that uses the requested stability_frames."""
    def feed(wav, **kw):
        kw.setdefault("stability_frames", stability_frames)
        return feed_wav_to_chord_stream(wav, **kw)
    return feed


def main():
    print(f"{'fixture':<14}  {'stab':<4}  {'n_ev':>4}  {'root':>5}  {'triad':>5}  {'lag(s)':>6}  {'flicker':>7}")
    print("-" * 64)

    # we patch the score_live's feeder dynamically by monkey-patching the
    # imported feeder at call time
    import tests.eval.recognition_bench as rb
    original_feeder = rb.feed_wav_to_chord_stream

    rows: list[tuple[str, int, dict]] = []
    for stability in (1, 2, 3):
        rb.feed_wav_to_chord_stream = _patched_feeder(stability)
        for name, seq in fixtures().items():
            wav = synthesize(seq, name)
            score, lag, run = score_live(name, seq, wav)
            n_truth = len(seq.chords)
            # flicker proxy: extra events beyond what's needed to capture
            # every chord change. n_truth-runs are the irreducible minimum.
            n_runs = sum(1 for i, c in enumerate(seq.chords)
                         if i == 0 or c.symbol != seq.chords[i-1].symbol)
            n_events = len(run.events)
            flicker = max(0, n_events - n_runs) / max(1, n_runs)
            print(f"{name:<14}  {stability:<4}  {n_events:>4}  "
                  f"{score.root*100:5.1f}  {score.triad*100:5.1f}  "
                  f"{lag:6.2f}  {flicker:7.2f}")
        print()

    rb.feed_wav_to_chord_stream = original_feeder


if __name__ == "__main__":
    main()

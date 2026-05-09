"""Diagnostic: dump exactly what the live ChordStream sees for each fixture.

Used during iteration only — print each detected event with timestamp + the
ground-truth chord active at that audio time. Helps tell apart:
    - root errors (wrong fundamental)
    - quality errors (right root, wrong triad/7th type)
    - timing errors (right chord, just lagging or ahead of the bar)
    - duplicates (same chord re-committed multiple times)
"""

from __future__ import annotations

from tests.eval.synth import (
    chord_root_pc,
    feed_wav_to_chord_stream,
    fixtures,
    synthesize,
)


def main():
    for name, seq in fixtures().items():
        print(f"\n=== {name} (bpm={seq.bpm}) ===")
        wav = synthesize(seq, name)
        run = feed_wav_to_chord_stream(wav)
        spb = 60.0 / seq.bpm
        truth = [(c.start_beat * spb, c.symbol) for c in seq.chords]
        if not run.events:
            print("  (no live events)")
            continue
        print("  truth → live:")
        ti = 0
        for ev in run.events:
            t = ev.detected_at  # audio seconds (from sync feeder)
            while ti + 1 < len(truth) and truth[ti + 1][0] <= t:
                ti += 1
            t_sym = truth[ti][1]
            root_match = chord_root_pc(ev.symbol) == chord_root_pc(t_sym)
            mark = "✓" if root_match else "✗"
            print(f"    {mark} t={t:5.2f}s  truth={t_sym:<8}  live={ev.symbol:<8}  "
                  f"key={ev.key:<4}  conf={ev.confidence:.2f}")


if __name__ == "__main__":
    main()

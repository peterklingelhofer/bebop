"""Diagnose what pitch classes the comp plays that aren't in the song's
chroma top-3. Outputs the most frequent "outside" pitch classes per cell.
"""

from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np

from tests.eval.coherence_bench import _chroma_of
from tests.eval.synth import EVAL_DIR, fixtures, synthesize


_PC_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def diag(name: str, voicing: str, rhythm: str):
    seq = fixtures()[name]
    song_wav = synthesize(seq, name)
    comp_mid = EVAL_DIR / f"comp_{name}_{voicing}_{rhythm}_s00.mid"
    comp_wav = comp_mid.with_suffix(".wav")
    if not comp_wav.exists():
        from bebop.render import render_midi_to_wav
        if not comp_mid.exists():
            from tests.eval.comp_bench import render_and_score
            render_and_score(seq, name, voicing=voicing, rhythm=rhythm)
        render_midi_to_wav(comp_mid, comp_wav, sample_rate=22050, gain=0.4)

    song = _chroma_of(song_wav)
    comp = _chroma_of(comp_wav)
    n = min(song.shape[1], comp.shape[1])
    song = song[:, :n]
    comp = comp[:, :n]

    # for each frame, what is comp's argmax pc, and is it in song's top-3
    song_top3 = np.argsort(-song, axis=0)[:3, :]
    comp_top1 = np.argmax(comp, axis=0)
    in_top3 = np.any(song_top3 == comp_top1[None, :], axis=0)

    outside_frames = ~in_top3
    if not outside_frames.any():
        print(f"  {voicing}/{rhythm}: all comp argmax pcs ∈ song top-3")
        return

    print(f"\n{name} · {voicing}/{rhythm} ({outside_frames.sum()}/{n} frames outside, "
          f"{100*outside_frames.mean():.1f}%)")
    # frequency histogram of comp argmax during outside frames
    hist = np.bincount(comp_top1[outside_frames], minlength=12)
    pairs = sorted([(c, _PC_NAMES[i], i) for i, c in enumerate(hist) if c > 0],
                   reverse=True)
    print("  comp's outside argmax pcs (count, pc_name):")
    for c, n_str, pc in pairs[:5]:
        # what was the song's strongest pc in those frames?
        mask = outside_frames & (comp_top1 == pc)
        song_dom = np.argmax(song[:, mask].mean(axis=1)) if mask.sum() > 0 else -1
        print(f"    {n_str:<3}  count={c:<4}  song's dominant pc here: {_PC_NAMES[song_dom] if song_dom >= 0 else '—'}")


def main():
    # the worst offenders from the coherence bench
    for fixture, voicing, rhythm in [
        ("ii_V_I_C", "rootless", "charleston"),
        ("ii_V_I_C", "evans", "charleston"),
        ("dorian_vamp_D", "rootless", "charleston"),
        ("dorian_vamp_D", "evans", "charleston"),
        ("ii_V_i_Dm", "drop2", "freddie_green"),
    ]:
        diag(fixture, voicing, rhythm)


if __name__ == "__main__":
    main()

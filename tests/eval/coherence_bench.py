"""Coherence bench: does the comp's harmonic content live inside the song's?

For each fixture we render the synthesized song audio + the comp's audio,
align them, and frame-by-frame compute:

    subset_score      — cosine similarity of normalized chromas, averaged
                        over time. Captures whether the comp and song are
                        emphasizing the same pitch classes overall. >0.85 is
                        "comp lives in the song's harmonic neighborhood."

    silent_pc_mass    — the share of comp's chroma mass that lands on pitch
                        classes where the song is near-silent. This is the
                        signal you actually want: "is comp playing notes the
                        song doesn't?" Robust to comp/song register mismatch
                        (e.g. song has loud bass + light comp keys; comp's
                        loudest pc is the chord 3rd while song's is the bass
                        root — both are valid, no real clash).

    argmax_outside_top3  — legacy register-clash detector: how often does
                        comp's argmax pc fall outside the song's top-3 pcs.
                        Sensitive to register/voicing mismatch; not a
                        chord-correctness signal. Kept because it useful
                        catches gross perturbations (e.g. bass-octave bugs).
"""

from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np

from bebop.render import render_midi_to_wav

from tests.eval.synth import EVAL_DIR, fixtures, fmt_n, fmt_pct, synthesize


def _chroma_of(wav_path: Path, sr: int = 22050) -> np.ndarray:
    """Return a 12 × T chromagram, harmonic-only, normalized per frame."""
    y, _ = librosa.load(str(wav_path), sr=sr, mono=True)
    y_h = librosa.effects.harmonic(y, margin=2.0)
    chroma = librosa.feature.chroma_cqt(y=y_h, sr=sr, hop_length=512,
                                        bins_per_octave=36)
    sums = chroma.sum(axis=0, keepdims=True)
    sums[sums < 1e-9] = 1.0
    return chroma / sums


def _coherence_metrics(song_chroma: np.ndarray, comp_chroma: np.ndarray,
                        *, song_silence_threshold: float = 0.04) -> dict:
    """Compare two normalized chromagrams.

    `song_silence_threshold` (per-frame normalized chroma value) defines
    "song is near-silent on this pc" for the silent_pc_mass metric. 0.04
    means the song allocates less than 4% of its frame mass to that pc —
    well below any real chord pitch's contribution.
    """
    n = min(song_chroma.shape[1], comp_chroma.shape[1])
    if n == 0:
        return {"subset_score": float("nan"),
                "silent_pc_mass": float("nan"),
                "argmax_outside_top3": float("nan")}
    song = song_chroma[:, :n]
    comp = comp_chroma[:, :n]

    # subset_score: cosine similarity of normalized chromas, averaged over time
    subset = float(np.mean(np.sum(song * comp, axis=0) /
                            (np.linalg.norm(song, axis=0) *
                             np.linalg.norm(comp, axis=0) + 1e-9)))

    # silent_pc_mass: fraction of comp's chroma mass on pcs where the song
    # is near-silent. Robust to register/voicing mismatch.
    silent_mask = song < song_silence_threshold
    outside_mass = float((comp * silent_mask).sum())
    total_comp = float(comp.sum())
    silent_pc_mass = outside_mass / max(1e-9, total_comp)

    # argmax_outside_top3: legacy register-clash signal
    song_top3 = np.argsort(-song, axis=0)[:3, :]
    comp_top1 = np.argmax(comp, axis=0)
    in_top3 = np.any(song_top3 == comp_top1[None, :], axis=0)
    argmax_outside = float(1.0 - np.mean(in_top3))
    return {"subset_score": subset,
            "silent_pc_mass": silent_pc_mass,
            "argmax_outside_top3": argmax_outside}


def run_coherence_bench() -> dict:
    """Render comp + song separately, then compare their chromagrams."""
    print("\n=== coherence bench ===")
    print(f"{'fixture':<14}  {'voicing':<8}  {'rhythm':<14}  "
          f"{'subset':>7}  {'silent':>7}  {'top3-out':>9}")
    print("-" * 65)
    cells = [
        ("rootless", "charleston"),
        ("evans", "charleston"),
        ("drop2", "freddie_green"),
        ("quartal", "sustained"),
    ]
    results: dict[str, dict] = {}
    for name, seq in fixtures().items():
        song_wav = synthesize(seq, name)
        song_chroma = _chroma_of(song_wav)
        for voicing, rhythm in cells:
            mid = EVAL_DIR / f"comp_{name}_{voicing}_{rhythm}_s00.mid"
            if not mid.exists():
                # comp_bench creates these; if user runs coherence first,
                # render on the fly via the same path
                from tests.eval.comp_bench import render_and_score
                render_and_score(seq, name, voicing=voicing, rhythm=rhythm)
            comp_wav = mid.with_suffix(".wav")
            if not comp_wav.exists():
                try:
                    render_midi_to_wav(mid, comp_wav, sample_rate=22050, gain=0.4)
                except Exception as e:
                    print(f"{name:<14}  {voicing:<8}  {rhythm:<14}  ERR rendering: {e}")
                    continue
            comp_chroma = _chroma_of(comp_wav)
            m = _coherence_metrics(song_chroma, comp_chroma)
            print(f"{name:<14}  {voicing:<8}  {rhythm:<14}  "
                  f"{fmt_n(m['subset_score'], w=7, p=3)}  "
                  f"{fmt_pct(m['silent_pc_mass']):>7}  "
                  f"{fmt_pct(m['argmax_outside_top3']):>9}")
            results.setdefault(name, {})[f"{voicing}/{rhythm}"] = m
    print()
    return results


if __name__ == "__main__":
    run_coherence_bench()

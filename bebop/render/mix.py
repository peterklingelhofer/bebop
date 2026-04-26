"""Mix two audio files into a single stereo WAV via ffmpeg's amix.

Usage:
    mix_with_original("comp.wav", "song.wav", "out.wav", comp_gain_db=-3, song_gain_db=0)

ffmpeg is preferred over pydub because it's faster, handles sample-rate
conversion automatically, and supports per-input gain via the `volume` filter.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def mix_audio(
    comp_wav: str | Path,
    original_wav: str | Path,
    output_wav: str | Path,
    *,
    comp_gain_db: float = -2.0,
    original_gain_db: float = -1.0,
) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("`ffmpeg` not found on PATH. Install with: brew install ffmpeg")

    output_wav = Path(output_wav)
    output_wav.parent.mkdir(parents=True, exist_ok=True)

    # ffmpeg filter: apply per-input volume in dB, then amix with normalized=0 so we don't
    # compress dynamics. We use longest=1 so silence at the end of the shorter input doesn't
    # truncate the output.
    fc = (
        f"[0:a]volume={comp_gain_db}dB[c];"
        f"[1:a]volume={original_gain_db}dB[s];"
        f"[c][s]amix=inputs=2:normalize=0:duration=longest[out]"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(comp_wav),
        "-i", str(original_wav),
        "-filter_complex", fc,
        "-map", "[out]",
        "-c:a", "pcm_s16le",
        str(output_wav),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg mix failed: {proc.stderr.strip()}")
    return output_wav

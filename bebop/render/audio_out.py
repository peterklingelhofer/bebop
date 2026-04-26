"""Render a MIDI file to audio (.wav) via the fluidsynth CLI + a SoundFont.

Uses the binary `fluidsynth` (installed via Homebrew) rather than the Python
bindings — the CLI is more reliable on macOS and fully supports gain control,
sample-rate selection, and dry rendering.

Soundfont discovery order:
    1. Explicit `soundfont_path` argument
    2. $BEBOP_SOUNDFONT environment variable
    3. Common system locations (e.g. /usr/local/share/sounds/sf2/)
    4. ~/.bebop/soundfonts/*.sf2
A clear error is raised if none is found, with a one-liner pointing at a
public-domain piano soundfont download.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_SOUNDFONT_HINT = (
    "No SoundFont (.sf2) found. Either:\n"
    "  - pass --soundfont /path/to/piano.sf2,\n"
    "  - set BEBOP_SOUNDFONT=/path/to/piano.sf2, or\n"
    "  - drop a .sf2 file into ~/.bebop/soundfonts/.\n"
    "A solid free option: `curl -L -o ~/.bebop/soundfonts/FluidR3_GM.sf2 \\\n"
    "  https://archive.org/download/fluidr3-gm-gs/FluidR3_GM.sf2`"
)

_SEARCH_DIRS = (
    Path("/usr/local/share/sounds/sf2"),
    Path("/usr/share/sounds/sf2"),
    Path("/Library/Audio/Sounds/Banks"),  # Logic / GarageBand
    Path.home() / ".bebop" / "soundfonts",
    Path.home() / "Library" / "Audio" / "Sounds" / "Banks",
)


def find_soundfont(explicit: str | Path | None = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
        raise FileNotFoundError(f"--soundfont path does not exist: {p}")
    env = os.environ.get("BEBOP_SOUNDFONT")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p
    for d in _SEARCH_DIRS:
        if d.is_dir():
            for sf in sorted(d.glob("*.sf2")):
                return sf
    raise FileNotFoundError(_SOUNDFONT_HINT)


def render_midi_to_wav(
    midi_path: str | Path,
    wav_path: str | Path,
    *,
    soundfont_path: str | Path | None = None,
    sample_rate: int = 44100,
    gain: float = 0.6,
) -> Path:
    """Render `midi_path` to a 16-bit stereo WAV at `wav_path` via fluidsynth."""
    if shutil.which("fluidsynth") is None:
        raise RuntimeError("`fluidsynth` not found on PATH. Install with: brew install fluidsynth")
    sf = find_soundfont(soundfont_path)

    midi_path = Path(midi_path)
    wav_path = Path(wav_path)
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "fluidsynth",
        "-ni",                      # no shell, no interactive
        "-F", str(wav_path),        # render to file
        "-r", str(sample_rate),
        "-g", str(gain),
        "-O", "s16",                # 16-bit signed PCM
        str(sf),
        str(midi_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"fluidsynth failed: {proc.stderr.strip()}")
    return wav_path

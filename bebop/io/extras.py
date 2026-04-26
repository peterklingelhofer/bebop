"""Adapters that shell out to the `.venv-extras` Python 3.11 venv for ML models.

The extras venv hosts:
    - basic-pitch (Spotify, audio -> MIDI transcription)
    - autochord (BTC bidirectional transformer chord recognizer; depends on
      the locally-built NNLS-Chroma vamp plugin in ~/Library/Audio/Plug-Ins/Vamp)

Both need older TensorFlow (<2.16, Keras 2) than our main 3.12 venv supports.
We invoke them as subprocesses, capture MIDI/JSON output, and parse it back
into a ChordSequence — keeping the main venv lean while still benefiting from
state-of-the-art ML.

If the extras venv isn't installed, these functions raise a clear install hint.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from bebop.io.midi_in import parse_midi
from bebop.types import ChordSequence

_ROOT = Path(__file__).resolve().parents[2]
_EXTRAS_PY = _ROOT / ".venv-extras" / "bin" / "python"
_INSTALL_HINT = (
    "extras venv not found at .venv-extras. Set it up with:\n"
    "  uv venv --python cpython-3.11.13-macos-aarch64-none .venv-extras\n"
    "  VIRTUAL_ENV=.venv-extras uv pip install 'numpy<2' 'cython<3' setuptools wheel\n"
    "  VIRTUAL_ENV=.venv-extras uv pip install --no-build-isolation vamp\n"
    "  VIRTUAL_ENV=.venv-extras uv pip install basic-pitch 'tensorflow<2.16' \\\n"
    "      'tf-keras<2.16' 'setuptools<81' --no-build-isolation"
)


def _ensure_extras() -> Path:
    if not _EXTRAS_PY.is_file():
        raise RuntimeError(_INSTALL_HINT)
    return _EXTRAS_PY


def transcribe_basic_pitch(audio_path: str | Path, *, output_midi: str | Path | None = None) -> Path:
    """Run Spotify's basic-pitch on `audio_path`, write MIDI, return its path.

    If `output_midi` is None, writes into a temp dir.
    """
    py = _ensure_extras()
    audio_path = Path(audio_path).resolve()
    if output_midi is None:
        out_dir = Path(tempfile.mkdtemp(prefix="bebop_bp_"))
    else:
        output_midi = Path(output_midi).resolve()
        out_dir = output_midi.parent
        out_dir.mkdir(parents=True, exist_ok=True)

    script = (
        "from basic_pitch.inference import predict_and_save\n"
        "from basic_pitch import ICASSP_2022_MODEL_PATH\n"
        f"predict_and_save([{audio_path.as_posix()!r}], {out_dir.as_posix()!r},\n"
        "                 save_midi=True, sonify_midi=False,\n"
        "                 save_model_outputs=False, save_notes=False,\n"
        "                 model_or_model_path=ICASSP_2022_MODEL_PATH)\n"
    )
    proc = subprocess.run([str(py), "-c", script], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"basic-pitch failed:\n{proc.stderr}")

    # basic-pitch writes <stem>_basic_pitch.mid into out_dir
    written = out_dir / f"{audio_path.stem}_basic_pitch.mid"
    if not written.exists():
        raise RuntimeError(f"basic-pitch did not produce expected file at {written}")

    if output_midi is not None and written != Path(output_midi):
        shutil.move(str(written), str(output_midi))
        return Path(output_midi)
    return written


def parse_basic_pitch(audio_path: str | Path, *, bpm: float, **kwargs) -> ChordSequence:
    """audio -> basic-pitch (deep model) -> MIDI -> our chord recognizer.

    `kwargs` pass through to `parse_midi` (e.g. min_velocity, windows_per_bar).
    """
    midi_path = transcribe_basic_pitch(audio_path)
    return parse_midi(midi_path, bpm=bpm, **kwargs)


# ─────────────────────────── autochord ─────────────────────────────

# Mapping from autochord's Harte-style labels to (root_pc, suffix).
# Autochord emits "C:maj", "Eb:min", "Bb:7", etc., and "N" for no-chord.
_PC = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4,
       "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8,
       "A": 9, "A#": 10, "Bb": 10, "B": 11}
_SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_QUALITY_TO_SUFFIX = {
    "maj": "", "min": "m", "dim": "dim", "aug": "aug",
    "maj7": "maj7", "min7": "m7", "7": "7",
    "minmaj7": "m(maj7)", "hdim7": "m7b5", "dim7": "dim7",
    "maj6": "6", "min6": "m6", "sus4": "sus4", "sus2": "sus2",
}


def _harte_to_chord_symbol(label: str) -> tuple[str, str | None] | None:
    """Convert 'Eb:min' / 'C:maj' / 'C#/E' / 'N' into (symbol, optional_bass)."""
    if label in ("N", "X", ""):
        return None
    bass: str | None = None
    if "/" in label:
        label, bass_str = label.split("/", 1)
        bass = _SHARP_NAMES[_PC.get(bass_str, 0)]
    if ":" in label:
        root_str, qual = label.split(":", 1)
    else:
        root_str, qual = label, "maj"
    if root_str not in _PC:
        return None
    root_pc = _PC[root_str]
    suffix = _QUALITY_TO_SUFFIX.get(qual, "")
    return f"{_SHARP_NAMES[root_pc]}{suffix}", bass


def parse_chordino(
    audio_path: str | Path,
    *,
    bpm: float,
    beats_per_bar: int = 4,
) -> ChordSequence:
    """audio -> Chordino vamp plugin (via chord-extractor) -> ChordSequence.

    Chordino is the original NNLS-Chroma chord recognizer (Mauch & Dixon 2010).
    It outputs simpler chord labels than autochord (no slash chords) but is
    fast and well-validated. Useful as a third "audio" opinion in the ensemble.
    """
    py = _ensure_extras()
    audio_path = Path(audio_path).resolve()
    out_json = Path(tempfile.mktemp(suffix=".json", prefix="bebop_chordino_"))
    script = (
        "from chord_extractor.extractors import Chordino\n"
        "import json\n"
        "ch = Chordino(roll_on=1.0)\n"
        f"chords = ch.extract({audio_path.as_posix()!r})\n"
        f"with open({out_json.as_posix()!r}, 'w') as f:\n"
        "    json.dump([(c.timestamp, c.chord) for c in chords], f)\n"
    )
    proc = subprocess.run([str(py), "-c", script], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"chordino failed:\n{proc.stderr}")

    raw = json.loads(out_json.read_text())
    out_json.unlink(missing_ok=True)

    # chordino emits onset-only labels — derive end times by looking at the next chord
    seconds_per_beat = 60.0 / bpm
    snap = 1.0
    from bebop.types import Chord
    chords: list[Chord] = []
    for i, (t_sec, label) in enumerate(raw):
        if label in ("N", "X", ""):
            continue
        t_end = raw[i + 1][0] if i + 1 < len(raw) else t_sec + 4 * seconds_per_beat
        # chordino labels are like "F", "C", "Ebm", "Cmaj7", "Aaug", "F6"
        ident = _chordino_label_to_symbol(label)
        if ident is None:
            continue
        symbol, bass = ident
        start_beat = round((t_sec / seconds_per_beat) / snap) * snap
        end_beat = round((t_end / seconds_per_beat) / snap) * snap
        dur = end_beat - start_beat
        if dur < 1.0:
            continue
        chords.append(Chord(symbol=symbol, start_beat=start_beat,
                            duration_beats=dur, bass=bass, confidence=0.80))
    return ChordSequence(chords=chords, bpm=bpm, time_signature=(beats_per_bar, 4))


def _chordino_label_to_symbol(label: str) -> tuple[str, str | None] | None:
    """Parse chordino labels: 'F', 'Ebm', 'Cmaj7', 'F6', 'Aaug', 'C/E', etc."""
    if not label:
        return None
    bass: str | None = None
    if "/" in label:
        label, bass_str = label.split("/", 1)
        if bass_str in _PC:
            bass = _SHARP_NAMES[_PC[bass_str]]
    # split root from suffix
    if len(label) >= 2 and label[1] in ("#", "b"):
        root_str, suffix = label[:2], label[2:]
    else:
        root_str, suffix = label[:1], label[1:]
    if root_str not in _PC:
        return None
    # chordino's "aug" maps to our "aug", "maj7" to "maj7", "m" to "m", "6" to "6"
    return f"{_SHARP_NAMES[_PC[root_str]]}{suffix}", bass


def parse_autochord(
    audio_path: str | Path,
    *,
    bpm: float,
    beats_per_bar: int = 4,
) -> ChordSequence:
    """audio -> autochord (BTC neural net + NNLS-Chroma) -> ChordSequence.

    autochord emits time-segmented chord labels in Harte syntax. We convert each
    segment to our Chord type and snap timestamps to the beat grid.
    """
    py = _ensure_extras()
    audio_path = Path(audio_path).resolve()

    # autochord prints status to stdout, so we ask it to write JSON to a tempfile
    out_json = Path(tempfile.mktemp(suffix=".json", prefix="bebop_autochord_"))
    script = (
        "import autochord, json\n"
        f"chords = autochord.recognize({audio_path.as_posix()!r})\n"
        f"with open({out_json.as_posix()!r}, 'w') as f:\n"
        "    json.dump([(s, e, l) for s, e, l in chords], f)\n"
    )
    proc = subprocess.run([str(py), "-c", script], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"autochord failed:\n{proc.stderr}")
    if not out_json.exists():
        raise RuntimeError(f"autochord did not produce {out_json}")

    raw = json.loads(out_json.read_text())
    out_json.unlink(missing_ok=True)

    # snap autochord's continuous-time segments to a 1-beat grid: it doesn't
    # do beat tracking, so its boundaries fall on arbitrary timestamps and
    # our ensemble + display work better with clean integer beats.
    seconds_per_beat = 60.0 / bpm
    snap_beats = 1.0
    from bebop.types import Chord
    chords: list[Chord] = []
    for start_sec, end_sec, label in raw:
        ident = _harte_to_chord_symbol(label)
        if ident is None:
            continue
        symbol, bass = ident
        start_beat = round((start_sec / seconds_per_beat) / snap_beats) * snap_beats
        end_beat = round((end_sec / seconds_per_beat) / snap_beats) * snap_beats
        dur_beats = end_beat - start_beat
        if dur_beats < 1.0:
            continue
        chords.append(Chord(symbol=symbol, start_beat=start_beat,
                            duration_beats=dur_beats, bass=bass, confidence=0.85))
    return ChordSequence(chords=chords, bpm=bpm, time_signature=(beats_per_bar, 4))

"""On-disk cache for slow chord-recognition sources.

Sources keyed by (audio_file_content_hash, source_name, bpm). basic-pitch and
autochord take 15-30s on a 3-minute song; from cache they're <1ms. JSON-serialized
ChordSequence under `.bebop_cache/<hash_prefix>/<source>__bpm<N>.json`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bebop.types import Chord, ChordSequence, KeyChange

_CACHE_DIR = Path(".bebop_cache")


def _hash_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _cache_path(audio_path: Path, source: str, bpm: float) -> Path:
    digest = _hash_file(audio_path)
    return _CACHE_DIR / digest[:16] / f"{source}__bpm{int(bpm)}.json"


def load(audio_path: Path, source: str, bpm: float) -> ChordSequence | None:
    cp = _cache_path(audio_path, source, bpm)
    if not cp.is_file():
        return None
    try:
        data = json.loads(cp.read_text())
        return _from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def store(audio_path: Path, source: str, bpm: float, seq: ChordSequence) -> None:
    cp = _cache_path(audio_path, source, bpm)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(_to_dict(seq), indent=None, separators=(",", ":")))


def _to_dict(seq: ChordSequence) -> dict:
    # coerce numpy scalars to Python floats — recognizers sometimes return float32
    return {
        "bpm": float(seq.bpm),
        "time_signature": list(seq.time_signature),
        "key_map": [{"start_beat": float(kc.start_beat), "key": kc.key} for kc in seq.key_map],
        "chords": [
            {"symbol": c.symbol, "start_beat": float(c.start_beat),
             "duration_beats": float(c.duration_beats),
             "bass": c.bass, "confidence": float(c.confidence)}
            for c in seq.chords
        ],
    }


def _from_dict(d: dict) -> ChordSequence:
    return ChordSequence(
        bpm=d["bpm"],
        time_signature=tuple(d["time_signature"]),
        key_map=[KeyChange(start_beat=k["start_beat"], key=k["key"]) for k in d.get("key_map", [])],
        chords=[
            Chord(symbol=c["symbol"], start_beat=c["start_beat"],
                  duration_beats=c["duration_beats"],
                  bass=c.get("bass"), confidence=c.get("confidence", 1.0))
            for c in d["chords"]
        ],
    )

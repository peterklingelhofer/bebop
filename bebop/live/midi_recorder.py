"""Append completed notes to a `pretty_midi.PrettyMIDI` and periodically flush to disk.

Two instrument tracks (mirroring offline write_midi):
    "Live Comp Piano" (program 0)
    "Live Comp Bass"  (program 32)

`flush()` rewrites the entire .mid file; pretty_midi has no incremental
write API, but a session of even thousands of notes serializes in ms.

We accept finished notes (start + end already known) rather than scheduling
them ourselves — the live MIDI scheduler is the source of truth for timing,
and the recorder is just a tee of what was actually heard.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import pretty_midi


@dataclass(frozen=True, slots=True)
class MidiHit:
    """A completed note ready to be appended to the disk recording.

    `start_seconds` and `end_seconds` are session-relative (t=0 is the
    recorder's t0, which is whatever the caller chooses — typically the
    first chord detection or session start).
    `track` is "piano" or "bass".
    """
    pitch: int
    velocity: int
    start_seconds: float
    end_seconds: float
    track: str


class MidiRecorder:
    """Append-only MIDI recorder with periodic flushes to disk."""

    def __init__(
        self,
        output_path: str | Path,
        *,
        bpm_getter,                    # callable() -> float (read live knob)
        flush_interval_seconds: float = 5.0,
    ) -> None:
        self.output_path = Path(output_path)
        self.bpm_getter = bpm_getter
        self.flush_interval = flush_interval_seconds

        self._pm = pretty_midi.PrettyMIDI(initial_tempo=float(bpm_getter()))
        self._piano = pretty_midi.Instrument(program=0, name="Live Comp Piano")
        self._bass = pretty_midi.Instrument(program=32, name="Live Comp Bass")
        self._pm.instruments.append(self._bass)
        self._pm.instruments.append(self._piano)

        self._dirty = False
        self._last_flush = 0.0
        self._lock = asyncio.Lock()
        self._flusher_task: asyncio.Task | None = None
        self._running = False
        self._n_hits = 0

    @property
    def total_hits(self) -> int:
        return self._n_hits

    async def append(self, hits: list[MidiHit]) -> None:
        if not hits:
            return
        async with self._lock:
            for h in hits:
                note = pretty_midi.Note(
                    velocity=max(1, min(127, h.velocity)),
                    pitch=int(h.pitch),
                    start=max(0.0, h.start_seconds),
                    end=max(h.start_seconds + 0.05, h.end_seconds),
                )
                if h.track == "bass":
                    self._bass.notes.append(note)
                else:
                    self._piano.notes.append(note)
            self._n_hits += len(hits)
            self._dirty = True

    async def flush(self) -> Path:
        async with self._lock:
            if not self._dirty:
                return self.output_path
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self._pm.write(str(self.output_path))
            self._dirty = False
            self._last_flush = time.monotonic()
            return self.output_path

    async def _flusher(self) -> None:
        while self._running:
            await asyncio.sleep(self.flush_interval)
            try:
                await self.flush()
            except Exception as e:
                print(f"[midi_recorder] flush error: {e}")

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._flusher_task = asyncio.create_task(self._flusher(), name="midi_flusher")

    async def stop(self) -> None:
        self._running = False
        if self._flusher_task is not None:
            self._flusher_task.cancel()
            try:
                await self._flusher_task
            except asyncio.CancelledError:
                pass
            self._flusher_task = None
        await self.flush()

"""Async MIDI scheduler — timed dispatch of note_on/note_off with per-chord
cancellation.

A single async loop sleeps until the next event's deadline, sends it through
`MidiOut`, and notifies a callback when each note completes (so the disk
recorder can append the finished note with its true duration).

Cancellation by `group_id` is how the comp engine truncates a chord on chord
change — un-fired notes are dropped, currently-sounding notes get an
immediate note_off.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from bebop.live.midi_out import MidiOut


@dataclass
class ScheduledNote:
    group_id: int
    channel: int
    pitch: int
    velocity: int
    on_time: float          # absolute time.monotonic()
    off_time: float
    track: str = "piano"    # "piano" | "bass" — used by the recorder
    on_sent: bool = False
    off_sent: bool = False


# called once per note, after note_off has fired (whether scheduled or forced)
NoteCompleteCallback = Callable[[ScheduledNote], Awaitable[None]]


class MidiScheduler:
    """Schedules and dispatches MIDI events with millisecond-ish precision."""

    def __init__(
        self,
        midi_out: MidiOut,
        on_note_complete: NoteCompleteCallback | None = None,
    ) -> None:
        self.midi_out = midi_out
        self.on_note_complete = on_note_complete
        self._notes: list[ScheduledNote] = []
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="midi_scheduler")

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def schedule_many(self, notes: list[ScheduledNote]) -> None:
        if not notes:
            return
        async with self._lock:
            self._notes.extend(notes)
        self._wake.set()

    async def cancel_group(self, group_id: int) -> None:
        """Stop everything from a chord:
            - drop notes that haven't started yet
            - send immediate note_off for currently-sounding notes
        """
        completed: list[ScheduledNote] = []
        async with self._lock:
            keep: list[ScheduledNote] = []
            for n in self._notes:
                if n.group_id != group_id:
                    keep.append(n)
                    continue
                if n.on_sent and not n.off_sent:
                    self.midi_out.send_note_off(n.channel, n.pitch)
                    n.off_sent = True
                    n.off_time = time.monotonic()
                    completed.append(n)
                # else: drop (never played, or already complete)
            self._notes = keep
        if self.on_note_complete is not None:
            for n in completed:
                try:
                    await self.on_note_complete(n)
                except Exception as e:
                    print(f"[scheduler] note-complete callback error: {e}")
        self._wake.set()

    async def panic(self) -> None:
        """Drop all pending notes, force note_offs for anything sounding."""
        completed: list[ScheduledNote] = []
        async with self._lock:
            for n in self._notes:
                if n.on_sent and not n.off_sent:
                    n.off_sent = True
                    n.off_time = time.monotonic()
                    completed.append(n)
            self._notes = []
        self.midi_out.panic()
        if self.on_note_complete is not None:
            for n in completed:
                try:
                    await self.on_note_complete(n)
                except Exception as e:
                    print(f"[scheduler] note-complete callback error: {e}")

    def _next_deadline_locked(self) -> float:
        """Caller must hold self._lock. Returns next firing time, or +inf."""
        next_t = float("inf")
        for n in self._notes:
            if not n.on_sent and n.on_time < next_t:
                next_t = n.on_time
            if n.on_sent and not n.off_sent and n.off_time < next_t:
                next_t = n.off_time
        return next_t

    async def _run(self) -> None:
        while self._running:
            async with self._lock:
                deadline = self._next_deadline_locked()
            wait = deadline - time.monotonic()
            if wait == float("inf"):
                self._wake.clear()
                try:
                    await self._wake.wait()
                except asyncio.CancelledError:
                    raise
                continue
            if wait > 0:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                    # got woken (new schedule / cancellation) — recompute
                    continue
                except asyncio.TimeoutError:
                    pass
            await self._fire_due()

    async def _fire_due(self) -> None:
        completed: list[ScheduledNote] = []
        now = time.monotonic()
        async with self._lock:
            for n in self._notes:
                if not n.on_sent and n.on_time <= now:
                    self.midi_out.send_note_on(n.channel, n.pitch, n.velocity)
                    n.on_sent = True
                if n.on_sent and not n.off_sent and n.off_time <= now:
                    self.midi_out.send_note_off(n.channel, n.pitch)
                    n.off_sent = True
                    completed.append(n)
            self._notes = [n for n in self._notes if not (n.on_sent and n.off_sent)]
        if self.on_note_complete is not None:
            for n in completed:
                try:
                    await self.on_note_complete(n)
                except Exception as e:
                    print(f"[scheduler] note-complete callback error: {e}")

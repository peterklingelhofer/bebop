"""Live comping: each detected chord becomes a forward-scheduled batch of MIDI events.

When a new chord stabilizes:
    1. Cancel the previous chord's group — truncates any sustained bass pitch
       and drops un-played rhythm hits
    2. Voice the new chord (threading prev_voicing for voice-leading)
    3. Render `lookahead_bars` of the rhythm template + a long-held bassline,
       all timestamped relative to wall-clock NOW (the recognizer's
       commit time), and schedule them via MidiScheduler

The scheduler dispatches at deadline, sending live MIDI to IAC immediately
and notifying the disk recorder via its on_note_complete callback once each
note finishes — so the .mid file matches what was actually played.

Reharm is applied to a one-chord ChordSequence. Substitutions that need
context (ii-V insertion looks ahead) just no-op; extension/dominant/modal
subs still fire — so high spice still adds color, just not full reharm.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from itertools import count

from bebop.live.chord_stream import ChordEvent
from bebop.live.midi_out import CHANNEL_BASS, CHANNEL_PIANO
from bebop.live.scheduler import MidiScheduler, ScheduledNote
from bebop.reharm import reharmonize
from bebop.rhythm import render_rhythm, resolve_rhythm
from bebop.types import Chord, ChordSequence
from bebop.voicing import voice_chord, VoicedChord


_GROUP_COUNTER = count(1)


@dataclass
class LiveKnobs:
    """Mutable bag of knob values, written by the server, read by the engine.
    `enabled=False` mutes scheduling — the comp falls silent until re-enabled.
    """
    spice: float = 0.4
    voicing: str = "rootless"      # rootless | evans | drop2 | quartal
    rhythm: str = "charleston"     # any name from rhythm.all_rhythm_names()
    bpm: float = 120.0
    seed: int = 0
    enabled: bool = True
    piano_bass: bool = False        # if True, piano LH carries the bassline

    def snapshot(self) -> "LiveKnobs":
        return LiveKnobs(
            spice=self.spice, voicing=self.voicing, rhythm=self.rhythm,
            bpm=self.bpm, seed=self.seed, enabled=self.enabled,
            piano_bass=self.piano_bass,
        )


@dataclass
class _Active:
    group_id: int
    event: ChordEvent
    knobs: LiveKnobs
    voicing: VoicedChord
    activation_time: float


class CompEngine:
    """Forward-scheduling live comp engine.

    Owns the "currently sounding chord" state. Each new ChordEvent cancels
    the prior chord's group and schedules `lookahead_bars` worth of comp for
    the new one. If a chord lasts longer than `lookahead_bars * 4` beats,
    the comp goes quiet until the next chord change — fine for jazz where
    chords rarely sit that long, and protects the scheduler from unbounded
    growth.
    """

    def __init__(
        self,
        knobs: LiveKnobs,
        scheduler: MidiScheduler,
        *,
        lookahead_bars: int = 16,
    ) -> None:
        self.knobs = knobs
        self.scheduler = scheduler
        self.lookahead_bars = lookahead_bars
        self._active: _Active | None = None
        self._prev_voicing: VoicedChord | None = None

    @property
    def active_event(self) -> ChordEvent | None:
        return self._active.event if self._active is not None else None

    async def on_chord(self, event: ChordEvent) -> None:
        # cancel anything still pending from the previous chord
        if self._active is not None:
            await self.scheduler.cancel_group(self._active.group_id)
            self._active = None

        knobs = self.knobs.snapshot()
        if not knobs.enabled:
            return

        bpm = max(20.0, knobs.bpm)
        spb = 60.0 / bpm
        chord_duration_beats = self.lookahead_bars * 4.0

        # one-chord sequence so reharmonize() can apply (most) substitutions;
        # context-dependent ones no-op which is fine — we re-react on the
        # next real chord change anyway
        seq = ChordSequence(
            chords=[Chord(symbol=event.symbol, start_beat=0.0,
                          duration_beats=chord_duration_beats,
                          confidence=event.confidence)],
            bpm=bpm, time_signature=(4, 4),
        )
        try:
            reharmed = reharmonize(seq, spice=knobs.spice, seed=knobs.seed)
            chord_for_voicing = reharmed.chords[0] if reharmed.chords else seq.chords[0]
        except Exception as e:
            print(f"[comp_engine] reharm failed for {event.symbol}: {e}")
            chord_for_voicing = seq.chords[0]

        try:
            v = voice_chord(chord_for_voicing, previous=self._prev_voicing,
                            style=knobs.voicing)
        except Exception as e:
            print(f"[comp_engine] voicing failed for {event.symbol}: {e}")
            return

        try:
            template_name, shift_beats = resolve_rhythm(knobs.rhythm)
        except ValueError as e:
            print(f"[comp_engine] {e}")
            template_name, shift_beats = "charleston", 0.0

        # use detected_at (real wall-clock when recognizer committed) — we
        # can't schedule notes in the past
        now = max(time.monotonic(), event.detected_at)
        group_id = next(_GROUP_COUNTER)
        notes: list[ScheduledNote] = []

        # bassline: one sustained pitch covering the whole lookahead. Will
        # be truncated by cancel_group when the next chord arrives.
        bass_channel = CHANNEL_PIANO if knobs.piano_bass else CHANNEL_BASS
        bass_track = "piano" if knobs.piano_bass else "bass"
        bass_start = now + shift_beats * spb
        bass_end = bass_start + chord_duration_beats * spb
        if bass_start >= now:
            notes.append(ScheduledNote(
                group_id=group_id, channel=bass_channel,
                pitch=v.bass_pitch, velocity=80,
                on_time=bass_start, off_time=bass_end,
                track=bass_track,
            ))

        # rhythm: render full pattern across lookahead_bars
        if v.chord_pitches:
            for hit in render_rhythm(template_name, chord_duration_beats):
                hit_start = now + (hit.offset_beats + shift_beats) * spb
                hit_end = hit_start + hit.duration_beats * spb
                if hit_start < now:
                    continue
                for p in v.chord_pitches:
                    notes.append(ScheduledNote(
                        group_id=group_id, channel=CHANNEL_PIANO,
                        pitch=p, velocity=hit.velocity,
                        on_time=hit_start, off_time=hit_end,
                        track="piano",
                    ))

        await self.scheduler.schedule_many(notes)

        self._active = _Active(group_id=group_id, event=event, knobs=knobs,
                               voicing=v, activation_time=now)
        self._prev_voicing = v

    async def flush(self) -> None:
        """Stop the currently-active chord (called on shutdown)."""
        if self._active is not None:
            await self.scheduler.cancel_group(self._active.group_id)
            self._active = None
        await self.scheduler.panic()

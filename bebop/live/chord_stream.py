"""Real-time chord recognition over a rolling audio window.

Runs CQT chroma + template matching on the most recent ~2 s of audio,
emits a stable chord symbol once the same chord has been the winner for
N consecutive analysis frames. Below a silence threshold, emits no events.

The scoring logic is intentionally a thin adapter around the templates and
prior weights from `bebop.io.audio_in` — same musical "ear" online and
offline, so the suggestions a player hears live match what the offline
matrix would produce.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import librosa
import numpy as np

from bebop.io.audio_in import _TEMPLATE_PRIOR
from bebop.io.midi_in import (
    _PITCH_NAMES_FLAT,
    _PITCH_NAMES_SHARP,
    _TEMPLATES,
    _diatonic_pcs,
    detect_key,
)
from bebop.live.audio_capture import AudioCaptureRing


@dataclass(frozen=True, slots=True)
class ChordEvent:
    symbol: str           # e.g. "Cmaj7"
    confidence: float     # winning score (cosine similarity-ish, post-prior)
    started_at: float     # wall-clock time.monotonic() when the chord first stabilized
    detected_at: float    # wall-clock time when this event was emitted (== started_at on first emit)
    key: str              # detected key at emit time, e.g. "D" or "Am"


def _identify(chroma_vec: np.ndarray, bass_vec: np.ndarray | None,
              key: str | None, prefer_flats: bool) -> tuple[str, float] | None:
    """Same scoring shape as bebop.io.audio_in._identify_audio_chord, inlined to
    avoid coupling to its windowed parse function."""
    if float(chroma_vec.sum()) < 1e-6:
        return None

    key_root_pc: int | None = None
    key_is_major = True
    if key:
        is_minor = key.endswith("m")
        tonic_str = key[:-1] if is_minor else key
        try:
            from music21 import pitch as m21pitch
            key_root_pc = m21pitch.Pitch(tonic_str).pitchClass
            key_is_major = not is_minor
        except Exception:
            pass

    a = chroma_vec / (np.linalg.norm(chroma_vec) + 1e-9)
    bass_normed = None
    if bass_vec is not None and bass_vec.sum() > 1e-9:
        bass_normed = bass_vec / bass_vec.sum()

    best: tuple[float, int, str] | None = None
    for root in range(12):
        for suffix, template in _TEMPLATES:
            # online side: skip 9/11/13 — chroma can't disambiguate
            if any(c.isdigit() and c not in "67" for c in suffix):
                continue
            template_pcs = np.zeros(12, dtype=np.float64)
            for iv in template:
                template_pcs[(root + iv) % 12] = 1.0
            template_pcs /= template_pcs.sum()
            b = template_pcs / (np.linalg.norm(template_pcs) + 1e-9)
            score = float(np.dot(a, b))
            if bass_normed is not None:
                score += 0.18 * bass_normed[root]
            score += _TEMPLATE_PRIOR.get(suffix, 0.0)
            if key_root_pc is not None:
                diatonic = _diatonic_pcs(key_root_pc, key_is_major)
                if root in diatonic:
                    score += 0.06
                if root == key_root_pc:
                    score += 0.04
                if root == (key_root_pc + 7) % 12:
                    score += 0.03
            if best is None or score > best[0]:
                best = (score, root, suffix)
    assert best is not None
    score, root_pc, suffix = best
    name_table = _PITCH_NAMES_FLAT if prefer_flats else _PITCH_NAMES_SHARP
    return f"{name_table[root_pc]}{suffix}", score


def _chroma(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """CQT-based full-range and bass-range chroma, averaged over the window."""
    if len(y) < sr // 4:
        return np.zeros(12), np.zeros(12)
    fmin = librosa.note_to_hz("C2")
    bins_per_octave = 36
    n_octaves = 6
    cqt = np.abs(librosa.cqt(y, sr=sr, hop_length=512, fmin=fmin,
                             n_bins=bins_per_octave * n_octaves,
                             bins_per_octave=bins_per_octave))
    chroma = librosa.feature.chroma_cqt(C=cqt, bins_per_octave=bins_per_octave,
                                        hop_length=512)
    bass_bins = bins_per_octave * 3 // 2
    bass_chroma = librosa.feature.chroma_cqt(C=cqt[:bass_bins],
                                             bins_per_octave=bins_per_octave,
                                             hop_length=512)
    # average over time
    return chroma.mean(axis=1), bass_chroma.mean(axis=1)


def analyze_ring(
    ring: AudioCaptureRing,
    last_key: str,
    *,
    analysis_window_seconds: float,
    silence_rms: float,
    prefer_flats: bool = False,
) -> tuple[tuple[str, float] | None, str]:
    """Pull the latest window from `ring`, compute chroma, identify chord.

    Returns ((symbol, score), key) on success or (None, last_key) if silent /
    insufficient data. Pure function of ring state — used by both the live
    async loop and the headless eval harness, so behavior is identical.
    """
    rms = ring.rms(0.5)
    if rms < silence_rms:
        return None, last_key
    y = ring.latest(analysis_window_seconds)
    try:
        y_h = librosa.effects.harmonic(y, margin=2.0)
    except Exception:
        y_h = y
    chroma_vec, bass_vec = _chroma(y_h, ring.samplerate)
    if chroma_vec.sum() < 1e-6:
        return None, last_key
    key, _ = detect_key(chroma_vec)
    ident = _identify(chroma_vec, bass_vec, key, prefer_flats)
    return ident, key


class ChordStream:
    """Async chord recognizer. Polls the ring buffer at `analysis_period`,
    emits a `ChordEvent` to `on_chord` only when a new chord stabilizes."""

    def __init__(
        self,
        ring: AudioCaptureRing,
        on_chord,                      # async callable(ChordEvent)
        *,
        analysis_window_seconds: float = 1.5,
        analysis_period: float = 0.4,   # how often we re-analyze
        stability_frames: int = 2,      # consecutive analyses needed to commit
        silence_rms: float = 0.005,     # below this, force "silence"
        prefer_flats: bool = False,
        latency_compensation_seconds: float | None = None,
    ) -> None:
        self.ring = ring
        self.on_chord = on_chord
        self.analysis_window_seconds = analysis_window_seconds
        self.analysis_period = analysis_period
        self.stability_frames = stability_frames
        self.silence_rms = silence_rms
        self.prefer_flats = prefer_flats
        # how far back in time the chord actually started, vs when the
        # recognizer commits to it. Lower bound is window + (N-1)*period
        # — chroma needs the window to fill, then N consecutive analyses
        # must agree. Used to back-date `started_at` so the rendered MIDI
        # lines up with the audio in your DAW.
        if latency_compensation_seconds is None:
            latency_compensation_seconds = (
                analysis_window_seconds + max(0, stability_frames - 1) * analysis_period
            )
        self.latency_compensation_seconds = latency_compensation_seconds

        self._task: asyncio.Task | None = None
        self._running = False
        self._last_committed: str | None = None
        self._candidate: str | None = None
        self._candidate_count: int = 0
        self._last_event: ChordEvent | None = None
        self._key: str = "C"

    @property
    def last_event(self) -> ChordEvent | None:
        return self._last_event

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while self._running:
            t0 = time.monotonic()
            try:
                # Run heavy analysis in a thread so the event loop stays
                # responsive (handlers, websocket sends, etc.)
                ident, key = await loop.run_in_executor(None, self._analyze_once)
            except Exception as e:
                # Don't let a transient analysis error kill the loop
                print(f"[chord_stream] analysis error: {e}")
                ident, key = None, self._key

            self._key = key
            await self._update_state(ident)

            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, self.analysis_period - elapsed)
            await asyncio.sleep(sleep_for)

    def _analyze_once(self) -> tuple[tuple[str, float] | None, str]:
        return analyze_ring(
            self.ring, self._key,
            analysis_window_seconds=self.analysis_window_seconds,
            silence_rms=self.silence_rms,
            prefer_flats=self.prefer_flats,
        )

    def step(self, ident: tuple[str, float] | None, key: str,
             *, audio_time: float) -> ChordEvent | None:
        """Synchronous version of `_update_state`: feeds an analysis result
        into the candidate-stability state machine and returns a `ChordEvent`
        if a new chord just committed (else None).

        Used by the eval harness to drive the recognizer at audio-time rather
        than wall-clock — so tests are deterministic and time-correct.
        Production runs use the async loop, which calls `_update_state`.
        """
        self._key = key
        if ident is None:
            self._candidate = None
            self._candidate_count = 0
            return None
        sym, score = ident
        if sym == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = sym
            self._candidate_count = 1
        if (
            self._candidate_count >= self.stability_frames
            and self._candidate != self._last_committed
        ):
            ev = ChordEvent(
                symbol=self._candidate,
                confidence=score,
                started_at=audio_time - self.latency_compensation_seconds,
                detected_at=audio_time,
                key=self._key,
            )
            self._last_committed = self._candidate
            self._last_event = ev
            return ev
        return None

    async def _update_state(self, ident: tuple[str, float] | None) -> None:
        if ident is None:
            self._candidate = None
            self._candidate_count = 0
            return
        sym, score = ident
        if sym == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = sym
            self._candidate_count = 1
        if (
            self._candidate_count >= self.stability_frames
            and self._candidate != self._last_committed
        ):
            now = time.monotonic()
            # back-date started_at to roughly when the chord actually began
            # in the audio — see `latency_compensation_seconds` docstring above
            ev = ChordEvent(
                symbol=self._candidate,
                confidence=score,
                started_at=now - self.latency_compensation_seconds,
                detected_at=now,
                key=self._key,
            )
            self._last_committed = self._candidate
            self._last_event = ev
            try:
                await self.on_chord(ev)
            except Exception as e:
                print(f"[chord_stream] on_chord handler error: {e}")

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="chord_stream")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

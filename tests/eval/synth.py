"""Shared utilities for the eval harness.

All benches share three primitives:
    fixtures()       -> dict[name, ChordSequence] of ground-truth progressions
    synthesize(seq)  -> Path to a rendered .wav of `seq`
    chord_pcs(sym)   -> set[int] of pitch classes for a chord symbol

Plus a helper that feeds a pre-rendered WAV through a `ChordStream` driven by
*audio-time*, not wall-clock — so tests are deterministic and the timestamps
on captured events match the audio, not the test runner's pacing.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pretty_midi
import soundfile as sf

from bebop.io.midi_in import _PITCH_NAMES_SHARP, _TEMPLATES
from bebop.live.audio_capture import AudioCaptureRing
from bebop.live.chord_stream import ChordEvent, ChordStream, analyze_ring
from bebop.render import render_midi_to_wav
from bebop.types import Chord, ChordSequence, KeyChange


EVAL_DIR = Path("output/eval")
EVAL_DIR.mkdir(parents=True, exist_ok=True)


# ─── ground-truth chord-symbol → pitch-class set ─────────────────────────────

_TEMPLATE_BY_SUFFIX = {suffix: tpl for suffix, tpl in _TEMPLATES}


@functools.cache
def chord_pcs(symbol: str) -> frozenset[int]:
    """Return the pitch-class set for a chord symbol like 'Cmaj7' or 'F#m'.

    Uses bebop's own chord-template table to stay consistent with what the
    recognizer is matching against.
    """
    # crude root parser: 1-2 chars for note, rest is suffix
    if len(symbol) >= 2 and symbol[1] in "#b":
        root_str, suffix = symbol[:2], symbol[2:]
    else:
        root_str, suffix = symbol[:1], symbol[1:]

    # normalize flats -> sharps
    flat_to_sharp = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
    root_str = flat_to_sharp.get(root_str, root_str)

    if root_str not in _PITCH_NAMES_SHARP:
        raise ValueError(f"can't parse root from chord symbol: {symbol!r}")
    root_pc = _PITCH_NAMES_SHARP.index(root_str)

    # match suffix to a template; fall back to bare triad
    tpl = _TEMPLATE_BY_SUFFIX.get(suffix)
    if tpl is None:
        # try common aliases the recognizer doesn't include
        if suffix in ("M7", "Maj7", "maj"):
            tpl = _TEMPLATE_BY_SUFFIX["maj7"] if suffix.endswith("7") else _TEMPLATE_BY_SUFFIX[""]
        elif suffix.startswith("7") or suffix == "9":
            tpl = _TEMPLATE_BY_SUFFIX["7"]
        elif suffix.startswith("m") and "7" in suffix:
            tpl = _TEMPLATE_BY_SUFFIX["m7"]
        elif suffix.startswith("m"):
            tpl = _TEMPLATE_BY_SUFFIX["m"]
        else:
            tpl = _TEMPLATE_BY_SUFFIX[""]

    return frozenset((root_pc + iv) % 12 for iv in tpl)


def chord_root_pc(symbol: str) -> int:
    """Just the root pitch class — useful for bass-note checks."""
    if len(symbol) >= 2 and symbol[1] in "#b":
        root_str = symbol[:2]
    else:
        root_str = symbol[:1]
    flat_to_sharp = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
    root_str = flat_to_sharp.get(root_str, root_str)
    return _PITCH_NAMES_SHARP.index(root_str)


# ─── fixtures: small ground-truth progressions ───────────────────────────────


def _build(symbols_per_bar: list[str], *, key: str, bpm: float = 100.0) -> ChordSequence:
    """Quick chord-sequence builder: one chord per bar, 4 beats each, given key."""
    chords = [
        Chord(symbol=s, start_beat=4 * i, duration_beats=4.0)
        for i, s in enumerate(symbols_per_bar)
    ]
    return ChordSequence(
        chords=chords, bpm=bpm, time_signature=(4, 4),
        key_map=[KeyChange(start_beat=0.0, key=key)],
    )


@functools.cache
def fixtures() -> dict[str, ChordSequence]:
    """Canonical eval fixtures. Small + diverse so iteration is fast."""
    return {
        # textbook ii-V-I in C, four bars
        "ii_V_I_C":      _build(["Dm7", "G7", "Cmaj7", "Cmaj7"], key="C"),
        # rhythm changes A section in Bb (8 bars)
        "rhythm_Bb":     _build(
            ["Bb", "G7", "Cm7", "F7", "Bb", "Eb", "Bb", "F7"], key="Bb"),
        # blues in F (12 bars)
        "blues_F":       _build(
            ["F7", "Bb7", "F7", "F7",
             "Bb7", "Bb7", "F7", "D7",
             "Gm7", "C7", "F7", "C7"], key="F"),
        # minor ii-V-i in D minor (4 bars)
        "ii_V_i_Dm":     _build(["Em7b5", "A7", "Dm", "Dm"], key="Dm"),
        # modal: dorian vamp in D (4 bars, two-chord)
        "dorian_vamp_D": _build(["Dm7", "G7", "Dm7", "G7"], key="Dm"),
    }


# ─── synthesize a ChordSequence to audio via fluidsynth ──────────────────────


def synthesize(seq: ChordSequence, name: str, *, voicing_density: int = 4) -> Path:
    """Render `seq` as block-chord audio so the recognizer has something to
    chew on. We use voiced triads/sevenths in the middle register + a sustained
    root in the bass — clean, no rhythm, just held chords. This is the "easy
    mode" reference signal; if the recognizer can't handle this, it can't
    handle real audio either.
    """
    wav_out = EVAL_DIR / f"{name}.wav"
    if wav_out.exists():
        return wav_out

    pm = pretty_midi.PrettyMIDI(initial_tempo=seq.bpm)
    piano = pretty_midi.Instrument(program=0, name="piano")
    bass = pretty_midi.Instrument(program=32, name="bass")
    pm.instruments.append(bass)
    pm.instruments.append(piano)

    spb = 60.0 / seq.bpm
    for chord in seq.chords:
        pcs = sorted(chord_pcs(chord.symbol))
        # bass root one octave below middle C-ish
        bass_pitch = 36 + chord_root_pc(chord.symbol)
        # piano: stacked triad/seventh near middle C, take first `voicing_density` pcs
        piano_pitches = [60 + ((pc - 60) % 12) for pc in pcs[:voicing_density]]
        start = chord.start_beat * spb
        end = (chord.start_beat + chord.duration_beats) * spb
        bass.notes.append(pretty_midi.Note(velocity=85, pitch=bass_pitch,
                                           start=start, end=end))
        for p in piano_pitches:
            piano.notes.append(pretty_midi.Note(velocity=72, pitch=p,
                                                start=start, end=end))

    midi_path = EVAL_DIR / f"{name}.mid"
    pm.write(str(midi_path))
    render_midi_to_wav(midi_path, wav_out, sample_rate=22050, gain=0.5)
    return wav_out


# ─── feed a WAV through the live ChordStream at simulated wall clock ─────────


@dataclass
class CapturedRun:
    events: list[ChordEvent]
    duration_seconds: float


def feed_wav_to_chord_stream(
    wav_path: Path,
    *,
    analysis_window: float = 1.0,
    analysis_period: float = 0.3,
    stability_frames: int = 2,         # production default
    silence_rms: float = 0.001,        # synth audio is very clean
) -> CapturedRun:
    """Synchronously stream a WAV through ChordStream's analysis logic at
    audio-time, capturing every committed event.

    The async loop in production uses wall-clock; tests would have to either
    run at real-time or fight time-scaling bugs. Here we drive it directly:
    push `analysis_period` seconds of audio, run one analysis, advance our
    own audio_time variable. The captured event timestamps are in *audio
    seconds*, which is what the bench wants to compare against ground truth.

    Behavior matches the production async loop exactly — both go through
    `analyze_ring` + `ChordStream.step`.
    """
    audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    target_sr = 22050
    if sr != target_sr:
        new_n = int(len(audio) * target_sr / sr)
        audio = np.interp(np.linspace(0, len(audio), new_n, endpoint=False),
                          np.arange(len(audio)), audio).astype(np.float32)
        sr = target_sr

    duration = len(audio) / sr
    ring = AudioCaptureRing(capacity_seconds=max(15.0, duration + 5.0),
                            samplerate=sr)
    # ChordStream is constructed only for its state-machine (step()); we
    # never start the async loop
    stream = ChordStream(
        ring, on_chord=lambda _ev: None,
        analysis_window_seconds=analysis_window,
        analysis_period=analysis_period,
        stability_frames=stability_frames,
        silence_rms=silence_rms,
    )

    captured: list[ChordEvent] = []
    audio_time = 0.0
    chunk_size = max(1, int(round(analysis_period * sr)))
    pos = 0
    key = "C"

    while pos < len(audio):
        end = min(pos + chunk_size, len(audio))
        chunk = audio[pos:end]
        # AudioCaptureRing._callback expects (frames, channels) shape
        ring._callback(chunk.reshape(-1, 1), len(chunk), None, None)
        pos = end
        audio_time = pos / sr

        ident, key = analyze_ring(
            ring, key,
            analysis_window_seconds=analysis_window,
            silence_rms=silence_rms,
        )
        ev = stream.step(ident, key, audio_time=audio_time)
        if ev is not None:
            captured.append(ev)

    return CapturedRun(events=captured, duration_seconds=duration)


# ─── small helper for nice CLI tables ────────────────────────────────────────


def fmt_pct(x: float) -> str:
    return f"{100 * x:5.1f}%"


def fmt_n(x: float, w: int = 6, p: int = 2) -> str:
    return f"{x:>{w}.{p}f}"

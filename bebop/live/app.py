"""Top-level orchestrator: audio capture → chord stream → forward-scheduling
comp engine → MidiScheduler (live IAC + disk recorder), plus the dashboard.

`run_live()` is the single coroutine the CLI calls. It owns the lifecycle of
every stage and tears them down cleanly on Ctrl-C / SIGTERM.
"""

from __future__ import annotations

import asyncio
import signal
import time
import webbrowser
from pathlib import Path

from bebop.live.audio_capture import AudioCaptureRing, list_input_devices
from bebop.live.chord_stream import ChordEvent, ChordStream
from bebop.live.comp_engine import CompEngine, LiveKnobs
from bebop.live.midi_out import MidiOut, list_outputs
from bebop.live.midi_recorder import MidiHit, MidiRecorder
from bebop.live.scheduler import MidiScheduler, ScheduledNote
from bebop.live.server import DashboardServer
from bebop.rhythm import all_rhythm_names


async def run_live(
    *,
    output_path: Path,
    device: int | str | None = None,
    midi_port: str | None = None,
    no_midi: bool = False,
    bpm: float = 120.0,
    spice: float = 0.4,
    voicing: str = "rootless",
    rhythm: str = "charleston",
    piano_bass: bool = False,
    host: str = "127.0.0.1",
    port: int = 8765,
    flush_seconds: float = 5.0,
    analysis_window: float = 1.0,
    analysis_period: float = 0.3,
    stability_frames: int = 2,
    silence_rms: float = 0.005,
    open_browser: bool = True,
    lookahead_bars: int = 16,
) -> int:
    """Run the live comping session until Ctrl-C / SIGTERM."""
    knobs = LiveKnobs(
        spice=spice, voicing=voicing, rhythm=rhythm, bpm=bpm,
        piano_bass=piano_bass, enabled=True,
    )

    # ── audio input ──
    ring = AudioCaptureRing(device=device)
    try:
        dev_info = ring.start()
    except Exception as e:
        print(f"\n[live] failed to open audio input device: {e}")
        print("\navailable input devices:")
        for d in list_input_devices():
            print(f"  [{d.index}] {d.name}  ({d.channels} ch, {d.samplerate:.0f} Hz)")
        print("\ntip: brew install blackhole-2ch  →  set Logic output to BlackHole 2ch")
        return 2

    print(f"\n[live] audio in:  {dev_info.name}  "
          f"({dev_info.channels} ch, {dev_info.samplerate:.0f} Hz)")

    # ── MIDI output (live IAC) ──
    midi_out = MidiOut(port_name=midi_port)
    midi_port_label = "(disk only)"
    if not no_midi:
        try:
            midi_port_label = midi_out.open()
            print(f"[live] MIDI out:  {midi_port_label}")
        except Exception as e:
            print(f"[live] MIDI out:  disabled ({e})")
            print("[live] available MIDI ports:")
            for name in list_outputs():
                print(f"           {name}")
            print("[live] continuing in disk-only mode")
    else:
        print("[live] MIDI out:  disabled (--no-midi)")

    # ── disk recorder ──
    recorder = MidiRecorder(output_path, bpm_getter=lambda: knobs.bpm,
                            flush_interval_seconds=flush_seconds)
    recorder.start()

    # ── scheduler + disk-recording callback ──
    # latency_compensation: subtracted from the disk-MIDI timestamps so the
    # rendered .mid file lines up roughly with the audio when dragged into a
    # DAW (live MIDI to IAC stays at real wall-clock — those notes already
    # played).
    recognition_lag = analysis_window + max(0, stability_frames - 1) * analysis_period
    scheduler_t0 = time.monotonic()

    async def on_note_complete(n: ScheduledNote) -> None:
        await recorder.append([MidiHit(
            pitch=n.pitch,
            velocity=n.velocity,
            start_seconds=max(0.0, n.on_time - scheduler_t0 - recognition_lag),
            end_seconds=max(0.05, n.off_time - scheduler_t0 - recognition_lag),
            track=n.track,
        )])

    scheduler = MidiScheduler(midi_out, on_note_complete=on_note_complete)
    scheduler.start()

    # ── comp engine ──
    engine = CompEngine(knobs, scheduler, lookahead_bars=lookahead_bars)

    # ── dashboard server ──
    server = DashboardServer(
        knobs, host=host, port=port,
        device_label=dev_info.name,
        output_path=str(output_path),
        rhythms=all_rhythm_names(),
        silence_rms=silence_rms,
        midi_port=midi_port_label,
    )

    async def on_panic() -> None:
        print("[live] panic — flushing all sounding notes")
        await engine.flush()

    server.on_panic(on_panic)

    async def on_chord(event: ChordEvent) -> None:
        await engine.on_chord(event)
        elapsed = event.detected_at - scheduler_t0
        await server.broadcast({
            "type": "chord",
            "symbol": event.symbol,
            "confidence": round(event.confidence, 3),
            "key": event.key,
            "t": round(max(0.0, elapsed), 1),
        })
        await server.broadcast({"type": "stats", "total_hits": recorder.total_hits})

    chord_stream = ChordStream(
        ring,
        on_chord=on_chord,
        analysis_window_seconds=analysis_window,
        analysis_period=analysis_period,
        stability_frames=stability_frames,
        silence_rms=silence_rms,
    )
    chord_stream.start()

    url = await server.start()
    print(f"[live] dashboard: {url}")
    print(f"[live] disk MIDI: {output_path}  (flushed every {flush_seconds:.0f}s)")
    print("[live] press Ctrl-C to stop\n")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"[live] couldn't auto-open browser: {e}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop(*_):
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())

    LEVEL_HZ = 5.0
    SILENCE_AFTER_S = 4.0
    STATS_EVERY_N_TICKS = int(LEVEL_HZ * 2)

    async def ticker_loop():
        n = 0
        while not stop_event.is_set():
            await asyncio.sleep(1.0 / LEVEL_HZ)
            await server.broadcast({"type": "level", "rms": ring.rms(0.2)})
            ev = chord_stream.last_event
            if ev is None or (time.monotonic() - ev.detected_at) > SILENCE_AFTER_S:
                await server.broadcast({"type": "silence"})
            n += 1
            if n % STATS_EVERY_N_TICKS == 0:
                await server.broadcast({"type": "stats", "total_hits": recorder.total_hits})

    ticker = asyncio.create_task(ticker_loop(), name="live_ticker")

    try:
        await stop_event.wait()
    finally:
        print("\n[live] shutting down ...")
        ticker.cancel()
        await chord_stream.stop()
        await engine.flush()
        await scheduler.stop()
        midi_out.close()
        ring.stop()
        await recorder.stop()
        await server.stop()
        print(f"[live] wrote {recorder.total_hits} notes to {output_path}")

    return 0

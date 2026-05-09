"""Live comping: listen to a virtual audio cable, recognize chords, write MIDI in real time.

Pipeline (each module owns one stage):
    audio_capture    sounddevice input stream → rolling float32 ring buffer
    chord_stream     ring buffer → CQT chroma → chord symbols (debounced)
    comp_engine      chord events + live knobs → reharm → voicing → rhythm hits
    midi_recorder    rhythm hits → pretty_midi notes, periodically flushed to disk
    server           aiohttp WebSocket dashboard for live knob tweaks

Entry point: `bebop live` (see bebop.live.app for the orchestrator that wires
all five stages together and exposes a single `run()` coroutine)
"""

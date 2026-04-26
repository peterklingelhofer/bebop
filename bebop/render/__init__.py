"""Output stage: write MIDI, render to audio, mix with original, and HTML report."""

from bebop.render.audio_out import find_soundfont, render_midi_to_wav
from bebop.render.midi_out import write_midi
from bebop.render.mix import mix_audio
from bebop.render.report import MatrixCell, write_html_report

__all__ = ["write_midi", "render_midi_to_wav", "find_soundfont", "mix_audio",
           "write_html_report", "MatrixCell"]

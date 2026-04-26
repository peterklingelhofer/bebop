"""Command-line interface for bebop.

Examples:
    # chart input only, mild reharm, MIDI out
    bebop --chart charts/butterfly_boy.txt --spice 0.3 --out output/comp.mid

    # ensemble: chart + Melodyne MIDI + audio chromagram
    bebop --chart charts/butterfly_boy.txt \\
          --midi 'audio/butterfly boy acoustic guitar MIDI.midi' \\
          --audio 'audio/butterfly boy 20260330 no acoustic guitar 94bpm.wav' \\
          --spice 0.5 --bpm 94 --print --out output/comp.mid

    # render to audio + mix with original
    bebop --chart charts/butterfly_boy.txt --spice 0.5 --bpm 94 \\
          --out output/comp.mid --render-audio --mix-with audio/song.wav

You may pass any combination of --chart, --midi, --audio. With 2+ sources
the ensemble voter picks the most-supported chord per beat.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bebop.io import cache
from bebop.io.alignment import compute_first_onset_offset_beats, shift_sequence
from bebop.io.audio_in import parse_audio
from bebop.io.chart import parse_chart
from bebop.io.disagreement import print_disagreement_report, write_suggested_chart
from bebop.io.ensemble import vote
from bebop.io.extras import parse_autochord, parse_basic_pitch, parse_chordino
from bebop.io.midi_in import parse_midi
from bebop.reharm import reharmonize
from bebop.render import MatrixCell, mix_audio, render_midi_to_wav, write_html_report, write_midi
from bebop.types import ChordSequence


def _cached(audio_path: Path, source: str, bpm: float, fn) -> ChordSequence:
    """Try cache, run `fn()` on miss, store the result."""
    seq = cache.load(audio_path, source, bpm)
    if seq is not None:
        print(f"  [cached] {source}")
        return seq
    print(f"  [running] {source} ...")
    seq = fn()
    cache.store(audio_path, source, bpm, seq)
    return seq


def _print_progression(label: str, seq: ChordSequence) -> None:
    print(f"\n=== {label} ({len(seq.chords)} chords, {seq.total_beats} beats, bpm={seq.bpm:.1f}) ===")
    if seq.key_map:
        print("  key map:", " | ".join(f"{kc.key}@beat{kc.start_beat:.0f}" for kc in seq.key_map))
    for c in seq.chords:
        bar = int(c.start_beat // seq.beats_per_bar) + 1
        beat_in_bar = (c.start_beat % seq.beats_per_bar) + 1
        sym = c.symbol + (f"/{c.bass}" if c.bass else "")
        conf = f"  [{c.confidence:.2f}]" if c.confidence < 0.99 else ""
        print(f"  bar {bar:>3} beat {beat_in_bar:>3.1f}  {sym:<14}  ({c.duration_beats}b){conf}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bebop")
    parser.add_argument("--chart", type=Path, help="Real Book-style chord chart")
    parser.add_argument("--midi", type=Path, help="Polyphonic MIDI (e.g. Melodyne export)")
    parser.add_argument("--audio", type=Path, help="WAV file for librosa CQT chord recognition")
    parser.add_argument("--basic-pitch", type=Path, dest="basic_pitch",
                        help="WAV file for Spotify basic-pitch (deep-model audio->MIDI->chords). "
                             "Requires .venv-extras venv (Python 3.11 + TF<2.16).")
    parser.add_argument("--autochord", type=Path, dest="autochord_path",
                        help="WAV file for autochord (BTC bidirectional transformer + NNLS-Chroma). "
                             "Requires .venv-extras venv and ~/Library/Audio/Plug-Ins/Vamp/nnls-chroma.dylib.")
    parser.add_argument("--chordino", type=Path, dest="chordino_path",
                        help="WAV file for Chordino (NNLS-Chroma chord recognizer, Mauch & Dixon 2010). "
                             "Requires same setup as --autochord.")

    parser.add_argument("--spice", type=float, default=0.4,
                        help="Reharmonization adventurousness, 0.0 (mild) to 1.0 (wild)")
    parser.add_argument("--spice-sweep", default=None,
                        help="Comma-separated list of spice values, e.g. '0,0.3,0.6,0.9'. "
                             "Each emits its own MIDI/WAV/mix. Multiplies with --voicings.")
    parser.add_argument("--seed", type=int, default=0,
                        help="Random seed for reproducible substitutions")
    parser.add_argument("--suggest-chart", type=Path, default=None,
                        help="Write the ensemble consensus to this chord-chart .txt for auditing/editing.")
    parser.add_argument("--html-report", type=Path, default=None,
                        help="Write a single-page HTML audit with all audio variants embedded, "
                             "the disagreement table, and the chart diff.")
    parser.add_argument("--bpm", type=float, default=None,
                        help="Override BPM (required for --audio if no --chart provided)")
    parser.add_argument("--midi-offset", type=float, default=None,
                        help="Manual MIDI offset in beats. Positive shifts MIDI later (use when "
                             "MIDI was trimmed earlier than the audio). Overrides --first-onset-align.")
    parser.add_argument("--first-onset-align", action="store_true",
                        help="Auto-align MIDI to audio by detecting first onset in each. Off by "
                             "default — only useful when MIDI and audio share the same musical t=0 "
                             "(e.g. MIDI was transcribed directly from the WAV).")
    parser.add_argument("--rhythm", default="charleston",
                        choices=["charleston", "two_and_four", "sustained", "anticipations"])
    parser.add_argument("--voicings", default="rootless,evans,drop2,quartal",
                        help="Comma-separated voicing styles. Each emits its own MIDI/WAV/mix file. "
                             "Choices: rootless, evans, drop2, quartal. Default: all four.")
    parser.add_argument("--no-bass", action="store_true", help="Skip the bass track")
    parser.add_argument("--print", action="store_true", dest="print_prog",
                        help="Print the chord progression(s) to stdout")

    parser.add_argument("--out", type=Path, required=True, help="Output MIDI path")
    parser.add_argument("--render-audio", action="store_true",
                        help="Also render the comp MIDI to a .wav next to --out")
    parser.add_argument("--soundfont", type=Path, default=None,
                        help="Path to a .sf2 SoundFont (or set $BEBOP_SOUNDFONT)")
    parser.add_argument("--mix-with", type=Path, default=None,
                        help="Mix the rendered comp with this WAV; writes <out>.mix.wav")
    parser.add_argument("--comp-gain-db", type=float, default=-2.0)
    parser.add_argument("--song-gain-db", type=float, default=-1.0)

    args = parser.parse_args(argv)

    if not (args.chart or args.midi or args.audio or args.basic_pitch
            or args.autochord_path or args.chordino_path):
        parser.error("at least one of --chart, --midi, --audio, --basic-pitch, "
                     "--autochord, --chordino is required")

    sources: list[ChordSequence] = []
    sources_named: dict[str, ChordSequence] = {}
    if args.chart:
        s = parse_chart(args.chart)
        if args.bpm is not None:
            s.bpm = args.bpm
        if args.print_prog:
            _print_progression("CHART", s)
        sources.append(s)
        sources_named["CHART"] = s

    if args.midi:
        s = parse_midi(args.midi, bpm=args.bpm)
        if args.print_prog:
            _print_progression("MIDI", s)
        sources.append(s)
        sources_named["MIDI"] = s

    if args.audio:
        bpm = args.bpm or (sources[0].bpm if sources else None)
        if bpm is None:
            parser.error("--audio requires --bpm (or a --chart that declares bpm:)")
        s = _cached(args.audio, "audio_cqt", bpm, lambda: parse_audio(args.audio, bpm=bpm))
        if args.print_prog:
            _print_progression("AUDIO (librosa CQT)", s)
        sources.append(s)
        sources_named["AUDIO"] = s

    if args.basic_pitch:
        bpm = args.bpm or (sources[0].bpm if sources else None)
        if bpm is None:
            parser.error("--basic-pitch requires --bpm (or a --chart that declares bpm:)")
        s = _cached(args.basic_pitch, "basic_pitch", bpm,
                    lambda: parse_basic_pitch(args.basic_pitch, bpm=bpm))
        if args.print_prog:
            _print_progression("BASIC-PITCH (deep model)", s)
        sources.append(s)
        sources_named["BASIC"] = s

    if args.autochord_path:
        bpm = args.bpm or (sources[0].bpm if sources else None)
        if bpm is None:
            parser.error("--autochord requires --bpm (or a --chart that declares bpm:)")
        s = _cached(args.autochord_path, "autochord", bpm,
                    lambda: parse_autochord(args.autochord_path, bpm=bpm))
        if args.print_prog:
            _print_progression("AUTOCHORD (BTC + NNLS-Chroma)", s)
        sources.append(s)
        sources_named["AUTO"] = s

    if args.chordino_path:
        bpm = args.bpm or (sources[0].bpm if sources else None)
        if bpm is None:
            parser.error("--chordino requires --bpm (or a --chart that declares bpm:)")
        s = _cached(args.chordino_path, "chordino", bpm,
                    lambda: parse_chordino(args.chordino_path, bpm=bpm))
        if args.print_prog:
            _print_progression("CHORDINO (NNLS-Chroma)", s)
        sources.append(s)
        sources_named["CHORD"] = s

    # ── MIDI <-> audio temporal alignment ──
    # Both parse_midi and parse_audio assume time 0 in their file maps to beat 0 of the
    # song. If those don't actually line up musically, the MIDI's vote in the ensemble is
    # systematically offset and gets diluted. Two ways to control:
    #   - manual --midi-offset N (in beats) — overrides auto
    #   - automatic first-onset alignment (default ON when both MIDI + an audio source exist)
    if args.midi and "MIDI" in sources_named:
        # pick whichever audio source path we have, in order of trustworthiness
        audio_for_align = (args.audio or args.basic_pitch
                           or args.autochord_path or args.chordino_path)
        applied_offset_beats: float | None = None
        if args.midi_offset is not None:
            applied_offset_beats = args.midi_offset
            print(f"\nMIDI alignment: manual --midi-offset {args.midi_offset:+.2f} beats")
        elif audio_for_align is not None and args.first_onset_align:
            bpm_for_align = args.bpm or sources_named["MIDI"].bpm
            result = compute_first_onset_offset_beats(args.midi, audio_for_align,
                                                      bpm=bpm_for_align)
            if result is not None:
                offset, midi_t, audio_t = result
                if abs(offset) >= 0.05:
                    applied_offset_beats = offset
                    print(f"\nMIDI alignment: --first-onset-align shifted {offset:+.2f} beats "
                          f"(MIDI first note at {midi_t:.2f}s, audio first onset at {audio_t:.2f}s).")
                else:
                    print(f"\nMIDI alignment: --first-onset-align detected {offset:+.2f} beats "
                          f"(within tolerance, no shift applied).")
            else:
                print("\nMIDI alignment: skipped (no onset detected in MIDI or audio).")

        if applied_offset_beats is not None and applied_offset_beats != 0:
            sources_named["MIDI"] = shift_sequence(sources_named["MIDI"], applied_offset_beats)
            # rebuild the positional list so the ensemble vote sees the shifted MIDI
            sources = list(sources_named.values())

    # combine
    if len(sources) == 1:
        seq = sources[0]
    else:
        seq = vote(*sources)
        if args.print_prog:
            _print_progression("ENSEMBLE", seq)
        print_disagreement_report(sources_named)

    if args.suggest_chart:
        path = write_suggested_chart(seq, args.suggest_chart,
                                     title=f"butterfly_boy auto-chart from ensemble of {len(sources)} sources")
        print(f"\nwrote chord-chart suggestion: {path}")

    if args.bpm is not None:
        seq.bpm = args.bpm

    voicings = [v.strip() for v in args.voicings.split(",") if v.strip()]
    if not voicings:
        parser.error("--voicings must list at least one style")

    if args.spice_sweep:
        try:
            spice_values = [float(s.strip()) for s in args.spice_sweep.split(",") if s.strip()]
        except ValueError:
            parser.error(f"--spice-sweep expects comma-separated floats, got {args.spice_sweep!r}")
    else:
        spice_values = [args.spice]

    base_path = args.out
    multi = len(spice_values) > 1 or len(voicings) > 1

    print()
    print(f"=== rendering matrix: {len(spice_values)} spice × {len(voicings)} voicing"
          f" = {len(spice_values) * len(voicings)} variants ===")

    matrix_cells: list[MatrixCell] = []
    for spice in spice_values:
        reharmed = reharmonize(seq, spice=spice, seed=args.seed)
        if args.print_prog and len(spice_values) == 1:
            _print_progression(f"REHARMONIZED (spice={spice})", reharmed)

        for voicing in voicings:
            if multi:
                stem = base_path.stem
                spice_str = f"spice{int(round(spice * 100)):02d}"
                midi_path = base_path.with_name(f"{stem}.{spice_str}.{voicing}{base_path.suffix}")
            else:
                midi_path = base_path

            midi_out = write_midi(reharmed, midi_path, rhythm=args.rhythm,
                                  include_bass=not args.no_bass, voicing=voicing)
            tag = f"spice={spice:.2f} {voicing:<8}"
            print(f"[{tag}] MIDI: {midi_out}")

            wav_out: Path | None = None
            mix_out: Path | None = None
            if args.render_audio or args.mix_with:
                wav_out = midi_out.with_suffix(".wav")
                render_midi_to_wav(midi_out, wav_out, soundfont_path=args.soundfont)
                print(f"[{tag}] WAV:  {wav_out}")

                # only pre-render the mix.wav when there's no HTML report —
                # the HTML report mixes in-browser at user-controlled volumes
                if args.mix_with and not args.html_report:
                    mix_out = midi_out.with_suffix(".mix.wav")
                    mix_audio(wav_out, args.mix_with, mix_out,
                              comp_gain_db=args.comp_gain_db, original_gain_db=args.song_gain_db)
                    print(f"[{tag}] MIX:  {mix_out}")

            matrix_cells.append(MatrixCell(spice=spice, voicing=voicing,
                                           midi_path=midi_out, wav_path=wav_out, mix_path=mix_out))

    if args.html_report:
        hand_text = args.chart.read_text() if args.chart else None
        suggested_text = args.suggest_chart.read_text() if args.suggest_chart \
            and args.suggest_chart.exists() else None
        report_path = write_html_report(
            args.html_report,
            title=f"bebop — {base_path.stem}",
            bpm=seq.bpm,
            cells=matrix_cells,
            sources=sources_named,
            hand_chart_text=hand_text,
            suggested_chart_text=suggested_text,
            original_audio_path=args.mix_with,
        )
        print(f"\nwrote HTML report: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

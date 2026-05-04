"""Combine two (or more) bebop matrix renders into a single HTML report.

Each matrix run produces a set of `output/<prefix>.spice*.<...>.{mid,wav}` files.
This script scans for multiple prefixes, tags every cell with a midi_source
label so you can A/B them in one HTML, and re-computes the chord-chart
explainer data per midi-source by re-running the ensemble vote + reharm.

Example:
    python tools/combine_matrices.py \\
        --prefix output/matrix --label acoustic_guitar \\
            --midi 'audio/...acoustic guitar...mid' \\
        --prefix output/noacoustic_matrix --label no_acoustic_melodyne \\
            --midi 'audio/...no acoustic melodyne...midi' --first-onset-align \\
        --chart charts/butterfly_boy.txt \\
        --audio 'audio/butterfly boy no acoustic guitar 20260502.wav' \\
        --basic-pitch 'audio/butterfly boy no acoustic guitar 20260502.wav' \\
        --autochord 'audio/butterfly boy no acoustic guitar 20260502.wav' \\
        --chordino 'audio/butterfly boy no acoustic guitar 20260502.wav' \\
        --bpm 94 \\
        --html-report output/butterfly_boy_combined.html
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bebop.io import cache
from bebop.io.alignment import compute_first_onset_offset_beats, shift_sequence
from bebop.io.audio_in import parse_audio
from bebop.io.chart import parse_chart
from bebop.io.ensemble import vote
from bebop.io.extras import parse_autochord, parse_basic_pitch, parse_chordino
from bebop.io.midi_in import parse_midi
from bebop.reharm import reharmonize
from bebop.render import MatrixCell, write_html_report
from bebop.render.explainer import chart_comparison, voicing_breakdown


_PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
def _midi_name(m: int) -> str:
    return f"{_PITCH_NAMES[m % 12]}{m // 12 - 1}"


def _scan_matrix_prefix(prefix: Path, label: str) -> list[MatrixCell]:
    """Find <prefix>.spice*.* files and build MatrixCell entries with midi_source=label."""
    cells: list[MatrixCell] = []
    pattern = f"{prefix.name}.spice*.mid"
    for mid_path in sorted(prefix.parent.glob(pattern)):
        # filename: <prefix>.spice<NN>.<voicing>.<rhythm>[.<bass>][.<alignment>][.<pb>].mid
        rel = mid_path.stem[len(prefix.name) + 1:]   # drop "<prefix>." part
        parts = rel.split(".")
        spice = int(parts[0].replace("spice", "")) / 100
        voicing = parts[1]
        rhythm = parts[2] if len(parts) > 2 else "charleston"
        # remaining parts (bass, alignment, pb) are present only when those
        # dimensions had >1 value. Defaults match the most common single-mode runs.
        bass = "sustained"
        alignment = "raw"
        pb = "off"
        for token in parts[3:]:
            if token in ("sustained", "walking"):
                bass = token
            elif token == "nobass":
                bass = "none"
            elif token in ("raw", "aligned"):
                alignment = token
            elif token in ("upright", "pianobass"):
                pb = "on" if token == "pianobass" else "off"
        wav_path = mid_path.with_suffix(".wav")
        cells.append(MatrixCell(
            spice=spice, voicing=voicing, rhythm=rhythm, bass=bass,
            alignment=alignment, piano_bass=pb, midi_source=label,
            midi_path=mid_path, wav_path=wav_path if wav_path.exists() else None,
            mix_path=None,
        ))
    return cells


def _ensemble_seq_for_midi(chart_path: Path, midi_path: Path, audio_path: Path,
                           bpm: float, also_audio: bool, also_basic: bool,
                           also_autochord: bool, also_chordino: bool,
                           apply_first_onset_align: bool) -> tuple:
    """Build the ensemble-voted ChordSequence for one MIDI source.

    Returns (seq, source_dict) — source_dict useful for downstream metadata.
    """
    sources: dict = {"CHART": parse_chart(chart_path),
                     "MIDI":  parse_midi(midi_path, bpm=bpm)}
    if also_audio:
        sources["AUDIO"] = (cache.load(audio_path, "audio_cqt", bpm)
                            or parse_audio(audio_path, bpm=bpm))
    if also_basic:
        sources["BASIC"] = (cache.load(audio_path, "basic_pitch", bpm)
                            or parse_basic_pitch(audio_path, bpm=bpm))
    if also_autochord:
        sources["AUTO"] = (cache.load(audio_path, "autochord", bpm)
                           or parse_autochord(audio_path, bpm=bpm))
    if also_chordino:
        sources["CHORD"] = (cache.load(audio_path, "chordino", bpm)
                            or parse_chordino(audio_path, bpm=bpm))
    if apply_first_onset_align and audio_path is not None:
        result = compute_first_onset_offset_beats(midi_path, audio_path, bpm=bpm)
        if result is not None and abs(result[0]) >= 0.05:
            sources["MIDI"] = shift_sequence(sources["MIDI"], result[0])
            print(f"  [{midi_path.name}] first-onset align: shifted {result[0]:+.2f} beats")
    return vote(*sources.values()), sources


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    # --prefix can be repeated; --label and --midi go with it positionally
    p.add_argument("--prefix", action="append", required=True,
                   help="Glob prefix for one matrix run, e.g. output/matrix or output/noacoustic_matrix. "
                        "Repeat for each run.")
    p.add_argument("--label", action="append", required=True,
                   help="midi-source label for each --prefix (in order). e.g. acoustic_guitar.")
    p.add_argument("--midi", action="append", required=True,
                   help="MIDI source file for each --prefix (in order). Used to recompute the "
                        "ensemble vote + per-variant chord-chart explainer for that source.")
    p.add_argument("--first-onset-align", action="append", default=None,
                   help="Per-prefix flag: 'true' to apply first-onset alignment for that MIDI source. "
                        "Repeat once per prefix in order.")

    p.add_argument("--chart", type=Path, required=True)
    p.add_argument("--audio", type=Path, required=True,
                   help="The shared audio file used as the audio source for all midi sources.")
    p.add_argument("--basic-pitch", type=Path, default=None)
    p.add_argument("--autochord", type=Path, default=None)
    p.add_argument("--chordino", type=Path, default=None)
    p.add_argument("--bpm", type=float, required=True)
    p.add_argument("--html-report", type=Path, required=True)
    p.add_argument("--duck-hz", type=int, default=200)

    args = p.parse_args()

    if len(args.prefix) != len(args.label) or len(args.prefix) != len(args.midi):
        p.error("--prefix, --label, --midi must be repeated the same number of times")
    align_flags = args.first_onset_align or [None] * len(args.prefix)
    if len(align_flags) != len(args.prefix):
        p.error("--first-onset-align must be repeated once per --prefix (or omitted entirely)")

    # gather cells from each prefix
    all_cells: list[MatrixCell] = []
    explainer_progressions: dict[str, list[dict]] = {}
    explainer_voicings: dict[str, list[dict]] = {}
    sources_for_disagreement: dict | None = None

    for prefix_str, label, midi_path_str, align_str in zip(args.prefix, args.label, args.midi, align_flags):
        prefix = Path(prefix_str)
        midi_path = Path(midi_path_str)
        apply_align = (align_str or "").lower() in ("true", "1", "yes", "on")
        cells = _scan_matrix_prefix(prefix, label)
        print(f"  '{label}' from '{prefix}': {len(cells)} cells")
        all_cells.extend(cells)

        # recompute ensemble seq for THIS midi source so we can build the
        # explainer per-source
        seq, sources = _ensemble_seq_for_midi(
            args.chart, midi_path, args.audio, args.bpm,
            also_audio=True, also_basic=args.basic_pitch is not None,
            also_autochord=args.autochord is not None,
            also_chordino=args.chordino is not None,
            apply_first_onset_align=apply_align,
        )
        if sources_for_disagreement is None:
            sources_for_disagreement = sources

        original_seq = sources["CHART"]
        # find unique (spice, alignment) combos for this label
        for c in cells:
            prog_key = f"{label}_{c.spice:.2f}_{c.alignment}"
            if prog_key not in explainer_progressions:
                # alignment="raw" means we use the (possibly shifted) seq as-is
                # alignment="aligned" means we'd shift again — but combiner mostly handles
                # raw-only matrices. If you want alignment dim too, run the combiner separately.
                reharmed = reharmonize(seq, spice=c.spice, seed=0)
                rows = chart_comparison(original_seq, reharmed)
                explainer_progressions[prog_key] = [
                    {"bar": r.bar, "beat": r.beat, "duration_beats": r.duration_beats,
                     "original_symbol": r.original_symbol, "new_symbol": r.new_symbol,
                     "new_bass": r.new_bass, "theory_note": r.theory_note}
                    for r in rows
                ]
                for v_style in {x.voicing for x in cells}:
                    vkey = f"{label}_{c.spice:.2f}_{c.alignment}_{v_style}"
                    if vkey in explainer_voicings:
                        continue
                    vbs = voicing_breakdown(reharmed, v_style)
                    explainer_voicings[vkey] = [
                        {"chord_symbol": v.chord_symbol,
                         "bass_pitch": v.bass_pitch, "bass_name": _midi_name(v.bass_pitch),
                         "bass_interval": v.bass_interval,
                         "intervals": list(v.intervals), "pitches": list(v.chord_pitches),
                         "pitch_names": [_midi_name(p) for p in v.chord_pitches],
                         "summary": v.summary}
                        for v in vbs
                    ]

    print(f"\ncombined: {len(all_cells)} total cells across "
          f"{len({c.midi_source for c in all_cells})} midi sources")

    write_html_report(
        args.html_report,
        title=f"bebop combined — {args.html_report.stem}",
        bpm=args.bpm, cells=all_cells,
        sources=sources_for_disagreement or {},
        hand_chart_text=args.chart.read_text() if args.chart else None,
        suggested_chart_text=None,
        original_audio_path=args.audio,
        duck_hpf_hz=args.duck_hz,
        explainer_data={"progressions": explainer_progressions, "voicings": explainer_voicings},
    )
    print(f"wrote {args.html_report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

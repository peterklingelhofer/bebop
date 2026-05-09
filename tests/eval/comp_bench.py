"""Comp bench: feed ground-truth chord sequences into the comp pipeline and
score the rendered MIDI against checkable musical properties.

We deliberately bypass the recognizer here — the question is "given a clean
chord sequence, does the comp engine produce sensible notes?" Recognition
quality is the recognition_bench's job.

For each fixture × voicing × rhythm cell we measure:

    chord_membership  — fraction of comp piano notes whose pitch class is in
                        the chord's pitch-class set. >0.95 is the bar.
    bass_root         — fraction of bass notes whose pitch class equals the
                        chord root. ~1.00 is expected.
    voice_lead_avg    — average semitones moved by chord pitches between
                        successive chords. Lower = smoother voice leading.
                        <4 semitones average is healthy for jazz piano.
    density_per_bar   — average comp piano notes (NOT counting the same chord
                        played as multiple pitches) per bar. Used as a sanity
                        check that the rhythm template fired the right number
                        of hits.

These metrics are mechanical — they don't tell you "does it sound good," but
they catch the obvious bugs: out-of-key notes, wrong-root bass, voicing
collapse, dropped rhythm hits.
"""

from __future__ import annotations

from dataclasses import dataclass

import pretty_midi

from bebop.reharm import reharmonize
from bebop.render import write_midi
from bebop.types import ChordSequence

from tests.eval.synth import EVAL_DIR, chord_pcs, chord_root_pc, fixtures, fmt_n, fmt_pct


@dataclass
class CompMetrics:
    chord_membership: float
    bass_root: float
    voice_lead_avg: float
    density_per_bar: float
    n_piano_notes: int
    n_bass_notes: int


def _score_midi(midi_path: str, seq: ChordSequence) -> CompMetrics:
    """Inspect a rendered comp MIDI and score it against `seq`'s ground truth."""
    pm = pretty_midi.PrettyMIDI(midi_path)
    spb = 60.0 / seq.bpm

    piano_inst = next((i for i in pm.instruments if "piano" in i.name.lower()), None)
    bass_inst = next((i for i in pm.instruments if "bass" in i.name.lower()), None)
    piano_notes = piano_inst.notes if piano_inst else []
    bass_notes = bass_inst.notes if bass_inst else []

    # build a fast lookup: at time t, which chord is active?
    def chord_at(t_sec: float):
        beat = t_sec / spb
        for c in seq.chords:
            if c.start_beat <= beat < c.start_beat + c.duration_beats:
                return c
        return None

    # chord-membership: piano note pitch-class ∈ chord's pcs
    in_chord = 0
    for n in piano_notes:
        c = chord_at(n.start)
        if c is None:
            continue
        if (n.pitch % 12) in chord_pcs(c.symbol):
            in_chord += 1
    membership = in_chord / max(1, len(piano_notes))

    # bass-root: bass note pitch-class == chord root
    root_hits = 0
    for n in bass_notes:
        c = chord_at(n.start)
        if c is None:
            continue
        if (n.pitch % 12) == chord_root_pc(c.symbol):
            root_hits += 1
    bass_root = root_hits / max(1, len(bass_notes))

    # voice-leading: average semitone movement between successive chord
    # voicings. Sample one voicing (the chord notes sounding at chord onset)
    # per chord, then sum nearest-neighbor distances between consecutive
    # voicings.
    voicings_at_onset: list[list[int]] = []
    for c in seq.chords:
        onset_sec = c.start_beat * spb
        # piano notes whose start ≈ chord onset (within first beat of chord)
        chord_window_end = onset_sec + spb
        sounding = sorted({n.pitch for n in piano_notes
                           if onset_sec <= n.start < chord_window_end})
        if sounding:
            voicings_at_onset.append(sounding)
    avg_lead = 0.0
    if len(voicings_at_onset) > 1:
        total = 0.0
        pairs = 0
        for prev, curr in zip(voicings_at_onset, voicings_at_onset[1:]):
            # nearest-neighbor sum: each prev pitch's nearest curr pitch
            for p in prev:
                total += min(abs(p - c) for c in curr)
                pairs += 1
        avg_lead = total / max(1, pairs)

    # density: piano notes per bar, but collapse near-simultaneous notes
    # (same chord played as 4 pitches simultaneously = 1 hit, not 4)
    n_bars = max(1, seq.total_beats / 4.0)
    grouped = 0
    last_t = -1.0
    for n in sorted(piano_notes, key=lambda n: n.start):
        if n.start - last_t > 0.05:    # 50ms grouping window
            grouped += 1
            last_t = n.start
    density = grouped / n_bars

    return CompMetrics(
        chord_membership=membership,
        bass_root=bass_root,
        voice_lead_avg=avg_lead,
        density_per_bar=density,
        n_piano_notes=len(piano_notes),
        n_bass_notes=len(bass_notes),
    )


def render_and_score(seq: ChordSequence, name: str, *,
                      voicing: str, rhythm: str, spice: float = 0.0) -> CompMetrics:
    """Render a comp MIDI for `seq` with the given knobs, then score it."""
    out_path = EVAL_DIR / f"comp_{name}_{voicing}_{rhythm}_s{int(spice*100):02}.mid"
    if not out_path.exists():
        # apply reharm at requested spice (0.0 = pass-through)
        if spice > 0:
            seq_to_use = reharmonize(seq, spice=spice, seed=0)
        else:
            seq_to_use = seq
        write_midi(seq_to_use, out_path,
                   rhythm=rhythm, voicing=voicing,
                   include_bass=True, walking_bass=False, piano_bass=False)
    return _score_midi(str(out_path), seq)


def run_comp_bench() -> dict:
    print("\n=== comp bench ===")
    print(f"{'fixture':<14}  {'voicing':<8}  {'rhythm':<14}  "
          f"{'in-chord':>8}  {'bass-root':>9}  {'lead':>6}  {'dens':>6}")
    print("-" * 72)
    cells = [
        ("rootless", "charleston"),
        ("evans", "charleston"),
        ("drop2", "freddie_green"),
        ("quartal", "sustained"),
        ("rootless", "anticipations"),
    ]
    results: dict[str, dict] = {}
    for name, seq in fixtures().items():
        for voicing, rhythm in cells:
            try:
                m = render_and_score(seq, name, voicing=voicing, rhythm=rhythm)
            except Exception as e:
                print(f"{name:<14}  {voicing:<8}  {rhythm:<14}  ERR  {e}")
                continue
            print(f"{name:<14}  {voicing:<8}  {rhythm:<14}  "
                  f"{fmt_pct(m.chord_membership):>8}  {fmt_pct(m.bass_root):>9}  "
                  f"{fmt_n(m.voice_lead_avg):>6}  {fmt_n(m.density_per_bar):>6}")
            results.setdefault(name, {})[f"{voicing}/{rhythm}"] = m.__dict__
    print()
    return results


if __name__ == "__main__":
    run_comp_bench()

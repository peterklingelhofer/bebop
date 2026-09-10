"""Score-view sanity check: root spelling, key-based flat/sharp preference,
and the per-chord data the HTML report's Score section draws from.

Run directly (no pytest, matches the other tests/eval scripts):
    python -m tests.eval.score_check
"""

from __future__ import annotations

from pathlib import Path

from bebop.io.chart import parse_chart
from bebop.reharm import reharmonize
from bebop.reharm.substitutions import is_dominant, prefer_flats_for_key, quality_of, root_name
from bebop.render.explainer import chart_comparison, explain_substitution, spell_pitch, voicing_breakdown
from bebop.types import Chord, ChordSequence, KeyChange

DEMO_CHART = Path(__file__).resolve().parents[2] / "charts" / "demo.txt"


def main() -> None:
    seq = parse_chart(DEMO_CHART)

    # (a) key of F: reharmonizing should never spell a root with a sharp
    for spice in (0.5, 0.9):
        reharmed = reharmonize(seq, spice=spice, seed=0)
        for chord in reharmed.chords:
            root = root_name(chord.symbol)
            assert "#" not in root, (
                f"spice={spice}: {chord.symbol!r} has a sharp-spelled root in the key of F"
            )
        print(f"  spice {spice}: {len(reharmed.chords)} chords, no sharp roots")

    # (b) prefer_flats_for_key
    assert prefer_flats_for_key("Bb")
    assert prefer_flats_for_key("Dm")
    assert not prefer_flats_for_key("G")
    assert not prefer_flats_for_key(None)
    print("  prefer_flats_for_key: ok")

    # (c) chart_comparison's original_pcs: bar 1 of demo.txt is "F" -> (0, 5, 9)
    reharmed0 = reharmonize(seq, spice=0.0, seed=0)
    rows = chart_comparison(seq, reharmed0)
    bar1 = next(r for r in rows if r.bar == 1)
    assert bar1.original_symbol == "F", f"expected bar 1 to be F, got {bar1.original_symbol!r}"
    assert bar1.original_pcs == (0, 5, 9), f"bar 1 original_pcs = {bar1.original_pcs}"
    print("  chart_comparison original_pcs: ok")

    # (d) root_name keeps the chart's own spelling
    assert root_name("Bbmaj7") == "Bb"
    assert root_name("F#m7b5") == "F#"
    print("  root_name: ok")

    # (e) extension-change wording consumes each matched token instead of
    # re-matching a shorter token inside a longer one already counted
    assert explain_substitution(["Bb"], "Bbmaj9") == "added maj9"
    assert explain_substitution(["Gm"], "Gm7") == "added 7"
    assert explain_substitution(["C7"], "C13") == "added 13"
    print("  explain_substitution extension wording: ok")

    # (f) 11ths and 13ths are dominant too, not just 7ths and 9ths
    assert quality_of("C13") == "dom"
    assert is_dominant("F13")
    print("  quality_of / is_dominant on 11ths and 13ths: ok")

    # (g) tritone-sub detection: Gb7 resolves DOWN a half-step to F (the next
    # chord), which is the sub for F's own dominant, C7
    note = explain_substitution(["F"], "Gb7", "F", prefer_flats=True)
    assert note.startswith("tritone sub"), f"expected a tritone-sub note, got {note!r}"
    print("  tritone-sub detection: ok")

    # (h) pitch spelling comes from the chord, not the key: A7 in the key of F
    # (which otherwise prefers flats) still spells its own third C#, not Db
    seq_a7 = ChordSequence(
        chords=[Chord(symbol="F", start_beat=0.0, duration_beats=4.0),
                Chord(symbol="A7", start_beat=4.0, duration_beats=4.0)],
        key_map=[KeyChange(start_beat=0.0, key="F")],
    )
    vb_a7 = voicing_breakdown(seq_a7, "rootless")[1]
    names = (vb_a7.bass_name,) + vb_a7.pitch_names
    assert any(n.startswith("C#") for n in names), f"A7 should spell a C#: {names}"
    assert not any(n.startswith("Db") for n in names), f"A7 should not spell a Db: {names}"

    seq_bbmaj9 = ChordSequence(chords=[Chord(symbol="Bbmaj9", start_beat=0.0, duration_beats=4.0)])
    vb_bbmaj9 = voicing_breakdown(seq_bbmaj9, "rootless")[0]
    assert vb_bbmaj9.bass_name.startswith("Bb"), f"Bbmaj9 bass should spell Bb: {vb_bbmaj9.bass_name}"

    assert spell_pitch(59, "Cb", False) == "Cb4"
    print("  chord-based pitch spelling: ok")

    print("ok")


if __name__ == "__main__":
    main()

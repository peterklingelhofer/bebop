"""Single-page HTML audit report with in-browser dual-audio mixer.

Embeds:
    - the original song as a single shared <audio> element
    - the spice × voicing comp matrix; each cell has a Play button that starts
      the original + that cell's comp WAV in sync, with per-cell volume slider
      to balance the comp against the original (set to taste in the browser)
    - the source-disagreement table (sorted by noisiness)
    - a side-by-side diff: your hand chart vs. the ensemble's suggested chart

Vanilla JS, no framework. Audio paths are relative to the report file so it
travels well.
"""

from __future__ import annotations

import difflib
import html as html_lib
import os
from dataclasses import dataclass
from pathlib import Path

from bebop.io.disagreement import disagreement_table
from bebop.types import ChordSequence


def _relpath(target: Path, start: Path) -> str:
    """Compute a path from `start` to `target` that may traverse up directories."""
    return os.path.relpath(target.resolve(), start=start.resolve())


@dataclass(frozen=True, slots=True)
class MatrixCell:
    spice: float
    voicing: str
    midi_path: Path
    wav_path: Path | None
    mix_path: Path | None      # legacy; HTML mixer renders in-browser instead


_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
       max-width: 1300px; margin: 2rem auto; padding: 0 1rem; color: #222; line-height: 1.5; }
h1 { margin-bottom: 0.2rem; }
h2 { margin-top: 2.5rem; padding-bottom: 0.2rem; border-bottom: 2px solid #eee; }
.meta { color: #666; font-size: 0.9rem; margin-bottom: 1rem; }

.transport {
    position: sticky; top: 0; z-index: 10;
    background: #fff; padding: 0.8rem 1rem; margin-bottom: 1.5rem;
    border: 1px solid #eaeaea; border-radius: 8px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.04);
}
.transport-row { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
.transport audio { flex: 1; min-width: 280px; height: 36px; margin: 0; }
.transport label { font-size: 0.85rem; color: #555; }
.now-playing { font-size: 0.85rem; color: #888; margin-top: 0.5rem; }
.now-playing strong { color: #222; }

table { border-collapse: collapse; margin: 1rem 0; font-size: 0.9rem; }
th, td { padding: 0.4rem 0.7rem; text-align: left; border-bottom: 1px solid #eee; }
th { background: #f5f5f5; font-weight: 600; }
tr:hover { background: #fafbfc; }

.matrix { display: grid; gap: 0.7rem; margin-top: 1rem; }
.matrix-cell { background: #fafafa; padding: 0.7rem; border-radius: 6px;
               border: 1px solid #eaeaea; transition: background 0.1s; }
.matrix-cell.active { background: #fff8d6; border-color: #d4b13b; }
.matrix-cell .label { font-size: 0.7rem; color: #888; text-transform: uppercase;
                      letter-spacing: 0.5px; margin: 0.5rem 0 0.2rem 0; }
.matrix-cell audio { width: 100%; height: 30px; margin: 0.2rem 0; }

.play-btn {
    width: 100%; padding: 0.5rem; font-size: 0.95rem; font-weight: 600;
    background: #2563eb; color: white; border: none; border-radius: 5px;
    cursor: pointer; transition: background 0.1s;
}
.play-btn:hover { background: #1d4ed8; }
.play-btn.playing { background: #dc2626; }
.play-btn.playing:hover { background: #b91c1c; }

.vol-row { display: flex; align-items: center; gap: 0.5rem; margin: 0.4rem 0; }
.vol-row label { font-size: 0.75rem; color: #555; min-width: 3.5em; }
.vol-row input[type=range] { flex: 1; }
.vol-row .vol-readout { font-size: 0.7rem; color: #888; min-width: 2.5em; text-align: right; }

.diff-pre { font-family: 'SF Mono', Menlo, monospace; font-size: 0.8rem;
            white-space: pre; overflow-x: auto;
            background: #fafafa; padding: 1rem; border-radius: 6px;
            border: 1px solid #eaeaea; }
.diff-add { background: #e6ffec; }
.diff-del { background: #ffebe9; }
.diff-hdr { color: #888; font-weight: 600; }
.disagree-1 { background: white; }
.disagree-2 { background: #fff8dc; }
.disagree-3 { background: #ffe4b5; }
.disagree-4 { background: #ffc1a1; }
.disagree-5 { background: #ff9a83; }
.disagree-6 { background: #ff7a6a; color: white; }
.legend { display: flex; gap: 1rem; flex-wrap: wrap; align-items: center;
          font-size: 0.8rem; margin: 0.5rem 0 1rem 0; }
.legend span { padding: 0.2rem 0.6rem; border-radius: 4px; }
"""

_JS = """
(() => {
    const original = document.getElementById('original-audio');
    const origVol = document.getElementById('original-vol');
    const origVolReadout = document.getElementById('original-vol-readout');
    const nowPlaying = document.getElementById('now-playing');
    let activeCellId = null;

    if (!original) return;  // no original audio embedded

    // master volume for original
    const setOrigVol = (v) => {
        original.volume = v;
        if (origVolReadout) origVolReadout.textContent = Math.round(v * 100) + '%';
    };
    if (origVol) {
        setOrigVol(parseFloat(origVol.value));
        origVol.addEventListener('input', e => setOrigVol(parseFloat(e.target.value)));
    }

    const stopActive = () => {
        if (activeCellId) {
            const prevComp = document.querySelector(`audio.comp[data-id="${activeCellId}"]`);
            const prevBtn = document.querySelector(`button.play-btn[data-id="${activeCellId}"]`);
            const prevCell = document.querySelector(`.matrix-cell[data-id="${activeCellId}"]`);
            if (prevComp) { prevComp.pause(); prevComp.currentTime = 0; }
            if (prevBtn) { prevBtn.classList.remove('playing'); prevBtn.textContent = '▶ Play with original'; }
            if (prevCell) prevCell.classList.remove('active');
        }
        original.pause();
        original.currentTime = 0;
        activeCellId = null;
        if (nowPlaying) nowPlaying.innerHTML = 'Nothing playing — click any <strong>Play with original</strong> button.';
    };

    // wire each cell's play button
    document.querySelectorAll('button.play-btn').forEach(btn => {
        const id = btn.dataset.id;
        const comp = document.querySelector(`audio.comp[data-id="${id}"]`);
        const cell = document.querySelector(`.matrix-cell[data-id="${id}"]`);
        if (!comp) return;

        btn.addEventListener('click', () => {
            if (activeCellId === id) {
                // toggle off
                stopActive();
                return;
            }
            stopActive();
            activeCellId = id;
            original.currentTime = 0;
            comp.currentTime = 0;
            // start them as close to simultaneously as possible
            const p1 = original.play();
            const p2 = comp.play();
            Promise.all([p1, p2]).catch(err => console.warn('play failed:', err));
            btn.classList.add('playing');
            btn.textContent = '■ Stop';
            cell.classList.add('active');
            if (nowPlaying) {
                nowPlaying.innerHTML = `Playing: <strong>${cell.dataset.label}</strong>`;
            }
        });
    });

    // when the original audio ends or pauses (user-triggered), stop the active comp too
    original.addEventListener('ended', stopActive);
    original.addEventListener('pause', () => {
        // distinguish user pause from our stopActive (which already cleared state)
        if (activeCellId) {
            const comp = document.querySelector(`audio.comp[data-id="${activeCellId}"]`);
            if (comp && !comp.paused) comp.pause();
        }
    });
    original.addEventListener('play', () => {
        if (activeCellId) {
            const comp = document.querySelector(`audio.comp[data-id="${activeCellId}"]`);
            if (comp && comp.paused) comp.play().catch(() => {});
        }
    });

    // per-cell volume slider for the comp
    document.querySelectorAll('input.comp-vol').forEach(slider => {
        const id = slider.dataset.id;
        const comp = document.querySelector(`audio.comp[data-id="${id}"]`);
        const readout = document.querySelector(`.vol-readout[data-id="${id}"]`);
        if (!comp) return;
        const apply = (v) => {
            comp.volume = v;
            if (readout) readout.textContent = Math.round(v * 100) + '%';
        };
        apply(parseFloat(slider.value));
        slider.addEventListener('input', e => apply(parseFloat(e.target.value)));
    });
})();
"""


def _render_diff(hand: str, suggested: str) -> str:
    diff = difflib.unified_diff(
        hand.splitlines(),
        suggested.splitlines(),
        fromfile="charts/butterfly_boy.txt (hand)",
        tofile="charts/butterfly_boy.suggested.txt (ensemble)",
        lineterm="",
    )
    out: list[str] = []
    for line in diff:
        esc = html_lib.escape(line)
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            out.append(f'<span class="diff-hdr">{esc}</span>')
        elif line.startswith("+"):
            out.append(f'<span class="diff-add">{esc}</span>')
        elif line.startswith("-"):
            out.append(f'<span class="diff-del">{esc}</span>')
        else:
            out.append(esc)
    return "\n".join(out) if out else "(charts identical)"


def _render_matrix(cells: list[MatrixCell], report_dir: Path,
                   has_original: bool, default_comp_vol: float) -> str:
    spices = sorted({c.spice for c in cells})
    voicings = sorted({c.voicing for c in cells})
    cell_lookup = {(c.spice, c.voicing): c for c in cells}

    out: list[str] = []
    out.append('<div class="matrix" style="grid-template-columns: 6em '
               + " ".join(["1fr"] * len(voicings)) + ';">')

    out.append('<div></div>')
    for v in voicings:
        out.append(f'<div class="matrix-cell" style="background: #efefef; text-align: center;">'
                   f'<strong>{html_lib.escape(v)}</strong></div>')

    for sp in spices:
        out.append(f'<div class="matrix-cell" style="background: #efefef; '
                   f'display: flex; align-items: center; justify-content: center;">'
                   f'<strong>spice {sp:.1f}</strong></div>')
        for v in voicings:
            c = cell_lookup.get((sp, v))
            if c is None:
                out.append('<div class="matrix-cell">—</div>')
                continue
            cell_id = f"sp{int(round(sp * 100)):02d}_{v}"
            label = f"spice {sp:.1f} / {v}"
            html_parts: list[str] = [
                f'<div class="matrix-cell" data-id="{cell_id}" data-label="{html_lib.escape(label)}">'
            ]

            if c.wav_path is not None and c.wav_path.exists():
                rel = _relpath(c.wav_path, report_dir)
                # invisible comp audio — controlled via the play button + volume slider
                html_parts.append(
                    f'<audio class="comp" data-id="{cell_id}" preload="metadata" src="{rel}"></audio>'
                )
                if has_original:
                    html_parts.append(
                        f'<button class="play-btn" data-id="{cell_id}">▶ Play with original</button>'
                    )
                    html_parts.append('<div class="vol-row">')
                    html_parts.append('<label>comp</label>')
                    html_parts.append(
                        f'<input type="range" class="comp-vol" data-id="{cell_id}" '
                        f'min="0" max="1" step="0.01" value="{default_comp_vol}">'
                    )
                    html_parts.append(f'<span class="vol-readout" data-id="{cell_id}">'
                                      f'{int(default_comp_vol * 100)}%</span>')
                    html_parts.append('</div>')
                # always include a standalone player too in case the user wants to scrub
                html_parts.append('<div class="label">comp only (scrub)</div>')
                html_parts.append(f'<audio controls preload="none" src="{rel}"></audio>')

            rel_midi = _relpath(c.midi_path, report_dir)
            html_parts.append(f'<div class="label" style="margin-top: 0.4rem;">'
                              f'<a href="{rel_midi}">download .mid</a></div>')
            html_parts.append('</div>')
            out.append("".join(html_parts))
    out.append('</div>')
    return "\n".join(out)


def _render_disagreement(sources: dict[str, ChordSequence]) -> str:
    rows = disagreement_table(sources)
    if not rows:
        return "<p>(no source data)</p>"
    names = list(sources)
    out: list[str] = []
    out.append('<div class="legend">'
               + "".join(f'<span class="disagree-{i}">{i} chord{"s" if i>1 else ""} '
                         f'{"= unanimous" if i==1 else "→ disagree"}</span>'
                         for i in range(1, len(names) + 1))
               + '</div>')
    out.append('<table>')
    out.append('<tr><th>bar</th><th>agree</th>' + "".join(f'<th>{html_lib.escape(n)}</th>'
                                                          for n in names) + '</tr>')
    rows_sorted = sorted(rows, key=lambda r: (-r[2], r[0]))
    for bar, labels, score in rows_sorted:
        cls = f"disagree-{min(score, 6)}"
        agree_str = "all" if score == 1 else f"{len(names)-score+1}/{len(names)}"
        cells = "".join(f'<td>{html_lib.escape(labels.get(n, "--"))}</td>' for n in names)
        out.append(f'<tr class="{cls}"><td>{bar}</td><td>{agree_str}</td>{cells}</tr>')
    out.append('</table>')
    return "\n".join(out)


def write_html_report(
    output_path: str | Path,
    *,
    title: str,
    bpm: float,
    cells: list[MatrixCell],
    sources: dict[str, ChordSequence],
    hand_chart_text: str | None = None,
    suggested_chart_text: str | None = None,
    original_audio_path: str | Path | None = None,
    default_original_vol: float = 0.7,
    default_comp_vol: float = 0.35,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_dir = output_path.parent
    has_original = original_audio_path is not None and Path(original_audio_path).exists()

    parts: list[str] = []
    parts.append('<!DOCTYPE html>')
    parts.append(f'<html><head><meta charset="utf-8"><title>{html_lib.escape(title)}</title>')
    parts.append(f'<style>{_CSS}</style></head><body>')
    parts.append(f'<h1>{html_lib.escape(title)}</h1>')
    parts.append(f'<p class="meta">{bpm:.0f} BPM &nbsp;·&nbsp; '
                 f'{len(cells)} variants &nbsp;·&nbsp; '
                 f'{len(sources)} chord sources</p>')

    if has_original:
        rel_orig = _relpath(Path(original_audio_path), report_dir)
        parts.append('<div class="transport">')
        parts.append('<div class="transport-row">')
        parts.append(f'<audio id="original-audio" preload="metadata" src="{rel_orig}" controls></audio>')
        parts.append('</div>')
        parts.append('<div class="transport-row" style="margin-top: 0.5rem;">')
        parts.append('<label>original volume</label>')
        parts.append(f'<input type="range" id="original-vol" min="0" max="1" step="0.01" '
                     f'value="{default_original_vol}" style="flex: 1; max-width: 240px;">')
        parts.append(f'<span id="original-vol-readout">{int(default_original_vol * 100)}%</span>')
        parts.append('</div>')
        parts.append('<div class="now-playing" id="now-playing">'
                     'Nothing playing — click any <strong>Play with original</strong> button below.</div>')
        parts.append('</div>')
    else:
        parts.append('<p class="meta"><em>(no original audio supplied — pass --mix-with to enable '
                     'in-browser overlay)</em></p>')

    if cells:
        parts.append('<h2>Comp matrix</h2>')
        parts.append('<p class="meta">Each cell\'s <strong>Play with original</strong> button starts '
                     'the song and the comp together in sync. Slide the comp volume to find your '
                     'balance — the original\'s volume slider is up top. Click a different cell\'s '
                     'play button to A/B between variants. The "comp only (scrub)" player below '
                     'each is for hearing the voicing in isolation or scrubbing through the comp.</p>')
        parts.append(_render_matrix(cells, report_dir, has_original, default_comp_vol))

    if sources:
        parts.append('<h2>Source disagreement</h2>')
        parts.append('<p class="meta">Sorted by disagreement score (noisiest bars first). '
                     'Use this to find which bars deserve a second listen.</p>')
        parts.append(_render_disagreement(sources))

    if hand_chart_text and suggested_chart_text:
        parts.append('<h2>Chord chart diff: hand vs. ensemble suggestion</h2>')
        parts.append('<p class="meta">Lines in red are in your hand chart but not the suggestion. '
                     'Lines in green are the ensemble&rsquo;s additions.</p>')
        parts.append(f'<pre class="diff-pre">{_render_diff(hand_chart_text, suggested_chart_text)}</pre>')

    parts.append(f'<script>{_JS}</script>')
    parts.append('</body></html>')
    output_path.write_text("\n".join(parts))
    return output_path

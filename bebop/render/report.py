"""Single-page HTML audit report with in-browser dual-audio mixer.

Embeds:
    - the original song as a single shared <audio> element, with an optional
      "duck song bass" toggle that swaps in a pre-rendered HPF version of the
      original (created at render time via ffmpeg)
    - the spice × voicing comp matrix; each cell has a Play button that starts
      the original + that cell's comp WAV in sync, with per-cell volume slider
      to balance the comp against the original
    - the source-disagreement table (sorted by noisiness)
    - a side-by-side diff: your hand chart vs. the ensemble's suggested chart

Vanilla JS, no Web Audio, no framework. Audio paths are relative to the report
file. Original audio is symlinked into the report directory at render time so
everything is same-directory and works equally well from file:// or http://.
"""

from __future__ import annotations

import difflib
import html as html_lib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from bebop.io.disagreement import disagreement_table
from bebop.render.explainer import (
    ChartRow,
    VoicingBreakdown,
    chart_comparison,
    voicing_breakdown,
)
from bebop.types import ChordSequence


def _relpath(target: Path, start: Path) -> str:
    """Compute a path from `start` to `target` that may traverse up directories."""
    return os.path.relpath(target.resolve(), start=start.resolve())


@dataclass(frozen=True, slots=True)
class MatrixCell:
    spice: float
    voicing: str
    bass: str                  # "sustained" or "walking" (or "none" for --no-bass)
    rhythm: str = "charleston" # one of charleston, two_and_four, sustained, anticipations
    alignment: str = "raw"     # "raw" or "aligned" — present when --align-both is on
    piano_bass: str = "off"    # "off" (upright only) or "on" (piano LH doubles bass)
    midi_source: str = "midi"  # label for which --midi source produced this cell
                                # (only matters when combining multiple runs)
    midi_path: Path = Path()
    wav_path: Path | None = None
    mix_path: Path | None = None  # legacy; HTML mixer renders in-browser instead


_DEFAULT_DUCK_HPF_HZ = 200     # default cutoff for the "duck song bass" pre-rendered version


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

.duck-btn {
    padding: 0.35rem 0.9rem; font-size: 0.85rem; font-weight: 500;
    background: #f3f4f6; color: #222; border: 1px solid #d1d5db; border-radius: 5px;
    cursor: pointer; transition: background 0.1s;
}
.duck-btn:hover { background: #e5e7eb; }
.duck-btn.ducked { background: #2563eb; color: white; border-color: #2563eb; }
.duck-btn.ducked:hover { background: #1d4ed8; }
.duck-help { font-size: 0.75rem; color: #888; }

table { border-collapse: collapse; margin: 1rem 0; font-size: 0.9rem; }
th, td { padding: 0.4rem 0.7rem; text-align: left; border-bottom: 1px solid #eee; }
th { background: #f5f5f5; font-weight: 600; }
tr:hover { background: #fafbfc; }

.matrix { display: grid; gap: 0.7rem; margin-top: 1rem; }
.matrix-cell { background: #fafafa; padding: 0.7rem; border-radius: 6px;
               border: 1px solid #eaeaea; }
.matrix-header {
    /* sticky column headers — stay visible below the transport bar while scrolling */
    position: sticky;
    top: var(--transport-height, 180px);
    z-index: 5;
}
.matrix-header-corner {
    position: sticky;
    top: var(--transport-height, 180px);
    z-index: 5;
    background: #fff;     /* mask the spice column behind it */
}
.matrix-cell .label { font-size: 0.7rem; color: #888; text-transform: uppercase;
                      letter-spacing: 0.5px; margin: 0.5rem 0 0.2rem 0; }
.matrix-cell audio { width: 100%; height: 30px; margin: 0.2rem 0; }
.bass-variant { padding: 0.5rem 0; }
.bass-variant + .bass-variant { border-top: 1px dashed #ddd; margin-top: 0.5rem; }
.bass-variant.active { background: #fff8d6; border-radius: 4px;
                       box-shadow: 0 0 0 2px #d4b13b inset; padding: 0.5rem; }
.bass-label { font-size: 0.75rem; font-weight: 600; color: #555;
              text-transform: uppercase; letter-spacing: 0.5px;
              margin-bottom: 0.3rem; }

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

.chart-btn {
    margin-top: 0.3rem;
    padding: 0.25rem 0.5rem;
    font-size: 0.7rem; font-weight: 500;
    background: #f3f4f6; color: #444; border: 1px solid #d1d5db; border-radius: 4px;
    cursor: pointer; width: 100%;
}
.chart-btn:hover { background: #e5e7eb; }

.modal-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    display: none; align-items: flex-start; justify-content: center;
    z-index: 100; padding: 4vh 2vw;
}
.modal-backdrop.open { display: flex; }
.modal-content {
    background: #fff; border-radius: 10px; padding: 1.4rem 1.6rem;
    max-width: 1100px; width: 100%; max-height: 92vh; overflow-y: auto;
    box-shadow: 0 18px 36px rgba(0,0,0,0.25);
    font-size: 0.85rem;
}
.modal-header { display: flex; justify-content: space-between; align-items: baseline;
                gap: 1rem; padding-bottom: 0.5rem; border-bottom: 1px solid #eee;
                margin-bottom: 0.8rem; }
.modal-header h3 { margin: 0; font-size: 1.05rem; }
.modal-close { background: transparent; border: none; font-size: 1.6rem; line-height: 1;
               cursor: pointer; color: #888; padding: 0; }
.modal-close:hover { color: #222; }

.chord-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.chord-table th { background: #f7f7f9; text-align: left; padding: 0.4rem 0.6rem;
                  border-bottom: 2px solid #e5e7eb; position: sticky; top: 0; }
.chord-table td { padding: 0.35rem 0.6rem; border-bottom: 1px solid #f0f0f0;
                  vertical-align: top; }
.chord-table tr:hover { background: #fafbfc; }
.chord-table tr.now-playing {
    background: #fff3b0;
    box-shadow: inset 4px 0 0 #d4a017;
}
.chord-table tr.now-playing td { font-weight: 600; }
.chord-table .col-bar { color: #888; font-variant-numeric: tabular-nums; width: 4em; }
.chord-table .col-original { color: #666; width: 7em; }
.chord-table .col-new { font-weight: 600; width: 9em; }
.chord-table .col-note { color: #555; font-style: italic; font-size: 0.8rem; }
.chord-table .col-voicing { color: #444; font-family: 'SF Mono', Menlo, monospace;
                            font-size: 0.78rem; white-space: nowrap; }
.voicing-summary { color: #666; font-style: italic; margin-bottom: 0.2rem;
                   font-family: -apple-system, BlinkMacSystemFont, sans-serif;
                   font-size: 0.75rem; }
.voicing-notes { display: grid; grid-template-columns: auto auto 1fr; gap: 0.2rem 0.6rem;
                 align-items: baseline; }
.voicing-notes .pitch { font-weight: 600; color: #222; }
.voicing-notes .role  { color: #b35; font-size: 0.7rem; text-transform: uppercase;
                        letter-spacing: 0.4px; }
.voicing-notes .iv    { color: #555; }
.voicing-notes .bass-row .pitch { color: #1a4d8f; }   /* bass note in blue */
.voicing-notes .bass-row .role  { color: #1a4d8f; }

.score-controls { display: flex; align-items: center; gap: 1.5rem; flex-wrap: wrap;
                  margin: 0.8rem 0; }
.score-controls label { font-size: 0.85rem; color: #555; }
.score-controls select {
    margin-left: 0.4rem; padding: 0.35rem 0.6rem; font-size: 0.85rem;
    background: #f3f4f6; color: #222; border: 1px solid #d1d5db; border-radius: 5px;
}
.score-legend { display: flex; align-items: center; gap: 0.8rem; font-size: 0.8rem; color: #555; }
.score-legend .swatch {
    display: inline-block; width: 0.8em; height: 0.8em; border-radius: 2px;
    margin-right: 0.3em; vertical-align: -0.1em;
}
.score-legend .swatch.black { background: #222; }
.score-legend .swatch.red { background: #c0392b; }
.score-wrap { background: #fff; border: 1px solid #eaeaea; border-radius: 8px;
              padding: 1rem; overflow-x: auto; }
.score-system + .score-system { margin-top: 0.5rem; }
.score-notes { font-size: 0.8rem; color: #555; margin: 0.3rem 0 1rem; line-height: 1.5; }
.score-notes b { color: #222; font-weight: 600; }
.score-notes .sym { color: #c0392b; font-weight: 600; }
"""

_JS = """
(() => {
    // Measure the sticky transport bar's height and feed it to the
    // --transport-height CSS variable so the matrix's sticky column
    // headers (.matrix-header) sit just below it without overlap. Re-measures
    // on resize because the transport wraps differently at narrow widths.
    const setTransportHeight = () => {
        const t = document.querySelector('.transport');
        if (!t) return;
        const h = t.getBoundingClientRect().height;
        document.documentElement.style.setProperty('--transport-height', `${h + 8}px`);
    };
    setTransportHeight();
    window.addEventListener('resize', setTransportHeight);

    const original = document.getElementById('original-audio');
    const origVol = document.getElementById('original-vol');
    const origVolReadout = document.getElementById('original-vol-readout');
    const duckBtn = document.getElementById('duck-btn');
    const nowPlaying = document.getElementById('now-playing');
    let activeCellId = null;

    if (!original) return;

    // master volume for the original — pure HTMLMediaElement.volume, no Web Audio
    const setOrigVol = (v) => {
        original.volume = v;
        if (origVolReadout) origVolReadout.textContent = Math.round(v * 100) + '%';
    };
    if (origVol) {
        setOrigVol(parseFloat(origVol.value));
        origVol.addEventListener('input', e => setOrigVol(parseFloat(e.target.value)));
    }

    // ── duck-song-bass toggle: swap the original's <audio src> between full and
    // a pre-rendered high-passed version. Preserves currentTime + paused state
    // across the swap so the user can flip mid-listen without losing position.
    if (duckBtn) {
        const fullSrc = duckBtn.dataset.fullSrc;
        const duckedSrc = duckBtn.dataset.duckedSrc;
        let isDucked = false;
        duckBtn.addEventListener('click', () => {
            const wasPlaying = !original.paused;
            const t = original.currentTime;
            isDucked = !isDucked;
            duckBtn.classList.toggle('ducked', isDucked);
            duckBtn.textContent = isDucked ? '▣ song bass: DUCKED' : '☐ duck song bass';
            const newSrc = isDucked ? duckedSrc : fullSrc;
            // load metadata, then restore time + resume if we were playing
            const onReady = () => {
                original.removeEventListener('loadedmetadata', onReady);
                original.currentTime = t;
                if (wasPlaying) original.play().catch(err => console.warn('resume:', err));
            };
            original.addEventListener('loadedmetadata', onReady);
            original.src = newSrc;
            original.load();
        });
    }

    const stopActive = () => {
        if (activeCellId) {
            const prevComp = document.querySelector(`audio.comp[data-id="${activeCellId}"]`);
            const prevBtn = document.querySelector(`button.play-btn[data-id="${activeCellId}"]`);
            const prevCell = document.querySelector(`.bass-variant[data-id="${activeCellId}"]`);
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
        const cell = document.querySelector(`.bass-variant[data-id="${id}"]`);
        if (!comp) return;

        btn.addEventListener('click', () => {
            if (activeCellId === id) {
                stopActive();
                return;
            }
            stopActive();
            activeCellId = id;
            original.currentTime = 0;
            comp.currentTime = 0;
            // start them as close to simultaneously as possible
            Promise.all([original.play(), comp.play()]).catch(err => console.warn('play failed:', err));
            btn.classList.add('playing');
            btn.textContent = '■ Stop';
            cell.classList.add('active');
            if (nowPlaying) nowPlaying.innerHTML = `Playing: <strong>${cell.dataset.label}</strong>`;
        });
    });

    // when the original's natural end is reached, stop everything
    original.addEventListener('ended', stopActive);

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

    // ─── chord-chart modal + theory popup ───
    const modal = document.getElementById('chord-modal');
    const modalBody = document.getElementById('modal-body');
    const modalTitle = document.getElementById('modal-title');
    const modalClose = document.querySelector('.modal-close');
    let highlightFrame = null;
    let modalRows = [];      // current modal's row elements with .data-end-beat etc.

    const closeModal = () => {
        if (!modal) return;
        modal.classList.remove('open');
        if (highlightFrame) cancelAnimationFrame(highlightFrame);
        highlightFrame = null;
        modalRows.forEach(r => r.classList.remove('now-playing'));
        modalRows = [];
    };
    if (modalClose) modalClose.addEventListener('click', closeModal);
    if (modal) modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

    const data = window._bebopExplainer || null;

    // Pick whichever audio element is currently playing (in priority: original,
    // then the active comp, then any comp-only scrub player). Returns {audio, source}.
    const findPlayingAudio = () => {
        if (!original.paused) return { audio: original, source: 'original' };
        if (activeCellId) {
            const comp = document.querySelector(`audio.comp[data-id="${activeCellId}"]`);
            if (comp && !comp.paused) return { audio: comp, source: 'comp' };
        }
        // fallback: any <audio controls> that's currently playing (scrub player)
        for (const a of document.querySelectorAll('audio[controls]')) {
            if (!a.paused) return { audio: a, source: 'scrub' };
        }
        return null;
    };

    const startHighlightLoop = (bpm) => {
        if (highlightFrame) cancelAnimationFrame(highlightFrame);
        let lastIdx = -1;
        const tick = () => {
            if (!modal.classList.contains('open')) return;
            const playing = findPlayingAudio();
            let idx = -1;
            if (playing) {
                const beat = playing.audio.currentTime * bpm / 60;
                // find row whose [start_beat, end_beat) contains this beat
                for (let i = 0; i < modalRows.length; i++) {
                    const sb = parseFloat(modalRows[i].dataset.startBeat);
                    const eb = parseFloat(modalRows[i].dataset.endBeat);
                    if (beat >= sb && beat < eb) { idx = i; break; }
                }
            }
            if (idx !== lastIdx) {
                modalRows.forEach(r => r.classList.remove('now-playing'));
                if (idx >= 0) {
                    modalRows[idx].classList.add('now-playing');
                    // scroll into view (within the modal body)
                    const el = modalRows[idx];
                    const container = el.closest('.modal-content');
                    const elTop = el.offsetTop;
                    const containerTop = container.scrollTop;
                    const containerH = container.clientHeight;
                    if (elTop < containerTop + 80 || elTop > containerTop + containerH - 100) {
                        container.scrollTo({ top: elTop - containerH / 2, behavior: 'smooth' });
                    }
                }
                lastIdx = idx;
            }
            highlightFrame = requestAnimationFrame(tick);
        };
        highlightFrame = requestAnimationFrame(tick);
    };

    const openChartFor = (progressionId, voicingId, label) => {
        if (!data || !modal) return;
        const progression = data.progressions[progressionId] || [];
        const voicings = data.voicings[voicingId] || [];
        modalTitle.textContent = label;

        const rows = progression.map((row, i) => {
            const v = voicings[i] || null;
            const orig = row.original_symbol || '<span style="color:#bbb">(inserted)</span>';
            const newSym = row.new_symbol + (row.new_bass ? '/' + row.new_bass : '');
            const note = row.theory_note || '';
            let voicingCell = '';
            if (v) {
                // Render every sounding note: bass first, then chord-voicing pitches
                // top-to-bottom from low to high. Each row shows pitch · role · interval.
                const rhRows = (v.pitches || []).map((midi, idx) => {
                    return `
                        <div class="pitch">${v.pitch_names[idx]}</div>
                        <div class="role">RH</div>
                        <div class="iv">${v.intervals[idx]}</div>
                    `;
                }).join('');
                const bassRow = (v.bass_name) ? `
                    <div class="bass-row pitch">${v.bass_name}</div>
                    <div class="bass-row role">bass</div>
                    <div class="bass-row iv">${v.bass_interval}</div>
                ` : '';
                voicingCell = `
                    <div class="voicing-summary">${v.summary || ''}</div>
                    <div class="voicing-notes">${bassRow}${rhRows}</div>
                `;
            }
            const endBeat = row.beat + row.duration_beats;
            return `
                <tr data-start-beat="${row.beat}" data-end-beat="${endBeat}">
                    <td class="col-bar">${row.bar} (${row.duration_beats}b)</td>
                    <td class="col-original">${orig}</td>
                    <td class="col-new">${newSym}</td>
                    <td class="col-note">${note}</td>
                    <td class="col-voicing">${voicingCell}</td>
                </tr>`;
        }).join('');

        modalBody.innerHTML = `
            <table class="chord-table">
                <thead><tr>
                    <th>Bar</th>
                    <th>Original</th>
                    <th>This variant</th>
                    <th>What changed</th>
                    <th>Voicing</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>`;
        modalRows = Array.from(modalBody.querySelectorAll('tbody tr'));
        modal.classList.add('open');
        startHighlightLoop(data.bpm);
    };

    document.querySelectorAll('button.chart-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            openChartFor(btn.dataset.progressionId,
                         btn.dataset.voicingId,
                         btn.dataset.label);
        });
    });
})();
"""


_SCORE_JS = r"""
(() => {
    const container = document.getElementById('score');
    if (!container) return;
    if (!window.Vex) {
        container.textContent = 'The score needs VexFlow from cdnjs; open this page with network access.';
        return;
    }
    const { Renderer, Stave, StaveNote, Voice, Formatter, Accidental, StaveConnector, Dot } = Vex.Flow;
    const RED = { fillStyle: '#c0392b', strokeStyle: '#c0392b' };

    const data = window._bebopExplainer || { progressions: {}, voicings: {} };
    const progressions = data.progressions || {};
    const voicingsMap = data.voicings || {};
    const [TS_NUM, TS_DEN] = data.time_signature || [4, 4];

    // the six durations (in quarter-note beats) the score can render; anything
    // else picks whichever of these is closest
    const DURATIONS = [4, 3, 2, 1.5, 1, 0.5];
    const DUR_CODE = { 4: 'w', 3: 'h', 2: 'h', 1.5: 'q', 1: 'q', 0.5: '8' };
    const DUR_DOTTED = { 3: true, 1.5: true };

    const nearestDuration = (beats) => DURATIONS.reduce(
        (best, d) => (Math.abs(d - beats) < Math.abs(best - beats) ? d : best), DURATIONS[0]);

    const toVexDuration = (beats) => {
        const d = nearestDuration(beats);
        return { code: DUR_CODE[d], dotted: !!DUR_DOTTED[d] };
    };

    // "Bb4" -> "bb/4", "F#3" -> "f#/3", "C4" -> "c/4"
    const toVexKey = (name) => {
        const m = /^([A-Ga-g])([#b]?)(-?\d+)$/.exec(name || 'C4');
        return m ? `${m[1].toLowerCase()}${m[2]}/${m[3]}` : 'c/4';
    };

    // double-bass parts are written an octave above where they sound
    const bassDisplayName = (name) => (name ? name.replace(/(-?\d+)$/, (o) => String(+o + 1)) : null);

    // chord symbols: first letter is the root, never touched; everything after
    // it is quality/extensions, where # and b are accidentals to prettify
    const prettyChord = (sym) => (!sym ? sym : sym[0] + sym.slice(1).replace(/#/g, '♯').replace(/b/g, '♭'));

    // theory notes are prose ("half-step below", "borrowed") so only prettify
    // a b/# immediately before a digit (an extension like "b9") or right after
    // an uppercase note letter (a note name like "Bb" or "F#"); words like
    // "below"/"borrowed" start lowercase so they're untouched either way
    const prettyNote = (text) => text
        .replace(/b(\d)/g, '♭$1').replace(/#(\d)/g, '♯$1')
        .replace(/([A-G])b(?![a-z])/g, '$1♭').replace(/([A-G])#/g, '$1♯');

    const escapeHtml = (s) => String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

    const groupByBar = (rows) => {
        const measures = [];
        let cur = null;
        rows.forEach((row, i) => {
            if (!cur || cur.bar !== row.bar) {
                cur = { bar: row.bar, idx: [] };
                measures.push(cur);
            }
            cur.idx.push(i);
        });
        return measures;
    };

    function draw(progKey, voicingKey) {
        container.innerHTML = '';
        const rows = progressions[progKey] || [];
        const vs = voicingsMap[voicingKey] || [];
        if (!rows.length) return;

        const measures = groupByBar(rows);
        const width = container.clientWidth || 800;
        let barsPerSystem = 4;
        while (barsPerSystem > 1 && width / barsPerSystem < 190) barsPerSystem--;
        const measureWidth = (width - 8) / barsPerSystem;

        // chord labels sit on shelf 1 and lift to shelf 3 on collision; TOP
        // leaves room for the lift
        const TOP = 20, STAVE_GAP = 100;
        const svgHeight = TOP + STAVE_GAP + 100;

        const connect = (ctx, treble, bass, type) => {
            new StaveConnector(treble, bass).setType(type).setContext(ctx).draw();
        };

        let prevKey;
        let globalMeasureIdx = 0;

        for (let sysStart = 0; sysStart < measures.length; sysStart += barsPerSystem) {
            const sysMeasures = measures.slice(sysStart, sysStart + barsPerSystem);

            const sysDiv = document.createElement('div');
            sysDiv.className = 'score-system';
            const svgDiv = document.createElement('div');
            svgDiv.className = 'score-svg';
            const notesP = document.createElement('p');
            notesP.className = 'score-notes';
            sysDiv.appendChild(svgDiv);
            sysDiv.appendChild(notesP);
            container.appendChild(sysDiv);

            const renderer = new Renderer(svgDiv, Renderer.Backends.SVG);
            renderer.resize(width, svgHeight);
            const ctx = renderer.getContext();

            const noteEntries = [];
            // the right edge of the last chord label on the lower shelf, carried
            // across the system so a label can't run into the next bar's
            let chordLowerEnd = -Infinity;
            let lastBar = null;
            let prevEntrySym = null;
            let prevEntryNote = null;

            sysMeasures.forEach((measure, posInSys) => {
                const x = posInSys * measureWidth + 4;
                const y = TOP;
                const isFirstOfSystem = posInSys === 0;
                const isVeryFirstMeasure = globalMeasureIdx === 0;

                const measureRows = measure.idx.map((i) => rows[i]);
                const key = measureRows[0].key || null;
                const keyChanged = key !== prevKey;

                const trebleStave = new Stave(x, y, measureWidth);
                const bassStave = new Stave(x, y + STAVE_GAP, measureWidth);

                if (isFirstOfSystem) {
                    trebleStave.addClef('treble');
                    bassStave.addClef('bass');
                }
                if (isVeryFirstMeasure) {
                    trebleStave.addTimeSignature(`${TS_NUM}/${TS_DEN}`);
                    bassStave.addTimeSignature(`${TS_NUM}/${TS_DEN}`);
                }
                if ((isFirstOfSystem || keyChanged) && key) {
                    trebleStave.addKeySignature(key);
                    bassStave.addKeySignature(key);
                }
                prevKey = key;

                trebleStave.setContext(ctx).draw();
                bassStave.setContext(ctx).draw();

                if (isFirstOfSystem) {
                    connect(ctx, trebleStave, bassStave, StaveConnector.type.BRACE);
                    connect(ctx, trebleStave, bassStave, StaveConnector.type.SINGLE_LEFT);
                }
                connect(ctx, trebleStave, bassStave, StaveConnector.type.SINGLE_RIGHT);

                const trebleNotes = [];
                const bassNotes = [];
                const redIdxByNote = [];
                const bassRedByNote = [];

                measure.idx.forEach((i) => {
                    const row = rows[i];
                    const v = vs[i] || null;
                    const { code, dotted } = toVexDuration(row.duration_beats);
                    const origPcs = row.original_pcs || [];
                    const isRed = (pc) => origPcs.length === 0 || !origPcs.includes(((pc % 12) + 12) % 12);

                    const pitches = (v && v.pitches) || [];
                    const pitchNames = (v && v.pitch_names) || [];
                    const trebleKeys = pitches.length ? pitchNames.map(toVexKey) : ['b/4'];
                    const trebleNote = new StaveNote({
                        clef: 'treble', keys: trebleKeys,
                        duration: pitches.length ? code : code + 'r', auto_stem: true,
                    });
                    const redIdx = new Set();
                    pitches.forEach((p, idx) => {
                        if (isRed(p)) { trebleNote.setKeyStyle(idx, RED); redIdx.add(idx); }
                    });
                    if (dotted) Dot.buildAndAttach([trebleNote], { all: true });
                    trebleNotes.push(trebleNote);
                    redIdxByNote.push(redIdx);

                    // written an octave above sounding pitch, like a real double-bass part
                    const bassName = v && v.bass_name;
                    const bassKey = bassName ? toVexKey(bassDisplayName(bassName)) : 'd/3';
                    const bassNote = new StaveNote({
                        clef: 'bass', keys: [bassKey],
                        duration: bassName ? code : code + 'r',
                    });
                    const bassIsRed = !!bassName && isRed(v.bass_pitch);
                    if (bassIsRed) bassNote.setStyle(RED);
                    if (dotted) Dot.buildAndAttach([bassNote], { all: true });
                    bassNotes.push(bassNote);
                    bassRedByNote.push(bassIsRed);
                });

                const tv = new Voice({ num_beats: TS_NUM, beat_value: TS_DEN })
                    .setStrict(false).addTickables(trebleNotes);
                const bv = new Voice({ num_beats: TS_NUM, beat_value: TS_DEN })
                    .setStrict(false).addTickables(bassNotes);
                Accidental.applyAccidentals([tv, bv], key || 'C');

                trebleNotes.forEach((note, idx) => {
                    const redSet = redIdxByNote[idx];
                    note.getModifiers().forEach((mod) => {
                        if (mod instanceof Accidental && redSet.has(mod.getIndex())) mod.setStyle(RED);
                    });
                });
                bassNotes.forEach((note, idx) => {
                    if (!bassRedByNote[idx]) return;
                    note.getModifiers().forEach((mod) => {
                        if (mod instanceof Accidental) mod.setStyle(RED);
                    });
                });

                new Formatter().joinVoices([tv]).joinVoices([bv]).formatToStave([tv, bv], trebleStave);
                tv.draw(ctx, trebleStave);
                bv.draw(ctx, bassStave);

                // chord symbols: black is the original chord, shown once at the start
                // of its span (a run of chords over the same original harmony); red is
                // whatever changed; a chord that repeats the previous one carries no label
                measure.idx.forEach((rowIdx, i) => {
                    const row = rows[rowIdx];
                    const note = trebleNotes[i];
                    const prevRow = rowIdx > 0 ? rows[rowIdx - 1] : null;
                    const newSym = row.new_symbol + (row.new_bass ? '/' + row.new_bass : '');
                    const isSpanStart = !prevRow || prevRow.bar !== row.bar
                        || prevRow.original_symbol !== row.original_symbol;
                    const showOrig = !!row.original_symbol && isSpanStart;
                    const changed = !row.original_symbol || newSym !== row.original_symbol;
                    const isRepeat = !!prevRow && row.new_symbol === prevRow.new_symbol;
                    const showNew = changed && !isRepeat;
                    if (!showOrig && !showNew) return;

                    const origText = showOrig ? prettyChord(row.original_symbol) : null;
                    const newText = showNew ? prettyChord(newSym) : null;
                    ctx.save();
                    ctx.setFont('-apple-system, Helvetica Neue, Arial', 13, 'bold');
                    const origW = origText ? ctx.measureText(origText).width : 0;
                    const gap = origText && newText ? 5 : 0;
                    const newW = newText ? ctx.measureText(newText).width : 0;
                    const nx = Math.min(note.getAbsoluteX(), width - 4 - origW - gap - newW);
                    // the lower shelf is the default; only a genuine collision with the
                    // previous label lifts this one to the upper shelf
                    const useUpper = nx < chordLowerEnd + 6;
                    const ny = trebleStave.getYForTopText(useUpper ? 3 : 1);
                    if (origText) {
                        ctx.setFillStyle('#222');
                        ctx.fillText(origText, nx, ny);
                    }
                    if (newText) {
                        ctx.setFillStyle(RED.fillStyle);
                        ctx.fillText(newText, nx + origW + gap, ny);
                    }
                    if (!useUpper) chordLowerEnd = nx + origW + gap + newW;
                    ctx.restore();
                });

                // theory notes go under the system as HTML so they wrap and select
                // like normal text
                measure.idx.forEach((rowIdx) => {
                    const row = rows[rowIdx];
                    if (!row.theory_note) return;
                    const sym = row.new_symbol + (row.new_bass ? '/' + row.new_bass : '');
                    // a run of adjacent chords in the same bar with the same symbol
                    // and the same reason says nothing new the first entry didn't
                    if (row.bar === lastBar && sym === prevEntrySym && row.theory_note === prevEntryNote) return;
                    const barPart = row.bar !== lastBar ? `<b>bar ${row.bar}</b> ` : '';
                    lastBar = row.bar;
                    prevEntrySym = sym;
                    prevEntryNote = row.theory_note;
                    noteEntries.push(`${barPart}<span class="sym">${escapeHtml(prettyChord(sym))}</span>: `
                        + escapeHtml(prettyNote(row.theory_note)));
                });

                globalMeasureIdx++;
            });

            notesP.innerHTML = noteEntries.join(' · ');
        }
    }

    const select = document.getElementById('score-pick');
    const redrawFromSelect = () => {
        if (!select || !select.options.length) return;
        const opt = select.options[select.selectedIndex];
        draw(opt.dataset.prog, opt.dataset.voicing);
    };
    if (select) select.addEventListener('change', redrawFromSelect);

    let resizeTimer = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(redrawFromSelect, 150);
    });

    redrawFromSelect();
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


def _score_option_label(spice_str: str, voicing_style: str, alignment: str,
                        source: str, show_source: bool) -> str:
    label = f"spice {spice_str} · {voicing_style}"
    if alignment != "raw":
        label += " · aligned"
    if show_source:
        label += f" · midi: {source}"
    return label


def _render_score_controls(explainer_data: dict) -> str:
    """One <option> per (progression key, voicing key) pair, in matrix order:

    spice ascending (like the matrix rows), then alignment/midi-source (like
    the matrix's per-cell tie-break), then voicing name ascending (like the
    matrix columns).
    """
    progressions = explainer_data.get("progressions") or {}
    voicings_map = explainer_data.get("voicings") or {}
    if not progressions:
        return ""

    def split_pk(pk: str) -> tuple[str, str, str]:
        source, spice_str, alignment = pk.rsplit("_", 2)
        return source, spice_str, alignment

    alignment_order = {"raw": 0, "aligned": 1}
    sources = sorted({split_pk(pk)[0] for pk in progressions})
    source_order = {s: i for i, s in enumerate(sources)}
    show_source = len(sources) > 1

    pks = sorted(progressions, key=lambda pk: (
        float(split_pk(pk)[1]),
        alignment_order.get(split_pk(pk)[2], 99),
        source_order.get(split_pk(pk)[0], 99),
    ))

    options: list[str] = []
    for pk in pks:
        source, spice_str, alignment = split_pk(pk)
        vks = sorted((vk for vk in voicings_map if vk.startswith(pk + "_")),
                    key=lambda vk: vk[len(pk) + 1:])
        for vk in vks:
            voicing_style = vk[len(pk) + 1:]
            label = _score_option_label(spice_str, voicing_style, alignment, source, show_source)
            options.append(
                f'<option data-prog="{html_lib.escape(pk)}" '
                f'data-voicing="{html_lib.escape(vk)}">{html_lib.escape(label)}</option>'
            )
    return "\n".join(options)


def _render_score_section(explainer_data: dict) -> str:
    options_html = _render_score_controls(explainer_data)
    parts: list[str] = []
    parts.append('<h2>Score</h2>')
    parts.append('<p class="meta">Black noteheads are the chord the chart or the ensemble '
                 'heard at that beat. Red noteheads are what bebop adds at this spice and '
                 'voicing: the pitches the MIDI plays, in the register it plays them. The bass '
                 'is written an octave above where it sounds, as double-bass parts are. The '
                 'chord symbol above each chord shows the change; the reasons are listed under '
                 'each line.</p>')
    parts.append('<div class="score-controls">')
    parts.append(f'<label>variant <select id="score-pick">{options_html}</select></label>')
    parts.append('<span class="score-legend">'
                 '<span class="swatch black"></span> in the original chord '
                 '<span class="swatch red"></span> added by bebop'
                 '</span>')
    parts.append('</div>')
    parts.append('<div id="score" class="score-wrap"></div>')
    return "\n".join(parts)


def _render_bass_variant(c: MatrixCell, report_dir: Path,
                         has_original: bool, default_comp_vol: float,
                         show_rhythm: bool, show_alignment: bool,
                         show_piano_bass: bool, show_midi_source: bool) -> str:
    """Render one (rhythm, bass[, alignment][, piano_bass][, midi_source]) sub-variant."""
    # cell_id includes midi_source so two cells with same other fields but different
    # midi sources don't collide
    cell_id = (f"src_{c.midi_source}_sp{int(round(c.spice * 100)):02d}_{c.voicing}_{c.rhythm}_"
               f"{c.bass}_{c.alignment}_pb{c.piano_bass}")
    # filename-safe characters only for HTML data-id attributes
    cell_id = cell_id.replace("/", "_").replace(" ", "_").replace("+", "p")

    label_parts = [f"spice {c.spice:.2f}", c.voicing]
    if show_rhythm:
        label_parts.append(c.rhythm)
    label_parts.append(f"{c.bass} bass")
    if show_alignment:
        label_parts.append(f"{c.alignment} timing")
    if show_piano_bass:
        label_parts.append("bass on piano" if c.piano_bass == "on" else "bass on upright")
    if show_midi_source:
        label_parts.append(f"midi: {c.midi_source}")
    label = " / ".join(label_parts)

    sub_parts = []
    if show_rhythm:
        sub_parts.append(c.rhythm)
    sub_parts.append(f"{c.bass} bass")
    if show_alignment:
        sub_parts.append(f"{c.alignment} timing")
    if show_piano_bass:
        sub_parts.append("bass on piano" if c.piano_bass == "on" else "bass on upright")
    if show_midi_source:
        sub_parts.append(f"midi: {c.midi_source}")
    sub_label = " · ".join(sub_parts) if sub_parts else f"{c.bass} bass"

    parts: list[str] = [f'<div class="bass-variant" data-id="{cell_id}" '
                        f'data-label="{html_lib.escape(label)}">']
    parts.append(f'<div class="bass-label">{html_lib.escape(sub_label)}</div>')

    if c.wav_path is not None and c.wav_path.exists():
        rel = _relpath(c.wav_path, report_dir)
        parts.append(
            f'<audio class="comp" data-id="{cell_id}" preload="metadata" src="{rel}"></audio>'
        )
        if has_original:
            parts.append(
                f'<button class="play-btn" data-id="{cell_id}">▶ Play with original</button>'
            )
            parts.append('<div class="vol-row">')
            parts.append('<label>comp</label>')
            parts.append(
                f'<input type="range" class="comp-vol" data-id="{cell_id}" '
                f'min="0" max="1" step="0.01" value="{default_comp_vol}">'
            )
            parts.append(f'<span class="vol-readout" data-id="{cell_id}">'
                         f'{int(default_comp_vol * 100)}%</span>')
            parts.append('</div>')
        parts.append('<div class="label">comp only (scrub)</div>')
        parts.append(f'<audio controls preload="none" src="{rel}"></audio>')

    rel_midi = _relpath(c.midi_path, report_dir)
    parts.append(f'<div class="label" style="margin-top: 0.3rem;">'
                 f'<a href="{rel_midi}">download .mid</a></div>')
    # chord-chart explainer button — shows reharm + voicing breakdown in a modal
    progression_id = f"{c.midi_source}_{c.spice:.2f}_{c.alignment}"
    voicing_id = f"{c.midi_source}_{c.spice:.2f}_{c.alignment}_{c.voicing}"
    parts.append(
        f'<button class="chart-btn" data-progression-id="{progression_id}" '
        f'data-voicing-id="{voicing_id}" data-label="{html_lib.escape(label)}">'
        f'📋 Chord chart + theory</button>'
    )
    parts.append('</div>')
    return "".join(parts)


def _render_matrix(cells: list[MatrixCell], report_dir: Path,
                   has_original: bool, default_comp_vol: float) -> str:
    spices = sorted({c.spice for c in cells})
    voicings = sorted({c.voicing for c in cells})
    rhythms = sorted({c.rhythm for c in cells})
    alignments = sorted({c.alignment for c in cells})
    piano_bass_styles = sorted({c.piano_bass for c in cells})
    midi_sources = sorted({c.midi_source for c in cells})
    show_rhythm = len(rhythms) > 1
    show_alignment = len(alignments) > 1
    show_piano_bass = len(piano_bass_styles) > 1
    show_midi_source = len(midi_sources) > 1
    groups: dict[tuple[float, str], list[MatrixCell]] = {}
    for c in cells:
        groups.setdefault((c.spice, c.voicing), []).append(c)
    bass_order = {"sustained": 0, "walking": 1, "none": 2}
    rhythm_order = {r: i for i, r in enumerate(rhythms)}
    alignment_order = {"raw": 0, "aligned": 1}
    pb_order = {"off": 0, "on": 1}
    midi_source_order = {s: i for i, s in enumerate(midi_sources)}
    for grp in groups.values():
        # within a cell: rhythm → midi_source → bass → alignment → piano_bass.
        # putting midi_source second makes adjacent rows differ only by midi_source
        # within the same rhythm — ideal for the "is the new MIDI better?" A/B
        grp.sort(key=lambda c: (rhythm_order.get(c.rhythm, 99),
                                midi_source_order.get(c.midi_source, 99),
                                bass_order.get(c.bass, 99),
                                alignment_order.get(c.alignment, 99),
                                pb_order.get(c.piano_bass, 99)))

    out: list[str] = []
    out.append('<div class="matrix" style="grid-template-columns: 6em '
               + " ".join(["1fr"] * len(voicings)) + ';">')

    out.append('<div class="matrix-header-corner"></div>')
    for v in voicings:
        out.append(f'<div class="matrix-cell matrix-header" style="background: #efefef; text-align: center;">'
                   f'<strong>{html_lib.escape(v)}</strong></div>')

    for sp in spices:
        out.append(f'<div class="matrix-cell" style="background: #efefef; '
                   f'display: flex; align-items: center; justify-content: center;">'
                   f'<strong>spice {sp:.2f}</strong></div>')
        for v in voicings:
            grp = groups.get((sp, v))
            if not grp:
                out.append('<div class="matrix-cell">—</div>')
                continue
            html_parts = ['<div class="matrix-cell">']
            for c in grp:
                html_parts.append(_render_bass_variant(c, report_dir, has_original,
                                                       default_comp_vol,
                                                       show_rhythm, show_alignment,
                                                       show_piano_bass, show_midi_source))
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


def _ensure_original_in_report_dir(original_audio_path: Path, report_dir: Path) -> Path:
    """Symlink the original audio next to the HTML so paths stay same-directory.

    Returns the path inside `report_dir`.
    """
    link_in_report = report_dir / original_audio_path.name
    try:
        if not link_in_report.exists():
            link_in_report.symlink_to(original_audio_path.resolve())
    except (OSError, NotImplementedError):
        if not link_in_report.exists():
            shutil.copy2(original_audio_path, link_in_report)
    return link_in_report


def _ensure_ducked_version(original_in_report: Path, hpf_hz: int = _DEFAULT_DUCK_HPF_HZ) -> Path | None:
    """Pre-render a high-passed version of the original via ffmpeg, beside the original.

    Idempotent: skips the work if the output already exists. Returns the ducked
    file path, or None if ffmpeg isn't available (toggle is then omitted from HTML).
    """
    if shutil.which("ffmpeg") is None:
        return None
    ducked = original_in_report.with_name(original_in_report.stem + f".ducked{hpf_hz}.wav")
    if ducked.exists():
        return ducked
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(original_in_report),
        "-af", f"highpass=f={hpf_hz}",
        "-c:a", "pcm_s16le",
        str(ducked),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # log once, return None so the toggle just doesn't show up
        print(f"  [report] ffmpeg ducked-version render failed: {proc.stderr.strip()}")
        return None
    return ducked


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
    duck_hpf_hz: int = _DEFAULT_DUCK_HPF_HZ,
    explainer_data: dict | None = None,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_dir = output_path.parent
    has_original = original_audio_path is not None and Path(original_audio_path).exists()
    has_score = bool(explainer_data and explainer_data.get("progressions"))

    parts: list[str] = []
    parts.append('<!DOCTYPE html>')
    parts.append(f'<html><head><meta charset="utf-8"><title>{html_lib.escape(title)}</title>')
    parts.append(f'<style>{_CSS}</style></head><body>')
    parts.append(f'<h1>{html_lib.escape(title)}</h1>')
    parts.append(f'<p class="meta">{bpm:.0f} BPM &nbsp;·&nbsp; '
                 f'{len(cells)} variants &nbsp;·&nbsp; '
                 f'{len(sources)} chord sources</p>')

    if has_original:
        original_in_report = _ensure_original_in_report_dir(Path(original_audio_path), report_dir)
        ducked_in_report = _ensure_ducked_version(original_in_report, hpf_hz=duck_hpf_hz)
        rel_orig = original_in_report.name
        rel_ducked = ducked_in_report.name if ducked_in_report else None

        parts.append('<div class="transport">')
        parts.append('<div class="transport-row">')
        parts.append(f'<audio id="original-audio" preload="metadata" src="{rel_orig}" controls></audio>')
        parts.append('</div>')
        parts.append('<div class="transport-row" style="margin-top: 0.5rem;">')
        parts.append('<label>original volume</label>')
        parts.append(f'<input type="range" id="original-vol" min="0" max="1" step="0.01" '
                     f'value="{default_original_vol}" style="flex: 1; max-width: 240px;">')
        parts.append(f'<span id="original-vol-readout">{int(default_original_vol * 100)}%</span>')
        if rel_ducked is not None:
            parts.append(f'<button id="duck-btn" class="duck-btn" '
                         f'data-full-src="{rel_orig}" data-ducked-src="{rel_ducked}">'
                         f'☐ duck song bass</button>')
            parts.append(f'<span class="duck-help">swap to a pre-rendered '
                         f'{duck_hpf_hz} Hz HPF version of the song to hear the comp\'s bass</span>')
        parts.append('</div>')
        parts.append('<div class="now-playing" id="now-playing">'
                     'Nothing playing — click any <strong>Play with original</strong> button below.</div>')
        parts.append('</div>')
    else:
        parts.append('<p class="meta"><em>(no original audio supplied — pass --mix-with to enable '
                     'in-browser overlay)</em></p>')

    if has_score:
        parts.append(_render_score_section(explainer_data))

    if cells:
        parts.append('<h2>Comp matrix</h2>')
        parts.append('<p class="meta">Each cell\'s <strong>Play with original</strong> button starts '
                     'the song and the comp together in sync. Slide the comp volume to find your '
                     'balance — the original\'s volume slider is up top, plus a <strong>duck song '
                     'bass</strong> toggle that swaps in a high-passed version of the song so the '
                     'comp\'s bass cuts through. Click a different cell\'s play button to A/B '
                     'between variants. The "comp only (scrub)" player below each is for hearing '
                     'the voicing in isolation.</p>')
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

    # ── chord-chart explainer modal + injected data ──
    parts.append('<div id="chord-modal" class="modal-backdrop">')
    parts.append('  <div class="modal-content">')
    parts.append('    <div class="modal-header">')
    parts.append('      <h3 id="modal-title">chord chart</h3>')
    parts.append('      <button class="modal-close" aria-label="close">×</button>')
    parts.append('    </div>')
    parts.append('    <div id="modal-body"></div>')
    parts.append('  </div>')
    parts.append('</div>')

    if explainer_data is not None:
        injected = json.dumps({"bpm": bpm, **explainer_data}, separators=(",", ":"))
    else:
        injected = json.dumps({"bpm": bpm, "progressions": {}, "voicings": {}})
    parts.append(f'<script>window._bebopExplainer = {injected};</script>')

    if has_score:
        # jsdelivr serves the real 4.2.2 build; cdnjs's files under that version
        # are the legacy 3.0.9 bundle, which lacks Dot.buildAndAttach
        parts.append('<script src="https://cdn.jsdelivr.net/npm/vexflow@4.2.2/build/cjs/'
                     'vexflow.js"></script>')

    parts.append(f'<script>{_JS}</script>')
    if has_score:
        parts.append(f'<script>{_SCORE_JS}</script>')
    parts.append('</body></html>')
    output_path.write_text("\n".join(parts))
    return output_path

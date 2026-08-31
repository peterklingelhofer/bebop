# bebop

A learning tool for music theory - pitch and chord analysis with jazz-theory-backed suggestions for increasing harmonic complexity. Takes a chord chart or audio, runs ensemble chord recognition (six sources, consensus vote), applies tiered reharmonization (7ths → ii-V's → tritone subs → Coltrane changes), and writes voiced MIDI, audio and an auditable HTML report of every decision and why it was made.

The reharmonization is not AI: every chord substitution and voicing comes from
deterministic music theory, and each decision is logged with the reason it was
made, so you can audit and adjust it. Machine learning is used in exactly one
place, the chord-recognition step, and only for audio input: of the optional
audio sources, basic-pitch (Spotify's audio-to-MIDI model) and autochord (a
transformer-based recognizer) are ML; the chart, MIDI, and librosa chromagram
paths are not. Hand it a chord chart and no ML runs at all.

Built as a Python engine, with a Rust core (via PyO3) for performance-critical
paths and a native Swift Audio Unit plugin for real-time use in Logic.

## Quick start

```bash
# the simplest case: a chord chart, mild reharm, MIDI out
bebop --chart charts/song.txt --spice 0.3 --out output/comp.mid
```

The full thing — six chord sources, full spice × voicing matrix, audio render,
HTML report with in-browser mixer:

```bash
bebop \
  --chart charts/song.txt \
  --midi 'audio/song.midi' \
  --audio 'audio/song.wav' \
  --basic-pitch 'audio/song.wav' \
  --autochord 'audio/song.wav' \
  --chordino 'audio/song.wav' \
  --bpm 94 \
  --spice-sweep '0.0,0.3,0.5,0.7,0.9' \
  --voicings rootless,evans,drop2,quartal \
  --suggest-chart charts/song.suggested.txt \
  --html-report output/song.html \
  --out output/matrix.mid \
  --render-audio \
  --mix-with 'audio/song.wav'
```

That produces 40 MIDI files (5 spice × 4 voicing × 2 bass styles), 40 rendered
WAVs, an auto-suggested chord chart, and an HTML page where you can A/B all 40
variants against the original at your own volume balance. Each (spice, voicing)
cell in the matrix renders **both** sustained-root and quarter-note walking
bass so you can hear the bass-line difference side by side.

## Setup

```bash
# main venv (arm64 Python 3.12)
uv sync

# extras venv for basic-pitch and autochord (Python 3.11 + TF<2.16)
uv venv --python 3.11 .venv-extras
VIRTUAL_ENV=.venv-extras uv pip install 'numpy<2' 'cython<3' setuptools wheel
VIRTUAL_ENV=.venv-extras uv pip install --no-build-isolation vamp
VIRTUAL_ENV=.venv-extras uv pip install basic-pitch autochord chord-extractor \
    'tensorflow<2.16' 'tf-keras<2.16' 'setuptools<81' --no-build-isolation

# native binaries (arm64 brew at /opt/homebrew)
brew install cmake boost ffmpeg fluid-synth

# build the NNLS-Chroma vamp plugin (autochord + chordino need it)
./vendor/build_nnls_chroma.sh

# a SoundFont for fluidsynth (auto-discovered from ~/.bebop/soundfonts/*.sf2)
mkdir -p ~/.bebop/soundfonts
curl -L -o ~/.bebop/soundfonts/general.sf2 \
  "https://github.com/musescore/MuseScore/raw/master/share/sound/MS%20Basic.sf3"
```

## Common recipes

### One quick comp with a single voicing

```bash
bebop --chart charts/song.txt --bpm 94 --spice 0.4 \
      --voicings evans --rhythm charleston \
      --out output/quick.mid --render-audio
```

### Hear options across the spice spectrum

```bash
bebop --chart charts/song.txt --bpm 94 \
      --spice-sweep '0.0,0.3,0.5,0.7,0.9' \
      --voicings rootless \
      --out output/sweep.mid --render-audio
```

### Auto-derive a chord chart from audio

```bash
bebop --autochord 'audio/song.wav' --chordino 'audio/song.wav' \
      --basic-pitch 'audio/song.wav' --bpm 94 --spice 0 \
      --suggest-chart charts/song.draft.txt \
      --out /tmp/_unused.mid
# then edit charts/song.draft.txt by ear, then re-run with --chart
```

### Sweep multiple rhythm patterns alongside voicings

```bash
bebop --chart charts/song.txt --bpm 94 \
      --spice-sweep '0.3,0.5' \
      --voicings rootless,evans \
      --rhythms charleston,anticipations \
      --out output/sweep.mid --render-audio
# 2 spice × 2 voicings × 2 rhythms × 2 bass styles = 16 variants in the matrix
```

### A/B all four Charleston shifts at once

```bash
bebop --chart charts/song.txt --bpm 94 --spice 0 \
      --voicings rootless,evans \
      --rhythms charleston,charleston_+1,charleston_+2,charleston_+3 \
      --bass-mode piano --follow-dynamics \
      --html-report output/charleston_shifts.html \
      --out output/cs.mid --render-audio --mix-with audio/song.wav
# Same chord progression, same voicing — just rotate the Charleston by 1, 2, 3 beats
```

### Make the comp swell with the song

```bash
bebop --chart charts/song.txt \
      --audio 'audio/song.wav' \
      --bpm 94 --spice 0.4 \
      --voicings rootless --follow-dynamics \
      --out output/dynamic.mid --render-audio \
      --mix-with 'audio/song.wav'
# velocity scales 0.55× (quiet) to 1.20× (loud) following the audio's RMS envelope
```

### Maximum-comparison A/B grid: every axis side-by-side

```bash
bebop --chart charts/butterfly_boy.txt \
      --midi 'audio/butterfly boy acoustic guitar MIDI.midi' \
      --audio 'audio/butterfly boy 20260330 no acoustic guitar 94bpm.wav' \
      --basic-pitch 'audio/butterfly boy 20260330 no acoustic guitar 94bpm.wav' \
      --autochord 'audio/butterfly boy 20260330 no acoustic guitar 94bpm.wav' \
      --chordino 'audio/butterfly boy 20260330 no acoustic guitar 94bpm.wav' \
      --bpm 94 \
      --spice-sweep '0.0,0.01' \
      --voicings rootless,evans,drop2,quartal \
      --rhythms charleston,two_and_four,sustained,anticipations \
      --walking-bass --align-both --bass-mode both --follow-dynamics \
      --html-report output/butterfly_boy.html \
      --out output/matrix.mid --render-audio \
      --mix-with 'audio/butterfly boy 20260330 no acoustic guitar 94bpm.wav'
# 2 spice × 4 voicing × 4 rhythm × 2 bass × 2 alignment × 2 bass-mode = 256 variants
# every cell A/B's all six axes for direct comparison
```

### A/B with the original in the browser

```bash
bebop --chart charts/song.txt \
      --autochord 'audio/song.wav' --chordino 'audio/song.wav' \
      --basic-pitch 'audio/song.wav' \
      --bpm 94 --spice-sweep '0.3,0.6,0.9' \
      --voicings rootless,evans \
      --html-report output/song.html \
      --out output/m.mid --render-audio \
      --mix-with 'audio/song.wav'
# open output/song.html in a browser
```

## Viewing the HTML report

Just **double-click `output/song.html`**. Everything works from `file://` —
per-cell play buttons, comp volume sliders, the **duck song bass** toggle, the
disagreement table, and the chord chart diff.

The report writer symlinks the original audio next to the HTML at render time
and pre-renders a 250 Hz high-passed version (via ffmpeg) right beside it.
Clicking **☐ duck song bass** in the transport bar swaps the audio src to the
HPF version while preserving playback position — so you can flip on the fly to
hear the comp's bass cut through the song. No Web Audio, no local server, no
CORS issues.

## Realtime modes — two options

There are two ways to run bebop in real time:

1. **Native AU plugin** ([bebop-au/](bebop-au/)) — a `.component` you drop
   into Logic. Plugin loads on a bus, recognizes chords from the audio
   passing through it, emits MIDI to whatever destination you route the
   AU's MIDI output to. No virtual audio cables, no browser dashboard,
   no separate Python process — it's just an Audio Unit. **This is the
   recommended path for Logic users.** See [bebop-au/README.md](bebop-au/README.md).
2. **`bebop live` CLI** (described below) — a Python process that listens
   to a virtual audio cable (BlackHole) and streams MIDI back via the
   IAC Driver bus. Predates the AU plugin and is still maintained as the
   alternative for users without a host that loads AUv2 plugins, or who
   want to control the comp through a browser dashboard rather than the
   AU's parameter UI.

Both share the underlying chord recognition + voicing + rhythm code in the
[`bebop/` Python package](bebop/).

## Live mode (`bebop live`)

`bebop live` is a real-time accompanist: it listens to a virtual audio cable,
recognizes chords as they play in your DAW, and **streams MIDI back into Logic
in real time** through the IAC Driver bus. A browser dashboard exposes spice
/ voicing / rhythm / bpm / piano-bass knobs that take effect at the next
chord change. A `.mid` file is written to disk in parallel (back-dated by the
recognition lag) so you also get a clean offline-aligned take.

**Inherent latency:** the recognizer needs ~1 s of audio to identify a chord,
so the live comp lands ~1 beat behind each chord change. It reacts like a
slightly-late accompanist, not a click-locked piano. Acceptable in most
contexts, but if it bothers you, the disk MIDI is back-dated and lines up
when dragged in offline.

### One-time setup (macOS)

```bash
# free virtual audio cable for routing Logic's audio out → bebop's audio in
brew install blackhole-2ch
```

In **Audio MIDI Setup** (`/Applications/Utilities/Audio MIDI Setup.app`):
1. **Window → Show MIDI Studio**
2. Double-click **IAC Driver** → check *Device is online* → keep "Bus 1"
3. **Window → Show Audio Devices** → `+` → *Create Multi-Output Device*
4. Check both your normal output (e.g. Built-in Output / AirPods) and **BlackHole 2ch**

In **Logic**:
1. **Logic Pro → Settings → Audio → Output Device**: the Multi-Output Device
2. New software-instrument track → **input**: *IAC Driver Bus 1* → load a
   piano patch. (Optional: a second track for *channel 2* with a bass patch
   — bebop sends piano on ch 1, bass on ch 2.)
3. Mute drum tracks before pressing play (chroma recognition gets confused
   by transients).

### Run

```bash
bebop live                              # auto-picks BlackHole + IAC
bebop live --list-devices               # audio input ports
bebop live --list-midi-outs             # MIDI output ports
bebop live --no-midi                    # disk-only mode (no live IAC)
bebop live --bpm 94 --spice 0.5 --voicing evans --rhythm charleston \
           --midi-out "IAC Driver Bus 1"
```

The dashboard at `http://127.0.0.1:8765/` (auto-opens) shows the audio level
meter (so you know BlackHole is feeding signal), the currently-heard chord,
recent history, all knobs, and a panic button. Knob changes apply at the
next chord change. Press **Ctrl-C** to stop — the .mid file you wanted is
already at `output/live_<timestamp>.mid`.

## Butterfly boy reference command

Preferred config for the bundled `audio/butterfly boy ...` files — piano
carries the bass (no upright; cleanest sound on this song), sustained bass
only (no walking), and the MIDI / WAV are time-locked to the first downbeat
so no alignment correction is needed:

```bash
PATH="/opt/homebrew/bin:$PATH" ~/.local/bin/uv run bebop \
  --chart charts/butterfly_boy.txt \
  --midi 'audio/butterfly boy acoustic guitar midi 94 bpm locked to 1st downbeat.mid' \
  --audio 'audio/butterfly boy no acoustic guitar 20260502.wav' \
  --basic-pitch 'audio/butterfly boy no acoustic guitar 20260502.wav' \
  --autochord 'audio/butterfly boy no acoustic guitar 20260502.wav' \
  --chordino 'audio/butterfly boy no acoustic guitar 20260502.wav' \
  --bpm 94 \
  --spice-sweep '0.0,0.01' \
  --voicings rootless,evans,drop2,quartal \
  --rhythms charleston,two_and_four,sustained,anticipations \
  --bass-mode piano \
  --follow-dynamics \
  --duck-hz 200 \
  --suggest-chart charts/butterfly_boy.suggested.txt \
  --html-report output/butterfly_boy.html \
  --out output/matrix.mid \
  --render-audio \
  --mix-with 'audio/butterfly boy no acoustic guitar 20260502.wav'
```

→ 2 spice × 4 voicing × 4 rhythm × 1 bass × 1 bass-mode = **32 variants**, ~4 min render
(plus first-time chord-recognizer cost on the new audio: ~1 min).

Other flags you can mix in:
- `--walking-bass` — also render walking-bass quarter-note lines (doubles the count)
- `--bass-mode both` — render both upright-only AND piano-bass variants for A/B
- `--bass-mode upright` — keep the bassline on the upright (default if `--bass-mode` is omitted)
- `--first-onset-align` — auto-detect first-onset offset between MIDI and WAV. Off by default; only useful if your MIDI and WAV don't share the same downbeat
- `--align-both` — render every variant twice, once raw and once with first-onset alignment, for A/B
- Wider `--spice-sweep` — try `'0.0,0.3,0.5,0.7'` for a fuller spice spread

## Chord chart format

Real Book style with a few headers. `#` starts a whole-line comment; `#` inside
a header value (like `key: D` followed by a comment) is also stripped. `#` and
`b` inside chord symbols are accidentals (e.g. `F#m7`, `Bb13`).

```
bpm: 94
time: 4/4
key: C

| F      | C      | C      | C      |
| C      | G      | C7     | E7     |

key: A          # mid-song key change applies from the next bar
| A      | E/G#  | F#m    | C#m    |
| %      |              # repeat previous bar
| -      |              # rest / skip bar
```

Multiple chords in one bar split that bar evenly across them: `| Dm7 G7 |`
is two beats each in 4/4.

## Flag reference

### Input sources (any combination — at least one required)

| Flag | What it does |
|---|---|
| `--chart PATH` | Real Book-style chord chart (`.txt`) |
| `--midi PATH` | Polyphonic MIDI (e.g. Melodyne export). Template-matches chord per beat-window from the symbolic note data |
| `--audio PATH` | WAV file. librosa CQT chromagram + harmonic-percussive separation + bass-aware template matcher. No ML, fast, zero install |
| `--basic-pitch PATH` | WAV file. Spotify deep model audio→MIDI, then our chord matcher. Best polyphonic transcription. Requires `.venv-extras` |
| `--autochord PATH` | WAV file. BTC bidirectional transformer + NNLS-Chroma vamp plugin. Best end-to-end chord recognition. Requires `.venv-extras` + arm64 NNLS-Chroma dylib |
| `--chordino PATH` | WAV file. NNLS-Chroma direct chord recognizer (Mauch & Dixon 2010). Requires same setup as `--autochord` |

### Reharmonization

| Flag | Default | What it does |
|---|---|---|
| `--spice FLOAT` | `0.4` | Reharm adventurousness, 0.0 (mild) to 1.0 (wild). Tiered: 0.0–0.2 adds 7ths/9ths, 0.2–0.4 inserts ii-V's, 0.4–0.6 tritone subs + altered dominants, 0.6–0.8 diminished passing + modal interchange, 0.8–1.0 Coltrane changes + side-slipping |
| `--spice-sweep "0,0.3,…"` | (off) | Comma-separated spice list. Each value emits its own MIDI/WAV; multiplies with `--voicings` |
| `--seed INT` | `0` | Random seed for reproducible substitutions |

### Voicing & rhythm

| Flag | Default | What it does |
|---|---|---|
| `--voicings rootless,evans,drop2,quartal` | all four | Comma-separated voicing styles. Each emits its own MIDI/WAV. `rootless` = bass + 3-7-9 shell, `evans` = full Bill Evans LH+RH, `drop2` = 4-note close with 2nd-from-top dropped an octave, `quartal` = stacked 4ths (McCoy Tyner) |
| `--rhythm STYLE` | `charleston` | Single rhythm template. See full list below |
| `--rhythms "a,b,c"` | (uses `--rhythm`) | Comma-separated rhythm sweep. Each rhythm × voicing × spice × bass becomes its own matrix variant |

#### Available rhythm templates

| name | hits within a 4-beat bar | character |
|---|---|---|
| `charleston` | 1 + "and of 3" | the classic; downbeat plus syncopated push |
| `charleston_+1` | 2 + "and of 4" | shifted +1 beat; landed on 2 with a strong push into next bar |
| `charleston_+2` | "and of 1" + 3 | shifted +2 beats; answers the original Charleston |
| `charleston_+3` | "and of 2" + 4 | shifted +3 beats; late and pushy |
| `two_and_four` | 2 + 4 | Freddie Green chunk minus the downbeats |
| `freddie_green` | 1 + 2 + 3 + 4 (2 and 4 emphasized) | full Count Basie back-beat-emphasized quarter chunk |
| `sustained` | held for the whole bar | one chord per bar; ballad pad |
| `anticipations` | 1 + 3 + "and of 4" | forward-leaning, the "and of 4" pushes into the next bar |
| `reverse_charleston` | "and of 1" + 3 | call-and-response answer to Charleston |
| `ahmad_jamal` | 1 + 2 + "and of 3" | front-loaded then a syncopated push |
| `bossa` | 1 + "and of 2" + 4 | latin-flavored 3-2 clave shape |
| `kenny_barron` | 1 + "and of 2" + 4 (dotted-quarter pulses) | 3-against-4 polyrhythmic feel |
| `--no-bass` | (off) | Skip the bass track entirely. |
| `--walking-bass` | (off) | Also render quarter-note walking bass alongside the default sustained-root bass, so each cell has both for A/B. Walking bass plays beat 1 root, beat 2 fifth, beat 3 third, last beat chromatic approach to the next chord's root. Doubles the variant count |
| `--bass-mode {upright,piano,both}` | `upright` | Who plays the bassline. `upright` (default): bass instrument plays it, piano just does chords. `piano`: piano LH plays it, upright is silent (solo-piano feel). `both`: render both variants per cell for A/B (doubles variant count) |
| `--follow-dynamics` | (off) | Scale comp note velocities by the audio's RMS loudness envelope so the comp swells with the song instead of playing flat. Computed from `--mix-with` (or any audio source). Range: 0.55× quiet → 1.20× loud, smoothed across 2 beats so phrases breathe rather than each note jumping |
| `--bpm FLOAT` | from input | Override BPM. Required for `--audio`/`--basic-pitch`/`--autochord`/`--chordino` if no chart provides a bpm |

### MIDI/audio temporal alignment

| Flag | Default | What it does |
|---|---|---|
| `--midi-offset BEATS` | (off) | Manual MIDI shift in beats. Positive = MIDI later. Overrides `--first-onset-align` |
| `--first-onset-align` | off | Auto-align MIDI to audio by detecting first onset in each. Only useful when MIDI was transcribed directly from the WAV (so they share t=0). For separated-stem workflows, leave off |
| `--align-both` | off | Render every variant **twice** — once with raw MIDI timing, once with first-onset alignment applied — so you can A/B them in the matrix. Implies `--first-onset-align` if no `--midi-offset` is given. Doubles the variant count |

### Output

| Flag | What it does |
|---|---|
| `--out PATH` | (required) Output MIDI path. With `--spice-sweep` or multiple `--voicings`, the filename gets suffixed: `out.spice30.evans.sustained.mid`, `out.spice30.evans.walking.mid` etc. |
| `--render-audio` | Also render each MIDI to a `.wav` via fluidsynth + SoundFont |
| `--soundfont PATH` | Override SoundFont. Default: `$BEBOP_SOUNDFONT` env, then standard system dirs, then `~/.bebop/soundfonts/*.sf2` |
| `--mix-with PATH` | A WAV (usually the original song) to mix the comp against. Without `--html-report`, writes a `.mix.wav` per variant. With `--html-report`, the mix happens in-browser at user-controlled volumes (no `.mix.wav` files) |
| `--comp-gain-db FLOAT` | Comp track gain when writing `.mix.wav`. Default `-2.0` |
| `--song-gain-db FLOAT` | Original track gain when writing `.mix.wav`. Default `-1.0` |

### Reporting

| Flag | What it does |
|---|---|
| `--print` | Print every input source's chord progression and the ensemble consensus to stdout, plus a disagreement report sorted by noisiest bars first |
| `--suggest-chart PATH` | Write the ensemble consensus as a Real Book-style chord chart you can audit, diff, and edit. Then re-run with that file as `--chart` |
| `--html-report PATH` | Write a single-page HTML audit. Embeds the original audio, every variant as `<audio>` players, a per-cell mixer with comp volume sliders, a **duck song bass** toggle (swaps in a pre-rendered HPF version of the song so the comp's bass cuts through; cutoff via `--duck-hz`), a **📋 Chord chart + theory** popup per cell (shows the bar-by-bar progression with theory annotations like "tritone sub", "added maj7", "secondary dominant" plus voicing breakdowns showing scale degrees), the disagreement table, and a chord-chart diff (hand vs. ensemble). The currently-playing chord is highlighted in the popup during playback. Works from `file://` |
| `--duck-hz N` | Cutoff Hz for the **duck song bass** pre-rendered HPF version. Default `200`. Lower (e.g. `120`) preserves more low-mids of the song; higher (e.g. `400`) is more aggressive and exposes the comp's bass more |

## Architecture in one line each

- **`bebop/io/`** — chord input adapters (chart, midi, audio, basic-pitch, autochord, chordino), the ensemble voter, the alignment and disagreement utilities, and the on-disk cache for slow ML sources
- **`bebop/reharm/`** — tiered substitution engine with the spice knob; key-aware modal interchange
- **`bebop/voicing/`** — chord symbols → MIDI pitches via music21, with four voicing styles and voice leading
- **`bebop/rhythm/`** — rhythmic comping templates (Charleston, anticipations, etc.)
- **`bebop/render/`** — pretty_midi MIDI writer, fluidsynth audio renderer, ffmpeg mixer, HTML report generator, and the chord-chart-explainer (per-variant theory annotations and voicing breakdowns shown in the report's modal)
- **`vendor/`** — the Vamp Plugin SDK and NNLS-Chroma sources, with a build script that produces an arm64 dylib for `~/Library/Audio/Plug-Ins/Vamp/`

## Caching

The slow ML chord-recognition sources (`--audio`, `--basic-pitch`, `--autochord`,
`--chordino`) are each cached on disk under `.bebop_cache/<sha256-prefix>/`,
keyed by the audio file's content hash and the BPM. First run takes 30–60s;
subsequent runs against the same file return instantly.

To invalidate: `rm -rf .bebop_cache/`.

## License

bebop is released under the [MIT License](LICENSE).

It optionally uses third-party chord-recognition tools that are installed
separately and keep their own licenses: the Vamp Plugin SDK (BSD-style) and
NNLS-Chroma (GPL), both cloned and built locally by
`vendor/build_nnls_chroma.sh` (never bundled in this repo), and the
pip-installed `basic-pitch` and `autochord` models. NNLS-Chroma is invoked at
runtime as a separate Vamp plugin rather than linked, so bebop's own MIT code
is unaffected. Skip those optional sources (use `--chart` / `--midi`) and none
of them are involved.

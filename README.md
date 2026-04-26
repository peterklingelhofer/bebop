# bebop

Add advanced jazz comping to existing songs. Takes a chord chart and/or audio in,
gets six different chord-recognition opinions, votes a consensus, applies tiered
jazz reharmonization (7ths → ii-V's → tritone subs → Coltrane changes), voices
the result in your choice of jazz piano styles, and writes MIDI plus rendered
audio plus a single-page HTML report you can open and audit in a browser.

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

That produces 20 MIDI files, 20 rendered WAVs, an auto-suggested chord chart,
and an HTML page where you can A/B all 20 variants against the original at
your own volume balance.

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
| `--rhythm STYLE` | `charleston` | Rhythm template. One of `charleston` (beat 1 + "and" of 3), `two_and_four` (Freddie Green chunk), `sustained` (whole-bar pad), `anticipations` (push on "and" of 4) |
| `--no-bass` | (off) | Skip the bass track entirely |
| `--bpm FLOAT` | from input | Override BPM. Required for `--audio`/`--basic-pitch`/`--autochord`/`--chordino` if no chart provides a bpm |

### MIDI/audio temporal alignment

| Flag | Default | What it does |
|---|---|---|
| `--midi-offset BEATS` | (off) | Manual MIDI shift in beats. Positive = MIDI later. Overrides `--first-onset-align` |
| `--first-onset-align` | off | Auto-align MIDI to audio by detecting first onset in each. Only useful when MIDI was transcribed directly from the WAV (so they share t=0). For separated-stem workflows, leave off |

### Output

| Flag | What it does |
|---|---|
| `--out PATH` | (required) Output MIDI path. With `--spice-sweep` or multiple `--voicings`, the filename gets suffixed: `out.spice30.evans.mid` etc. |
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
| `--html-report PATH` | Write a single-page HTML audit. Embeds the original audio, every variant as `<audio>` players, a per-cell mixer with volume sliders, the disagreement table color-coded by severity, and a chord-chart diff (hand vs. ensemble) |

## Architecture in one line each

- **`bebop/io/`** — chord input adapters (chart, midi, audio, basic-pitch, autochord, chordino), the ensemble voter, the alignment and disagreement utilities, and the on-disk cache for slow ML sources
- **`bebop/reharm/`** — tiered substitution engine with the spice knob; key-aware modal interchange
- **`bebop/voicing/`** — chord symbols → MIDI pitches via music21, with four voicing styles and voice leading
- **`bebop/rhythm/`** — rhythmic comping templates (Charleston, anticipations, etc.)
- **`bebop/render/`** — pretty_midi MIDI writer, fluidsynth audio renderer, ffmpeg mixer, HTML report generator
- **`vendor/`** — the Vamp Plugin SDK and NNLS-Chroma sources, with a build script that produces an arm64 dylib for `~/Library/Audio/Plug-Ins/Vamp/`

## Caching

The slow ML chord-recognition sources (`--audio`, `--basic-pitch`, `--autochord`,
`--chordino`) are each cached on disk under `.bebop_cache/<sha256-prefix>/`,
keyed by the audio file's content hash and the BPM. First run takes 30–60s;
subsequent runs against the same file return instantly.

To invalidate: `rm -rf .bebop_cache/`.

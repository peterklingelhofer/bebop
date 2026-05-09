//! Differential test: bebop_cli (Rust) and the Python CLI must produce
//! identical comp MIDI for the same WAV input.
//!
//! Both paths call the same Python functions internally (parse_audio →
//! reharmonize → write_midi) — the only difference is the orchestrator
//! (Rust vs the existing `bebop` CLI). So output should be byte-stable
//! once we canonicalize note ordering.
//!
//! Test fixtures come from the existing `tests/eval/` synth harness, which
//! renders a small library of jazz progressions to .wav files. We run each
//! through both pipelines and compare the resulting note lists.

use std::path::{Path, PathBuf};
use std::process::Command;

use bebop_rs::{midi, process_wav_offline};

/// Repository root, computed from this file's location at compile time.
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf()
}

/// One bench fixture: a WAV path + the bpm / key knobs we feed both pipelines.
struct Fixture {
    name: &'static str,
    bpm: f64,
}

const FIXTURES: &[Fixture] = &[
    Fixture { name: "ii_V_I_C", bpm: 100.0 },
    Fixture { name: "rhythm_Bb", bpm: 100.0 },
    Fixture { name: "blues_F", bpm: 100.0 },
    Fixture { name: "ii_V_i_Dm", bpm: 100.0 },
    Fixture { name: "dorian_vamp_D", bpm: 100.0 },
];

/// Make sure the eval fixtures' WAVs exist; render them via the Python
/// harness if they don't yet.
fn ensure_fixtures(root: &Path) {
    let eval_dir = root.join("output/eval");
    let any_missing = FIXTURES
        .iter()
        .any(|f| !eval_dir.join(format!("{}.wav", f.name)).exists());
    if !any_missing {
        return;
    }
    eprintln!("[differential] rendering missing eval fixtures ...");
    let status = Command::new("uv")
        .args(["run", "python", "-c", "from tests.eval.synth import fixtures, synthesize; [synthesize(seq, name) for name, seq in fixtures().items()]"])
        .current_dir(root)
        .status()
        .expect("uv must be on PATH for fixture rendering");
    assert!(status.success(), "fixture rendering failed");
}

/// Render a comp MIDI for `fixture` via the Python CLI.
fn render_python(root: &Path, fixture: &Fixture, out: &Path) {
    let wav = root.join("output/eval").join(format!("{}.wav", fixture.name));
    // We bypass the bebop CLI and call the same library functions directly,
    // so any drift between bebop's CLI flag-handling and our Rust path
    // doesn't muddy the diff. The library calls match what bebop_rs does.
    let py_script = format!(
        "from pathlib import Path
from bebop.io.audio_in import parse_audio
from bebop.reharm import reharmonize
from bebop.render import write_midi
seq = parse_audio('{wav}', bpm={bpm}, windows_per_bar=2, beats_per_bar=4)
seq = reharmonize(seq, spice=0.0, seed=0)
write_midi(seq, '{out}', rhythm='charleston', voicing='rootless',
           include_bass=True, walking_bass=False, piano_bass=False)
",
        wav = wav.display(),
        bpm = fixture.bpm,
        out = out.display(),
    );
    let status = Command::new("uv")
        .args(["run", "python", "-c", &py_script])
        .current_dir(root)
        .status()
        .expect("uv must be on PATH");
    assert!(status.success(), "python render of {} failed", fixture.name);
}

#[test]
fn rust_and_python_produce_identical_comp_midi() {
    let root = repo_root();
    ensure_fixtures(&root);

    let tmp = tempfile::tempdir().unwrap();
    let mut fail_count = 0;
    let mut pass_count = 0;
    let mut messages: Vec<String> = Vec::new();

    for fixture in FIXTURES {
        let wav = root
            .join("output/eval")
            .join(format!("{}.wav", fixture.name));
        let py_out = tmp.path().join(format!("{}_py.mid", fixture.name));
        let rs_out = tmp.path().join(format!("{}_rs.mid", fixture.name));

        render_python(&root, fixture, &py_out);
        process_wav_offline(&wav, &rs_out, fixture.bpm, 0.0, "rootless", "charleston")
            .expect("rust render");

        // ignore track names — `write_midi` embeds the output filename's
        // stem ("Bass · py" vs "Bass · rs"), which differs between the two
        // runs purely because we use different temp paths. The musical
        // content (channel, pitch, velocity, timing) is what we care about.
        let strip_name = |notes: Vec<midi::Note>| {
            notes
                .into_iter()
                .map(|n| midi::Note { track_name: String::new(), ..n })
                .collect::<Vec<_>>()
        };
        let py_notes = strip_name(midi::load_notes(&py_out).expect("read py midi"));
        let rs_notes = strip_name(midi::load_notes(&rs_out).expect("read rs midi"));

        if py_notes == rs_notes {
            messages.push(format!(
                "  ✓ {:<14}  {} notes match",
                fixture.name,
                py_notes.len()
            ));
            pass_count += 1;
        } else {
            messages.push(format!(
                "  ✗ {:<14}  py={} rs={} mismatch",
                fixture.name,
                py_notes.len(),
                rs_notes.len()
            ));
            fail_count += 1;
        }
    }

    for msg in &messages {
        eprintln!("{msg}");
    }
    eprintln!(
        "[differential] {} pass, {} fail of {} fixtures",
        pass_count,
        fail_count,
        FIXTURES.len()
    );
    assert_eq!(fail_count, 0, "{} fixture(s) diverged", fail_count);
}

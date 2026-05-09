//! PyO3 bindings into the existing `bebop` Python package.
//!
//! In Phase A we re-use bebop's offline pipeline as-is — `parse_audio` for
//! chord recognition, `reharmonize` for substitution, `write_midi` for
//! rendering. This proves the embedded-Python plumbing works end-to-end
//! before we tackle the AU framework.

use std::path::Path;
use std::sync::Once;

use anyhow::{Context, Result};
use pyo3::prelude::*;

/// Path to the project venv's site-packages, baked in at compile time so the
/// embedded interpreter can find the `bebop` package without env-var
/// gymnastics. PYO3_PYTHON already points at the venv's python; PyO3 uses
/// that for build-time linking but the embedded interpreter at runtime
/// inherits libpython's compiled-in sys.path, NOT the venv's site-packages.
/// We patch sys.path on first use.
const VENV_SITE_PACKAGES: &str =
    "/Users/peterklingelhofer/Dev/bebop/.venv/lib/python3.12/site-packages";

static INIT_PATH: Once = Once::new();

/// Run once on first PyO3 call. Adds the project venv's site-packages to
/// sys.path so `import bebop` resolves. Public so tests that hold the GIL
/// directly (instead of going through our wrappers) can also call it.
pub fn ensure_venv_on_path(py: Python<'_>) -> PyResult<()> {
    let mut err: Option<PyErr> = None;
    INIT_PATH.call_once(|| {
        if let Err(e) = (|| -> PyResult<()> {
            // site.addsitedir does two things vs. a raw sys.path.insert:
            //   1. Adds the directory itself to sys.path (so plain packages
            //      like `bebop/` resolve)
            //   2. PROCESSES any .pth files in the directory, expanding
            //      them into sys.path. bebop is installed in editable
            //      mode (pip install -e), which ships only a .pth pointer
            //      to the project root; without processing the .pth,
            //      `import bebop` fails even though the directory is on
            //      sys.path.
            py.import_bound("site")?
                .getattr("addsitedir")?
                .call1((VENV_SITE_PACKAGES,))?;
            Ok(())
        })() {
            err = Some(e);
        }
    });
    if let Some(e) = err {
        return Err(e);
    }
    Ok(())
}

/// One scheduled note, mirroring `bebop.live.midi_recorder.MidiHit`.
/// Used for the live path; the offline path writes directly to a .mid file.
#[derive(Debug, Clone)]
pub struct MidiHit {
    pub pitch: u8,
    pub velocity: u8,
    pub start_seconds: f64,
    pub end_seconds: f64,
    pub track: String,
}

/// Run a WAV file through bebop's offline pipeline and write a comp .mid
/// next to it (or to `out_path` if given). Returns the path written.
///
/// `bpm` is required because chroma-only recognition can't infer tempo.
/// `spice` ∈ \[0.0, 1.0\] controls reharm intensity (0 = no substitutions).
pub fn process_wav_offline(
    wav_path: &Path,
    out_path: &Path,
    bpm: f64,
    spice: f64,
    voicing: &str,
    rhythm: &str,
) -> Result<()> {
    Python::with_gil(|py| -> Result<()> {
        ensure_venv_on_path(py)?;
        // Import the bebop package; surface a clear error if it's missing
        // (e.g. PYO3_PYTHON points at a venv that doesn't have bebop installed).
        let audio_in = py
            .import_bound("bebop.io.audio_in")
            .context("failed to import bebop.io.audio_in — is bebop installed in PYO3_PYTHON's venv?")?;
        let reharm = py.import_bound("bebop.reharm")?;
        let render = py.import_bound("bebop.render")?;

        // parse_audio(wav_path, bpm=..., windows_per_bar=2, beats_per_bar=4)
        let parse_kwargs = pyo3::types::PyDict::new_bound(py);
        parse_kwargs.set_item("bpm", bpm)?;
        parse_kwargs.set_item("windows_per_bar", 2)?;
        parse_kwargs.set_item("beats_per_bar", 4)?;
        let seq = audio_in
            .getattr("parse_audio")?
            .call((wav_path.to_str().unwrap(),), Some(&parse_kwargs))?;

        // reharmonize(seq, spice=..., seed=0) — pass seed=0 so output is
        // deterministic across runs (matches the Python CLI's default).
        let reharm_kwargs = pyo3::types::PyDict::new_bound(py);
        reharm_kwargs.set_item("spice", spice)?;
        reharm_kwargs.set_item("seed", 0i64)?;
        let reharmed = reharm
            .getattr("reharmonize")?
            .call((seq,), Some(&reharm_kwargs))?;

        // write_midi(seq, out_path, rhythm=..., voicing=..., include_bass=True,
        //            walking_bass=False, piano_bass=False)
        let mid_kwargs = pyo3::types::PyDict::new_bound(py);
        mid_kwargs.set_item("rhythm", rhythm)?;
        mid_kwargs.set_item("voicing", voicing)?;
        mid_kwargs.set_item("include_bass", true)?;
        mid_kwargs.set_item("walking_bass", false)?;
        mid_kwargs.set_item("piano_bass", false)?;
        render
            .getattr("write_midi")?
            .call(
                (reharmed, out_path.to_str().unwrap()),
                Some(&mid_kwargs),
            )?;

        Ok(())
    })
}

/// Sanity check: import bebop and return its declared version.
/// Used by the Phase-A "PyO3 alive" test.
pub fn bebop_version() -> Result<String> {
    Python::with_gil(|py| -> Result<String> {
        ensure_venv_on_path(py)?;
        // verify bebop is importable (we don't use the module, but the
        // import surfaces a clear error if PYO3_PYTHON's venv lacks it)
        let _bebop = py.import_bound("bebop")?;
        // bebop has no __version__ attr, so look at the package metadata
        let v: String = py
            .import_bound("importlib.metadata")?
            .getattr("version")?
            .call1(("bebop",))?
            .extract()?;
        Ok(v)
    })
}

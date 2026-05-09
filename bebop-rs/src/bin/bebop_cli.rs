//! `bebop_cli` — Phase A reference binary.
//!
//! Takes a WAV in, runs it through the embedded-Python bebop pipeline, writes
//! a comp .mid out. End-to-end equivalent of:
//!
//!     uv run bebop --audio in.wav --bpm 100 --spice 0.4 \
//!         --voicing rootless --rhythm charleston --out comp.mid
//!
//! Used by the differential test (`tests/differential.rs`) to verify the
//! Rust-driven pipeline produces the same MIDI as the Python CLI does.

use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;

#[derive(Parser, Debug)]
#[command(
    name = "bebop_cli",
    about = "Native Rust front-end to the bebop comp pipeline (embedded Python)."
)]
struct Cli {
    /// Input WAV (mono or stereo, any sample rate; bebop resamples internally).
    #[arg(long)]
    r#in: PathBuf,

    /// Output .mid path. Parent directory will be created if it doesn't exist.
    #[arg(long)]
    out: PathBuf,

    /// Beats per minute. Required because chroma can't infer tempo.
    #[arg(long)]
    bpm: f64,

    /// Reharm intensity, 0.0..1.0. Default matches the Python CLI's default.
    #[arg(long, default_value_t = 0.4)]
    spice: f64,

    /// Voicing style: rootless | evans | drop2 | quartal.
    #[arg(long, default_value = "rootless")]
    voicing: String,

    /// Rhythm template; see `bebop.rhythm.all_rhythm_names()`.
    #[arg(long, default_value = "charleston")]
    rhythm: String,
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    if let Some(parent) = cli.out.parent() {
        std::fs::create_dir_all(parent)?;
    }

    println!("[bebop-rs] embedded Python: {}", bebop_rs::python::bebop_version()?);
    println!(
        "[bebop-rs] {} → {} (bpm={}, spice={}, voicing={}, rhythm={})",
        cli.r#in.display(), cli.out.display(),
        cli.bpm, cli.spice, cli.voicing, cli.rhythm
    );

    bebop_rs::process_wav_offline(
        &cli.r#in,
        &cli.out,
        cli.bpm,
        cli.spice,
        &cli.voicing,
        &cli.rhythm,
    )?;
    println!("[bebop-rs] wrote {}", cli.out.display());
    Ok(())
}

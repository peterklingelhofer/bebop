//! Tiny WAV-reading helper. Used by the Phase-A CLI and (later) by the
//! live-path tests that feed pre-rendered audio through the ring buffer.

use std::path::Path;

use anyhow::{Context, Result};
use hound::WavReader;

/// Load a WAV as mono f32 at its native sample rate. Multi-channel inputs
/// are folded to mono by averaging.
pub fn load_mono_f32(path: &Path) -> Result<(Vec<f32>, u32)> {
    let mut reader =
        WavReader::open(path).with_context(|| format!("opening WAV {}", path.display()))?;
    let spec = reader.spec();
    let sr = spec.sample_rate;
    let channels = spec.channels as usize;

    let samples: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Float => reader.samples::<f32>().collect::<Result<Vec<_>, _>>()?,
        hound::SampleFormat::Int => {
            let max = (1i64 << (spec.bits_per_sample - 1)) as f32;
            reader
                .samples::<i32>()
                .map(|s| s.map(|v| v as f32 / max))
                .collect::<Result<Vec<_>, _>>()?
        }
    };

    if channels == 1 {
        return Ok((samples, sr));
    }
    let mut mono = Vec::with_capacity(samples.len() / channels);
    for frame in samples.chunks(channels) {
        let s: f32 = frame.iter().sum::<f32>() / channels as f32;
        mono.push(s);
    }
    Ok((mono, sr))
}

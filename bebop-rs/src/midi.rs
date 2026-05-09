//! Tiny MIDI helpers — for now just a function that reads a .mid file and
//! returns a normalized list of note events suitable for byte-stable
//! comparison in the differential test.
//!
//! pretty_midi (the Python writer) is non-deterministic in its raw byte
//! output (track-name encoding order, default-tempo placement) so we can't
//! diff bytes directly. We canonicalize to (channel, pitch, start_seconds,
//! duration_seconds, velocity) tuples and compare those.

use std::path::Path;

use anyhow::{Context, Result};
use midly::{MetaMessage, MidiMessage, Smf, Timing, TrackEventKind};

/// One canonicalized note event from a .mid file.
#[derive(Debug, Clone, PartialEq)]
pub struct Note {
    pub channel: u8,
    pub pitch: u8,
    pub velocity: u8,
    pub start_seconds: f64,
    pub end_seconds: f64,
    /// The track's name (the meta-event "Track Name" if any).
    pub track_name: String,
}

/// Read a .mid file into a sorted list of canonicalized notes.
pub fn load_notes(path: &Path) -> Result<Vec<Note>> {
    let bytes = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    let smf = Smf::parse(&bytes)?;
    let ticks_per_beat = match smf.header.timing {
        Timing::Metrical(t) => u16::from(t) as f64,
        Timing::Timecode(_, _) => anyhow::bail!("SMPTE timecode MIDI files not supported"),
    };

    // Default tempo if not set: 500_000 µs/beat (= 120 bpm). pretty_midi sets
    // an `initial_tempo` meta event, which we capture as we go.
    let mut notes: Vec<Note> = Vec::new();

    for track in &smf.tracks {
        let mut track_name = String::new();
        let mut us_per_beat: u32 = 500_000;
        // pending note_on events keyed by (channel, pitch) so we can match
        // the corresponding note_off
        let mut pending: std::collections::HashMap<(u8, u8), (f64, u8)> =
            std::collections::HashMap::new();

        let mut t_ticks: u64 = 0;
        let mut t_seconds: f64 = 0.0;
        for ev in track {
            // delta is in ticks; convert to seconds using the current tempo
            let delta_ticks = u32::from(ev.delta) as f64;
            let delta_seconds = (delta_ticks / ticks_per_beat) * (us_per_beat as f64 / 1_000_000.0);
            t_ticks += u32::from(ev.delta) as u64;
            t_seconds += delta_seconds;

            match ev.kind {
                TrackEventKind::Meta(MetaMessage::Tempo(t)) => {
                    us_per_beat = u32::from(t);
                }
                TrackEventKind::Meta(MetaMessage::TrackName(name)) => {
                    track_name = String::from_utf8_lossy(name).into_owned();
                }
                TrackEventKind::Midi { channel, message } => match message {
                    MidiMessage::NoteOn { key, vel } => {
                        let pitch = u8::from(key);
                        let velocity = u8::from(vel);
                        if velocity == 0 {
                            // running-status note_off
                            if let Some((start, v)) =
                                pending.remove(&(u8::from(channel), pitch))
                            {
                                notes.push(Note {
                                    channel: u8::from(channel),
                                    pitch,
                                    velocity: v,
                                    start_seconds: start,
                                    end_seconds: t_seconds,
                                    track_name: track_name.clone(),
                                });
                            }
                        } else {
                            pending.insert((u8::from(channel), pitch), (t_seconds, velocity));
                        }
                    }
                    MidiMessage::NoteOff { key, .. } => {
                        let pitch = u8::from(key);
                        if let Some((start, v)) = pending.remove(&(u8::from(channel), pitch)) {
                            notes.push(Note {
                                channel: u8::from(channel),
                                pitch,
                                velocity: v,
                                start_seconds: start,
                                end_seconds: t_seconds,
                                track_name: track_name.clone(),
                            });
                        }
                    }
                    _ => {}
                },
                _ => {}
            }
            let _ = t_ticks; // currently unused but tracked for future debugging
        }
    }

    // sort for stable comparison: (track_name, start, pitch, channel)
    notes.sort_by(|a, b| {
        a.track_name
            .cmp(&b.track_name)
            .then(a.start_seconds.partial_cmp(&b.start_seconds).unwrap())
            .then(a.pitch.cmp(&b.pitch))
            .then(a.channel.cmp(&b.channel))
    });
    Ok(notes)
}

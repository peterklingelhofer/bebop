// SPDX-License-Identifier: MIT
//
// SPSC lock-free queue of MIDI events, written by the worker thread
// (chord-recognition layer) and drained by the realtime audio thread
// (which delivers them to the host via AUMIDIOutputCallback).
//
// Each event is a 3-byte short MIDI message (status, data1, data2)
// — enough for note_on, note_off, control_change, pitch_bend, etc.
// SysEx isn't needed for comp output.
//
// Same wraparound pattern as AudioRingBuffer; wrapped in its own type
// for clarity (and so the producer/consumer aren't accidentally connected
// to a different queue).

#ifndef BEBOP_MIDI_EVENT_QUEUE_H
#define BEBOP_MIDI_EVENT_QUEUE_H

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace bebop {

struct MidiEvent {
    uint8_t status;   // e.g. 0x90 | channel for note_on, 0x80 | channel for note_off
    uint8_t data1;    // pitch (0..127) or controller number
    uint8_t data2;    // velocity (0..127) or controller value
    /// Sample offset within the current render block. Used by the
    /// rhythm-sync path to land beat-anchored events on the right sample;
    /// wall-clock events leave it at 0 and fire at block start
    uint32_t sampleOffset;
};

/// Beat-scheduled event for the rhythm-sync pathway. Worker drains a
/// batch of these from Rust on each chord commit and pushes them to a
/// SPSC inbox; the audio thread builds a local sorted view and fires
/// events when the host's beat at sample resolution crosses each
/// event's `dueAtBeat`. Layout matches `BebopBeatEvent` in
/// `bebop-rs/include/bebop_rs.h`
struct BeatEvent {
    uint8_t  status;
    uint8_t  data1;
    uint8_t  data2;
    uint8_t  _pad;
    uint32_t genId;       // comp-burst generation; advances on each new chord
    double   dueAtBeat;   // host beat at which to fire
};

class MidiEventQueue {
public:
    explicit MidiEventQueue(size_t capacity)
        : mBuf(capacity), mCapacity(capacity)
    {}

    /// Producer-side (worker thread). Returns true if pushed, false if
    /// the queue is full (event dropped). We don't expect overflow at
    /// our scale — a chord change produces ~5-10 events at <1 Hz.
    bool push(MidiEvent ev) noexcept
    {
        const size_t writeIdx = mWriteIdx.load(std::memory_order_relaxed);
        const size_t readIdx  = mReadIdx.load(std::memory_order_acquire);
        const size_t next = (writeIdx + 1) % mCapacity;
        if (next == readIdx) {
            return false; // queue full
        }
        mBuf[writeIdx] = ev;
        mWriteIdx.store(next, std::memory_order_release);
        return true;
    }

    /// Consumer-side (audio thread, RT-safe). Returns true and fills `out`
    /// if an event was available; false if empty.
    bool pop(MidiEvent& out) noexcept
    {
        const size_t readIdx  = mReadIdx.load(std::memory_order_relaxed);
        const size_t writeIdx = mWriteIdx.load(std::memory_order_acquire);
        if (readIdx == writeIdx) {
            return false;
        }
        out = mBuf[readIdx];
        mReadIdx.store((readIdx + 1) % mCapacity, std::memory_order_release);
        return true;
    }

    /// Cheap count snapshot (audio thread).
    size_t available() const noexcept
    {
        const size_t writeIdx = mWriteIdx.load(std::memory_order_acquire);
        const size_t readIdx  = mReadIdx.load(std::memory_order_relaxed);
        return (writeIdx + mCapacity - readIdx) % mCapacity;
    }

private:
    std::vector<MidiEvent> mBuf;
    const size_t mCapacity;
    alignas(64) std::atomic<size_t> mWriteIdx { 0 };
    alignas(64) std::atomic<size_t> mReadIdx  { 0 };
};

/// Voicing update for the rhythm-driven sync mode. The audio thread
/// keeps the LATEST voicing as "current" and emits its pitches at every
/// rhythm hit. Layout matches `BebopVoicing` in
/// `bebop-rs/include/bebop_rs.h` (16 bytes)
struct Voicing {
    uint32_t genId;
    int8_t   bassPitch;
    uint8_t  pitchCount;
    uint8_t  _pad[2];
    uint8_t  pitches[8];
};

/// SPSC queue of Voicing updates (worker → audio thread)
class VoicingQueue {
public:
    explicit VoicingQueue(size_t capacity)
        : mBuf(capacity), mCapacity(capacity) {}
    bool push(Voicing v) noexcept {
        const size_t writeIdx = mWriteIdx.load(std::memory_order_relaxed);
        const size_t readIdx  = mReadIdx.load(std::memory_order_acquire);
        const size_t next = (writeIdx + 1) % mCapacity;
        if (next == readIdx) { return false; }
        mBuf[writeIdx] = v;
        mWriteIdx.store(next, std::memory_order_release);
        return true;
    }
    bool pop(Voicing& out) noexcept {
        const size_t readIdx  = mReadIdx.load(std::memory_order_relaxed);
        const size_t writeIdx = mWriteIdx.load(std::memory_order_acquire);
        if (readIdx == writeIdx) { return false; }
        out = mBuf[readIdx];
        mReadIdx.store((readIdx + 1) % mCapacity, std::memory_order_release);
        return true;
    }
private:
    std::vector<Voicing> mBuf;
    const size_t mCapacity;
    alignas(64) std::atomic<size_t> mWriteIdx { 0 };
    alignas(64) std::atomic<size_t> mReadIdx  { 0 };
};

/// Same SPSC pattern as MidiEventQueue but carrying BeatEvent. Used for
/// the worker→audio handoff of beat-scheduled events when Rhythm Sync
/// is on
class BeatEventQueue {
public:
    explicit BeatEventQueue(size_t capacity)
        : mBuf(capacity), mCapacity(capacity)
    {}

    bool push(BeatEvent ev) noexcept
    {
        const size_t writeIdx = mWriteIdx.load(std::memory_order_relaxed);
        const size_t readIdx  = mReadIdx.load(std::memory_order_acquire);
        const size_t next = (writeIdx + 1) % mCapacity;
        if (next == readIdx) { return false; }
        mBuf[writeIdx] = ev;
        mWriteIdx.store(next, std::memory_order_release);
        return true;
    }

    bool pop(BeatEvent& out) noexcept
    {
        const size_t readIdx  = mReadIdx.load(std::memory_order_relaxed);
        const size_t writeIdx = mWriteIdx.load(std::memory_order_acquire);
        if (readIdx == writeIdx) { return false; }
        out = mBuf[readIdx];
        mReadIdx.store((readIdx + 1) % mCapacity, std::memory_order_release);
        return true;
    }

private:
    std::vector<BeatEvent> mBuf;
    const size_t mCapacity;
    alignas(64) std::atomic<size_t> mWriteIdx { 0 };
    alignas(64) std::atomic<size_t> mReadIdx  { 0 };
};

} // namespace bebop

#endif // BEBOP_MIDI_EVENT_QUEUE_H

// SPDX-License-Identifier: MIT
//
// Single-producer single-consumer ring buffer for the AU's audio →
// worker-thread audio handoff. The audio render block writes; a separate
// worker thread reads. Lock-free, allocation-free in steady state, safe
// to call from the realtime thread.
//
// Capacity is fixed at construction (allocates once, never again).
// Overflow is the writer's problem: `write()` returns the number of samples
// actually written; if the consumer is too slow, the producer drops the
// excess rather than blocking. This is the right behavior for a chord
// recognizer that only needs occasional 1-second windows.
//
// Mono float32. Audio is downmixed before reaching this buffer.

#ifndef BEBOP_AUDIO_RING_BUFFER_H
#define BEBOP_AUDIO_RING_BUFFER_H

#include <atomic>
#include <cstddef>
#include <cstring>
#include <vector>

namespace bebop {

class AudioRingBuffer {
public:
    explicit AudioRingBuffer(size_t capacitySamples)
        : mBuf(capacitySamples), mCapacity(capacitySamples)
    {}

    /// Realtime-safe (no allocation, no locks, no syscalls).
    /// Writes up to `n` samples; returns how many were actually accepted.
    /// Drops the tail if the buffer is too full (producer-priority
    /// semantics: consumer's job to catch up).
    size_t write(const float* src, size_t n) noexcept
    {
        const size_t cap = mCapacity;
        const size_t writeIdx = mWriteIdx.load(std::memory_order_relaxed);
        const size_t readIdx  = mReadIdx.load(std::memory_order_acquire);

        // available_space = cap - 1 - (writeIdx - readIdx)  [mod cap]
        // The "-1" reserves one slot to disambiguate full vs empty —
        // standard ring-buffer trick.
        const size_t used = (writeIdx + cap - readIdx) % cap;
        const size_t available = (cap - 1) - used;
        const size_t toCopy = (n < available) ? n : available;
        if (toCopy == 0) {
            return 0;
        }

        // Two memcpys, in case of wraparound.
        const size_t firstChunk = ((writeIdx + toCopy) <= cap)
            ? toCopy
            : (cap - writeIdx);
        std::memcpy(&mBuf[writeIdx], src, firstChunk * sizeof(float));
        if (toCopy > firstChunk) {
            std::memcpy(&mBuf[0], src + firstChunk,
                        (toCopy - firstChunk) * sizeof(float));
        }
        mWriteIdx.store((writeIdx + toCopy) % cap,
                        std::memory_order_release);
        return toCopy;
    }

    /// Snapshot of how many samples are currently readable. Cheap.
    size_t available() const noexcept
    {
        const size_t writeIdx = mWriteIdx.load(std::memory_order_acquire);
        const size_t readIdx  = mReadIdx.load(std::memory_order_relaxed);
        return (writeIdx + mCapacity - readIdx) % mCapacity;
    }

    /// Reads up to `n` samples into `dst`. Returns how many were actually
    /// copied. Called from the worker thread, NOT realtime-safe.
    size_t read(float* dst, size_t n) noexcept
    {
        const size_t cap = mCapacity;
        const size_t readIdx  = mReadIdx.load(std::memory_order_relaxed);
        const size_t writeIdx = mWriteIdx.load(std::memory_order_acquire);
        const size_t used = (writeIdx + cap - readIdx) % cap;
        const size_t toCopy = (n < used) ? n : used;
        if (toCopy == 0) {
            return 0;
        }

        const size_t firstChunk = ((readIdx + toCopy) <= cap)
            ? toCopy
            : (cap - readIdx);
        std::memcpy(dst, &mBuf[readIdx], firstChunk * sizeof(float));
        if (toCopy > firstChunk) {
            std::memcpy(dst + firstChunk, &mBuf[0],
                        (toCopy - firstChunk) * sizeof(float));
        }
        mReadIdx.store((readIdx + toCopy) % cap,
                       std::memory_order_release);
        return toCopy;
    }

    /// Peek without advancing the read cursor. Used by the analysis pass
    /// to grab a 1-second window while leaving the buffer state alone for
    /// the next analysis to overlap.
    size_t peek(float* dst, size_t n) const noexcept
    {
        const size_t cap = mCapacity;
        const size_t readIdx  = mReadIdx.load(std::memory_order_relaxed);
        const size_t writeIdx = mWriteIdx.load(std::memory_order_acquire);
        const size_t used = (writeIdx + cap - readIdx) % cap;
        const size_t toCopy = (n < used) ? n : used;
        if (toCopy == 0) {
            return 0;
        }
        const size_t firstChunk = ((readIdx + toCopy) <= cap)
            ? toCopy
            : (cap - readIdx);
        std::memcpy(dst, &mBuf[readIdx], firstChunk * sizeof(float));
        if (toCopy > firstChunk) {
            std::memcpy(dst + firstChunk, &mBuf[0],
                        (toCopy - firstChunk) * sizeof(float));
        }
        return toCopy;
    }

    /// Drop `n` samples from the front of the readable region.
    /// Used after `peek` + analysis to advance past consumed samples.
    void advance(size_t n) noexcept
    {
        const size_t avail = available();
        const size_t toAdvance = (n < avail) ? n : avail;
        mReadIdx.store(
            (mReadIdx.load(std::memory_order_relaxed) + toAdvance) % mCapacity,
            std::memory_order_release);
    }

    size_t capacity() const noexcept { return mCapacity; }

private:
    std::vector<float> mBuf;
    const size_t mCapacity;
    // Cache-line padded to avoid false sharing between the producer and
    // consumer's update of these counters.
    alignas(64) std::atomic<size_t> mWriteIdx { 0 };
    alignas(64) std::atomic<size_t> mReadIdx  { 0 };
};

} // namespace bebop

#endif // BEBOP_AUDIO_RING_BUFFER_H

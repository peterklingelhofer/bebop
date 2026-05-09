// SPDX-License-Identifier: MIT
//
// Unit tests for AudioRingBuffer. Built and run from the Makefile;
// no external test framework so the deps stay light. Failures abort()
// the test process so `make test-rb` exits non-zero.

#include "../Sources/AudioRingBuffer.h"

#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <thread>
#include <vector>

#define ASSERT_EQ(a, b) do {                                          \
    auto _a = (a); auto _b = (b);                                     \
    if (!(_a == _b)) {                                                \
        std::fprintf(stderr, "FAIL %s:%d  %s == %s  (%lld vs %lld)\n",\
                     __FILE__, __LINE__, #a, #b,                      \
                     (long long)_a, (long long)_b);                   \
        std::abort();                                                 \
    }                                                                 \
} while (0)

#define ASSERT_TRUE(x) do {                                           \
    if (!(x)) {                                                       \
        std::fprintf(stderr, "FAIL %s:%d  %s\n",                      \
                     __FILE__, __LINE__, #x);                         \
        std::abort();                                                 \
    }                                                                 \
} while (0)

using bebop::AudioRingBuffer;

// 1 — basic write/read round trip
static void test_basic() {
    AudioRingBuffer rb(16);
    float input[8] = { 1, 2, 3, 4, 5, 6, 7, 8 };
    ASSERT_EQ(rb.write(input, 8), 8u);
    ASSERT_EQ(rb.available(), 8u);

    float out[8] = {};
    ASSERT_EQ(rb.read(out, 8), 8u);
    for (int i = 0; i < 8; ++i) ASSERT_EQ(out[i], input[i]);
    ASSERT_EQ(rb.available(), 0u);
    std::puts("✓ basic write/read");
}

// 2 — overflow drops samples (producer-priority)
static void test_overflow_drops() {
    AudioRingBuffer rb(16); // capacity-1 = 15 usable
    float input[20] = {};
    for (int i = 0; i < 20; ++i) input[i] = float(i);
    const size_t written = rb.write(input, 20);
    ASSERT_EQ(written, 15u); // not 20 — 1 slot reserved for empty/full disambig
    ASSERT_EQ(rb.available(), 15u);
    std::puts("✓ overflow drops");
}

// 3 — wraparound is correct
static void test_wraparound() {
    AudioRingBuffer rb(8); // 7 usable
    float a[5] = { 1, 2, 3, 4, 5 };
    rb.write(a, 5);
    float pull[3];
    rb.read(pull, 3); // now read=3, write=5, used=2
    float b[4] = { 6, 7, 8, 9 };
    ASSERT_EQ(rb.write(b, 4), 4u); // wraparound across the buffer's end

    float out[6] = {};
    ASSERT_EQ(rb.read(out, 6), 6u);
    // Expect: 4, 5, 6, 7, 8, 9
    float expected[6] = { 4, 5, 6, 7, 8, 9 };
    for (int i = 0; i < 6; ++i) ASSERT_EQ(out[i], expected[i]);
    std::puts("✓ wraparound");
}

// 4 — peek + advance pattern
static void test_peek_advance() {
    AudioRingBuffer rb(32);
    float input[10];
    for (int i = 0; i < 10; ++i) input[i] = float(i + 100);
    rb.write(input, 10);

    float peek1[5] = {};
    ASSERT_EQ(rb.peek(peek1, 5), 5u);
    ASSERT_EQ(peek1[0], 100.f);
    ASSERT_EQ(peek1[4], 104.f);
    // peek should not advance — peeking again gives same result
    float peek2[5] = {};
    rb.peek(peek2, 5);
    ASSERT_EQ(peek2[0], 100.f);
    // now advance and peek again
    rb.advance(3);
    float peek3[5] = {};
    rb.peek(peek3, 5);
    ASSERT_EQ(peek3[0], 103.f);
    std::puts("✓ peek + advance");
}

// 5 — concurrent producer + consumer (the only way to verify lock-free
//     correctness is to exercise it under stress)
static void test_concurrent() {
    constexpr size_t kCap = 1024;
    constexpr size_t kTotal = 100'000;
    AudioRingBuffer rb(kCap);
    std::atomic<bool> done { false };

    // Producer: write monotonically increasing floats
    std::thread producer([&] {
        size_t i = 0;
        while (i < kTotal) {
            float chunk[64];
            const size_t n = (kTotal - i) < 64 ? (kTotal - i) : 64;
            for (size_t k = 0; k < n; ++k) chunk[k] = float(i + k);
            const size_t w = rb.write(chunk, n);
            i += w;
            if (w == 0) {
                std::this_thread::yield();
            }
        }
        done.store(true, std::memory_order_release);
    });

    // Consumer: read and verify monotonic
    std::thread consumer([&] {
        size_t expected = 0;
        while (true) {
            float chunk[128];
            const size_t r = rb.read(chunk, 128);
            for (size_t k = 0; k < r; ++k) {
                ASSERT_EQ(chunk[k], float(expected));
                ++expected;
            }
            if (r == 0) {
                if (done.load(std::memory_order_acquire) && rb.available() == 0) break;
                std::this_thread::yield();
            }
        }
        ASSERT_EQ(expected, kTotal);
    });

    producer.join();
    consumer.join();
    std::puts("✓ concurrent SPSC stress (100k samples)");
}

int main() {
    test_basic();
    test_overflow_drops();
    test_wraparound();
    test_peek_advance();
    test_concurrent();
    std::puts("PASS");
    return 0;
}

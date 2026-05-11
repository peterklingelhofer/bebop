// SPDX-License-Identifier: MIT
//
// Rhythm template tables, mirrored from bebop/rhythm/templates.py for the
// audio-thread phase-locked sync mode. Templates here are used by the AU
// shell to drive rhythm hits at sample-accurate host-beat positions —
// the Python-side templates (still authoritative) drive the offline
// `bebop` CLI and free-mode comp generation.
//
// Order MUST match BebopAU.cpp's `kBebopParamRhythm` value-strings array
// (which mirrors Python's `bebop.rhythm.all_rhythm_names()` sorted).
// The AU-param index → template lookup happens via `lookupTemplate`

#ifndef BEBOP_RHYTHM_TEMPLATES_H
#define BEBOP_RHYTHM_TEMPLATES_H

#include <cstddef>
#include <cstdint>

namespace bebop {

struct RhythmHit {
    double  offsetBeats;     // [0, 4)
    double  durationBeats;   // not currently used in phase-lock (note-off
                             // is at next hit's onset minus a small gap)
    uint8_t velocity;        // 0..127
};

struct RhythmTemplate {
    const char*       name;
    const RhythmHit*  hits;
    size_t            count;
    /// Whole-template shift in beats (charleston_+1, etc.). Applied as
    /// `(hit.offset + shift) mod 4` so the shifted version still tiles
    /// to a 4-beat bar
    double            shiftBeats;
};

namespace detail {

inline constexpr RhythmHit kAhmadJamal[] = {
    {0.0, 0.9, 82}, {1.0, 0.9, 70}, {2.5, 1.5, 78},
};
inline constexpr RhythmHit kAnticipations[] = {
    {0.0, 1.5, 84}, {2.0, 0.5, 68}, {3.5, 0.5, 90},
};
inline constexpr RhythmHit kBossa[] = {
    {0.0, 1.5, 80}, {1.5, 1.0, 72}, {3.0, 1.0, 76},
};
inline constexpr RhythmHit kCharleston[] = {
    {0.0, 1.5, 90}, {2.5, 1.5, 78},
};
inline constexpr RhythmHit kFreddieGreen[] = {
    {0.0, 0.9, 65}, {1.0, 0.9, 78}, {2.0, 0.9, 65}, {3.0, 0.9, 78},
};
inline constexpr RhythmHit kKennyBarron[] = {
    {0.0, 1.5, 82}, {1.5, 1.5, 76}, {3.0, 1.0, 80},
};
inline constexpr RhythmHit kReverseCharleston[] = {
    {0.5, 1.5, 78}, {2.0, 1.5, 84},
};
inline constexpr RhythmHit kSustained[] = {
    {0.0, 4.0, 72},
};
inline constexpr RhythmHit kTwoAndFour[] = {
    {1.0, 0.9, 70}, {3.0, 0.9, 75},
};

inline constexpr RhythmTemplate kTemplates[] = {
    // Order MUST match all_rhythm_names() (sorted) and the AU's
    // value-strings for kBebopParamRhythm
    {"ahmad_jamal",        kAhmadJamal,        3, 0.0},
    {"anticipations",      kAnticipations,     3, 0.0},
    {"bossa",              kBossa,             3, 0.0},
    {"charleston",         kCharleston,        2, 0.0},
    {"charleston_+1",      kCharleston,        2, 1.0},
    {"charleston_+2",      kCharleston,        2, 2.0},
    {"charleston_+3",      kCharleston,        2, 3.0},
    {"freddie_green",      kFreddieGreen,      4, 0.0},
    {"kenny_barron",       kKennyBarron,       3, 0.0},
    {"reverse_charleston", kReverseCharleston, 2, 0.0},
    {"sustained",          kSustained,         1, 0.0},
    {"two_and_four",       kTwoAndFour,        2, 0.0},
};

} // namespace detail

inline constexpr size_t kRhythmCount =
    sizeof(detail::kTemplates) / sizeof(detail::kTemplates[0]);

/// Look up a template by AU-param index. Returns nullptr if out of range
inline const RhythmTemplate* lookupTemplate(size_t idx) noexcept {
    if (idx >= kRhythmCount) return nullptr;
    return &detail::kTemplates[idx];
}

} // namespace bebop

#endif // BEBOP_RHYTHM_TEMPLATES_H

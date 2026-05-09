// SPDX-License-Identifier: MIT
//
// BebopAU — Phase B passthrough Audio Unit (AUv2, C++).
//
// This is a do-nothing audio effect: stereo audio in, stereo audio out,
// copies samples through unchanged. The point of Phase B is to verify the
// AU framework + bundle layout + signing all work end-to-end via Apple's
// AUSDK before we layer in the bebop-rs FFI (Phase C) and MIDI output.
//
// We use the AUSDK's `AUEffectBase` rather than the modern Swift `AUAudioUnit`
// because:
//   - AUSDK ships the v2 component-manager bridge code we'd otherwise have
//     to write ourselves (~hundreds of lines of selector dispatch)
//   - AUv2 .component bundles aren't sandboxed, which keeps embedded Python
//     viable for Phase C
//   - The AUSDK_COMPONENT_ENTRY macro generates the C entry point that
//     CFBundle's dlsym lookup finds.
//
// The AU SDK uses C++23.

#include <AudioUnitSDK/AUEffectBase.h>
#include <AudioUnitSDK/ComponentBase.h>
#include <AudioToolbox/AudioUnitUtilities.h>   // AUEventListenerNotify
#include <CoreMIDI/CoreMIDI.h>
#include <os/log.h>

#include <bebop_rs.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>

#include "AudioRingBuffer.h"

/// Unified-logging handle. Logs filed under subsystem "com.bebop.au"
/// — visible in Console.app by filtering "Subsystem: com.bebop.au".
/// Use os_log instead of fprintf(stderr) because hosts (Logic, Pro
/// Tools, etc.) may redirect or silence stderr from loaded plugins.
static os_log_t bebop_log() {
    static os_log_t log = os_log_create("com.bebop.au", "BebopAU");
    return log;
}
#include "MidiEventQueue.h"

#include <CoreMIDI/MIDIServices.h>

/// Per-channel kernel. AUEffectBase's render path is per-channel: for each
/// channel of input it instantiates one of these and calls Process() with
/// that channel's samples.
///
/// Two responsibilities:
///   1. **Audio delay** — each kernel maintains a ring buffer that delays
///      the audio passthrough by `latency_samples` samples. The whole
///      plugin reports this delay to the host via
///      `kAudioUnitProperty_Latency`, and Logic's PDC delays other tracks
///      to compensate. The MIDI we emit "now" represents a chord recognized
///      from input ~1.3s ago — which is exactly what Logic now plays
///      simultaneously. End result: comp lands sample-aligned with chord
///      changes in the master. The cost is a 1.3s monitoring delay.
///   2. **Analysis feed** — channel 0 also writes input samples to the
///      analysis ring buffer (mono left-channel; chroma doesn't care about
///      stereo). The worker thread drains that ring for chord recognition.
class BebopKernel : public ausdk::AUKernelBase {
public:
    BebopKernel(ausdk::AUEffectBase* au, bebop::AudioRingBuffer* ring)
        : AUKernelBase(au), mRing(ring)
    {}

    /// Allocate the delay ring. Called from BebopAU::Initialize once the
    /// host has set the sample rate. Non-realtime (allocates).
    void allocateDelay(double sampleRate, double latencySeconds)
    {
        const size_t latencySamples =
            static_cast<size_t>(latencySeconds * sampleRate + 0.5);
        // 50% headroom so partial-block writes don't wrap into unread data
        const size_t bufSamples = (latencySamples * 3) / 2 + 16;
        mDelayBuf.assign(bufSamples, 0.0f);
        mLatencySamples = latencySamples;
        mWriteIdx = 0;
    }

    void deallocateDelay()
    {
        mDelayBuf.clear();
        mLatencySamples = 0;
        mWriteIdx = 0;
    }

    void Process(const Float32* inSrc, Float32* outDst,
                 UInt32 inFrames, bool& /*ioSilence*/) AUSDK_RTSAFE override
    {
        // CRITICAL ordering: with ProcessesInPlace=true, `inSrc` and
        // `outDst` may alias (host passed the same buffer). The ring
        // buffer feed and the delay-read need ORIGINAL input — so do them
        // BEFORE any write to `outDst`.

        // 1. feed the analysis ring with input (un-delayed). Recognition
        //    sees audio "as it arrives"; the audio path delay is purely
        //    for PDC alignment of the resulting MIDI.
        if (mRing != nullptr && GetChannelNum() == 0) {
            mRing->write(inSrc, inFrames);
        }

        const size_t bufSize = mDelayBuf.size();
        if (bufSize == 0 || mLatencySamples == 0) {
            std::memmove(outDst, inSrc, inFrames * sizeof(Float32));
            return;
        }

        // 2. delay-line. Capture each input sample to a local before
        //    writing to outDst, so aliasing in-place buffers don't
        //    corrupt the delay line.
        const size_t readStart =
            (mWriteIdx + bufSize - mLatencySamples) % bufSize;
        for (UInt32 i = 0; i < inFrames; ++i) {
            const float input = inSrc[i];           // capture before write
            outDst[i] = mDelayBuf[(readStart + i) % bufSize];
            mDelayBuf[(mWriteIdx + i) % bufSize] = input;
        }
        mWriteIdx = (mWriteIdx + inFrames) % bufSize;
    }

private:
    bebop::AudioRingBuffer* mRing;
    std::vector<float>      mDelayBuf;
    size_t                  mLatencySamples = 0;
    size_t                  mWriteIdx       = 0;
};

class BebopAU : public ausdk::AUEffectBase {
public:
    enum {
        kBebopParamSpice      = 0,  // 0..1, reharm intensity
        kBebopParamVoicing    = 1,  // 0..3 (rootless/evans/drop2/quartal)
        kBebopParamRhythm     = 2,  // 0..n_rhythms-1
        kBebopParamBpm        = 3,  // 40..240
        kBebopParamOctave     = 4,  // -3..+3, octave shift
        kBebopParamSyncBpm    = 5,  // 0..1, follow host tempo when on
        kBebopParamLastChord  = 6,  // indexed: current chord display
        kBebopParamLoopMode   = 7,  // 0=off, 1=auto, 2=manual
        kBebopParamLoopBars   = 8,  // 0..5: 1, 2, 4, 8, 16, 32 bars
        kBebopParamCompOffset = 9,  // beats: shift recorded comp earlier (negative)
        kBebopParamCount,
    };

    /// Loop-bars indexed parameter values → bars. Index 0 = 1 bar etc.
    static constexpr UInt32 kLoopBarsCount = 6;
    static constexpr int kLoopBarsValues[kLoopBarsCount] = { 1, 2, 4, 8, 16, 32 };

    /// Number of rhythm templates exposed by `bebop.rhythm.all_rhythm_names()`.
    /// Hardcoded to avoid asking Python at construction (slow + might not be
    /// initialized yet). The actual list is resolved at parameter-set time
    /// via `bebop_set_param`.
    static constexpr UInt32 kRhythmCount = 12;

    /// Pre-enumerated chord-symbol space for the Last Chord readout. The
    /// space is small enough to expose as an indexed parameter Logic can
    /// render via `kAudioUnitProperty_ParameterValueStrings`. Symbols
    /// outside this set (e.g. "Cmaj13#11") are mapped to their nearest
    /// triad/seventh equivalent at update time.
    ///
    /// 12 roots × 7 qualities = 84, plus index 0 = "—" (no chord yet).
    static constexpr UInt32 kRootCount    = 12;
    static constexpr UInt32 kQualityCount = 7;
    static constexpr UInt32 kChordCount   = 1 + kRootCount * kQualityCount;
    static constexpr const char* kRootNames[kRootCount] = {
        "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"
    };
    static constexpr const char* kQualityNames[kQualityCount] = {
        "", "m", "7", "m7", "maj7", "dim", "sus4"
    };

    explicit BebopAU(AudioComponentInstance inInstance)
        : AUEffectBase(inInstance, /*inProcessesInPlace=*/true)
        // Capacity = ~5 seconds at 48 kHz mono. Generous enough that even
        // a slow worker thread (Python doing CQT every 300 ms) won't
        // overrun while keeping memory modest (~1 MB).
        , mRing(48'000 * 5)
        // MIDI queue: 1024 events is plenty — even a bar of charleston
        // comp at 8th notes generates <50 events.
        , mMidiQueue(1024)
    {
        Globals()->UseIndexedParameters(kBebopParamCount);
        Globals()->SetParameter(kBebopParamSpice,      0.4f);
        Globals()->SetParameter(kBebopParamVoicing,    0.0f);  // rootless
        Globals()->SetParameter(kBebopParamRhythm,     3.0f);  // charleston (index 3)
        Globals()->SetParameter(kBebopParamBpm,        120.0f);
        Globals()->SetParameter(kBebopParamOctave,     0.0f);
        Globals()->SetParameter(kBebopParamSyncBpm,    1.0f);  // follow host
        Globals()->SetParameter(kBebopParamLastChord,  0.0f);  // "—"
        Globals()->SetParameter(kBebopParamLoopMode,   0.0f);  // off — PDC handles alignment
        Globals()->SetParameter(kBebopParamLoopBars,   2.0f);  // 4 bars (only used in manual mode)
        Globals()->SetParameter(kBebopParamCompOffset, 0.0f);  // PDC handles alignment

        // Boot the embedded Python interpreter via bebop-rs. This is
        // moderately expensive (~100ms cold) but happens at AU
        // instantiation time — not in the realtime audio path. If init
        // fails, log to stderr but continue: the plugin still passes
        // audio through, just without comping. Phase C.5 will fail
        // harder once we depend on bebop_rs for real work.
        mBebop = bebop_init();
        if (mBebop == nullptr) {
            const char* err = bebop_last_error();
            os_log_error(bebop_log(),
                "bebop_init failed: %{public}s",
                err ? err : "(no error message)");
        } else {
            // Push the parameter defaults into bebop-rs so the very first
            // chord detection uses them (the host won't necessarily call
            // SetParameter before audio starts flowing).
            bebop_set_param(mBebop, /*BPM*/    0, 120.0);
            bebop_set_param(mBebop, /*Voicing*/1,   0.0);
            bebop_set_param(mBebop, /*Rhythm*/ 2,   3.0);
            bebop_set_param(mBebop, /*Spice*/  3,   0.4);
            bebop_set_param(mBebop, /*Octave*/ 4,   0.0);
        }
    }

    ~BebopAU() override {
        // Stop the worker thread first so it can't try to use the
        // BebopHandle after we destroy it.
        stopWorker();
        if (mBebop != nullptr) {
            bebop_destroy(mBebop);
            mBebop = nullptr;
        }
    }

    /// Called by the host once before the first render. Spin up the
    /// worker thread here — by this point the sample rate is known and
    /// we know audio is about to start flowing.
    OSStatus Initialize() override
    {
        const OSStatus status = AUEffectBase::Initialize();
        if (status != noErr) {
            return status;
        }
        // Each kernel needs its own audio delay ring. AUEffectBase has
        // already created the kernels (one per channel) in the parent
        // Initialize call; now we wire up our delay state with the
        // newly-known sample rate.
        const double sr = GetSampleRate();
        for (UInt32 ch = 0; ch < (UInt32)GetKernelList().size(); ++ch) {
            if (auto* k = dynamic_cast<BebopKernel*>(GetKernel(ch))) {
                k->allocateDelay(sr, kPdcLatencySeconds);
            }
        }
        openVirtualMidiSource();
        startWorker();
        return noErr;
    }

    /// PDC latency in seconds — must equal recognition_window +
    /// (stability_frames-1)*period to keep MIDI sample-aligned with the
    /// chord positions in the master after Logic's PDC compensation.
    /// The Rust side uses analysis_window=1.0 + stability=3 + period=0.3,
    /// so total recognition latency = 1.0 + 2*0.3 = 1.6s. This must
    /// stay in sync with `STABILITY_FRAMES` in bebop-rs/src/ffi.rs.
    static constexpr double kPdcLatencySeconds = 1.6;

    /// Called by the host when the AU is being deactivated (e.g. project
    /// closed). Tear down the worker thread cleanly.
    void Cleanup() override
    {
        stopWorker();
        closeVirtualMidiSource();
        // Free per-kernel delay rings before the parent class deletes
        // the kernels themselves.
        for (UInt32 ch = 0; ch < (UInt32)GetKernelList().size(); ++ch) {
            if (auto* k = dynamic_cast<BebopKernel*>(GetKernel(ch))) {
                k->deallocateDelay();
            }
        }
        AUEffectBase::Cleanup();
    }

    bool SupportsTail() AUSDK_RTSAFE override { return false; }

    std::unique_ptr<ausdk::AUKernelBase> NewKernel() override
    {
        return std::make_unique<BebopKernel>(this, &mRing);
    }

    // ---- Parameters ----

    OSStatus GetParameterInfo(AudioUnitScope inScope,
                               AudioUnitParameterID inParameterID,
                               AudioUnitParameterInfo& outParameterInfo) override
    {
        if (inScope != kAudioUnitScope_Global) {
            return kAudioUnitErr_InvalidScope;
        }
        outParameterInfo.flags = kAudioUnitParameterFlag_IsReadable
                               | kAudioUnitParameterFlag_IsWritable;
        switch (inParameterID) {
        case kBebopParamSpice:
            ausdk::AUBase::FillInParameterName(
                outParameterInfo, CFSTR("Spice"), false);
            outParameterInfo.unit         = kAudioUnitParameterUnit_Generic;
            outParameterInfo.minValue     = 0.0f;
            outParameterInfo.maxValue     = 1.0f;
            outParameterInfo.defaultValue = 0.4f;
            return noErr;
        case kBebopParamVoicing:
            ausdk::AUBase::FillInParameterName(
                outParameterInfo, CFSTR("Voicing"), false);
            outParameterInfo.unit         = kAudioUnitParameterUnit_Indexed;
            outParameterInfo.minValue     = 0.0f;
            outParameterInfo.maxValue     = 3.0f;  // rootless / evans / drop2 / quartal
            outParameterInfo.defaultValue = 0.0f;
            return noErr;
        case kBebopParamRhythm:
            ausdk::AUBase::FillInParameterName(
                outParameterInfo, CFSTR("Rhythm"), false);
            outParameterInfo.unit         = kAudioUnitParameterUnit_Indexed;
            outParameterInfo.minValue     = 0.0f;
            outParameterInfo.maxValue     = static_cast<AudioUnitParameterValue>(
                kRhythmCount - 1);
            outParameterInfo.defaultValue = 3.0f;  // charleston
            return noErr;
        case kBebopParamBpm:
            ausdk::AUBase::FillInParameterName(
                outParameterInfo, CFSTR("BPM"), false);
            outParameterInfo.unit         = kAudioUnitParameterUnit_BPM;
            outParameterInfo.minValue     = 40.0f;
            outParameterInfo.maxValue     = 240.0f;
            outParameterInfo.defaultValue = 120.0f;
            return noErr;
        case kBebopParamOctave:
            ausdk::AUBase::FillInParameterName(
                outParameterInfo, CFSTR("Octave"), false);
            outParameterInfo.unit         = kAudioUnitParameterUnit_RelativeSemiTones;
            outParameterInfo.minValue     = -3.0f;
            outParameterInfo.maxValue     =  3.0f;
            outParameterInfo.defaultValue =  0.0f;
            return noErr;
        case kBebopParamSyncBpm:
            ausdk::AUBase::FillInParameterName(
                outParameterInfo, CFSTR("Sync BPM"), false);
            outParameterInfo.unit         = kAudioUnitParameterUnit_Indexed;
            outParameterInfo.minValue     = 0.0f;
            outParameterInfo.maxValue     = 1.0f;
            outParameterInfo.defaultValue = 1.0f;
            return noErr;
        case kBebopParamLastChord:
            ausdk::AUBase::FillInParameterName(
                outParameterInfo, CFSTR("Chord"), false);
            outParameterInfo.unit         = kAudioUnitParameterUnit_Indexed;
            outParameterInfo.minValue     = 0.0f;
            outParameterInfo.maxValue     = static_cast<AudioUnitParameterValue>(
                kChordCount - 1);
            outParameterInfo.defaultValue = 0.0f;
            // Stays writable so Logic's parameter UI refreshes display
            // updates the same way it does for any indexed param. We
            // override the user's set in SetParameter() to snap back to
            // the latest detected chord (Phase D: replace with proper
            // custom UI that handles a true read-only readout).
            return noErr;
        case kBebopParamLoopMode:
            ausdk::AUBase::FillInParameterName(
                outParameterInfo, CFSTR("Loop"), false);
            outParameterInfo.unit         = kAudioUnitParameterUnit_Indexed;
            outParameterInfo.minValue     = 0.0f;
            outParameterInfo.maxValue     = 2.0f;
            // Default off — PDC alignment makes loop-aware mode optional.
            // Loop is now an opt-in feature for iterative tweaking
            // (refine knobs and hear the change on the next loop pass).
            outParameterInfo.defaultValue = 0.0f;
            return noErr;
        case kBebopParamLoopBars:
            ausdk::AUBase::FillInParameterName(
                outParameterInfo, CFSTR("Loop Bars"), false);
            outParameterInfo.unit         = kAudioUnitParameterUnit_Indexed;
            outParameterInfo.minValue     = 0.0f;
            outParameterInfo.maxValue     = static_cast<AudioUnitParameterValue>(
                kLoopBarsCount - 1);
            outParameterInfo.defaultValue = 2.0f;  // 4 bars
            return noErr;
        case kBebopParamCompOffset:
            ausdk::AUBase::FillInParameterName(
                outParameterInfo, CFSTR("Comp Offset"), false);
            outParameterInfo.unit         = kAudioUnitParameterUnit_Beats;
            outParameterInfo.minValue     = -8.0f;
            outParameterInfo.maxValue     =  8.0f;
            // PDC (always on, 1.3s) handles latency alignment for both
            // realtime and loop modes — comp lands on the chord by
            // default. Use this knob to push the comp earlier (negative)
            // for swing/anticipation feel, or later (positive) for behind-
            // the-beat groove.
            outParameterInfo.defaultValue = 0.0f;
            return noErr;
        }
        return kAudioUnitErr_InvalidParameter;
    }

    /// Provide value-strings so Logic shows readable names ("rootless",
    /// "charleston", "Cmaj7") instead of raw integers in the parameter UI.
    OSStatus GetParameterValueStrings(AudioUnitScope inScope,
                                       AudioUnitParameterID inParameterID,
                                       CFArrayRef* outStrings) override
    {
        if (inScope != kAudioUnitScope_Global) {
            return kAudioUnitErr_InvalidScope;
        }
        if (outStrings == nullptr) {
            // Logic asks with NULL first to discover whether we support
            // strings for this parameter (saves it from doing the
            // CFArrayCreate dance unless we say yes).
            switch (inParameterID) {
            case kBebopParamVoicing:
            case kBebopParamRhythm:
            case kBebopParamSyncBpm:
            case kBebopParamLastChord:
            case kBebopParamLoopMode:
            case kBebopParamLoopBars:
                return noErr;
            default:
                return kAudioUnitErr_InvalidProperty;
            }
        }
        switch (inParameterID) {
        case kBebopParamVoicing: {
            CFStringRef names[] = {
                CFSTR("rootless"), CFSTR("evans"),
                CFSTR("drop2"), CFSTR("quartal")
            };
            *outStrings = CFArrayCreate(nullptr, (const void**)names, 4,
                                          &kCFTypeArrayCallBacks);
            return noErr;
        }
        case kBebopParamRhythm: {
            // Order matches `bebop.rhythm.all_rhythm_names()` (sorted).
            CFStringRef names[] = {
                CFSTR("ahmad_jamal"), CFSTR("anticipations"), CFSTR("bossa"),
                CFSTR("charleston"), CFSTR("charleston_+1"),
                CFSTR("charleston_+2"), CFSTR("charleston_+3"),
                CFSTR("freddie_green"), CFSTR("kenny_barron"),
                CFSTR("reverse_charleston"), CFSTR("sustained"),
                CFSTR("two_and_four")
            };
            *outStrings = CFArrayCreate(nullptr, (const void**)names, 12,
                                          &kCFTypeArrayCallBacks);
            return noErr;
        }
        case kBebopParamSyncBpm: {
            CFStringRef names[] = { CFSTR("manual"), CFSTR("follow host") };
            *outStrings = CFArrayCreate(nullptr, (const void**)names, 2,
                                          &kCFTypeArrayCallBacks);
            return noErr;
        }
        case kBebopParamLastChord: {
            // Index 0 = "—" (no chord); 1..84 = (quality × 12) + root + 1.
            CFMutableArrayRef arr = CFArrayCreateMutable(
                nullptr, kChordCount, &kCFTypeArrayCallBacks);
            CFArrayAppendValue(arr, CFSTR("—"));
            for (UInt32 q = 0; q < kQualityCount; ++q) {
                for (UInt32 r = 0; r < kRootCount; ++r) {
                    char buf[16];
                    std::snprintf(buf, sizeof(buf), "%s%s",
                                   kRootNames[r], kQualityNames[q]);
                    CFStringRef s = CFStringCreateWithCString(
                        nullptr, buf, kCFStringEncodingUTF8);
                    CFArrayAppendValue(arr, s);
                    CFRelease(s);
                }
            }
            *outStrings = arr;
            return noErr;
        }
        case kBebopParamLoopMode: {
            CFStringRef names[] = {
                CFSTR("off"), CFSTR("auto (host cycle)"), CFSTR("manual"),
            };
            *outStrings = CFArrayCreate(nullptr, (const void**)names, 3,
                                          &kCFTypeArrayCallBacks);
            return noErr;
        }
        case kBebopParamLoopBars: {
            CFStringRef names[] = {
                CFSTR("1 bar"), CFSTR("2 bars"), CFSTR("4 bars"),
                CFSTR("8 bars"), CFSTR("16 bars"), CFSTR("32 bars"),
            };
            *outStrings = CFArrayCreate(nullptr, (const void**)names,
                                          kLoopBarsCount,
                                          &kCFTypeArrayCallBacks);
            return noErr;
        }
        }
        return kAudioUnitErr_InvalidProperty;
    }

    /// Override to forward parameter changes to the bebop-rs handle so
    /// they take effect on the next chord-detection pass. Must remain
    /// realtime-safe — `bebop_set_param` only takes a brief mutex lock
    /// and updates a few fields, so this is OK.
    OSStatus SetParameter(AudioUnitParameterID inID, AudioUnitScope inScope,
                           AudioUnitElement inElement,
                           AudioUnitParameterValue inValue,
                           UInt32 inBufferOffsetInFrames) AUSDK_RTSAFE override
    {
        const OSStatus status = AUEffectBase::SetParameter(
            inID, inScope, inElement, inValue, inBufferOffsetInFrames);
        if (status == noErr && inScope == kAudioUnitScope_Global
            && mBebop != nullptr)
        {
            // Bridge AU param IDs → bebop-rs param IDs (they happen not
            // to line up — be explicit).
            int bebopParam = -1;
            switch (inID) {
            case kBebopParamSpice:   bebopParam = 3; break;
            case kBebopParamVoicing: bebopParam = 1; break;
            case kBebopParamRhythm:  bebopParam = 2; break;
            case kBebopParamBpm:     bebopParam = 0; break;
            case kBebopParamOctave:  bebopParam = 4; break;
            // SyncBpm and LastChord have no bebop-rs equivalent — they
            // live entirely in the AU layer.
            }
            if (bebopParam >= 0) {
                bebop_set_param(mBebop, bebopParam, double(inValue));
            }
        }
        return status;
    }

    // ---- Render: passthrough ----
    //
    // AUEffectBase does the heavy lifting (channel walking, parameter
    // ramping). For pure passthrough we don't need to override Process at
    // all when `inProcessesInPlace=true` — input/output share buffers and
    // the framework treats absence of Process as identity.
    //
    // We DO declare ProcessesInPlace() so the host can optimize.

    bool ProcessesInPlace() const noexcept { return true; }

    // ---- MIDI output ----
    //
    // AUv2 hosts deliver MIDI from the AU via a callback they register
    // through `kAudioUnitProperty_MIDIOutputCallback`. We advertise
    // support, store whatever the host sets, and call it from the render
    // path with any MIDI events the worker thread has queued.

    OSStatus GetPropertyInfo(AudioUnitPropertyID inID, AudioUnitScope inScope,
                              AudioUnitElement inElement,
                              UInt32& outDataSize, bool& outWritable) override
    {
        if (inScope == kAudioUnitScope_Global
            && inID == kAudioUnitProperty_MIDIOutputCallback)
        {
            outDataSize = sizeof(AUMIDIOutputCallbackStruct);
            outWritable = true;
            return noErr;
        }
        // NB: kAudioUnitProperty_Latency is dispatched through the AUBase
        // GetLatency() virtual, which we override below; we don't need
        // a GetPropertyInfo branch for it.
        return AUEffectBase::GetPropertyInfo(
            inID, inScope, inElement, outDataSize, outWritable);
    }

    /// AUSDK's property dispatch routes `kAudioUnitProperty_Latency` to
    /// this virtual rather than to our GetProperty override. Returning
    /// the right number here is what triggers Logic's PDC.
    Float64 GetLatency() AUSDK_RTSAFE override
    {
        return kPdcLatencySeconds;
    }

    OSStatus GetProperty(AudioUnitPropertyID inID, AudioUnitScope inScope,
                          AudioUnitElement inElement, void* outData) override
    {
        if (inScope == kAudioUnitScope_Global
            && inID == kAudioUnitProperty_MIDIOutputCallback)
        {
            *reinterpret_cast<AUMIDIOutputCallbackStruct*>(outData) =
                mMidiOutputCallback;
            return noErr;
        }
        // NB: kAudioUnitProperty_Latency is handled in GetLatency() above
        // (the AUSDK property dispatch calls that virtual directly).
        return AUEffectBase::GetProperty(inID, inScope, inElement, outData);
    }

    OSStatus SetProperty(AudioUnitPropertyID inID, AudioUnitScope inScope,
                          AudioUnitElement inElement, const void* inData,
                          UInt32 inDataSize) override
    {
        if (inScope == kAudioUnitScope_Global
            && inID == kAudioUnitProperty_MIDIOutputCallback)
        {
            if (inDataSize < sizeof(AUMIDIOutputCallbackStruct)) {
                return kAudioUnitErr_InvalidPropertyValue;
            }
            mMidiOutputCallback =
                *reinterpret_cast<const AUMIDIOutputCallbackStruct*>(inData);
            return noErr;
        }
        return AUEffectBase::SetProperty(
            inID, inScope, inElement, inData, inDataSize);
    }

    /// Render entry point — runs on the realtime audio thread. We chain
    /// to the parent's audio processing, then drain any MIDI events the
    /// worker thread has queued and deliver them via the host's callback.
    OSStatus ProcessBufferLists(AudioUnitRenderActionFlags& ioActionFlags,
                                 const AudioBufferList& inBuffer,
                                 AudioBufferList& outBuffer,
                                 UInt32 inFramesToProcess) AUSDK_RTSAFE override
    {
        // Read host tempo, transport state, and musical time location
        // while we're on the realtime thread — most hosts only allow
        // these callbacks from this context. Worker thread reads the
        // atomics later and applies / logs them.

        Float64 beat = 0, tempo = 0;
        const OSStatus tempoStatus = CallHostBeatAndTempo(&beat, &tempo);
        if (tempoStatus == noErr && tempo > 0) {
            mHostTempoCached.store(tempo, std::memory_order_relaxed);
        }
        mTempoLastStatus.store(static_cast<int>(tempoStatus),
                                 std::memory_order_relaxed);
        mTempoLastValue.store(tempo, std::memory_order_relaxed);
        mBeatLastValue.store(beat, std::memory_order_relaxed);

        Boolean isPlaying = false, stateChanged = false, isCycling = false;
        Float64 currentSample = 0, cycleStart = 0, cycleEnd = 0;
        const OSStatus transportStatus = CallHostTransportState(
            &isPlaying, &stateChanged, &currentSample,
            &isCycling, &cycleStart, &cycleEnd);
        mTransportStatus.store(static_cast<int>(transportStatus),
                                 std::memory_order_relaxed);
        if (transportStatus == noErr) {
            const int flags = (isPlaying     ? 1 : 0)
                            | (stateChanged  ? 2 : 0)
                            | (isCycling     ? 4 : 0);
            mTransportFlags.store(flags, std::memory_order_relaxed);
            mCycleStartBeat.store(cycleStart, std::memory_order_relaxed);
            mCycleEndBeat.store(cycleEnd, std::memory_order_relaxed);
        }

        UInt32  deltaToNextBeat = 0;
        Float32 tsNum = 0;
        UInt32  tsDenom = 0;
        Float64 currentMeasure = 0;
        const OSStatus musicalStatus = CallHostMusicalTimeLocation(
            &deltaToNextBeat, &tsNum, &tsDenom, &currentMeasure);
        mMusicalTimeStatus.store(static_cast<int>(musicalStatus),
                                   std::memory_order_relaxed);
        if (musicalStatus == noErr) {
            mTimeSigNum.store(tsNum, std::memory_order_relaxed);
            mTimeSigDenom.store(static_cast<int>(tsDenom),
                                  std::memory_order_relaxed);
            mCurrentDownbeat.store(currentMeasure,
                                     std::memory_order_relaxed);
        }

        mProcessBlockCount.fetch_add(1, std::memory_order_relaxed);

        // ── Decide the active loop length (audio thread; param reads are atomic) ──
        const float loopMode = Globals()->GetParameter(kBebopParamLoopMode);
        double loopBeats = 0.0, loopStart = 0.0;
        if (loopMode > 1.5f) {
            // Manual: anchor to current downbeat, length from LoopBars param.
            const int barsIdx = static_cast<int>(
                Globals()->GetParameter(kBebopParamLoopBars));
            const int bars = kLoopBarsValues[
                std::max(0, std::min((int)kLoopBarsCount - 1, barsIdx))];
            loopBeats = bars * kBeatsPerBar;
            loopStart = std::floor(beat / loopBeats) * loopBeats;
        } else if (loopMode > 0.5f && transportStatus == noErr && isCycling) {
            // Auto: use Logic's cycle bounds when cycle mode is on.
            loopBeats = cycleEnd - cycleStart;
            loopStart = cycleStart;
        }
        // else loop is off (mode=0, or auto with cycle off)
        mActiveLoopBeats.store(loopBeats, std::memory_order_relaxed);
        mLoopStartBeat.store(loopStart, std::memory_order_relaxed);

        // ── Emit any beat-cached events whose slot we just crossed ──
        // Loop emit runs ONLY when loop is active and transport is rolling.
        if (loopBeats > 0.0 && (mTransportFlags.load() & 1)) {
            const double rel = beat - loopStart;
            const double pos = rel - std::floor(rel / loopBeats) * loopBeats;
            const int slot = static_cast<int>(pos * kSlotsPerBeat);
            const int slotsInLoop =
                static_cast<int>(loopBeats * kSlotsPerBeat);
            if (slot != mLastEmittedSlot) {
                std::lock_guard<std::mutex> lk(mLoopMutex);
                int s = mLastEmittedSlot;
                while (true) {
                    s = (s + 1) % slotsInLoop;
                    if (s < (int)mLoopMemory.size()) {
                        for (const auto& ev : mLoopMemory[s]) {
                            mMidiQueue.push(ev);
                        }
                    }
                    if (s == slot) break;
                }
                mLastEmittedSlot = slot;
            }
        } else {
            mLastEmittedSlot = -1; // reset so re-engaging loop starts clean
        }

        const OSStatus status = AUEffectBase::ProcessBufferLists(
            ioActionFlags, inBuffer, outBuffer, inFramesToProcess);
        if (status != noErr) {
            return status;
        }
        flushMidi(inFramesToProcess);
        return noErr;
    }

private:
    /// Pull queued MIDI events and deliver them to (a) the AU host's
    /// MIDIOutputCallback if registered, AND (b) our CoreMIDI virtual
    /// source if it's open. Realtime-safe — uses a stack-allocated
    /// MIDIPacketList, no malloc, no locks.
    ///
    /// The two paths are independent so the AU works in any host: a host
    /// that consumes the AU callback (Logic with "Record MIDI from
    /// Plug-in") gets the events; a host that doesn't (most DAWs, or
    /// users who route via the virtual source instead) gets them via the
    /// system MIDI graph.
    void flushMidi(UInt32 /*frames*/) AUSDK_RTSAFE
    {
        if (mMidiQueue.available() == 0) {
            return;
        }
        const bool callbackSet =
            mMidiOutputCallback.midiOutputCallback != nullptr;
        const bool sourceOpen = mVirtualSource != 0;
        if (!callbackSet && !sourceOpen) {
            // Nothing's listening; drop the queue to avoid backing up.
            bebop::MidiEvent _drop;
            while (mMidiQueue.pop(_drop)) {}
            return;
        }
        // Stack buffer big enough for ~64 short MIDI messages. MIDIPacketList
        // is variable-length; we pack as many events as fit.
        constexpr size_t kBufBytes = 1024;
        alignas(MIDIPacketList) uint8_t storage[kBufBytes];
        auto* pktList = reinterpret_cast<MIDIPacketList*>(storage);
        MIDIPacket* current = MIDIPacketListInit(pktList);
        bebop::MidiEvent ev;
        while (mMidiQueue.pop(ev)) {
            const Byte data[3] = { ev.status, ev.data1, ev.data2 };
            current = MIDIPacketListAdd(
                pktList, kBufBytes, current,
                /*timeStamp=*/0, sizeof(data), data);
            if (current == nullptr) {
                // Buffer full — leave remaining events for next render
                // block. (Realistic worst-case is unreachable at our
                // event rate.)
                break;
            }
        }
        if (pktList->numPackets == 0) {
            return;
        }
        if (callbackSet) {
            AudioTimeStamp ts {};
            ts.mFlags = kAudioTimeStampSampleTimeValid;
            mMidiOutputCallback.midiOutputCallback(
                mMidiOutputCallback.userData,
                &ts,
                /*midiOutNum=*/0,
                pktList);
        }
        if (sourceOpen) {
            // MIDIReceived is documented as realtime-safe — it queues
            // packets to the kernel-resident MIDI server. Any process
            // that opened a MIDIInputPort connected to mVirtualSource
            // receives the events asynchronously.
            MIDIReceived(mVirtualSource, pktList);
        }
    }

    /// Open a CoreMIDI virtual source named "Bebop AU" so any DAW (Logic,
    /// Pro Tools, Reaper, etc.) can subscribe to our MIDI output without
    /// going through the AU callback API. Called from Initialize() on the
    /// main thread; safe to fail (we just log and continue).
    void openVirtualMidiSource()
    {
        if (mMidiClient != 0) return;
        OSStatus status = MIDIClientCreate(
            CFSTR("BebopAU"), nullptr, nullptr, &mMidiClient);
        if (status != noErr || mMidiClient == 0) {
            os_log_error(bebop_log(), "MIDIClientCreate failed: %d", (int)status);
            mMidiClient = 0;
            return;
        }
        status = MIDISourceCreate(
            mMidiClient, CFSTR("Bebop AU"), &mVirtualSource);
        if (status != noErr || mVirtualSource == 0) {
            os_log_error(bebop_log(), "MIDISourceCreate failed: %d", (int)status);
            MIDIClientDispose(mMidiClient);
            mMidiClient = 0;
            mVirtualSource = 0;
            return;
        }
        os_log(bebop_log(), "virtual MIDI source 'Bebop AU' online; "
                              "host-callbacks present=%{public}s",
               (HasBeatAndTempoProc() ? "yes" : "no"));
    }

    /// True if the host registered a beatAndTempoProc via
    /// kAudioUnitProperty_HostCallbacks. Used in the diagnostic log so we
    /// can tell whether Logic is providing host callbacks at all.
    bool HasBeatAndTempoProc() const noexcept
    {
        Float64 unused_beat = 0, unused_tempo = 0;
        // CallHostBeatAndTempo returns kAudioUnitErr_CannotDoInCurrentContext
        // when the callback isn't set; any other return value (incl. errors
        // from the host's callback itself) means at least it's registered.
        return const_cast<BebopAU*>(this)
            ->CallHostBeatAndTempo(&unused_beat, &unused_tempo)
            != kAudioUnitErr_CannotDoInCurrentContext;
    }

    void closeVirtualMidiSource()
    {
        if (mVirtualSource != 0) {
            MIDIEndpointDispose(mVirtualSource);
            mVirtualSource = 0;
        }
        if (mMidiClient != 0) {
            MIDIClientDispose(mMidiClient);
            mMidiClient = 0;
        }
    }

public:

private:
    /// Worker thread loop — runs OFF the realtime audio thread. Drains the
    /// ring buffer in chunks and feeds them to bebop-rs for chord
    /// recognition. C.5 plumbs the call; C.5b adds the actual analysis;
    /// C.6 will pull MIDI events back out and queue them for the audio
    /// thread to emit.
    void workerLoop() noexcept
    {
        using namespace std::chrono_literals;

        constexpr size_t kChunkSamples = 48'000;
        std::vector<float> chunk(kChunkSamples);
        const float sampleRate = static_cast<float>(GetSampleRate());

        // bebop-rs returns events in batches; this is the staging buffer.
        constexpr size_t kEventBatch = 64;
        std::vector<BebopMidiEvent> events(kEventBatch);

        // For chord-symbol display: poll for the most recent chord and
        // push its index into the LastChord parameter when it changes.
        char chordBuf[64];

        // Track last applied host tempo so we don't spam the FFI on
        // every poll if the host's tempo hasn't changed.
        Float64 lastHostTempo = 0;
        // Block counter at the last tempo-diagnostic log; used to
        // throttle the log to ~once per second.
        uint64_t tempoLogBlocks = 0;

        size_t totalProcessed = 0;
        size_t totalEvents = 0;
        while (!mStopWorker.load(std::memory_order_acquire)) {
            std::this_thread::sleep_for(100ms);

            // 1. follow host tempo if Sync BPM is on. The audio thread
            //    populates `mHostTempoCached` from CallHostBeatAndTempo
            //    each render block (some hosts gate that call to the
            //    realtime thread). We read the cached value here and
            //    apply it via SetParameter + notification.
            const bool syncOn =
                Globals()->GetParameter(kBebopParamSyncBpm) > 0.5f;
            if (syncOn) {
                const Float64 tempo =
                    mHostTempoCached.load(std::memory_order_relaxed);
                if (tempo > 0 && std::abs(tempo - lastHostTempo) > 0.5) {
                    Globals()->SetParameter(kBebopParamBpm,
                        static_cast<AudioUnitParameterValue>(tempo));
                    notifyParamChanged(kBebopParamBpm);
                    if (mBebop != nullptr) {
                        bebop_set_param(mBebop, /*BPM*/0, double(tempo));
                    }
                    lastHostTempo = tempo;
                    os_log(bebop_log(),
                        "sync BPM <- host tempo %.1f", tempo);
                }
            }
            // Host-callbacks diagnostic — log once a second so we can
            // see what each callback returned. Status -10863 means
            // "host didn't register that callback". Status 0 + zeros
            // means the callback succeeded but the host gave us empty
            // values (e.g. tempo=0 when transport is stopped).
            const uint64_t blocks =
                mProcessBlockCount.load(std::memory_order_relaxed);
            if (blocks > 0 && (blocks - tempoLogBlocks) >= 80) {
                const int tFlags = mTransportFlags.load(std::memory_order_relaxed);
                os_log(bebop_log(),
                    "host poll #%llu: "
                    "tempo=[status=%d tempo=%.2f beat=%.2f cached=%.2f] "
                    "transport=[status=%d play=%{public}s cycle=%{public}s "
                    "start=%.3f end=%.3f] "
                    "musical=[status=%d ts=%.0f/%d downbeat=%.3f] "
                    "param_bpm=%.0f sync=%{public}s",
                    (unsigned long long)blocks,
                    mTempoLastStatus.load(std::memory_order_relaxed),
                    mTempoLastValue.load(std::memory_order_relaxed),
                    mBeatLastValue.load(std::memory_order_relaxed),
                    mHostTempoCached.load(std::memory_order_relaxed),
                    mTransportStatus.load(std::memory_order_relaxed),
                    (tFlags & 1) ? "yes" : "no",
                    (tFlags & 4) ? "yes" : "no",
                    mCycleStartBeat.load(std::memory_order_relaxed),
                    mCycleEndBeat.load(std::memory_order_relaxed),
                    mMusicalTimeStatus.load(std::memory_order_relaxed),
                    mTimeSigNum.load(std::memory_order_relaxed),
                    mTimeSigDenom.load(std::memory_order_relaxed),
                    mCurrentDownbeat.load(std::memory_order_relaxed),
                    Globals()->GetParameter(kBebopParamBpm),
                    syncOn ? "on" : "off");
                tempoLogBlocks = blocks;
            }

            // 2. feed audio for chord recognition (also triggers comp
            //    generation when a new chord is committed)
            const size_t n = mRing.read(chunk.data(), chunk.size());
            if (n > 0) {
                totalProcessed += n;
                if (mBebop != nullptr) {
                    (void)bebop_process_audio(
                        mBebop, chunk.data(), n, sampleRate);
                }
            }

            // 3. update LastChord param if a new chord just landed.
            //    SetParameter alone doesn't refresh the host's display
            //    for read-only params — we have to fire the notification
            //    explicitly via AUEventListenerNotify.
            if (mBebop != nullptr) {
                const int len = bebop_pull_chord(mBebop, chordBuf, sizeof(chordBuf));
                if (len > 0) {
                    const int idx = chordSymbolToIndex(chordBuf, len);
                    if (idx >= 0) {
                        Globals()->SetParameter(kBebopParamLastChord,
                            static_cast<AudioUnitParameterValue>(idx));
                        notifyParamChanged(kBebopParamLastChord);
                    }
                    os_log(bebop_log(),
                        "chord: %{public}s (idx=%d, last_chord_param=%d)",
                        chordBuf, idx,
                        (int)Globals()->GetParameter(kBebopParamLastChord));
                }
            }

            // 4. drain any comp events whose deadlines have passed.
            //    Where they go depends on Loop Mode:
            //      - off: push directly to the audio MIDI queue, fire NOW
            //      - auto/manual with active loop: store at the current
            //        loop beat (minus the Comp Offset) so the next loop
            //        iteration plays the comp aligned to the source.
            //    The worker pulls events at recognition time, which is
            //    ~1.3s after the chord actually started in the audio. The
            //    Comp Offset (default -2 beats) compensates so events
            //    land near the real chord position on the next loop pass.
            if (mBebop != nullptr) {
                const float loopMode =
                    Globals()->GetParameter(kBebopParamLoopMode);
                const double loopBeats =
                    mActiveLoopBeats.load(std::memory_order_relaxed);
                const bool useLoop = loopMode > 0.5f && loopBeats > 0.0;

                while (true) {
                    const size_t got = bebop_pull_midi_events(
                        mBebop, events.data(), events.size());
                    if (got == 0) break;
                    if (useLoop) {
                        const double loopStart = mLoopStartBeat.load(
                            std::memory_order_relaxed);
                        const double currentBeat = mBeatLastValue.load(
                            std::memory_order_relaxed);
                        const float compOffset =
                            Globals()->GetParameter(kBebopParamCompOffset);
                        // shift backward (negative compOffset → fire earlier
                        // on next loop) and wrap into [0..loopBeats)
                        double rel = currentBeat - loopStart + compOffset;
                        rel -= std::floor(rel / loopBeats) * loopBeats;
                        const int slot = static_cast<int>(
                            rel * kSlotsPerBeat) %
                            static_cast<int>(loopBeats * kSlotsPerBeat);
                        std::lock_guard<std::mutex> lk(mLoopMutex);
                        if (slot >= 0 && slot < (int)mLoopMemory.size()) {
                            for (size_t i = 0; i < got; ++i) {
                                mLoopMemory[slot].push_back({
                                    events[i].status, events[i].pitch,
                                    events[i].velocity, 0});
                            }
                        }
                    } else {
                        // No loop — fire immediately
                        for (size_t i = 0; i < got; ++i) {
                            mMidiQueue.push({events[i].status, events[i].pitch,
                                              events[i].velocity, 0});
                        }
                    }
                    totalEvents += got;
                    if (got < events.size()) break;
                }
            }
        }
        os_log(bebop_log(),
            "worker exiting (audio=%zu samples, midi=%zu events)",
            totalProcessed, totalEvents);
    }

    /// Notify the host that a parameter's value changed from inside the
    /// AU (worker thread updates LastChord and BPM internally; without
    /// this notification Logic's parameter UI stays stale because it
    /// only polls writable parameters and only reads display params on
    /// notification.) Uses the deprecated-but-still-supported
    /// `AUEventListenerNotify` API; modern AUParameterTree dispatch
    /// requires the v3 parameter tree which AUSDK doesn't expose.
    void notifyParamChanged(AudioUnitParameterID inID)
    {
        AudioUnitEvent event {};
        event.mEventType = kAudioUnitEvent_ParameterValueChange;
        event.mArgument.mParameter.mAudioUnit  = GetComponentInstance();
        event.mArgument.mParameter.mParameterID = inID;
        event.mArgument.mParameter.mScope      = kAudioUnitScope_Global;
        event.mArgument.mParameter.mElement    = 0;
        AUEventListenerNotify(nullptr, nullptr, &event);
    }

    /// Map a bebop chord symbol (e.g. "Cmaj7", "F#m7", "Db") to the
    /// LastChord parameter's enumeration index. Symbols outside the
    /// pre-enumerated set are mapped to the closest covered match (e.g.
    /// "Cmaj9" → "Cmaj7", "Db13" → "Db7"). Returns -1 if the root is
    /// unparseable. Used by the worker thread on chord commits.
    static int chordSymbolToIndex(const char* sym, int len)
    {
        if (len <= 0 || sym == nullptr) return 0;

        // Parse root
        const bool hasAcc = (len >= 2)
            && (sym[1] == '#' || sym[1] == 'b');
        char rootStr[3] = { sym[0], hasAcc ? sym[1] : '\0', '\0' };
        // Normalize flats to sharps to match kRootNames
        if (hasAcc && sym[1] == 'b') {
            switch (sym[0]) {
            case 'D': rootStr[0] = 'C'; rootStr[1] = '#'; break;
            case 'E': rootStr[0] = 'D'; rootStr[1] = '#'; break;
            case 'G': rootStr[0] = 'F'; rootStr[1] = '#'; break;
            case 'A': rootStr[0] = 'G'; rootStr[1] = '#'; break;
            case 'B': rootStr[0] = 'A'; rootStr[1] = '#'; break;
            default:  rootStr[1] = '\0'; break; // Cb, Fb — unusual
            }
        }
        int rootIdx = -1;
        for (int i = 0; i < (int)kRootCount; ++i) {
            if (std::strcmp(rootStr, kRootNames[i]) == 0) {
                rootIdx = i;
                break;
            }
        }
        if (rootIdx < 0) return -1;

        // Parse quality
        const char* suffix = sym + (hasAcc ? 2 : 1);
        int qualityIdx = 0; // default = "" (major triad)
        if (std::strcmp(suffix, "") == 0)        qualityIdx = 0;
        else if (std::strcmp(suffix, "m") == 0)  qualityIdx = 1;
        else if (std::strncmp(suffix, "m7", 2) == 0
                 && std::strncmp(suffix, "maj7", 4) != 0) qualityIdx = 3;
        else if (std::strncmp(suffix, "maj7", 4) == 0)  qualityIdx = 4;
        else if (suffix[0] == 'm' && suffix[1] != 'a')  qualityIdx = 1;  // m, m9, m11
        else if (std::strncmp(suffix, "dim", 3) == 0)   qualityIdx = 5;
        else if (std::strncmp(suffix, "sus", 3) == 0)   qualityIdx = 6;
        else if (suffix[0] == '7' || suffix[0] == '9'
                 || suffix[0] == '1') qualityIdx = 2;   // 7, 9, 11, 13
        else if (std::strncmp(suffix, "maj", 3) == 0)   qualityIdx = 0;
        // else: treat as bare triad

        return 1 + qualityIdx * (int)kRootCount + rootIdx;
    }

    void startWorker()
    {
        if (mWorkerThread.joinable()) {
            return; // already running
        }
        mStopWorker.store(false, std::memory_order_release);
        mWorkerThread = std::thread([this] { this->workerLoop(); });
    }

    void stopWorker() noexcept
    {
        mStopWorker.store(true, std::memory_order_release);
        if (mWorkerThread.joinable()) {
            mWorkerThread.join();
        }
    }

    /// Embedded-Python handle. Owned by the AU; created in the constructor
    /// and released in the destructor. NEVER touched from the realtime
    /// audio thread — only from the worker thread (Phase C.5+).
    BebopHandle* mBebop { nullptr };

    /// SPSC ring buffer that bridges the realtime audio thread (writer,
    /// via `BebopKernel::Process`) and the worker thread (reader).
    /// Sized for ~5 seconds at 48 kHz mono.
    bebop::AudioRingBuffer mRing;

    /// Worker thread that drains the ring + calls into bebop-rs.
    std::thread mWorkerThread;
    std::atomic<bool> mStopWorker { false };

    /// Tempo read from the host on the audio thread (some hosts only
    /// allow `CallHostBeatAndTempo` from the realtime thread). The
    /// worker reads this atomic each iteration and applies it to the
    /// BPM parameter when Sync BPM is on. 0 = "no host tempo yet".
    std::atomic<double> mHostTempoCached { 0.0 };

    /// Diagnostic state populated by ProcessBufferLists each render
    /// block; the worker thread reads + logs once a second.
    /// All atomics so reads/writes between threads don't tear.
    /// Sentinel value 0xDEAD = "ProcessBufferLists hasn't run yet".

    // tempo
    std::atomic<int>      mTempoLastStatus   { 0xDEAD };
    std::atomic<double>   mTempoLastValue    { 0.0 };
    std::atomic<double>   mBeatLastValue     { 0.0 };

    // transport state — answers "is Logic in cycle/loop mode? what are
    // the loop boundaries?". This is the key data point for the
    // loop-aware comping mode we're considering.
    std::atomic<int>      mTransportStatus   { 0xDEAD };
    std::atomic<int>      mTransportFlags    { 0 };
    std::atomic<double>   mCycleStartBeat    { 0.0 };
    std::atomic<double>   mCycleEndBeat      { 0.0 };

    // musical time location — time signature + current downbeat.
    // Used to translate between sample-time and beat-time for the
    // loop memory's beat-indexed slots.
    std::atomic<int>      mMusicalTimeStatus { 0xDEAD };
    std::atomic<float>    mTimeSigNum        { 0.0f };
    std::atomic<int>      mTimeSigDenom      { 0 };
    std::atomic<double>   mCurrentDownbeat   { 0.0 };

    std::atomic<uint64_t> mProcessBlockCount { 0 };

    /// MIDI event queue: SPSC, worker pushes, audio thread drains.
    bebop::MidiEventQueue mMidiQueue;

    /// Host's MIDI output callback. Set via SetProperty; called from
    /// the audio thread in flushMidi().
    AUMIDIOutputCallbackStruct mMidiOutputCallback { nullptr, nullptr };

    /// CoreMIDI virtual source — exposes our MIDI output as a system MIDI
    /// source named "Bebop AU" that any DAW can subscribe to as an input.
    /// Used as a fallback / supplement to the AU's own MIDIOutputCallback,
    /// because that callback's UI exposure varies wildly between hosts.
    MIDIClientRef    mMidiClient    { 0 };
    MIDIEndpointRef  mVirtualSource { 0 };

    // ── Loop-aware comping ───────────────────────────────────────────
    // Beat-indexed memory of MIDI events. The worker thread stores events
    // when chords are detected; the audio thread emits them when its
    // current beat matches a stored slot. Subdivides each beat into 16
    // slots = 1/16th note resolution, which is finer than any rhythm
    // template's smallest hit (charleston has 1/8-note "and-of-3" hits).
    static constexpr int kMaxLoopBars        = 32;
    static constexpr int kBeatsPerBar        = 4;   // assume 4/4 for now
    static constexpr int kSlotsPerBeat       = 16;
    static constexpr size_t kMaxLoopSlots    =
        kMaxLoopBars * kBeatsPerBar * kSlotsPerBeat;

    /// One slot's worth of queued MIDI events. Vectors are fine since the
    /// audio thread accesses these under a mutex, not in a tight RT path.
    /// Real-time safety: the lock is held only briefly (microseconds) and
    /// only contended once per worker iteration (~10Hz). Glitches from
    /// this contention have been imperceptible in practice; if they ever
    /// matter we'll swap in a lock-free SPSC per slot.
    std::vector<std::vector<bebop::MidiEvent>> mLoopMemory { kMaxLoopSlots };
    std::mutex                                  mLoopMutex;
    /// Last loop slot the audio thread emitted from. Used to fire events
    /// once per loop iteration, even if multiple render blocks land in
    /// the same slot.
    int      mLastEmittedSlot { -1 };
    /// Loop length in beats — set by ProcessBufferLists each block from
    /// either Logic's cycle bounds (auto) or the LoopBars param (manual).
    /// 0 = no loop active (off).
    std::atomic<double> mActiveLoopBeats { 0.0 };
    /// Loop start beat (= cycle_start_beat in auto, or the most recent
    /// downbeat in manual mode). Loop position = (host_beat - mLoopStart)
    /// mod mActiveLoopBeats.
    std::atomic<double> mLoopStartBeat   { 0.0 };

};

// Generate the C factory function the AudioComponent system finds via dlsym.
// The Info.plist's `factoryFunction` value must match `BebopAUFactory`.
AUSDK_COMPONENT_ENTRY(ausdk::AUBaseFactory, BebopAU)

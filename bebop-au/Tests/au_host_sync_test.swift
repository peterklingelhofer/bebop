// SPDX-License-Identifier: MIT
//
// Host-simulator test for Rhythm Sync timing.
//
// Loads the bebop AU, registers fake host callbacks providing a known
// transport rolling at a fixed BPM, drives renders with the ii-V-I
// fixture audio, and verifies that MIDI emit timestamps land at expected
// host-beat positions for the selected rhythm template.
//
// This is the "test without Logic" for sync timing — instead of opening
// Logic and listening for off-grid hits, we know objectively the host's
// beat at every render and we know the precise mach time of every MIDI
// packet. We can compute "host beat at packet fire time" exactly and
// assert it lands within tolerance of the rhythm template's offsets.

import AudioToolbox
import CoreMIDI
import Foundation
import Darwin

// ─── Fixed test config ─────────────────────────────────────────────────────

let kSampleRate: Double = 22_050   // matches fixture
let kBpm: Double = 100             // matches fixture
let kFrames: UInt32 = 512
let kRhythmIdx: Float = 3          // charleston (offsets 0.0 + 2.5)
let kRhythmName: String = "charleston"
// Charleston hit positions within a 4-beat bar
let kExpectedOffsets: [Double] = [0.0, 2.5]
// Tolerance: ~13 ms at 94 BPM. Music-perceptual threshold + real-time
// pacing slack (Thread.sleep precision is ~1 ms on macOS). The bugs
// we're catching here (Logic timestamp-reference mismatch) caused
// 2+ beat offsets, well within signal range even with a loose
// tolerance
let kBeatTolerance: Double = 0.02

@inline(__always) func fcc(_ s: String) -> OSType {
    var v: OSType = 0
    for ch in s.utf8.prefix(4) { v = (v << 8) | OSType(ch) }
    return v
}

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(Data("FAIL: \(msg)\n".utf8))
    exit(1)
}

// ─── Mach time helpers ─────────────────────────────────────────────────────

var sTimebase = mach_timebase_info_data_t()
mach_timebase_info(&sTimebase)
let kNsPerMach = Double(sTimebase.numer) / Double(sTimebase.denom)

func machToNs(_ m: UInt64) -> Double { Double(m) * kNsPerMach }
func nsToMach(_ ns: Double) -> UInt64 { UInt64(ns / kNsPerMach) }

// ─── Simulated host state ──────────────────────────────────────────────────
//
// Beat is computed from sample-time + tempo so it never drifts from the
// "wall clock" the test imposes via inTimeStamp.

final class HostState {
    var sampleRate: Double = kSampleRate
    var bpm: Double = kBpm
    var startSample: Double = 0   // first frame of the CURRENT block
    var isPlaying: Bool = true
    var hostMachAtSample0: UInt64 = 0  // wall-clock when sample 0 plays

    /// Project-bar offset. When non-zero, the simulated host's
    /// `currentMeasureDownBeat` reports a value shifted by this amount,
    /// modeling DAWs (Logic with PDC) whose downbeat report doesn't
    /// align with master beat 0
    var downbeatOffset: Double = 0.0

    var beat: Double { startSample / sampleRate * bpm / 60 }

    /// Mach time of `startSample` (the current block's first frame).
    /// MUST match what we pass as inTimeStamp.mHostTime to AudioUnitRender
    var hostTime: UInt64 {
        let nsSinceSample0 = startSample / sampleRate * 1e9
        return hostMachAtSample0 + nsToMach(nsSinceSample0)
    }
}

let host = HostState()
host.hostMachAtSample0 = mach_absolute_time()
let hostRefRetained = Unmanaged.passRetained(host)
defer { hostRefRetained.release() }
let hostRef = hostRefRetained.toOpaque()

// ─── Host callbacks ────────────────────────────────────────────────────────

let beatAndTempoProc: @convention(c) (
    UnsafeMutableRawPointer?, UnsafeMutablePointer<Float64>?,
    UnsafeMutablePointer<Float64>?
) -> OSStatus = { ud, outBeat, outTempo in
    guard let ud = ud else { return -50 }
    let h = Unmanaged<HostState>.fromOpaque(ud).takeUnretainedValue()
    outBeat?.pointee = h.beat
    outTempo?.pointee = h.bpm
    return noErr
}

let transportStateProc: @convention(c) (
    UnsafeMutableRawPointer?, UnsafeMutablePointer<DarwinBoolean>?,
    UnsafeMutablePointer<DarwinBoolean>?, UnsafeMutablePointer<Float64>?,
    UnsafeMutablePointer<DarwinBoolean>?, UnsafeMutablePointer<Float64>?,
    UnsafeMutablePointer<Float64>?
) -> OSStatus = { ud, isPlaying, changed, sampleInTL, isCycling, cycleStart, cycleEnd in
    guard let ud = ud else { return -50 }
    let h = Unmanaged<HostState>.fromOpaque(ud).takeUnretainedValue()
    isPlaying?.pointee = DarwinBoolean(h.isPlaying)
    changed?.pointee = false
    sampleInTL?.pointee = h.startSample
    isCycling?.pointee = false
    cycleStart?.pointee = 0
    cycleEnd?.pointee = 0
    return noErr
}

let musicalProc: @convention(c) (
    UnsafeMutableRawPointer?, UnsafeMutablePointer<UInt32>?,
    UnsafeMutablePointer<Float32>?, UnsafeMutablePointer<UInt32>?,
    UnsafeMutablePointer<Float64>?
) -> OSStatus = { ud, deltaToNext, tsNum, tsDenom, downbeat in
    guard let ud = ud else { return -50 }
    let h = Unmanaged<HostState>.fromOpaque(ud).takeUnretainedValue()
    deltaToNext?.pointee = 0
    tsNum?.pointee = 4
    tsDenom?.pointee = 4
    // Report the current bar's downbeat in MASTER-BEAT coordinates,
    // optionally shifted by `downbeatOffset` to simulate hosts with
    // a non-zero project-bar offset (e.g. Logic with PDC)
    let effectiveBeat = h.beat - h.downbeatOffset
    let bar = floor(effectiveBeat / 4) * 4 + h.downbeatOffset
    downbeat?.pointee = bar
    return noErr
}

// ─── Find + open AU ────────────────────────────────────────────────────────

var desc = AudioComponentDescription(
    componentType: fcc("aufx"), componentSubType: fcc("BBOP"),
    componentManufacturer: fcc("Bbop"),
    componentFlags: 0, componentFlagsMask: 0
)
guard let comp = AudioComponentFindNext(nil, &desc) else {
    fail("AudioComponentFindNext returned nil — plugin not registered")
}
var instance: AudioComponentInstance?
var s = AudioComponentInstanceNew(comp, &instance)
guard s == noErr, let au = instance else {
    fail("AudioComponentInstanceNew: \(s)")
}
defer { AudioComponentInstanceDispose(au) }
print("✓ AudioComponentInstanceNew")

// Stream format: stereo float32 deinterleaved at fixture's sample rate
var format = AudioStreamBasicDescription(
    mSampleRate: kSampleRate, mFormatID: kAudioFormatLinearPCM,
    mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked
                  | kAudioFormatFlagIsNonInterleaved,
    mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4,
    mChannelsPerFrame: 2, mBitsPerChannel: 32, mReserved: 0
)
_ = AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat,
    kAudioUnitScope_Input, 0, &format, UInt32(MemoryLayout.size(ofValue: format)))
_ = AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat,
    kAudioUnitScope_Output, 0, &format, UInt32(MemoryLayout.size(ofValue: format)))

// ─── Set parameters: sync mode on, charleston, fixed BPM, no spice ────────
//
// AU param indices (BebopAU.cpp):
//   Spice=0  Voicing=1  Rhythm=2  BPM=3  Octave=4  SyncBpm=5  LastChord=6
//   LoopMode=7  LoopBars=8  CompOffset=9  RhythmSync=10

_ = AudioUnitSetParameter(au, /*Spice*/0,        kAudioUnitScope_Global, 0, 0.0,         0)
_ = AudioUnitSetParameter(au, /*Rhythm*/2,       kAudioUnitScope_Global, 0, kRhythmIdx,  0)
_ = AudioUnitSetParameter(au, /*BPM*/3,          kAudioUnitScope_Global, 0, Float(kBpm), 0)
_ = AudioUnitSetParameter(au, /*SyncBpm*/5,      kAudioUnitScope_Global, 0, 0.0,         0)
_ = AudioUnitSetParameter(au, /*LoopMode*/7,     kAudioUnitScope_Global, 0, 0.0,         0)
_ = AudioUnitSetParameter(au, /*RhythmSync*/10,  kAudioUnitScope_Global, 0, 1.0,         0)
// MIDI Latency compensation is for Logic-specific scheduling offset;
// our simulator doesn't apply that offset so we zero it for this test
_ = AudioUnitSetParameter(au, /*MidiLatencyMs*/13, kAudioUnitScope_Global, 0, 0.0,       0)

// ─── Register HostCallbacks so the AU can call CallHostBeatAndTempo etc. ──

var cb = HostCallbackInfo(
    hostUserData:                hostRef,
    beatAndTempoProc:            beatAndTempoProc,
    musicalTimeLocationProc:     musicalProc,
    transportStateProc:          transportStateProc,
    transportStateProc2:         nil
)
s = AudioUnitSetProperty(au, kAudioUnitProperty_HostCallbacks,
    kAudioUnitScope_Global, 0, &cb,
    UInt32(MemoryLayout<HostCallbackInfo>.size))
guard s == noErr else { fail("set HostCallbacks: \(s)") }
print("✓ HostCallbacks registered")

// ─── MIDI capture ──────────────────────────────────────────────────────────

struct CapturedEvent {
    let machTime: UInt64        // packet's mach timestamp
    let blockHostTime: UInt64   // mHostTime of the block this came from
    let blockBeat: Double       // host beat at the block's first frame
    let bytes: [UInt8]
}

final class Capture {
    var events: [CapturedEvent] = []
}
let capRetained = Unmanaged.passRetained(Capture())
defer { capRetained.release() }
let capRef = capRetained.toOpaque()

let midiCallback: AUMIDIOutputCallback = { ud, ts, _, pktList in
    guard let ud = ud else { return noErr }
    let cap = Unmanaged<Capture>.fromOpaque(ud).takeUnretainedValue()
    let blockHostMach = ts.pointee.mHostTime
    // The AU populates only mHostTime in its callback (deliberately —
    // hosts use it to map packet timestamps to play time). Recover the
    // host beat by inverting the test's wall-clock anchor.
    let h = Unmanaged<HostState>.fromOpaque(hostRef).takeUnretainedValue()
    let nsSinceAnchor = machToNs(blockHostMach &- h.hostMachAtSample0)
    let sampleAtAnchor = nsSinceAnchor * kSampleRate / 1e9
    let blockBeat = sampleAtAnchor / kSampleRate * kBpm / 60
    var pkt = pktList.pointee.packet
    for _ in 0..<pktList.pointee.numPackets {
        let len = Int(pkt.length)
        // A single MIDIPacket can hold multiple back-to-back short MIDI
        // messages (3 bytes each for note_on/off/CC). Iterate them so
        // we capture every message, not just the first
        withUnsafeBytes(of: &pkt.data) { raw in
            var i = 0
            while i + 3 <= len {
                let bytes: [UInt8] = [
                    raw.load(fromByteOffset: i,     as: UInt8.self),
                    raw.load(fromByteOffset: i + 1, as: UInt8.self),
                    raw.load(fromByteOffset: i + 2, as: UInt8.self),
                ]
                cap.events.append(CapturedEvent(
                    machTime: pkt.timeStamp,
                    blockHostTime: blockHostMach,
                    blockBeat: blockBeat,
                    bytes: bytes))
                i += 3
            }
        }
        pkt = MIDIPacketNext(&pkt).pointee
    }
    return noErr
}
var midiCb = AUMIDIOutputCallbackStruct(
    midiOutputCallback: midiCallback, userData: capRef)
s = AudioUnitSetProperty(au, kAudioUnitProperty_MIDIOutputCallback,
    kAudioUnitScope_Global, 0, &midiCb,
    UInt32(MemoryLayout<AUMIDIOutputCallbackStruct>.size))
guard s == noErr else { fail("set MIDIOutputCallback: \(s)") }

// ─── Load fixture and render with simulated host clock ────────────────────

func loadMono(path: String) -> ([Float], Double) {
    let url = URL(fileURLWithPath: path) as CFURL
    var fileRef: ExtAudioFileRef?
    var status = ExtAudioFileOpenURL(url, &fileRef)
    guard status == noErr, let f = fileRef else {
        fail("ExtAudioFileOpenURL(\(path)): \(status)")
    }
    defer { ExtAudioFileDispose(f) }
    var srcFormat = AudioStreamBasicDescription()
    var sz = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    _ = ExtAudioFileGetProperty(f, kExtAudioFileProperty_FileDataFormat, &sz, &srcFormat)
    var clientFormat = AudioStreamBasicDescription(
        mSampleRate: srcFormat.mSampleRate, mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
        mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4,
        mChannelsPerFrame: 1, mBitsPerChannel: 32, mReserved: 0)
    _ = ExtAudioFileSetProperty(f, kExtAudioFileProperty_ClientDataFormat,
        UInt32(MemoryLayout.size(ofValue: clientFormat)), &clientFormat)
    var lengthFrames: Int64 = 0
    sz = UInt32(MemoryLayout<Int64>.size)
    _ = ExtAudioFileGetProperty(f, kExtAudioFileProperty_FileLengthFrames, &sz, &lengthFrames)
    var samples = [Float](repeating: 0, count: Int(lengthFrames))
    samples.withUnsafeMutableBufferPointer { sp in
        var bl = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(
                mNumberChannels: 1,
                mDataByteSize: UInt32(Int(lengthFrames) * 4),
                mData: UnsafeMutableRawPointer(sp.baseAddress)))
        var io = UInt32(lengthFrames)
        _ = ExtAudioFileRead(f, &io, &bl)
    }
    return (samples, srcFormat.mSampleRate)
}

let manifestDir = (#file as NSString).deletingLastPathComponent
let projectRoot = (manifestDir as NSString).deletingLastPathComponent
let fixturePath = (projectRoot as NSString)
    .appendingPathComponent("../output/eval/ii_V_I_C.wav")
let (mono, _) = loadMono(path: fixturePath)
print("✓ loaded \(mono.count) mono samples from fixture")

nonisolated(unsafe) var fixturePos = 0
let fixtureSamples = mono
let fixtureRender: AURenderCallback = { _, _, _, _, frames, ioData -> OSStatus in
    guard let ioData = ioData else { return noErr }
    let abl = UnsafeMutableAudioBufferListPointer(ioData)
    for buf in abl {
        let dst = buf.mData!.assumingMemoryBound(to: Float.self)
        for i in 0..<Int(frames) {
            if fixturePos + i < fixtureSamples.count {
                dst[i] = fixtureSamples[fixturePos + i]
            } else {
                dst[i] = 0
            }
        }
    }
    fixturePos += Int(frames)
    return noErr
}
var renderCb = AURenderCallbackStruct(inputProc: fixtureRender, inputProcRefCon: nil)
_ = AudioUnitSetProperty(au, kAudioUnitProperty_SetRenderCallback,
    kAudioUnitScope_Input, 0, &renderCb,
    UInt32(MemoryLayout<AURenderCallbackStruct>.size))

s = AudioUnitInitialize(au)
guard s == noErr else { fail("AudioUnitInitialize: \(s)") }
defer { AudioUnitUninitialize(au) }
print("✓ AudioUnitInitialize")

// Run the audio thread MUCH faster than realtime: just call render in a
// tight loop. The Bebop worker runs in a separate thread on wall-clock,
// so we throttle render to roughly real-time so the worker keeps up
let totalFrames = mono.count + Int(kSampleRate * 4)  // fixture + 4s tail
let blocksPerBurst = Int(0.5 * kSampleRate / Double(kFrames))  // 0.5s per burst

class OutputBufferList {
    let abl: UnsafeMutablePointer<AudioBufferList>
    private var data: [UnsafeMutablePointer<Float>]
    init(channels: Int, frames: Int) {
        let size = MemoryLayout<AudioBufferList>.size
                   + (channels - 1) * MemoryLayout<AudioBuffer>.size
        let mem = UnsafeMutableRawPointer.allocate(byteCount: size,
            alignment: MemoryLayout<AudioBufferList>.alignment)
        abl = mem.bindMemory(to: AudioBufferList.self, capacity: 1)
        abl.pointee.mNumberBuffers = UInt32(channels)
        let p = UnsafeMutableAudioBufferListPointer(abl)
        data = []
        for i in 0..<channels {
            let buf = UnsafeMutablePointer<Float>.allocate(capacity: frames)
            buf.initialize(repeating: 0, count: frames)
            p[i] = AudioBuffer(mNumberChannels: 1,
                mDataByteSize: UInt32(frames * 4), mData: buf)
            data.append(buf)
        }
    }
    func deallocate() {
        for d in data { d.deallocate() }
        UnsafeMutableRawPointer(abl).deallocate()
    }
}

let outBuf = OutputBufferList(channels: 2, frames: Int(kFrames))
defer { outBuf.deallocate() }

var rendered = 0
var actionFlags = AudioUnitRenderActionFlags(rawValue: 0)
// Real-time pacing: each rendered block must take its simulated
// duration in real wall time, so `mach_absolute_time()` inside the AU
// (which we now use as the MIDI packet timestamp reference, matching
// what Logic does) advances in lockstep with the simulator's
// `host.startSample`. Without this, the audio thread runs faster than
// realtime and packet timestamps land long before their intended
// simulated beat
while rendered < totalFrames {
    for _ in 0..<blocksPerBurst {
        if rendered >= totalFrames { break }
        var ts = AudioTimeStamp()
        ts.mFlags = [.sampleTimeValid, .hostTimeValid]
        ts.mSampleTime = host.startSample
        ts.mHostTime = host.hostTime
        let st = AudioUnitRender(au, &actionFlags, &ts, 0, kFrames, outBuf.abl)
        if st != noErr { fail("AudioUnitRender: \(st)") }
        host.startSample += Double(kFrames)
        rendered += Int(kFrames)
        // Pace: sleep until real time reaches the simulated block end
        let desiredMach = host.hostTime
        let nowMach = mach_absolute_time()
        if desiredMach > nowMach {
            let waitNs = machToNs(desiredMach &- nowMach)
            // Use a busy-wait equivalent via Thread.sleep for ms-scale
            // waits; the test takes ~16s of real time, which is the
            // tradeoff for sample-accurate timestamp simulation
            Thread.sleep(forTimeInterval: waitNs / 1e9)
        }
    }
}
print("✓ rendered \(rendered) frames over \(host.startSample / kSampleRate) seconds (host-beat \(host.beat))")

// ─── Verify ────────────────────────────────────────────────────────────────

let cap = Unmanaged<Capture>.fromOpaque(capRef).takeUnretainedValue()
print("ⓘ captured \(cap.events.count) MIDI packets")

// For each captured event, compute host beat at packet fire time:
//   sampleOffset = (packet.machTime - block.mHostTime) ns / ns_per_sample
//   beat_at_packet = block.beat + sampleOffset / sr * bpm/60
let nsPerSample = 1e9 / kSampleRate

struct AnalyzedEvent {
    let beatAbs: Double
    let beatInBar: Double
    let status: UInt8
    let pitch: UInt8
    let velocity: UInt8
}

var analyzed: [AnalyzedEvent] = []
for ev in cap.events {
    let offsetNs = machToNs(ev.machTime &- ev.blockHostTime)
    let offsetSamples = offsetNs / nsPerSample
    let offsetBeats = offsetSamples / kSampleRate * kBpm / 60
    let beat = ev.blockBeat + offsetBeats
    let beatInBar = beat.truncatingRemainder(dividingBy: 4)
    if ev.bytes.count >= 3 {
        analyzed.append(AnalyzedEvent(
            beatAbs: beat,
            beatInBar: beatInBar < 0 ? beatInBar + 4 : beatInBar,
            status: ev.bytes[0],
            pitch: ev.bytes[1],
            velocity: ev.bytes[2]))
    }
}

// Drift to nearest expected offset, accounting for bar wrap. A hit at
// beat-in-bar 3.9999 is really 0.0001 before the next bar's downbeat
// (offset 0.0), not 1.5 beats away from offset 2.5. Without wrap-around
// we'd false-positive on perfectly-on-grid downbeats
func driftToNearestOffset(_ beatInBar: Double) -> Double {
    var best = 4.0
    for off in kExpectedOffsets {
        let direct = abs(beatInBar - off)
        let wrapped = abs((beatInBar - off + 4).truncatingRemainder(dividingBy: 4))
        let wrapped2 = abs((off - beatInBar + 4).truncatingRemainder(dividingBy: 4))
        let d = min(direct, min(wrapped, wrapped2))
        if d < best { best = d }
    }
    return best
}

// Filter to PIANO note_ons (channel 1, status 0x90) — these are the
// rhythm hits we expect to land at the rhythm template's offsets.
// (Bass note_ons land on chord-onset, which is wherever recognition
// commits — verified separately by au_fixture_test.)
let pianoOns = analyzed.filter { $0.status == 0x90 && $0.velocity > 0 }
print("ⓘ \(pianoOns.count) piano note_ons captured")
for ev in pianoOns.prefix(20) {
    let drift = driftToNearestOffset(ev.beatInBar)
    print("  beat=\(String(format: "%7.4f", ev.beatAbs)) "
          + "in_bar=\(String(format: "%.4f", ev.beatInBar)) "
          + "drift=\(String(format: "%.4f", drift))")
}

guard !pianoOns.isEmpty else {
    fail("no piano note_ons captured — sync path didn't fire any events")
}

// Compute drift statistics
var maxDrift = 0.0
var totalDrift = 0.0
var failures: [AnalyzedEvent] = []
for ev in pianoOns {
    let drift = driftToNearestOffset(ev.beatInBar)
    maxDrift = max(maxDrift, drift)
    totalDrift += drift
    if drift > kBeatTolerance {
        failures.append(ev)
    }
}
let meanDrift = totalDrift / Double(pianoOns.count)
print("ⓘ mean drift = \(String(format: "%.4f", meanDrift)) beats, "
      + "max drift = \(String(format: "%.4f", maxDrift)) beats")
print("ⓘ tolerance = \(kBeatTolerance) beats")

if !failures.isEmpty {
    print("✗ \(failures.count)/\(pianoOns.count) hits exceeded tolerance:")
    for ev in failures.prefix(8) {
        let drift = driftToNearestOffset(ev.beatInBar)
        print("  beat=\(String(format: "%7.4f", ev.beatAbs)) "
              + "in_bar=\(String(format: "%.4f", ev.beatInBar)) "
              + "drift=\(String(format: "%.4f", drift))")
    }
    fail("rhythm sync timing exceeded tolerance — see logs above")
}

print("✓ all \(pianoOns.count) piano hits within ±\(kBeatTolerance) beats of charleston grid")
print("PASS")

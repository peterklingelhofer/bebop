// SPDX-License-Identifier: MIT
//
// Fixture-driven AU integration test — feeds an eval WAV (a known chord
// progression rendered to audio) through the AU and asserts the
// captured MIDI looks correct.
//
// This is the closest thing to a Logic integration test that doesn't
// need Logic. Catches regressions in:
//   - chord recognition through the embedded Python pipeline
//   - voicing + rhythm event generation in bebop-rs
//   - the C++ MIDI queue + render-thread output path
//
// Fails on:
//   - no MIDI emitted at all (pipeline broken)
//   - wrong chord roots (recognition broken or wrong chord progression)
//   - too few voicing pitches per chord (voicing broken)

import AudioToolbox
import CoreMIDI
import Foundation

@inline(__always) func fcc(_ s: String) -> OSType {
    var v: OSType = 0
    for ch in s.utf8.prefix(4) { v = (v << 8) | OSType(ch) }
    return v
}

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(Data("FAIL: \(msg)\n".utf8))
    exit(1)
}

// ─── Fixture: load mono float32 from a WAV ──────────────────────────────────
//
// Uses CoreAudio's ExtAudioFile rather than wiring up our own RIFF parser.

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
    status = ExtAudioFileGetProperty(f,
        kExtAudioFileProperty_FileDataFormat,
        &sz, &srcFormat)
    guard status == noErr else { fail("get file format: \(status)") }
    let sr = srcFormat.mSampleRate

    var clientFormat = AudioStreamBasicDescription(
        mSampleRate: sr, mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
        mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4,
        mChannelsPerFrame: 1, mBitsPerChannel: 32, mReserved: 0
    )
    status = ExtAudioFileSetProperty(f,
        kExtAudioFileProperty_ClientDataFormat,
        UInt32(MemoryLayout.size(ofValue: clientFormat)), &clientFormat)
    guard status == noErr else { fail("set client format: \(status)") }

    var lengthFrames: Int64 = 0
    sz = UInt32(MemoryLayout<Int64>.size)
    _ = ExtAudioFileGetProperty(f,
        kExtAudioFileProperty_FileLengthFrames, &sz, &lengthFrames)
    let nFrames = Int(lengthFrames)

    var samples = [Float](repeating: 0, count: nFrames)
    samples.withUnsafeMutableBufferPointer { sp in
        var bufferList = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(
                mNumberChannels: 1,
                mDataByteSize: UInt32(nFrames * 4),
                mData: UnsafeMutableRawPointer(sp.baseAddress)
            )
        )
        var ioFrames = UInt32(nFrames)
        _ = ExtAudioFileRead(f, &ioFrames, &bufferList)
    }
    return (samples, sr)
}

// ─── Fixture-driven render ──────────────────────────────────────────────────

let manifest = (#file as NSString).deletingLastPathComponent
let projectRoot = (manifest as NSString).deletingLastPathComponent
let fixturePath = (projectRoot as NSString)
    .appendingPathComponent("../output/eval/ii_V_I_C.wav")

print("loading fixture: \(fixturePath)")
let (mono, fileSr) = loadMono(path: fixturePath)
print("✓ loaded \(mono.count) mono samples @ \(fileSr) Hz "
      + "(\(Double(mono.count) / fileSr) seconds)")

// Capture state for the AU's render input callback. The AU expects stereo;
// we duplicate mono → stereo on the fly.
nonisolated(unsafe) var fixturePos: Int = 0
let fixtureSamples = mono

let fixtureRenderCallback: AURenderCallback = { _, _, _, _, frames, ioData -> OSStatus in
    guard let ioData = ioData else { return noErr }
    let abl = UnsafeMutableAudioBufferListPointer(ioData)
    let n = Int(frames)
    for buffer in abl {
        let dst = buffer.mData!.assumingMemoryBound(to: Float.self)
        for i in 0..<n {
            if fixturePos + i < fixtureSamples.count {
                dst[i] = fixtureSamples[fixturePos + i]
            } else {
                dst[i] = 0
            }
        }
    }
    fixturePos += n
    return noErr
}

// ─── Open AU + capture ──────────────────────────────────────────────────────

final class MidiCapture { var packets: [[UInt8]] = [] }
let cap = Unmanaged.passRetained(MidiCapture())
defer { cap.release() }

let midiCallback: AUMIDIOutputCallback = { userData, _, _, pktList in
    guard let userData = userData else { return noErr }
    let mc = Unmanaged<MidiCapture>.fromOpaque(userData).takeUnretainedValue()
    var pkt = pktList.pointee.packet
    for _ in 0..<pktList.pointee.numPackets {
        var bytes: [UInt8] = []
        let len = Int(pkt.length)
        withUnsafeBytes(of: &pkt.data) { raw in
            for i in 0..<len {
                bytes.append(raw.load(fromByteOffset: i, as: UInt8.self))
            }
        }
        mc.packets.append(bytes)
        pkt = MIDIPacketNext(&pkt).pointee
    }
    return noErr
}

var desc = AudioComponentDescription(
    componentType: fcc("aufx"), componentSubType: fcc("BBOP"),
    componentManufacturer: fcc("Bbop"),
    componentFlags: 0, componentFlagsMask: 0
)
guard let comp = AudioComponentFindNext(nil, &desc) else {
    fail("AudioComponentFindNext returned nil")
}
var instance: AudioComponentInstance?
var status = AudioComponentInstanceNew(comp, &instance)
guard status == noErr, let au = instance else {
    fail("AudioComponentInstanceNew: \(status)")
}
defer { AudioComponentInstanceDispose(au) }

// Set BPM to match the fixture (100 BPM — see tests/eval/synth.py fixtures()).
_ = AudioUnitSetParameter(au, /*BPM*/3, kAudioUnitScope_Global, 0, 100.0, 0)
// Pin Spice to 0 so reharm-driven root substitutions don't shift the
// expected ii-V-I bass roots. The AU's default spice is 0.4 which would
// transform e.g. Dm7 → A7 and break root-pc assertions
_ = AudioUnitSetParameter(au, /*Spice*/0, kAudioUnitScope_Global, 0, 0.0, 0)

// Stereo float32 deinterleaved at the fixture's sample rate.
var format = AudioStreamBasicDescription(
    mSampleRate: fileSr, mFormatID: kAudioFormatLinearPCM,
    mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked
                  | kAudioFormatFlagIsNonInterleaved,
    mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4,
    mChannelsPerFrame: 2, mBitsPerChannel: 32, mReserved: 0
)
_ = AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat,
    kAudioUnitScope_Input, 0, &format, UInt32(MemoryLayout.size(ofValue: format)))
_ = AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat,
    kAudioUnitScope_Output, 0, &format, UInt32(MemoryLayout.size(ofValue: format)))

var renderCb = AURenderCallbackStruct(inputProc: fixtureRenderCallback,
                                        inputProcRefCon: nil)
_ = AudioUnitSetProperty(au, kAudioUnitProperty_SetRenderCallback,
    kAudioUnitScope_Input, 0, &renderCb,
    UInt32(MemoryLayout<AURenderCallbackStruct>.size))

var midiCb = AUMIDIOutputCallbackStruct(midiOutputCallback: midiCallback,
                                          userData: cap.toOpaque())
_ = AudioUnitSetProperty(au, kAudioUnitProperty_MIDIOutputCallback,
    kAudioUnitScope_Global, 0, &midiCb,
    UInt32(MemoryLayout<AUMIDIOutputCallbackStruct>.size))

status = AudioUnitInitialize(au)
guard status == noErr else { fail("AudioUnitInitialize: \(status)") }
defer { AudioUnitUninitialize(au) }

// Render the entire fixture, interleaving with sleeps so the worker
// thread has time to detect chords + queue MIDI between bursts.
let kFrames: UInt32 = 512
let outBuf = AVOutputBufferList(channels: 2, frames: Int(kFrames))
defer { outBuf.deallocate() }
var ts = AudioTimeStamp(); ts.mFlags = .sampleTimeValid; ts.mSampleTime = 0
var actionFlags = AudioUnitRenderActionFlags(rawValue: 0)

let totalFrames = mono.count
let buffersPerBurst = Int(0.5 * fileSr) / Int(kFrames)
var rendered = 0
while rendered < totalFrames {
    for _ in 0..<buffersPerBurst {
        if rendered >= totalFrames { break }
        _ = AudioUnitRender(au, &actionFlags, &ts, 0, kFrames, outBuf.abl)
        ts.mSampleTime += Double(kFrames)
        rendered += Int(kFrames)
    }
    Thread.sleep(forTimeInterval: 0.2)
}
// Final drain.
Thread.sleep(forTimeInterval: 0.5)
for _ in 0..<buffersPerBurst {
    _ = AudioUnitRender(au, &actionFlags, &ts, 0, kFrames, outBuf.abl)
    ts.mSampleTime += Double(kFrames)
}
print("✓ rendered fixture (\(rendered) frames)")

// ─── Verify ─────────────────────────────────────────────────────────────────

struct ShortMsg { let status, data1, data2: UInt8 }
var msgs: [ShortMsg] = []
for pkt in cap.takeUnretainedValue().packets {
    var i = 0
    while i + 3 <= pkt.count {
        msgs.append(ShortMsg(status: pkt[i], data1: pkt[i+1], data2: pkt[i+2]))
        i += 3
    }
}
print("✓ captured \(msgs.count) MIDI short messages from \(cap.takeUnretainedValue().packets.count) packets")

let bassNoteOns = msgs.filter { $0.status == 0x91 }
let pianoNoteOns = msgs.filter { $0.status == 0x90 }
guard bassNoteOns.count >= 2 else {
    fail("expected ≥2 distinct bass note_ons across ii-V-I, got \(bassNoteOns.count)")
}
guard pianoNoteOns.count >= 4 else {
    fail("expected ≥4 piano voicing notes across ii-V-I, got \(pianoNoteOns.count)")
}

// ii_V_I_C is Dm7 G7 Cmaj7 Cmaj7. Bass roots as MIDI pitch classes:
//   Dm7 → D = 2;  G7 → G = 7;  Cmaj7 → C = 0
// (reharm at default spice=0.4 may alter these slightly, so we accept
// any of {2, 7, 0} appearing ≥ once.)
let bassPcs = Set(bassNoteOns.map { Int($0.data1) % 12 })
print("ⓘ bass pitch-classes seen: \(bassPcs.sorted())")
let expected: Set<Int> = [2, 7, 0]
let overlap = bassPcs.intersection(expected)
guard overlap.count >= 2 else {
    fail("bass roots \(bassPcs.sorted()) don't match ii-V-I expectations \(expected.sorted())")
}
print("✓ bass progression hits ≥2 of expected ii-V-I roots: \(overlap.sorted())")

// Piano voicings: chroma should span at least 5 distinct pitch classes
// (Dm7 + G7 + Cmaj7 union has ~7 unique pcs).
let pianoPcs = Set(pianoNoteOns.map { Int($0.data1) % 12 })
print("ⓘ piano pitch-classes seen: \(pianoPcs.sorted())")
guard pianoPcs.count >= 5 else {
    fail("expected ≥5 distinct piano pcs, got \(pianoPcs.count): \(pianoPcs.sorted())")
}
print("✓ piano voicings span \(pianoPcs.count) distinct pitch classes")

print("PASS")

// ─── AudioBufferList helper ─────────────────────────────────────────────────

final class AVOutputBufferList {
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
                                 mDataByteSize: UInt32(frames * 4),
                                 mData: buf)
            data.append(buf)
        }
    }
    func deallocate() {
        for d in data { d.deallocate() }
        UnsafeMutableRawPointer(abl).deallocate()
    }
}

// SPDX-License-Identifier: MIT
//
// AU virtual MIDI source test — proves that the AU exposes a CoreMIDI
// virtual source named "Bebop AU" that any process can subscribe to,
// receiving the same MIDI events the AU emits via the AU host callback.
//
// This is the path Logic / Pro Tools / Reaper / etc. use when their
// AU-MIDI-output UI is buried (or doesn't exist for `aufx` plugins).
// The user just creates a software-instrument track with MIDI input
// "Bebop AU" and our AU drives it.
//
// Test flow:
//   1. Instantiate the AU → triggers Initialize() → opens the virtual
//      source named "Bebop AU"
//   2. Scan CoreMIDI sources, find ours
//   3. Open a MIDIInputPort and subscribe to it
//   4. Render audio through the AU
//   5. Assert ≥1 MIDI event arrived on our input port

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

// ─── 1. Instantiate the AU ──────────────────────────────────────────────────

var desc = AudioComponentDescription(
    componentType: fcc("aufx"), componentSubType: fcc("BBOP"),
    componentManufacturer: fcc("Bbop"),
    componentFlags: 0, componentFlagsMask: 0
)
guard let comp = AudioComponentFindNext(nil, &desc) else {
    fail("AudioComponentFindNext returned nil — plugin not installed?")
}
var instance: AudioComponentInstance?
var status = AudioComponentInstanceNew(comp, &instance)
guard status == noErr, let au = instance else {
    fail("AudioComponentInstanceNew: \(status)")
}
defer { AudioComponentInstanceDispose(au) }

// Stereo float32 deinterleaved at 44.1kHz.
var format = AudioStreamBasicDescription(
    mSampleRate: 44_100, mFormatID: kAudioFormatLinearPCM,
    mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked
                  | kAudioFormatFlagIsNonInterleaved,
    mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4,
    mChannelsPerFrame: 2, mBitsPerChannel: 32, mReserved: 0
)
_ = AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat,
    kAudioUnitScope_Input, 0, &format, UInt32(MemoryLayout.size(ofValue: format)))
_ = AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat,
    kAudioUnitScope_Output, 0, &format, UInt32(MemoryLayout.size(ofValue: format)))

// 440 Hz sine on a stable phase so the recognizer commits "Am".
nonisolated(unsafe) var phaseState: Double = 0
let omega = 2.0 * Double.pi * 440.0 / 44_100.0
let sineCallback: AURenderCallback = { _, _, _, _, frames, ioData -> OSStatus in
    guard let ioData = ioData else { return noErr }
    let abl = UnsafeMutableAudioBufferListPointer(ioData)
    for buffer in abl {
        let data = buffer.mData!.assumingMemoryBound(to: Float.self)
        for i in 0..<Int(frames) {
            data[i] = Float(sin(phaseState + omega * Double(i))) * 0.25
        }
    }
    phaseState += omega * Double(frames)
    return noErr
}
var cb = AURenderCallbackStruct(inputProc: sineCallback, inputProcRefCon: nil)
_ = AudioUnitSetProperty(au, kAudioUnitProperty_SetRenderCallback,
    kAudioUnitScope_Input, 0, &cb,
    UInt32(MemoryLayout<AURenderCallbackStruct>.size))

status = AudioUnitInitialize(au)
guard status == noErr else { fail("AudioUnitInitialize: \(status)") }
defer { AudioUnitUninitialize(au) }
print("✓ AU initialized — virtual source should now exist")

// ─── 2. Find the "Bebop AU" virtual source ─────────────────────────────────

func findSource(named: String) -> MIDIEndpointRef? {
    let n = MIDIGetNumberOfSources()
    for i in 0..<n {
        let src = MIDIGetSource(i)
        var nameRef: Unmanaged<CFString>?
        guard MIDIObjectGetStringProperty(src, kMIDIPropertyName, &nameRef) == noErr,
              let cf = nameRef?.takeRetainedValue() else { continue }
        if (cf as String) == named { return src }
    }
    return nil
}

guard let source = findSource(named: "Bebop AU") else {
    let names: [String] = (0..<MIDIGetNumberOfSources()).compactMap { i in
        var nr: Unmanaged<CFString>?
        if MIDIObjectGetStringProperty(MIDIGetSource(i), kMIDIPropertyName, &nr) == noErr {
            return nr?.takeRetainedValue() as String?
        }
        return nil
    }
    fail("CoreMIDI source 'Bebop AU' not found. Visible sources: \(names)")
}
print("✓ found CoreMIDI virtual source 'Bebop AU'")

// ─── 3. Subscribe with a MIDIInputPort ─────────────────────────────────────

final class Capture { var bytes: [UInt8] = [] }
let captured = Unmanaged.passRetained(Capture())
defer { captured.release() }

var client: MIDIClientRef = 0
status = MIDIClientCreate("BebopAUTest" as CFString, nil, nil, &client)
guard status == noErr else { fail("MIDIClientCreate: \(status)") }
defer { MIDIClientDispose(client) }

let readBlock: MIDIReadBlock = { pktList, _ in
    let cap = captured.takeUnretainedValue()
    var pkt = pktList.pointee.packet
    for _ in 0..<pktList.pointee.numPackets {
        let len = Int(pkt.length)
        withUnsafeBytes(of: &pkt.data) { raw in
            for j in 0..<len {
                cap.bytes.append(raw.load(fromByteOffset: j, as: UInt8.self))
            }
        }
        pkt = MIDIPacketNext(&pkt).pointee
    }
}
var port: MIDIPortRef = 0
status = MIDIInputPortCreateWithBlock(client, "BebopAUTestPort" as CFString,
                                        &port, readBlock)
guard status == noErr else { fail("MIDIInputPortCreateWithBlock: \(status)") }
status = MIDIPortConnectSource(port, source, nil)
guard status == noErr else { fail("MIDIPortConnectSource: \(status)") }
print("✓ subscribed to virtual source")

// ─── 4. Render audio through the AU ────────────────────────────────────────

let kFrames: UInt32 = 512
let outBuf = AVOutputBufferList(channels: 2, frames: Int(kFrames))
defer { outBuf.deallocate() }
var ts = AudioTimeStamp(); ts.mFlags = .sampleTimeValid; ts.mSampleTime = 0
var actionFlags = AudioUnitRenderActionFlags(rawValue: 0)

func renderBurst(_ seconds: Double) {
    let buffers = Int(seconds * 44_100) / Int(kFrames)
    for _ in 0..<buffers {
        _ = AudioUnitRender(au, &actionFlags, &ts, 0, kFrames, outBuf.abl)
        ts.mSampleTime += Double(kFrames)
    }
}
for _ in 0..<10 {
    renderBurst(0.5)
    Thread.sleep(forTimeInterval: 0.2)
    renderBurst(0.5)
    if !captured.takeUnretainedValue().bytes.isEmpty { break }
}
Thread.sleep(forTimeInterval: 0.3)
renderBurst(0.5)

// ─── 5. Assert events arrived ──────────────────────────────────────────────

let bytes = captured.takeUnretainedValue().bytes
print("✓ captured \(bytes.count) bytes from virtual source")
guard bytes.count >= 3 else {
    fail("expected ≥3 bytes (one short MIDI message), got \(bytes.count)")
}

// Parse short messages.
struct ShortMsg { let status, d1, d2: UInt8 }
var msgs: [ShortMsg] = []
var i = 0
while i + 3 <= bytes.count {
    msgs.append(ShortMsg(status: bytes[i], d1: bytes[i+1], d2: bytes[i+2]))
    i += 3
}
let bassNotes = msgs.filter { $0.status == 0x91 }.count
let pianoNotes = msgs.filter { $0.status == 0x90 }.count
print("✓ \(bassNotes) bass note_ons + \(pianoNotes) piano note_ons via virtual source")
guard bassNotes >= 1, pianoNotes >= 1 else {
    fail("expected real comp output (bass + piano), got bass=\(bassNotes) piano=\(pianoNotes)")
}
print("PASS")

// ─── helpers ────────────────────────────────────────────────────────────────

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

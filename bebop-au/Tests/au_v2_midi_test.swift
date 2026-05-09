// SPDX-License-Identifier: MIT
//
// Direct AUv2 MIDI capture test — bypasses AVAudioEngine and uses the
// classic AudioUnit C API the way Logic does. This is the path our v2
// MIDIOutputCallback actually flows through, so it's the authoritative
// way to verify Phase C.6 end-to-end without Logic.
//
// Test flow:
//   1. Find + instantiate our AU via AudioComponentFindNext / Open
//   2. Set up the AU's input/output stream formats
//   3. Register a MIDIOutputCallback that captures events
//   4. AudioUnitInitialize
//   5. Render N buffers with a 440Hz sine
//   6. Assert MIDI events were captured (chord detected → MIDI emitted)

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

// ─── Capture state ──────────────────────────────────────────────────────────

final class MidiCapture {
    var packets: [[UInt8]] = []
}

let capture = Unmanaged.passRetained(MidiCapture())
defer { capture.release() }

let midiCallback: AUMIDIOutputCallback = { userData, _, _, pktList in
    guard let userData = userData else { return noErr }
    let cap = Unmanaged<MidiCapture>.fromOpaque(userData).takeUnretainedValue()
    var pkt = pktList.pointee.packet
    for _ in 0..<pktList.pointee.numPackets {
        var bytes: [UInt8] = []
        let len = Int(pkt.length)
        // pkt.data is a tuple; use mirror via withUnsafeBytes
        withUnsafeBytes(of: &pkt.data) { raw in
            for i in 0..<len {
                bytes.append(raw.load(fromByteOffset: i, as: UInt8.self))
            }
        }
        cap.packets.append(bytes)
        pkt = MIDIPacketNext(&pkt).pointee
    }
    return noErr
}

// ─── Find + open the AU ─────────────────────────────────────────────────────

var desc = AudioComponentDescription(
    componentType: fcc("aufx"),
    componentSubType: fcc("BBOP"),
    componentManufacturer: fcc("Bbop"),
    componentFlags: 0,
    componentFlagsMask: 0
)
guard let comp = AudioComponentFindNext(nil, &desc) else {
    fail("AudioComponentFindNext returned nil — plugin not registered")
}
print("✓ AudioComponentFindNext")

var instance: AudioComponentInstance?
var status = AudioComponentInstanceNew(comp, &instance)
guard status == noErr, let au = instance else {
    fail("AudioComponentInstanceNew: \(status)")
}
print("✓ AudioComponentInstanceNew")

// ─── Configure stream formats (stereo float32 deinterleaved, 44.1kHz) ──────

var format = AudioStreamBasicDescription(
    mSampleRate: 44_100,
    mFormatID: kAudioFormatLinearPCM,
    mFormatFlags: kAudioFormatFlagIsFloat
                  | kAudioFormatFlagIsPacked
                  | kAudioFormatFlagIsNonInterleaved,
    mBytesPerPacket: 4,
    mFramesPerPacket: 1,
    mBytesPerFrame: 4,
    mChannelsPerFrame: 2,
    mBitsPerChannel: 32,
    mReserved: 0
)
status = AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat,
    kAudioUnitScope_Input, 0, &format, UInt32(MemoryLayout.size(ofValue: format)))
guard status == noErr else { fail("set input format: \(status)") }
status = AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat,
    kAudioUnitScope_Output, 0, &format, UInt32(MemoryLayout.size(ofValue: format)))
guard status == noErr else { fail("set output format: \(status)") }

// ─── Register MIDI output callback ──────────────────────────────────────────

var midiCb = AUMIDIOutputCallbackStruct(
    midiOutputCallback: midiCallback,
    userData: capture.toOpaque()
)
status = AudioUnitSetProperty(au, kAudioUnitProperty_MIDIOutputCallback,
    kAudioUnitScope_Global, 0,
    &midiCb, UInt32(MemoryLayout<AUMIDIOutputCallbackStruct>.size))
guard status == noErr else {
    fail("set MIDIOutputCallback: \(status)")
}
print("✓ MIDIOutputCallback registered")

// ─── Set input render callback that produces 440Hz sine ────────────────────

var phase: Double = 0
let omega = 2.0 * Double.pi * 440.0 / 44_100.0

let renderCallback: AURenderCallback = { _, _, _, _, frames, ioData -> OSStatus in
    guard let ioData = ioData else { return noErr }
    let abl = UnsafeMutableAudioBufferListPointer(ioData)
    for buffer in abl {
        let data = buffer.mData!.assumingMemoryBound(to: Float.self)
        for i in 0..<Int(frames) {
            data[i] = Float(sin(phase + omega * Double(i))) * 0.25
        }
    }
    phase += omega * Double(frames)
    return noErr
}

var cbStruct = AURenderCallbackStruct(inputProc: renderCallback, inputProcRefCon: nil)
status = AudioUnitSetProperty(au, kAudioUnitProperty_SetRenderCallback,
    kAudioUnitScope_Input, 0,
    &cbStruct, UInt32(MemoryLayout<AURenderCallbackStruct>.size))
guard status == noErr else { fail("set render callback: \(status)") }

// ─── Initialize + render ────────────────────────────────────────────────────

status = AudioUnitInitialize(au)
guard status == noErr else { fail("AudioUnitInitialize: \(status)") }
print("✓ AudioUnitInitialize")

let kFrames: UInt32 = 512
let outBuf = AVOutputBufferList(channels: 2, frames: Int(kFrames))
defer { outBuf.deallocate() }

// Worker thread is wall-clock paced (100ms sleeps + ~50-100ms analysis)
// so we have to interleave rendering with wall-clock waits, AND we have
// to keep rendering AFTER the worker has had time to push events so the
// audio thread's flushMidi has a chance to fire.
//
// Pattern per pass:
//   - render 0.5s of audio (gives the worker fresh data to analyze)
//   - sleep 0.2s (lets the worker analyze + push to queue)
//   - render another 0.5s (drains the queue via ProcessBufferLists)
//
// Pass count must cover the stability filter's recognition latency:
// window (1.0s) + (STABILITY_FRAMES-1)*period (2*0.3s) = 1.6s minimum
// before the first stable commit. Each pass is ~1.2s wall-time, so 16
// passes give comfortable headroom for the worker to commit a chord
var ts = AudioTimeStamp()
ts.mFlags = .sampleTimeValid
ts.mSampleTime = 0
var actionFlags = AudioUnitRenderActionFlags(rawValue: 0)

func renderBurst(_ seconds: Double) {
    let buffers = Int(seconds * 44_100) / Int(kFrames)
    for _ in 0..<buffers {
        let s = AudioUnitRender(au, &actionFlags, &ts, 0, kFrames, outBuf.abl)
        if s != noErr { fail("AudioUnitRender: \(s)") }
        ts.mSampleTime += Double(kFrames)
    }
}

for pass in 0..<16 {
    renderBurst(0.5)            // audio in
    Thread.sleep(forTimeInterval: 0.2) // worker wakes + pushes
    renderBurst(0.5)            // audio in + drain
    if !capture.takeUnretainedValue().packets.isEmpty {
        print("✓ MIDI captured after pass \(pass)")
        break
    }
}
print("✓ rendered with worker-thread interleave")

// ─── Verify ─────────────────────────────────────────────────────────────────

AudioUnitUninitialize(au)
AudioComponentInstanceDispose(au)

let mc = capture.takeUnretainedValue()
print("ⓘ MIDI events captured: \(mc.packets.count)")
for ev in mc.packets.prefix(5) {
    let hex = ev.map { String(format: "%02x", $0) }.joined(separator: " ")
    print("  - \(hex)")
}

guard !mc.packets.isEmpty else {
    fail("no MIDI events emitted — chord recognition or queue is broken")
}

// Unpack short messages (3 bytes each) from each packet — a packet may
// contain several messages back-to-back.
struct ShortMsg { let status, data1, data2: UInt8 }
var msgs: [ShortMsg] = []
for pkt in mc.packets {
    var i = 0
    while i + 3 <= pkt.count {
        msgs.append(ShortMsg(status: pkt[i], data1: pkt[i+1], data2: pkt[i+2]))
        i += 3
    }
}
print("✓ unpacked \(msgs.count) short MIDI messages from \(mc.packets.count) packet(s)")
for m in msgs.prefix(8) {
    print("  - status=0x\(String(format: "%02x", m.status)) "
          + "pitch=\(m.data1) vel=\(m.data2)")
}

// 440 Hz sine → recognizer should commit "Am" → bebop-rs runs the
// rootless voicing on it → bass note + 3-pitch piano voicing.
// Expect at least one bass note (channel 2, status 0x91) and several
// piano notes (channel 1, status 0x90).
let bassNotes = msgs.filter { $0.status == 0x91 }.count
let pianoNotes = msgs.filter { $0.status == 0x90 }.count
guard bassNotes >= 1 else {
    fail("expected ≥1 bass note (status 0x91), got \(bassNotes)")
}
guard pianoNotes >= 1 else {
    fail("expected ≥1 piano note (status 0x90), got \(pianoNotes)")
}
print("✓ comp output: \(bassNotes) bass + \(pianoNotes) piano note_ons")
print("PASS")

// ─── Buffer list helper ─────────────────────────────────────────────────────

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

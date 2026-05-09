"""Capture live audio from an input device (typically BlackHole) into a ring buffer.

We open a `sounddevice.InputStream` at `samplerate=22050, channels=1` to match
the rest of bebop's audio pipeline (parse_audio uses the same SR), and write
incoming frames into a circular numpy buffer. The chord-stream stage reads
recent windows out of this buffer at its own cadence — decoupled from the audio
callback, which must return quickly to avoid xruns.

Design notes:
    - The audio callback is a real-time thread. It does ZERO heavy work — just
      copies frames into the ring and advances the write head.
    - Multi-channel input is folded to mono by averaging channels; chroma
      analysis doesn't care about stereo.
    - `latest(n_seconds)` returns a contiguous copy of the most recent N
      seconds. The caller is on a normal asyncio thread, so we hold the lock
      briefly to grab a snapshot, then release.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np
import sounddevice as sd


SAMPLE_RATE = 22050     # matches bebop.io.audio_in's target_sr
BLOCK_SIZE = 1024       # ~46 ms; small enough that callbacks return fast


@dataclass
class DeviceInfo:
    index: int
    name: str
    channels: int
    samplerate: float


def list_input_devices() -> list[DeviceInfo]:
    """Enumerate all input-capable audio devices visible to PortAudio."""
    out: list[DeviceInfo] = []
    for i, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            out.append(DeviceInfo(
                index=i,
                name=dev["name"],
                channels=int(dev["max_input_channels"]),
                samplerate=float(dev["default_samplerate"]),
            ))
    return out


def find_blackhole() -> DeviceInfo | None:
    """Find a BlackHole device by name match. Returns the first hit or None."""
    for dev in list_input_devices():
        if "blackhole" in dev.name.lower():
            return dev
    return None


class AudioCaptureRing:
    """Thread-safe ring buffer fed by a sounddevice input stream.

    `capacity_seconds` should be at least as long as the chord-analysis window
    (~2 s) plus generous slack for jitter. Default is 10 s.
    """

    def __init__(
        self,
        device: int | str | None = None,
        capacity_seconds: float = 10.0,
        samplerate: int = SAMPLE_RATE,
    ) -> None:
        self.samplerate = samplerate
        self.capacity = int(capacity_seconds * samplerate)
        self._buf = np.zeros(self.capacity, dtype=np.float32)
        self._write_head = 0
        self._frames_written = 0   # monotonic; lets readers detect new data
        self._lock = threading.Lock()
        self._device = device
        self._stream: sd.InputStream | None = None

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        # indata shape is (frames, channels). Fold to mono by averaging.
        if indata.ndim == 2 and indata.shape[1] > 1:
            mono = indata.mean(axis=1).astype(np.float32, copy=False)
        else:
            mono = indata[:, 0].astype(np.float32, copy=False) if indata.ndim == 2 else indata.astype(np.float32, copy=False)

        with self._lock:
            n = len(mono)
            end = self._write_head + n
            if end <= self.capacity:
                self._buf[self._write_head:end] = mono
            else:
                # wrap
                first = self.capacity - self._write_head
                self._buf[self._write_head:] = mono[:first]
                self._buf[: n - first] = mono[first:]
            self._write_head = end % self.capacity
            self._frames_written += n

    def start(self) -> DeviceInfo:
        """Open the stream and return info about the device that was selected.

        If `device=None` was passed, we auto-pick BlackHole if present, else
        the system default input. Raises RuntimeError if neither is usable.
        """
        if self._stream is not None:
            raise RuntimeError("AudioCaptureRing already started")

        device = self._device
        if device is None:
            bh = find_blackhole()
            if bh is not None:
                device = bh.index
            # else fall through to PortAudio default

        self._stream = sd.InputStream(
            device=device,
            channels=1,
            samplerate=self.samplerate,
            blocksize=BLOCK_SIZE,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

        # report what we actually opened
        info = sd.query_devices(self._stream.device, "input")
        return DeviceInfo(
            index=int(self._stream.device) if isinstance(self._stream.device, int) else -1,
            name=info["name"],
            channels=int(info["max_input_channels"]),
            samplerate=float(self._stream.samplerate),
        )

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def latest(self, n_seconds: float) -> np.ndarray:
        """Copy out the most recent `n_seconds` of audio as a contiguous mono array.

        Returns silence (zeros) until the buffer has been filled at least once.
        """
        n = min(int(n_seconds * self.samplerate), self.capacity)
        with self._lock:
            if self._frames_written < n:
                return np.zeros(n, dtype=np.float32)
            start = (self._write_head - n) % self.capacity
            if start + n <= self.capacity:
                return self._buf[start:start + n].copy()
            tail = self.capacity - start
            out = np.empty(n, dtype=np.float32)
            out[:tail] = self._buf[start:]
            out[tail:] = self._buf[: n - tail]
            return out

    @property
    def frames_written(self) -> int:
        return self._frames_written

    def rms(self, n_seconds: float = 0.5) -> float:
        """RMS of the most recent N seconds — used as a silence gate."""
        y = self.latest(n_seconds)
        if y.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(y * y) + 1e-12))

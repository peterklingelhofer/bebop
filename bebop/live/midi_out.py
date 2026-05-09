"""Live MIDI output to a virtual MIDI bus (IAC Driver on macOS).

Wraps mido's port API with safer defaults: panic-on-shutdown, helpful errors
when the port can't be found, and a no-op fallback when nothing is open
(disk-only mode).

To set up IAC on macOS:
    1. Open Audio MIDI Setup (in /Applications/Utilities)
    2. Window → Show MIDI Studio
    3. Double-click the IAC Driver
    4. Check "Device is online" and add a Bus 1 if there isn't one
    5. In Logic: new software-instrument track, set its input to "IAC Driver Bus 1"

bebop sends piano on channel 1 and bass on channel 2 — point one Logic track
at ch 1 with a piano patch and another at ch 2 with a bass patch (or use the
"Auto demix by channel" option on a multi-timbral instrument).
"""

from __future__ import annotations

import contextlib

import mido


CHANNEL_PIANO = 0   # display channel 1
CHANNEL_BASS = 1    # display channel 2


def list_outputs() -> list[str]:
    """List available MIDI output port names. Empty if no backend / no ports."""
    try:
        return mido.get_output_names()
    except Exception as e:
        print(f"[midi_out] couldn't enumerate MIDI ports: {e}")
        return []


def find_iac(prefer: str = "IAC") -> str | None:
    """First MIDI output port whose name contains `prefer` (case-insensitive)."""
    needle = prefer.lower()
    for name in list_outputs():
        if needle in name.lower():
            return name
    return None


def resolve_port(name_or_substr: str | None) -> str | None:
    """Resolve a user-provided port spec:
        - None: auto-pick first IAC port (or None if none found)
        - exact match: returned as-is
        - substring: first port containing it (case-insensitive)
    """
    if name_or_substr is None:
        return find_iac()
    ports = list_outputs()
    if name_or_substr in ports:
        return name_or_substr
    needle = name_or_substr.lower()
    for p in ports:
        if needle in p.lower():
            return p
    return None


class MidiOut:
    """Thin wrapper around `mido.open_output` that's safe to construct without
    actually opening a port — useful for disk-only mode. `open()` is the only
    thing that touches the OS; until then this is a no-op sink.
    """

    def __init__(self, port_name: str | None = None) -> None:
        self.port_name = port_name
        self.port: mido.ports.BaseOutput | None = None
        # (channel, pitch) currently held by us — for panic
        self._sounding: set[tuple[int, int]] = set()

    @property
    def is_open(self) -> bool:
        return self.port is not None

    def open(self) -> str:
        """Open the underlying port. Returns the actual name used.
        Raises RuntimeError if no port can be resolved."""
        if self.port is not None:
            return self.port_name or ""
        name = resolve_port(self.port_name)
        if name is None:
            raise RuntimeError(
                "no matching MIDI output port found. enable IAC Driver in "
                "Audio MIDI Setup, or pass --midi-out 'NAME'. "
                "see available ports with: bebop live --list-midi-outs"
            )
        self.port = mido.open_output(name)
        self.port_name = name
        return name

    def close(self) -> None:
        if self.port is not None:
            self.panic()
            with contextlib.suppress(Exception):
                self.port.close()
        self.port = None

    def send_note_on(self, channel: int, pitch: int, velocity: int) -> None:
        if self.port is None:
            return
        v = max(1, min(127, velocity))
        self.port.send(mido.Message("note_on", channel=channel, note=pitch, velocity=v))
        self._sounding.add((channel, pitch))

    def send_note_off(self, channel: int, pitch: int) -> None:
        if self.port is None:
            return
        self.port.send(mido.Message("note_off", channel=channel, note=pitch, velocity=0))
        self._sounding.discard((channel, pitch))

    def panic(self) -> None:
        """Force-stop everything we believe is sounding, plus an
        all-notes-off CC on every channel for safety."""
        if self.port is None:
            return
        for ch, pitch in list(self._sounding):
            with contextlib.suppress(Exception):
                self.port.send(mido.Message("note_off", channel=ch, note=pitch, velocity=0))
        self._sounding.clear()
        for ch in range(16):
            with contextlib.suppress(Exception):
                self.port.send(mido.Message("control_change", channel=ch, control=123, value=0))

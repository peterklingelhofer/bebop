"""Real Book-style chord chart parser with mid-song key changes.

Format:
    `# ...` whole-line comments only (don't conflict with `#` accidentals).
    Header lines (anywhere, but typically at top):
        `bpm: 94`
        `time: 4/4`
        `key: D`         # initial key
    Mid-song key changes are declared on their own line between bar lines:
        `key: A`         # applies from the *next* bar onward
        `key: F at 32`   # explicit start beat (rare; bar-boundary form is preferred)
    Bar lines:
        `| Cmaj7 | Am7 | Dm7 G7 | Cmaj7 |`
        `| % |`          # repeat previous bar
        `| - |`          # skip bar (rest)
"""

from __future__ import annotations

import re
from pathlib import Path

from bebop.types import Chord, ChordSequence, KeyChange

_HEADER_RE = re.compile(r"^\s*(bpm|time|key)\s*:\s*(.+?)\s*$", re.IGNORECASE)
_BAR_SPLIT_RE = re.compile(r"\|")
_KEY_AT_RE = re.compile(r"^(.+?)\s+at\s+(\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
# inline comment in a HEADER value: whitespace + `#` + anything to EOL.
# (we only strip in headers because `#` is also a chord accidental in bar lines.)
_HEADER_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")


def parse_chart(path: str | Path) -> ChordSequence:
    text = Path(path).read_text()
    seq = ChordSequence()
    bar_idx = 0
    last_bar_chords: list[str] = []
    initial_key_set = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        header_match = _HEADER_RE.match(line)
        if header_match:
            key = header_match.group(1).lower()
            value = _HEADER_INLINE_COMMENT_RE.sub("", header_match.group(2)).strip()
            if key == "bpm":
                seq.bpm = float(value)
            elif key == "time":
                num, denom = value.split("/")
                seq.time_signature = (int(num), int(denom))
            elif key == "key":
                # accept either "key: D" (apply from current bar) or "key: D at 32" (explicit beat)
                m = _KEY_AT_RE.match(value)
                if m:
                    key_name, at_beat_str = m.group(1).strip(), m.group(2)
                    start = float(at_beat_str)
                else:
                    key_name = value.strip()
                    start = bar_idx * seq.beats_per_bar
                # if no key declared yet and we're at the start, this is the initial key
                if not initial_key_set and start == 0:
                    seq.key_map.append(KeyChange(start_beat=0.0, key=key_name))
                    initial_key_set = True
                else:
                    seq.key_map.append(KeyChange(start_beat=start, key=key_name))
                    initial_key_set = True
            continue

        # bar line — split on `|`, drop empty leading/trailing tokens
        bars = [b.strip() for b in _BAR_SPLIT_RE.split(line) if b.strip()]
        for bar in bars:
            chords_in_bar = bar.split()
            if chords_in_bar == ["%"]:
                chords_in_bar = last_bar_chords
            if not chords_in_bar:
                continue
            beats_per_bar = seq.beats_per_bar
            beats_each = beats_per_bar / len(chords_in_bar)
            for i, sym in enumerate(chords_in_bar):
                if sym == "-":
                    continue
                start = bar_idx * beats_per_bar + i * beats_each
                # parse optional slash bass: "C/G"
                bass = None
                if "/" in sym:
                    sym, bass = sym.split("/", 1)
                seq.chords.append(
                    Chord(symbol=sym, start_beat=start, duration_beats=beats_each, bass=bass)
                )
            last_bar_chords = chords_in_bar
            bar_idx += 1

    # sort key map (defensive: in case `at` clauses are out of order)
    seq.key_map.sort(key=lambda kc: kc.start_beat)
    return seq

"""Minimal palette.

Chrome is neutral grey so it recedes. Colour is spent only where it carries
meaning: a green -> amber -> red ramp for load, and two cool hues for network
direction. Packed 24-bit ints; -1 means the terminal's own background.
"""

from __future__ import annotations

BG = -1

# Chrome and text
BRIGHT = 0xFFFFFF   # selected rows, headings
FG = 0xE6EBF2       # primary values
MUTED = 0xA3ADBC    # secondary values
FAINT = 0x76818F    # labels
FRAME = 0x2C3340    # panel borders
FRAME_HI = 0x60A5FA # panel titles
GRID = 0x39424F     # meter track
GRIDLINE = 0x232A35 # graph rules
ROW_ALT = 0x141922  # zebra striping
SEL_BG = 0x1C3557   # selected row
ACCENT = 0x60A5FA

# Load semantics
OK = 0x4ADE80
WARN = 0xFBBF24
CRIT = 0xFF5252

# Network direction
DOWN = 0x38BDF8
UP = 0xC084FC

# Reclaimable memory: present, but subordinate to anything actually in use
CACHE = 0x5E82B5

GRAD = (0x3FA45C, 0x4ADE80, 0xBEE84A, 0xFBBF24, 0xFF5252)

WARN_AT, CRIT_AT = 0.60, 0.85

_cache: dict[int, int] = {}


def _mix(a: int, b: int, t: float) -> int:
    ar, ag, ab = (a >> 16) & 255, (a >> 8) & 255, a & 255
    br, bg, bb = (b >> 16) & 255, (b >> 8) & 255, b & 255
    # Interpolate squared channels so mid-tones stay luminous.
    r = int((ar * ar + (br * br - ar * ar) * t) ** 0.5)
    g = int((ag * ag + (bg * bg - ag * ag) * t) ** 0.5)
    bl = int((ab * ab + (bb * bb - ab * ab) * t) ** 0.5)
    return (r << 16) | (g << 8) | bl


def heat(t: float) -> int:
    """Smooth load ramp at t in [0,1]; quantised and memoised (hot path)."""
    k = 0 if t < 0 else 127 if t > 1 else int(t * 127)
    c = _cache.get(k)
    if c is None:
        pos = (k / 127.0) * (len(GRAD) - 1)
        i = int(pos)
        c = GRAD[-1] if i >= len(GRAD) - 1 else _mix(GRAD[i], GRAD[i + 1], pos - i)
        _cache[k] = c
    return c


def status(t: float) -> int:
    """Three-step colour for discrete readouts: healthy, warning, critical."""
    return OK if t < WARN_AT else WARN if t < CRIT_AT else CRIT


def value(t: float, idle: float = 0.005) -> int:
    """Colour for a numeric cell: greyed out when effectively zero."""
    return FAINT if t < idle else status(t)

"""Drawing primitives: braille area charts, sub-cell meters, panel frames, formatters."""

from __future__ import annotations

from . import palette as P
from .term import A_BOLD, Rect, Screen

# Braille dot bits, [column][row-from-top]
_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
_BRAILLE = [chr(0x2800 | m) for m in range(256)]
BLANK_DOT = "\u2800"


def _mask(bits) -> int:
    m = 0
    for b in bits:
        m |= b
    return m


# Per column-half: a fully lit cell, and a cell lit with `rem` dots growing up
# from the bottom (normal charts) or down from the top (flipped ones).
_FULL = tuple(_mask(d) for d in _DOTS)
_PART_UP = tuple(tuple(_mask(d[3 - j] for j in range(rem)) for rem in range(4))
                 for d in _DOTS)
_PART_DOWN = tuple(tuple(_mask(d[j] for j in range(rem)) for rem in range(4))
                   for d in _DOTS)

# Bars are drawn with rules rather than full blocks: block glyphs fill the whole
# cell, so stacked rows visually merge into boxes.
BAR = "\u2501"       # heavy rule, filled
BAR_HALF = "\u2578"  # heavy left half, for the fractional cell
TRACK = "\u2500"     # light rule, empty
SOFT = "\u2505"      # heavy dashes, secondary segment

TL, TR, BL, BR = "\u256d", "\u256e", "\u2570", "\u256f"
HZ, VT = "\u2500", "\u2502"


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# ------------------------------------------------------------------ format

def hbytes(n: float, pad: int = 0) -> str:
    n = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024.0 or u == "T":
            s = ("%d%s" % (n, u)) if u == "B" else (
                "%.0f%s" % (n, u) if abs(n) >= 100 else "%.1f%s" % (n, u))
            return s.rjust(pad)
        n /= 1024.0
    return "?"


def hrate(n: float, pad: int = 0) -> str:
    return (hbytes(n) + "/s").rjust(pad)


def hduration(sec: float) -> str:
    sec = int(max(0, sec))
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if d:
        return "%dd %02dh" % (d, h)
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm %02ds" % (m, s)


def hspan(sec: float) -> str:
    """Compact width of a time window, for graph axis labels."""
    if sec < 90:
        return "%ds" % int(sec)
    if sec < 5400:
        return "%dm" % round(sec / 60.0)
    return "%.1fh" % (sec / 3600.0)


def hcputime(sec: float) -> str:
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


def trunc(s: str, n: int) -> str:
    if n <= 0:
        return ""
    return s if len(s) <= n else s[: n - 1] + "\u2026"


# ------------------------------------------------------------------ widgets

def frame(scr: Screen, r: Rect, title: str = "", right: str = "", hot: float = -1.0) -> Rect:
    """Rounded panel border with an inline title; returns the inner rect."""
    if r.w < 2 or r.h < 2:
        return Rect(r.x, r.y, 0, 0)
    c = P.FRAME
    x2, y2 = r.x + r.w - 1, r.y + r.h - 1
    top = HZ * (r.w - 2)
    scr.put(r.x, r.y, TL + top + TR, c)
    scr.put(r.x, y2, BL + top + BR, c)
    for y in range(r.y + 1, y2):
        scr.cell(r.x, y, VT, c)
        scr.cell(x2, y, VT, c)
    if title:
        t = trunc(title.upper(), max(0, r.w - 6))
        scr.put(r.x + 2, r.y, " " + t + " ", P.FRAME_HI, attr=A_BOLD)
    if right and r.w > len(right) + len(title) + 9:
        col = P.heat(hot) if hot >= 0 else P.MUTED
        scr.put(x2 - len(right) - 2, r.y, " " + right + " ", col)
    return Rect(r.x + 2, r.y + 1, r.w - 4, r.h - 2)


def meter(scr: Screen, x: int, y: int, w: int, frac: float, track: bool = True,
          solid: int = -1) -> None:
    """Horizontal bar with half-cell precision, coloured by load threshold."""
    if w <= 0:
        return
    frac = clamp(frac, 0.0, 1.0)
    col = P.status(frac) if solid < 0 else solid
    units = frac * w
    full = int(units)
    # Anything non-zero keeps at least a stub, so a busy-but-small value never
    # renders as an empty bar.
    half = (units - full) >= 0.5 or (full == 0 and units > 0.02)
    for i in range(w):
        if i < full:
            scr.cell(x + i, y, BAR, col)
        elif i == full and half:
            scr.cell(x + i, y, BAR_HALF, col)
        elif track:
            scr.cell(x + i, y, TRACK, P.GRID)
        else:
            scr.cell(x + i, y, " ")


def stacked(scr: Screen, x: int, y: int, w: int, parts, track: bool = True) -> None:
    """Segmented bar. parts = [(fraction_of_whole, colour, glyph), ...].

    Distinct glyphs mean the segments stay readable without relying on colour.
    """
    if w <= 0:
        return
    pos = 0.0
    end = 0
    for frac, col, glyph in parts:
        start = int(round(pos * w))
        pos = min(1.0, pos + max(0.0, frac))
        stop = int(round(pos * w))
        for i in range(max(0, start), min(w, stop)):
            scr.cell(x + i, y, glyph, col)
        end = max(end, min(w, stop))
    if track:
        for i in range(end, w):
            scr.cell(x + i, y, TRACK, P.GRID)


def plot(scr: Screen, r: Rect, series, vmax: float, flip: bool = False,
         baseline: bool = True, solid: int = -1) -> None:
    """Braille area chart: 2 samples per column, 4 levels per row."""
    w, h = r.w, r.h
    if w <= 0 or h <= 0:
        return
    cols = w * 2
    data = list(series)[-cols:]
    if len(data) < cols:
        data = [0.0] * (cols - len(data)) + data
    inv = 1.0 / max(vmax, 1e-9)
    sub = h * 4
    top = h - 1
    grid = [[0] * w for _ in range(h)]
    part = _PART_DOWN if flip else _PART_UP
    for i in range(cols):
        v = data[i]
        if v <= 0:
            continue
        f = v * inv
        n = sub if f >= 1.0 else (int(f * sub + 0.5) or 1)
        # Whole cells are filled four dots at a time; only the tip is partial.
        q, rem = divmod(n, 4)
        half = i & 1
        cx = i >> 1
        full = _FULL[half]
        if flip:
            for gy in range(q):
                grid[gy][cx] |= full
            if rem:
                grid[q][cx] |= part[half][rem]
        else:
            for gy in range(top, top - q, -1):
                grid[gy][cx] |= full
            if rem:
                grid[top - q][cx] |= part[half][rem]

    span = max(1, h - 1)
    cells = scr.cells
    lo = max(0, -r.x)
    hi = min(w, scr.w - r.x)
    for gy in range(h):
        sy = r.y + gy
        if sy < 0 or sy >= scr.h:
            continue
        level = (gy / span) if flip else ((h - 1 - gy) / span)
        colr = solid if solid >= 0 else P.heat(level)
        on_base = baseline and (gy == 0 if flip else gy == top)
        srow = cells[sy]
        grow = grid[gy]
        base_cell = (BLANK_DOT, P.GRIDLINE, -1, 0)
        for cx in range(lo, hi):
            m = grow[cx]
            if m:
                srow[r.x + cx] = (_BRAILLE[m], colr, -1, 0)
            elif on_base:
                srow[r.x + cx] = base_cell


def gridlines(scr: Screen, r: Rect, levels=(0.25, 0.5, 0.75)) -> None:
    """Faint horizontal rules drawn before a plot so the plot sits on top."""
    if r.h < 3:
        return
    if r.h < 6:
        levels = (0.5,)
    for lv in levels:
        y = r.y + int(round((1.0 - lv) * (r.h - 1)))
        if r.y <= y < r.y + r.h:
            scr.fill(Rect(r.x, y, r.w, 1), "\u2508", P.GRIDLINE)


def scrollbar(scr: Screen, x: int, y: int, h: int, total: int, offset: int) -> None:
    """Vertical position indicator; draws nothing when everything fits."""
    if h <= 0 or total <= h:
        return
    size = max(1, int(h * h / total))
    span = max(1, total - h)
    pos = int(round((h - size) * min(1.0, offset / span)))
    for i in range(h):
        on = pos <= i < pos + size
        scr.cell(x, y + i, "\u2503" if on else "\u2502",
                 P.FRAME_HI if on else P.GRIDLINE)


def heatstrip(scr: Screen, x: int, y: int, values, width: int) -> None:
    """One cell per value, coloured by load (per-core heat map)."""
    n = min(len(values), width)
    for i in range(n):
        t = clamp(values[i] / 100.0, 0.0, 1.0)
        hot = t > 0.04
        scr.cell(x + i, y, BAR if hot else TRACK, P.status(t) if hot else P.GRID)


def hrule(scr: Screen, r: Rect, y: int, text: str = "") -> None:
    scr.put(r.x, r.y + y, "\u2500" * r.w, P.GRIDLINE)
    if text:
        scr.put(r.x + 1, r.y + y, " " + text + " ", P.FAINT)

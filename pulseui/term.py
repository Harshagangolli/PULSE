"""Terminal plumbing: raw mode, alternate screen, input decoding, and a
double-buffered screen that only emits the cells that actually changed."""

from __future__ import annotations

import os
import select
import signal
import sys
import termios
import tty

A_BOLD, A_DIM, A_ITALIC, A_UNDER, A_REV = 1, 2, 4, 8, 16

_ENTER = "\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J"
_LEAVE = "\x1b[?7h\x1b[?25h\x1b[?1049l\x1b[0m"
_MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
_MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"

_sgr_cache: dict[tuple[int, int, int], str] = {}


def sgr(fg: int, bg: int, attr: int) -> str:
    key = (fg, bg, attr)
    s = _sgr_cache.get(key)
    if s is None:
        p = ["0"]
        if attr:
            if attr & A_BOLD:
                p.append("1")
            if attr & A_DIM:
                p.append("2")
            if attr & A_ITALIC:
                p.append("3")
            if attr & A_UNDER:
                p.append("4")
            if attr & A_REV:
                p.append("7")
        if fg >= 0:
            p.append("38;2;%d;%d;%d" % ((fg >> 16) & 255, (fg >> 8) & 255, fg & 255))
        if bg >= 0:
            p.append("48;2;%d;%d;%d" % ((bg >> 16) & 255, (bg >> 8) & 255, bg & 255))
        s = "\x1b[" + ";".join(p) + "m"
        _sgr_cache[key] = s
    return s


class Rect:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x: int, y: int, w: int, h: int):
        self.x, self.y, self.w, self.h = x, y, max(0, w), max(0, h)

    def inset(self, dx: int, dy: int) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.w - 2 * dx, self.h - 2 * dy)

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def __repr__(self):
        return f"Rect({self.x},{self.y},{self.w},{self.h})"


BLANK = (" ", -1, -1, 0)
_SAFE = {c: 32 for c in range(32)}
_SAFE[127] = 32


class Screen:
    """Cell grid with a diff-based flush.

    flush() keeps the finished grid as the diff baseline instead of copying it,
    so clear() must start every frame — it is what hands drawing a fresh grid.
    """

    def __init__(self, w: int = 80, h: int = 24):
        self.w = self.h = 0
        self.cells: list[list[tuple]] = []
        self.prev: list[list[tuple]] | None = None
        self.resize(w, h)

    def resize(self, w: int, h: int) -> None:
        self.w, self.h = max(1, w), max(1, h)
        self.cells = [[BLANK] * self.w for _ in range(self.h)]
        self.prev = None

    def clear(self, bg: int = -1) -> None:
        blank = BLANK if bg < 0 else (" ", -1, bg, 0)
        w = self.w
        self.cells = [[blank] * w for _ in range(self.h)]

    def put(self, x: int, y: int, text: str, fg: int = -1, bg: int = -1, attr: int = 0) -> int:
        if y < 0 or y >= self.h or not text:
            return x
        w = self.w
        if x >= w:
            return x
        if not text.isprintable():
            text = text.translate(_SAFE)
        end = x + len(text)
        lo = x
        if lo < 0:
            text = text[-lo:]
            lo = 0
        if end > w:
            text = text[:w - lo]
        if text:
            self.cells[y][lo:lo + len(text)] = [(c, fg, bg, attr) for c in text]
        return end if end < w else w

    def put_clip(self, r: Rect, dx: int, dy: int, text: str, fg=-1, bg=-1, attr=0) -> None:
        """Draw text at rect-relative (dx,dy), truncated to the rect."""
        if dy < 0 or dy >= r.h or dx >= r.w:
            return
        if dx < 0:
            text = text[-dx:]
            dx = 0
        self.put(r.x + dx, r.y + dy, text[: r.w - dx], fg, bg, attr)

    def cell(self, x: int, y: int, ch: str, fg=-1, bg=-1, attr=0) -> None:
        if 0 <= y < self.h and 0 <= x < self.w:
            self.cells[y][x] = (ch, fg, bg, attr)

    def fill(self, r: Rect, ch: str = " ", fg=-1, bg=-1, attr=0) -> None:
        c = (ch, fg, bg, attr)
        x0 = max(0, r.x)
        x1 = min(self.w, r.x + r.w)
        if x1 <= x0:
            return
        span = [c] * (x1 - x0)
        for y in range(max(0, r.y), min(self.h, r.y + r.h)):
            self.cells[y][x0:x1] = span

    def shade(self, r: Rect, bg: int) -> None:
        """Repaint background, keeping glyphs and foreground."""
        for y in range(max(0, r.y), min(self.h, r.y + r.h)):
            row = self.cells[y]
            for x in range(max(0, r.x), min(self.w, r.x + r.w)):
                ch, fg, _, at = row[x]
                row[x] = (ch, fg, bg, at)

    def flush(self, out) -> None:
        parts: list[str] = []
        prev = self.prev
        cfg = cbg = -2
        cattr = -1
        cy = cx = -1
        w = self.w
        for y in range(self.h):
            row = self.cells[y]
            prow = prev[y] if prev is not None else None
            if prow is not None:
                if prow == row:
                    continue
                x0 = 0
                while x0 < w and row[x0] == prow[x0]:
                    x0 += 1
                x1 = w - 1
                while x1 > x0 and row[x1] == prow[x1]:
                    x1 -= 1
            else:
                x0, x1 = 0, w - 1
            if cy != y or cx != x0:
                parts.append("\x1b[%d;%dH" % (y + 1, x0 + 1))
                cy, cx = y, x0
            for x in range(x0, x1 + 1):
                ch, fg, bg, attr = row[x]
                if fg != cfg or bg != cbg or attr != cattr:
                    parts.append(sgr(fg, bg, attr))
                    cfg, cbg, cattr = fg, bg, attr
                parts.append(ch)
            cx = x1 + 1
        if parts:
            parts.append("\x1b[0m")
            out.write("".join(parts))
            out.flush()
        self.prev = self.cells

    def invalidate(self) -> None:
        self.prev = None


# ---------------------------------------------------------------- input

class Key:
    __slots__ = ("name", "ch", "mouse")

    def __init__(self, name: str, ch: str = "", mouse=None):
        self.name = name
        self.ch = ch
        self.mouse = mouse  # (button, col, row, pressed)

    def __repr__(self):
        return f"Key({self.name!r})"


_SEQ = {
    "[A": "up", "[B": "down", "[C": "right", "[D": "left",
    "[H": "home", "[F": "end", "[Z": "shift-tab",
    "OA": "up", "OB": "down", "OC": "right", "OD": "left",
    "OH": "home", "OF": "end",
    "[1~": "home", "[4~": "end", "[5~": "pgup", "[6~": "pgdn",
    "[2~": "insert", "[3~": "delete",
    "[7~": "home", "[8~": "end",
}


def decode(buf: str) -> tuple[list[Key], str]:
    """Turn a raw stdin chunk into keys; returns (keys, leftover)."""
    keys: list[Key] = []
    i, n = 0, len(buf)
    while i < n:
        c = buf[i]
        if c != "\x1b":
            if c == "\x03":
                keys.append(Key("ctrl-c"))
            elif c in ("\r", "\n"):
                keys.append(Key("enter"))
            elif c == "\t":
                keys.append(Key("tab"))
            elif c in ("\x7f", "\x08"):
                keys.append(Key("backspace"))
            elif c == "\x15":
                keys.append(Key("ctrl-u"))
            elif c < " ":
                keys.append(Key("ctrl-" + chr(ord(c) + 96)))
            else:
                keys.append(Key("char", c))
            i += 1
            continue
        # escape sequence
        if i + 1 >= n:
            return keys, buf[i:]
        nxt = buf[i + 1]
        if nxt == "[" and i + 2 < n and buf[i + 2] == "<":
            end = -1
            for j in range(i + 3, n):
                if buf[j] in "Mm":
                    end = j
                    break
            if end < 0:
                return keys, buf[i:]
            try:
                b, mx, my = (int(v) for v in buf[i + 3:end].split(";"))
                keys.append(Key("mouse", mouse=(b, mx - 1, my - 1, buf[end] == "M")))
            except ValueError:
                pass
            i = end + 1
            continue
        if nxt in "[O":
            for ln in (4, 3, 2):
                seq = buf[i + 1:i + 1 + ln]
                if seq in _SEQ:
                    keys.append(Key(_SEQ[seq]))
                    i += 1 + ln
                    break
            else:
                if i + 2 >= n:
                    return keys, buf[i:]
                end = -1
                for j in range(i + 2, n):
                    if buf[j].isalpha() or buf[j] == "~":
                        end = j
                        break
                if end < 0:
                    return keys, buf[i:]
                i = end + 1
            continue
        if nxt == "\x1b":
            keys.append(Key("escape"))
            i += 1
            continue
        keys.append(Key("alt-" + nxt))
        i += 2
    return keys, ""


class Terminal:
    def __init__(self, mouse: bool = True):
        self.fd = sys.stdin.fileno()
        self.out = sys.stdout
        self.saved = None
        self.mouse = mouse
        self.resized = True
        self._pending = ""
        self._active = False

    def size(self) -> tuple[int, int]:
        try:
            sz = os.get_terminal_size(self.out.fileno())
            return sz.columns, sz.lines
        except OSError:
            return 100, 32

    def __enter__(self) -> "Terminal":
        try:
            self.saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        except termios.error:
            self.saved = None
        self.out.write(_ENTER + (_MOUSE_ON if self.mouse else ""))
        self.out.flush()
        self._active = True
        try:
            signal.signal(signal.SIGWINCH, self._winch)
        except ValueError:
            pass
        return self

    def __exit__(self, *exc) -> None:
        self.restore()

    def restore(self) -> None:
        if not self._active:
            return
        self._active = False
        try:
            self.out.write((_MOUSE_OFF if self.mouse else "") + _LEAVE)
            self.out.flush()
        except Exception:
            pass
        if self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except termios.error:
                pass

    def _winch(self, *_):
        self.resized = True

    def wait(self, timeout: float) -> list[Key]:
        """Block up to `timeout` seconds; return any decoded keys."""
        # A bare ESC is indistinguishable from the start of an escape sequence,
        # so hold it briefly and emit it if nothing follows.
        lone_esc = self._pending == "\x1b"
        if lone_esc:
            timeout = min(timeout, 0.04)
        try:
            r, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            return []
        if not r:
            if lone_esc and self._pending == "\x1b":
                self._pending = ""
                return [Key("escape")]
            return []
        try:
            chunk = os.read(self.fd, 4096).decode("utf-8", "replace")
        except (OSError, InterruptedError):
            return []
        if not chunk:
            return []
        keys, self._pending = decode(self._pending + chunk)
        return keys

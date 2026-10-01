#!/usr/bin/env python3
"""Regression suite for pulse. Stdlib only, Linux only.

The optimised readers and drawing primitives are checked against reference
implementations of the code they replaced, so any behavioural drift fails here
rather than on someone's terminal.

    python3 -m unittest test_pulse -v
    python3 test_pulse.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pulseui import collect as C            # noqa: E402
from pulseui import draw as D               # noqa: E402
from pulseui import palette as P            # noqa: E402
from pulseui import panels                  # noqa: E402
from pulseui import term as T               # noqa: E402
from pulseui.app import (                   # noqa: E402
    INTERVALS, App, run_json, run_once, snapshot,
)
from pulseui.term import (                  # noqa: E402
    A_BOLD, A_DIM, A_ITALIC, A_REV, A_UNDER, BLANK, Rect, Screen, decode, sgr,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
HZ = C.HZ
PAGE = C.PAGE

# Captured before anything is monkeypatched, so the fakes can delegate safely.
_REAL = {n: getattr(os, n) for n in ("listdir", "open", "read", "close", "stat")}


# ====================================================================== helpers

class Null:
    """Stand-in for a terminal that throws bytes away."""

    def __init__(self):
        self.chunks = []

    def write(self, s):
        self.chunks.append(s)

    def flush(self):
        pass

    @property
    def text(self):
        return "".join(self.chunks)


@contextlib.contextmanager
def frozen_clock(t=1000.0, clock="12:34:56"):
    """Pin the wall clock so two renders of the same state are byte-identical."""
    with mock.patch.object(time, "strftime", return_value=clock), \
         mock.patch.object(time, "monotonic", return_value=t):
        yield


def exitcode(status):
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)


def fresh_sampler(settle=0.12):
    s = C.Sampler()
    s.sample()
    time.sleep(settle)
    s.sample()
    return s


def make_app(w=160, h=45, interval=2000):
    app = App(interval)
    app.s = fresh_sampler()
    scr = Screen(w, h)
    app.rebuild()
    app.draw(scr)
    return app, scr


def render_text(scr):
    return "\n".join("".join(c[0] for c in row) for row in scr.cells)


def has_tool(cmd):
    return any(os.path.exists(os.path.join(d, cmd))
               for d in ("/bin", "/usr/bin", "/sbin", "/usr/sbin"))


class _Key:
    __slots__ = ("name", "ch", "mouse")

    def __init__(self, name, ch="", mouse=None):
        self.name, self.ch, self.mouse = name, ch, mouse


# ------------------------------------------------------- reference (pre-opt)

def ref_sanitize_ascii(s):
    """The original ASCII branch, which the isprintable() fast path replaced."""
    return s.translate(C._CTRL)


def ref_read_cpu():
    """The original reader: cpu lines only, stopping at the first non-cpu line."""
    out, agg = [], []
    with open("/proc/stat", "rb") as f:
        for raw in f:
            if not raw.startswith(b"cpu"):
                if out:
                    break
                continue
            p = raw.split()
            v = [int(x) for x in p[1:9]] if len(p) >= 9 else [int(x) for x in p[1:]]
            while len(v) < 8:
                v.append(0)
            idle = v[3] + v[4]
            total = sum(v)
            out.append((total, total - idle))
            if not agg:
                agg = v
    return out, agg


def ref_read_statmisc():
    out = {"ctxt": 0, "forks": 0, "running": 0, "blocked": 0, "intr": 0, "btime": 0}
    names = {"ctxt": "ctxt", "processes": "forks", "procs_running": "running",
             "procs_blocked": "blocked", "intr": "intr", "btime": "btime"}
    with open("/proc/stat", "rb") as f:
        for raw in f:
            p = raw.split()
            if p and p[0].decode() in names:
                out[names[p[0].decode()]] = int(p[1])
    return out


def ref_plot(scr, r, series, vmax, flip=False, baseline=True, solid=-1):
    w, h = r.w, r.h
    if w <= 0 or h <= 0:
        return
    cols = w * 2
    data = list(series)[-cols:]
    data = [0.0] * (cols - len(data)) + data
    vmax = max(vmax, 1e-9)
    sub = h * 4
    grid = [[0] * w for _ in range(h)]
    for i, v in enumerate(data):
        cx, half = divmod(i, 2)
        n = int(D.clamp(v / vmax, 0.0, 1.0) * sub + 0.5)
        if n == 0 and v > 0:
            n = 1
        col = D._DOTS[half]
        for s in range(n):
            gy, dy = (s >> 2, s & 3) if flip else (h - 1 - (s >> 2), 3 - (s & 3))
            grid[gy][cx] |= col[dy]
    span = max(1, h - 1)
    for gy in range(h):
        level = (gy / span) if flip else ((h - 1 - gy) / span)
        colr = solid if solid >= 0 else P.heat(level)
        row = grid[gy]
        for cx in range(w):
            m = row[cx]
            if m:
                scr.cell(r.x + cx, r.y + gy, D._BRAILLE[m], colr)
            elif baseline and ((flip and gy == 0) or (not flip and gy == h - 1)):
                scr.cell(r.x + cx, r.y + gy, "\u2800", P.GRIDLINE)


class RefScreen(Screen):
    """Screen with the original clear/put/fill/flush bodies."""

    def clear(self, bg=-1):
        blank = BLANK if bg < 0 else (" ", -1, bg, 0)
        for y in range(self.h):
            self.cells[y] = [blank] * self.w

    def put(self, x, y, text, fg=-1, bg=-1, attr=0):
        if y < 0 or y >= self.h or not text:
            return x
        if not text.isprintable():
            text = text.translate(T._SAFE)
        row = self.cells[y]
        w = self.w
        for ch in text:
            if x >= w:
                break
            if x >= 0:
                row[x] = (ch, fg, bg, attr)
            x += 1
        return x

    def fill(self, r, ch=" ", fg=-1, bg=-1, attr=0):
        c = (ch, fg, bg, attr)
        for y in range(max(0, r.y), min(self.h, r.y + r.h)):
            row = self.cells[y]
            for x in range(max(0, r.x), min(self.w, r.x + r.w)):
                row[x] = c

    def flush(self, out):
        Screen.flush(self, out)
        self.prev = [r[:] for r in self.cells]


# --------------------------------------------------------------- ANSI replayer

class AnsiTerm:
    """Replays what flush() emits so the result can be compared against the cell
    grid it was supposed to reproduce."""

    _SEQ = re.compile(r"\x1b\[([0-9;]*)([Hm])")

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.cells = [[BLANK] * w for _ in range(h)]
        self.x = self.y = 0
        self.fg = self.bg = -1
        self.attr = 0

    def feed(self, s):
        i, n = 0, len(s)
        while i < n:
            if s[i] == "\x1b":
                m = self._SEQ.match(s, i)
                if m is None:
                    raise AssertionError("unparsable escape: %r" % s[i:i + 24])
                params = [int(v) for v in m.group(1).split(";") if v != ""]
                if m.group(2) == "H":
                    self.y = (params[0] if params else 1) - 1
                    self.x = (params[1] if len(params) > 1 else 1) - 1
                else:
                    self._sgr(params or [0])
                i = m.end()
                continue
            if 0 <= self.y < self.h and 0 <= self.x < self.w:
                self.cells[self.y][self.x] = (s[i], self.fg, self.bg, self.attr)
            self.x += 1
            i += 1

    def _sgr(self, p):
        i = 0
        while i < len(p):
            v = p[i]
            if v == 0:
                self.fg = self.bg = -1
                self.attr = 0
            elif v == 1:
                self.attr |= A_BOLD
            elif v == 2:
                self.attr |= A_DIM
            elif v == 3:
                self.attr |= A_ITALIC
            elif v == 4:
                self.attr |= A_UNDER
            elif v == 7:
                self.attr |= A_REV
            elif v in (38, 48) and i + 4 < len(p) and p[i + 1] == 2:
                col = (p[i + 2] << 16) | (p[i + 3] << 8) | p[i + 4]
                if v == 38:
                    self.fg = col
                else:
                    self.bg = col
                i += 4
            i += 1


# ------------------------------------------------------------- fake /proc tree

def build_stat(pid, comm, ppid=1, state="S", utime=0, stime=0, nice=0,
               threads=1, start=1000, vsize=0, rss=0, trailing=True):
    """A /proc/<pid>/stat line; f[] indices match the reader's post-comm split."""
    f = ["0"] * 30
    f[0] = state
    f[1] = str(ppid)
    f[11] = str(utime)
    f[12] = str(stime)
    f[16] = str(nice)
    f[17] = str(threads)
    f[19] = str(start)
    f[20] = str(vsize)
    f[21] = str(rss)
    line = "%d (%s) %s" % (pid, comm, " ".join(f))
    return (line + ("\n" if trailing else "")).encode()


class FakeProc:
    """Serves a synthetic /proc to collect.py, passing everything else through."""

    def __init__(self, procs, uid_map=None, deny=(), vanish=()):
        self.procs = procs                 # {pid: stat bytes}
        self.uid_map = uid_map or {}
        self.deny = set(deny)              # stat file cannot be opened
        self.vanish = set(vanish)          # directory gone by the time we stat it
        self.stat_calls = []
        self._fds = {}
        self._next = 900000

    def listdir(self, path):
        if path == "/proc":
            return [str(p) for p in self.procs] + ["self", "net", "1x", "kcore"]
        return _REAL["listdir"](path)

    def open(self, path, flags, *a, **kw):
        if isinstance(path, str) and path.startswith("/proc/") \
                and path.endswith("/stat"):
            pid = int(path.split("/")[2])
            if pid in self.deny:
                raise PermissionError(13, "denied")
            fd = self._next
            self._next += 1
            self._fds[fd] = self.procs[pid]
            return fd
        return _REAL["open"](path, flags, *a, **kw)

    def read(self, fd, n):
        if fd in self._fds:
            return self._fds[fd][:n]
        return _REAL["read"](fd, n)

    def close(self, fd):
        if fd in self._fds:
            del self._fds[fd]
            return None
        return _REAL["close"](fd)

    def stat(self, path, *a, **kw):
        m = isinstance(path, str) and re.fullmatch(r"/proc/(\d+)", path)
        if m:
            pid = int(m.group(1))
            self.stat_calls.append(pid)
            if pid in self.vanish:
                raise ProcessLookupError(3, "gone")
            return os.stat_result(
                (0o40555, 0, 0, 1, self.uid_map.get(pid, 0), 0, 0, 0, 0, 0))
        return _REAL["stat"](path, *a, **kw)

    def __enter__(self):
        self._patches = [mock.patch.object(os, n, getattr(self, n))
                         for n in ("listdir", "open", "read", "close", "stat")]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def bare_sampler(ncores=4, mem_total=8 << 30, uptime=10000.0):
    s = object.__new__(C.Sampler)
    s.ncores = ncores
    s.proc_prev = {}
    s.proc_ids = {}
    s._cmd_cache = {}
    s.mem = {"total": mem_total}
    s.uptime = uptime
    return s


def run_procs(fake, sampler=None, cpu_delta=1000, dt=1.0):
    s = sampler or bare_sampler()
    with fake:
        s._procs(cpu_delta, dt)
    return s


# ====================================================================== tests

class TestSanitize(unittest.TestCase):
    """sanitize() gained an isprintable() fast path; ASCII output must stay
    byte-identical, and nothing that renders in other than one column may pass."""

    def assertGridSafe(self, out):
        for ch in out:
            self.assertTrue(ch.isprintable(), repr(ch))
            self.assertEqual(unicodedata.combining(ch), 0, repr(ch))
            self.assertNotIn(unicodedata.east_asian_width(ch), ("W", "F"), repr(ch))

    def test_every_ascii_char(self):
        for i in range(128):
            self.assertEqual(C.sanitize(chr(i)), ref_sanitize_ascii(chr(i)),
                             "char %d" % i)

    def test_empty(self):
        self.assertEqual(C.sanitize(""), "")

    def test_fuzz_ascii_matches_original(self):
        rnd = random.Random(11)
        pool = [chr(i) for i in range(128)]
        for _ in range(5000):
            s = "".join(rnd.choice(pool) for _ in range(rnd.randint(0, 24)))
            self.assertEqual(C.sanitize(s), ref_sanitize_ascii(s), repr(s))

    def test_fuzz_unicode_is_grid_safe(self):
        rnd = random.Random(12)
        pool = ["a", " ", "\x00", "\x1b", "\x7f", "\u0301", "\u4e2d", "\uff21",
                "\u00e9", "\U0001F600", "\u200b", "\t", "\n", "\u3000", "\u2500",
                "\u202e", "\ufeff", "\x85", "\u0378", "\u00ad"]
        for _ in range(5000):
            s = "".join(rnd.choice(pool) for _ in range(rnd.randint(0, 20)))
            self.assertGridSafe(C.sanitize(s))

    def test_control_chars_removed(self):
        self.assertNotIn("\x1b", C.sanitize("a\x1b[31mb"))
        self.assertEqual(C.sanitize("a\x00b"), "a b")

    def test_wide_and_combining(self):
        self.assertEqual(C.sanitize("\u4e2d"), "?")
        self.assertEqual(C.sanitize("e\u0301"), "e")

    def test_bidi_zero_width_and_c1_are_neutralised(self):
        """Attacker-controlled names must not reorder or collapse the line."""
        for ch in ("\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
                   "\u2066", "\u2067", "\u2068", "\u2069",
                   "\u200b", "\u200e", "\u200f", "\ufeff", "\u00ad",
                   "\x85", "\x9b", "\u0378"):
            out = C.sanitize("a" + ch + "b")
            self.assertNotIn(ch, out, repr(ch))
            self.assertEqual(out, "a b", repr(ch))

    def test_clean_string_is_returned_unchanged(self):
        s = "plain name"
        self.assertIs(C.sanitize(s), s)


class TestStatParsing(unittest.TestCase):
    """_procs now uses raw fds and a bounded split; field mapping must hold."""

    def test_field_mapping(self):
        raw = build_stat(42, "worker", ppid=7, state="R", utime=300, stime=100,
                         nice=-5, threads=9, start=5000, vsize=1234, rss=64)
        s = run_procs(FakeProc({42: raw}, uid_map={42: 1000}))
        p, = s.procs
        self.assertEqual(p.pid, 42)
        self.assertEqual(p.ppid, 7)
        self.assertEqual(p.name, "worker")
        self.assertEqual(p.state, "R")
        self.assertEqual(p.nice, -5)
        self.assertEqual(p.threads, 9)
        self.assertEqual(p.vsz, 1234)
        self.assertEqual(p.rss, 64 * PAGE)
        self.assertEqual(p.start, 5000)
        self.assertEqual(p.uid, 1000)
        self.assertAlmostEqual(p.cputime, 400 / HZ)
        self.assertAlmostEqual(p.elapsed, 10000.0 - 5000 / HZ)

    def test_comm_with_spaces_and_parens(self):
        for comm in ("a b c", "evil ) name", "((nested))", ")", "(", " lead",
                     "trail ", "tab\there", "%s|weird", "x" * 15, ")("):
            s = run_procs(FakeProc({5: build_stat(5, comm)}))
            self.assertEqual(len(s.procs), 1, repr(comm))
            self.assertEqual(s.procs[0].name, C.sanitize(comm), repr(comm))

    def test_comm_with_control_chars_is_sanitized(self):
        s = run_procs(FakeProc({5: build_stat(5, "ev\x1bil\x07")}))
        self.assertEqual(s.procs[0].name, "ev il ")
        self.assertNotIn("\x1b", s.procs[0].name)

    def test_no_trailing_newline(self):
        s = run_procs(FakeProc({5: build_stat(5, "abc", trailing=False)}))
        self.assertEqual(s.procs[0].name, "abc")

    def test_truncated_and_garbage_lines_are_skipped(self):
        bad = {1: b"", 2: b"1 (x) S", 3: b"garbage without parens\n",
               4: b"5 (ok) S " + b"0 " * 4, 5: b"\x00\x01\x02",
               6: b"7 (x) S notanumber 0 0\n"}
        s = run_procs(FakeProc({**bad, 9: build_stat(9, "fine")}))
        self.assertEqual([p.pid for p in s.procs], [9])

    def test_unreadable_and_vanished_pids_are_skipped(self):
        procs = {i: build_stat(i, "p%d" % i) for i in (1, 2, 3, 4)}
        s = run_procs(FakeProc(procs, deny={2}, vanish={3}))
        self.assertEqual(sorted(p.pid for p in s.procs), [1, 4])

    def test_non_numeric_proc_entries_ignored(self):
        s = run_procs(FakeProc({7: build_stat(7, "x")}))
        self.assertEqual([p.pid for p in s.procs], [7])

    def test_counts_and_totals(self):
        procs = {i: build_stat(i, "p%d" % i, threads=i, rss=i)
                 for i in range(1, 11)}
        s = run_procs(FakeProc(procs))
        self.assertEqual(s.proc_count, 10)
        self.assertEqual(s.thread_count, sum(range(1, 11)))

    def test_cpu_percent_math(self):
        fake = FakeProc({1: build_stat(1, "a", utime=0, stime=0)})
        s = run_procs(fake, cpu_delta=1000)
        self.assertEqual(s.procs[0].cpu, 0.0)      # no previous sample yet
        # 250 of 1000 aggregate ticks on a 4-core box == one core saturated.
        fake.procs = {1: build_stat(1, "a", utime=200, stime=50)}
        with fake:
            s._procs(1000, 1.0)
        self.assertAlmostEqual(s.procs[0].cpu, 100.0)

    def test_cpu_percent_is_clamped(self):
        fake = FakeProc({1: build_stat(1, "a", utime=0)})
        s = run_procs(fake, cpu_delta=1000)
        fake.procs = {1: build_stat(1, "a", utime=10 ** 9)}
        with fake:
            s._procs(1000, 1.0)
        self.assertLessEqual(s.procs[0].cpu, 100.0 * s.ncores)
        fake.procs = {1: build_stat(1, "a", utime=0)}   # counter went backwards
        with fake:
            s._procs(1000, 1.0)
        self.assertGreaterEqual(s.procs[0].cpu, 0.0)

    def test_zero_cpu_delta_is_safe(self):
        s = run_procs(FakeProc({1: build_stat(1, "a", utime=5)}), cpu_delta=0)
        self.assertEqual(s.procs[0].cpu, 0.0)

    def test_zero_mem_total_is_safe(self):
        s = bare_sampler()
        s.mem = {"total": 0}
        run_procs(FakeProc({1: build_stat(1, "a", rss=10)}), sampler=s)
        self.assertGreaterEqual(s.procs[0].mem, 0.0)

    def test_parses_real_stat_files(self):
        """Feed the sampler every real /proc/<pid>/stat on this machine and
        compare against an independent parse of the very same bytes.

        Snapshotting first makes this deterministic: comparing a live sample to
        a later re-read races against processes that rename or change state.
        """
        raws, uids = {}, {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % entry, "rb") as f:
                    raw = f.read(1024)
                uids[int(entry)] = os.stat("/proc/" + entry).st_uid
                raws[int(entry)] = raw
            except OSError:
                continue
        self.assertGreater(len(raws), 20, "not enough live processes to verify")

        s = run_procs(FakeProc(raws, uid_map=uids), cpu_delta=0)
        got = {p.pid: p for p in s.procs}
        self.assertEqual(set(got), set(raws), "processes dropped while parsing")
        for pid, raw in raws.items():
            p = got[pid]
            close = raw.rindex(b")")
            name = C.sanitize(
                raw[raw.index(b"(") + 1:close].decode("utf-8", "replace"))
            f_ = raw[close + 2:].split()
            self.assertEqual(p.name, name, pid)
            self.assertEqual(p.ppid, int(f_[1]), pid)
            self.assertEqual(p.state, f_[0].decode(), pid)
            self.assertEqual(p.nice, int(f_[16]), pid)
            self.assertEqual(p.threads, int(f_[17]), pid)
            self.assertEqual(p.start, int(f_[19]), pid)
            self.assertEqual(p.vsz, int(f_[20]), pid)
            self.assertEqual(p.rss, int(f_[21]) * PAGE, pid)
            self.assertEqual(p.cputime, (int(f_[11]) + int(f_[12])) / HZ, pid)
            self.assertEqual(p.uid, uids[pid], pid)
            self.assertEqual(p.user, C._user(uids[pid]), pid)

    def test_live_sample_agrees_with_immutable_proc_fields(self):
        """A real sample must match the one field that can never change."""
        s = fresh_sampler()
        checked = 0
        for p in s.procs:
            try:
                with open("/proc/%d/stat" % p.pid, "rb") as f:
                    raw = f.read(1024)
            except OSError:
                continue                    # raced away, fine
            f_ = raw[raw.rindex(b")") + 2:].split()
            self.assertEqual(p.start, int(f_[19]), p.pid)
            checked += 1
        self.assertGreater(checked, 20)


class TestIdentityCache(unittest.TestCase):
    """uid/user are cached per pid and validated by start tick."""

    def test_stat_is_skipped_on_second_sample(self):
        procs = {i: build_stat(i, "p%d" % i) for i in range(1, 21)}
        fake = FakeProc(procs, uid_map={i: 1000 for i in range(1, 21)})
        s = run_procs(fake)
        self.assertEqual(len(fake.stat_calls), 20)
        fake.stat_calls.clear()
        with fake:
            s._procs(1000, 1.0)
        self.assertEqual(fake.stat_calls, [], "uid cache should avoid every stat")
        self.assertTrue(all(p.uid == 1000 for p in s.procs))

    def test_pid_reuse_refreshes_identity(self):
        fake = FakeProc({7: build_stat(7, "old", start=100)}, uid_map={7: 1000})
        s = run_procs(fake)
        self.assertEqual(s.procs[0].uid, 1000)
        # Same pid, different start tick == a different process.
        fake.procs = {7: build_stat(7, "new", start=999)}
        fake.uid_map = {7: 0}
        with fake:
            s._procs(1000, 1.0)
        self.assertEqual(s.procs[0].uid, 0, "stale uid served after pid reuse")
        self.assertEqual(s.procs[0].name, "new")
        self.assertEqual(s.procs[0].start, 999)

    def test_cache_does_not_grow_without_bound(self):
        s = None
        for gen in range(12):
            procs = {gen * 100 + i: build_stat(gen * 100 + i, "p", start=gen)
                     for i in range(30)}
            s = run_procs(FakeProc(procs), sampler=s)
        self.assertEqual(len(s.proc_ids), 30)
        self.assertEqual(len(s.proc_prev), 30)

    def test_name_is_not_cached(self):
        """comm can change at runtime; only identity is cached."""
        fake = FakeProc({3: build_stat(3, "before", start=5)})
        s = run_procs(fake)
        fake.procs = {3: build_stat(3, "after", start=5)}
        with fake:
            s._procs(1000, 1.0)
        self.assertEqual(s.procs[0].name, "after")

    def test_rss_is_not_cached(self):
        fake = FakeProc({3: build_stat(3, "x", start=5, rss=10)})
        s = run_procs(fake)
        fake.procs = {3: build_stat(3, "x", start=5, rss=99)}
        with fake:
            s._procs(1000, 1.0)
        self.assertEqual(s.procs[0].rss, 99 * PAGE)


class TestCmdlineCache(unittest.TestCase):
    def setUp(self):
        self.s = fresh_sampler()
        self.p = next(p for p in self.s.procs if p.pid == os.getpid())

    def test_matches_direct_read(self):
        self.assertEqual(self.s.cmdline(self.p), C.read_cmdline(os.getpid()))

    def test_second_call_is_cached(self):
        first = self.s.cmdline(self.p)
        with mock.patch.object(C, "read_cmdline",
                               side_effect=AssertionError("should not re-read")):
            self.assertEqual(self.s.cmdline(self.p), first)

    def test_start_tick_change_busts_cache(self):
        self.s.cmdline(self.p)
        self.p.start += 1                      # simulate pid reuse
        with mock.patch.object(C, "read_cmdline", return_value="/new/binary") as m:
            self.assertEqual(self.s.cmdline(self.p), "/new/binary")
            self.assertTrue(m.called)

    def test_kernel_thread_falls_back_to_name(self):
        p = C.Proc()
        p.pid, p.start, p.name = 2, 0, "kthreadd"
        with mock.patch.object(C, "read_cmdline", return_value=""):
            self.assertEqual(self.s.cmdline(p), "[kthreadd]")

    def test_cache_is_pruned_to_live_pids(self):
        for p in self.s.procs[:40]:
            self.s.cmdline(p)
        for g in range(10 ** 7, 10 ** 7 + len(self.s.procs) + 50):
            self.s._cmd_cache[g] = (0, "ghost")
        self.s.sample()
        self.assertLessEqual(len(self.s._cmd_cache), len(self.s.proc_prev))
        self.assertNotIn(10 ** 7, self.s._cmd_cache)

    def test_cmdline_is_sanitized_and_bounded(self):
        raw = b"/bin/sh\x00-c\x00echo \x1b[31mhi\x00" + b"A" * 99999
        with mock.patch("builtins.open", mock.mock_open(read_data=raw)):
            got = C.read_cmdline(1)
        self.assertNotIn("\x1b", got)
        self.assertLessEqual(len(got), 4096)

    def test_unreadable_cmdline(self):
        with mock.patch("builtins.open", side_effect=PermissionError):
            self.assertEqual(C.read_cmdline(1), "")


class TestReadCpu(unittest.TestCase):
    """read_cpu absorbed read_statmisc; both halves must still agree."""

    def test_cpu_rows_match_reference(self):
        for _ in range(3):
            out, agg, _ = C.read_cpu()
            rout, ragg = ref_read_cpu()
            self.assertEqual(len(out), len(rout))
            self.assertEqual(len(agg), len(ragg))
            self.assertEqual(len(out), (os.cpu_count() or 1) + 1)
            time.sleep(0.02)

    def test_misc_fields_match_reference(self):
        _, _, misc = C.read_cpu()
        ref = ref_read_statmisc()
        self.assertEqual(set(misc), set(ref))
        self.assertEqual(misc["btime"], ref["btime"])
        for k in ("ctxt", "forks", "intr"):
            self.assertGreater(misc[k], 0, k)
            self.assertLessEqual(abs(misc[k] - ref[k]),
                                 max(50000, ref[k] // 500), k)

    def test_aggregate_is_first_row(self):
        out, agg, _ = C.read_cpu()
        total, busy = out[0]
        self.assertEqual(total, sum(agg))
        self.assertEqual(busy, total - (agg[3] + agg[4]))

    def test_counters_are_monotonic(self):
        a, _, ma = C.read_cpu()
        time.sleep(0.05)
        b, _, mb = C.read_cpu()
        for (ta, _), (tb, _) in zip(a, b):
            self.assertGreaterEqual(tb, ta)
        self.assertGreaterEqual(mb["ctxt"], ma["ctxt"])
        self.assertEqual(ma["btime"], mb["btime"])

    def test_synthetic_stat_file(self):
        data = (b"cpu  100 20 30 40 5 0 1 2 0 0\n"
                b"cpu0 50 10 15 20 2 0 1 1 0 0\n"
                b"cpu1 50 10 15 20 3 0 0 1 0 0\n"
                b"intr 999 " + b"1 " * 500 + b"\n"
                b"ctxt 123456\n"
                b"btime 1600000000\n"
                b"processes 4242\n"
                b"procs_running 3\n"
                b"procs_blocked 1\n"
                b"softirq 555 " + b"2 " * 300 + b"\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=data)):
            out, agg, misc = C.read_cpu()
        self.assertEqual(len(out), 3)
        self.assertEqual(agg, [100, 20, 30, 40, 5, 0, 1, 2])
        self.assertEqual(out[0], (198, 198 - 45))
        self.assertEqual(misc, {"ctxt": 123456, "forks": 4242, "running": 3,
                                "blocked": 1, "intr": 999, "btime": 1600000000})

    def test_short_cpu_line_is_padded(self):
        data = b"cpu  1 2 3 4\ncpu0 1 2 3 4\nctxt 7\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=data)):
            _, agg, misc = C.read_cpu()
        self.assertEqual(agg, [1, 2, 3, 4, 0, 0, 0, 0])
        self.assertEqual(misc["ctxt"], 7)

    def test_missing_file(self):
        with mock.patch("builtins.open", side_effect=OSError):
            out, agg, misc = C.read_cpu()
        self.assertEqual((out, agg), ([], []))
        self.assertEqual(misc["ctxt"], 0)

    def test_stat_file_opened_once_per_sample(self):
        s = fresh_sampler()
        real, opens = io.open, []

        def counting(path, *a, **kw):
            if path == "/proc/stat":
                opens.append(path)
            return real(path, *a, **kw)

        with mock.patch("builtins.open", counting):
            s.sample()
        self.assertEqual(len(opens), 1, "/proc/stat read %d times" % len(opens))


class TestReadFreq(unittest.TestCase):
    def setUp(self):
        C._freq_paths = None
        C._freq_fallback = (0.0, 0.0)

    tearDown = setUp

    def test_probes_paths_once(self):
        n = os.cpu_count() or 1
        with mock.patch.object(os.path, "exists", return_value=False) as ex:
            with mock.patch.object(C, "_read", return_value=""):
                C.read_freq(n)
                first = ex.call_count
                C.read_freq(n)
                C.read_freq(n)
        self.assertEqual(ex.call_count, first, "cpufreq paths re-probed")

    def test_fallback_is_cached_but_expires(self):
        cpuinfo = "processor\t: 0\ncpu MHz\t\t: 2400.00\n"
        with mock.patch.object(os.path, "exists", return_value=False):
            with mock.patch.object(C, "_read", return_value=cpuinfo) as rd:
                self.assertAlmostEqual(C.read_freq(1), 2400.0)
                self.assertAlmostEqual(C.read_freq(1), 2400.0)
                self.assertEqual(rd.call_count, 1, "cpuinfo re-parsed every tick")
                C._freq_fallback = (time.monotonic() - 10.0, 2400.0)
                C.read_freq(1)
                self.assertEqual(rd.call_count, 2, "fallback cache never expires")

    def test_reads_cpufreq_when_present(self):
        with mock.patch.object(os.path, "exists", return_value=True):
            with mock.patch.object(C, "_readint", return_value=1_500_000):
                self.assertAlmostEqual(C.read_freq(4), 1500.0)

    def test_returns_zero_when_nothing_available(self):
        with mock.patch.object(os.path, "exists", return_value=False):
            with mock.patch.object(C, "_read", return_value=""):
                self.assertEqual(C.read_freq(2), 0.0)

    def test_live_value_is_sane(self):
        f = C.read_freq(os.cpu_count() or 1)
        self.assertGreaterEqual(f, 0.0)
        self.assertLess(f, 100000.0)


class TestCachedSources(unittest.TestCase):
    def test_iface_speed_reads_once(self):
        C._IFACE_SPEED.clear()
        with mock.patch.object(C, "_readint", return_value=1000) as rd:
            for _ in range(5):
                self.assertEqual(C.iface_speed("eth0"), 1000)
            self.assertEqual(rd.call_count, 1)
            C.iface_speed("eth1")
            self.assertEqual(rd.call_count, 2)
        C._IFACE_SPEED.clear()

    def test_whole_device_check_is_cached(self):
        C._WHOLE_DEV.clear()
        first = C.read_diskstats()
        with mock.patch.object(os.path, "isdir",
                               side_effect=AssertionError("re-probed /sys/block")):
            second = C.read_diskstats()
        self.assertEqual(set(first), set(second))

    def test_partitions_excluded_whole_devices_kept(self):
        for name in C.read_diskstats():
            self.assertTrue(os.path.isdir("/sys/block/" + name.replace("/", "!")),
                            "%s is not a whole device" % name)
            self.assertFalse(name.startswith(("loop", "ram", "zram", "fd", "sr")))

    def test_filesystem_usage_is_cached_then_refreshed(self):
        s = fresh_sampler()
        before = s.fs
        with mock.patch.object(C, "read_usage",
                               side_effect=AssertionError("statvfs every tick")):
            s.sample()
        self.assertIs(s.fs, before, "cached fs list was dropped")
        s._fs_at = time.monotonic() - 60.0
        with mock.patch.object(C, "read_usage", wraps=C.read_usage) as ru:
            s.sample()
        self.assertTrue(ru.called, "fs usage never refreshes")

    def test_battery_is_cached_then_refreshed(self):
        s = fresh_sampler()
        with mock.patch.object(C, "read_battery",
                               side_effect=AssertionError("read every tick")):
            s.sample()
        s._batt_at = time.monotonic() - 60.0
        with mock.patch.object(C, "read_battery", return_value=(55, "Charging")):
            s.sample()
        self.assertEqual(s.battery, (55, "Charging"))

    def test_temps_cached_even_without_sensors(self):
        s = fresh_sampler()
        with mock.patch.object(C, "read_temps",
                               side_effect=AssertionError("temps every tick")):
            s.sample()
        s._temp_at = time.monotonic() - 60.0
        with mock.patch.object(C, "read_temps", return_value=[("cpu", 44.0)]):
            s.sample()
        self.assertEqual(s.temp, 44.0)

    def test_mounts_cached_then_refreshed(self):
        s = fresh_sampler()
        with mock.patch.object(C, "read_mounts",
                               side_effect=AssertionError("mounts every tick")):
            s.sample()
        s._mount_at = time.monotonic() - 60.0
        s._fs_at = time.monotonic() - 60.0
        with mock.patch.object(C, "read_mounts", wraps=C.read_mounts) as rm:
            s.sample()
        self.assertTrue(rm.called)

    def test_pick_cpu_temp_prefers_package(self):
        temps = [("fan", 10.0), ("Package id 0", 55.0), ("Core 0", 50.0)]
        self.assertEqual(C.pick_cpu_temp(temps), 55.0)
        self.assertEqual(C.pick_cpu_temp([]), 0.0)


class TestScreenPrimitives(unittest.TestCase):
    def _pair(self, w, h):
        a, b = Screen(w, h), RefScreen(w, h)
        a.clear()
        b.clear()
        return a, b

    def test_put_fuzz_matches_reference(self):
        rnd = random.Random(21)
        pool = "ab \x00\x07\u2500\u2801\u2026"
        for _ in range(8000):
            w, h = rnd.randint(1, 30), rnd.randint(1, 12)
            a, b = self._pair(w, h)
            txt = "".join(rnd.choice(pool) for _ in range(rnd.randint(0, 24)))
            x, y = rnd.randint(-30, 40), rnd.randint(-4, h + 4)
            fg = rnd.choice([-1, 0x336699])
            bg = rnd.choice([-1, 0])
            at = rnd.choice([0, A_BOLD, A_DIM | A_UNDER])
            ra = a.put(x, y, txt, fg, bg, at)
            rb = b.put(x, y, txt, fg, bg, at)
            self.assertEqual(ra, rb, (txt, x, y, w, h))
            self.assertEqual(a.cells, b.cells, (txt, x, y, w, h))

    def test_put_return_value_chains(self):
        scr = Screen(20, 3)
        scr.clear()
        x = scr.put(0, 0, "abc")
        self.assertEqual(x, 3)
        self.assertEqual(scr.put(x, 0, "de"), 5)
        self.assertEqual("".join(c[0] for c in scr.cells[0][:5]), "abcde")

    def test_put_beyond_right_edge_returns_input(self):
        scr = Screen(10, 2)
        scr.clear()
        self.assertEqual(scr.put(15, 0, "ab"), 15)
        self.assertEqual(scr.put(10, 0, "ab"), 10)

    def test_put_never_writes_out_of_bounds(self):
        scr = Screen(8, 3)
        scr.clear()
        scr.put(-100, 1, "x" * 300)
        scr.put(200, 1, "y" * 10)
        scr.put(3, -5, "z")
        scr.put(3, 99, "z")
        self.assertEqual(len(scr.cells), 3)
        self.assertTrue(all(len(r) == 8 for r in scr.cells))

    def test_fill_fuzz_matches_reference(self):
        rnd = random.Random(22)
        for _ in range(3000):
            w, h = rnd.randint(1, 20), rnd.randint(1, 10)
            a, b = self._pair(w, h)
            r = Rect(rnd.randint(-6, w + 2), rnd.randint(-4, h + 2),
                     rnd.randint(0, w + 6), rnd.randint(0, h + 4))
            a.fill(r, "#", 1, 2, 3)
            b.fill(r, "#", 1, 2, 3)
            self.assertEqual(a.cells, b.cells, r)

    def test_clear_resets_every_cell(self):
        scr = Screen(12, 4)
        scr.put(0, 0, "xxxx", 1, 2, 3)
        scr.clear()
        self.assertTrue(all(c == BLANK for row in scr.cells for c in row))
        scr.clear(bg=0x101010)
        self.assertTrue(all(c == (" ", -1, 0x101010, 0)
                            for row in scr.cells for c in row))

    def test_clear_detaches_from_flushed_baseline(self):
        """flush() keeps the grid as prev; clear() must not mutate it."""
        scr, out = Screen(10, 3), Null()
        scr.clear()
        scr.put(0, 0, "hello")
        scr.flush(out)
        before = [r[:] for r in scr.prev]
        scr.clear()
        scr.put(0, 0, "world")
        self.assertEqual(scr.prev, before, "clear() corrupted the diff baseline")
        self.assertIsNot(scr.prev, scr.cells)
        for pr, cr in zip(scr.prev, scr.cells):
            self.assertIsNot(pr, cr)

    def test_resize_reallocates_and_invalidates(self):
        scr = Screen(10, 3)
        scr.clear()
        scr.flush(Null())
        scr.resize(40, 12)
        self.assertIsNone(scr.prev)
        self.assertEqual(scr.w, 40)
        self.assertEqual(len(scr.cells), 12)
        self.assertTrue(all(len(r) == 40 for r in scr.cells))

    def test_shade_keeps_glyph_and_fg(self):
        scr = Screen(6, 2)
        scr.clear()
        scr.put(0, 0, "ab", 0xAABBCC, -1, A_BOLD)
        scr.shade(Rect(0, 0, 6, 2), 0x111111)
        self.assertEqual(scr.cells[0][0], ("a", 0xAABBCC, 0x111111, A_BOLD))

    def test_rect_helpers(self):
        r = Rect(2, 3, 10, 5)
        self.assertTrue(r.contains(2, 3))
        self.assertFalse(r.contains(12, 3))
        self.assertFalse(r.contains(1, 3))
        self.assertEqual((r.inset(1, 1).x, r.inset(1, 1).w), (3, 8))
        self.assertEqual(Rect(0, 0, -5, -5).w, 0)


class TestFlushProtocol(unittest.TestCase):
    """Replay the emitted escapes and compare against the intended grid."""

    def _check(self, scr, term, out):
        term.feed(out.text)
        out.chunks.clear()
        self.assertEqual(term.cells, scr.cells)

    def test_replay_matches_grid_random_edits(self):
        rnd = random.Random(31)
        w, h = 40, 12
        scr, out = Screen(w, h), Null()
        term = AnsiTerm(w, h)
        for _ in range(150):
            scr.clear()
            for _ in range(rnd.randint(0, 14)):
                scr.put(rnd.randint(-3, w), rnd.randint(0, h - 1),
                        "".join(rnd.choice("abcXY .\u2500\u2801")
                                for _ in range(rnd.randint(1, 12))),
                        rnd.choice([-1, 0xFF0000, 0x00FF00]),
                        rnd.choice([-1, 0x000040]),
                        rnd.choice([0, A_BOLD, A_DIM | A_UNDER, A_REV, A_ITALIC]))
            scr.flush(out)
            self._check(scr, term, out)

    def test_replay_matches_real_frames(self):
        app, scr = make_app(120, 38)
        out, term = Null(), AnsiTerm(120, 38)
        for i in range(10):
            if i == 3:
                app.sort = "mem"
            if i == 5:
                app.mode = "help"
            if i == 7:
                app.mode, app.zoom = "normal", True
            if i == 9:
                app.zoom = False
            app.s.sample()
            app.rebuild()
            with frozen_clock():
                app.draw(scr)
                scr.flush(out)
            self._check(scr, term, out)

    def test_unchanged_frame_emits_nothing(self):
        app, scr = make_app(100, 30)
        out = Null()
        with frozen_clock():
            app.draw(scr)
            scr.flush(out)
            out.chunks.clear()
            app.draw(scr)                     # identical content
            scr.flush(out)
        self.assertEqual(out.text, "", "redundant repaint of an identical frame")

    def test_output_matches_reference_screen_stream(self):
        app = make_app(110, 34)[0]
        a, b = Screen(110, 34), RefScreen(110, 34)
        oa, ob = Null(), Null()
        for i in range(8):
            if i == 2:
                app.tree = True
                app.rebuild()
            if i == 4:
                app.mode = "listeners"
                app.listeners = C.read_listeners()
                app.lowners = {}
            if i == 6:
                app.mode, app.tree = "normal", False
                app.rebuild()
            with frozen_clock():
                app.draw(a)
                a.flush(oa)
                app.draw(b)
                b.flush(ob)
        self.assertEqual(oa.chunks, ob.chunks)

    def test_invalidate_forces_full_repaint(self):
        app, scr = make_app(80, 24)
        out = Null()
        with frozen_clock():
            app.draw(scr)
            scr.flush(out)
            out.chunks.clear()
            scr.invalidate()
            app.draw(scr)
            scr.flush(out)
        self.assertTrue(out.text)
        term = AnsiTerm(80, 24)
        term.feed(out.text)
        self.assertEqual(term.cells, scr.cells)

    def test_resize_then_flush_is_consistent(self):
        app, scr = make_app(100, 30)
        out = Null()
        with frozen_clock():
            app.draw(scr)
            scr.flush(out)
            out.chunks.clear()
            scr.resize(70, 20)
            app.draw(scr)
            scr.flush(out)
        term = AnsiTerm(70, 20)
        term.feed(out.text)
        self.assertEqual(term.cells, scr.cells)

    def test_output_always_ends_with_a_reset(self):
        """Without the trailing reset the terminal keeps pulse's last colour."""
        app, scr = make_app(90, 28)
        out = Null()
        for _ in range(4):
            app.s.sample()
            app.rebuild()
            app.draw(scr)
            out.chunks.clear()
            scr.flush(out)
            if out.text:
                self.assertTrue(out.text.endswith("\x1b[0m"),
                                repr(out.text[-16:]))

    def test_primitive_flush_ends_with_a_reset(self):
        scr, out = Screen(12, 3), Null()
        scr.clear()
        scr.put(0, 0, "hi", 0xFF0000, 0x00FF00, A_BOLD)
        scr.flush(out)
        self.assertTrue(out.text.endswith("\x1b[0m"), repr(out.text))


class TestPlot(unittest.TestCase):
    def test_fuzz_matches_reference(self):
        rnd = random.Random(41)
        for t in range(800):
            W, H = rnd.randint(20, 90), rnd.randint(10, 40)
            a, b = Screen(W, H), Screen(W, H)
            a.clear()
            b.clear()
            r = Rect(rnd.randint(-3, W - 2), rnd.randint(0, H - 2),
                     rnd.randint(1, W), rnd.randint(1, 12))
            n = rnd.randint(0, 200)
            kind = t % 5
            if kind == 0:
                ser = [rnd.random() * 100 for _ in range(n)]
            elif kind == 1:
                ser = [0.0] * n
            elif kind == 2:
                ser = [rnd.choice([0, 1e-9, 0.001, 100, 1e6]) for _ in range(n)]
            elif kind == 3:
                ser = [-5 + rnd.random() * 110 for _ in range(n)]
            else:
                ser = [float(rnd.randint(0, 100)) for _ in range(n)]
            vmax = rnd.choice([100.0, 1.0, 1e-12, max(ser or [1.0]), 0.0])
            flip, base = bool(t % 2), bool(t % 3)
            solid = -1 if t % 5 else 0x00FF00
            D.plot(a, r, ser, vmax, flip, base, solid)
            ref_plot(b, r, ser, vmax, flip, base, solid)
            self.assertEqual(a.cells, b.cells,
                             "plot mismatch r=%r vmax=%r flip=%s" % (r, vmax, flip))

    def test_zero_sized_rect_is_noop(self):
        scr = Screen(10, 4)
        scr.clear()
        before = [r[:] for r in scr.cells]
        D.plot(scr, Rect(0, 0, 0, 5), [1, 2, 3], 10.0)
        D.plot(scr, Rect(0, 0, 5, 0), [1, 2, 3], 10.0)
        self.assertEqual(scr.cells, before)

    def test_full_scale_fills_every_dot(self):
        scr = Screen(4, 3)
        scr.clear()
        D.plot(scr, Rect(0, 0, 4, 3), [100.0] * 8, 100.0, baseline=False)
        for y in range(3):
            for x in range(4):
                self.assertEqual(scr.cells[y][x][0], chr(0x28FF))

    def test_tiny_value_still_shows(self):
        scr = Screen(2, 3)
        scr.clear()
        D.plot(scr, Rect(0, 0, 2, 3), [0.0001] * 4, 100.0, baseline=False)
        self.assertNotEqual(scr.cells[2][0][0], " ")

    def test_never_draws_outside_rect(self):
        rnd = random.Random(42)
        for _ in range(300):
            W, H = 30, 12
            scr = Screen(W, H)
            scr.clear()
            r = Rect(rnd.randint(0, 20), rnd.randint(0, 8),
                     rnd.randint(1, 10), rnd.randint(1, 4))
            D.plot(scr, r, [rnd.random() * 50 for _ in range(40)], 50.0)
            for y in range(H):
                for x in range(W):
                    if not (r.x <= x < r.x + r.w and r.y <= y < r.y + r.h):
                        self.assertEqual(scr.cells[y][x], BLANK, (x, y, r))

    def test_flip_mirrors_orientation(self):
        up, down = Screen(4, 4), Screen(4, 4)
        up.clear()
        down.clear()
        ser = [100.0] * 2 + [0.0] * 6
        D.plot(up, Rect(0, 0, 4, 4), ser, 100.0, flip=False, baseline=False)
        D.plot(down, Rect(0, 0, 4, 4), ser, 100.0, flip=True, baseline=False)
        self.assertNotEqual(up.cells[0][0][0], " ")
        self.assertNotEqual(down.cells[0][0][0], " ")
        self.assertEqual(up.cells[0][3], BLANK)
        self.assertEqual(down.cells[3][3], BLANK)


class TestGridInvariants(unittest.TestCase):
    """Hostile process names must never break the cell grid."""

    EVIL = ["\x1b[31mRED", "a\x00b", "\u4e2d\u6587\u30c6\u30b9\u30c8",
            "e\u0301\u0301\u0301", "\uff21\uff22", "tab\there", "nl\nhere",
            "\x07bell", "\u202eRTL", "x" * 200, "\r\rcarriage", "\x1b]0;t\x07"]

    def test_names_render_within_grid(self):
        app, scr = make_app(120, 36)
        for i, p in enumerate(app.visible[:len(self.EVIL)]):
            p.name = C.sanitize(self.EVIL[i])
            p.cmd = C.sanitize(self.EVIL[i] + " --flag")
        app.draw(scr)
        self.assertEqual(len(scr.cells), 36)
        for row in scr.cells:
            self.assertEqual(len(row), 120)
            for ch, fg, bg, attr in row:
                self.assertEqual(len(ch), 1, repr(ch))
                self.assertTrue(ch.isprintable() or ch == " ", repr(ch))
                self.assertNotIn(unicodedata.east_asian_width(ch), ("W", "F"))
                self.assertEqual(unicodedata.combining(ch), 0)

    def test_escape_sequences_never_reach_output(self):
        app, scr = make_app(110, 32)
        for p in app.visible[:20]:
            p.name = C.sanitize("\x1b[2J\x1b[31mboom")
            p.cmd = C.sanitize("\x1b]0;title\x07")
        out = Null()
        app.draw(scr)
        scr.flush(out)
        body = re.sub(r"\x1b\[[0-9;]*[Hm]", "", out.text)
        self.assertNotIn("\x1b", body)
        self.assertNotIn("\x07", body)

    def test_filter_text_is_clipped(self):
        app, scr = make_app(80, 24)
        app.mode = "filter"
        app.filter = "x" * 500
        app.draw(scr)
        self.assertTrue(all(len(r) == 80 for r in scr.cells))

    def test_long_hostname_and_toast(self):
        app, scr = make_app(80, 24)
        app.s.host = "h" * 300
        app.notify("t" * 300)
        app.draw(scr)
        self.assertTrue(all(len(r) == 80 for r in scr.cells))


class TestGroundTruth(unittest.TestCase):
    """Cross-check readings against independent sources."""

    @classmethod
    def setUpClass(cls):
        cls.s = fresh_sampler(0.25)

    def test_core_count(self):
        self.assertEqual(self.s.ncores, os.cpu_count())
        self.assertEqual(len(self.s.cores), self.s.ncores)
        self.assertEqual(len(self.s.core_hist), self.s.ncores)

    def test_uptime_matches_proc(self):
        with open("/proc/uptime") as f:
            real = float(f.read().split()[0])
        self.assertLess(abs(self.s.uptime - real), 10.0)

    def test_loadavg_matches_proc(self):
        with open("/proc/loadavg") as f:
            p = f.read().split()
        self.assertAlmostEqual(self.s.load[0], float(p[0]), delta=2.0)
        self.assertEqual(self.s.load[4], int(p[3].split("/")[1]))

    def test_memory_matches_meminfo(self):
        m = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                m[k] = int(v.split()[0]) * 1024
        self.assertEqual(self.s.mem["total"], m["MemTotal"])
        self.assertAlmostEqual(self.s.mem["avail"], m["MemAvailable"],
                               delta=512 << 20)
        self.assertEqual(self.s.mem["used"],
                         max(0, self.s.mem["total"] - self.s.mem["avail"]))
        self.assertEqual(self.s.mem["swap_total"], m["SwapTotal"])
        self.assertEqual(self.s.mem["swap_used"],
                         max(0, m["SwapTotal"] - m["SwapFree"]))

    def test_kernel_and_host(self):
        self.assertEqual(self.s.kernel, os.uname().release)
        self.assertTrue(self.s.host)

    @unittest.skipUnless(has_tool("ps"), "ps not available")
    def test_process_table_agrees_with_ps(self):
        raw = subprocess.run(["ps", "-eo", "pid,ppid,uid", "--no-headers"],
                             capture_output=True, text=True, timeout=60).stdout
        theirs = {}
        for line in raw.splitlines():
            f = line.split()
            if len(f) == 3:
                theirs[int(f[0])] = (int(f[1]), int(f[2]))
        ours = {p.pid: (p.ppid, p.uid) for p in self.s.procs}
        common = set(ours) & set(theirs)
        self.assertGreater(len(common), 20, "process tables barely overlap")
        bad = [pid for pid in common if ours[pid] != theirs[pid]]
        # ps and pulse sample at different instants, so allow a little drift.
        self.assertLessEqual(len(bad), max(1, len(common) // 50),
                             "ppid/uid disagree for %r" % bad[:10])

    @unittest.skipUnless(has_tool("df"), "df not available")
    def test_filesystems_agree_with_df(self):
        raw = subprocess.run(["df", "-P", "-B1"], capture_output=True,
                             text=True, timeout=60).stdout
        theirs = {}
        for line in raw.splitlines()[1:]:
            f = line.split()
            if len(f) >= 6:
                theirs[f[5]] = int(f[1])
        checked = 0
        for fs in self.s.fs:
            if fs["mnt"] in theirs:
                self.assertAlmostEqual(fs["total"], theirs[fs["mnt"]],
                                       delta=max(1 << 20, fs["total"] // 50),
                                       msg=fs["mnt"])
                checked += 1
        self.assertGreater(checked, 0, "no filesystem could be cross-checked")

    def test_network_totals_match_proc_net_dev(self):
        raw = {}
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                p = rest.split()
                raw[name.strip()] = (int(p[0]), int(p[8]))
        for n in self.s.nets:
            self.assertIn(n["name"], raw)
            self.assertGreaterEqual(raw[n["name"]][0], n["rx_total"])
            self.assertNotEqual(n["name"], "lo")

    def test_listening_ports_are_plausible(self):
        for proto, bind, port, uid, inode in C.read_listeners():
            self.assertIn(proto, ("tcp", "udp"))
            self.assertTrue(0 < port < 65536, port)
            self.assertGreaterEqual(uid, 0)
            self.assertTrue(bind)

    def test_percentages_in_range(self):
        s = self.s
        self.assertTrue(0.0 <= s.cpu <= 100.0)
        self.assertTrue(all(0.0 <= c <= 100.0 for c in s.cores))
        self.assertTrue(all(0.0 <= v <= 100.0 for v in s.cpu_mix))
        for p in s.procs:
            self.assertTrue(0.0 <= p.cpu <= 100.0 * s.ncores, p.name)
            self.assertTrue(0.0 <= p.mem <= 100.0, p.name)
        for d in s.disks:
            self.assertTrue(0.0 <= d["util"] <= 100.0)
        for f in s.fs:
            self.assertTrue(0.0 <= f["pct"] <= 100.0)

    def test_self_is_present_and_correct(self):
        me = [p for p in self.s.procs if p.pid == os.getpid()]
        self.assertEqual(len(me), 1)
        self.assertEqual(me[0].uid, os.getuid())
        self.assertEqual(me[0].user, C._user(os.getuid()))


class TestApp(unittest.TestCase):
    def test_sort_keys_all_work(self):
        app, scr = make_app()
        for key in panels.SORT_KEYS:
            app.sort = key
            for desc in (True, False):
                app.sort_desc = desc
                app.rebuild()
                app.draw(scr)
                self.assertTrue(app.visible)

    def test_sort_order_is_correct(self):
        app, _ = make_app()
        app.sort, app.sort_desc = "cpu", True
        app.rebuild()
        vals = [p.cpu for p in app.visible]
        self.assertEqual(vals, sorted(vals, reverse=True))
        app.sort_desc = False
        app.rebuild()
        vals = [p.cpu for p in app.visible]
        self.assertEqual(vals, sorted(vals))

    def test_name_sort_is_ascending_by_default(self):
        app, _ = make_app()
        app.sort, app.sort_desc = "name", True
        app.rebuild()
        names = [p.name.lower() for p in app.visible]
        self.assertEqual(names, sorted(names))

    def test_filter_matches_pid(self):
        app, _ = make_app()
        app.filter = str(os.getpid())
        app.rebuild()
        self.assertIn(os.getpid(), [p.pid for p in app.visible])

    def test_filter_no_match(self):
        app, _ = make_app()
        app.filter = "zzz-definitely-no-such-process-zzz"
        app.rebuild()
        self.assertEqual(app.visible, [])

    def test_filter_is_case_insensitive(self):
        app, _ = make_app()
        target = app.visible[0].name.strip()
        if not target:
            self.skipTest("no usable process name")
        app.filter = target.upper()
        app.rebuild()
        self.assertTrue(app.visible)

    def test_filter_all_terms_must_match(self):
        app, _ = make_app()
        app.filter = "%s zzz-nope" % os.getpid()
        app.rebuild()
        self.assertEqual(app.visible, [])

    def test_only_mine_and_kernel_toggles(self):
        app, _ = make_app()
        app.only_mine = True
        app.rebuild()
        self.assertTrue(all(p.uid == os.getuid() for p in app.visible))
        app.only_mine = False
        app.show_kernel = False
        app.rebuild()
        self.assertTrue(all(not (p.ppid == 2 or p.pid == 2) for p in app.visible))
        app.show_kernel = True
        app.rebuild()
        self.assertGreaterEqual(len(app.visible), 1)

    def test_tree_mode_parents_precede_children(self):
        app, _ = make_app()
        app.tree = True
        app.show_kernel = True
        app.rebuild()
        seen = set()
        for p in app.visible:
            if p.depth > 0:
                self.assertIn(p.ppid, seen, "child %d before parent" % p.pid)
            seen.add(p.pid)

    def test_tree_keeps_every_process(self):
        app, _ = make_app()
        app.show_kernel = True
        app.rebuild()
        flat = len(app.visible)
        app.tree = True
        app.rebuild()
        self.assertEqual(len(app.visible), flat)

    def test_tree_handles_cycles(self):
        app, _ = make_app()
        a, b = C.Proc(), C.Proc()
        a.pid, a.ppid, b.pid, b.ppid = 90001, 90002, 90002, 90001
        for p in (a, b):
            p.name, p.user, p.uid, p.cpu, p.mem = "cyc", "x", 0, 0.0, 0.0
            p.rss = p.vsz = p.threads = p.nice = 0
            p.state, p.cputime, p.elapsed, p.start = "S", 0.0, 0.0, 0
        app.s.procs = app.s.procs + [a, b]
        app.tree = True
        app.rebuild()                      # must terminate
        self.assertTrue(app.visible)

    def test_selection_pins_across_resort(self):
        app, _ = make_app()
        app.selected = 5
        app.pin()
        pid = app.selected_pid
        app.sort = "mem"
        app.rebuild()
        if pid in [p.pid for p in app.visible]:
            self.assertEqual(app.current().pid, pid)

    def test_reset_restores_defaults(self):
        app, _ = make_app()
        app.sort, app.tree, app.filter = "mem", True, "x"
        app.only_mine = app.zoom = app.show_kernel = True
        app.selected_pid = 1
        self.assertFalse(app.is_default)
        app.reset()
        self.assertTrue(app.is_default)

    def test_reset_keeps_rate_and_pause(self):
        app, _ = make_app()
        app.interval_ms, app.paused = 5000, True
        app.sort = "mem"
        app.reset()
        self.assertEqual(app.interval_ms, 5000)
        self.assertTrue(app.paused)

    def test_clamp_scroll_bounds(self):
        app, _ = make_app()
        app.page = 10
        for n in (0, 1, 5, 10, 11, 500):
            for sel in (-50, 0, 3, 499, 10 ** 6):
                app.selected, app.scroll = sel, sel
                app.clamp_scroll(n)
                self.assertTrue(0 <= app.selected <= max(0, n - 1))
                self.assertTrue(0 <= app.scroll <= max(0, n - app.page))

    def test_layout_stays_inside_screen(self):
        for w in (60, 80, 99, 100, 140, 300):
            for h in (16, 20, 30, 60):
                app, _ = make_app(w, h)
                lay = app.layout(w, h)
                self.assertIn("proc", lay)
                for r in lay.values():
                    self.assertGreaterEqual(r.w, 0)
                    self.assertGreaterEqual(r.h, 0)
                    self.assertLessEqual(r.x + r.w, w)
                    self.assertLessEqual(r.y + r.h, h)

    def test_zoom_gives_everything_to_processes(self):
        app, _ = make_app(120, 40)
        app.zoom = True
        self.assertEqual(set(app.layout(120, 40)), {"proc"})

    def test_renders_at_many_sizes(self):
        app, _ = make_app()
        for w, h in [(1, 1), (10, 5), (59, 15), (60, 16), (61, 17), (80, 24),
                     (100, 30), (120, 40), (200, 60), (400, 100), (61, 200),
                     (300, 17)]:
            scr = Screen(w, h)
            app.draw(scr)
            scr.flush(Null())
            self.assertEqual(len(scr.cells), h)
            self.assertTrue(all(len(r) == w for r in scr.cells))

    def test_too_small_notice(self):
        app, _ = make_app()
        scr = Screen(30, 10)
        app.draw(scr)
        self.assertTrue(render_text(scr).strip())

    def test_all_overlays_render(self):
        app, scr = make_app(150, 45)
        app.listeners = C.read_listeners()
        app.lowners = C.socket_owners(r[4] for r in app.listeners)
        app.detail_proc = app.visible[0]
        app.pending_kill = ("SIGTERM", app.visible[0])
        for mode, marker in (("help", "KEYS"), ("listeners", "LISTENING"),
                             ("detail", "cgroup"), ("confirm", "CONFIRM")):
            app.mode = mode
            app.draw(scr)
            self.assertIn(marker, render_text(scr), mode)

    def test_empty_process_list(self):
        app, scr = make_app()
        app.s.procs = []
        app.s.proc_count = 0
        app.rebuild()
        self.assertEqual(app.visible, [])
        self.assertIsNone(app.current())
        app.draw(scr)
        app.mode, app.detail_proc = "detail", None
        app.draw(scr)


class TestKeyHandling(unittest.TestCase):
    KEYS = ["up", "down", "pgup", "pgdn", "home", "end", "enter", "escape", "tab"]
    CHARS = "cmtrTfuKzkXLs +-=_?h/"
    STORM = "cmtrTfuKzkX +-=_?h/abz19"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _app(self):
        return make_app(140, 40)

    def test_every_char_key_is_safe(self):
        app, scr = self._app()
        for ch in self.CHARS:
            app.mode = "normal"
            app.handle(_Key("char", ch))
            app.draw(scr)
        self.assertTrue(app.running)

    def test_every_named_key_is_safe(self):
        app, scr = self._app()
        for name in self.KEYS:
            app.mode = "normal"
            app.handle(_Key(name))
            app.draw(scr)

    def test_quit_keys(self):
        app, _ = self._app()
        app.handle(_Key("char", "q"))
        self.assertFalse(app.running)
        app, _ = self._app()
        app.handle(_Key("ctrl-c"))
        self.assertFalse(app.running)

    def test_ctrl_c_quits_from_every_mode(self):
        for mode in ("normal", "filter", "listeners", "help", "detail"):
            app, _ = self._app()
            app.mode = mode
            app.handle(_Key("ctrl-c"))
            self.assertFalse(app.running, mode)

    def test_filter_mode_editing(self):
        app, _ = self._app()
        app.handle(_Key("char", "f"))
        self.assertEqual(app.mode, "filter")
        for ch in "root":
            app.handle(_Key("char", ch))
        self.assertEqual(app.filter, "root")
        app.handle(_Key("backspace"))
        self.assertEqual(app.filter, "roo")
        app.handle(_Key("ctrl-u"))
        self.assertEqual(app.filter, "")
        app.handle(_Key("char", "x"))
        app.handle(_Key("escape"))
        self.assertEqual(app.filter, "")
        self.assertEqual(app.mode, "normal")

    def test_filter_enter_keeps_text(self):
        app, _ = self._app()
        app.mode = "filter"
        for ch in "abc":
            app.handle(_Key("char", ch))
        app.handle(_Key("enter"))
        self.assertEqual(app.mode, "normal")
        self.assertEqual(app.filter, "abc")

    def test_backspace_on_empty_filter(self):
        app, _ = self._app()
        app.mode = "filter"
        for _ in range(5):
            app.handle(_Key("backspace"))
        self.assertEqual(app.filter, "")

    def test_confirm_declines_by_default(self):
        app, _ = self._app()
        app.pending_kill = ("SIGTERM", app.visible[0])
        app.mode = "confirm"
        with mock.patch.object(os, "kill") as k:
            app.handle(_Key("char", "n"))
            self.assertFalse(k.called)
        self.assertEqual(app.mode, "normal")
        self.assertIsNone(app.pending_kill)

    def test_confirm_accepts_y(self):
        app, _ = self._app()
        target = app.visible[0]
        app.pending_kill = ("SIGTERM", target)
        app.mode = "confirm"
        with mock.patch.object(os, "kill") as k:
            app.handle(_Key("char", "y"))
            k.assert_called_once()
            self.assertEqual(k.call_args[0][0], target.pid)

    def test_kill_errors_are_reported_not_raised(self):
        app, _ = self._app()
        for exc in (PermissionError, ProcessLookupError, OSError("boom")):
            app.mode = "confirm"
            app.pending_kill = ("SIGKILL", app.visible[0])
            app.toast = ""
            with mock.patch.object(os, "kill", side_effect=exc):
                app.handle(_Key("char", "y"))
            self.assertTrue(app.toast, exc)

    def test_interval_bump_stays_in_range(self):
        app, _ = self._app()
        for _ in range(20):
            app.handle(_Key("char", "+"))
        self.assertEqual(app.interval_ms, min(INTERVALS))
        for _ in range(40):
            app.handle(_Key("char", "-"))
        self.assertEqual(app.interval_ms, max(INTERVALS))

    def test_snapshot_writes_a_file(self):
        app, _ = self._app()
        app.handle(_Key("char", "s"))
        files = [f for f in os.listdir(".") if f.startswith("pulse-")]
        self.assertEqual(len(files), 1)
        with open(files[0]) as f:
            self.assertIn("cpu", json.load(f))

    def test_snapshot_failure_is_reported(self):
        app, _ = self._app()
        with mock.patch("builtins.open", side_effect=OSError("nope")):
            app.handle(_Key("char", "s"))
        self.assertIn("failed", app.toast)

    def test_mouse_events(self):
        app, scr = self._app()
        app.draw(scr)
        for btn in (0, 64, 65):
            for mx in (0, 5, 40, 139):
                for my in (0, 1, 20, 39):
                    app.handle(_Key("mouse", mouse=(btn, mx, my, True)))
                    app.handle(_Key("mouse", mouse=(btn, mx, my, False)))
        app.draw(scr)
        self.assertTrue(0 <= app.selected <= max(0, len(app.visible) - 1))

    def test_column_header_click_sorts(self):
        app, scr = self._app()
        app.draw(scr)
        for x, w, skey in app.col_layout:
            if skey in panels.SORT_KEYS:
                app.handle(_Key("mouse", mouse=(0, x, app.body_rect.y - 1, True)))
                self.assertEqual(app.sort, skey)
                break

    def test_random_key_storm(self):
        rnd = random.Random(51)
        app, scr = self._app()
        names = self.KEYS + ["char", "mouse", "ctrl-c", "ctrl-u", "backspace"]
        for i in range(3000):
            name = rnd.choice(names)
            if name == "char":
                k = _Key("char", rnd.choice(self.STORM))
            elif name == "mouse":
                k = _Key("mouse", mouse=(rnd.choice([0, 1, 64, 65]),
                                         rnd.randint(0, 200), rnd.randint(0, 60),
                                         rnd.choice([True, False])))
            else:
                k = _Key(name)
            app.running = True
            app.handle(k)
            if i % 25 == 0:
                app.draw(scr)
                scr.flush(Null())
                self.assertTrue(all(len(r) == 140 for r in scr.cells))

    def test_decode_round_trip(self):
        keys, rest = decode("\x1b[A\x1b[Bq\r\x7f")
        self.assertEqual([k.name for k in keys],
                         ["up", "down", "char", "enter", "backspace"])
        self.assertEqual(rest, "")
        keys, _ = decode("\x1b[<0;10;20M")
        self.assertEqual(keys[0].name, "mouse")
        self.assertEqual(keys[0].mouse, (0, 9, 19, True))
        keys, rest = decode("\x1b[")            # split sequence
        self.assertEqual(keys, [])
        self.assertEqual(rest, "\x1b[")

    def test_decode_never_raises(self):
        rnd = random.Random(52)
        pool = "\x1b[<>;0123456789ABCDMmHq~O\x00\x7f\r\n\t"
        for _ in range(5000):
            s = "".join(rnd.choice(pool) for _ in range(rnd.randint(0, 24)))
            decode(s)


class TestFormatters(unittest.TestCase):
    def test_hbytes(self):
        self.assertEqual(D.hbytes(0), "0B")
        self.assertEqual(D.hbytes(512), "512B")
        self.assertEqual(D.hbytes(1024), "1.0K")
        self.assertEqual(D.hbytes(1536), "1.5K")
        self.assertEqual(D.hbytes(1 << 20), "1.0M")
        self.assertEqual(D.hbytes(1 << 30), "1.0G")
        self.assertTrue(D.hbytes(1 << 50).endswith("T"))
        self.assertEqual(len(D.hbytes(1234, 8)), 8)

    def test_durations(self):
        self.assertEqual(D.hduration(0), "0m 00s")
        self.assertEqual(D.hduration(59), "0m 59s")
        self.assertEqual(D.hduration(3600), "1h 00m")
        self.assertEqual(D.hduration(86400), "1d 00h")
        self.assertEqual(D.hduration(-5), "0m 00s")
        self.assertEqual(D.hcputime(0), "00:00")
        self.assertEqual(D.hcputime(3661), "1:01:01")

    def test_trunc(self):
        self.assertEqual(D.trunc("abc", 0), "")
        self.assertEqual(D.trunc("abc", 3), "abc")
        self.assertEqual(D.trunc("abcd", 3), "ab\u2026")
        self.assertEqual(len(D.trunc("x" * 50, 10)), 10)
        self.assertEqual(D.trunc("abc", -1), "")

    def test_clamp(self):
        self.assertEqual(D.clamp(5, 0, 10), 5)
        self.assertEqual(D.clamp(-5, 0, 10), 0)
        self.assertEqual(D.clamp(50, 0, 10), 10)

    def test_hspan(self):
        self.assertTrue(D.hspan(30).endswith("s"))
        self.assertTrue(D.hspan(600).endswith("m"))
        self.assertTrue(D.hspan(7200).endswith("h"))

    def test_sgr_cached_and_valid(self):
        a, b = sgr(0xFF0000, -1, A_BOLD), sgr(0xFF0000, -1, A_BOLD)
        self.assertIs(a, b)
        self.assertTrue(a.startswith("\x1b[") and a.endswith("m"))
        self.assertIn("38;2;255;0;0", a)
        self.assertIn("48;2;0;0;255", sgr(-1, 0x0000FF, 0))

    def test_widgets_stay_in_bounds(self):
        scr = Screen(20, 5)
        scr.clear()
        for frac in (-1.0, 0.0, 0.01, 0.5, 0.999, 1.0, 5.0):
            D.meter(scr, 0, 0, 20, frac)
        D.meter(scr, 0, 0, 0, 0.5)
        D.scrollbar(scr, 19, 0, 5, 100, 50)
        D.scrollbar(scr, 19, 0, 5, 2, 0)
        D.heatstrip(scr, 0, 4, [0.0, 50.0, 100.0], 3)
        D.gridlines(scr, Rect(0, 0, 20, 5))
        self.assertTrue(all(len(r) == 20 for r in scr.cells))


class TestStability(unittest.TestCase):
    def test_repeated_sampling_is_stable(self):
        s = fresh_sampler()
        for _ in range(40):
            s.sample()
        self.assertGreater(s.proc_count, 0)
        self.assertLessEqual(len(s.proc_prev), s.proc_count + 50)
        self.assertLessEqual(len(s.proc_ids), s.proc_count + 50)
        self.assertLessEqual(len(s._cmd_cache), max(1, len(s.proc_prev)))

    def test_history_is_bounded(self):
        s = C.Sampler(history=16)
        for _ in range(120):
            s.sample()
        for k, d in s.hist.items():
            self.assertLessEqual(len(d), 16, k)
        for d in s.core_hist:
            self.assertLessEqual(len(d), 256)

    def test_long_render_loop_is_stable(self):
        app, scr = make_app(130, 40)
        out = Null()
        for i in range(60):
            if i % 10 == 0:
                app.s.sample()
                app.rebuild()
            app.draw(scr)
            scr.flush(out)
            out.chunks.clear()
        self.assertTrue(all(len(r) == 130 for r in scr.cells))

    def test_sample_time_recorded(self):
        s = fresh_sampler()
        self.assertGreater(s.sample_ms, 0.0)
        self.assertLess(s.sample_ms, 5000.0)

    def test_rates_non_negative(self):
        s = fresh_sampler()
        for _ in range(5):
            s.sample()
            for v in (s.ctxt_rate, s.fork_rate, s.rx, s.tx, s.dr, s.dw):
                self.assertGreaterEqual(v, 0.0)

    def test_first_sample_has_no_spikes(self):
        s = C.Sampler()
        s.sample()
        self.assertEqual(s.cpu, 0.0)
        self.assertTrue(all(p.cpu == 0.0 for p in s.procs))
        self.assertEqual(s.rx, 0.0)
        self.assertEqual(s.ctxt_rate, 0.0)


class TestHeadless(unittest.TestCase):
    def test_snapshot_shape(self):
        s = fresh_sampler()
        snap = snapshot(s)
        for key in ("ts", "host", "kernel", "uptime_s", "cpu", "load", "pressure",
                    "memory", "network", "disks", "filesystems", "sockets",
                    "processes"):
            self.assertIn(key, snap)
        self.assertLessEqual(len(snap["processes"]["top"]), 40)
        cpus = [p["cpu_pct"] for p in snap["processes"]["top"]]
        self.assertEqual(cpus, sorted(cpus, reverse=True))

    def test_snapshot_is_json_serialisable(self):
        s = fresh_sampler()
        back = json.loads(json.dumps(snapshot(s)))
        self.assertEqual(back["cpu"]["cores"], s.ncores)
        self.assertEqual(back["processes"]["count"], s.proc_count)

    def test_run_json(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            self.assertEqual(run_json(), 0)
        json.loads(buf.getvalue())

    def test_run_once(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            self.assertEqual(run_once(500), 0)
        text = buf.getvalue()
        self.assertIn("PROCESSES", text)
        self.assertTrue(text.endswith("\n"))


class TestCLI(unittest.TestCase):
    def _run(self, *args, timeout=90):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "pulse.py")] + list(args),
            capture_output=True, text=True, timeout=timeout)

    def test_version(self):
        r = self._run("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("pulse", r.stdout)

    def test_help(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--interval", r.stdout)

    def test_json_output(self):
        r = self._run("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["cpu"]["cores"], os.cpu_count())
        self.assertGreater(d["processes"]["count"], 0)
        self.assertEqual(r.stderr, "")

    def test_once_output(self):
        r = self._run("--once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PROCESSES", r.stdout)
        self.assertEqual(r.stderr, "")

    def test_piped_stdout_renders_once(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CPU", r.stdout)

    def test_interval_is_clamped(self):
        for val in ("1", "999999", "-5"):
            r = self._run("-i", val, "--once")
            self.assertEqual(r.returncode, 0, r.stderr)


class TestInteractiveTUI(unittest.TestCase):
    """Drive the real binary through a pty and exercise every mode."""

    def _drive(self, keys, cols=150, rows=45, resizes=None, extra=()):
        import fcntl
        import pty
        import select
        import struct
        import termios

        resizes = resizes or {}
        pid, fd = pty.fork()
        if pid == 0:                                    # child
            os.environ["TERM"] = "xterm-256color"
            os.chdir(ROOT)
            os.execv(sys.executable,
                     [sys.executable, "pulse.py", "-i", "250"] + list(extra))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        chunks = []

        def drain(t):
            end = time.monotonic() + t
            while time.monotonic() < end:
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    try:
                        c = os.read(fd, 1 << 20)
                    except OSError:
                        return False
                    if not c:
                        return False
                    chunks.append(c)
            return True

        drain(1.2)
        for i, k in enumerate(keys):
            if i in resizes:
                r, c = resizes[i]
                fcntl.ioctl(fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", r, c, 0, 0))
            try:
                os.write(fd, k.encode())
            except OSError:
                break
            if not drain(0.22):
                break
        drain(0.8)
        with contextlib.suppress(OSError):
            os.write(fd, b"q")
        deadline = time.monotonic() + 6
        status = None
        while time.monotonic() < deadline:
            wpid, st = os.waitpid(pid, os.WNOHANG)
            if wpid:
                status = st
                break
            drain(0.1)
        if status is None:
            os.kill(pid, 9)
            _, status = os.waitpid(pid, 0)
        with contextlib.suppress(OSError):
            os.close(fd)
        return exitcode(status), b"".join(chunks)

    def test_full_session(self):
        keys = ["?", "\x1b", "m", "t", "c", "r", "T", "T", "u", "u", "K", "K",
                "z", "z", "f", "py", "\r", "\x1b", "L", "\x1b",
                "\x1b[B", "\x1b[B", "\x1b[A", "\r", "\x1b",
                "k", "n", "X", "n", " ", " ", "+", "+", "-", "-",
                "\x1b[6~", "\x1b[5~", "\x1b[F", "\x1b[H",
                "\x1b[<64;10;20M", "\x1b[<65;10;20M", "\x1b[<0;30;25M",
                "\x1b[<0;12;3M", "\x1b", "q"]
        code, data = self._drive(keys)
        self.assertEqual(code, 0)
        self.assertGreater(len(data), 10000)
        low = data.lower()
        for bad in (b"traceback", b"exception", b"unhandled"):
            self.assertNotIn(bad, low)
        for panel in (b"CPU", b"MEMORY", b"PROCESSES", b"NETWORK"):
            self.assertIn(panel, data)

    def test_survives_resizing(self):
        keys = ["m"] * 14
        resizes = {2: (20, 58), 4: (16, 60), 6: (60, 220), 8: (45, 150),
                   10: (10, 30), 12: (40, 120)}
        code, data = self._drive(keys, resizes=resizes)
        self.assertEqual(code, 0)
        self.assertNotIn(b"Traceback", data)

    def test_restores_terminal_on_exit(self):
        code, data = self._drive(["m"])
        self.assertEqual(code, 0)
        self.assertIn(b"\x1b[?1049l", data)      # left the alternate screen
        self.assertIn(b"\x1b[?25h", data)        # cursor visible again
        self.assertIn(b"\x1b[?1006l", data)      # mouse reporting off

    def test_no_mouse_flag(self):
        code, data = self._drive(["m"], extra=["--no-mouse"])
        self.assertEqual(code, 0)
        self.assertNotIn(b"\x1b[?1000h", data)

    def test_paused_still_quits(self):
        code, data = self._drive([" ", "m", "m"])
        self.assertEqual(code, 0)
        self.assertIn(b"PAUSED", data)


class TestPanels(unittest.TestCase):
    def test_columns_shrink_with_width(self):
        widths = [len(panels._columns(w)) for w in (40, 60, 70, 80, 100, 140)]
        self.assertEqual(widths, sorted(widths))
        for w in range(10, 220, 7):
            cols = panels._columns(w)
            self.assertTrue(cols)
            self.assertEqual(cols[-1][0], "COMMAND")
            self.assertGreaterEqual(cols[-1][1], 10)

    def test_cell_formats_every_column(self):
        s = fresh_sampler()
        p = s.procs[0]
        p.cmd = "cmd"
        for key in ("PID", "USER", "CPU%", "MEM%", "RSS", "THR", "ST", "TIME",
                    "COMMAND"):
            txt, col = panels._cell(p, key, "")
            self.assertIsInstance(txt, str)
            self.assertIsInstance(col, int)

    def test_colour_helpers(self):
        for v in (-1.0, 0.0, 0.25, 0.5, 0.99, 1.0, 2.0):
            self.assertIsInstance(P.heat(v), int)
            self.assertIsInstance(P.status(v), int)
            self.assertIsInstance(P.value(v), int)


if __name__ == "__main__":
    unittest.main(verbosity=2)

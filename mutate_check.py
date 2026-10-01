#!/usr/bin/env python3
"""Mutation check: break each optimisation on purpose and confirm the suite
notices. A green suite that cannot fail is worthless.

    python3 mutate_check.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "pulseui")

# (label, file, old snippet, new snippet, tests that must fail)
MUTATIONS = [
    ("procs: wrong split bound", "collect.py",
     'p = raw[close + 2:].split(None, 22)',
     'p = raw[close + 2:].split(None, 15)',
     "TestStatParsing"),

    ("procs: uid cache ignores start tick", "collect.py",
     'if ident is not None and ident[0] == start:',
     'if ident is not None:',
     "TestIdentityCache"),

    ("procs: reuse stale cpu ticks across pid reuse", "collect.py",
     'pr.rss = int(p[21]) * PAGE',
     'pr.rss = int(p[21]) * PAGE * 2',
     "TestStatParsing"),

    ("sanitize: skip control stripping", "collect.py",
     'return s if s.isprintable() else s.translate(_CTRL)',
     'return s',
     "TestSanitize"),

    ("sanitize: let bidi overrides through", "collect.py",
     '        if not ch.isprintable():\n            out.append(" ")',
     '        if ord(ch) < 32 or ord(ch) == 127:\n            out.append(" ")',
     "TestSanitize TestGridInvariants"),

    ("read_cpu: drop misc counters", "collect.py",
     'key = _MISC_KEYS.get(raw[:sp])',
     'key = None',
     "TestReadCpu"),

    ("read_cpu: split the giant intr line", "collect.py",
     'misc[key] = int(raw[sp + 1:].split(None, 1)[0])',
     'misc[key] = int(raw.split()[2])',
     "TestReadCpu"),

    ("read_freq: never cache the cpuinfo fallback", "collect.py",
     '    if at and now - at < 5.0:\n        return val',
     '    if False:\n        return val',
     "TestReadFreq"),

    ("read_freq: re-probe cpufreq paths every call", "collect.py",
     '    if _freq_paths is None:',
     '    if True:',
     "TestReadFreq"),

    ("iface_speed: drop the cache", "collect.py",
     '    v = _IFACE_SPEED.get(name)\n    if v is None:',
     '    v = None\n    if v is None:',
     "TestCachedSources"),

    ("diskstats: re-probe /sys/block every tick", "collect.py",
     '                whole = _WHOLE_DEV.get(name)\n                if whole is None:',
     '                whole = None\n                if whole is None:',
     "TestCachedSources"),

    ("fs usage: statvfs on every tick", "collect.py",
     'if now - self._fs_at > 5.0 or not self._fs_at:',
     'if True:',
     "TestCachedSources"),

    ("battery: read on every tick", "collect.py",
     'if now - self._batt_at > 15.0 or not self._batt_at:',
     'if True:',
     "TestCachedSources"),

    ("temps: rescan /sys whenever empty", "collect.py",
     'if not self._temp_at or now - self._temp_at > 2.0:',
     'if now - self._temp_at > 2.0 or not self._temps:',
     "TestCachedSources"),

    ("cmdline cache: ignore start tick", "collect.py",
     'if hit is not None and hit[0] == p.start:',
     'if hit is not None:',
     "TestCmdlineCache"),

    ("cmdline cache: never prune", "collect.py",
     'if len(self._cmd_cache) > len(cur):',
     'if False:',
     "TestCmdlineCache"),

    ("put: off-by-one on the right edge", "term.py",
     '            text = text[:w - lo]',
     '            text = text[:w - lo + 1]',
     "TestScreenPrimitives"),

    ("put: wrong return value", "term.py",
     '        return end if end < w else w',
     '        return end',
     "TestScreenPrimitives"),

    ("put: drop the control-char guard", "term.py",
     '        if not text.isprintable():\n            text = text.translate(_SAFE)',
     '        pass',
     "TestScreenPrimitives TestGridInvariants"),

    ("clear: reuse the outer list (aliases prev)", "term.py",
     '        self.cells = [[blank] * w for _ in range(self.h)]',
     '        for y in range(self.h):\n            self.cells[y] = [blank] * w',
     "TestScreenPrimitives TestFlushProtocol"),

    ("flush: forget the diff baseline", "term.py",
     '        self.prev = self.cells',
     '        self.prev = None',
     "TestFlushProtocol"),

    ("flush: skip the trailing reset", "term.py",
     '            parts.append("\\x1b[0m")',
     '            pass',
     "TestFlushProtocol"),

    ("fill: off-by-one", "term.py",
     '        x1 = min(self.w, r.x + r.w)',
     '        x1 = min(self.w, r.x + r.w - 1)',
     "TestScreenPrimitives"),

    ("plot: wrong partial-cell mask", "draw.py",
     '                grid[top - q][cx] |= part[half][rem]',
     '                grid[top - q][cx] |= part[half][rem - 1]',
     "TestPlot"),

    ("plot: skip the full-cell fill", "draw.py",
     '            for gy in range(top, top - q, -1):\n                grid[gy][cx] |= full',
     '            for gy in range(top, top - q, -1):\n                pass',
     "TestPlot"),

    ("plot: drop the tiny-value floor", "draw.py",
     'n = sub if f >= 1.0 else (int(f * sub + 0.5) or 1)',
     'n = sub if f >= 1.0 else int(f * sub + 0.5)',
     "TestPlot"),

    ("plot: ignore the flip flag", "draw.py",
     '    part = _PART_DOWN if flip else _PART_UP',
     '    part = _PART_UP',
     "TestPlot"),
]


def run(label, fname, old, new, tests):
    path = os.path.join(SRC, fname)
    with open(path) as f:
        src = f.read()
    if src.count(old) != 1:
        return "SKIP", "anchor found %d times" % src.count(old)
    backup = src
    try:
        shutil.rmtree(os.path.join(SRC, "__pycache__"), ignore_errors=True)
        with open(path, "w") as f:
            f.write(src.replace(old, new))
        cmd = [sys.executable, "-B", "-m", "unittest"] + \
              ["test_pulse." + t for t in tests.split()]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=600, env=env)
        if r.returncode != 0:
            tail = [ln for ln in r.stderr.splitlines()
                    if ln.startswith(("FAIL:", "ERROR:"))]
            return "CAUGHT", tail[0][:72] if tail else "non-zero exit"
        return "MISSED", "suite still passed"
    finally:
        with open(path, "w") as f:
            f.write(backup)
        shutil.rmtree(os.path.join(SRC, "__pycache__"), ignore_errors=True)


def main():
    shutil.rmtree(os.path.join(SRC, "__pycache__"), ignore_errors=True)
    caught = missed = skipped = 0
    for label, fname, old, new, tests in MUTATIONS:
        status, detail = run(label, fname, old, new, tests)
        print("%-8s %-46s %s" % (status, label, detail))
        caught += status == "CAUGHT"
        missed += status == "MISSED"
        skipped += status == "SKIP"
    print("\n%d caught, %d MISSED, %d skipped, %d total"
          % (caught, missed, skipped, len(MUTATIONS)))
    return 1 if (missed or skipped) else 0


if __name__ == "__main__":
    sys.exit(main())

"""Application state, responsive layout, input handling and the render loop."""

from __future__ import annotations

import json
import os
import signal
import sys
import time

from . import collect as C
from . import palette as P
from . import panels
from .draw import clamp
from .term import Rect, Screen, Terminal

INTERVALS = [250, 500, 1000, 2000, 3000, 5000, 10000]
DEFAULT_SORT = "cpu"
SORT_BY_KEY = {"c": "cpu", "m": "mem", "t": "thr"}
MIN_W, MIN_H = 60, 16
BLINK_MS = 90          # heartbeat flash: long enough to see, short enough to read as a blink


class App:
    def __init__(self, interval_ms: int = 2000, mouse: bool = True):
        self.s = C.Sampler()
        self.interval_ms = interval_ms
        self.mouse = mouse
        self.running = True
        self.paused = False
        self.dirty = True

        self.sort = DEFAULT_SORT
        self.sort_desc = True
        self.filter = ""
        self.mode = "normal"
        self.tree = False
        self.show_kernel = False
        self.only_mine = False
        self.zoom = False

        self.selected = 0
        self.scroll = 0
        self.page = 10
        self.selected_pid: int | None = None
        self.visible: list[C.Proc] = []
        self.col_layout: list = []
        self.body_rect = Rect(0, 0, 0, 0)

        self.detail_proc = None
        self.pending_kill = None
        self.listeners: list = []
        self.lowners: dict = {}
        self.lscroll = 0
        self.lpage = 10
        self.toast = ""
        self.toast_until = 0.0
        self.uid = os.getuid()
        self.next_tick = 0.0
        self.blink_until = 0.0

    # ------------------------------------------------------------ process list
    def rebuild(self) -> None:
        pinned = self.selected_pid
        offset = int(clamp(self.selected - self.scroll, 0, max(0, self.page - 1)))
        procs = self.s.procs
        if not self.show_kernel:
            procs = [p for p in procs if not (p.ppid == 2 or p.pid == 2)]
        if self.only_mine:
            procs = [p for p in procs if p.uid == self.uid]
        if self.filter:
            terms = self.filter.lower().split()
            procs = [p for p in procs if self._matches(p, terms)]
        keyfn = panels.SORT_KEYS[self.sort][2]
        rev = self.sort_desc
        if self.sort == "name":
            rev = not rev
        if self.tree:
            self.visible = self._tree_order(procs, keyfn, rev)
        else:
            for p in procs:
                p.depth = 0
            self.visible = sorted(procs, key=keyfn, reverse=rev)

        if pinned is not None:
            idx = next((i for i, p in enumerate(self.visible) if p.pid == pinned), -1)
            if idx >= 0:
                # Keep the pinned row on the same screen line as the list reorders.
                self.selected = idx
                self.scroll = idx - offset
            else:
                self.clamp_scroll(len(self.visible))
                self.pin()
        self.clamp_scroll(len(self.visible))
        self._load_cmdlines()

    def _matches(self, p, terms) -> bool:
        """Every term must hit the name, full argv, user or exact pid.

        Reading argv for all processes costs ~4ms, so it happens only while a
        filter is active.
        """
        if p.cmd is None:
            p.cmd = self.s.cmdline(p)
        hay = ("%s %s %s" % (p.name, p.user, p.cmd)).lower()
        pid = str(p.pid)
        return all(t in hay or t == pid for t in terms)

    def pin(self) -> None:
        """Lock the selection onto whatever process is under the cursor now."""
        p = self.current()
        self.selected_pid = p.pid if p is not None else None

    @property
    def is_default(self) -> bool:
        return (self.sort == DEFAULT_SORT and self.sort_desc and not self.filter
                and self.selected_pid is None and not self.tree
                and not self.only_mine and not self.show_kernel and not self.zoom)

    def reset(self) -> None:
        """Back to the startup view. Refresh rate and pause are settings, not view
        state, so they survive."""
        self.sort, self.sort_desc = DEFAULT_SORT, True
        self.filter = ""
        self.selected_pid = None
        self.tree = False
        self.only_mine = False
        self.show_kernel = False
        self.zoom = False
        self.selected = self.scroll = 0
        self.rebuild()

    @staticmethod
    def _tree_order(procs, keyfn, rev):
        byid = {p.pid: p for p in procs}
        kids: dict[int, list] = {}
        roots = []
        for p in procs:
            if p.ppid != p.pid and p.ppid in byid:
                kids.setdefault(p.ppid, []).append(p)
            else:
                roots.append(p)
        out = []
        stack = [(p, 0) for p in sorted(roots, key=keyfn, reverse=not rev)]
        while stack:
            p, d = stack.pop()
            p.depth = d
            out.append(p)
            ch = kids.get(p.pid)
            if ch:
                for c in sorted(ch, key=keyfn, reverse=not rev):
                    stack.append((c, d + 1))
        return out

    def _load_cmdlines(self) -> None:
        """Read /proc/<pid>/cmdline only for rows that will actually be drawn."""
        lo = max(0, self.scroll - 4)
        cmdline = self.s.cmdline
        for p in self.visible[lo: lo + self.page + 8]:
            if p.cmd is None:
                p.cmd = cmdline(p)

    def clamp_scroll(self, n: int) -> None:
        self.selected = int(clamp(self.selected, 0, max(0, n - 1)))
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + self.page:
            self.scroll = self.selected - self.page + 1
        self.scroll = int(clamp(self.scroll, 0, max(0, n - self.page)))

    def current(self):
        if 0 <= self.selected < len(self.visible):
            return self.visible[self.selected]
        return None

    def notify(self, msg: str, secs: float = 2.5) -> None:
        self.toast = msg
        self.toast_until = time.monotonic() + secs

    # ------------------------------------------------------------ layout
    def layout(self, w: int, h: int) -> dict[str, Rect]:
        body = Rect(0, 1, w, h - 2)
        if self.zoom:
            return {"proc": body}
        s = self.s
        out: dict[str, Rect] = {}
        cpu_h = int(clamp(body.h // 3, 7, 13))
        rest = body.h - cpu_h

        # Size the middle row to what it actually has to say, so the process
        # list keeps everything left over.
        mem_need = 5 + (1 + len(s.disks) if s.disks else 0) \
                     + (1 + len(s.fs) if s.fs else 0)
        net_need = 8
        wide = w >= 100
        # Narrow means the two panels stack, so ask for both their heights at
        # once; asking for mem_need alone could never satisfy the stacked case.
        want = max(mem_need, net_need) if wide else mem_need + net_need

        mid_h = 0
        if rest - 8 >= 8:
            mid_h = int(clamp(want, 8, rest - 8))
        proc_h = rest - mid_h

        out["cpu"] = Rect(0, body.y, w, cpu_h)
        y = body.y + cpu_h
        if mid_h:
            if wide:
                mw = int(w * 0.56)
                out["mem"] = Rect(0, y, mw, mid_h)
                out["net"] = Rect(mw, y, w - mw, mid_h)
            elif mid_h >= mem_need + net_need:
                out["mem"] = Rect(0, y, w, mem_need)
                out["net"] = Rect(0, y + mem_need, w, mid_h - mem_need)
            else:
                out["mem"] = Rect(0, y, w, mid_h)
            y += mid_h
        out["proc"] = Rect(0, y, w, proc_h)
        return out

    def draw(self, scr: Screen) -> None:
        scr.clear()
        if scr.w < MIN_W or scr.h < MIN_H:
            panels.too_small(scr)
            return
        s = self.s
        panels.header(scr, Rect(1, 0, scr.w - 2, 1), s, self)
        lay = self.layout(scr.w, scr.h)
        if "cpu" in lay:
            panels.cpu(scr, lay["cpu"], s, self)
        if "mem" in lay:
            panels.memory(scr, lay["mem"], s, self)
        if "net" in lay:
            panels.network(scr, lay["net"], s, self)
        panels.processes(scr, lay["proc"], s, self)
        panels.footer(scr, Rect(1, scr.h - 1, scr.w - 2, 1), s, self)
        if self.mode == "help":
            panels.help_overlay(scr, self)
        elif self.mode == "listeners":
            panels.listeners_overlay(scr, self)
        elif self.mode == "detail":
            panels.detail_overlay(scr, self)
        elif self.mode == "confirm":
            panels.confirm_overlay(scr, self)

    # ------------------------------------------------------------ input
    def handle(self, key) -> None:
        self.dirty = True
        name, ch = key.name, key.ch

        if name == "mouse":
            self._mouse(key.mouse)
            return

        if self.mode == "filter":
            if name == "enter":
                self.mode = "normal"
            elif name == "escape":
                self.filter = ""
                self.mode = "normal"
            elif name == "backspace":
                self.filter = self.filter[:-1]
            elif name == "ctrl-u":
                self.filter = ""
            elif name == "char":
                self.filter += ch
            elif name == "ctrl-c":
                self.running = False
            self.selected = self.scroll = 0
            self.selected_pid = None
            self.rebuild()
            return

        if self.mode == "confirm":
            if name == "char" and ch in "yY":
                self._do_kill()
            self.pending_kill = None
            self.mode = "normal"
            return

        if self.mode == "listeners":
            if name == "ctrl-c":
                self.running = False
            elif name == "up":
                self.lscroll -= 1
            elif name == "down":
                self.lscroll += 1
            elif name == "pgup":
                self.lscroll -= self.lpage
            elif name == "pgdn":
                self.lscroll += self.lpage
            elif name == "home":
                self.lscroll = 0
            elif name == "end":
                self.lscroll = len(self.listeners)
            elif name == "mouse":
                btn = key.mouse[0]
                if btn == 64:
                    self.lscroll -= 3
                elif btn == 65:
                    self.lscroll += 3
                else:
                    self.mode = "normal"
            else:
                self.mode = "normal"
            self.lscroll = int(clamp(self.lscroll, 0,
                                     max(0, len(self.listeners) - self.lpage)))
            return

        if self.mode in ("help", "detail"):
            if name == "ctrl-c":
                self.running = False
            self.mode = "normal"
            return

        if name == "escape":
            if not self.is_default:
                self.reset()
                self.notify("view reset")
            return
        if name == "ctrl-c" or (name == "char" and ch == "q"):
            self.running = False
            return

        n = len(self.visible)
        if name == "up":
            self.selected -= 1
        elif name == "down":
            self.selected += 1
        elif name == "pgup":
            self.selected -= self.page
        elif name == "pgdn":
            self.selected += self.page
        elif name == "home":
            self.selected = 0
        elif name == "end":
            self.selected = n - 1
        elif name == "enter":
            p = self.current()
            if p is not None:
                self.detail_proc = p
                self.mode = "detail"
        elif name == "char":
            self._char(ch)
        self.clamp_scroll(n)
        if name in ("up", "down", "pgup", "pgdn", "home", "end"):
            self.pin()
        self._load_cmdlines()

    def _char(self, ch: str) -> None:
        if ch in SORT_BY_KEY:
            new = SORT_BY_KEY[ch]
            if new == self.sort:
                self.sort_desc = not self.sort_desc
            else:
                self.sort, self.sort_desc = new, True
            self.rebuild()
        elif ch == "r":
            self.sort_desc = not self.sort_desc
            self.rebuild()
        elif ch == "T":
            self.tree = not self.tree
            self.rebuild()
            self.notify("tree view " + ("on" if self.tree else "off"))
        elif ch in ("f", "/"):
            self.mode = "filter"
        elif ch == "u":
            self.only_mine = not self.only_mine
            self.rebuild()
            self.notify("showing " + ("my processes" if self.only_mine else "all users"))
        elif ch == "K":
            self.show_kernel = not self.show_kernel
            self.rebuild()
            self.notify("kernel threads " + ("shown" if self.show_kernel else "hidden"))
        elif ch == "z":
            self.zoom = not self.zoom
        elif ch == "k":
            p = self.current()
            if p is not None:
                self.pending_kill = ("SIGTERM", p)
                self.mode = "confirm"
        elif ch == "X":
            p = self.current()
            if p is not None:
                self.pending_kill = ("SIGKILL", p)
                self.mode = "confirm"
        elif ch == "L":
            self.listeners = C.read_listeners()
            self.lowners = C.socket_owners(r[4] for r in self.listeners)
            self.lscroll = 0
            self.mode = "listeners"
        elif ch == "s":
            self._snapshot()
        elif ch == " ":
            self.paused = not self.paused
        elif ch in ("?", "h"):
            self.mode = "help"
        elif ch in ("+", "="):
            self._bump(-1)
        elif ch in ("-", "_"):
            self._bump(1)

    def _bump(self, delta: int) -> None:
        cur = min(range(len(INTERVALS)),
                  key=lambda i: abs(INTERVALS[i] - self.interval_ms))
        self.interval_ms = INTERVALS[int(clamp(cur + delta, 0, len(INTERVALS) - 1))]
        self.next_tick = 0.0
        self.notify("refreshing every %gs" % (self.interval_ms / 1000.0))

    def _mouse(self, m) -> None:
        btn, mx, my, pressed = m
        if btn in (64, 65):
            # Move selection with the viewport; otherwise clamp_scroll snaps it back.
            step = -3 if btn == 64 else 3
            self.scroll += step
            self.selected += step
            self.clamp_scroll(len(self.visible))
            self.pin()
            self._load_cmdlines()
            return
        if not pressed or btn != 0:
            return
        for x, w, skey in self.col_layout:
            if skey in panels.SORT_KEYS and x <= mx < x + w and my == self.body_rect.y - 1:
                if skey == self.sort:
                    self.sort_desc = not self.sort_desc
                else:
                    self.sort, self.sort_desc = skey, True
                self.rebuild()
                return
        if self.body_rect.contains(mx, my):
            self.selected = self.scroll + (my - self.body_rect.y)
            self.clamp_scroll(len(self.visible))
            self.pin()

    def _do_kill(self) -> None:
        sig, p = self.pending_kill
        num = signal.SIGTERM if sig == "SIGTERM" else signal.SIGKILL
        try:
            os.kill(p.pid, num)
            self.notify("sent %s to %d (%s)" % (sig, p.pid, p.name))
        except PermissionError:
            self.notify("permission denied for pid %d" % p.pid)
        except ProcessLookupError:
            self.notify("pid %d already gone" % p.pid)
        except OSError as e:
            self.notify("kill failed: %s" % e)

    def _snapshot(self) -> None:
        path = os.path.abspath("pulse-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        try:
            with open(path, "w") as f:
                json.dump(snapshot(self.s), f, indent=2)
            self.notify("saved " + os.path.basename(path))
        except OSError as e:
            self.notify("snapshot failed: %s" % e)

    # ------------------------------------------------------------ loop
    def run(self) -> int:
        with Terminal(mouse=self.mouse) as term:
            scr = Screen(*term.size())
            self.s.sample()
            time.sleep(0.12)
            self.s.sample()
            self.rebuild()
            self.next_tick = time.monotonic() + self.interval_ms / 1000.0
            last_sec = -1
            while self.running:
                if term.resized:
                    term.resized = False
                    scr.resize(*term.size())
                    self.dirty = True
                now = time.monotonic()
                if not self.paused and now >= self.next_tick:
                    self.s.sample()
                    self.rebuild()
                    self.next_tick = now + self.interval_ms / 1000.0
                    self.blink_until = now + BLINK_MS / 1000.0
                    self.dirty = True
                elif self.blink_until and now >= self.blink_until:
                    # Switching the heartbeat back off is its own repaint;
                    # without it the dot stays lit until the next second ticks.
                    self.blink_until = 0.0
                    self.dirty = True
                sec = int(time.time())
                if sec != last_sec:
                    last_sec = sec
                    self.dirty = True
                if self.dirty:
                    self.draw(scr)
                    scr.flush(term.out)
                    self.dirty = False
                now = time.monotonic()
                wait = 0.25 if self.paused else max(0.0, self.next_tick - now)
                if self.blink_until > now:
                    wait = min(wait, self.blink_until - now)
                for k in term.wait(max(0.005, min(wait, 0.25))):
                    self.handle(k)
        return 0


# ---------------------------------------------------------------- headless

def snapshot(s: C.Sampler) -> dict:
    top = sorted(s.procs, key=lambda p: p.cpu, reverse=True)[:40]
    return {
        "ts": time.time(),
        "host": s.host,
        "kernel": s.kernel,
        "uptime_s": round(s.uptime, 1),
        "cpu": {"model": s.model, "cores": s.ncores, "usage_pct": round(s.cpu, 2),
                "per_core": [round(v, 2) for v in s.cores],
                "freq_mhz": round(s.freq, 1), "temp_c": round(s.temp, 1),
                "mix": dict(zip(("user", "system", "iowait", "steal"),
                                (round(v, 2) for v in s.cpu_mix)))},
        "load": {"1m": s.load[0], "5m": s.load[1], "15m": s.load[2],
                 "running": s.load[3]},
        "pressure": {k: {"some_avg10": v[0], "full_avg10": v[1]}
                     for k, v in s.psi.items()},
        "memory": s.mem,
        "network": {"primary": s.primary, "rx_bps": round(s.rx), "tx_bps": round(s.tx),
                    "rx_total": s.rx_total, "tx_total": s.tx_total,
                    "interfaces": s.nets},
        "disks": s.disks,
        "filesystems": s.fs,
        "sockets": s.sockets,
        "processes": {
            "count": s.proc_count, "threads": s.thread_count,
            "top": [{"pid": p.pid, "name": p.name, "user": p.user,
                     "cpu_pct": round(p.cpu, 2), "mem_pct": round(p.mem, 2),
                     "rss": p.rss, "state": p.state, "threads": p.threads}
                    for p in top],
        },
    }


def run_once(interval_ms: int) -> int:
    s = C.Sampler()
    s.sample()
    time.sleep(min(0.4, max(0.15, interval_ms / 1000.0)))
    s.sample()
    app = App(interval_ms)
    app.s = s
    app.rebuild()
    try:
        cols, rows = os.get_terminal_size()
    except OSError:
        cols, rows = 110, 38
    scr = Screen(cols, max(rows, MIN_H))
    app.page = scr.h
    app._load_cmdlines()
    app.draw(scr)
    out = []
    from .term import sgr
    for y in range(scr.h):
        cfg = cbg = -2
        cattr = -1
        for ch, fg, bg, attr in scr.cells[y]:
            if fg != cfg or bg != cbg or attr != cattr:
                out.append(sgr(fg, bg, attr))
                cfg, cbg, cattr = fg, bg, attr
            out.append(ch)
        out.append("\x1b[0m\n")
    sys.stdout.write("".join(out))
    return 0


def run_json() -> int:
    s = C.Sampler()
    s.sample()
    time.sleep(0.25)
    s.sample()
    json.dump(snapshot(s), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0

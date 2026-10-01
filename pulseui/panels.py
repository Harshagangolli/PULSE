"""Panel renderers. Every function draws into a Rect and clips to it."""

from __future__ import annotations

import os
import time

from . import collect as C
from . import palette as P
from .draw import (BAR, SOFT, clamp, frame, gridlines, hbytes, hcputime,
                   hduration, heatstrip, hrate, hrule, hspan, meter, plot,
                   scrollbar, stacked, trunc)
from .term import A_BOLD, Rect, Screen

SEP = "\u00b7"
DOWN_ARROW = "\u25bc"
UP_ARROW = "\u25b2"


def _pct(v: float) -> str:
    return "%.0f%%" % v if v >= 99.5 else "%.1f%%" % v


def _field(scr: Screen, x: int, y: int, name: str, val: str,
           col: int = P.FG, limit: int = 10 ** 6) -> int:
    """`label value` pair, skipped entirely if it would not fit."""
    if x + len(name) + len(val) + 1 > limit:
        return x
    scr.put(x, y, name, P.FAINT)
    scr.put(x + len(name) + 1, y, val, col)
    return x + len(name) + len(val) + 3


# ------------------------------------------------------------------ chrome

BRAND = "P  U  L  S  E"


def _seg(name: str, val: str) -> str:
    return (name + ": " if name else "") + val


def _bits_w(bits) -> int:
    """Columns a run of `label: value` pairs needs, separators included."""
    return sum(len(_seg(n, v)) for n, v, _ in bits) + 3 * (len(bits) - 1)


def _draw_bits(scr: Screen, x: int, y: int, bits, limit: int) -> int:
    """Lay the pairs out left to right, dropping any that would pass `limit`."""
    for i, (name, val, col) in enumerate(bits):
        seg = _seg(name, val)
        if x + (3 if i else 0) + len(seg) - 1 > limit:
            break
        if i:
            scr.put(x + 1, y, SEP, P.FRAME)
            x += 3
        if name:
            scr.put(x, y, name + ":", P.FAINT)
        scr.put(x + len(seg) - len(val), y, val, col)
        x += len(seg)
    return x


def header(scr: Screen, r: Rect, s: C.Sampler, app) -> None:
    y = r.y
    clock = time.strftime("%H:%M:%S")
    clock_x = r.x + r.w - len(clock)
    pause_w = 9 if app.paused else 0

    # The brand holds the centre of the bar; readings flow either side of it.
    brand_x = r.x + (r.w - len(BRAND)) // 2
    limit = brand_x - 3

    tail = [("uptime", hduration(s.uptime), P.MUTED),
            ("procs", str(s.proc_count), P.MUTED)]
    # The host name yields width first, so uptime and procs survive a narrow bar.
    budget = limit - r.x + 1 - len("host: ") - 3 - _bits_w(tail)
    host = trunc(s.host, int(clamp(budget, 4, 24)))
    _draw_bits(scr, r.x, y, [("host", host, P.BRIGHT)] + tail, limit)

    load1 = s.load[0]
    right = [("load", "%.2f %.2f %.2f" % s.load[:3],
              P.status(clamp(load1 / max(1, s.ncores), 0, 1)))]
    if s.container:
        right.append(("", s.container, P.MUTED))
    if s.battery:
        right.append(("bat", "%d%%" % s.battery[0],
                      P.status(1.0 - s.battery[0] / 100.0)))
    redge = clock_x - pause_w - 2
    rx = redge - _bits_w(right) + 1
    if rx > brand_x + len(BRAND) + 1:
        _draw_bits(scr, rx, y, right, redge)

    scr.put(brand_x, y, BRAND, P.ACCENT, attr=A_BOLD)
    if app.paused:
        scr.put(clock_x - pause_w, y, " PAUSED ", P.BRIGHT, P.SEL_BG, A_BOLD)
    scr.put(clock_x, y, clock, P.FG)


HINTS = [
    ("\u2191\u2193", "select"), ("f", "filter"), ("c m t", "sort"),
    ("T", "tree"), ("k", "kill"), ("\u21b5", "details"),
    ("+ -", "rate"), ("?", "keys"), ("q", "quit"),
]


def footer(scr: Screen, r: Rect, s: C.Sampler, app) -> None:
    y = r.y
    if app.mode == "filter":
        scr.put(r.x, y, " search ", P.BRIGHT, P.SEL_BG, A_BOLD)
        scr.put(r.x + 9, y, app.filter + "\u2588", P.FG)
        scr.put(r.x + 11 + len(app.filter), y,
                "name, command, user or pid " + SEP + " esc clears", P.FAINT)
        return

    beat = time.monotonic() < app.blink_until
    rate = "every %s" % hspan(app.interval_ms / 1000.0)
    if app.interval_ms < 1000:
        rate = "every %dms" % app.interval_ms
    tx = r.x + r.w - len(rate) - 2
    scr.cell(tx, y, "\u25cf", P.ACCENT if beat else P.FRAME)
    scr.put(tx + 2, y, rate, P.FAINT)

    if app.toast and time.monotonic() < app.toast_until:
        scr.put(r.x, y, " " + app.toast + " ", P.BRIGHT, P.SEL_BG, A_BOLD)
        return
    hints = HINTS if app.is_default else [("esc", "reset")] + HINTS
    x = r.x
    for k, v in hints:
        if x + len(k) + len(v) + 2 > tx - 1:
            break
        scr.put(x, y, k, P.FG, attr=A_BOLD)
        x += len(k) + 1
        scr.put(x, y, v, P.FAINT)
        x += len(v) + 2


# ------------------------------------------------------------------ cpu

def cpu(scr: Screen, r: Rect, s: C.Sampler, app) -> None:
    iw, ih = r.w - 4, r.h - 2
    side = 24 if (iw >= 76 and s.ncores <= ih - 2) else 0
    right = _pct(s.cpu)
    if s.temp:
        right += "  %.0f\u00b0C" % s.temp
    inner = frame(scr, r, "cpu", right, s.cpu / 100.0)
    if inner.h <= 0:
        return

    gh = max(1, inner.h - 2)
    g = Rect(inner.x, inner.y, inner.w - side, gh)
    gridlines(scr, g)
    plot(scr, g, s.hist["cpu"], 100.0)
    scr.put(g.x, g.y, "100", P.FAINT)
    if gh >= 3:
        scr.put(g.x, g.y + gh - 1, "  0", P.FAINT)

    if side:
        cx = inner.x + inner.w - side + 1
        for i in range(s.ncores):
            v = s.cores[i]
            scr.put(cx, inner.y + i, "cpu%-2d" % i, P.FAINT)
            meter(scr, cx + 6, inner.y + i, side - 13, v / 100.0)
            scr.put(cx + side - 6, inner.y + i, "%3.0f%%" % v, P.value(v / 100.0))

    y = inner.y + inner.h - 2
    mw = max(8, min(28, inner.w // 3))
    scr.put(inner.x, y, "total", P.FAINT)
    meter(scr, inner.x + 6, y, mw, s.cpu / 100.0)
    x = inner.x + 7 + mw
    scr.put(x, y, _pct(s.cpu).rjust(5), P.status(s.cpu / 100.0), attr=A_BOLD)
    x += 7
    lim = inner.x + inner.w
    usr, sys_, io, steal = s.cpu_mix
    for name, v in (("user", usr), ("sys", sys_), ("wait", io), ("steal", steal)):
        if v <= 0 and name in ("wait", "steal"):
            continue
        x = _field(scr, x, y, name, "%.1f%%" % v, P.value(v / 100.0), lim)
    if s.freq and x + 10 < lim:
        _field(scr, x, y, "freq", "%.1fGHz" % (s.freq / 1000.0), P.MUTED, lim)

    y += 1
    x = inner.x
    lim = inner.x + inner.w
    if not side and s.ncores > 1 and inner.w > s.ncores + 26:
        scr.put(x, y, "cores", P.FAINT)
        heatstrip(scr, x + 6, y, s.cores, s.ncores)
        x += 7 + s.ncores
    if s.psi and x + 30 < lim:
        scr.put(x, y, "stalled", P.FAINT)
        x += 8
        for res, short in (("cpu", "cpu"), ("memory", "mem"), ("io", "io")):
            if res not in s.psi:
                continue
            v = s.psi[res][0]
            x = _field(scr, x, y, short, "%.1f%%" % v,
                       P.value(clamp(v / 25.0, 0, 1)), lim)
    blk = s.misc.get("blocked", 0)
    if x + 22 < lim:
        x = _field(scr, x, y, "running", str(s.load[3]), P.MUTED, lim)
        _field(scr, x, y, "blocked", str(blk), P.CRIT if blk else P.MUTED, lim)


# ------------------------------------------------------------------ memory

def memory(scr: Screen, r: Rect, s: C.Sampler, app) -> None:
    m = s.mem
    total = m["total"] or 1
    used_f = m["used"] / total
    inner = frame(scr, r, "memory",
                  "%s of %s" % (hbytes(m["used"]), hbytes(total)), used_f)
    if inner.h <= 0:
        return
    w = inner.w

    y = inner.y
    bw = max(10, min(34, w - 12))
    scr.put(inner.x, y, "ram", P.FAINT)
    stacked(scr, inner.x + 5, y, bw, [
        (used_f, P.status(used_f), BAR),
        (m["cached"] / total, P.CACHE, SOFT),
    ])
    scr.put(inner.x + 6 + bw, y, _pct(used_f * 100).rjust(5),
            P.status(used_f), A_BOLD)

    y += 1
    x = inner.x + 5
    lim = inner.x + w
    x = _field(scr, x, y, "used", hbytes(m["used"]), P.status(used_f), lim)
    x = _field(scr, x, y, "cache", hbytes(m["cached"]), P.CACHE, lim)
    _field(scr, x, y, "free", hbytes(m["avail"]), P.MUTED, lim)

    y += 1
    st = m["swap_total"]
    scr.put(inner.x, y, "swap", P.FAINT)
    if st:
        sf = m["swap_used"] / st
        meter(scr, inner.x + 5, y, bw, sf)
        scr.put(inner.x + 6 + bw, y, _pct(sf * 100).rjust(5), P.status(sf))
        _field(scr, inner.x + 12 + bw, y, "of", hbytes(st), P.MUTED, lim)
    else:
        scr.put(inner.x + 5, y, "off", P.FAINT)

    y += 1
    if y < inner.y + inner.h and s.disks:
        hrule(scr, inner, y - inner.y, "disk")
        y += 1
        for d in s.disks:
            if y >= inner.y + inner.h:
                break
            scr.put(inner.x, y, trunc(d["name"], 7).ljust(8), P.FG)
            meter(scr, inner.x + 8, y, 8, d["util"] / 100.0)
            scr.put(inner.x + 17, y, "%3.0f%%" % d["util"], P.value(d["util"] / 100.0))
            x = _field(scr, inner.x + 23, y, "read", hrate(d["r"]), P.MUTED,
                       inner.x + inner.w)
            _field(scr, x, y, "write", hrate(d["w"]), P.MUTED, inner.x + inner.w)
            y += 1

    if y < inner.y + inner.h - 1 and s.fs:
        hrule(scr, inner, y - inner.y, "mounts")
        y += 1
        for d in s.fs:
            if y >= inner.y + inner.h:
                break
            f = d["pct"] / 100.0
            scr.put(inner.x, y, trunc(d["mnt"], 13).ljust(14), P.FG)
            meter(scr, inner.x + 14, y, 10, f)
            scr.put(inner.x + 25, y, "%3.0f%%" % d["pct"], P.status(f))
            scr.put(inner.x + 30, y, "%s used" % hbytes(d["used"], 5), P.status(f))
            if inner.w >= 51:
                scr.put(inner.x + 41, y, "%s free" % hbytes(d["free"], 5), P.MUTED)
            if inner.w >= 60:
                scr.put(inner.x + 52, y, "of " + hbytes(d["total"], 5), P.FAINT)
            y += 1


# ------------------------------------------------------------------ network

def _rate(scr: Screen, right_edge: int, y: int, arrow: str, val: str, col: int) -> None:
    """Right-align the number but keep its arrow tucked against it."""
    scr.put(right_edge - len(val) - 2, y, arrow, P.FAINT)
    scr.put(right_edge - len(val), y, val, col)


def network(scr: Screen, r: Rect, s: C.Sampler, app) -> None:
    right = "%s %s  %s %s" % (DOWN_ARROW, hrate(s.rx), UP_ARROW, hrate(s.tx))
    inner = frame(scr, r, "network", right)
    if inner.h <= 0:
        return

    list_h = min(2, max(0, inner.h - 4))
    gh = max(2, inner.h - list_h)
    top = gh // 2
    bot = gh - top
    scale = max(max(s.hist["rx"], default=0.0), max(s.hist["tx"], default=0.0), 1024.0)

    plot(scr, Rect(inner.x, inner.y, inner.w, top), s.hist["rx"], scale,
         solid=P.DOWN)
    plot(scr, Rect(inner.x, inner.y + top, inner.w, bot), s.hist["tx"], scale,
         flip=True, solid=P.UP)

    scr.put(inner.x, inner.y, "%s %s" % (DOWN_ARROW, hrate(s.rx)), P.DOWN, attr=A_BOLD)
    scr.put(inner.x, inner.y + top, "%s %s" % (UP_ARROW, hrate(s.tx)), P.UP, attr=A_BOLD)
    peak = "peak " + hrate(scale)
    scr.put(inner.x + inner.w - len(peak), inner.y, peak, P.FAINT)

    y = inner.y + gh
    if y < inner.y + inner.h:
        name = s.primary or "network"
        scr.put(inner.x, y, trunc(name, 12).ljust(13), P.FG, attr=A_BOLD)
        _rate(scr, inner.x + 24, y, DOWN_ARROW, hrate(s.rx),
              P.DOWN if s.rx else P.FAINT)
        _rate(scr, inner.x + 38, y, UP_ARROW, hrate(s.tx),
              P.UP if s.tx else P.FAINT)
        pick = next((n for n in s.nets if n["name"] == s.primary), None)
        if pick and inner.w > 52 and (pick["errs"] or pick["drops"]):
            bad = "  ".join(t for t in (
                "%d err" % pick["errs"] if pick["errs"] else "",
                "%d drop" % pick["drops"] if pick["drops"] else "") if t)
            scr.put(inner.x + 41, y, bad, P.WARN)
        y += 1
    if y < inner.y + inner.h:
        scr.put(inner.x, y, "usage", P.FAINT)
        _rate(scr, inner.x + 22, y, DOWN_ARROW, hbytes(s.rx_total), P.MUTED)
        _rate(scr, inner.x + 36, y, UP_ARROW, hbytes(s.tx_total), P.MUTED)
        total = "%s total" % hbytes(s.rx_total + s.tx_total)
        if inner.w > 56:
            scr.put(inner.x + 41, y, total, P.FAINT)


# ------------------------------------------------------------------ processes

SORTS = [
    ("cpu", "CPU%", lambda p: p.cpu),
    ("mem", "MEM%", lambda p: p.rss),
    ("thr", "THR", lambda p: p.threads),
    ("pid", "PID", lambda p: p.pid),
    ("time", "TIME", lambda p: p.cputime),
    ("name", "NAME", lambda p: p.name.lower()),
    ("user", "USER", lambda p: (p.user.lower(), -p.cpu)),
]
SORT_KEYS = {s[0]: s for s in SORTS}


def _columns(w: int):
    cols = [["PID", 7, "r", "pid"]]
    if w >= 70:
        cols.append(["USER", 9, "l", "user"])
    cols.append(["CPU%", 6, "r", "cpu"])
    cols.append(["MEM%", 6, "r", "mem"])
    if w >= 56:
        cols.append(["RSS", 8, "r", "mem"])
    if w >= 86:
        cols.append(["THR", 5, "r", "thr"])
    if w >= 78:
        cols.append(["ST", 3, "c", None])
    if w >= 64:
        cols.append(["TIME", 9, "r", "time"])
    used = sum(c[1] + 1 for c in cols)
    cols.append(["COMMAND", max(10, w - used), "l", "name"])
    return cols


def _cell(p, key: str, prefix: str) -> tuple[str, int]:
    if key == "PID":
        return str(p.pid), P.MUTED
    if key == "USER":
        # Brighter, not amber: root is worth noticing but it is not a warning.
        return trunc(p.user, 9), P.FG if p.uid == 0 else P.MUTED
    if key == "CPU%":
        return "%.1f" % p.cpu, P.value(p.cpu / 100.0)
    if key == "MEM%":
        return "%.1f" % p.mem, P.value(p.mem / 50.0)
    if key == "RSS":
        return hbytes(p.rss), P.MUTED
    if key == "THR":
        return str(p.threads), P.MUTED
    if key == "ST":
        return p.state, (P.CRIT if p.state in "ZD" else
                         P.OK if p.state == "R" else P.FAINT)
    if key == "TIME":
        return hcputime(p.cputime), P.MUTED
    return prefix + (p.cmd or p.name), (P.FG if p.cpu >= 0.05 else P.MUTED)


def processes(scr: Screen, r: Rect, s: C.Sampler, app) -> None:
    rows = app.visible
    # Narrow terminals drop columns, so the title always states the sort order;
    # the header arrow is only a secondary cue.
    cols = _columns(r.w - 7)
    bits = []
    if app.selected_pid is not None:
        bits.append("pid %d" % app.selected_pid)
    if app.filter:
        bits.append("\u201c%s\u201d" % trunc(app.filter, 12))
    bits.append("%d of %d" % (len(rows), s.proc_count))
    bits.append(SORT_KEYS[app.sort][1] + ("\u2193" if app.sort_desc else "\u2191"))
    inner = frame(scr, r, "processes", ("  " + SEP + "  ").join(bits))
    if inner.h < 2:
        return

    app.col_layout = []
    x = inner.x + 2
    marked = False
    for name, width, align, skey in cols:
        active = skey == app.sort and not marked
        marked = marked or active
        head = name + ("\u2193" if app.sort_desc else "\u2191") if active else name
        txt = (head.rjust(width) if align == "r" else
               head.center(width) if align == "c" else head.ljust(width))
        scr.put(x, inner.y, txt[:width], P.BRIGHT if active else P.FAINT,
                -1, A_BOLD if active else 0)
        app.col_layout.append((x, width, skey))
        x += width + 1

    body = Rect(inner.x, inner.y + 1, inner.w, inner.h - 1)
    app.page = body.h
    app.clamp_scroll(len(rows))
    scrollbar(scr, inner.x + inner.w - 1, body.y, body.h, len(rows), app.scroll)

    for i in range(body.h):
        idx = app.scroll + i
        if idx >= len(rows):
            break
        p = rows[idx]
        y = body.y + i
        sel = idx == app.selected
        bg = P.SEL_BG if sel else (P.ROW_ALT if idx & 1 else -1)
        if bg >= 0:
            scr.fill(Rect(body.x, y, body.w - 1, 1), " ", P.FG, bg)
        if sel:
            scr.cell(inner.x, y, "\u258f", P.ACCENT, bg, A_BOLD)
        prefix = ("  " * (p.depth - 1) + "\u2514\u2500 ") if (app.tree and p.depth) else ""
        attr = A_BOLD if sel else 0
        x = inner.x + 2
        for name, width, align, _s in cols:
            txt, col = _cell(p, name, prefix if name == "COMMAND" else "")
            txt = trunc(txt, width)
            # trunc already caps the length, so justifying lands exactly on width.
            txt = (txt.rjust(width) if align == "r" else
                   txt.center(width) if align == "c" else txt.ljust(width))
            scr.put(x, y, txt, P.BRIGHT if sel else col, bg, attr)
            x += width + 1
    app.body_rect = body


# ------------------------------------------------------------------ overlays

def _box(scr: Screen, w: int, h: int, title: str) -> Rect:
    w = min(w, scr.w - 2)
    h = min(h, scr.h - 2)
    r = Rect((scr.w - w) // 2, (scr.h - h) // 2, w, h)
    scr.fill(r, " ", P.FG, P.BG)
    scr.shade(r, 0x000000)
    return frame(scr, r, title)


HELP = [
    ("moving around", ""),
    ("\u2191 \u2193", "select a row \u2014 pins it while the list re-sorts"),
    ("PgUp PgDn", "page up and down"),
    ("Home End", "jump to the first / last row"),
    ("mouse", "click a row, or a column header to sort by it"),
    ("", ""),
    ("finding things", ""),
    ("f  /", "search name, command line, user or pid"),
    ("c m t", "sort by cpu, memory, threads"),
    ("r", "reverse the sort order"),
    ("T", "show the parent / child tree"),
    ("u  K", "only my processes / include kernel threads"),
    ("", ""),
    ("acting on one", ""),
    ("\u21b5", "details: exe, cwd, open files, cgroup"),
    ("k  X", "terminate (SIGTERM) / force kill (SIGKILL)"),
    ("", ""),
    ("everything else", ""),
    ("esc", "close a panel, or reset sort / filter / selection"),
    ("L", "what is listening on which port"),
    ("z  space", "fullscreen process list / freeze"),
    ("+ -", "refresh faster / slower"),
    ("s", "save a JSON snapshot here"),
    ("q", "quit (or ctrl-c)"),
]


def help_overlay(scr: Screen, app) -> None:
    # Fall back to two columns rather than silently cutting entries off.
    heads = [i for i, (k, v) in enumerate(HELP) if k and not v]
    two = len(HELP) + 3 > scr.h - 2 and scr.w >= 128 and len(heads) > 1
    if two:
        mid = (len(HELP) + 1) // 2
        cut = min(heads[1:], key=lambda i: abs(i - mid))
        groups = [HELP[:cut], HELP[cut:]]
        bw = 128
    else:
        groups = [HELP]
        bw = 74
    tall = max(len(g) for g in groups)
    inner = _box(scr, bw, min(tall + 3, scr.h - 2), "keys")
    if inner.h <= 0:
        return
    colw = inner.w // len(groups)
    for ci, entries in enumerate(groups):
        x0 = inner.x + ci * colw
        for i, (k, v) in enumerate(entries):
            if i >= inner.h:
                break
            y = inner.y + i
            if not v and k:
                scr.put(x0, y, k, P.ACCENT, attr=A_BOLD)
            elif k:
                scr.put(x0 + 1, y, k.rjust(11), P.FG, attr=A_BOLD)
                scr.put(x0 + 14, y, trunc(v, colw - 15), P.MUTED)


def listeners_overlay(scr: Screen, app) -> None:
    data = app.listeners
    h = min(len(data) + 4, max(6, scr.h - 4))
    inner = _box(scr, 74, h, "listening ports")
    if inner.h <= 1:
        return
    page = inner.h - 1
    app.lpage = page
    top = int(clamp(app.lscroll, 0, max(0, len(data) - page)))
    app.lscroll = top

    scr.put(inner.x, inner.y, "PORT   PROTO  BIND              PROCESS",
            P.FAINT, attr=A_BOLD)
    if len(data) > page:
        tag = "\u2191\u2193  %d-%d of %d" % (top + 1, min(top + page, len(data)), len(data))
    else:
        tag = "%d ports" % len(data)
    scr.put(inner.x + inner.w - len(tag), inner.y, tag, P.FAINT)

    for i in range(page):
        idx = top + i
        if idx >= len(data):
            break
        proto, addr, port, uid, inode = data[idx]
        y = inner.y + 1 + i
        scr.put(inner.x, y, str(port).ljust(7), P.BRIGHT, attr=A_BOLD)
        scr.put(inner.x + 7, y, proto.ljust(7), P.MUTED)
        scr.put(inner.x + 14, y, trunc(addr, 17).ljust(18), P.FG)
        own = app.lowners.get(inode)
        if own:
            scr.put(inner.x + 32, y, trunc("%s  %d" % (own[1], own[0]), 24), P.OK)
        else:
            scr.put(inner.x + 32, y, C._user(uid), P.FAINT)
    scrollbar(scr, inner.x + inner.w - 1, inner.y + 1, page, len(data), top)
    if not data:
        scr.put(inner.x, inner.y + 1, "nothing is listening", P.FAINT)


def _detail_lines(pid: int, p) -> list[tuple[str, str]]:
    base = "/proc/%d" % pid
    out = [("name", p.name), ("pid", str(pid)), ("parent", str(p.ppid)),
           ("user", "%s (uid %d)" % (p.user, p.uid)),
           ("state", "%s \u2014 %s" % (p.state, C.STATE_NAMES.get(p.state, "?"))),
           ("threads", str(p.threads)), ("nice", str(p.nice)),
           ("cpu", "%.1f%% now, %s total" % (p.cpu, hcputime(p.cputime))),
           ("memory", "%s resident, %s virtual (%.1f%%)"
            % (hbytes(p.rss), hbytes(p.vsz), p.mem)),
           ("started", hduration(p.elapsed) + " ago")]
    for lbl, path in (("executable", "/exe"), ("working dir", "/cwd")):
        try:
            out.append((lbl, os.readlink(base + path)))
        except OSError:
            pass
    try:
        out.append(("open files", str(len(os.listdir(base + "/fd")))))
    except OSError:
        pass
    cg = C._read(base + "/cgroup").strip().splitlines()
    if cg:
        out.append(("cgroup", cg[-1].split(":")[-1]))
    io = C._read(base + "/io")
    if io:
        rd = wr = 0
        for line in io.splitlines():
            if line.startswith("read_bytes:"):
                rd = int(line.split()[1])
            elif line.startswith("write_bytes:"):
                wr = int(line.split()[1])
        out.append(("disk", "%s read, %s written" % (hbytes(rd), hbytes(wr))))
    status = C._read(base + "/status")
    for key, lbl in (("voluntary_ctxt_switches", "ctx switches"),
                     ("nonvoluntary_ctxt_switches", "ctx preempted")):
        for line in status.splitlines():
            if line.startswith(key):
                out.append((lbl, line.split()[-1]))
    cmd = C.read_cmdline(pid)
    if cmd:
        out.append(("command", cmd))
    return out


def detail_overlay(scr: Screen, app) -> None:
    p = app.detail_proc
    if p is None:
        return
    w = min(scr.w - 4, 94)
    wrapped: list[tuple[str, str]] = []
    for k, v in _detail_lines(p.pid, p):
        avail = w - 20
        if len(v) <= avail:
            wrapped.append((k, v))
        else:
            for j in range(0, len(v), avail):
                wrapped.append((k if j == 0 else "", v[j:j + avail]))
    inner = _box(scr, w, min(len(wrapped) + 3, scr.h - 2), "%s \u2014 pid %d" % (p.name, p.pid))
    if inner.h <= 0:
        return
    for i, (k, v) in enumerate(wrapped[: inner.h]):
        y = inner.y + i
        if k:
            scr.put(inner.x + 1, y, k.rjust(14), P.FAINT)
        scr.put(inner.x + 17, y, v, P.FG if k else P.MUTED)


def confirm_overlay(scr: Screen, app) -> None:
    sig, p = app.pending_kill
    inner = _box(scr, 60, 5, "confirm")
    if inner.h <= 0:
        return
    verb = "Terminate" if sig == "SIGTERM" else "Force kill"
    scr.put(inner.x + 1, inner.y, verb, P.CRIT, attr=A_BOLD)
    scr.put(inner.x + 2 + len(verb), inner.y, "%s (pid %d)?" % (p.name, p.pid), P.FG)
    scr.put(inner.x + 1, inner.y + 2, "y", P.BRIGHT, P.SEL_BG, A_BOLD)
    scr.put(inner.x + 3, inner.y + 2, "yes", P.MUTED)
    scr.put(inner.x + 8, inner.y + 2, "n", P.BRIGHT, P.SEL_BG, A_BOLD)
    scr.put(inner.x + 10, inner.y + 2, "cancel", P.MUTED)


def too_small(scr: Screen) -> None:
    msg = "terminal too small \u2014 needs at least 60 x 16"
    scr.put(max(0, (scr.w - len(msg)) // 2), scr.h // 2, msg[: scr.w], P.WARN)

# pulse

A terminal system monitor for Linux.

```
./pulse
```

No `pip install`, no `cargo build`, no shared libraries. Python 3.8+ stdlib only.
Everything comes straight from `/proc` and `/sys` — pulse never shells out.

## Why it beats btop

| | btop | pulse |
|---|---|---|
| Install | compile or package manager | copy a folder, run it |
| PSI pressure stall | no | yes — `cpu`/`mem`/`io` saturation |
| Per-device disk util% + queue depth | partial | yes |
| Cumulative network use since boot | no | yes |
| Listening sockets view | no | yes — `L` |
| Process inspector | no | yes — `↵` shows exe, cwd, fds, cgroup, ctx switches |
| Selection survives re-sorting | by row | pinned to PID |
| Machine-readable output | no | `--json` snapshot, `s` to dump while running |
| Renders to a pipe/file | no | `--once` |

Pressure stall info is the one that matters. CPU can read 40% while the box is
falling over, because tasks are stalled on memory reclaim or I/O rather than
burning cycles. `stalled cpu/mem/io` on the CPU panel shows the percentage of
time work was stalled — it goes red before load average notices anything.

## Keys

| | |
|---|---|
| `↑` `↓` `PgUp` `PgDn` `Home` `End` | move selection — **pins it to that process** |
| `esc` | close a panel, or reset sort / filter / selection |
| `c` `m` `t` | sort by cpu / memory / threads |
| `r` | reverse sort |
| `f` or `/` | search name, command line, user or pid |
| `T` | process tree |
| `u` | only my processes |
| `K` | show kernel threads |
| `↵` | inspect selected process |
| `k` / `X` | SIGTERM / SIGKILL (with confirmation) |
| `L` | listening ports (scrollable) |
| `z` | zoom process panel full screen |
| `s` | write a JSON snapshot to cwd |
| `space` | pause |
| `+` `-` | faster / slower refresh |
| `?` | help |
| `q` or `ctrl-c` | quit |

Mouse works too: click a row to select, click a column header to sort, wheel to
scroll. `PID`, `USER`, `TIME` and `COMMAND` have no key binding but are still
sortable by clicking them.

`esc` never exits. It backs out of whatever you are in: it closes an open panel,
or if you are already on the list, puts the view back exactly as it started —
default sort, no filter, no pinned row, no tree, no zoom. Your refresh rate and
pause state are settings rather than view state, so they survive it. Only `q`
and `ctrl-c` quit.

## The selection is pinned, not positional

A sorted-by-CPU list reorders constantly, so a monitor that tracks *row number*
will hand you a different process between the moment you aim and the moment you
press `k`. pulse tracks the **PID** instead, and holds it on the same screen
line while the list churns underneath. Once you highlight something it stays
highlighted — the title shows `pid 1234` and the row is marked with a bar — so
`k` always hits what you were looking at. `esc` lets go.

If the pinned process exits, the selection falls back to the same screen line
and re-pins to whatever is there.

## Search

`f` (or `/`) searches the **full command line**, not just the process name. A
Java service is called `java` and a script is called `python3`, so matching only
the name is close to useless — typing `pulse` finds `python3 ./pulse` the way you
would expect.

Terms are ANDed, so `python pulse` matches lines containing both. Name, argv,
username and an exact pid are all searched, case-insensitively.

Reading argv for every process costs about 3 ms, so it happens only while a
search is active. Results are cached per process and keyed on its start tick, so
only the first filtered refresh pays: subsequent ones cost ~0.2 ms. Because a
recycled PID always carries a different start tick, the cache can never serve
one process's command line for another.

## Listening ports

`L` lists every port the machine is listening on, one row per port: IPv4 and
IPv6, and a service bound to several addresses, collapse into a single entry
rather than repeating. It scrolls with the arrows, PgUp/PgDn, Home/End and the
wheel, and the header shows your position so nothing is silently cut off.

Where the owning process can be identified it is shown, resolved by matching
socket inodes against `/proc/*/fd`. The kernel only lets you read another
process's file descriptors if you own it, so you get names for your own services
and a username for the rest; run as root to name everything.

Ports inside a container's network namespace do not appear here — the host only
sees what is published to it.

## Network

A box with Docker on it has a dozen `veth*` interfaces that mirror traffic you
are already counting, so the panel tracks exactly one: the interface holding the
default route, read from `/proc/net/route`. That is the one actually facing the
internet. If there is no default route it falls back to the busiest physical
interface.

Under the live rate the `usage` row shows **cumulative bytes since boot**, both
directions plus a combined total — useful on a metered or capped connection.
Those counters come from the interface itself, so they reset if it is taken
down, not only at reboot.

Every interface is still recorded in `--json` output.

## Refresh rate

One rate for everything — graphs, meters and the process list all sample
together. Default is **2 seconds**. `+` and `-` step through 250ms · 500ms · 1s ·
2s · 3s · 5s · 10s while running, or start at one with `./pulse -i 500`.

The current rate sits bottom-right as `● every 2s`. The dot flashes for 90 ms on
each sample and then goes dark, so the beat is a blink you can count rather than
a light that is simply on — if it stops blinking, pulse is wedged. `space`
freezes it.

Graphs are 2 samples per column, so the window they cover scales with the rate:
at the default a 60-column chart holds four minutes, and at 250 ms the same
chart holds thirty seconds. History is capped at 1024 samples per series.

## CLI

```
./pulse                  # interactive
./pulse -i 250           # 250 ms refresh (clamped to 100–10000)
./pulse --json           # one snapshot to stdout, exit
./pulse --once           # render one frame as plain text (also automatic when piped)
./pulse --no-mouse
./pulse --version
```

`--json` makes it a metrics collector: `./pulse --json | jq .pressure`.

## How it stays cheap

Measured on a 2-core ARM VM, 316 processes, 160x45 terminal:

| rate | CPU | RSS | terminal bytes |
|---|---|---|---|
| 250 ms | 4.5% of one core | 14 MiB | 28 KiB/s |
| 500 ms | 2.2% of one core | 14 MiB | 18 KiB/s |
| 1 s | 1.3% of one core | 14 MiB | 10 KiB/s |
| **2 s (default)** | **0.6% of one core** | 14 MiB | 5.6 KiB/s |
| 5 s | 0.4% of one core | 14 MiB | 3.1 KiB/s |

A tick costs 4.8 ms to sample and 1.3 ms to draw; most of the rest of the second
is spent in `select()`. Of the 14 MiB resident, about 11 MiB is the CPython
interpreter itself.

- **Diff rendering.** The screen is a cell grid. On each frame only the changed
  span of each changed row is written, so a mostly-idle screen costs a few
  hundred bytes instead of a full repaint. Nothing is ever cleared, so there is
  no flicker and no tearing on a slow SSH link. The finished grid becomes the
  next frame's baseline rather than being copied.
- **Raw `/proc` reads.** Per-process stats go through `os.open`/`os.read`
  directly; the buffered `open()` wrapper more than doubles the cost of a
  one-shot read, and that loop runs once per process per tick. Only the fields
  actually used are split out of each line.
- **Identity caching.** A process's uid and owner are looked up once and then
  validated against its start tick, which removes a `stat()` syscall per
  process per tick without ever serving stale data across PID reuse.
- **Lazy `cmdline` reads.** `/proc/<pid>/cmdline` is only read for the rows
  actually on screen, not for all 316 processes.
- **One pass over `/proc/stat`.** The kernel regenerates that file on every open
  and it grows a line per core, so CPU times and the context-switch and fork
  counters come from a single read. The enormous `intr` and `softirq` lines are
  never split.
- **Cached slow data.** Filesystem usage is refreshed every 5 s (a `statvfs` can
  block outright on a stalled NFS mount), mount tables every 10 s, temperature
  sensors every 2 s, battery every 15 s. Things that cannot change — which cores
  expose cpufreq, an interface's link speed, whether a block device is a whole
  disk — are probed once.

## Layout

The top bar reads `host: · uptime: · procs:` on the left and load average on the
right, with the brand held in the centre and the clock in the corner. When the
bar gets narrow the host name is the first thing to shorten, so uptime and
process count survive.

Chrome is neutral grey so it recedes; colour is spent only where it means
something. Load runs **green → amber → red** past 60% and 85%, so you read health
by hue instead of decoding shades. Network keeps two cool hues, cyan down and
violet up. Anything idle is greyed out rather than coloured, so the things that
are actually busy are the things that catch your eye.

The memory bar is segmented rather than a single fill — `█` used, `▒` cache, then
track — because "93% full" means nothing until you know how much of it is
reclaimable cache. Distinct glyphs mean it still reads correctly if you are
colour-blind or piping through something lossy. Each mount shows used, free and
total on one line, right-aligned so the figures line up down the column.

Graphs are braille — 2 samples per column and 4 levels per row, so a 30x8 panel
holds 60 samples at 32 levels of vertical resolution, over faint gridlines at
25/50/75%. Meters use 1/8-width blocks for sub-cell precision.

The process table has zebra-striped rows, a position scrollbar, and a bar marker
on the pinned row. Panels carry a column of interior padding so nothing sits
flush against a border.

The layout is responsive, and everything clips to its rect so nothing ever
bleeds:

| terminal | result |
|---|---|
| ≥ 100 cols | memory and network side by side |
| < 100 cols, tall | memory and network stacked |
| < 100 cols, short | network drops |
| < 25 rows | memory and network drop, leaving CPU and processes |
| < 60x16 | "resize" notice |

Process names and argv are attacker-controlled, so everything drawn from them is
stripped of control characters, bidi overrides, zero-width joiners and
double-width glyphs first. A process cannot name itself something that reorders
your terminal or desyncs the cell grid.

## Tests

```
python3 -m unittest test_pulse      # 164 tests, ~45s
python3 mutate_check.py             # proves the suite catches regressions
```

The optimised readers and drawing primitives are checked against reference
implementations of the code they replaced — `put`, `fill`, `plot`, `sanitize`
and the `/proc/stat` reader are all fuzzed against the originals. Three things
go beyond ordinary unit tests:

- An **ANSI replayer** parses what `flush()` emits back into a grid and asserts
  it reproduces the intended screen exactly, which is what proves the diff
  renderer never skips an update.
- A **fake `/proc`** makes PID reuse, permission denials, vanishing processes,
  truncated files and command names like `evil ) name` deterministic.
- The real binary is driven through a **pty**, resized mid-session, and sent
  every key and mouse event.

`mutate_check.py` breaks each optimisation on purpose — wrong field offsets, a
cache that ignores its invalidation key, off-by-one clipping — and confirms the
suite fails each time. A green suite that cannot fail is worthless.

## Files

```
pulse.py            CLI entry
pulseui/term.py     raw tty, alt screen, input decoding, diff-rendered Screen
pulseui/draw.py     braille plots, meters, frames, formatters
pulseui/collect.py  all /proc + /sys reads
pulseui/panels.py   panel renderers and overlays
pulseui/app.py      state, layout, event loop
pulseui/palette.py  the green
test_pulse.py       regression suite
mutate_check.py     mutation check for the suite
```

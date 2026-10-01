"""All data acquisition. Pure /proc + /sys reads, no external processes, no deps."""

from __future__ import annotations

import os
import pwd
import time
import unicodedata
from collections import deque

HZ = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGESIZE")
SECTOR = 512
HIST = 1024

_CTRL = {c: 32 for c in range(32)}
_CTRL[127] = 32


def sanitize(s: str) -> str:
    """Strip anything that would desync the cell grid: controls, combining and
    double-width glyphs. Process names and argv are attacker-controlled."""
    if s.isascii():
        # isprintable() rules out every ASCII char _CTRL maps, so the common
        # case returns the original string instead of building a copy.
        return s if s.isprintable() else s.translate(_CTRL)
    out = []
    for ch in s:
        # Covers C0/C1 controls, bidi overrides, zero-width joiners and
        # unassigned code points: all render in zero or unpredictable columns.
        if not ch.isprintable():
            out.append(" ")
        elif unicodedata.combining(ch):
            continue
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)

_uid_cache: dict[int, str] = {}


def _user(uid: int) -> str:
    u = _uid_cache.get(uid)
    if u is None:
        try:
            u = pwd.getpwuid(uid).pw_name
        except KeyError:
            u = str(uid)
        _uid_cache[uid] = u
    return u


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def _readint(path: str, default: int = -1) -> int:
    try:
        with open(path, "rb") as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return default


# ------------------------------------------------------------------ static

def hostname() -> str:
    return _read("/etc/hostname").strip() or os.uname().nodename


def cpu_model() -> str:
    txt = _read("/proc/cpuinfo")
    for key in ("model name", "Model", "Hardware", "cpu model"):
        for line in txt.splitlines():
            if line.startswith(key):
                return line.split(":", 1)[1].strip()
    mach = os.uname().machine
    impl = ""
    for line in txt.splitlines():
        if line.startswith("CPU implementer"):
            impl = {"0x41": "ARM", "0x43": "Cavium", "0x50": "APM"}.get(
                line.split(":")[1].strip(), "")
    return (impl + " " + mach).strip()


def in_container() -> str:
    if os.path.exists("/.dockerenv"):
        return "docker"
    cg = _read("/proc/1/cgroup")
    for tag in ("docker", "lxc", "kubepods", "containerd", "podman"):
        if tag in cg:
            return tag
    if "container=" in _read("/proc/1/environ").replace("\0", " "):
        return "container"
    return ""


# ------------------------------------------------------------------ cpu

_MISC_KEYS = {b"ctxt": "ctxt", b"processes": "forks", b"procs_running": "running",
              b"procs_blocked": "blocked", b"intr": "intr", b"btime": "btime"}


def read_cpu() -> tuple[list[tuple[int, int]], list[int], dict[str, int]]:
    """Returns ([(total, busy) per cpu, index 0 = aggregate], aggregate raw
    fields, misc counters).

    The kernel regenerates /proc/stat on every open and it grows a line per
    core, so the whole sample takes exactly one pass over it.
    """
    out: list[tuple[int, int]] = []
    agg: list[int] = []
    misc = {"ctxt": 0, "forks": 0, "running": 0, "blocked": 0, "intr": 0, "btime": 0}
    try:
        with open("/proc/stat", "rb") as f:
            for raw in f:
                sp = raw.find(b" ")
                if sp < 0:
                    continue
                if raw.startswith(b"cpu"):
                    p = raw.split(None, 9)
                    v = [int(x) for x in p[1:9]] if len(p) >= 9 else [int(x) for x in p[1:]]
                    while len(v) < 8:
                        v.append(0)
                    idle = v[3] + v[4]
                    total = sum(v)
                    out.append((total, total - idle))
                    if not agg:
                        agg = v
                    continue
                key = _MISC_KEYS.get(raw[:sp])
                if key is not None:
                    # intr and softirq carry hundreds of fields; never split them.
                    try:
                        misc[key] = int(raw[sp + 1:].split(None, 1)[0])
                    except (ValueError, IndexError):
                        pass
    except OSError:
        pass
    return out, agg, misc


_freq_paths: list[str] | None = None
_freq_fallback = (0.0, 0.0)


def read_freq(ncores: int) -> float:
    """Average current core frequency in MHz, or 0."""
    global _freq_paths, _freq_fallback
    if _freq_paths is None:
        # Which cores expose cpufreq never changes; probe once, not every tick.
        _freq_paths = [p for p in
                       ("/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % i
                        for i in range(ncores)) if os.path.exists(p)]
    tot = n = 0
    for path in _freq_paths:
        khz = _readint(path, -1)
        if khz > 0:
            tot += khz // 1000
            n += 1
    if n:
        return tot / n
    # No cpufreq: the only source left is a full /proc/cpuinfo parse, and the
    # value there is static on such machines anyway.
    at, val = _freq_fallback
    now = time.monotonic()
    if at and now - at < 5.0:
        return val
    ftot = fn = 0.0
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("cpu mhz"):
            try:
                ftot += float(line.split(":")[1])
                fn += 1
            except ValueError:
                pass
    val = ftot / fn if fn else 0.0
    _freq_fallback = (now, val)
    return val


def read_temps() -> list[tuple[str, float]]:
    """(label, celsius) from hwmon, falling back to thermal zones."""
    out: list[tuple[str, float]] = []
    try:
        for hw in sorted(os.listdir("/sys/class/hwmon")):
            base = "/sys/class/hwmon/" + hw
            chip = _read(base + "/name").strip()
            for ent in sorted(os.listdir(base)):
                if not (ent.startswith("temp") and ent.endswith("_input")):
                    continue
                milli = _readint(base + "/" + ent, -1)
                if milli <= 0 or milli > 200000:
                    continue
                lbl = _read(base + "/" + ent[:-6] + "_label").strip()
                out.append(((lbl or chip or "temp"), milli / 1000.0))
    except OSError:
        pass
    if not out:
        try:
            for z in sorted(os.listdir("/sys/class/thermal")):
                if not z.startswith("thermal_zone"):
                    continue
                milli = _readint("/sys/class/thermal/%s/temp" % z, -1)
                if 0 < milli <= 200000:
                    typ = _read("/sys/class/thermal/%s/type" % z).strip() or z
                    out.append((typ, milli / 1000.0))
        except OSError:
            pass
    return out


def pick_cpu_temp(temps: list[tuple[str, float]]) -> float:
    prefer = ("package", "tctl", "tdie", "cpu", "core 0", "soc", "k10temp", "coretemp")
    for want in prefer:
        for lbl, v in temps:
            if want in lbl.lower():
                return v
    return max((v for _, v in temps), default=0.0)


# ------------------------------------------------------------------ memory

def read_mem() -> dict[str, int]:
    m: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "rb") as f:
            for raw in f:
                p = raw.split()
                if len(p) >= 2:
                    try:
                        m[p[0][:-1].decode()] = int(p[1]) * 1024
                    except ValueError:
                        pass
    except OSError:
        pass
    total = m.get("MemTotal", 0)
    avail = m.get("MemAvailable", m.get("MemFree", 0))
    cached = m.get("Cached", 0) + m.get("SReclaimable", 0) - m.get("Shmem", 0)
    return {
        "total": total,
        "avail": avail,
        "free": m.get("MemFree", 0),
        "used": max(0, total - avail),
        "cached": max(0, cached),
        "buffers": m.get("Buffers", 0),
        "shmem": m.get("Shmem", 0),
        "dirty": m.get("Dirty", 0),
        "writeback": m.get("Writeback", 0),
        "slab": m.get("Slab", 0),
        "swap_total": m.get("SwapTotal", 0),
        "swap_free": m.get("SwapFree", 0),
        "swap_used": max(0, m.get("SwapTotal", 0) - m.get("SwapFree", 0)),
        "commit": m.get("Committed_AS", 0),
        "commit_limit": m.get("CommitLimit", 0),
        "zswap": m.get("Zswap", 0),
    }


# ------------------------------------------------------------------ pressure

def read_psi() -> dict[str, tuple[float, float]]:
    """Pressure stall info: resource -> (some avg10, full avg10)."""
    out: dict[str, tuple[float, float]] = {}
    for res in ("cpu", "memory", "io"):
        txt = _read("/proc/pressure/" + res)
        if not txt:
            continue
        some = full = 0.0
        for line in txt.splitlines():
            try:
                v = float(line.split("avg10=")[1].split()[0])
            except (IndexError, ValueError):
                continue
            if line.startswith("some"):
                some = v
            elif line.startswith("full"):
                full = v
        out[res] = (some, full)
    return out


# ------------------------------------------------------------------ net

def read_net() -> dict[str, tuple[int, ...]]:
    out: dict[str, tuple[int, ...]] = {}
    try:
        with open("/proc/net/dev", "rb") as f:
            for raw in f:
                if b":" not in raw:
                    continue
                name, rest = raw.split(b":", 1)
                iface = name.strip().decode()
                p = rest.split()
                if len(p) < 16:
                    continue
                try:
                    out[iface] = (int(p[0]), int(p[1]), int(p[2]), int(p[3]),
                                  int(p[8]), int(p[9]), int(p[10]), int(p[11]))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


_IFACE_SPEED: dict[str, int] = {}


def iface_up(name: str) -> bool:
    return _read("/sys/class/net/%s/operstate" % name).strip() == "up"


def iface_speed(name: str) -> int:
    """Link speed is a property of the hardware, so it is read once per boot of
    the interface rather than once per tick."""
    v = _IFACE_SPEED.get(name)
    if v is None:
        v = _readint("/sys/class/net/%s/speed" % name, -1)
        _IFACE_SPEED[name] = v
    return v


VIRTUAL = ("veth", "docker", "br-", "virbr", "tun", "tap", "wg", "cni",
           "flannel", "kube", "dummy", "bond", "ifb", "vmnet", "zt")


def default_route_iface() -> str:
    """Interface carrying the default route — the one facing the internet."""
    try:
        with open("/proc/net/route", "r", errors="replace") as f:
            next(f, None)
            for line in f:
                p = line.split()
                if len(p) > 2 and p[1] == "00000000":
                    return p[0]
    except OSError:
        pass
    for path in ("/proc/net/ipv6_route",):
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    p = line.split()
                    # destination prefix length 0 == default route
                    if len(p) >= 10 and p[1] == "00":
                        return p[-1]
        except OSError:
            pass
    return ""


def read_sockets() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in _read("/proc/net/sockstat").splitlines():
        if line.startswith("sockets:"):
            out["used"] = int(line.split()[-1])
        elif line.startswith("TCP:"):
            p = line.split()
            out["tcp"] = int(p[2])
            out["tw"] = int(p[6]) if len(p) > 6 else 0
        elif line.startswith("UDP:"):
            out["udp"] = int(line.split()[2])
    return out


_HEX_ANY = ("00000000", "00000000000000000000000000000000")


def read_listeners() -> list[tuple[str, str, int, int, int]]:
    """(proto, bind, port, uid, socket inode) per listening port.

    One row per port: v4/v6 and multi-homed binds collapse together, since they
    are one service as far as anyone asking "what is open" is concerned.
    """
    groups: dict[tuple[str, int], list] = {}
    for proto, path, state in (("tcp", "/proc/net/tcp", "0A"),
                               ("tcp", "/proc/net/tcp6", "0A"),
                               ("udp", "/proc/net/udp", "07"),
                               ("udp", "/proc/net/udp6", "07")):
        for line in _read(path).splitlines()[1:]:
            p = line.split()
            if len(p) < 10 or p[3] != state:
                continue
            try:
                hexaddr, hexport = p[1].split(":")
                port = int(hexport, 16)
                uid = int(p[7])
                inode = int(p[9])
            except (ValueError, IndexError):
                continue
            addr = "*" if hexaddr in _HEX_ANY else _fmt_hex_addr(hexaddr)
            g = groups.get((proto, port))
            if g is None:
                groups[(proto, port)] = [[addr], uid, inode]
            elif addr not in g[0]:
                g[0].append(addr)

    out = []
    for (proto, port), (addrs, uid, inode) in groups.items():
        if "*" in addrs:
            bind = "*"
        elif len(addrs) == 1:
            bind = addrs[0]
        else:
            bind = "%s +%d" % (addrs[0], len(addrs) - 1)
        out.append((proto, bind, port, uid, inode))
    out.sort(key=lambda r: (r[2], r[0]))
    return out


def socket_owners(inodes) -> dict[int, tuple[int, str]]:
    """Map socket inode -> (pid, name) by walking /proc/*/fd.

    Only covers processes whose fds we may read: our own, or everything as root.
    """
    want = set(inodes)
    out: dict[int, tuple[int, str]] = {}
    if not want:
        return out
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        base = "/proc/" + entry + "/fd/"
        try:
            fds = os.listdir(base)
        except OSError:
            continue
        name = None
        for fd in fds:
            try:
                target = os.readlink(base + fd)
            except OSError:
                continue
            if not target.startswith("socket:["):
                continue
            try:
                ino = int(target[8:-1])
            except ValueError:
                continue
            if ino in want:
                if name is None:
                    name = sanitize(_read("/proc/%s/comm" % entry).strip()) or "?"
                out[ino] = (int(entry), name)
        if len(out) == len(want):
            break
    return out


def _fmt_hex_addr(h: str) -> str:
    if len(h) == 8:
        b = bytes.fromhex(h)[::-1]
        return "%d.%d.%d.%d" % tuple(b)
    try:
        groups = [h[i:i + 8] for i in range(0, 32, 8)]
        raw = b"".join(bytes.fromhex(g)[::-1] for g in groups)
        parts = ["%x" % int.from_bytes(raw[i:i + 2], "big") for i in range(0, 16, 2)]
        return ":".join(parts)
    except ValueError:
        return "?"


# ------------------------------------------------------------------ disk

_WHOLE_DEV: dict[str, bool] = {}


def read_diskstats() -> dict[str, tuple[int, int, int, int, int, int]]:
    """dev -> (read_bytes, write_bytes, reads, writes, io_ms, inflight)."""
    out: dict[str, tuple[int, int, int, int, int, int]] = {}
    try:
        with open("/proc/diskstats", "rb") as f:
            for raw in f:
                p = raw.split()
                if len(p) < 14:
                    continue
                name = p[2].decode()
                if name.startswith(("loop", "ram", "zram", "fd", "sr")):
                    continue
                whole = _WHOLE_DEV.get(name)
                if whole is None:
                    whole = os.path.isdir("/sys/block/" + name.replace("/", "!"))
                    _WHOLE_DEV[name] = whole
                if not whole:
                    continue  # partition, not a whole device
                try:
                    out[name] = (int(p[5]) * SECTOR, int(p[9]) * SECTOR,
                                 int(p[3]), int(p[7]), int(p[12]), int(p[11]))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


_FS_OK = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
          "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "overlay", "nfs", "nfs4"}


def read_mounts() -> list[tuple[str, str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    try:
        with open("/proc/mounts", "r", errors="replace") as f:
            for line in f:
                p = line.split()
                if len(p) < 3:
                    continue
                dev, mnt, fs = p[0], p[1].replace("\\040", " "), p[2]
                if fs not in _FS_OK or mnt in seen:
                    continue
                if dev in seen and fs == "overlay":
                    continue
                seen.add(mnt)
                out.append((dev, mnt, fs))
    except OSError:
        pass
    return out


def read_usage(mnt: str):
    try:
        st = os.statvfs(mnt)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if total <= 0:
        return None
    return total, total - free, free


# ------------------------------------------------------------------ misc

def read_load() -> tuple[float, float, float, int, int]:
    p = _read("/proc/loadavg").split()
    if len(p) < 4:
        return 0.0, 0.0, 0.0, 0, 0
    run, tot = (p[3].split("/") + ["0"])[:2]
    return float(p[0]), float(p[1]), float(p[2]), int(run), int(tot)


def read_uptime() -> float:
    try:
        return float(_read("/proc/uptime").split()[0])
    except (ValueError, IndexError):
        return 0.0


def read_battery():
    try:
        for name in sorted(os.listdir("/sys/class/power_supply")):
            base = "/sys/class/power_supply/" + name
            if _read(base + "/type").strip() != "Battery":
                continue
            cap = _readint(base + "/capacity", -1)
            if cap < 0:
                continue
            return cap, _read(base + "/status").strip()
    except OSError:
        pass
    return None


# ------------------------------------------------------------------ processes

class Proc:
    __slots__ = ("pid", "ppid", "name", "state", "threads", "nice", "rss", "vsz",
                 "cpu", "mem", "cputime", "elapsed", "uid", "user", "cmd",
                 "depth", "start")

    def __init__(self):
        self.cmd = None
        self.depth = 0


def read_cmdline(pid: int) -> str:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            raw = f.read(4096)
    except OSError:
        return ""
    if not raw:
        return ""
    return sanitize(raw.replace(b"\0", b" ").decode("utf-8", "replace")).strip()


STATE_NAMES = {
    "R": "running", "S": "sleeping", "D": "disk-wait", "Z": "zombie",
    "T": "stopped", "t": "traced", "I": "idle", "X": "dead", "W": "paging",
}


class Sampler:
    """Owns all previous-sample state and the rolling history buffers."""

    def __init__(self, history: int = HIST):
        self.ncores = os.cpu_count() or 1
        self.model = cpu_model()
        self.host = hostname()
        self.kernel = os.uname().release
        self.container = in_container()
        self.boot_id = _read("/proc/sys/kernel/random/boot_id").strip()[:8]

        self.t = 0.0
        self.dt = 0.0
        self.cpu_prev: list[tuple[int, int]] = []
        self.cpu_agg_prev: list[int] = []
        self.net_prev: dict[str, tuple[int, ...]] = {}
        self.disk_prev: dict[str, tuple[int, ...]] = {}
        self.proc_prev: dict[int, tuple[int, int, int]] = {}
        self.proc_ids: dict[int, tuple[int, int, str]] = {}
        self._cmd_cache: dict[int, tuple[int, str]] = {}
        self.misc_prev: dict[str, int] = {}
        self._route_at = 0.0
        self._mount_at = 0.0
        self._mounts: list = []
        self._fs_at = 0.0
        self._batt_at = 0.0
        self._batt = None
        self._temp_at = 0.0
        self._temps: list[tuple[str, float]] = []

        h = history
        self.hist = {
            "cpu": deque([0.0] * 8, maxlen=h),
            "mem": deque([0.0] * 8, maxlen=h),
            "swap": deque([0.0] * 8, maxlen=h),
            "rx": deque([0.0] * 8, maxlen=h),
            "tx": deque([0.0] * 8, maxlen=h),
            "dr": deque([0.0] * 8, maxlen=h),
            "dw": deque([0.0] * 8, maxlen=h),
            "psi_cpu": deque([0.0] * 8, maxlen=h),
            "psi_io": deque([0.0] * 8, maxlen=h),
            "psi_mem": deque([0.0] * 8, maxlen=h),
        }
        self.core_hist = [deque([0.0] * 8, maxlen=256) for _ in range(self.ncores)]

        # Filled by sample()
        self.cpu = 0.0
        self.cores: list[float] = [0.0] * self.ncores
        self.cpu_mix = (0.0, 0.0, 0.0, 0.0)
        self.freq = 0.0
        self.temp = 0.0
        self.temps: list[tuple[str, float]] = []
        self.mem: dict[str, int] = read_mem()
        self.psi: dict[str, tuple[float, float]] = {}
        self.nets: list = []
        self.primary = ""
        self.rx = self.tx = 0.0
        self.rx_total = self.tx_total = 0
        self.disks: list = []
        self.dr = self.dw = 0.0
        self.fs: list = []
        self.sockets: dict[str, int] = {}
        self.load = (0.0, 0.0, 0.0, 0, 0)
        self.uptime = 0.0
        self.misc: dict[str, int] = {}
        self.ctxt_rate = 0.0
        self.fork_rate = 0.0
        self.battery = None
        self.procs: list[Proc] = []
        self.proc_count = 0
        self.thread_count = 0
        self.sample_ms = 0.0

    # -- cached, slow-changing sources -----------------------------------
    def _mountlist(self, now: float):
        # Keyed on "have we looked yet", not on the result: a box with no
        # sensors would otherwise rescan /sys on every single tick.
        if not self._mount_at or now - self._mount_at > 10.0:
            self._mount_at = now
            self._mounts = read_mounts()
        return self._mounts

    def _templist(self, now: float):
        if not self._temp_at or now - self._temp_at > 2.0:
            self._temp_at = now
            self._temps = read_temps()
        return self._temps

    def _battery(self, now: float):
        if now - self._batt_at > 15.0 or not self._batt_at:
            self._batt_at = now
            self._batt = read_battery()
        return self._batt

    # -- main --------------------------------------------------------------
    def sample(self) -> None:
        t0 = time.perf_counter()
        now = time.monotonic()
        dt = (now - self.t) if self.t else 0.0
        self.t, self.dt = now, dt

        cpus, agg, misc = read_cpu()
        prev_agg = self.cpu_agg_prev
        if self.cpu_prev and len(cpus) == len(self.cpu_prev):
            for i, (tot, busy) in enumerate(cpus):
                ptot, pbusy = self.cpu_prev[i]
                d = tot - ptot
                pct = 0.0 if d <= 0 else max(0.0, min(100.0, 100.0 * (busy - pbusy) / d))
                if i == 0:
                    self.cpu = pct
                elif i - 1 < len(self.cores):
                    self.cores[i - 1] = pct
                    self.core_hist[i - 1].append(pct)
        cpu_delta = (sum(agg) - sum(prev_agg)) if (agg and prev_agg) else 0
        if cpu_delta > 0:
            d = [a - b for a, b in zip(agg, prev_agg)]
            self.cpu_mix = (100.0 * (d[0] + d[1]) / cpu_delta, 100.0 * d[2] / cpu_delta,
                            100.0 * d[4] / cpu_delta, 100.0 * d[7] / cpu_delta)
        self.cpu_prev, self.cpu_agg_prev = cpus, agg
        self.hist["cpu"].append(self.cpu)

        self.freq = read_freq(self.ncores)
        self.temps = self._templist(now)
        self.temp = pick_cpu_temp(self.temps)

        self.mem = read_mem()
        mt = self.mem["total"] or 1
        self.hist["mem"].append(100.0 * self.mem["used"] / mt)
        st = self.mem["swap_total"]
        self.hist["swap"].append(100.0 * self.mem["swap_used"] / st if st else 0.0)

        self.psi = read_psi()
        for res, key in (("cpu", "psi_cpu"), ("io", "psi_io"), ("memory", "psi_mem")):
            self.hist[key].append(self.psi.get(res, (0.0, 0.0))[0])

        self._net(dt)
        self._disk(dt, now)
        self.sockets = read_sockets()

        self.load = read_load()
        self.uptime = read_uptime()
        if self.misc_prev and dt > 0:
            self.ctxt_rate = max(0, misc["ctxt"] - self.misc_prev["ctxt"]) / dt
            self.fork_rate = max(0, misc["forks"] - self.misc_prev["forks"]) / dt
        self.misc, self.misc_prev = misc, misc
        self.battery = self._battery(now)

        self._procs(cpu_delta, dt)
        self.sample_ms = (time.perf_counter() - t0) * 1000.0

    def _net(self, dt: float) -> None:
        cur = read_net()
        rows = []
        for name, v in cur.items():
            if name == "lo":
                continue
            p = self.net_prev.get(name)
            drx = dtx = 0.0
            if p and dt > 0:
                drx = max(0, v[0] - p[0]) / dt
                dtx = max(0, v[4] - p[4]) / dt
            rows.append({"name": name, "rx": drx, "tx": dtx, "rx_total": v[0],
                         "tx_total": v[4], "errs": v[2] + v[6], "drops": v[3] + v[7],
                         "up": iface_up(name), "speed": iface_speed(name)})
        self.net_prev = cur
        rows.sort(key=lambda r: (not r["up"], -(r["rx"] + r["tx"]),
                                 -(r["rx_total"] + r["tx_total"])))
        self.nets = rows

        if self.t - self._route_at > 5.0 or not self.primary:
            self._route_at = self.t
            self.primary = self._pick_primary(rows)
        pick = next((r for r in rows if r["name"] == self.primary), None)
        if pick is None:
            # No usable interface: fall back to the aggregate so graphs still move.
            self.rx = sum(r["rx"] for r in rows)
            self.tx = sum(r["tx"] for r in rows)
            self.rx_total = sum(r["rx_total"] for r in rows)
            self.tx_total = sum(r["tx_total"] for r in rows)
        else:
            self.rx, self.tx = pick["rx"], pick["tx"]
            self.rx_total, self.tx_total = pick["rx_total"], pick["tx_total"]
        self.hist["rx"].append(self.rx)
        self.hist["tx"].append(self.tx)

    @staticmethod
    def _pick_primary(rows) -> str:
        names = {r["name"] for r in rows}
        dev = default_route_iface()
        if dev in names:
            return dev
        real = [r for r in rows
                if r["up"] and not r["name"].startswith(VIRTUAL)]
        if real:
            return max(real, key=lambda r: r["rx_total"] + r["tx_total"])["name"]
        return rows[0]["name"] if rows else ""

    def _disk(self, dt: float, now: float) -> None:
        cur = read_diskstats()
        rows = []
        tr = tw = 0.0
        for name, v in cur.items():
            p = self.disk_prev.get(name)
            r = w = util = 0.0
            iops = 0.0
            if p and dt > 0:
                r = max(0, v[0] - p[0]) / dt
                w = max(0, v[1] - p[1]) / dt
                iops = max(0, (v[2] - p[2]) + (v[3] - p[3])) / dt
                util = min(100.0, max(0, v[4] - p[4]) / (dt * 1000.0) * 100.0)
            tr += r
            tw += w
            rows.append({"name": name, "r": r, "w": w, "util": util,
                         "iops": iops, "inflight": v[5]})
        self.disk_prev = cur
        rows.sort(key=lambda d: -(d["r"] + d["w"] + d["util"]))
        self.disks = rows
        self.dr, self.dw = tr, tw
        self.hist["dr"].append(tr)
        self.hist["dw"].append(tw)

        fs = []
        # statvfs hits the filesystem and can block outright on a stalled network
        # mount, so refresh usage on its own slow clock rather than every tick.
        if now - self._fs_at > 5.0 or not self._fs_at:
            self._fs_at = now
            for dev, mnt, kind in self._mountlist(now):
                u = read_usage(mnt)
                if u:
                    fs.append({"dev": dev, "mnt": mnt, "fs": kind,
                               "total": u[0], "used": u[1], "free": u[2],
                               "pct": 100.0 * u[1] / u[0]})
            fs.sort(key=lambda d: (d["mnt"] != "/", -d["total"]))
            self.fs = fs

    def cmdline(self, p: Proc) -> str:
        """Cached argv. A live process keeps its cmdline, so (pid, start tick) is
        a safe key — pid reuse always brings a different start tick."""
        hit = self._cmd_cache.get(p.pid)
        if hit is not None and hit[0] == p.start:
            return hit[1]
        txt = read_cmdline(p.pid) or ("[%s]" % p.name)
        self._cmd_cache[p.pid] = (p.start, txt)
        return txt

    def _procs(self, cpu_delta: int, dt: float) -> None:
        procs: list[Proc] = []
        prev = self.proc_prev
        cur: dict[int, int] = {}
        ids = self.proc_ids
        new_ids: dict[int, tuple[int, int, str]] = {}
        mem_total = self.mem["total"] or 1
        ncores = self.ncores
        uptime = self.uptime
        cpu_cap = 100.0 * ncores
        scale = (100.0 * ncores / cpu_delta) if cpu_delta > 0 else 0.0
        threads = 0
        # Raw fd reads: the io module's buffered wrapper more than doubles the
        # cost of a one-shot read, and this loop runs once per process per tick.
        _open, _rd, _close, _stat = os.open, os.read, os.close, os.stat
        rdonly = os.O_RDONLY

        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            base = "/proc/" + entry
            try:
                fd = _open(base + "/stat", rdonly)
            except OSError:
                continue
            try:
                raw = _rd(fd, 1024)
            except OSError:
                continue
            finally:
                _close(fd)
            try:
                close = raw.rindex(b")")
                # rss is field 21; stop splitting there instead of tearing the
                # whole line into ~50 objects.
                p = raw[close + 2:].split(None, 22)
                utime, stime = int(p[11]), int(p[12])
                start = int(p[19])
                pr = Proc()
                pr.ppid = int(p[1])
                pr.state = p[0].decode()
                pr.nice = int(p[16])
                pr.threads = int(p[17])
                pr.vsz = int(p[20])
                pr.rss = int(p[21]) * PAGE
                pr.name = sanitize(
                    raw[raw.index(b"(") + 1:close].decode("utf-8", "replace"))
            except (IndexError, ValueError):
                continue

            pid = int(entry)
            ident = ids.get(pid)
            if ident is not None and ident[0] == start:
                pr.uid, pr.user = ident[1], ident[2]
            else:
                # Ownership only needs a stat when the pid is new or recycled.
                try:
                    pr.uid = _stat(base).st_uid
                except OSError:
                    continue
                pr.user = _user(pr.uid)
                ident = (start, pr.uid, pr.user)
            new_ids[pid] = ident

            ticks = utime + stime
            cur[pid] = ticks
            old = prev.get(pid)
            pr.pid = pid
            pr.start = start
            pr.cpu = 0.0 if old is None else max(
                0.0, min(cpu_cap, (ticks - old) * scale))
            pr.cputime = ticks / HZ
            pr.elapsed = max(0.0, uptime - start / HZ)
            pr.mem = 100.0 * pr.rss / mem_total
            threads += pr.threads
            procs.append(pr)

        self.proc_prev = cur
        self.proc_ids = new_ids
        if len(self._cmd_cache) > len(cur):
            self._cmd_cache = {k: v for k, v in self._cmd_cache.items() if k in cur}
        self.procs = procs
        self.proc_count = len(procs)
        self.thread_count = threads

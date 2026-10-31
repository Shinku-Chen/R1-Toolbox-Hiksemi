"""Production temperature sources/monitor/history/HTTP with temporary sensors.

Only stdin advances the source or samples MonitoringService. HTTP is the real
WebConfigServer/FnmonitorPanel and must never read a sensor or start collection.
Every kernel-looking path is below the private temporary fixture directory.
"""
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time

repo, fixture = (Path(value).resolve() for value in sys.argv[1:3])
assert fixture.name.startswith("r1-temperature-curves-") and fixture.is_dir()
sys.path.insert(0, str(repo / "runtime"))
from device_info import _Reader, _sensors
from fnm_compat import FnmonitorPanel
from monitoring import MonitoringService
from temperature_sources import TemperatureSources
from web_config import WebConfigServer

forbidden = []
io_counts = {"sensor_opens": 0}


def reject_io(*args, **kwargs):
    forbidden.append("hardware_or_collection_io")
    raise AssertionError("temperature fixture forbids commands/statvfs/outbound I/O")


subprocess.Popen = subprocess.run = reject_io
socket.socket.connect = socket.create_connection = reject_io
if hasattr(os, "statvfs"):
    os.statvfs = reject_io
if hasattr(os, "fstatvfs"):
    os.fstatvfs = reject_io
if not hasattr(os, "O_NONBLOCK"):
    os.O_NONBLOCK = 0  # Only bounded regular TEMP files are opened on Windows.


def audit_io(event, args):
    if event != "open" or not args or not isinstance(args[0], (str, bytes)):
        return
    target = Path(os.fsdecode(args[0])).absolute()
    sensor = (target.name == "temp" or re.fullmatch(r"temp\d+_(?:input|max|crit)", target.name)
              or re.fullmatch(r"trip_point_\d+_(?:temp|type)", target.name))
    if not sensor or not any(part in ("hwmon", "thermal") for part in target.parts):
        return
    if fixture not in target.parents:
        forbidden.append("outside_sensor_open")
        raise AssertionError("sensor files must stay below the TEMP fixture")
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    if (isinstance(mode, str) and not any(char in mode for char in ("r", "+"))
            or mode is None and isinstance(flags, int)
            and flags & (os.O_WRONLY | os.O_RDWR) == os.O_WRONLY):
        return  # Stdin fixture setup writes are not production sensor reads.
    if "hwmon9" in target.parts:
        forbidden.append("disk_sensor_open")
        raise AssertionError("disk sensor attributes must not be read")
    io_counts["sensor_opens"] += 1


sys.addaudithook(audit_io)


class Controller:
    state_dir = fixture / "data"

    def snapshot(self):
        return {"settings": {}, "capabilities": {}, "screen": {}, "rgb": {}}


class Insights:
    def __init__(self, state):
        self.state = state

    def snapshot(self):
        return {"sampled_at": self.state.iso(), "system": {"memory": {}},
                "interfaces": [], "disks": {"items": []},
                "device": copy.deepcopy(self.state.device)}

    refresh_smart = refresh_storage = refresh_visible_storage = reject_io


class Basic:
    def __init__(self, state):
        self.state = state

    def snapshot(self):
        # Deliberately different from the real canonical CPU measurement: a
        # successful page test must not silently fall back to this legacy input.
        return {"sampled_at": self.state.iso(), "cpu": {"temp_c": 99},
                "memory": {}, "disks": [], "network": {}}

    refresh_capacity = fill_storage = reject_io


class Source(TemperatureSources):
    def __init__(self, state):
        self.state = state
        super().__init__(None, sys_root=state.folder / "sys", clock=state.clock)

    def _text(self, target):
        resolved = Path(target).absolute()
        assert self.sys_root == resolved or self.sys_root in resolved.parents
        if resolved.name.endswith("_input") or resolved.name == "temp":
            self.state.sensor_reads += 1
            if any(part == "hwmon9" for part in resolved.parts):
                forbidden.append("disk_temperature_read")
                raise AssertionError("disk temperature attribute must not be opened")
        return super()._text(target)

    def snapshot(self):
        if self.state.source_mode in ("stale", "future", "missing"):
            rows = copy.deepcopy(self.state.saved_sources)
            for row in rows:
                if self.state.source_mode == "future":
                    row["sampled_monotonic"] = self.state.mono + 60
                elif self.state.source_mode == "missing":
                    row.update(status="unavailable", temperature_c=None)
            return rows
        rows = super().snapshot()
        self.state.saved_sources = copy.deepcopy(rows)
        return rows


class MonitorProxy:
    def __init__(self, state):
        self.state = state

    def snapshot(self):
        return self.state.monitor.snapshot()

    def history(self, *args, **kwargs):
        return self.state.monitor.history(*args, **kwargs)

    def traffic(self, *args, **kwargs):
        return self.state.monitor.traffic(*args, **kwargs)

    def history_diag(self):
        return {"available": True, "db_exists": self.state.monitor.db is not None,
                "recording": self.state.monitor.settings["history_enabled"],
                "reason": "controlled TEMP SQLite history"}


class State:
    CASES = ("board", "no_board", "cpu_only", "history_empty", "history_off",
             "stale", "future", "cache_expired", "zero", "negative", "cache_hit",
             "slow_device", "wall_jump")

    def __init__(self):
        self.serial, self.sensor_reads = 0, 0
        self.wall, self.mono = time.time() - 150, 1000.0
        self.insights, self.basic = Insights(self), Basic(self)
        self.monitor = None
        self.configure("board")

    def clock(self):
        # Each real caller observes a strictly later clock. A sampler that
        # captures now before Source.snapshot incorrectly rejects every new read.
        self.mono += 0.001
        return self.mono

    def iso(self):
        return datetime.datetime.fromtimestamp(self.wall, datetime.timezone.utc).isoformat()

    def write(self, relative, value):
        target = self.folder / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(str(value), encoding="ascii")

    def configure(self, case):
        assert case in self.CASES
        if self.monitor is not None:
            self.monitor.stop()
        self.case, self.serial = case, self.serial + 1
        self.folder = fixture / ("case_" + str(self.serial) + "_" + case)
        self.folder.mkdir(mode=0o700)
        for name in ("sys/block", "sys/class/hwmon", "sys/class/thermal", "proc/self", "dev", "run/udev", "data"):
            (self.folder / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.write("proc/self/mountinfo", "")
        self.write("proc/sys/kernel/random/boot_id", "12345678-1234-1234-1234-123456789abc")
        self.write("proc/diskstats", "")
        # Case-local producers restart while the long-lived HTTP adapter keeps
        # its clock high-water mark. Real monotonic time never resets with a
        # collector, so keep this fixture's clock increasing across cases too.
        self.wall, self.mono = time.time() - 150, self.mono + 1000.0
        self.sensor_reads, self.source_mode, self.saved_sources = 0, "normal", []
        self.wall_anchor_before = None
        self.cpu = 0 if case == "zero" else -5 if case == "negative" else 47
        self.board = 0 if case == "zero" else -3 if case == "negative" else 33
        self.write("sys/class/hwmon/hwmon0/name", "coretemp")
        self.write("sys/class/hwmon/hwmon0/temp1_label", "Package id 0")
        self.write("sys/class/hwmon/hwmon0/temp1_input", self.cpu * 1000)
        if case != "cpu_only":
            # These valid readings must remain individually visible but must
            # never be promoted to a physical motherboard measurement.
            for index, (driver, value) in enumerate((("acpitz", 27800), ("INT3400", 20000), ("pch_cannonlake", 88000))):
                self.write("sys/class/thermal/thermal_zone" + str(index) + "/type", driver)
                self.write("sys/class/thermal/thermal_zone" + str(index) + "/temp", value)
            self.write("sys/class/thermal/thermal_zone3/type", "x86_pkg_temp")
            self.write("sys/class/thermal/thermal_zone3/temp", self.cpu * 1000)
            self.write("sys/class/hwmon/hwmon9/name", "drivetemp")
            self.write("sys/class/hwmon/hwmon9/temp1_input", 125000)
        self.has_board = case not in ("no_board", "cpu_only")
        if self.has_board:
            self.write("sys/class/hwmon/hwmon1/name", "nct6775")
            for index, (label, value) in enumerate((("SYSTIN", self.board), ("CPUTIN", 90), ("AUXTIN", 91)), 1):
                self.write("sys/class/hwmon/hwmon1/temp" + str(index) + "_label", label)
                self.write("sys/class/hwmon/hwmon1/temp" + str(index) + "_input", value * 1000)
        self.sources = Source(self)
        self.monitor = MonitoringService(self.folder / "data", self.insights, self.basic, self.sources,
            proc_root=self.folder / "proc", sys_root=self.folder / "sys", clock=self.clock,
            wall_clock=lambda: self.wall)
        if os.name == "nt":
            # Windows does not represent POSIX directory write bits. This seam
            # only replaces that unrelated directory-mode check; real SQLite
            # open/insert/history aggregation below remain production methods.
            self.monitor._prepare_directory = lambda: (self.folder / "data").mkdir(mode=0o700, exist_ok=True)
        self.monitor.settings["history_enabled"] = case != "history_off"
        self.sample()
        if case not in ("history_off", "history_empty"):
            # Span a complete 24h aggregation bucket (120 seconds) so at least
            # two genuine historical points exist regardless of wall alignment.
            for _ in range(4):
                self.advance()
        if case == "cache_hit":
            before = self.sensor_reads
            self.sample(advance=False)
            self.cache_read_count = self.sensor_reads - before
        else:
            self.cache_read_count = None
        if case in ("stale", "future"):
            self.source_mode = case
            if case == "stale":
                self.mono += 4
                self.wall += 4
            self.sample(advance=False)
        if case == "cache_expired":
            # The worker stops after a valid observation. Passive GET must
            # expire it without sampling again or changing the original date.
            self.wall += 4
            self.mono += 4
        if case == "slow_device":
            self.cpu, self.board = 55, 42
            self.write("sys/class/hwmon/hwmon0/temp1_input", 55000)
            self.write("sys/class/thermal/thermal_zone3/temp", 55000)
            self.write("sys/class/hwmon/hwmon1/temp1_input", 42000)
            self.wall += 4
            self.mono += 4
            self.sample(update_inventory=False)
        if case == "wall_jump":
            self.wall_anchor_before = copy.deepcopy(self.monitor.snapshot()["current"])
            self.wall += 60
            # The real one-second source cache is hit: its sample monotonic
            # time stays unchanged while the consuming wall clock jumps.
            self.sample(update_inventory=False)
        if case == "history_empty":
            self.monitor.history_cache = {span: self.monitor._empty_history(span, "controlled empty history")
                                          for span in self.monitor.history_cache}

    def inventory(self):
        # The genuine device reader sees only this private TEMP sysfs tree.
        # Keep this slow inventory intentionally distinct from canonical sources.
        reader = _Reader((self.folder / "sys",))
        thermal, fans = _sensors(reader, self.folder / "sys")
        self.device = {"identity": {"hostname": "Temperature-Fixture"}, "storage": {},
                       "sampled_at": self.iso(), "thermal": thermal, "fans": fans}

    def sample(self, advance=False, update_inventory=True):
        if advance:
            self.wall += 30
            self.mono += 30
        if update_inventory:
            self.inventory()
        self.monitor.next_flush = 0
        self.monitor.sample_once()

    def advance(self):
        self.sample(advance=True)

    def transition(self, mode):
        assert mode in ("missing", "restore")
        self.source_mode = "missing" if mode == "missing" else "normal"
        if mode == "restore":
            self.cpu, self.board = 51, 35
            self.write("sys/class/hwmon/hwmon0/temp1_input", 51000)
            self.write("sys/class/thermal/thermal_zone3/temp", 51000)
            self.write("sys/class/hwmon/hwmon1/temp1_input", 35000)
        self.advance()

    def stats(self):
        return {"case": self.case, "wall": self.wall, "mono": self.mono,
                "sensor_reads": self.sensor_reads, "sensor_opens": io_counts["sensor_opens"],
                "cache_read_count": self.cache_read_count,
                "saved_sources": self.saved_sources, "current": self.monitor.snapshot()["current"],
                "history": self.monitor.history("24h"), "expected_cpu": self.cpu,
                "expected_board": self.board if self.has_board else None,
                "wall_anchor_before": self.wall_anchor_before,
                "device_sampled_at": self.device["sampled_at"],
                "forbidden": forbidden}


state = State()
os.environ["R1_WEB_AUTH_MODE"] = "password"
os.environ["R1_DEV_NO_PAIRING"] = "0"
controller = Controller()
controller.state_dir.mkdir(mode=0o700, exist_ok=True)
proxy = MonitorProxy(state)
# Instrument a TEMP copy of the built document, never its business responses or
# renderer. The only added statement records the original setData arguments;
# all production parsing, merging, line rendering and SVG stay unchanged.
assets = fixture / "assets"
assets.mkdir(mode=0o700)
built = (repo / "ui/web/index.html").read_bytes().decode("utf-8")
needle = "setData(cols, span) {"
assert built.count(needle) == 1
probe = ("    /* TEMP_READ_ONLY_TEMPERATURE_INPUT */\n"
         "    if (Array.isArray(window.__r1TemperatureCurveInputs) && this.opt.series.length === 2"
         " && this.opt.series[0].m === 'temp' && this.opt.series[1].m === 'temp_mb')"
         " window.__r1TemperatureCurveInputs.push(JSON.parse(JSON.stringify({ref:this.host.dataset.ref,"
         "sheet:!!this.host.closest('#sheetBody'),labels:this.opt.series.map(s=>s.label),cols,span})));\n")
observed = built.replace(needle, needle + "\n" + probe)
assert observed.replace(needle + "\n" + probe, needle) == built
(assets / "index.html").write_bytes(observed.encode("utf-8"))
(assets / "login.html").write_bytes((repo / "ui/web/login.html").read_bytes())
ui_evidence = {"source_sha256": hashlib.sha256(built.encode()).hexdigest(),
               "observed_sha256": hashlib.sha256(observed.encode()).hexdigest(),
               "only_change": "one TEMP read-only setData input observer", "replacement_count": 1}
server = WebConfigServer(controller, insights=state.insights, monitor=state.basic,
                         monitoring=proxy, host="127.0.0.1", port=0, asset_dir=assets)
server.panel = FnmonitorPanel(controller, state.insights, proxy, None, None, state.basic,
    state_dir=server.state_dir, web=server, proc_root=state.folder / "proc",
    sys_root=state.folder / "sys", dev_root=state.folder / "dev",
    udev_root=state.folder / "run/udev", clock=state.clock, wall_clock=lambda: state.wall,
    weather_fetcher=lambda city: {"ok": False, "error": "fixture_weather_unavailable"})
try:
    for attempt in range(20):
        server.port = 20000 + secrets.randbelow(40000)
        try:
            server.start()
            break
        except OSError:
            if server.server is not None or attempt == 19:
                raise
    print(json.dumps({"ready": True, "owner": "http://127.0.0.1:" + str(server.port),
                      "ui_evidence": ui_evidence}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        operation = request["operation"]
        if operation.startswith("case:"):
            state.configure(operation.split(":", 1)[1])
        elif operation == "advance":
            state.advance()
        elif operation in ("missing", "restore"):
            state.transition(operation)
        elif operation == "expire_only":
            state.wall += 4
            state.mono += 4
        elif operation != "stats":
            raise ValueError("unknown fixed fixture operation")
        print(json.dumps({"id": request["id"], "state": state.stats(), "forbidden": forbidden}), flush=True)
finally:
    state.monitor.stop()
    server.stop()

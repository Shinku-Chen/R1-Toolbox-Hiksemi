"""Real network producer/monitor/panel fixture; all kernel inputs are temporary.

The browser receives production HTTP projections, never fulfilled JSON routes.
Only socket accepts are permitted: subprocesses, statvfs and outbound connects
are traps so a fixture regression cannot probe hardware or an external service.
"""
import copy
import datetime
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

repo, fixture = (Path(value).resolve() for value in sys.argv[1:3])
assert fixture.name.startswith("r1-network-curves-") and fixture.is_dir()
sys.path.insert(0, str(repo / "runtime"))
from fnm_compat import FnmonitorPanel
from insights import InsightsCollector
from monitoring import MonitoringService
from web_config import WebConfigServer
import ui_service

if not hasattr(os, "O_NONBLOCK"):
    os.O_NONBLOCK = 0  # Regular fixture files do not need the Linux-only flag.

forbidden = []


def reject_io(*args, **kwargs):
    forbidden.append("hardware_or_outbound_io")
    raise AssertionError("network fixture forbids subprocess/statvfs/outbound I/O")


subprocess.Popen = subprocess.run = reject_io
socket.socket.connect = socket.create_connection = reject_io
if hasattr(os, "statvfs"):
    os.statvfs = reject_io
ui_service.interface_ipv4 = lambda name: "192.0.2.2" if name else None


class Controller:
    state_dir = fixture / "data"

    def snapshot(self):
        return {"settings": {}, "capabilities": {}, "screen": {}, "rgb": {}}


class Sources:
    def snapshot(self):
        return []


class Insights:
    """Publish only rows obtained from the real InsightsCollector below."""

    def __init__(self, state):
        self.state = state

    def snapshot(self):
        return {
            "sampled_at": datetime.datetime.fromtimestamp(
                int(self.state.wall), datetime.timezone.utc).isoformat(),
            "interfaces": copy.deepcopy(self.state.rows), "disks": {"items": []},
            "system": {"model": "HIKSEMI R1", "memory": {}},
            "device": {"identity": {"hostname": "Network-Fixture"}, "storage": {},
                       "network": [{"name": row["name"], "speed_mbps": 1000}
                                   for row in self.state.rows]},
        }


class Basic:
    def __init__(self, state):
        self.state = state

    def snapshot(self):
        return {**copy.deepcopy(self.state.basic), "disks": [],
                "sampled_at": datetime.datetime.fromtimestamp(
                    int(self.state.wall), datetime.timezone.utc).isoformat()}


class MonitorProxy:
    def __init__(self, state):
        self.state = state

    def snapshot(self):
        return self.state.monitoring.snapshot()

    def history(self, *args, **kwargs):
        return self.state.monitoring.history(*args, **kwargs)

    def traffic(self, *args, **kwargs):
        return self.state.monitoring.traffic(*args, **kwargs)

    def history_diag(self):
        return {"available": False, "reason": "history disabled in network fixture"}


class State:
    CASES = ("numeric", "control_file", "one_unknown", "bridge_filtered",
             "bridge_included", "bond_filtered", "bond_included", "first",
             "restart", "rollback", "misaligned", "only_virtual",
             "only_virtual_filtered", "mixed_rx_unknown", "mixed_tx_unknown",
             "only_virtual_mixed", "zero", "only_virtual_zero")

    def __init__(self):
        self.serial = 0
        self.wall, self.mono = time.time(), 1000.0
        self.insights, self.basic_view = Insights(self), Basic(self)
        self.configure("numeric")

    def write(self, relative, value):
        target = self.folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(value), encoding="ascii")

    def new_collector(self):
        # Construct only the network collector seam, not the disk background loop.
        collector = object.__new__(InsightsCollector)
        collector.sys_root = self.folder / "sys"
        collector._previous_network = {}
        collector.ipv4_provider = lambda: {name: ["192.0.2.2"] for name in self.names}
        return collector

    def configure(self, case):
        assert case in self.CASES
        self.case, self.serial = case, self.serial + 1
        self.folder = fixture / ("case_" + str(self.serial) + "_" + case)
        for name in ("proc/self", "sys", "dev", "run/udev", "data"):
            (self.folder / name).mkdir(parents=True, exist_ok=True)
        self.names = ["wg0"] if case.startswith("only_virtual") else ["eth0"]
        if case == "one_unknown":
            self.names.append("eth1")
        if case.startswith("bridge_"):
            self.names.append("br0")
        if case.startswith("bond_"):
            self.names.append("bond0")
        self.write("proc/sys/kernel/random/boot_id", "12345678-1234-1234-1234-123456789abc")
        self.write("proc/self/mountinfo", "")
        self.write("proc/net/route", "Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
                   + self.names[0] + " 00000000 00000000 0001 0 0 1 00000000\n")
        self.write("proc/meminfo", "MemTotal: 4096000 kB\nMemAvailable: 2048000 kB\n")
        self.write("proc/stat", "cpu  10 0 10 80 0 0 0 0\n")
        self.write("proc/uptime", "10 10\n")
        self.write("proc/diskstats", "")
        self.counts = {name: [10000000, 5000000] for name in self.names}
        self.rates = {name: [900000, 300000] for name in self.names}
        if case in ("zero", "only_virtual_zero"):
            self.rates[self.names[0]] = [0, 0]
        if case in ("control_file", "one_unknown", "misaligned"):
            self.rates[self.names[0]] = [1250000, 480000]
        for index, name in enumerate(self.names):
            self.write("sys/class/net/" + name + "/operstate", "up")
            self.write("sys/class/net/" + name + "/ifindex", index + 1)
            self.write("sys/class/net/" + name + "/iflink", index + 1)
            self.write("sys/class/net/" + name + "/address", "02:00:00:00:00:0" + str(index + 1))
        if case.startswith("bridge_"):
            (self.folder / "sys/class/net/br0/bridge").mkdir()
        if case.startswith("bond_"):
            (self.folder / "sys/class/net/bond0/bonding").mkdir()
        if case == "control_file":
            # Linux publishes this ordinary sysfs control file alongside links
            # to interfaces. It has no interface counters and must not poison
            # the canonical rate; an actual unknown eth1 still must do so.
            self.write("sys/class/net/bonding_masters", "bond0\n")
        self.collector = self.new_collector()
        self.sampler = ui_service.StatusSampler(
            proc_root=self.folder / "proc", sys_root=self.folder / "sys", clock=lambda: self.mono)
        self.rows, self.basic = [], {}
        self.monitoring = MonitoringService(
            self.folder / "data", self.insights, self.basic_view, Sources(),
            proc_root=self.folder / "proc", sys_root=self.folder / "sys",
            clock=lambda: self.mono, wall_clock=lambda: self.wall)
        self.monitoring.settings["history_enabled"] = False
        self.monitoring.settings["traffic_exclude_virtual"] = case not in (
            "bridge_included", "bond_included", "only_virtual", "only_virtual_mixed", "only_virtual_zero")
        self.sample()
        self.first_rows = copy.deepcopy(self.rows)
        self.first_basic = copy.deepcopy(self.basic["network"])
        if case != "first":
            self.advance()
        if case == "restart":
            self.collector = self.new_collector()
            self.sampler.previous_network = None
            self.wall += 5
            self.mono += 5
            self.sample()
        if case == "rollback":
            self.counts = {name: [1, 1] for name in self.names}
            self.wall += 5
            self.mono += 5
            self.sample()
        if case == "misaligned":
            self.rates[self.names[0]] = [900000, 300000]
            self.advance()
            self.rates[self.names[0]] = [1250000, 480000]
            self.advance(sample_monitor=False)

    def sample(self, sample_monitor=True):
        for name, values in self.counts.items():
            if self.case == "one_unknown" and name == "eth1":
                continue
            for field, value in zip(("rx_bytes", "tx_bytes"), values):
                self.write("sys/class/net/" + name + "/statistics/" + field, value)
        self.rows = self.collector._interfaces(self.mono)
        # Monitoring's published contract is directional. Keep the real counter
        # sample for the other direction while modelling one unavailable source.
        # Do not fabricate the aggregate response or fulfill a business route.
        if self.case in ("mixed_rx_unknown", "only_virtual_mixed"):
            self.rows[0]["rx_bytes_per_sec"] = None
        elif self.case == "mixed_tx_unknown":
            self.rows[0]["tx_bytes_per_sec"] = None
        self.basic = self.sampler.sample()
        if sample_monitor:
            self.monitoring.sample_once()

    def advance(self, sample_monitor=True):
        self.wall += 5
        self.mono += 5
        for name, rates in self.rates.items():
            self.counts[name] = [old + int(rate * 5) for old, rate in zip(self.counts[name], rates)]
        self.sample(sample_monitor)

    def stats(self):
        return {"case": self.case, "wall": self.wall, "rows": self.rows,
                "first_rows": self.first_rows, "primary": self.basic["network"],
                "first_primary": self.first_basic, "monitoring": self.monitoring.snapshot(),
                "forbidden": forbidden}


state = State()
os.environ["R1_WEB_AUTH_MODE"] = "password"
os.environ["R1_DEV_NO_PAIRING"] = "0"
controller = Controller()
controller.state_dir.mkdir(parents=True, exist_ok=True)
server = WebConfigServer(
    controller, insights=state.insights, monitor=state.basic_view,
    monitoring=MonitorProxy(state), host="127.0.0.1", port=0, asset_dir=repo / "ui/web")
server.panel = FnmonitorPanel(
    controller, state.insights, MonitorProxy(state), None, None, state.basic_view,
    state_dir=server.state_dir, web=server, proc_root=state.folder / "proc",
    sys_root=state.folder / "sys", dev_root=state.folder / "dev",
    udev_root=state.folder / "run/udev", clock=lambda: state.mono,
    wall_clock=lambda: state.wall,
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
    print(json.dumps({"ready": True, "owner": "http://127.0.0.1:" + str(server.port)}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        operation = request["operation"]
        if operation.startswith("case:"):
            state.configure(operation.split(":", 1)[1])
        elif operation == "advance":
            state.advance()
        elif operation in ("rx_unknown", "rx_restore"):
            # Preserve this interface inventory and page DOM while the real
            # monitor publishes a missing direction, then a recovered sample.
            assert state.names == ["eth0"]
            state.case = "mixed_rx_unknown" if operation == "rx_unknown" else "numeric"
            state.advance()
        elif operation != "stats":
            raise ValueError("unknown fixed fixture operation")
        print(json.dumps({"id": request["id"], "state": state.stats(),
                          "forbidden": forbidden}), flush=True)
finally:
    server.stop()

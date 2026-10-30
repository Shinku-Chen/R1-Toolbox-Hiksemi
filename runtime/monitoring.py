"""Local monitoring, bounded history and reports, independently implemented.

All hardware inputs are existing read-only snapshots, proc counters or RAPL
sysfs counters. Nothing starts SMART self-tests, wakes disks, sends notifications
or estimates whole-machine power. Public read APIs return cached memory only.
"""
import copy
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
try:
    import sqlite3
except ImportError:  # Some official firmware Python builds omit _sqlite3.
    sqlite3 = None
import stat
import tempfile
import threading
import time
from collections import deque

RANGES = {"10m": 600, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}
DEFAULTS = {"history_enabled": True, "retention_days": 30, "traffic_exclude_virtual": True,
            "thresholds": {"cpu_warn": 85, "cpu_critical": 95, "memory_warn": 85, "memory_critical": 95,
                "cpu_temp_warn": 70, "cpu_temp_critical": 80, "disk_temp_warn": 50, "disk_temp_critical": 60,
                "capacity_warn": 85, "capacity_critical": 95}}
META = {
    "cpu_usage_percent": ("CPU 使用率", "%"), "memory_usage_percent": ("内存使用率", "%"),
    "rx_bytes_per_sec": ("已计网口接收速率", "B/s"), "tx_bytes_per_sec": ("已计网口发送速率", "B/s"),
    "cpu_temperature_c": ("CPU 最高温度", "°C"), "disk_temperature_c": ("硬盘最高温度", "°C"),
    "cpu_package_w": ("CPU 封装功耗 · RAPL", "W"),
    "disk_read_bytes_per_sec": ("物理磁盘读取速率", "B/s"),
    "disk_write_bytes_per_sec": ("物理磁盘写入速率", "B/s"),
    "fan_cpu_target_percent": ("CPU 风扇请求输出", "%"),
    "fan_system_target_percent": ("系统风扇请求输出", "%"),
}
VIRTUAL = re.compile(r"^(?:lo$|docker|veth|br-|br[0-9]+$|virbr|bond[0-9]+$|team[0-9]+$|tun|tap|vnet|lxc|kube|wg|vxlan)")
IFACE = re.compile(r"[A-Za-z0-9_.:@-]{1,64}")
DISK = re.compile(r"(?:sd[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+|vd[a-z]+|xvd[a-z]+)")
MAX_SAMPLES, MAX_METRICS, MAX_EVENTS, MAX_SMART = 86400, 64, 2000, 1000
SQLiteError = sqlite3.Error if sqlite3 is not None else type("SQLiteUnavailable", (Exception,), {})


def number(value, low=None, high=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if low is not None and value < low or high is not None and value > high:
        return None
    return value


def label(value, limit=120):
    return "".join(char for char in str(value) if ord(char) >= 32)[:limit]


def fresh(snapshot, stamp, maximum):
    """A cached sample's timestamp is not refreshed by reading it again."""
    if not isinstance(snapshot, dict):
        return False
    try:
        raw = snapshot.get("sampled_at") or snapshot.get("timestamp")
        sampled = dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        return 0 <= stamp - sampled <= maximum
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False


def validate_settings(changes, previous):
    if not isinstance(changes, dict) or not changes or set(changes) - DEFAULTS.keys():
        raise ValueError("监控设置包含不支持的字段")
    result = copy.deepcopy(previous)
    for key, value in changes.items():
        if key == "thresholds":
            if not isinstance(value, dict) or not value or set(value) - DEFAULTS["thresholds"].keys():
                raise ValueError("监控阈值字段无效")
            result[key].update(value)
        else:
            result[key] = value
    for key in ("history_enabled", "traffic_exclude_virtual"):
        if type(result[key]) is not bool:
            raise ValueError("监控开关必须为布尔值")
    if type(result["retention_days"]) is not int or not 1 <= result["retention_days"] <= 30:
        raise ValueError("历史保留时间必须为 1–30 天")
    for prefix, minimum, maximum in (("cpu", 1, 100), ("memory", 1, 100), ("capacity", 1, 100),
                                      ("cpu_temp", 10, 110), ("disk_temp", 10, 90)):
        warn, critical = (result["thresholds"][prefix + suffix] for suffix in ("_warn", "_critical"))
        if type(warn) is not int or type(critical) is not int or not minimum <= warn < critical <= maximum:
            raise ValueError("警告阈值必须小于严重阈值且在允许范围内")
    return result


class MonitoringService:
    def __init__(self, state_dir, insights, monitor, sources, *, fan_policy=None,
                 proc_root="/proc", sys_root="/sys", clock=None, wall_clock=None, timezone=None):
        self.state_dir, self.proc, self.sys = Path(state_dir), Path(proc_root), Path(sys_root)
        self.insights, self.monitor, self.sources, self.fan_policy = insights, monitor, sources, fan_policy
        self.clock, self.wall_clock, self.timezone = clock or time.monotonic, wall_clock or time.time, timezone
        self.lock, self.worker_lock = threading.RLock(), threading.RLock()
        self.stopped, self.thread, self.db = threading.Event(), None, None
        self.settings, self.settings_error = copy.deepcopy(DEFAULTS), None
        self.db_reason, self.last_write = "历史服务正在准备", None
        self.history_cache = {key: self._empty_history(key, "历史服务正在准备") for key in RANGES}
        self.current = {"metrics": [], "power": {"available": False, "cpu_package_w": None, "reason": "等待两次采样"}, "disk_io": []}
        self.summary, self.smart = {}, []
        self.active, self.events, self.observations = {}, deque(maxlen=MAX_EVENTS), deque(maxlen=MAX_SMART)
        self.pending_events, self.pending_smart = deque(maxlen=200), deque(maxlen=200)
        self.baselines, self.traffic_totals, self.pending_traffic = {}, {}, {}
        self.last_smart, self.previous_power, self.previous_disk = {}, {}, {}
        self.next_flush, self.next_db_retry = 0, 0
        self.traffic_loaded, self.traffic_partial = False, True
        self.last_wall, self.last_metrics, self.last_report = None, {}, {}
        self.traffic_cache = {"available": False, "periods": {}, "interfaces": [], "totals": {"today": None, "month": None},
                              "coverage": "observed_counter_deltas", "partial": True, "reason": "等待真实计数差分"}
        self.alerts_cache = {"active": [], "events": [], "smart_observations": []}
        self._load_settings()
        if sqlite3 is None:
            self.db_reason = "宿主 Python 未提供 sqlite3；实时监控可用，持久历史不可用"

    def _read(self, path, maximum=65536, root=None):
        descriptor = None
        try:
            resolved = Path(path).resolve()
            if root is not None:
                base = Path(root).resolve()
                if resolved != base and base not in resolved.parents:
                    return None
            descriptor = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                return None
            raw = os.read(descriptor, maximum + 1)
            return raw.decode("utf-8", "replace") if len(raw) <= maximum else None
        except (OSError, ValueError, RuntimeError):
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _load_settings(self):
        path = self.state_dir / "monitoring.json"
        if not path.exists() and not path.is_symlink():
            return
        try:
            if path.is_symlink() or self.state_dir.is_symlink():
                raise ValueError("监控配置路径无效")
            raw = self._read(path, 16384, self.state_dir)
            document = json.loads(raw) if raw is not None else None
            if not isinstance(document, dict) or set(document) != {"version", "settings"} or document["version"] != 1:
                raise ValueError("监控配置格式无效")
            self.settings = validate_settings(document["settings"], DEFAULTS)
        except (OSError, ValueError, TypeError, RecursionError):
            self.settings_error = "监控设置未能加载，正在使用默认值"

    def _prepare_directory(self):
        if self.state_dir.is_symlink():
            raise ValueError("监控数据目录不能是符号链接")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.stat().st_mode & 0o022:
            raise ValueError("监控数据目录不能允许其他用户写入")

    def action(self, name, payload):
        if name != "monitor.settings.update" or not isinstance(payload, dict) or set(payload) != {"settings"}:
            return {"ok": False, "settings_saved": False, "error": "不支持的监控操作"}
        temporary = None
        try:
            with self.worker_lock:
                updated = validate_settings(payload["settings"], self.settings)
                self._prepare_directory()
                target = self.state_dir / "monitoring.json"
                if target.is_symlink():
                    raise ValueError("监控配置文件不能是符号链接")
                with tempfile.NamedTemporaryFile(dir=self.state_dir, prefix=".monitoring-", delete=False) as stream:
                    stream.write((json.dumps({"version": 1, "settings": updated}, sort_keys=True) + "\n").encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                    temporary = Path(stream.name)
                temporary.replace(target)
                descriptor = os.open(self.state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                with self.lock:
                    self.settings, self.settings_error = updated, None
                    if not updated["history_enabled"]:
                        self.history_cache = {key: self._empty_history(key, "历史记录已关闭") for key in RANGES}
                    self.next_flush = 0
            return {"ok": True, "settings_saved": True, "state": self.snapshot()}
        except (OSError, ValueError, TypeError, SQLiteError):
            return {"ok": False, "settings_saved": False, "error": "监控设置未保存，请检查字段或数据卷"}
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _open_db(self):
        if self.db is not None:
            return
        if sqlite3 is None:
            raise SQLiteError("sqlite3 unavailable")
        self._prepare_directory()
        path = self.state_dir / "monitoring.sqlite3"
        for candidate in (path, Path(str(path) + "-journal")):
            if candidate.is_symlink() or candidate.exists() and not candidate.is_file():
                raise ValueError("监控历史文件不是普通文件")
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(descriptor)
        # Only worker_lock owners use this connection; public APIs never do.
        connection = sqlite3.connect(str(path), timeout=0.2, check_same_thread=False)
        try:
            if hasattr(connection, "setlimit"):
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
            connection.execute("PRAGMA page_size=4096")
            connection.execute("PRAGMA max_page_count=32768")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY, metrics TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS traffic(kind TEXT, period TEXT, iface TEXT, rx INTEGER, tx INTEGER,
                    PRIMARY KEY(kind,period,iface));
                CREATE TABLE IF NOT EXISTS baselines(iface TEXT PRIMARY KEY, boot TEXT, ts INTEGER, rx INTEGER, tx INTEGER);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts INTEGER, body TEXT);
                CREATE TABLE IF NOT EXISTS smart(id INTEGER PRIMARY KEY, ts INTEGER, body TEXT);
            """)
            self.db = connection
            if not self.traffic_loaded:
                for kind, period, iface, rx, tx in connection.execute("SELECT kind,period,iface,rx,tx FROM traffic LIMIT 4096"):
                    if kind in ("day", "month") and IFACE.fullmatch(iface or "") and all(type(n) is int and 0 <= n <= 2**63 - 1 for n in (rx, tx)):
                        self.traffic_totals[(kind, period, iface)] = [rx, tx]
                for iface, boot, stamp, rx, tx in connection.execute("SELECT iface,boot,ts,rx,tx FROM baselines LIMIT 128"):
                    if IFACE.fullmatch(iface or "") and all(type(value) is int and value >= 0 for value in (stamp, rx, tx)):
                        self.baselines[iface] = (boot, stamp, rx, tx)
                for table, destination in (("events", self.events), ("smart", self.observations)):
                    for body, in connection.execute("SELECT body FROM " + table + " ORDER BY id DESC LIMIT 100"):
                        try:
                            item = json.loads(body) if isinstance(body, str) and len(body) <= 16384 else None
                            if isinstance(item, dict):
                                destination.appendleft(item)
                        except (ValueError, TypeError):
                            pass
                self.traffic_loaded = True
        except BaseException:
            connection.close()
            self.db = None
            raise

    def _periods(self, stamp):
        date = dt.datetime.fromtimestamp(stamp, self.timezone).astimezone(self.timezone)
        return date.strftime("%Y-%m-%d"), date.strftime("%Y-%m")

    def _virtual_interface(self, name):
        """Avoid adding bridge/bond/VLAN counters to their lower interfaces."""
        if VIRTUAL.match(name):
            return True
        path = self.sys / "class/net" / name
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(self.sys.resolve())
            if "devices/virtual/net/" in relative.as_posix() + "/":
                return True
            # These are kernel topology entries, never connection profiles.
            if (resolved / "bridge").is_dir() or (resolved / "bonding").is_dir():
                return True
        except (OSError, ValueError, RuntimeError):
            return False
        try:
            index = int(self._read(path / "ifindex", 32, self.sys))
            link = int(self._read(path / "iflink", 32, self.sys))
            return index > 0 and link > 0 and index != link
        except (TypeError, ValueError):
            return False

    def _traffic_sample(self, interfaces, stamp):
        boot = (self._read(self.proc / "sys/kernel/random/boot_id", 128, self.proc) or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", boot):
            boot = None
        current = set()
        for item in interfaces[:128]:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not IFACE.fullmatch(name):
                continue
            current.add(name)
            rx, tx = item.get("rx_bytes"), item.get("tx_bytes")
            if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in (rx, tx)):
                self.baselines.pop(name, None)
                self.traffic_partial = True
                continue
            previous = self.baselines.get(name)
            self.baselines[name] = (boot, stamp, rx, tx)
            if previous is None or boot is None or previous[0] != boot or rx < previous[2] or tx < previous[3] or stamp < previous[1]:
                self.traffic_partial = True
                continue
            before, after = self._periods(previous[1]), self._periods(stamp)
            for index, kind in enumerate(("day", "month")):
                if before[index] != after[index]:
                    self.traffic_partial = True  # Never apportion a missing interval across midnight.
                    continue
                key = (kind, after[index], name)
                delta = (rx - previous[2], tx - previous[3])
                totals = self.traffic_totals.setdefault(key, [0, 0])
                pending = self.pending_traffic.setdefault(key, [0, 0])
                for channel in (0, 1):
                    totals[channel] += delta[channel]
                    pending[channel] += delta[channel]
        self.baselines = {name: value for name, value in self.baselines.items() if name in current}
        day, month = self._periods(stamp)
        self.traffic_totals = {key: value for key, value in self.traffic_totals.items()
                               if key[1] == (day if key[0] == "day" else month)}
        if len(self.pending_traffic) > 512:
            self.pending_traffic = dict(list(self.pending_traffic.items())[-512:])
            self.traffic_partial = True

    def _power(self, now):
        domains, previous = [], {}
        base = self.sys / "class/powercap"
        try:
            paths = sorted(base.glob("intel-rapl:*"))[:16]
        except OSError:
            paths = []
        for path in paths:
            if not re.fullmatch(r"intel-rapl:[0-9]+", path.name):
                continue
            name = (self._read(path / "name", 128, self.sys) or "").strip()
            if not re.fullmatch(r"package(?:-[0-9]+)?", name):
                continue
            try:
                energy = int(self._read(path / "energy_uj", 128, self.sys))
                maximum = int(self._read(path / "max_energy_range_uj", 128, self.sys))
                if not 0 <= energy < maximum <= 2**64:
                    raise ValueError()
            except (TypeError, ValueError):
                continue
            identity = str(path.resolve())
            if identity in previous:
                continue
            previous[identity] = (now, energy, maximum)
            old = self.previous_power.get(identity)
            watts = None
            if old is not None and 0 < now - old[0] <= 60 and old[2] == maximum:
                delta = energy - old[1] if energy >= old[1] else maximum - old[1] + energy
                watts = number(delta / 1_000_000 / (now - old[0]), 0, 1000)
            domains.append({"name": name, "watts": watts})
        self.previous_power = previous
        valid = bool(domains) and all(row["watts"] is not None for row in domains)
        return {"available": valid, "cpu_package_w": sum(row["watts"] for row in domains) if valid else None,
                "domains": domains, "source": "RAPL package energy counter", "scope": "cpu_package",
                "reason": None if valid else "未发现可读 RAPL 封装计数器或尚未形成有效差分；不估算整机功耗"}

    def _disk_io(self, now):
        rows, previous = [], {}
        text = self._read(self.proc / "diskstats", 262144, self.proc)
        for line in (text or "").splitlines()[:4096]:
            fields = line.split()
            if len(fields) < 14 or not DISK.fullmatch(fields[2]) or len(rows) >= 128:
                continue
            try:
                read, written = int(fields[5]) * 512, int(fields[9]) * 512
                if read < 0 or written < 0 or not (self.sys / "block" / fields[2]).exists():
                    continue
                previous[fields[2]] = (now, read, written)
                old = self.previous_disk.get(fields[2])
                rates = (None, None)
                if old and 0 < now - old[0] <= 60 and read >= old[1] and written >= old[2]:
                    rates = ((read - old[1]) / (now - old[0]), (written - old[2]) / (now - old[0]))
                rows.append({"name": fields[2], "read_bytes_per_sec": rates[0], "write_bytes_per_sec": rates[1]})
            except (ValueError, OverflowError):
                pass
        self.previous_disk = previous
        return rows

    def _event(self, event):
        self.events.append(copy.deepcopy(event))
        self.pending_events.append(copy.deepcopy(event))

    def _alerts(self, values, disks, capacities, stamp):
        thresholds, candidates, observed = self.settings["thresholds"], {}, set()
        for identity, key, value, unit in (
            ("cpu_load", "cpu", values.get("cpu_usage_percent"), "%"),
            ("memory", "memory", values.get("memory_usage_percent"), "%"),
            ("cpu_temperature", "cpu_temp", values.get("cpu_temperature_c"), "°C"),
            ("disk_temperature", "disk_temp", values.get("disk_temperature_c"), "°C")):
            if value is not None:
                observed.add(identity)
                severity = "critical" if value >= thresholds[key + "_critical"] else "warning" if value >= thresholds[key + "_warn"] else None
                if severity:
                    candidates[identity] = {"id": identity, "level": severity, "label": META.get(
                        {"cpu_load": "cpu_usage_percent", "memory": "memory_usage_percent", "cpu_temperature": "cpu_temperature_c", "disk_temperature": "disk_temperature_c"}[identity])[0],
                        "value": value, "unit": unit}
        for row in capacities[:128]:
            value = number(row.get("usage_percent"), 0, 100) if isinstance(row, dict) else None
            if value is not None:
                identity = "capacity:" + hashlib.sha256(str(row.get("mount", row.get("name", "unknown"))).encode()).hexdigest()[:16]
                observed.add(identity)
            if value is not None and value >= thresholds["capacity_warn"]:
                candidates[identity] = {"id": identity, "level": "critical" if value >= thresholds["capacity_critical"] else "warning",
                                        "label": "数据盘已用空间", "value": value, "unit": "%"}
        for row in disks:
            if type(row["passed"]) is bool:
                observed.add("smart:" + row["id"])
            if row["passed"] is False:
                identity = "smart:" + row["id"]
                candidates[identity] = {"id": identity, "level": "critical", "label": row["label"] + " SMART 报告异常", "value": None, "unit": None}
        for identity, previous in self.active.items():
            if identity not in candidates:
                # Missing telemetry is not a recovery. Keep an explicit stale
                # warning until a fresh source actually clears the condition.
                if identity not in observed:
                    candidates[identity] = {**previous, "stale": True}
                    continue
                self._event({"timestamp": stamp, "kind": "recovered", "id": identity, "label": previous["label"], "level": "info"})
        for identity, item in candidates.items():
            previous = self.active.get(identity)
            item.setdefault("since", previous.get("since", stamp) if previous else stamp)
            item["timestamp"] = stamp
            if previous is None or previous.get("level") != item["level"]:
                self._event({**item, "kind": "raised" if previous is None else "changed"})
        self.active = candidates

    def sample_once(self):
        """One fixtureable sample. Production calls this only from our worker."""
        with self.worker_lock:
            now, stamp = self.clock(), int(self.wall_clock())
            if self.settings["history_enabled"] and self.db is None and now >= self.next_db_retry:
                self.next_db_retry = now + 30
                try:
                    self._open_db()
                except (OSError, ValueError, SQLiteError):
                    self.db_reason = "宿主 Python 未提供 sqlite3；实时监控可用，持久历史不可用" if sqlite3 is None else "历史数据卷不可用，实时监控继续运行"
                    with self.lock:
                        self.history_cache = {key: self._empty_history(key, self.db_reason) for key in RANGES}
            try:
                insight = self.insights.snapshot() or {}
            except Exception:
                insight = {}
            try:
                basic = self.monitor.snapshot() or {}
            except Exception:
                basic = {}
            try:
                sources = self.sources.snapshot() or []
            except Exception:
                sources = []
            insight = insight if isinstance(insight, dict) else {}
            basic = basic if isinstance(basic, dict) else {}
            sources = sources if isinstance(sources, list) else []
            system = insight.get("system", {})
            system = system if isinstance(system, dict) else {}
            memory = system.get("memory", {})
            memory = memory if isinstance(memory, dict) else {}
            main_fresh, basic_fresh = fresh(insight, stamp, 15), fresh(basic, stamp, 15)
            basic_cpu = basic.get("cpu") if isinstance(basic.get("cpu"), dict) else {}
            basic_memory = basic.get("memory") if isinstance(basic.get("memory"), dict) else {}
            values = {"cpu_usage_percent": number(system.get("cpu_usage_percent"), 0, 100) if main_fresh else None,
                      "memory_usage_percent": number(memory.get("usage_percent"), 0, 100) if main_fresh else None}
            if basic_fresh:
                for key, value in (("cpu_usage_percent", basic_cpu.get("usage_percent")), ("memory_usage_percent", basic_memory.get("usage_percent"))):
                    if values[key] is None:
                        values[key] = number(value, 0, 100)
            meta = dict(META)
            temperatures = {"cpu": [], "disk": []}
            for row in sources[:128]:
                if not isinstance(row, dict) or row.get("status") != "ok":
                    continue
                value = number(row.get("temperature_c"), -40, 150)
                sampled = number(row.get("sampled_monotonic"), 0)
                age = number(row.get("max_age_seconds"), 0, 120)
                if value is None or sampled is None or age is None or not 0 <= now - sampled <= age:
                    continue
                if row.get("kind") in temperatures:
                    temperatures[row["kind"]].append(value)
                identity = row.get("id")
                if isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", identity) and len(values) < MAX_METRICS - 15:
                    key = "temperature." + identity
                    values[key], meta[key] = value, (label(row.get("label", identity)), "°C")
            for kind, key in (("cpu", "cpu_temperature_c"), ("disk", "disk_temperature_c")):
                values[key] = max(temperatures[kind]) if temperatures[kind] else None
            interfaces = insight.get("interfaces", [])
            interfaces = interfaces[:128] if isinstance(interfaces, list) and main_fresh else []
            self._traffic_sample(interfaces, stamp)
            selected = [row for row in interfaces if isinstance(row, dict)
                        and isinstance(row.get("name"), str) and IFACE.fullmatch(row["name"])
                        and not (self.settings["traffic_exclude_virtual"] and self._virtual_interface(row["name"]))]
            for field in ("rx_bytes_per_sec", "tx_bytes_per_sec"):
                rates = [number(row.get(field), 0) for row in selected]
                values[field] = sum(rates) if rates and all(value is not None for value in rates) else None
            try:
                power = self._power(now)
            except Exception:
                self.previous_power = {}
                power = {"available": False, "cpu_package_w": None, "domains": [], "scope": "cpu_package", "reason": "RAPL 采样失败；不估算整机功耗"}
            try:
                io_rows = self._disk_io(now)
            except Exception:
                self.previous_disk = {}
                io_rows = []
            values["cpu_package_w"] = power["cpu_package_w"]
            for field, metric in (("read_bytes_per_sec", "disk_read_bytes_per_sec"), ("write_bytes_per_sec", "disk_write_bytes_per_sec")):
                rates = [row[field] for row in io_rows]
                values[metric] = sum(rates) if rates and all(value is not None for value in rates) else None
            if self.fan_policy is not None:
                try:
                    fan = self.fan_policy.snapshot().get("channels", {})
                except Exception:
                    fan = {}
                for channel in ("cpu", "system"):
                    row = fan.get(channel, {})
                    values["fan_" + channel + "_target_percent"] = number(row.get("target_percent"), 0, 100) if row.get("active") is True else None
            inventory = insight.get("device") if isinstance(insight.get("device"), dict) else {}
            inventory_fans = inventory.get("fans", []) if fresh(inventory, stamp, 65) and inventory.get("available") is not False else []
            for row in inventory_fans[:8] if isinstance(inventory_fans, list) else []:
                if isinstance(row, dict) and number(row.get("rpm"), 0, 100000) is not None:
                    key = "fan_rpm." + hashlib.sha256(str(row.get("id")).encode()).hexdigest()[:16]
                    values[key], meta[key] = row["rpm"], (label(str(row.get("driver", "")) + " · " + str(row.get("channel", row.get("id", "")))), "rpm")
            smart_rows = []
            disk_inventory = insight.get("disks") if isinstance(insight.get("disks"), dict) else {}
            disk_source = disk_inventory.get("items", [])
            for row in disk_source[:128] if isinstance(disk_source, list) else []:
                if not isinstance(row, dict):
                    continue
                identity = str(row.get("stable_identity") or row.get("id", "unknown"))[:128]
                health = row.get("health") if isinstance(row.get("health"), dict) else {}
                sampled = number(row.get("smart_sampled_monotonic"), 0)
                is_fresh = sampled is not None and 0 <= now - sampled <= 90
                passed = health.get("passed")
                passed = passed if type(passed) is bool and row.get("standby") is not True and is_fresh else None
                entry = {"id": identity, "label": label(row.get("model") or row.get("id", "磁盘")), "passed": passed,
                         "standby": row.get("standby") is True, "temperature_c": number(row.get("smart_temperature_c"), -40, 150) if is_fresh and row.get("standby") is not True else None,
                         "reason": "休眠盘不主动唤醒" if row.get("standby") is True else "SMART 缓存已过期或缺测" if not is_fresh else label(health.get("reason") or "缓存 SMART 观测"),
                         "sample_age_seconds": now - sampled if sampled is not None and now >= sampled else None,
                         "timestamp": stamp}
                smart_rows.append(entry)
                signature = (passed, entry["standby"])
                old = self.last_smart.get(identity)
                if old is None or old[0] != signature or stamp - old[1] >= 86400:
                    observation = {**entry, "kind": "cached_smart_observation", "changed": old is not None and old[0] != signature}
                    self.observations.append(observation)
                    self.pending_smart.append(observation)
                    self.last_smart[identity] = (signature, stamp)
            self.last_smart = {key: value for key, value in list(self.last_smart.items())[-256:]}
            capacities = basic.get("disks", []) if basic_fresh else []
            self._alerts(values, smart_rows, capacities if isinstance(capacities, list) else [], stamp)
            metrics = [{"id": key, "label": meta.get(key, (key, ""))[0], "unit": meta.get(key, (key, ""))[1],
                        "value": value, "sampled_at": stamp} for key, value in values.items()][:MAX_METRICS]
            valid = sum(row["value"] is not None for row in metrics)
            traffic = self._traffic_snapshot(stamp)
            with self.lock:
                self.current = {"metrics": metrics, "power": power, "disk_io": io_rows, "sampled_at": stamp}
                self.smart = smart_rows
                self.traffic_cache = traffic
                self.alerts_cache = {"active": list(self.active.values()), "events": list(self.events)[-100:],
                                     "smart_observations": list(self.observations)[-100:]}
                self.summary = {"status": "critical" if any(row["level"] == "critical" for row in self.active.values()) else "warning" if self.active else "ok" if valid else "unknown",
                    "label": "存在严重告警" if any(row["level"] == "critical" for row in self.active.values()) else "存在需要留意的指标" if self.active else "已采集指标未见告警" if valid else "等待有效数据",
                    "active_alerts": len(self.active), "known_metrics": valid, "unknown_metrics": len(metrics) - valid}
                self.last_metrics, self.last_wall = {row["id"]: {"value": row["value"], "label": row["label"], "unit": row["unit"]} for row in metrics}, stamp
                self.last_report = {"cpu_model": label(system.get("cpu_model") or ""), "uptime_seconds": number(system.get("uptime_seconds"), 0),
                                    "memory_total_bytes": number(memory.get("total_bytes"), 0)}
            if now >= self.next_flush:
                self.next_flush = now + 30
                self._flush(stamp)

    def _flush(self, stamp):
        if not self.settings["history_enabled"]:
            self.pending_events.clear(); self.pending_smart.clear(); self.pending_traffic.clear()
            return
        if self.db is None:
            return
        try:
            cutoff = stamp - self.settings["retention_days"] * 86400
            with self.db:
                self.db.execute("DELETE FROM samples WHERE ts<?", (cutoff,))
                pages = self.db.execute("PRAGMA page_count").fetchone()[0]
                free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
                if pages - free > 30000:
                    self.db.execute("DELETE FROM samples WHERE ts IN (SELECT ts FROM samples ORDER BY ts LIMIT 10000)")
                encoded = json.dumps(self.last_metrics, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > 60000:
                    raise ValueError("bounded sample exceeded")
                self.db.execute("INSERT OR REPLACE INTO samples VALUES(?,?)", (stamp, encoded))
                for (kind, period, iface), values in self.pending_traffic.items():
                    self.db.execute("INSERT INTO traffic VALUES(?,?,?,?,?) ON CONFLICT(kind,period,iface) DO UPDATE SET rx=rx+excluded.rx,tx=tx+excluded.tx", (kind, period, iface, *values))
                self.db.execute("DELETE FROM baselines")
                self.db.executemany("INSERT INTO baselines VALUES(?,?,?,?,?)", ((name, *values) for name, values in self.baselines.items()))
                for table, values in (("events", self.pending_events), ("smart", self.pending_smart)):
                    self.db.executemany("INSERT INTO " + table + "(ts,body) VALUES(?,?)", ((row["timestamp"], json.dumps(row, ensure_ascii=True, allow_nan=False)) for row in values))
                    limit = MAX_EVENTS if table == "events" else MAX_SMART
                    self.db.execute("DELETE FROM " + table + " WHERE id NOT IN (SELECT id FROM " + table + " ORDER BY id DESC LIMIT ?)", (limit,))
                self.db.execute("DELETE FROM samples WHERE ts NOT IN (SELECT ts FROM samples ORDER BY ts DESC LIMIT ?)", (MAX_SAMPLES,))
                self.db.execute("DELETE FROM traffic WHERE rowid NOT IN (SELECT rowid FROM traffic ORDER BY period DESC LIMIT 4096)")
            self.pending_events.clear(); self.pending_smart.clear(); self.pending_traffic.clear()
            self.last_write, self.db_reason = stamp, None
            self._refresh_history(stamp)
        except (SQLiteError, OSError, ValueError, TypeError, OverflowError):
            try:
                self.db.rollback()
            except SQLiteError:
                pass
            self.db_reason = "历史写入未确认，实时监控继续运行；未把缺测补成零"
            with self.lock:
                for history in self.history_cache.values():
                    history["recording"] = False
                    history["reason"] = self.db_reason

    def _empty_history(self, range, reason):
        return {"available": False, "range": range, "aggregation": "last", "series": [], "reason": reason,
                "sample_interval_seconds": 30, "recording": False, "first_timestamp": None, "last_timestamp": None}

    def _refresh_history(self, stamp):
        caches = {}
        for key, seconds in RANGES.items():
            step = max(30, math.ceil(seconds / 720))
            rows = self.db.execute("SELECT s.ts,s.metrics FROM samples s JOIN (SELECT MAX(ts) ts FROM samples WHERE ts>=? AND ts<=? GROUP BY CAST((ts-?)/? AS INTEGER)) b ON s.ts=b.ts ORDER BY s.ts LIMIT 720",
                                   (stamp - seconds, stamp, stamp - seconds, step)).fetchall()
            series, parsed, earliest, latest = {}, [], None, None
            for timestamp, raw in rows:
                try:
                    values = json.loads(raw) if isinstance(raw, str) and len(raw) <= 60000 else None
                except (ValueError, TypeError, RecursionError):
                    continue
                if not isinstance(values, dict):
                    continue
                earliest, latest = timestamp if earliest is None else earliest, timestamp
                clean = {}
                for metric, item in list(values.items())[:MAX_METRICS]:
                    if not isinstance(metric, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,150}", metric) or not isinstance(item, dict):
                        continue
                    if metric not in series and len(series) < MAX_METRICS:
                        series[metric] = {"id": metric, "label": label(item.get("label", metric)), "unit": label(item.get("unit", ""), 16), "points": []}
                    clean[metric] = number(item.get("value"))
                parsed.append((timestamp, clean))
            for metric, row in series.items():
                previous = None
                for timestamp, values in parsed:
                    # Do not draw a line across an unsampled shutdown interval.
                    if previous is not None and timestamp - previous > step * 2:
                        row["points"].append([previous + step, None])
                    row["points"].append([timestamp, values.get(metric)])
                    previous = timestamp
                row["points"] = row["points"][-720:]
            caches[key] = {"available": bool(parsed), "range": key, "aggregation": "last", "series": list(series.values()),
                "reason": None if parsed else "记录刚开始或所选时间内暂无历史", "sample_interval_seconds": 30, "recording": True,
                "first_timestamp": earliest, "last_timestamp": latest}
        with self.lock:
            self.history_cache = caches

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({"available": bool(self.current["metrics"]), "settings": self.settings, "settings_error": self.settings_error,
                "current": self.current, "summary": self.summary, "smart": self.smart,
                "history": {"available": self.last_write is not None and self.settings["history_enabled"],
                    "recording": self.settings["history_enabled"] and self.db_reason is None, "retention_days": self.settings["retention_days"],
                    "sample_interval_seconds": 30, "last_write_at": self.last_write, "reason": "历史记录已关闭" if not self.settings["history_enabled"] else self.db_reason,
                    "storage_warning": "每 30 秒批量写入独立数据卷，可能影响该卷所在硬盘休眠"}})

    def history(self, range="24h"):
        if range not in RANGES:
            raise ValueError("不支持的历史时间范围")
        with self.lock:
            return copy.deepcopy(self.history_cache[range])

    def history_diag(self):
        """Bounded diagnostics for the panel; never creates or repairs a file."""
        path = self.state_dir / "monitoring.sqlite3"
        result = {"available": self.db is not None, "data_dir": str(self.state_dir),
                  "db_file": str(path), "db_exists": path.is_file(), "rows": None,
                  "last_ts": None, "recording": bool(self.settings.get("history_enabled")),
                  "db_error": self.db_reason, "sample_interval_seconds": 30}
        try:
            if self.db is not None:
                row = self.db.execute("SELECT COUNT(*), MAX(ts) FROM samples").fetchone()
                if row is not None:
                    result["rows"], result["last_ts"] = int(row[0] or 0), row[1]
        except Exception as error:
            result["db_error"] = type(error).__name__
        return result

    def _traffic_snapshot(self, stamp):
        day, month = self._periods(stamp)
        names = sorted({key[2] for key in self.traffic_totals if key[1] in (day, month)})
        if self.settings["traffic_exclude_virtual"]:
            names = [name for name in names if not self._virtual_interface(name)]
        items = []
        for name in names[:128]:
            daily = self.traffic_totals.get(("day", day, name))
            monthly = self.traffic_totals.get(("month", month, name))
            items.append({"name": name, "today": {"rx_bytes": daily[0], "tx_bytes": daily[1]} if daily else None,
                          "month": {"rx_bytes": monthly[0], "tx_bytes": monthly[1]} if monthly else None})
        totals = {}
        for period in ("today", "month"):
            rows = [item[period] for item in items if item[period] is not None]
            totals[period] = {"rx_bytes": sum(row["rx_bytes"] for row in rows), "tx_bytes": sum(row["tx_bytes"] for row in rows)} if rows else None
        return {"available": bool(items), "periods": {"day": day, "month": month}, "interfaces": items, "totals": totals,
            "coverage": "observed_counter_deltas", "partial": self.traffic_partial,
            "reason": "仅统计已观测的真实计数增量；重启、计数重置或跨日缺口不估算补齐"}

    def traffic(self):
        with self.lock:
            return copy.deepcopy(self.traffic_cache)

    def alerts(self):
        with self.lock:
            return copy.deepcopy(self.alerts_cache)

    def health_events(self):
        """One cache generation for machine health assessment and its alerts."""
        with self.lock:
            return copy.deepcopy({'alerts': {key: self.alerts_cache[key] for key in ('active', 'events')},
                'state': {'current': {'sampled_at': self.current.get('sampled_at')}, 'summary': self.summary}})

    def report(self):
        with self.lock:
            return copy.deepcopy({"schema_version": 1, "generated_at": int(self.wall_clock()), "scope": "locally_observed_metrics",
                "system": self.last_report, "summary": self.summary, "current": self.current, "smart": self.smart,
                "alerts": self.alerts_cache["active"], "traffic": self.traffic_cache,
                "limitations": ["缺测不表示正常", "CPU RAPL 不是整机功耗", "风扇请求输出不是 RPM 或 PWM 读回", "SMART 仅消费现有不唤醒缓存，不运行自检"]})

    def export_csv(self, range="24h"):
        history = self.history(range)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["timestamp_utc", "metric", "value", "unit"])
        for row in history["series"]:
            for stamp, value in row["points"]:
                # Labels are not exported. Guard textual cells even if a local
                # history file was replaced with malformed metadata.
                fields = [row["id"], row["unit"]]
                fields = ["'" + field if field.startswith(("=", "+", "-", "@", "\t", "\r")) else field for field in fields]
                writer.writerow([dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat(), fields[0], "" if value is None else value, fields[1]])
        return stream.getvalue()

    def start(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return self
            self.stopped.clear()
            self.thread = threading.Thread(target=self._loop, name="r1-monitoring", daemon=True)
            self.thread.start()
        return self

    def _loop(self):
        try:
            while not self.stopped.is_set():
                try:
                    self.sample_once()
                except Exception:
                    with self.lock:
                        self.db_reason = "本轮监控采样失败，其他服务继续运行"
                self.stopped.wait(2)
            with self.worker_lock:
                if self.last_wall is not None:
                    self._flush(self.last_wall)
        finally:
            if self.db is not None:
                self.db.close()
                self.db = None

    def stop(self):
        self.stopped.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
        elif self.thread is None:
            with self.worker_lock:
                if self.db is not None:
                    self.db.close()
                    self.db = None

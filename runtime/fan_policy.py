"""Independent fan rules, persistent configuration and supervised native control.

The browser edits rules; this service samples and applies them independently.
No hardware is accessed here except through the injected native adapter.
"""
import copy
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
import time


CHANNELS = ("cpu", "system")
QUICK_PERCENT = {"low": 50, "medium": 75, "high": 100}
FIELDS = {"mode", "sources", "quiet_below", "normal_at", "full_at",
          "quiet_percent", "normal_percent", "manual_percent"}
DEFAULT_CONFIG = {"schema_version": 1, "channels": {
    "cpu": {"mode": "vendor", "sources": [], "quiet_below": 40, "normal_at": 55,
            "full_at": 70, "quiet_percent": 55, "normal_percent": 75, "manual_percent": 70},
    "system": {"mode": "vendor", "sources": [], "quiet_below": 35, "normal_at": 45,
               "full_at": 55, "quiet_percent": 55, "normal_percent": 75, "manual_percent": 70},
}}


def _require(value, message):
    if not value:
        raise ValueError(message)


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_config(value):
    _require(isinstance(value, dict) and set(value) == {"schema_version", "channels"}, "风扇配置字段无效")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1, "不支持的风扇配置版本")
    _require(isinstance(value["channels"], dict) and set(value["channels"]) == set(CHANNELS), "需分别提供 CPU 和系统风扇配置")
    result = copy.deepcopy(value)
    for name in CHANNELS:
        item = result["channels"][name]
        _require(isinstance(item, dict) and set(item) == FIELDS, "风扇通道配置字段无效")
        _require(item["mode"] in ("vendor", "manual", "temperature"), "风扇模式无效")
        sources = item["sources"]
        _require(isinstance(sources, list) and len(sources) <= 32
                 and all(isinstance(source, str) and 0 < len(source) <= 256
                         and not any(ord(c) < 32 for c in source) for source in sources)
                 and len(sources) == len(set(sources)), "温度来源需要是最多 32 项的不重复列表")
        _require(item["mode"] != "temperature" or sources, "自定义温控至少选择一个温度来源")
        for key in ("quiet_below", "normal_at", "full_at"):
            _require(type(item[key]) is int and 15 <= item[key] <= 85, "温度阈值需要为 15–85°C 整数")
        _require(item["quiet_below"] < item["normal_at"] < item["full_at"], "静音、普通、全速温度必须递增")
        for key in ("quiet_percent", "normal_percent", "manual_percent"):
            _require(type(item[key]) is int and 50 <= item[key] <= 100, "目标输出需要为 50–100% 整数")
        _require(item["quiet_percent"] <= item["normal_percent"], "普通档输出不能低于静音档")
    return result


def observed_sources(rows, now):
    """Bound source metadata and derive freshness; zero degrees is a value."""
    result, seen = [], set()
    for item in rows[:512] if isinstance(rows, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen:
            continue
        if not 0 < len(item["id"]) <= 256:
            continue
        seen.add(item["id"])
        row = {key: copy.deepcopy(item.get(key)) for key in (
            "id", "label", "kind", "temperature_c", "status", "sampled_monotonic",
            "max_age_seconds", "critical_celsius", "observed_at")}
        stamp, limit = row["sampled_monotonic"], row["max_age_seconds"]
        age = now - stamp if _finite(stamp) and 0 <= stamp <= now else None
        row["age_seconds"] = age
        row["fresh"] = bool(age is not None and _finite(limit) and 0 < limit <= 300 and age <= limit)
        temperature = row["temperature_c"]
        if not _finite(temperature) or not -40 <= temperature <= 150:
            row["temperature_c"] = None
        if row["status"] not in ("ok", "standby", "unavailable"):
            row["status"] = "unavailable"
        if not row["fresh"]:
            row["status"] = "stale"
        elif row["status"] == "ok" and row["temperature_c"] is None:
            row["status"] = "unavailable"
        if not _finite(row["critical_celsius"]) or not 0 < row["critical_celsius"] <= 150:
            row["critical_celsius"] = None
        result.append(row)
    return result


def decide(policy, rows, previous=None):
    """Raise immediately on a fresh sample; reduce with explicit hysteresis."""
    if policy["mode"] == "vendor":
        return {"level": "vendor", "target_percent": None, "reference_c": None,
                "source_id": None, "source_label": None, "reason": "原厂自动管理"}
    by_id = {row["id"]: row for row in rows}
    selected = [by_id.get(source) for source in policy["sources"]]
    temperatures = [row for row in selected if row and row["fresh"]
                    and row["status"] == "ok" and row["temperature_c"] is not None]
    hottest = max(temperatures, key=lambda row: row["temperature_c"]) if temperatures else None
    base = {"reference_c": hottest["temperature_c"] if hottest else None,
            "source_id": hottest["id"] if hottest else None,
            "source_label": hottest["label"] if hottest else None}
    cpus = [row for row in rows if row["kind"] == "cpu" and row["fresh"]
            and row["status"] == "ok" and row["temperature_c"] is not None]
    cpu_hot = max(cpus, key=lambda row: row["temperature_c"]) if cpus else None
    safety = None
    if cpu_hot is None:
        safety = "CPU 保护温度不可读，使用安全全速"
    elif any(row["temperature_c"] >= min(80, row["critical_celsius"] or 80) for row in cpus):
        safety = "CPU 达到独立保护温度，使用安全全速"
    elif policy["mode"] == "temperature":
        missing = [source for source, row in zip(policy["sources"], selected)
                   if row is None or not row["fresh"] or row["status"] not in ("ok", "standby")
                   or row["status"] == "ok" and row["temperature_c"] is None]
        if missing:
            safety = "所选温度来源缺失、无效或过期，使用安全全速"
        elif any(row["critical_celsius"] is not None and row["temperature_c"] >= row["critical_celsius"] for row in temperatures):
            safety = "所选设备达到临界温度，使用安全全速"
    if safety:
        return {**base, "level": "failsafe", "target_percent": 100, "reason": safety}
    if policy["mode"] == "manual":
        return {**base, "level": "manual", "target_percent": policy["manual_percent"], "reason": "固定目标输出；CPU 过热保护持续有效"}
    if hottest is None:
        # Only explicitly fresh standby entries can reach this branch. An old
        # or missing SMART result above never silently becomes a sleeping disk.
        return {**base, "level": "quiet", "target_percent": policy["quiet_percent"],
                "reason": "所选硬盘均报告休眠；不唤醒硬盘，CPU 保护持续有效"}
    temperature = hottest["temperature_c"]
    prior = previous if previous in ("quiet", "normal", "full") else None
    if temperature >= policy["full_at"]:
        level = "full"
    elif prior == "full" and temperature >= policy["full_at"] - 3:
        level = "full"
    elif temperature >= policy["normal_at"]:
        level = "normal"
    elif temperature <= policy["quiet_below"]:
        level = "quiet"
    else:
        level = "normal" if prior in ("normal", "full") else "quiet"
    duty = {"quiet": policy["quiet_percent"], "normal": policy["normal_percent"], "full": 100}[level]
    return {**base, "level": level, "target_percent": duty, "reason": "所选有效来源取最高温；升温立即升档，降温使用迟滞"}


class FanPolicyController:
    def __init__(self, state_dir, sources, adapter, *, clock=None, interval=1):
        self.state_dir, self.sources, self.adapter = Path(state_dir), sources, adapter
        self.clock, self.interval = clock or time.monotonic, interval
        self.lock = threading.RLock()
        self.control_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = self.worker = None
        self.busy = False
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.active_config = copy.deepcopy(DEFAULT_CONFIG)
        self.active_on_boot = False
        self.settings_error = self.last_error = None
        self.catalog = []
        self.capability = {"available": False, "reason": "正在检测独立调速支持", "rpm_available": False}
        self.channels = {name: self._vendor_state() for name in CHANNELS}
        self._load()
        self._refresh_capability()

    @staticmethod
    def _vendor_state():
        return {"mode": "vendor", "active": False, "reference_c": None, "source_id": None,
                "source_label": None, "level": "vendor", "target_percent": None,
                "target_raw": None, "rpm": None, "reason": "原厂策略管理"}

    def _load(self):
        path = self.state_dir / "fan-policy.json"
        try:
            if not path.exists() and not path.is_symlink():
                return
            _require(not self.state_dir.is_symlink(), "设置目录不是普通目录")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                _require(stat.S_ISREG(info.st_mode) and info.st_size <= 65536, "风扇配置文件过大或类型无效")
                raw = stream.read(65537)
            _require(len(raw) <= 65536, "风扇配置文件过大")
            data = json.loads(raw)
            _require(isinstance(data, dict) and set(data) == {"config", "active_config", "resume_on_boot"}
                     and type(data["resume_on_boot"]) is bool, "风扇配置结构无效")
            self.config = validate_config(data["config"])
            self.active_config = validate_config(data["active_config"])
            self.active_on_boot = data["resume_on_boot"]
        except (OSError, ValueError, TypeError, RecursionError):
            self.config = copy.deepcopy(DEFAULT_CONFIG)
            self.active_config = copy.deepcopy(DEFAULT_CONFIG)
            self.active_on_boot = False
            self.settings_error = "风扇配置未能读取；保持原厂管理，请检查设置数据卷"

    def _save(self, config, active_config, resume):
        _require(not self.state_dir.is_symlink(), "设置目录不是普通目录")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.state_dir / "fan-policy.json"
        _require(not target.is_symlink(), "风扇配置文件不是普通文件")
        data = json.dumps({"config": config, "active_config": active_config, "resume_on_boot": resume},
                          ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        _require(len(data) <= 65536, "风扇配置文件过大")
        with tempfile.NamedTemporaryFile(prefix=".fan-policy-", dir=self.state_dir, delete=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        temporary.replace(target)
        fd = os.open(self.state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _refresh_capability(self):
        try:
            capability = self.adapter.capabilities()
        except Exception:
            capability = {"available": False, "reason": "原厂风扇适配暂不可用", "rpm_available": False}
        with self.lock:
            self.capability = copy.deepcopy(capability)

    def snapshot(self):
        with self.lock:
            now = self.clock()
            sources = observed_sources(self.catalog, now)
            channels = copy.deepcopy(self.channels)
            for state in channels.values():
                applied = state.get("applied_monotonic")
                if state["active"] and _finite(applied) and now - applied > 5 and not self.busy:
                    state.update(active=False, target_percent=None, target_raw=None,
                                 reason="控制回执已过期，监督服务恢复状态待确认")
                selected = next((row for row in sources if row["id"] == state["source_id"]), None)
                if selected is not None and not selected["fresh"]:
                    state["reference_c"] = None
            active = any(item["active"] for item in channels.values())
            recovery_confirmed = not any("未确认" in item["reason"] or "待确认" in item["reason"] for item in channels.values())
            return {"config": copy.deepcopy(self.config), "active_on_boot": self.active_on_boot,
                    "pending_changes": self.config != self.active_config or (not active and not self.busy
                        and any(item["mode"] != "vendor" for item in self.config["channels"].values())), "active": active,
                    "busy": self.busy, "capability": copy.deepcopy(self.capability), "channels": channels,
                    "sources": sources, "settings_error": self.settings_error,
                    "last_error": self.last_error, "recovery_confirmed": recovery_confirmed}

    def tick(self):
        catalog = observed_sources(self.sources.snapshot(), self.clock())
        with self.lock:
            self.catalog = catalog
            busy = self.busy
        if not busy:
            self._refresh_capability()
        if not self.control_lock.acquire(blocking=False):
            return
        try:
            self._control_tick(catalog)
        finally:
            self.control_lock.release()

    def _fault(self, reason):
        restored = True
        try:
            self.adapter.release()
        except Exception:
            restored = False
        with self.lock:
            self.channels = {name: self._vendor_state() for name in CHANNELS}
            if not restored:
                for state in self.channels.values():
                    state["reason"] = "自定义已中止，原厂恢复未确认；请检查监督服务"
            self.active_config = copy.deepcopy(DEFAULT_CONFIG)
            self.active_on_boot = False
            self.last_error = reason
            try:
                self._save(self.config, self.active_config, False)
            except (OSError, ValueError):
                self.settings_error = "控制已中止，但停用状态未能保存；请检查设置数据卷"

    def _control_tick(self, catalog):
        # A capability probe can wait behind a native handoff. Recalculate age
        # at the decision boundary rather than extending a pre-probe sample.
        catalog = observed_sources(catalog, self.clock())
        with self.lock:
            self.catalog = catalog
            if self.busy or self.stop_event.is_set() or not any(item["active"] for item in self.channels.values()):
                return
            settings = copy.deepcopy(self.active_config)
            previous = {name: self.channels[name]["level"] for name in CHANNELS}
        states, targets = {}, {}
        for name in CHANNELS:
            policy = settings["channels"][name]
            decision = decide(policy, catalog, previous[name])
            enabled = policy["mode"] != "vendor"
            raw = max(128, min(255, round(decision["target_percent"] * 255 / 100))) if enabled else None
            states[name] = {**decision, "mode": policy["mode"], "active": enabled, "target_raw": raw, "rpm": None}
            if enabled:
                targets[name] = raw
        try:
            self.adapter.write(targets)
        except Exception:
            self._fault("自定义控制已中止；已请求监督服务恢复原厂管理，请检查硬件与控制权")
            return
        with self.lock:
            for state in states.values():
                state["applied_monotonic"] = self.clock()
            self.channels = states
            self.last_error = None

    def _apply_worker(self, config):
        with self.control_lock:
            self._apply_locked(config)

    def _apply_locked(self, config):
        wanted = {name for name in CHANNELS if config["channels"][name]["mode"] != "vendor"}
        try:
            self.adapter.start(wanted) if wanted else self.adapter.release()
            if self.stop_event.is_set():
                self.adapter.release()
                return
            if wanted:
                # Never enter a quiet/manual state before the first fresh
                # temperature cycle and a successful acknowledgement.
                self.adapter.write({name: 255 for name in wanted})
            with self.lock:
                self._save(self.config, config, bool(wanted))
                self.active_config = copy.deepcopy(config)
                self.active_on_boot = bool(wanted)
                self.channels = {name: {**self._vendor_state(), "mode": config["channels"][name]["mode"],
                    "active": name in wanted, "level": "failsafe" if name in wanted else "vendor",
                    "target_percent": 100 if name in wanted else None, "target_raw": 255 if name in wanted else None,
                    "applied_monotonic": self.clock() if name in wanted else None,
                    "reason": "等待第一轮有效温度采样" if name in wanted else "原厂自动管理"} for name in CHANNELS}
                self.last_error = self.settings_error = None
        except Exception as error:
            restored = True
            try:
                self.adapter.release()
            except Exception:
                restored = False
            detail = str(error).strip().replace(chr(10), " ")[:500] if type(error) in (ValueError, RuntimeError) else ""
            with self.lock:
                self.channels = {name: self._vendor_state() for name in CHANNELS}
                if not restored:
                    for state in self.channels.values():
                        state["reason"] = "接管失败，原厂恢复未确认；请检查监督服务"
                self.active_config = copy.deepcopy(DEFAULT_CONFIG)
                self.active_on_boot = False
                self.last_error = ("配置已保存，但接管未完成：" + detail + "；已请求恢复原厂管理，需检查适配状态后重新应用") if detail else "配置已保存，但接管未完成；已请求恢复原厂管理，需检查适配状态后重新应用"
                try:
                    self._save(self.config, DEFAULT_CONFIG, False)
                except (OSError, ValueError):
                    self.settings_error = "控制未生效且回退状态未能保存，请检查设置数据卷"
        finally:
            with self.lock:
                self.busy = False

    def action(self, name, payload):
        saved = False
        try:
            _require(isinstance(payload, dict), "需要配置对象")
            confirmed = False
            apply_config = None
            if name == "fan.policy.release":
                _require(set(payload) == {"confirmed"} and payload["confirmed"] is True, "恢复原厂管理需要明确确认")
                config = copy.deepcopy(self.config)
                for item in config["channels"].values():
                    item["mode"] = "vendor"
                apply, confirmed = True, True
            elif name == "fan.policy.quick":
                _require(set(payload) == {"channel", "mode"}, "风扇快速调节参数无效")
                channel, mode = payload["channel"], payload["mode"]
                _require(channel in CHANNELS, "风扇通道无效")
                _require(mode in ("auto", "low", "medium", "high", "manual"), "风扇快速模式无效")
                config = copy.deepcopy(self.config)
                item = config["channels"][channel]
                if mode == "auto":
                    item["mode"] = "vendor"
                elif mode in QUICK_PERCENT:
                    item["mode"] = "manual"
                    item["manual_percent"] = QUICK_PERCENT[mode]
                else:
                    item["mode"] = "temperature"
                # Keep another channel's saved draft in config, but apply only
                # that channel's already active rule.  A quick single-channel
                # action must not silently apply the other channel's draft.
                apply_config = copy.deepcopy(config)
                for other in CHANNELS:
                    if other != channel:
                        apply_config["channels"][other] = copy.deepcopy(self.active_config["channels"][other])
                apply, confirmed = True, True
            elif name == "fan.policy.update":
                _require(not set(payload) - {"config", "apply", "confirmed"} and "config" in payload
                         and type(payload.get("apply", False)) is bool, "风扇配置请求无效")
                config, apply = payload["config"], payload.get("apply", False)
                confirmed = payload.get("confirmed") is True
            else:
                raise ValueError("不支持的风扇配置操作")
            config = validate_config(config)
            if apply_config is not None:
                apply_config = validate_config(apply_config)
            _require(not apply or confirmed, "应用风扇温控需要明确确认")
            with self.lock:
                _require(not self.busy and not self.stop_event.is_set(), "风扇控制正在切换或停止，请稍后重试")
                resume = self.active_on_boot
                apply_target = apply_config if apply_config is not None else config
                if apply and all(item["mode"] == "vendor" for item in apply_target["channels"].values()):
                    resume = False
                self._save(config, self.active_config, resume)
                saved = True
                self.config = config
                self.active_on_boot = resume
                self.settings_error = None
                if apply:
                    capability = self.capability
                    _require(not any(item["mode"] != "vendor" for item in apply_target["channels"].values())
                             or capability.get("available") is True, "当前设备尚不满足独立调速条件；配置已保存，未接管")
                    self.busy = True
                    self.worker = threading.Thread(target=self._apply_worker, args=(copy.deepcopy(apply_target),),
                                                   daemon=True, name="r1-fan-policy-apply")
                    try:
                        self.worker.start()
                    except RuntimeError:
                        self.busy = False
                        raise
            return {"ok": True, "settings_saved": True, "applied": False, "pending": apply,
                    "fan_policy": self.snapshot()}
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            # Keep the actionable preflight/validation reason available to the
            # panel.  Do not expose raw OS/path details; those stay generic.
            detail = str(error).strip()[:160] if type(error) in (ValueError, RuntimeError) else ''
            with self.lock:
                self.last_error = ('风扇配置未应用：' + detail) if detail else '风扇配置未应用；请检查设备支持或设置数据卷'
            return {"ok": False, "settings_saved": saved, "applied": False, "pending": False,
                    "error": self.last_error,
                    "fan_policy": self.snapshot()}

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.tick()
            except Exception:
                # Do not retain a stale active label or resume automatically
                # after a source service failure.
                if self.control_lock.acquire(blocking=False):
                    try:
                        with self.lock:
                            active = any(item["active"] for item in self.channels.values())
                        if active:
                            self._fault("温度采集异常；已请求监督服务恢复安全输出与原厂管理")
                        else:
                            with self.lock:
                                self.last_error = "温度采集暂不可用，保持原厂管理"
                    finally:
                        self.control_lock.release()

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        try:
            self.tick()
        except Exception:
            # An optional source failure must not prevent the display from
            # opening. Initial native handoff starts at full output, and the
            # normal loop can recover measurements or release ownership.
            with self.lock:
                self.catalog = []
                self.last_error = "温度采集暂不可用；自定义控制保持安全输出或原厂管理"
        if self.active_on_boot:
            with self.lock:
                self.busy = True
                self.worker = threading.Thread(target=self._apply_worker, args=(copy.deepcopy(self.active_config),),
                                               daemon=True, name="r1-fan-policy-restore")
                self.worker.start()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="r1-fan-policy")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        for thread in (self.thread, self.worker):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=15)
        if self.worker is not None and self.worker.is_alive():
            raise RuntimeError("风扇切换未及时结束，监督服务将接手恢复")
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError("风扇采样未及时结束，监督服务将接手恢复")
        self.adapter.release()
        with self.lock:
            self.channels = {name: self._vendor_state() for name in CHANNELS}

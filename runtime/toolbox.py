"""Explicit, capability-gated NAS controls; settings never touch official app files."""
import copy
import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time

PAGES = ("overview", "storage", "network", "controls", "apps", "system", "settings")
DEFAULTS = {
    "theme": "dark", "font_scale": 100, "page_interval": 0,
    "sleep_timeout": 0, "brightness": 80, "night_enabled": False,
    "night_start": "22:00", "night_end": "07:00", "night_brightness": 20,
    "enabled_pages": list(PAGES),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, low, high, label):
    require(type(value) is int and low <= value <= high, label + "超出允许范围")
    return value


def keys(payload, allowed):
    require(isinstance(payload, dict) and not (set(payload) - set(allowed)), "参数包含未知字段")


def validated_settings(changes, original):
    keys(changes, DEFAULTS)
    result = copy.deepcopy(original)
    result.update(changes)
    require(result["theme"] in ("dark", "light", "ink", "jade"), "不支持的主题")
    for name, low, high in (("font_scale", 80, 140), ("page_interval", 0, 3600),
                            ("sleep_timeout", 0, 86400), ("brightness", 5, 100),
                            ("night_brightness", 5, 100)):
        integer(result[name], low, high, name)
    require(type(result["night_enabled"]) is bool, "night_enabled 必须是布尔值")
    for name in ("night_start", "night_end"):
        require(isinstance(result[name], str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", result[name]),
                name + " 必须是 HH:MM")
    require(not result["night_enabled"] or result["night_start"] != result["night_end"], "夜间开始和结束时间不能相同")
    pages = result["enabled_pages"]
    require(isinstance(pages, list) and pages and all(isinstance(p, str) and p in PAGES for p in pages)
            and len(pages) == len(set(pages)), "至少保留一个有效页面，且页面不能重复")
    return result


class ToolboxController:
    def __init__(self, state_dir=None, sys_root="/sys", runner=None, dev_root="/dev", clock=None):
        self.state_dir = Path(state_dir) if state_dir is not None else Path("/run/r1-toolbox")
        self.sys_root, self.dev_root = Path(sys_root), Path(dev_root)
        self.runner = runner or self._run
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.settings = copy.deepcopy(DEFAULTS)
        self.settings_error = None
        self.last_activity = self.clock()
        self.asleep = False
        self.saved_brightness = None
        self.original_screen = {}
        self.night_active = None
        self.owned_fans = {}
        self.fan_error = None
        self.rgb_bus = self.rgb_mode = self.rgb_color = None
        self.pending = None
        self.executing = False
        self.power_error = None
        self.systemctl = shutil.which("systemctl")
        self.i2cget, self.i2cset = shutil.which("i2cget"), shutil.which("i2cset")
        self._load_settings()

    @staticmethod
    def _run(argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=3)

    @staticmethod
    def _read_int(path):
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_int(path, value):
        path.write_text(str(value))

    def _load_settings(self):
        path = self.state_dir / "settings.json"
        try:
            if not path.exists() and not path.is_symlink():
                return
            require(not self.state_dir.is_symlink(), "设置目录不能是符号链接")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_size <= 16384, "设置文件无效或过大")
                data = handle.read(16385)
            require(len(data) <= 16384, "设置文件过大")
            self.settings = validated_settings(json.loads(data.decode("utf-8")), DEFAULTS)
        except (OSError, ValueError, UnicodeError) as error:
            self.settings_error = "设置未加载：" + str(error)

    def _save_settings(self, settings):
        require(not self.state_dir.is_symlink(), "设置目录不能是符号链接")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        target = self.state_dir / "settings.json"
        require(not target.is_symlink(), "设置文件不能是符号链接")
        data = (json.dumps(settings, ensure_ascii=False, sort_keys=True) + "\n").encode()
        require(len(data) <= 16384, "设置文件过大")
        with tempfile.NamedTemporaryFile(prefix=".settings-", dir=self.state_dir, delete=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(target)

    def _backlight(self):
        folder = self.sys_root / "class/backlight/intel_backlight"
        maximum = self._read_int(folder / "max_brightness")
        if maximum is None or maximum <= 0 or not (folder / "brightness").is_file():
            return None, None
        return folder, maximum

    def _brightness(self):
        folder, maximum = self._backlight()
        value = self._read_int(folder / "brightness") if folder else None
        return round(100 * value / maximum) if value is not None and 0 <= value <= maximum else None

    def _remember_screen(self):
        """Capture raw pre-control values once; a theme-only setting takes no ownership."""
        folder, _ = self._backlight()
        paths = [self.sys_root / "class/graphics/fb0/blank"]
        if folder is not None:
            paths.insert(0, folder / "brightness")
        for path in paths:
            if path not in self.original_screen:
                value = self._read_int(path)
                if value is not None:
                    self.original_screen[path] = value

    def _set_brightness(self, value):
        folder, maximum = self._backlight()
        require(folder is not None, "未发现可用的 intel_backlight 背光接口")
        self._remember_screen()
        self._write_int(folder / "brightness", max(1, round(maximum * value / 100)))

    def _effective_brightness(self):
        if self.settings["night_enabled"] and self._is_night():
            return self.settings["night_brightness"]
        return self.settings["brightness"]

    def _sleep(self):
        folder, _ = self._backlight()
        blank = self.sys_root / "class/graphics/fb0/blank"
        require(blank.is_file() or folder is not None, "未发现可用的息屏接口")
        if self.asleep:
            return
        self.saved_brightness = self._brightness()
        self._remember_screen()
        if blank.is_file():
            self._write_int(blank, 4)
        else:
            self._write_int(folder / "brightness", 0)
        self.asleep = True

    def _wake(self):
        self.last_activity = self.clock()
        folder, _ = self._backlight()
        blank = self.sys_root / "class/graphics/fb0/blank"
        if not self.asleep and self._brightness() != 0 and self._read_int(blank) in (None, 0):
            return
        require(blank.is_file() or folder is not None, "未发现可用的唤醒接口")
        self._remember_screen()
        value = self.saved_brightness if self.saved_brightness and not self.settings["night_enabled"] else self._effective_brightness()
        if folder:
            self._set_brightness(value)
        if blank.is_file():
            self._write_int(blank, 0)
        self.asleep = False

    def _fan_channels(self):
        result = []
        for folder in sorted((self.sys_root / "class/hwmon").glob("hwmon*")):
            try:
                driver = (folder / "name").read_text().strip()
            except OSError:
                continue
            if not re.fullmatch(r"it(?:87|8[0-9]{3})", driver):
                continue
            for path in sorted(folder.glob("pwm[0-9]*")):
                if not re.fullmatch(r"pwm[0-9]+", path.name):
                    continue
                value, enabled = self._read_int(path), self._read_int(folder / (path.name + "_enable"))
                if value is None or enabled is None or not 0 <= value <= 255:
                    continue
                identifier = folder.name + ":" + path.name
                owned = self.owned_fans.get(identifier)
                rpm = self._read_int(folder / ("fan" + path.name[3:] + "_input"))
                if rpm is not None and rpm < 0:
                    rpm = None
                result.append({"id": identifier, "driver": driver, "duty_percent": round(value * 100 / 255),
                               "owned": owned is not None, "mode": owned["mode"] if owned else "native",
                               "rpm": rpm, "rpm_verified": rpm is not None,
                               "rpm_reason": "同号 fanN_input；与实际风扇的对应仍需确认" if rpm is not None
                               else "无同号 fanN_input，无法验证实际转速"})
        return result

    def _temperature(self):
        values = []
        for folder in (self.sys_root / "class/hwmon").glob("hwmon*"):
            try:
                if (folder / "name").read_text().strip() not in ("coretemp", "k10temp"):
                    continue
            except OSError:
                continue
            for path in folder.glob("temp[0-9]*_input"):
                value = self._read_int(path)
                if value is not None and -50000 <= value <= 150000:
                    values.append(value / 1000)
        return max(values) if values else None

    def _fan_paths(self, channel):
        require(isinstance(channel, str) and re.fullmatch(r"hwmon[0-9]+:pwm[0-9]+", channel), "必须明确选择 hwmonN:pwmN 通道")
        require(any(item["id"] == channel for item in self._fan_channels()), "该风扇通道不受支持或不存在；未修改硬件")
        hwmon, pwm = channel.split(":")
        folder = self.sys_root / "class/hwmon" / hwmon
        return folder / pwm, folder / (pwm + "_enable")

    def _restore_fan(self, channel):
        original = self.owned_fans.get(channel)
        require(original is not None, "此通道尚未由 Toolbox 接管")
        path, enable = self._fan_paths(channel)
        self._write_int(path, original["pwm"])
        self._write_int(enable, original["enable"])
        del self.owned_fans[channel]

    def _set_fan(self, payload):
        keys(payload, ("channel", "mode", "value", "takeover"))
        require(payload.get("takeover") is True, "需要明确确认接管并选择风扇通道")
        channel, mode = payload.get("channel"), payload.get("mode")
        require(mode in ("manual", "auto", "low", "medium", "high"), "不支持的风扇模式")
        value = integer(payload.get("value", 100), 50, 100, "风扇占空比")
        path, enable = self._fan_paths(channel)
        if mode == "auto":
            temperature = self._temperature()
            value = 100 if temperature is None else max(60, min(100, round(60 + (temperature - 40) * 2)))
        elif mode in ("low", "medium", "high"):
            value = {"low": 60, "medium": 80, "high": 100}[mode]
        if channel not in self.owned_fans:
            self.owned_fans[channel] = {"pwm": self._read_int(path), "enable": self._read_int(enable), "mode": mode}
        try:
            self._write_int(enable, 1)
            self._write_int(path, round(255 * value / 100))
            self.owned_fans[channel]["mode"] = mode
            self._check_fan_rpm(channel, path, value)
            self.fan_error = None
        except OSError:
            self._restore_fan(channel)
            raise

    def _check_fan_rpm(self, channel, path, value):
        rpm = self._read_int(path.parent / ("fan" + path.name[3:] + "_input"))
        if rpm == 0:
            if value < 100:
                self._write_int(path, 255)
            self.owned_fans[channel]["mode"] = "high"
            self.fan_error = "同号 RPM 读数为 0，已回退满速；请确认通道对应或检查风扇"
            raise RuntimeError(self.fan_error)

    def _set_rgb(self, payload):
        keys(payload, ("bus", "mode", "color"))
        bus = payload.get("bus", self.rgb_bus)
        integer(bus, 0, 255, "I2C bus")
        mode = integer(payload.get("mode"), 0, 3, "RGB 模式")
        color = payload.get("color")
        require(isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color), "颜色必须是 #RRGGBB")
        require(self.i2cget and self.i2cset and (self.dev_root / ("i2c-" + str(bus))).exists(),
                "I2C 工具或指定总线不存在；未写入 RGB")
        if bus != self.rgb_bus:
            probe = self.runner([self.i2cget, "-y", str(bus), "0x26", "0x90", "b"])
            try:
                current = int(probe.stdout.strip(), 16)
            except (ValueError, AttributeError):
                current = -1
            require(probe.returncode == 0 and current in (0, 1, 2, 3), "0x26 非强制读取验证失败；未写入 RGB")
        channels = [int(color[index:index + 2], 16) for index in (1, 3, 5)]
        for register, value in list(zip((0x92, 0x93, 0x94), channels)) + [(0x90, mode)]:
            result = self.runner([self.i2cset, "-y", str(bus), "0x26", hex(register), hex(value), "b"])
            require(result.returncode == 0, "RGB 写入失败；请检查总线状态")
        self.rgb_bus, self.rgb_mode, self.rgb_color = bus, mode, color.upper()

    def _is_night(self):
        now = datetime.datetime.now().strftime("%H:%M")
        start, end = self.settings["night_start"], self.settings["night_end"]
        return start <= now < end if start < end else now >= start or now < end

    def snapshot(self):
        with self.lock:
            folder, maximum = self._backlight()
            blank = self.sys_root / "class/graphics/fb0/blank"
            fans = self._fan_channels()
            buses = sorted(int(path.name.split("-", 1)[1]) for path in self.dev_root.glob("i2c-*")
                           if re.fullmatch(r"i2c-[0-9]+", path.name))
            screen_available = folder is not None or blank.is_file()
            remaining = max(0, math.ceil(self.pending["deadline"] - self.clock())) if self.pending else None
            return {
                "capabilities": {
                    "screen": {"available": screen_available, "reason": "" if screen_available else "未发现背光或 fb0 息屏接口"},
                    "fans": {"available": bool(fans), "reason": "" if fans else "缺少 IT87 hwmon 风扇驱动/PWM 通道；gpio-it87 不提供风扇控制，不加载模块或使用 /dev/port"},
                    "rgb": {"available": self.rgb_bus is not None, "reason": "" if self.rgb_bus is not None else "需明确指定 I2C bus 并验证 0x26；默认不写入"},
                    "power": {"available": self.systemctl is not None, "reason": "" if self.systemctl else "宿主缺少 systemctl"},
                },
                "settings": copy.deepcopy(self.settings), "settings_error": self.settings_error,
                "screen": {"brightness": self._brightness(), "asleep": self.asleep or (self._read_int(blank) or 0) != 0,
                           "max_brightness": maximum},
                "fans": {"channels": fans, "temperature_c": self._temperature(), "last_error": self.fan_error},
                "rgb": {"bus": self.rgb_bus, "mode": self.rgb_mode, "color": self.rgb_color, "availableBuses": buses},
                "power": {"pending": {"operation": self.pending["operation"], "seconds_remaining": remaining} if self.pending else None,
                          "executing": self.executing, "last_error": self.power_error},
            }

    def action(self, name, payload):
        try:
            with self.lock:
                require(not self.stopped.is_set(), "控制服务正在停止，操作未执行")
                require(isinstance(payload, dict), "参数必须是 JSON 对象")
                if name == "settings.update":
                    keys(payload, ("settings",))
                    updated = validated_settings(payload.get("settings"), self.settings)
                    self._save_settings(updated)
                    changes = payload["settings"]
                    self.settings, self.settings_error = updated, None
                    self.last_activity = self.clock()
                    if not self.asleep and set(changes) & {"brightness", "night_enabled", "night_start", "night_end", "night_brightness"}:
                        try:
                            self._set_brightness(self._effective_brightness())
                        except (OSError, ValueError) as error:
                            raise RuntimeError("设置已保存，但背光调整失败：" + str(error)) from error
                elif name == "screen.brightness":
                    keys(payload, ("value",))
                    value = integer(payload.get("value"), 5, 100, "亮度")
                    self._set_brightness(value)
                    self.settings["brightness"] = value
                    self.last_activity = self.clock()
                    self._save_settings(self.settings)
                elif name == "screen.sleep":
                    keys(payload, ())
                    self._sleep()
                elif name == "screen.wake":
                    keys(payload, ())
                    self._wake()
                elif name == "screen.activity":
                    keys(payload, ())
                    self.last_activity = self.clock()
                elif name == "fan.set":
                    self._set_fan(payload)
                elif name == "fan.restore":
                    keys(payload, ("channel",))
                    self._restore_fan(payload.get("channel"))
                elif name == "rgb.set":
                    self._set_rgb(payload)
                elif name == "power.request":
                    keys(payload, ("operation", "confirmed"))
                    require(payload.get("confirmed") is True, "电源操作需要明确确认")
                    operation = payload.get("operation")
                    require(operation in ("reboot", "poweroff"), "不支持的电源操作")
                    require(self.systemctl is not None, "宿主缺少 systemctl")
                    require(self.pending is None and not self.executing, "已有待执行或正在执行的电源操作")
                    self.pending = {"operation": operation, "deadline": self.clock() + 5}
                    self.power_error = None
                    self.start()
                elif name == "power.cancel":
                    keys(payload, ())
                    require(not self.executing, "电源操作已开始，无法取消")
                    self.pending = None
                else:
                    raise ValueError("不支持的操作")
            return {"ok": True, "state": self.snapshot()}
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            return {"ok": False, "error": str(error)}

    def tick(self):
        command = None
        with self.lock:
            if self.stopped.is_set():
                return
            if self.pending and self.clock() >= self.pending["deadline"]:
                command = [self.systemctl, "--no-block", self.pending["operation"]]
                self.pending, self.executing = None, True
            if command is None:
                if self.settings["night_enabled"]:
                    active = self._is_night()
                    if active != self.night_active:
                        if not self.asleep:
                            self._set_brightness(self._effective_brightness())
                        self.night_active = active
                timeout = self.settings["sleep_timeout"]
                if timeout and not self.asleep and self.clock() - self.last_activity >= timeout:
                    self._sleep()
                for channel, original in list(self.owned_fans.items()):
                    try:
                        path, _ = self._fan_paths(channel)
                        if original["mode"] == "auto":
                            temperature = self._temperature()
                            value = 100 if temperature is None else max(60, min(100, round(60 + (temperature - 40) * 2)))
                            self._write_int(path, round(255 * value / 100))
                        else:
                            value = self._read_int(path) * 100 / 255
                        self._check_fan_rpm(channel, path, value)
                    except (OSError, ValueError, RuntimeError, TypeError) as error:
                        # One failed channel must not prevent checking the others.
                        self.fan_error = str(error)
        if command:
            try:
                result = self.runner(command)
                if result.returncode != 0:
                    raise RuntimeError("systemctl 执行失败：" + (getattr(result, "stderr", "") or str(result.returncode)).strip())
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                with self.lock:
                    self.power_error, self.executing = str(error), False

    def _loop(self):
        while not self.stopped.wait(0.25):
            try:
                self.tick()
            except (OSError, ValueError, RuntimeError) as error:
                with self.lock:
                    self.settings_error = "定时控制失败：" + str(error)

    def start(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            # Persisted brightness is a wake/action target, not boot-time ownership.
            # Only an explicitly enabled saved night schedule may apply it on tick.
            self.stopped.clear()
            self.thread = threading.Thread(target=self._loop, name="r1-controls", daemon=True)
            self.thread.start()

    def stop(self):
        self.stopped.set()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        with self.lock:
            self.pending = None
            for channel in list(self.owned_fans):
                try:
                    self._restore_fan(channel)
                except (OSError, ValueError) as error:
                    self.settings_error = "风扇恢复失败：" + str(error)
            # Restore brightness before blank so a previously visible screen does
            # not remain blank after this service exits or the official UI returns.
            for path in sorted(self.original_screen, key=lambda item: item.name == "blank"):
                try:
                    self._write_int(path, self.original_screen[path])
                    del self.original_screen[path]
                except OSError as error:
                    self.settings_error = "屏幕原始状态恢复失败：" + str(error)
            self.asleep, self.saved_brightness = False, None

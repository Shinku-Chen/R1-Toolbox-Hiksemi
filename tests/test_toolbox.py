import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("r1_toolbox_controls", Path(__file__).resolve().parents[1] / "runtime/toolbox.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SaverSettingsTests(unittest.TestCase):
    """Ordered saver selections are configuration, never hardware discovery."""

    def test_defaults_and_mixed_items_preserve_order_and_do_not_alias_inputs(self):
        self.assertEqual(MODULE.DEFAULTS["saver_items"], ["cpu_usage", "memory_usage"])
        items = ["temperature:disk:wwn:absent-123", "memory_usage", "cpu_temperature", "cpu_usage"]
        original = copy.deepcopy(MODULE.DEFAULTS)
        result = MODULE.validated_settings({"saver_items": items}, original)
        self.assertEqual(result["saver_items"], items)
        items.append("board_temperature")
        self.assertNotEqual(result["saver_items"], items)
        self.assertEqual(original, MODULE.DEFAULTS)

    def test_legacy_content_migrates_only_when_explicit_or_items_are_absent(self):
        custom = copy.deepcopy(MODULE.DEFAULTS)
        custom["saver_items"] = ["temperature:sysfs:missing", "memory_usage"]
        for content, expected in (("usage", ["cpu_usage", "memory_usage"]),
                                   ("temperature", ["cpu_temperature", "board_temperature"])):
            with self.subTest(content=content):
                result = MODULE.validated_settings({"saver_content": content}, custom)
                self.assertEqual(result["saver_items"], expected)
                legacy = copy.deepcopy(MODULE.DEFAULTS)
                legacy.pop("saver_items")
                legacy["saver_content"] = content
                result = MODULE.validated_settings({"saver_timeout": 60}, legacy)
                self.assertEqual(result["saver_items"], expected)

    def test_explicit_items_win_over_simultaneous_legacy_content(self):
        result = MODULE.validated_settings({"saver_content": "temperature", "saver_items": ["memory_usage"]},
                                           MODULE.DEFAULTS)
        self.assertEqual(result["saver_items"], ["memory_usage"])
        self.assertEqual(result["saver_content"], "temperature")

    def test_unrelated_updates_preserve_custom_and_unknown_device_selections(self):
        original = copy.deepcopy(MODULE.DEFAULTS)
        original["saver_items"] = ["temperature:disk:wwn:unplugged", "cpu_usage", "board_temperature"]
        for changes in ({"brightness": 60}, {"saver_timeout": 300}, {"saver_brightness": 20}, {"theme": "light"}):
            with self.subTest(changes=changes):
                result = MODULE.validated_settings(changes, original)
                self.assertEqual(result["saver_items"], original["saver_items"])
                self.assertIsNot(result["saver_items"], original["saver_items"])

    def test_dynamic_id_maximum_length_and_twelve_item_limit_are_accepted(self):
        longest = "temperature:" + "A0:_.-" * 42 + "abcd"
        self.assertEqual(len(longest), 268)
        self.assertEqual(MODULE.validated_settings({"saver_items": [longest]}, MODULE.DEFAULTS)["saver_items"], [longest])
        items = ["temperature:sysfs:source-" + str(index) for index in range(12)]
        self.assertEqual(MODULE.validated_settings({"saver_items": items}, MODULE.DEFAULTS)["saver_items"], items)

    def test_empty_duplicate_unknown_invalid_type_and_overlong_selections_are_rejected(self):
        invalid = ([], None, "cpu_usage", ("cpu_usage",), [True], [1], [{}], [["cpu_usage"]],
                   ["cpu_usage", "cpu_usage"], ["gpu_usage"], ["temperature:"],
                   ["temperature:" + "x" * 257], ["temperature:含中文"], ["temperature:a/b"],
                   ["temperature:a\\b"], ["temperature:a b"], ["temperature:a\n"],
                   ["temperature:a\x00"], ["temperature:a%20"],
                   ["temperature:sysfs:" + str(index) for index in range(13)])
        for items in invalid:
            with self.subTest(items=items), self.assertRaises(ValueError):
                MODULE.validated_settings({"saver_items": items}, MODULE.DEFAULTS)


class ToolboxTests(unittest.TestCase):
    def test_releasing_screen_restores_backlight_and_does_not_stop_controls(self):
        folder = self.sys / 'class/backlight/intel_backlight'
        self.write(folder / 'max_brightness', '1000')
        self.write(folder / 'brightness', '700')
        self.write(self.sys / 'class/graphics/fb0/blank', '0')
        with mock.patch.object(self.controller, '_save_settings'):
            result = self.controller.action('screen.brightness', {'value': 20})
        self.assertTrue(result['ok'], result)
        self.controller.set_screen_managed(False)
        self.assertEqual((folder / 'brightness').read_text(), '700')
        self.assertFalse(self.controller.stopped.is_set())
        self.assertFalse(self.controller.snapshot()['capabilities']['screen']['available'])
        self.assertFalse(self.controller.action('screen.brightness', {'value': 10})['ok'])

    def test_unmanaged_screen_saves_settings_without_applying_night_or_idle_control(self):
        folder = self.sys / 'class/backlight/intel_backlight'
        self.write(folder / 'max_brightness', '1000')
        self.write(folder / 'brightness', '700')
        self.write(self.sys / 'class/graphics/fb0/blank', '0')
        self.controller.set_screen_managed(False)
        with mock.patch.object(self.controller, '_save_settings'):
            result = self.controller.action('settings.update', {'settings': {'brightness': 10,
                'night_enabled': True, 'night_start': '00:00', 'night_end': '23:59', 'sleep_timeout': 60}})
        self.assertTrue(result['ok'], result)
        self.now = 3600
        self.controller.tick()
        self.assertEqual((folder / 'brightness').read_text(), '700')
        self.assertEqual((self.sys / 'class/graphics/fb0/blank').read_text(), '0')
        self.assertFalse(self.controller.asleep)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sys, self.dev, self.state = self.root / "sys", self.root / "dev", self.root / "state"
        self.sys.mkdir()
        self.dev.mkdir()
        self.calls, self.now = [], 0.0
        self.finder = mock.patch.object(MODULE.shutil, "which", side_effect=lambda name: "/usr/sbin/" + name)
        self.finder.start()
        self.controller = MODULE.ToolboxController(
            self.state, self.sys, runner=self.runner, dev_root=self.dev, clock=lambda: self.now)
        self.backlight = self.sys / "class/backlight/intel_backlight"
        self.write(self.backlight / "brightness", "48000")
        self.write(self.backlight / "max_brightness", "96000")
        self.write(self.sys / "class/graphics/fb0/blank", "0")
        self.write(self.sys / "class/hwmon/hwmon0/name", "acpitz")
        self.write(self.sys / "class/hwmon/hwmon1/name", "coretemp")

    def tearDown(self):
        self.controller.stop()
        self.finder.stop()
        self.temporary.cleanup()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def runner(self, argv):
        self.calls.append(argv)
        value = "0x55\n" if "0x01" in argv else "0x01\n"
        return subprocess.CompletedProcess(argv, 0, value, "")

    def files(self):
        return {p.relative_to(self.sys).as_posix(): p.read_bytes() for p in self.sys.rglob("*") if p.is_file()}

    def test_default_lifecycle_does_not_touch_hardware_or_probe_i2c(self):
        before = self.files()
        self.controller.start()
        self.controller.tick()
        snapshot = self.controller.snapshot()
        self.controller.stop()
        self.assertEqual(before, self.files())
        self.assertEqual([], self.calls)
        self.assertFalse(snapshot["capabilities"]["fans"]["available"])
        self.assertIn("IT87", snapshot["capabilities"]["fans"]["reason"])
        self.assertFalse(snapshot["capabilities"]["rgb"]["available"])
        self.assertFalse(self.state.exists())

    def test_settings_reject_unknown_and_invalid_values_without_saving(self):
        bad = [{"unknown": 1}, {"font_scale": True}, {"font_scale": float("nan")},
               {"brightness": float("inf")}, {"brightness": 4}, {"font_scale": 141},
               {"enabled_pages": []}, {"enabled_pages": ["bad"]},
               {"enabled_pages": ["overview", "overview"]},
               {"night_enabled": 1}, {"night_start": "25:00"}, {"page_interval": -1}]
        before = self.files()
        for changes in bad:
            with self.subTest(changes=changes):
                self.assertFalse(self.controller.action("settings.update", {"settings": changes})["ok"])
        self.assertEqual(before, self.files())
        self.assertFalse(self.state.exists())

    def test_settings_save_only_in_state_dir_and_reload(self):
        result = self.controller.action("settings.update", {"settings": {"theme": "jade", "font_scale": 120}})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["settings_saved"])
        self.assertTrue((self.state / "settings.json").is_file())
        other = MODULE.ToolboxController(self.state, self.sys, runner=self.runner, dev_root=self.dev)
        self.assertEqual(other.snapshot()["settings"]["font_scale"], 120)
        self.assertEqual(other.snapshot()["settings"]["theme"], "jade")

    def test_custom_saver_items_persist_and_reload_after_brightness_update(self):
        items = ["temperature:disk:wwn:gone-device", "memory_usage", "cpu_temperature"]
        result = self.controller.action("settings.update", {"settings": {"saver_items": items}})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["settings_saved"])
        result = self.controller.action("settings.update", {"settings": {"brightness": 65}})
        self.assertTrue(result["ok"], result)
        saved = json.loads((self.state / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["saver_items"], items)
        other = MODULE.ToolboxController(self.state, self.sys, runner=self.runner, dev_root=self.dev)
        self.assertEqual(other.snapshot()["settings"]["saver_items"], items)

    def test_legacy_settings_file_migrates_in_memory_without_rewriting_or_hardware_reads(self):
        before = self.files()
        self.state.mkdir()
        path = self.state / "settings.json"
        raw = '{"saver_content":"temperature","saver_timeout":300}'
        path.write_text(raw, encoding="utf-8")
        other = MODULE.ToolboxController(self.state, self.sys, runner=self.runner, dev_root=self.dev)
        self.assertEqual(other.settings["saver_items"], ["cpu_temperature", "board_temperature"])
        self.assertIsNone(other.settings_error)
        self.assertEqual(path.read_text(encoding="utf-8"), raw)
        self.assertEqual(before, self.files())
        self.assertEqual(self.calls, [])

    def test_invalid_saved_items_fall_back_to_defaults_with_error(self):
        self.state.mkdir()
        (self.state / "settings.json").write_text('{"saver_items":[]}', encoding="utf-8")
        other = MODULE.ToolboxController(self.state, self.sys, runner=self.runner, dev_root=self.dev)
        self.assertEqual(other.settings["saver_items"], ["cpu_usage", "memory_usage"])
        self.assertIsNotNone(other.settings_error)

    def test_invalid_saver_selection_never_reaches_storage_or_hardware(self):
        before = self.files()
        with mock.patch.object(self.controller, "_save_settings") as save:
            for items in ([], ["cpu_usage", "cpu_usage"], ["temperature:../bad/path"], ["gpu_usage"]):
                result = self.controller.action("settings.update", {"settings": {"saver_items": items}})
                self.assertFalse(result["ok"])
                self.assertFalse(result["settings_saved"])
            save.assert_not_called()
        self.assertEqual(before, self.files())

    def test_same_value_save_failure_is_not_reported_as_persisted(self):
        changes = {"theme": self.controller.settings["theme"]}
        with mock.patch.object(self.controller, "_save_settings", side_effect=OSError("fixture read-only volume")):
            result = self.controller.action("settings.update", {"settings": changes})
        self.assertFalse(result["ok"])
        self.assertFalse(result["settings_saved"])
        self.assertFalse((self.state / "settings.json").exists())

    def test_oversize_and_symlink_settings_are_not_followed(self):
        self.state.mkdir()
        target = self.root / "outside"
        target.write_text('{"theme":"light"}')
        (self.state / "settings.json").symlink_to(target)
        other = MODULE.ToolboxController(self.state, self.sys, runner=self.runner)
        self.assertEqual("dark", other.settings["theme"])
        self.assertIsNotNone(other.settings_error)
        self.assertFalse(other.action("settings.update", {"settings": {"theme": "jade"}})["ok"])
        self.assertEqual('{"theme":"light"}', target.read_text())

    def test_brightness_sleep_wake_and_activity(self):
        result = self.controller.action("screen.brightness", {"value": 25})
        self.assertTrue(result["ok"])
        self.assertEqual("24000", (self.backlight / "brightness").read_text())
        self.assertFalse(self.controller.action("screen.brightness", {"value": True})["ok"])
        self.assertTrue(self.controller.action("screen.sleep", {})["ok"])
        self.assertEqual("4", (self.sys / "class/graphics/fb0/blank").read_text())
        self.assertTrue(self.controller.action("screen.wake", {})["ok"])
        self.assertEqual("0", (self.sys / "class/graphics/fb0/blank").read_text())
        self.assertEqual("24000", (self.backlight / "brightness").read_text())
        self.now = 5
        self.controller.action("screen.activity", {})
        self.assertEqual(5, self.controller.last_activity)

    def test_detect_zero_backlight_and_preexisting_blank_and_wake(self):
        self.write(self.backlight / "brightness", "0")
        self.assertFalse(self.controller.snapshot()["screen"]["asleep"])
        self.write(self.sys / "class/graphics/fb0/blank", "4")
        self.assertTrue(self.controller.snapshot()["screen"]["asleep"])
        self.assertTrue(self.controller.action("screen.wake", {})["ok"])
        self.assertEqual("0", (self.sys / "class/graphics/fb0/blank").read_text())
        self.assertGreater(int((self.backlight / "brightness").read_text()), 0)

    def test_sleep_stop_restores_pre_service_raw_screen_state(self):
        blank = self.sys / "class/graphics/fb0/blank"
        self.write(self.backlight / "brightness", "0")
        self.write(blank, "0")
        self.assertTrue(self.controller.action("screen.sleep", {})["ok"])
        self.assertEqual("4", blank.read_text())
        self.controller.stop()
        self.assertEqual("0", blank.read_text())
        self.assertEqual("0", (self.backlight / "brightness").read_text())
        self.assertFalse(self.controller.original_screen)

    def test_brightness_then_sleep_restores_original_values_not_intermediate(self):
        self.controller.action("screen.brightness", {"value": 25})
        self.controller.action("screen.sleep", {})
        self.controller.action("screen.wake", {})
        self.controller.action("screen.sleep", {})
        self.controller.stop()
        self.assertEqual("48000", (self.backlight / "brightness").read_text())
        self.assertEqual("0", (self.sys / "class/graphics/fb0/blank").read_text())

    def test_theme_only_saved_default_brightness_does_not_take_control_on_restart(self):
        self.controller.action("settings.update", {"settings": {"theme": "jade"}})
        other = MODULE.ToolboxController(self.state, self.sys, runner=self.runner, clock=lambda: self.now)
        before = self.files()
        other.start()
        other.tick()
        other.stop()
        self.assertEqual(before, self.files())
        self.assertFalse(other.original_screen)

    def test_unsupported_fan_never_writes(self):
        before = self.files()
        result = self.controller.action("fan.set", {"channel": "hwmon2:pwm3", "mode": "auto", "takeover": True})
        self.assertFalse(result["ok"])
        self.assertEqual(before, self.files())
        self.assertEqual([], self.calls)

    def test_fan_requires_explicit_mapping_and_restores_on_stop(self):
        folder = self.sys / "class/hwmon/hwmon2"
        self.write(folder / "name", "it8688")
        self.write(folder / "pwm3", "120")
        self.write(folder / "pwm3_enable", "2")
        self.assertFalse(self.controller.action("fan.set", {"channel": "hwmon2:pwm3", "mode": "auto"})["ok"])
        self.assertEqual("120", (folder / "pwm3").read_text())
        result = self.controller.action("fan.set", {"channel": "hwmon2:pwm3", "mode": "auto", "takeover": True})
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["state"]["fans"]["temperature_c"])
        self.assertEqual("255", (folder / "pwm3").read_text())
        self.assertEqual("1", (folder / "pwm3_enable").read_text())
        self.controller.stop()
        self.assertEqual("120", (folder / "pwm3").read_text())
        self.assertEqual("2", (folder / "pwm3_enable").read_text())

    def test_low_fan_rpm_zero_falls_back_full_speed_and_reports_error(self):
        folder = self.sys / "class/hwmon/hwmon2"
        for name, value in (("name", "it8688"), ("pwm3", "200"), ("pwm3_enable", "2"), ("fan3_input", "0")):
            self.write(folder / name, value)
        self.assertFalse(self.controller.action("fan.set", {"channel": "hwmon2:pwm3", "mode": "manual",
                                                            "value": 49, "takeover": True})["ok"])
        self.assertEqual("200", (folder / "pwm3").read_text())
        result = self.controller.action("fan.set", {"channel": "hwmon2:pwm3", "mode": "low", "takeover": True})
        self.assertFalse(result["ok"])
        self.assertIn("回退满速", result["error"])
        self.assertEqual("255", (folder / "pwm3").read_text())
        self.assertEqual("high", self.controller.snapshot()["fans"]["channels"][0]["mode"])

    def test_low_fan_without_rpm_explicitly_reports_unverifiable(self):
        folder = self.sys / "class/hwmon/hwmon2"
        for name, value in (("name", "it8688"), ("pwm3", "200"), ("pwm3_enable", "2")):
            self.write(folder / name, value)
        result = self.controller.action("fan.set", {"channel": "hwmon2:pwm3", "mode": "low", "takeover": True})
        self.assertTrue(result["ok"])
        channel = result["state"]["fans"]["channels"][0]
        self.assertEqual(60, channel["duty_percent"])
        self.assertIsNone(channel["rpm"])
        self.assertFalse(channel["rpm_verified"])
        self.assertIn("无法验证", channel["rpm_reason"])

    def test_manual_fan_that_stops_later_falls_back_full_speed_on_tick(self):
        folder = self.sys / "class/hwmon/hwmon2"
        for name, value in (("name", "it8688"), ("pwm3", "200"), ("pwm3_enable", "2"), ("fan3_input", "900")):
            self.write(folder / name, value)
        result = self.controller.action("fan.set", {"channel": "hwmon2:pwm3", "mode": "manual",
                                                     "value": 60, "takeover": True})
        self.assertTrue(result["ok"])
        self.assertEqual("153", (folder / "pwm3").read_text())
        self.write(folder / "fan3_input", "0")
        self.controller.tick()
        self.assertEqual("255", (folder / "pwm3").read_text())
        fans = self.controller.snapshot()["fans"]
        self.assertEqual("high", fans["channels"][0]["mode"])
        self.assertIn("RPM 读数为 0", fans["last_error"])
        self.assertIn("对应仍需确认", fans["channels"][0]["rpm_reason"])

    def test_saved_setting_brightness_failure_is_explicit(self):
        with mock.patch.object(self.controller, "_set_brightness", side_effect=OSError("read only")):
            result = self.controller.action("settings.update", {"settings": {"brightness": 70}})
        self.assertFalse(result["ok"])
        self.assertIn("设置已保存，但背光调整失败", result["error"])
        self.assertTrue(result["settings_saved"])
        self.assertTrue((self.state / "settings.json").is_file())

    def test_settings_oversize_is_rejected(self):
        self.state.mkdir()
        (self.state / "settings.json").write_bytes(b" " * 16385)
        other = MODULE.ToolboxController(self.state, self.sys, runner=self.runner)
        self.assertEqual("dark", other.settings["theme"])
        self.assertIsNotNone(other.settings_error)

    def test_idle_and_explicit_night_settings(self):
        result = self.controller.action("settings.update", {"settings": {"sleep_timeout": 10}})
        self.assertTrue(result["ok"])
        self.now = 9
        self.controller.action("screen.activity", {})
        self.now = 18
        self.controller.tick()
        self.assertFalse(self.controller.asleep)
        self.now = 19
        self.controller.tick()
        self.assertTrue(self.controller.asleep)
        self.controller.action("screen.wake", {})
        with mock.patch.object(self.controller, "_is_night", return_value=True):
            result = self.controller.action("settings.update", {"settings": {"night_enabled": True, "night_brightness": 15}})
        self.assertTrue(result["ok"])
        self.assertEqual("14400", (self.backlight / "brightness").read_text())

    def test_screen_saver_dims_and_restores_on_activity(self):
        result = self.controller.action("settings.update", {"settings": {
            "saver_timeout": 10, "saver_brightness": 30,
            "saver_content": "temperature", "sleep_timeout": 0,
        }})
        self.assertTrue(result["ok"], result)
        self.now = 9
        self.controller.tick()
        self.assertFalse(self.controller.saver)
        self.now = 10
        self.controller.tick()
        self.assertTrue(self.controller.saver)
        self.assertEqual(self.snapshot_saver(), True)
        self.assertEqual((self.backlight / "brightness").read_text(), "28800")
        self.controller.action("screen.activity", {})
        self.assertFalse(self.controller.saver)
        self.assertEqual(self.snapshot_saver(), False)
        self.assertEqual((self.backlight / "brightness").read_text(), "76800")

    def snapshot_saver(self):
        return self.controller.snapshot()["screen"]["saver"]

    def test_power_requires_confirmation_delays_and_cancels(self):
        self.assertFalse(self.controller.action("power.request", {"operation": "reboot", "confirmed": 1})["ok"])
        self.assertFalse(self.controller.action("power.request", {"operation": "shell", "confirmed": True})["ok"])
        self.assertTrue(self.controller.action("power.request", {"operation": "reboot", "confirmed": True})["ok"])
        self.now = 4
        self.controller.tick()
        self.assertEqual([], self.calls)
        self.assertFalse(self.controller.action("power.request", {"operation": "poweroff", "confirmed": True})["ok"])
        self.assertTrue(self.controller.action("power.cancel", {})["ok"])
        self.now = 10
        self.controller.tick()
        self.assertEqual([], self.calls)
        self.assertTrue(self.controller.action("power.request", {"operation": "poweroff", "confirmed": True})["ok"])
        self.now = 15
        self.controller.tick()
        self.assertEqual([["/usr/sbin/systemctl", "--no-block", "poweroff"]], self.calls)

    def test_power_failure_is_reported_without_retry(self):
        self.controller.runner = lambda argv: subprocess.CompletedProcess(argv, 1, "", "failed")
        self.controller.action("power.request", {"operation": "reboot", "confirmed": True})
        self.now = 5
        self.controller.tick()
        state = self.controller.snapshot()["power"]
        self.assertFalse(state["executing"])
        self.assertIn("failed", state["last_error"])
        self.assertIsNone(state["pending"])

    def test_stopped_controller_rejects_queued_actions_without_restarting(self):
        self.controller.start()
        self.controller.stop()
        before = self.files()
        for name, payload in (("power.request", {"operation": "reboot", "confirmed": True}),
                              ("screen.brightness", {"value": 50}),
                              ("settings.update", {"settings": {"theme": "jade"}})):
            result = self.controller.action(name, payload)
            self.assertFalse(result["ok"])
            self.assertIn("正在停止", result["error"])
        self.assertEqual([], self.calls)
        self.assertEqual(before, self.files())
        self.assertTrue(self.controller.stopped.is_set())
        self.assertFalse(self.controller.thread.is_alive())

    def rgb_fixture(self, buses=(0,), name="Synopsys DesignWare I2C adapter"):
        self.rgb_registers = {}
        for bus in buses:
            (self.dev / ("i2c-" + str(bus))).touch()
            self.write(self.sys / "class/i2c-dev" / ("i2c-" + str(bus)) / "name", name)
            self.rgb_registers[bus] = {1: 0x55, 0x90: 1, 0x91: 73, 0x92: 3, 0x93: 4, 0x94: 5}
        check = mock.patch.object(MODULE.stat, "S_ISCHR", return_value=True)
        check.start()
        self.addCleanup(check.stop)

        def runner(argv):
            self.calls.append(list(argv))
            bus, register = int(argv[2]), int(argv[4], 16)
            if Path(argv[0]).name == "i2cset":
                self.rgb_registers[bus][register] = int(argv[5], 16)
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, hex(self.rgb_registers[bus][register]), "")
        self.controller.runner = runner

    def test_rgb_automatically_uses_only_designware_fixed_address_nonforce_reads(self):
        self.rgb_fixture((7,))
        (self.dev / "i2c-2").touch()
        self.write(self.sys / "class/i2c-dev/i2c-2/name", "SMBus I801 adapter")
        result = self.controller.action("rgb.probe", {})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"]["rgb"]["bus"], 7)
        self.assertEqual(result["state"]["rgb"]["color"], "#030405")
        self.assertEqual([["/usr/sbin/i2cget", "-y", "7", "0x26", f"0x{register:02x}", "b"]
                          for register in (1, 0x90, 0x91, 0x92, 0x93, 0x94)], self.calls)
        self.assertTrue(all("-f" not in argv and Path(argv[0]).name == "i2cget" for argv in self.calls))

    def test_rgb_no_manual_bus_or_noncharacter_device_can_enable_control(self):
        self.rgb_fixture()
        self.assertFalse(self.controller.action("rgb.probe", {"bus": 0})["ok"])
        self.assertFalse(self.controller.action("rgb.set", {"bus": 0, "mode": 1, "color": "#FF0000"})["ok"])
        self.assertEqual([], self.calls)
        with mock.patch.object(MODULE.stat, "S_ISCHR", return_value=False):
            self.assertFalse(self.controller.action("rgb.probe", {})["ok"])
        self.assertEqual([], self.calls)

    def test_rgb_wrong_chip_or_multiple_matching_controllers_never_write(self):
        self.rgb_fixture((0, 1))
        self.assertFalse(self.controller.action("rgb.probe", {})["ok"])
        self.assertFalse(self.controller.snapshot()["capabilities"]["rgb"]["available"])
        self.rgb_registers[0][1] = self.rgb_registers[1][1] = 0x12
        self.assertFalse(self.controller.action("rgb.set", {"mode": 1, "color": "#00FF00"})["ok"])
        self.assertTrue(all(Path(argv[0]).name == "i2cget" for argv in self.calls))
        self.controller.runner = lambda argv: subprocess.CompletedProcess(argv, 1, "", "busy")
        self.assertFalse(self.controller.action("rgb.probe", {})["ok"])

    def test_rgb_brightness_independent_readback_and_repeated_target_skip_writes(self):
        self.rgb_fixture()
        target = {"mode": 3, "color": "#00FF00", "brightness": 40}
        result = self.controller.action("rgb.set", target)
        self.assertTrue(result["ok"], result)
        self.assertIn(["/usr/sbin/i2cset", "-y", "0", "0x26", "0x91", "0x66", "b"], self.calls)
        self.assertIn(["/usr/sbin/i2cset", "-y", "0", "0x26", "0x93", "0xff", "b"], self.calls)
        before = len([argv for argv in self.calls if Path(argv[0]).name == "i2cset"])
        self.assertTrue(self.controller.action("rgb.set", target)["ok"])
        self.assertEqual(before, len([argv for argv in self.calls if Path(argv[0]).name == "i2cset"]))
        self.assertEqual(result["state"]["rgb"]["brightness"], 40)

    def test_rgb_failed_write_revokes_capability_instead_of_claiming_success(self):
        self.rgb_fixture()
        original = self.controller.runner
        self.controller.runner = lambda argv: subprocess.CompletedProcess(argv, 1, "", "failed") if Path(argv[0]).name == "i2cset" else original(argv)
        self.assertFalse(self.controller.action("rgb.set", {"mode": 1, "color": "#00FF00"})["ok"])
        self.assertFalse(self.controller.snapshot()["capabilities"]["rgb"]["available"])

    def test_rgb_policy_ownership_restores_exact_bytes_or_preserves_external_change(self):
        self.rgb_fixture()
        owner = object()
        original = self.controller.lighting_claim(owner)
        actual = self.controller.lighting_apply(owner, {"mode": 1, "color": "#FF0000", "brightness": 40}, expected=original)
        self.assertFalse(self.controller.action("rgb.set", {"mode": 0, "color": "#000000"})["ok"])
        self.assertTrue(self.controller.lighting_release(owner, original, actual))
        self.assertEqual(self.controller.lighting_probe()["registers"], original["registers"])
        original = self.controller.lighting_claim(owner)
        actual = self.controller.lighting_apply(owner, {"mode": 1, "color": "#00FF00", "brightness": 40}, expected=original)
        self.rgb_registers[0][0x92] = 88
        self.assertFalse(self.controller.lighting_release(owner, original, actual))
        self.assertEqual(self.rgb_registers[0][0x92], 88)
        self.assertIsNone(self.controller._rgb_owner)

    def test_official_strategy_is_separate_from_pwm_and_requires_confirmation(self):
        native = mock.Mock()
        native.snapshot.return_value = {"available": True, "control_available": True, "mode": "auto", "mode_code": 5,
                                        "cpu_rpm": 1200, "system_rpm": None, "grade": 2}
        self.controller.histor = native
        state = self.controller.snapshot()
        self.assertTrue(state["capabilities"]["fans"]["available"])
        self.assertEqual([], state["fans"]["channels"])
        self.assertEqual("auto", state["fans"]["official"]["mode"])
        self.assertEqual(2, state["fans"]["official"]["grade"])
        for payload in ({"mode": "low"}, {"mode": "off", "confirmed": True},
                        {"mode": True, "confirmed": True}, {"mode": "low", "confirmed": 1},
                        {"mode": "low", "confirmed": True, "channel": "cpu"}):
            self.assertFalse(self.controller.action("fan.official", payload)["ok"])
        native.set_mode.assert_not_called()
        result = self.controller.action("fan.official", {"mode": "medium", "confirmed": True})
        self.assertTrue(result["ok"], result)
        native.set_mode.assert_called_once_with("medium", confirmed=True)
        self.assertEqual([], self.calls)

    def test_official_adapter_lifecycle_and_error_are_preserved(self):
        native = mock.Mock()
        native.snapshot.return_value = {"mode": None, "control_available": False}
        native.set_mode.side_effect = ValueError("原厂接口未就绪")
        self.controller.histor = native
        result = self.controller.action("fan.official", {"mode": "auto", "confirmed": True})
        self.assertFalse(result["ok"])
        self.assertIn("未就绪", result["error"])
        self.controller.start()
        native.start.assert_called_once_with()
        self.controller.stop()
        native.stop.assert_called_once_with()

    def test_curve_rejects_invalid_points_before_taking_ownership(self):
        folder = self.sys / "class/hwmon/hwmon2"
        for name, value in (("name", "it8688"), ("pwm3", "120"), ("pwm3_enable", "2")):
            self.write(folder / name, value)
        invalid = [[], [{"temperature": 40, "duty": 60}],
                   [{"temperature": 40, "duty": 60}, {"temperature": 40, "duty": 80}],
                   [{"temperature": 40, "duty": 80}, {"temperature": 70, "duty": 60}],
                   [{"temperature": 40, "duty": 49}, {"temperature": 70, "duty": 100}],
                   [{"temperature": True, "duty": 60}, {"temperature": 70, "duty": 100}],
                   [{"temperature": 40, "duty": 60, "extra": 1}, {"temperature": 70, "duty": 100}]]
        before = self.files()
        for curve in invalid:
            result = self.controller.action("fan.set", {"channel": "hwmon2:pwm3", "mode": "curve",
                                                        "curve": curve, "takeover": True})
            self.assertFalse(result["ok"], curve)
        self.assertEqual(before, self.files())
        self.assertFalse(self.controller.owned_fans)

    def test_curve_interpolates_and_fails_safe_on_hot_or_missing_sensor(self):
        folder = self.sys / "class/hwmon/hwmon2"
        for name, value in (("name", "it8688"), ("pwm3", "120"), ("pwm3_enable", "2"), ("fan3_input", "900")):
            self.write(folder / name, value)
        temperature = self.sys / "class/hwmon/hwmon1/temp1_input"
        self.write(temperature, "50000")
        curve = [{"temperature": 40, "duty": 60}, {"temperature": 70, "duty": 90}]
        result = self.controller.action("fan.set", {"channel": "hwmon2:pwm3", "mode": "curve",
                                                    "curve": curve, "takeover": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual(str(round(255 * .7)), (folder / "pwm3").read_text())
        self.assertEqual(curve, result["state"]["fans"]["channels"][0]["curve"])
        for value in ("80000", "invalid"):
            self.write(temperature, value)
            self.controller.tick()
            self.assertEqual("255", (folder / "pwm3").read_text())
        self.controller.stop()
        self.assertEqual("120", (folder / "pwm3").read_text())
        self.assertEqual("2", (folder / "pwm3_enable").read_text())


if __name__ == "__main__":
    unittest.main()

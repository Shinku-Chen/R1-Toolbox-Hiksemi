"""Temperature policies against fake native control, never real hardware."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fan_policy_under_test", ROOT / "runtime/fan_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def source(name, temperature, kind="cpu", status="ok", stamp=100, maximum=5):
    return {"id": name, "label": name, "kind": kind, "status": status,
            "temperature_c": temperature, "sampled_monotonic": stamp,
            "max_age_seconds": maximum, "critical_celsius": None}


def config():
    value = copy.deepcopy(policy.DEFAULT_CONFIG)
    value["channels"]["cpu"].update(mode="temperature", sources=["cpu", "disk1"])
    value["channels"]["system"].update(mode="temperature", sources=["disk1", "disk2"],
                                       quiet_below=30, normal_at=40, full_at=55)
    return value


class Adapter:
    def __init__(self):
        self.available = True
        self.calls = []
        self.failure = False
        self.channels = set()

    def capabilities(self):
        return {"available": self.available, "channels": ["cpu", "system"], "rpm_available": False}

    def start(self, channels):
        self.calls.append(("start", set(channels)))
        self.channels = set(channels)

    def write(self, targets):
        if self.failure:
            raise OSError("fixture lost control")
        self.calls.append(("write", dict(targets)))

    def release(self):
        self.calls.append(("release",))
        self.channels.clear()


class RulesTests(unittest.TestCase):
    def decide(self, value, temperatures, previous=None, now=100):
        return policy.decide(value, policy.observed_sources(temperatures, now), previous)

    def test_each_fan_uses_its_own_selected_maximum(self):
        settings = config()["channels"]
        rows = [source("cpu", 60), source("disk1", 35, "disk"), source("disk2", 45, "disk")]
        cpu = self.decide(settings["cpu"], rows)
        system = self.decide(settings["system"], rows)
        self.assertEqual((cpu["reference_c"], cpu["source_id"], cpu["level"]), (60, "cpu", "normal"))
        self.assertEqual((system["reference_c"], system["source_id"], system["level"]), (45, "disk2", "normal"))
        self.assertEqual(settings["system"]["normal_at"], 40)

    def test_rising_crosses_multiple_levels_immediately(self):
        settings = config()["channels"]["cpu"]
        for temperature, expected in ((40, "quiet"), (55, "normal"), (70, "full"), (78, "full")):
            with self.subTest(temperature=temperature):
                result = self.decide(settings, [source("cpu", temperature), source("disk1", 30, "disk")], "quiet")
                self.assertEqual(result["level"], expected)

    def test_three_thresholds_have_real_cooling_hysteresis(self):
        settings = config()["channels"]["cpu"]
        def at(value, prior):
            return self.decide(settings, [source("cpu", value), source("disk1", 30, "disk")], prior)["level"]
        self.assertEqual(at(69, "full"), "full")
        self.assertEqual(at(67, "full"), "full")
        self.assertEqual(at(66, "full"), "normal")
        self.assertEqual(at(45, "normal"), "normal")
        self.assertEqual(at(45, "quiet"), "quiet")
        self.assertEqual(at(40, "normal"), "quiet")

    def test_missing_required_source_cannot_be_ignored_or_become_zero(self):
        settings = config()["channels"]["system"]
        for row in (None, source("disk2", None, "disk", "unavailable"),
                    source("disk2", 25, "disk", stamp=0, maximum=90)):
            values = [source("cpu", 40), source("disk1", 25, "disk")]
            if row:
                values.append(row)
            self.assertEqual(self.decide(settings, values)["level"], "failsafe")

    def test_zero_is_a_real_temperature_and_future_timestamps_are_not_fresh(self):
        rows = policy.observed_sources([source("disk", 0, "disk")], 100)
        self.assertEqual(rows[0]["temperature_c"], 0)
        self.assertTrue(rows[0]["fresh"])
        future = policy.observed_sources([source("cpu", 50, stamp=101)], 100)[0]
        self.assertFalse(future["fresh"])
        self.assertEqual(future["status"], "stale")

    def test_manual_and_disk_only_selection_keep_unconditional_cpu_guard(self):
        settings = config()["channels"]["system"]
        for mode in ("temperature", "manual"):
            settings["mode"] = mode
            result = self.decide(settings, [source("cpu", 80), source("disk1", 20, "disk"), source("disk2", 21, "disk")])
            self.assertEqual(result["target_percent"], 100)
            self.assertEqual(result["level"], "failsafe")
            self.assertEqual(self.decide(settings, [source("disk1", 20, "disk"), source("disk2", 21, "disk")])["level"], "failsafe")

    def test_fresh_standby_is_excluded_but_stale_standby_is_not(self):
        settings = config()["channels"]["system"]
        values = [source("cpu", 45), source("disk1", None, "disk", "standby"), source("disk2", None, "disk", "standby")]
        result = self.decide(settings, values)
        self.assertEqual(result["level"], "quiet")
        self.assertIsNone(result["reference_c"])
        values[-1]["sampled_monotonic"] = 0
        self.assertEqual(self.decide(settings, values)["level"], "failsafe")

    def test_all_fields_are_validated_before_any_control(self):
        value = config()
        self.assertEqual(policy.validate_config(value), value)
        for changes in ({"normal_at": 40}, {"quiet_percent": 49}, {"normal_percent": 54},
                        {"manual_percent": True}, {"sources": []}, {"sources": ["x", "x"]},
                        {"full_at": float("nan")}, {"command": "anything"}):
            candidate = copy.deepcopy(value)
            candidate["channels"]["cpu"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                policy.validate_config(candidate)

    def test_optional_native_system_mode_preserves_schema_one_compatibility(self):
        legacy = config()
        self.assertEqual(policy.validate_config(legacy), legacy)
        self.assertNotIn("vendor_mode", policy.validate_config(legacy)["channels"]["system"])
        for mode in policy.VENDOR_MODES:
            candidate = copy.deepcopy(legacy)
            candidate["channels"]["system"]["vendor_mode"] = mode
            self.assertEqual(policy.validate_config(candidate), candidate)
        for channel, mode in (("cpu", "auto"), ("system", "stop"), ("system", 5), ("system", None)):
            candidate = copy.deepcopy(legacy)
            candidate["channels"][channel]["vendor_mode"] = mode
            with self.subTest(channel=channel, mode=mode), self.assertRaises(ValueError):
                policy.validate_config(candidate)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="r1-fan-policy-test-"))
        self.addCleanup(self.recycle)
        self.adapter = Adapter()
        self.rows = [source("cpu", 60), source("disk1", 35, "disk"), source("disk2", 45, "disk")]
        self.sources = mock.Mock()
        self.sources.snapshot.side_effect = lambda: copy.deepcopy(self.rows)
        self.controller = policy.FanPolicyController(self.directory, self.sources, self.adapter, clock=lambda: 100)

    def recycle(self):
        trash = Path.home() / ".Trash"
        if sys.platform == "darwin" and trash.is_dir():
            shutil.move(str(self.directory), str(trash / self.directory.name))
        else:
            print("测试临时目录已保留：", self.directory)

    def apply(self, value=None):
        result = self.controller.action("fan.policy.update", {"config": value or config(), "apply": True, "confirmed": True})
        if self.controller.worker:
            self.controller.worker.join(timeout=3)
            self.assertFalse(self.controller.worker.is_alive())
        return result

    def test_small_screen_quick_modes_change_one_channel_only(self):
        with mock.patch.object(self.controller, "_save"):
            result = self.controller.action("fan.policy.quick", {"channel": "cpu", "mode": "low"})
            self.assertTrue(result["ok"], result)
            self.controller.worker.join(timeout=3)
            cpu = self.controller.config["channels"]["cpu"]
            self.assertEqual((cpu["mode"], cpu["manual_percent"]), ("manual", 50))
            self.assertEqual(self.controller.config["channels"]["system"]["mode"], "vendor")
            self.assertFalse(self.controller.action("fan.policy.quick", {"channel": "cpu", "mode": "manual"})["ok"])
            pending = config()
            pending["channels"]["system"].update(mode="manual", manual_percent=100)
            self.controller.config = pending
            result = self.controller.action("fan.policy.quick", {"channel": "cpu", "mode": "low"})
            self.assertTrue(result["ok"], result)
            self.controller.worker.join(timeout=3)
            self.assertEqual(result["fan_policy"]["config"]["channels"]["system"]["mode"], "manual")
            self.assertEqual(self.controller.active_config["channels"]["system"]["mode"], "vendor")
            self.assertIn(("start", {"cpu"}), self.adapter.calls)
            result = self.controller.action("fan.policy.quick", {"channel": "cpu", "mode": "auto"})
            self.assertTrue(result["ok"], result)
            self.controller.worker.join(timeout=3)
            self.assertEqual(self.controller.config["channels"]["cpu"]["mode"], "vendor")
            result = self.controller.action("fan.policy.quick", {"channel": "cpu", "mode": "manual"})
            self.assertTrue(result["ok"], result)
            self.controller.worker.join(timeout=3)
            self.assertEqual(self.controller.config["channels"]["cpu"]["mode"], "temperature")
            self.assertEqual(result["fan_policy"]["config"]["channels"]["system"]["mode"], "manual")

    def test_save_without_apply_survives_reconstruction_without_hardware(self):
        self.adapter.available = False
        result = self.controller.action("fan.policy.update", {"config": config(), "apply": False})
        self.assertTrue(result["settings_saved"])
        self.assertEqual(self.adapter.calls, [])
        restored = policy.FanPolicyController(self.directory, self.sources, self.adapter, clock=lambda: 100)
        self.assertEqual(restored.config, config())
        self.assertFalse(restored.active_on_boot)
        self.assertTrue(restored.snapshot()["pending_changes"])

    def test_initial_temperature_service_failure_does_not_abort_startup(self):
        with mock.patch.object(self.sources, 'snapshot', side_effect=OSError('fixture unavailable')):
            self.controller.start()
            self.assertTrue(self.controller.thread.is_alive())
            self.assertFalse(self.controller.snapshot()['active'])
            self.assertIn('温度采集暂不可用', self.controller.snapshot()['last_error'])
            self.assertEqual(self.adapter.calls, [])
        self.controller.stop()

    @unittest.skipUnless(hasattr(__import__('os'), 'mkfifo'), 'requires POSIX FIFO fixture')
    def test_nonregular_configuration_is_rejected_without_open_blocking(self):
        import os
        os.mkfifo(self.directory / 'fan-policy.json')
        restored = policy.FanPolicyController(self.directory, self.sources, self.adapter, clock=lambda: 100)
        self.assertFalse(restored.active_on_boot)
        self.assertTrue(restored.settings_error)

    def test_activation_is_safe_full_before_first_sample_then_independent(self):
        self.assertTrue(self.apply()["ok"])
        self.assertEqual(self.adapter.calls[:2], [("start", {"cpu", "system"}), ("write", {"cpu": 255, "system": 255})])
        self.controller.tick()
        self.assertEqual(self.adapter.calls[-1], ("write", {"cpu": round(75 * 255 / 100), "system": round(75 * 255 / 100)}))
        state = self.controller.snapshot()
        self.assertEqual(state["channels"]["cpu"]["source_id"], "cpu")
        self.assertEqual(state["channels"]["system"]["source_id"], "disk2")
        self.assertIsNone(state["channels"]["cpu"]["rpm"])
        self.assertTrue(state["active_on_boot"])

    def test_cpu_only_keeps_system_vendor_and_percent_never_under_raw_128(self):
        value = config()
        value["channels"]["system"]["mode"] = "vendor"
        value["channels"]["cpu"].update(mode="manual", manual_percent=50)
        self.apply(value)
        self.controller.tick()
        self.assertEqual(self.adapter.calls[-1], ("write", {"cpu": 128}))
        self.assertFalse(self.controller.snapshot()["channels"]["system"]["active"])

    def test_saved_pending_rules_do_not_change_running_or_resume_policy(self):
        self.apply()
        original = copy.deepcopy(self.controller.active_config)
        pending = config()
        pending["channels"]["cpu"]["normal_percent"] = 90
        self.controller.action("fan.policy.update", {"config": pending, "apply": False})
        self.controller.tick()
        self.assertEqual(self.adapter.calls[-1][1]["cpu"], round(75 * 255 / 100))
        restored = policy.FanPolicyController(self.directory, self.sources, self.adapter, clock=lambda: 100)
        self.assertEqual(restored.config, pending)
        self.assertEqual(restored.active_config, original)
        self.assertTrue(restored.active_on_boot)

    def test_write_failure_releases_and_disables_automatic_retakeover(self):
        self.apply()
        self.adapter.failure = True
        self.controller.tick()
        self.assertEqual(self.adapter.calls[-1], ("release",))
        self.assertFalse(self.controller.snapshot()["active"])
        self.assertFalse(self.controller.active_on_boot)
        restored = policy.FanPolicyController(self.directory, self.sources, self.adapter, clock=lambda: 100)
        self.assertFalse(restored.active_on_boot)

    def test_restore_vendor_cancels_each_custom_channel(self):
        self.apply()
        result = self.controller.action("fan.policy.release", {"confirmed": True})
        self.controller.worker.join(timeout=3)
        self.assertTrue(result["ok"])
        self.assertEqual(self.adapter.calls[-1], ("release",))
        self.assertFalse(self.controller.active_on_boot)
        self.assertTrue(all(item["mode"] == "vendor" for item in self.controller.config["channels"].values()))

    def test_wrong_confirmation_invalid_config_and_failed_storage_never_start(self):
        self.assertFalse(self.controller.action("fan.policy.update", {"config": config(), "apply": True})["ok"])
        invalid = config()
        invalid["channels"]["cpu"]["quiet_percent"] = 0
        self.assertFalse(self.controller.action("fan.policy.update", {"config": invalid, "apply": True, "confirmed": True})["ok"])
        with mock.patch.object(self.controller, "_save", side_effect=OSError("fixture read-only")):
            result = self.controller.action("fan.policy.update", {"config": config(), "apply": True, "confirmed": True})
        self.assertFalse(result["settings_saved"])
        self.assertEqual(self.adapter.calls, [])

    def test_unknown_hardware_can_save_but_cannot_apply(self):
        self.adapter.available = False
        self.controller._refresh_capability()
        result = self.apply()
        self.assertTrue(result["settings_saved"])
        self.assertFalse(result["ok"])
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(json.loads((self.directory / "fan-policy.json").read_text())["config"], config())

    def test_snapshot_and_async_ack_do_not_wait_for_native_start_lock(self):
        entered, finish = threading.Event(), threading.Event()
        native_lock = threading.Lock()
        original_cap = self.adapter.capabilities
        def slow_start(channels):
            with native_lock:
                entered.set()
                self.assertTrue(finish.wait(2))
        def locked_cap():
            with native_lock:
                return original_cap()
        self.adapter.start = slow_start
        self.adapter.capabilities = locked_cap
        before = time.monotonic()
        result = self.controller.action("fan.policy.update", {"config": config(), "apply": True, "confirmed": True})
        self.assertTrue(entered.wait(1))
        self.assertTrue(result["pending"])
        self.assertTrue(self.controller.snapshot()["busy"])
        self.assertLess(time.monotonic() - before, 0.5)
        finish.set()
        self.controller.worker.join(timeout=2)

    def test_failed_recovery_never_claims_vendor_is_confirmed(self):
        self.apply()
        self.adapter.failure = True
        self.adapter.release = mock.Mock(side_effect=OSError("fixture recovery failure"))
        self.controller.tick()
        state = self.controller.snapshot()
        self.assertFalse(state["active"])
        self.assertTrue(all("未确认" in item["reason"] for item in state["channels"].values()))

    def test_slow_native_probe_cannot_extend_cpu_sample_freshness(self):
        self.apply()
        now = [100]
        self.controller.clock = lambda: now[0]
        def slow_cap():
            now[0] = 110
            return {"available": True}
        self.adapter.capabilities = slow_cap
        self.controller.tick()
        self.assertEqual(self.adapter.calls[-1][1], {"cpu": 255, "system": 255})
        self.assertEqual(self.controller.channels["cpu"]["level"], "failsafe")

    def test_release_disarms_resume_before_its_pending_worker_completes(self):
        self.apply()
        finish = threading.Event()
        self.adapter.release = lambda: finish.wait(2)
        result = self.controller.action("fan.policy.release", {"confirmed": True})
        self.assertTrue(result["pending"])
        data = json.loads((self.directory / "fan-policy.json").read_text())
        self.assertFalse(data["resume_on_boot"])
        finish.set()
        self.controller.worker.join(timeout=3)


class NativeModeTests(unittest.TestCase):
    """Official policy changes use the same serialized ownership transaction."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="r1-fan-native-modes-"))
        self.adapter = Adapter()
        self.histor = mock.Mock()
        self.mode = "low"
        self.histor.snapshot.side_effect = lambda: {
            "available": True, "control_available": True, "mode": self.mode, "scope": "system_fan"}
        self.histor.set_mode.side_effect = self.set_mode
        sources = mock.Mock()
        sources.snapshot.return_value = [source("cpu", 40)]
        self.controller = policy.FanPolicyController(self.directory, sources, self.adapter,
                                                    histor=self.histor, clock=lambda: 100)
        # These tests exercise ownership/RPC sequencing rather than directory
        # fsync portability; persistent-file behavior has separate regressions.
        self.controller._save = mock.Mock()
        self.addCleanup(self.controller.stop)

    def set_mode(self, mode, confirmed=False):
        self.assertIs(confirmed, True)
        self.assertFalse(self.adapter.channels, "official RPC requires every native channel to be released")
        self.adapter.calls.append(("official", mode))
        self.mode = mode
        return self.histor.snapshot()

    def apply(self, value):
        result = self.controller.action("fan.policy.update", {"config": value, "apply": True, "confirmed": True})
        if self.controller.worker is not None:
            self.controller.worker.join(timeout=4)
            self.assertFalse(self.controller.worker.is_alive())
        self.assertTrue(result["ok"], result)
        return self.controller.snapshot()

    def native_config(self, cpu="vendor", mode="high"):
        value = copy.deepcopy(policy.DEFAULT_CONFIG)
        value["channels"]["cpu"]["mode"] = cpu
        value["channels"]["system"]["vendor_mode"] = mode
        return value

    def test_snapshot_uses_cache_without_rpc_and_missing_adapter_is_explicit(self):
        state = self.controller.snapshot()
        self.assertEqual(state["official"]["mode"], "low")
        self.histor.set_mode.assert_not_called()
        self.controller.histor = None
        unavailable = self.controller.snapshot()["official"]
        self.assertFalse(unavailable["available"])
        self.assertFalse(unavailable["control_available"])

    def test_native_system_only_releases_before_confirmed_set_and_does_not_resume_on_boot(self):
        self.adapter.available = False
        self.controller._refresh_capability()
        state = self.apply(self.native_config())
        self.assertEqual(self.adapter.calls, [("release",), ("official", "high")])
        self.assertFalse(state["active"])
        self.assertFalse(state["active_on_boot"])
        self.assertFalse(state["pending_changes"])
        self.assertIsNone(state["last_error"])

    def test_cpu_only_and_vendor_system_can_coexist_after_system_is_released(self):
        value = self.native_config("manual")
        state = self.apply(value)
        self.assertEqual(self.adapter.calls, [("release",), ("official", "high"),
                                             ("start", {"cpu"}), ("write", {"cpu": 255})])
        self.assertTrue(state["channels"]["cpu"]["active"])
        self.assertFalse(state["channels"]["system"]["active"])
        self.assertTrue(state["active_on_boot"])
        self.assertEqual(self.controller.active_config, value)

    def test_official_rpc_waits_until_all_channel_recovery_has_returned_success(self):
        entered, finish = threading.Event(), threading.Event()
        self.adapter.channels = {"cpu", "system"}
        original_release = self.adapter.release
        def pending_release():
            entered.set()
            self.assertTrue(finish.wait(3))
            original_release()
        self.adapter.release = pending_release
        result = self.controller.action("fan.policy.update", {"config": self.native_config("manual"),
                                        "apply": True, "confirmed": True})
        self.assertTrue(result["ok"])
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.adapter.channels, {"cpu", "system"})
        self.histor.set_mode.assert_not_called()
        self.assertEqual(self.adapter.calls, [])
        finish.set()
        self.controller.worker.join(timeout=3)
        self.assertFalse(self.controller.worker.is_alive())
        self.assertEqual(self.adapter.calls, [("release",), ("official", "high"),
                                             ("start", {"cpu"}), ("write", {"cpu": 255})])

    def test_manual_system_retains_native_fallback_without_applying_it(self):
        value = self.native_config(mode="medium")
        value["channels"]["system"]["mode"] = "manual"
        self.apply(value)
        self.histor.set_mode.assert_not_called()
        self.assertEqual(self.controller.active_config["channels"]["system"]["vendor_mode"], "medium")
        self.controller.action("fan.policy.release", {"confirmed": True})
        self.controller.worker.join(timeout=3)
        self.assertEqual(self.adapter.calls[-1], ("release",))
        self.assertNotIn("vendor_mode", self.controller.config["channels"]["system"])
        self.histor.set_mode.assert_not_called()

    def test_legacy_config_without_native_mode_preserves_actual_vendor_choice(self):
        self.apply(copy.deepcopy(policy.DEFAULT_CONFIG))
        self.assertEqual(self.mode, "low")
        self.histor.set_mode.assert_not_called()
        self.assertNotIn("vendor_mode", self.controller.active_config["channels"]["system"])

    def test_official_action_uses_active_cpu_rule_instead_of_unsaved_cpu_draft(self):
        active = self.native_config("manual", "low")
        self.apply(active)
        draft = copy.deepcopy(active)
        draft["channels"]["cpu"]["manual_percent"] = 99
        self.controller.action("fan.policy.update", {"config": draft, "apply": False})
        result = self.controller.action("fan.official", {"mode": "medium", "confirmed": True})
        self.assertTrue(result["ok"], result)
        self.controller.worker.join(timeout=3)
        self.assertEqual(self.controller.config["channels"]["cpu"]["manual_percent"], 99)
        self.assertEqual(self.controller.active_config["channels"]["cpu"], active["channels"]["cpu"])
        self.assertEqual(self.controller.snapshot()["official"]["mode"], "medium")
        self.assertTrue(self.controller.snapshot()["pending_changes"])

    def test_official_action_requires_exact_confirmation_and_known_mode(self):
        for payload in ({"mode": "high"}, {"mode": "stop", "confirmed": True},
                        {"mode": "low", "confirmed": 1}, {"mode": "auto", "confirmed": True, "channel": "cpu"}):
            self.assertFalse(self.controller.action("fan.official", payload)["ok"])
        self.assertEqual(self.adapter.calls, [])
        self.histor.set_mode.assert_not_called()

    def test_unconfirmed_recovery_blocks_official_until_release_is_confirmed(self):
        self.controller.channels["system"]["reason"] = "原厂恢复未确认"
        result = self.controller.action("fan.official", {"mode": "high", "confirmed": True})
        self.assertFalse(result["ok"])
        self.assertIn("原厂恢复尚未确认", result["error"])
        self.assertEqual(self.adapter.calls, [])
        self.histor.set_mode.assert_not_called()
        result = self.controller.action("fan.policy.release", {"confirmed": True})
        self.assertTrue(result["ok"])
        self.controller.worker.join(timeout=3)
        self.assertTrue(self.controller.snapshot()["recovery_confirmed"])
        result = self.controller.action("fan.official", {"mode": "high", "confirmed": True})
        self.assertTrue(result["ok"])
        self.controller.worker.join(timeout=3)
        self.assertEqual(self.adapter.calls, [("release",), ("release",), ("official", "high")])

    def test_explicit_native_update_cannot_bypass_unconfirmed_recovery(self):
        self.controller.channels["cpu"]["reason"] = "控制回执过期，恢复状态待确认"
        result = self.controller.action("fan.policy.update", {"config": self.native_config(),
                                        "apply": True, "confirmed": True})
        self.assertFalse(result["ok"])
        self.assertTrue(result["settings_saved"])
        self.assertEqual(self.adapter.calls, [])
        self.histor.set_mode.assert_not_called()

    def test_single_channel_target_is_constructed_under_state_lock(self):
        entered = threading.Event()
        results = []
        def official_action():
            entered.set()
            results.append(self.controller.action("fan.official", {"mode": "high", "confirmed": True}))
        with self.controller.lock:
            action = threading.Thread(target=official_action)
            action.start()
            self.assertTrue(entered.wait(1))
            latest = copy.deepcopy(policy.DEFAULT_CONFIG["channels"]["cpu"])
            latest.update(mode="manual", manual_percent=88)
            self.controller.active_config["channels"]["cpu"] = latest
        action.join(timeout=2)
        self.assertFalse(action.is_alive())
        self.assertTrue(results[0]["ok"], results)
        self.controller.worker.join(timeout=3)
        self.assertEqual(self.controller.active_config["channels"]["cpu"], latest)

    def test_external_vendor_change_sets_pending_without_background_overwrite(self):
        self.apply(self.native_config("manual", "low"))
        self.mode = "high"
        self.histor.set_mode.reset_mock()
        self.controller.tick()
        self.assertTrue(self.controller.snapshot()["pending_changes"])
        self.histor.set_mode.assert_not_called()
        self.controller.action("fan.policy.quick", {"channel": "cpu", "mode": "low"})
        self.controller.worker.join(timeout=3)
        self.assertEqual(self.mode, "high")
        self.histor.set_mode.assert_not_called()

    def test_release_and_legacy_system_auto_preserve_newer_external_native_selection(self):
        for action, payload in (("fan.policy.release", {"confirmed": True}),
                                ("fan.policy.quick", {"channel": "system", "mode": "auto"})):
            with self.subTest(action=action):
                self.apply(self.native_config("manual", "low"))
                self.mode = "high"
                self.histor.set_mode.reset_mock()
                self.controller.action(action, payload)
                self.controller.worker.join(timeout=3)
                self.assertEqual(self.mode, "high")
                self.histor.set_mode.assert_not_called()
                self.assertNotIn("vendor_mode", self.controller.config["channels"]["system"])

    def test_startup_restores_only_independent_cpu_and_preserves_external_vendor_mode(self):
        self.controller.config = self.controller.active_config = self.native_config("manual", "low")
        self.controller.active_on_boot = True
        self.mode = "high"
        self.controller.start()
        self.controller.worker.join(timeout=3)
        self.assertTrue(self.controller.snapshot()["channels"]["cpu"]["active"])
        self.assertTrue(self.controller.snapshot()["pending_changes"])
        self.histor.set_mode.assert_not_called()

    def test_startup_of_vendor_only_mode_does_not_launch_native_or_send_rpc(self):
        self.controller.config = self.controller.active_config = self.native_config()
        self.controller.start()
        self.assertEqual(self.adapter.calls, [])
        self.assertIsNone(self.controller.worker)
        self.histor.set_mode.assert_not_called()

    def test_official_failure_releases_cpu_and_never_marks_target_applied(self):
        self.histor.set_mode.side_effect = ValueError("fixture rejected ACK")
        state = self.apply(self.native_config("manual"))
        self.assertEqual(self.adapter.calls[-1], ("release",))
        self.assertFalse(state["active"])
        self.assertFalse(state["active_on_boot"])
        self.assertTrue(state["pending_changes"])
        self.assertIn("fixture rejected ACK", state["last_error"])
        self.assertNotEqual(self.controller.active_config, self.controller.config)
        self.assertFalse(any(call[0] == "start" for call in self.adapter.calls))

    def test_official_readback_mismatch_cannot_claim_success(self):
        self.histor.set_mode.side_effect = lambda mode, confirmed: {"mode": "low"}
        state = self.apply(self.native_config("manual", "high"))
        self.assertFalse(state["active"])
        self.assertTrue(state["pending_changes"])
        self.assertIn("设置结果未能确认", state["last_error"])

    def test_failed_official_recovery_remains_unconfirmed(self):
        self.histor.set_mode.side_effect = ValueError("fixture rejected ACK")
        with mock.patch.object(self.adapter, "release", side_effect=RuntimeError("fixture recovery failure")):
            state = self.apply(self.native_config("manual"))
        self.assertFalse(state["recovery_confirmed"])
        self.assertFalse(state["active"])
        self.histor.set_mode.assert_not_called()
        self.assertFalse(any(call[0] == "start" for call in self.adapter.calls))

    def test_missing_official_adapter_does_not_report_native_selection_as_applied(self):
        self.controller.histor = None
        state = self.apply(self.native_config())
        self.assertTrue(state["pending_changes"])
        self.assertIn("原厂风扇接口不可用", state["last_error"])

    def test_slow_rpc_runs_without_native_guard_and_restarts_selected_cpu_rule_afterwards(self):
        entered, finish = threading.Event(), threading.Event()
        active = self.native_config("manual", "low")
        active["channels"]["cpu"]["manual_percent"] = 83
        self.apply(active)
        self.adapter.calls.clear()
        def blocked_set(mode, confirmed=False):
            self.assertFalse(self.adapter.channels)
            entered.set()
            self.assertTrue(finish.wait(4))
            self.assertFalse(self.adapter.channels)
            return self.set_mode(mode, confirmed)
        self.histor.set_mode.side_effect = blocked_set
        result = self.controller.action("fan.official", {"mode": "high", "confirmed": True})
        self.assertTrue(result["ok"])
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.controller.snapshot()["busy"])
        self.assertFalse(self.adapter.channels)
        # A normal sample cannot write or restart ownership while a slow
        # official RPC is in flight under the shared control lock.
        self.controller.tick()
        self.assertEqual(self.adapter.calls, [("release",)])
        finish.set()
        self.controller.worker.join(timeout=3)
        self.assertFalse(self.controller.worker.is_alive())
        self.assertIsNone(self.controller.snapshot()["last_error"])
        self.assertEqual(self.adapter.calls, [("release",), ("official", "high"),
                                             ("start", {"cpu"}), ("write", {"cpu": 255})])
        self.assertEqual(self.controller.active_config["channels"]["cpu"], active["channels"]["cpu"])
        self.controller.tick()
        self.assertEqual(self.adapter.calls[-1], ("write", {"cpu": round(83 * 255 / 100)}))

    def test_cpu_restart_failure_after_official_ack_does_not_report_completed_handoff(self):
        self.adapter.start = mock.Mock(side_effect=RuntimeError("fixture CPU handoff rejected"))
        state = self.apply(self.native_config("manual"))
        self.assertEqual(self.mode, "high")
        self.assertFalse(state["active"])
        self.assertFalse(state["active_on_boot"])
        self.assertIn("fixture CPU handoff rejected", state["last_error"])
        self.assertEqual(self.adapter.calls[-1], ("release",))


if __name__ == "__main__":
    unittest.main()

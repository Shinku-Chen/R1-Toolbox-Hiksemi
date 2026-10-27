"""Real controller/sysfs/persistence with I2C command substitutes; no NAS I/O."""
import copy
import datetime
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


policy = load('light_policy_fixture', ROOT / 'runtime/light_policy.py')
toolbox = load('light_toolbox_fixture', ROOT / 'runtime/toolbox.py')
temperature = load('light_temperature_fixture', ROOT / 'runtime/temperature_sources.py')


class LightTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='r1-light-fixture-'))
        self.sys, self.dev, self.state = self.root / 'sys', self.root / 'dev', self.root / 'data'
        self.sys.mkdir()
        self.dev.mkdir()
        (self.dev / 'i2c-7').touch()
        self.write(self.sys / 'class/i2c-dev/i2c-7/name', 'Synopsys DesignWare I2C adapter')
        self.write(self.sys / 'class/hwmon/hwmon0/name', 'coretemp')
        self.cpu_path = self.sys / 'class/hwmon/hwmon0/temp1_input'
        self.write(self.cpu_path, '40000')
        self.write(self.cpu_path.with_name('temp1_label'), 'Package id 0')
        self.now, self.wall = 100.0, datetime.datetime(2026, 10, 28, 12, 0)
        self.calls = []
        self.registers = {1: 0x55, 0x90: 3, 0x91: 73, 0x92: 25, 0x93: 35, 0x94: 45}
        self.original = dict(self.registers)
        finder = mock.patch.object(toolbox.shutil, 'which', side_effect=lambda name: '/fixture/' + name)
        checker = mock.patch.object(toolbox.stat, 'S_ISCHR', return_value=True)
        finder.start()
        checker.start()
        self.addCleanup(finder.stop)
        self.addCleanup(checker.stop)
        self.controller = toolbox.ToolboxController(self.state, self.sys, self.runner, self.dev, lambda: self.now)
        self.sources = temperature.TemperatureSources(None, self.sys, lambda: self.now)
        self.light = self.make()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.light.stop()
        self.controller.stop()
        trash = Path.home() / '.Trash'
        if sys.platform == 'darwin' and trash.is_dir():
            shutil.move(str(self.root), str(trash / self.root.name))

    def make(self):
        return policy.LightPolicyController(self.controller, self.sources, self.state,
                                             clock=lambda: self.now, wall_clock=lambda: self.wall)

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def runner(self, argv):
        self.calls.append(list(argv))
        self.assertIn(Path(argv[0]).name, ('i2cget', 'i2cset'))
        self.assertEqual(argv[1:4], ['-y', '7', '0x26'])
        register = int(argv[4], 16)
        if Path(argv[0]).name == 'i2cset':
            self.registers[register] = int(argv[5], 16)
            return subprocess.CompletedProcess(argv, 0, '', '')
        return subprocess.CompletedProcess(argv, 0, hex(self.registers[register]), '')

    def writes(self):
        return [argv for argv in self.calls if Path(argv[0]).name == 'i2cset']

    def config(self, mode='manual'):
        value = copy.deepcopy(policy.DEFAULT_CONFIG)
        value.update(enabled=True, mode=mode)
        value['sources'] = [self.sources.snapshot()[0]['id']]
        value['manual'] = {'mode': 1, 'color': '#A02030', 'brightness': 50}
        value['stops'] = [{'temperature': 20, 'color': '#000000'}, {'temperature': 60, 'color': '#FFFFFF'}]
        return value

    def apply(self, config=None):
        result = self.light.action('light.policy.update', {'config': config or self.config(), 'apply': True})
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['settings_saved'])
        self.assertFalse(result['applied'])
        self.light.worker.join(timeout=2)
        self.assertFalse(self.light.worker.is_alive())
        return self.light.snapshot()

    def test_default_start_only_automatically_reads_and_failure_does_not_loop(self):
        self.light.start()
        deadline = time.monotonic() + 2
        while not self.calls and time.monotonic() < deadline:
            time.sleep(.01)
        self.light.stop()
        self.assertTrue(self.calls)
        self.assertEqual([], self.writes())
        self.assertEqual(self.registers, self.original)
        self.assertFalse((self.state / 'light-policy.json').exists())
        self.assertFalse(self.light.snapshot()['active'])

    def test_failed_default_probe_never_becomes_a_background_retry_or_write(self):
        self.controller.runner = lambda argv: self.calls.append(list(argv)) or subprocess.CompletedProcess(argv, 1, '', 'fixture absent')
        self.light.start()
        deadline = time.monotonic() + 2
        while not self.calls and time.monotonic() < deadline:
            time.sleep(.01)
        before = len(self.calls)
        for _ in range(3):
            self.light.tick()
        self.assertEqual(len(self.calls), before)
        self.assertEqual([], self.writes())
        self.assertFalse(self.light.snapshot()['capability']['available'])

    def test_save_only_actual_volume_and_reconstruct_never_applies(self):
        config = self.config('temperature')
        result = self.light.action('light.policy.update', {'config': config, 'apply': False})
        self.assertTrue(result['settings_saved'])
        self.assertFalse(result['pending'])
        self.assertEqual([], self.calls)
        saved = json.loads((self.state / 'light-policy.json').read_text())
        self.assertEqual(saved['config'], config)
        self.assertFalse(saved['resume_on_boot'])
        restored = self.make()
        self.assertEqual(restored.snapshot()['config'], config)
        self.assertFalse(restored.snapshot()['active_on_boot'])
        restored.start()
        restored.stop()
        self.assertEqual([], self.writes())

    def test_manual_apply_readback_conflict_and_exact_raw_restore(self):
        state = self.apply()
        self.assertTrue(state['active'] and state['applied'] and state['active_on_boot'])
        self.assertEqual(self.registers[0x91], 128)
        self.assertEqual(self.controller.snapshot()['rgb']['brightness'], 50)
        self.assertFalse(self.controller.action('rgb.set', {'mode': 0, 'color': '#000000'})['ok'])
        self.light.stop()
        self.assertEqual(self.registers, self.original)
        self.assertFalse(self.light.snapshot()['applied'])

    def test_only_successfully_applied_enabled_config_resumes_after_reconstruction(self):
        config = self.config()
        self.apply(config)
        self.light.stop()
        restored = self.make()
        try:
            self.assertTrue(restored.resume_on_boot)
            restored.start()
            deadline = time.monotonic() + 2
            while not restored.snapshot()['active'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(restored.snapshot()['active'])
            self.assertEqual(restored.snapshot()['config'], config)
            self.assertEqual(restored.snapshot()['target'], config['manual'])
        finally:
            restored.stop()

    def test_temperature_actual_sysfs_max_and_color_interpolation(self):
        self.write(self.cpu_path.with_name('temp2_input'), '50000')
        config = self.config('temperature')
        config['sources'] = [row['id'] for row in self.sources.snapshot()]
        self.now += 1
        state = self.apply(config)
        self.assertEqual(state['reference_c'], 50)
        self.assertEqual(state['target']['color'], '#BFBFBF')
        self.assertEqual(state['runtime']['source_id'], next(row['id'] for row in self.sources.snapshot() if row['temperature_c'] == 50))
        self.assertTrue(all(row['fresh'] for row in state['sources']))

    def test_snapshot_expires_temperature_decision_without_tick_or_hardware_reads(self):
        state = self.apply(self.config('temperature'))
        self.assertTrue(state['applied'])
        last_requested, stored_decision = state['target'], copy.deepcopy(self.light.decision)
        before = len(self.calls)
        self.now += 4
        with mock.patch.object(self.sources, 'snapshot', side_effect=AssertionError('snapshot must be cached')):
            state = self.light.snapshot()
        self.assertEqual(len(self.calls), before)
        self.assertFalse(state['sources'][0]['fresh'])
        self.assertFalse(state['applied'] or state['runtime']['applied'])
        for value in (state, state['runtime']):
            self.assertIsNone(value['reference_c'])
            self.assertIsNone(value['source_id'])
            self.assertIsNone(value['source_label'])
            self.assertIn('等待下一轮低亮保护确认', value['reason'])
            self.assertEqual(value['last_requested_target'], last_requested)
            self.assertEqual(value['target'], last_requested)
        self.assertEqual(self.light.decision, stored_decision)

    def test_snapshot_missing_or_invalid_required_temperature_cannot_claim_applied(self):
        self.apply(self.config('temperature'))
        healthy = copy.deepcopy(self.light.catalog[0])
        for changes in (None, {'status': 'unavailable'}, {'temperature_c': None},
                        {'temperature_c': float('nan')}, {'temperature_c': 151}):
            with self.subTest(changes=changes):
                self.light.catalog = [] if changes is None else [{**healthy, **changes}]
                state = self.light.snapshot()
                self.assertFalse(state['applied'])
                self.assertIsNone(state['reference_c'])
                self.assertIsNone(state['runtime']['source_age_seconds'])

    def test_snapshot_fresh_standby_is_excluded_without_invalidating_confirmed_decision(self):
        config = self.config('temperature')
        cpu = self.sources.snapshot()[0]
        standby = {'id': 'disk:standby', 'label': 'Sleeping disk', 'status': 'standby',
                   'temperature_c': None, 'sampled_monotonic': self.now, 'max_age_seconds': 90}
        config['sources'].append(standby['id'])
        with mock.patch.object(self.sources, 'snapshot', return_value=[cpu, standby]):
            self.apply(config)
        self.now += 2
        state = self.light.snapshot()
        self.assertTrue(state['applied'])
        self.assertEqual(state['reference_c'], 40)
        self.assertFalse(state['fallback'])
        config['sources'] = [standby['id']]
        with mock.patch.object(self.sources, 'snapshot', return_value=[standby]):
            self.apply(config)
        state = self.light.snapshot()
        self.assertTrue(state['applied'] and state['fallback'])
        self.assertIsNone(state['reference_c'])
        self.assertEqual(state['target']['color'], '#FFAA00')
        self.now += 89
        self.assertFalse(self.light.snapshot()['applied'])

    def test_stale_temperature_catalog_does_not_invalidate_manual_lighting(self):
        self.apply()
        self.now += 4
        state = self.light.snapshot()
        self.assertFalse(state['sources'][0]['fresh'])
        self.assertTrue(state['applied'])

    def test_missing_stale_and_all_standby_have_low_brightness_warning(self):
        config = self.config('temperature')
        cpu = config['sources'][0]
        for row in (None, {'id': cpu, 'status': 'ok', 'temperature_c': 40,
                          'sampled_monotonic': 0, 'max_age_seconds': 3},
                    {'id': cpu, 'status': 'standby', 'temperature_c': None,
                     'sampled_monotonic': 100, 'max_age_seconds': 90}):
            decision = policy.decide(config, [row] if row else [], 100, self.wall)
            self.assertTrue(decision['fallback'])
            self.assertEqual(decision['target'], {'mode': 1, 'color': '#FFAA00', 'brightness': 20})

    def test_zero_and_negative_temperature_are_values_not_missing(self):
        config = self.config('temperature')
        for value in (0, -5):
            row = {'id': config['sources'][0], 'status': 'ok', 'temperature_c': value,
                   'sampled_monotonic': 100, 'max_age_seconds': 3}
            result = policy.decide(config, [row], 100, self.wall)
            self.assertFalse(result['fallback'])
            self.assertEqual(result['reference_c'], value)

    def test_midnight_schedule_caps_then_restores_mode_brightness(self):
        config = self.config()
        config['schedule'].update(enabled=True, start='22:30', end='06:30', brightness=15)
        self.wall = self.wall.replace(hour=23)
        self.assertTrue(self.apply(config)['schedule_active'])
        self.assertEqual(self.light.target['brightness'], 15)
        self.wall = self.wall.replace(hour=5)
        self.light.tick()
        self.assertTrue(self.light.snapshot()['runtime']['schedule_active'])
        self.wall = self.wall.replace(hour=7)
        self.light.tick()
        self.assertFalse(self.light.snapshot()['schedule_active'])
        self.assertEqual(self.light.target['brightness'], 50)

    def test_same_target_ticks_do_not_write_and_external_change_is_preserved(self):
        self.apply()
        count = len(self.writes())
        for _ in range(4):
            self.now += 1
            self.light.tick()
        self.assertEqual(len(self.writes()), count)
        self.registers[0x92] = 77
        self.now += 31
        self.light.tick()
        self.assertEqual(self.registers[0x92], 77)
        self.assertFalse(self.light.snapshot()['active'])
        self.assertFalse(self.light.snapshot()['active_on_boot'])
        self.assertIsNotNone(self.light.snapshot()['last_error'])

    def test_save_only_draft_does_not_change_running_or_resume_config(self):
        original = self.config()
        self.apply(original)
        draft = copy.deepcopy(original)
        draft['manual']['color'] = '#FFFFFF'
        self.light.action('light.policy.update', {'config': draft, 'apply': False})
        self.light.tick()
        self.assertEqual(self.light.snapshot()['target']['color'], '#A02030')
        self.assertFalse(self.light.snapshot()['applied'])
        restored = self.make()
        self.assertEqual(restored.config, draft)
        self.assertEqual(restored.active_config, original)
        self.assertTrue(restored.resume_on_boot)

    def test_explicit_disable_disarms_persisted_resume_and_restores(self):
        self.apply()
        config = self.config()
        config['enabled'] = False
        result = self.light.action('light.policy.update', {'config': config, 'apply': True})
        self.light.worker.join(2)
        self.assertTrue(result['settings_saved'])
        self.assertTrue(self.light.snapshot()['applied'])
        self.assertFalse(self.make().resume_on_boot)
        self.assertEqual(self.registers, self.original)

    def test_unknown_device_saves_but_never_writes_or_resumes(self):
        self.registers[1] = 0x12
        state = self.apply()
        self.assertFalse(state['active'] or state['applied'] or state['active_on_boot'])
        self.assertTrue(state['settings_saved'])
        self.assertEqual([], self.writes())

    def test_same_value_storage_failure_is_not_success_and_never_touches_i2c(self):
        with mock.patch.object(self.light, '_save', side_effect=OSError('fixture read only')):
            result = self.light.action('light.policy.update', {'config': copy.deepcopy(policy.DEFAULT_CONFIG), 'apply': True})
        self.assertFalse(result['ok'] or result['settings_saved'])
        self.assertEqual([], self.calls)

    def test_hardware_readback_failure_never_claims_applied_or_restore_success(self):
        runner = self.controller.runner
        self.controller.runner = lambda argv: subprocess.CompletedProcess(argv, 1, '', 'fixture fail') if Path(argv[0]).name == 'i2cset' else runner(argv)
        state = self.apply()
        self.assertFalse(state['applied'] or state['active'])
        self.assertIsNotNone(state['last_error'])
        self.assertFalse(self.make().resume_on_boot)

    def test_ack_without_changed_register_readback_is_not_applied(self):
        runner = self.controller.runner
        self.controller.runner = lambda argv: subprocess.CompletedProcess(argv, 0, '', '') if Path(argv[0]).name == 'i2cset' else runner(argv)
        state = self.apply()
        self.assertFalse(state['applied'] or state['active'] or state['active_on_boot'])
        self.assertEqual(self.registers, self.original)
        self.assertIsNotNone(state['last_error'])

    def test_missing_source_does_not_silently_use_the_other_hottest_source(self):
        config = self.config('temperature')
        config['sources'].append('disk:missing')
        rows = self.sources.snapshot()
        result = policy.decide(config, rows, self.now, self.wall)
        self.assertTrue(result['fallback'])
        self.assertEqual(result['reference_c'], 40)
        self.assertEqual(result['target']['color'], '#FFAA00')

    def test_oversize_or_symlink_config_cannot_enable_boot_writes(self):
        self.state.mkdir()
        path = self.state / 'light-policy.json'
        path.write_bytes(b'x' * 65537)
        self.assertFalse(self.make().resume_on_boot)
        path.rename(self.root / 'oversize-config')
        path.symlink_to(self.root / 'oversize-config')
        restored = self.make()
        self.assertFalse(restored.resume_on_boot)
        self.assertIsNotNone(restored.settings_error)
        self.assertEqual([], self.calls)

    def test_snapshot_and_async_ack_stay_fast_while_actual_rgb_read_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        runner = self.controller.runner

        def blocked(argv):
            entered.set()
            release.wait(2)
            return runner(argv)

        self.controller.runner = blocked
        started = time.monotonic()
        result = self.light.action('light.policy.update', {'config': self.config(), 'apply': True})
        self.assertLess(time.monotonic() - started, .1)
        self.assertTrue(result['pending'])
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        self.light.snapshot()
        self.controller.snapshot()
        self.assertLess(time.monotonic() - started, .1)
        self.assertFalse(self.controller.action('rgb.probe', {})['ok'])
        release.set()
        self.light.worker.join(2)

    def test_invalid_fields_stops_and_schedules_are_rejected_before_any_command(self):
        for update in ({'enabled': 1}, {'mode': 'shell'}, {'brightness': True}, {'stops': []},
                       {'sources': ['a', 'a']}, {'command': 'bad'}, {'schema_version': True}):
            config = self.config()
            config.update(update)
            result = self.light.action('light.policy.update', {'config': config, 'apply': True})
            self.assertFalse(result['ok'], update)
        config = self.config()
        config['schedule']['enabled'], config['schedule']['start'] = True, '25:00'
        self.assertFalse(self.light.action('light.policy.update', {'config': config})['ok'])
        self.assertEqual([], self.calls)


if __name__ == '__main__':
    unittest.main()

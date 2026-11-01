"""Selective panel cache contracts, without proc/sys, commands, or HTTP.

The complete snapshot remains the only disk projection. These tests exercise
the real collector and panel methods; only external IO/lifecycle seams are fixed.
"""
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))


def load(name):
    spec = importlib.util.spec_from_file_location(
        'panel_cache_test_' + name, ROOT / 'runtime' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


insights, compat = load('insights'), load('fnm_compat')
PANEL_FIELDS = {'timestamp', 'system', 'interfaces', 'containers', 'vms',
                'ports', 'processes', 'gpu', 'device'}
DEVICE_FIELDS = {'sampled_at', 'cpu', 'identity', 'network', 'thermal', 'fans', 'storage'}


class CopyCanary:
    def __deepcopy__(self, memo):
        raise AssertionError('excluded private cache was copied')


class SnapshotOnly:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def snapshot(self):
        self.calls += 1
        return self.value


class ExplicitCache(SnapshotOnly):
    def __init__(self, value, cached):
        super().__init__(value)
        self.cached, self.cache_calls = cached, 0

    def panel_cache_snapshot(self):
        self.cache_calls += 1
        return self.cached


class InsightsPanelCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-panel-cache-')
        self.addCleanup(self.temp.cleanup)
        self.now = 100.0
        self.collector = insights.InsightsCollector(
            proc_root='/fixture/proc', sys_root='/fixture/sys',
            page_size=4096, clock_ticks=100, clock=lambda: self.now,
            storage_client=mock.Mock(),
            runner=mock.Mock(side_effect=AssertionError('no command')),
            command_finder=lambda name: None)
        self.collector._state = self.state()

    @staticmethod
    def state():
        return {
            'schema_version': 1, 'timestamp': '2026-11-01T00:00:00+00:00',
            'system': {'cpu_usage_percent': 12.5, 'cpu_model': 'fixture CPU',
                       'cpu_cores': [{'id': 0, 'usage_percent': 12.5}],
                       'load_average': [0.1, 0.2, 0.3], 'uptime_seconds': 50,
                       'memory': {'total_bytes': 4096, 'used_bytes': 1024,
                                  'usage_percent': 25}},
            'interfaces': [{'name': 'eth0', 'rx_bytes_per_sec': 10,
                            'tx_bytes_per_sec': 20}],
            'containers': {'available': True, 'items': []},
            'vms': {'available': True, 'items': []},
            'ports': [{'protocol': 'tcp', 'port': 18732}],
            'processes': [{'pid': 123, 'name': 'fixture', 'cpu_percent': 1.0}],
            'gpu': {'available': True, 'devices': [{'name': 'card0', 'busy_percent': 4}]},
            'disks': {'items': []}, 'history': [{'private': 'DISK_HISTORY_CANARY'}],
            'raw': 'TOP_RAW_CANARY',
            'device': {
                'sampled_at': '2026-11-01T00:00:00+00:00',
                'cpu': {'model': 'fixture CPU', 'physical_cores': 1,
                        'logical_cores': 1, 'architecture': 'x86_64',
                        'frequencies_mhz': [{'current': 1000}]},
                'identity': {'hostname': 'fixture', 'name': 'fixture NAS',
                             'name_source': 'fixture', 'kernel': 'fixture kernel',
                             'os': {'NAME': 'fixture'}, 'firmware': {'version': 'fixture'},
                             'pci': [], 'dmi': {'sys_vendor': 'fixture',
                                              'board_name': 'fixture board',
                                              'product_name': 'fixture product'}},
                'network': [{'name': 'eth0', 'driver': 'fixture'}],
                'thermal': [{'name': 'coretemp', 'celsius': 41}],
                'fans': [{'name': 'fan1', 'rpm': 1200}],
                'storage': {'devices': [{'name': 'sda', 'serial': 'DISK_SERIAL_CANARY'}],
                            'raid': [{'name': 'md0', 'level': 'raid1', 'members': [],
                                      'degraded': 0, 'state': 'clean'}],
                            'mdstat': 'RAW_MDSTAT_CANARY'},
                'backlight': 'BACKLIGHT_CANARY', 'private': 'DEVICE_PRIVATE_CANARY',
            },
        }

    def panel(self, collector=None):
        return compat.FnmonitorPanel(
            None, self.collector if collector is None else collector,
            None, None, None, None, state_dir=self.temp.name,
            clock=lambda: self.now, wall_clock=lambda: 1700000100)

    @contextlib.contextmanager
    def no_io(self):
        with contextlib.ExitStack() as stack:
            for name in ('read_text', 'read_bytes', 'iterdir', 'glob', 'stat',
                         'lstat', 'resolve', 'open'):
                stack.enter_context(mock.patch.object(
                    Path, name, side_effect=AssertionError('no filesystem IO')))
            for name in ('open', 'statvfs', 'fstatvfs', 'readlink', 'listdir', 'scandir'):
                stack.enter_context(mock.patch.object(
                    os, name, create=True, side_effect=AssertionError('no OS IO')))
            stack.enter_context(mock.patch.object(
                insights, '_text', side_effect=AssertionError('no metadata read')))
            for name in ('socket.socket', 'socket.getaddrinfo', 'subprocess.Popen'):
                stack.enter_context(mock.patch(name, side_effect=AssertionError('no network or command')))
            yield

    def disk(self, sampled=100.0):
        lifetime = ('fixture-identity', ('fixture-generation', 1), '8:0')
        self.collector._state['disks']['items'] = [
            {'id': 'sda', '_lifecycle': lifetime, 'model': 'fixture disk', 'size_bytes': 1000}]
        smart = {'smart_available': True, 'temp_c': 37.0,
                 'smart_temperature_c': 37.0, 'health': {'passed': True},
                 'standby': False, 'smart_status': 'ok',
                 'smart_sampled_monotonic': sampled, 'smart_sampled_at': 1700000100}
        self.collector._smart_cache['sda'] = (sampled, smart, lifetime)
        return lifetime

    def test_fixed_fields_equal_complete_snapshot_for_retained_non_disk_inputs(self):
        with self.no_io():
            full = self.collector.snapshot()
            cached = self.collector.panel_cache_snapshot()
        self.assertEqual(set(cached), PANEL_FIELDS)
        self.assertEqual(set(cached['device']), DEVICE_FIELDS)
        for name in PANEL_FIELDS - {'device'}:
            self.assertEqual(cached[name], full[name])
        for name in DEVICE_FIELDS - {'storage'}:
            self.assertEqual(cached['device'][name], full['device'][name])
        self.assertEqual(cached['device']['storage'], {'raid': full['device']['storage']['raid']})

    def test_excluded_disks_history_serial_and_unknown_fields_never_copied(self):
        for name in ('disks', 'history', 'raw', 'unknown'):
            self.collector._state[name] = CopyCanary()
        device = self.collector._state['device']
        device['storage']['devices'] = CopyCanary()
        device['storage']['mdstat'] = CopyCanary()
        device['private'] = CopyCanary()
        device['identity']['private'] = CopyCanary()
        device['identity']['dmi'].update(product_serial=CopyCanary(), board_serial=CopyCanary(),
                                         unknown=CopyCanary())
        with self.no_io():
            value = self.collector.panel_cache_snapshot()
        encoded = json.dumps(value)
        for token in ('serial', 'CANARY', 'mdstat', 'private', 'history', 'disks', 'unknown'):
            self.assertNotIn(token, encoded)
        self.assertEqual(value['device']['identity']['dmi']['board_name'], 'fixture board')

    def test_cache_snapshot_runs_no_disk_lifecycle_or_probe_helpers(self):
        self.disk()
        with contextlib.ExitStack() as stack:
            probes = [stack.enter_context(mock.patch.object(
                self.collector, name, side_effect=AssertionError('no disk helper')))
                for name in ('_smart_context', '_disk_generation', '_disk_identity',
                             '_smart', '_run', '_disks', 'storage_identity_cached')]
            stack.enter_context(self.no_io())
            value = self.collector.panel_cache_snapshot()
        self.assertEqual(value['system']['cpu_usage_percent'], 12.5)
        for probe in probes:
            probe.assert_not_called()

    def test_returns_independent_nested_values_without_mutating_collector(self):
        before = copy.deepcopy(self.collector._state)
        value = self.collector.panel_cache_snapshot()
        value['interfaces'][0]['rx_bytes_per_sec'] = 999
        value['system']['cpu_cores'][0]['usage_percent'] = 99
        value['device']['identity']['dmi']['board_name'] = 'changed'
        value['device']['storage']['raid'][0]['members'].append('changed')
        self.assertEqual(self.collector._state, before)
        self.collector._state['system']['cpu_cores'][0]['usage_percent'] = 24
        self.assertEqual(value['system']['cpu_cores'][0]['usage_percent'], 99)
        self.assertEqual(self.collector.panel_cache_snapshot()['system']['cpu_cores'][0]['usage_percent'], 24)

    def test_missing_or_malformed_optional_cache_inputs_remain_unknown_to_panel(self):
        for device in (None, [], 'invalid', 123):
            with self.subTest(device_type=type(device).__name__):
                self.collector._state = {'timestamp': None, 'device': device}
                with self.no_io():
                    value = self.collector.panel_cache_snapshot()
                    panel = self.panel()
                    cpu = panel._cpu()
                    network = panel._net()
                    raid = panel._raid()
                self.assertNotIn('device', value)
                self.assertIsNone(cpu['percent'])
                self.assertEqual(network, [])
                self.assertEqual(raid, [])

    def test_concurrent_updates_are_copied_under_one_collector_lock(self):
        self.collector._state['system']['generation'] = 0
        self.collector._state['device']['cpu']['generation'] = 0
        errors = []
        started = threading.Event()

        def publish():
            try:
                started.set()
                for generation in range(1, 101):
                    with self.collector._lock:
                        self.collector._state['system']['generation'] = generation
                        self.collector._state['device']['cpu']['generation'] = generation
                    time.sleep(0)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=publish)
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            for _ in range(100):
                value = self.collector.panel_cache_snapshot()
                self.assertEqual(value['system']['generation'], value['device']['cpu']['generation'])
                time.sleep(0)
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_partial_device_fields_preserve_real_dates_and_raid_scope(self):
        self.collector._state['device'] = {'sampled_at': None, 'storage': {'raid': None,
                                                                       'devices': CopyCanary()}}
        value = self.collector.panel_cache_snapshot()
        self.assertIsNone(value['device']['sampled_at'])
        self.assertEqual(value['device']['storage'], {'raid': None})
        self.assertEqual(value['timestamp'], self.collector._state['timestamp'])

    def test_reader_waits_for_atomic_collector_publication(self):
        started, finished = threading.Event(), threading.Event()
        values, errors = [], []

        def read():
            started.set()
            try:
                values.append(self.collector.panel_cache_snapshot())
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=read, daemon=True)
        try:
            with self.collector._lock:
                worker.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.wait(0.05))
                self.collector._state['system']['cpu_usage_percent'] = 24
                self.collector._state['device']['cpu']['physical_cores'] = 2
        finally:
            if worker.ident is not None:
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(values[0]['system']['cpu_usage_percent'], 24)
        self.assertEqual(values[0]['device']['cpu']['physical_cores'], 2)

    def test_explicit_type_hook_is_preferred_and_returns_an_isolated_copy(self):
        component = ExplicitCache({'private': 'full-only'}, {'system': {'cpu_usage_percent': 3}})
        panel = self.panel(component)
        value = panel._insights()
        value['system']['cpu_usage_percent'] = 4
        self.assertEqual(component.cache_calls, 1)
        self.assertEqual(component.calls, 0)
        self.assertEqual(component.cached['system']['cpu_usage_percent'], 3)

    def test_inherited_explicit_hook_remains_supported(self):
        class Child(ExplicitCache):
            pass
        component = Child({}, {'system': {'cpu_usage_percent': 5}})
        self.assertEqual(self.panel(component)._insights()['system']['cpu_usage_percent'], 5)
        self.assertEqual((component.calls, component.cache_calls), (0, 1))

    def test_snapshot_only_legacy_object_keeps_original_safe_copy(self):
        component = SnapshotOnly({'system': {'cpu_usage_percent': 6}})
        value = self.panel(component)._insights()
        value['system']['cpu_usage_percent'] = 7
        self.assertEqual(component.calls, 1)
        self.assertEqual(component.value['system']['cpu_usage_percent'], 6)

    def test_dynamic_mock_hook_does_not_opt_in_legacy_object(self):
        component = mock.Mock()
        component.snapshot.return_value = {'system': {'cpu_usage_percent': 8}}
        accidental_hook = component.panel_cache_snapshot
        self.assertEqual(self.panel(component)._insights()['system']['cpu_usage_percent'], 8)
        component.snapshot.assert_called_once_with()
        accidental_hook.assert_not_called()

    def test_noncallable_type_hook_keeps_snapshot_only_compatibility(self):
        class Legacy(SnapshotOnly):
            panel_cache_snapshot = None
        component = Legacy({'system': {'cpu_usage_percent': 9}})
        self.assertEqual(self.panel(component)._insights()['system']['cpu_usage_percent'], 9)
        self.assertEqual(component.calls, 1)

    def test_hook_invalid_results_stay_unknown_without_falling_back_to_full_disk_scan(self):
        for result in (None, [], 'invalid', 1, False):
            with self.subTest(result_type=type(result).__name__):
                component = ExplicitCache({'private': 'must not use full'}, result)
                self.assertEqual(self.panel(component)._insights(), {})
                self.assertEqual((component.calls, component.cache_calls), (0, 1))

    def test_hook_exception_stays_unknown_without_full_fallback(self):
        class Broken(SnapshotOnly):
            def panel_cache_snapshot(self):
                raise ValueError('PRIVATE_ERROR_CANARY')
        component = Broken({'private': 'must not use full'})
        self.assertEqual(self.panel(component)._insights(), {})
        self.assertEqual(component.calls, 0)

    def test_hook_copy_fault_does_not_return_private_or_mutable_input(self):
        component = ExplicitCache({}, {'system': CopyCanary()})
        self.assertEqual(self.panel(component)._insights(), {})
        self.assertEqual(component.calls, 0)

    def test_non_disk_panel_consumers_equal_complete_snapshot_adapter(self):
        with self.no_io():
            legacy = self.panel(SnapshotOnly(self.collector.snapshot()))
            selective = self.panel()
            for name in ('_cpu', '_mem', '_net', '_gpu', '_hardware', '_raid'):
                with self.subTest(consumer=name):
                    self.assertEqual(getattr(selective, name)(), getattr(legacy, name)())

    def test_disk_opt_in_keeps_real_full_snapshot_and_its_lifecycle_checks(self):
        lifetime = self.disk()
        panel = self.panel()
        with mock.patch.object(self.collector, '_smart_context', return_value=lifetime) as context:
            with mock.patch.object(self.collector, '_disk_identity', return_value='fixture-identity'):
                with mock.patch.object(self.collector, 'panel_cache_snapshot',
                                       wraps=self.collector.panel_cache_snapshot) as cached:
                    with self.no_io():
                        value = panel._insights(include_disks=True)
        self.assertEqual(value['disks']['items'][0]['temp_c'], 37)
        self.assertGreaterEqual(context.call_count, 3)
        cached.assert_not_called()

    def test_disk_projection_still_takes_two_full_snapshots_and_no_selective_hook(self):
        lifetime = self.disk()
        panel = self.panel()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.collector, '_smart_context', return_value=lifetime))
            stack.enter_context(mock.patch.object(self.collector, '_disk_identity', return_value='fixture-identity'))
            full = stack.enter_context(mock.patch.object(self.collector, 'snapshot', wraps=self.collector.snapshot))
            cached = stack.enter_context(mock.patch.object(
                self.collector, 'panel_cache_snapshot', wraps=self.collector.panel_cache_snapshot))
            stack.enter_context(mock.patch.object(panel, '_mounts', return_value={}))
            stack.enter_context(mock.patch.object(panel, '_basic', return_value={'disks': []}))
            stack.enter_context(mock.patch.object(panel, '_volume_labels', return_value={}))
            stack.enter_context(self.no_io())
            value = panel._disks()
        self.assertEqual(len(value), 1)
        self.assertEqual(full.call_count, 2)
        cached.assert_not_called()

    def test_full_snapshot_still_rejects_replacement_before_projection(self):
        self.disk()
        with mock.patch.object(self.collector, '_smart_context', return_value=('replacement',)):
            with mock.patch.object(self.collector, '_smart', wraps=self.collector._smart) as smart:
                with self.no_io():
                    value = self.collector.snapshot()
        self.assertEqual(value['disks']['items'], [])
        smart.assert_not_called()

    def test_full_snapshot_still_rejects_generation_change_after_projection(self):
        lifetime = self.disk()
        with mock.patch.object(self.collector, '_smart_context',
                               side_effect=[lifetime, lifetime, ('replacement',)]):
            with mock.patch.object(self.collector, '_disk_identity', return_value='fixture-identity'):
                with self.no_io():
                    value = self.collector.snapshot()
        self.assertEqual(value['disks']['items'], [])

    def test_full_snapshot_keeps_stale_and_future_smart_measurements_unknown(self):
        for sampled in (69.0, 101.0):
            with self.subTest(sampled=sampled):
                lifetime = self.disk(sampled)
                with mock.patch.object(self.collector, '_smart_context', return_value=lifetime):
                    with mock.patch.object(self.collector, '_disk_identity', return_value='fixture-identity'):
                        with self.no_io():
                            value = self.collector.snapshot()['disks']['items'][0]
                self.assertIsNone(value['temp_c'])
                self.assertIsNone(value['health']['passed'])
                self.assertIsNone(value['standby'])
                self.assertFalse(value['smart_fresh'])

    def test_projection_observer_fault_preserves_full_snapshot_result(self):
        panel = self.panel()
        with mock.patch.object(panel, '_capacity_projection_begin', side_effect=ValueError('observer canary')):
            with self.no_io():
                actual = panel._insights(include_disks=True)
                expected = self.collector.snapshot()
        self.assertEqual(actual, expected)

    def test_selective_hook_never_turns_real_full_lifecycle_failure_into_a_disk_grant(self):
        self.disk()
        error = ValueError('lifecycle failure canary')
        with mock.patch.object(self.collector, '_smart_context', side_effect=error):
            with self.no_io():
                self.assertNotIn('disks', self.collector.panel_cache_snapshot())
                with self.assertRaises(ValueError) as caught:
                    self.collector.snapshot()
                self.assertEqual(self.panel()._insights(include_disks=True), {})
        self.assertIs(caught.exception, error)


if __name__ == '__main__':
    unittest.main()

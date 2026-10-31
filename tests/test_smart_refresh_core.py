"""Explicit SMART refresh with fake commands and real proc/sys fixture paths.

Nothing in this suite executes smartctl or accesses a real block device. The
command seam observes the actual collector entry points rather than replacing
the production SMART/cache/lifecycle logic under test.
"""

import copy
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
insights = importlib.import_module('insights')
device_info = importlib.import_module('device_info')
TemperatureSources = importlib.import_module('temperature_sources').TemperatureSources
panel_module = importlib.import_module('fnm_compat')
PRIVATE = 'SMART-PRIVATE-SERIAL-PATH-EXCEPTION-SENTINEL'


class SmartRefreshCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(tempfile.mkdtemp(prefix='r1-smart-core-'))
        self.assertEqual(self.temporary.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(shutil.rmtree, self.temporary, ignore_errors=True)
        self.proc, self.sys = self.temporary / 'proc', self.temporary / 'sys'
        self.proc.mkdir()
        self.sys.mkdir()
        self.now = 100.0
        self.calls = []
        self.program_available = True
        self.behavior = None
        self.active = True
        self.collector = insights.InsightsCollector(
            self.proc, self.sys, runner=self.runner,
            command_finder=lambda name: ('/fixture/bin/smartctl'
                if name == 'smartctl' and self.program_available else None),
            clock=lambda: self.now, page_size=4096, clock_ticks=100,
            ipv4_provider=lambda: {}, sample_interval=0.1, slow_interval=1,
            storage_client=SimpleNamespace(snapshot=lambda: {
                'available': False, 'reason': 'fixture_unavailable', 'disks': [], 'spaces': []}))
        self.addCleanup(self.collector.stop)
        self.folder = self.block('sda', '8:0', 'disk-a')

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value), encoding='utf-8')

    def block(self, name, dev, identity):
        folder = self.sys / 'block' / name
        self.write(folder / 'dev', dev + '\n')
        self.write(folder / 'size', '1000000\n')
        self.write(folder / 'queue/rotational', '1\n')
        self.write(folder / 'device/model', 'Fixture physical disk\n')
        self.write(folder / 'wwid', identity + '\n')
        return folder

    @staticmethod
    def good_response(temperature=37):
        return {'smart_status': {'passed': True}, 'temperature': {'current': temperature},
            'serial_number': PRIVATE, 'power_on_time': {'hours': 123},
            'ata_smart_attributes': {'table': [
                {'id': 194, 'name': 'Temperature_Celsius', 'raw': {'value': temperature}}]}}

    def runner(self, argv, timeout=3):
        self.calls.append((list(argv), timeout))
        value = self.behavior(argv, timeout) if callable(self.behavior) else self.behavior
        if isinstance(value, BaseException):
            raise value
        if value is None:
            value = (0, self.good_response())
        code, output = value
        return SimpleNamespace(returncode=code,
            stdout=output if isinstance(output, str) else json.dumps(output), stderr=PRIVATE)

    def refresh(self, force=False):
        return self.collector.refresh_smart(force=force, authorizer=lambda: self.active)

    def row(self):
        return next(item for item in self.collector._disks()['items'] if item['id'] == 'sda')

    def publish_disks(self):
        self.collector._publish_integration('disks', self.collector._disks)
        return self.collector.snapshot()['disks']['items']

    def panel(self):
        return panel_module.FnmonitorPanel(None, self.collector, None, None, None, None,
            state_dir=self.temporary / 'panel', clock=lambda: self.now,
            proc_root=self.proc, sys_root=self.sys, dev_root=self.temporary / 'dev',
            udev_root=self.temporary / 'run/udev')

    def assert_receipt(self, result, statuses):
        self.assertEqual(set(result), {'ok', 'force', 'status', 'started_at', 'finished_at', 'results'})
        self.assertIs(result['ok'], True)
        self.assertIn(result['status'], ('complete', 'partial'))
        for key in ('started_at', 'finished_at'):
            self.assertIsInstance(result[key], (int, float))
            self.assertGreater(result[key], 0)
        self.assertGreaterEqual(result['finished_at'], result['started_at'])
        self.assertEqual({item['device']: item['status'] for item in result['results']}, statuses)
        for item in result['results']:
            self.assertEqual(set(item), {'device', 'status', 'reason', 'sampled_at'})
            self.assertTrue(item['reason'] is None or type(item['reason']) is str)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_background_multiple_fast_slow_and_passive_views_never_invoke_smartctl(self):
        self.write(self.proc / 'stat', 'cpu 10 0 0 90 0 0 0 0\ncpu0 10 0 0 90 0 0 0 0\n')
        for _ in range(3):
            self.collector._collect_fast()
            self.collector._collect_slow()
            self.collector.snapshot()
            self.collector.smart_cached('sda')
            self.collector.storage_identity_cached()
            self.collector.storage_name_diagnostics()
            self.collector.storage_identity_diagnostics()
            self.collector.disk_activity_diagnostics()
            self.now += 35
        panel = self.panel()
        panel.smart('sda')
        panel.disk_health()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.row()['health']['passed'])
        self.assertFalse(self.row()['smart_available'])

    def test_real_startup_and_second_slow_round_never_invoke_smartctl(self):
        twice = threading.Event()
        original, count = self.collector._disks, [0]

        def observe_round():
            result = original()
            count[0] += 1
            if count[0] == 2:
                twice.set()
            else:
                self.collector._refresh['disks'].set()
            return result

        with mock.patch.object(self.collector, '_disks', side_effect=observe_round):
            self.collector.start()
            self.assertTrue(twice.wait(timeout=2), 'second real background disk round did not complete')
            self.collector.stop()
        self.assertGreaterEqual(count[0], 2)
        self.assertEqual(self.calls, [])

    def test_startup_rounds_snapshots_and_control_never_open_disk_temperature_attributes(self):
        sensor = self.folder / 'device/hwmon/hwmon4'
        self.write(sensor / 'name', 'drivetemp')
        for name in ('temp1_input', 'temp1_max', 'temp1_crit', 'temp1_label'):
            self.write(sensor / name, 41000)
        alias = self.sys / 'class/hwmon/hwmon4'
        self.write(alias / 'name', 'drivetemp')
        for name in ('temp1_input', 'temp1_max', 'temp1_crit', 'temp1_label'):
            self.write(alias / name, 41000)
        cpu = self.sys / 'class/hwmon/hwmon5'
        self.write(cpu / 'name', 'coretemp')
        self.write(cpu / 'temp1_input', 55000)
        twice, attempts = threading.Event(), []
        original_disks, rounds = self.collector._disks, [0]
        original_open, original_text = Path.open, device_info._Reader.text

        def guard(path):
            if Path(path).name.startswith('temp') and Path(path).parent in (sensor, alias):
                attempts.append(str(path))
                raise AssertionError('passive disk temperature attribute read')

        def open_file(path, *args, **kwargs):
            guard(path)
            return original_open(path, *args, **kwargs)

        def read(reader, path, *args, **kwargs):
            guard(path)
            return original_text(reader, path, *args, **kwargs)

        def disk_round():
            result = original_disks()
            rounds[0] += 1
            if rounds[0] >= 2:
                twice.set()
            else:
                self.collector._refresh['disks'].set()
            return result

        with mock.patch.object(Path, 'open', open_file), \
                mock.patch.object(device_info.os, 'O_NONBLOCK', getattr(os, 'O_NONBLOCK', 0), create=True), \
                mock.patch.object(device_info._Reader, 'text', read), \
                mock.patch.object(self.collector, '_disks', side_effect=disk_round):
            self.collector.start()
            try:
                self.assertTrue(twice.wait(timeout=2))
                sources = TemperatureSources(self.collector, self.sys, clock=lambda: self.now)
                for _ in range(3):
                    self.collector._collect_slow()
                    self.collector.snapshot()
                    self.assertEqual(sources.cpu_guard(), 55)
                    self.now += 2
            finally:
                self.collector.stop()
        self.assertEqual(attempts, [])
        self.assertGreaterEqual(rounds[0], 2)
        self.assertEqual(self.calls, [])

    def test_snapshot_discards_legacy_sysfs_temperature_even_in_same_lifetime(self):
        self.publish_disks()
        self.collector._state['disks']['items'][0].update(temp_c=88, temperature_source='sysfs')
        disk = self.collector.snapshot()['disks']['items'][0]
        self.assertIsNone(disk['temp_c'])
        self.assertEqual(disk['temperature_source'], 'smart')
        self.assertEqual(self.calls, [])

    def test_default_explicit_refresh_is_standby_aware_and_projects_real_cache(self):
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'refreshed'})
        self.assertIs(result['force'], False)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.calls[0][0], ['/fixture/bin/smartctl', '-n', 'standby', '-j', '-a', '/dev/sda'])
        self.assertGreater(self.calls[0][1], 0)
        self.assertLessEqual(self.calls[0][1], 3)
        row = self.row()
        self.assertTrue(row['smart_available'])
        self.assertTrue(row['health']['passed'])
        self.assertEqual(row['smart_temperature_c'], 37)
        self.assertEqual(self.collector.smart_cached('sda')['smart_data']['power_on_time']['hours'], 123)
        before = len(self.calls)
        self.publish_disks()
        details = self.panel().smart('sda')
        self.assertTrue(details['ok'])
        self.assertEqual(details['power_on_hours'], 123)
        self.assertEqual(len(self.calls), before)

    def test_force_only_omits_standby_option_for_that_explicit_call(self):
        forced = self.refresh(force=True)
        self.assert_receipt(forced, {'sda': 'refreshed'})
        self.assertIs(forced['force'], True)
        self.assertEqual(self.calls[0][0], ['/fixture/bin/smartctl', '-j', '-a', '/dev/sda'])
        regular = self.refresh()
        self.assert_receipt(regular, {'sda': 'refreshed'})
        self.assertEqual(self.calls[1][0], ['/fixture/bin/smartctl', '-n', 'standby', '-j', '-a', '/dev/sda'])
        before = len(self.calls)
        self.now += 60
        self.collector._collect_slow()
        self.collector.snapshot()
        self.assertEqual(len(self.calls), before)

    def test_standby_skip_has_no_retry_or_force_escalation(self):
        self.behavior = (2, {'power_mode': {'string': 'STANDBY'},
            'smartctl': {'messages': [{'string': 'Device is in STANDBY mode, exit(2)'}]}})
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'skipped_standby'})
        self.assertEqual(len(self.calls), 1)
        self.assertIn('-n', self.calls[0][0])
        self.assertIsNone(self.row()['health']['passed'])
        self.assertIsNone(self.row()['smart_temperature_c'])
        self.assertTrue(self.row()['standby'])

    def test_unsupported_reply_never_retries_without_standby_guard(self):
        self.behavior = (2, {'smartctl': {'messages': [
            {'string': 'Device does not support SMART ' + PRIVATE}]}})
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'unavailable'})
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(self.calls), 1)
        self.assertIn('-n', self.calls[0][0])
        self.assertFalse(self.row()['smart_available'])
        self.assertIsNone(self.row()['health']['passed'])

    def test_expired_cache_keeps_old_sample_time_but_not_current_health_or_temperature(self):
        self.refresh()
        original = self.collector.smart_cached('sda')
        sample = original['smart_sampled_monotonic']
        self.now += 31
        cached = self.collector.smart_cached('sda')
        self.assertIs(cached['fresh'], False)
        self.assertIsNone(cached['health']['passed'])
        self.assertIsNone(cached['smart_temperature_c'])
        self.assertEqual(cached['smart_sampled_monotonic'], sample)
        self.assertNotIn('smart_data', cached)
        # Retaining private history must not expose expired measurements as
        # the current owner-facing cached result or renew its sampling time.
        private = self.collector._smart_cache['sda']
        self.assertEqual(private[1]['smart_data']['power_on_time']['hours'], 123)
        self.assertEqual(private[0], sample)
        self.publish_disks()
        row = self.row()
        self.assertIsNone(row['health']['passed'])
        self.assertIsNone(row['smart_temperature_c'])
        self.assertEqual(len(self.calls), 1)

    def test_sysfs_temperature_never_fills_expired_manual_smart_cache(self):
        self.refresh()
        self.write(self.folder / 'device/hwmon/hwmon4/temp1_input', '41000\n')
        self.now += 31
        row = self.row()
        self.assertIsNone(row['temp_c'])
        self.assertIsNone(row['health']['passed'])
        self.assertIsNone(row['smart_temperature_c'])
        self.assertEqual(len(self.calls), 1)

    def test_reused_device_name_with_changed_stable_identity_drops_old_cache(self):
        self.refresh()
        self.write(self.folder / 'wwid', 'replacement-disk\n')
        self.assertIsNone(self.collector.smart_cached('sda'))
        row = self.row()
        self.assertIsNone(row['health']['passed'])
        self.assertIsNone(row['smart_temperature_c'])
        self.assertEqual(len(self.calls), 1)

    def test_replaced_sysfs_lifetime_drops_old_cache_even_with_same_wwid(self):
        self.refresh()
        self.folder.rename(self.sys / 'block/old-disk-fixture')
        self.folder = self.block('sda', '8:0', 'disk-a')
        self.assertIsNone(self.collector.smart_cached('sda'))
        self.assertFalse(self.row()['smart_available'])
        self.assertEqual(len(self.calls), 1)

    def test_changed_major_minor_drops_cache_even_when_metadata_identity_is_same(self):
        self.refresh()
        self.write(self.folder / 'dev', '8:16\n')
        self.assertIsNone(self.collector.smart_cached('sda'))
        self.assertIsNone(self.row()['health']['passed'])
        self.assertEqual(len(self.calls), 1)

    def test_identity_change_during_command_discards_the_response_and_prior_cache(self):
        self.refresh()

        def replace(argv, timeout):
            self.write(self.folder / 'wwid', 'disk-after-command\n')
            return 0, self.good_response(62)

        self.behavior = replace
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'identity_changed'})
        self.assertIsNone(result['results'][0]['sampled_at'])
        self.assertIsNone(self.collector.smart_cached('sda'))
        self.assertIsNone(self.row()['smart_temperature_c'])

    def test_device_disappearance_during_command_is_not_published(self):
        def disappear(argv, timeout):
            self.folder.rename(self.sys / 'block/disappeared-fixture')
            return 0, self.good_response()

        self.behavior = disappear
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'identity_changed'})
        self.assertIsNone(self.collector.smart_cached('sda'))

    def test_current_command_timeout_is_safe_and_does_not_retry(self):
        self.behavior = subprocess.TimeoutExpired(PRIVATE, 3, output=PRIVATE, stderr=PRIVATE)
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'failed'})
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.row()['health']['passed'])
        self.assertNotIn(PRIVATE, self.row()['health'].get('reason') or '')

    def test_invalid_output_and_unsafe_exception_text_are_never_in_receipt(self):
        for output in ((0, PRIVATE), OSError('/private/' + PRIVATE)):
            with self.subTest(error=type(output).__name__):
                self.behavior = output
                result = self.refresh()
                self.assert_receipt(result, {'sda': 'failed'})
                self.assertIsNone(self.row()['health']['passed'])
                self.assertNotIn(PRIVATE, self.row()['health'].get('reason') or '')

    def test_missing_smartctl_is_unavailable_without_invoking_runner(self):
        self.program_available = False
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'unavailable'})
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.row()['health']['passed'])

    def test_unprivileged_or_invalid_refresh_never_invokes_runner(self):
        for authorize in (None, lambda: False, lambda: 1):
            with self.assertRaises(insights.SmartRefreshError) as raised:
                self.collector.refresh_smart(authorizer=authorize)
            self.assertEqual(raised.exception.reason, 'authorization_denied')
        for force in (1, 'true', None, {}):
            with self.assertRaises(insights.SmartRefreshError) as raised:
                self.collector.refresh_smart(force=force, authorizer=lambda: True)
            self.assertEqual(raised.exception.reason, 'invalid_parameters')
        self.assertEqual(self.calls, [])

    def test_authorization_revocation_discards_entire_staged_batch(self):
        self.refresh()
        original = copy.deepcopy(self.collector.smart_cached('sda'))
        self.block('sdb', '8:16', 'disk-b')

        def revoke(argv, timeout):
            self.active = False
            return 0, self.good_response(63)

        self.behavior = revoke
        with self.assertRaises(insights.SmartRefreshError) as raised:
            self.refresh()
        self.assertEqual(raised.exception.reason, 'authorization_denied')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.collector.smart_cached('sda'), original)
        self.assertIsNone(self.collector.smart_cached('sdb'))

    def test_authorization_failure_before_publication_does_not_publish_partial_batch(self):
        self.block('sdb', '8:16', 'disk-b')
        active = [True]

        def revoke_second(argv, timeout):
            if argv[-1] == '/dev/sdb':
                active[0] = False
            return 0, self.good_response()

        self.behavior = revoke_second
        with self.assertRaises(insights.SmartRefreshError) as raised:
            self.collector.refresh_smart(authorizer=lambda: active[0])
        self.assertEqual(raised.exception.reason, 'authorization_denied')
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(self.collector.smart_cached('sda'))
        self.assertIsNone(self.collector.smart_cached('sdb'))

    def test_concurrent_refresh_is_busy_while_snapshot_and_background_remain_passive(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        results = []

        def blocked(argv, timeout):
            entered.set()
            release.wait(timeout=2)
            return 0, self.good_response()

        self.behavior = blocked
        worker = threading.Thread(target=lambda: results.append(self.refresh()))
        worker.start()
        self.addCleanup(worker.join, timeout=2)
        self.assertTrue(entered.wait(timeout=1))
        with self.assertRaises(insights.SmartRefreshError) as raised:
            self.refresh()
        self.assertEqual(raised.exception.reason, 'busy')
        started = time.monotonic()
        self.collector.snapshot()
        self.collector._disks()
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(len(self.calls), 1)
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assert_receipt(results[0], {'sda': 'refreshed'})
        self.assertTrue(self.row()['health']['passed'])

    def test_batch_deadline_is_not_renewed_after_slow_first_device(self):
        self.block('sdb', '8:16', 'disk-b')

        def exhaust(argv, timeout):
            self.now += 31
            return 0, self.good_response()

        self.behavior = exhaust
        result = self.refresh()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(item['status'] != 'refreshed' for item in result['results']))
        self.assertIsNone(self.collector.smart_cached('sda'))
        self.assertIsNone(self.collector.smart_cached('sdb'))

    def test_non_physical_partition_and_emmc_entries_never_become_command_targets(self):
        self.block('sda1', '8:1', 'partition-a')
        self.block('loop0', '7:0', 'loop-a')
        self.block('mmcblk0', '179:0', 'boot-emmc')
        result = self.refresh()
        self.assertTrue(all(item['status'] != 'refreshed' for item in result['results']
                            if item['device'] != 'sda'))
        self.assertEqual([argv[-1] for argv, _ in self.calls], ['/dev/sda'])

    def test_invalid_device_identity_is_unavailable_before_any_command(self):
        self.write(self.folder / 'dev', '8:0\nPRIVATE-EXTRA\n')
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'unavailable'})
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.collector.smart_cached('sda'))

    def test_bad_health_is_preserved_as_failed_health_not_unknown_or_command_failure(self):
        self.behavior = (8, {'smart_status': {'passed': False}, 'temperature': {'current': 48}})
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'refreshed'})
        self.assertFalse(self.row()['health']['passed'])
        self.assertTrue(self.row()['smart_available'])
        self.assertEqual(len(self.calls), 1)

    def test_snapshot_itself_expires_manual_health_without_a_new_background_round(self):
        self.refresh()
        self.publish_disks()
        current = self.collector.snapshot()['disks']['items'][0]
        self.assertTrue(current['health']['passed'])
        sampled = current['smart_sampled_monotonic']
        self.now += 31
        current = self.collector.snapshot()['disks']['items'][0]
        self.assertIsNone(current['health']['passed'])
        self.assertIsNone(current['smart_temperature_c'])
        self.assertIsNone(current['temp_c'])
        self.assertIs(current['smart_fresh'], False)
        self.assertEqual(current['smart_sampled_monotonic'], sampled)
        self.assertEqual(len(self.calls), 1)

    def test_later_batch_device_cannot_publish_an_earlier_replaced_device(self):
        self.block('sdb', '8:16', 'disk-b')

        def replace_first_when_reading_second(argv, timeout):
            if argv[-1] == '/dev/sdb':
                self.write(self.folder / 'wwid', 'disk-a-replaced-before-publication\n')
            return 0, self.good_response()

        self.behavior = replace_first_when_reading_second
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'identity_changed', 'sdb': 'refreshed'})
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(self.collector.smart_cached('sda'))
        self.assertTrue(self.collector.smart_cached('sdb')['health']['passed'])
        self.assertIsNone(result['results'][0]['sampled_at'])

    def test_major_minor_change_during_command_is_rejected_without_stable_id_change(self):
        def change_mapping(argv, timeout):
            self.write(self.folder / 'dev', '8:32\n')
            return 0, self.good_response()

        self.behavior = change_mapping
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'identity_changed'})
        self.assertIsNone(self.collector.smart_cached('sda'))

    def test_standby_after_healthy_sample_does_not_reuse_temperature_or_health(self):
        self.refresh()
        self.behavior = (2, {'power_mode': {'string': 'SLEEP'}})
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'skipped_standby'})
        self.assertIsNone(self.row()['health']['passed'])
        self.assertIsNone(self.row()['smart_temperature_c'])
        self.assertNotIn('power_on_time', self.collector.smart_cached('sda')['smart_data'])
        self.assertEqual(len(self.calls), 2)

    def test_cached_return_is_detached_and_public_snapshot_omits_raw_smart_payload(self):
        self.refresh()
        cached = self.collector.smart_cached('sda')
        cached['smart_data']['power_on_time']['hours'] = 9999
        cached['health']['passed'] = False
        self.assertEqual(self.collector.smart_cached('sda')['smart_data']['power_on_time']['hours'], 123)
        self.assertTrue(self.row()['health']['passed'])
        self.publish_disks()
        self.assertNotIn(PRIVATE, json.dumps(self.collector.snapshot()))
        self.assertNotIn('smart_data', json.dumps(self.collector.snapshot()))

    def test_monotonic_clock_regression_does_not_turn_old_sample_into_fresh_health(self):
        self.refresh()
        self.now -= 1
        cached = self.collector.smart_cached('sda')
        self.assertIs(cached['fresh'], False)
        self.assertIsNone(cached['health']['passed'])
        self.assertIsNone(cached['smart_temperature_c'])
        self.assertEqual(len(self.calls), 1)

    def test_stopped_collector_cannot_start_an_explicit_refresh(self):
        self.collector.stop()
        with self.assertRaises(insights.SmartRefreshError) as raised:
            self.refresh()
        self.assertEqual(raised.exception.reason, 'stopping')
        self.assertEqual(self.calls, [])

    def test_authorizer_exception_is_fixed_denial_without_exception_text(self):
        def broken_authorizer():
            raise OSError('/private/' + PRIVATE)

        with self.assertRaises(insights.SmartRefreshError) as raised:
            self.collector.refresh_smart(authorizer=broken_authorizer)
        self.assertEqual(raised.exception.reason, 'authorization_denied')
        self.assertNotIn(PRIVATE, str(raised.exception))
        self.assertEqual(self.calls, [])

    def test_nvme_and_sata_batch_both_use_fixed_standby_aware_targets(self):
        self.block('nvme0n1', '259:0', 'nvme-a')
        result = self.refresh()
        self.assert_receipt(result, {'nvme0n1': 'refreshed', 'sda': 'refreshed'})
        self.assertEqual({tuple(argv[1:]) for argv, _ in self.calls}, {
            ('-n', 'standby', '-j', '-a', '/dev/nvme0n1'),
            ('-n', 'standby', '-j', '-a', '/dev/sda')})

    def test_malformed_nested_native_data_is_unknown_and_never_exposes_text(self):
        self.behavior = (0, {'smartctl': [PRIVATE], 'smart_status': {'passed': True}})
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'failed'})
        self.assertIsNone(self.row()['health']['passed'])
        self.assertFalse(self.row()['smart_available'])
        self.assertNotIn(PRIVATE, self.row()['health'].get('reason') or '')

    def test_manual_refresh_does_not_invoke_native_storage_rpc_or_schedule_disk_collection(self):
        storage = mock.Mock()
        storage.snapshot.side_effect = AssertionError('explicit SMART refresh must not trigger storage RPC')
        self.collector.storage_client = storage
        self.assertFalse(self.collector._refresh['disks'].is_set())
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'refreshed'})
        storage.snapshot.assert_not_called()
        self.assertFalse(self.collector._refresh['disks'].is_set())
        self.assertTrue(self.collector.smart_cached('sda')['health']['passed'])

    def test_failed_refresh_does_not_overwrite_or_renew_prior_valid_cache(self):
        self.refresh()
        sampled = self.collector.smart_cached('sda')['smart_sampled_monotonic']
        self.now += 20
        for failure in (subprocess.TimeoutExpired(PRIVATE, 3),
                        (2, {'smartctl': {'messages': [{'string': 'SMART unavailable ' + PRIVATE}]}}),
                        (0, PRIVATE)):
            with self.subTest(failure=type(failure).__name__):
                self.behavior = failure
                result = self.refresh()
                self.assertEqual(result['status'], 'partial')
                self.assertIn(result['results'][0]['status'], ('failed', 'unavailable'))
                cached = self.collector.smart_cached('sda')
                self.assertEqual(cached['smart_sampled_monotonic'], sampled)
                self.assertEqual(cached['smart_temperature_c'], 37)
                self.assertTrue(cached['health']['passed'])
                self.assertNotIn(PRIVATE, json.dumps(result))
        self.now += 11
        self.assertIsNone(self.collector.smart_cached('sda')['health']['passed'])
        self.assertIsNone(self.collector.smart_cached('sda')['smart_temperature_c'])

    def test_unverified_power_mode_default_never_publishes_apparent_health_or_escalates(self):
        cases = ({'power_mode': {'string': 'UNKNOWN'}},
                 {'power_mode': {'string': 'UNSUPPORTED'}},
                 {'smartctl': {'messages': [{'string': 'Checking power mode is not supported'}]}},
                 {'smartctl': {'messages': [{'string': 'CHECK POWER MODE failed'}]}},
                 {'smartctl': {'messages': [{'string': 'CHECK POWER MODE not implemented, ignoring -n option'}]}},
                 {'smartctl': {'messages': [{'string': 'ignoring -n standby option'}]}},
                 {'smartctl': {'messages': [{'string': 'Option -n ignored because power mode is not implemented'}]}})
        for extra in cases:
            with self.subTest(case=next(iter(extra))):
                self.setUp()
                self.behavior = (0, dict(self.good_response(), **extra))
                before = len(self.calls)
                result = self.refresh()
                self.assert_receipt(result, {'sda': 'unavailable'})
                self.assertEqual(result['results'][0]['reason'], 'power_mode_unverified')
                self.assertEqual(len(self.calls), before + 1)
                self.assertIn('-n', self.calls[-1][0])
                self.assertIsNone(self.collector.smart_cached('sda'))
                self.assertIsNone(self.row()['health']['passed'])
                self.assertIsNone(self.row()['smart_temperature_c'])

    def test_unverified_power_mode_does_not_replace_previously_valid_cache(self):
        self.refresh()
        sampled = self.collector.smart_cached('sda')['smart_sampled_monotonic']
        self.now += 20
        self.behavior = (0, dict(self.good_response(69),
            smartctl={'messages': [{'string': 'Failed to check power mode ' + PRIVATE}]}))
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'unavailable'})
        cached = self.collector.smart_cached('sda')
        self.assertEqual(cached['smart_sampled_monotonic'], sampled)
        self.assertEqual(cached['smart_temperature_c'], 37)
        self.assertEqual(len(self.calls), 2)

    def test_expired_owner_details_do_not_return_old_ata_or_nvme_measurements(self):
        self.behavior = (0, dict(self.good_response(),
            nvme_smart_health_information_log={'critical_warning': 0,
                'temperature': 37, 'power_on_hours': 123}))
        self.refresh()
        sampled_at = self.collector.smart_cached('sda')['smart_sampled_at']
        self.publish_disks()
        self.assertTrue(self.panel().smart('sda')['attributes'])
        self.now += 31
        detail = self.panel().smart('sda')
        self.assertTrue(detail['ok'])
        self.assertIs(detail['fresh'], False)
        self.assertEqual(detail['sampled_at'], sampled_at)
        self.assertIsNone(detail['health'])
        self.assertIsNone(detail['temp'])
        self.assertFalse(detail['attributes'])
        self.assertFalse(detail['nvme_rows'])
        self.assertFalse(detail['raw'])
        self.assertIsNone(detail['power_on_hours'])
        self.assertIsNone(detail['power_cycle_count'])
        private = self.collector._smart_cache['sda'][1]
        self.assertEqual(private['smart_data']['power_on_time']['hours'], 123)
        self.assertEqual(private['smart_data']['nvme_smart_health_information_log']['temperature'], 37)
        self.assertEqual(private['smart_sampled_at'], sampled_at)
        self.assertEqual(len(self.calls), 1)

    def test_snapshot_does_not_rebind_old_metadata_or_smart_to_replacement_disk(self):
        self.write(self.folder / 'device/hwmon/hwmon4/temp1_input', '41000\n')
        self.refresh()
        self.publish_disks()
        original = self.collector.snapshot()['disks']['items'][0]
        self.assertEqual(original['temp_c'], 37)
        self.assertEqual(original['model'], 'Fixture physical disk')
        self.folder.rename(self.sys / 'block/replaced-fixture')
        self.folder = self.block('sda', '8:0', 'new-disk-without-hwmon')
        self.assertEqual(self.collector.snapshot()['disks']['items'], [],
            'the previous lifecycle must not be rebound to the replacement device')
        self.publish_disks()
        replacement = self.collector.snapshot()['disks']['items'][0]
        self.assertNotEqual(replacement['stable_identity'], original['stable_identity'])
        self.assertIsNone(replacement['temp_c'])
        self.assertIsNone(replacement['smart_temperature_c'])
        self.assertIsNone(replacement['health']['passed'])
        self.assertEqual(len(self.calls), 1)

    def replace_during_row_sample(self):
        """Swap only this real fixture device; never touch a host block node."""
        self.folder.rename(self.sys / 'block/previous-row-device-fixture')
        self.folder = self.block('sda', '8:0', 'replacement-during-row-sample')
        self.write(self.folder / 'device/model', 'Replacement physical disk\n')
        self.write(self.folder / 'size', '2000000\n')

    def test_row_size_read_cannot_bind_old_measurement_to_new_lifetime(self):
        original = insights._integer
        replaced = [False]

        def read_then_replace(path, *args, **kwargs):
            measured = original(path, *args, **kwargs)
            if Path(path) == self.folder / 'size' and not replaced[0]:
                self.assertEqual(measured, 1000000)
                replaced[0] = True
                self.replace_during_row_sample()
            return measured

        with mock.patch.object(insights, '_integer', side_effect=read_then_replace):
            rows = self.publish_disks()
        self.assertTrue(replaced[0])
        self.assertEqual(rows, [], 'the row anchor must precede the size read')
        self.assertNotIn('sda', self.collector._disk_name_context)
        replacement = self.publish_disks()[0]
        self.assertEqual(replacement['model'], 'Replacement physical disk')
        self.assertEqual(replacement['size_bytes'], 2000000 * 512)
        self.assertIsNone(replacement['temp_c'])
        self.assertEqual(self.calls, [])

    def test_row_size_and_model_read_replacement_discards_the_entire_row(self):
        for boundary in ('size', 'model'):
            with self.subTest(boundary=boundary):
                self.setUp()
                self.write(self.folder / 'device/hwmon/hwmon4/temp1_input', '41000\n')
                replaced = [False]
                function = '_integer' if boundary == 'size' else '_text'
                original = getattr(insights, function)
                selected = self.folder / ('size' if boundary == 'size' else 'device/model')

                def read_then_replace(path, *args, **kwargs):
                    measured = original(path, *args, **kwargs)
                    if Path(path) == selected and not replaced[0]:
                        replaced[0] = True
                        self.replace_during_row_sample()
                    return measured

                with mock.patch.object(insights, function, side_effect=read_then_replace):
                    rows = self.publish_disks()
                self.assertTrue(replaced[0])
                self.assertEqual(rows, [])
                self.assertNotIn('sda', self.collector._disk_name_context)
                self.assertNotIn('sda', self.collector._disk_verified_serials)
                self.assertEqual(self.calls, [])

    def test_replacement_after_serial_name_join_cannot_reanchor_old_row(self):
        self.write(self.folder / 'device/hwmon/hwmon4/temp1_input', '41000\n')
        original = self.collector._storage_serial_match
        replaced = [False]

        def joined_then_replace(*args, **kwargs):
            answer = original(*args, **kwargs)
            if not replaced[0]:
                replaced[0] = True
                self.replace_during_row_sample()
            return answer

        with mock.patch.object(self.collector, '_storage_serial_match', side_effect=joined_then_replace):
            rows = self.publish_disks()
        self.assertTrue(replaced[0])
        self.assertEqual(rows, [], 'publication must recheck the original row lifetime')
        self.assertNotIn('sda', self.collector._disk_name_context)
        self.assertNotIn('sda', self.collector._disk_verified_serials)
        self.assertEqual(self.calls, [])

    def test_stable_same_lifetime_row_preserves_metadata_without_passive_temperature(self):
        self.write(self.folder / 'device/hwmon/hwmon4/temp1_input', '41000\n')
        identity = self.collector._disk_identity('sda', self.folder)
        rows = self.publish_disks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['stable_identity'], identity)
        self.assertIsNone(rows[0]['temp_c'])
        self.assertEqual(rows[0]['model'], 'Fixture physical disk')
        self.assertEqual(rows[0]['size_bytes'], 1000000 * 512)
        self.assertNotIn('_lifecycle', rows[0])
        self.assertEqual(self.calls, [])

    def test_deadline_expiring_during_commit_metadata_cannot_publish_new_sample(self):
        self.refresh()

        class DelayedItems(dict):
            def items(cache):
                # Fixed cache pruning is the last metadata work before commit.
                # Advancing this test clock models that work using the remaining
                # budget; it must not renew the original absolute window.
                self.now += 31
                return super().items()

        self.collector._smart_cache = DelayedItems(self.collector._smart_cache)
        self.behavior = (0, self.good_response(69))
        result = self.refresh()
        self.assert_receipt(result, {'sda': 'failed'})
        self.assertEqual(result['results'][0]['reason'], 'timeout')
        self.assertEqual(self.collector._smart_cache['sda'][1]['smart_temperature_c'], 37)
        self.assertEqual(len(self.calls), 2)

    def test_old_background_enumeration_cannot_prune_newly_refreshed_disk_cache(self):
        # _disks captures its real sorted sysfs list before reading per-device
        # size. Insert a new physical disk and an explicit refresh at
        # that boundary; no real thread scheduling or SMART process is needed.
        original = insights._integer
        inserted = [False]
        receipts = []

        def add_after_enumeration(path, *args, **kwargs):
            if Path(path) == self.folder / 'size' and not inserted[0]:
                inserted[0] = True
                self.block('sdb', '8:16', 'disk-added-after-old-enumeration')
                receipts.append(self.refresh())
            return original(path, *args, **kwargs)

        with mock.patch.object(insights, '_integer', side_effect=add_after_enumeration):
            old_round = self.collector._disks()
        self.assertEqual([item['id'] for item in old_round['items']], ['sda'])
        self.assert_receipt(receipts[0], {'sda': 'refreshed', 'sdb': 'refreshed'})
        cached = self.collector.smart_cached('sdb')
        self.assertIsNotNone(cached, 'stale background active list erased newly published manual cache')
        self.assertTrue(cached['health']['passed'])
        self.assertEqual(cached['smart_temperature_c'], 37)
        self.assertEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main()

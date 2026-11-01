"""Real overview HTTP with selective non-disk cache reads and unchanged gates.

Only the external SMART runner, capacity worker, mount watch and portable
device-identity seam are substituted. Seventeen parsed mount rows model eight
ext4 filesystems, eight unsupported FUSE mounts and one unsupported eMMC mount.
No NAS, real smartctl/statvfs, user directory or background sampler is used.
"""
import concurrent.futures
import copy
import json
import os
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import test_smart_power_compat as power
import test_capacity_refresh_http as capacity_http

service = power.service
insights_module = power.smart_fixture.insights
panel_module = power.smart_fixture.panel_module


class FullOnlyInsights:
    """A legacy provider deliberately offers no selective-cache capability."""
    def __init__(self, original):
        self.original = original

    def snapshot(self):
        return self.original.snapshot()

    def __getattr__(self, name):
        if name == 'panel_cache_snapshot':
            raise AttributeError(name)
        return getattr(self.original, name)


class OverviewProjectionHTTPTests(unittest.TestCase):
    READ_COUNTS = []

    def setUp(self):
        self.core = power.SmartPowerCompatTests()
        self.core.setUp()
        self.addCleanup(self.core.doCleanups)
        self.fixture = capacity_http.CapacityRefreshHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.web, self.server = capacity_http.web_config, self.fixture.server
        self.collector, self.monitor, self.sampler = self.core.collector, self.core.monitor, self.core.sampler
        self.server.insights, self.server.monitor = self.collector, self.monitor
        self.server.panel = self.core.panel
        self.panel = self.server.panel
        self.panel.web, self.panel.controller = self.server, self.server.controller
        self.partition_names, specs = {}, []
        for index, name in enumerate(('sda', 'sdb', 'sdc', 'sdd')):
            for part in (1, 2, 3, 4):
                device = '8:' + str(index * 16 + part)
                self.partition_names[device] = name
                specs.append((101 + len(specs), device, name, part,
                              'ext4' if part <= 2 else 'fuseblk'))
        specs.append((117, '179:1', 'mmcblk0', 1, 'ext4'))
        self.core.write(self.core.proc / 'self/mountinfo', ''.join(
            '%s 1 %s / /drives/fixture-%s-%s rw - %s /dev/%s%s rw\n'
            % (mount_id, device, name, part, filesystem, name, part)
            for mount_id, device, name, part, filesystem in specs))
        self.patch(service, '_capacity_device', side_effect=self.capacity_device)
        self.core.responses = {name: self.core.response({'name': 'ACTIVE'})
                               for name in ('sda', 'sdb', 'sdc', 'sdd')}
        _, receipt, _, _ = self.core.refresh_visible()
        self.assertEqual(len(receipt['results']), 17)
        self.assertEqual(sum(row['status'] == 'refreshed' for row in receipt['results']), 8)
        self.assertEqual(len(self.sampler._capacity_cache), 8)
        # Monitoring remains a production cache and is deliberately not
        # optimized by this change. Only Windows' missing metadata flag is a
        # portable OS seam; all opened metadata files stay in the TEMP fixture.
        import monitoring
        if not hasattr(os, 'O_NONBLOCK'):
            self.patch(os, 'O_NONBLOCK', new=0, create=True)
        self.analytics = monitoring.MonitoringService(
            self.core.temporary / 'analytics', self.collector, self.monitor,
            SimpleNamespace(snapshot=lambda: []), proc_root=self.core.proc, sys_root=self.core.sys,
            clock=lambda: self.core.now, wall_clock=lambda: self.core.wall)
        self.analytics.settings['history_enabled'] = False
        self.analytics.sample_once()
        self.addCleanup(self.analytics.stop)
        self.panel.monitoring = self.server.monitoring = self.analytics
        self.runner_count, self.worker_count = len(self.core.calls), len(self.core.capacity_calls)
        self.forbidden = {'statvfs': 0, 'fstatvfs': 0, 'Popen': 0}
        def deny(name):
            def rejected(*args, **kwargs):
                self.forbidden[name] += 1
                raise AssertionError('overview must not collect disk measurements')
            return rejected
        self.patch(service.os, 'statvfs', create=True, side_effect=deny('statvfs'))
        self.patch(service.os, 'fstatvfs', create=True, side_effect=deny('fstatvfs'))
        self.patch(service.subprocess, 'Popen', side_effect=deny('Popen'))

    def patch(self, target, name, **kwargs):
        patch = mock.patch.object(target, name, **kwargs)
        result = patch.start()
        self.addCleanup(patch.stop)
        return result

    def capacity_device(self, _sys, device):
        name = self.partition_names.get(device)
        if name is None:
            return None
        lifecycle = self.collector._smart_context(name)
        return ('portable-kernel-fixture', lifecycle, name) if lifecycle else None

    def request(self, fixture=None):
        self.monitor.updated_at = time.monotonic()
        code, value = (fixture or self.fixture).request(method='GET', path='/api/overview')
        return code, value

    def assert_no_collection(self):
        self.assertEqual(len(self.core.calls), self.runner_count)
        self.assertEqual(len(self.core.capacity_calls), self.worker_count)
        self.assertEqual(self.forbidden, {'statvfs': 0, 'fstatvfs': 0, 'Popen': 0})

    @staticmethod
    def normalize(value, path=()):
        # Request wall time and derived boot time do not describe a cached
        # disk sample. Preserve capacity/SMART dates and every business value.
        if isinstance(value, dict):
            return {key: OverviewProjectionHTTPTests.normalize(item, path + (key,))
                    for key, item in value.items()
                    if not (key == 'ts' and not path or key == 'boot_time')}
        if isinstance(value, list):
            return [OverviewProjectionHTTPTests.normalize(item, path + ('[]',)) for item in value]
        return value

    def measured(self, value):
        return [row for row in value['disks_detail'] if row.get('total') is not None]

    def counted_request(self, *, legacy=False):
        original = self.panel.insights
        self.panel.insights = FullOnlyInsights(self.collector) if legacy else self.collector
        try:
            with mock.patch.object(self.collector, 'snapshot', wraps=self.collector.snapshot) as full, \
                    mock.patch.object(self.collector, 'panel_cache_snapshot', wraps=self.collector.panel_cache_snapshot) as cache, \
                    mock.patch.object(self.sampler, 'capacity_snapshot', wraps=self.sampler.capacity_snapshot) as capacity, \
                    mock.patch.object(insights_module, '_text', wraps=insights_module._text) as reads:
                code, value = self.request()
                counts = {'full': full.call_count, 'cache': cache.call_count,
                          'capacity': capacity.call_count, 'text_reads': reads.call_count}
        finally:
            self.panel.insights = original
        self.assertEqual(code, 200)
        return value, counts

    def test_current_http_matches_legacy_with_two_full_and_four_capacity_checks(self):
        legacy, old = self.counted_request(legacy=True)
        selective, new = self.counted_request()
        self.assertEqual(self.normalize(selective), self.normalize(legacy))
        self.assertEqual(len(self.measured(selective)), 4)
        self.assertEqual(old['full'], 26)
        self.assertEqual(old['cache'], 0)
        self.assertEqual(new['full'], 2)
        self.assertEqual(new['cache'], 24)
        self.assertEqual(old['capacity'], new['capacity'])
        self.assertEqual(new['capacity'], 4)
        self.assertLess(new['text_reads'], old['text_reads'])
        type(self).READ_COUNTS.append({'state': 'current', 'legacy': old, 'selective': new})
        self.assert_no_collection()

    def test_real_fast_counter_frames_preserve_positive_non_disk_http_values(self):
        # These are kernel-format TEMP inputs, not precomputed panel values.
        # Real fast collection must derive the CPU delta, memory and NIC rates
        # before both real HTTP paths consume the identical cache.
        import datetime
        # The integration timestamp uses a wall-clock seam independent of the
        # collector's injected monotonic clock; bind both to this TEMP sample.
        self.patch(insights_module, '_timestamp', side_effect=lambda:
                   datetime.datetime.fromtimestamp(self.core.wall, datetime.timezone.utc).isoformat())
        self.core.write(self.core.proc / 'cpuinfo', 'processor : 0\nmodel name : Fixture CPU\n')
        self.core.write(self.core.proc / 'meminfo',
                        'MemTotal: 1000 kB\nMemAvailable: 400 kB\n'
                        'SwapTotal: 200 kB\nSwapFree: 150 kB\n')
        self.core.write(self.core.proc / 'loadavg', '1.0 0.5 0.25 1/4 42\n')
        self.core.write(self.core.proc / 'uptime', '120.0 60.0\n')
        self.core.write(self.core.proc / 'net/route',
                        'Iface Destination Gateway Flags RefCnt Use Metric Mask\n'
                        'eth0 00000000 00000000 0001 0 0 10 00000000\n')
        interface = self.core.sys / 'class/net/eth0'
        self.core.write(interface / 'operstate', 'up\n')
        self.core.write(self.core.proc / 'stat',
                        'cpu 100 0 100 800 0 0 0 0\ncpu0 100 0 100 800 0 0 0 0\n')
        self.core.write(interface / 'statistics/rx_bytes', '10000\n')
        self.core.write(interface / 'statistics/tx_bytes', '20000\n')
        self.collector._collect_fast()
        self.core.now += 2
        self.core.wall += 2
        self.core.write(self.core.proc / 'stat',
                        'cpu 150 0 150 900 0 0 0 0\ncpu0 150 0 150 900 0 0 0 0\n')
        self.core.write(interface / 'statistics/rx_bytes', '12000\n')
        self.core.write(interface / 'statistics/tx_bytes', '24000\n')
        self.collector._collect_fast()
        self.analytics.sample_once()

        legacy, old = self.counted_request(legacy=True)
        selective, new = self.counted_request()
        self.assertEqual(self.normalize(selective), self.normalize(legacy))
        self.assertEqual(selective['cpu']['percent'], 50.0)
        self.assertEqual(selective['cpu']['per_core'], [50.0])
        self.assertEqual(selective['cpu']['model'], 'Fixture CPU')
        self.assertEqual(selective['mem']['total'], 1000 * 1024)
        self.assertEqual(selective['mem']['used'], 600 * 1024)
        self.assertEqual(selective['mem']['available'], 400 * 1024)
        self.assertEqual(selective['mem']['percent'], 60.0)
        self.assertEqual(selective['mem']['swap_used'], 50 * 1024)
        rows = [row for row in selective['net'] if row['name'] == 'eth0']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['rx_bytes'], rows[0]['tx_bytes']), (12000, 24000))
        self.assertEqual((rows[0]['rx_rate'], rows[0]['tx_rate']), (1000.0, 2000.0))
        self.assertEqual(rows[0]['operstate'], 'up')
        self.assertEqual(rows[0]['ipv4'], [])
        self.assertEqual((selective['net_aggregate']['rx_rate'],
                          selective['net_aggregate']['tx_rate']), (1000.0, 2000.0))
        self.assertEqual((old['full'], new['full'], new['cache']), (26, 2, 24))
        self.assertEqual((old['capacity'], new['capacity']), (4, 4))
        self.assertEqual(len(self.measured(selective)), 4)
        self.assert_no_collection()

    def test_unknown_http_matches_legacy_without_skipping_metadata_revalidation(self):
        self.core.watch.changed.return_value = True
        self.sampler.capacity_snapshot()
        self.core.watch.changed.return_value = False
        legacy, old = self.counted_request(legacy=True)
        selective, new = self.counted_request()
        self.assertEqual(self.normalize(selective), self.normalize(legacy))
        self.assertEqual(self.measured(selective), [])
        self.assertTrue(all(row['mounts'] for row in selective['disks_detail']))
        self.assertEqual((new['full'], new['cache'], new['capacity']), (2, 24, 4))
        self.assertEqual(old['capacity'], 4)
        self.assertLess(new['text_reads'], old['text_reads'])
        type(self).READ_COUNTS.append({'state': 'unknown', 'legacy': old, 'selective': new})
        self.assert_no_collection()

    def test_multiple_http_requests_keep_original_dates_and_see_new_cache_without_ttl(self):
        _, first = self.request()
        dates = {row['name']: row['capacity_sampled_at'] for row in first['disks_detail']}
        for ordinal, cpu in enumerate((19, 41, 73), 1):
            self.core.now += 1
            self.core.wall += 1
            with self.collector._lock:
                self.collector._state['system']['cpu_usage_percent'] = cpu
            code, value = self.request()
            self.assertEqual(code, 200)
            self.assertEqual(value['cpu']['percent'], cpu)
            self.assertEqual({row['name']: row['capacity_sampled_at'] for row in value['disks_detail']}, dates)
            self.assertEqual(len(self.measured(value)), 4)
        self.assert_no_collection()

    def test_swap_drops_changed_disk_and_preserves_real_lifetime_rejection(self):
        folder = self.core.sys / 'block/sda'
        folder.rename(self.core.sys / 'block/removed-sda')
        self.core.block('sda', '8:0', 'replacement-fixture-device')
        # Observe the real first transition independently: one response may
        # report identity_changed, while later stable empty projections report
        # unavailable. Do not hide either distinction in payload normalization.
        code, rejected = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(self.measured(rejected), [])
        self.assertFalse(self.sampler._capacity_cache)
        self.assertTrue(all(row['temp'] is None and row['health'] is None
                            for row in rejected['disks_detail'] if row['name'] == 'sda'))
        legacy, _ = self.counted_request(legacy=True)
        selective, _ = self.counted_request()
        self.assertEqual(self.normalize(selective), self.normalize(legacy))
        self.assertEqual(self.measured(selective), [])
        self.assert_no_collection()

    def test_actual_unmount_metadata_and_namespace_event_still_clear_current(self):
        text = (self.core.proc / 'self/mountinfo').read_text(encoding='utf-8')
        self.core.write(self.core.proc / 'self/mountinfo', '\n'.join(text.splitlines()[1:]) + '\n')
        code, value = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(self.measured(value), [])
        self.assertFalse(self.sampler._capacity_cache)
        self.assert_no_collection()

    def test_real_namespace_watch_is_not_cached_away_by_selective_getter(self):
        with mock.patch.object(self.sampler, '_capacity_check_watch', wraps=self.sampler._capacity_check_watch) as checked:
            self.core.watch.changed.return_value = True
            code, value = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(self.measured(value), [])
        self.assertEqual(checked.call_count, 4)
        self.assertFalse(self.sampler._capacity_cache)
        self.assert_no_collection()

    def test_json_owner_revocation_discards_entire_actual_http_response(self):
        original = self.web.json.dumps
        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and 'disks_detail' in value:
                self.server.auth_mode = 'pair'
            return original(value, *args, **kwargs)
        with mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            code, value = self.request()
        self.assertEqual(code, 403)
        self.assertNotIn('disks_detail', value)
        self.assert_no_collection()

    def test_cache_hook_exception_or_wrong_shape_never_falls_back_to_full_snapshot(self):
        for mode in ('exception', 'wrong_shape'):
            with self.subTest(mode=mode), \
                    mock.patch.object(self.collector, 'snapshot', wraps=self.collector.snapshot) as full, \
                    mock.patch.object(self.collector, 'panel_cache_snapshot',
                        **({'side_effect': ValueError('private-selective-observer-canary')}
                           if mode == 'exception' else {'return_value': []})):
                code, value = self.request()
                self.assertEqual(code, 200)
                self.assertIsNone(value['cpu']['percent'])
                self.assertEqual(len(self.measured(value)), 4)
                self.assertEqual(full.call_count, 2)
                self.assertNotIn('private-selective-observer-canary', json.dumps(value))
        self.assert_no_collection()

    def test_smart_stale_and_future_remain_unknown_without_losing_dated_capacity(self):
        _, first = self.request()
        dates = {row['name']: row['capacity_sampled_at'] for row in first['disks_detail']}
        for now in (self.core.now - 1, self.core.now + 31):
            with self.subTest(now=now):
                self.core.now = now
                code, value = self.request()
                self.assertEqual(code, 200)
                self.assertTrue(all(row['temp'] is None and row['health'] is None
                                    and row['smart_fresh'] is False for row in value['disks_detail']))
                self.assertEqual({row['name']: row['capacity_sampled_at'] for row in value['disks_detail']}, dates)
                self.assertEqual(len(self.measured(value)), 4)
        self.assert_no_collection()

    def test_pure_projection_observer_fault_does_not_change_http_result(self):
        _, baseline = self.request()
        with mock.patch.object(self.panel, '_try_capacity_projection_end', side_effect=ValueError('observer-canary')):
            code, value = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(self.normalize(value), self.normalize(baseline))
        self.assert_no_collection()

    def test_concurrent_distinct_collectors_do_not_share_request_or_thread_cache(self):
        other = insights_module.InsightsCollector(
            self.core.proc, self.core.sys, runner=self.core.runner,
            command_finder=lambda _: '/fixture/bin/smartctl', clock=lambda: self.core.now,
            page_size=4096, clock_ticks=100, ipv4_provider=lambda: {},
            storage_client=SimpleNamespace(snapshot=lambda: {'available': False, 'disks': [], 'spaces': []}))
        self.addCleanup(other.stop)
        with other._lock:
            other._state = copy.deepcopy(self.collector._state)
        other._smart_measurements = copy.deepcopy(self.collector._smart_measurements)
        other._smart_visible_observations = copy.deepcopy(self.collector._smart_visible_observations)
        other._smart_attempts = copy.deepcopy(self.collector._smart_attempts)
        second = capacity_http.CapacityRefreshHTTPTests()
        second.setUp()
        self.addCleanup(second.doCleanups)
        second.server.monitor = self.monitor
        second.server.insights = other
        second.server.panel = panel_module.FnmonitorPanel(
            second.server.controller, other, self.analytics, None, None, self.monitor,
            state_dir=self.core.temporary / 'other-panel', web=second.server,
            clock=lambda: self.core.now, wall_clock=lambda: self.core.wall,
            proc_root=self.core.proc, sys_root=self.core.sys,
            dev_root=self.core.temporary / 'dev', udev_root=self.core.temporary / 'run/udev')
        with self.collector._lock:
            self.collector._state['system']['cpu_usage_percent'] = 21
        with other._lock:
            other._state['system']['cpu_usage_percent'] = 83
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(self.request, endpoint) for endpoint in (self.fixture, second) for _ in range(3)]
            values = [future.result(timeout=8) for future in requests]
        self.assertEqual([value['cpu']['percent'] for code, value in values], [21] * 3 + [83] * 3)
        self.assertTrue(all(code == 200 for code, value in values))
        self.assert_no_collection()


if __name__ == '__main__':
    unittest.main()

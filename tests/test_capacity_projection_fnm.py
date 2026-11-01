"""Capacity display breadcrumbs observe the real cached projection, never replay it.

The shared SMART fixture replaces only external commands, the capacity worker,
and kernel identity seams. StatusSampler, StatusMonitor and FnmonitorPanel remain
the production implementations. This is not a real-mount or NAS acceptance test.
"""
import copy
import json
from pathlib import Path
import threading
import unittest
from unittest import mock

import test_smart_power_compat as power_fixture


class CapacityProjectionFnmTests(unittest.TestCase):
    def setUp(self):
        self.case = power_fixture.SmartPowerCompatTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.panel = self.case.panel

    def seed(self):
        self.case.ordinary_mix()
        return self.case.refresh_visible()[3]

    def diagnostics(self):
        return self.panel.capacity_projection_diagnostics()

    def test_initial_memory_view_marks_all_unexecuted_stages_not_observed(self):
        with mock.patch.object(self.panel, '_basic', side_effect=AssertionError('unexpected snapshot')), \
                mock.patch.object(self.panel, '_mounts', side_effect=AssertionError('unexpected mount read')):
            value = self.diagnostics()
        self.assertEqual(value['sequence'], 0)
        self.assertEqual(value['ring'], [])
        self.assertTrue(all(row['result'] == 'not_observed' for row in value['latest'].values()))
        self.assertFalse(value['atomic'])

    def test_real_normal_chain_records_matching_dates_and_independent_summary_inputs(self):
        overview = self.seed()
        value = self.diagnostics()
        disks, summary, final = (value['latest'][key] for key in ('disks', 'summary', 'overview'))
        self.assertEqual(disks['row_count'], 4)
        self.assertEqual(disks['measured_count'], 3)
        self.assertEqual(disks['counts']['matched'], 4)
        self.assertEqual(disks['counts']['capacity_unknown'], 1)
        self.assertEqual(disks['counts']['contributed'], 3)
        self.assertEqual({row['device'] for row in disks['devices']}, {'sda', 'sdb', 'sdc', 'sdd'})
        self.assertEqual(summary['counts']['contributed'], 3)
        self.assertTrue(summary['summary_has_value'])
        self.assertEqual(disks['sampled_at_oldest'], overview['disks_detail'][1]['capacity_sampled_at'])
        self.assertEqual(final['children']['disks']['sequence'], disks['sequence'])
        self.assertEqual(final['children']['summary']['sequence'], summary['sequence'])
        self.assertNotEqual(disks['operation_id'], summary['operation_id'])
        self.assertFalse(final['atomic'])
        # Each is the Monitor sideband for this call, not a later global value.
        for event in (disks, summary):
            self.assertEqual(event['monitor_source']['status'], 'observed')
            self.assertEqual(event['monitor_source']['result'], 'returned')
        self.assertNotEqual(disks['monitor_source']['sequence'], summary['monitor_source']['sequence'])

    def test_fnm_mount_matching_failure_is_visible_without_erasing_sampler_cache(self):
        self.seed()
        before = copy.deepcopy(self.case.sampler._capacity_cache)
        self.panel._mount_cache = (self.case.now, {name: ['/drives/unmatched-' + name]
                                                 for name in self.case.mounts})
        result = self.panel.overview()
        value = self.diagnostics()
        self.assertEqual(self.case.sampler._capacity_cache, before)
        self.assertIsNone(result['disk_total'])
        self.assertTrue(all(row['percent'] is None for row in result['disks_detail']))
        self.assertEqual(value['latest']['disks']['counts']['unknown'], 4)
        self.assertEqual(value['latest']['disks']['measured_count'], 0)
        self.assertEqual(value['latest']['summary']['counts']['out_of_scope'], 4)
        self.assertFalse(value['latest']['summary']['summary_has_value'])
        self.assertEqual(value['latest']['disks']['monitor_source']['measured_count'], 3)

    def test_monitor_projection_exception_preserves_fallback_and_cache_but_records_stage(self):
        self.seed()
        before = copy.deepcopy(self.case.sampler._capacity_cache)
        with mock.patch.object(self.case.sampler, 'capacity_snapshot',
                               side_effect=RuntimeError('token-path-private-canary')):
            result = self.panel.overview()
        value = self.diagnostics()
        self.assertEqual(self.case.sampler._capacity_cache, before)
        self.assertIsNone(result['disk_total'])
        self.assertTrue(all(row['percent'] is None for row in result['disks_detail']))
        self.assertTrue(any(row['stage'] == 'safe' and row['result'] == 'projection_error'
                            for row in value['ring']))
        self.assertEqual(value['latest']['disks']['children']['basic']['result'], 'returned')
        self.assertEqual(value['latest']['disks']['children']['basic']['input_result'], 'projection_error')
        self.assertNotIn('token-path-private-canary', json.dumps(value))

    def test_real_cache_invalidation_is_distinct_from_a_projection_exception(self):
        self.seed()
        self.case.watch.changed.return_value = True
        result = self.panel.overview()
        value = self.diagnostics()
        self.assertFalse(self.case.sampler._capacity_cache)
        self.assertTrue(all(row['percent'] is None for row in result['disks_detail']))
        self.assertEqual(value['latest']['safe']['result'], 'returned')
        self.assertEqual(value['latest']['disks']['monitor_source']['measured_count'], 0)
        self.assertEqual(value['latest']['disks']['measured_count'], 0)
        self.assertEqual(value['latest']['summary']['result'], 'returned')

    def test_diagnostics_only_copies_ram_and_never_samples_or_reads_mounts(self):
        self.seed()
        expected = self.diagnostics()
        with mock.patch.object(self.panel, '_basic', side_effect=AssertionError('basic')), \
                mock.patch.object(self.panel, '_mounts', side_effect=AssertionError('mounts')), \
                mock.patch.object(self.panel.monitor, 'snapshot', side_effect=AssertionError('monitor')), \
                mock.patch.object(self.case.sampler, 'capacity_snapshot', side_effect=AssertionError('inventory')), \
                mock.patch.object(self.case.sampler, '_capacity_probe', side_effect=AssertionError('worker')), \
                mock.patch.object(self.case.collector, 'snapshot', side_effect=AssertionError('collector')), \
                mock.patch.object(Path, 'read_text', side_effect=AssertionError('file read')):
            actual = self.diagnostics()
            actual['ring'].clear()
            again = self.diagnostics()
        self.assertEqual(again, expected)

    def test_none_monitor_preserves_old_safe_fallback_without_claimed_evidence(self):
        self.panel.monitor = None
        self.assertEqual(self.panel._basic(), {})
        event = self.diagnostics()['latest']['basic']
        self.assertEqual(event['monitor_source'], {'status': 'not_observed'})
        self.assertEqual(event['row_count'], 0)
        self.assertEqual(event['result'], 'returned')

    def test_non_dict_monitor_result_keeps_original_fallback_and_records_invalid_type(self):
        monitor = mock.Mock()
        self.panel.monitor = monitor
        for invalid in (None, [], 7, 'private-invalid-result-canary'):
            with self.subTest(kind=type(invalid).__name__):
                monitor.snapshot.return_value = invalid
                self.assertEqual(self.panel._basic(), {})
                value = self.diagnostics()
                self.assertEqual(value['latest']['safe']['result'], 'invalid_type')
                self.assertEqual(value['latest']['basic']['input_result'], 'invalid_type')
                self.assertEqual(value['latest']['basic']['row_count'], 0)
                self.assertNotIn('private-invalid-result-canary', json.dumps(value))
        fallback = {'fallback': True}
        self.assertIs(self.panel._safe(monitor, fallback), fallback)

    def test_observation_exceptions_do_not_change_normal_business_values(self):
        expected = self.seed()
        for method in ('_capacity_projection_begin', '_capacity_projection_end', '_capacity_projection_hint'):
            with self.subTest(method=method), mock.patch.object(
                    self.panel, method, side_effect=RuntimeError('diagnostic-observer-canary')):
                self.assertEqual(self.panel.overview(), expected)
            self.assertEqual(getattr(self.panel._capacity_projection_local, 'stack', []), [])
        with mock.patch.object(self.panel.monitor, 'capacity_projection_observation',
                               side_effect=RuntimeError('diagnostic-observer-canary')):
            self.assertEqual(self.panel.overview(), expected)

    def test_hint_recording_failure_does_not_change_capacity_matching_or_summary(self):
        expected = self.seed()
        original = self.panel._capacity_projection_begin
        class BrokenCounts(dict):
            def get(self, *args):
                raise RuntimeError('private-hint-canary')
        def broken(stage):
            frame = original(stage)
            frame['counts'] = BrokenCounts()
            return frame
        with mock.patch.object(self.panel, '_capacity_projection_begin', side_effect=broken):
            self.assertEqual(self.panel.overview(), expected)

    def test_monitor_sideband_is_bound_to_original_return_before_safe_deepcopy(self):
        original = {'disks': [{'mount': '/private/canary', 'total_bytes': 100, 'used_bytes': 25,
                                'capacity_sampled_at': 1700000000.0}]}
        source = {'status': 'observed', 'sequence': 777, 'operation_id': 42,
                  'sampled_monotonic': 10.0, 'sampled_at': 1700000000.0,
                  'result': 'returned', 'row_count': 1, 'measured_count': 1,
                  'date_digest': 'a' * 64, 'capacity_epoch': 3, 'mount_epoch': 4,
                  'atomic_with_capacity': True, 'raw_path': '/private/canary',
                  'token': 'source-private-canary'}
        self.panel.monitor = mock.Mock()
        self.panel.monitor.snapshot.return_value = original
        def observation(value):
            self.assertIs(value, original)
            return source
        self.panel.monitor.capacity_projection_observation.side_effect = observation
        actual = self.panel._basic()
        self.assertEqual(actual, original)
        self.assertIsNot(actual, original)
        event = self.diagnostics()['latest']['basic']
        self.assertEqual(event['monitor_source']['sequence'], 777)
        self.assertEqual(event['monitor_source']['capacity_epoch'], 3)
        self.assertNotIn('/private/canary', json.dumps(self.diagnostics()))
        self.assertNotIn('source-private-canary', json.dumps(self.diagnostics()))

    def test_ring_is_bounded_and_old_sample_dates_do_not_become_request_dates(self):
        self.seed()
        old = self.diagnostics()['latest']['disks']['sampled_at_oldest']
        for _ in range(40):
            self.panel._disks()
        value = self.diagnostics()
        self.assertEqual(len(value['ring']), 16)
        self.assertGreater(value['dropped_count'], 0)
        self.assertEqual(value['latest']['disks']['sampled_at_oldest'], old)
        self.assertEqual(value['latest']['disks']['sampled_at_newest'], old)

    def test_counter_exhaustion_is_explicit_and_does_not_change_projection_values(self):
        self.seed()
        expected = self.panel._basic()
        self.panel._capacity_projection_operation = 2 ** 53 - 1
        sequence = self.diagnostics()['sequence']
        self.assertEqual(self.panel._basic(), expected)
        value = self.diagnostics()
        self.assertTrue(value['sequence_saturated'])
        self.assertEqual(value['sequence'], sequence)

    def test_concurrent_basic_calls_keep_exact_source_sequence_and_date(self):
        barrier = threading.Barrier(2)
        local = threading.local()
        monitor = mock.Mock()
        def snapshot():
            index = int(threading.current_thread().name.rsplit('-', 1)[-1])
            local.value = {'disks': [{'total_bytes': 100, 'used_bytes': 25,
                                     'capacity_sampled_at': 1700000000.0 + index}]}
            local.index = index
            barrier.wait(timeout=5)
            return local.value
        def observation(value):
            self.assertIs(value, local.value)
            return {'status': 'observed', 'sequence': local.index, 'operation_id': local.index,
                    'row_count': 1, 'measured_count': 1, 'result': 'returned',
                    'sampled_at': 1700000000.0 + local.index, 'sampled_monotonic': 100.0,
                    'capacity_epoch': 3, 'mount_epoch': 4, 'atomic_with_capacity': True}
        monitor.snapshot.side_effect = snapshot
        monitor.capacity_projection_observation.side_effect = observation
        self.panel.monitor = monitor
        errors = []
        def run():
            try:
                self.panel._basic()
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=run, name='capacity-projection-%s' % index)
                   for index in (101, 202)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        basics = [row for row in self.diagnostics()['ring'] if row['stage'] == 'basic']
        self.assertEqual(len(basics), 2)
        self.assertEqual({row['monitor_source']['sequence'] for row in basics}, {101, 202})
        self.assertEqual(len({row['operation_id'] for row in basics}), 2)
        for row in basics:
            self.assertEqual(row['sampled_at_oldest'], 1700000000.0 + row['monitor_source']['sequence'])

    def test_old_monitor_without_sideband_does_not_synthesize_latest_correlation(self):
        class OldMonitor:
            def snapshot(self):
                return {'disks': [{'total_bytes': 100, 'used_bytes': 25,
                                   'capacity_sampled_at': 1700000000.0}]}
        self.panel.monitor = OldMonitor()
        self.assertEqual(self.panel._basic()['disks'][0]['used_bytes'], 25)
        self.assertEqual(self.diagnostics()['latest']['basic']['monitor_source'], {'status': 'not_observed'})

    def test_existing_filesystem_deduplication_is_counted_without_changing_values(self):
        self.seed()
        before = self.panel._disks()
        cached = self.panel._mount_cache[1]
        alias = '/drives/duplicate-sdb'
        cached['sdb'].append(alias)
        self.panel._mount_devices[alias] = self.panel._mount_devices[cached['sdb'][0]]
        original_basic = self.panel._basic
        def duplicated():
            data = original_basic()
            row = copy.deepcopy(next(row for row in data['disks']
                                     if row['mount'] == self.case.mounts['sdb'][2]))
            row['mount'] = alias
            data['disks'].append(row)
            return data
        with mock.patch.object(self.panel, '_basic', side_effect=duplicated):
            actual = self.panel._disks()
            summary = self.panel._disk_summary()
        self.assertEqual([row['total'] for row in actual], [row['total'] for row in before])
        self.assertEqual(summary, (9000, 2250, 25.0))
        value = self.diagnostics()
        self.assertEqual(value['latest']['disks']['counts']['deduplicated'], 1)
        self.assertEqual(value['latest']['summary']['counts']['deduplicated'], 1)


if __name__ == '__main__':
    unittest.main()

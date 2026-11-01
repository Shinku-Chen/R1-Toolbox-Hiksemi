"""Capacity decisions are remembered by production paths, never replayed by readers."""
import contextlib
import copy
import json
import os
import sys
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest import mock

import test_capacity_refresh_core as core

service = core.service
REAL_DEVICE = service._capacity_device


class CapacityLedgerCoreTests(unittest.TestCase):
    """Compose the existing fixture; do not inherit and re-run its test methods."""

    def setUp(self):
        self.case = core.CapacityCoreTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.sampler = self.case.sampler

    def refresh(self):
        return self.case.refresh()

    def diagnostics(self):
        value = self.sampler.capacity_cache_diagnostics()
        self.assertEqual(value['schema_version'], 1)
        return value

    def first(self, reason):
        value = self.diagnostics()
        first = value['first_invalidation']
        self.assertIsNotNone(first)
        self.assertEqual(first['reason'], reason)
        self.assertGreater(len(first['before']['records']), len(first['after']['records']))
        self.assertEqual(first['before']['records'][0]['mount_id'], 11)
        self.assertEqual(first['before']['records'][0]['sampled_at'],
                         value['last_commit']['records'][0]['sampled_at'])
        return first

    def event(self, reason):
        matches = [event for event in self.diagnostics()['events'] if event['reason'] == reason]
        self.assertTrue(matches, reason)
        return matches[-1]

    def test_cold_diagnostic_is_memory_only_and_does_not_initialize_a_watch(self):
        with mock.patch.object(service, '_capacity_inventory', side_effect=AssertionError('inventory')), \
                mock.patch.object(service, '_CapacityMountWatch', side_effect=AssertionError('watch')), \
                mock.patch.object(self.sampler, 'sample', side_effect=AssertionError('sample')), \
                mock.patch.object(self.sampler, 'capacity_snapshot', side_effect=AssertionError('projection')), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=AssertionError('worker')), \
                mock.patch.object(service.os, 'statvfs', create=True, side_effect=AssertionError('statvfs')):
            first, second = self.diagnostics(), self.diagnostics()
        self.assertEqual(first, second)
        self.assertIsNone(first['last_commit'])
        self.assertIsNone(first['first_invalidation'])
        self.assertIsNone(self.sampler._capacity_watch)

    def test_real_successful_commit_records_original_dates_and_no_false_invalidation(self):
        receipt = self.refresh()
        value = self.diagnostics()
        self.assertIsNone(value['first_invalidation'])
        self.assertGreater(value['generation'], 0)
        self.assertEqual(value['last_commit']['reason'], 'commit_success')
        self.assertEqual(len(value['last_commit']['records']), 1)
        self.assertEqual(value['last_commit']['records'][0]['sampled_at'],
                         receipt['results'][0]['sampled_at'])
        self.assertTrue(value['latest_inventory']['complete'])

    def test_reading_diagnostics_neither_consumes_event_nor_reconciles_inventory(self):
        self.refresh()
        expected = self.diagnostics()
        self.case.watch.changed.reset_mock()
        with mock.patch.object(service, '_capacity_inventory', side_effect=AssertionError('inventory')), \
                mock.patch.object(self.sampler, '_capacity_reconcile', side_effect=AssertionError('reconcile')), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=AssertionError('worker')), \
                mock.patch.object(self.sampler, '_capacity_reap_exited', side_effect=AssertionError('reaper')), \
                mock.patch.object(service.os, 'open', side_effect=AssertionError('filesystem read')), \
                mock.patch.object(service.subprocess, 'Popen', side_effect=AssertionError('child')), \
                mock.patch.object(service.os, 'statvfs', create=True, side_effect=AssertionError('statvfs')):
            for _ in range(12):
                self.assertEqual(self.diagnostics(), expected)
        self.case.watch.changed.assert_not_called()

    def test_diagnostic_copy_is_detached_from_private_ledger(self):
        self.refresh()
        original = self.diagnostics()
        modified = self.diagnostics()
        modified['last_commit']['records'][0]['mount_id'] = 999
        modified['events'].clear()
        self.assertEqual(self.diagnostics(), original)

    def test_mount_options_change_records_full_key_change_and_original_sample(self):
        self.refresh()
        self.case.mountinfo.write_text(self.case.mountinfo.read_text().replace(' rw - ', ' ro - '))
        self.sampler.capacity_snapshot()
        first = self.first('active_keys_changed')
        self.assertNotEqual(first['before']['inventory'], first['after']['inventory'])

    def test_each_key_component_is_observed_without_leaking_mount_text(self):
        changes = {
            'parent': lambda value: value.replace('11 1 ', '11 2 '),
            'mount_options': lambda value: value.replace(' rw - ', ' ro - '),
            'super_options': lambda value: value.replace('/dev/sdb1 rw', '/dev/sdb1 ro'),
            'optional_propagation': lambda value: value.replace(' rw - ', ' rw shared:23 - '),
            'source': lambda value: value.replace('/dev/sdb1', '/dev/SOURCE_PRIVATE_CANARY'),
            'target': lambda value: value.replace('/drives/data', '/drives/TARGET_PRIVATE_CANARY'),
        }
        for label, mutate in changes.items():
            with self.subTest(component=label):
                self.case.write_mount()
                self.refresh()
                original = self.diagnostics()['last_commit']['records'][0]['key_digest']
                self.case.mountinfo.write_text(mutate(self.case.mountinfo.read_text()))
                self.sampler.capacity_snapshot()
                first = self.first('active_keys_changed')
                before_row, after_row = first['before']['inventory']['entries'][0], first['after']['inventory']['entries'][0]
                field = {'parent': 'parent_id', 'optional_propagation': 'optional_fields'}.get(label, label)
                if label == 'parent':
                    self.assertNotEqual(before_row['parent_id'], after_row['parent_id'])
                else:
                    self.assertNotEqual(before_row['components'][field], after_row['components'][field])
                self.assertTrue(first['component_changes']['mounts'][0]['changed'][field])
                self.assertNotEqual(before_row['components']['mountline'], after_row['components']['mountline'])
                self.assertEqual(first['before']['records'][0]['key_digest'], original)
                public = json.dumps(self.diagnostics())
                for canary in ('SOURCE_PRIVATE_CANARY', 'TARGET_PRIVATE_CANARY', '/drives/data', '/dev/sdb1'):
                    self.assertNotIn(canary, public)

    def test_device_generation_change_is_distinct_from_mountinfo_change(self):
        self.refresh()
        self.case.generation += 1
        self.sampler.capacity_snapshot()
        self.first('active_keys_changed')
        self.assertTrue(self.diagnostics()['latest_inventory']['complete'])

    def test_observed_unmount_remains_first_failure_after_dozen_empty_polls(self):
        self.refresh()
        self.case.mountinfo.write_text('')
        self.sampler.capacity_snapshot()
        expected = copy.deepcopy(self.first('active_keys_changed'))
        for _ in range(80):
            self.sampler.capacity_snapshot()
        value = self.diagnostics()
        self.assertEqual(value['first_invalidation'], expected)
        self.assertLessEqual(len(value['events']), 32)
        self.assertGreater(value['dropped_events'], 0)
        self.assertFalse(self.sampler._capacity_cache)

    def test_new_successful_commit_starts_new_generation_and_clears_old_first(self):
        self.refresh()
        generation = self.diagnostics()['generation']
        self.case.mountinfo.write_text('')
        self.sampler.capacity_snapshot()
        self.first('active_keys_changed')
        self.case.write_mount()
        self.refresh()
        value = self.diagnostics()
        self.assertGreater(value['generation'], generation)
        self.assertIsNone(value['first_invalidation'])
        self.assertEqual(len(value['last_commit']['records']), 1)

    def test_malformed_inventory_invalidation_has_failed_scan_evidence(self):
        self.refresh()
        self.case.mountinfo.write_text('PRIVATE_MOUNTINFO_CANARY\n')
        self.sampler.capacity_snapshot()
        first = self.first('inventory_incomplete')
        self.assertFalse(first['after']['inventory']['complete'])
        self.assertFalse(self.diagnostics()['latest_inventory']['complete'])
        self.assertNotIn('PRIVATE_MOUNTINFO_CANARY', json.dumps(self.diagnostics()))

    def test_duplicate_inventory_is_not_reported_as_complete_empty_namespace(self):
        self.refresh()
        self.case.mountinfo.write_text(self.case.mountinfo.read_text() * 2)
        self.sampler.capacity_snapshot()
        self.first('inventory_incomplete')
        self.assertFalse(self.diagnostics()['latest_inventory']['complete'])

    def test_oversized_inventory_is_explicitly_incomplete(self):
        self.refresh()
        self.case.mountinfo.write_text('x' * 262145)
        self.sampler.capacity_snapshot()
        self.first('inventory_incomplete')
        self.assertFalse(self.diagnostics()['latest_inventory']['complete'])

    def test_inventory_that_changes_during_real_device_read_is_not_mixed(self):
        self.refresh()
        def changing_identity(*_):
            self.case.write_mount(mount_id=12)
            return ('physical', self.case.generation, 'sdb')
        with mock.patch.object(service, '_capacity_device', side_effect=changing_identity):
            self.sampler.capacity_snapshot()
        self.first('inventory_incomplete')
        self.assertFalse(self.diagnostics()['latest_inventory']['complete'])

    def test_complete_scan_with_unproven_device_retains_unavailable_row_evidence(self):
        self.refresh()
        with mock.patch.object(service, '_capacity_device', return_value=None):
            self.sampler.capacity_snapshot()
        self.first('active_keys_changed')
        inventory = self.diagnostics()['latest_inventory']
        self.assertTrue(inventory['complete'])
        self.assertEqual(len(inventory['entries']), 1)
        self.assertEqual(inventory['entries'][0]['reason'], 'device_identity_unavailable')

    def test_watch_event_invalidates_positive_cache_and_is_latched(self):
        self.refresh()
        self.case.watch.observation = {'reason': 'mount_event', 'mask': 2,
                                       'namespace_read': True, 'polled': True}
        self.case.watch.changed.return_value = True
        self.sampler.capacity_snapshot()
        self.first('watch_mount_event')
        self.assertIsNone(self.sampler._capacity_watch)
        self.case.watch.close.assert_called_once()

    def test_missing_watch_is_distinct_from_kernel_event(self):
        self.refresh()
        self.sampler._capacity_watch = None
        self.sampler.capacity_snapshot()
        self.first('watch_missing')

    def test_start_and_close_record_actual_cache_removal(self):
        for action in ('start', 'close'):
            with self.subTest(action=action):
                self.sampler.start_capacity()
                self.refresh()
                before = self.diagnostics()['state']['capacity_epoch']
                getattr(self.sampler, action + '_capacity')()
                first = self.first(action)
                self.assertGreater(first['capacity_epoch'], before)
                self.assertGreater(first['mount_epoch'], first['before_epoch']['mount_epoch'])

    def test_empty_lifecycle_decisions_cannot_create_a_first_positive_failure(self):
        self.sampler.start_capacity()
        self.sampler.close_capacity()
        self.sampler.start_capacity()
        value = self.diagnostics()
        self.assertIsNone(value['first_invalidation'])
        self.assertIsNone(value['last_commit'])

    def test_denied_request_preserves_prior_commit_and_does_not_fake_invalidation(self):
        self.refresh()
        prior = self.diagnostics()['last_commit']
        with self.assertRaises(service.CapacityRefreshError) as caught:
            self.sampler.refresh_capacity(authorizer=lambda: False)
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        value = self.diagnostics()
        self.assertIsNone(value['first_invalidation'])
        self.assertEqual(value['last_commit'], prior)
        self.assertTrue(self.sampler._capacity_cache)
        self.event('batch_authorization_denied')

    def test_pending_revocation_is_visible_and_does_not_publish_or_leak_exception(self):
        allowed = [True]
        descriptors = []
        def probe(*_):
            result = self.case.probe()
            descriptors.append(result[1])
            allowed[0] = False
            return result
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            with self.assertRaises(service.CapacityRefreshError):
                self.sampler.refresh_capacity(authorizer=lambda: allowed[0])
        self.event('batch_authorization_denied')
        self.assertFalse(self.sampler._capacity_cache)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertIsNone(self.diagnostics()['first_invalidation'])

    def test_pending_timeout_is_distinct_from_authorization_denial(self):
        deadline = [time.monotonic()]
        descriptors = []
        def probe(*_):
            result = self.case.probe()
            descriptors.append(result[1])
            deadline[0] += 20
            return result
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe), \
                mock.patch.object(service.time, 'monotonic', side_effect=lambda: deadline[0]):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(caught.exception.reason, 'timeout')
        self.event('batch_timeout')
        self.assertFalse(self.sampler._capacity_cache)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_postpublication_revocation_records_historical_rollback(self):
        self.refresh()
        old = copy.deepcopy(self.sampler._capacity_cache)
        committed = copy.deepcopy(self.diagnostics()['last_commit'])
        allowed = [True]
        replace = self.sampler._capacity_replace_cache
        self.case.values['used_bytes'] = 204800
        self.case.values['usage_percent'] = 50.0
        def replace_and_revoke(records, *args, **kwargs):
            result = replace(records, *args, **kwargs)
            if records and records[11]['values']['usage_percent'] == 50:
                allowed[0] = False
            return result
        with mock.patch.object(self.sampler, '_capacity_replace_cache', side_effect=replace_and_revoke), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.case.probe):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: allowed[0])
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        self.assertEqual(self.sampler._capacity_cache, old)
        self.assertEqual(self.diagnostics()['last_commit'], committed)
        self.event('rollback_authorization_denied')

    def test_monitor_diagnostic_is_independent_of_stale_heartbeat_without_sampling(self):
        self.refresh()
        monitor = service.StatusMonitor(self.sampler)
        monitor.updated_at = time.monotonic() - 50
        monitor.snapshot()
        with mock.patch.object(monitor, 'snapshot', side_effect=AssertionError('snapshot')), \
                mock.patch.object(self.sampler, 'sample', side_effect=AssertionError('sample')), \
                mock.patch.object(self.sampler, 'capacity_snapshot', side_effect=AssertionError('capacity')), \
                mock.patch.object(service, '_capacity_inventory', side_effect=AssertionError('inventory')):
            value = monitor.capacity_cache_diagnostics()
        self.assertEqual(value['last_commit'], self.diagnostics()['last_commit'])
        self.assertEqual(value['monitor']['last_projection']['result'], 'heartbeat_stale')
        self.assertIs(value['monitor']['atomic_with_capacity'], False)
        self.assertIsNone(value['first_invalidation'])


    def test_real_inventory_limit_is_complete_for_all_128_and_incomplete_for_129(self):
        def mounts(count):
            return ''.join(f'{100 + index} 1 8:{index + 1} / /drives/disk{index} rw - ext4 /dev/sdb{index + 1} rw\n'
                           for index in range(count))
        self.case.mountinfo.write_text(mounts(128))
        self.sampler.capacity_snapshot()
        inventory = self.diagnostics()['latest_inventory']
        self.assertTrue(inventory['complete'])
        self.assertFalse(inventory['truncated'])
        self.assertEqual(len(inventory['entries']), 128)
        self.assertEqual({row['mount_id'] for row in inventory['entries']}, set(range(100, 228)))
        self.case.mountinfo.write_text(mounts(129))
        self.sampler.capacity_snapshot()
        inventory = self.diagnostics()['latest_inventory']
        self.assertFalse(inventory['complete'])
        self.assertEqual(inventory['reason'], 'entry_limit')

    def test_pending_device_change_is_not_published_and_preserves_first_actual_loss(self):
        self.refresh()
        original = self.diagnostics()['last_commit']
        descriptors = []
        def probe(*_):
            result = self.case.probe()
            descriptors.append(result[1])
            self.case.generation += 1
            return result
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            receipt = self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(receipt['results'][0]['status'], 'identity_changed')
        self.first('active_keys_changed')
        self.assertEqual(self.diagnostics()['last_commit'], original)
        self.assertFalse(self.sampler._capacity_cache)
        self.assertEqual(self.diagnostics()['state']['pending'], 0)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_postpublication_timeout_restores_original_dated_cache(self):
        self.refresh()
        old = copy.deepcopy(self.sampler._capacity_cache)
        committed = copy.deepcopy(self.diagnostics()['last_commit'])
        tick = [time.monotonic()]
        replace = self.sampler._capacity_replace_cache
        self.case.values['used_bytes'] = 204800
        self.case.values['usage_percent'] = 50.0
        def replace_and_expire(records, *args, **kwargs):
            result = replace(records, *args, **kwargs)
            if records and records[11]['values']['usage_percent'] == 50:
                tick[0] += 20
            return result
        with mock.patch.object(self.sampler, '_capacity_replace_cache', side_effect=replace_and_expire), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.case.probe), \
                mock.patch.object(service.time, 'monotonic', side_effect=lambda: tick[0]):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(caught.exception.reason, 'timeout')
        self.assertEqual(self.sampler._capacity_cache, old)
        self.assertEqual(self.diagnostics()['last_commit'], committed)
        self.event('rollback_timeout')
        self.assertIsNone(self.diagnostics()['first_invalidation'])

    def test_single_row_probe_identity_error_records_actual_removal(self):
        self.refresh()
        with mock.patch.object(self.sampler, '_capacity_probe',
                               side_effect=service.CapacityRefreshError('identity_changed')):
            receipt = self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(receipt['results'][0]['status'], 'identity_changed')
        self.first('row_identity_changed')
        self.assertFalse(self.sampler._capacity_cache)

    def test_batch_and_lifecycle_sources_are_distinct_from_passive_projection(self):
        self.refresh()
        self.assertEqual(self.diagnostics()['last_commit']['source'], 'explicit_refresh')
        self.sampler.sample()
        self.assertEqual(self.event('projection')['source'], 'sample')
        self.sampler.capacity_snapshot()
        self.assertEqual(self.event('projection')['source'], 'get_projection')
        self.sampler.close_capacity()
        self.assertEqual(self.first('close')['source'], 'close')


    def test_pending_count_is_real_batch_state_and_is_cleared_at_terminal(self):
        observed = []
        def authorize():
            observed.append(self.diagnostics()['state']['pending'])
            return True
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.case.probe):
            self.sampler.refresh_capacity(authorizer=authorize)
        self.assertIn(1, observed)
        self.assertEqual(observed[0], 0)
        self.assertEqual(self.diagnostics()['state']['pending'], 0)
        self.assertEqual(self.event('batch_finished')['counts']['pending'], 0)

    def test_negative_cache_is_recorded_without_inventing_a_successful_commit(self):
        with mock.patch.object(self.sampler, '_capacity_probe',
                               side_effect=service.CapacityRefreshError('probe_failed')):
            receipt = self.sampler.refresh_capacity_missing(authorizer=lambda: True, eligibility=lambda _: None)
        self.assertEqual(receipt['results'][0]['reason'], 'probe_failed')
        value = self.diagnostics()
        self.assertEqual(value['state']['negative'], 1)
        self.assertEqual(value['state']['positive'], 0)
        self.assertIsNone(value['last_commit'])
        self.assertIsNone(value['first_invalidation'])
        self.case.mountinfo.write_text('')
        self.sampler.capacity_snapshot()
        self.assertEqual(self.diagnostics()['state']['negative'], 0)
        self.assertIsNone(self.diagnostics()['first_invalidation'])

    def test_memory_diagnostic_does_not_poll_or_reap_a_lingering_worker(self):
        worker = types.SimpleNamespace(poll=mock.Mock(side_effect=AssertionError('poll child')))
        self.sampler._capacity_worker = worker
        try:
            value = self.diagnostics()
        finally:
            self.sampler._capacity_worker = None
        self.assertTrue(value['state']['worker_present'])
        worker.poll.assert_not_called()

    def test_instances_and_events_remain_distinct_across_sampler_restart(self):
        self.refresh()
        original = self.diagnostics()
        new_sampler = service.StatusSampler(self.case.proc, self.case.sys)
        self.addCleanup(new_sampler.close_capacity)
        current = new_sampler.capacity_cache_diagnostics()
        self.assertNotEqual(current['instance'], original['instance'])
        self.assertEqual(current['seq'], 0)
        self.assertEqual(current['generation'], 0)
        self.assertIsNone(current['last_commit'])
        self.assertIsNone(current['first_invalidation'])

    def test_missing_heartbeat_does_not_masquerade_as_capacity_invalidation(self):
        self.refresh()
        monitor = service.StatusMonitor(self.sampler)
        rows = monitor.snapshot()['disks']
        value = monitor.capacity_cache_diagnostics()
        self.assertEqual(value['monitor']['last_projection']['result'], 'heartbeat_missing')
        self.assertEqual(rows[0]['usage_percent'], 75.0)
        self.assertIsNone(value['first_invalidation'])
        self.assertEqual(value['state']['positive'], 1)

    def test_sampler_error_is_safely_named_and_does_not_claim_cache_loss(self):
        self.refresh()
        monitor = service.StatusMonitor(self.sampler)
        with mock.patch.object(self.sampler, 'sample', side_effect=RuntimeError('fixed test failure')), \
                mock.patch.object(monitor.stopped, 'wait', side_effect=lambda _: monitor.stopped.set()), \
                contextlib.redirect_stdout(__import__('io').StringIO()):
            monitor._run()
        value = monitor.capacity_cache_diagnostics()
        self.assertEqual(value['monitor']['last_sample']['result'], 'sample_error')
        self.assertIsNone(value['first_invalidation'])
        self.assertEqual(value['state']['positive'], 1)

    def test_lossless_stat_projection_is_portable_above_javascript_integer_limit(self):
        stat = types.SimpleNamespace(st_dev=2**60+1, st_ino=2**61+3, st_ctime_ns=2**62+7)
        value = service._capacity_stat_evidence(stat)
        self.assertEqual(value, {'dev': '1152921504606846977', 'ino': '2305843009213693955',
                                 'ctime_ns': '4611686018427387911'})
        self.assertEqual(json.loads(json.dumps(value)), value)


    def test_busy_second_batch_cannot_clear_the_lock_owners_pending_records(self):
        self.sampler._capacity_refresh_lock.acquire()
        self.sampler._capacity_pending = 1
        try:
            with mock.patch.object(self.sampler, '_capacity_probe', side_effect=AssertionError('busy worker')):
                with self.assertRaises(service.CapacityRefreshError) as caught:
                    self.sampler.refresh_capacity(authorizer=lambda: self.fail('busy request authorizer'))
            self.assertEqual(caught.exception.reason, 'busy')
            self.assertEqual(self.diagnostics()['state']['pending'], 1)
            self.assertEqual(self.event('batch_busy')['counts']['pending'], 1)
            self.assertEqual(self.event('batch_finished')['counts']['pending'], 1)
            self.assertTrue(self.sampler._capacity_refresh_lock.locked())
        finally:
            self.sampler._capacity_pending = 0
            self.sampler._capacity_refresh_lock.release()

    def test_interleaved_inventory_retains_the_evidence_actually_reconciled(self):
        self.refresh()
        baseline = self.case.mountinfo.read_text()
        original_key = self.diagnostics()['last_commit']['records'][0]['key_digest']
        self.case.mountinfo.write_text(baseline.replace(' rw - ', ' rw,noatime - '))
        used_entries, complete = self.sampler._capacity_inventory_observe()
        self.assertTrue(complete)
        used_seq = used_entries.observation['seq']
        used_key = used_entries[0]['_key']
        self.assertNotEqual(used_key, original_key)
        # This is the deterministic order of two legal concurrent observers:
        # reader A captures the changed row; reader B projects the old row;
        # then reader A resumes reconciliation with its captured inventory.
        self.case.mountinfo.write_text(baseline)
        self.assertEqual(self.sampler.capacity_snapshot()[0]['usage_percent'], 75.0)
        interloper = self.diagnostics()['latest_inventory']
        self.assertGreater(interloper['seq'], used_seq)
        self.assertEqual(interloper['entries'][0]['key_digest'], original_key)
        self.sampler._capacity_reconcile(used_entries, complete)
        value = self.diagnostics()
        first = self.first('active_keys_changed')
        self.assertEqual(value['latest_inventory']['seq'], interloper['seq'])
        self.assertEqual(first['inventory_seq'], used_seq)
        self.assertEqual(first['after']['inventory']['seq'], used_seq)
        self.assertEqual(first['after']['inventory']['entries'][0]['key_digest'], used_key)
        self.assertTrue(first['component_changes']['mounts'][0]['changed']['mount_options'])
        self.assertTrue(first['component_changes']['mounts'][0]['changed']['key_digest'])
        self.assertEqual(first['before']['inventory']['entries'][0]['key_digest'], original_key)
        self.assertFalse(self.sampler._capacity_cache)

    def test_full_sysfs_target_and_parent_key_material_are_hashed_and_attributed(self):
        identity = ('PRIVATE_SYSFS_TARGET_CANARY', 'PRIVATE_SYSFS_PARENT_CANARY',
                    (1, 2, 3), (1, 4, 5), (1, 6, 7), '8:16', '12', 'sdb')
        replacement = ('OTHER_PRIVATE_SYSFS_TARGET_CANARY', 'OTHER_PRIVATE_SYSFS_PARENT_CANARY') + identity[2:]
        with mock.patch.object(service, '_capacity_device', return_value=identity) as device:
            self.refresh()
            previous = self.diagnostics()['last_commit']['inventory']['entries'][0]
            device.return_value = replacement
            self.sampler.capacity_snapshot()
        first = self.first('active_keys_changed')
        current = first['after']['inventory']['entries'][0]
        changed = first['component_changes']['mounts'][0]['changed']
        for field in ('sysfs_target', 'sysfs_parent', 'device_material', 'key_digest'):
            self.assertIs(changed[field], True)
        self.assertEqual(previous['components']['mountline'], current['components']['mountline'])
        self.assertIs(changed['mountline'], False)
        self.assertNotEqual(previous['components']['sysfs_target'], current['components']['sysfs_target'])
        self.assertNotEqual(previous['components']['sysfs_parent'], current['components']['sysfs_parent'])
        encoded = json.dumps(self.diagnostics())
        for raw in identity[:2] + replacement[:2]:
            self.assertNotIn(raw, encoded)


class CapacityWatchObservationTests(unittest.TestCase):
    """Use the production watcher method with only its kernel operations replaced."""

    def setUp(self):
        self.watch = service._CapacityMountWatch.__new__(service._CapacityMountWatch)
        self.watch.descriptor = 41
        self.watch.namespace = Path('/proc/self/ns/mnt')
        self.watch.identity = 'mnt:[12345]'
        self.watch.poller = mock.Mock()
        self.watch.poller.poll.return_value = []

    def test_unchanged_watch_reads_namespace_once_and_polls_once(self):
        with mock.patch.object(service.os, 'readlink', return_value='mnt:[12345]') as read:
            self.assertFalse(self.watch.changed())
        read.assert_called_once_with(self.watch.namespace)
        self.watch.poller.poll.assert_called_once_with(0)
        self.assertEqual(self.watch.observation['reason'], 'unchanged')
        self.assertTrue(self.watch.observation['namespace_read'])
        self.assertTrue(self.watch.observation['polled'])

    def test_namespace_change_short_circuits_poll_and_preserves_original_identity(self):
        with mock.patch.object(service.os, 'readlink', return_value='mnt:[12346]') as read:
            self.assertTrue(self.watch.changed())
        read.assert_called_once_with(self.watch.namespace)
        self.watch.poller.poll.assert_not_called()
        self.assertEqual(self.watch.identity, 'mnt:[12345]')
        self.assertEqual(self.watch.observation['reason'], 'namespace_changed')
        self.assertFalse(self.watch.observation['polled'])

    def test_lost_descriptor_neither_reads_namespace_nor_consumes_poll(self):
        self.watch.descriptor = None
        with mock.patch.object(service.os, 'readlink', side_effect=AssertionError('extra read')):
            self.assertTrue(self.watch.changed())
        self.watch.poller.poll.assert_not_called()
        self.assertEqual(self.watch.observation['reason'], 'descriptor_lost')
        self.assertFalse(self.watch.observation['namespace_read'])
        self.assertFalse(self.watch.observation['polled'])

    def test_event_mask_is_from_the_single_real_poll_result(self):
        self.watch.poller.poll.return_value = [(41, 10)]
        with mock.patch.object(service.os, 'readlink', return_value='mnt:[12345]'):
            self.assertTrue(self.watch.changed())
        self.watch.poller.poll.assert_called_once_with(0)
        self.assertEqual(self.watch.observation['reason'], 'mount_event')
        self.assertEqual(self.watch.observation['mask'], 10)

    def test_invalid_namespace_link_never_exposes_its_raw_target(self):
        with mock.patch.object(service.os, 'readlink', return_value='PRIVATE_NAMESPACE_PATH_CANARY'):
            self.assertTrue(self.watch.changed())
        self.watch.poller.poll.assert_not_called()
        self.assertNotIn('PRIVATE_NAMESPACE_PATH_CANARY', json.dumps(self.watch.observation))
        self.assertEqual(self.watch.observation['reason'], 'namespace_changed')

    def test_watch_failure_retains_safe_reason_without_exception_text(self):
        with mock.patch.object(service.os, 'readlink', side_effect=OSError('WATCH_SECRET_CANARY')):
            self.assertTrue(self.watch.changed())
        self.assertEqual(self.watch.observation['reason'], 'watch_error')
        self.assertNotIn('WATCH_SECRET_CANARY', json.dumps(self.watch.observation))
        self.watch.poller.poll.assert_not_called()


@unittest.skipUnless(sys.platform.startswith('linux'), 'real sysfs-style major:minor symlinks require Linux')
class CapacityLedgerRealSysfsTests(unittest.TestCase):
    """Retain production sysfs identity parsing rather than a synthetic key."""

    def setUp(self):
        self.case = core.CapacitySysfsTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.proc = self.case.root / 'proc'
        (self.proc / 'self').mkdir(parents=True)
        (self.proc / 'self/mountinfo').write_text(
            '11 1 8:17 / /drives/PRIVATE_DISK_NAME rw shared:42 - ext4 /dev/PRIVATE_DEVICE_SOURCE rw\n')
        self.sampler = service.StatusSampler(self.proc, self.case.root)
        self.addCleanup(self.sampler.close_capacity)
        self.watch = types.SimpleNamespace(changed=mock.Mock(return_value=False), close=mock.Mock())
        patch = mock.patch.object(service, '_CapacityMountWatch', return_value=self.watch)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(service, '_capacity_fd_mount', return_value=11)
        patch.start()
        self.addCleanup(patch.stop)

    def refresh(self):
        def probe(*_):
            return {'total_bytes': 409600, 'used_bytes': 307200, 'usage_percent': 75.0}, os.open(os.devnull, os.O_RDONLY)
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            return self.sampler.refresh_capacity(authorizer=lambda: True)

    def test_real_kernel_identity_integers_are_decimal_strings_without_paths(self):
        diskseq = '18446744073709551615'
        (self.case.physical / 'diskseq').write_text(diskseq + '\n')
        self.refresh()
        diagnostic = self.sampler.capacity_cache_diagnostics()
        device = diagnostic['latest_inventory']['entries'][0]['device_identity']
        self.assertTrue(device['complete'])
        self.assertEqual(device['diskseq'], diskseq)
        for field, path in (('link_before', self.case.links / '8:17'),
                            ('target_before', self.case.partition), ('parent_before', self.case.physical),
                            ('link_after', self.case.links / '8:17'),
                            ('target_after', self.case.partition), ('parent_after', self.case.physical)):
            original = path.lstat() if field.startswith('link') else path.stat()
            self.assertEqual(device[field], {'dev': str(original.st_dev), 'ino': str(original.st_ino),
                                             'ctime_ns': str(original.st_ctime_ns)})
            self.assertTrue(all(isinstance(value, str) and value.isdecimal() for value in device[field].values()))
        public = json.dumps(diagnostic)
        for value in ('PRIVATE_DISK_NAME', 'PRIVATE_DEVICE_SOURCE', str(self.case.root), '/drives/'):
            self.assertNotIn(value, public)

    def test_real_parent_ctime_change_has_explicit_component_provenance(self):
        self.refresh()
        old = self.sampler.capacity_cache_diagnostics()['last_commit']['inventory']['entries'][0]
        # Creating metadata within the fixture sysfs directory changes its real
        # ctime/inode generation. It is not a user mount or hardware operation.
        (self.case.physical / 'new-kernel-metadata').write_text('x')
        self.sampler.capacity_snapshot()
        value = self.sampler.capacity_cache_diagnostics()
        failure = value['first_invalidation']
        self.assertIsNotNone(failure)
        self.assertEqual(failure['reason'], 'active_keys_changed')
        new = failure['after']['inventory']['entries'][0]
        self.assertEqual(old['components']['mountline'], new['components']['mountline'])
        self.assertNotEqual(old['device_identity']['parent_before']['ctime_ns'],
                            new['device_identity']['parent_before']['ctime_ns'])
        self.assertNotEqual(old['key_digest'], new['key_digest'])
        self.assertIsNone(self.sampler.capacity_snapshot()[0]['total_bytes'])


if __name__ == '__main__':
    unittest.main()

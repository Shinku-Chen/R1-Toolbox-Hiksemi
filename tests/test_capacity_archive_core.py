"""Detached past observations never grant current capacity or disk association."""
import copy
import json
import os
import time
import unittest
from unittest import mock

import test_capacity_history_core as history
import test_smart_power_compat as power

service = history.service


class CapacityArchiveCoreTests(unittest.TestCase):
    def setUp(self):
        self.core = history.CapacityHistoryCoreTests()
        self.core.setUp()
        self.addCleanup(self.core.doCleanups)
        self.sampler = self.core.sampler

    def archive(self):
        return getattr(self.sampler.capacity_snapshot(), 'capacity_observation_archive', None)

    def test_archive_is_staged_until_original_receipt_finalizes(self):
        receipt = self.core.refresh(publish=False)
        self.assertIsNone(self.archive())
        self.assertFalse(self.sampler.publish_capacity_history(copy.deepcopy(receipt), authorizer=lambda: True))
        self.assertIsNone(self.archive())
        self.assertTrue(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))
        value = self.archive()
        self.assertIsNotNone(value)
        self.assertEqual(value['observation_id'], str(receipt.batch))
        self.assertEqual((value['total'], value['used'], value['available']), (44000, 11000, 33000))
        self.assertEqual(value['coverage'], {'scope': 'measured_filesystem_subset',
            'filesystem_count': 3, 'disk_count': 2, 'complete': False})

    def test_incomplete_inventory_destroys_bound_history_but_not_archive(self):
        self.core.refresh()
        before = self.archive()
        self.assertIsNotNone(before)
        self.core.event()
        (self.core.proc / 'self/mountinfo').write_text('malformed\n', encoding='utf-8')
        rows = self.sampler.capacity_snapshot()
        self.assertEqual(getattr(rows, 'capacity_observations'), {'disks': {}, 'summary': None})
        self.assertEqual(getattr(rows, 'capacity_observation_archive', None), before)
        self.core.write_mounts()
        rows = self.sampler.capacity_snapshot()
        self.assertTrue(all(row['total_bytes'] is None for row in rows))
        self.assertEqual(rows.capacity_observations, {'disks': {}, 'summary': None})
        self.assertEqual(rows.capacity_observation_archive, before)

    def test_archive_is_immutable_numeric_only_and_private_in_normal_json(self):
        self.core.refresh()
        rows = self.sampler.capacity_snapshot()
        archive = getattr(rows, 'capacity_observation_archive', None)
        self.assertIsNotNone(archive)
        self.assertEqual(set(archive), {'schema_version', 'status', 'source', 'reason',
            'observation_id', 'total', 'used', 'available', 'percent', 'sampled_at',
            'sampled_at_latest', 'coverage', 'retention'})
        self.assertEqual((archive['schema_version'], archive['status'], archive['source'],
            archive['reason'], archive['retention']), (1, 'historical',
            'past_successful_observation', 'current_binding_unverified', 'memory_until_restart'))
        with self.assertRaises(TypeError):
            archive['total'] = 0
        with self.assertRaises(TypeError):
            archive['coverage']['complete'] = True
        self.assertIs(copy.deepcopy(archive), archive)
        self.assertNotIn('past_successful_observation', json.dumps(rows))
        self.assertNotIn('physical-', json.dumps(archive))
        self.assertEqual(copy.deepcopy(rows).capacity_observation_archive, archive)

    def test_successful_partial_batch_replaces_archive_without_old_partition_mix(self):
        self.core.refresh()
        first = self.archive()
        measure = self.core.measure
        def partial(entry, *args):
            if entry['mount_id'] == 12:
                raise service.CapacityRefreshError('probe_failed')
            return measure(entry, *args)
        self.core.probe.side_effect = partial
        with mock.patch.object(service.time, 'time', return_value=first['sampled_at_latest'] + 60):
            receipt = self.core.refresh()
        value = self.archive()
        self.assertEqual(value['observation_id'], str(receipt.batch))
        self.assertEqual((value['total'], value['used']), (32000, 8000))
        self.assertEqual(value['coverage']['filesystem_count'], 2)
        self.assertGreater(value['sampled_at'], first['sampled_at_latest'])

    def test_standby_old_sibling_is_not_mixed_into_new_archive(self):
        self.core.refresh()
        before = self.archive()
        receipt = self.core.refresh(visible=True,
            eligibility=lambda name: 'standby' if name == 'sda' else None)
        value = self.archive()
        self.assertEqual(value['observation_id'], str(receipt.batch))
        self.assertNotEqual(value['observation_id'], before['observation_id'])
        self.assertEqual((value['total'], value['coverage']['filesystem_count'],
            value['coverage']['disk_count']), (21000, 1, 1))
        self.assertEqual(self.core.view()[1]['disks']['sda']['observation_id'], before['observation_id'])

    def test_cached_only_failed_and_empty_batches_do_not_update_or_renew_archive(self):
        self.core.refresh()
        before = self.archive()
        result = self.sampler.refresh_capacity_missing(authorizer=lambda: True, eligibility=lambda _: None)
        self.assertTrue(all(row['status'] == 'cached' for row in result['results']))
        self.assertFalse(self.sampler.publish_capacity_history(result, authorizer=lambda: True))
        self.assertEqual(self.archive(), before)
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=service.CapacityRefreshError('probe_failed')):
            result = self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertFalse(self.sampler.publish_capacity_history(result, authorizer=lambda: True))
        self.assertEqual(self.archive(), before)
        self.core.mounts = []
        self.core.write_mounts()
        result = self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertFalse(self.sampler.publish_capacity_history(result, authorizer=lambda: True))
        self.assertEqual(self.archive(), before)

    def test_late_identical_candidate_cannot_overwrite_new_archive_after_bound_history_prune(self):
        with mock.patch.object(service.time, 'time', return_value=1700000000.0):
            old = self.core.refresh(publish=False)
            new = self.core.refresh()
        before = self.archive()
        with self.sampler._capacity_lock:
            self.sampler._capacity_history_prune([], False)
        self.assertFalse(self.sampler._capacity_history)
        # Preserve the original per-disk publication semantics; only the
        # independent archive's global ordering rejects the older batch.
        self.assertTrue(self.sampler.publish_capacity_history(old, authorizer=lambda: True))
        self.assertEqual(self.archive(), before)
        self.assertEqual(before['observation_id'], str(new.batch))

    def test_late_disjoint_disk_history_publishes_without_overwriting_newer_archive(self):
        old = self.core.refresh(publish=False, visible=True,
            eligibility=lambda name: None if name == 'sda' else 'standby')
        new = self.core.refresh(visible=True,
            eligibility=lambda name: None if name == 'sdb' else 'standby')
        before = self.archive()
        self.assertTrue(self.sampler.publish_capacity_history(old, authorizer=lambda: True))
        self.assertEqual(self.archive(), before)
        self.assertEqual(before['observation_id'], str(new.batch))
        self.assertEqual(self.core.view()[1]['disks']['sda']['observation_id'], str(old.batch))

    def test_final_authorizer_rejection_restores_previous_archive_and_history(self):
        self.core.refresh()
        before, history_before = self.archive(), copy.deepcopy(self.sampler._capacity_history)
        candidate = self.core.refresh(publish=False)
        calls = 0
        def current():
            nonlocal calls
            calls += 1
            return calls != 3
        self.assertFalse(self.sampler.publish_capacity_history(candidate, authorizer=current))
        self.assertEqual(calls, 3, 'archive must not add authorizer calls')
        self.assertEqual(self.archive(), before)
        self.assertEqual(self.sampler._capacity_history, history_before)

    def test_stop_start_during_last_guard_cannot_restore_old_archive(self):
        self.core.refresh()
        candidate = self.core.refresh(publish=False)
        calls = 0
        def current():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.sampler.close_capacity()
                self.sampler.start_capacity()
            return True
        self.assertFalse(self.sampler.publish_capacity_history(candidate, authorizer=current))
        self.assertIsNone(self.archive())
        self.assertFalse(self.sampler._capacity_history)

    def test_final_guard_clock_exception_rolls_back_under_publication_lock(self):
        self.core.refresh()
        before, bound = self.archive(), copy.deepcopy(self.sampler._capacity_history)
        candidate = self.core.refresh(publish=False)
        real_clock, raised = time.monotonic, False
        def clock():
            nonlocal raised
            if not raised and self.sampler._capacity_observation_archive is not before:
                raised = True
                raise ValueError('clock')
            return real_clock()
        with mock.patch.object(service.time, 'monotonic', side_effect=clock):
            self.assertFalse(self.sampler.publish_capacity_history(candidate, authorizer=lambda: True))
        self.assertTrue(raised)
        self.assertEqual(self.archive(), before)
        self.assertEqual(self.sampler._capacity_history, bound)

    def test_start_close_and_expired_or_revoked_candidate_do_not_publish(self):
        self.core.refresh()
        pending = self.core.refresh(publish=False)
        pending.deadline = 0
        before = self.archive()
        self.assertFalse(self.sampler.publish_capacity_history(pending, authorizer=lambda: True))
        self.assertEqual(self.archive(), before)
        pending = self.core.refresh(publish=False)
        self.assertFalse(self.sampler.publish_capacity_history(pending, authorizer=lambda: False))
        self.assertEqual(self.archive(), before)
        self.sampler.close_capacity()
        self.assertIsNone(self.archive())
        self.sampler.start_capacity()
        self.assertIsNone(self.archive())
        self.assertFalse(self.sampler.publish_capacity_history(pending, authorizer=lambda: True))

    def test_identity_changes_cannot_reattach_archive_to_current_disks(self):
        self.core.refresh()
        archive = self.archive()
        self.core.generations['8:1'] += 1
        self.core.generations['8:2'] += 1
        rows, bound = self.core.view()
        self.assertNotIn('sda', bound['disks'])
        self.assertTrue(all(row['total_bytes'] is None for row in rows))
        self.assertEqual(rows.capacity_observation_archive, archive)
        self.core.generations['8:17'] = None
        self.assertEqual(self.archive(), archive)
        self.assertEqual(self.core.view()[1], {'disks': {}, 'summary': None})

    def test_observer_fault_and_repeated_gets_do_not_collect_or_renew_archive(self):
        with mock.patch.object(self.sampler, '_capacity_record', side_effect=ValueError('observer')):
            self.core.refresh()
        before = self.archive()
        self.assertIsNotNone(before)
        calls = list(self.core.calls)
        with mock.patch.object(service.time, 'time', return_value=before['sampled_at_latest'] + 3600), \
             mock.patch.object(self.sampler, '_capacity_probe', side_effect=AssertionError('worker')), \
             mock.patch.object(service.os, 'statvfs', create=True, side_effect=AssertionError('statvfs')):
            for _ in range(12):
                self.assertEqual(self.archive(), before)
        self.assertEqual(self.core.calls, calls)
        self.core.assert_handles_closed()

    def test_archive_construction_failure_leaves_previous_publication_unchanged(self):
        self.core.refresh()
        before, bound = self.archive(), copy.deepcopy(self.sampler._capacity_history)
        pending = self.core.refresh(publish=False)
        with mock.patch.object(self.sampler, '_capacity_archive_view', side_effect=ValueError('archive')):
            self.assertFalse(self.sampler.publish_capacity_history(pending, authorizer=lambda: True))
        self.assertEqual(self.archive(), before)
        self.assertEqual(self.sampler._capacity_history, bound)
        self.assertTrue(self.sampler._capacity_cache)

    def test_bound_history_projection_fault_does_not_hide_independent_archive(self):
        self.core.refresh()
        before = self.archive()
        with mock.patch.object(self.sampler, '_capacity_history_projection', side_effect=ValueError('history')):
            rows = self.sampler.capacity_snapshot()
        self.assertTrue(all(row['total_bytes'] is not None for row in rows))
        self.assertEqual(rows.capacity_observations, {'disks': {}, 'summary': None})
        self.assertEqual(rows.capacity_observation_archive, before)

    def test_duplicate_mount_and_parent_conflict_never_reattach_archive(self):
        self.core.refresh()
        before = self.archive()
        self.core.mounts.append((99, '8:17', '/drives/alias'))
        self.core.write_mounts()
        rows, bound = self.core.view()
        self.assertNotIn('sdb', bound['disks'])
        self.assertTrue(all(row['total_bytes'] is None for row in rows))
        self.assertEqual(rows.capacity_observation_archive, before)
        self.core.mounts.pop()
        self.core.write_mounts()
        original = self.core.device
        def conflicting(sys_root, dev):
            value = original(sys_root, dev)
            return (*value[:4], (1, 30, 2), value[5], '2', value[7]) if dev == '8:2' else value
        with mock.patch.object(service, '_capacity_device', side_effect=conflicting):
            rows, bound = self.core.view()
        self.assertNotIn('sda', bound['disks'])
        self.assertTrue(all(row['total_bytes'] is None for row in rows))
        self.assertEqual(rows.capacity_observation_archive, before)

    def test_real_ordinary_eight_measurements_survive_watch_incomplete_and_restoration_only_in_archive(self):
        case = power.SmartPowerCompatTests('test_name_states_reach_per_batch_capacity_and_panel_with_a_standby')
        case.setUp()
        self.addCleanup(case.doCleanups)
        parts = {100 + i * 10 + j: (name, f'8:{i * 16 + j}', j)
                 for i, name in enumerate(('sda', 'sdb', 'sdc', 'sdd')) for j in (1, 2)}
        text = ''.join(f'{mid} 1 {dev} / /drives/fixture-{name}-{j} rw - ext4 /dev/{name}{j} rw\n'
                       for mid, (name, dev, j) in parts.items())
        case.write(case.proc / 'self/mountinfo', text)
        def identity(_sys, dev):
            item = next((item for item in parts.values() if item[1] == dev), None)
            if item is None:
                return None
            name, _, j = item
            lifetime = case.collector._smart_context(name)
            return ('partition-' + dev, 'physical-' + name, lifetime,
                    lifetime, lifetime, '8:' + str(('sda', 'sdb', 'sdc', 'sdd').index(name) * 16), '1', name)
        with mock.patch.object(service, '_capacity_device', side_effect=identity):
            case.responses = {name: case.response({'name': 'ACTIVE'}) for name in case.mounts}
            smart, receipt, _, _ = case.refresh_visible()
            self.assertEqual(sum(row['status'] == 'refreshed' for row in receipt['results']), 8)
            self.assertTrue(case.monitor.publish_capacity_history(receipt, authorizer=lambda: case.active))
            before = case.panel.overview(include_identity=True)
            archive = before['capacity_observation_archive']
            self.assertEqual(archive['coverage']['filesystem_count'], 8)
            case.watch.changed.return_value = True
            after_event = case.panel.overview(include_identity=True)
            case.watch.changed.return_value = False
            self.assertIsNone(after_event['disk_size'])
            self.assertEqual(after_event['capacity_observation_archive'], archive)
            self.assertIsNotNone(after_event['capacity_last_observation_summary'])
            case.write(case.proc / 'self/mountinfo', 'malformed\n')
            after_bad = case.panel.overview(include_identity=True)
            self.assertIsNone(after_bad['capacity_last_observation_summary'])
            self.assertEqual(after_bad['capacity_observation_archive'], archive)
            case.write(case.proc / 'self/mountinfo', text)
            after_restore = case.panel.overview(include_identity=True)
            self.assertIsNone(after_restore['capacity_last_observation_summary'])
            self.assertTrue(all(row['total'] is None and row['capacity_last_observation'] is None
                                for row in after_restore['disks_detail']))
            self.assertEqual(after_restore['capacity_observation_archive'], archive)
            self.assertEqual(len(case.calls), 4)
            self.assertEqual(len(case.capacity_calls), 8)
            case.assert_closed_handles()


if __name__ == '__main__':
    unittest.main()

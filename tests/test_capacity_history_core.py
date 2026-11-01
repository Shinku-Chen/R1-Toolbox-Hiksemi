"""Real capacity decisions and publication; portable OS seams, no hardware IO."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import test_capacity_refresh_core as base
import test_smart_power_compat as power

service = base.service


class CapacityHistoryCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-history-core-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc, self.sys = self.root / 'proc', self.root / 'sys'
        (self.proc / 'self').mkdir(parents=True)
        self.sys.mkdir()
        self.generations = {'8:1': 1, '8:2': 1, '8:17': 1}
        self.devices = {'8:1': 'sda', '8:2': 'sda', '8:17': 'sdb'}
        self.mounts = [(11, '8:1', '/drives/a'), (12, '8:2', '/drives/a2'),
                       (21, '8:17', '/drives/b')]
        self.write_mounts()
        self.watch = SimpleNamespace(changed=mock.Mock(return_value=False), close=mock.Mock())
        self.install(service, '_CapacityMountWatch', return_value=self.watch)
        self.install(service, '_capacity_device', side_effect=self.device)
        self.sampler = service.StatusSampler(self.proc, self.sys)
        self.addCleanup(self.sampler.close_capacity)
        self.handles, self.calls = [], []
        self.probe = self.install(self.sampler, '_capacity_probe', side_effect=self.measure)

    def install(self, target, name, **kwargs):
        patch = mock.patch.object(target, name, **kwargs)
        value = patch.start()
        self.addCleanup(patch.stop)
        return value

    def device(self, _sys, dev):
        name, gen = self.devices.get(dev), self.generations.get(dev)
        if name is None or gen is None:
            return None
        # The exact private production tuple is supplied at the OS identity seam;
        # it is not synthesized from public names or diagnostic evidence.
        return ('/sys/partition-' + dev, '/sys/physical-' + name,
                (1, 10, gen), (1, 20, gen), (1, 30, 1),
                '8:0' if name == 'sda' else '8:16', '1', name)

    def write_mounts(self):
        (self.proc / 'self/mountinfo').write_text(''.join(
            f'{mid} 1 {dev} / {mount} rw - ext4 /dev/{self.devices[dev]}{2 if dev == "8:2" else 1} rw\n'
            for mid, dev, mount in self.mounts), encoding='utf-8')

    def measure(self, entry, deadline, guard):
        guard()
        self.calls.append(entry['mount_id'])
        fd = os.open(os.devnull, os.O_RDONLY)
        self.handles.append(fd)
        total = entry['mount_id'] * 1000
        return {'total_bytes': total, 'used_bytes': total // 4, 'usage_percent': 25.0}, fd

    def refresh(self, *, publish=True, visible=False, eligibility=None, authorizer=lambda: True):
        if visible:
            result = self.sampler.refresh_capacity_visible(authorizer=authorizer,
                                                           eligibility=eligibility or (lambda _: None))
        else:
            result = self.sampler.refresh_capacity(authorizer=authorizer)
        if publish:
            self.assertTrue(self.sampler.publish_capacity_history(result, authorizer=authorizer))
        return result

    def view(self):
        rows = self.sampler.capacity_snapshot()
        return rows, getattr(rows, 'capacity_observations', {'disks': {}, 'summary': None})

    def event(self):
        self.watch.changed.return_value = True
        rows, history = self.view()
        self.watch.changed.return_value = False
        return rows, history

    def assert_handles_closed(self):
        for fd in self.handles:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_core_commit_stages_but_does_not_publish_history(self):
        receipt = self.refresh(publish=False)
        rows, history = self.view()
        self.assertTrue(all(row['total_bytes'] for row in rows))
        self.assertEqual(history, {'disks': {}, 'summary': None})
        self.assertTrue(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))
        self.assertIsNotNone(self.view()[1]['summary'])
        self.assertFalse(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))

    def test_mount_event_keeps_dated_history_but_clears_current_and_negative(self):
        self.refresh()
        _, before = self.view()
        self.sampler._capacity_negative['failure'] = (time.monotonic() + 30, 'failed', 'probe_failed', None)
        rows, after = self.event()
        self.assertTrue(all(row['total_bytes'] is None for row in rows))
        self.assertFalse(self.sampler._capacity_cache)
        self.assertFalse(self.sampler._capacity_negative)
        for name in ('sda', 'sdb'):
            for field in ('total', 'used', 'percent', 'sampled_at', 'sampled_at_latest', 'observation_id'):
                self.assertEqual(before['disks'][name][field], after['disks'][name][field])
            self.assertEqual(after['disks'][name]['reason'], 'mount_continuity_unverified')
        self.assertFalse(after['summary']['coverage']['complete'])
        self.assert_handles_closed()

    def test_repeated_passive_projection_never_probes_and_dates_do_not_move(self):
        self.refresh()
        expected = self.event()[1]
        self.calls.clear()
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=AssertionError('worker')), \
                mock.patch.object(service.os, 'statvfs', create=True, side_effect=AssertionError('statvfs')):
            for _ in range(40):
                self.assertEqual(self.view()[1], expected)
        self.assertEqual(self.calls, [])

    def test_private_sideband_and_candidate_never_enter_public_json(self):
        receipt = self.refresh(publish=False)
        self.assertNotIn('records', json.dumps(receipt))
        clone = copy.deepcopy(receipt)
        self.assertIs(type(clone), dict)
        self.assertFalse(self.sampler.publish_capacity_history(clone, authorizer=lambda: True))
        self.assertTrue(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))
        rows, history = self.view()
        serialized = json.dumps(rows)
        self.assertNotIn('last_successful_observation', serialized)
        self.assertNotIn('physical-', serialized)
        self.assertEqual(copy.deepcopy(rows).capacity_observations, history)
        monitor = service.StatusMonitor(self.sampler)
        self.assertNotIn('last_successful_observation', json.dumps(monitor.snapshot()))

    def test_original_projection_diagnostic_still_binds_exact_returned_rows(self):
        self.refresh()
        rows = self.sampler.capacity_snapshot()
        source = self.sampler.capacity_projection_observation(rows)
        self.assertEqual(source['status'], 'observed')
        self.assertTrue(source['atomic_with_capacity'])
        monitor = service.StatusMonitor(self.sampler)
        value = monitor.snapshot()
        projected = monitor.capacity_projection_observation(value)
        self.assertEqual(projected['status'], 'observed')
        self.assertNotEqual(projected['sampler_projection_seq'], 'not_observed')
        self.assertEqual(projected['capacity_epoch'], self.sampler._capacity_epoch)

    def test_sideband_is_immutable_bounded_values_and_deepcopy_safe(self):
        self.refresh()
        rows, history = self.view()
        with self.assertRaises(TypeError):
            history['summary'] = 'private-observation-canary'
        with self.assertRaises(TypeError):
            history['disks']['sda']['total'] = 0
        self.assertIs(copy.deepcopy(history), history)
        self.assertEqual(copy.deepcopy(rows), rows)

    def test_same_read_identity_does_not_change_worker_json_or_4096_budget(self):
        self.mounts = [(11, '8:1', '/drives/' + 'x' * 3700)]
        self.write_mounts()
        entries, complete = service._capacity_inventory(self.proc, self.sys)
        self.assertTrue(complete)
        entry = entries[0]
        self.assertIsNotNone(entry.capacity_identity)
        shape = lambda value: json.dumps({'entry': value, 'deadline': 123456.5, 'channel': 7},
                                        ensure_ascii=True).encode()
        self.assertEqual(shape(entry), shape(dict(entry)))
        self.assertLess(len(shape(entry)), 4096)
        self.assertGreater(len(shape(dict(entry, _capacity_identity=entry.capacity_identity))), 4096)
        self.assertNotIn('/sys/physical', shape(entry).decode())

    def test_authorizer_exception_after_history_assignment_rolls_back_history_only(self):
        self.refresh()
        expected = self.view()[1]
        receipt = self.refresh(publish=False)
        calls = 0
        def authorizer():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise ValueError('fixed authorization seam')
            return True
        self.assertFalse(self.sampler.publish_capacity_history(receipt, authorizer=authorizer))
        self.assertEqual(self.view()[1], expected)
        self.assertTrue(self.sampler._capacity_cache)

    def test_stop_start_inside_final_publication_check_never_restores_old_history(self):
        self.refresh()
        receipt = self.refresh(publish=False)
        calls = 0
        def authorizer():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.sampler.close_capacity()
                self.sampler.start_capacity()
            return True
        self.assertFalse(self.sampler.publish_capacity_history(receipt, authorizer=authorizer))
        self.assertEqual(self.view()[1]['disks'], {})

    def test_device_partition_generation_change_rejects_old_history(self):
        self.refresh()
        self.generations['8:1'] += 1
        _, history = self.view()
        self.assertEqual(history['disks']['sda']['coverage']['filesystem_count'], 1)
        self.assertEqual(history['disks']['sda']['total'], 12000)
        self.generations['8:2'] += 1
        self.assertNotIn('sda', self.view()[1]['disks'])
        self.assertIn('sdb', self.view()[1]['disks'])

    def test_cross_partition_physical_generation_conflict_rejects_whole_disk_group(self):
        self.refresh()
        original = self.device
        def conflicting(sys_root, dev):
            value = original(sys_root, dev)
            if dev == '8:2':
                value = (*value[:4], (1, 30, 2), value[5], '2', value[7])
            return value
        with mock.patch.object(service, '_capacity_device', side_effect=conflicting):
            self.assertNotIn('sda', self.view()[1]['disks'])
            receipt = self.refresh(publish=False)
            self.assertTrue(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))
            history = self.view()[1]
        self.assertNotIn('sda', history['disks'])
        self.assertEqual(history['summary']['total'], 21000)

    def test_missing_identity_cannot_reappear_as_old_history(self):
        self.refresh()
        self.generations['8:17'] = None
        self.assertNotIn('sdb', self.view()[1]['disks'])
        self.generations['8:17'] = 1
        self.assertNotIn('sdb', self.view()[1]['disks'])

    def test_unmount_missing_inventory_irrevocably_removes_affected_observation(self):
        self.refresh()
        self.mounts = self.mounts[:2]
        self.write_mounts()
        self.assertNotIn('sdb', self.view()[1]['disks'])
        self.mounts.append((21, '8:17', '/drives/b'))
        self.write_mounts()
        self.assertNotIn('sdb', self.view()[1]['disks'])

    def test_incomplete_inventory_erases_history_without_revival(self):
        self.refresh()
        (self.proc / 'self/mountinfo').write_text('malformed\n')
        self.assertEqual(self.view()[1], {'disks': {}, 'summary': None})
        self.write_mounts()
        self.assertEqual(self.view()[1], {'disks': {}, 'summary': None})

    def test_duplicate_partition_mount_never_doubles_history(self):
        self.refresh()
        self.mounts.append((99, '8:17', '/drives/duplicate'))
        self.write_mounts()
        history = self.view()[1]
        self.assertNotIn('sdb', history['disks'])
        self.assertEqual(history['summary']['total'], 23000)
        self.assertEqual(history['summary']['coverage']['filesystem_count'], 2)

    def test_start_close_and_old_publication_never_revive_history(self):
        receipt = self.refresh(publish=False)
        self.sampler.start_capacity()
        self.assertFalse(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))
        self.assertEqual(self.view()[1]['disks'], {})
        self.refresh()
        self.sampler.close_capacity()
        self.assertEqual(self.view()[1]['disks'], {})

    def test_revocation_and_timeout_before_final_publication_create_no_history(self):
        for why in ('authorization', 'deadline'):
            with self.subTest(why=why):
                receipt = self.refresh(publish=False)
                if why == 'deadline':
                    receipt.deadline = 0
                self.assertFalse(self.sampler.publish_capacity_history(
                    receipt, authorizer=lambda: why != 'authorization'))
                self.assertEqual(self.view()[1]['disks'], {})

    def test_late_publication_cannot_overwrite_newer_same_disk(self):
        # Equal wall dates and equal values deliberately prevent cache/date
        # differences from masking the independent business-sequence gate.
        with mock.patch.object(service.time, 'time', return_value=1793522657.0):
            old = self.refresh(publish=False)
            new = self.refresh()
        expected = self.view()[1]
        self.assertFalse(self.sampler.publish_capacity_history(old, authorizer=lambda: True))
        self.assertEqual(self.view()[1], expected)
        self.assertEqual(expected['summary']['observation_id'], str(new.batch))

    def test_new_partial_disk_batch_replaces_whole_old_disk_group(self):
        self.refresh()
        original = self.sampler._capacity_probe.side_effect
        def partial(entry, *args):
            if entry['mount_id'] == 12:
                raise service.CapacityRefreshError('probe_failed')
            return original(entry, *args)
        self.sampler._capacity_probe.side_effect = partial
        self.refresh()
        history = self.view()[1]
        self.assertEqual(history['disks']['sda']['total'], 11000)
        self.assertEqual(history['disks']['sda']['coverage']['filesystem_count'], 1)
        self.assertEqual(history['summary']['total'], 32000)
        self.assertFalse(history['summary']['coverage']['complete'])

    def test_standby_sibling_retains_old_own_date_but_pool_is_one_new_batch(self):
        self.refresh()
        old_a = self.view()[1]['disks']['sda']
        self.refresh(visible=True, eligibility=lambda name: 'standby' if name == 'sda' else None)
        history = self.event()[1]
        self.assertEqual(history['disks']['sda']['sampled_at'], old_a['sampled_at'])
        self.assertEqual(history['disks']['sda']['observation_id'], old_a['observation_id'])
        self.assertEqual(history['summary']['total'], 21000)
        self.assertEqual(history['summary']['coverage']['disk_count'], 1)

    def test_all_standby_batch_does_not_create_new_observation(self):
        self.refresh()
        old = self.view()[1]
        receipt = self.refresh(publish=False, visible=True, eligibility=lambda _: 'standby')
        self.assertFalse(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))
        self.assertEqual(self.view()[1], old)

    def test_observer_failure_does_not_change_business_or_history_binding(self):
        with mock.patch.object(self.sampler, '_capacity_record', side_effect=ValueError('observer')):
            self.refresh()
            rows, history = self.event()
        self.assertTrue(all(row['total_bytes'] is None for row in rows))
        self.assertEqual(history['summary']['total'], 44000)
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())

    def test_history_failure_does_not_change_current_business_or_strand_lock(self):
        with mock.patch.object(self.sampler, '_capacity_history_candidate', side_effect=ValueError('history')):
            receipt = self.refresh(publish=False)
        self.assertIs(type(receipt), dict)
        expected = [row['total_bytes'] for row in self.view()[0]]
        with mock.patch.object(self.sampler, '_capacity_history_projection', side_effect=ValueError('history')):
            self.assertEqual([row['total_bytes'] for row in self.sampler.capacity_snapshot()], expected)
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())

    def test_private_inventory_carrier_fault_does_not_change_real_inventory_or_capacity(self):
        original = service._CapacityInventoryRow
        class AttributeFault(original):
            def __setattr__(self, name, value):
                if name == 'capacity_identity' and value is not None:
                    raise ValueError('private identity observation failed')
                super().__setattr__(name, value)
        for replacement in (mock.Mock(side_effect=ValueError('private row failed')), AttributeFault):
            with self.subTest(replacement=type(replacement).__name__), \
                    mock.patch.object(service, '_CapacityInventoryRow', new=replacement):
                result = self.refresh(publish=False)
                self.assertTrue(result['ok'])
                entries, complete = service._capacity_inventory(self.proc, self.sys)
                self.assertTrue(complete)
                self.assertTrue(all(entry['_key'] for entry in entries))
                rows, history = self.view()
                self.assertTrue(all(row['total_bytes'] is not None for row in rows))
                self.assertEqual(history['disks'], {})
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())

    def test_core_publication_rollback_does_not_create_history(self):
        original = self.sampler._capacity_replace_cache
        active = True
        def replace(records, *args, **kwargs):
            nonlocal active
            original(records, *args, **kwargs)
            if kwargs.get('reason') == 'commit_prepare':
                active = False
        with mock.patch.object(self.sampler, '_capacity_replace_cache', side_effect=replace):
            with self.assertRaises(service.CapacityRefreshError):
                self.refresh(publish=False, authorizer=lambda: active)
        self.assertEqual(self.view()[1]['disks'], {})
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())
        self.assert_handles_closed()

    def test_real_ordinary_smart_capacity_monitor_fnm_chain_preserves_history_after_event(self):
        fixture = power.SmartPowerCompatTests('test_name_states_reach_per_batch_capacity_and_panel_with_a_standby')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        def identity(_sys, dev):
            name = next((name for name, (_, number, _) in fixture.mounts.items() if number == dev), None)
            lifetime = fixture.collector._smart_context(name) if name else None
            return ('partition-' + dev, 'physical-' + str(name), lifetime, lifetime,
                    lifetime, dev, '1', name) if lifetime else None
        with mock.patch.object(service, '_capacity_device', side_effect=identity):
            fixture.ordinary_mix()
            smart, receipt, _, _ = fixture.refresh_visible()
            self.assertTrue(fixture.monitor.publish_capacity_history(receipt, authorizer=lambda: fixture.active))
            before = fixture.panel.overview(include_identity=True)
            fixture.watch.changed.return_value = True
            after = fixture.panel.overview(include_identity=True)
            self.assertEqual(len(fixture.calls), 4)
            self.assertEqual(fixture.capacity_calls, ['sdb', 'sdc', 'sdd'])
            self.assertTrue(all(argv[1:5] == ['-n', 'standby', '--json=o', '-a'] for argv, _ in fixture.calls))
            self.assertIsNone(after['disk_size'])
            for row in after['disks_detail']:
                self.assertIsNone(row['total'])
                if row['name'] != 'sda':
                    old = next(d for d in before['disks_detail'] if d['name'] == row['name'])
                    self.assertEqual(row['capacity_last_observation']['sampled_at'],
                                     old['capacity_last_observation']['sampled_at'])
                    self.assertEqual(row['capacity_last_observation']['percent'], 25.0)
            self.assertEqual(after['capacity_last_observation_summary']['total'], 9000)
            self.assertEqual(next(row for row in smart['results'] if row['device'] == 'sda')['status'],
                             'skipped_standby')
            fixture.assert_closed_handles()


if __name__ == '__main__':
    unittest.main()

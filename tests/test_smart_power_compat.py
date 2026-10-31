"""SMART power JSON compatibility through real capacity/cache/panel projection.

Portable fixtures fake only smartctl output, capacity-worker measurement and
kernel mount proof. Production parsers, per-batch grants, StatusSampler cache,
StatusMonitor projection and FnmonitorPanel remain in the path. These tests do
not claim real Linux FD/SCM_RIGHTS coverage or physical disk sleep.
"""
import copy
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import test_capacity_refresh_core as capacity_fixture
import test_smart_refresh_core as smart_fixture


service = capacity_fixture.service
MISSING = object()


class SmartPowerCompatTests(unittest.TestCase):
    write = smart_fixture.SmartRefreshCoreTests.write
    block = smart_fixture.SmartRefreshCoreTests.block
    good_response = staticmethod(smart_fixture.SmartRefreshCoreTests.good_response)

    def setUp(self):
        self.responses = {}
        smart_fixture.SmartRefreshCoreTests.setUp(self)
        for name, dev in [('sdb', '8:16'), ('sdc', '8:32'), ('sdd', '8:48'),
                          ('mmcblk0', '179:0')]:
            self.block(name, dev, 'fixture-' + name)
        self.mounts = {name: (11 + i, f'8:{1 + i * 16}', f'/drives/fixture-{name}')
                       for i, name in enumerate(('sda', 'sdb', 'sdc', 'sdd'))}
        self.write(self.proc / 'self/mountinfo', ''.join(
            f'{mount_id} 1 {dev} / {target} rw - ext4 /dev/{name}1 rw\n'
            for name, (mount_id, dev, target) in self.mounts.items()))
        self.wall = 1700000000.0
        self.capacity_calls = []
        self.handles = []
        self.capacity_values = {name: {'total_bytes': (i + 1) * 1000,
                                      'used_bytes': (i + 1) * 250,
                                      'usage_percent': 25.0}
                                for i, name in enumerate(self.mounts)}
        self.watch = SimpleNamespace(changed=mock.Mock(return_value=False), close=mock.Mock())
        # Namespace/worker seams are deliberately portable, not fake Linux acceptance.
        self.install(service, '_capacity_device', side_effect=self.capacity_device)
        self.install(service, '_CapacityMountWatch', return_value=self.watch)
        self.install(service.time, 'time', side_effect=lambda: self.wall)
        self.sampler = service.StatusSampler(self.proc, self.sys)
        self.addCleanup(self.sampler.close_capacity)
        self.install(self.sampler, '_capacity_probe', side_effect=self.capacity_probe)
        self.monitor = service.StatusMonitor(self.sampler)
        self.monitor.status, self.monitor.updated_at = service.empty_status(), time.monotonic()
        self.panel = smart_fixture.panel_module.FnmonitorPanel(
            None, self.collector, None, None, None, self.monitor,
            state_dir=self.temporary / 'panel', clock=lambda: self.now,
            wall_clock=lambda: self.wall, proc_root=self.proc, sys_root=self.sys,
            dev_root=self.temporary / 'dev', udev_root=self.temporary / 'run/udev')

    def install(self, target, name, **kwargs):
        patch = mock.patch.object(target, name, **kwargs)
        value = patch.start()
        self.addCleanup(patch.stop)
        return value

    def runner(self, argv, timeout=3):
        self.calls.append((list(argv), timeout))
        value = self.responses.get(Path(argv[-1]).name, (0, self.good_response()))
        if callable(value):
            value = value(argv, timeout)
        if isinstance(value, BaseException):
            raise value
        code, data = value
        return SimpleNamespace(returncode=code, stdout=json.dumps(data),
                               stderr=smart_fixture.PRIVATE)

    def capacity_device(self, _sys, device):
        name = next((name for name, (_, dev, _) in self.mounts.items() if dev == device), None)
        if name is None:
            return None
        lifetime = self.collector._smart_context(name)
        return ('portable-kernel-fixture', lifetime, name) if lifetime else None

    def capacity_probe(self, entry, _deadline, guard):
        guard()
        name = entry['_device_name']
        self.capacity_calls.append(name)
        descriptor = os.open(os.devnull, os.O_RDONLY)
        self.handles.append(descriptor)
        return copy.deepcopy(self.capacity_values[name]), descriptor

    def response(self, power=MISSING, *, temperature=37, code=0, messages=None):
        data = self.good_response(temperature)
        if power is not MISSING:
            data['power_mode'] = copy.deepcopy(power)
        if messages is not None:
            data['smartctl'] = {'messages': [{'string': text} for text in messages]}
        return code, data

    def ordinary_mix(self):
        self.responses = {
            'sda': (2, {'power_mode': {'string': 'STANDBY'}}),
            'sdb': self.response({'name': 'ACTIVE'}, temperature=34),
            'sdc': self.response({'name': 'IDLE'}, temperature=35),
            'sdd': self.response({'name': 'ACTIVE or IDLE'}, temperature=36)}

    def project(self):
        self.collector._publish_integration('disks', self.collector._disks)
        # No background thread runs in this fixture. Keep only the monitor's
        # basic metadata heartbeat current; capacity is still reprojected by
        # the real StatusMonitor.snapshot/StatusSampler.capacity_snapshot path.
        self.monitor.updated_at = time.monotonic()
        return self.panel.overview()

    def refresh_visible(self):
        smart = self.collector.refresh_smart_visible(authorizer=lambda: self.active)
        eligible = self.collector.capacity_visible_eligibility(smart)
        capacity = self.monitor.refresh_capacity_visible(
            authorizer=lambda: self.active, eligibility=eligible)
        return smart, capacity, eligible, self.project()

    def seed_capacity(self):
        self.sampler.refresh_capacity(authorizer=lambda: self.active)
        old = {row['mount_id']: copy.deepcopy(row) for row in self.sampler.capacity_snapshot()}
        self.capacity_calls.clear()
        self.wall += 10
        for value in self.capacity_values.values():
            value.update(used_bytes=value['total_bytes'] // 2, usage_percent=50.0)
        return old

    def primary_round(self, power=MISSING, *, messages=None, code=0, temperature=37):
        """Run a new batch, without negative-result reuse hiding parser input."""
        self.now += 31
        self.wall += 31
        self.responses = {name: self.response({'string': 'ACTIVE'}) for name in self.mounts}
        self.responses['sda'] = self.response(power, messages=messages, code=code,
                                             temperature=temperature)
        self.capacity_calls.clear()
        smart, capacity, eligible, overview = self.refresh_visible()
        self.assertEqual(set(self.capacity_calls) - {'sda'}, {'sdb', 'sdc', 'sdd'})
        projected = {row['name']: row for row in overview['disks_detail']}
        for name in ('sdb', 'sdc', 'sdd'):
            self.assertEqual(projected[name]['capacity_status'], 'cached')
            self.assertIsNotNone(projected[name]['percent'])
        return ({row['device']: row for row in smart['results']}['sda'],
                {row['mount_id']: row for row in capacity['results']}[11],
                eligible, projected['sda'])

    def assert_closed_handles(self):
        for descriptor in set(self.handles):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_name_states_reach_per_batch_capacity_and_panel_with_a_standby(self):
        self.ordinary_mix()
        smart, capacity, eligible, overview = self.refresh_visible()
        statuses = {row['device']: row for row in smart['results']}
        self.assertEqual(statuses['sda']['status'], 'skipped_standby')
        self.assertEqual(statuses['mmcblk0']['reason'], 'smart_unsupported')
        self.assertEqual(eligible('sda'), 'standby')
        self.assertEqual(eligible('mmcblk0'), 'power_state_unverified')
        self.assertEqual(self.capacity_calls, ['sdb', 'sdc', 'sdd'])
        rows = {row['name']: row for row in overview['disks_detail']}
        self.assertEqual(set(rows), set(self.mounts))
        self.assertIsNone(rows['sda']['percent'])
        for name in ('sdb', 'sdc', 'sdd'):
            self.assertIsNone(eligible(name))
            self.assertIs(rows[name]['standby'], False)
            self.assertEqual(rows[name]['percent'], 25.0)
            self.assertEqual(rows[name]['health'], 'PASS')
            self.assertIsNotNone(rows[name]['temp'])
        self.assertEqual(overview['disk_size'], 9000)
        self.assertEqual(overview['disk_used'], 2250)
        self.assertEqual(overview['disk_total'], 25.0)
        self.assertEqual(capacity['results'][0]['status'], 'skipped_standby')
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(argv[1:] == ['-n', 'standby', '--json=o', '-a', argv[-1]]
                            for argv, _ in self.calls))
        self.assertNotIn(smart_fixture.PRIVATE, json.dumps((smart, capacity, overview)))
        self.assertFalse(self.collector._smart_cache)
        self.assertFalse(self.collector._disk_serial_cache)
        self.assert_closed_handles()

    def test_standby_preserves_old_capacity_while_name_active_disks_refresh(self):
        old = self.seed_capacity()
        self.ordinary_mix()
        _, capacity, _, overview = self.refresh_visible()
        rows = {row['name']: row for row in overview['disks_detail']}
        a = rows['sda']
        self.assertEqual((a['total'], a['used'], a['percent'], a['capacity_sampled_at']),
                         (1000, 250, 25.0, old[11]['capacity_sampled_at']))
        self.assertEqual(self.capacity_calls, ['sdb', 'sdc', 'sdd'])
        for name in ('sdb', 'sdc', 'sdd'):
            self.assertEqual(rows[name]['percent'], 50.0)
            self.assertGreater(rows[name]['capacity_sampled_at'], old[self.mounts[name][0]]['capacity_sampled_at'])
        self.assertEqual(capacity['results'][0]['sampled_at'], old[11]['capacity_sampled_at'])
        self.assertEqual(overview['disk_total'], 47.5)
        self.assert_closed_handles()

    def test_legacy_string_states_continue_to_grant_capacity(self):
        for state in ('ACTIVE', 'IDLE', 'ACTIVE or IDLE', 'idle mode'):
            with self.subTest(state=state):
                smart, capacity, eligible, row = self.primary_round({'string': state})
                self.assertEqual(smart['status'], 'refreshed')
                self.assertIsNone(eligible('sda'))
                self.assertEqual(capacity['status'], 'refreshed')
                self.assertEqual(row['percent'], 25.0)
                self.assertIs(row['standby'], False)

    def test_missing_power_keeps_valid_temperature_health_without_capacity_proof(self):
        for state in (MISSING, {}, {'string': ''}):
            with self.subTest(state=state):
                smart, capacity, eligible, row = self.primary_round(state)
                self.assertEqual(smart['status'], 'refreshed')
                self.assertEqual(eligible('sda'), 'power_state_unverified')
                self.assertNotIn('sda', self.capacity_calls)
                self.assertEqual((capacity['status'], capacity['reason']),
                                 ('unavailable', 'power_state_unverified'))
                self.assertEqual(row['temp'], 37)
                self.assertEqual(row['health'], 'PASS')
                self.assertIsNone(row['standby'])
                self.assertIsNone(row['percent'])

    def test_ignored_standby_protection_never_grants_even_with_active_string(self):
        for note in ('Ignoring -n', 'Power mode is unsupported',
                     'Device power state unknown; ignoring -n'):
            with self.subTest(note=note):
                smart, capacity, eligible, row = self.primary_round(
                    {'string': 'ACTIVE'}, messages=[note])
                self.assertEqual((smart['status'], smart['reason']),
                                 ('unavailable', 'power_mode_unverified'))
                self.assertEqual(eligible('sda'), 'power_state_unverified')
                self.assertNotIn('sda', self.capacity_calls)
                self.assertEqual(capacity['reason'], 'power_state_unverified')
                self.assertIsNone(row['temp'])
                self.assertIsNone(row['health'])

    def test_positive_capacity_does_not_grant_missing_power_or_renew_history(self):
        old = self.seed_capacity()
        _, capacity, eligible, row = self.primary_round(MISSING)
        self.assertEqual(eligible('sda'), 'power_state_unverified')
        self.assertNotIn('sda', self.capacity_calls)
        self.assertEqual((row['total'], row['used'], row['percent'], row['capacity_sampled_at']),
                         (1000, 250, 25.0, old[11]['capacity_sampled_at']))
        self.assertEqual(capacity['sampled_at'], old[11]['capacity_sampled_at'])

    def test_temperature_missing_does_not_destroy_independent_healthy_result(self):
        smart, capacity, eligible, row = self.primary_round({'string': 'ACTIVE'}, temperature=None)
        self.assertEqual(smart['status'], 'refreshed')
        self.assertIsNone(eligible('sda'))
        self.assertEqual(capacity['status'], 'refreshed')
        self.assertIsNone(row['temp'])
        self.assertEqual(row['health'], 'PASS')

    def test_official_name_ata_pairs_reach_capacity_in_exact_supported_states(self):
        # smartmontools 7.5 ataprint.cpp: ata_value is not an inferred truthy flag.
        for name, value in [('IDLE', 128), ('IDLE_A', 129), ('IDLE_B', 130),
                            ('IDLE_C', 131), ('ACTIVE or IDLE', 255)]:
            with self.subTest(name=name, ata_value=value):
                smart, capacity, eligible, row = self.primary_round(
                    {'name': name, 'ata_value': value})
                self.assertEqual(smart['status'], 'refreshed')
                self.assertIsNone(eligible('sda'))
                self.assertEqual(capacity['status'], 'refreshed')
                self.assertIs(row['standby'], False)
                self.assertEqual((row['temp'], row['health'], row['percent']), (37, 'PASS', 25.0))

    def test_name_string_canonical_agreement_preserves_legacy_mode_suffix(self):
        for power in ({'name': '  active  OR\t idle  ', 'string': 'ACTIVE or IDLE mode', 'ata_value': 255},
                      {'name': 'idle_a', 'string': ' IDLE_A MODE ', 'ata_value': 129},
                      {'name': 'ACTIVE', 'string': ' active mode '},
                      {'string': 'idle mode', 'ata_value': 128}):
            with self.subTest(power=power):
                _, capacity, eligible, row = self.primary_round(power)
                self.assertIsNone(eligible('sda'))
                self.assertEqual(capacity['status'], 'refreshed')
                self.assertIs(row['standby'], False)

    def test_official_sleep_standby_and_standby_y_skip_without_sampling_capacity(self):
        for name, value in [('SLEEP', -1), ('STANDBY', 0), ('STANDBY_Y', 1)]:
            with self.subTest(name=name, ata_value=value):
                smart, capacity, eligible, row = self.primary_round(
                    {'name': name, 'ata_value': value}, code=2)
                self.assertEqual(smart['status'], 'skipped_standby')
                self.assertEqual(eligible('sda'), 'standby')
                self.assertEqual((capacity['status'], capacity['reason']), ('skipped_standby', 'standby'))
                self.assertNotIn('sda', self.capacity_calls)
                self.assertIs(row['standby'], True)
                self.assertIsNone(row['temp'])
                self.assertIsNone(row['health'])

    def test_name_string_conflicts_cannot_choose_a_favorable_fallback(self):
        for power in ({'name': 'ACTIVE', 'string': 'STANDBY'},
                      {'name': 'STANDBY', 'string': 'ACTIVE'},
                      {'name': 'IDLE', 'string': 'ACTIVE or IDLE'},
                      {'name': 'ACTIVE', 'string': 'UNKNOWN'},
                      {'name': '', 'string': 'ACTIVE'},
                      {'name': None, 'string': 'ACTIVE'},
                      {'name': 'ACTIVE', 'string': None}):
            with self.subTest(power=power):
                _, capacity, eligible, _ = self.primary_round(power)
                self.assertEqual(eligible('sda'), 'power_state_unverified')
                self.assertNotIn('sda', self.capacity_calls)
                self.assertEqual((capacity['status'], capacity['reason']),
                                 ('unavailable', 'power_state_unverified'))

    def test_numeric_conflicts_types_and_active_without_exact_mapping_never_grant(self):
        cases = [{'name': 'IDLE', 'ata_value': value}
                 for value in (0, 1, -1, 129, 255, True, False, 128.0, '128', None, [], {})]
        cases += [{'name': 'ACTIVE', 'ata_value': value} for value in (0, 128, 255)]
        cases += [{'string': 'ACTIVE or IDLE', 'ata_value': 0},
                  {'name': 'STANDBY', 'ata_value': 255}]
        for power in cases:
            with self.subTest(power=power):
                _, capacity, eligible, _ = self.primary_round(power)
                self.assertEqual(eligible('sda'), 'power_state_unverified')
                self.assertNotIn('sda', self.capacity_calls)
                self.assertEqual(capacity['reason'], 'power_state_unverified')

    def test_numeric_only_missing_malformed_or_unknown_cannot_prove_nonstandby(self):
        cases = [None, [], 'ACTIVE', True, 255, {'ata_value': 0}, {'ata_value': 255},
                 {'name': None}, {'name': True}, {'name': ['ACTIVE']},
                 {'name': {'name': 'ACTIVE'}}, {'name': 'ACTIVE?'},
                 {'name': 'UNKNOWN'}, {'name': 'unsupported'}]
        for power in cases:
            with self.subTest(power=power):
                _, capacity, eligible, _ = self.primary_round(power)
                self.assertEqual(eligible('sda'), 'power_state_unverified')
                self.assertNotIn('sda', self.capacity_calls)
                self.assertEqual(capacity['reason'], 'power_state_unverified')

    def test_active_nv_modes_keep_valid_measurement_without_new_capacity_authority(self):
        for name, value in [('ACTIVE_NV_DOWN', 64), ('ACTIVE_NV_UP', 65)]:
            with self.subTest(name=name):
                smart, capacity, eligible, row = self.primary_round({'name': name, 'ata_value': value})
                self.assertEqual(smart['status'], 'refreshed')
                self.assertEqual(eligible('sda'), 'power_state_unverified')
                self.assertNotIn('sda', self.capacity_calls)
                self.assertEqual(capacity['reason'], 'power_state_unverified')
                self.assertEqual((row['temp'], row['health']), (37, 'PASS'))
                self.assertIsNone(row['standby'])

    def test_ignored_n_still_rejects_official_name_and_matching_ata(self):
        smart, capacity, eligible, row = self.primary_round(
            {'name': 'ACTIVE or IDLE', 'ata_value': 255}, messages=['Ignoring -n'])
        self.assertEqual((smart['status'], smart['reason']), ('unavailable', 'power_mode_unverified'))
        self.assertEqual(eligible('sda'), 'power_state_unverified')
        self.assertEqual(capacity['reason'], 'power_state_unverified')
        self.assertNotIn('sda', self.capacity_calls)
        self.assertIsNone(row['temp'])
        self.assertIsNone(row['health'])

    def test_official_power_evidence_is_bound_to_exact_batch_receipt(self):
        self.ordinary_mix()
        smart = self.collector.refresh_smart_visible(authorizer=lambda: self.active)
        copied_eligibility = self.collector.capacity_visible_eligibility(copy.deepcopy(smart))
        capacity = self.monitor.refresh_capacity_visible(
            authorizer=lambda: self.active, eligibility=copied_eligibility)
        self.assertEqual(self.capacity_calls, [])
        self.assertTrue(all(row['reason'] == 'power_state_unverified' for row in capacity['results']))
        self.assertTrue(all(row['total_bytes'] is None for row in self.sampler.capacity_snapshot()))
        original_eligibility = self.collector.capacity_visible_eligibility(smart)
        self.assertIsNone(original_eligibility('sdb'))

    def test_official_power_proof_does_not_survive_device_replacement(self):
        self.ordinary_mix()
        smart = self.collector.refresh_smart_visible(authorizer=lambda: self.active)
        eligibility = self.collector.capacity_visible_eligibility(smart)
        self.write(self.sys / 'block/sdb/wwid', 'replacement-disk\n')
        self.assertEqual(eligibility('sdb'), 'power_state_unverified')
        capacity = self.monitor.refresh_capacity_visible(
            authorizer=lambda: self.active, eligibility=eligibility)
        rows = {row['name']: row for row in self.project()['disks_detail']}
        self.assertEqual(self.capacity_calls, ['sdc', 'sdd'])
        self.assertIsNone(rows['sdb']['percent'])
        self.assertIsNone(rows['sdb']['temp'])
        self.assertEqual(next(row for row in capacity['results'] if row['mount_id'] == 12)['reason'],
                         'power_state_unverified')
        self.assertEqual(rows['sdc']['percent'], 25.0)
        self.assertEqual(rows['sdd']['percent'], 25.0)

    def test_current_owner_revocation_blocks_capacity_even_after_valid_name_batch(self):
        self.ordinary_mix()
        smart = self.collector.refresh_smart_visible(authorizer=lambda: self.active)
        eligibility = self.collector.capacity_visible_eligibility(smart)
        self.active = False
        with self.assertRaises(service.CapacityRefreshError) as raised:
            self.monitor.refresh_capacity_visible(authorizer=lambda: self.active,
                                                  eligibility=eligibility)
        self.assertEqual(raised.exception.reason, 'authorization_denied')
        self.assertEqual(self.capacity_calls, [])

    def test_manual_missing_power_keeps_measurement_but_cannot_authorize_old_fill(self):
        self.responses = {name: self.response({'name': 'IDLE', 'ata_value': 128})
                          for name in self.mounts}
        self.responses['sda'] = self.response()
        self.collector.refresh_smart(force=False, authorizer=lambda: self.active)
        sample = self.collector.smart_cached('sda')
        self.assertEqual((sample['temp_c'], sample['health']['passed'], sample['standby']),
                         (37, True, None))
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')
        self.monitor.refresh_capacity_missing(authorizer=lambda: self.active,
            eligibility=self.collector.capacity_fill_eligibility)
        self.assertEqual(self.capacity_calls, ['sdb', 'sdc', 'sdd'])
        self.now += 31
        self.responses['sda'] = self.response({'name': 'IDLE', 'ata_value': 128})
        self.collector.refresh_smart(force=False, authorizer=lambda: self.active)
        self.assertIsNone(self.collector.capacity_fill_eligibility('sda'))
        self.capacity_calls.clear()
        self.monitor.refresh_capacity_missing(authorizer=lambda: self.active,
            eligibility=self.collector.capacity_fill_eligibility)
        self.assertEqual(self.capacity_calls, ['sda'])

    def test_old_smart_fill_cannot_promote_missing_power_cached_measurement(self):
        self.responses = {name: self.response({'name': 'ACTIVE'}) for name in self.mounts}
        self.responses['sda'] = self.response()
        first = self.collector.fill_smart_missing(authorizer=lambda: self.active)
        self.assertEqual(next(row for row in first['results'] if row['device'] == 'sda')['status'], 'refreshed')
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')
        before = len(self.calls)
        second = self.collector.fill_smart_missing(authorizer=lambda: self.active)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(next(row for row in second['results'] if row['device'] == 'sda')['status'], 'cached')
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')
        self.monitor.refresh_capacity_missing(authorizer=lambda: self.active,
            eligibility=self.collector.capacity_fill_eligibility)
        self.assertEqual(self.capacity_calls, ['sdb', 'sdc', 'sdd'])

    def test_forced_official_power_sample_does_not_create_ordinary_fill_evidence(self):
        self.responses = {name: self.response({'name': 'IDLE', 'ata_value': 128})
                          for name in self.mounts}
        self.collector.refresh_smart(force=True, authorizer=lambda: self.active)
        self.assertTrue(all(argv[1:3] == ['--json=o', '-a'] for argv, _ in self.calls))
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')
        self.monitor.refresh_capacity_missing(authorizer=lambda: self.active,
            eligibility=self.collector.capacity_fill_eligibility)
        self.assertEqual(self.capacity_calls, [])


if __name__ == '__main__':
    unittest.main()

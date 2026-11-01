"""Attempt evidence follows real capacity decisions, without repeating checks."""
import copy
import json
import unittest
from collections import Counter
from unittest import mock

import test_capacity_history_core as history

service = history.service


class CapacityAttemptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = history.CapacityHistoryCoreTests(
            'test_core_commit_stages_but_does_not_publish_history')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.sampler = self.fixture.sampler

    def diagnostics(self):
        value = self.sampler.capacity_cache_diagnostics()['attempt_diagnostics']
        return service._capacity_attempt_decode(value) if 'encoding' in value else value

    def latest(self):
        value = self.diagnostics()
        return next(row for row in value['attempts'] if row['id'] == value['latest_completed'])

    def phases(self, record, kind='phases'):
        data = self.diagnostics()
        result = []
        for stage, phase, status, reason, pairs, error in record[kind]:
            result.append({'stage': data['stage_values'][stage],
                'phase': data['worker_phase_order'][phase] if phase is not None else None,
                'status': data['status_values'][status], 'reason': data['reason_values'][reason],
                'observations': dict(pairs), 'error': error})
        return result

    def test_rejected_attempt_is_retained_outside_empty_poll_ring(self):
        with mock.patch.object(self.sampler, '_capacity_probe',
                               side_effect=service.CapacityRefreshError('identity_changed')):
            result = self.fixture.refresh(publish=False)
        self.assertEqual([row['status'] for row in result['results']],
                         ['identity_changed'] * 3)
        before = self.diagnostics()
        self.assertIsNotNone(before['first_rejected_id'])
        for _ in range(45):
            self.sampler.capacity_snapshot()
        after = self.diagnostics()
        self.assertEqual(after['first_rejected_id'], before['first_rejected_id'])

    def test_real_success_has_no_unclassified_or_observer_errors(self):
        self.fixture.refresh()
        self.assertEqual(self.diagnostics()['observation_errors'], 0)
        self.assertEqual(self.latest()['source'], 'explicit_refresh')
        self.assertEqual(self.latest()['origin'], 'unknown')

    def test_later_success_does_not_evict_first_failure(self):
        with mock.patch.object(self.sampler, '_capacity_probe',
                               side_effect=service.CapacityRefreshError('identity_changed')):
            self.fixture.refresh(publish=False)
        original = self.diagnostics()['first_rejected_id']
        for _ in range(9):
            self.fixture.refresh()
        self.assertLessEqual(len(self.diagnostics()['attempts']), 4)
        first = next(row for row in self.diagnostics()['attempts'] if row['id'] == original)
        self.assertEqual(first['first_rejection']['reason'], 'identity_changed')

    def test_per_row_rejection_survives_final_generic_disposition(self):
        with mock.patch.object(self.sampler, '_capacity_probe',
                               side_effect=service.CapacityRefreshError('identity_changed')):
            self.fixture.refresh(publish=False)
        for row in self.latest()['rows'].values():
            data = self.diagnostics()
            self.assertEqual(data['stage_values'][row['first_rejection'][0]], 'worker_request')
            self.assertEqual(data['reason_values'][row['first_rejection'][3]], 'identity_changed')
            self.assertTrue(any(p['stage'] == 'commit' and p['status'] == 'failed'
                                for p in self.phases(row)))

    def test_watch_guard_rejection_precedes_outer_probe_error(self):
        def changed(entry, deadline, guard):
            self.fixture.watch.changed.return_value = True
            self.fixture.watch.observation = {'reason': 'mount_event', 'polled': True,
                                             'namespace_read': True, 'mask': 10}
            return self.fixture.measure(entry, deadline, guard)
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=changed):
            result = self.fixture.refresh(publish=False)
        first = self.latest()['first_rejection']
        self.assertEqual(first['stage'], 'parent_watch')
        self.assertEqual(first['reason'], 'mount_epoch_changed')
        self.assertEqual(first['observations']['watch_reason'], 'mount_event')
        self.assertEqual(result['results'][0]['status'], 'identity_changed')

    def test_same_batch_key_change_is_not_reported_as_worker_failure_only(self):
        def changed(entry, deadline, guard):
            value = self.fixture.measure(entry, deadline, guard)
            self.fixture.generations[entry['device']] += 1
            return value
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=changed):
            self.fixture.refresh(publish=False)
        first = self.latest()['first_rejection']
        self.assertEqual(first['stage'], 'post_probe_inventory')
        self.assertEqual(first['reason'], 'batch_changed')
        self.fixture.assert_handles_closed()

    def test_authorization_exception_preserves_error_and_releases_batch_lock(self):
        with self.assertRaises(service.CapacityRefreshError) as caught:
            self.sampler.refresh_capacity(authorizer=lambda: False)
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        self.assertEqual(self.latest()['status'], 'failed')
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())
        self.fixture.refresh()

    def test_history_candidate_and_actual_publication_are_separate(self):
        receipt = self.fixture.refresh(publish=False)
        phases = self.phases(self.latest(), 'history')
        self.assertTrue(any(p['stage'] == 'candidate' and p['status'] == 'passed' for p in phases))
        self.assertFalse(any(p['stage'] == 'finalize' for p in phases))
        self.assertTrue(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))
        self.assertTrue(any(p['stage'] == 'finalize' and p['phase'] == 'published'
                            for p in self.phases(self.latest(), 'history')))

    def test_history_prune_records_first_real_removed_binding(self):
        self.fixture.refresh()
        self.fixture.generations['8:1'] += 1
        self.fixture.view()
        first = copy.deepcopy(self.diagnostics()['first_historical_removal'])
        self.assertIsNotNone(first)
        for _ in range(40):
            self.fixture.view()
        self.assertEqual(self.diagnostics()['first_historical_removal'], first)

    def test_diagnostic_read_is_ram_only_even_with_real_attempts(self):
        self.fixture.refresh()
        with mock.patch.object(self.sampler, '_capacity_inventory_observe', side_effect=AssertionError()), \
                mock.patch.object(self.sampler, '_capacity_check_watch', side_effect=AssertionError()), \
                mock.patch.object(self.sampler, 'capacity_snapshot', side_effect=AssertionError()), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=AssertionError()):
            for _ in range(5):
                self.assertEqual(self.diagnostics()['latest_completed'], '1')

    def test_private_attempt_carrier_does_not_enter_public_json_or_copy(self):
        receipt = self.fixture.refresh(publish=False)
        self.assertIsNotNone(receipt.capacity_attempt_id)
        self.assertNotIn('capacity_attempt', json.dumps(receipt))
        self.assertFalse(hasattr(copy.deepcopy(receipt), 'capacity_attempt_id'))

    def test_origin_is_annotation_and_does_not_grant_authority(self):
        previous = self.sampler.capacity_attempt_origin_set('web_owner_refresh')
        with self.assertRaises(service.CapacityRefreshError):
            self.sampler.refresh_capacity(authorizer=lambda: False)
        self.assertEqual(self.latest()['origin'], 'web_owner_refresh')
        self.sampler.capacity_attempt_origin_set(previous)

    def test_start_does_not_let_old_response_correlate_to_new_attempt(self):
        old = self.fixture.refresh(publish=False)
        self.sampler.start_capacity()
        self.assertFalse(self.sampler.publish_capacity_history(old, authorizer=lambda: True))
        self.assertFalse(self.diagnostics()['attempts'])

    def test_observer_entry_and_storage_failures_do_not_change_success(self):
        for target, name in ((self.sampler, '_capacity_attempt_step'),
                             (self.sampler._capacity_attempts, 'step'),
                             (self.sampler._capacity_attempts, 'finish'),
                             (self.sampler._capacity_attempts, 'history')):
            with self.subTest(name=name), mock.patch.object(target, name, side_effect=ValueError('private-canary')):
                receipt = self.fixture.refresh(publish=False)
                self.assertEqual(receipt['status'], 'complete')
                self.assertTrue(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))
                self.assertFalse(self.sampler._capacity_refresh_lock.locked())
                self.fixture.assert_handles_closed()

    def test_constructor_unavailable_does_not_change_business_cache(self):
        self.sampler._capacity_attempts = None
        receipt = self.fixture.refresh(publish=False)
        self.assertTrue(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))
        self.assertEqual(receipt['status'], 'complete')
        self.assertEqual(len(self.sampler._capacity_cache), 3)
        self.assertEqual(self.diagnostics()['observation_status'], 'observer_error')

    def test_observer_entry_and_storage_double_fault_preserve_business_and_release(self):
        with mock.patch.object(self.sampler, '_capacity_observe', side_effect=ValueError('private-entry')), \
                mock.patch.object(self.sampler._capacity_attempts, 'finish', side_effect=RuntimeError('private-store')):
            receipt = self.fixture.refresh(publish=False)
            self.assertEqual(receipt['status'], 'complete')
            self.assertTrue(self.sampler.publish_capacity_history(receipt, authorizer=lambda: True))
            self.assertEqual(len(self.sampler._capacity_cache), 3)
            self.assertFalse(self.sampler._capacity_refresh_lock.locked())
            self.fixture.assert_handles_closed()
        # Observation recovery must not require clearing a permanently busy
        # lock or resubmitting the failed observation's real business request.
        later = self.fixture.refresh(publish=False)
        self.assertEqual(later['status'], 'complete')
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())

    def test_observer_entry_fault_does_not_replace_original_business_exception(self):
        failure = service.CapacityRefreshError('authorization_denied')
        with mock.patch.object(self.sampler, '_capacity_observe', side_effect=ValueError('private-entry')), \
                mock.patch.object(self.sampler, '_capacity_refresh_acquired', side_effect=failure):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertIs(caught.exception, failure)
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())
        self.fixture.assert_handles_closed()

    def test_admission_failure_cannot_reuse_previous_row_or_phase(self):
        self.fixture.refresh()
        self.fixture.mounts = [(22, '8:17', '/drives/new')]
        self.fixture.write_mounts()
        with self.assertRaises(service.CapacityRefreshError):
            self.sampler.refresh_capacity(authorizer=lambda: False)
        attempt = self.latest()
        self.assertEqual(attempt['rows'], {})
        self.assertIsNone(attempt['first_rejection']['mount_id'])
        self.assertEqual(attempt['first_rejection']['stage'], 'authorization')

    def test_real_seventeen_row_guard_shape_retains_cause_after_other_source_success(self):
        mounts = [(539, '8:50', 'sdd'), (517, '8:49', 'sdd'), (605, '8:18', 'sdb'),
                  (583, '8:17', 'sdb'), (638, '8:2', 'sda'), (616, '8:1', 'sda'),
                  (550, '8:33', 'sdc'), (572, '8:34', 'sdc')]
        bydev = {dev: name for _, dev, name in mounts}
        bydev['259:1'] = 'mmcblk0'
        def identity(_sys, dev):
            name = bydev.get(dev)
            if name is None:
                return None
            parent = {'sda': '8:0', 'sdb': '8:16', 'sdc': '8:32', 'sdd': '8:48', 'mmcblk0': '179:0'}[name]
            minor = int(dev.split(':')[1])
            return ('/sys/fixed/' + dev, '/sys/fixed/' + name, (18, minor + 1000, 1),
                    (18, minor + 2000, 1), (18, 3000 + int(parent.split(':')[1]), 1), parent, '5', name)
        lines = []
        for index, (mid, dev, name) in enumerate(mounts):
            lines.extend((f'{mid} 1 {dev} / /drives/p{mid} rw - ext4 /dev/{name}{index + 1} rw\n',
                          f'{800 + index} 1 0:{100 + index} / /drives/f{index} rw - fuseblk fuse rw\n'))
        lines.append('225 1 259:1 / /drives/system rw - ext4 /dev/mmcblk0p1 rw\n')
        (self.fixture.proc / 'self/mountinfo').write_text(''.join(lines), encoding='utf-8')
        triggered = False
        def watch_changed():
            nonlocal triggered
            result, triggered = triggered, False
            self.fixture.watch.observation = {'namespace_read': True, 'polled': True,
                'reason': 'mount_event' if result else 'unchanged', 'mask': 10 if result else 0}
            return result
        self.fixture.watch.changed.side_effect = watch_changed
        fired = False
        def rejected(entry, deadline, guard):
            nonlocal fired, triggered
            if not fired:
                fired, triggered = True, True
            guard()
            self.fail('the original real namespace gate must reject before measurement')
        eligibility = lambda name: 'power_state_unverified' if name == 'mmcblk0' else None
        with mock.patch.object(service, '_capacity_device', side_effect=identity):
            with mock.patch.object(self.sampler, '_capacity_probe', side_effect=rejected):
                result = self.sampler.refresh_capacity_visible(authorizer=lambda: True, eligibility=eligibility)
            self.assertEqual(Counter(row['reason'] for row in result['results']),
                             {'identity_changed': 8, 'unsupported_filesystem': 8, 'power_state_unverified': 1})
            first = self.latest()
            self.assertEqual(self.diagnostics()['observation_errors'], 0)
            self.assertEqual(self.diagnostics()['observation_status'], 'complete')
            self.assertEqual(first['first_rejection']['reason'], 'mount_epoch_changed')
            for _ in range(40):
                self.sampler.capacity_snapshot()
            self.sampler.refresh_capacity_missing(authorizer=lambda: True, eligibility=eligibility)
            retained = next(row for row in self.diagnostics()['attempts'] if row['id'] == first['id'])
            self.assertEqual(retained, first)
            self.assertEqual(self.diagnostics()['first_rejected_id'], first['id'])
            self.assertEqual(len(self.sampler._capacity_cache), 8)


if __name__ == '__main__':
    unittest.main()

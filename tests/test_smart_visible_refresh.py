"""Explicit visible-page refresh; fake SMART commands, real fixture metadata."""
import copy
import json
import subprocess
import unittest
from unittest import mock

import test_smart_refresh_core as fixture


class SmartVisibleRefreshTests(unittest.TestCase):
    setUp = fixture.SmartRefreshCoreTests.setUp
    write = fixture.SmartRefreshCoreTests.write
    block = fixture.SmartRefreshCoreTests.block
    runner = fixture.SmartRefreshCoreTests.runner
    row = fixture.SmartRefreshCoreTests.row
    publish_disks = fixture.SmartRefreshCoreTests.publish_disks
    refresh = fixture.SmartRefreshCoreTests.refresh
    good_response = staticmethod(fixture.SmartRefreshCoreTests.good_response)

    def visible(self):
        return self.collector.refresh_smart_visible(authorizer=lambda: self.active)

    def active_response(self, temperature=46, power='ACTIVE or IDLE'):
        return {'power_mode': {'string': power}, **self.good_response(temperature)}

    def test_positive_manual_cache_does_not_replace_new_command_or_expand_identity_authority(self):
        self.refresh()
        original = copy.deepcopy(self.collector._smart_cache)
        self.now += 1
        self.behavior = (0, self.active_response())
        result = self.visible()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][0][1:], ['-n', 'standby', '--json=o', '-a', '/dev/sda'])
        self.assertFalse(result['force'])
        self.assertEqual(self.collector._smart_cache, original)
        current = self.collector.smart_cached('sda')
        self.assertEqual(current['smart_temperature_c'], 46)
        self.assertFalse(current['standby'])
        self.assertNotIn('smart_data', current)
        self.assertEqual(current['smart_identity'], original['sda'][1]['smart_identity'])
        self.assertIsNone(self.collector.capacity_visible_eligibility(result)('sda'))

    def test_cold_visible_refresh_is_measurement_only_and_repeats_positive_measurement(self):
        self.behavior = (0, self.active_response())
        first = self.visible()
        self.visible()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.collector._smart_cache, {})
        self.assertEqual(self.collector._disk_serial_cache, {})
        self.assertNotIn(fixture.PRIVATE, json.dumps(self.collector._smart_measurements))
        self.assertNotIn('smart_data', self.collector.smart_cached('sda'))
        self.assertIsNone(self.collector.smart_cached('sda').get('smart_identity'))
        self.assertEqual(self.collector.capacity_visible_eligibility(first)('sda'), 'power_state_unverified')

    def test_explicit_active_power_enumerations_only(self):
        for power in ('ACTIVE', 'IDLE', 'ACTIVE or IDLE', 'idle mode'):
            with self.subTest(power=power):
                self.behavior = (0, self.active_response(power=power))
                result = self.visible()
                self.assertIsNone(self.collector.capacity_visible_eligibility(result)('sda'))
                self.assertIs(self.collector.smart_cached('sda')['standby'], False)

    def test_null_or_missing_power_preserves_valid_measurement_without_capacity_grant(self):
        for power in (None, '', 'UNKNOWN', 'unsupported', 'ACTIVE?'):
            with self.subTest(power=power):
                self.collector._smart_attempts.clear()  # Each malformed fixture is a new attempt.
                value = self.good_response()
                if power is not None:
                    value['power_mode'] = {'string': power}
                self.behavior = (0, value)
                result = self.visible()
                self.assertEqual(self.collector.capacity_visible_eligibility(result)('sda'), 'power_state_unverified')
                if power in ('UNKNOWN', 'unsupported'):
                    self.assertEqual(result['results'][0]['status'], 'unavailable')
                    self.assertEqual(result['results'][0]['reason'], 'power_mode_unverified')
                    self.assertIsNone(self.collector.smart_cached('sda')['smart_temperature_c'])
                else:
                    self.assertEqual(result['results'][0]['status'], 'refreshed')
                    self.assertIsNone(self.collector.smart_cached('sda')['standby'])
                    self.assertEqual(self.collector.smart_cached('sda')['smart_temperature_c'], 37)
                    self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')

    def test_standby_negative_reuse_keeps_original_attempt_time_and_never_grants_capacity(self):
        self.behavior = (2, {'power_mode': {'string': 'STANDBY'}})
        first = self.visible()
        self.now += 29
        second = self.visible()
        self.assertEqual(second['results'], first['results'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.collector.capacity_visible_eligibility(second)('sda'), 'standby')
        self.assertIsNone(self.collector.smart_cached('sda')['smart_temperature_c'])
        self.now += 1
        self.visible()
        self.assertEqual(len(self.calls), 2)

    def test_new_failure_masks_old_fresh_temperature_but_preserves_historical_date(self):
        self.refresh()
        before = self.collector.smart_cached('sda')
        manual = copy.deepcopy(self.collector._smart_cache)
        self.now += 1
        self.behavior = subprocess.TimeoutExpired('fixture-command', 3)
        first = self.visible()
        value = self.collector.smart_cached('sda')
        self.assertIsNone(value['smart_temperature_c'])
        self.assertIsNone(value['temp_c'])
        self.assertIsNone(value['health']['passed'])
        self.assertFalse(value['smart_fresh'])
        self.assertEqual(value['smart_sampled_at'], before['smart_sampled_at'])
        self.assertEqual(value['smart_sampled_monotonic'], before['smart_sampled_monotonic'])
        self.assertEqual(self.collector._smart_cache, manual)
        self.assertEqual(self.collector.capacity_visible_eligibility(first)('sda'), 'power_state_unverified')
        self.assertEqual(self.visible()['results'], first['results'])
        self.assertEqual(len(self.calls), 2)

    def test_failure_preserves_newer_visible_date_instead_of_older_manual_date(self):
        self.refresh()
        manual = self.collector.smart_cached('sda')['smart_sampled_at']
        self.now += 5
        self.behavior = (0, self.active_response())
        with mock.patch.object(fixture.insights.time, 'time', return_value=manual + 5):
            self.visible()
        before = self.collector.smart_cached('sda')
        self.assertEqual(before['smart_sampled_at'], manual + 5)
        self.now += 1
        self.behavior = subprocess.TimeoutExpired('fixture-command', 3)
        self.visible()
        after = self.collector.smart_cached('sda')
        self.assertIsNone(after['smart_temperature_c'])
        self.assertEqual(after['smart_sampled_at'], before['smart_sampled_at'])
        self.assertEqual(after['smart_sampled_monotonic'], before['smart_sampled_monotonic'])

    def test_new_standby_cannot_display_old_manual_temperature_as_current(self):
        self.refresh()
        self.behavior = (2, {'power_mode': {'string': 'STANDBY'}})
        receipt = self.visible()
        value = self.collector.smart_cached('sda')
        self.assertTrue(value['standby'])
        self.assertIsNone(value['smart_temperature_c'])
        self.assertEqual(self.collector.capacity_visible_eligibility(receipt)('sda'), 'standby')

    def test_later_actual_manual_success_supersedes_failure_without_changing_fill_contract(self):
        self.refresh()
        self.behavior = (1, {})
        self.visible()
        self.behavior = (0, self.good_response(41))
        self.refresh()
        self.assertEqual(self.collector.smart_cached('sda')['smart_temperature_c'], 41)
        calls = len(self.calls)
        self.assertEqual(self.collector.fill_smart_missing(authorizer=lambda: True)['results'][0]['status'], 'cached')
        self.assertEqual(len(self.calls), calls)

    def test_stop_does_not_revive_old_manual_temperature_hidden_by_failure(self):
        self.refresh()
        self.behavior = (1, {})
        self.visible()
        self.collector.stop()
        self.collector._stop.clear()
        value = self.collector.smart_cached('sda')
        self.assertIsNone(value['smart_temperature_c'])
        self.assertFalse(value['smart_fresh'])

    def test_receipt_copies_other_round_and_later_actual_attempt_cannot_reuse_grant(self):
        self.behavior = (0, self.active_response())
        first = self.visible()
        granted = self.collector.capacity_visible_eligibility(first)
        self.assertIsNone(granted('sda'))
        self.assertEqual(self.collector.capacity_visible_eligibility(copy.deepcopy(first))('sda'), 'power_state_unverified')
        self.visible()  # Same fake monotonic time: sequence must still distinguish it.
        self.assertEqual(granted('sda'), 'power_state_unverified')
        current = self.visible()
        granted = self.collector.capacity_visible_eligibility(current)
        self.refresh()
        self.assertEqual(granted('sda'), 'power_state_unverified')

    def test_grant_rechecks_lifetime_expiry_and_collector_epoch(self):
        self.behavior = (0, self.active_response())
        receipt = self.visible()
        granted = self.collector.capacity_visible_eligibility(receipt)
        self.write(self.folder / 'dev', '8:16')
        self.assertEqual(granted('sda'), 'power_state_unverified')
        self.write(self.folder / 'dev', '8:0')
        receipt = self.visible()
        granted = self.collector.capacity_visible_eligibility(receipt)
        self.now += 30
        self.assertEqual(granted('sda'), 'power_state_unverified')
        receipt = self.visible()
        granted = self.collector.capacity_visible_eligibility(receipt)
        self.collector.stop()
        self.collector._stop.clear()
        self.assertEqual(granted('sda'), 'power_state_unverified')

    def test_replacement_inside_command_never_publishes_a_visible_measurement(self):
        def replace(argv, timeout):
            self.write(self.folder / 'dev', '8:16')
            return 0, self.active_response()
        self.behavior = replace
        receipt = self.visible()
        self.assertEqual(receipt['results'][0]['status'], 'identity_changed')
        self.assertEqual(self.collector._smart_measurements, {})
        self.assertEqual(self.collector.capacity_visible_eligibility(receipt)('sda'), 'power_state_unverified')

    def test_owner_revocation_discards_both_measurements_and_failure_overlay(self):
        self.refresh()
        before = self.collector.smart_cached('sda')
        def revoke(argv, timeout):
            self.active = False
            return 0, self.active_response()
        self.behavior = revoke
        with self.assertRaises(fixture.insights.SmartRefreshError) as caught:
            self.visible()
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        self.assertEqual(self.collector._smart_visible_observations, {})
        self.assertEqual(self.collector.smart_cached('sda'), before)

    def test_final_proof_preparation_revocation_does_not_publish_new_cache(self):
        class RevokingObservations(dict):
            def items(inner):
                self.active = False
                return super().items()
        self.collector._smart_visible_observations = RevokingObservations()
        self.behavior = (0, self.active_response())
        with self.assertRaises(fixture.insights.SmartRefreshError):
            self.visible()
        self.assertEqual(self.collector._smart_measurements, {})
        self.assertEqual(self.collector._smart_attempts, {})

    def test_nonblocking_shared_batch_lock(self):
        self.collector._smart_refresh_lock.acquire()
        try:
            with self.assertRaises(fixture.insights.SmartRefreshError) as caught:
                self.visible()
        finally:
            self.collector._smart_refresh_lock.release()
        self.assertEqual(caught.exception.reason, 'busy')
        self.assertEqual(self.calls, [])

    def test_absolute_deadline_does_not_create_capacity_proof(self):
        def slow(argv, timeout):
            self.assertLessEqual(timeout, 3)
            self.now += 31
            return 0, self.active_response()
        self.behavior = slow
        receipt = self.visible()
        self.assertEqual(receipt['results'][0]['reason'], 'timeout')
        self.assertEqual(self.collector.capacity_visible_eligibility(receipt)('sda'), 'power_state_unverified')
        self.assertEqual(self.collector._smart_measurements, {})

    def test_final_metadata_deadline_does_not_publish_uncommitted_sample_date(self):
        self.refresh()
        original = self.collector.smart_cached('sda')
        class ExpiringObservations(dict):
            def items(inner):
                self.now += 31
                return super().items()
        self.collector._smart_visible_observations = ExpiringObservations()
        self.behavior = (0, self.active_response())
        with mock.patch.object(fixture.insights.time, 'time', return_value=original['smart_sampled_at'] + 5):
            receipt = self.visible()
        self.assertEqual(receipt['results'][0]['reason'], 'timeout')
        value = self.collector.smart_cached('sda')
        self.assertIsNone(value['smart_temperature_c'])
        self.assertEqual(value['smart_sampled_at'], original['smart_sampled_at'])


if __name__ == '__main__':
    unittest.main()

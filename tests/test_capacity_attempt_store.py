import hashlib
import json
import unittest
from unittest import mock

import test_capacity_refresh_core as core

Store = core.service._CapacityAttemptStore


def logical_snapshot(store):
    return core.service._capacity_attempt_decode(store.snapshot())


def unpack_presence(snapshot, text, names):
    # The production decoder has already checked the closed wire grammar.
    if type(text) is not dict or not set(text) <= set(names):
        raise AssertionError('decoded fields exceed the fixed layout')
    return dict(text)


def unpack(snapshot, events):
    """Contract decoder, not an implementation mirror of business predicates."""
    result = []
    for stage, phase, status, reason, pairs, observation_error in events:
        names = [snapshot['observation_fields'][index] for index in snapshot['observation_layouts'][stage]]
        result.append({'stage': snapshot['stage_values'][stage],
                       'phase': snapshot['worker_phase_order'][phase] if phase is not None else None,
                       'status': snapshot['status_values'][status],
                       'reason': snapshot['reason_values'][reason],
                       'observations': unpack_presence(snapshot, pairs, names),
                       'observation_error': observation_error})
    return result


class CapacityAttemptStoreTests(unittest.TestCase):
    def begin(self, store, **kwargs):
        return store.begin(3, 5, 'web_owner_refresh', 'visible_refresh', sampled_mono=100.25, sampled_at=1700000000.5, **kwargs)

    def test_all_fixed_phases_are_represented_and_absent_mean_not_run(self):
        store = Store(); key = self.begin(store)
        store.step(key, 'initial_key', 11, observations={'actual_key_digest': '1' * 64})
        data = logical_snapshot(store)
        self.assertEqual(data['missing_stage_status'], 'not_run')
        self.assertIn('post_fd_release', data['row_stage_order'])
        self.assertEqual(len(data['attempts'][0]['rows']['11']['phases']), 1)
        self.assertEqual(unpack(data, data['attempts'][0]['rows']['11']['phases'])[0]['stage'], 'initial_key')

    def test_four_slots_retain_first_last_active_latest_and_are_not_shared(self):
        store = Store(); first = self.begin(store)
        store.step(first, 'initial_key', 11, status='failed', reason='identity_changed')
        store.finish(first, 'failed')
        for unused in range(20):
            key = self.begin(store); store.finish(key, 'complete')
        last = self.begin(store); store.step(last, 'commit', 11, status='failed', reason='epoch_changed'); store.finish(last, 'failed')
        complete = self.begin(store); store.finish(complete, 'complete')
        active = self.begin(store)
        data = logical_snapshot(store)
        self.assertEqual({item['id'] for item in data['attempts']}, {first, last, complete, active})
        self.assertEqual(data['first_rejected_since_lifecycle'], first)
        self.assertEqual(data['last_rejected'], last)
        self.assertGreater(data['evicted_attempts'], 0)
        data['attempts'][0]['status'] = 'CANARY'
        self.assertNotIn('CANARY', json.dumps(logical_snapshot(store)))

    def test_repeated_worker_subphase_cannot_hide_first_cancel(self):
        store = Store(); key = self.begin(store)
        store.step(key, 'worker_inventory', 17, status='failed', reason='inventory_changed', observations={'phase': 'inventory_opened'})
        store.step(key, 'worker_inventory', 17, observations={'phase': 'inventory_before'})
        store.step(key, 'worker_inventory', 17, observations={'phase': 'inventory_opened'})
        data = logical_snapshot(store); value = data['attempts'][0]
        self.assertEqual(value['first_rejection']['phase'], 'inventory_opened')
        self.assertEqual(value['first_rejection']['status'], 'failed')
        self.assertEqual(value['phase_replacements'], 1)
        self.assertEqual(len(unpack(data, value['rows']['17']['phases'])), 2)

    def test_epoch_change_is_observed_without_rebinding_original_epoch(self):
        store = Store(); key = self.begin(store)
        store.finish(key, 'complete', epoch=4, mount_epoch=5)
        value = logical_snapshot(store)['attempts'][0]
        self.assertEqual((value['status'], value['epoch'], value['mount_epoch']), ('stale', '3', '5'))
        self.assertEqual(value['first_rejection']['stage'], 'epoch')

    def test_row_limit_and_identity_conflict_do_not_splice_old_evidence(self):
        store = Store(); key = self.begin(store)
        for mount_id in range(1, 130):
            store.row(key, mount_id, {'device_name': 'sda', 'key_digest': '1' * 64})
        store.row(key, 1, {'device_name': 'sdb', 'key_digest': '2' * 64})
        value = logical_snapshot(store)['attempts'][0]
        self.assertEqual(len(value['rows']), 128)
        self.assertEqual(value['rows_dropped'], 1)
        snapshot = logical_snapshot(store); pairs = value['rows']['1']['metadata']
        decoded = unpack_presence(snapshot, pairs, snapshot['metadata_fields'])
        self.assertEqual(decoded, {'device_name': 'sda', 'key_digest': '1' * 64})

    def test_privacy_canaries_are_not_copied_or_stringified(self):
        store = Store(); key = self.begin(store)
        canary = '/Users/admin/PRIVATE-token-CANARY-192.0.2.3'
        store.row(key, 1, {'device': canary, 'path': canary, 'key_digest': canary})
        store.step(key, 'worker_measure', 1, reason=canary, observations={'raw': canary, 'code': canary, 'request_bytes': canary})
        store.history(key, 'candidate', 'failed', observations={'token': canary, 'removed_count': canary})
        text = json.dumps(logical_snapshot(store))
        self.assertNotIn('CANARY', text)
        self.assertEqual(logical_snapshot(store)['observation_status'], 'observer_error')

    def test_nan_boolean_in_integer_and_negative_clock_are_not_evidence(self):
        store = Store(); key = self.begin(store)
        store.step(key, 'parent_scm', 1, observations={'fd_count': True, 'sampled_at': float('nan'), 'sampled_mono': -1})
        data = logical_snapshot(store)
        self.assertEqual(unpack(data, data['attempts'][0]['rows']['1']['phases'])[0]['observations'], {})

    def test_large_numeric_identity_is_lossless_decimal_string(self):
        store = Store(); key = store.begin(2**63, 2**63+1, 'web_owner_refresh', 'visible_refresh')
        store.step(key, 'parent_watch', 1, observations={'expected_capacity_epoch': 2**63+2})
        data = logical_snapshot(store)
        self.assertEqual(data['attempts'][0]['epoch'], str(2**63))
        self.assertEqual(unpack(data, data['attempts'][0]['rows']['1']['phases'])[0]['observations']['expected_capacity_epoch'], str(2**63+2))

    def test_empty_prune_does_not_override_real_historical_removal(self):
        store = Store(); first = self.begin(store)
        store.history(first, 'prune', 'passed', observations={'removed_count': 0})
        self.assertIsNone(logical_snapshot(store)['first_historical_removal'])
        store.history(first, 'prune', 'passed', reason='historical_pruned', observations={'removed_count': 2})
        for unused in range(20):
            key = self.begin(store); store.history(key, 'prune', 'passed', observations={'removed_count': 0}); store.finish(key, 'complete')
        data = logical_snapshot(store)
        self.assertEqual(data['first_historical_removal']['observations']['removed_count'], 2)
        self.assertEqual(data['first_historical_removal']['attempt_id'], first)
        self.assertLessEqual(len(data['attempts']), 4)

    def test_history_stages_are_separate_and_guard_fields_fixed(self):
        store = Store(); key = self.begin(store)
        for stage in store.HISTORY_STAGES:
            fields = {'guard_passed': True, 'write_completed': False, 'publisher_called': False,
                      'serialized': True, 'code': 200}
            store.history(key, stage, 'passed', observations={name: value for name, value in fields.items()
                                                            if name in store._ALLOWED[stage]})
        data = logical_snapshot(store)
        self.assertEqual({item['stage'] for item in unpack(data, data['attempts'][0]['history'])}, set(store.HISTORY_STAGES))
        self.assertEqual(data['observation_errors'], 0)

    def test_reset_drops_old_evidence_but_never_reuses_attempt_id(self):
        store = Store(); old = self.begin(store); store.step(old, 'commit', status='failed')
        store.reset(); key = self.begin(store); store.step(old, 'commit', status='failed')
        data = logical_snapshot(store)
        self.assertGreater(int(key), int(old))
        self.assertIsNone(data['first_rejected_since_lifecycle'])
        self.assertEqual([value['id'] for value in data['attempts']], [key])

    def test_snapshot_has_no_os_clock_or_callback_reads(self):
        store = Store(); self.begin(store)
        with mock.patch('builtins.open', side_effect=AssertionError('OS')), mock.patch('time.time', side_effect=AssertionError('clock')), mock.patch('time.monotonic', side_effect=AssertionError('clock')):
            self.assertEqual(len(logical_snapshot(store)['attempts']), 1)

    def test_each_row_retains_precise_first_cancel_after_generic_final_result(self):
        store = Store(); key = self.begin(store)
        for mount_id in (1, 2):
            store.step(key, 'worker_inventory', mount_id, status='failed', reason='key_mismatch',
                       observations={'phase': 'inventory_opened', 'inventory_complete': True, 'matched': False})
            store.step(key, 'worker_request', mount_id, status='failed', reason='worker_identity_changed')
            store.step(key, 'commit', mount_id, status='failed', reason='identity_changed', observations={'result_status': 'identity_changed'})
        data = logical_snapshot(store)
        for row in data['attempts'][0]['rows'].values():
            first = unpack(data, [row['first_rejection']])[0]
            self.assertEqual((first['stage'], first['phase'], first['reason']), ('worker_inventory', 'inventory_opened', 'key_mismatch'))
            self.assertEqual(row['final_disposition']['result_status'], 'identity_changed')

    def test_precise_history_phase_survives_coarse_web_result(self):
        store = Store(); key = self.begin(store)
        store.history(key, 'finalize', 'failed', 'cache_changed', {'phase': 'cache_match'})
        store.history(key, 'finalize', 'failed', 'publication_rejected', {'publisher_called': True, 'code': 403})
        data = logical_snapshot(store); value = data['attempts'][0]
        self.assertEqual(len(unpack(data, value['history'])), 2)
        self.assertEqual(value['first_history_rejection']['reason'], 'cache_changed')
        self.assertEqual(value['first_history_rejection']['phase'], 'cache_match')

    def test_actual_web_and_worker_fields_allow_none_without_fabricating_pass(self):
        store = Store(); key = self.begin(store)
        store.row(key, 9999999999, {'device_name': 'sd' + 'z' * 253, 'device': '9999999:9999999'})
        store.step(key, 'parent_watch', 9999999999, observations={'watch_reason': None, 'watch_mask': None, 'actual_mount_epoch': None})
        store.step(key, 'worker_fd_before', 9999999999, observations={'expected_device': '8:1', 'actual_device': '8:1', 'before_digest': 'f'*64, 'fd_checked': True, 'phase': 'fd_before'})
        store.history(key, 'json_after', 'passed', observations={'code': 200, 'serialized': True, 'guard_passed': True})
        self.assertEqual(logical_snapshot(store)['observation_errors'], 0)

    def test_history_rejection_is_pinned_while_benign_rows_do_not_pin_attempts(self):
        store = Store(); first = self.begin(store)
        for reason in ('unsupported_filesystem', 'power_state_unverified', 'standby'):
            store.step(first, 'initial_key', 1, 'failed', reason)
        self.assertIsNone(logical_snapshot(store)['first_rejected_id'])
        store.history(first, 'json_after', 'failed', 'authorization_denied', {'guard_passed': False})
        for unused in range(25):
            key = self.begin(store); store.finish(key, 'complete')
        data = logical_snapshot(store)
        self.assertEqual(data['first_rejected_id'], first)
        original = next(item for item in data['attempts'] if item['id'] == first)
        self.assertEqual(original['first_rejection']['stage'], 'json_after')
        self.assertEqual(original['first_history_rejection']['reason'], 'authorization_denied')

    def test_observational_id_exhaustion_is_bounded_without_wrap_or_reuse(self):
        store = Store(); store._next = store._MAX_ID - 1
        last = self.begin(store)
        self.assertEqual(last, str(store._MAX_ID))
        self.assertIsNone(self.begin(store))
        data = logical_snapshot(store)
        self.assertEqual([item['id'] for item in data['attempts']], [last])
        self.assertTrue(data['sequence_exhausted'])
        self.assertEqual(data['observation_status'], 'observer_error')
        store._lifecycle = store._MAX_ID
        store.reset()
        self.assertIsNone(self.begin(store))
        self.assertEqual(logical_snapshot(store)['attempts'], [])

    def test_fixed_subphase_cardinality_cannot_expand_each_row_or_history(self):
        store = Store(); key = self.begin(store)
        for phase in store.WORKER_PHASES:
            store.step(key, 'parent_watch', 1, observations={'phase': phase})
            store.history(key, 'json_after', 'passed', observations={'phase': phase})
        data = logical_snapshot(store); value = data['attempts'][0]
        self.assertEqual(len(value['rows']['1']['phases']), 1)
        self.assertEqual(len(value['history']), 1)
        self.assertEqual(data['observation_status'], 'observer_error')

    def test_four_maximal_attempts_fit_existing_full_diagnostic_envelope(self):
        import test_capacity_diagnostic_bounds as bounds
        value = bounds.maximum_full_fixture()
        maximum = maximum_fixture()
        self.assertEqual(maximum['observation_errors'], 0)
        self.assertEqual(len(maximum['attempts']), 4)
        self.assertTrue(all(len(item['rows']) == 128 for item in maximum['attempts']))
        self.assertIsNotNone(maximum['first_historical_removal'])
        value['attempt_diagnostics'] = maximum
        envelope = bounds._production_envelope()(value)
        self.assertEqual(envelope['status'], 'complete')
        self.assertTrue(envelope['evidence_returned'])
        self.assertEqual(len(envelope['attempt_diagnostics']['attempts']), 4)
        self.assertLess(len(json.dumps(envelope, ensure_ascii=True, allow_nan=False).encode()), 4 * 1024 * 1024)

    def test_four_unique_u64_scalar_attempts_fit_full_envelope_without_dedupe_assumption(self):
        import test_capacity_diagnostic_bounds as bounds
        value = bounds.maximum_full_fixture()
        maximum = maximum_fixture(unique_counters=True)
        self.assertEqual(maximum['observation_errors'], 0)
        self.assertEqual(len(maximum['attempts']), 4)
        self.assertTrue(all(len(item['rows']) == 128 for item in maximum['attempts']))
        self.assertIsNotNone(maximum['first_historical_removal'])
        value['attempt_diagnostics'] = maximum
        envelope = bounds._production_envelope()(value)
        self.assertEqual(envelope['status'], 'complete')
        self.assertTrue(envelope['evidence_returned'])
        self.assertEqual(len(envelope['attempt_diagnostics']['attempts']), 4)
        self.assertLess(envelope['evidence_encoded_bytes'], 4 * 1024 * 1024 - 16384)
        self.assertLess(len(json.dumps(envelope, ensure_ascii=True, allow_nan=False).encode()), 4 * 1024 * 1024)

    def test_every_stage_and_summary_has_a_closed_producer_field_set(self):
        store = Store()
        self.assertEqual(set(store._ALLOWED), set(store.ROW_STAGES + store.BATCH_STAGES + store.HISTORY_STAGES))
        key = self.begin(store)
        store.history(key, 'write', 'passed', observations={'response_bytes': 3000, 'write_completed': True})
        store.finish(key, 'complete', observations={'inventory_count': 128, 'code': 200})
        data = logical_snapshot(store); value = data['attempts'][0]
        self.assertEqual(unpack(data, value['history'])[0]['observations'], {'write_completed': True})
        self.assertEqual(value['summary'], {'code': 200})
        self.assertEqual(data['observation_status'], 'observer_error')

    def test_presence_layout_decoding_is_lossless_and_rejects_ambiguous_masks(self):
        import copy
        store = Store(); key = self.begin(store)
        observed = {'watch_present': False, 'expected_capacity_epoch': 2**64 - 1,
                    'watch_mask': None}
        store.step(key, 'parent_watch', 1, observations=observed)
        wire = store.snapshot()
        logical = core.service._capacity_attempt_decode(wire)
        event = logical['attempts'][0]['rows']['1']['phases'][0]
        self.assertEqual(event[4], dict(observed, expected_capacity_epoch=str(2**64 - 1)))
        self.assertNotIn('actual_capacity_epoch', event[4])
        # A mask outside the fixed field layout, missing token or surplus token
        # is ambiguous evidence and must reject the entire item.
        for malformed in ('zzz:0', '1:', '0:0'):
            changed = copy.deepcopy(wire)
            changed['attempts'][0]['rows']['1']['phases'][0][3] = malformed
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                core.service._capacity_attempt_decode(changed)


def maximum_fixture(*, unique_counters=False):
    store = Store()
    store._next = store._MAX_ID - 4
    store._lifecycle = store._MAX_ID
    boolean = store.BOOLS
    counter = store.COUNTS
    scalar_sequence = 0
    def observations(names, salt):
        nonlocal scalar_sequence
        result = {}
        for name in names:
            if name in ('expected_device', 'actual_device'):
                result[name] = '9999999:9999999'
            elif name in boolean:
                result[name] = False
            elif name in counter:
                scalar_sequence += 1
                result[name] = (1 << 64) - 1 - (scalar_sequence if unique_counters else 0)
            elif name in store.DIGESTS:
                result[name] = hashlib.sha256((salt + name).encode()).hexdigest()
            elif name in store.ENUMS:
                result[name] = max(store.ENUMS[name], key=len)
            elif name in ('sampled_mono', 'sampled_at'):
                result[name] = 999999999999999.9
        return result
    for index in range(4):
        key = store.begin(2**64-1, 2**64-1, 'web_owner_capacity', 'explicit_refresh', sampled_mono=1e15, sampled_at=1e15)
        for row_index in range(1, 129):
            mount_id = 9999999000 + row_index
            identity_index = index * 128 + row_index - 1
            suffix = ''.join(chr(97 + identity_index // divisor % 26) for divisor in (676, 26, 1))
            store.row(key, mount_id, {'device_name': 'sd' + 'z' * 250 + suffix,
                'device': f'9999999999:{9999999999 - identity_index}',
                'major_minor': f'9999999998:{9999999999 - identity_index}',
                'key_digest': hashlib.sha256(f'row-key-{index}-{mount_id}'.encode()).hexdigest(),
                'identity_digest': hashlib.sha256(f'row-identity-{index}-{mount_id}'.encode()).hexdigest()})
            for stage in store.ROW_STAGES:
                names = store._ALLOWED.get(stage, frozenset()) | store.COMMON
                for phase in (None, *store.PHASES_BY_STAGE.get(stage, ())):
                    # Each accepted event gets independent maximum-width
                    # scalars/hashes, not a shared phase-dictionary shortcut.
                    item = observations(names, f'{index}-{mount_id}-{stage}-{phase}')
                    item.pop('phase', None)
                    if phase is not None:
                        item['phase'] = phase
                    store.step(key, stage, mount_id, status='failed' if stage == 'initial_key' else 'passed',
                               reason='worker_protocol_invalid', observations=item)
        for stage in store.BATCH_STAGES:
            item = observations(store._ALLOWED[stage], str(index))
            item.pop('phase', None)
            store.step(key, stage, observations=item)
        for stage in store.HISTORY_STAGES:
            item = observations(store._ALLOWED[stage], str(index))
            item.pop('phase', None)
            store.history(key, stage, 'failed' if stage == 'finalize' else 'passed', 'publication_error', observations=item)
            for phase in store.PHASES_BY_STAGE.get(stage, ()):
                phase_item = observations(store._ALLOWED[stage], f'{index}-{stage}-{phase}')
                phase_item['phase'] = phase
                store.history(key, stage, 'failed', 'publication_error', observations=phase_item)
        if index == 0:
            # Real removed counts are bounded by 128. Retain the independent
            # first-removal latch alongside the widest latest prune event.
            fields = store._ALLOWED['prune']
            removal = observations(fields, str(index))
            removal.pop('phase', None)
            removal['removed_count'] = 128
            store.history(key, 'prune', 'failed', 'key_changed', removal)
            latest = observations(fields, str(index))
            latest.pop('phase', None)
            store.history(key, 'prune', 'passed', 'publication_error', latest)
        if index != 3:
            store.finish(key, 'partial', observations=observations(store.SUMMARY_FIELDS, 'summary'), sampled_mono=1e15, sampled_at=1e15)
    return store.snapshot()


if __name__ == '__main__':
    unittest.main(verbosity=2)

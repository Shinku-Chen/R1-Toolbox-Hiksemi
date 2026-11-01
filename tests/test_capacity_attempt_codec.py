"""Closed RAM-only codec: malformed evidence never becomes a passed stage."""
import copy
import hashlib
import json
import unittest
from unittest import mock

import test_capacity_refresh_core as core

service = core.service
Store = service._CapacityAttemptStore
encode = service._capacity_attempt_encode
decode = service._capacity_attempt_decode


def logical_snapshot(store):
    """Use the real Store conversion, retaining its pre-codec logical value."""
    with mock.patch.object(service, '_capacity_attempt_encode', side_effect=copy.deepcopy):
        return store.snapshot()


def small_fixture():
    store = Store()
    key = store.begin(3, 5, 'web_owner_refresh', 'visible_refresh',
                      sampled_mono=100.25, sampled_at=1700000000.5)
    store.row(key, 11, {'device': '0008:00001', 'major_minor': '0008:00001',
                       'device_name': 'sda1', 'key_digest': '0' * 64,
                       'identity_digest': 'f' * 64})
    store.step(key, 'post_fd_release', 11, 'failed', 'identity_changed',
               {'inventory_count': 1, 'inventory_complete': False})
    store.step(key, 'post_fd_release', 11, observations={'inventory_count': 2,
               'inventory_complete': True, 'phase': None})
    store.step(key, 'worker_fd_before', 11, observations={'phase': 'fd_before',
               'expected_device': '0000008:0000001', 'actual_device': '8:1',
               'fd_checked': False, 'before_digest': 'a' * 64})
    store.step(key, 'parent_watch', 11, observations={'watch_present': False,
               'watch_reason': None, 'expected_capacity_epoch': (1 << 64) - 1})
    store.step(key, 'commit', 11, observations={'result_status': 'refreshed'})
    store.history(key, 'finalize', 'failed', 'cache_changed',
                  {'phase': 'cache_match', 'history_batch': 1})
    store.history(key, 'finalize', 'failed', 'publication_rejected', {'code': 403})
    store.history(key, 'prune', 'passed', 'historical_pruned', {'removed_count': 1})
    store.finish(key, 'partial', observations={'code': 'identity_changed'},
                 sampled_mono=101.5, sampled_at=1700000001.75)
    return logical_snapshot(store)


def strong_fixture(*, wide_counts=False):
    """All fixed slots and independent first cancels; no favorable dedupe.

    Counts reach u64 in the real Store. wide_counts widens only typed event
    payloads to the separate codec stress bound; headers/raw latches stay u64.
    """
    store, sequence = Store(), 0
    store._next, store._lifecycle = store._MAX_ID - 4, store._MAX_ID

    def observations(stage, salt, phase=None):
        nonlocal sequence
        values = {}
        for name in sorted(store._ALLOWED[stage]):
            if name == 'phase':
                values[name] = phase
            elif name in ('expected_device', 'actual_device'):
                sequence += 1
                values[name] = f'9999999:{9999999 - sequence}'
            elif name in store.BOOLS:
                values[name] = False
            elif name in store.COUNTS:
                sequence += 1
                values[name] = (1 << 64) - 1 - sequence
            elif name in store.DIGESTS:
                values[name] = hashlib.sha256((salt + '/' + name).encode()).hexdigest()
            elif name in store.ENUMS:
                values[name] = max(store.ENUMS[name], key=lambda item: (len(item), item))
        return values

    for batch in range(4):
        key = store.begin((1 << 64) - 1 - batch, (1 << 64) - 5 - batch,
                          'web_owner_capacity', 'explicit_refresh', sampled_mono=1e15, sampled_at=1e15)
        for index in range(128):
            ordinal = batch * 128 + index
            mount_id = 9999999000 + ordinal
            label = 'sd' + 'z' * 250 + ''.join(chr(97 + ordinal // scale % 26) for scale in (676, 26, 1))
            store.row(key, mount_id, {'device_name': label,
                'device': f'9999999999:{9999999999 - ordinal}',
                'major_minor': f'9999999998:{9999999999 - ordinal}',
                'key_digest': hashlib.sha256(f'key/{ordinal}'.encode()).hexdigest(),
                'identity_digest': hashlib.sha256(f'identity/{ordinal}'.encode()).hexdigest()})
            store.step(key, 'post_fd_release', mount_id, 'failed', 'publication_result_unobserved',
                       observations('post_fd_release', f'old/{ordinal}'))
            for stage in store.ROW_STAGES:
                for phase in (None, *store.PHASES_BY_STAGE.get(stage, ())):
                    store.step(key, stage, mount_id, 'failed', 'publication_result_unobserved',
                               observations(stage, f'{ordinal}/{stage}/{phase}', phase))
        store.history(key, 'finalize', 'failed', 'publication_result_unobserved',
                      observations('finalize', f'history-old/{batch}', 'before_publish'))
        removal = observations('prune', f'removal-old/{batch}')
        removal['removed_count'] = 128
        store.history(key, 'prune', 'failed', 'publication_result_unobserved', removal)
        for stage in store.BATCH_STAGES:
            store.step(key, stage, status='failed', reason='publication_result_unobserved',
                       observations=observations(stage, f'batch/{batch}/{stage}'))
        for stage in store.HISTORY_STAGES:
            for phase in (None, *store.PHASES_BY_STAGE.get(stage, ())):
                store.history(key, stage, 'failed', 'publication_result_unobserved',
                              observations(stage, f'history/{batch}/{stage}/{phase}', phase))
        if batch != 3:
            store.finish(key, 'partial', observations={'code': 'publication_result_unobserved'},
                         sampled_mono=1e15, sampled_at=1e15)
    result = logical_snapshot(store)
    assert result['observation_errors'] == 0
    if wide_counts:
        for attempt in result['attempts']:
            groups = [attempt['phases'], attempt['history']]
            for row in attempt['rows'].values():
                groups += [row['phases'], [row['first_rejection']]]
            for events in groups:
                for event in events:
                    for name, value in event[4].items():
                        if name in store.COUNTS and name not in ('expected_device', 'actual_device') and value is not None:
                            sequence += 1
                            event[4][name] = str(10**32 - 1 - sequence)
    return result


def first_row(value):
    return value['attempts'][0]['rows']['11']


def replace_token(wire, stage, field, token):
    layout = wire['slot_layouts']['row']
    event = next(item for item in first_row(wire)['phases']
                 if wire['stage_values'][layout[item[0]][0]] == stage)
    names = [wire['observation_fields'][index]
             for index in wire['observation_layouts'][layout[event[0]][0]]]
    mask, content = event[3].split(':')
    mask = int(mask, 36)
    selected = [name for index, name in enumerate(names) if mask & (1 << index)]
    values = content.split(',') if content else []
    values[selected.index(field)] = token
    event[3] = service._CapacityAttemptCodec.b36(mask) + ':' + ','.join(values)


class CapacityAttemptCodecTests(unittest.TestCase):
    def setUp(self):
        self.logical = small_fixture()
        self.wire = encode(self.logical)

    def rejected(self, mutate, *, logical=False):
        value = copy.deepcopy(self.logical if logical else self.wire)
        mutate(value)
        with self.assertRaisesRegex(ValueError, '^capacity_attempt_codec_invalid$'):
            (encode if logical else decode)(value)

    def test_actual_store_snapshot_roundtrips_all_original_evidence(self):
        self.assertEqual(decode(self.wire), self.logical)
        self.assertEqual(service._capacity_attempt_decode_json(json.dumps(self.wire)), self.logical)
        self.assertEqual(service._capacity_attempt_decode_json(json.dumps(self.wire).encode()), self.logical)
        self.assertEqual(self.wire['encoding'], 'fixed_fields_v2_b36_b64digest')
        self.assertEqual({key: len(value) for key, value in self.wire['slot_layouts'].items()},
                         {'row': 22, 'batch': 9, 'history': 13})

    def test_missing_null_false_and_device_leading_zeros_are_distinct(self):
        decoded = decode(self.wire)
        self.assertEqual(first_row(decoded)['metadata']['device'], '0008:00001')
        events = first_row(decoded)['phases']
        watch = next(item[4] for item in events if decoded['stage_values'][item[0]] == 'parent_watch')
        self.assertIs(watch['watch_present'], False)
        self.assertIsNone(watch['watch_reason'])
        self.assertNotIn('watch_mask', watch)
        fd = next(item[4] for item in events if decoded['stage_values'][item[0]] == 'worker_fd_before')
        self.assertEqual(fd['expected_device'], '0000008:0000001')

    def test_js_boundary_u64_and_32digit_token_roundtrip_without_store_expansion(self):
        for number in ((1 << 53) - 1, 1 << 53, (1 << 64) - 1, 10**32 - 1):
            with self.subTest(number=number):
                value = copy.deepcopy(self.logical)
                event = next(item for item in first_row(value)['phases']
                             if value['stage_values'][item[0]] == 'parent_watch')
                event[4]['expected_capacity_epoch'] = number if number < (1 << 53) else str(number)
                self.assertEqual(decode(encode(value)), value)
        store = Store(); key = store.begin(1, 1, 'unknown', 'unknown')
        store.step(key, 'parent_watch', 11, observations={'expected_capacity_epoch': 1 << 64})
        data = decode(store.snapshot())
        self.assertEqual(data['observation_status'], 'observer_error')
        self.assertNotIn('expected_capacity_epoch', first_row(data)['phases'][0][4])

    def test_independent_first_cancel_and_raw_history_latches_survive_replacements(self):
        decoded = decode(self.wire)
        row = first_row(decoded)
        self.assertEqual(row['first_rejection'][4]['inventory_count'], 1)
        current = next(item for item in row['phases'] if decoded['stage_values'][item[0]] == 'post_fd_release')
        self.assertEqual(current[4]['inventory_count'], 2)
        self.assertEqual(decoded['attempts'][0]['first_history_rejection']['reason'], 'cache_changed')
        self.assertEqual(decoded['first_historical_removal']['observations']['removed_count'], 1)

    def test_strong_all_slots_independent_maximum_roundtrip(self):
        for wide in (False, True):
            with self.subTest(codec_stress_32digits=wide):
                logical = strong_fixture(wide_counts=wide)
                wire = encode(logical)
                self.assertEqual(decode(wire), logical)
                self.assertEqual(len(wire['digest_pool']) // 43, 16400)
                self.assertEqual(len(wire['string_pool']), 3584)
                self.assertEqual([len(item['rows']) for item in wire['attempts']], [128] * 4)
                self.assertTrue(all(len(row['phases']) == 22 and row['first_rejection'] is not None
                                    for item in wire['attempts'] for row in item['rows'].values()))
                self.assertTrue(all(len(item['phases']) == 9 and len(item['history']) == 13 for item in wire['attempts']))

    def test_closed_root_and_compiled_tables_reject_mutations(self):
        changes = [lambda w: w.__setitem__('raw_path', '/CANARY'),
            lambda w: w.__setitem__('schema', True), lambda w: w.__setitem__('schema', 1.0),
            lambda w: w.__setitem__('encoding', 'unknown'),
            lambda w: w['observation_fields'].__setitem__(0, 'token'),
            lambda w: w['slot_layouts']['row'][0].__setitem__(0, True),
            lambda w: w['phase_columns'].append('raw'),
            lambda w: w['observation_layouts'][0].append(0),
            lambda w: w.__setitem__('max_rows', 129)]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index): self.rejected(mutate)

    def test_raw_headers_have_exact_types_ids_clocks_and_u64_epochs(self):
        changes = [lambda w: w.__setitem__('lifecycle', '01'),
            lambda w: w.__setitem__('lifecycle', str(1 << 63)),
            lambda w: w.__setitem__('observation_errors', True),
            lambda w: w.__setitem__('sequence_exhausted', 0),
            lambda w: w.__setitem__('first_rejected_id', None),
            lambda w: w['attempts'][0].__setitem__('id', '0'),
            lambda w: w['attempts'][0].__setitem__('epoch', str(1 << 64)),
            lambda w: w['attempts'][0].__setitem__('source', '/CANARY'),
            lambda w: w['attempts'][0].__setitem__('started_at', float('nan')),
            lambda w: w['attempts'][0].__setitem__('finished_mono', float('inf')),
            lambda w: w['attempts'][0].__setitem__('raw', '/CANARY'),
            lambda w: w['attempts'][0]['summary'].__setitem__('token', '/CANARY'),
            lambda w: w['attempts'][0].pop('summary'),
            lambda w: w.__setitem__('latest_completed', '999')]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index): self.rejected(mutate)

    def test_raw_latches_cannot_escape_stage_field_and_numeric_grammar(self):
        changes = [lambda w: w['attempts'][0]['first_rejection'].__setitem__('token', '/CANARY'),
            lambda w: w['attempts'][0]['first_rejection']['observations'].__setitem__('raw', '/CANARY'),
            lambda w: w['attempts'][0]['first_rejection'].__setitem__('status', 'passed'),
            lambda w: w['attempts'][0]['first_rejection'].__setitem__('mount_id', True),
            lambda w: w['attempts'][0]['first_rejection']['observations'].__setitem__('inventory_count', str(1 << 64)),
            lambda w: w['attempts'][0]['first_history_rejection'].__setitem__('phase', 'inventory_before'),
            lambda w: w['first_historical_removal'].__setitem__('event', 'write'),
            lambda w: w['first_historical_removal']['observations'].__setitem__('removed_count', 0),
            lambda w: w['first_historical_removal'].__setitem__('epoch', '/CANARY')]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index): self.rejected(mutate)

    def test_slot_flags_reason_duplicates_and_row_bindings_are_strict(self):
        changes = [lambda w: first_row(w)['phases'][0].__setitem__(0, -1),
            lambda w: first_row(w)['phases'][0].__setitem__(0, True),
            lambda w: first_row(w)['phases'][0].__setitem__(1, True),
            lambda w: first_row(w)['phases'][0].__setitem__(1, 6),
            lambda w: first_row(w)['phases'][0].__setitem__(2, -1),
            lambda w: first_row(w)['phases'][0].__setitem__(2, True),
            lambda w: first_row(w)['phases'][0].pop(),
            lambda w: first_row(w)['phases'].append(copy.deepcopy(first_row(w)['phases'][0])),
            lambda w: first_row(w).__setitem__('mount_id', True),
            lambda w: first_row(w).__setitem__('mount_id', 12),
            lambda w: first_row(w)['first_rejection'].__setitem__(1, 0),
            lambda w: first_row(w)['final_disposition'].__setitem__('raw', '/CANARY'),
            lambda w: w['attempts'].append(copy.deepcopy(w['attempts'][0]))]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index): self.rejected(mutate)

    def test_masks_and_typed_tokens_are_canonical(self):
        changes = [lambda w: first_row(w)['phases'][0].__setitem__(3, 'zzz:0'),
            lambda w: first_row(w)['phases'][0].__setitem__(3, '00:'),
            lambda w: first_row(w)['phases'][0].__setitem__(3, '1:'),
            lambda w: replace_token(w, 'parent_watch', 'expected_capacity_epoch', 'z' * 22),
            lambda w: replace_token(w, 'parent_watch', 'expected_capacity_epoch', '01'),
            lambda w: replace_token(w, 'parent_watch', 'expected_capacity_epoch', service._CapacityAttemptCodec.b36(10**32)),
            lambda w: replace_token(w, 'parent_watch', 'watch_present', '2'),
            lambda w: replace_token(w, 'parent_watch', 'watch_reason', 'zz'),
            lambda w: replace_token(w, 'worker_fd_before', 'phase', '0'),
            lambda w: replace_token(w, 'worker_fd_before', 'before_digest', '0'),
            lambda w: replace_token(w, 'worker_fd_before', 'before_digest', 'zzz')]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index): self.rejected(mutate)

    def test_digest_chunks_validate_alphabet_width_and_unused_pad_bits(self):
        changes = [lambda w: w.__setitem__('digest_pool', w['digest_pool'] + 'a'),
            lambda w: w.__setitem__('digest_pool', '!' + w['digest_pool'][1:]),
            lambda w: w.__setitem__('digest_pool', w['digest_pool'][:42] + 'B' + w['digest_pool'][43:]),
            lambda w: w.__setitem__('digest_pool', 'A' * (43 * 16401))]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index): self.rejected(mutate)

    def test_string_pool_grammar_and_field_specific_width_are_closed(self):
        changes = [lambda w: w['string_pool'].__setitem__(0, '/Users/admin/CANARY'),
            lambda w: w['string_pool'].__setitem__(0, 'sd中文'),
            lambda w: w['string_pool'].__setitem__(0, 'sd' + 'z' * 254),
            lambda w: w.__setitem__('string_pool', ['sda'] * 3585)]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index): self.rejected(mutate)
        value = copy.deepcopy(self.wire)
        value['string_pool'].append('00000008:00000001')
        replace_token(value, 'worker_fd_before', 'expected_device', service._CapacityAttemptCodec.b36(len(value['string_pool']) - 1).zfill(3))
        with self.assertRaises(ValueError): decode(value)

    def test_last_legal_pool_references_decode_without_coercion(self):
        value = copy.deepcopy(self.wire)
        value['string_pool'] += ['0000008:0000001'] * (3584 - len(value['string_pool']))
        value['digest_pool'] += 'A' * (43 * (16400 - len(value['digest_pool']) // 43))
        replace_token(value, 'worker_fd_before', 'expected_device', service._CapacityAttemptCodec.b36(3583).zfill(3))
        replace_token(value, 'worker_fd_before', 'before_digest', service._CapacityAttemptCodec.b36(16399).zfill(3))
        expected = copy.deepcopy(self.logical)
        event = next(item for item in first_row(expected)['phases']
                     if expected['stage_values'][item[0]] == 'worker_fd_before')
        event[4]['before_digest'] = '0' * 64
        self.assertEqual(decode(value), expected)

    def test_http_code_variants_boolean_and_unknown_enum_reject(self):
        for code in (True, 99, 600, 'CANARY'):
            with self.subTest(code=code):
                self.rejected(lambda w: w['attempts'][0]['summary'].__setitem__('code', code))
        value = copy.deepcopy(self.wire)
        event = next(item for item in value['attempts'][0]['history']
                     if value['stage_values'][value['slot_layouts']['history'][item[0]][0]] == 'finalize'
                     and value['slot_layouts']['history'][item[0]][1] is None)
        mask, unused = event[3].split(':')
        for token in ('0', 'i2r', 'izz', 'ezz', 'i01'):
            bad = copy.deepcopy(value)
            target = bad['attempts'][0]['history'][value['attempts'][0]['history'].index(event)]
            target[3] = mask + ':' + token
            with self.subTest(token=token), self.assertRaises(ValueError): decode(bad)

    def test_metadata_null_unknown_fields_and_boolean_counts_reject_on_encode(self):
        for mutate in (lambda w: first_row(w)['metadata'].__setitem__('device', None),
                       lambda w: first_row(w)['metadata'].__setitem__('raw', '/CANARY'),
                       lambda w: first_row(w)['phases'][0][4].__setitem__('inventory_count', True),
                       lambda w: first_row(w)['phases'][0][4].__setitem__('inventory_count', '0001')):
            self.rejected(mutate, logical=True)

    def test_json_reader_rejects_duplicates_nonfinite_and_oversize_before_decode(self):
        encoded = json.dumps(self.wire)
        for raw in ('{"schema":1,"schema":1}', encoded.replace('"schema": 1', '"schema": NaN', 1),
                    b'\xff', ' ' * (5 * 1024 * 1024 + 1)):
            with self.subTest(prefix=repr(raw[:25])), self.assertRaises(ValueError):
                service._capacity_attempt_decode_json(raw)
        with mock.patch.object(service.json, 'loads', side_effect=AssertionError('oversize parsed')):
            with self.assertRaises(ValueError): service._capacity_attempt_decode_json(b' ' * (5 * 1024 * 1024 + 1))

    def test_cardinality_and_raw_header_bool_counter_boundaries_reject(self):
        changes = [lambda w: w['attempts'].__imul__(5),
            lambda w: w['attempts'][0]['rows'].update({str(i): copy.deepcopy(first_row(w)) for i in range(100, 229)}),
            lambda w: first_row(w)['phases'].__imul__(23),
            lambda w: w.__setitem__('rows_dropped', 1 << 31),
            lambda w: w['attempts'][0].__setitem__('phase_replacements', True),
            lambda w: w['attempts'][0].__setitem__('id', '01'),
            lambda w: w['attempts'][0].__setitem__('lifecycle', '2'),
            lambda w: first_row(w).__setitem__('final_disposition', {'status': 'passed', 'reason': None, 'result_status': True}),
            lambda w: w['first_historical_removal'].__setitem__('lifecycle', '2')]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index): self.rejected(mutate)

    def test_null_metadata_ref_and_missing_pool_entries_reject(self):
        self.rejected(lambda w: first_row(w).__setitem__('metadata', '1:-'))
        self.rejected(lambda w: w.__setitem__('digest_pool', ''))
        self.rejected(lambda w: w.__setitem__('string_pool', []))

    def test_non_json_subclasses_are_rejected_without_copy_callbacks(self):
        class CallbackText(str):
            def __deepcopy__(self, memo):
                raise AssertionError('untrusted callback executed')
        self.rejected(lambda w: w.__setitem__('encoding', CallbackText(w['encoding'])))
        self.rejected(lambda w: w['first_historical_removal'].__setitem__('event', CallbackText('prune')))
        self.rejected(lambda w: w['attempts'][0]['first_history_rejection'].__setitem__(
            'observation_status', CallbackText('observer_error')))

    def test_snapshot_and_codec_never_read_os_or_clock_and_results_are_detached(self):
        store = Store(); key = store.begin(1, 2, 'unknown', 'unknown')
        store.step(key, 'commit', 11, 'failed', 'identity_changed')
        with mock.patch('builtins.open', side_effect=AssertionError('OS read')), \
                mock.patch.object(service.time, 'monotonic', side_effect=AssertionError('clock read')), \
                mock.patch.object(service.time, 'time', side_effect=AssertionError('clock read')), \
                mock.patch.object(service, '_capacity_inventory', side_effect=AssertionError('inventory')):
            wire = store.snapshot()
            decoded = decode(wire)
        decoded['attempts'][0]['status'] = 'CANARY'
        wire['attempts'][0]['status'] = 'CANARY'
        self.assertNotIn('CANARY', json.dumps(store.snapshot()))

    def test_encoding_fault_preserves_original_store_records(self):
        store = Store(); key = store.begin(1, 2, 'unknown', 'unknown')
        store.step(key, 'commit', 11, 'failed', 'identity_changed')
        before = logical_snapshot(store)
        with mock.patch.object(service, '_capacity_attempt_encode', side_effect=ValueError('controlled')):
            with self.assertRaisesRegex(ValueError, '^controlled$'): store.snapshot()
        self.assertEqual(logical_snapshot(store), before)

    def test_observer_error_flag_cannot_be_reported_as_complete(self):
        self.rejected(lambda w: first_row(w)['phases'][0].__setitem__(1, first_row(w)['phases'][0][1] | 1))
        value = copy.deepcopy(self.wire)
        first_row(value)['phases'][0][1] |= 1
        value.update(observation_status='observer_error', observation_errors=1)
        result = decode(value)
        self.assertTrue(first_row(result)['phases'][0][5])
        self.assertEqual(result['observation_status'], 'observer_error')


if __name__ == '__main__':
    unittest.main()

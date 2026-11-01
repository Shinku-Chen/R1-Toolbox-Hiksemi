"""Upper-cardinality, upper-width capacity provenance without OS sampling.

All mount-line/options/source text is consumed by the real inventory observer
and becomes fixed 64-character digests.  The deliberately wide inode/ctime
strings represent a conservative 32-character diagnostic contract, wider than
the real kernel's 64-bit identities.  Device components use Linux NAME_MAX
(255 ASCII bytes); mount IDs use the production parser's ten-digit bound.
"""

import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import test_capacity_refresh_core as core

service = core.service
IDENTITY_DECIMAL_WIDTH = 32
INVENTORY_LIMIT = 128
RING_LIMIT = 32
PROJECTION_SCHEMA_BOUND_BYTES = 600 * 1024
SUGGESTED_RESPONSE_LIMIT = 4 * 1024 * 1024
WIDE_COUNTER = int('9' * 32)
WIDE_FLOAT = -1.7976931348623157e308
FNM_SAFE_COUNTER = 2 ** 53 - 1
FNM_FINITE_TIME = 2.2250738585072014e-308


def _production_envelope():
    runtime = Path(__file__).resolve().parents[1] / 'runtime'
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    spec = importlib.util.spec_from_file_location('capacity_bounds_web_under_test', runtime / 'web_config.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._capacity_diagnostic_envelope


def _wide_stat():
    return {key: '9' * IDENTITY_DECIMAL_WIDTH for key in ('dev', 'ino', 'ctime_ns')}


def _read_observation():
    return {'reason': None, 'bytes': 64, 'complete': True}


def _device_observation():
    value = {'complete': True, 'stage': 'complete', 'reason': None,
             'target_digest': 'f' * 64, 'physical_digest': 'f' * 64,
             'diskseq': '9' * 20, 'parent_major_minor': '9999999:9999999',
             'reads': {name: _read_observation() for name in ('device', 'parent_device', 'diskseq')}}
    # The numeric diskseq plus optional newline cannot occupy all 64 bytes;
    # unlike the two dev attributes, its production grammar forbids padding.
    value['reads']['diskseq']['bytes'] = 21
    for name in ('link_before', 'target_before', 'parent_before',
                 'target_after', 'parent_after', 'link_after'):
        value[name] = _wide_stat()
    return value


def maximum_inventory(offset):
    """Exercise real parsing/digest publication; only OS read/device seams vary."""
    # 128 distinct 10-digit mount IDs, 7+7-digit device numbers and 255-byte
    # kernel basenames.  Long raw paths/options are intentionally not output.
    text = ''.join(
        f'{9999999000 + offset + index} 9999999999 9999999:{9999999 - index} / '
        f'/drives/{"x" * 255}{index} rw,{"x" * 255} shared:9999999999 '
        f'- btrfs /dev/{"x" * 255}{index} rw,{"x" * 255}\n'
        for index in range(INVENTORY_LIMIT))
    if len(text.encode('utf8')) >= 262144:
        raise AssertionError('maximum fixture exceeded the real bounded read')
    evidence = {}
    def device(*_):
        service._CAPACITY_TRACE.device.update(_device_observation())
        stat = (int('9' * IDENTITY_DECIMAL_WIDTH),) * 3
        return ('/sys/' + 'x' * 255, '/sys/' + 'y' * 255,
                stat, stat, stat, '9999999:9999999', '9' * 20, 'sd' + 'z' * 253)
    previous = getattr(service._CAPACITY_TRACE, 'inventory', None)
    service._CAPACITY_TRACE.inventory = evidence
    try:
        with mock.patch.object(service, '_capacity_read', return_value=text), \
                mock.patch.object(service, '_capacity_device', side_effect=device):
            entries, complete = service._capacity_inventory('unused-proc', 'unused-sys')
    finally:
        service._CAPACITY_TRACE.inventory = previous
    if not complete or len(entries) != INVENTORY_LIMIT:
        raise AssertionError('real inventory observer rejected maximum fixture')
    evidence.update(seq=WIDE_COUNTER, sampled_mono=-1.7976931348623157e308,
                    sampled_at=-1.7976931348623157e308,
                    capacity_epoch=WIDE_COUNTER, mount_epoch=WIDE_COUNTER)
    return entries, evidence


def maximum_capacity_fixture(*, disjoint=False):
    """Retain four inventories and summaries; both diff cardinalities matter.

    A 256-ID disjoint union has no per-component pairs.  A 128-ID common
    inventory has every changed bit (all false is the widest Boolean spelling)
    and is larger in JSON despite having fewer changed rows.
    """
    sampler = service.StatusSampler('unused-proc', 'unused-sys')
    before_entries, before_inventory = maximum_inventory(0)
    after_entries, after_inventory = maximum_inventory(128 if disjoint else 0)
    ledger = sampler._capacity_ledger
    sampler._capacity_epoch = sampler._capacity_mount_epoch = WIDE_COUNTER
    sampler._capacity_context.source = 'explicit_refresh'
    sampler._capacity_context.stage = 'final_inventory'
    sampler._capacity_negative = {index: None for index in range(INVENTORY_LIMIT)}
    sampler._capacity_pending = INVENTORY_LIMIT
    sampler._capacity_last_watch = {'namespace_read': True, 'polled': True,
        'reason': 'mount_event', 'mask': 65535, 'stage': None,
        'namespace': 'mnt:[' + '9' * 25 + ']',
        'namespace_current': 'mnt:[' + '9' * 25 + ']'}
    def records(entries):
        return {row['mount_id']: {'key': row['_key'], 'sampled_at': -1.7976931348623157e308,
            'values': {'total_bytes': 99999999999999999999,
                       'used_bytes': 99999999999999999999, 'usage_percent': 99.99999999999999}}
                for row in entries}
    before, after = records(before_entries), records(after_entries)
    for item in after.values():
        item['values']['usage_percent'] = 88.88888888888888
    sampler._capacity_cache = before
    sampler._capacity_context.inventory = before_inventory
    ledger.reconciled_inventory = before_inventory
    with mock.patch.object(service.time, 'time', return_value=-1.7976931348623157e308), \
            mock.patch.object(service.time, 'monotonic', return_value=-1.7976931348623157e308):
        # Leave room for the real event producer's increments, keeping every
        # published counter inside the exact 32-character decimal contract.
        ledger.seq = ledger.generation = ledger.dropped = WIDE_COUNTER - 1000
        sampler._capacity_record('commit_success', commit=True)
        sampler._capacity_context.inventory = after_inventory
        ledger.latest = after_inventory
        for _ in range(RING_LIMIT):
            sampler._capacity_cache = after
            sampler._capacity_record('pending_rejected_authorization_denied',
                                     before=ledger.records(before))
    value = sampler.capacity_cache_diagnostics()
    return value


def maximum_monitor_fixture():
    """Public Monitor schema: 16 ring + latest + first-loss before/after."""
    event = {'row_count': 128, 'measured_count': 128, 'date_digest': 'f' * 64,
        'dates_count': 128, 'date_min': WIDE_FLOAT, 'date_max': WIDE_FLOAT, 'truncated': False,
        'status': 'observed', 'sequence': WIDE_COUNTER, 'operation_id': WIDE_COUNTER,
        'sampled_monotonic': WIDE_FLOAT, 'sampled_at': WIDE_FLOAT,
        'result': 'projection_error', 'capacity_epoch': WIDE_COUNTER, 'mount_epoch': WIDE_COUNTER,
        'capacity_generation': WIDE_COUNTER, 'sampler_projection_seq': WIDE_COUNTER,
        'atomic_with_capacity': False, 'atomic_scope': 'sampler_projection_rows'}
    return {'last_sample': {'result': 'sample_error', 'sampled_mono': WIDE_FLOAT,
                           'sampled_at': WIDE_FLOAT},
        'last_projection': {'result': 'heartbeat_missing', 'sampled_mono': WIDE_FLOAT,
                            'heartbeat_mono': WIDE_FLOAT},
        'updated_mono': WIDE_FLOAT, 'interval_seconds': WIDE_FLOAT, 'basic_available': False,
        'projection': {'status': 'observed', 'events': [copy.deepcopy(event) for _ in range(16)],
            'ring_limit': 16, 'latest': copy.deepcopy(event), 'dropped_events': WIDE_COUNTER,
            'first_loss': {'before': copy.deepcopy(event), 'after': copy.deepcopy(event)}},
        'atomic_with_capacity': False, 'read_order': ['monitor', 'capacity']}


def maximum_fnm_fixture():
    """Deliberate upper envelope: all 22 retained events carry every stage field.

    Real latest safe/basic/mounts/summary/overview events cannot each have 128
    device rows and all six children. Including them here dominates the actual
    producer schema rather than relying on favorable stage combinations.
    """
    source = {'status': 'observed', 'sequence': FNM_SAFE_COUNTER, 'operation_id': FNM_SAFE_COUNTER,
        'row_count': 128, 'measured_count': 128, 'sampled_monotonic': FNM_FINITE_TIME,
        'sampled_at': FNM_FINITE_TIME, 'capacity_epoch': FNM_SAFE_COUNTER, 'mount_epoch': FNM_SAFE_COUNTER,
        'result': 'projection_error', 'date_digest': 'f' * 64, 'atomic_with_capacity': False}
    stages = ('safe', 'basic', 'mounts', 'disks', 'summary', 'overview')
    event = {'stage': 'overview', 'operation_id': FNM_SAFE_COUNTER, 'parent_operation_id': FNM_SAFE_COUNTER,
        'sampled_monotonic': FNM_FINITE_TIME, 'sampled_at': FNM_FINITE_TIME,
        'result': 'projection_error', 'atomic': False,
        'counts': {key: 65536 for key in ('matched', 'unknown', 'deduplicated', 'contributed',
                                          'out_of_scope', 'invalid_identity', 'capacity_unknown')},
        'children': {stage: {'sequence': FNM_SAFE_COUNTER, 'operation_id': FNM_SAFE_COUNTER,
            'result': 'projection_error', 'input_result': 'projection_error',
            'monitor_source': copy.deepcopy(source)} for stage in stages},
        'monitor_source': source, 'row_count': 128, 'measured_count': 128,
        'sample_date_count': 128, 'sampled_at_oldest': FNM_FINITE_TIME, 'sampled_at_newest': FNM_FINITE_TIME,
        'date_digest': 'f' * 64, 'summary_has_value': False,
        'device_count': 65536, 'mount_count': 65536, 'basic_row_count': 65536,
        'mount_cache': 'not_observed', 'mount_read': 'not_observed',
        'devices': [{'device': 'sd' + 'z' * 30, 'matched': 65536, 'unknown': 65536,
                     'deduplicated': 65536, 'contributed': 65536, 'capacity_unknown': 65536}
                    for _ in range(128)],
        'input_result': 'projection_error', 'sequence': FNM_SAFE_COUNTER}
    return {'schema_version': 1, 'source': 'fnm_normal_consumption', 'instance': 'f' * 24,
        'sequence': FNM_SAFE_COUNTER, 'atomic': False, 'sequence_saturated': False,
        'ring_limit': 16, 'dropped_count': FNM_SAFE_COUNTER,
        'latest': {stage: copy.deepcopy(event) for stage in stages},
        'ring': [copy.deepcopy(event) for _ in range(16)]}


def maximum_full_fixture():
    value = maximum_capacity_fixture()
    value['monitor'] = maximum_monitor_fixture()
    value['fnm'] = maximum_fnm_fixture()
    return value


class CapacityDiagnosticBoundsTests(unittest.TestCase):
    def test_full_cardinality_fixture_keeps_four_inventories_and_all_changed_ids(self):
        value = maximum_capacity_fixture(disjoint=True)
        inventories = [value['latest_inventory'], value['last_commit']['inventory'],
                       value['first_invalidation']['before']['inventory'],
                       value['first_invalidation']['after']['inventory']]
        self.assertEqual([len(item['entries']) for item in inventories], [128] * 4)
        self.assertEqual(len(value['events']), RING_LIMIT)
        self.assertEqual(len(value['first_invalidation']['component_changes']['mounts']), 256)
        self.assertTrue(all(len(item['changed_mount_ids']) == 128 for item in value['events']))
        self.assertEqual(len(value['first_invalidation']['before']['records']), 128)
        self.assertEqual(len(value['first_invalidation']['after']['records']), 128)
        self.assertEqual(len(value['last_commit']['records']), 128)

    def test_maximum_width_capacity_payload_is_bounded_without_dropping_evidence(self):
        value = maximum_capacity_fixture()
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False).encode('utf8')
        # This is the complete ledger payload, not an estimate from a 128-row
        # latest-only inventory. The actual projection is tested separately.
        self.assertLess(len(encoded), 2 * 1024 * 1024)
        self.assertNotIn(b'/drives/', encoded)
        self.assertNotIn(b'/sys/', encoded)
        self.assertNotIn(b'/dev/', encoded)

    def test_paired_changes_are_larger_than_disjoint_union_and_keep_all_bits(self):
        paired, disjoint = maximum_capacity_fixture(), maximum_capacity_fixture(disjoint=True)
        changes = paired['first_invalidation']['component_changes']['mounts']
        self.assertEqual(len(changes), 128)
        self.assertTrue(all(len(row['changed']) == 24 for row in changes))
        self.assertTrue(all(value is False for row in changes for value in row['changed'].values()))
        self.assertGreater(len(json.dumps(paired)), len(json.dumps(disjoint)))

    def test_component_summaries_are_fixed_hashes_not_raw_option_lengths(self):
        _, observation = maximum_inventory(0)
        for row in observation['entries']:
            self.assertEqual(len(row['device']), 255)
            self.assertEqual(len(row['major_minor']), 15)
            self.assertTrue(all(item is None or len(item) == 64
                                for item in row['components'].values()))
            self.assertEqual(set(row['device_identity']['reads']), {'device', 'parent_device', 'diskseq'})
            for key in ('link_before', 'target_before', 'parent_before',
                        'target_after', 'parent_after', 'link_after'):
                self.assertEqual([len(item) for item in row['device_identity'][key].values()], [32] * 3)

    def test_full_monitor_fnm_upper_envelope_returns_complete_without_trimming_first_loss(self):
        envelope = _production_envelope()
        value = maximum_full_fixture()
        result = envelope(value)
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['evidence_returned'])
        self.assertFalse(result['truncated'])
        self.assertEqual(len(result['first_invalidation']['before']['inventory']['entries']), 128)
        self.assertEqual(len(result['first_invalidation']['after']['inventory']['entries']), 128)
        self.assertEqual(len(result['fnm']['ring']), 16)
        self.assertEqual(len(result['fnm']['latest']), 6)
        self.assertEqual(len(result['monitor']['projection']['events']), 16)
        self.assertEqual(result['fnm']['sequence'], FNM_SAFE_COUNTER)
        self.assertEqual(result['monitor']['projection']['latest']['capacity_epoch'], '9' * 32)
        self.assertLess(result['evidence_encoded_bytes'], result['response_limit_bytes'] - 16384)
        self.assertLess(len(json.dumps(result).encode('utf8')), SUGGESTED_RESPONSE_LIMIT)

    def test_final_projection_schema_measured_size_replaces_old_reserve_guess(self):
        envelope = _production_envelope()
        projection = {'monitor': maximum_monitor_fixture(), 'fnm': maximum_fnm_fixture()}
        # 512KiB was an interim allowance, not a claimed schema proof. The
        # final intentionally over-complete producer shapes now replace it.
        measured = envelope(projection)
        self.assertEqual(measured['status'], 'complete')
        self.assertGreater(measured['evidence_encoded_bytes'], 0)
        self.assertLessEqual(measured['evidence_encoded_bytes'], PROJECTION_SCHEMA_BOUND_BYTES)
        full = envelope(maximum_full_fixture())
        self.assertGreater(full['evidence_encoded_bytes'], measured['evidence_encoded_bytes'])
        self.assertGreater(full['response_limit_bytes'] - full['evidence_encoded_bytes'], 16384)

    def test_integer_precision_and_out_of_range_encoding_preserve_original_ram(self):
        envelope = _production_envelope()
        positive, negative = 2 ** 53 + 1, -(2 ** 53 + 1)
        value = {'first_invalidation': {'sequence': positive},
                 'nested': [{'counter': negative}], 'limit_case': 10 ** 32 - 1}
        result = envelope(value)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['first_invalidation']['sequence'], str(positive))
        self.assertEqual(result['nested'][0]['counter'], str(negative))
        self.assertEqual(result['limit_case'], '9' * 32)
        self.assertEqual(value['first_invalidation']['sequence'], positive)
        self.assertEqual(value['nested'][0]['counter'], negative)
        value['limit_case'] = 10 ** 32
        rejected = envelope(value)
        self.assertEqual(rejected['status'], 'encoding_unavailable')
        self.assertFalse(rejected['evidence_returned'])
        self.assertFalse(rejected['truncated'])
        self.assertTrue(rejected['first_invalidation_retained_in_memory'])
        self.assertEqual(value['limit_case'], 10 ** 32)


if __name__ == '__main__':
    unittest.main()

"""Complete evidence bounds, including every old inventory and new attempt."""
import hashlib
import json
import re
import unittest

import test_capacity_attempt_codec as codec
import test_capacity_diagnostic_bounds as bounds


def strengthened_base_fixture():
    """Conservative existing-schema superset, with no favorable digest reuse."""
    sequence = 0
    def visit(value, path=''):
        nonlocal sequence
        if type(value) is int and abs(value) > 2**53 - 1:
            # The existing envelope's public numeric contract uses decimal
            # strings here; include those quote bytes in the bound fixture.
            return str(value)
        if type(value) is dict:
            return {key: visit(item, path + '/' + key) for key, item in value.items()}
        if type(value) is list:
            return [visit(item, path + '/' + str(index)) for index, item in enumerate(value)]
        if type(value) is str:
            if re.fullmatch('[0-9a-f]{64}', value):
                return hashlib.sha256(path.encode()).hexdigest()
            if re.fullmatch('[0-9]{20,32}', value):
                sequence += 1
                return str(10**32 - 1 - sequence)
            if len(value) == 255 and value.startswith('sd'):
                sequence += 1
                return 'sd' + 'z' * 247 + ''.join(chr(97 + sequence // divisor % 26)
                    for divisor in (26**5, 26**4, 26**3, 26**2, 26, 1))
        return value
    base = bounds.maximum_full_fixture()
    # The real sampler now supplies an empty attempt item too. Replace it with
    # the four maximal attempts exactly once, rather than counting two items.
    base.pop('attempt_diagnostics', None)
    return visit(base)


def compact_json(value):
    # Match the production envelope encoder, including its default separators.
    return json.dumps(value, ensure_ascii=True, allow_nan=False).encode()


class CapacityAttemptBoundsTests(unittest.TestCase):
    def check_complete(self, *, wide_counts):
        logical = codec.strong_fixture(wide_counts=wide_counts)
        wire = codec.service._capacity_attempt_encode(logical)
        self.assertEqual(codec.service._capacity_attempt_decode(wire), logical)
        self.assertEqual(len(wire['digest_pool']) // 43, 16400)
        self.assertEqual(len(wire['string_pool']), 3584)
        value = strengthened_base_fixture()
        value['attempt_diagnostics'] = wire
        before = compact_json(value)
        envelope = bounds._production_envelope()(value)
        self.assertEqual(envelope['status'], 'complete')
        self.assertTrue(envelope['evidence_returned'])
        self.assertFalse(envelope['truncated'])
        self.assertEqual(envelope['evidence_encoded_bytes'], len(before))
        self.assertLess(len(before), 4 * 1024 * 1024 - 16384)
        self.assertLess(len(compact_json(envelope)), 4 * 1024 * 1024)
        self.assertEqual(compact_json(value), before)
        self.assertEqual(envelope['attempt_diagnostics'], wire)
        self.assertEqual(len(wire['attempts']), 4)
        for attempt in wire['attempts']:
            self.assertEqual(len(attempt['rows']), 128)
            self.assertEqual(len(attempt['phases']), 9)
            self.assertEqual(len(attempt['history']), 13)
            for row in attempt['rows'].values():
                self.assertEqual(len(row['phases']), 22)
                self.assertIsNotNone(row['first_rejection'])
        return logical, wire, envelope

    def test_full_independent_u64_store_evidence_keeps_all_stages_under_existing_limit(self):
        self.check_complete(wide_counts=False)

    def test_32digit_codec_pressure_superset_keeps_complete_envelope_and_does_not_expand_store(self):
        self.check_complete(wide_counts=True)
        store = codec.Store()
        key = store.begin(1, 1, 'unknown', 'explicit_refresh')
        store.step(key, 'parent_watch', 1, observations={'expected_capacity_epoch': 2**64})
        logical = codec.service._capacity_attempt_decode(store.snapshot())
        self.assertEqual(logical['observation_status'], 'observer_error')
        self.assertNotIn('expected_capacity_epoch', logical['attempts'][0]['rows']['1']['phases'][0][4])

    def test_event_cardinality_and_pool_bound_are_schema_derived(self):
        store = codec.Store
        row_slots = sum(1 + len(store.PHASES_BY_STAGE.get(stage, ())) for stage in store.ROW_STAGES)
        history_slots = sum(1 + len(store.PHASES_BY_STAGE.get(stage, ())) for stage in store.HISTORY_STAGES)
        row_digests = sum(sum(key in store.DIGESTS for key in store._ALLOWED[stage]) *
            (1 + len(store.PHASES_BY_STAGE.get(stage, ()))) for stage in store.ROW_STAGES)
        history_digests = sum(sum(key in store.DIGESTS for key in store._ALLOWED[stage]) *
            (1 + len(store.PHASES_BY_STAGE.get(stage, ()))) for stage in store.HISTORY_STAGES)
        self.assertEqual((row_slots, len(store.BATCH_STAGES), history_slots), (22, 9, 13))
        self.assertEqual(4 * 128 * (row_slots + 1) + 4 * 9 + 4 * history_slots, 11864)
        self.assertEqual(4 * 128 * (row_digests + 3 + 2) + 4 * history_digests, 16400)
        self.assertEqual(4 * 128 * (3 + 4), 3584)


if __name__ == '__main__':
    unittest.main()

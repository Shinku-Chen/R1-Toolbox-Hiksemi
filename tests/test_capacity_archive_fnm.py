"""The owner overview discloses a dated, unbound past capacity observation.

Only existing fake collector metadata is used. The real adapter must carry the
archive from the same normal disk read, without deriving current disk identity
or performing any capacity, SMART, or filesystem collection.
"""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    'capacity_archive_fnm_history_fixture',
    Path(__file__).with_name('test_capacity_history_fnm.py'))
history_fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history_fixture)
compat = history_fixture.compat


def archive(**changes):
    """Fixed producer contract; no current disk, mount, or identity accompanies it."""
    value = {
        'schema_version': 1, 'status': 'historical',
        'source': 'past_successful_observation',
        'reason': 'current_binding_unverified', 'observation_id': '7',
        'total': 400, 'used': 100, 'available': 300, 'percent': 25.0,
        'sampled_at': 1000.5, 'sampled_at_latest': 1001.5,
        'coverage': {'scope': 'measured_filesystem_subset',
                     'filesystem_count': 4, 'disk_count': 2, 'complete': False},
        'retention': 'memory_until_restart',
    }
    value.update(changes)
    return value


class CapacityArchiveFnmTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the existing fixture setup, without inheriting its tests.
        history_fixture.CapacityHistoryFnmTests.setUp(self)
        self.archive = archive()
        self.rows.capacity_observation_archive = self.archive

    def owner(self):
        return self.panel.overview(include_identity=True)

    def test_owner_receives_fixed_archive_only_at_overview_top_level(self):
        result = self.owner()
        self.assertEqual(result['capacity_observation_archive'], self.archive)
        self.assertEqual(set(result['capacity_observation_archive']), {
            'schema_version', 'status', 'source', 'reason', 'observation_id',
            'total', 'used', 'available', 'percent', 'sampled_at',
            'sampled_at_latest', 'coverage', 'retention'})
        self.assertTrue(all('capacity_observation_archive' not in row
                            for row in result['disks_detail']))
        self.assertEqual(result['disks_detail'][0]['capacity_last_observation'], self.history)
        self.assertEqual(result['capacity_last_observation_summary'], self.summary)
        self.assertIsNone(result['disks_detail'][0]['total'])
        self.assertEqual((result['disk_size'], result['disk_used'], result['disk_total']),
                         (200, 100, 50))

    def test_nonowner_omits_archive_even_when_private_sideband_contains_it(self):
        result = self.panel.overview()
        self.assertNotIn('capacity_observation_archive', result)
        self.assertNotIn('past_successful_observation', json.dumps(result))
        self.assertNotIn('memory_until_restart', json.dumps(result))

    def test_nonowner_does_not_access_private_sideband(self):
        class ExplosiveRows(list):
            @property
            def capacity_observations(self):
                raise AssertionError('nonowner touched archive sideband')
            @property
            def capacity_observation_archive(self):
                raise AssertionError('nonowner touched independent archive')
        self.monitor.value['disks'] = ExplosiveRows(self.rows)
        result = self.panel.overview()
        self.assertNotIn('capacity_observation_archive', result)
        self.assertEqual(result['disks_detail'][1]['percent'], 50)

    def test_owner_legacy_missing_or_null_archive_is_explicitly_null(self):
        for kind in ('legacy', 'missing', 'null', 'no_monitor'):
            with self.subTest(kind=kind):
                self.monitor.value['disks'] = self.rows
                self.panel.monitor = self.monitor
                self.rows.capacity_observation_archive = self.archive
                if kind == 'legacy':
                    self.monitor.value['disks'] = list(self.rows)
                elif kind == 'missing':
                    del self.rows.capacity_observation_archive
                elif kind == 'null':
                    self.rows.capacity_observation_archive = None
                else:
                    self.panel.monitor = None
                self.assertIsNone(self.owner()['capacity_observation_archive'])

    def test_archive_survives_empty_current_inventory_and_empty_history(self):
        rows = history_fixture.CapacityRows([])
        rows.capacity_observations = {'disks': {}, 'summary': None}
        rows.capacity_observation_archive = self.archive
        self.monitor.value['disks'] = rows
        self.insights.value['disks']['items'] = []
        self.panel._mounts = lambda: {}
        result = self.owner()
        self.assertEqual(result['disks_detail'], [])
        self.assertEqual(result['capacity_observation_archive'], self.archive)
        self.assertIsNone(result['capacity_last_observation_summary'])
        self.assertIsNone(result['disk_size'])
        self.assertIsNone(result['disk_used'])
        self.assertIsNone(result['disk_total'])

    def test_archive_is_not_filtered_by_present_disk_or_partition_membership(self):
        self.rows.capacity_observations.update(disks={}, summary=None)
        for present in ('sdc', 'nvme2n1', 'sda'):
            with self.subTest(present=present):
                self.insights.value['disks']['items'] = [
                    {'id': present, 'size_bytes': 99999, 'health': {}}]
                self.panel._mounts = lambda: {present: ['/drives/replaced-partition']}
                result = self.owner()
                self.assertEqual(result['capacity_observation_archive'], self.archive)
                self.assertIsNone(result['capacity_last_observation_summary'])
                current = result['disks_detail'][0]
                self.assertIsNone(current['capacity_last_observation'])
                self.assertIsNone(current['total'])
                self.assertIsNone(current['used'])
                self.assertIsNone(current['percent'])
                self.assertEqual(current['capacity_status'], 'unknown')

    def test_archive_comes_from_same_disk_read_not_a_later_snapshot(self):
        original = self.monitor.snapshot
        def changing_snapshot():
            result = copy.deepcopy(original())
            if self.monitor.calls > 1:
                result['disks'].capacity_observation_archive = archive(
                    observation_id='99', sampled_at=1400, sampled_at_latest=1401)
            return result
        self.monitor.snapshot = changing_snapshot
        result = self.owner()
        self.assertGreater(self.monitor.calls, 1)
        self.assertEqual(result['capacity_observation_archive'], self.archive)

    def test_archive_adds_no_monitor_or_insight_reads(self):
        self.panel.overview()
        baseline = self.monitor.calls, self.insights.calls
        self.monitor.calls = self.insights.calls = 0
        self.owner()
        self.assertEqual((self.monitor.calls, self.insights.calls), baseline)

    def test_archive_attribute_is_consumed_once_on_the_normal_disk_read(self):
        reads = []
        class CountingRows(history_fixture.CapacityRows):
            @property
            def capacity_observation_archive(rows):
                # _safe legitimately deepcopies rows before consumption. Keep
                # the observer outside that copy so it counts the actual read.
                reads.append(1)
                if len(reads) != 1:
                    raise AssertionError('archive was reread')
                return self.archive
        rows = CountingRows(self.rows)
        rows.capacity_observations = self.rows.capacity_observations
        self.monitor.value['disks'] = rows
        self.assertEqual(self.owner()['capacity_observation_archive'], self.archive)
        self.assertEqual(len(reads), 1)

    def test_passive_get_never_renews_original_date_range(self):
        first = self.owner()['capacity_observation_archive']
        self.panel.wall_clock = lambda: 10 ** 9
        self.panel.clock = lambda: 10 ** 8
        second = self.owner()['capacity_observation_archive']
        self.assertEqual(first, second)
        self.assertEqual((second['sampled_at'], second['sampled_at_latest']), (1000.5, 1001.5))

    def test_returned_archive_and_deepcopy_do_not_alias_collector(self):
        first = self.owner()
        first['capacity_observation_archive']['coverage']['filesystem_count'] = 99
        first['capacity_observation_archive']['used'] = 99
        second = copy.deepcopy(self.owner())
        second['capacity_observation_archive']['coverage']['disk_count'] = 99
        self.assertEqual(self.archive, archive())
        self.assertEqual(self.owner()['capacity_observation_archive'], archive())

    def test_private_archive_metadata_is_not_serialized_on_disk_list(self):
        result = self.panel._disks(include_identity=True)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(copy.deepcopy(result)), 2)
        self.assertNotIn('past_successful_observation', json.dumps(result))
        self.assertNotIn('capacity_observation_archive', json.dumps(result))

    def test_archive_does_not_appear_in_owner_or_regular_smart_detail(self):
        for owner in (False, True):
            with self.subTest(owner=owner):
                result = self.panel.smart('sda', include_identity=owner)
                self.assertNotIn('capacity_observation_archive', result)
                self.assertNotIn('past_successful_observation', json.dumps(result))

    def test_only_allowlisted_numeric_and_fixed_metadata_are_public(self):
        self.archive.update(path='/private/archive-path-canary', serial='archive-serial-canary',
                            token='archive-token-canary', key=('private', 'tuple-canary'),
                            raw={'fd': 'archive-fd-canary'})
        self.archive['coverage']['namespace'] = 'archive-namespace-canary'
        result = self.owner()
        self.assertEqual(result['capacity_observation_archive'], archive())
        encoded = json.dumps(result)
        for sentinel in ('archive-path-canary', 'archive-serial-canary', 'archive-token-canary',
                         'tuple-canary', 'archive-fd-canary', 'archive-namespace-canary'):
            self.assertNotIn(sentinel, encoded)

    def test_malformed_archive_or_missing_required_field_is_null(self):
        invalid = [False, True, 0, [], 'archive', {'schema_version': 1}]
        for key in archive():
            value = archive()
            value.pop(key)
            invalid.append(value)
        for value in invalid:
            with self.subTest(value=value):
                self.rows.capacity_observation_archive = value
                result = self.owner()
                self.assertIsNone(result['capacity_observation_archive'])
                self.assertEqual(result['disks_detail'][1]['percent'], 50)

    def test_invalid_fixed_markers_and_observation_ids_are_rejected(self):
        cases = [('schema_version', True), ('schema_version', '1'), ('schema_version', 2),
                 ('status', 'current'), ('source', 'last_successful_observation'),
                 ('reason', 'last_successful_measurement'), ('retention', 'persistent'),
                 ('observation_id', 7), ('observation_id', '0'), ('observation_id', '-1'),
                 ('observation_id', '01'), ('observation_id', '\u0661'),
                 ('observation_id', '1/private'), ('observation_id', str(2 ** 63))]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.rows.capacity_observation_archive = archive(**{key: value})
                self.assertIsNone(self.owner()['capacity_observation_archive'])
        for value in ('1', str(2 ** 63 - 1)):
            with self.subTest(valid_id=value):
                self.rows.capacity_observation_archive = archive(observation_id=value)
                self.assertEqual(self.owner()['capacity_observation_archive']['observation_id'], value)

    def test_invalid_numeric_values_cannot_influence_current_capacity(self):
        cases = [('total', 0), ('total', True), ('total', float('inf')),
                 ('total', 10 ** 1000), ('used', -1), ('used', 401),
                 ('used', float('nan')), ('available', 301), ('available', False),
                 ('percent', 26), ('percent', float('nan')), ('percent', True),
                 ('sampled_at', 0), ('sampled_at', True), ('sampled_at', float('inf')),
                 ('sampled_at_latest', 999), ('sampled_at_latest', float('nan'))]
        for key, value in cases:
            with self.subTest(key=key, value=repr(value)[:80]):
                self.rows.capacity_observation_archive = archive(**{key: value})
                result = self.owner()
                self.assertIsNone(result['capacity_observation_archive'])
                self.assertEqual((result['disk_size'], result['disk_used'], result['disk_total']),
                                 (200, 100, 50))

    def test_invalid_coverage_is_rejected(self):
        cases = [('scope', 'whole_pool'), ('complete', True), ('complete', 0),
                 ('filesystem_count', 0), ('filesystem_count', 129),
                 ('filesystem_count', True), ('disk_count', 0), ('disk_count', 129),
                 ('disk_count', 5), ('disk_count', True)]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                broken = archive()
                broken['coverage'][key] = value
                self.rows.capacity_observation_archive = broken
                self.assertIsNone(self.owner()['capacity_observation_archive'])

    def test_malformed_per_disk_history_does_not_discard_independent_archive(self):
        class BrokenDisks(dict):
            def __deepcopy__(rows, memo):
                # Fail only the optional history decoder, after the normal
                # safe snapshot copy succeeds; no collector value is mutated.
                return rows
            def items(self):
                raise RuntimeError('per-disk-observer-canary')
        self.rows.capacity_observations.update(disks=BrokenDisks(), summary=None)
        result = self.owner()
        self.assertEqual(result['capacity_observation_archive'], self.archive)
        self.assertIsNone(result['capacity_last_observation_summary'])
        self.assertTrue(all(row['capacity_last_observation'] is None
                            for row in result['disks_detail']))
        self.assertNotIn('per-disk-observer-canary', json.dumps(result))

    def test_archive_observer_failure_preserves_current_and_existing_history(self):
        class BrokenArchive(dict):
            def get(self, *args):
                raise RuntimeError('archive-observer-canary')
        self.rows.capacity_observation_archive = BrokenArchive()
        result = self.owner()
        self.assertIsNone(result['capacity_observation_archive'])
        self.assertEqual(result['capacity_last_observation_summary'], self.summary)
        self.assertEqual(result['disks_detail'][0]['capacity_last_observation'], self.history)
        self.assertEqual(result['disks_detail'][1]['percent'], 50)
        self.assertNotIn('archive-observer-canary', json.dumps(result))

    def test_archive_attribute_failure_preserves_existing_history(self):
        class BrokenRows(history_fixture.CapacityRows):
            @property
            def capacity_observation_archive(rows):
                raise RuntimeError('archive-attribute-canary')
        rows = BrokenRows(self.rows)
        rows.capacity_observations = self.rows.capacity_observations
        self.monitor.value['disks'] = rows
        result = self.owner()
        self.assertIsNone(result['capacity_observation_archive'])
        self.assertEqual(result['capacity_last_observation_summary'], self.summary)
        self.assertEqual(result['disks_detail'][1]['percent'], 50)
        self.assertNotIn('archive-attribute-canary', json.dumps(result))

    def test_archive_projection_does_not_read_paths_or_collect_capacity(self):
        # Settings-file reads are unrelated to this projection; keep that
        # existing store lookup out of the capacity collection trap.
        self.panel.store.config = lambda: {'interval': 1}
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('file read')), \
                mock.patch.object(compat.os, 'statvfs', create=True,
                                  side_effect=AssertionError('statvfs')):
            result = self.owner()
        self.assertEqual(result['capacity_observation_archive'], self.archive)


if __name__ == '__main__':
    unittest.main()

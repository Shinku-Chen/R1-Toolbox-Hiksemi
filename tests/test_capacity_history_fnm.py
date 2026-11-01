"""Owner-only historical capacity travels beside the exact normal disk read.

These tests exercise the real adapter. Only collector snapshots and unrelated
overview sections are fixtures; no hardware or mounted filesystem is accessed.
"""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('capacity_history_fnm_fixture',
                                              ROOT / 'runtime/fnm_compat.py')
compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compat)


class CapacityRows(list):
    pass


def observation(*, total=100, used=25, stamp=1000.5, latest=1001.5, count=1, disks=1, ident='7'):
    return {
        'status': 'historical', 'source': 'last_successful_observation',
        'reason': 'mount_continuity_unverified', 'observation_id': ident,
        'total': total, 'used': used, 'available': total - used, 'percent': used / total * 100,
        'sampled_at': stamp, 'sampled_at_latest': latest,
        'coverage': {'scope': 'measured_filesystem_subset',
                     'filesystem_count': count, 'disk_count': disks, 'complete': False},
    }


class Snapshot:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return self.value


class CapacityHistoryFnmTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='r1-history-fnm-')
        self.addCleanup(self.directory.cleanup)
        self.history = observation()
        self.summary = observation(total=200, used=80, count=2, disks=2)
        self.rows = CapacityRows([
            {'mount': '/drives/a', 'total_bytes': None, 'used_bytes': None,
             'capacity_sampled_at': None, 'capacity_status': 'unknown'},
            {'mount': '/drives/b', 'total_bytes': 200, 'used_bytes': 100,
             'capacity_sampled_at': 1200.25, 'capacity_status': 'cached'},
        ])
        self.rows.capacity_observations = {
            'disks': {'sda': self.history, 'sdb': observation(used=55)},
            'summary': self.summary,
        }
        self.monitor = Snapshot({'disks': self.rows, 'uptime_seconds': 5})
        self.insights = Snapshot({
            'disks': {'items': [{'id': 'sda', 'size_bytes': 1000, 'health': {}},
                                {'id': 'sdb', 'size_bytes': 2000, 'health': {}}]},
            'device': {'storage': {'devices': []}},
        })
        self.panel = compat.FnmonitorPanel(
            None, self.insights, None, None, None, self.monitor,
            state_dir=self.directory.name, clock=lambda: 50, wall_clock=lambda: 1500,
            proc_root=self.directory.name, sys_root=self.directory.name,
            dev_root=self.directory.name, udev_root=self.directory.name)
        self.panel._mount_devices = {'/drives/a': '8:1', '/drives/b': '8:17'}
        self.panel._mounts = lambda: {'sda': ['/drives/a'], 'sdb': ['/drives/b']}
        self.panel._volume_labels = lambda: {}
        # Keep _disks, _basic, _disk_summary, _safe and overview real. Other
        # sections are unrelated cached collectors, not part of capacity proof.
        for method in ('_cpu', '_mem', '_fan', '_light', '_network_aggregate',
                       '_power', '_gpu', '_memory_modules', '_docker', '_modules',
                       '_ports', '_hardware', '_raid', '_apps', '_system'):
            setattr(self.panel, method, lambda: {})
        self.panel._sensors = lambda: {'fans': []}
        self.panel._net = lambda: []
        self.panel._disk_io = lambda: []
        self.panel._temperature = lambda disks: {}
        self.panel.config = lambda: {}

    def test_owner_receives_separate_dated_history_and_current_fields_are_unchanged(self):
        owner = self.panel.overview(include_identity=True)
        regular = self.panel.overview()
        self.assertEqual(owner['disks_detail'][0]['capacity_last_observation'], self.history)
        self.assertEqual(owner['capacity_last_observation_summary'], self.summary)
        self.assertIsNone(owner['disks_detail'][0]['total'])
        self.assertIsNone(owner['disks_detail'][0]['percent'])
        self.assertIsNone(owner['disks_detail'][0]['capacity_sampled_at'])
        self.assertEqual(owner['disks_detail'][0]['capacity_status'], 'unknown')
        self.assertEqual((owner['disk_size'], owner['disk_used'], owner['disk_total']), (200, 100, 50))
        self.assertEqual((regular['disk_size'], regular['disk_used'], regular['disk_total']), (200, 100, 50))
        for disk in owner['disks_detail']:
            baseline = next(row for row in regular['disks_detail'] if row['name'] == disk['name'])
            self.assertEqual({key: value for key, value in disk.items()
                              if key not in ('capacity_last_observation', 'serial_status', 'serial_sources')},
                             baseline)

    def test_nonowner_omits_all_history_even_when_sideband_is_present(self):
        result = self.panel.overview()
        self.assertNotIn('capacity_last_observation_summary', result)
        self.assertTrue(all('capacity_last_observation' not in row for row in result['disks_detail']))
        self.assertNotIn('last_successful_observation', json.dumps(result))

    def test_nonowner_never_accesses_optional_sideband(self):
        class ExplosiveRows(list):
            @property
            def capacity_observations(self):
                raise AssertionError('private sideband was accessed')
        self.monitor.value['disks'] = ExplosiveRows(self.rows)
        result = self.panel._disks()
        self.assertEqual(result[1]['percent'], 50)

    def test_owner_missing_sideband_is_explicitly_null(self):
        self.monitor.value['disks'] = list(self.rows)
        result = self.panel.overview(include_identity=True)
        self.assertIsNone(result['capacity_last_observation_summary'])
        self.assertTrue(all(row['capacity_last_observation'] is None for row in result['disks_detail']))

    def test_summary_comes_from_same_disk_read_not_later_monitor_snapshot(self):
        original = self.monitor.snapshot
        def changing_snapshot():
            value = copy.deepcopy(original())
            if self.monitor.calls > 1:
                value['disks'].capacity_observations['summary'] = observation(used=99, ident='99')
            return value
        self.monitor.snapshot = changing_snapshot
        owner = self.panel.overview(include_identity=True)
        self.assertGreater(self.monitor.calls, 1)
        self.assertEqual(owner['capacity_last_observation_summary'], self.summary)

    def test_optional_history_adds_no_monitor_or_insight_reads(self):
        self.panel.overview()
        calls = self.monitor.calls, self.insights.calls
        self.monitor.calls = self.insights.calls = 0
        self.panel.overview(include_identity=True)
        self.assertEqual((self.monitor.calls, self.insights.calls), calls)

    def test_summary_is_transported_without_reaggregation_or_date_rewrite(self):
        result = self.panel.overview(include_identity=True)
        summary = result['capacity_last_observation_summary']
        self.assertEqual(summary, self.summary)
        self.assertEqual(summary['used'], sum(
            row['capacity_last_observation']['used'] for row in result['disks_detail']))
        self.assertEqual(summary['sampled_at'], 1000.5)
        self.assertEqual(summary['sampled_at_latest'], 1001.5)
        self.assertFalse(summary['coverage']['complete'])

    def test_returned_objects_do_not_alias_collector_history(self):
        result = self.panel.overview(include_identity=True)
        result['disks_detail'][0]['capacity_last_observation']['coverage']['filesystem_count'] = 99
        result['capacity_last_observation_summary']['used'] = 99
        self.assertEqual(self.history['coverage']['filesystem_count'], 1)
        self.assertEqual(self.summary['used'], 80)

    def test_filtered_emmc_or_missing_disk_cannot_contribute_invisible_history(self):
        for missing in ('emmc', 'absent'):
            with self.subTest(missing=missing):
                self.rows.capacity_observations['disks']['mmcblk0'] = observation(used=30)
                self.rows.capacity_observations['summary'] = observation(
                    total=300, used=110, count=3, disks=3)
                if missing == 'emmc':
                    self.insights.value['disks']['items'].append(
                        {'id': 'mmcblk0', 'type': 'eMMC', 'size_bytes': 1000, 'health': {}})
                result = self.panel.overview(include_identity=True)
                self.assertEqual([row['name'] for row in result['disks_detail']], ['sda', 'sdb'])
                self.assertIsNone(result['capacity_last_observation_summary'])
                self.assertEqual(result['disks_detail'][0]['capacity_last_observation'], self.history)
                self.insights.value['disks']['items'] = self.insights.value['disks']['items'][:2]

    def test_valid_latest_batch_summary_excludes_retained_older_disk_history(self):
        old = observation(used=10, stamp=900, latest=901, ident='6')
        self.rows.capacity_observations['disks']['sda'] = old
        self.rows.capacity_observations['disks']['sdc'] = observation(
            total=300, used=150, stamp=1002, latest=1003)
        self.rows.capacity_observations['summary'] = observation(
            total=400, used=205, count=2, disks=2, stamp=1000.5, latest=1003)
        self.insights.value['disks']['items'].append({'id': 'sdc', 'health': {}})
        result = self.panel.overview(include_identity=True)
        self.assertEqual(result['disks_detail'][0]['capacity_last_observation'], old)
        self.assertEqual(result['capacity_last_observation_summary'],
                         self.rows.capacity_observations['summary'])
        self.assertEqual(result['capacity_last_observation_summary']['used'], 205)

    def test_summary_mismatch_is_hidden_without_reaggregating_or_changing_disk_history(self):
        cases = [observation(total=200, used=81, count=2, disks=2),
                 observation(total=200, used=80, count=3, disks=2),
                 observation(total=200, used=80, count=2, disks=1),
                 observation(total=200, used=80, count=2, disks=2, stamp=999),
                 observation(total=200, used=80, count=2, disks=2, latest=1002),
                 observation(total=200, used=80, count=2, disks=2, ident='9')]
        for broken in cases:
            with self.subTest(summary=broken):
                self.rows.capacity_observations['summary'] = broken
                result = self.panel.overview(include_identity=True)
                self.assertIsNone(result['capacity_last_observation_summary'])
                self.assertEqual(result['disks_detail'][0]['capacity_last_observation'], self.history)
                self.assertEqual(result['disks_detail'][1]['percent'], 50)

    def test_only_allowlisted_fields_are_projected(self):
        self.history.update({'path': '/private/canary', 'token': 'secret-canary',
                             'raw': {'SN': 'serial-canary'}})
        self.history['coverage']['root'] = '/private/coverage-canary'
        self.summary['private'] = 'summary-canary'
        value = self.panel.overview(include_identity=True)
        encoded = json.dumps(value)
        for sentinel in ('secret-canary', 'serial-canary', 'coverage-canary', 'summary-canary'):
            self.assertNotIn(sentinel, encoded)
        self.assertEqual(set(value['disks_detail'][0]['capacity_last_observation']), {
            'status', 'source', 'reason', 'observation_id', 'total', 'used', 'available',
            'percent', 'sampled_at', 'sampled_at_latest', 'coverage'})

    def test_invalid_fixed_markers_are_rejected(self):
        for key, value in (('status', 'current'), ('source', 'raw'), ('reason', '/private/path'),
                           ('observation_id', '1/private'), ('observation_id', '1' * 33),
                           ('observation_id', 1), ('observation_id', '\u0661')):
            with self.subTest(key=key, value=value):
                broken = observation()
                broken[key] = value
                self.assertIsNone(compat._capacity_last_observation(broken))

    def test_invalid_numeric_values_are_rejected_without_affecting_current_projection(self):
        for key, value in (('total', 0), ('total', True), ('total', 10 ** 1000),
                           ('used', -1), ('used', 101), ('used', float('nan')),
                           ('available', 50), ('percent', float('inf')), ('percent', 26),
                           ('sampled_at', 0), ('sampled_at_latest', 999)):
            with self.subTest(key=key, value=repr(value)[:60]):
                broken = observation()
                broken[key] = value
                self.rows.capacity_observations['disks']['sda'] = broken
                result = self.panel._disks(include_identity=True)
                self.assertIsNone(result[0]['capacity_last_observation'])
                self.assertEqual(result[1]['percent'], 50)

    def test_invalid_coverage_is_rejected(self):
        for key, value in (('scope', 'whole_pool'), ('complete', True),
                           ('filesystem_count', 0), ('filesystem_count', 129),
                           ('disk_count', True), ('disk_count', 129)):
            with self.subTest(key=key, value=value):
                broken = observation()
                broken['coverage'][key] = value
                self.assertIsNone(compat._capacity_last_observation(broken))

    def test_observation_failure_keeps_business_values_and_does_not_become_proof(self):
        class BrokenHistory(dict):
            def get(self, *args):
                raise RuntimeError('private-observer-canary')
        self.rows.capacity_observations = BrokenHistory()
        result = self.panel.overview(include_identity=True)
        self.assertEqual((result['disk_size'], result['disk_used'], result['disk_total']), (200, 100, 50))
        self.assertIsNone(result['capacity_last_observation_summary'])
        self.assertTrue(all(row['capacity_last_observation'] is None for row in result['disks_detail']))
        self.assertNotIn('private-observer-canary', json.dumps(result))

    def test_large_sideband_is_rejected_instead_of_partial_uniqueness_claim(self):
        self.rows.capacity_observations['disks'] = {str(index): observation() for index in range(129)}
        result = self.panel.overview(include_identity=True)
        self.assertIsNone(result['capacity_last_observation_summary'])
        self.assertTrue(all(row['capacity_last_observation'] is None for row in result['disks_detail']))

    def test_smart_detail_exposes_history_only_for_owner(self):
        owner = self.panel.smart('sda', include_identity=True)
        regular = self.panel.smart('sda')
        self.assertEqual(owner['capacity_last_observation'], self.history)
        self.assertNotIn('capacity_last_observation', regular)
        self.assertEqual({key: value for key, value in owner.items()
                          if key != 'capacity_last_observation'}, regular)

    def test_private_summary_attribute_is_not_serialized_as_an_extra_disk_or_field(self):
        value = self.panel._disks(include_identity=True)
        self.assertEqual(len(value), 2)
        self.assertEqual(value.capacity_history_summary, self.summary)
        self.assertNotIn('capacity_history_summary', json.dumps(value))
        self.assertEqual(copy.deepcopy(value).capacity_history_summary, self.summary)

    def test_no_current_capacity_is_created_from_history_when_all_rows_are_unknown(self):
        for row in self.rows:
            row.update(total_bytes=None, used_bytes=None, capacity_sampled_at=None,
                       capacity_status='unknown')
        result = self.panel.overview(include_identity=True)
        self.assertIsNotNone(result['capacity_last_observation_summary'])
        self.assertIsNone(result['disk_size'])
        self.assertIsNone(result['disk_used'])
        self.assertIsNone(result['disk_total'])
        self.assertTrue(all(row['total'] is None and row['used'] is None and row['percent'] is None
                            for row in result['disks_detail']))

    def test_owner_history_does_not_open_paths_or_start_collectors(self):
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('file read')), \
                mock.patch.object(compat.os, 'statvfs', create=True,
                                  side_effect=AssertionError('statvfs')):
            result = self.panel._disks(include_identity=True)
        self.assertEqual(result[0]['capacity_last_observation'], self.history)


if __name__ == '__main__':
    unittest.main()

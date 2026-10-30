"""Readiness observes only the existing mount/cache planner, never data paths."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_readiness as readiness


ROOT = 'root_' + 'a' * 24
MISSING = 'root_' + 'b' * 24


def root_catalog(*, read_only=False):
    return {'available': True, 'status': 'authorization_plan', 'boundary_version': 3, 'roots': [
        {'id': ROOT, 'label': '视频资源', 'mount': '/drives/fixture-data/Users/admin',
         'read_only': read_only}]}


def grant(path='', access='readonly', root=ROOT):
    return {'id': root, 'path': path, 'access': access}


class FileReadinessTests(unittest.TestCase):
    def test_private_custody_selection_is_unavailable_without_opening_or_rewriting_it(self):
        original = [grant('.r1-toolbox-ops/source')]
        result = self.collect(original)
        selection = result['planning']['selection']
        self.assertFalse(selection['verified'])
        self.assertIn('内部恢复区', selection['grants'][0]['reason'])
        self.assertEqual(original, [grant('.r1-toolbox-ops/source')])

    def collect(self, grants=None, catalog=None):
        provider = mock.Mock(return_value=root_catalog() if catalog is None else catalog)
        result = readiness.collect_file_access_readiness(object(), grants=grants, root_collector=provider)
        provider.assert_called_once()
        return result

    def test_real_planner_only_reads_mount_metadata_and_cached_overview(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / 'self').mkdir()
            (proc / 'self/mountinfo').write_text(
                '100 1 8:1 / /drives/fixture-data rw - ext4 /dev/sda1 rw\n', encoding='utf-8')
            panel = mock.Mock(proc_root=proc)
            panel.overview.return_value = {'disks_detail': [
                {'name': 'sda', 'serial': 'FIXTURE-SERIAL', 'user_name': '视频资源',
                 'mounts': ['/drives/fixture-data']}]}
            original_open = Path.open
            def restricted_open(path, *args, **kwargs):
                self.assertEqual(path, proc / 'self/mountinfo')
                return original_open(path, *args, **kwargs)
            with mock.patch.object(Path, 'open', new=restricted_open), \
                    mock.patch('os.stat', side_effect=AssertionError('no data-path stat')), \
                    mock.patch('os.scandir', side_effect=AssertionError('no directory enumeration')), \
                    mock.patch('socket.create_connection', side_effect=AssertionError('no network')) as network, \
                    mock.patch('subprocess.run', side_effect=AssertionError('no commands')) as commands:
                value = readiness.collect_file_access_readiness(panel)
            network.assert_not_called()
            commands.assert_not_called()
            self.assertEqual(panel.mock_calls, [mock.call.overview(include_identity=True)])
            self.assertTrue(value['planning']['available'])
            self.assertTrue(value['capabilities']['root_metadata'])
            self.assertNotIn('FIXTURE-SERIAL', json.dumps(value))
            self.assertNotIn('/drives/', json.dumps(value))

    def test_root_plan_does_not_turn_into_file_access_or_authorization(self):
        result = self.collect([grant('视频')])
        self.assertTrue(result['diagnostic_available'])
        self.assertFalse(result['available'])
        self.assertFalse(result['file_access_available'])
        self.assertEqual(result['status'], 'metadata_only')
        self.assertEqual(result['execution_backend']['status'], 'requires_per_operation_verification')
        self.assertIsNone(result['execution_backend']['runtime_verified'])
        self.assertIn('prepare_file_download', result['execution_backend']['implemented_tools'])
        self.assertFalse(result['execution_backend']['write_implemented'])
        for capability in ('list_directory', 'get_file_info', 'download', 'upload', 'file_write'):
            self.assertIs(result['capabilities'][capability], False)
        self.assertFalse(result['effects']['grants_authorization'])
        self.assertFalse(result['effects']['root_fallback_allowed'])
        self.assertTrue(result['planning']['selection']['verified'])
        self.assertFalse(result['planning']['selection']['grants'][0]['file_access_verified'])

    def test_four_dual_partition_disks_and_external_bind_remain_metadata_only(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / 'self').mkdir()
            disks, lines = [], []
            for index, name in enumerate(('sda', 'sdb', 'sdc', 'sdd')):
                mounts = [f'/drives/fixture_{name}_{part}' for part in (1, 2)]
                for part, mount in enumerate(mounts, 1):
                    lines.append(f'{100 + index * 2 + part} 1 8:{index * 16 + part} '
                                 f'/ {mount} rw - ext4 /dev/{name}{part} rw\n')
                disks.append({'name': name, 'serial': 'FIXTURE-' + name, 'mounts': mounts})
            disks[-1]['mounts'].append('/home/openclaw')
            lines.append('200 1 8:50 /Users/fixture-user/openclaw /home/openclaw rw - ext4 /dev/sdd2 rw\n')
            (proc / 'self/mountinfo').write_text(''.join(lines), encoding='utf-8')
            panel = mock.Mock(proc_root=proc)
            panel.overview.return_value = {'disks_detail': disks}
            value = readiness.collect_file_access_readiness(panel)
        self.assertEqual(len(value['planning']['roots']), 8)
        self.assertTrue(value['planning']['available'])
        self.assertFalse(value['file_access_available'])
        self.assertEqual(value['status'], 'metadata_only')
        self.assertFalse(value['effects']['opens_user_files'])
        self.assertFalse(value['effects']['grants_authorization'])
        self.assertNotIn('/drives/', json.dumps(value))
        self.assertNotIn('/home/openclaw', json.dumps(value))

    def test_unselected_scope_is_explicit_and_does_not_open_paths(self):
        for grants in (None, []):
            value = self.collect(grants)
            self.assertEqual(value['planning']['selection']['status'], 'not_selected')
            self.assertEqual(value['planning']['selection']['grants'], [])
            checks = {item['id']: item for item in value['checks']}
            self.assertEqual(checks['selected_grant_bindings']['status'], 'not_selected')
            self.assertEqual(checks['live_root_object_identity']['status'], 'not_selected')
            self.assertFalse(value['file_access_available'])

    def test_multiple_grants_for_one_root_are_preserved_in_order(self):
        inputs = [grant('视频'), grant('备份/资料'), grant('', 'readwrite')]
        value = self.collect(inputs)
        rows = value['planning']['selection']['grants']
        self.assertEqual([(row['id'], row['path'], row['access']) for row in rows],
                         [(row['id'], row['path'], row['access']) for row in inputs])
        self.assertEqual([row['index'] for row in rows], [0, 1, 2])
        self.assertTrue(all(row['verified'] for row in rows))
        self.assertFalse(value['file_access_available'])
        self.assertEqual(inputs, [grant('视频'), grant('备份/资料'), grant('', 'readwrite')])

    def test_stale_root_is_not_rebound_by_same_label(self):
        value = self.collect([grant('视频', root=MISSING)])
        row = value['planning']['selection']['grants'][0]
        self.assertEqual(row['id'], MISSING)
        self.assertFalse(row['verified'])
        self.assertEqual(value['planning']['roots'][0]['id'], ROOT)
        self.assertEqual(value['planning']['selection']['status'], 'unverified')

    def test_readonly_mount_cannot_validate_write_grant(self):
        value = self.collect([grant('视频', 'readwrite'), grant('备份')],
                             root_catalog(read_only=True))
        rows = value['planning']['selection']['grants']
        self.assertFalse(rows[0]['verified'])
        self.assertTrue(rows[1]['verified'])
        self.assertFalse(value['planning']['selection']['verified'])

    def test_empty_and_inconsistent_catalogs_fail_closed(self):
        base = root_catalog()
        invalid = [None, [], {}, {'available': True, 'status': 'authorization_plan', 'roots': []},
                   dict(base, available=False), dict(base, available=1),
                   dict(base, boundary_version=1), dict(base, boundary_version=2),
                   dict(base, boundary_version=True),
                   {key: value for key, value in base.items() if key != 'boundary_version'},
                   dict(base, status='live_verified'), dict(base, roots=base['roots'] * 2),
                   dict(base, roots=base['roots'] * 129)]
        for raw in invalid:
            with self.subTest(raw_type=type(raw).__name__):
                provider = mock.Mock(return_value=raw)
                value = readiness.collect_file_access_readiness(None, grants=[grant()], root_collector=provider)
                self.assertFalse(value['planning']['available'])
                self.assertFalse(value['planning']['selection']['verified'])
                self.assertFalse(value['file_access_available'])

    def test_only_exact_admin_root_metadata_can_be_projected(self):
        for path in ('/drives/fixture-data', '/drives/fixture-data/Users', '/drives/fixture-data/Users2',
                     '/drives/fixture-data/users', '/drives/fixture-data/Users/alice',
                     '/drives/fixture-data/Users/../Applications',
                     '/drives/nested/data/Users'):
            raw = root_catalog()
            raw['roots'][0]['mount'] = path
            with self.subTest(path=path):
                value = self.collect([grant()], raw)
                self.assertFalse(value['planning']['available'])
                self.assertFalse(value['planning']['selection']['verified'])
                self.assertEqual(value['planning']['roots'], [])
                self.assertNotIn(path, json.dumps(value))

    def test_provider_failure_is_fixed_and_does_not_leak_exception(self):
        secret = 'fixture-secret-not-a-real-credential'
        provider = mock.Mock(side_effect=RuntimeError(secret))
        value = readiness.collect_file_access_readiness(None, root_collector=provider)
        self.assertNotIn(secret, json.dumps(value))
        self.assertFalse(value['planning']['available'])
        self.assertTrue(value['diagnostic_available'])
        self.assertIn('root_metadata_plan', value['blockers'])

    def test_extra_provider_evidence_cannot_mark_acl_verified_or_leak_secrets(self):
        raw = root_catalog()
        raw.update(account_mapping={'verified': True, 'unix_uid': 0},
                   acl_verified=True, safe_open=True, token='fixture-secret')
        raw['roots'][0].update(password='fixture-secret', files=['private.txt'])
        value = self.collect([grant()], raw)
        self.assertNotIn('fixture-secret', json.dumps(value))
        self.assertNotIn('private.txt', json.dumps(value))
        self.assertFalse(value['account_mapping']['verified'])
        self.assertIsNone(value['account_mapping']['nas_user_id'])
        self.assertIsNone(value['account_mapping']['unix_uid'])
        self.assertIsNone(value['account_mapping']['supplementary_groups'])
        checks = {item['id']: item for item in value['checks']}
        for identifier in ('nas_subject_mapping', 'uid_gid_groups_mapping',
                           'native_acl_semantics', 'safe_open_same_object'):
            self.assertEqual(checks[identifier]['status'], 'unverified')
            self.assertFalse(checks[identifier]['verified'])

    def test_invalid_grants_are_reported_without_echoing_arbitrary_fields(self):
        invalid = [grant('../secret'), grant('/absolute'), grant('a//b'), grant('a\\b'),
                   grant('a\x00b'), grant('\ud800'), grant('a' * 1025),
                   dict(grant(), token='fixture-secret'), {'id': 'not-a-root'}, 'not-a-grant']
        value = self.collect(invalid)
        rows = value['planning']['selection']['grants']
        self.assertEqual(len(rows), len(invalid))
        self.assertTrue(all(not row['verified'] for row in rows))
        encoded = json.dumps(value)
        self.assertNotIn('fixture-secret', encoded)
        self.assertNotIn('/absolute', encoded)
        self.assertFalse(value['file_access_available'])

    def test_duplicate_exact_grants_are_not_silently_merged(self):
        value = self.collect([grant('视频'), grant('视频')])
        rows = value['planning']['selection']['grants']
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]['verified'])
        self.assertFalse(rows[1]['verified'])
        self.assertFalse(value['planning']['selection']['verified'])

    def test_selection_limit_and_bad_types_are_bounded(self):
        for grants in ({}, True, 'root', [grant(str(i)) for i in range(33)]):
            value = self.collect(grants)
            self.assertEqual(value['planning']['selection']['status'], 'unverified')
            self.assertEqual(value['planning']['selection']['grants'], [])
            self.assertFalse(value['file_access_available'])

    def test_reference_firmware_is_not_running_device_evidence(self):
        value = self.collect([grant()])
        reference = value['static_reference']
        self.assertEqual(reference['actions'], ['get_list', 'get_info', 'download'])
        self.assertFalse(reference['runtime_verified'])
        checks = {row['id']: row for row in value['checks']}
        for name in ('native_path_protocol', 'safe_open_same_object',
                     'download_range_and_stream', 'read_only_device_acceptance'):
            self.assertEqual(checks[name]['status'], 'unverified')
            self.assertIn(name, value['blockers'])

    def test_result_is_detached_bounded_json_with_fixed_checklist(self):
        raw = root_catalog()
        before = copy.deepcopy(raw)
        value = self.collect([grant('目录/' + '😀' * 250)], raw)
        self.assertEqual(raw, before)
        value['static_reference']['known'].append('not-evidence')
        fresh = self.collect()
        self.assertNotIn('not-evidence', fresh['static_reference']['known'])
        self.assertEqual(len(fresh['checks']), 10)
        self.assertTrue(all(row['status'] in ('verified', 'unverified', 'not_selected')
                            and row['verified'] == (row['status'] == 'verified') for row in fresh['checks']))
        self.assertLess(len(json.dumps(fresh, ensure_ascii=False).encode('utf-8')), 32768)
        self.assertFalse(fresh['effects']['enumerates_user_directories'])
        self.assertFalse(fresh['effects']['reads_credentials'])
        self.assertFalse(fresh['effects']['starts_smart'])
        self.assertFalse(fresh['effects']['calls_native_actions'])


if __name__ == '__main__':
    unittest.main()

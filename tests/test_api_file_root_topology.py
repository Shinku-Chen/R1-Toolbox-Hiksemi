"""Root diagnostics explain exclusions without browsing data or granting access."""

import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_roots as roots


def line(mount='/drives/data', *, mid=100, parent=1, device='8:1',
         fsroot='/', flags='rw', filesystem='ext4', source='/dev/sda1', superflags='rw'):
    return f'{mid} {parent} {device} {fsroot} {mount} {flags} - {filesystem} {source} {superflags}\n'


def fingerprint(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class ShortReader(io.BytesIO):
    def __init__(self, raw, step=7, fail_after=None):
        super().__init__(raw)
        self.step, self.fail_after, self.eof_seen, self.reads = step, fail_after, False, 0

    def read(self, size=-1):
        self.reads += 1
        if self.fail_after is not None and self.tell() >= self.fail_after:
            raise OSError('private path and secret must not escape')
        result = super().read(min(size, self.step))
        self.eof_seen |= result == b''
        return result


class ApiFileRootTopologyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.proc = Path(temporary.name)
        (self.proc / 'self').mkdir()
        self.mountinfo = self.proc / 'self/mountinfo'
        self.disk = {'name': 'sda', 'serial': 'PRIVATE-SERIAL', 'user_name': 'private label',
                     'volume': 'PRIVATE-UUID', 'mounts': ['/drives/data']}
        self.panel = mock.Mock(proc_root=self.proc)
        self.panel.overview.return_value = {'disks_detail': [self.disk]}
        self.set_mounts(line())

    def set_mounts(self, value):
        self.mountinfo.write_bytes(value.encode('utf-8'))

    def topology(self):
        return roots.collect_file_root_topology(self.panel)

    def candidate(self, result, mount='/drives/data'):
        return next(row for row in result['candidates'] if row['mount_fingerprint'] == fingerprint(mount))

    def test_complete_shape_catalog_fingerprint_and_existing_root_id_bytes(self):
        result, catalog = self.topology(), roots.collect_file_roots(self.panel)
        expected_fp = hashlib.sha256(json.dumps(catalog, sort_keys=True, ensure_ascii=True,
            separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()
        identity = ['r1-file-admin-root-v3', 'PRIVATE-SERIAL', '/dev/sda1', '8:1',
                    'ext4', '/drives/data/Users/admin']
        expected_id = 'root_' + hashlib.sha256(json.dumps(identity, ensure_ascii=True,
            separators=(',', ':')).encode('ascii')).hexdigest()[:24]
        self.assertTrue(result['available'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['reason_codes'], [])
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['scope'], 'file_root_topology')
        self.assertEqual(result['boundary_version'], 3)
        self.assertEqual(result['catalog_fingerprint'], expected_fp)
        self.assertEqual(result['catalog_root_ids'], [expected_id])
        candidate = self.candidate(result)
        self.assertEqual(candidate['candidate_id'], 'candidate_' + fingerprint('/drives/data')[:24])
        self.assertEqual(candidate['root_id'], expected_id)
        self.assertTrue(candidate['eligible'])
        self.assertTrue(candidate['catalog_included'])
        self.assertEqual(candidate['entry_count'], 1)
        self.assertEqual(candidate['users_mount_count'], 0)
        entry = candidate['entries'][0]
        self.assertEqual((entry['mount_id'], entry['parent_mount_id'], entry['device']), (100, 1, '8:1'))
        self.assertEqual((entry['filesystem'], entry['filesystem_root']), ('ext4', 'full'))
        self.assertEqual(entry['source'], {'class': 'supported_block_name', 'device_name': 'sda1'})
        self.assertEqual(entry['canonical_source_aliases'],
                         {'count': 1, 'mount_fingerprints': [fingerprint('/drives/data')]})
        self.assertEqual(entry['canonical_device_aliases'], entry['canonical_source_aliases'])
        self.assertEqual(entry['cache'], {'observed_device_records': 1, 'valid_identity_records': 1,
            'identity_available': True, 'serial_unique': True, 'mount_matches': True})
        mount_report, cache_report = result['inputs']['mountinfo'], result['inputs']['cached_disks']
        self.assertEqual(mount_report, {'status': 'ok', 'eof': True,
            'bytes_read': len(line().encode()), 'parsed_records': 1, 'malformed_records': 0})
        self.assertEqual(cache_report, {'status': 'ok', 'records': 1,
            'valid_identity_records': 1, 'invalid_identity_records': 0})
        self.assertEqual(result['inputs']['verification'], {'mountinfo': mount_report,
            'cached_disks': cache_report, 'mountinfo_unchanged': True, 'cached_disks_unchanged': True})

    def test_reads_only_fixed_proc_file_and_existing_identity_cache(self):
        original_open = Path.open
        opened = []

        def open_fixed(path, *args, **kwargs):
            self.assertEqual(str(path), str(self.mountinfo))
            self.assertEqual(args, ('rb',))
            opened.append(str(path))
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, 'open', open_fixed), \
             mock.patch('os.stat', side_effect=AssertionError('no stat')), \
             mock.patch('os.open', side_effect=AssertionError('no data open')), \
             mock.patch('os.scandir', side_effect=AssertionError('no directories')), \
             mock.patch('os.listdir', side_effect=AssertionError('no directories')), \
             mock.patch('subprocess.run', side_effect=AssertionError('no native commands')), \
             mock.patch('subprocess.Popen', side_effect=AssertionError('no native commands')):
            result = self.topology()
        self.assertTrue(result['complete'])
        self.assertEqual(opened, [str(self.mountinfo)] * 2)
        self.assertEqual(self.panel.mock_calls, [mock.call.overview(include_identity=True)] * 2)
        self.assertEqual(result['effects'], {'metadata_only': True, 'reads_proc_mountinfo': True,
            'uses_existing_identity_cache': True, 'opens_data_paths': False, 'stats_data_paths': False,
            'enumerates_user_directories': False, 'opens_user_files': False, 'calls_native_actions': False,
            'starts_smart': False, 'reads_credentials': False, 'grants_authorization': False})

    def test_each_qualification_rejection_is_visible_without_making_acquisition_incomplete(self):
        scenarios = (
            ('candidate_path_invalid', line('/drives/..'), None, '/drives/..'),
            ('candidate_mount_stacked', line() + line(mid=101), None, '/drives/data'),
            ('source_unsupported', line(source='/dev/mapper/private'), None, '/drives/data'),
            ('filesystem_unsupported', line(filesystem='private-fs'), None, '/drives/data'),
            ('filesystem_root_not_full', line(fsroot='/private'), None, '/drives/data'),
            ('canonical_source_not_unique', line() + line('/drives/alias', mid=101), None, '/drives/data'),
            ('canonical_device_not_unique', line() + line('/drives/alias', mid=101, source='/dev/sda2'), None, '/drives/data'),
            ('mount_flags_invalid', line(flags='rw,ro'), None, '/drives/data'),
            ('super_flags_invalid', line(superflags='relatime'), None, '/drives/data'),
            ('root_ancestor_stacked', line() + line('/', mid=1) + line('/', mid=2), None, '/drives/data'),
            ('drives_ancestor_stacked', line() + line('/drives', mid=2) + line('/drives', mid=3), None, '/drives/data'),
            ('cached_device_identity_missing', line(), [dict(self.disk, serial=None)], '/drives/data'),
            ('cached_device_identity_ambiguous', line(), [self.disk, dict(self.disk)], '/drives/data'),
            ('cached_serial_not_unique', line(), [self.disk, dict(self.disk, name='sdb')], '/drives/data'),
            ('cached_mount_unmatched', line(), [dict(self.disk, mounts=['/drives/old'])], '/drives/data'),
        )
        for reason, text, disks, mount in scenarios:
            with self.subTest(reason=reason):
                self.set_mounts(text)
                self.panel.overview.return_value = {'disks_detail': [self.disk] if disks is None else disks}
                result = self.topology()
                self.assertTrue(result['complete'], result['reason_codes'])
                candidate = self.candidate(result, mount)
                self.assertIn(reason, candidate['reason_codes'])
                self.assertFalse(candidate['eligible'])
                self.assertIsNone(candidate['root_id'])
                self.assertFalse(candidate['catalog_included'])

    def test_all_applicable_reasons_are_preserved_in_one_candidate(self):
        self.set_mounts(line(source='private-secret', filesystem='secret-fs', fsroot='/secret',
                             flags='ro,rw', superflags='relatime')
            + line('/', mid=1) + line('/', mid=2)
            + line('/drives', mid=3) + line('/drives', mid=4)
            + line('/drives/data/Users', mid=101))
        candidate = self.candidate(self.topology())
        self.assertEqual(set(candidate['reason_codes']), {
            'root_ancestor_stacked', 'drives_ancestor_stacked',
            'source_unsupported', 'filesystem_unsupported', 'filesystem_root_not_full',
            'canonical_source_not_unique', 'canonical_device_not_unique',
            'mount_flags_invalid', 'super_flags_invalid'})

    def test_external_aliases_allowed_internal_full_aliases_reject(self):
        original = roots.collect_file_roots(self.panel)['roots'][0]['id']
        self.set_mounts(line() + line('/home/private', mid=101)
                       + line('/mnt/private', mid=102, fsroot='/private')
                       + line('/drives/data/Applications/private', mid=103, fsroot='/private'))
        result = self.topology()
        self.assertTrue(result['complete'])
        self.assertEqual(result['catalog_root_ids'], [original])
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(self.candidate(result)['entries'][0]['canonical_source_aliases']['count'], 1)
        self.set_mounts(line() + line('/drives/alias', mid=104))
        result = self.topology()
        self.assertTrue(result['complete'])
        self.assertEqual(result['catalog_root_ids'], [])
        aliases = self.candidate(result)['entries'][0]['canonical_source_aliases']
        self.assertEqual(aliases['count'], 2)
        self.assertEqual(set(aliases['mount_fingerprints']),
                         {fingerprint('/drives/data'), fingerprint('/drives/alias')})

    def test_users_mount_evidence_is_hashed_without_rejecting_admin_root(self):
        child = '/drives/data/Users/admin/secret-personal-file'
        self.set_mounts(line() + line('/home/private', mid=101) + line(child, mid=102, fsroot='/secret'))
        result = self.topology()
        candidate = self.candidate(result)
        self.assertTrue(result['complete'])
        self.assertEqual(candidate['users_mount_count'], 1)
        self.assertEqual(candidate['users_mounts'], [{'mount_fingerprint': fingerprint(child),
            'class': 'users_descendant', 'mount_id': 102, 'parent_mount_id': 1,
            'device': '8:1', 'filesystem': 'ext4', 'filesystem_class': 'supported'}])
        self.assertNotIn('secret', json.dumps(result))
        self.assertTrue(candidate['eligible'])
        self.assertTrue(candidate['qualification_passed'])
        self.assertTrue(candidate['catalog_included'])
        self.assertNotIn('users_mount_present', candidate['reason_codes'])
        self.assertEqual(result['catalog_root_ids'], [candidate['root_id']])

    def test_secret_uuid_fsroot_options_labels_and_mount_paths_never_escape(self):
        secret = 'UNIQUE_PRIVATE_SECRET_123'
        self.disk.update(serial=secret, user_name=secret, volume=secret)
        self.disk['mounts'] = ['/drives/data', '/home/' + secret]
        self.set_mounts(line('/drives/' + secret, source='/dev/disk/by-uuid/' + secret,
            filesystem=secret, fsroot='/' + secret, flags='rw,token=' + secret,
            superflags='rw,password=' + secret)
            + line('/drives/' + secret + '/Users/admin/' + secret, mid=101, filesystem=secret))
        result = self.topology()
        serialized = json.dumps(result)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('/drives/', serialized)
        self.assertNotIn('/Users', serialized)
        self.assertNotIn('/home/', serialized)
        entry = result['candidates'][0]['entries'][0]
        self.assertEqual(entry['source'], {'class': 'device_alias', 'device_name': None})
        self.assertIsNone(entry['filesystem'])
        self.assertEqual(entry['filesystem_root'], 'subdirectory')

    def test_known_unsupported_and_virtual_sources_keep_only_fixed_classification(self):
        for source, expected in (('/dev/mmcblk0p1', {'class': 'unsupported_block_name', 'device_name': 'mmcblk0p1'}),
                                  ('tmpfs', {'class': 'virtual_or_unknown', 'device_name': None}),
                                  ('UUID=PRIVATE', {'class': 'virtual_or_unknown', 'device_name': None})):
            with self.subTest(source=source):
                self.set_mounts(line(source=source, filesystem='tmpfs'))
                entry = self.candidate(self.topology())['entries'][0]
                self.assertEqual(entry['source'], expected)
                self.assertEqual(entry['filesystem'], 'tmpfs')

    def test_catalog_order_not_root_id_order_and_partitions_remain_separate(self):
        self.disk['mounts'] = ['/drives/z', '/drives/a']
        self.set_mounts(line('/drives/z', source='/dev/sda2', device='8:2')
                       + line('/drives/a', mid=101))
        result, catalog = self.topology(), roots.collect_file_roots(self.panel)
        self.assertTrue(result['complete'])
        self.assertEqual(result['catalog_root_ids'], [row['id'] for row in catalog['roots']])
        self.assertEqual([row['mount'] for row in catalog['roots']], ['/drives/a/Users/admin', '/drives/z/Users/admin'])
        self.assertEqual(result['candidate_count'], 2)

    def test_mount_escape_fingerprint_uses_exact_decoded_utf8_path(self):
        self.disk['mounts'] = ['/drives/my disk']
        self.set_mounts(line('/drives/my\\040disk'))
        result = self.topology()
        self.assertTrue(result['complete'])
        self.assertEqual(result['candidates'][0]['mount_fingerprint'], fingerprint('/drives/my disk'))
        self.assertTrue(result['candidates'][0]['eligible'])

    def test_short_reads_must_reach_eof_for_topology_and_catalog(self):
        for collector in (roots.collect_file_roots, roots.collect_file_root_topology):
            with self.subTest(collector=collector.__name__):
                readers = []

                def opened(*args, **kwargs):
                    reader = ShortReader(line().encode(), step=3)
                    readers.append(reader)
                    return reader

                with mock.patch.object(Path, 'open', side_effect=opened):
                    result = collector(self.panel)
                self.assertTrue(all(reader.eof_seen for reader in readers))
                self.assertTrue(all(reader.reads > 3 for reader in readers))
                self.assertEqual(len(readers), 1 if collector is roots.collect_file_roots else 2)
                self.assertTrue(result['available'])
                if collector is roots.collect_file_root_topology:
                    self.assertTrue(result['complete'])

    def test_exact_byte_limit_requires_one_more_read_to_prove_eof(self):
        baseline = line()
        padding = 'x' * (roots._MAX_MOUNTINFO - len(baseline.encode()) - 1)
        raw = baseline.replace(' - ', ' ' + padding + ' - ').encode()
        self.assertEqual(len(raw), roots._MAX_MOUNTINFO)
        readers = [ShortReader(raw, step=65536), ShortReader(raw, step=65536)]
        with mock.patch.object(Path, 'open', side_effect=readers):
            result = self.topology()
        self.assertTrue(all(reader.eof_seen for reader in readers))
        self.assertTrue(result['complete'])
        self.assertEqual(result['inputs']['mountinfo']['bytes_read'], roots._MAX_MOUNTINFO)

    def test_oversize_proc_is_partial_and_does_not_acquire_cache(self):
        self.mountinfo.write_bytes(b'x' * (roots._MAX_MOUNTINFO + 1))
        result = self.topology()
        self.assertFalse(result['complete'])
        self.assertIn('mountinfo_limit_exceeded', result['reason_codes'])
        self.assertFalse(result['inputs']['mountinfo']['eof'])
        self.assertEqual(result['inputs']['mountinfo']['bytes_read'], roots._MAX_MOUNTINFO + 1)
        self.panel.overview.assert_not_called()
        self.assertEqual(result['catalog_root_ids'], [])

    def test_failure_after_a_complete_short_read_retains_partial_evidence_not_grants(self):
        raw = line().encode()
        readers = [ShortReader(raw, step=len(raw), fail_after=len(raw)) for unused in range(2)]
        with mock.patch.object(Path, 'open', side_effect=readers):
            result = self.topology()
        self.assertFalse(result['complete'])
        self.assertIn('mountinfo_read_failed', result['reason_codes'])
        self.assertEqual(result['inputs']['mountinfo']['parsed_records'], 1)
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(result['catalog_root_ids'], [])
        self.assertIsNone(result['candidates'][0]['root_id'])
        self.panel.overview.assert_not_called()
        self.assertNotIn('private path', json.dumps(result))

    def test_missing_invalid_encoding_and_malformed_metadata_are_incomplete(self):
        for raw, code in ((line().encode() + b'\xff\n', 'mountinfo_invalid_encoding'),
                          (line().encode() + b'invalid hidden mount\n', 'mountinfo_malformed'),
                          (line().encode() + b'101 bad 8:1 / /drives/data/Users rw - ext4 /dev/sda1 rw\n', 'mountinfo_malformed')):
            with self.subTest(code=code, raw=raw[-10:]):
                self.mountinfo.write_bytes(raw)
                result = self.topology()
                self.assertFalse(result['complete'])
                self.assertIn(code, result['reason_codes'])
                self.assertEqual(result['catalog_root_ids'], [])
        self.mountinfo.unlink()
        result = self.topology()
        self.assertFalse(result['available'])
        self.assertFalse(result['complete'])
        self.assertIn('mountinfo_unavailable', result['reason_codes'])

    def test_malformed_or_limited_cache_is_incomplete_but_missing_identity_is_complete(self):
        for disks, expected in ((None, 'cached_disks_invalid'), ([None], 'cached_disks_partial'),
                                 ([dict(self.disk, mounts=None)], 'cached_disks_partial'),
                                 ([self.disk] * 129, 'cached_disks_limit_exceeded')):
            with self.subTest(expected=expected):
                self.panel.overview.return_value = {'disks_detail': disks}
                result = self.topology()
                self.assertFalse(result['complete'])
                self.assertIn(expected, result['reason_codes'])
        self.panel.overview.return_value = {'disks_detail': [dict(self.disk, serial=None)]}
        result = self.topology()
        self.assertTrue(result['complete'])
        self.assertEqual(result['inputs']['cached_disks']['invalid_identity_records'], 1)
        self.assertIn('cached_device_identity_missing', self.candidate(result)['reason_codes'])
        self.panel.overview.side_effect = OSError('secret credentials')
        result = self.topology()
        self.assertFalse(result['complete'])
        self.assertIn('cached_disks_unavailable', result['reason_codes'])
        self.assertNotIn('secret credentials', json.dumps(result))

    def test_partial_cache_diagnostic_does_not_change_existing_catalog_filter(self):
        self.disk['mounts'].append(None)
        self.panel.overview.return_value = {'disks_detail': [self.disk, None]}
        catalog, result = roots.collect_file_roots(self.panel), self.topology()
        self.assertTrue(catalog['available'])
        self.assertEqual(result['catalog_root_ids'], [catalog['roots'][0]['id']])
        self.assertFalse(result['complete'])
        self.assertTrue(result['candidates'][0]['qualification_passed'])
        self.assertIsNone(result['candidates'][0]['eligible'])
        self.assertTrue(result['candidates'][0]['catalog_included'])

    def test_uncollected_cache_counts_and_identity_are_unknown_not_empty(self):
        for snapshot in ({}, {'disks_detail': None}):
            with self.subTest(snapshot=snapshot):
                self.panel.overview.return_value = snapshot
                result = self.topology()
                self.assertEqual(result['inputs']['cached_disks'], {'status': 'invalid',
                    'records': None, 'valid_identity_records': None, 'invalid_identity_records': None})
                candidate = self.candidate(result)
                self.assertIsNone(candidate['qualification_passed'])
                self.assertIsNone(candidate['eligible'])
                self.assertEqual(candidate['entries'][0]['cache'], {'observed_device_records': None,
                    'valid_identity_records': None, 'identity_available': None,
                    'serial_unique': None, 'mount_matches': None})
                self.assertIn('cached_device_identity_unavailable', candidate['reason_codes'])
                self.assertNotIn('cached_device_identity_missing', candidate['reason_codes'])
        self.panel.overview.return_value = {'disks_detail': [self.disk] * 129}
        cache = self.topology()['inputs']['cached_disks']
        self.assertEqual(cache['records'], 129)
        self.assertIsNone(cache['valid_identity_records'])
        self.assertIsNone(cache['invalid_identity_records'])
        self.mountinfo.unlink()
        cache = self.topology()['inputs']['cached_disks']
        self.assertEqual(cache, {'status': 'not_collected', 'records': None,
            'valid_identity_records': None, 'invalid_identity_records': None})

    def test_unprojectable_ids_do_not_change_legacy_catalog_but_are_not_complete_diagnostics(self):
        self.set_mounts(line(mid='١٠٠'))
        catalog, result = roots.collect_file_roots(self.panel), self.topology()
        self.assertTrue(catalog['available'])
        self.assertFalse(result['complete'])
        self.assertIn('mountinfo_projection_invalid', result['reason_codes'])
        self.assertEqual(result['catalog_root_ids'], [catalog['roots'][0]['id']])
        self.assertIsNone(self.candidate(result)['entries'][0]['mount_id'])
        self.assertIsNone(self.candidate(result)['eligible'])

    def test_output_candidate_and_evidence_limits_are_explicit(self):
        self.set_mounts(''.join(line('/drives/d' + str(index), mid=100 + index,
            device='8:' + str(index + 1), source='/dev/sda' + str(index + 1)) for index in range(129)))
        self.disk['mounts'] = ['/drives/d' + str(index) for index in range(128)]
        result = self.topology()
        self.assertEqual(result['candidate_count'], 129)
        self.assertEqual(len(result['candidates']), 128)
        self.assertEqual(result['omitted_candidates'], 1)
        self.assertFalse(result['complete'])
        self.assertIn('topology_limit_exceeded', result['reason_codes'])
        self.disk['mounts'] = ['/drives/data']
        self.set_mounts(line() + ''.join(line('/drives/data/Users/admin/secret-' + str(index),
            mid=101 + index) for index in range(520)))
        result = self.topology()
        self.assertEqual(self.candidate(result)['users_mount_count'], 520)
        self.assertLess(len(self.candidate(result)['users_mounts']), 520)
        self.assertFalse(result['complete'])
        self.assertIn('topology_limit_exceeded', result['reason_codes'])

    def test_root_limit_preserves_catalog_failure_and_reports_every_retained_candidate(self):
        disks, text = [], []
        # Two partitions per cached disk stay below the old per-disk/cache limits.
        for index in range(65):
            name = 'sd' + chr(97 + index // 26) + chr(97 + index % 26)
            mounts = ['/drives/' + name + '1', '/drives/' + name + '2']
            disks.append(dict(self.disk, name=name, serial='PRIVATE-' + name, mounts=mounts))
            for partition in (1, 2):
                text.append(line(mounts[partition - 1], mid=100 + len(text),
                    device='8:' + str(1 + len(text)), source='/dev/' + name + str(partition)))
        self.set_mounts(''.join(text))
        self.panel.overview.return_value = {'disks_detail': disks}
        catalog, result = roots.collect_file_roots(self.panel), self.topology()
        self.assertEqual(catalog['status'], 'limit_exceeded')
        self.assertEqual(result['catalog_root_ids'], [])
        self.assertIn('catalog_limit_exceeded', result['reason_codes'])
        self.assertTrue(all(row['root_id'] is None for row in result['candidates']))

    def test_empty_metadata_and_no_eligible_roots_can_still_be_complete(self):
        self.set_mounts('')
        self.panel.overview.return_value = {'disks_detail': []}
        result = self.topology()
        self.assertTrue(result['available'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['catalog_root_ids'], [])

    def test_cache_only_canonical_mount_is_a_hashed_rejected_candidate(self):
        missing = '/drives/BSJQZGLBMYA'
        self.disk['mounts'] = ['/drives/data', missing, '/home/private', missing + '/Applications/private']
        original = roots.collect_file_roots(self.panel)
        result = self.topology()
        self.assertTrue(result['complete'])
        self.assertEqual(result['candidate_count'], 2)
        self.assertEqual(result['catalog_root_ids'], [original['roots'][0]['id']])
        candidate = self.candidate(result, missing)
        self.assertEqual(candidate['origin'], 'cache_only')
        self.assertEqual(candidate['entry_count'], 0)
        self.assertEqual(candidate['entries'], [])
        self.assertIsNone(candidate['root_id'])
        self.assertFalse(candidate['catalog_included'])
        self.assertFalse(candidate['qualification_passed'])
        self.assertFalse(candidate['eligible'])
        self.assertEqual(candidate['reason_codes'], ['mountinfo_not_present'])
        self.assertEqual(candidate['cached_mount_associations'], {'records': 1, 'items': [{
            'device_name': 'sda', 'identity_available': True, 'device_identity_unique': True,
            'serial_unique': True, 'mount_matches': True}]})
        self.assertNotIn(missing, json.dumps(result))
        self.assertNotIn('/home/private', json.dumps(result))

    def test_rejected_topology_changes_cannot_hide_behind_an_unchanged_empty_catalog(self):
        cases = (
            (line(fsroot='/private-first'), line(fsroot='/private-second')),
            (line(flags='rw,first-secret'), line(flags='rw,second-secret')),
            (line(source='/dev/mapper/first-secret'), line(source='/dev/mapper/second-secret')),
            (line(filesystem='first-secret'), line(filesystem='second-secret')),
        )
        self.disk['serial'] = None  # All catalogs stay empty in both samples.
        for before, after in cases:
            with self.subTest(before=before):
                self.set_mounts(before)
                before_catalog = roots.collect_file_roots(self.panel)
                self.set_mounts(after)
                self.assertEqual(before_catalog, roots.collect_file_roots(self.panel))
                with mock.patch.object(Path, 'open', side_effect=[io.BytesIO(before.encode()), io.BytesIO(after.encode())]):
                    result = self.topology()
                self.assertFalse(result['complete'])
                self.assertIn('mountinfo_changed', result['reason_codes'])
                self.assertFalse(result['inputs']['verification']['mountinfo_unchanged'])
                self.assertEqual(result['catalog_root_ids'], [])
                self.assertIsNone(self.candidate(result)['eligible'])
                self.assertNotIn('secret', json.dumps(result))

    def test_consumed_cache_fields_are_rechecked_even_for_rejected_candidates(self):
        before = dict(self.disk, serial=None)
        changes = ({'name': 'sdb'}, {'serial': 'different-private'}, {'mounts': ['/drives/other']},
                   {'user_name': 'different-private'}, {'volume': 'different-private'})
        for change in changes:
            with self.subTest(change=change):
                after = dict(before, **change)
                self.panel.overview.side_effect = [{'disks_detail': [before]}, {'disks_detail': [after]}]
                result = self.topology()
                self.assertFalse(result['complete'])
                self.assertIn('cached_disks_changed', result['reason_codes'])
                self.assertFalse(result['inputs']['verification']['cached_disks_unchanged'])
                self.assertEqual(result['catalog_root_ids'], [])
                self.assertNotIn('private', json.dumps(result))

    def test_unrelated_telemetry_changes_do_not_invalidate_topology_stability(self):
        before = dict(self.disk, temp=31, used=100, percent=2, smart_sampled_at=123,
                      disk_io={'read_bytes': 123}, extra_cached_metadata={'value': 'first-private'})
        after = dict(self.disk, temp=32, used=200, percent=3, smart_sampled_at=124,
                     disk_io={'read_bytes': 456}, extra_cached_metadata={'value': 'second-private'})
        self.panel.overview.side_effect = [{'disks_detail': [before]}, {'disks_detail': [after]}]
        result = self.topology()
        self.assertTrue(result['complete'])
        self.assertTrue(result['inputs']['verification']['cached_disks_unchanged'])
        self.assertEqual(result['reason_codes'], [])
        self.assertNotIn('private', json.dumps(result))

    def test_consumed_cache_record_order_structure_and_invalid_record_types_are_rechecked(self):
        other = dict(self.disk, name='sdb', serial='other-private', mounts=['/drives/other'])
        scenarios = (([self.disk, other], [other, self.disk]), ([None], ['invalid-private']),
                     ([dict(self.disk, user_name=None)], [{key: value for key, value in self.disk.items()
                                                         if key != 'user_name'}]))
        for before, after in scenarios:
            with self.subTest(before=before):
                self.panel.overview.side_effect = [{'disks_detail': before}, {'disks_detail': after}]
                result = self.topology()
                self.assertFalse(result['complete'])
                self.assertIn('cached_disks_changed', result['reason_codes'])
                self.assertNotIn('private', json.dumps(result))

    def test_recheck_failure_is_unknown_and_first_sample_cannot_claim_stability(self):
        self.panel.overview.side_effect = [{'disks_detail': [self.disk]}, OSError('private failure')]
        result = self.topology()
        self.assertFalse(result['complete'])
        self.assertIsNone(result['inputs']['verification']['cached_disks_unchanged'])
        self.assertIn('recheck_cached_disks_unavailable', result['reason_codes'])
        self.assertIn('cached_disks_stability_unavailable', result['reason_codes'])
        self.assertEqual(result['inputs']['verification']['cached_disks'], {'status': 'unavailable',
            'records': None, 'valid_identity_records': None, 'invalid_identity_records': None})
        self.assertIsNone(self.candidate(result)['eligible'])
        self.assertNotIn('private failure', json.dumps(result))

    def test_cache_fingerprint_is_bounded_and_non_json_metadata_is_not_complete(self):
        for value, reason in (('x' * (roots._MAX_CACHE_METADATA + 1), 'cached_disks_metadata_limit_exceeded'),
                               (object(), 'cached_disks_metadata_invalid')):
            with self.subTest(reason=reason):
                self.disk['user_name'] = value
                result = self.topology()
                self.assertFalse(result['complete'])
                self.assertIn(reason, result['reason_codes'])
                self.assertIsNone(result['inputs']['verification']['cached_disks_unchanged'])
                self.assertTrue(result['candidates'][0]['catalog_included'])
                self.assertIsNone(result['candidates'][0]['eligible'])


if __name__ == '__main__':
    unittest.main()

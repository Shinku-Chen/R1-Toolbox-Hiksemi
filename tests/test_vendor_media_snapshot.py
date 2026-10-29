"""Temporary media snapshots only; never native sessions, files or services."""

import builtins
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import vendor_media_snapshot as snapshot


FIRST = '2026-10-29T00:00:00.000+00:00'
SECOND = '2026-10-29T00:01:00.000+00:00'
THIRD = '2026-10-29T00:02:00.000+00:00'
PRIVATE = 'private-fixture-token-or-path'
IDS = ('face', 'ocr', 'thumbnail', 'similar', 'ai', 'scraper', 'shared_album')


def face_data():
    return {'total': 9, 'totalpic': 6, 'totalvid': 3, 'processed': 6,
            'processedpic': 4, 'processedvid': 2,
            'picture_completion_ratio': 4 / 6, 'video_completion_ratio': 2 / 3,
            'sync_percent': 25, 'upgrade_status': 0, 'range_status': 0,
            'estimated_remaining': {'value': 2, 'unit': None}}


def data_by_id():
    return {'face': face_data(),
            'ocr': {'totalpic': 10, 'processedpic': 3, 'remainpic': 7,
                    'picture_completion_ratio': .3},
            'thumbnail': {'b': 1, 'st': 2, 'pt': 3, 'sc': 4, 'cc': 5},
            'similar': {'totalpic': 8, 'processedpic': 0, 'remainpic': 8,
                        'pending_hashes': 8, 'completion_ratio': None},
            'ai': {'configuration_status': 1, 'recognition_range_count': 2},
            'scraper': {'scaned': 4, 'total': 10, 'timestamp': 1234, 'num': 3, 'native_status': 0},
            'shared_album': {'scan_num': 1, 'failed_num': 0, 'await_num': 2, 'native_status': 1}}


def batch_result():
    values = data_by_id()
    return {'available': True, 'status': 'ok', 'sampled_at': PRIVATE,
            'credential': {'token': PRIVATE}, 'scope': {'user_id': PRIVATE},
            'results': [{'id': query, 'available': True, 'query_success': True,
                         'status': 'ok', 'reason': None, 'data': values[query]}
                        for query in IDS]}


class MediaSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='r1-media-snapshot-')
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.path = self.directory / 'media-snapshot.json'
        patcher = mock.patch.object(snapshot, '_stamp', return_value=FIRST)
        self.clock = patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, result=None, kind='batch'):
        value = snapshot.record_media_snapshot(self.directory,
            batch_result() if result is None else result, kind)
        self.assertTrue(value['stored'], value)
        return snapshot.collect_media_snapshot(self.directory)

    def item(self, value, query):
        return next(row for row in value['data']['results'] if row['id'] == query)

    def write_state(self, value):
        self.path.write_text(json.dumps(value, allow_nan=False), encoding='utf-8')
        os.chmod(self.path, 0o600)

    def test_missing_cache_is_not_collected_and_never_creates_a_file(self):
        value = snapshot.collect_media_snapshot(self.directory, root='/fixture-no-native-root')
        self.assertEqual(value['status'], 'not_collected')
        self.assertFalse(value['available'])
        self.assertIsNone(value['data'])
        self.assertEqual(value['source'], 'toolbox.media_snapshot')
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_all_seven_projected_results_have_own_capture_time_and_private_atomic_storage(self):
        value = self.record()
        self.assertTrue(value['available'])
        self.assertEqual(value['status'], 'cached')
        self.assertTrue(value['data']['complete'])
        self.assertFalse(value['data']['live'])
        self.assertTrue(value['data']['explicit_refresh'])
        self.assertEqual(value['data']['captured_at'], FIRST)
        self.assertEqual([row['id'] for row in value['data']['results']], list(IDS))
        self.assertTrue(all(row['captured_at'] == FIRST for row in value['data']['results']))
        self.assertEqual(value['data']['coverage'], {'total': 7, 'applicable': 7,
            'not_applicable': 0, 'succeeded': 7, 'data_available': 7, 'data_complete': 7})
        self.assertNotIn(PRIVATE, self.path.read_text(encoding='utf-8'))
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertEqual(list(self.directory.glob('.media-snapshot-*')), [])
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_shared_album_not_applicable_does_not_make_a_complete_applicable_batch_fail(self):
        result = batch_result()
        result['results'][-1] = {'id': 'shared_album', 'available': False, 'query_success': False,
            'status': 'not_applicable', 'reason': 'no_existing_owner_album', 'data': None}
        value = self.record(result)
        self.assertEqual(value['status'], 'cached')
        self.assertTrue(value['data']['complete'])
        self.assertEqual(value['data']['coverage'], {'total': 7, 'applicable': 6,
            'not_applicable': 1, 'succeeded': 6, 'data_available': 6, 'data_complete': 6})
        self.assertIsNone(self.item(value, 'shared_album')['data'])

    def test_not_applicable_cannot_hide_a_failed_non_album_or_unconfirmed_album_context(self):
        for query in ('face', 'shared_album'):
            result = batch_result()
            row = next(item for item in result['results'] if item['id'] == query)
            row.update(available=False, query_success=False, status='not_applicable',
                       reason='no_existing_owner_album' if query == 'face' else PRIVATE, data=None)
            with self.subTest(query=query):
                value = self.record(result)
                self.assertFalse(value['data']['complete'])
                self.assertEqual(value['data']['coverage']['not_applicable'], 0)
                self.assertNotIn(PRIVATE, json.dumps(value))

    def test_complete_batch_cannot_reuse_an_older_not_applicable_observation(self):
        result = batch_result()
        result['results'][-1] = {'id': 'shared_album', 'available': False, 'query_success': False,
            'status': 'not_applicable', 'reason': 'no_existing_owner_album', 'data': None}
        self.record(result)
        state = json.loads(self.path.read_text(encoding='utf-8'))
        state['captured_at'] = SECOND
        for row in state['results'][:-1]:
            row['captured_at'] = SECOND
        self.write_state(state)
        value = snapshot.collect_media_snapshot(self.directory)
        self.assertTrue(value['available'])
        self.assertFalse(value['data']['complete'])
        self.assertEqual(value['data']['coverage']['not_applicable'], 1)

    def test_failed_refresh_discards_old_success_for_that_item_and_retains_last_failure(self):
        self.record()
        self.clock.return_value = SECOND
        failed = {'available': False, 'status': 'unavailable', 'reason': 'timeout', 'data': None}
        value = self.record(failed, kind='face')
        face = self.item(value, 'face')
        self.assertFalse(face['available'])
        self.assertFalse(face['query_success'])
        self.assertIsNone(face['data'])
        self.assertEqual(face['captured_at'], SECOND)
        self.assertEqual(face['last_failure'], {'captured_at': SECOND, 'status': 'unavailable', 'reason': 'timeout'})
        self.assertEqual(self.item(value, 'ocr')['captured_at'], FIRST)
        self.assertFalse(value['data']['complete'])
        self.clock.return_value = THIRD
        value = self.record({'available': True, 'status': 'ok', 'data': face_data()}, kind='face')
        face = self.item(value, 'face')
        self.assertTrue(face['available'])
        self.assertEqual(face['captured_at'], THIRD)
        self.assertEqual(face['last_failure']['captured_at'], SECOND)
        self.assertFalse(value['data']['complete'])

    def test_batch_all_failures_are_readable_metadata_but_not_available_business_data(self):
        self.record()
        result = {'results': [{'id': query, 'available': False, 'query_success': False,
                              'status': 'unavailable', 'reason': 'credential_unavailable', 'data': None}
                             for query in IDS]}
        self.clock.return_value = SECOND
        value = self.record(result)
        self.assertFalse(value['available'])
        self.assertEqual(value['status'], 'cached_empty')
        self.assertFalse(value['data']['complete'])
        self.assertEqual(value['data']['coverage']['data_available'], 0)
        for row in value['data']['results']:
            self.assertIsNone(row['data'])
            self.assertEqual(row['captured_at'], SECOND)
            self.assertEqual(row['last_failure']['reason'], 'credential_unavailable')

    def test_face_only_snapshot_has_six_explicit_unknown_items_and_no_complete_batch(self):
        value = self.record({'available': True, 'status': 'ok', 'data': face_data()}, kind='face')
        self.assertTrue(value['available'])
        self.assertFalse(value['data']['complete'])
        for row in value['data']['results'][1:]:
            self.assertEqual(row['status'], 'not_collected')
            self.assertIsNone(row['captured_at'])
            self.assertIsNone(row['data'])

    def test_untrusted_extra_fields_strings_and_false_ratios_never_enter_saved_projection(self):
        result = batch_result()
        for row in result['results']:
            row.update(token=PRIVATE, path=PRIVATE, native={'url': PRIVATE})
            row['data'].update(token=PRIVATE, user_name=PRIVATE, files=[PRIVATE])
        result['results'][0]['data'].update(picture_completion_ratio=.99,
            estimated_remaining={'value': 2, 'unit': PRIVATE, 'token': PRIVATE})
        result['results'][3]['data']['completion_ratio'] = 1
        value = self.record(result)
        self.assertNotIn(PRIVATE, self.path.read_text(encoding='utf-8'))
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertEqual(self.item(value, 'face')['data']['picture_completion_ratio'], 4 / 6)
        self.assertIsNone(self.item(value, 'face')['data']['estimated_remaining']['unit'])
        self.assertIsNone(self.item(value, 'similar')['data']['completion_ratio'])

    def test_bad_item_shapes_unknown_reasons_and_duplicates_are_isolated(self):
        for changed in (None, {'id': 'ocr', 'status': []},
                        {'id': 'ocr', 'status': 'ok', 'query_success': True, 'available': True, 'data': PRIVATE},
                        {'id': 'ocr', 'status': 'native_error', 'reason': PRIVATE, 'data': None}):
            result = batch_result()
            result['results'][1] = changed
            with self.subTest(shape=type(changed).__name__):
                value = self.record(result)
                self.assertEqual(self.item(value, 'ocr')['status'], 'invalid_response' if isinstance(changed, dict)
                                 and changed.get('status') == 'ok' else 'native_error' if isinstance(changed, dict)
                                 and changed.get('status') == 'native_error' else 'unavailable')
                self.assertFalse(self.item(value, 'ocr')['available'])
                self.assertTrue(self.item(value, 'face')['available'])
                self.assertNotIn(PRIVATE, json.dumps(value))
        result = batch_result()
        result['results'][-1] = copy.deepcopy(result['results'][1])
        value = self.record(result)
        self.assertFalse(self.item(value, 'ocr')['available'])
        self.assertFalse(self.item(value, 'shared_album')['available'])
        self.assertTrue(self.item(value, 'face')['available'])

    def test_negative_boolean_nonfinite_and_out_of_range_data_remain_unknown(self):
        for bad in (-1, True, float('nan'), float('inf'), 2 ** 53, PRIVATE):
            result = batch_result()
            result['results'][0]['data']['totalpic'] = bad
            result['results'][1]['data']['remainpic'] = bad
            result['results'][4]['data']['recognition_range_count'] = bad
            with self.subTest(value_type=type(bad).__name__):
                value = self.record(result)
                self.assertIsNone(self.item(value, 'face')['data']['totalpic'])
                self.assertIsNone(self.item(value, 'ocr')['data']['remainpic'])
                self.assertIsNone(self.item(value, 'ai')['data']['recognition_range_count'])
                self.assertEqual(self.item(value, 'face')['status'], 'partial')
                self.assertTrue(self.item(value, 'face')['last_failure'])
                self.assertFalse(value['data']['complete'])
        result = batch_result()
        result['results'][4]['data']['recognition_range_count'] = 257
        value = self.record(result)
        self.assertIsNone(self.item(value, 'ai')['data']['recognition_range_count'])

    def test_zero_totals_unknown_native_enums_and_overcomplete_counts_do_not_invent_progress(self):
        result = batch_result()
        result['results'][0]['data'].update(totalpic=0, processedpic=0, upgrade_status=99,
                                           range_status=1, sync_percent=101)
        result['results'][1]['data'].update(totalpic=1, processedpic=2)
        result['results'][-1]['data']['native_status'] = 99
        value = self.record(result)
        face = self.item(value, 'face')['data']
        for field in ('picture_completion_ratio', 'upgrade_status', 'range_status', 'sync_percent'):
            self.assertIsNone(face[field])
        self.assertIsNone(self.item(value, 'ocr')['data']['picture_completion_ratio'])
        self.assertIsNone(self.item(value, 'shared_album')['data']['native_status'])
        self.assertFalse(value['data']['complete'])

    def test_saved_signed_native_status_remains_a_value_without_an_invented_state_label(self):
        result = batch_result()
        result['results'][4]['data']['configuration_status'] = -7
        result['results'][5]['data']['native_status'] = -3
        value = self.record(result)
        self.assertEqual(self.item(value, 'ai')['data']['configuration_status'], -7)
        self.assertEqual(self.item(value, 'scraper')['data']['native_status'], -3)
        self.assertNotIn('running', json.dumps(value))

    def test_reads_never_import_session_transport_or_touch_any_other_file(self):
        self.record()
        original_import, original_open = builtins.__import__, os.open
        opened = []
        def restricted_import(name, *args, **kwargs):
            if name in ('vendor_media_auth', 'vendor_media_probe', 'sqlite3', 'ssl'):
                raise AssertionError('native dependencies forbidden')
            return original_import(name, *args, **kwargs)
        def open_cache(path, *args, **kwargs):
            opened.append(Path(path))
            self.assertEqual(Path(path), self.path)
            return original_open(path, *args, **kwargs)
        before = self.path.read_bytes()
        with mock.patch('builtins.__import__', side_effect=restricted_import), \
                mock.patch.object(snapshot.os, 'open', side_effect=open_cache):
            value = snapshot.collect_media_snapshot(self.directory, root='/never-read-native-files')
        self.assertTrue(value['available'])
        self.assertEqual(opened, [self.path])
        self.assertEqual(self.path.read_bytes(), before)

    def test_reads_and_restarts_preserve_capture_time_and_return_detached_results(self):
        original = self.record()
        before = self.path.read_bytes()
        original['data']['results'][0]['data']['processedpic'] = 999
        self.clock.return_value = THIRD
        with mock.patch.object(snapshot, '_stamp', side_effect=AssertionError('passive read has no new capture')):
            later = snapshot.collect_media_snapshot(self.directory)
        self.assertEqual(later['data']['captured_at'], FIRST)
        self.assertEqual(self.item(later, 'face')['data']['processedpic'], 4)
        self.assertFalse(later['data']['live'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_corrupted_cached_item_is_isolated_without_returning_its_private_fields(self):
        self.record()
        state = json.loads(self.path.read_text(encoding='utf-8'))
        state['results'][1]['data']['token'] = PRIVATE
        self.write_state(state)
        before = self.path.read_bytes()
        value = snapshot.collect_media_snapshot(self.directory)
        self.assertFalse(self.item(value, 'ocr')['available'])
        self.assertEqual(self.item(value, 'ocr')['reason'], 'cache_item_invalid')
        self.assertTrue(self.item(value, 'face')['available'])
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertFalse(value['data']['complete'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_file_envelope_duplicate_json_fields_nonfinite_utf8_and_oversize_are_unavailable(self):
        self.record()
        valid = json.loads(self.path.read_text(encoding='utf-8'))
        bad_states = ([], {**valid, 'version': True}, {**valid, 'version': 2},
                      {**valid, 'captured_at': PRIVATE}, {**valid, 'kind': PRIVATE},
                      {**valid, 'results': []}, {**valid, 'token': PRIVATE})
        raw_values = [json.dumps(value).encode('utf-8') for value in bad_states]
        raw_values += [b'{', b'\xff', b'{"version":1,"version":1}',
                       b'{"version":NaN}', b'x' * (snapshot._MAX_BYTES + 1)]
        for raw in raw_values:
            with self.subTest(length=len(raw)):
                self.path.write_bytes(raw)
                os.chmod(self.path, 0o600)
                value = snapshot.collect_media_snapshot(self.directory)
                self.assertFalse(value['available'])
                self.assertEqual(value['status'], 'cache_invalid')
                self.assertIsNone(value['data'])
                self.assertNotIn(PRIVATE, json.dumps(value))
                self.assertEqual(self.path.read_bytes(), raw)

    def test_directory_state_target_is_not_overwritten_or_used_as_a_cache(self):
        self.path.mkdir()
        result = snapshot.record_media_snapshot(self.directory, batch_result())
        self.assertFalse(result['stored'])
        self.assertTrue(self.path.is_dir())
        self.assertFalse(snapshot.collect_media_snapshot(self.directory)['available'])

    @unittest.skipIf(os.name == 'nt', 'Unix private permissions, symlinks and FIFO')
    def test_insecure_permissions_symlinks_and_fifo_are_rejected_without_following(self):
        self.record()
        os.chmod(self.path, 0o644)
        self.assertFalse(snapshot.collect_media_snapshot(self.directory)['available'])
        os.chmod(self.path, 0o600)
        target = self.directory / 'real-media-cache.json'
        self.path.rename(target)
        original = target.read_bytes()
        self.path.symlink_to(target)
        self.assertFalse(snapshot.collect_media_snapshot(self.directory)['available'])
        self.assertFalse(snapshot.record_media_snapshot(self.directory, batch_result())['stored'])
        self.assertEqual(target.read_bytes(), original)
        self.path.unlink()
        self.path.symlink_to(self.directory / 'missing-media-cache.json')
        self.assertFalse(snapshot.record_media_snapshot(self.directory, batch_result())['stored'])
        self.assertEqual(snapshot.collect_media_snapshot(self.directory)['status'], 'cache_invalid')
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertFalse(snapshot.collect_media_snapshot(self.directory)['available'])
        self.assertFalse(snapshot.record_media_snapshot(self.directory, batch_result())['stored'])

    def test_atomic_write_failure_returns_safe_receipt_keeps_previous_bytes_and_cleans_temp(self):
        self.record()
        before = self.path.read_bytes()
        self.clock.return_value = SECOND
        with mock.patch.object(snapshot.os, 'replace', side_effect=OSError(PRIVATE)):
            receipt = snapshot.record_media_snapshot(self.directory, batch_result())
        self.assertFalse(receipt['stored'])
        self.assertNotIn(PRIVATE, json.dumps(receipt))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob('.media-snapshot-*')), [])
        self.assertEqual(snapshot.collect_media_snapshot(self.directory)['data']['captured_at'], FIRST)

    def test_invalid_kind_is_rejected_without_file_writes_or_native_fallback(self):
        for kind in ('all', None, [], True):
            with self.subTest(kind=type(kind).__name__), self.assertRaises(ValueError):
                snapshot.record_media_snapshot(self.directory, batch_result(), kind)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()

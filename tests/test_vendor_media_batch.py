"""Fixed media batch fixtures; no native credentials, sockets or NAS calls."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import vendor_media_batch as batch
from vendor_media_probe import MediaProbeError


SECRET = 'private-media-secret-should-never-leave-host'
ALBUM_ID = 'share_5000'
SAFE_CREDENTIAL = {
    'available': True, 'status': 'ready', 'reason': None, 'user_id': 100000,
    'records_checked': 3, 'candidate_count': 2,
    'selection': 'newest_in_vendor_window',
}


class Session:
    user_id = 100000
    source_id = SECRET
    base_token = SECRET

    def __init__(self):
        self.signed = []

    def derive_access_token(self):
        signed = SECRET + ':request-' + str(len(self.signed) + 1)
        self.signed.append(signed)
        return signed


def frames():
    """Only audited getters; private additions test the output allowlist."""
    private = {
        'msg': SECRET, 'access_token': SECRET, 'user_name': SECRET,
        'path': '/private/' + SECRET, 'source_id': SECRET,
        'signed_url': 'https://127.0.0.1/?access_token=' + SECRET,
    }
    result = {
        'face': {'code': 0, 'total': 12, 'totalpic': 10, 'totalvid': 2,
                 'processed': 6, 'processedpic': 5, 'processedvid': 1,
                 'left': 1, 'status': 0, 'range_status': 0, 'percent': 100},
        'ocr': {'code': 0, 'totalpic': 10, 'processedpic': 5, 'remainpic': 5},
        'thumbnail': {'b': 1, 'st': 2, 'pt': 3, 'sc': 4, 'cc': 5},
        'similar': {'code': 0, 'totalpic': 5, 'processedpic': 0, 'remainpic': 5},
        'ai': {'code': 0, 'status': 1,
               'list': [{'path': '/private/' + SECRET, 'user_id': 100000}]},
        'scraper': {'code': 0, 'scaned': 2, 'total': 4, 'timestamp': 123, 'num': 1, 'status': 2},
        'shared_album_catalog': {'code': 0, 'share_album_list': [
            {'creater': 0, 'album_id': 'share_6000', 'type': 'share', 'name': SECRET},
            {'creater': 1, 'album_id': ALBUM_ID, 'type': 'share', 'name': SECRET,
             'path': '/private/' + SECRET, 'user_name': SECRET},
        ]},
        'shared_album': {'scan_num': 7, 'failed_num': 0, 'await_num': 2, 'status': 1},
    }
    for frame in result.values():
        frame.update(private)
    return result


class MediaBatchTests(unittest.TestCase):
    def setUp(self):
        self.frames = frames()
        self.session = Session()
        self.reader = Mock(return_value=(self.session, deepcopy(SAFE_CREDENTIAL)))
        self.calls = []
        self.overrides = {}

    def transport(self, token, query_id, *, root='/', parameters=None):
        self.calls.append((token, query_id, root, deepcopy(parameters)))
        override = self.overrides.get(query_id)
        if isinstance(override, BaseException):
            raise override
        if override is not None:
            return override
        return 200, deepcopy(self.frames[query_id])

    def collect(self, reader=None):
        return batch.collect_media_batch(root='fixture-root',
                                         session_reader=reader or self.reader,
                                         transport=self.transport)

    @staticmethod
    def item(result, query_id):
        return next(row for row in result['results'] if row['id'] == query_id)

    def assert_private(self, result):
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False)
        for private in (SECRET, ALBUM_ID, 'share_6000', '/private/', 'request-',
                        'signed_url', 'user_name', 'source_id', 'access_token'):
            self.assertNotIn(private, encoded)

    def test_all_seven_queries_succeed_with_eight_fixed_native_calls(self):
        result = self.collect()
        self.reader.assert_called_once_with(root='fixture-root')
        self.assertEqual([row['id'] for row in result['results']], list(batch.QUERY_IDS))
        self.assertEqual([call[1] for call in self.calls],
                         ['face', 'ocr', 'thumbnail', 'similar', 'ai', 'scraper',
                          'shared_album_catalog', 'shared_album'])
        self.assertEqual(len(self.session.signed), 8)
        self.assertEqual([call[0] for call in self.calls], self.session.signed)
        self.assertEqual(len(set(self.session.signed)), 8)
        self.assertTrue(all(call[2] == 'fixture-root' for call in self.calls))
        self.assertTrue(all(call[3] is None for call in self.calls[:-1]))
        self.assertEqual(self.calls[-1][3], {'album_id': ALBUM_ID, 'type': 'share'})
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['available'])
        self.assertTrue(result['all_queries_succeeded'])
        self.assertEqual(result['scope'], {'user_id': 100000})
        self.assertEqual(result['coverage'],
                         {'total': 7, 'attempted': 7, 'succeeded': 7, 'not_applicable': 0,
                          'data_available': 7, 'data_complete': 7})
        self.assertTrue(result['credential_read_only'])
        self.assertTrue(result['native_query_may_update_state'])
        self.assertTrue(all(item['native']['first_frame_only'] for item in result['results']))
        self.assertTrue(all(type(item['elapsed_ms']) is int for item in result['results']))
        self.assert_private(result)

    def test_private_fields_from_credentials_and_native_frames_are_omitted(self):
        credential = deepcopy(SAFE_CREDENTIAL)
        credential.update(token=SECRET, source_id=SECRET, user_name=SECRET, sql=SECRET)
        result = self.collect(Mock(return_value=(self.session, credential)))
        self.assert_private(result)
        self.assertEqual(set(result['credential']), set(SAFE_CREDENTIAL))
        self.assertEqual(result['discovery']['owner_album_count'], 1)
        self.assertTrue(result['discovery']['selected_existing_album'])

    def test_missing_session_never_signs_or_calls_network(self):
        result = self.collect(Mock(return_value=(None, {
            'available': False, 'status': 'no_session', 'reason': SECRET, 'user_id': 100000})))
        self.assertFalse(result['available'])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['coverage']['attempted'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.session.signed, [])
        self.assertIsNone(result['discovery'])
        self.assertTrue(all(item['reason'] == 'credential_unavailable' for item in result['results']))
        self.assert_private(result)

    def test_reader_exception_is_sanitized_and_never_calls_network(self):
        result = self.collect(Mock(side_effect=RuntimeError(SECRET)))
        self.assertEqual(self.calls, [])
        self.assertEqual(result['credential']['reason'], 'credential_read_failed')
        self.assertEqual(result['scope'], {'user_id': 100000})
        self.assert_private(result)

    def test_owner_must_match_both_private_session_and_public_metadata(self):
        for session_id, credential_id in ((100001, 100000), (100000, 100001),
                                           ('100000', 100000), (True, 100000)):
            with self.subTest(session_id=session_id, credential_id=credential_id):
                session = Session()
                session.user_id = session_id
                credential = deepcopy(SAFE_CREDENTIAL)
                credential['user_id'] = credential_id
                result = self.collect(Mock(return_value=(session, credential)))
                self.assertEqual(self.calls, [])
                self.assertFalse(result['available'])
                self.assertEqual(result['scope'], {'user_id': 100000})

    def test_credential_requires_available_true_and_ready_status(self):
        for available, status in ((False, 'ready'), (True, 'no_session'), (1, 'ready'), (True, SECRET)):
            with self.subTest(available=available, status=status):
                credential = deepcopy(SAFE_CREDENTIAL)
                credential.update(available=available, status=status)
                result = self.collect(Mock(return_value=(self.session, credential)))
                self.assertEqual(self.calls, [])
                self.assertFalse(result['all_queries_succeeded'])
                self.assert_private(result)

    def test_timeout_is_isolated_and_other_queries_are_not_retried(self):
        self.overrides['ocr'] = MediaProbeError('timeout')
        result = self.collect()
        row = self.item(result, 'ocr')
        self.assertTrue(row['attempted'])
        self.assertFalse(row['query_success'])
        self.assertEqual(row['reason'], 'timeout')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['coverage']['succeeded'], 6)
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(sum(call[1] == 'ocr' for call in self.calls), 1)
        self.assertTrue(self.item(result, 'shared_album')['query_success'])

    def test_native_auth_rejection_is_isolated_without_changing_session(self):
        self.overrides['face'] = (200, {'code': -1002, 'msg': SECRET, 'token': SECRET})
        result = self.collect()
        row = self.item(result, 'face')
        self.assertFalse(row['query_success'])
        self.assertEqual(row['reason'], 'native_request_rejected')
        self.assertEqual(row['native']['code'], -1002)
        self.assertEqual(len(self.calls), 8)
        self.reader.assert_called_once_with(root='fixture-root')
        self.assertEqual(result['coverage']['succeeded'], 6)
        self.assert_private(result)

    def test_unexpected_transport_exception_never_leaks_details(self):
        self.overrides['thumbnail'] = RuntimeError(SECRET + ALBUM_ID)
        result = self.collect()
        row = self.item(result, 'thumbnail')
        self.assertEqual(row['reason'], 'transport_failed')
        self.assertFalse(row['available'])
        self.assertEqual(len(self.calls), 8)
        self.assert_private(result)

    def test_invalid_signature_stays_local_and_does_not_abort_batch(self):
        self.session.derive_access_token = Mock(return_value=123)
        result = self.collect()
        self.assertEqual(self.calls, [])
        self.assertFalse(result['available'])
        self.assertEqual(self.item(result, 'face')['reason'], 'token_unavailable')
        self.assertEqual(result['discovery']['reason'], 'token_unavailable')

    def test_no_existing_owner_album_is_not_applicable_without_fabrication(self):
        self.frames['shared_album_catalog']['share_album_list'] = [
            {'creater': 0, 'album_id': 'share_6000', 'type': 'share', 'name': SECRET}]
        result = self.collect()
        row = self.item(result, 'shared_album')
        self.assertEqual(row['status'], 'not_applicable')
        self.assertEqual(row['reason'], 'no_existing_owner_album')
        self.assertFalse(row['attempted'])
        self.assertFalse(row['query_success'])
        self.assertEqual(result['coverage']['not_applicable'], 1)
        self.assertEqual(result['coverage']['attempted'], 6)
        self.assertEqual(len(self.calls), 7)
        self.assertFalse(result['all_queries_succeeded'])
        self.assertEqual(result['discovery']['owner_album_count'], 0)
        self.assertFalse(result['discovery']['selected_existing_album'])

    def test_empty_album_catalog_is_not_applicable(self):
        self.frames['shared_album_catalog']['share_album_list'] = []
        result = self.collect()
        self.assertEqual(self.item(result, 'shared_album')['status'], 'not_applicable')
        self.assertEqual(len(self.calls), 7)

    def test_catalog_failure_never_calls_shared_album(self):
        cases = (
            MediaProbeError('timeout'),
            (200, {'code': -1, 'share_album_list': self.frames['shared_album_catalog']['share_album_list']}),
            (403, {'code': 0, 'share_album_list': []}),
            (200, {'share_album_list': []}),
            (200, {'code': 0, 'share_album_list': {}}),
            (200, {'code': 0, 'share_album_list': [None] * 257}),
        )
        for response in cases:
            with self.subTest(response=type(response).__name__):
                self.calls.clear()
                self.overrides['shared_album_catalog'] = response
                result = self.collect()
                self.assertFalse(result['discovery']['query_success'])
                self.assertFalse(self.item(result, 'shared_album')['attempted'])
                self.assertNotIn('shared_album', [call[1] for call in self.calls])
                self.assert_private(result)

    def test_conflicting_selected_album_context_is_not_queried(self):
        self.frames['shared_album_catalog']['share_album_list'].append(
            {'creater': 1, 'album_id': ALBUM_ID, 'type': 'different'})
        result = self.collect()
        self.assertEqual(result['discovery']['status'], 'invalid_response')
        self.assertEqual(result['discovery']['reason'], 'album_context_ambiguous')
        self.assertFalse(self.item(result, 'shared_album')['attempted'])
        self.assertEqual(len(self.calls), 7)
        self.assert_private(result)

    def test_malformed_album_rows_are_never_used_as_private_parameters(self):
        bad_rows = (
            None, {}, {'creater': True, 'album_id': ALBUM_ID, 'type': 'share'},
            {'creater': '1', 'album_id': ALBUM_ID, 'type': 'share'},
            {'creater': 1, 'album_id': '../private', 'type': 'share'},
            {'creater': 1, 'album_id': 'share_secret', 'type': 'share'},
            {'creater': 1, 'album_id': ALBUM_ID, 'type': '\nshare'},
            {'creater': 1, 'album_id': ALBUM_ID, 'type': '\ud800'},
            {'creater': 1, 'album_id': ALBUM_ID, 'type': 'x' * 32},
            {'creater': 1, 'album_id': 'share_' + '1' * 11, 'type': 'share'},
        )
        for row in bad_rows:
            with self.subTest(row=repr(row)):
                self.calls.clear()
                self.frames['shared_album_catalog']['share_album_list'] = [row]
                result = self.collect()
                self.assertFalse(self.item(result, 'shared_album')['attempted'])
                self.assertNotIn('shared_album', [call[1] for call in self.calls])

    def test_codeless_success_requires_complete_bounded_integer_schema(self):
        for query_id, key in (('thumbnail', 'pt'), ('shared_album', 'scan_num')):
            original = deepcopy(self.frames[query_id])
            for value in (None, True, 1.0, '1', 2 ** 53, float('nan'), float('inf')):
                with self.subTest(query_id=query_id, value=repr(value)):
                    self.frames[query_id] = deepcopy(original)
                    self.frames[query_id][key] = value
                    result = self.collect()
                    row = self.item(result, query_id)
                    self.assertFalse(row['query_success'])
                    self.assertFalse(row['available'])
                    self.assertEqual(row['status'], 'invalid_response')
                    self.assertIsNone(row['data'])
            self.frames[query_id] = deepcopy(original)
            del self.frames[query_id][key]
            result = self.collect()
            self.assertFalse(self.item(result, query_id)['query_success'])
            self.frames[query_id] = original

    def test_codeless_negative_counters_are_unknown_not_fabricated_zero(self):
        for query_id, key in (('thumbnail', 'pt'), ('shared_album', 'scan_num')):
            with self.subTest(query_id=query_id):
                original = self.frames[query_id][key]
                self.frames[query_id][key] = -1
                result = self.collect()
                row = self.item(result, query_id)
                self.assertTrue(row['query_success'])
                self.assertEqual(row['status'], 'partial')
                self.assertIsNone(row['data'][key])
                self.frames[query_id][key] = original

    def test_shared_album_unknown_status_is_not_interpreted_as_running(self):
        self.frames['shared_album']['status'] = 12
        result = self.collect()
        row = self.item(result, 'shared_album')
        self.assertEqual(row['status'], 'partial')
        self.assertIsNone(row['data']['native_status'])
        self.assertNotIn('running', row['data'])

    def test_queries_with_required_code_do_not_accept_missing_or_invalid_codes(self):
        for query_id in ('face', 'ocr', 'similar', 'ai', 'scraper'):
            original = deepcopy(self.frames[query_id])
            for code in (None, True, 0.0, '0', 2 ** 53):
                with self.subTest(query_id=query_id, code=repr(code)):
                    self.frames[query_id] = deepcopy(original)
                    self.frames[query_id]['code'] = code
                    result = self.collect()
                    row = self.item(result, query_id)
                    self.assertFalse(row['query_success'])
                    self.assertEqual(row['status'], 'invalid_response')
                    self.assertEqual(row['reason'], 'native_code_unavailable')
            self.frames[query_id] = deepcopy(original)
            del self.frames[query_id]['code']
            self.assertFalse(self.item(self.collect(), query_id)['query_success'])
            self.frames[query_id] = original

    def test_codeless_getters_still_honor_explicit_native_error_codes(self):
        for query_id in ('thumbnail', 'shared_album'):
            with self.subTest(query_id=query_id):
                self.frames[query_id]['code'] = -2
                row = self.item(self.collect(), query_id)
                self.assertFalse(row['query_success'])
                self.assertEqual(row['reason'], 'native_request_rejected')
                del self.frames[query_id]['code']

    def test_transport_shape_and_http_failures_are_not_success(self):
        for response in ((True, {}), ('200', {}), (200, []), (99, {}),
                         (302, {'code': 0}), (500, {'code': 0}), (200, None)):
            with self.subTest(response=response):
                self.overrides['ocr'] = response
                row = self.item(self.collect(), 'ocr')
                self.assertFalse(row['query_success'])
                self.assertFalse(row['available'])
                self.assertIsNone(row['data'])

    def test_invalid_counts_preserve_unknowns_and_partial_status(self):
        self.frames['face'].update(total=-1, totalpic=True, totalvid='2', processed=2 ** 53,
                                   processedpic=float('nan'), processedvid=-1)
        self.frames['ocr'].update(totalpic='10', processedpic=-1, remainpic=True)
        result = self.collect()
        for key in ('total', 'totalpic', 'totalvid', 'processed', 'processedpic', 'processedvid'):
            self.assertIsNone(self.item(result, 'face')['data'][key])
        for key in ('totalpic', 'processedpic', 'remainpic', 'picture_completion_ratio'):
            self.assertIsNone(self.item(result, 'ocr')['data'][key])
        self.assertEqual(self.item(result, 'face')['status'], 'partial')
        self.assertEqual(self.item(result, 'ocr')['status'], 'partial')
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['all_queries_succeeded'])
        self.assertEqual(result['coverage']['data_complete'], 5)
        self.assertEqual(result['coverage']['data_available'], 5)
        json.dumps(result, allow_nan=False)

    def test_zero_denominators_and_overcomplete_counts_have_no_completion_ratio(self):
        for total, processed in ((0, 0), (5, 6)):
            with self.subTest(total=total, processed=processed):
                self.frames['face'].update(totalpic=total, processedpic=processed,
                                           totalvid=total, processedvid=processed)
                self.frames['ocr'].update(totalpic=total, processedpic=processed)
                result = self.collect()
                face = self.item(result, 'face')['data']
                self.assertIsNone(face['picture_completion_ratio'])
                self.assertIsNone(face['video_completion_ratio'])
                self.assertIsNone(self.item(result, 'ocr')['data']['picture_completion_ratio'])

    def test_similar_only_reports_queue_without_inventing_completion(self):
        result = self.collect()
        data = self.item(result, 'similar')['data']
        self.assertEqual(data['pending_hashes'], 5)
        self.assertIsNone(data['completion_ratio'])
        self.assertNotIn('percent', data)
        self.assertNotIn('picture_completion_ratio', data)

    def test_ai_only_reports_configuration_and_range_count(self):
        result = self.collect()
        data = self.item(result, 'ai')['data']
        self.assertEqual(data, {'configuration_status': 1, 'recognition_range_count': 1})
        for invalid in ({}, [None], [dict()] * 257):
            with self.subTest(invalid_type=type(invalid).__name__):
                self.frames['ai']['list'] = invalid
                row = self.item(self.collect(), 'ai')
                self.assertIsNone(row['data']['recognition_range_count'])
                self.assertEqual(row['status'], 'partial')

    def test_native_semantics_keep_unverified_units_and_state_labels_unknown(self):
        result = self.collect()
        face = self.item(result, 'face')['data']
        self.assertEqual(face['sync_percent'], 100)
        self.assertEqual(face['picture_completion_ratio'], 0.5)
        self.assertIsNone(face['estimated_remaining']['unit'])
        thumbnail = self.item(result, 'thumbnail')['data']
        self.assertEqual(set(thumbnail), {'b', 'st', 'pt', 'sc', 'cc'})
        scraper = self.item(result, 'scraper')['data']
        self.assertEqual(scraper['native_status'], 2)
        self.assertNotIn('running', scraper)
        self.assertNotIn('completion_ratio', scraper)


if __name__ == '__main__':
    unittest.main()

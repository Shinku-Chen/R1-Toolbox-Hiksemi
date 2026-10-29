"""Fixed media contracts with fake native frames; no NAS business writes."""

import importlib
import json
from pathlib import Path
import sys
import threading
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
ops = importlib.import_module('vendor_media_operations')
transport = importlib.import_module('vendor_business_transport')


PRINCIPAL = {'key_id': 'fixture-owner-key', 'grant_revision': 3, 'credential_generation': 1}


def allow(operation, parameters, body):
    return True


class Client:
    def __init__(self, frame=None, error=None):
        self.frame = frame
        self.error = error
        self.calls = []
        self.entered = None
        self.release = None

    def request(self, operation, parameters, body, authorizer, *, confirmed=False):
        self.calls.append((operation, parameters, body, confirmed))
        if self.entered is not None:
            self.entered.set()
            self.release.wait(3)
        if self.error is not None:
            raise self.error
        return 200, self.frame


class MediaOperationTests(unittest.TestCase):
    def backend(self, frame=None, error=None, maximum=1024):
        client = Client(frame, error)
        backend = ops.MediaOperations(client=client, idempotency_store=ops.IdempotencyStore(maximum))
        return backend, client

    def execute(self, backend, tool, args=None, authorizer=allow, principal=PRINCIPAL):
        return backend.execute(tool, {} if args is None else args,
                               authorizer=authorizer, principal=principal)

    def creation(self, name='旅行', key='req:1'):
        return {'name': name, 'confirmed': True, 'idempotency_key': key}

    def album(self, name='旅行', identifier='8'):
        return {'code': 0, 'album_id': identifier, 'name': name, 'type': 'custom',
                'count': 0, 'pic_url_list': [{'pic_url': '/PRIVATE-TOKEN'}], 'msg': 'SECRET'}

    def test_catalog_scopes_and_pending_actions_have_no_fake_executor(self):
        self.assertEqual(len(ops.TOOLS), 7)
        self.assertEqual(ops.TOOLS['revoke_share']['scope'], 'sharing')
        self.assertTrue(ops.TOOLS['create_album']['write'])
        backend, client = self.backend()
        for tool in ops.PENDING:
            self.assertEqual(self.execute(backend, tool)['reason'], 'unsupported_operation')
        self.assertFalse(client.calls)

    def test_album_lists_only_project_labels_identity_and_nullable_count(self):
        frame = {'code': 0, 'all_album_list': [self.album(),
                 {'album_id': '0', 'name': '最近', 'type': 'addtime', 'count': None,
                  'shareid': 'PRIVATE-SHARE', 'pic_url_list': ['/secret']}], 'msg': 'SECRET'}
        backend, client = self.backend(frame)
        result = self.execute(backend, 'list_albums')
        self.assertTrue(result['available'], result)
        self.assertEqual(result['data']['returned_count'], 2)
        self.assertTrue(result['data']['items'][0]['editable'])
        self.assertFalse(result['data']['items'][1]['editable'])
        self.assertIsNone(result['data']['items'][1]['count'])
        self.assertIsNone(result['data']['complete'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(client.calls[0], ('albums_list', {}, {}, False))

    def test_shared_albums_project_creator_without_private_birth_sex_covers(self):
        backend, _ = self.backend({'code': 0, 'share_album_list': [
            {'album_id': 'share_12', 'name': '家人', 'type': 'custom', 'count': 0,
             'creater': 1, 'birth': 'PRIVATE', 'sex': 'PRIVATE', 'pic_url1': 'PRIVATE'}]})
        result = self.execute(backend, 'list_shared_albums')
        self.assertTrue(result['available'], result)
        self.assertTrue(result['data']['items'][0]['owner_created'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_invalid_or_duplicate_list_objects_fail_closed(self):
        for rows in (None, [None], [self.album(), self.album()],
                     [{'album_id': '../8', 'name': 'x', 'type': 'custom'}],
                     [{'album_id': '8', 'name': 'x\n', 'type': 'custom'}],
                     [self.album()] * 257):
            backend, _ = self.backend({'code': 0, 'all_album_list': rows})
            result = self.execute(backend, 'list_albums')
            self.assertFalse(result['available'])
            self.assertIsNone(result['data'])

    def test_movie_list_never_reinterprets_unknown_id_or_exposes_sources(self):
        backend, _ = self.backend({'code': 0, 'list': [
            {'lid': 11, 'name': '影视', 'sources': ['/SECRET'], 'members': ['SECRET']},
            {'id': 33, 'name': '未知身份'}]})
        result = self.execute(backend, 'list_movie_libraries')
        self.assertTrue(result['available'], result)
        self.assertEqual(result['data']['items'][0]['library_id'], 11)
        self.assertIsNone(result['data']['items'][1]['library_id'])
        self.assertFalse(result['data']['items'][1]['identity_available'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_write_requires_confirmation_key_principal_and_current_authorizer(self):
        backend, client = self.backend(self.album())
        for args, auth, principal in (({'name': '旅行'}, allow, PRINCIPAL),
                ({'name': '旅行', 'confirmed': True}, allow, PRINCIPAL),
                (self.creation(), None, PRINCIPAL), (self.creation(), lambda *a: False, PRINCIPAL),
                (self.creation(), lambda *a: 1, PRINCIPAL), (self.creation(), allow, None),
                (self.creation(key='../oops'), allow, PRINCIPAL)):
            result = self.execute(backend, 'create_album', args, auth, principal)
            self.assertFalse(result['available'], result)
            self.assertEqual(result['mutation_outcome'], 'not_sent')
        self.assertFalse(client.calls)

    def test_native_validation_happens_before_authorization(self):
        backend, client = self.backend(self.album())
        auth_calls = []
        for args in ({'album_id': '7', 'name': 'x', 'confirmed': True, 'idempotency_key': 'a'},
                     {'album_id': '8', 'name': 'x&members=1', 'confirmed': True, 'idempotency_key': 'a'},
                     {'album_id': '8', 'name': 'x', 'confirmed': True, 'idempotency_key': 'a', 'url': 'x'}):
            result = self.execute(backend, 'rename_album', args,
                                  lambda *a: auth_calls.append(a) or True)
            self.assertFalse(result['available'], result)
        self.assertEqual(auth_calls, [])
        self.assertFalse(client.calls)

    def test_success_requires_native_code_and_exact_album_ack(self):
        for frame in ({}, {'code': True}, {'code': '0'}, self.album(identifier='7'),
                      self.album(name='wrong')):
            backend, client = self.backend(frame)
            result = self.execute(backend, 'create_album', self.creation())
            self.assertFalse(result['available'], result)
            self.assertEqual(result['reason'], 'result_unconfirmed')
            self.assertEqual(result['mutation_outcome'], 'unknown')
            self.assertEqual(len(client.calls), 1)
        backend, _ = self.backend({'code': -2003, 'msg': 'SECRET'})
        result = self.execute(backend, 'create_album', self.creation())
        self.assertEqual(result['status'], 'native_error')
        self.assertEqual(result['native']['code'], -2003)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_acknowledged_create_is_idempotent_and_conflict_is_rejected(self):
        backend, client = self.backend(self.album())
        first = self.execute(backend, 'create_album', self.creation())
        self.assertTrue(first['available'], first)
        self.assertEqual(first['data']['verification'], 'factory_acknowledgement_only')
        replay = self.execute(backend, 'create_album', self.creation())
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(replay['data'], first['data'])
        again = self.execute(backend, 'create_album', self.creation())
        self.assertTrue(again['available'])
        conflict = self.execute(backend, 'create_album', self.creation(name='别的'))
        self.assertEqual(conflict['reason'], 'idempotency_conflict')
        self.assertEqual(len(client.calls), 1)
        denied = self.execute(backend, 'create_album', self.creation(), lambda *a: False)
        self.assertEqual(denied['reason'], 'authorization_denied')
        self.assertIsNone(denied['data'])

    def test_unknown_write_is_retained_and_never_blindly_replayed(self):
        backend, client = self.backend(error=transport.BusinessTransportError(
            'timeout', request_sent=True))
        first = self.execute(backend, 'create_album', self.creation())
        self.assertEqual(first['reason'], 'result_unconfirmed')
        self.assertEqual(first['failure_reason'], 'timeout')
        replay = self.execute(backend, 'create_album', self.creation())
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(replay['reason'], 'result_unconfirmed')
        self.assertEqual(len(client.calls), 1)

    def test_native_rejection_and_invalid_ack_are_also_retained(self):
        for frame in ({'code': -1, 'msg': 'PRIVATE'}, self.album(name='not-requested')):
            backend, client = self.backend(frame)
            first = self.execute(backend, 'create_album', self.creation())
            replay = self.execute(backend, 'create_album', self.creation())
            self.assertFalse(first['available'])
            self.assertTrue(replay['idempotent_replay'])
            self.assertEqual(replay['reason'], first['reason'])
            self.assertEqual(len(client.calls), 1)

    def test_principal_binding_distinguishes_grant_and_credential_generations(self):
        backend, client = self.backend(self.album())
        first = self.execute(backend, 'create_album', self.creation())
        self.assertTrue(first['available'])
        other = dict(PRINCIPAL, grant_revision=4)
        self.assertTrue(self.execute(backend, 'create_album', self.creation(), principal=other)['available'])
        rotated = dict(PRINCIPAL, credential_generation=2)
        self.assertTrue(self.execute(backend, 'create_album', self.creation(), principal=rotated)['available'])
        self.assertEqual(len(client.calls), 3)
        for principal in ({}, dict(PRINCIPAL, key_id='../invalid'), dict(PRINCIPAL, grant_revision=True),
                          ('fixture', 1, 1)):
            result = self.execute(backend, 'create_album', self.creation(key='invalid'), principal=principal)
            self.assertEqual(result['reason'], 'idempotency_required')

    def test_proved_not_sent_failure_can_retry_and_capacity_does_not_evict(self):
        backend, client = self.backend(error=transport.BusinessTransportError('identity_unverified'), maximum=1)
        self.assertEqual(self.execute(backend, 'create_album', self.creation())['mutation_outcome'], 'not_sent')
        client.error, client.frame = None, self.album()
        self.assertTrue(self.execute(backend, 'create_album', self.creation())['available'])
        full = self.execute(backend, 'create_album', self.creation(key='other'))
        self.assertEqual(full['reason'], 'idempotency_capacity')
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(self.execute(backend, 'create_album', self.creation())['idempotent_replay'])

    def test_concurrent_duplicate_is_busy_without_second_native_request(self):
        backend, client = self.backend(self.album())
        client.entered, client.release = threading.Event(), threading.Event()
        results = []
        thread = threading.Thread(target=lambda: results.append(self.execute(backend, 'create_album', self.creation())))
        thread.start()
        try:
            self.assertTrue(client.entered.wait(3))
            duplicate = self.execute(backend, 'create_album', self.creation())
            self.assertEqual(duplicate['reason'], 'busy')
            self.assertEqual(len(client.calls), 1)
        finally:
            client.release.set()
            thread.join(3)
        self.assertTrue(results[0]['available'])

    def test_rename_and_single_share_revoke_use_fixed_native_fields(self):
        backend, client = self.backend({'code': 0})
        args = {'library_id': 5, 'name': '新库', 'confirmed': True, 'idempotency_key': 'movie1'}
        self.assertTrue(self.execute(backend, 'rename_movie_library', args)['available'])
        self.assertEqual(client.calls[-1], ('movie_library_rename', {}, {'lid': 5, 'name': '新库'}, True))
        args = {'share_id': 'factory-sid_12', 'confirmed': True, 'idempotency_key': 'share1'}
        self.assertTrue(self.execute(backend, 'revoke_share', args)['available'])
        self.assertEqual(client.calls[-1], ('share_revoke', {}, {'share_id_list': ['factory-sid_12']}, True))
        for sid in ('%s', '../other', 'a/b', '', 'a' * 128):
            bad = dict(args, share_id=sid, idempotency_key='bad' + str(len(sid)))
            self.assertEqual(self.execute(backend, 'revoke_share', bad)['reason'], 'invalid_parameters')

    def test_fixed_diagnostic_is_three_independent_reads_without_sample_creation(self):
        backend, client = self.backend(error=transport.BusinessTransportError('timeout'))
        result = ops.collect_media_operations_probe(client=client, authorizer=allow)
        self.assertEqual([c[0] for c in client.calls], [
            'albums_list', 'shared_albums_list', 'movie_libraries_list'])
        self.assertTrue(all(not c[3] for c in client.calls))
        self.assertEqual(len(result['items']), 3)
        self.assertTrue(result['may_wake_disks'])
        self.assertTrue(all(i['reason'] == 'timeout' for i in result['items']))


if __name__ == '__main__':
    unittest.main()

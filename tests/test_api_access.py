"""Fixture-only access key storage, authorization and secret-boundary checks."""

import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('api_access', ROOT / 'runtime/api_access.py')
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


class ApiAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1800000000
        self.store = access.ApiAccessStore(self.temp.name, clock=lambda: self.now)

    def create(self, **updates):
        return self.store.create_key({'name': '局域网 AI', **updates})

    def enable(self):
        self.store.configure({'enabled': True})

    def authorize(self, key, peer='192.168.31.22', scope='read'):
        return self.store.authorize('Bearer ' + key['token'], peer, scope)

    def write_state(self, value):
        self.store.path.write_text(json.dumps(value), encoding='utf-8')
        os.chmod(self.store.path, 0o600)

    def test_default_is_disabled_without_creating_file(self):
        value = self.store.snapshot()
        self.assertFalse(value['enabled'])
        self.assertTrue(value['storage_ok'])
        self.assertEqual(value['keys'], [])
        self.assertEqual(value['audit'], [])
        self.assertEqual([item['id'] for item in value['scope_catalog']], list(access.SCOPES))
        self.assertEqual(self.store.authorize(None, '127.0.0.1'), (None, 'disabled'))
        self.assertFalse(self.store.path.exists())

    def test_secret_only_in_creation_response_and_persistence_is_digest_only(self):
        key = self.create()
        self.assertRegex(key['token'], r'^r1_[A-Za-z0-9_-]{43}$')
        self.assertEqual(key['scopes'], ['read'])
        self.assertEqual(key['expires_at'], self.now + 90 * 86400)
        self.assertNotIn(key['token'], repr(self.store))
        persisted = self.store.path.read_text(encoding='utf-8')
        self.assertNotIn(key['token'], persisted)
        self.assertIn(hashlib.sha256(key['token'].encode('ascii')).hexdigest(), persisted)
        public = json.dumps(self.store.snapshot(), ensure_ascii=False)
        self.assertNotIn(key['token'], public)
        self.assertNotIn('digest', public)
        self.assertNotIn(hashlib.sha256(key['token'].encode('ascii')).hexdigest(), public)
        self.assertEqual(key['status'], 'active')
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)

    def test_enable_disable_revoke_and_reopen_take_effect_immediately(self):
        key = self.create()
        self.assertEqual(self.authorize(key), (None, 'disabled'))
        self.enable()
        principal, reason = self.authorize(key)
        self.assertEqual(reason, 'ok')
        self.assertEqual(principal, {'key_id': key['id'], 'name': key['name'], 'scopes': ['read']})
        other = access.ApiAccessStore(self.temp.name, clock=lambda: self.now)
        other.configure({'enabled': False})
        self.assertEqual(self.authorize(key), (None, 'disabled'))
        other.configure({'enabled': True})
        revoked = other.revoke_key(key['id'])
        self.assertEqual(revoked['status'], 'revoked')
        self.assertEqual(self.authorize(key), (None, 'key_revoked'))
        self.assertNotIn('token', revoked)
        self.assertEqual(other.revoke_key(key['id'])['revoked_at'], self.now)

    def test_exact_expiry_and_nonexpiring_key(self):
        finite = self.create(expires_days=1)
        unlimited = self.create(expires_days=0)
        self.enable()
        self.now += 86400 - 1
        self.assertEqual(self.authorize(finite)[1], 'ok')
        self.now += 1
        self.assertEqual(self.authorize(finite), (None, 'key_expired'))
        self.assertEqual(self.authorize(unlimited)[1], 'ok')
        rows = {row['id']: row for row in self.store.snapshot()['keys']}
        self.assertEqual(rows[finite['id']]['status'], 'expired')
        self.assertIsNone(rows[unlimited['id']]['expires_at'])

    def test_scopes_do_not_implicitly_grant_any_other_permission(self):
        key = self.create(scopes=['light'])
        self.enable()
        self.assertEqual(self.authorize(key, scope='light')[1], 'ok')
        self.assertEqual(self.authorize(key, scope='read'), (None, 'scope_denied'))
        self.assertEqual(self.authorize(key, scope='power'), (None, 'scope_denied'))
        self.assertEqual(self.authorize(key, scope='execute'), (None, 'invalid_scope'))
        self.assertEqual(self.authorize(key, scope=None)[1], 'ok')

    def test_lan_only_exact_allowlist_and_mapped_normalization(self):
        key = self.create(allowed_ips=['::ffff:192.168.31.22', 'fd00:1234::1'])
        self.assertEqual(key['allowed_ips'], ['192.168.31.22', 'fd00:1234::1'])
        self.enable()
        for peer in ('192.168.31.22', '::ffff:192.168.31.22', 'fd00:1234::1'):
            with self.subTest(peer=peer):
                self.assertEqual(self.authorize(key, peer)[1], 'ok')
        self.assertEqual(self.authorize(key, '192.168.31.23'), (None, 'peer_not_allowed'))
        for peer in ('8.8.8.8', '100.64.0.1', '169.254.1.1', '0.0.0.0', 'fe80::1',
                     '2001:db8::1', '::', 'localhost', '192.168.31.0/24', 'fd00::1%eth0',
                     ' 127.0.0.1', '127.0.0.1:80', None, ['127.0.0.1']):
            with self.subTest(peer=peer):
                self.assertEqual(self.authorize(key, peer), (None, 'peer_not_lan'))
        any_lan = self.create()
        for peer in ('127.0.0.1', '::1', '10.0.0.2', '172.16.0.1', '172.31.255.254',
                     '192.168.0.1', 'fc00::1', 'fdff::ffff'):
            with self.subTest(peer=peer):
                self.assertEqual(self.authorize(any_lan, peer)[1], 'ok')
        self.assertEqual(self.authorize(any_lan, '172.32.0.1'), (None, 'peer_not_lan'))

    def test_invalid_create_bodies_and_no_input_reflection(self):
        bodies = (None, [], {}, {'name': ''}, {'name': ' a'}, {'name': 'a\n'},
                  {'name': 'x' * 65}, {'name': 3}, {'name': 'a', 'token': 'sensitive'},
                  {'name': 'a', 'scopes': []}, {'name': 'a', 'scopes': 'read'},
                  {'name': 'a', 'scopes': ['read', 'read']}, {'name': 'a', 'scopes': ['unknown']},
                  {'name': 'a', 'scopes': [[]]}, {'name': 'a', 'scopes': [True]},
                  {'name': 'a', 'allowed_ips': ['8.8.8.8']},
                  {'name': 'a', 'allowed_ips': ['192.168.1.0/24']},
                  {'name': 'a', 'allowed_ips': ['127.0.0.1', '::ffff:127.0.0.1']},
                  {'name': 'a', 'allowed_ips': [None]}, {'name': 'a', 'allowed_ips': {}},
                  {'name': 'a', 'allowed_ips': ['127.0.0.1'] * 33},
                  *({'name': 'a', 'expires_days': value} for value in (
                      True, None, -1, 366, '90', 1.0, float('nan'), float('inf'), 10**1000)))
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(ValueError) as raised:
                    self.store.create_key(body)
                self.assertNotIn('sensitive', str(raised.exception))
        self.assertFalse(self.store.path.exists())

    def test_configure_and_revoke_strict_fields(self):
        for body in ({}, {'enabled': 1}, {'enabled': 'true'}, {'enabled': True, 'token': 'sensitive'}, None):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.store.configure(body)
        for value in (None, True, '', '1' * 23, '0' * 25, ['0' * 24], '0' * 24):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.revoke_key(value)

    def test_bearer_is_bounded_and_strict(self):
        key = self.create()
        self.enable()
        self.assertEqual(self.store.authorize('bEaReR ' + key['token'], '127.0.0.1')[1], 'ok')
        for header in (None, {}, 'Basic ' + key['token'], 'Bearer  ' + key['token'],
                       'Bearer\t' + key['token'], 'Bearer ' + key['token'] + '\n',
                       'Bearer ' + key['token'][:-1], 'Bearer ' + 'r1_' + 'a' * 44,
                       'Bearer ' + 'x' * 1000000, 'Bearer ' + 'r1_' + '中' * 43):
            with self.subTest(header_type=type(header).__name__):
                self.assertEqual(self.store.authorize(header, '127.0.0.1'), (None, 'invalid_key'))

    def test_constant_time_digest_comparison_covers_all_records(self):
        first = self.create()
        self.create(name='另一客户端')
        self.enable()
        original = access.hmac.compare_digest
        with mock.patch.object(access.hmac, 'compare_digest', wraps=original) as compare:
            self.assertEqual(self.authorize(first)[1], 'ok')
        self.assertEqual(compare.call_count, 2)

    def test_corrupt_or_missing_state_fails_closed_and_does_not_overwrite(self):
        key = self.create()
        self.enable()
        self.store.path.write_text('{invalid sensitive}', encoding='utf-8')
        self.assertEqual(self.authorize(key), (None, 'storage_unavailable'))
        snapshot = self.store.snapshot()
        self.assertFalse(snapshot['enabled'])
        self.assertFalse(snapshot['storage_ok'])
        self.assertEqual(snapshot['keys'], [])
        self.assertNotIn('sensitive', repr(snapshot))
        with self.assertRaises(OSError):
            self.store.configure({'enabled': True})
        self.assertEqual(self.store.path.read_text(encoding='utf-8'), '{invalid sensitive}')
        self.store.path.unlink()
        self.assertEqual(self.authorize(key), (None, 'disabled'))

    def test_malformed_persistent_schema_is_entirely_rejected(self):
        key = self.create()
        self.enable()
        valid = json.loads(self.store.path.read_text(encoding='utf-8'))
        variants = ([], {'version': 1, 'enabled': True, 'keys': [], 'audit': [], 'token': key['token']},
                    {**valid, 'version': True}, {**valid, 'enabled': 1},
                    {**valid, 'keys': valid['keys'] * 2},
                    {**valid, 'keys': [{**valid['keys'][0], 'scopes': ['unknown']}]},
                    {**valid, 'keys': [{**valid['keys'][0], 'allowed_ips': ['8.8.8.8']}]},
                    {**valid, 'keys': [{**valid['keys'][0], 'digest': 'invalid'}]},
                    {**valid, 'keys': [{**valid['keys'][0], 'created_at': True}]},
                    {**valid, 'audit': [{'operation': 'shell'}]},
                    {**valid, 'keys': valid['keys'] * 65})
        for value in variants:
            with self.subTest(value_type=type(value).__name__):
                self.write_state(value)
                self.assertEqual(self.authorize(key), (None, 'storage_unavailable'))
                self.assertFalse(self.store.snapshot()['storage_ok'])
        for text in ('{"version":1,"enabled":true,"enabled":false,"keys":[],"audit":[]}',
                     '{"version":1,"enabled":NaN,"keys":[],"audit":[]}', 'x' * 262145):
            self.store.path.write_text(text, encoding='utf-8')
            self.assertFalse(self.store.snapshot()['storage_ok'])

    def test_failed_atomic_replace_preserves_file_and_latches_authorization_closed(self):
        key = self.create()
        self.enable()
        before = self.store.path.read_bytes()
        with mock.patch.object(access.os, 'replace', side_effect=OSError('sensitive')):
            with self.assertRaises(OSError) as raised:
                self.store.configure({'enabled': False})
        self.assertNotIn('sensitive', str(raised.exception))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.authorize(key), (None, 'storage_unavailable'))
        self.assertEqual(list(Path(self.temp.name).glob('.api-access-*')), [])

    @unittest.skipIf(os.name == 'nt', 'Unix permission and symlink safety')
    def test_insecure_permissions_symlink_and_special_files_are_rejected(self):
        key = self.create()
        self.enable()
        os.chmod(self.store.path, 0o644)
        self.assertEqual(self.authorize(key), (None, 'storage_unavailable'))
        os.chmod(self.store.path, 0o600)
        target = self.store.path.with_name('real.json')
        self.store.path.rename(target)
        self.store.path.symlink_to(target)
        self.assertEqual(self.authorize(key), (None, 'storage_unavailable'))
        self.store.path.unlink()
        os.mkfifo(self.store.path)
        self.assertEqual(self.authorize(key), (None, 'storage_unavailable'))

    def test_active_limit_and_inactive_history_bound(self):
        for number in range(32):
            self.create(name='客户端%d' % number)
        with self.assertRaises(ValueError):
            self.create()
        first_id = self.store.snapshot()['keys'][0]['id']
        self.store.revoke_key(first_id)
        for number in range(40):
            key = self.create(name='历史%d' % number)
            self.store.revoke_key(key['id'])
        rows = self.store.snapshot()['keys']
        self.assertEqual(len(rows), 64)
        self.assertEqual(sum(row['status'] == 'active' for row in rows), 31)
        self.assertNotIn(first_id, [row['id'] for row in rows])

    def test_audit_is_bounded_sanitized_and_snapshot_is_detached(self):
        key = self.create()
        self.enable()
        principal, _ = self.authorize(key)
        for number in range(105):
            self.now += 1
            self.store.record(principal, 'get_nas_status', '::ffff:192.168.31.22', 'success', 12.9)
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot['audit']), 100)
        self.assertEqual(snapshot['audit'][0], {
            'timestamp': self.now, 'key_id': key['id'], 'operation': 'get_nas_status',
            'peer': '192.168.31.22', 'outcome': 'success', 'duration_ms': 12,
        })
        self.assertEqual(snapshot['keys'][0]['last_used_at'], self.now)
        self.assertNotIn(key['token'], json.dumps(snapshot))
        snapshot['keys'][0]['scopes'].append('power')
        snapshot['scope_catalog'][0]['label'] = 'modified'
        snapshot['audit'][0]['operation'] = 'modified'
        self.assertEqual(self.store.snapshot()['keys'][0]['scopes'], ['read'])
        self.assertNotEqual(self.store.snapshot()['scope_catalog'][0]['label'], 'modified')
        self.store.record({'key_id': key['token'], 'token': key['token']}, 'authenticate', key['token'], 'denied')
        last = self.store.snapshot()['audit'][0]
        self.assertIsNone(last['key_id'])
        self.assertIsNone(last['peer'])

    def test_audit_rejects_unknown_operations_outcomes_or_bad_numbers(self):
        for operation, outcome, duration in (
                ('shell sensitive', 'success', 0), ('tools/call', 'sensitive', 0),
                ([], 'success', 0), ('ping', [], 0),
                *(('ping', 'success', value) for value in (
                    True, None, -1, 3600001, float('nan'), float('inf'), 10**1000))):
            with self.subTest(operation=operation, duration_type=type(duration).__name__):
                with self.assertRaises(ValueError):
                    self.store.record(None, operation, '127.0.0.1', outcome, duration)
        self.assertFalse(self.store.path.exists())

    def test_shared_instance_threads_preserve_created_keys(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            keys = list(pool.map(lambda number: self.create(name='客户端%d' % number), range(16)))
        self.assertEqual(len(self.store.snapshot()['keys']), 16)
        self.enable()
        for key in keys:
            self.assertEqual(self.authorize(key)[1], 'ok')


if __name__ == '__main__':
    unittest.main()

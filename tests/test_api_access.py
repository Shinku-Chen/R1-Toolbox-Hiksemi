"""Fixture-only access key storage, authorization and secret-boundary checks."""

import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
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

    def v1_state(self, key):
        """Construct old bytes from safe local fixtures, without native secrets."""
        state = json.loads(self.store.path.read_text(encoding='utf-8'))
        state['version'] = 1
        for row in state['keys']:
            for field in ('role', 'policy_version', 'file_roots'):
                row.pop(field, None)
        return state

    def test_default_is_disabled_without_creating_file(self):
        value = self.store.snapshot()
        self.assertFalse(value['enabled'])
        self.assertTrue(value['storage_ok'])
        self.assertEqual(value['keys'], [])
        self.assertEqual(value['audit'], [])
        self.assertEqual([item['id'] for item in value['scope_catalog']], list(access.SCOPES))
        self.assertEqual([item['id'] for item in value['role_catalog']],
                         ['readonly', 'readwrite', 'admin'])
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
        self.assertEqual(principal, {
            'key_id': key['id'], 'name': key['name'], 'scopes': ['read'],
            'role': 'legacy', 'policy_version': 1, 'file_roots': [],
        })
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

    def test_explicit_roles_and_future_domains_are_preserved_without_implied_grants(self):
        self.enable()
        for role in ('readonly', 'readwrite', 'admin'):
            key = self.create(role=role, scopes=['read', 'docker', 'apps', 'albums'])
            principal, reason = self.authorize(key)
            with self.subTest(role=role):
                self.assertEqual(reason, 'ok')
                self.assertEqual(key['policy_version'], 2)
                self.assertEqual(principal['role'], role)
                self.assertEqual(principal['policy_version'], 2)
                self.assertEqual(principal['scopes'], ['read', 'docker', 'apps', 'albums'])
                self.assertEqual(principal['file_roots'], [])
                self.assertEqual(self.authorize(key, scope='docker')[1], 'ok')
                self.assertEqual(self.authorize(key, scope='power'), (None, 'scope_denied'))
                # Authorization checks the domain only. MCP separately applies
                # the operation's minimum role; the store never promotes it.
                self.assertNotIn('token', principal)
                self.assertNotIn('digest', principal)

    def test_legacy_create_keeps_original_scopes_and_rejects_new_domains(self):
        key = self.create(scopes=['fan', 'light', 'power'])
        self.assertEqual(key['role'], 'legacy')
        self.assertEqual(key['policy_version'], 1)
        self.assertEqual(key['file_roots'], [])
        for body in ({'scopes': ['docker']}, {'role': 'legacy'},
                     {'file_roots': [{'id': 'root_' + 'a' * 24, 'path': '', 'access': 'readonly'}]},
                     {'file_roots': None}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.create(**body)

    def test_v1_reads_do_not_rewrite_and_first_write_migrates_without_grant_changes(self):
        key = self.create(scopes=['read', 'fan', 'power'], allowed_ips=['192.168.31.22'])
        self.enable()
        old = self.v1_state(key)
        self.write_state(old)
        before = self.store.path.read_bytes()
        snapshot = self.store.snapshot()
        principal, reason = self.authorize(key)
        self.assertEqual(reason, 'ok')
        self.assertEqual(snapshot['keys'][0]['role'], 'legacy')
        self.assertEqual(principal['policy_version'], 1)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.configure({'enabled': True})
        migrated = json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertEqual(migrated['version'], 2)
        self.assertEqual(migrated['keys'][0], {
            **old['keys'][0], 'policy_version': 1, 'role': 'legacy', 'file_roots': [],
        })
        self.assertEqual(self.authorize(key), (principal, 'ok'))
        self.assertEqual(self.authorize(key, scope='docker'), (None, 'scope_denied'))
        self.assertEqual(self.authorize(key, peer='192.168.31.23'), (None, 'peer_not_allowed'))

    def test_v1_audit_write_migrates_and_preserves_revocation_and_expiry(self):
        valid = self.create(expires_days=1)
        revoked = self.create()
        self.store.revoke_key(revoked['id'])
        self.enable()
        old = self.v1_state(valid)
        self.write_state(old)
        principal, _ = self.authorize(valid)
        self.store.record(principal, 'integration/capabilities', '192.168.31.22', 'success')
        state = json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertEqual(state['version'], 2)
        self.assertTrue(all(row['role'] == 'legacy' for row in state['keys']))
        self.assertEqual(self.authorize(revoked), (None, 'key_revoked'))
        self.now += 86400
        self.assertEqual(self.authorize(valid), (None, 'key_expired'))

    def test_v1_revoke_write_and_new_key_creation_migrate_atomically(self):
        for operation in ('revoke', 'create'):
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory() as folder:
                    store = access.ApiAccessStore(folder, clock=lambda: self.now)
                    created = store.create_key({'name': '旧键', 'scopes': ['power']})
                    state = json.loads(store.path.read_text(encoding='utf-8'))
                    state['version'] = 1
                    for row in state['keys']:
                        for field in ('role', 'policy_version', 'file_roots'):
                            row.pop(field)
                    store.path.write_text(json.dumps(state), encoding='utf-8')
                    os.chmod(store.path, 0o600)
                    if operation == 'revoke':
                        store.revoke_key(created['id'])
                    else:
                        store.create_key({'name': '新键', 'role': 'readonly', 'scopes': ['read']})
                    result = json.loads(store.path.read_text(encoding='utf-8'))
                    self.assertEqual(result['version'], 2)
                    self.assertEqual(result['keys'][0]['role'], 'legacy')
                    self.assertEqual(result['keys'][0]['scopes'], ['power'])
                    self.assertEqual(result['keys'][0]['digest'], state['keys'][0]['digest'])

    def test_failed_v1_migration_preserves_original_bytes_and_latches_closed(self):
        key = self.create(scopes=['read', 'power'])
        self.enable()
        self.write_state(self.v1_state(key))
        before = self.store.path.read_bytes()
        with mock.patch.object(access.os, 'replace', side_effect=OSError('private detail')):
            with self.assertRaises(OSError):
                self.store.configure({'enabled': True})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.authorize(key), (None, 'storage_unavailable'))
        self.assertEqual(list(Path(self.temp.name).glob('.api-access-*')), [])

    def test_large_file_grants_cannot_write_a_state_exceeding_the_read_limit(self):
        key = self.create(role='readonly')
        self.enable()
        roots = [{'id': 'root_' + 'a' * 24, 'path': ('视' * 1020) + str(number),
                  'access': 'readonly'} for number in range(32)]
        # Each body is structurally valid. Repeated keys must not produce a
        # successful write that the next request rejects as an oversized file.
        for number in range(10):
            before = self.store.path.read_bytes()
            try:
                self.create(name='大范围%d' % number, role='readonly',
                            scopes=['files'], file_roots=roots)
            except OSError:
                self.assertEqual(self.store.path.read_bytes(), before)
                self.assertLessEqual(len(before), access._MAX_FILE_BYTES)
                self.assertEqual(self.authorize(key), (None, 'storage_unavailable'))
                self.assertEqual(list(Path(self.temp.name).glob('.api-access-*')), [])
                break
        else:
            self.fail('fixture did not exercise the state-size bound')

    def test_v2_file_roots_are_canonical_detached_and_never_resolved_by_store(self):
        roots = [{'id': 'root_' + 'a' * 24, 'path': '视频资源/电影', 'access': 'readonly'},
                 {'id': 'root_' + 'b' * 24, 'path': '', 'access': 'readonly'}]
        # These identifiers need not exist here. The integration owner verifies
        # them against its cold storage-root snapshot before calling this store.
        key = self.create(role='readonly', scopes=['read', 'files'], file_roots=roots)
        self.enable()
        roots[0]['path'] = 'changed'
        self.assertEqual(key['file_roots'][0]['path'], '视频资源/电影')
        principal, _ = self.authorize(key, scope='files')
        principal['file_roots'][0]['path'] = 'changed again'
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['keys'][0]['file_roots'][0]['path'], '视频资源/电影')
        snapshot['keys'][0]['file_roots'][0]['access'] = 'readwrite'
        snapshot['role_catalog'][0]['label'] = 'changed'
        self.assertEqual(self.authorize(key, scope='files')[0]['file_roots'][0]['access'], 'readonly')
        self.assertEqual(self.store.snapshot()['role_catalog'][0]['label'], '只读')

    def test_file_root_access_does_not_exceed_role(self):
        root = {'id': 'root_' + 'a' * 24, 'path': '', 'access': 'readwrite'}
        with self.assertRaises(ValueError):
            self.create(role='readonly', scopes=['files'], file_roots=[root])
        for role in ('readwrite', 'admin'):
            for mode in ('readonly', 'readwrite'):
                with self.subTest(role=role, mode=mode):
                    key = self.create(role=role, scopes=['files'], file_roots=[{**root, 'access': mode}])
                    self.assertEqual(key['file_roots'][0]['access'], mode)

    def test_file_root_limit_duplicate_identity_and_invalid_paths_are_rejected(self):
        root = {'id': 'root_' + 'a' * 24, 'path': '', 'access': 'readonly'}
        bad_paths = ('/', '/photos', '../photos', 'a/../b', '.', './photos', 'a/./b',
                     'a//b', 'a/', '\\photos', 'a\\b', 'a\x00b', 'a\nb', 'a\tb',
                     'a' * 1025, '\ud800', None, [], 1)
        invalid = (None, {}, [None], [{**root, 'id': 'root_' + 'A' * 24}],
                   [{**root, 'id': 'root_' + 'a' * 23}], [{**root, 'id': 'arbitrary-path'}],
                   [{**root, 'extra': True}], [{**root, 'access': 'admin'}],
                   [{**root, 'access': []}], [root, {**root, 'access': 'readwrite'}],
                   *([{**root, 'path': path}] for path in bad_paths),
                   [{**root, 'path': str(number)} for number in range(33)])
        for value in invalid:
            with self.subTest(roots=value), self.assertRaises(ValueError):
                self.create(role='admin', scopes=['files'], file_roots=value)
        self.assertFalse(self.store.path.exists())
        accepted = [{**root, 'path': str(number)} for number in range(32)]
        self.assertEqual(len(self.create(role='admin', scopes=['files'], file_roots=accepted)['file_roots']), 32)

    def test_file_scope_and_root_range_must_be_selected_together(self):
        root = {'id': 'root_' + 'a' * 24, 'path': '', 'access': 'readonly'}
        for updates in ({'scopes': ['files'], 'file_roots': []},
                        {'scopes': ['files']}, {'scopes': ['read'], 'file_roots': [root]}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.create(role='admin', **updates)
        for role in (None, True, [], {}, 'READONLY', 'legacy'):
            with self.subTest(role=role), self.assertRaises(ValueError):
                self.create(role=role)

    def test_malformed_v2_policy_fields_fail_closed_without_overwrite(self):
        key = self.create(role='admin', scopes=['read'])
        self.enable()
        state = json.loads(self.store.path.read_text(encoding='utf-8'))
        row = state['keys'][0]
        bad_rows = ({**row, 'policy_version': 1}, {**row, 'policy_version': True},
                    {**row, 'policy_version': 3}, {**row, 'role': 'legacy'},
                    {**row, 'role': []}, {**row, 'role': 'superadmin'},
                    {field: value for field, value in row.items() if field != 'file_roots'},
                    {**row, 'file_roots': None},
                    {**row, 'scopes': ['files'], 'file_roots': []},
                    {**row, 'policy_version': 1, 'role': 'legacy', 'scopes': ['docker']},
                    {**row, 'policy_version': 1, 'role': 'legacy',
                     'file_roots': [{'id': 'root_' + 'a' * 24, 'path': '', 'access': 'readonly'}]})
        for bad in bad_rows:
            with self.subTest(row=bad):
                self.write_state({**state, 'keys': [bad]})
                before = self.store.path.read_bytes()
                self.assertEqual(self.authorize(key), (None, 'storage_unavailable'))
                with self.assertRaises(OSError):
                    self.store.configure({'enabled': True})
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_v1_cannot_smuggle_role_fields_or_new_scopes(self):
        key = self.create()
        self.enable()
        old = self.v1_state(key)
        row = old['keys'][0]
        for bad in ({**row, 'role': 'admin'}, {**row, 'scopes': ['docker']},
                    {**row, 'policy_version': 2}, {**row, 'file_roots': []}):
            with self.subTest(row=bad):
                self.write_state({**old, 'keys': [bad]})
                self.assertEqual(self.authorize(key), (None, 'storage_unavailable'))


if __name__ == '__main__':
    unittest.main()

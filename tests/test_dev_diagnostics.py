"""固定诊断选择、隔离失败和保留完整只读证据。"""

import copy
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dev_diagnostics', ROOT / 'runtime/dev_diagnostics.py')
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class RequestTests(unittest.TestCase):
    def test_file_backend_catalog_describes_admin_handles_and_metadata_effects(self):
        description = next(row['description'] for row in diagnostics.catalog()['checks']
                           if row['id'] == 'file_backend_context')
        self.assertIn('Users/admin 句柄元数据', description)
        self.assertIn('显式', description)
        self.assertIn('不读取目录内容', description)
        self.assertIn('可能唤盘', description)

    def test_catalog_is_fixed_read_only_and_independent(self):
        value = diagnostics.catalog()
        self.assertEqual(diagnostics.CHECKS, ('device', 'storage', 'business', 'fan'))
        self.assertEqual(set(value), {'schema_version', 'read_only', 'checks'})
        self.assertEqual(value['schema_version'], 1)
        self.assertIs(value['read_only'], True)
        self.assertEqual(tuple(row['id'] for row in value['checks']), diagnostics.ALLOWED_CHECKS)
        for row in value['checks']:
            self.assertEqual(set(row), {'id', 'label', 'description'})
            self.assertTrue(row['label'])
            self.assertTrue(row['description'])
        value['checks'][0]['label'] = 'changed'
        value['checks'].clear()
        self.assertEqual(len(diagnostics.catalog()['checks']), 11)
        self.assertNotEqual(diagnostics.catalog()['checks'][0]['label'], 'changed')

    def test_valid_actions_and_requested_order_leave_request_unchanged(self):
        self.assertEqual(diagnostics.parse_request({'action': 'catalog'}), ('catalog', ()))
        self.assertEqual(diagnostics.parse_request({'action': 'snapshot'}),
                         ('snapshot', diagnostics.CHECKS))
        body = {'action': 'snapshot', 'checks': ['fan', 'device']}
        before = copy.deepcopy(body)
        self.assertEqual(diagnostics.parse_request(body), ('snapshot', ('fan', 'device')))
        self.assertEqual(body, before)

    def test_storage_identity_is_explicit_only_and_does_not_expand_the_default(self):
        self.assertNotIn('storage_identity', diagnostics.parse_request({'action': 'snapshot'})[1])
        self.assertEqual(diagnostics.parse_request({'action': 'snapshot', 'checks': ['storage_identity']}),
                         ('snapshot', ('storage_identity',)))
        self.assertEqual(diagnostics.parse_request({'action': 'snapshot', 'checks': ['storage_identity', 'storage']}),
                         ('snapshot', ('storage_identity', 'storage')))
        provider = Mock(return_value=(200, {'ok': True, 'storage': {'serial': 'fixture-full-serial'}}))
        collectors = {name: lambda: (200, {'ok': True}) for name in diagnostics.CHECKS}
        collectors['storage_identity'] = provider
        default = diagnostics.collect(diagnostics.CHECKS, collectors)
        self.assertNotIn('fixture-full-serial', json.dumps(default))
        provider.assert_not_called()
        explicit = diagnostics.collect(('storage_identity',), collectors)
        self.assertEqual(explicit['checks']['storage_identity']['data']['storage']['serial'], 'fixture-full-serial')
        provider.assert_called_once_with()

    def test_rejects_every_nonfixed_action_and_parameter_shape(self):
        invalid = [None, [], 'snapshot', True, {}, {'action': None}, {'action': []},
                   {'action': {}}, {'action': 'retry'}, {'action': 'exec'},
                   {'action': 'snapshot', 'command': 'id'},
                   {'action': 'snapshot', 'path': '/private'},
                   {'action': 'catalog', 'checks': ['device']},
                   {'action': 'snapshot', 'checks': None},
                   {'action': 'snapshot', 'checks': 'device'},
                   {'action': 'snapshot', 'checks': ('device',)},
                   {'action': 'snapshot', 'checks': {}},
                   {'action': 'snapshot', 'checks': []},
                   {'action': 'snapshot', 'checks': ['device'] * 5},
                   {'action': 'snapshot', 'checks': ['device', 'device']},
                   {'action': 'snapshot', 'checks': ['unknown']},
                   {'action': 'snapshot', 'checks': [True]},
                   {'action': 'snapshot', 'checks': [1]},
                   {'action': 'snapshot', 'checks': [None]},
                   {'action': 'snapshot', 'checks': [[]]},
                   {'action': 'snapshot', 'checks': [{}]}]
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(ValueError) as raised:
                    diagnostics.parse_request(body)
                self.assertTrue(any('\u4e00' <= char <= '\u9fff' for char in str(raised.exception)))
                self.assertNotIn('/private', str(raised.exception))


class CollectionTests(unittest.TestCase):
    def test_calls_only_selected_providers_once_in_requested_order(self):
        calls = []

        def provider(name):
            def callback():
                calls.append(name)
                return 200, {'ok': True, 'snapshot': {'name': name}}
            return callback

        collectors = {name: provider(name) for name in diagnostics.CHECKS}
        with patch.object(diagnostics, '_stamp', side_effect=['start', 'finish']):
            value = diagnostics.collect(('fan', 'device'), collectors)
        self.assertEqual(calls, ['fan', 'device'])
        self.assertEqual(tuple(value['checks']), ('fan', 'device'))
        self.assertEqual(set(value), {'schema_version', 'read_only', 'started_at', 'finished_at',
                                     'checks', 'all_checks_completed'})
        self.assertEqual((value['started_at'], value['finished_at']), ('start', 'finish'))
        self.assertEqual(value['schema_version'], 1)
        self.assertIs(value['read_only'], True)
        self.assertIs(value['all_checks_completed'], True)
        for name, row in value['checks'].items():
            self.assertEqual(row, {'http_status': 200, 'ok': True,
                                   'data': {'snapshot': {'name': name}}, 'error': None})

    def test_utc_timestamps_are_ordered(self):
        value = diagnostics.collect(('device',), {'device': lambda: (200, {'ok': True})})
        start = datetime.fromisoformat(value['started_at'])
        finish = datetime.fromisoformat(value['finished_at'])
        self.assertIsNotNone(start.utcoffset())
        self.assertEqual(start.utcoffset().total_seconds(), 0)
        self.assertLessEqual(start, finish)
        json.dumps(value, allow_nan=False)

    def test_partial_failure_lock_conflict_and_exception_do_not_stop_later_check(self):
        calls = []

        def callback(name, result=None, fail=False):
            def run():
                calls.append(name)
                if fail:
                    raise RuntimeError('/secret/private-location password=private-value')
                return result
            return run

        collectors = {
            'device': callback('device', (200, {'ok': False, 'error': '/private/source'})),
            'storage': callback('storage', (409, {'ok': False, 'error': '/private/lock'})),
            'business': callback('business', fail=True),
            'fan': callback('fan', (200, {'ok': True, 'snapshot': {'rpm': None}})),
        }
        value = diagnostics.collect(diagnostics.CHECKS, collectors)
        self.assertEqual(calls, list(diagnostics.CHECKS))
        self.assertIs(value['all_checks_completed'], False)
        for name, status in (('device', 503), ('storage', 409), ('business', 503)):
            row = value['checks'][name]
            self.assertEqual(row['http_status'], status)
            self.assertIs(row['ok'], False)
            self.assertIsNone(row['data'])
            self.assertTrue(row['error'])
        self.assertIs(value['checks']['fan']['ok'], True)
        self.assertNotIn('/private', json.dumps(value))
        self.assertNotIn('/secret', json.dumps(value))
        self.assertNotIn('private-value', json.dumps(value))

    def test_error_statuses_are_preserved_only_for_valid_http_errors(self):
        for status in (400, 401, 403, 404, 409, 429, 500, 599):
            with self.subTest(status=status):
                value = diagnostics.collect(('device',), {
                    'device': lambda: (status, {'ok': True, 'error': 'private'})})
                self.assertEqual(value['checks']['device']['http_status'], status)
                self.assertIsNone(value['checks']['device']['data'])
                self.assertNotIn('private', value['checks']['device']['error'])
        for status in (None, True, False, '503', 99, 199, 200.0, 301, 399, 600):
            with self.subTest(status=status):
                value = diagnostics.collect(('device',), {
                    'device': lambda: (status, {'ok': False})})
                self.assertEqual(value['checks']['device']['http_status'], 503)

    def test_missing_and_invalid_providers_are_isolated(self):
        malformed = [None, False, lambda: None, lambda: (200,), lambda: (200, []),
                     lambda: (200, {'ok': 1}), lambda: (200, {'snapshot': {}})]
        for provider in malformed:
            with self.subTest(provider=provider):
                value = diagnostics.collect(('device', 'fan'), {
                    'device': provider, 'fan': lambda: (204, {'ok': True})})
                self.assertEqual(value['checks']['device']['http_status'], 503)
                self.assertIs(value['checks']['device']['ok'], False)
                self.assertIs(value['checks']['fan']['ok'], True)
        value = diagnostics.collect(('device',), {})
        self.assertEqual(value['checks']['device']['http_status'], 503)

    def test_unavailable_nested_category_is_success_and_payload_is_not_mutated(self):
        payload = {'ok': True, 'snapshot': {
            'categories': {'media': {'available': False, 'status': 'unverified_read_only',
                                      'reason': '尚未确认只读来源。', 'data': None}},
            'evidence': ['record'] * 300, 'long_text': '证据' * 3000}}
        before = copy.deepcopy(payload)
        value = diagnostics.collect(('business',), {'business': lambda: (200, payload)})
        self.assertIs(value['all_checks_completed'], True)
        self.assertEqual(value['checks']['business']['data'], {'snapshot': before['snapshot']})
        self.assertEqual(payload, before)
        self.assertNotIn('ok', value['checks']['business']['data'])

    def test_invalid_selection_is_rejected_before_any_provider_runs(self):
        provider = Mock(return_value=(200, {'ok': True}))
        for checks in ((), [], 'device', ['unknown'], ['device', 'device'], [[]]):
            with self.subTest(checks=checks):
                with self.assertRaises(ValueError):
                    diagnostics.collect(checks, {'device': provider})
        provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()

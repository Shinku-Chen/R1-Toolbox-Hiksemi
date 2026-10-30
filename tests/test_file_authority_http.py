"""Owner-only custody management is separate from machine file permission."""
from unittest import mock
import unittest

from test_mcp_completion_http import CompletionHTTPBase

ROOT_ID = 'root_' + 'a' * 24
RECOVERY = 'operation_' + 'b' * 32
BASE = '/api/files/authority'


class FileAuthorityHTTPTests(CompletionHTTPBase):
    def setUp(self):
        super().setUp()
        self.authority = mock.Mock()
        self.authority.catalog.return_value = {'ok': True, 'authorities': []}
        self.authority.preflight.return_value = {'ok': True, 'ready': False, 'blockers': ['parent_writable']}
        self.authority.initialize.return_value = {'ok': True, 'may_wake_disks': True}
        self.authority.snapshot.return_value = {'ok': True, 'recoveries': [], 'may_wake_disks': True}
        self.authority.revoke.return_value = {'ok': True}
        self.authority.restore.return_value = {'ok': True,
            'recovery': {'recovery_id': RECOVERY, 'state': 'restored', 'index_verified': True}}
        self.server.integration.file_authority = self.authority

    def test_owner_metadata_does_not_initialize_or_scan_user_files(self):
        self.assertEqual(self.browser('GET', BASE)[0], 200)
        self.assertEqual(self.browser('GET', '/api/access')[0], 200)
        self.assertEqual(self.authority.catalog.call_count, 2)
        for method in ('initialize', 'snapshot', 'preflight', 'revoke', 'restore'):
            getattr(self.authority, method).assert_not_called()

    def test_owner_actions_have_fixed_shapes_and_explicit_confirmation(self):
        cases = [('preflight', {'root_id': ROOT_ID}, 'preflight'),
                 ('initialize', {'root_id': ROOT_ID, 'confirmed': True}, 'initialize'),
                 ('inspect', {'root_id': ROOT_ID}, 'snapshot'),
                 ('revoke', {'root_id': ROOT_ID, 'confirmed': True}, 'revoke'),
                 ('restore', {'root_id': ROOT_ID, 'recovery_id': RECOVERY, 'confirmed': True}, 'restore')]
        for action, body, method in cases:
            status, _, result = self.browser('POST', BASE + '/' + action, body)
            self.assertEqual(status, 200, result)
            call = getattr(self.authority, method).call_args
            self.assertEqual(call.args, (ROOT_ID,))
            self.assertTrue(call.kwargs['current']())
            if action not in ('inspect', 'preflight'):
                self.assertIs(call.kwargs['confirmed'], True)
            for invalid in ({**body, 'path': '/PRIVATE'}, {**body, 'command': 'PRIVATE'},
                            {key: value for key, value in body.items() if key != 'root_id'}):
                count = getattr(self.authority, method).call_count
                self.assertEqual(self.browser('POST', BASE + '/' + action, invalid)[0], 400)
                self.assertEqual(getattr(self.authority, method).call_count, count)
        for action in ('initialize', 'revoke', 'restore'):
            body = {'root_id': ROOT_ID, 'confirmed': False}
            if action == 'restore':
                body['recovery_id'] = RECOVERY
            self.assertEqual(self.browser('POST', BASE + '/' + action, body)[0], 400)

    def test_bearer_or_developer_cannot_initialize_or_restore_custody(self):
        key = self.key('admin', ['read'], ['get_nas_status'])
        token_headers = {'Authorization': 'Bearer ' + key['token']}
        self.assertEqual(self.browser('GET', BASE, headers=token_headers)[0], 403)
        for action in ('initialize', 'inspect', 'preflight', 'revoke', 'restore'):
            body = {'root_id': ROOT_ID}
            if action not in ('inspect', 'preflight'):
                body['confirmed'] = True
            if action == 'restore':
                body['recovery_id'] = RECOVERY
            self.assertEqual(self.browser('POST', BASE + '/' + action, body, headers=token_headers)[0], 403)
            self.assertIn(self.browser('POST', BASE + '/' + action, body, cookie=False)[0], (401, 403))
        self.authority.initialize.assert_not_called()
        self.authority.restore.assert_not_called()

    def test_live_owner_revalidation_and_same_origin_json_gates(self):
        status, _, value = self.browser('POST', BASE + '/initialize', {'root_id': ROOT_ID, 'confirmed': True})
        self.assertEqual(status, 200, value)
        current = self.authority.initialize.call_args.kwargs['current']
        self.assertTrue(current())
        self.server.auth.revoke(self.server.auth.cookie_sid(self.cookie))
        self.assertFalse(current())
        before = self.authority.initialize.call_count
        for headers in ({'Origin': 'http://foreign.invalid'}, {'X-FNM-Request': ''},
                        {'Content-Type': 'text/plain'}):
            self.assertIn(self.browser('POST', BASE + '/initialize',
                {'root_id': ROOT_ID, 'confirmed': True}, headers=headers)[0], (400, 401, 403, 415))
        self.assertEqual(self.authority.initialize.call_count, before)


if __name__ == '__main__':
    unittest.main()

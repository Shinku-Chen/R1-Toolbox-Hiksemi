"""Owner-only definitions, collected with the other integration HTTP fixtures."""

import json
from pathlib import Path
import unittest
from unittest import mock

import test_integration_http as fixture
from api_contracts import contract_catalog


ROUTE = '/api/access/contracts'
ROOT = Path(__file__).resolve().parents[1]


class ApiContractHTTPTests(unittest.TestCase):
    setUp = fixture.IntegrationHTTPTests.setUp
    _patch = fixture.IntegrationHTTPTests._patch
    request = fixture.IntegrationHTTPTests.request
    browser = fixture.IntegrationHTTPTests.browser
    rpc = fixture.IntegrationHTTPTests.rpc
    rest_action = fixture.IntegrationHTTPTests.rest_action
    message = staticmethod(fixture.IntegrationHTTPTests.message)
    key = fixture.IntegrationHTTPTests.key
    assert_no_actions = fixture.IntegrationHTTPTests.assert_no_actions
    assert_tool_error = fixture.IntegrationHTTPTests.assert_tool_error

    def test_owner_catalog_is_static_available_with_mcp_off_and_does_not_read_resources(self):
        roots = self._patch(self.server.integration, 'file_root_catalog',
                            side_effect=AssertionError('must not inspect NAS roots'))
        before = self.server.api_access.path.read_bytes() if self.server.api_access.path.exists() else None
        status, _, value = self.browser(path=ROUTE)
        self.assertEqual(200, status)
        self.assertEqual({'ok': True, **contract_catalog()}, value)
        self.assertEqual(20, len(value['tools']))
        self.assertTrue(all(tool['implemented'] is False for tool in value['tools']))
        after = self.server.api_access.path.read_bytes() if self.server.api_access.path.exists() else None
        self.assertEqual(before, after)
        roots.assert_not_called()
        self.assert_no_actions()

    def test_artifact_matches_runtime_contract_definitions(self):
        artifact = ROOT / 'docs/api/file-docker-contracts.json'
        self.assertEqual(contract_catalog(), json.loads(artifact.read_text(encoding='utf-8')))

    def test_password_owner_cookie_is_required_and_bearer_never_substitutes(self):
        key = self.key()
        for cookie, headers in ((False, {}),
                                (False, {'Authorization': 'Bearer ' + key['token']}),
                                (True, {'Authorization': 'Bearer ' + key['token']}),
                                (True, {'Authorization': ''})):
            with self.subTest(cookie=cookie, authorization=bool(headers)):
                status, _, value = self.browser(path=ROUTE, cookie=cookie, headers=headers)
                self.assertIn(status, (401, 403))
                self.assertNotIn('tools', value)
        self.assert_no_actions()

    def test_developer_whitelist_does_not_grant_owner_definitions(self):
        # A valid developer window can read diagnostics, not owner API metadata.
        self.server.auth_mode = 'pair'
        with mock.patch.object(self.server.auth, 'required', return_value=False), \
                mock.patch.object(self.server, '_dev_authorized', return_value=True):
            status, _, value = self.browser(path=ROUTE, cookie=False)
        self.assertEqual(403, status)
        self.assertNotIn('tools', value)
        self.assert_no_actions()

    def test_contracts_follow_the_existing_password_open_owner_rule(self):
        with mock.patch.object(self.server.auth, 'required', return_value=False):
            status, _, value = self.browser(path=ROUTE, cookie=False)
        self.assertEqual(200, status)
        self.assertEqual('definitions_only', value['status'])
        self.assert_no_actions()

    def test_query_fragment_and_foreign_origin_are_rejected(self):
        for path, headers in ((ROUTE + '?tool=list_directory', {}),
                              (ROUTE + '#fragment', {}),
                              (ROUTE, {'Origin': 'http://foreign.invalid'})):
            with self.subTest(path=path, headers=tuple(headers)):
                status, _, value = self.browser(path=path, headers=headers)
                self.assertIn(status, (400, 403))
                self.assertNotIn('tools', value)
        self.assert_no_actions()

    def test_definitions_do_not_expand_live_mcp_or_rest_for_any_role(self):
        names = {tool['name'] for tool in contract_catalog()['tools']}
        self.server.api_access.configure({'enabled': True})
        for role in ('readonly', 'readwrite', 'admin'):
            key = self.server.api_access.create_key({
                'name': 'fixture ' + role, 'role': role,
                'scopes': list(fixture.SCOPES) + ['files', 'docker'],
                'file_roots': [{'id': 'root_' + 'a' * 24, 'path': 'fixture',
                                'access': 'readonly' if role == 'readonly' else 'readwrite'}],
                'allowed_ips': [], 'expires_days': 90})
            with self.subTest(role=role):
                status, _, value = self.rpc(key)
                self.assertEqual(200, status)
                self.assertFalse(names & {tool['name'] for tool in value['result']['tools']})
            for name in names:
                with self.subTest(role=role, name=name):
                    status, _, value = self.rest_action(key, tool=name, arguments={})
                    self.assertEqual(200, status)
                    self.assert_tool_error(value, 'unknown_tool')
        self.assert_no_actions()


if __name__ == '__main__':
    unittest.main()

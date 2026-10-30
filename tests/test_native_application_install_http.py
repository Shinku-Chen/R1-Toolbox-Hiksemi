"""Real owner/MCP/REST gates for the fixed native application installation.

The native client replies are private fixtures; HTTP authorization, schemas,
dispatch, plans and idempotency are production code. No NAS is contacted.
"""

import json
import unittest

import test_mcp_completion_http as completion
import test_native_application_install as native_fixture
from test_native_application_expansion import catalogs as expanded_catalogs
from native_capability_specs import APPLICATION_INSTALL_SPECS
from vendor_business_transport import BusinessTransportError
from vendor_media_operations import IdempotencyStore
from vendor_native_capabilities import NativeCapabilities


PLAN = 'plan_native_application_install'
INSTALL = 'install_native_application'


class NativeApplicationInstallHTTPTests(completion.CompletionHTTPBase):
    def key(self, role, scopes, tools=None, *, applications=None, **kwargs):
        """New Keys explicitly choose installation objects, never all apps."""
        key = super().key(role, scopes, tools, **kwargs)
        if tools is not None and set(tools) & {PLAN, INSTALL}:
            selected = [34] if applications is None else applications
            status, _, value = self.browser('POST', '/api/access/keys/update',
                {'id': key['id'], 'application_ids': selected})
            self.assertEqual(status, 200, value)
            key.update(value['key'])
        return key

    def setUp(self):
        super().setUp()
        self.client = native_fixture.Client()
        self.server.integration.native_operations = NativeCapabilities(
            client=self.client, idempotency_store=IdempotencyStore())
        self.admin = self.key('admin', ['apps'], [PLAN, INSTALL])

    def plan(self, key=None, *, rest=False):
        result = self.good(self.call(key or self.admin, PLAN, {'application_id': 34}, rest=rest))
        self.assertTrue(result['data']['allowed'])
        return result['data']['expected_version']

    def install_args(self, version, **changes):
        return {'application_id': 34, 'expected_version': version, 'confirmed': True,
                'idempotency_key': 'http:install:1', **changes}

    def test_owner_grants_require_admin_apps_exact_tools_and_plan_dependency(self):
        for role, scopes, tools in (('readwrite', ['apps'], [PLAN]),
                ('readonly', ['apps'], [PLAN]), ('admin', ['system'], [PLAN]),
                ('admin', ['apps'], [INSTALL])):
            status, _, value = self.browser('POST', '/api/access/keys', {
                'name': 'invalid native install grant', 'role': role,
                'scopes': scopes, 'tool_grants': tools, 'expires_days': 1})
            self.assertEqual(status, 400)
            self.assertFalse(value['ok'])
        for role in ('readonly', 'readwrite', 'admin'):
            key = self.key(role, ['apps'], [])
            status, _, value = self.rpc(key)
            self.assertEqual(status, 200)
            self.assertFalse({PLAN, INSTALL} & {row['name'] for row in value['result']['tools']})
            self.bad(self.call(key, PLAN, {'application_id': 34}), 'scope_denied')
        status, _, value = self.rpc(self.admin)
        self.assertEqual(status, 200)
        tools = {row['name']: row for row in value['result']['tools']}
        self.assertTrue({PLAN, INSTALL} <= set(tools))
        self.assertFalse(tools[PLAN]['annotations']['readOnlyHint'])
        self.assertFalse(tools[INSTALL]['annotations']['idempotentHint'])
        self.assertEqual(self.client.calls, [])

    def test_real_mcp_plan_rest_install_and_same_key_replay_are_one_fixed_write(self):
        version = self.plan()
        self.assertEqual([row[0] for row in self.client.calls], ['apply_list', 'module_inventory'])
        self.client.calls.clear()
        first = self.good(self.call(self.admin, INSTALL, self.install_args(version), rest=True))
        repeated = self.good(self.call(self.admin, INSTALL, self.install_args(version)))
        self.assertTrue(first['data']['accepted'])
        self.assertFalse(first['data']['effect_confirmed'])
        self.assertIsNone(first['data']['native_task_id'])
        self.assertTrue(repeated['idempotent_replay'])
        self.assertEqual([row[0] for row in self.client.calls],
                         ['apply_list', 'module_inventory', 'application_install'])
        _, parameters, body, confirmed = self.client.calls[-1]
        self.assertEqual(parameters, {})
        self.assertTrue(confirmed)
        self.assertEqual(body, {'client_id': 'r1-toolbox', 'flag': 0, 'size': 1234,
            'apply_id': 34, 'module_name': 'netdisk_bd2', 'vid': '', 'module_type': 0,
            'source': 0, 'version': 'V1.0.14', 'guest': 0})
        audit = json.dumps(self.server.api_access.snapshot()['audit'])
        for secret in ('PRIVATE', self.admin['token'], 'netdisk_bd2', 'expected_version', version):
            self.assertNotIn(secret, audit)

    def test_strict_schemas_reject_native_parameters_urls_and_missing_confirmation(self):
        version = 'app_install_' + '0' * 32
        for changes in ({'confirmed': False}, {'idempotency_key': ''}, {'url': 'https://PRIVATE'},
                {'path': '/PRIVATE'}, {'flag': 1}, {'source': 1}, {'expected_version': 'bad'},
                {'application_id': True}):
            with self.subTest(fields=list(changes)):
                self.bad(self.call(self.admin, INSTALL, self.install_args(version, **changes)),
                         'invalid_arguments')
        self.bad(self.call(self.admin, PLAN, {'application_id': 34, 'action': 'install_apply'}, rest=True),
                 'invalid_arguments')
        arguments = self.install_args(version)
        del arguments['confirmed']
        self.bad(self.call(self.admin, INSTALL, arguments, rest=True), 'invalid_arguments')
        self.assertEqual(self.client.calls, [])

    def test_plans_are_bound_to_observed_catalog_key_and_credential_generation(self):
        version = self.plan(rest=True)
        other = self.key('admin', ['apps'], [PLAN, INSTALL])
        self.bad(self.call(other, INSTALL, self.install_args(version)), 'installation_plan_changed')
        self.client.frames['module_inventory']['available_list'][0]['inst_path'] = '/OTHER-PRIVATE'
        self.bad(self.call(self.admin, INSTALL, self.install_args(version)), 'installation_plan_changed')
        self.assertNotIn('application_install', [row[0] for row in self.client.calls])
        self.client.frames = native_fixture.catalogs()
        version = self.plan()
        status, _, rotated = self.browser('POST', '/api/access/keys/rotate', {'id': self.admin['id']})
        self.assertEqual(status, 200)
        self.admin = {**rotated['key'], 'token': rotated['token']}
        self.bad(self.call(self.admin, INSTALL, self.install_args(version, idempotency_key='after:rotate')),
                 'installation_plan_changed')
        self.assertNotIn('application_install', [row[0] for row in self.client.calls])

    def test_unknown_native_reply_is_retained_and_never_replayed_as_a_write(self):
        version = self.plan()
        self.client.reply = BusinessTransportError('timeout', request_sent=True)
        first = self.call(self.admin, INSTALL, self.install_args(version))
        self.bad(first, 'result_unconfirmed')
        self.assertEqual(first['structuredContent']['mutation_outcome'], 'unknown')
        self.client.reply = {'apply_id': 34, 'module_name': 'netdisk_bd2'}
        repeated = self.call(self.admin, INSTALL, self.install_args(version), rest=True)
        self.bad(repeated, 'result_unconfirmed')
        self.assertTrue(repeated['structuredContent']['idempotent_replay'])
        self.assertEqual(sum(row[0] == 'application_install' for row in self.client.calls), 1)

    def test_revocation_during_catalog_or_after_ack_discards_current_result(self):
        for stage in ('module_inventory', 'application_install'):
            self.admin = self.key('admin', ['apps'], [PLAN, INSTALL])
            version = self.plan()
            self.client.calls.clear()
            self.client.after = lambda operation: (
                self.server.api_access.revoke_key(self.admin['id']) if operation == stage else None)
            result = self.call(self.admin, INSTALL, self.install_args(version))
            self.bad(result, 'invalid_key')
            self.client.after = None
            if stage == 'module_inventory':
                self.assertNotIn('application_install', [row[0] for row in self.client.calls])
            else:
                self.assertEqual(sum(row[0] == 'application_install' for row in self.client.calls), 1)
            status, _, _ = self.rest_action(self.admin, INSTALL, self.install_args(version))
            self.assertEqual(status, 401)

    def test_legacy_role_token_does_not_acquire_installation_tools(self):
        self.server.api_access.configure({'enabled': True})
        legacy = self.server.api_access.create_key({'name': 'legacy v2', 'role': 'admin',
            'scopes': ['apps'], 'file_roots': [], 'expires_days': 1})
        self.bad(self.call(legacy, PLAN, {'application_id': 34}), 'scope_denied')
        self.bad(self.call(legacy, INSTALL, self.install_args('app_install_' + '0' * 32)), 'scope_denied')
        self.assertEqual(self.client.calls, [])

    def test_new_keys_default_to_no_installation_resources_and_explicit_selection_is_exact(self):
        empty = super().key('admin', ['apps'], [PLAN, INSTALL])
        self.assertEqual(empty['application_ids'], [])
        self.bad(self.call(empty, PLAN, {'application_id': 34}), 'authorization_denied')
        selected = self.key('admin', ['apps'], [PLAN, INSTALL], applications=[7])
        self.client.frames = expanded_catalogs(8)
        self.bad(self.call(selected, PLAN, {'application_id': 8}, rest=True), 'authorization_denied')
        self.assertEqual(self.client.calls, [])

    def test_more_audited_application_ids_work_through_real_mcp_and_rest_gates(self):
        for identifier in (7, 8, 35):
            with self.subTest(identifier=identifier):
                key = self.key('admin', ['apps'], [PLAN, INSTALL], applications=[identifier])
                self.client.frames = expanded_catalogs(identifier)
                self.client.reply = {'apply_id': identifier,
                    'module_name': APPLICATION_INSTALL_SPECS[identifier]['module_name']}
                self.client.calls.clear()
                plan = self.good(self.call(key, PLAN, {'application_id': identifier}))['data']
                self.assertTrue(plan['allowed'])
                args = self.install_args(plan['expected_version'], application_id=identifier,
                                         idempotency_key=f'http:expanded:{identifier}')
                result = self.good(self.call(key, INSTALL, args, rest=True))['data']
                self.assertTrue(result['accepted'])
                self.assertFalse(result['effect_confirmed'])
                self.assertEqual(self.client.calls[-1][2]['apply_id'], identifier)
                self.assertEqual(self.client.calls[-1][2]['module_name'],
                                 APPLICATION_INSTALL_SPECS[identifier]['module_name'])

    def test_editing_only_application_resources_invalidates_old_plan_and_receipt(self):
        key = self.key('admin', ['apps'], [PLAN, INSTALL], applications=[7, 34])
        self.client.frames = expanded_catalogs(7)
        self.client.reply = {'apply_id': 7, 'module_name': 'netdisk_bd'}
        plan = self.good(self.call(key, PLAN, {'application_id': 7}))['data']
        args = self.install_args(plan['expected_version'], application_id=7)
        self.good(self.call(key, INSTALL, args))
        status, _, changed = self.browser('POST', '/api/access/keys/update',
                                          {'id': key['id'], 'application_ids': [34]})
        self.assertEqual(status, 200)
        self.assertEqual(changed['key']['application_ids'], [34])
        self.bad(self.call(key, INSTALL, args, rest=True), 'authorization_denied')
        self.assertEqual(sum(row[0] == 'application_install' for row in self.client.calls), 1)

    def test_application_resource_revocation_during_preflight_or_ack_is_current(self):
        for stage in ('module_inventory', 'application_install'):
            with self.subTest(stage=stage):
                key = self.key('admin', ['apps'], [PLAN, INSTALL], applications=[7])
                self.client.frames = expanded_catalogs(7)
                self.client.reply = {'apply_id': 7, 'module_name': 'netdisk_bd'}
                plan = self.good(self.call(key, PLAN, {'application_id': 7}))['data']
                args = self.install_args(plan['expected_version'], application_id=7,
                                         idempotency_key=f'http:revoke:{stage}')
                self.client.calls.clear()
                self.client.after = lambda operation: self.server.api_access.update_key(
                    {'id': key['id'], 'application_ids': []}) if operation == stage else None
                self.bad(self.call(key, INSTALL, args), 'invalid_key')
                self.client.after = None
                self.assertEqual(sum(row[0] == 'application_install' for row in self.client.calls),
                                 1 if stage == 'application_install' else 0)


if __name__ == '__main__':
    unittest.main()

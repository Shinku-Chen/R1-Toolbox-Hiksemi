"""Exact application resource grants and per-module new-install boundaries."""

import json
import unittest

import test_native_application_install as fixture
from native_capability_specs import APPLICATION_INSTALL_SPECS
from vendor_business_transport import BusinessTransportError, _validate
from vendor_native_capabilities import NativeCapabilities, IdempotencyStore


def catalogs(identifier):
    """Use the production identity; all package observations are local fixtures."""
    frames = fixture.catalogs()
    spec = APPLICATION_INSTALL_SPECS[identifier]
    app = frames['apply_list']['list'][0]
    module = frames['module_inventory']['available_list'][0]
    app.update(apply_id=identifier, module_name=spec['module_name'])
    module.update(id=identifier, module_name=spec['module_name'])
    return frames


class ApplicationExpansionTests(unittest.TestCase):
    def setUp(self):
        self.client = fixture.Client()
        self.backend = NativeCapabilities(client=self.client, idempotency_store=IdempotencyStore())
        self.principal = {**fixture.PRINCIPAL, 'application_ids': [7, 8, 34, 35]}
        self.active = True

    def call(self, name, arguments, principal=None):
        return self.backend.execute(name, arguments,
            principal=self.principal if principal is None else principal,
            authorizer=lambda *_: self.active)

    def plan(self, identifier, principal=None):
        return self.call('plan_native_application_install', {'application_id': identifier}, principal)

    def install(self, identifier, version, *, key='expand:1', principal=None):
        return self.call('install_native_application', {'application_id': identifier,
            'expected_version': version, 'confirmed': True, 'idempotency_key': key}, principal)

    def choose(self, identifier):
        self.client.frames = catalogs(identifier)
        self.client.reply = {'apply_id': identifier,
                             'module_name': APPLICATION_INSTALL_SPECS[identifier]['module_name']}
        self.client.calls.clear()

    def test_only_individually_audited_modules_are_in_canonical_resource_choices(self):
        self.assertEqual({identifier: row['module_name'] for identifier, row in
                          APPLICATION_INSTALL_SPECS.items()},
                         {7: 'netdisk_bd', 8: 'contact_backup', 34: 'netdisk_bd2', 35: '66_print'})
        for identifier, row in APPLICATION_INSTALL_SPECS.items():
            self.assertEqual(row['application_id'], identifier)
            self.assertEqual(row['native_type'], 'extension')
            self.assertNotIn('path', json.dumps(row))
            self.assertFalse(row['resource_effects']['file_tool_sandbox_applies'])

    def test_all_audited_modules_use_their_own_exact_payload_and_effects(self):
        for identifier, spec in APPLICATION_INSTALL_SPECS.items():
            with self.subTest(identifier=identifier):
                self.choose(identifier)
                plan = self.plan(identifier)
                self.assertTrue(plan['data']['allowed'], plan)
                self.assertEqual(plan['data']['resource_effects'], spec['resource_effects'])
                self.assertEqual(plan['data']['effect_summary'], spec['effect_summary'])
                self.assertNotIn('PRIVATE', json.dumps(plan))
                result = self.install(identifier, plan['data']['expected_version'], key=f'app:{identifier}')
                self.assertTrue(result['data']['accepted'], result)
                self.assertFalse(result['data']['effect_confirmed'])
                self.assertEqual(self.client.calls[-1][2]['apply_id'], identifier)
                self.assertEqual(self.client.calls[-1][2]['module_name'], spec['module_name'])
                self.assertEqual(self.client.calls[-1][2]['flag'], 0)
                self.assertIsNone(result['data']['native_task_id'])

    def test_missing_legacy_field_never_gains_the_added_applications(self):
        for identifier in (7, 8, 35):
            self.choose(identifier)
            result = self.plan(identifier, fixture.PRINCIPAL)
            self.assertEqual(result['reason'], 'authorization_denied')
            self.assertIsNone(result['data'])
            self.assertEqual(self.client.calls, [])
        self.choose(34)
        self.assertTrue(self.plan(34, fixture.PRINCIPAL)['data']['allowed'])

    def test_explicit_empty_or_other_application_resource_denies_before_catalogs(self):
        for selected in ([], [34], [8, 35]):
            self.choose(7)
            principal = {**self.principal, 'application_ids': selected}
            self.assertEqual(self.plan(7, principal)['reason'], 'authorization_denied')
            self.assertEqual(self.client.calls, [])

    def test_malformed_resources_never_turn_into_all_applications(self):
        for selected in (None, '7', [True], [7, 7], [38], [7, 8, 34, 35, 7], {'7': True}):
            result = self.plan(7, {**self.principal, 'application_ids': selected})
            self.assertEqual(result['reason'], 'authorization_denied', selected)
        self.assertEqual(self.client.calls, [])

    def test_resource_list_is_part_of_plan_even_if_revision_is_unchanged(self):
        self.choose(7)
        plan = self.plan(7)['data']['expected_version']
        changed = {**self.principal, 'application_ids': [7]}
        result = self.install(7, plan, principal=changed)
        self.assertEqual(result['reason'], 'installation_plan_changed')
        self.assertEqual(result['mutation_outcome'], 'not_sent')
        self.assertNotIn('application_install', [row[0] for row in self.client.calls])

    def test_resource_order_does_not_invalidate_the_same_permission_set(self):
        self.choose(7)
        plan = self.plan(7)['data']['expected_version']
        principal = {**self.principal, 'application_ids': list(reversed(self.principal['application_ids']))}
        self.assertTrue(self.install(7, plan, principal=principal)['data']['accepted'])

    def test_resource_revocation_during_catalog_and_after_ack_discards_result(self):
        for stage in ('apply_list', 'module_inventory', 'application_install'):
            with self.subTest(stage=stage):
                self.setUp()
                self.choose(7)
                version = self.plan(7)['data']['expected_version']
                self.client.calls.clear()
                self.client.after = lambda op: self.principal.update(application_ids=[]) if op == stage else None
                result = self.install(7, version)
                self.assertIsNone(result['data'])
                self.assertEqual(result['mutation_outcome'],
                                 'unknown' if stage == 'application_install' else 'not_sent')
                if stage != 'application_install':
                    self.assertNotIn('application_install', [row[0] for row in self.client.calls])

    def test_same_key_receipt_still_requires_exact_application_resource(self):
        self.choose(7)
        version = self.plan(7)['data']['expected_version']
        self.assertTrue(self.install(7, version)['data']['accepted'])
        self.principal['application_ids'] = [34]
        result = self.install(7, version)
        self.assertEqual(result['reason'], 'authorization_denied')
        self.assertIsNone(result['data'])
        self.assertEqual(sum(row[0] == 'application_install' for row in self.client.calls), 1)

    def test_real_installed_table_membership_rejects_even_without_available_entry(self):
        for identifier in (7, 8):
            self.choose(identifier)
            inventory = self.client.frames['module_inventory']
            installed = inventory['available_list'].pop()
            installed.update(curr_ver='V1.0.0', status=4)
            inventory['installed_list'].append(installed)
            self.client.frames['apply_list']['list'][0]['status'] = 4
            plan = self.plan(identifier)
            self.assertFalse(plan['data']['allowed'])
            self.assertEqual(plan['data']['reason'], 'application_not_uninstalled')
        self.choose(35)
        self.client.frames['module_inventory']['available_list'][0]['curr_ver'] = 'V1.0.0'
        self.assertEqual(self.plan(35)['data']['reason'], 'application_not_uninstalled')

    def test_added_application_identity_and_package_conflicts_refuse_new_install(self):
        for identifier in (7, 8, 35):
            for group, field, value in (
                ('app', 'module_name', 'netdisk_bd2'), ('app', 'source', 1),
                ('app', 'module_type', 1), ('app', 'guest', True),
                ('module', 'type', 'core'), ('module', 'latest_ver', 'CHANGED'),
                ('module', 'size', 999)):
                self.choose(identifier)
                row = (self.client.frames['apply_list']['list'][0] if group == 'app'
                       else self.client.frames['module_inventory']['available_list'][0])
                row[field] = value
                self.assertFalse(self.plan(identifier)['data']['allowed'])

    def test_empty_display_label_is_never_a_native_installation_identity(self):
        self.choose(35)
        self.client.frames['apply_list']['list'][0]['module_name'] = ''
        plan = self.plan(35)
        self.assertFalse(plan['data']['allowed'])
        self.assertEqual(plan['data']['reason'], 'application_identity_changed')
        self.assertIsNone(plan['data']['expected_version'])

    def test_added_application_ack_is_exact_and_uncertain_receipts_do_not_replay(self):
        for identifier in (7, 8, 35):
            self.choose(identifier)
            version = self.plan(identifier)['data']['expected_version']
            self.client.reply = {'apply_id': 34, 'module_name': 'netdisk_bd2'}
            first = self.install(identifier, version, key=f'unknown:{identifier}')
            self.assertEqual(first['mutation_outcome'], 'unknown')
            self.client.reply = {'apply_id': identifier,
                'module_name': APPLICATION_INSTALL_SPECS[identifier]['module_name']}
            replay = self.install(identifier, version, key=f'unknown:{identifier}')
            self.assertTrue(replay['idempotent_replay'])
            self.assertEqual(replay['mutation_outcome'], 'unknown')
            self.assertEqual(sum(row[0] == 'application_install' for row in self.client.calls), 1)

    def test_transport_cannot_mix_audited_id_with_another_modules_name(self):
        for identifier in APPLICATION_INSTALL_SPECS:
            self.choose(identifier)
            plan = self.plan(identifier)['data']['expected_version']
            self.install(identifier, plan, key=f'payload:{identifier}')
            body = self.client.calls[-1][2]
            self.assertEqual(_validate('application_install', {}, body)[1], body)
            for changes in ({'module_name': 'docker'}, {'apply_id': True}, {'flag': 1},
                            {'source': 1}, {'module_type': 1}, {'guest': True},
                            {'path': '/PRIVATE'}, {'paramList': 'PRIVATE'}):
                with self.assertRaises(BusinessTransportError):
                    _validate('application_install', {}, {**body, **changes})


if __name__ == '__main__':
    unittest.main()

"""Pure role/domain policy, with no live NAS or hardware operations."""

from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import api_permissions as permissions


ROOT_ID = 'root_' + 'a' * 24
ROLES = ('readonly', 'readwrite', 'admin')
TOOL_SCOPES = {
    'get_nas_status': 'read', 'get_media_status': 'media',
    'set_fan_policy': 'fan', 'release_fan': 'fan',
    'set_light_policy': 'light', 'set_rgb': 'light',
    'set_screen': 'screen', 'update_screen_settings': 'screen',
    'request_power': 'power', 'cancel_power': 'power',
}


def principal(role, scopes):
    return {'policy_version': 2, 'role': role, 'scopes': scopes,
            'file_roots': [{'id': ROOT_ID, 'path': '', 'access': 'readonly'}]
            if 'files' in scopes else []}


class ApiPermissionTests(unittest.TestCase):
    def test_catalog_is_exact_and_future_domains_are_not_advertised_as_implemented(self):
        expected = ('read', 'fan', 'light', 'screen', 'power', 'media', 'files',
                    'docker', 'apps', 'downloads', 'movie', 'albums', 'sharing',
                    'backup', 'system')
        self.assertEqual(permissions.SCOPES, expected)
        self.assertEqual([row['id'] for row in permissions.ROLE_CATALOG], list(ROLES))
        self.assertEqual([row['id'] for row in permissions.SCOPE_CATALOG], list(expected))
        self.assertEqual(permissions.LEGACY_TOOLS, frozenset(TOOL_SCOPES))
        for row in permissions.SCOPE_CATALOG:
            with self.subTest(scope=row['id']):
                self.assertEqual(row['implemented'], row['id'] in permissions.LEGACY_SCOPES)
                self.assertIn(row['min_role'], ROLES)
                if not row['implemented']:
                    self.assertIn('待接入', row['description'])

    def test_role_domain_minimum_cartesian_matrix(self):
        # Every role/domain/minimum pair is checked both with and without the
        # explicit domain. Admin is a ceiling, never an all-domains wildcard.
        for level, role in enumerate(ROLES):
            for scope in permissions.SCOPES:
                for minimum, minimum_role in enumerate(ROLES):
                    for granted in (False, True):
                        scopes = [scope] if granted else ['read' if scope != 'read' else 'fan']
                        with self.subTest(role=role, scope=scope, minimum=minimum_role, granted=granted):
                            self.assertEqual(
                                permissions.permission_allows(principal(role, scopes), scope,
                                                              minimum_role, tool_name='future_tool'),
                                granted and level >= minimum,
                            )

    def test_existing_tools_obey_declared_minimum_roles(self):
        for name, scope in TOOL_SCOPES.items():
            minimum = permissions.MIN_ROLE_BY_TOOL[name]
            for level, role in enumerate(ROLES):
                with self.subTest(name=name, role=role):
                    self.assertEqual(permissions.permission_allows(
                        principal(role, [scope]), scope, minimum, tool_name=name),
                        level >= ROLES.index(minimum))
        self.assertEqual(permissions.MIN_ROLE_BY_TOOL['get_media_status'], 'readwrite')
        self.assertEqual(permissions.MIN_ROLE_BY_TOOL['cancel_power'], 'admin')

    def test_legacy_preserves_each_old_tool_without_granting_new_tools(self):
        for explicit in (False, True):
            for name, scope in TOOL_SCOPES.items():
                key = {'scopes': [scope]}
                if explicit:
                    key.update(policy_version=1, role='legacy', file_roots=[])
                with self.subTest(explicit=explicit, name=name):
                    self.assertTrue(permissions.permission_allows(
                        key, scope, permissions.MIN_ROLE_BY_TOOL[name], tool_name=name))
                    self.assertFalse(permissions.permission_allows(
                        key, scope, 'readonly', tool_name='new_status_tool'))
                    self.assertFalse(permissions.permission_allows(key, scope, 'readonly'))
                    self.assertFalse(permissions.permission_allows(
                        key, 'files', 'readonly', tool_name='browse_files'))
            upgraded = {'scopes': list(permissions.SCOPES)}
            if explicit:
                upgraded.update(policy_version=1, role='legacy', file_roots=[])
            self.assertFalse(permissions.permission_allows(
                upgraded, 'read', 'readonly', tool_name='get_nas_status'))

    def test_malformed_principal_or_policy_metadata_fails_closed(self):
        valid = principal('admin', ['read'])
        invalid = (None, [], 'admin', {}, {'scopes': 'read'}, {'scopes': ['*']},
                   {'scopes': ['read', 'read']}, {'scopes': ['read', []]},
                   {**valid, 'role': []}, {**valid, 'role': 'legacy'},
                   {**valid, 'role': 'ADMIN'}, {**valid, 'role': None},
                   {**valid, 'policy_version': True}, {**valid, 'policy_version': '2'},
                   {**valid, 'policy_version': 1}, {**valid, 'policy_version': 3},
                   {key: value for key, value in valid.items() if key != 'role'},
                   {key: value for key, value in valid.items() if key != 'policy_version'},
                   {key: value for key, value in valid.items() if key != 'file_roots'},
                   {**valid, 'file_roots': 'all'},
                   {'policy_version': 1, 'role': 'legacy', 'scopes': ['read'],
                    'file_roots': [{'id': ROOT_ID, 'path': '', 'access': 'readonly'}]})
        for value in invalid:
            with self.subTest(principal=value):
                self.assertFalse(permissions.permission_allows(
                    value, 'read', 'readonly', tool_name='get_nas_status'))

    def test_extending_new_tool_catalog_never_expands_fixed_legacy_tool_grants(self):
        old = {'scopes': ['read'], 'role': 'legacy', 'policy_version': 1, 'file_roots': []}
        with mock.patch.dict(permissions.MIN_ROLE_BY_TOOL, {'new_read_tool': 'readonly'}):
            self.assertNotIn('new_read_tool', permissions.LEGACY_TOOLS)
            self.assertFalse(permissions.permission_allows(
                old, 'read', 'readonly', tool_name='new_read_tool'))

    def test_unknown_scope_or_minimum_never_authorizes(self):
        for scope, minimum in (('*', 'readonly'), ('READ', 'readonly'),
                               ([], 'readonly'), (None, 'readonly'),
                               ('read', '*'), ('read', 'legacy'),
                               ('read', []), ('read', None)):
            with self.subTest(scope=scope, minimum=minimum):
                self.assertFalse(permissions.permission_allows(
                    principal('admin', ['read']), scope, minimum,
                    tool_name='get_nas_status'))

    def test_admin_still_needs_file_scope_and_explicit_roots(self):
        self.assertFalse(permissions.permission_allows(
            principal('admin', ['read']), 'files', 'readonly', tool_name='browse_files'))
        key = principal('admin', ['files'])
        key['file_roots'] = []
        self.assertFalse(permissions.permission_allows(key, 'files', 'readonly', tool_name='browse_files'))
        key['file_roots'] = [{'id': ROOT_ID, 'path': '../secret', 'access': 'readwrite'}]
        self.assertFalse(permissions.permission_allows(key, 'files', 'readonly', tool_name='browse_files'))


if __name__ == '__main__':
    unittest.main()

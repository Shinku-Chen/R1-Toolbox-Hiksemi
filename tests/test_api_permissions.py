"""Pure role/domain policy, with no live NAS or hardware operations."""

from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import api_permissions as permissions


ROOT_ID = 'root_' + 'a' * 24
PROJECT_ID = 'project_' + 'b' * 24
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


def explicit_principal(role, tools, scopes=None):
    scopes = scopes if scopes is not None else sorted({permissions.SCOPE_BY_TOOL[name] for name in tools})
    scopes = scopes or ['read']
    value = principal(role, scopes)
    value.update(policy_version=3, tool_grants=list(tools), docker_projects=[
        {'id': PROJECT_ID, 'operations': ['inspect'] if role == 'readonly' else
         ['inspect', 'update_image', 'control'] if role == 'admin' else ['inspect', 'update_image']}
    ] if 'docker' in scopes else [])
    if role != 'readonly' and value['file_roots']:
        value['file_roots'][0]['access'] = 'readwrite'
    return value


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
                self.assertEqual(row['implemented'], row['id'] in permissions.LEGACY_SCOPES
                                 + ('apps', 'downloads', 'movie', 'albums', 'sharing'))
                self.assertIn(row['min_role'], ROLES)
                if not row['implemented']:
                    self.assertIn('待接入', row['description'])

    def test_role_domain_minimum_cartesian_matrix(self):
        # Every role/domain/minimum pair is checked both with and without the
        # explicit domain. Admin is a ceiling, never an all-domains wildcard.
        for level, role in enumerate(ROLES):
            for scope in permissions.SCOPES:
                tool = next((name for name, domain in TOOL_SCOPES.items() if domain == scope), None)
                for minimum, minimum_role in enumerate(ROLES):
                    for granted in (False, True):
                        scopes = [scope] if granted else ['read' if scope != 'read' else 'fan']
                        with self.subTest(role=role, scope=scope, minimum=minimum_role, granted=granted):
                            self.assertEqual(
                                permissions.permission_allows(principal(role, scopes), scope,
                                                              minimum_role, tool_name=tool or 'future_tool'),
                                bool(tool) and granted and level >= minimum,
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

    def test_policy_v2_pre_authorized_domains_never_gain_any_of_the_new_twenty_tools(self):
        for role in ROLES:
            key = principal(role, list(permissions.SCOPES))
            for name in set(permissions.MIN_ROLE_BY_TOOL) - permissions.LEGACY_TOOLS:
                with self.subTest(role=role, tool=name):
                    self.assertFalse(permissions.permission_allows(
                        key, permissions.SCOPE_BY_TOOL[name], permissions.MIN_ROLE_BY_TOOL[name], tool_name=name))

    def test_known_catalog_matches_reviewed_twenty_and_literal_legacy_ten(self):
        from api_contracts import contract_catalog
        declared = {tool['name']: (tool['scope'], tool['min_role']) for tool in contract_catalog()['tools']}
        business = {'get_app_status': 'apps', 'get_download_status': 'downloads',
                    'get_movie_status': 'movie', 'get_album_status': 'albums',
                    'get_share_status': 'sharing'}
        operations = {'list_download_tasks', 'get_download_task', 'pause_download_task',
            'resume_download_task', 'remove_download_task', 'set_download_task_limits',
            'list_albums', 'list_shared_albums', 'create_album', 'rename_album',
            'list_movie_libraries', 'rename_movie_library', 'revoke_share', 'control_docker_project'}
        self.assertEqual(set(permissions.MIN_ROLE_BY_TOOL), set(TOOL_SCOPES) | set(declared) | set(business) | operations)
        self.assertEqual(set(permissions.SCOPE_BY_TOOL), set(permissions.MIN_ROLE_BY_TOOL))
        for name, scope in business.items():
            self.assertEqual(permissions.SCOPE_BY_TOOL[name], scope)
            self.assertEqual(permissions.MIN_ROLE_BY_TOOL[name], 'readonly')
        for name, (scope, role) in declared.items():
            self.assertEqual(permissions.SCOPE_BY_TOOL[name], scope)
            self.assertEqual(permissions.MIN_ROLE_BY_TOOL[name], role)

    def test_v3_role_domain_exact_tool_cartesian_and_unknown_names(self):
        for name, scope in permissions.SCOPE_BY_TOOL.items():
            tools = [name]
            if name == 'commit_file_upload':
                tools.append('prepare_file_upload')
            if name == 'execute_docker_project_update':
                tools.append('plan_docker_project_update')
            for level, role in enumerate(ROLES):
                for domain_granted in (False, True):
                    for tool_granted in (False, True):
                        scopes = [scope] if domain_granted else ['read' if scope != 'read' else 'fan']
                        key = explicit_principal(role, tools if tool_granted else [], scopes)
                        expected = (domain_granted and tool_granted
                                    and level >= ROLES.index(permissions.MIN_ROLE_BY_TOOL[name]))
                        with self.subTest(name=name, role=role, domain=domain_granted, tool=tool_granted):
                            self.assertEqual(permissions.permission_allows(
                                key, scope, permissions.MIN_ROLE_BY_TOOL[name], tool_name=name), expected)
        key = explicit_principal('admin', ['get_nas_status'])
        for name in (None, [], '*', 'new_future_tool'):
            self.assertFalse(permissions.permission_allows(key, 'read', 'readonly', tool_name=name))
        self.assertFalse(permissions.permission_allows(key, 'fan', 'readonly', tool_name='get_nas_status'))

    def test_v3_write_needs_writable_grant_but_copy_source_access_stays_runtime(self):
        key = explicit_principal('readwrite', ['copy_file'])
        self.assertTrue(permissions.permission_allows(key, 'files', 'readwrite', tool_name='copy_file'))
        key['file_roots'][0]['access'] = 'readonly'
        self.assertFalse(permissions.permission_allows(key, 'files', 'readwrite', tool_name='copy_file'))
        key['file_roots'].append({'id': ROOT_ID, 'path': 'target', 'access': 'readwrite'})
        self.assertTrue(permissions.permission_allows(key, 'files', 'readwrite', tool_name='copy_file'))

    def test_v3_docker_exact_operations_dependencies_and_role_ceiling(self):
        for name in ('list_docker_projects', 'get_docker_project_status', 'get_docker_project_summary',
                     'get_docker_project_task'):
            key = explicit_principal('admin', [name])
            key['docker_projects'][0]['operations'] = ['update_image']
            self.assertFalse(permissions.permission_allows(key, 'docker', 'readonly', tool_name=name))
        key = explicit_principal('admin', ['plan_docker_project_update', 'execute_docker_project_update'])
        self.assertTrue(permissions.permission_allows(key, 'docker', 'admin', tool_name='execute_docker_project_update'))
        key['tool_grants'].remove('plan_docker_project_update')
        self.assertFalse(permissions.permission_allows(key, 'docker', 'admin', tool_name='execute_docker_project_update'))
        key = explicit_principal('readwrite', [], ['docker'])
        self.assertTrue(permissions.valid_docker_projects(key['docker_projects'], 'readwrite', ['docker']))
        key['tool_grants'] = ['plan_docker_project_update']
        self.assertFalse(permissions.permission_allows(key, 'docker', 'admin', tool_name='plan_docker_project_update'))

    def test_v3_malformed_tools_or_resource_grants_fail_closed(self):
        good = explicit_principal('admin', ['get_nas_status'])
        bad = [dict(good, tool_grants=tools) for tools in (None, {}, ['*'], ['get_nas_status'] * 2, [[]])]
        bad += [dict(good, docker_projects=projects) for projects in (None, {}, [{'id': PROJECT_ID, 'operations': ['inspect']}])]
        for key in bad:
            with self.subTest(key=key):
                self.assertFalse(permissions.permission_allows(key, 'read', 'readonly', tool_name='get_nas_status'))
        project = {'id': PROJECT_ID, 'operations': ['inspect']}
        for value in (None, {}, [project, project], [{**project, 'id': 'name'}],
                      [{**project, 'operations': []}], [{**project, 'operations': ['inspect', 'inspect']}],
                      [{**project, 'operations': [[]]}], [{**project, 'operations': ['*']}],
                      [{**project, 'extra': True}], [project] * 33):
            self.assertFalse(permissions.valid_docker_projects(value, 'admin', ['docker']))
        self.assertFalse(permissions.valid_docker_projects(
            [{'id': PROJECT_ID, 'operations': ['update_image']}], 'readonly', ['docker']))
if __name__ == '__main__':
    unittest.main()

"""Draft 2020-12 fixtures validate definitions, not a real NAS backend."""

import copy
import json
from pathlib import Path
import sys
import unittest

from jsonschema import Draft202012Validator, FormatChecker


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from api_contracts import IMPLEMENTED_TOOLS, catalog, contract_catalog
from mcp_service import tool_catalog


ROOT = 'root_' + 'a' * 24
OTHER_ROOT = 'root_' + 'b' * 24
PROJECT = 'project_' + 'c' * 24
SERVICE = 'service_' + 'd' * 24
TRANSFER = 'transfer_' + 'e' * 32
OPERATION = 'operation_' + 'f' * 32
PLAN = 'plan_' + '1' * 32
TASK = 'task_' + '2' * 32
WHEN = '2026-10-29T10:00:00Z'
HASH = 'a' * 64
VERSION = 'object-version-fixture'
KEY = 'fixture-request-1'
CONTENT = '/api/integration/files/transfers/' + TRANSFER + '/content'


def entry(kind='file'):
    return {'name': '影片.mkv', 'path': '视频/影片.mkv', 'type': kind,
            'size_bytes': 1234 if kind == 'file' else None,
            'modified_at': WHEN, 'downloadable': kind == 'file'}


def operation():
    return {'operation_id': OPERATION, 'tool': 'copy_file', 'state': 'running',
            'processed_bytes': None, 'total_bytes': None,
            'result_version': None, 'updated_at': WHEN, 'error': None}


def task():
    return {'task_id': TASK, 'project_id': PROJECT, 'state': 'result_unconfirmed',
            'running': None, 'verified_target_digest': None,
            'verified_config_digest': None, 'verified_image_id': None,
            'updated_at': WHEN, 'error': None}


def transfer(direction='download'):
    return {'transfer_id': TRANSFER, 'direction': direction, 'state': 'prepared',
            'name': '影片.mkv', 'size_bytes': 1234,
            'sent_bytes': 0 if direction == 'download' else None,
            'received_bytes': 0 if direction == 'upload' else None,
            'verified_sha256': None, 'object_version': VERSION,
            'first_get_deadline': WHEN if direction == 'download' else None,
            'transfer_deadline': None, 'expires_at': None, 'error': None}


def valid_inputs():
    location = {'root_id': ROOT, 'path': '视频/验收'}
    change = {**location, 'idempotency_key': KEY}
    pair = {'source_root_id': ROOT, 'source_path': '视频/源.txt',
            'target_root_id': OTHER_ROOT, 'target_path': '验收/目标.txt',
            'expected_source_version': VERSION, 'idempotency_key': KEY}
    return {
        'list_file_roots': {},
        'list_directory': {**location, 'limit': 50, 'cursor': None},
        'get_file_info': location,
        'prepare_file_download': location,
        'get_file_transfer': {'transfer_id': TRANSFER},
        'cancel_file_transfer': {'transfer_id': TRANSFER},
        'list_docker_projects': {},
        'get_docker_project_status': {'project_id': PROJECT},
        'get_docker_project_summary': {'project_id': PROJECT},
        'prepare_file_upload': {**change, 'size_bytes': 1234, 'sha256': HASH},
        'commit_file_upload': {'transfer_id': TRANSFER, 'idempotency_key': KEY},
        'create_directory': change,
        'rename_file': {**change, 'new_name': '新名称.txt', 'expected_source_version': VERSION},
        'copy_file': pair,
        'move_file': pair,
        'trash_file': {**change, 'expected_source_version': VERSION, 'confirmed': True},
        'get_file_operation': {'operation_id': OPERATION},
        'plan_docker_project_update': {'project_id': PROJECT, 'service_id': SERVICE,
                                       'image_reference': 'registry.example/demo/app:fixture'},
        'execute_docker_project_update': {'plan_id': PLAN, 'idempotency_key': KEY, 'confirmed': True},
        'get_docker_project_task': {'project_id': PROJECT, 'task_id': TASK},
    }


def valid_outputs():
    file_op = {'ok': True, 'operation': operation(), 'may_wake_disks': True}
    return {
        'list_file_roots': {'ok': True, 'may_wake_disks': False, 'roots': [{
            'root_id': ROOT, 'label': '视频资源',
            'grants': [{'path': '视频', 'access': 'readonly'}],
            'binding_status': 'present', 'verification': 'metadata_only',
            'backend': {'status': 'backend_unverified', 'reason': '尚未核验原厂 ACL。'}}]},
        'list_directory': {'ok': True, 'root_id': ROOT, 'path': '视频',
                           'entries': [entry()],
                           'page': {'limit': 50, 'next_cursor': None, 'total': None},
                           'visibility': 'authorized_supported_only',
                           'consistency': 'best_effort', 'may_wake_disks': True},
        'get_file_info': {'ok': True, 'root_id': ROOT, 'path': '视频/影片.mkv',
                          'entry': entry(), 'may_wake_disks': True},
        'prepare_file_download': {'ok': True, 'transfer_id': TRANSFER, 'content_path': CONTENT,
                                  'name': '影片.mkv', 'size_bytes': 1234,
                                  'object_version': VERSION, 'state': 'prepared',
                                  'first_get_deadline': WHEN, 'transfer_deadline': None,
                                  'may_wake_disks': True},
        'get_file_transfer': {'ok': True, 'transfer': transfer()},
        'cancel_file_transfer': {'ok': True, 'transfer': dict(transfer(), state='cancelled')},
        'list_docker_projects': {'ok': True, 'projects': [{
            'project_id': PROJECT, 'label': '已登记项目', 'available': False,
            'reason': '后端尚未核验。'}]},
        'get_docker_project_status': {'ok': True, 'project': {
            'project_id': PROJECT, 'label': '已登记项目', 'native_status': 1,
            'pop_status': 0, 'running': None, 'health': None,
            'observed_at': WHEN, 'source': 'native'}},
        'get_docker_project_summary': {'ok': True, 'project_id': PROJECT,
            'label': '已登记项目', 'observed_at': WHEN, 'services': [{
                'service_id': SERVICE, 'name': 'app', 'image_reference': None,
                'ports': [{'published': None, 'target': 8080, 'protocol': 'tcp', 'exposure': None}],
                'mounts': [{'type': 'volume', 'access': 'readonly', 'label': '已授权数据'}],
                'restart_policy': None, 'risk_flags': ['unresolved_configuration']}]},
        'prepare_file_upload': {'ok': True, 'transfer_id': TRANSFER, 'content_path': CONTENT,
                                'direction': 'upload', 'state': 'prepared', 'size_bytes': 1234,
                                'sha256': HASH, 'overwrite': False, 'expires_at': WHEN,
                                'may_wake_disks': True},
        'commit_file_upload': file_op,
        'create_directory': file_op,
        'rename_file': file_op,
        'copy_file': file_op,
        'move_file': file_op,
        'trash_file': file_op,
        'get_file_operation': {'ok': True, 'operation': operation()},
        'plan_docker_project_update': {'ok': True, 'plan': {
            'plan_id': PLAN, 'project_id': PROJECT, 'service_id': SERVICE,
            'current_image': None, 'target_image': 'registry.example/demo/app@sha256:' + HASH,
            'target_digest': 'sha256:' + HASH, 'change': 'service_image_only',
            'expires_at': WHEN, 'requires_confirmation': True, 'restart_invalidates': True}},
        'execute_docker_project_update': {'ok': True, 'task': task()},
        'get_docker_project_task': {'ok': True, 'task': task()},
    }


class ApiContractsTests(unittest.TestCase):
    def test_file_contracts_keep_the_users_boundary_and_do_not_migrate_old_grants(self):
        boundaries = catalog()['boundaries']
        self.assertEqual(boundaries['file_path_boundary'], '/drives/*/Users/')
        self.assertIs(boundaries['legacy_whole_disk_grants_auto_migrate'], False)

    @classmethod
    def setUpClass(cls):
        cls.catalog = contract_catalog()
        cls.tools = {tool['name']: tool for tool in cls.catalog['tools']}

    def valid(self, name, value, *, output=False):
        schema = self.tools[name]['outputSchema' if output else 'inputSchema']
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)

    def invalid(self, name, value, *, output=False):
        schema = self.tools[name]['outputSchema' if output else 'inputSchema']
        self.assertFalse(Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(value),
                         msg=name)

    def test_catalog_is_detached_bounded_json_and_definitions_only(self):
        self.assertEqual(self.catalog['schema_version'], 1)
        self.assertEqual(self.catalog['policy_version'], 3)
        self.assertEqual(self.catalog['status'], 'definitions_only')
        self.assertFalse(self.catalog['execution_allowed'])
        self.assertFalse(self.catalog['mcp_registration_allowed'])
        self.assertLessEqual(len(json.dumps(self.catalog, ensure_ascii=False).encode('utf-8')), 512 * 1024)
        changed = catalog()
        changed['tools'][0]['inputSchema']['properties']['injected'] = {}
        changed['roles'][0]['label'] = 'mutated'
        self.assertEqual(contract_catalog(), self.catalog)
        self.assertEqual(catalog(), self.catalog)

    def test_twenty_definitions_register_seven_real_callbacks(self):
        self.assertEqual(set(self.tools), set(valid_inputs()))
        self.assertEqual(len(self.catalog['tools']), 20)
        self.assertEqual(sum(t['category'] == 'read' for t in self.tools.values()), 9)
        self.assertEqual(sum(t['category'] == 'file_write' for t in self.tools.values()), 8)
        self.assertEqual(sum(t['category'] == 'docker_update' for t in self.tools.values()), 3)
        active = {item['name'] for item in tool_catalog()}
        self.assertEqual(len(active), 70)
        self.assertEqual(active.intersection(self.tools), IMPLEMENTED_TOOLS)
        self.assertFalse(active.intersection(name for name, tool in self.tools.items() if not tool['implemented']))

    def test_all_schemas_validate_and_only_close_instance_objects(self):
        def walk(value):
            if isinstance(value, dict):
                if value.get('type') == 'object':
                    self.assertIs(value.get('additionalProperties'), False)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        for name, tool in self.tools.items():
            with self.subTest(name=name):
                for which in ('inputSchema', 'outputSchema'):
                    schema = tool[which]
                    Draft202012Validator.check_schema(schema)
                    walk(schema)
        Draft202012Validator.check_schema(self.catalog['errorSchema'])

    def test_standalone_schemas_embed_only_reachable_definitions(self):
        def references(node):
            found = set()
            if isinstance(node, dict):
                if '$ref' in node:
                    found.add(node['$ref'].removeprefix('#/$defs/'))
                for key, child in node.items():
                    if key != '$defs':
                        found.update(references(child))
            elif isinstance(node, list):
                for child in node:
                    found.update(references(child))
            return found
        for name, tool in self.tools.items():
            for key in ('inputSchema', 'outputSchema'):
                schema = tool[key]
                needed = references(schema)
                previous = set()
                while needed != previous:
                    previous = set(needed)
                    for definition in list(needed):
                        needed.update(references(schema['$defs'][definition]))
                self.assertEqual(set(schema.get('$defs', {})), needed, (name, key))

    def test_every_tool_has_valid_input_and_nonplaceholder_output_fixture(self):
        for name, value in valid_inputs().items():
            with self.subTest(name=name, direction='input'):
                self.valid(name, value)
        for name, value in valid_outputs().items():
            with self.subTest(name=name, direction='output'):
                self.valid(name, value, output=True)

    def test_every_tool_rejects_missing_required_and_extra_fields(self):
        for name, tool in self.tools.items():
            with self.subTest(name=name):
                value = copy.deepcopy(valid_inputs()[name])
                self.invalid(name, dict(value, shell='forbidden'))
                for required in tool['inputSchema']['required']:
                    missing = dict(value)
                    missing.pop(required)
                    self.invalid(name, missing)
                self.invalid(name, dict(valid_outputs()[name], native_token='forbidden'), output=True)
                self.invalid(name, {'ok': True}, output=True)

    def test_paths_reject_aliases_controls_surrogates_and_overlong_strings(self):
        invalid = ('/etc', '.', '..', 'a/../b', 'a/./b', 'a//b', 'a/', r'a\b',
                   '\x00', 'a\nb', '\x7f', '\ud800', 'a' * 1025)
        for path in invalid:
            with self.subTest(path=repr(path)):
                self.invalid('get_file_info', {'root_id': ROOT, 'path': path})
        for path in ('', '视频 资源/影片.txt', 'a..b', '😀' * 1024):
            with self.subTest(valid_length=len(path)):
                self.valid('get_file_info', {'root_id': ROOT, 'path': path})
                self.assertLessEqual(len(path.encode('utf-8')), 4096)
        self.invalid('create_directory', {'root_id': ROOT, 'path': '', 'idempotency_key': KEY})

    def test_component_name_cannot_be_path_or_dot(self):
        for name in ('', '.', '..', 'a/b', r'a\b', '\n', '\udfff', 'a' * 256):
            value = dict(valid_inputs()['rename_file'], new_name=name)
            with self.subTest(name=repr(name)):
                self.invalid('rename_file', value)
        self.valid('rename_file', dict(valid_inputs()['rename_file'], new_name='新 名称.txt'))

    def test_ids_are_exact_typed_prefixes_and_fixed_lengths(self):
        for tool, key in (('get_file_info', 'root_id'),
                          ('get_docker_project_status', 'project_id'),
                          ('get_file_transfer', 'transfer_id'),
                          ('get_file_operation', 'operation_id'),
                          ('execute_docker_project_update', 'plan_id'),
                          ('get_docker_project_task', 'task_id')):
            fixture = valid_inputs()[tool]
            for value in (True, None, fixture[key] + '\n', fixture[key].upper(),
                          fixture[key] + 'a', fixture[key][:-1], ROOT if key != 'root_id' else PROJECT):
                with self.subTest(tool=tool, value=repr(value)):
                    self.invalid(tool, dict(fixture, **{key: value}))

    def test_pagination_is_bounded_and_boolean_is_not_integer(self):
        fixture = valid_inputs()['list_directory']
        for limit in (True, False, 0, 201, 1.5, '50'):
            self.invalid('list_directory', dict(fixture, limit=limit))
        for cursor in ('', '中文', 'a\nb', 'a' * 2049, 1):
            self.invalid('list_directory', dict(fixture, cursor=cursor))
        self.valid('list_directory', dict(fixture, limit=200, cursor='fixture-cursor'))

    def test_safe_sizes_and_sha256(self):
        fixture = valid_inputs()['prepare_file_upload']
        for size in (-1, True, 1.25, 2 ** 53, '1234'):
            self.invalid('prepare_file_upload', dict(fixture, size_bytes=size))
        for size in (0, 2 ** 53 - 1):
            self.valid('prepare_file_upload', dict(fixture, size_bytes=size))
        for digest in ('', 'A' * 64, 'g' * 64, HASH + '\n', 'sha256:' + HASH):
            self.invalid('prepare_file_upload', dict(fixture, sha256=digest))

    def test_overwrite_requires_explicit_true_confirmation_and_target_version(self):
        fixture = valid_inputs()['prepare_file_upload']
        self.valid('prepare_file_upload', dict(fixture, overwrite=False))
        overwrite = dict(fixture, overwrite=True, expected_target_version=VERSION, confirmed=True)
        self.valid('prepare_file_upload', overwrite)
        for field in ('confirmed', 'expected_target_version'):
            value = dict(overwrite)
            del value[field]
            self.invalid('prepare_file_upload', value)
        for confirmation in (False, 1, 'true', None):
            self.invalid('prepare_file_upload', dict(overwrite, confirmed=confirmation))
        self.invalid('prepare_file_upload', dict(fixture, confirmed=True))
        self.invalid('prepare_file_upload', dict(fixture, expected_target_version=VERSION))
        self.invalid('prepare_file_upload', dict(overwrite, overwrite=False))

    def test_trash_and_execute_require_true_not_truthy(self):
        for name in ('trash_file', 'execute_docker_project_update'):
            for value in (False, 1, 'true', None):
                self.invalid(name, dict(valid_inputs()[name], confirmed=value))
        self.invalid('execute_docker_project_update',
                     dict(valid_inputs()['execute_docker_project_update'], image_reference='other/image:tag'))

    def test_write_idempotency_is_required_and_does_not_authorize_queries(self):
        for name in ('prepare_file_upload', 'commit_file_upload', 'create_directory',
                     'rename_file', 'copy_file', 'move_file', 'trash_file',
                     'execute_docker_project_update'):
            tool = self.tools[name]
            self.assertEqual(tool['idempotency']['mode'], 'required_key')
            self.assertEqual(tool['idempotency']['same_key_changed_arguments'], 'idempotency_conflict')
            for value in ('', 'a' * 129, 'a\nb', '中文', True):
                self.invalid(name, dict(valid_inputs()[name], idempotency_key=value))
        self.invalid('get_file_operation', {'operation_id': OPERATION, 'idempotency_key': KEY})

    def test_structured_authorization_is_not_an_executable_grant(self):
        for name, tool in self.tools.items():
            authorization = tool['authorization']
            self.assertEqual(authorization['policy_versions'], [3])
            self.assertEqual(authorization['tool_grant'], name)
            self.assertEqual(authorization['minimum_role'], tool['min_role'])
            self.assertEqual(authorization['scope'], tool['scope'])
            for required in ('runtime_acl_required', 'current_credential_required',
                             'current_expiry_required', 'grant_revision_required', 'unknown_denied'):
                self.assertIs(authorization[required], True)
            self.assertEqual(tool['implemented'], name in IMPLEMENTED_TOOLS)
            self.assertFalse(tool['execution_allowed'])
            if name in IMPLEMENTED_TOOLS:
                self.assertIsNone(tool['unimplemented_reason'])
            else:
                self.assertEqual(tool['unimplemented_reason']['code'], 'not_implemented')
                self.assertTrue(tool['unimplemented_reason']['required_gates'])
            self.assertFalse(authorization['admin_bypass_allowed'])

    def test_transfer_query_cancel_keep_original_upload_write_authority(self):
        for name in ('get_file_transfer', 'cancel_file_transfer'):
            rule = self.tools[name]['authorization']['resource_rules'][0]
            self.assertTrue(rule['same_key_and_generation'])
            self.assertEqual(rule['direction_from'], 'trusted_transfer_record')
            conditions = {item['direction']: item for item in rule['conditional_requirements']}
            self.assertEqual(conditions['upload']['minimum_role'], 'readwrite')
            self.assertEqual(conditions['upload']['tool_grants'], ['prepare_file_upload'])
            self.assertEqual(conditions['upload']['original_resource_access'], 'write')
            self.assertEqual(conditions['download']['original_resource_access'], 'read')
        query = self.tools['get_file_operation']
        self.assertEqual(query['min_role'], 'readwrite')
        rule = query['authorization']['resource_rules'][0]
        self.assertTrue(rule['original_tool_grant_required'])
        self.assertTrue(rule['original_resources_required'])

    def test_copy_move_and_project_task_resource_differences(self):
        copy_rules = self.tools['copy_file']['authorization']['resource_rules']
        move_rules = self.tools['move_file']['authorization']['resource_rules']
        self.assertEqual([rule['access'] for rule in copy_rules[:2]], ['read', 'write'])
        self.assertEqual([rule['access'] for rule in move_rules[:2]], ['write', 'write'])
        self.assertFalse(move_rules[2]['copy_then_delete_fallback_allowed'])
        self.assertTrue(move_rules[2]['atomic_move_required'])
        rules = self.tools['get_docker_project_task']['authorization']['resource_rules']
        self.assertEqual(rules[0]['operation'], 'inspect')
        self.assertTrue(rules[1]['same_key_and_generation'])
        self.assertTrue(rules[1]['same_project_required'])

    def test_output_preserves_null_and_rejects_fake_directory_size_or_leaks(self):
        info = copy.deepcopy(valid_outputs()['get_file_info'])
        info['entry'] = entry('directory')
        self.valid('get_file_info', info, output=True)
        info['entry']['size_bytes'] = 0
        self.invalid('get_file_info', info, output=True)
        info = copy.deepcopy(valid_outputs()['get_file_info'])
        info['entry']['size_bytes'] = None
        self.invalid('get_file_info', info, output=True)
        info['entry']['downloadable'] = False
        self.valid('get_file_info', info, output=True)
        summary = copy.deepcopy(valid_outputs()['get_docker_project_summary'])
        for key in ('environment', 'compose', 'host_path', 'access_token'):
            bad = copy.deepcopy(summary)
            bad['services'][0][key] = 'forbidden'
            self.invalid('get_docker_project_summary', bad, output=True)

    def test_transfer_output_distinguishes_upload_and_download(self):
        self.valid('get_file_transfer', {'ok': True, 'transfer': transfer('upload')}, output=True)
        for direction, field, value in (('upload', 'state', 'streaming'),
                                         ('download', 'state', 'receiving'),
                                         ('upload', 'sent_bytes', 1),
                                         ('download', 'received_bytes', 1)):
            data = transfer(direction)
            data[field] = value
            self.invalid('get_file_transfer', {'ok': True, 'transfer': data}, output=True)

    def test_fixed_errors_validate_for_every_tool_without_raw_payloads(self):
        failure = {'ok': False, 'error': {'code': 'not_implemented',
                                         'message': '仅接口定义，未实现。', 'retryable': False}}
        for name in self.tools:
            self.valid(name, failure, output=True)
        for error in ({'code': 'arbitrary_native_message', 'message': 'x', 'retryable': False},
                      {'code': 'not_found', 'message': 'x', 'retryable': 1},
                      {'code': 'not_found', 'message': 'x', 'retryable': False, 'body': 'raw'}):
            self.invalid('get_file_info', {'ok': False, 'error': error}, output=True)

    def test_http_contract_cannot_be_mistaken_for_a_registered_endpoint(self):
        http = self.catalog['http_contracts'][0]
        self.assertFalse(http['implemented'])
        self.assertFalse(http['execution_allowed'])
        self.assertFalse(http['query_allowed'])
        self.assertTrue(http['bearer_header_required'])
        self.assertEqual(http['directions']['download']['methods'], ['GET', 'HEAD'])
        self.assertEqual(http['directions']['upload']['methods'], ['PUT'])
        self.assertFalse(http['directions']['upload']['automatic_commit_allowed'])
        self.assertFalse(http['binary_in_mcp_allowed'])
        self.assertEqual(http['max_stream_block_bytes'], 65536)

    def test_image_reference_rejects_url_credentials_interpolation_and_shell(self):
        fixture = valid_inputs()['plan_docker_project_update']
        for image in ('https://registry.example/image:tag', 'user:pass@registry/image',
                      '${IMAGE}', 'image;command', '-option', 'image\n', 'a' * 513):
            self.invalid('plan_docker_project_update', dict(fixture, image_reference=image))
        for image in ('example/app:tag', 'registry.example:5000/team/app:tag',
                      'registry.example/app@sha256:' + HASH):
            self.valid('plan_docker_project_update', dict(fixture, image_reference=image))


if __name__ == '__main__':
    unittest.main()

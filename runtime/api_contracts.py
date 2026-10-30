"""Reviewable file/Docker interface definitions; no execution or authorization.

These schemas describe future APIs. They are not an MCP catalog, an ACL checker
or evidence that a native backend works. This module performs no I/O and imports
no service, credential reader or callback. Runtime authorization must use trusted
resource records and live objects, never interpret descriptive text as a grant.
"""

import copy

from api_permissions import ROLE_CATALOG


_DRAFT = 'https://json-schema.org/draft/2020-12/schema'
_SAFE_INTEGER = 2 ** 53 - 1
# Only these contracts have concrete callbacks. Definitions never grant access.
IMPLEMENTED_TOOLS = frozenset((
    'list_file_roots', 'list_docker_projects',
    'get_docker_project_status', 'get_docker_project_summary',
    'plan_docker_project_update', 'execute_docker_project_update', 'get_docker_project_task',
    'list_directory', 'get_file_info', 'prepare_file_download', 'get_file_transfer', 'cancel_file_transfer',
    'prepare_file_upload', 'commit_file_upload', 'get_file_operation',
))
_ERROR_CODES = (
    'not_implemented', 'invalid_arguments', 'invalid_key', 'scope_denied',
    'role_denied', 'tool_denied', 'permission_denied', 'root_unavailable',
    'acl_unverified', 'secure_open_unavailable', 'unsupported_file_type',
    'not_found', 'directory_changed', 'file_changed', 'transfer_expired',
    'transfer_busy', 'transfer_unavailable', 'unsupported_range',
    'unsupported_precondition', 'unsupported_path_class', 'target_exists', 'target_changed',
    'checksum_mismatch', 'recycle_unavailable', 'write_backend_unverified',
    'idempotency_conflict', 'project_unavailable', 'project_binding_changed',
    'native_unavailable', 'plan_expired', 'plan_conflict', 'task_unavailable',
    'resource_busy', 'limit_exceeded', 'timeout', 'result_unconfirmed',
)


def _ref(name):
    return {'$ref': '#/$defs/' + name}


def _nullable(value):
    return {'anyOf': [value, {'type': 'null'}]}


def _text(maximum=256, minimum=1, **kwargs):
    return {'type': 'string', 'minLength': minimum, 'maxLength': maximum,
            'pattern': r'^[^\x00-\x1f\x7f-\x9f\ud800-\udfff]*$(?![\s\S])',
            **kwargs}


def _integer(maximum=_SAFE_INTEGER, minimum=0):
    return {'type': 'integer', 'minimum': minimum, 'maximum': maximum}


def _object(properties=None, required=None, **kwargs):
    properties = properties or {}
    return {'type': 'object', 'properties': properties,
            'required': list(properties if required is None else required),
            'additionalProperties': False, **kwargs}


def _array(items, maximum=200):
    return {'type': 'array', 'items': items, 'maxItems': maximum}


def _identifier(prefix, digits):
    length = len(prefix) + digits
    return _text(length, length, pattern='^' + prefix + '[0-9a-f]{' + str(digits) + '}$')


_PATH_PATTERN = (r'^(?!(?:.*/)?(?:\.|\.\.)(?:/|$))'
                 r'(?:[^/\\\x00-\x1f\x7f-\x9f\ud800-\udfff]+'
                 r'(?:/[^/\\\x00-\x1f\x7f-\x9f\ud800-\udfff]+)*)?$(?![\s\S])')
_IMAGE_PATTERN = (r'^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?'
                  r'(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*'
                  r'(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}'
                  r'|@sha256:[0-9a-f]{64})?$(?![\s\S])')
_DOWNLOAD_STATES = ['prepared', 'streaming', 'ready', 'interrupted',
                    'completed', 'cancelled', 'expired', 'failed']
_UPLOAD_STATES = ['prepared', 'receiving', 'interrupted', 'verified',
                  'committing', 'completed', 'cancelled', 'expired', 'failed']
_FILE_WRITE_TOOLS = [
    'prepare_file_upload', 'commit_file_upload', 'create_directory',
    'rename_file', 'copy_file', 'move_file', 'trash_file',
]

_DEFS = {
    'root_id': _identifier('root_', 24),
    'project_id': _identifier('project_', 24),
    'service_id': _identifier('service_', 24),
    'transfer_id': _identifier('transfer_', 32),
    'operation_id': _identifier('operation_', 32),
    'plan_id': _identifier('plan_', 32),
    'task_id': _identifier('task_', 32),
    'safe_integer': _integer(),
    'nullable_integer': _nullable(_integer()),
    'timestamp': _text(40, format='date-time', pattern=r'Z$(?![\s\S])'),
    'nullable_timestamp': _nullable(_ref('timestamp')),
    'path': _text(1024, 0, pattern=_PATH_PATTERN, **{
        'x-maxUtf8Bytes': 4096,
        'description': '相对可信物理根的 POSIX 路径；1024 个 Unicode 标量最多 4096 个 UTF-8 字节。'}),
    'change_path': _text(1024, pattern=_PATH_PATTERN, **{
        'x-maxUtf8Bytes': 4096,
        'description': '非空相对路径；变更不能以授权物理根本身为对象。'}),
    'file_name': _text(255, pattern=(
        r'^(?!\.{1,2}$(?![\s\S]))[^/\\\x00-\x1f\x7f-\x9f\ud800-\udfff]+$(?![\s\S])')),
    'cursor': _nullable(_text(2048, pattern=r'^[\x21-\x7e]+$(?![\s\S])')),
    'object_version': _text(256, pattern=r'^[A-Za-z0-9_.:-]+$(?![\s\S])',
                            description='仅作已核验对象的版本绑定；不是强内容 ETag。'),
    'sha256': _text(64, 64, pattern=r'^[0-9a-f]{64}$'),
    'image_reference': _text(512, pattern=_IMAGE_PATTERN,
                             description='受限字面量镜像引用；仍须核验已登记仓库和不可变 digest。'),
    'image_digest': _text(71, 71, pattern=r'^sha256:[0-9a-f]{64}$'),
    'idempotency_key': _text(128, pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\s\S])'),
    'content_path': _text(110, pattern=(
        r'^/api/integration/files/transfers/transfer_[0-9a-f]{32}/content$(?![\s\S])')),
    'error': _object({'code': {'type': 'string', 'enum': list(_ERROR_CODES)},
                      'message': _text(256), 'retryable': {'type': 'boolean'}}),
    'failure': _object({'ok': {'const': False}, 'error': _ref('error')}),
    'file_grant': _object({'path': _ref('path'),
                           'access': {'enum': ['readonly', 'readwrite']}}),
    'backend_status': _object({
        'status': {'enum': ['available', 'backend_unverified', 'resource_unavailable']},
        'reason': _nullable(_text(256))}),
    'file_root': _object({
        'root_id': _ref('root_id'), 'label': _nullable(_text(128)),
        'grants': _array(_ref('file_grant'), 32),
        'binding_status': {'enum': ['present', 'unavailable']},
        'verification': {'const': 'metadata_only'}, 'backend': _ref('backend_status')}),
    'file_entry': _object({
        'name': _ref('file_name'), 'path': _ref('path'),
        'type': {'enum': ['file', 'directory']},
        'size_bytes': _ref('nullable_integer'),
        'modified_at': _ref('nullable_timestamp'), 'downloadable': {'type': 'boolean'}},
        allOf=[
            {'if': {'properties': {'type': {'const': 'directory'}}, 'required': ['type']},
             'then': {'properties': {'size_bytes': {'type': 'null'},
                                     'downloadable': {'const': False}}}},
            {'if': {'properties': {'downloadable': {'const': True}}, 'required': ['downloadable']},
             'then': {'properties': {'type': {'const': 'file'}, 'size_bytes': _integer()}}},
        ]),
    'directory_page': _object({
        'limit': _integer(200, 1), 'next_cursor': _ref('cursor'),
        'total': _ref('nullable_integer')}),
    'transfer': _object({
        'transfer_id': _ref('transfer_id'), 'direction': {'enum': ['download', 'upload']},
        'state': {'enum': sorted(set(_DOWNLOAD_STATES + _UPLOAD_STATES))},
        'name': _ref('file_name'), 'size_bytes': _integer(),
        'sent_bytes': _ref('nullable_integer'), 'received_bytes': _ref('nullable_integer'),
        'verified_sha256': _nullable(_ref('sha256')),
        'object_version': _nullable(_ref('object_version')),
        'first_get_deadline': _ref('nullable_timestamp'),
        'transfer_deadline': _ref('nullable_timestamp'),
        'expires_at': _ref('nullable_timestamp'), 'error': _nullable(_ref('error'))},
        allOf=[
            {'if': {'properties': {'direction': {'const': 'download'}}, 'required': ['direction']},
             'then': {'properties': {'state': {'enum': _DOWNLOAD_STATES},
                                     'received_bytes': {'type': 'null'},
                                     'verified_sha256': {'type': 'null'},
                                     'sent_bytes': _integer()}}},
            {'if': {'properties': {'direction': {'const': 'upload'}}, 'required': ['direction']},
             'then': {'properties': {'state': {'enum': _UPLOAD_STATES},
                                     'sent_bytes': {'type': 'null'},
                                     'first_get_deadline': {'type': 'null'},
                                     'received_bytes': _integer()}}},
        ]),
    'file_operation': _object({
        'operation_id': _ref('operation_id'), 'tool': {'enum': _FILE_WRITE_TOOLS},
        'state': {'enum': ['pending', 'running', 'completed', 'failed',
                           'interrupted', 'result_unconfirmed']},
        'processed_bytes': _ref('nullable_integer'), 'total_bytes': _ref('nullable_integer'),
        'result_version': _nullable(_ref('object_version')),
        'updated_at': _ref('nullable_timestamp'), 'error': _nullable(_ref('error'))}),
    'project': _object({'project_id': _ref('project_id'), 'label': _nullable(_text(128)),
                        'available': {'type': 'boolean'}, 'reason': _nullable(_text(256))}),
    'project_status': _object({
        'project_id': _ref('project_id'), 'label': _nullable(_text(128)),
        'native_status': _nullable(_integer(_SAFE_INTEGER, -1)),
        'pop_status': _ref('nullable_integer'), 'running': _nullable({'type': 'boolean'}),
        'health': _nullable({'enum': ['healthy', 'unhealthy', 'starting', 'not_configured']}),
        'observed_at': _ref('nullable_timestamp'),
        'source': {'enum': ['native', 'engine', 'native_and_engine']}}),
    'port': _object({'published': _nullable(_integer(65535, 1)),
                     'target': _nullable(_integer(65535, 1)),
                     'protocol': _nullable({'enum': ['tcp', 'udp']}),
                     'exposure': _nullable({'enum': ['loopback', 'lan', 'all_interfaces']})}),
    'mount_summary': _object({'type': {'enum': ['bind', 'volume', 'tmpfs']},
                              'access': _nullable({'enum': ['readonly', 'readwrite']}),
                              'label': _nullable(_text(128))}),
    'service_summary': _object({
        'service_id': _ref('service_id'), 'name': _text(128),
        'image_reference': _nullable(_ref('image_reference')),
        'ports': _array(_ref('port'), 128), 'mounts': _array(_ref('mount_summary'), 128),
        'restart_policy': _nullable({'enum': ['no', 'always', 'unless-stopped', 'on-failure']}),
        'risk_flags': {'type': 'array', 'maxItems': 16, 'uniqueItems': True,
                       'items': {'enum': ['privileged', 'host_network', 'devices',
                                          'host_mounts', 'capabilities', 'unresolved_configuration']}}}),
    'update_plan': _object({
        'plan_id': _ref('plan_id'), 'project_id': _ref('project_id'),
        'service_id': _ref('service_id'),
        'current_image': _nullable(_ref('image_reference')),
        'target_image': _ref('image_reference'), 'target_digest': _ref('image_digest'),
        'change': {'const': 'service_image_only'}, 'expires_at': _ref('timestamp'),
        'requires_confirmation': {'const': True}, 'restart_invalidates': {'const': True}}),
    'project_task': _object({
        'task_id': _ref('task_id'), 'project_id': _ref('project_id'),
        'state': {'enum': ['pending', 'running', 'completed', 'failed',
                           'conflict', 'result_unconfirmed']},
        'running': _nullable({'type': 'boolean'}),
        'verified_target_digest': _nullable(_ref('image_digest')),
        'verified_config_digest': _nullable(_ref('image_digest')),
        'verified_image_id': _nullable(_ref('image_digest')),
        'updated_at': _ref('nullable_timestamp'), 'error': _nullable(_ref('error'))}),
}


def _standalone(value):
    """Embed only reachable definitions, so every individual schema is portable."""
    result = {'$schema': _DRAFT, **copy.deepcopy(value)}
    found = set()

    def visit(node):
        if isinstance(node, dict):
            reference = node.get('$ref')
            if isinstance(reference, str) and reference.startswith('#/$defs/'):
                name = reference.removeprefix('#/$defs/')
                if name not in found:
                    found.add(name)
                    visit(_DEFS[name])
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    if found:
        result['$defs'] = {name: copy.deepcopy(_DEFS[name]) for name in sorted(found)}
    return result


def _success(properties):
    return _standalone({'oneOf': [
        _object({'ok': {'const': True}, **properties}), _ref('failure')]})


def _file_rule(root='root_id', path='path', access='read', **extra):
    return {'kind': 'file', 'root_field': root, 'path_field': path, 'access': access,
            'component_boundary': True, 'live_identity_required': True,
            'native_subject_acl_required': True, 'same_opened_object_required': True,
            'root_fallback_allowed': False, **extra}


def _transfer_rule():
    return {'kind': 'owned_transfer', 'id_field': 'transfer_id',
            'same_key_and_generation': True, 'same_peer': True,
            'original_grant_revision': True, 'direction_from': 'trusted_transfer_record',
            'conditional_requirements': [
                {'direction': 'download', 'minimum_role': 'readonly',
                 'tool_grants': ['prepare_file_download'], 'original_resource_access': 'read'},
                {'direction': 'upload', 'minimum_role': 'readwrite',
                 'tool_grants': ['prepare_file_upload'], 'original_resource_access': 'write'},
            ]}


def _project_rule(operation='inspect'):
    return {'kind': 'registered_project', 'id_field': 'project_id',
            'operation': operation, 'live_identity_required': True,
            'same_engine_config_and_service_identity': True,
            'same_name_rebinding_allowed': False}


def _tool(name, title, scope, role, arguments, output, resources, *,
          category='read', idempotency='read_only', gates=None):
    return {
        'name': name, 'title': title, 'scope': scope, 'min_role': role,
        'category': category, 'implemented': name in IMPLEMENTED_TOOLS,
        'execution_allowed': False,
        'status': 'registered_requires_authorization' if name in IMPLEMENTED_TOOLS else 'definitions_only',
        'unimplemented_reason': None if name in IMPLEMENTED_TOOLS else {
            'code': 'not_implemented',
            'message': '仅有接口定义，尚未注册执行器；不证明原厂后端或实机能力。',
            'required_gates': gates or ['native_contract', 'live_resource_identity',
                                        'runtime_authorization', 'automated_and_device_acceptance']},
        'inputSchema': _standalone(arguments), 'outputSchema': output,
        'authorization': {
            'policy_versions': [3], 'minimum_role': role, 'scope': scope,
            'tool_grant': name, 'bearer_only': True, 'real_peer_required': True,
            'mcp_enabled_required': True, 'current_credential_required': True,
            'current_expiry_required': True, 'grant_revision_required': True,
            'runtime_acl_required': True, 'unknown_denied': True,
            'admin_bypass_allowed': False,
            'browser_cookie_or_developer_window_allowed': False,
            'resource_rules': resources,
        },
        'idempotency': {
            'mode': idempotency,
            'same_key_same_arguments_returns_original': idempotency == 'required_key',
            'same_key_changed_arguments': 'idempotency_conflict' if idempotency == 'required_key' else None,
            'automatic_retry_allowed': False,
        },
    }


_LOCATION = {'root_id': _ref('root_id'), 'path': _ref('path')}
_CHANGE_LOCATION = {'root_id': _ref('root_id'), 'path': _ref('change_path')}
_IDEMPOTENCY = {'idempotency_key': _ref('idempotency_key')}
_TRANSFER_INPUT = _object({'transfer_id': _ref('transfer_id')})
_PROJECT_INPUT = _object({'project_id': _ref('project_id')})
_OPERATION_OUTPUT = _success({'operation': _ref('file_operation'), 'may_wake_disks': {'const': True}})
_FILE_GATES = ['native_path_mapping', 'nas_subject_uid_gid_and_acl',
               'safe_open_same_object', 'namespace_race_proof', 'read_only_device_acceptance']
_WRITE_GATES = _FILE_GATES[:-1] + ['native_index_quota_atomicity', 'isolated_write_acceptance']

_TOOLS = [
    _tool('list_file_roots', '列出已授权文件根', 'files', 'readonly', _object(),
          _success({'roots': _array(_ref('file_root'), 32), 'may_wake_disks': {'const': False}}),
          [{'kind': 'configured_file_grants', 'selected_only': True,
            'verification': 'metadata_only', 'filesystem_access_allowed': False}],
          gates=['policy_v3_explicit_grants', 'metadata_projection']),
    _tool('list_directory', '浏览已授权目录', 'files', 'readonly',
          _object({**_LOCATION, 'limit': {**_integer(200, 1), 'default': 50},
                   'cursor': _ref('cursor')}, ['root_id', 'path']),
          _success({'root_id': _ref('root_id'), 'path': _ref('path'),
                    'entries': _array(_ref('file_entry')), 'page': _ref('directory_page'),
                    'visibility': {'const': 'authorized_supported_only'},
                    'consistency': {'const': 'best_effort'}, 'may_wake_disks': {'const': True}}),
          [_file_rule(), {'kind': 'directory_page', 'selected_children_only': True,
                         'unfiltered_total_allowed': False, 'same_key_and_generation': True,
                         'cursor_grants_access': False, 'max_json_utf8_bytes': 131072}], gates=_FILE_GATES),
    _tool('get_file_info', '读取文件属性', 'files', 'readonly', _object(_LOCATION),
          _success({'root_id': _ref('root_id'), 'path': _ref('path'),
                    'entry': _ref('file_entry'), 'may_wake_disks': {'const': True}}),
          [_file_rule()], gates=_FILE_GATES),
    _tool('prepare_file_download', '准备文件下载', 'files', 'readonly', _object(_LOCATION),
          _success({'transfer_id': _ref('transfer_id'), 'content_path': _ref('content_path'),
                    'name': _ref('file_name'), 'size_bytes': _integer(),
                    'object_version': _ref('object_version'), 'state': {'const': 'prepared'},
                    'first_get_deadline': _ref('timestamp'), 'transfer_deadline': {'type': 'null'},
                    'may_wake_disks': {'const': True}}),
          [_file_rule(), {'kind': 'new_transfer', 'direction': 'download',
                         'same_key_and_generation': True, 'same_peer': True,
                         'original_grant_revision': True}],
          idempotency='bounded_resource_allocation', gates=_FILE_GATES),
    _tool('get_file_transfer', '查询文件传输', 'files', 'readonly', _TRANSFER_INPUT,
          _success({'transfer': _ref('transfer')}), [_transfer_rule()]),
    _tool('cancel_file_transfer', '取消本键文件传输', 'files', 'readonly', _TRANSFER_INPUT,
          _success({'transfer': _ref('transfer')}), [_transfer_rule()],
          idempotency='terminal_state_replay'),
    _tool('list_docker_projects', '列出已授权项目', 'docker', 'readonly', _object(),
          _success({'projects': _array(_ref('project'), 128)}),
          [{**_project_rule(), 'id_field': None, 'selected_only': True}]),
    _tool('get_docker_project_status', '读取项目状态', 'docker', 'readonly', _PROJECT_INPUT,
          _success({'project': _ref('project_status')}), [_project_rule()]),
    _tool('get_docker_project_summary', '读取项目脱敏摘要', 'docker', 'readonly', _PROJECT_INPUT,
          _success({'project_id': _ref('project_id'), 'label': _nullable(_text(128)),
                    'services': _array(_ref('service_summary'), 128),
                    'observed_at': _ref('nullable_timestamp')}), [_project_rule()]),
]

_UPLOAD_ARGUMENTS = _object({
    **_CHANGE_LOCATION, 'size_bytes': _integer(8 * 1024 ** 3), 'sha256': _ref('sha256'),
    **_IDEMPOTENCY, 'overwrite': {'const': False, 'default': False}},
    ['root_id', 'path', 'size_bytes', 'sha256', 'idempotency_key'])

_TOOLS.extend([
    _tool('prepare_file_upload', '准备文件上传', 'files', 'readwrite', _UPLOAD_ARGUMENTS,
          _success({'transfer_id': _ref('transfer_id'), 'content_path': _ref('content_path'),
                    'direction': {'const': 'upload'}, 'state': {'const': 'prepared'},
                    'size_bytes': _integer(8 * 1024 ** 3), 'sha256': _ref('sha256'),
                    'overwrite': {'const': False}, 'expires_at': _ref('timestamp'),
                    'may_wake_disks': {'const': True}}),
          [_file_rule(access='write', parent_access='write', root_mutation_allowed=False,
                      native_path_class='owner_admin_descendants'),
           {'kind': 'new_transfer', 'direction': 'upload', 'same_key_and_generation': True,
            'same_peer': True, 'original_grant_revision': True,
            'overwrite_allowed': False}],
          category='file_write', idempotency='required_key', gates=_WRITE_GATES),
    _tool('commit_file_upload', '提交已校验上传', 'files', 'readwrite',
          _object({'transfer_id': _ref('transfer_id'), **_IDEMPOTENCY}), _OPERATION_OUTPUT,
          [{'kind': 'owned_upload', 'id_field': 'transfer_id', 'same_key_and_generation': True,
            'same_peer': True, 'original_grant_revision': True,
            'required_tool_grants': ['prepare_file_upload'], 'original_resource_access': 'write',
            'original_target_and_overwrite_only': True, 'verified_size_and_sha256_required': True,
            'atomic_install_and_native_index_required': True}],
          category='file_write', idempotency='required_key', gates=_WRITE_GATES),
    _tool('create_directory', '新建单个目录', 'files', 'readwrite',
          _object({**_CHANGE_LOCATION, **_IDEMPOTENCY}), _OPERATION_OUTPUT,
          [_file_rule(access='write', parent_access='write', recursive=False,
                      root_mutation_allowed=False, overwrite_allowed=False)],
          category='file_write', idempotency='required_key', gates=_WRITE_GATES),
    _tool('rename_file', '重命名文件', 'files', 'readwrite',
          _object({**_CHANGE_LOCATION, 'new_name': _ref('file_name'),
                   'expected_source_version': _ref('object_version'), **_IDEMPOTENCY}),
          _OPERATION_OUTPUT,
          [_file_rule(access='write', parent_access='write', root_mutation_allowed=False,
                      source_version_required=True, overwrite_allowed=False),
           {'kind': 'derived_target', 'parent_from': 'path', 'name_field': 'new_name',
            'access': 'write', 'live_identity_required': True, 'native_subject_acl_required': True}],
          category='file_write', idempotency='required_key', gates=_WRITE_GATES),
])

for _name, _title, _source_access in (
        ('copy_file', '复制普通文件', 'read'), ('move_file', '原子移动普通文件', 'write')):
    _TOOLS.append(_tool(
        _name, _title, 'files', 'readwrite',
        _object({'source_root_id': _ref('root_id'), 'source_path': _ref('change_path'),
                 'target_root_id': _ref('root_id'), 'target_path': _ref('change_path'),
                 'expected_source_version': _ref('object_version'), **_IDEMPOTENCY}),
        _OPERATION_OUTPUT,
        [_file_rule('source_root_id', 'source_path', _source_access,
                    parent_access='write' if _source_access == 'write' else 'traverse',
                    source_version_required=True, ordinary_file_only=True),
         _file_rule('target_root_id', 'target_path', 'write', parent_access='write',
                    root_mutation_allowed=False, overwrite_allowed=False),
         {'kind': 'file_change_semantics', 'recursive': False,
          'same_filesystem_required': _name == 'move_file',
          'atomic_move_required': _name == 'move_file',
          'copy_then_delete_fallback_allowed': False}],
        category='file_write', idempotency='required_key', gates=_WRITE_GATES))

_TOOLS.extend([
    _tool('trash_file', '移入原厂回收站', 'files', 'readwrite',
          _object({**_CHANGE_LOCATION, 'expected_source_version': _ref('object_version'),
                   'confirmed': {'const': True}, **_IDEMPOTENCY}), _OPERATION_OUTPUT,
          [_file_rule(access='write', parent_access='write', root_mutation_allowed=False,
                      source_version_required=True, recursive=False),
           {'kind': 'native_recycle_bin', 'verified_required': True,
            'permanent_delete_fallback_allowed': False}],
          category='file_write', idempotency='required_key', gates=_WRITE_GATES),
    _tool('get_file_operation', '查询本键文件写任务', 'files', 'readwrite',
          _object({'operation_id': _ref('operation_id')}),
          _success({'operation': _ref('file_operation'), 'may_wake_disks': {'const': True}}),
          [{'kind': 'owned_file_operation', 'id_field': 'operation_id',
            'same_key_and_generation': True,
            'original_tool_from': 'trusted_operation_record',
            'original_tool_grant_required': True, 'original_resources_required': True,
            'source_target_access_from': 'trusted_operation_record',
            'write_authorization_required': True}], category='file_write'),
    _tool('plan_docker_project_update', '规划固定项目镜像更新', 'docker', 'admin',
          _object({'project_id': _ref('project_id'), 'service_id': _ref('service_id'),
                   'image_reference': _ref('image_reference')}),
          _success({'plan': _ref('update_plan')}),
          [_project_rule('update_image'),
           {'kind': 'registered_service_image', 'service_field': 'service_id',
            'image_field': 'image_reference', 'registered_repository_required': True,
            'pinned_digest_required': True, 'self_update_allowed': False,
            'single_service_single_compose_required': True}],
          category='docker_update', idempotency='bounded_resource_allocation',
          gates=['native_project_identity', 'image_repository_and_digest',
                 'single_service_compose_audit', 'concurrency_boundary']),
    _tool('execute_docker_project_update', '执行已绑定镜像更新计划', 'docker', 'admin',
          _object({'plan_id': _ref('plan_id'), **_IDEMPOTENCY, 'confirmed': {'const': True}}),
          _success({'task': _ref('project_task')}),
          [{'kind': 'owned_update_plan', 'id_field': 'plan_id',
            'same_key_and_generation': True, 'original_grant_revision': True,
            'required_tool_grants': ['plan_docker_project_update'],
            'project_operation': 'update_image', 'original_project_service_image_only': True,
            'unexpired_unconsumed_required': True, 'atomic_consumption_required': True,
            'live_identity_required': True, 'self_update_allowed': False,
            'cross_client_concurrency_proof_required': True}],
          category='docker_update', idempotency='required_key',
          gates=['native_project_identity', 'compose_readback', 'cross_client_concurrency',
                 'atomic_plan_consumption', 'image_digest_layer_verification', 'update_acceptance']),
    _tool('get_docker_project_task', '查询本键项目更新任务', 'docker', 'readonly',
          _object({'project_id': _ref('project_id'), 'task_id': _ref('task_id')}),
          _success({'task': _ref('project_task')}),
          [_project_rule(), {'kind': 'owned_project_task', 'id_field': 'task_id',
                            'project_field': 'project_id', 'same_key_and_generation': True,
                            'same_project_required': True}], category='docker_update'),
])

_CATALOG = {
    'schema_version': 1, 'status': 'definitions_only', 'policy_version': 3,
    'execution_allowed': False, 'mcp_registration_allowed': False,
    'roles': copy.deepcopy(list(ROLE_CATALOG)), 'tools': _TOOLS,
    'errorSchema': _standalone(_ref('failure')),
    'boundaries': {
        'runtime_authorization_required': True, 'runtime_acl_required': True,
        'live_credential_resource_and_opened_object_revalidation_required': True,
        'owner_management_only': True,
        'bearer_may_manage_keys_or_resource_registry': False,
        'developer_window_may_grant_machine_access': False,
        'legacy_policy_versions_gain_tools': False,
        'definitions_prove_native_or_device_capability': False,
        'credentials_or_host_paths_in_public_results': False,
        'arbitrary_action_shell_sql_or_url_proxy_allowed': False,
        'passive_polling_file_access_allowed': False,
        'file_path_boundary': '/drives/*/Users/',
        'legacy_whole_disk_grants_auto_migrate': False,
    },
    'http_contracts': [{
        'route_template': '/api/integration/files/transfers/{transfer_id}/content',
        'status': 'backend_gated_transport', 'implemented': True, 'execution_allowed': False,
        'query_allowed': False, 'fragment_allowed': False, 'bearer_header_required': True,
        'transfer_id_schema': _standalone(_ref('transfer_id')),
        'directions': {
            'download': {'methods': ['GET', 'HEAD'], 'required_tool': 'prepare_file_download',
                         'implemented': True,
                         'minimum_role': 'readonly', 'resource_access': 'read',
                         'single_byte_range_only': True, 'head_starts_or_extends_deadline': False,
                         'strong_etag_or_cross_version_resume_claimed': False},
            'upload': {'methods': ['PUT'], 'required_tool': 'prepare_file_upload',
                       'implemented': True, 'transport_implemented': True,
                       'status': 'registered_requires_authorization_and_live_backend',
                       'minimum_role': 'readwrite', 'resource_access': 'write',
                       'content_length_and_sha256_required': True,
                       'content_type': 'application/octet-stream',
                       'sha256_from': 'trusted_prepared_transfer',
                       'max_size_bytes': 8 * 1024 ** 3,
                       'chunked_content_encoding_and_conditional_headers_allowed': False,
                       'absolute_socket_deadline_seconds': 1800,
                       'already_received_excess_body_rejected': True,
                       'connection_always_closed': True,
                       'arbitrary_chunk_offsets_allowed': False,
                       'automatic_commit_allowed': False}},
        'same_key_generation_revision_peer_and_current_acl_required': True,
        'max_stream_block_bytes': 65536, 'binary_in_mcp_allowed': False,
        'buffer_entire_file_allowed': False,
    }],
}


def contract_catalog():
    """Return detached definitions, never a callable MCP/REST tool registry."""
    return copy.deepcopy(_CATALOG)


def catalog():
    """Concise alias for hosts displaying the owner-only contract document."""
    return contract_catalog()

"""Stateless MCP messages and a fixed NAS tool catalog, without network access.

The HTTP owner authenticates API keys, checks Host/Origin, limits bodies and logs
calls. This module checks roles, feature scopes and arguments before invoking
the injected hardware callback. It never opens a socket or reads vendor tokens.
"""

import base64
import copy
import json
import math
import re

from api_permissions import MIN_ROLE_BY_TOOL, permission_allows
from api_contracts import IMPLEMENTED_TOOLS, contract_catalog


MODERN_VERSION = '2026-07-28'
LEGACY_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
SUPPORTED_VERSIONS = (MODERN_VERSION,) + LEGACY_VERSIONS
META_VERSION = 'io.modelcontextprotocol/protocolVersion'
META_CLIENT_INFO = 'io.modelcontextprotocol/clientInfo'
META_CLIENT_CAPABILITIES = 'io.modelcontextprotocol/clientCapabilities'
META_SERVER_INFO = 'io.modelcontextprotocol/serverInfo'
META_REQUIRED_SCOPE = 'io.github.shinku-chen.r1-toolbox/requiredScope'
META_MINIMUM_ROLE = 'io.github.shinku-chen.r1-toolbox/minimumRole'
READ_SECTIONS = ('overview', 'system', 'storage', 'temperatures', 'network',
                 'containers', 'apps', 'fans', 'lights', 'screen')
_MISSING = object()
_HEX_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')
_SAFE_INTEGER = 2 ** 53 - 1


def _object_schema(properties=None, required=()):
    result = {'type': 'object', 'properties': properties or {},
              'additionalProperties': False}
    if required:
        result['required'] = list(required)
    return result


def _tool(name, title, description, scope, schema, *, read_only=False,
          destructive=False, idempotent=True):
    return {'name': name, 'title': title, 'description': description,
            'inputSchema': schema,
            'annotations': {'readOnlyHint': read_only,
                            'destructiveHint': destructive,
                            'idempotentHint': idempotent,
                            'openWorldHint': False},
            '_meta': {META_REQUIRED_SCOPE: scope,
                      META_MINIMUM_ROLE: MIN_ROLE_BY_TOOL[name]}}


_CONFIG = {'type': 'object', 'maxProperties': 64,
           'description': 'Toolbox 已有策略对象；由原有控制器继续校验内部字段。'}


def _fan_config_schema(version):
    """Describe both accepted input versions; the controller normalizes to v2."""
    temperatures = ('low_temp', 'high_temp') if version == 2 else ('quiet_below', 'normal_at', 'full_at')
    fields = {
        'mode': {'type': 'string', 'enum': ['vendor', 'manual', 'temperature']},
        'sources': {'type': 'array', 'maxItems': 32, 'uniqueItems': True,
                    'items': {'type': 'string', 'minLength': 1, 'maxLength': 256}},
        **{name: {'type': 'integer', 'minimum': 15, 'maximum': 85} for name in temperatures},
        **{name: {'type': 'integer', 'minimum': 10, 'maximum': 100}
           for name in ('quiet_percent', 'normal_percent', 'manual_percent')},
    }
    cpu = _object_schema(fields, fields)
    system = _object_schema(dict(fields, vendor_mode={'type': 'string',
                            'enum': ['auto', 'low', 'medium', 'high']}), fields)
    return _object_schema({'schema_version': {'type': 'integer', 'const': version},
                           'channels': _object_schema({'cpu': cpu, 'system': system},
                                                      ('cpu', 'system'))},
                          ('schema_version', 'channels'))


_FAN_CONFIG = {'type': 'object', 'maxProperties': 64,
               'description': '优先使用 schema_version=2；low_temp<high_temp，均为 15–85°C 整数。'
                              'T<low_temp 静音，两阈值含边界为普通，T>high_temp 全速 100%，无迟滞。'
                              '静音／普通／固定输出为 10–100%，普通不得低于静音；温控需选择来源。'
                              '兼容严格校验的 v1：normal_at→low_temp、full_at→high_temp，'
                              'quiet_below 不再参与控制；响应与保存统一为 v2。',
               'oneOf': [_fan_config_schema(2), _fan_config_schema(1)]}
_CONFIRMED = {'type': 'boolean', 'const': True,
              'description': '仅在用户已明确授权本次操作后传入 true。'}
_APPLY = {'type': 'boolean', 'default': True}
_TOOLS = (
    _tool('get_nas_status', '读取 NAS 状态',
          '读取已有监控快照。未知读数保留 null；不会临时唤醒硬盘执行 SMART。',
          'read', _object_schema({'section': {'type': 'string',
                                             'enum': list(READ_SECTIONS),
                                             'default': 'overview'}}), read_only=True),
    _tool('get_media_status', '读取媒体状态',
          '按需查询原厂媒体状态，face 为相册识别，all 为七类固定检测。'
          '原厂查询可能写完成标记或触发修复分支；不视为无副作用读取，不自动轮询。'
          '令牌留在 NAS 内部，不接受任意原厂地址、动作或相册标识。',
          'media', _object_schema({'action': {'type': 'string',
                                            'enum': ['face', 'all'],
                                            'default': 'face'}}), idempotent=False),
    _tool('set_fan_policy', '设置风扇策略',
          '保存 CPU／系统独立风扇策略，v2 温控使用低温／高温两阈值；'
          '在 apply=true 时按原有预检接管。必须已获用户确认；'
          '不会跳过驱动、二进制和唯一进程核验。',
          'fan', _object_schema({'config': _FAN_CONFIG, 'apply': _APPLY,
                                 'confirmed': _CONFIRMED}, ('config', 'confirmed'))),
    _tool('release_fan', '恢复原厂风扇管理',
          '用户确认后释放 Toolbox 风扇接管，交回原厂自动温控。',
          'fan', _object_schema({'confirmed': _CONFIRMED}, ('confirmed',))),
    _tool('set_light_policy', '设置灯效策略',
          '保存灯效策略，apply=true 时立即通过 Toolbox 原有控制器生效。',
          'light', _object_schema({'config': _CONFIG, 'apply': _APPLY}, ('config',))),
    _tool('set_rgb', '设置原生灯效',
          '设置原生灯效模式 0 至 3、RGB 颜色及可选亮度。',
          'light', _object_schema({'mode': {'type': 'integer', 'minimum': 0, 'maximum': 3},
                                   'color': {'type': 'string', 'pattern': '^#[0-9a-fA-F]{6}$'},
                                   'brightness': {'type': 'integer', 'minimum': 0,
                                                  'maximum': 100}}, ('mode', 'color'))),
    _tool('set_screen', '控制小屏',
          '设置小屏亮度，或使其休眠、唤醒。brightness 需要 5 至 100 的 value；'
          'sleep、wake 不接受 value。',
          'screen', dict(_object_schema({'action': {'type': 'string',
                                                   'enum': ['brightness', 'sleep', 'wake']},
                                         'value': {'type': 'integer', 'minimum': 5,
                                                   'maximum': 100}}, ('action',)),
                         allOf=[{'if': {'properties': {'action': {'const': 'brightness'}}},
                                 'then': {'required': ['value']},
                                 'else': {'not': {'required': ['value']}}}])),
    _tool('update_screen_settings', '设置小屏显示',
          '更新 Toolbox 已有小屏显示及屏保设置；内部字段由原有设置校验器核验。',
          'screen', _object_schema({'settings': _CONFIG}, ('settings',))),
    _tool('request_power', '请求重启或关机',
          '仅在用户明确确认后请求 reboot 或 poweroff。沿用 5 秒可取消倒计时，'
          '可通过 cancel_power 取消，不提供立即绕过倒计时的操作。',
          'power', _object_schema({'operation': {'type': 'string',
                                                'enum': ['reboot', 'poweroff']},
                                   'confirmed': _CONFIRMED}, ('operation', 'confirmed')),
          destructive=True, idempotent=False),
    _tool('cancel_power', '取消电源操作',
          '取消尚在 5 秒倒计时内的重启或关机请求。',
          'power', _object_schema()),
)
_TOOLS += tuple(
    _tool(contract['name'], contract['title'],
          '查询明确授权的文件根元数据或固定 Docker 项目；不执行文件读写或项目更新。',
          contract['scope'], contract['inputSchema'], read_only=True)
    for contract in contract_catalog()['tools'] if contract['name'] in IMPLEMENTED_TOOLS
)
_TOOLS_BY_NAME = {tool['name']: tool for tool in _TOOLS}


def tool_catalog(principal=None):
    """Return metadata filtered by the same role/domain gate as tool execution.

    Omitting the principal is only for the browser's authenticated management
    catalog. Machine requests always supply an authenticated principal, including
    legacy keys whose original ten-tool grants must not silently expand.
    """
    return [copy.deepcopy(tool) for tool in _TOOLS
            if principal is None or permission_allows(
                principal, tool['_meta'][META_REQUIRED_SCOPE],
                tool['_meta'][META_MINIMUM_ROLE], tool_name=tool['name'])]


def _valid_id(value):
    return ((type(value) is int and -_SAFE_INTEGER <= value <= _SAFE_INTEGER)
            or (isinstance(value, str) and len(value) <= 1024
                and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)))


def _error(code, message, request_id=_MISSING, data=None):
    payload = {'jsonrpc': '2.0', 'error': {'code': code, 'message': message}}
    if request_id is not _MISSING:
        payload['id'] = request_id
    if data is not None:
        payload['error']['data'] = data
    return payload


def _request_id(message):
    if isinstance(message, dict) and _valid_id(message.get('id')):
        return message['id']
    return _MISSING


def _unsupported(requested, request_id=_MISSING):
    # Never reflect arbitrary objects or header control characters in an error.
    safe = requested if (isinstance(requested, str) and len(requested) <= 32
                         and all(32 <= ord(char) <= 126 for char in requested)) else None
    return 400, _error(-32022, 'Unsupported protocol version', request_id,
                       {'supported': list(SUPPORTED_VERSIONS), 'requested': safe})


def _json_tree(value, *, max_depth=16, max_nodes=4096, max_string=32768):
    """Bound direct Python callers too; HTTP body limits alone do not cover them."""
    pending = [(value, 0)]
    seen = 0
    while pending:
        item, depth = pending.pop()
        seen += 1
        if seen > max_nodes or depth > max_depth:
            return False
        if item is None or type(item) in (bool, int):
            continue
        if type(item) is float:
            if not math.isfinite(item):
                return False
        elif isinstance(item, str):
            if (len(item) > max_string
                    or any(0xD800 <= ord(char) <= 0xDFFF for char in item)):
                return False
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, dict):
            if any(not isinstance(key, str) or len(key) > 1024
                   or any(0xD800 <= ord(char) <= 0xDFFF for char in key) for key in item):
                return False
            pending.extend((child, depth + 1) for child in item.values())
        else:
            return False
    return True


def _modern_meta_error(params):
    if not isinstance(params, dict) or not isinstance(params.get('_meta'), dict):
        return True
    meta = params['_meta']
    if (not isinstance(meta.get(META_VERSION), str)
            or not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict)):
        return True
    info = meta.get(META_CLIENT_INFO, _MISSING)
    if info is not _MISSING and not _client_info_valid(info):
        return True
    return not _json_tree(meta)


def _client_info_valid(value):
    allowed = {'name', 'version', 'title', 'description', 'icons', 'websiteUrl'}
    if (not isinstance(value, dict) or set(value) - allowed
            or not all(isinstance(value.get(key), str) and 0 < len(value[key]) <= 1024
                       for key in ('name', 'version'))):
        return False
    if any(not isinstance(value[key], str) for key in ('title', 'description', 'websiteUrl')
           if key in value):
        return False
    if 'icons' in value and not isinstance(value['icons'], list):
        return False
    return _json_tree(value)


def _protocol(message, protocol_version=None):
    """Check per-request era/version independently of connection or client labels."""
    request_id = _request_id(message)
    if protocol_version is not None and protocol_version not in SUPPORTED_VERSIONS:
        return None, _unsupported(protocol_version, request_id)
    params = message.get('params', {}) if isinstance(message, dict) else {}
    meta = params.get('_meta') if isinstance(params, dict) else None
    if isinstance(params, dict) and '_meta' in params and not isinstance(meta, dict):
        return None, (400, _error(-32602, 'Invalid request metadata', request_id))
    if (isinstance(meta, dict) and META_VERSION in meta
            and not isinstance(meta[META_VERSION], str)):
        return None, (400, _error(-32602, 'Invalid protocol version metadata', request_id))
    declared = meta.get(META_VERSION) if isinstance(meta, dict) else None
    if declared is not None and declared not in SUPPORTED_VERSIONS:
        return None, _unsupported(declared, request_id)
    modern = protocol_version == MODERN_VERSION or declared == MODERN_VERSION
    if modern:
        if _modern_meta_error(params):
            return None, (400, _error(-32602, 'Missing or invalid request metadata', request_id))
        if protocol_version != declared:
            return None, (400, _error(-32020, 'Protocol version header mismatch', request_id))
        return MODERN_VERSION, None
    if declared is not None and protocol_version is not None and declared != protocol_version:
        return None, (400, _error(-32020, 'Protocol version header mismatch', request_id))
    return protocol_version or '2025-03-26', None


def _get_header(headers, name):
    """HTTPMessage is case-insensitive; plain dictionaries are useful in fixtures."""
    value = headers.get(name) if hasattr(headers, 'get') else None
    if value is not None:
        return value
    if hasattr(headers, 'items'):
        return next((value for key, value in headers.items()
                     if isinstance(key, str) and key.lower() == name.lower()), None)
    return None


def _decode_name_header(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if value.startswith('=?base64?') and value.endswith('?='):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode('utf-8')
        except (ValueError, UnicodeError):
            return None
    if value != value.strip() or any(ord(char) < 32 or ord(char) > 126 for char in value):
        return None
    return value


def validate_http_metadata(message, headers, protocol_version=None):
    """Validate modern mirrored headers before any tool runs.

    Returns ``None`` on success or ``(HTTP status, JSON-RPC error)``. The owner
    must reject duplicate security/MCP headers before calling this helper. No
    request metadata is an authorization source; only ``principal`` grants tools.
    """
    request_id = _request_id(message)
    header_version = _get_header(headers, 'MCP-Protocol-Version')
    if (header_version is not None and protocol_version is not None
            and header_version != protocol_version):
        return 400, _error(-32020, 'Protocol version header mismatch', request_id)
    version = header_version if header_version is not None else protocol_version
    selected, failure = _protocol(message, version)
    if failure is not None:
        return failure
    if selected != MODERN_VERSION:
        return None
    method = message.get('method') if isinstance(message, dict) else None
    mirrored = _get_header(headers, 'Mcp-Method')
    if (not isinstance(mirrored, str) or not mirrored
            or any(ord(char) < 33 or ord(char) > 126 for char in mirrored)
            or mirrored != method):
        return 400, _error(-32020, 'Method header mismatch', request_id)
    if method == 'tools/call':
        params = message.get('params', {})
        name = params.get('name') if isinstance(params, dict) else None
        mirrored = _decode_name_header(_get_header(headers, 'Mcp-Name'))
        if not isinstance(name, str) or mirrored is None or mirrored != name:
            return 400, _error(-32020, 'Tool name header mismatch', request_id)
    return None


def _tool_error(code, message):
    return _tool_result({'ok': False, 'error': {'code': code, 'message': message,
                                             'retryable': False}})


def _tool_result(value):
    return {'content': [{'type': 'text',
                         'text': json.dumps(value, ensure_ascii=False,
                                            allow_nan=False, separators=(',', ':'))}],
            'structuredContent': value, 'isError': value.get('ok') is False}


def _arguments(name, arguments):
    """Check the advertised fixed outer schema; controllers own policy internals."""
    if not isinstance(arguments, dict) or not _json_tree(arguments):
        return None
    if name in IMPLEMENTED_TOOLS:
        if name in ('list_file_roots', 'list_docker_projects'):
            return {} if not arguments else None
        project = arguments.get('project_id')
        return ({'project_id': project} if set(arguments) == {'project_id'}
                and isinstance(project, str)
                and re.fullmatch(r'project_[0-9a-f]{24}', project, re.ASCII) else None)
    spec = _TOOLS_BY_NAME[name]['inputSchema']
    props = spec['properties']
    if set(arguments) - set(props) or set(spec.get('required', ())) - set(arguments):
        return None
    result = copy.deepcopy(arguments)
    for field, schema in props.items():
        if field not in result:
            if 'default' in schema:
                result[field] = schema['default']
            continue
        value = result[field]
        kind = schema['type']
        if ((kind == 'string' and not isinstance(value, str))
                or (kind == 'integer' and type(value) is not int)
                or (kind == 'boolean' and type(value) is not bool)
                or (kind == 'object' and not isinstance(value, dict))):
            return None
        if 'enum' in schema and value not in schema['enum']:
            return None
        if 'const' in schema and value is not schema['const']:
            return None
        if kind == 'integer' and not schema.get('minimum', -_SAFE_INTEGER) <= value <= schema.get('maximum', _SAFE_INTEGER):
            return None
        if kind == 'object' and len(value) > schema.get('maxProperties', 64):
            return None
        if field == 'color' and not _HEX_COLOR.fullmatch(value):
            return None
    if name == 'set_screen':
        brightness = result['action'] == 'brightness'
        if brightness != ('value' in result):
            return None
    return result


class McpService:
    """Pure JSON-RPC dispatcher. Credentials, HTTP and hardware stay with owner."""

    def __init__(self, execute, *, version='dev'):
        if not callable(execute):
            raise TypeError('execute must be callable')
        self.execute = execute
        self.server_info = {'name': 'r1-toolbox',
                            'version': version if isinstance(version, str) else 'dev'}

    def tool_catalog(self, principal=None):
        return tool_catalog(principal)

    def call_tool(self, name, arguments, principal):
        """Return a safe result; REST uses this identical role/domain boundary."""
        if not isinstance(name, str) or name not in _TOOLS_BY_NAME:
            return _tool_error('unknown_tool', '不支持的工具。')
        required = _TOOLS_BY_NAME[name]['_meta'][META_REQUIRED_SCOPE]
        minimum = _TOOLS_BY_NAME[name]['_meta'][META_MINIMUM_ROLE]
        if not permission_allows(principal, required, minimum, tool_name=name):
            # The shared helper also rejects malformed principals. Never echo
            # submitted roles, scopes or arguments while explaining a denial.
            if permission_allows(principal, required, 'readonly', tool_name=name):
                return _tool_error('role_denied', 'API 密钥的角色等级不足。')
            return _tool_error('scope_denied', 'API 密钥未获此功能域的权限。')
        normalized = _arguments(name, arguments)
        if normalized is None:
            return _tool_error('invalid_arguments', '工具参数不符合声明；请检查字段、范围和操作确认。')
        try:
            result = self.execute(name, normalized, principal)
            if (not isinstance(result, dict)
                    or ('ok' in result and type(result['ok']) is not bool)
                    or not _json_tree(result, max_depth=32, max_nodes=100000,
                                      max_string=1048576)):
                return _tool_error('invalid_result', '工具返回格式暂不可确认。')
            # Detach mutable controller state and reject non-JSON Python objects.
            detached = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return _tool_result(detached)
        except Exception:
            return _tool_error('execution_failed', '工具执行未完成；请查看 NAS 本地诊断。')

    def _complete(self, request_id, result, protocol):
        if protocol == MODERN_VERSION:
            result = dict(result, resultType='complete')
            result['_meta'] = {META_SERVER_INFO: dict(self.server_info)}
        return 200, {'jsonrpc': '2.0', 'id': request_id, 'result': result}

    def handle(self, message, principal, protocol_version=None):
        """Handle one JSON-RPC object, with no batch, sessions or server pushes."""
        request_id = _request_id(message)
        if (not isinstance(message, dict) or set(message) - {'jsonrpc', 'id', 'method', 'params'}
                or message.get('jsonrpc') != '2.0'
                or not isinstance(message.get('method'), str)
                or not 0 < len(message['method']) <= 256
                or any(ord(char) < 33 or ord(char) > 126 for char in message['method'])
                or ('id' in message and not _valid_id(message['id']))):
            return 400, _error(-32600, 'Invalid Request', request_id)
        params = message.get('params', {})
        if not isinstance(params, dict) or not _json_tree(params):
            return 400, _error(-32602, 'Invalid params', request_id)
        protocol, failure = _protocol(message, protocol_version)
        if failure is not None:
            return failure
        method = message['method']
        # Notifications never run a tool, even if named tools/call. This avoids
        # executing a write whose result/error the caller cannot observe.
        if 'id' not in message:
            if (method == 'notifications/initialized'
                    and set(params) - {'_meta'}):
                return 400, _error(-32602, 'Invalid notification params')
            if method == 'notifications/cancelled' and (
                    set(params) - {'requestId', 'reason', '_meta'}
                    or not _valid_id(params.get('requestId'))
                    or ('reason' in params and not isinstance(params['reason'], str))):
                return 400, _error(-32602, 'Invalid notification params')
            return 202, None
        if method.startswith('notifications/'):
            return 400, _error(-32600, 'Notifications must not include an id', request_id)
        try:
            if method == 'initialize' and protocol != MODERN_VERSION:
                allowed = {'protocolVersion', 'capabilities', 'clientInfo', '_meta'}
                if (set(params) - allowed or not isinstance(params.get('protocolVersion'), str)
                        or not isinstance(params.get('capabilities'), dict)
                        or not _client_info_valid(params.get('clientInfo'))):
                    return 400, _error(-32602, 'Invalid initialize params', request_id)
                requested = params['protocolVersion']
                selected = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
                return self._complete(request_id,
                                      {'protocolVersion': selected,
                                       'capabilities': {'tools': {'listChanged': False}},
                                       'serverInfo': dict(self.server_info),
                                       'instructions': '按 API 密钥授权范围操作 NAS；风扇和电源操作须获用户确认，电源操作保留 5 秒取消窗口。'}, protocol)
            if method == 'server/discover' and protocol == MODERN_VERSION:
                if set(params) - {'_meta'}:
                    return 400, _error(-32602, 'Invalid discover params', request_id)
                return self._complete(request_id,
                                      {'supportedVersions': list(SUPPORTED_VERSIONS),
                                       'capabilities': {'tools': {'listChanged': False}},
                                       'instructions': '工具按每次请求的 API 密钥范围授权；原厂媒体查询可能写完成状态，电源操作可在 5 秒内取消。'}, protocol)
            if method in ('ping', 'tools/list'):
                if set(params) - {'_meta'}:
                    return 400, _error(-32602, 'Invalid params', request_id)
                result = {} if method == 'ping' else {'tools': tool_catalog(principal or {})}
                return self._complete(request_id, result, protocol)
            if method == 'tools/call':
                if (set(params) - {'name', 'arguments', '_meta'}
                        or not isinstance(params.get('name'), str)
                        or not isinstance(params.get('arguments', {}), dict)):
                    return 400, _error(-32602, 'Invalid tools/call params', request_id)
                name = params['name']
                if name not in _TOOLS_BY_NAME:
                    return 400, _error(-32602, 'Unknown tool', request_id)
                return self._complete(request_id,
                                      self.call_tool(name, params.get('arguments', {}), principal), protocol)
            status = 404 if protocol == MODERN_VERSION else 200
            return status, _error(-32601, 'Method not found', request_id)
        except Exception:
            return 500, _error(-32603, 'Internal error', request_id)

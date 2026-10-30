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

from api_permissions import LEGACY_TOOLS, MIN_ROLE_BY_TOOL, permission_allows
from api_contracts import IMPLEMENTED_TOOLS, contract_catalog
from native_capability_specs import TOOL_SPECS as NATIVE_TOOL_SPECS
from api_monitoring import METRICS as MONITORING_METRICS, RANGES as MONITORING_RANGES


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
    _tool('get_monitoring_history', '读取监控历史',
          '只读内存中的单指标历史缓存，最多七百二十点；不查询数据库、不启动采集。'
          '空缺保持 null，CPU 封装功耗来自 RAPL；风扇百分比是请求输出，不是实测转速。',
          'read', _object_schema({'metric': {'type': 'string', 'enum': list(MONITORING_METRICS)},
                                  'range': {'type': 'string', 'enum': list(MONITORING_RANGES), 'default': '1h'},
                                  'limit': {'type': 'integer', 'minimum': 1, 'maximum': 720, 'default': 720}},
                                 ('metric',)), read_only=True),
    _tool('get_health_events', '读取健康告警与事件',
          '只读已有告警和最近事件缓存；缺测、过期或不完整的健康评估保持 unknown。'
          '不运行 SMART，不输出磁盘序列号或原始诊断文本。',
          'read', _object_schema({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50},
                                  'since': {'type': 'number', 'minimum': 0, 'maximum': 9007199254740991}}),
          read_only=True),
    _tool('get_network_traffic_totals', '读取网络流量累计',
          '只读已有网卡日／月流量缓存；仅含实际观测计数增量，缺测保持 null，'
          '不补算断网或重启缺口，不重新汇总接口。',
          'read', _object_schema(), read_only=True),
    _tool('get_docker_project_resource_usage', '读取固定项目资源用量',
          '显式读取已登记项目各服务的一次 Docker stats；核验引擎、项目和容器身份。'
          'CPU 仅用合法原始计数差分；缺少差分保持 null，不返回原始 Docker 响应或日志。',
          'docker', _object_schema({'project_id': {'type': 'string', 'pattern': '^project_[0-9a-f]{24}$'}},
                                   ('project_id',)), read_only=True),
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
          ('在明确授权的Users/admin与public子树浏览、读取属性或建立同键下载句柄；每次操作核验原厂权限与最终对象，字节走独立Bearer通道。'
           if contract['scope'] == 'files' and contract['name'] != 'list_file_roots' else
           '查询明确授权的文件根元数据或固定 Docker 项目，不读取用户文件内容。'
           if contract['idempotency']['mode'] == 'read_only' else
           '按登记项目、服务和同仓库镜像 digest 规划或执行管理员更新；须具体工具与项目更新授权，不自动重试。'),
          contract['scope'], contract['inputSchema'],
          read_only=contract['idempotency']['mode'] == 'read_only',
          destructive=contract['name'] == 'execute_docker_project_update',
          idempotent=contract['idempotency']['mode'] == 'read_only')
    for contract in contract_catalog()['tools'] if contract['name'] in IMPLEMENTED_TOOLS
)
_TOOLS += tuple(
    _tool(name, title,
          description + '仅读取已有脱敏缓存，返回来源、观察时间和最近失败；'
          '不自动查询原厂或联网刷新，缓存不代表当前状态。',
          scope, _object_schema(), read_only=True)
    for name, title, scope, description in (
        ('get_app_status', '读取应用目录缓存', 'apps', '读取部分历史应用目录，不推断实时运行状态。'),
        ('get_download_status', '读取下载状态缓存', 'downloads', '读取已采集的固定下载器状态，不包含文件名、路径或任务标识。'),
        ('get_movie_status', '读取影视处理缓存', 'movie', '读取上次显式采集的影视刮削进度。'),
        ('get_album_status', '读取相册处理缓存', 'albums', '读取人脸、文字、缩略图、相似图和 AI 配置的既有状态。'),
        ('get_share_status', '读取共享相册处理缓存', 'sharing', '只读取管理员既有共享相册的处理状态，不代表分享清单。'),
    )
)
_DOWNLOAD_ENGINE = {'type': 'string', 'enum': ['qbittorrent', 'aria2']}
_TASK_ID = {'type': 'string', 'minLength': 16, 'maxLength': 64,
            'pattern': r'^(?:[0-9a-f]{16}|[0-9a-f]{40}|[0-9a-f]{64})$'}
_IDEMPOTENCY_KEY = {'type': 'string', 'minLength': 1, 'maxLength': 128,
                    'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:-]*$'}
_MUTATION = {'confirmed': _CONFIRMED, 'idempotency_key': _IDEMPOTENCY_KEY}
_TASK_LOCATION = {'engine': _DOWNLOAD_ENGINE, 'task_id': _TASK_ID}
_TASK_MUTATION = {**_TASK_LOCATION, **_MUTATION,
                  'expected_version': {'type': 'string', 'pattern': r'^download_[0-9a-f]{32}$'}}
_TOOLS += (
    _tool('control_docker_project', '启停或重启已登记项目',
          '管理员确认后操作已登记项目；逐步核验引擎、配置与容器身份，保护 Toolbox，不删除容器或数据卷。',
          'docker', _object_schema({'project_id': {'type': 'string', 'pattern': r'^project_[0-9a-f]{24}$'},
              'operation': {'type': 'string', 'enum': ['start', 'stop', 'restart']},
              **_MUTATION}, ('project_id', 'operation', 'confirmed', 'idempotency_key')),
          destructive=True, idempotent=False),
    _tool('list_download_tasks', '列出下载任务',
          '显式查询已核验本机 qB／aria2 任务；只返回受限元数据，不返回宿主路径、Tracker 或凭据。',
          'downloads', _object_schema({'engine': _DOWNLOAD_ENGINE,
              'offset': {'type': 'integer', 'minimum': 0, 'maximum': 300, 'default': 0},
              'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}}, ('engine',)), read_only=True),
    _tool('get_download_task', '读取单个下载任务',
          '读取精确任务及版本；后续修改须使用该版本，不能使用 all 或批量选择器。',
          'downloads', _object_schema(_TASK_LOCATION, tuple(_TASK_LOCATION)), read_only=True),
)
_TOOLS += tuple(
    _tool(name, title, description + '需要明确确认、当前任务版本和本键幂等标识；不会自动重放未确认的写请求。',
          'downloads', _object_schema({**_TASK_MUTATION, **extra}, tuple(_TASK_MUTATION)))
    for name, title, description, extra in (
        ('pause_download_task', '暂停单个下载任务', '暂停已核验引擎中的一个任务。', {}),
        ('resume_download_task', '继续单个下载任务', '继续已核验引擎中的一个任务。', {}),
        ('remove_download_task', '移除单个下载任务', '只移除任务，始终保留已下载文件。', {}),
        ('set_download_task_limits', '设置单任务限速', '以字节／秒设置单任务限速，0 表示不限速。',
         {field: {'type': 'integer', 'minimum': 0, 'maximum': 2 ** 40}
          for field in ('download_limit_bps', 'upload_limit_bps')}),
    )
)
_LIMIT_FIELDS = {field: {'type': 'integer', 'minimum': 0, 'maximum': 2 ** 40}
                 for field in ('download_limit_bps', 'upload_limit_bps')}
_TOOLS += (
    _tool('get_download_limits', '读取下载器全局限速',
          '显式读取固定本机下载器的两个已配置限速和版本；不返回偏好设置、目录或凭据。',
          'downloads', _object_schema({'engine': _DOWNLOAD_ENGINE}, ('engine',)), read_only=True),
    _tool('set_download_limits', '设置下载器全局限速',
          '管理员确认后仅设置全局下载／上传字节每秒上限，0为不限速。须先读取版本；影响该引擎全部任务，不修改目标目录。',
          'downloads', _object_schema({'engine': _DOWNLOAD_ENGINE, **_MUTATION, **_LIMIT_FIELDS,
              'expected_version': {'type': 'string', 'pattern': r'^download_limits_[0-9a-f]{32}$'}},
              ('engine', 'confirmed', 'idempotency_key', 'expected_version')), idempotent=False),
)
_TOOLS += tuple(
    _tool(name, title, description + '仅支持qB的单个既有任务；须确认、当前版本和本键幂等标识，接受回执不表示异步完成。',
          'downloads', _object_schema({**_TASK_MUTATION, 'engine': {'type': 'string', 'enum': ['qbittorrent']}, **extra},
              tuple(_TASK_MUTATION) + tuple(extra)), idempotent=False)
    for name, title, description, extra in (
        ('recheck_download_task', '重新校验下载任务', '请求原生引擎重新校验原有数据，可能唤盘并改变任务状态。', {}),
        ('rename_download_task', '修改下载任务显示名称', '仅修改任务显示名，不调用文件或目录改名接口。',
         {'name': {'type': 'string', 'minLength': 1, 'maxLength': 255}}),
        ('set_download_task_force_start', '设置任务强制运行', '开启或关闭单任务强制运行标记；开启会按原目标运行。',
         {'enabled': {'type': 'boolean'}}),
        ('set_download_task_queue_position', '调整下载队列位置', '仅调整该任务的队列优先级，需要下载器启用排队。',
         {'position': {'type': 'string', 'enum': ['increase', 'decrease', 'top', 'bottom']}}),
    )
)
_TOOLS += tuple(
    _tool(name, title, description + '使用已有原厂管理员会话和固定接口，签名不离开宿主。',
          scope, _object_schema(props, tuple(props)), read_only=False,
          destructive=name == 'revoke_share', idempotent=False)
    for name, title, scope, props, writing, description in (
        ('list_albums', '列出原厂相册', 'albums', {}, False, '显式查询原厂目录，可能初始化资源或更新内部状态，需要读写权限；不返回照片内容和宿主路径。'),
        ('list_shared_albums', '列出共享相册', 'albums', {}, False, '显式查询当前管理员共享目录，原厂可能修复内部状态，需要读写权限。'),
        ('list_movie_libraries', '列出影视库', 'movie', {}, False, '显式查询影视库名称与标识，可能更新原厂内部状态，需要读写权限；不返回源路径或成员。'),
        ('create_album', '创建普通相册', 'albums',
         {**_MUTATION, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 255}}, True, '确认后创建普通相册。'),
        ('rename_album', '重命名普通相册', 'albums',
         {**_MUTATION, 'album_id': {'type': 'string', 'pattern': r'^[1-9][0-9]{0,9}$'},
          'name': {'type': 'string', 'minLength': 1, 'maxLength': 255}}, True, '确认后修改已有普通相册名称，系统相册不可修改。'),
        ('rename_movie_library', '重命名影视库', 'movie',
         {**_MUTATION, 'library_id': {'type': 'integer', 'minimum': 1, 'maximum': _SAFE_INTEGER},
          'name': {'type': 'string', 'minLength': 1, 'maxLength': 255}}, True, '仅修改已有影视库名称，不改变来源与成员。'),
        ('revoke_share', '撤销原厂分享', 'sharing',
         {**_MUTATION, 'share_id': {'type': 'string', 'minLength': 1, 'maxLength': 127, 'pattern': r'^[A-Za-z0-9_-]+$'}}, True, '确认后撤销当前管理员拥有的单个分享。'),
    )
)
_DEPLOYMENT_LOCATION = {'deployment_id': {'type': 'string', 'pattern': r'^deployment_[0-9a-f]{24}$'}}
_TOOLS += (
    _tool('list_docker_deployments', '读取已授权的 Docker 部署',
          '只读取本键获授的独立部署资源及实际容器状态；登记未来资源不表示已部署。',
          'docker', _object_schema(), read_only=True),
    _tool('plan_docker_deployment', '规划受控 Docker 部署或操作',
          '管理员选择所有者预先登记的未来资源，规划部署、同仓库digest更新、启停重启或移除；不能提交Compose、命令、目录或新网络参数。',
          'docker', dict(_object_schema({**_DEPLOYMENT_LOCATION,
              'operation': {'type': 'string', 'enum': ['deploy', 'update', 'start', 'stop', 'restart', 'remove']},
              'image_reference': {'type': 'string', 'minLength': 73, 'maxLength': 512,
                                  'pattern': r'^[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}$'}},
              ('deployment_id', 'operation')),
              allOf=[{'if': {'properties': {'operation': {'enum': ['deploy', 'update']}}},
                      'then': {'required': ['image_reference']}, 'else': {'not': {'required': ['image_reference']}}}]),
          read_only=True, idempotent=False),
    _tool('execute_docker_deployment', '执行受控 Docker 计划',
          '管理员确认后消费本键有效计划，返回真实任务；按固定资源和当前身份执行，移除保留全部数据卷，未知结果不重放。',
          'docker', _object_schema({'plan_id': {'type': 'string', 'pattern': r'^plan_[0-9a-f]{32}$'},
              **_MUTATION}, ('plan_id', 'confirmed', 'idempotency_key')), destructive=True, idempotent=False),
    _tool('get_docker_deployment_task', '查询受控部署任务',
          '仅查询本键、当前授权修订和凭据代际在已授权部署资源上提交的真实任务。',
          'docker', _object_schema({**_DEPLOYMENT_LOCATION,
              'task_id': {'type': 'string', 'pattern': r'^task_[0-9a-f]{32}$'}}, ('deployment_id', 'task_id')), read_only=True),
)
_TOOLS += tuple(_tool(item['name'], item['title'], item['description'], item['scope'], item['inputSchema'],
                    read_only=item['annotations']['readOnlyHint'],
                    destructive=item['annotations']['destructiveHint'],
                    idempotent=item['annotations']['idempotentHint']) for item in NATIVE_TOOL_SPECS)
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


def _schema_matches(value, schema, definitions=None, depth=0):
    """Validate the bounded JSON-schema subset used by our fixed input catalog.

    No external references, formats, code or schema supplied by an HTTP caller
    are evaluated. Controllers still validate native semantics and resources.
    """
    if depth > 32 or not isinstance(schema, dict):
        return False
    definitions = definitions if definitions is not None else schema.get('$defs', {})
    if '$ref' in schema:
        reference = schema['$ref']
        if not isinstance(reference, str) or not reference.startswith('#/$defs/'):
            return False
        return _schema_matches(value, definitions.get(reference[8:]), definitions, depth + 1)
    descend = lambda branch: _schema_matches(value, branch, definitions, depth + 1)
    if ('allOf' in schema and not all(descend(branch) for branch in schema['allOf'])
            or 'anyOf' in schema and not any(descend(branch) for branch in schema['anyOf'])
            or 'oneOf' in schema and sum(descend(branch) for branch in schema['oneOf']) != 1
            or 'not' in schema and descend(schema['not'])):
        return False
    if 'if' in schema:
        branch = schema.get('then' if descend(schema['if']) else 'else')
        if branch is not None and not descend(branch):
            return False
    kind = schema.get('type')
    if kind == 'null' and value is not None:
        return False
    if kind == 'boolean' and type(value) is not bool:
        return False
    if kind == 'integer' and (type(value) is not int or not schema.get('minimum', -_SAFE_INTEGER) <= value <= schema.get('maximum', _SAFE_INTEGER)):
        return False
    if kind == 'string':
        if not isinstance(value, str) or not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 16384):
            return False
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            return False
    if kind == 'array':
        if not isinstance(value, list) or not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 256):
            return False
        if schema.get('uniqueItems') and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            return False
        if 'items' in schema and not all(_schema_matches(item, schema['items'], definitions, depth + 1) for item in value):
            return False
    if kind == 'object' or 'properties' in schema or 'required' in schema:
        if not isinstance(value, dict) or not schema.get('minProperties', 0) <= len(value) <= schema.get('maxProperties', 64):
            return False
        properties = schema.get('properties', {})
        if set(schema.get('required', ())) - set(value):
            return False
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            return False
        if any(not _schema_matches(item, properties[key], definitions, depth + 1)
               for key, item in value.items() if key in properties):
            return False
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        return False
    if 'enum' in schema and not any(type(value) is type(item) and value == item for item in schema['enum']):
        return False
    return True


def _arguments(name, arguments):
    """Check the advertised fixed outer schema; controllers own policy internals."""
    if not isinstance(arguments, dict) or not _json_tree(arguments):
        return None
    if name in IMPLEMENTED_TOOLS:
        contract = next(item for item in contract_catalog()['tools'] if item['name'] == name)
        return copy.deepcopy(arguments) if _schema_matches(arguments, contract['inputSchema']) else None
    spec = _TOOLS_BY_NAME[name]['inputSchema']
    props = spec['properties']
    if set(arguments) - set(props) or set(spec.get('required', ())) - set(arguments):
        return None
    if name not in LEGACY_TOOLS and not _schema_matches(arguments, spec):
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

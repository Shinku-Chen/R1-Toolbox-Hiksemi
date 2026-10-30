"""Shared role ceilings and explicit feature grants for machine clients.

This module is deliberately pure: no files, sockets or native credentials are
opened when listing or authorizing a tool. File-root existence and path access
belong to the storage provider, after this policy check.
"""

import re

from native_capability_specs import TOOL_SPECS as _NATIVE_TOOL_SPECS


LEGACY_SCOPES = ('read', 'fan', 'light', 'screen', 'power', 'media')
SCOPES = LEGACY_SCOPES + (
    'files', 'docker', 'apps', 'downloads', 'movie', 'albums', 'sharing',
    'backup', 'system',
)
ROLE_CATALOG = (
    {'id': 'readonly', 'label': '只读',
     'description': '仅查询已授权功能；文件读取仍受所选范围限制。'},
    {'id': 'readwrite', 'label': '读写',
     'description': '可修改已授权功能的普通设置，不包含管理员操作。'},
    {'id': 'admin', 'label': '管理员',
     'description': '可执行已授权功能的管理操作，仍受功能域和文件范围限制。'},
)
SCOPE_CATALOG = (
    {'id': 'read', 'label': '读取状态', 'implemented': True, 'min_role': 'readonly',
     'description': '读取设备、磁盘、温度与策略状态。'},
    {'id': 'fan', 'label': '风扇控制', 'implemented': True, 'min_role': 'readwrite',
     'description': '修改风扇策略或恢复原厂管理，需要读写或管理员角色。'},
    {'id': 'light', 'label': '灯光控制', 'implemented': True, 'min_role': 'readwrite',
     'description': '修改灯效与 RGB 设置，需要读写或管理员角色。'},
    {'id': 'screen', 'label': '屏幕控制', 'implemented': True, 'min_role': 'readwrite',
     'description': '控制小屏与屏幕保护，需要读写或管理员角色。'},
    {'id': 'power', 'label': '电源操作', 'implemented': True, 'min_role': 'admin',
     'description': '请求或取消带延时的重启与关机，需要管理员角色。'},
    {'id': 'media', 'label': '媒体查询', 'implemented': True, 'min_role': 'readwrite',
     'description': '显式查询原厂媒体进度，可能写原厂完成状态，需要读写或管理员角色。'},
    {'id': 'files', 'label': '文件管理', 'implemented': True, 'min_role': 'readonly',
     'description': '浏览已核验的管理员／公共目录并下载；读写授权可在管理员后代上传新文件，覆盖及其他文件变更待接入。'},
    {'id': 'docker', 'label': 'Docker 管理', 'implemented': True, 'min_role': 'readonly',
     'description': '读取并控制已登记项目；管理员可使用所有者预先登记的独立资源部署、更新和移除容器。'},
    {'id': 'apps', 'label': '应用管理', 'implemented': True, 'min_role': 'readonly',
     'description': '读取应用缓存和显式原厂目录；管理员可预览并新装已审计的百度网盘 v2，其他安装、更新和卸载待接入。'},
    {'id': 'downloads', 'label': '下载管理', 'implemented': True, 'min_role': 'readonly',
     'description': '读取缓存或显式任务明细；可逐任务暂停、继续、移除及限速，移除始终保留文件；新增任务待接入。'},
    {'id': 'movie', 'label': '影视管理', 'implemented': True, 'min_role': 'readonly',
     'description': '读取刮削缓存、显式影视库目录并修改库名；来源、成员与新建媒体库待接入。'},
    {'id': 'albums', 'label': '相册管理', 'implemented': True, 'min_role': 'readonly',
     'description': '读取处理缓存、列出相册、创建和重命名普通相册；照片浏览和文件操作待接入。'},
    {'id': 'sharing', 'label': '分享管理', 'implemented': True, 'min_role': 'readonly',
     'description': '读取共享相册处理缓存并撤销当前管理员的既有单个分享；新建外链待接入。'},
    {'id': 'backup', 'label': '备份管理', 'implemented': True, 'min_role': 'readwrite',
     'description': '显式读取原厂备份任务和自动备份配置；目录、任务创建与调度写入须文件后端证明。'},
    {'id': 'system', 'label': '系统管理', 'implemented': True, 'min_role': 'admin',
     'description': '管理员读取用户目录、设备名称和AI开关，并可明确修改名称及识别开关；账户和存储变更仍待接入。'},
)
MIN_ROLE_BY_TOOL = {
    'get_nas_status': 'readonly',
    'get_monitoring_history': 'readonly', 'get_health_events': 'readonly',
    'get_network_traffic_totals': 'readonly',
    'get_docker_project_resource_usage': 'readonly',
    'get_app_status': 'readonly',
    'get_download_status': 'readonly',
    'get_movie_status': 'readonly',
    'get_album_status': 'readonly',
    'get_share_status': 'readonly',
    'list_download_tasks': 'readonly', 'get_download_task': 'readonly',
    'pause_download_task': 'readwrite', 'resume_download_task': 'readwrite',
    'remove_download_task': 'readwrite', 'set_download_task_limits': 'readwrite',
    'get_download_limits': 'readonly', 'set_download_limits': 'admin',
    'recheck_download_task': 'readwrite', 'rename_download_task': 'readwrite',
    'set_download_task_force_start': 'readwrite', 'set_download_task_queue_position': 'readwrite',
    'list_albums': 'readwrite', 'list_shared_albums': 'readwrite',
    'create_album': 'readwrite', 'rename_album': 'readwrite',
    'list_movie_libraries': 'readwrite', 'rename_movie_library': 'readwrite',
    'revoke_share': 'readwrite', 'control_docker_project': 'admin',
    'list_docker_deployments': 'readonly', 'get_docker_deployment_task': 'readonly',
    'plan_docker_deployment': 'admin', 'execute_docker_deployment': 'admin',
    'get_media_status': 'readwrite',
    'set_fan_policy': 'readwrite',
    'release_fan': 'readwrite',
    'set_light_policy': 'readwrite',
    'set_rgb': 'readwrite',
    'set_screen': 'readwrite',
    'update_screen_settings': 'readwrite',
    'request_power': 'admin',
    'cancel_power': 'admin',
    'list_file_roots': 'readonly',
    'list_directory': 'readonly',
    'get_file_info': 'readonly',
    'prepare_file_download': 'readonly',
    'get_file_transfer': 'readonly',
    'cancel_file_transfer': 'readonly',
    'prepare_file_upload': 'readwrite',
    'commit_file_upload': 'readwrite',
    'create_directory': 'readwrite',
    'rename_file': 'readwrite',
    'copy_file': 'readwrite',
    'move_file': 'readwrite',
    'trash_file': 'readwrite',
    'get_file_operation': 'readwrite',
    'list_docker_projects': 'readonly',
    'get_docker_project_status': 'readonly',
    'get_docker_project_summary': 'readonly',
    'plan_docker_project_update': 'admin',
    'execute_docker_project_update': 'admin',
    'get_docker_project_task': 'readonly',
}
LEGACY_TOOLS = frozenset((
    'get_nas_status', 'get_media_status', 'set_fan_policy', 'release_fan',
    'set_light_policy', 'set_rgb', 'set_screen', 'update_screen_settings',
    'request_power', 'cancel_power',
))
_ROLE_LEVELS = {'readonly': 0, 'readwrite': 1, 'admin': 2}
_ROOT_ID_RE = re.compile(r'root_[0-9a-f]{24}\Z', re.ASCII)
_PROJECT_ID_RE = re.compile(r'project_[0-9a-f]{24}\Z', re.ASCII)
SCOPE_BY_TOOL = {
    'get_nas_status': 'read', 'get_media_status': 'media',
    'get_monitoring_history': 'read', 'get_health_events': 'read',
    'get_network_traffic_totals': 'read',
    'get_docker_project_resource_usage': 'docker',
    'get_app_status': 'apps', 'get_download_status': 'downloads',
    'get_movie_status': 'movie', 'get_album_status': 'albums',
    'get_share_status': 'sharing',
    'list_download_tasks': 'downloads', 'get_download_task': 'downloads',
    'pause_download_task': 'downloads', 'resume_download_task': 'downloads',
    'remove_download_task': 'downloads', 'set_download_task_limits': 'downloads',
    'get_download_limits': 'downloads', 'set_download_limits': 'downloads',
    'recheck_download_task': 'downloads', 'rename_download_task': 'downloads',
    'set_download_task_force_start': 'downloads', 'set_download_task_queue_position': 'downloads',
    'list_albums': 'albums', 'list_shared_albums': 'albums',
    'create_album': 'albums', 'rename_album': 'albums',
    'list_movie_libraries': 'movie', 'rename_movie_library': 'movie',
    'revoke_share': 'sharing', 'control_docker_project': 'docker',
    'list_docker_deployments': 'docker', 'get_docker_deployment_task': 'docker',
    'plan_docker_deployment': 'docker', 'execute_docker_deployment': 'docker',
    'set_fan_policy': 'fan', 'release_fan': 'fan',
    'set_light_policy': 'light', 'set_rgb': 'light',
    'set_screen': 'screen', 'update_screen_settings': 'screen',
    'request_power': 'power', 'cancel_power': 'power',
    'list_file_roots': 'files', 'list_directory': 'files', 'get_file_info': 'files',
    'prepare_file_download': 'files', 'get_file_transfer': 'files',
    'cancel_file_transfer': 'files', 'prepare_file_upload': 'files',
    'commit_file_upload': 'files', 'create_directory': 'files',
    'rename_file': 'files', 'copy_file': 'files', 'move_file': 'files',
    'trash_file': 'files', 'get_file_operation': 'files',
    'list_docker_projects': 'docker', 'get_docker_project_status': 'docker',
    'get_docker_project_summary': 'docker', 'plan_docker_project_update': 'docker',
    'execute_docker_project_update': 'docker', 'get_docker_project_task': 'docker',
}
MIN_ROLE_BY_TOOL.update({item['name']: item['min_role'] for item in _NATIVE_TOOL_SPECS})
SCOPE_BY_TOOL.update({item['name']: item['scope'] for item in _NATIVE_TOOL_SPECS})

_WRITE_FILE_TOOLS = frozenset((
    'prepare_file_upload', 'commit_file_upload', 'create_directory',
    'rename_file', 'copy_file', 'move_file', 'trash_file', 'get_file_operation',
))
_UPDATE_PROJECT_TOOLS = frozenset((
    'plan_docker_project_update', 'execute_docker_project_update',
))
_TOOL_DEPENDENCIES = {
    'commit_file_upload': 'prepare_file_upload',
    'execute_docker_project_update': 'plan_docker_project_update',
    'execute_docker_deployment': 'plan_docker_deployment',
    'install_native_application': 'plan_native_application_install',
}


def _valid_scopes(value):
    return (isinstance(value, (list, tuple, set, frozenset)) and bool(value)
            and all(isinstance(item, str) and item in SCOPES for item in value)
            and len(set(value)) == len(value))


def valid_file_roots(value, role, scopes):
    """Check canonical grants without resolving or opening a storage path.

    Empty paths select an entire known root. Nonempty paths are relative POSIX
    subdirectories, with no aliases that could hide traversal or duplicate grants.
    """
    if (not isinstance(value, list) or len(value) > 32 or not _valid_scopes(scopes)
            or not isinstance(role, str) or role not in _ROLE_LEVELS):
        return False
    identities = set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {'id', 'path', 'access'}
                or not isinstance(row['id'], str) or not _ROOT_ID_RE.fullmatch(row['id'])
                or not isinstance(row['path'], str) or len(row['path']) > 1024
                or '\\' in row['path'] or any(not char.isprintable() for char in row['path'])
                or (row['path'] and any(part in ('', '.', '..') for part in row['path'].split('/')))
                or row['access'] not in ('readonly', 'readwrite')
                or role == 'readonly' and row['access'] != 'readonly'):
            return False
        identity = (row['id'], row['path'])
        if identity in identities:
            return False
        identities.add(identity)
    return bool(value) == ('files' in scopes)


def valid_docker_projects(value, role, scopes, *, has_deployments=False):
    """Validate configured project grants, without trusting names or a registry.

    Readwrite may pre-authorize update resources, but only admin can select an
    update tool. The owner adapter must separately verify every registered ID.
    """
    if (not isinstance(value, list) or len(value) > 32 or not _valid_scopes(scopes)
            or not isinstance(role, str) or role not in _ROLE_LEVELS):
        return False
    identities = set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {'id', 'operations'}
                or not isinstance(row['id'], str) or not _PROJECT_ID_RE.fullmatch(row['id'])
                or row['id'] in identities or not isinstance(row['operations'], list)
                or not 1 <= len(row['operations']) <= 3
                or any(not isinstance(item, str) or item not in ('inspect', 'control', 'update_image')
                       for item in row['operations'])
                or len(set(row['operations'])) != len(row['operations'])
                or role == 'readonly' and any(item != 'inspect' for item in row['operations'])
                or role == 'readwrite' and 'control' in row['operations']):
            return False
        identities.add(row['id'])
    return (bool(value) or has_deployments) == ('docker' in scopes)


_DEPLOYMENT_ID_RE = re.compile(r'deployment_[0-9a-f]{24}\Z', re.ASCII)
_DEPLOYMENT_READ_TOOLS = frozenset(('list_docker_deployments', 'get_docker_deployment_task'))
_DEPLOYMENT_WRITE_TOOLS = frozenset(('plan_docker_deployment', 'execute_docker_deployment'))
DEPLOYMENT_TOOLS = _DEPLOYMENT_READ_TOOLS | _DEPLOYMENT_WRITE_TOOLS
_DEPLOYMENTS_OMITTED = object()


def valid_docker_deployments(value, role, scopes):
    """Check explicit future-deployment grants; names never grant existing projects."""
    if (not isinstance(value, list) or len(value) > 32 or not _valid_scopes(scopes)
            or not isinstance(role, str) or role not in _ROLE_LEVELS
            or value and 'docker' not in scopes):
        return False
    identities = set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {'id', 'operations'}
                or not isinstance(row['id'], str) or not _DEPLOYMENT_ID_RE.fullmatch(row['id'])
                or row['id'] in identities or not isinstance(row['operations'], list)
                or not 1 <= len(row['operations']) <= 4
                or any(not isinstance(operation, str) or operation not in ('inspect', 'deploy', 'control', 'remove')
                       for operation in row['operations'])
                or len(set(row['operations'])) != len(row['operations'])
                or role != 'admin' and any(operation != 'inspect' for operation in row['operations'])):
            return False
        identities.add(row['id'])
    return True


def valid_tool_grants(value, role, scopes, file_roots, docker_projects, docker_deployments=_DEPLOYMENTS_OMITTED):
    """Check the exact known tool set and structural resource dependencies.

    This confers no filesystem, task ownership or native ACL proof. In
    particular transfer queries/cancel still restore their original permissions
    from a trusted runtime record before inspecting an upload or download.
    """
    docker_deployments = [] if docker_deployments is _DEPLOYMENTS_OMITTED else docker_deployments
    if (not isinstance(role, str) or role not in _ROLE_LEVELS
            or not isinstance(value, list) or len(value) > len(SCOPE_BY_TOOL)
            or not valid_file_roots(file_roots, role, scopes)
            or not valid_docker_deployments(docker_deployments, role, scopes)
            or not valid_docker_projects(docker_projects, role, scopes, has_deployments=bool(docker_deployments))
            or any(not isinstance(name, str) or name not in SCOPE_BY_TOOL for name in value)
            or len(set(value)) != len(value)):
        return False
    writable = any(root['access'] == 'readwrite' for root in file_roots)
    project_operations = {operation for project in docker_projects for operation in project['operations']}
    deployment_operations = {operation for deployment in docker_deployments for operation in deployment['operations']}
    for name in value:
        if (SCOPE_BY_TOOL[name] not in scopes
                or _ROLE_LEVELS[role] < _ROLE_LEVELS[MIN_ROLE_BY_TOOL[name]]
                or name in _WRITE_FILE_TOOLS and not writable
                or name in _UPDATE_PROJECT_TOOLS and 'update_image' not in project_operations
                or name == 'control_docker_project' and 'control' not in project_operations
                or name in _DEPLOYMENT_READ_TOOLS and 'inspect' not in deployment_operations
                or name in _DEPLOYMENT_WRITE_TOOLS and not deployment_operations.intersection(('deploy', 'control', 'remove'))
                or SCOPE_BY_TOOL[name] == 'docker' and name not in _UPDATE_PROJECT_TOOLS
                   and name != 'control_docker_project' and name not in DEPLOYMENT_TOOLS
                   and 'inspect' not in project_operations
                or name in _TOOL_DEPENDENCIES and _TOOL_DEPENDENCIES[name] not in value):
            return False
    return True


def permission_allows(principal, scope, minimum_role, *, tool_name=None):
    """Apply a role ceiling AND an explicitly selected domain, never role alone.

    Older in-process fixtures without role/version metadata use the same legacy
    ten-tool boundary as migrated v1 keys. Any partially supplied or mismatched
    metadata fails closed; a legacy grant never reaches a newly added tool.
    """
    if (not isinstance(principal, dict) or not isinstance(scope, str)
            or scope not in SCOPES or not isinstance(minimum_role, str)
            or minimum_role not in _ROLE_LEVELS
            or not isinstance(tool_name, str) or tool_name not in SCOPE_BY_TOOL
            or SCOPE_BY_TOOL[tool_name] != scope):
        return False
    scopes = principal.get('scopes')
    if (not isinstance(scopes, (list, tuple, set, frozenset)) or not scopes
            or any(not isinstance(item, str) or item not in SCOPES for item in scopes)
            or len(set(scopes)) != len(scopes) or scope not in scopes):
        return False
    if 'role' not in principal and 'policy_version' not in principal:
        if (principal.get('tool_grants', []) != [] or principal.get('docker_projects', []) != []
                or principal.get('docker_deployments', []) != []):
            return False
        legacy = True
    else:
        version, role = principal.get('policy_version'), principal.get('role')
        if type(version) is not int:
            return False
        if version == 1 and role == 'legacy':
            if (principal.get('file_roots') != [] or principal.get('tool_grants', []) != []
                    or principal.get('docker_projects', []) != [] or principal.get('docker_deployments', []) != []):
                return False
            legacy = True
        elif version == 2 and isinstance(role, str) and role in _ROLE_LEVELS:
            return (tool_name in LEGACY_TOOLS
                    and principal.get('tool_grants', []) == []
                    and principal.get('docker_projects', []) == []
                    and principal.get('docker_deployments', []) == []
                    and valid_file_roots(principal.get('file_roots'), role, scopes)
                    and _ROLE_LEVELS[role] >= _ROLE_LEVELS[minimum_role])
        elif version == 3 and isinstance(role, str) and role in _ROLE_LEVELS:
            return (valid_tool_grants(principal.get('tool_grants'), role, scopes,
                                      principal.get('file_roots'), principal.get('docker_projects'),
                                      principal.get('docker_deployments', []))
                    and tool_name in principal['tool_grants']
                    and _ROLE_LEVELS[role] >= _ROLE_LEVELS[minimum_role])
        else:
            return False
    return (legacy and isinstance(tool_name, str) and tool_name in LEGACY_TOOLS
            and all(item in LEGACY_SCOPES for item in scopes))

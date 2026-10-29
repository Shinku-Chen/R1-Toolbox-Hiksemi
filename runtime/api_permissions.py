"""Shared role ceilings and explicit feature grants for machine clients.

This module is deliberately pure: no files, sockets or native credentials are
opened when listing or authorizing a tool. File-root existence and path access
belong to the storage provider, after this policy check.
"""

import re


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
    {'id': 'files', 'label': '文件管理', 'implemented': False, 'min_role': 'readonly',
     'description': '待接入；可预授权所选文件范围，目前没有文件操作工具。'},
    {'id': 'docker', 'label': 'Docker 管理', 'implemented': False, 'min_role': 'readonly',
     'description': '待接入；可预授权容器管理与服务部署，目前没有对应操作工具。'},
    {'id': 'apps', 'label': '应用管理', 'implemented': False, 'min_role': 'readonly',
     'description': '待接入；可预授权应用市场与安装管理，目前没有对应操作工具。'},
    {'id': 'downloads', 'label': '下载管理', 'implemented': False, 'min_role': 'readonly',
     'description': '待接入；可预授权下载器与任务管理，目前没有对应操作工具。'},
    {'id': 'movie', 'label': '影视管理', 'implemented': False, 'min_role': 'readonly',
     'description': '待接入；可预授权影视墙与媒体库配置，目前没有对应操作工具。'},
    {'id': 'albums', 'label': '相册管理', 'implemented': False, 'min_role': 'readonly',
     'description': '待接入；可预授权相册浏览与创建，目前没有对应操作工具。'},
    {'id': 'sharing', 'label': '分享管理', 'implemented': False, 'min_role': 'readonly',
     'description': '待接入；可预授权文件与相册分享，目前没有对应操作工具。'},
    {'id': 'backup', 'label': '备份管理', 'implemented': False, 'min_role': 'readonly',
     'description': '待接入；可预授权备份任务管理，目前没有对应操作工具。'},
    {'id': 'system', 'label': '系统管理', 'implemented': False, 'min_role': 'admin',
     'description': '待接入；可预授权系统管理，目前没有对应操作工具。'},
)
MIN_ROLE_BY_TOOL = {
    'get_nas_status': 'readonly',
    'get_media_status': 'readwrite',
    'set_fan_policy': 'readwrite',
    'release_fan': 'readwrite',
    'set_light_policy': 'readwrite',
    'set_rgb': 'readwrite',
    'set_screen': 'readwrite',
    'update_screen_settings': 'readwrite',
    'request_power': 'admin',
    'cancel_power': 'admin',
}
LEGACY_TOOLS = frozenset((
    'get_nas_status', 'get_media_status', 'set_fan_policy', 'release_fan',
    'set_light_policy', 'set_rgb', 'set_screen', 'update_screen_settings',
    'request_power', 'cancel_power',
))
_ROLE_LEVELS = {'readonly': 0, 'readwrite': 1, 'admin': 2}
_ROOT_ID_RE = re.compile(r'root_[0-9a-f]{24}\Z', re.ASCII)


def valid_file_roots(value, role, scopes):
    """Check canonical grants without resolving or opening a storage path.

    Empty paths select an entire known root. Nonempty paths are relative POSIX
    subdirectories, with no aliases that could hide traversal or duplicate grants.
    """
    if not isinstance(value, list) or len(value) > 32:
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


def permission_allows(principal, scope, minimum_role, *, tool_name=None):
    """Apply a role ceiling AND an explicitly selected domain, never role alone.

    Older in-process fixtures without role/version metadata use the same legacy
    ten-tool boundary as migrated v1 keys. Any partially supplied or mismatched
    metadata fails closed; a legacy grant never reaches a newly added tool.
    """
    if (not isinstance(principal, dict) or not isinstance(scope, str)
            or scope not in SCOPES or not isinstance(minimum_role, str)
            or minimum_role not in _ROLE_LEVELS):
        return False
    scopes = principal.get('scopes')
    if (not isinstance(scopes, (list, tuple, set, frozenset)) or not scopes
            or any(not isinstance(item, str) or item not in SCOPES for item in scopes)
            or len(set(scopes)) != len(scopes) or scope not in scopes):
        return False
    if 'role' not in principal and 'policy_version' not in principal:
        legacy = True
    else:
        version, role = principal.get('policy_version'), principal.get('role')
        if type(version) is not int:
            return False
        if version == 1 and role == 'legacy':
            if principal.get('file_roots') != []:
                return False
            legacy = True
        elif version == 2 and isinstance(role, str) and role in _ROLE_LEVELS:
            return (valid_file_roots(principal.get('file_roots'), role, scopes)
                    and _ROLE_LEVELS[role] >= _ROLE_LEVELS[minimum_role])
        else:
            return False
    return (legacy and isinstance(tool_name, str) and tool_name in LEGACY_TOOLS
            and all(item in LEGACY_SCOPES for item in scopes))

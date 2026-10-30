"""Metadata-only evidence checklist; never opens or authorizes user files.

Only the existing root planner is called. Firmware reference facts describe an
offline sample, not the running NAS. No caller-supplied account/ACL evidence can
turn this diagnostic into a file backend or a runtime authorization decision.
"""

import copy
import re

from api_file_roots import collect_file_roots, is_admin_root_path, valid_relative_file_path


_ROOT_ID = re.compile(r'root_[0-9a-f]{24}\Z', re.ASCII)
_MAX_ROOTS = 128
_MAX_GRANTS = 32
_STATIC_REFERENCE = {
    'scope': 'reference_firmware_only',
    'firmware_build': 'V6.3.3_Build_260907',
    'sample_sha256': '402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26',
    'runtime_verified': False,
    'route': '/rest/1.0/file',
    'actions': ['get_list', 'get_info', 'download'],
    'known': ['inline_action_branches', 'path_authorization_check',
              'list_limit_offset', 'download_byte_stream_and_range_branch'],
    'unverified': ['http_method_and_encoding', 'virtual_path_to_root_mapping',
                   'nas_subject_to_unix_identity', 'native_acl_to_opened_object',
                   'namespace_race_safety', 'complete_range_response_contract'],
    'reason': '已实现精确固件的 admin 只读后端；本诊断不主动执行其原厂和真实句柄核验。',
}


def _plain(value, maximum, *, empty=False):
    return (isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum
            and all(char.isprintable() for char in value))


def _project_roots(raw):
    """Project bounded planner metadata; never copy mounts, secrets or extras."""
    if (not isinstance(raw, dict) or raw.get('available') is not True
            or raw.get('status') != 'authorization_plan'
            or type(raw.get('boundary_version')) is not int or raw['boundary_version'] != 3
            or not isinstance(raw.get('roots'), list)
            or not 0 < len(raw['roots']) <= _MAX_ROOTS):
        return []
    result, identities = [], set()
    for row in raw['roots']:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not _ROOT_ID.fullmatch(row['id']) or row['id'] in identities
                or not is_admin_root_path(row.get('mount'))
                or type(row.get('read_only')) is not bool
                or not _plain(row.get('label'), 128)):
            return []
        identities.add(row['id'])
        result.append({'id': row['id'], 'label': row['label'], 'read_only': row['read_only']})
    return result


def _selection(grants, roots):
    if grants is None or grants == []:
        return {'status': 'not_selected', 'verified': False, 'grants': [],
                'reason': '尚未选择文件范围；未检查或打开目录。'}
    if not isinstance(grants, list) or len(grants) > _MAX_GRANTS:
        return {'status': 'unverified', 'verified': False, 'grants': [],
                'reason': '文件范围格式或数量无效。'}
    known = {row['id']: row for row in roots}
    identities = set()
    output = []
    for index, row in enumerate(grants):
        row = row if isinstance(row, dict) else {}
        root_id = row.get('id')
        root_id = root_id if isinstance(root_id, str) and _ROOT_ID.fullmatch(root_id) else None
        path = row.get('path') if valid_relative_file_path(row.get('path')) else None
        access = row.get('access') if row.get('access') in ('readonly', 'readwrite') else None
        valid = (set(row) == {'id', 'path', 'access'} and root_id is not None
                 and path is not None and access is not None)
        identity = (root_id, path)
        root = known.get(root_id)
        if not valid:
            reason = '文件范围格式无效。'
        elif identity in identities:
            reason = '文件范围重复；未合并或扩大授权。'
        elif root is None:
            reason = '当前 admin 根规划无法确认此绑定；不换绑旧 Users 授权或其他盘。'
        elif root['read_only'] and access == 'readwrite':
            reason = '当前挂载只读，不能确认原读写范围。'
        else:
            reason = None
        if valid:
            identities.add(identity)
        verified = reason is None
        output.append({'index': index, 'id': root_id, 'path': path, 'access': access,
                       'status': 'verified' if verified else 'unverified',
                       'verified': verified, 'verification': 'authorization_plan_only',
                       'file_access_verified': False,
                       'reason': reason or '仅与根规划匹配，尚未证明实时对象与 ACL。'})
    verified = bool(output) and all(row['verified'] for row in output)
    return {'status': 'verified' if verified else 'unverified', 'verified': verified,
            'grants': output,
            'reason': '仅确认授权规划绑定；不授予文件访问。' if verified
            else '至少一条文件范围无法确认，完整保留各条结果。'}


def _check(identifier, status, scope, reason):
    return {'id': identifier, 'status': status, 'verified': status == 'verified',
            'scope': scope, 'reason': reason}


def collect_file_access_readiness(panel, *, grants=None, root_collector=None):
    """Return all known readiness gaps without touching user data or credentials.

    ``panel`` is the existing cached panel provider; ``grants`` is an optional
    owner-provided list of configured ``{id,path,access}`` ranges. ``root_collector``
    is an internal fixture seam, never an HTTP parameter. Neither its extra fields
    nor configured ranges are accepted as proof of account, ACL or fd safety.
    """
    collector = collect_file_roots if root_collector is None else root_collector
    try:
        roots = _project_roots(collector(panel))
    except Exception:
        # Fixed failure only: provider exceptions may include paths or secrets.
        roots = []
    planning_available = bool(roots)
    selection = _selection(grants, roots)
    selected = selection['status'] != 'not_selected'
    checks = [
        _check('root_metadata_plan', 'verified' if planning_available else 'unverified',
               'kernel_mount_metadata_and_cached_disk_identity',
               '已有可核对的根规划；没有访问盘内目录。' if planning_available
               else '当前没有可确认的根规划；缺少或不一致的身份不猜测补全。'),
        _check('selected_grant_bindings', selection['status'], 'authorization_plan_only',
               selection['reason']),
        _check('native_path_protocol', 'unverified', 'runtime_native_contract',
               '静态动作存在，但当前方法、编解码、虚拟路径映射及分页合同未核实。'),
        _check('nas_subject_mapping', 'unverified', 'runtime_account_identity',
               '生产只读后端逐操作核验固定管理员逻辑身份；本元数据诊断没有主动读账号或会话。'),
        _check('uid_gid_groups_mapping', 'unverified', 'runtime_unix_identity',
               '不采用 NAS ID 到 Unix UID 的等值映射；只读后端按原厂逻辑 ACL 对同一真实对象判权。'),
        _check('native_acl_semantics', 'unverified', 'effective_acl_for_same_object',
               '生产后端限已审计的 admin 子树，每次以原厂根读取许可和同对象拓扑重验；此处尚未执行。'),
        _check('live_root_object_identity', 'unverified' if selected else 'not_selected',
               'runtime_opened_object',
               '根规划不是实际打开时的挂载、父目录与目标对象证明。' if selected
               else '未选择目标，未打开或检查用户目录。'),
        _check('safe_open_same_object', 'unverified', 'acl_to_opened_handle',
               '生产后端已实现同挂载 O_NOFOLLOW 句柄遍历与身份复核；本规划结果不替代逐操作核验。'),
        _check('download_range_and_stream', 'unverified', 'runtime_binary_transport',
               '已实现真实 fd、Range 和逐块撤权检查；实机样本字节验收尚未完成。'),
        _check('read_only_device_acceptance', 'unverified', 'real_backend_and_device',
               '元数据诊断和静态参考不代替目录、ACL 与样本字节验收。'),
    ]
    blockers = [row['id'] for row in checks if row['status'] != 'verified']
    return {
        'schema_version': 1, 'scope': 'file_access_readiness', 'status': 'metadata_only',
        'diagnostic_available': True, 'available': False, 'file_access_available': False,
        'planning': {'available': planning_available,
                     'status': 'authorization_plan' if planning_available else 'unavailable',
                     'roots': roots, 'selection': selection},
        'checks': checks, 'blockers': blockers,
        'execution_backend': {
            'status': 'requires_per_operation_verification',
            'implemented_tools': ['list_directory', 'get_file_info', 'prepare_file_download',
                                  'get_file_transfer', 'cancel_file_transfer'],
            'supported_path_classes': ['owner_admin'],
            'authorization_model': 'native_logical_owner_acl_for_same_opened_object',
            'runtime_verified': None, 'write_implemented': False,
            'reason': '本入口只检查规划；实际访问须重新核验原厂许可、精确固件和同一文件句柄。'},
        'account_mapping': {'verified': False, 'nas_user_id': None, 'unix_uid': None,
                            'unix_gid': None, 'supplementary_groups': None},
        'capabilities': {
            'root_metadata': planning_available, 'list_directory': False,
            'get_file_info': False, 'download': False, 'upload': False, 'file_write': False},
        'effects': {'metadata_only': True, 'enumerates_user_directories': False,
                    'opens_user_files': False, 'starts_smart': False,
                    'reads_credentials': False, 'calls_native_actions': False,
                    'grants_authorization': False, 'root_fallback_allowed': False},
        'static_reference': copy.deepcopy(_STATIC_REFERENCE),
    }

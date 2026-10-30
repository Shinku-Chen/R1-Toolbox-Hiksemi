"""Evidence for a file backend, without treating metadata as file permission.

No file MCP callback is registered here. The factory's path ACL uses a logical
NAS identity; neither root-plan existence nor a Unix uid candidate proves that
ACL. The explicit context probe may touch Users directory metadata, never user
file contents, and must not join passive/default polling.
"""

import hashlib
import os
from pathlib import Path
import re
import stat
import time

from api_file_roots import collect_file_roots, is_users_root_path, valid_relative_file_path
from api_permissions import permission_allows


_OWNER = 100000
_MAX_ROOTS = 8
_MAX_METADATA = 262144
_REFERENCE = '402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26'
_REASONS = {
    'root_unavailable': '当前已授权 Users 根的真实挂载与生命周期无法确认。',
    'unsupported_file_type': '当前对象类型、符号链接或硬链接不支持文件访问。',
    'unsupported_path_class': '原厂文件后端仅接入管理员和 public 子树；其他用户空间尚不支持。',
    'not_found': '已授权目录中未找到此对象。',
    'directory_changed': '目录或分页绑定发生变化，请重新浏览。',
    'invalid_arguments': '文件参数无效。',
    'permission_denied': '当前密钥未获此文件范围及工具的权限。',
    'invalid_key': '当前密钥授权已失效。',
    'native_unavailable': '原厂文件服务的身份、会话或只读传输暂不可确认。',
    'timeout': '原厂文件只读请求超过固定时间窗口。',
    'resource_busy': '原厂文件只读通道正在使用，请稍后再试。',
    'acl_unverified': 'NAS 主体和原厂 ACL 尚未形成同对象授权证明。',
    'secure_open_unavailable': '尚无已核验的安全文件打开后端。',
    'file_changed': '文件身份或内容状态发生变化。',
    'transfer_expired': '文件传输窗口已过期。',
    'transfer_busy': '文件传输正在使用或超过名额。',
    'unsupported_range': '仅支持单个规范字节范围。',
    'transfer_unavailable': '当前密钥无法使用该传输。',
    'limit_exceeded': '传输超过已声明长度或服务限额。',
    'checksum_mismatch': '上传长度或 SHA-256 校验不一致。',
    'write_backend_unverified': '原厂写入、配额、索引与原子提交语义尚未核验。',
    'idempotency_conflict': '幂等标识已经绑定其他操作或参数。',
    'result_unconfirmed': '文件操作结果未确认；未自动重试或声称已回滚。',
}


class FileAccessError(Exception):
    """Fixed public error codes; never retain paths or exception messages."""

    def __init__(self, code):
        self.code = code if code in _REASONS else 'secure_open_unavailable'
        super().__init__(self.code)

    def result(self):
        return {'ok': False, 'error': {'code': self.code,
                'message': _REASONS[self.code], 'retryable': False}}


def assert_file_grant(principal, tool, root_id, path, *, write=False):
    """Check the configured range only; this deliberately grants no OS access."""
    minimum = 'readwrite' if write else 'readonly'
    if (not valid_relative_file_path(path) or not isinstance(root_id, str)
            or not re.fullmatch(r'root_[0-9a-f]{24}', root_id, re.ASCII)):
        raise FileAccessError('invalid_arguments')
    if not permission_allows(principal, 'files', minimum, tool_name=tool):
        raise FileAccessError('permission_denied')
    for grant in principal.get('file_roots', []):
        prefix = grant['path']
        if (grant['id'] == root_id and (not prefix or path == prefix or path.startswith(prefix + '/'))
                and (not write or grant['access'] == 'readwrite')):
            return
    raise FileAccessError('permission_denied')


def _read_fixed(root, relative, maximum=_MAX_METADATA):
    """Read bounded fixed host metadata; callers cannot supply a relative path."""
    path = Path(root).absolute() / relative
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError('invalid_metadata')
        # proc seq_files have st_size == 0 and may short-read before EOF. A
        # single read can silently omit later mount rows even below this cap.
        raw = bytearray()
        while len(raw) <= maximum:
            block = os.read(descriptor, min(65536, maximum + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(descriptor)
        if (len(raw) > maximum or before.st_size and len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError('metadata_changed')
        return raw.decode('utf-8', 'strict')
    finally:
        os.close(descriptor)


def _owner_identity(root='/'):
    """Read two fixed noncredential DB fields; never inspect a session/token row."""
    try:
        import sqlite3
    except ImportError:
        return False
    connection = None
    try:
        path = Path(root).absolute() / 'tmp/nand_config/USER_AUTH_DB_101'
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
            return False
        resolved = path.resolve(strict=True)
        resolved.relative_to(Path(root).resolve(strict=True))
        connection = sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True, timeout=0.2,
                                     isolation_level=None)
        deadline = time.monotonic() + 0.5
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        connection.execute('PRAGMA query_only=ON')

        def authorize(action, table, column, database, caller):
            if action == sqlite3.SQLITE_READ:
                valid = (database == 'main' and table == 'tb_user_auth'
                         and column in ('user_id', 'user_type'))
                return sqlite3.SQLITE_OK if valid else sqlite3.SQLITE_DENY
            return (sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION)
                    else sqlite3.SQLITE_DENY)

        connection.set_authorizer(authorize)
        connection.execute('BEGIN')
        rows = connection.execute('SELECT user_id, user_type FROM tb_user_auth '
                                  'WHERE user_id = ? LIMIT 2', (_OWNER,)).fetchall()
        after = path.lstat()
        return (len(rows) == 1 and all(type(value) is int for value in rows[0])
                and rows == [(_OWNER, 0)] and time.monotonic() < deadline
                and stat.S_ISREG(after.st_mode) and path.resolve(strict=True) == resolved
                and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino))
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def _mapping_candidate(root):
    """A passwd coincidence is observable evidence, never a NAS subject map."""
    try:
        passwd = _read_fixed(root, 'etc/passwd')
        groups = _read_fixed(root, 'etc/group')
        if len(passwd.splitlines()) > 512 or len(groups.splitlines()) > 512:
            raise ValueError('metadata_limit')
        found = False
        for line in passwd.splitlines():
            fields = line.split(':')
            if len(fields) != 7 or not fields[2].isdigit() or not fields[3].isdigit():
                raise ValueError('invalid_passwd')
            found |= int(fields[2]) == _OWNER
        for line in groups.splitlines():
            fields = line.split(':')
            if len(fields) != 4 or not fields[2].isdigit():
                raise ValueError('invalid_group')
        return {'status': 'unverified', 'reason': 'same_numeric_uid_is_only_a_candidate'
                if found else 'no_fixed_owner_uid_candidate_in_unix_accounts'}
    except Exception:
        return {'status': 'unverified', 'reason': 'unix_mapping_metadata_unavailable'}


def _mounts(root):
    result = {}
    for line in _read_fixed(root, 'proc/self/mountinfo').splitlines():
        halves = line.split(' - ', 1)
        if len(halves) != 2:
            raise ValueError('invalid_mountinfo')
        fields = halves[0].split()
        if len(fields) < 6 or not fields[0].isdigit():
            raise ValueError('invalid_mountinfo')
        path = re.sub(r'\\(040|011|012|134)', lambda m: chr(int(m[1], 8)), fields[4])
        result.setdefault(path, []).append((fields[0], fields[2]))
    return result


def _descriptor_mount_id(root, descriptor):
    text = _read_fixed(root, 'proc/self/fdinfo/' + str(descriptor), 4096)
    matches = re.findall(r'^mnt_id:\s*([0-9]+)\s*$', text, flags=re.MULTILINE)
    return matches[0] if len(matches) == 1 else None


def _users_context(root, row, expected):
    """Open directory handles only, rejecting links and unexpected mount hops."""
    failure = {'root_id': row['id'], 'status': 'unverified', 'reason': 'safe_metadata_open_unavailable',
               'handle_fingerprint': None, 'unix_mapping': 'unverified', 'native_acl': 'unverified'}
    if os.name != 'posix' or not hasattr(os, 'O_PATH') or not is_users_root_path(row.get('mount')):
        return failure
    descriptors = []
    try:
        flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(Path(root).absolute(), flags)
        descriptors.append(descriptor)
        for component in row['mount'].split('/')[1:]:
            descriptor = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        disk, users = descriptors[-2:]
        disk_info, user_info = os.fstat(disk), os.fstat(users)
        disk_mount = _descriptor_mount_id(root, disk)
        users_mount = _descriptor_mount_id(root, users)
        if (expected is None or disk_mount != expected[0] or users_mount != disk_mount
                or f'{os.major(disk_info.st_dev)}:{os.minor(disk_info.st_dev)}' != expected[1]
                or user_info.st_dev != disk_info.st_dev):
            return dict(failure, reason='opened_mount_identity_unconfirmed')
        identity = (disk_info.st_dev, disk_info.st_ino, disk_mount,
                    user_info.st_dev, user_info.st_ino, user_info.st_ctime_ns)
        fingerprint = hashlib.sha256(repr(identity).encode('ascii')).hexdigest()
        return dict(failure, status='verified', reason='directory_handle_metadata_only',
                    handle_fingerprint=fingerprint)
    except Exception:
        return failure
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def collect_file_backend_context(panel, *, root='/', root_collector=None,
                                 owner_reader=None, runtime_reader=None):
    """Explicit bounded evidence probe; injection arguments are never HTTP input.

    Reading Users directory metadata may wake a disk. This function enumerates
    no directory and opens no user content. It does not sign native requests,
    read tokens, or assert that a uid candidate can authorize a file operation.
    """
    started = time.monotonic()
    collector = collect_file_roots if root_collector is None else root_collector
    result = {'schema_version': 1, 'scope': 'file_backend_context', 'available': True,
              'status': 'evidence_incomplete', 'file_access_available': False,
              'authorization_model': 'native_logical_owner_acl_for_same_opened_object',
              'unix_identity_mapping_required': False,
              'owner': {'status': 'unverified', 'reason': 'fixed_owner_unconfirmed'},
              'unix_mapping': _mapping_candidate(root), 'roots': [], 'complete': False,
              'native_contract': {'status': 'unverified', 'reason': 'path_acl_not_bound_to_opened_object',
                                  'reference_sha256': _REFERENCE, 'runtime_verified': False,
                                  'route': '/rest/1.0/file', 'actions': ['get_list', 'get_info', 'download'],
                                  'path_acl_reference': 'rest_check_path_auth@0x281dc9',
                                  'listener': 'unverified'},
              'blockers': ['native_root_read_permission', 'native_acl_to_opened_object',
                           'runtime_file_protocol', 'native_write_index_quota_atomicity'],
              'effects': {'metadata_only': True, 'may_wake_disks': True,
                          'enumerates_user_directories': False, 'opens_user_files': False,
                          'reads_credentials': False, 'calls_native_actions': False,
                          'starts_smart': False, 'grants_authorization': False}}
    try:
        if (owner_reader or _owner_identity)(root=root) is True:
            result['owner'] = {'status': 'verified', 'reason': 'fixed_owner_logical_identity_only'}
        if runtime_reader is None:
            from vendor_runtime import require_emhistor_listener
            runtime_reader = require_emhistor_listener
        native = runtime_reader(root=root)
        result['native_contract']['listener'] = ('verified' if isinstance(native, dict)
                                                 and native.get('available') is True
                                                 and native.get('status') == 'verified' else 'unverified')
        boot = _read_fixed(root, 'proc/sys/kernel/random/boot_id', 128)
        before = collector(panel)
        mounts_before = _mounts(root)
        rows = before.get('roots') if isinstance(before, dict) else None
        if (not isinstance(rows, list) or before.get('boundary_version') != 2
                or before.get('available') is not True or len(rows) > 128):
            return result
        for row in rows[:_MAX_ROOTS]:
            if time.monotonic() - started > 4:
                break
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                    or not re.fullmatch(r'root_[0-9a-f]{24}', row['id'], re.ASCII)
                    or not is_users_root_path(row.get('mount'))):
                continue
            entries = mounts_before.get(row['mount'][:-6], [])
            expected = entries[0] if len(entries) == 1 else None
            result['roots'].append(_users_context(root, row, expected))
        after = collector(panel)
        mounts_after = _mounts(root)
        same = (boot == _read_fixed(root, 'proc/sys/kernel/random/boot_id', 128)
                and before == after and mounts_before == mounts_after)
        if not same:
            for row in result['roots']:
                row.update(status='unverified', reason='root_lifecycle_changed', handle_fingerprint=None)
        result['complete'] = same and len(result['roots']) == len(rows)
    except Exception:
        # Partial context is useful, but exceptions may contain account/path data.
        result['complete'] = False
    return result

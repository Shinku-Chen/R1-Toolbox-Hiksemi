"""Describe eligible storage roots using mount metadata and cached disk identity.

This is an authorization planner, not a filesystem browser. It never stats a
data path, enumerates a directory, reads file content or starts a SMART query.
Future file operations must revalidate live identity, ACLs and opened handles;
this metadata alone is not proof that a path is safe to open.
"""

import hashlib
import json
from pathlib import Path
import re


_MAX_MOUNTINFO = 262144
_MAX_ROOTS = 128
_DEVICE = re.compile(r'/dev/((?:sd[a-z]+|vd[a-z]+|xvd[a-z]+))(?:[0-9]+)?\Z')
_NVME = re.compile(r'/dev/(nvme[0-9]+n[0-9]+)(?:p[0-9]+)?\Z')
_FILESYSTEMS = frozenset(('ext2', 'ext3', 'ext4', 'btrfs', 'xfs', 'ntfs',
                          'ntfs3', 'fuseblk', 'exfat', 'vfat', 'f2fs', 'jfs'))
_ESCAPE = re.compile(r'\\(040|011|012|134)')
_ROOT_ID = re.compile(r'root_[0-9a-f]{24}\Z', re.ASCII)
_BOUNDARY_VERSION = 2


def _plain(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and value == value.strip() and all(c.isprintable() for c in value))


def _unescape(value):
    return _ESCAPE.sub(lambda match: chr(int(match[1], 8)), value)


def _data_path(value):
    """Only a directly mounted /drives/<disk> may supply a Users root."""
    return (_plain(value, 1024) and value.startswith('/drives/')
            and '\\' not in value and not value.endswith('/')
            and len(value.split('/')) == 3
            and all(part not in ('', '.', '..') for part in value.split('/')[1:]))


def is_users_root_path(value):
    """Check only lexical boundary metadata; this does not resolve the directory."""
    return (isinstance(value, str) and value.endswith('/Users')
            and _data_path(value[:-6]))


def valid_relative_file_path(value):
    """Accept literal POSIX paths below a selected Users root, without decoding."""
    if (not isinstance(value, str) or len(value) > 1024 or '\\' in value
            or any(not char.isprintable() for char in value)
            or (value and any(part in ('', '.', '..') for part in value.split('/')))):
        return False
    try:
        return len(value.encode('utf-8', 'strict')) <= 4096
    except UnicodeError:
        return False


def _empty(status='unavailable', reason='尚无已核验的数据盘目录；未浏览文件或唤醒硬盘。'):
    return {'available': False, 'status': status, 'reason': reason, 'roots': [],
            'boundary_version': _BOUNDARY_VERSION}


def collect_file_roots(panel):
    """Bind cached physical serials to current local data mounts, fail closed.

    IDs include the physical identity, Users boundary version and mount metadata,
    never the user label. Old whole-disk IDs cannot become Users grants silently.
    A missing serial, ambiguous mount, unsupported virtual/RAID source or changed
    association yields no selectable grant. This catalog confers no file I/O.
    """
    try:
        with (Path(panel.proc_root) / 'self/mountinfo').open('rb') as stream:
            raw = stream.read(_MAX_MOUNTINFO + 1)
        if len(raw) > _MAX_MOUNTINFO:
            return _empty('limit_exceeded', '挂载信息超过授权规划上限。')
        mountinfo = raw.decode('utf-8', 'strict')
        # Identity is consumed privately for binding, never projected to roots.
        # The default public overview intentionally redacts serials. This flag
        # reads only the existing lifecycle-verified identity cache, not SMART.
        snapshot = panel.overview(include_identity=True)
        disks = snapshot.get('disks_detail') if isinstance(snapshot, dict) else None
        if not isinstance(disks, list) or len(disks) > 128:
            return _empty()
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        return _empty()

    identities, serials = {}, {}
    for disk in disks:
        if not isinstance(disk, dict):
            continue
        name, serial, mounts = disk.get('name'), disk.get('serial'), disk.get('mounts')
        if (not _plain(name, 64) or not _plain(serial, 120)
                or not isinstance(mounts, list) or len(mounts) > 128):
            continue
        identities.setdefault(name, []).append(disk)
        serials.setdefault(serial, []).append(name)

    # A stacked mount hides the older path. Never pick whichever row is last.
    mounts, full_sources, full_devices = {}, {}, {}
    for line in mountinfo.splitlines():
        fields = line.split(' - ', 1)
        if len(fields) != 2:
            return _empty(reason='挂载信息格式无效；未确认文件范围。')
        before, after = fields[0].split(), fields[1].split()
        if (len(before) < 6 or len(after) < 3
                or not before[0].isdigit() or not before[1].isdigit()
                or not re.fullmatch(r'[0-9]+:[0-9]+', before[2])):
            return _empty(reason='挂载信息格式无效；未确认文件范围。')
        mount = _unescape(before[4])
        # Keep all mounts, including virtual/bind mounts below Users. Dropping
        # an ineligible child row would incorrectly approve the encompassing root.
        if not mount.startswith('/'):
            return _empty(reason='挂载信息格式无效；未确认文件范围。')
        mounts.setdefault(mount, []).append((before, after))
        source = _unescape(after[1])
        # A filesystem can expose subdirectory binds outside Users without
        # changing the one canonical full /drives mount. Only duplicate complete
        # filesystem roots are ambiguous; separate partitions remain distinct.
        if before[3] == '/':
            full_sources.setdefault(source, []).append(mount)
            full_devices.setdefault(before[2], []).append(mount)

    roots = []
    for mount, entries in sorted(mounts.items()):
        if not _data_path(mount) or len(entries) != 1:
            continue
        before, after = entries[0]
        source, filesystem = _unescape(after[1]), after[0]
        match = _DEVICE.fullmatch(source) or _NVME.fullmatch(source)
        mount_flags = set(before[5].split(',')) & {'ro', 'rw'}
        super_flags = set(after[2].split(',')) & {'ro', 'rw'}
        if (not match or filesystem not in _FILESYSTEMS
                or before[3] != '/' or len(full_sources[source]) != 1
                or len(full_devices[before[2]]) != 1
                or len(mount_flags) != 1 or len(super_flags) != 1
                or len(mounts.get('/drives', [])) > 1 or len(mounts.get('/', [])) > 1):
            continue
        users = mount + '/Users'
        if any(path == users or path.startswith(users + '/') for path in mounts):
            continue
        candidates = identities.get(match[1], [])
        if len(candidates) != 1:
            continue
        disk = candidates[0]
        if (len(serials[disk['serial']]) != 1
                or mount not in [_unescape(value) for value in disk['mounts'] if isinstance(value, str)]):
            continue
        identity = ['r1-file-users-root-v2', disk['serial'], source, before[2], filesystem, users]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True,
                                           separators=(',', ':')).encode('ascii')).hexdigest()
        label = disk.get('user_name')
        if not _plain(label, 128):
            label = disk.get('volume')
        if not _plain(label, 128):
            label = mount.rsplit('/', 1)[-1]
        roots.append({'id': 'root_' + digest[:24], 'label': label, 'mount': users,
                      'read_only': 'ro' in mount_flags or 'ro' in super_flags})
        if len(roots) > _MAX_ROOTS:
            return _empty('limit_exceeded', '数据盘目录超过授权规划上限。')
    if not roots:
        return _empty()
    return {'available': True, 'status': 'authorization_plan', 'roots': roots,
            'boundary_version': _BOUNDARY_VERSION,
            'reason': '仅规划数据盘 Users 子目录；未确认目录存在、原厂 ACL 或安全打开，文件传输尚未接入。'}


def validate_file_grants(grants, catalog):
    """Check owner-selected root IDs against this request's metadata snapshot."""
    if not isinstance(grants, list) or len(grants) > 32:
        raise ValueError('文件范围格式无效')
    if (not isinstance(catalog, dict) or type(catalog.get('boundary_version')) is not int
            or catalog['boundary_version'] != _BOUNDARY_VERSION
            or catalog.get('available') is not True or catalog.get('status') != 'authorization_plan'
            or not isinstance(catalog.get('roots'), list)
            or not 0 < len(catalog['roots']) <= _MAX_ROOTS):
        raise ValueError('当前无法确认 Users 文件边界，请刷新后选择')
    roots = {}
    for row in catalog['roots']:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not _ROOT_ID.fullmatch(row['id']) or row['id'] in roots
                or not is_users_root_path(row.get('mount'))
                or type(row.get('read_only')) is not bool):
            raise ValueError('当前无法确认 Users 文件边界，请刷新后选择')
        roots[row['id']] = row
    selected = set()
    for grant in grants:
        if (not isinstance(grant, dict) or set(grant) != {'id', 'path', 'access'}
                or not valid_relative_file_path(grant.get('path'))
                or grant.get('access') not in ('readonly', 'readwrite')):
            raise ValueError('文件范围格式无效')
        root = roots.get(grant['id']) if isinstance(grant.get('id'), str) else None
        if root is None:
            raise ValueError('文件范围对应的数据盘目录当前无法确认，请刷新后选择')
        binding = (grant['id'], grant['path'])
        if binding in selected:
            raise ValueError('文件范围重复')
        selected.add(binding)
        if root['read_only'] and grant.get('access') == 'readwrite':
            raise ValueError('只读挂载不能授予文件写入权限')

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


def _plain(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and value == value.strip() and all(c.isprintable() for c in value))


def _unescape(value):
    return _ESCAPE.sub(lambda match: chr(int(match[1], 8)), value)


def _data_path(value):
    """Only canonical data mounts beneath /drives are eligible in this phase."""
    return (_plain(value, 1024) and value.startswith('/drives/')
            and '\\' not in value and not value.endswith('/')
            and all(part not in ('', '.', '..') for part in value.split('/')[1:]))


def _empty(status='unavailable', reason='尚无已核验的数据盘目录；未浏览文件或唤醒硬盘。'):
    return {'available': False, 'status': status, 'reason': reason, 'roots': []}


def collect_file_roots(panel):
    """Bind cached physical serials to current local data mounts, fail closed.

    IDs include the physical identity and mount metadata, never the user label.
    A missing serial, ambiguous mount, unsupported virtual/RAID source or changed
    association yields no selectable grant. This catalog confers no file I/O.
    """
    try:
        with (Path(panel.proc_root) / 'self/mountinfo').open('rb') as stream:
            raw = stream.read(_MAX_MOUNTINFO + 1)
        if len(raw) > _MAX_MOUNTINFO:
            return _empty('limit_exceeded', '挂载信息超过授权规划上限。')
        mountinfo = raw.decode('utf-8', 'strict')
        snapshot = panel.overview()
        disks = snapshot.get('disks_detail') if isinstance(snapshot, dict) else None
        if not isinstance(disks, list) or len(disks) > 128:
            return _empty()
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        return _empty()

    identities = {}
    for disk in disks:
        if not isinstance(disk, dict):
            continue
        name, serial, mounts = disk.get('name'), disk.get('serial'), disk.get('mounts')
        if (not _plain(name, 64) or not _plain(serial, 120)
                or not isinstance(mounts, list) or len(mounts) > 128):
            continue
        identities.setdefault(name, []).append(disk)

    # A stacked mount hides the older path. Never pick whichever row is last.
    mounts = {}
    for line in mountinfo.splitlines():
        fields = line.split(' - ', 1)
        if len(fields) != 2:
            continue
        before, after = fields[0].split(), fields[1].split()
        if len(before) < 6 or len(after) < 3:
            continue
        mount = _unescape(before[4])
        if _data_path(mount):
            mounts.setdefault(mount, []).append((before, after))

    roots = []
    for mount, entries in sorted(mounts.items()):
        if len(entries) != 1:
            continue
        before, after = entries[0]
        source, filesystem = _unescape(after[1]), after[0]
        match = _DEVICE.fullmatch(source) or _NVME.fullmatch(source)
        if (not match or filesystem not in _FILESYSTEMS
                or before[3] != '/' or not re.fullmatch(r'[0-9]+:[0-9]+', before[2])):
            continue
        candidates = identities.get(match[1], [])
        if len(candidates) != 1:
            continue
        disk = candidates[0]
        if mount not in [_unescape(value) for value in disk['mounts'] if isinstance(value, str)]:
            continue
        identity = ['r1-file-root-v1', disk['serial'], source, before[2], filesystem, mount]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True,
                                           separators=(',', ':')).encode('ascii')).hexdigest()
        label = disk.get('user_name')
        if not _plain(label, 128):
            label = disk.get('volume')
        if not _plain(label, 128):
            label = mount.rsplit('/', 1)[-1]
        roots.append({'id': 'root_' + digest[:24], 'label': label, 'mount': mount,
                      'read_only': 'ro' in before[5].split(',') or 'ro' in after[2].split(',')})
        if len(roots) > _MAX_ROOTS:
            return _empty('limit_exceeded', '数据盘目录超过授权规划上限。')
    if not roots:
        return _empty()
    return {'available': True, 'status': 'authorization_plan', 'roots': roots,
            'reason': '仅由现有磁盘身份缓存和挂载信息生成授权范围；文件传输尚未接入。'}


def validate_file_grants(grants, catalog):
    """Check owner-selected root IDs against this request's metadata snapshot."""
    if not isinstance(grants, list) or len(grants) > 32:
        raise ValueError('文件范围格式无效')
    roots = {row['id']: row for row in catalog.get('roots', [])}
    for grant in grants:
        if not isinstance(grant, dict) or set(grant) != {'id', 'path', 'access'}:
            raise ValueError('文件范围格式无效')
        root = roots.get(grant['id']) if isinstance(grant.get('id'), str) else None
        if root is None:
            raise ValueError('文件范围对应的数据盘目录当前无法确认，请刷新后选择')
        if root['read_only'] and grant.get('access') == 'readwrite':
            raise ValueError('只读挂载不能授予文件写入权限')

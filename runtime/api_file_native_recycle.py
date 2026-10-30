"""Fixed v260907 recoverable owner archive, never a delete/action/SQL proxy.

Only an already verified object in an owner-established CustodyStage may move.
The archive is the real factory admin recycle directory and the exact factory
17-column database. Intent is durable before moving; failures retain the inode
and an owner recovery locator. No code in this module unlinks a user object.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import hashlib
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import struct
import threading
import time
from urllib.parse import quote

from api_file_operations import FileAccessError, _descriptor_mount_id, _read_fixed
from api_file_provider import audited_owner_write_rule
from api_file_native_index import (_authorized, _internal_sata, _listener_signature,
                                   _paths, _RECORD, _SOCKET)
from api_file_native_index_reader import NativeFileIndexReader, _fd_snapshot, _fd_flags
from api_file_mutation_authority import _read_at, _trusted
from vendor_media_auth import OWNER_ID


_ARCHIVE = 'admin/.DATA_CACHE/Recycle/' + str(OWNER_ID)
_CACHE = ('.DATA_CACHE', 'Recycle')
_DB = 'Recycle.db'
_SCRIPT_SHA256 = 'b091efc416336eae138b67b428d8d1b2d68a8de125a5d4004d3a9ea55a5ebfd4'
_COLUMNS = ('id', 'uuid', 'src', 'name', 'isdir', 'deltime', 'userid', 'md5name',
            'size', 'mtime', 'ctime', 'i32flag1', 'i32flag2', 'i64flag1', 'i64flag2',
            'szflag1', 'szflag2')
_TYPES = ('INTEGER', 'VARCHAR(64)', 'VARCHAR(1024)', 'VARCHAR(260)', 'INT32', 'INT64',
          'INT32', 'VARCHAR(260)', 'INT64', 'INT64', 'INT64', 'INT32', 'INT32',
          'INT64', 'INT64', 'VARCHAR(260)', 'VARCHAR(260)')
_FIELDS = _COLUMNS
_SELECT = ('SELECT uuid,src,name,isdir,deltime,userid,md5name,size,mtime,ctime '
           'FROM tb_recycle WHERE md5name=? AND userid=? LIMIT 2')
_SELECT_IDENTITY = ('SELECT uuid,src,name,isdir,deltime,userid,md5name,size,mtime,ctime,'
                    'id,i32flag1,i32flag2,i64flag1,i64flag2,szflag1,szflag2 '
                    'FROM tb_recycle WHERE md5name=? AND userid=? LIMIT 2')
_INSERT = ('INSERT INTO tb_recycle(uuid,src,name,isdir,deltime,userid,md5name,size,mtime,ctime) '
           'VALUES(?,?,?,?,?,?,?,?,?,?)')
_DELETE = ('DELETE FROM tb_recycle WHERE uuid=? AND src=? AND name=? AND isdir=? '
           'AND deltime=? AND userid=? AND md5name=? AND size=? AND mtime=? AND ctime=? AND id=? '
           'AND i32flag1 IS NULL AND i32flag2 IS NULL AND i64flag1 IS NULL AND i64flag2 IS NULL '
           'AND szflag1 IS NULL AND szflag2 IS NULL')
_LOCK = threading.RLock()
_TIMEOUT = 8.0


def _fixed_errors(code):
    """Never let a private database/OS exception disclose a host pathname."""
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except FileAccessError:
                raise
            except Exception:
                raise FileAccessError(code) from None
        return call
    return decorate


def _stable(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_nlink)


def _content(info):
    # Capturing and archiving change ctime; the same fd and bytes remain bound
    # by the caller's complete hash before/after custody, not by an old ctime.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns)


def _gate(current, deadline):
    _authorized(current)
    if time.monotonic() >= deadline:
        raise FileAccessError('timeout')


def _verify_bytes(descriptor, expected, current, deadline):
    """Owner recovery checks the saved complete bytes, including old-writer races."""
    if type(expected) is not str or re.fullmatch(r'[0-9a-f]{64}', expected) is None:
        raise FileAccessError('write_backend_unverified')
    before = os.fstat(descriptor)
    state = lambda info: _content(info) + (info.st_ctime_ns,)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 <= before.st_size <= 8 * 1024**3:
        raise FileAccessError('file_changed')
    digest, offset = hashlib.sha256(), 0
    while offset < before.st_size:
        _gate(current, deadline)
        if state(os.fstat(descriptor)) != state(before):
            raise FileAccessError('file_changed')
        block = os.pread(descriptor, min(65536, before.st_size - offset), offset)
        if not block:
            raise FileAccessError('file_changed')
        digest.update(block)
        offset += len(block)
    _gate(current, deadline)
    if (state(os.fstat(descriptor)) != state(before) or os.pread(descriptor, 1, offset)
            or digest.hexdigest() != expected):
        raise FileAccessError('file_changed')


def _arguments(row, path):
    _paths(row, path)
    if (not audited_owner_write_rule(path) or any(p.startswith('.') for p in path.split('/'))
            or len(('/Users/' + path).encode('utf8')) >= 1024):
        raise FileAccessError('unsupported_path_class')
    # Native archive names retain the last extension after a 32-byte digest.
    # Check the real encoded NAME_MAX before the source can enter custody.
    if 32 + len(os.path.splitext(path.rsplit('/', 1)[-1])[1].encode('utf8')) > 255:
        raise FileAccessError('limit_exceeded')


def _native_uuid(root, row):
    """Reproduce audited MKUUID for plain ext4 only, without running a script.

    get_zonelist_info.sh hashes echo(FS_UUID), including its newline. Native
    disk_create_zonelist copies that result into zone+0x45c. RAID, encryption,
    NTFS fallback and missing/duplicate filesystem identities are refused.
    """
    script = _read_fixed(root, 'histor/bin/fs/get_zonelist_info.sh', 65536)
    if hashlib.sha256(script.encode('utf8')).hexdigest() != _SCRIPT_SHA256:
        raise FileAccessError('write_backend_unverified')
    mounts = []
    for line in _read_fixed(root, 'proc/self/mountinfo').splitlines():
        halves = line.split(' - ', 1)
        if len(halves) != 2:
            raise FileAccessError('write_backend_unverified')
        before, after = halves[0].split(), halves[1].split()
        if len(before) < 6 or len(after) < 3:
            raise FileAccessError('write_backend_unverified')
        if before[3] == '/' and before[4].startswith('/drives/') and len(before[4].split('/')) == 3:
            mounts.append((before, after))
    selected = [item for item in mounts if item[0][4] == row['mount'][:-6]]
    if len(selected) != 1 or selected[0][1][0] != 'ext4' or len(mounts) > 128:
        raise FileAccessError('write_backend_unverified')
    values, seen = {}, set()
    for before, after in mounts:
        # Full mount aliases have already been rejected by the Users authority.
        if after[0] != 'ext4' or re.fullmatch(r'/dev/sd[a-z]+[1-9][0-9]*', after[1]) is None:
            continue
        raw = _read_fixed(root, 'run/udev/data/b' + before[2], 65536)
        fields = {}
        for line in raw.splitlines():
            if line.startswith('E:') and '=' in line:
                name, value = line[2:].split('=', 1)
                if name in fields:
                    raise FileAccessError('write_backend_unverified')
                fields[name] = value
        uuid = fields.get('ID_FS_UUID')
        # udev's on-disk database may omit the kernel-generated DEVNAME. Bind
        # its numeric key to the current sysfs partition uevent instead.
        partition = after[1].removeprefix('/dev/')
        uevent = _read_fixed(root, 'sys/class/block/' + partition + '/uevent', 4096)
        kernel = {}
        for line in uevent.splitlines():
            if '=' in line:
                name, value = line.split('=', 1)
                if name in kernel:
                    raise FileAccessError('write_backend_unverified')
                kernel[name] = value
        if (fields.get('DEVNAME', after[1]) != after[1] or fields.get('ID_FS_TYPE') != after[0]
                or kernel.get('DEVNAME') != partition
                or kernel.get('MAJOR', '') + ':' + kernel.get('MINOR', '') != before[2]
                or type(uuid) is not str or re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', uuid) is None
                or uuid in seen):
            raise FileAccessError('write_backend_unverified')
        seen.add(uuid)
        values[before[4]] = hashlib.md5((uuid + '\n').encode('ascii'), usedforsecurity=False).hexdigest()
    value = values.get(row['mount'][:-6])
    if value is None or _internal_sata(root, row) is not True:
        raise FileAccessError('write_backend_unverified')
    return value


def _schema_authorizer(action, table, column, database, caller):
    if caller is not None:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        return (sqlite3.SQLITE_OK if database == 'main' and table in ('sqlite_master', 'sqlite_schema')
                and column in ('type', 'name', 'tbl_name', 'sql') else sqlite3.SQLITE_DENY)
    if action == sqlite3.SQLITE_PRAGMA:
        return (sqlite3.SQLITE_OK if (table, column) in
                (('schema_version', None), ('table_xinfo', 'tb_recycle'),
                 ('index_list', 'tb_recycle'), ('index_info', 'deltime_index')) else sqlite3.SQLITE_DENY)
    return sqlite3.SQLITE_OK if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_DENY


def _schema(connection):
    rows = connection.execute('SELECT type,name,sql FROM sqlite_master '
                              'WHERE name=? OR tbl_name=? LIMIT 8', ('tb_recycle', 'tb_recycle')).fetchall()
    tables = [row for row in rows if row[:2] == ('table', 'tb_recycle')]
    if (len(rows) != 2 or len(tables) != 1 or type(tables[0][2]) is not str
            or not re.search(r'\bAUTOINCREMENT\b', tables[0][2], re.I)
            or any(row[0] not in ('table', 'index') for row in rows)):
        raise FileAccessError('write_backend_unverified')
    expected = ('CREATETABLETB_RECYCLE(' + ','.join(name + kind +
        ('PRIMARYKEYAUTOINCREMENT' if i == 0 else '') for i, (name, kind)
        in enumerate(zip(_COLUMNS, _TYPES))) + ')').upper()
    actual = re.sub(r'\s+', '', tables[0][2].replace('"', '')).upper()
    if actual.replace('CREATETABLEIFNOTEXISTS', 'CREATETABLE', 1) != expected:
        raise FileAccessError('write_backend_unverified')
    columns = connection.execute('PRAGMA table_xinfo(tb_recycle)').fetchall()
    if (len(columns) != 17 or any(col[0] != i or col[1] != _COLUMNS[i]
            or col[2].upper() != _TYPES[i] or col[3] != 0 or col[4] is not None
            or col[5] != (1 if i == 0 else 0) or col[6] != 0 for i, col in enumerate(columns))):
        raise FileAccessError('write_backend_unverified')
    indexes = connection.execute('PRAGMA index_list(tb_recycle)').fetchall()
    if (len(indexes) != 1 or indexes[0][1:] != ('deltime_index', 0, 'c', 0)
            or connection.execute('PRAGMA index_info(deltime_index)').fetchall() != [(0, 5, 'deltime')]):
        raise FileAccessError('write_backend_unverified')


def _data_authorizer(write=False, deleting=False):
    def check(action, table, column, database, caller):
        if caller is not None:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            allowed = database == 'main' and table == 'tb_recycle' and column in _FIELDS
        elif action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE):
            allowed = (database == 'main' and table == 'tb_recycle'
                       and (deleting if action == sqlite3.SQLITE_DELETE else write))
        else:
            allowed = action == sqlite3.SQLITE_SELECT
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
    return check


def _record_identity(connection, record, *, expected_id=None, absent=False):
    """Only this one native archive row; id and reserved NULLs are proof too."""
    rows = connection.execute(_SELECT_IDENTITY, (record[6], OWNER_ID)).fetchall()
    if not rows and absent:
        return None
    if (len(rows) != 1 or rows[0][:10] != record or type(rows[0][10]) is not int
            or rows[0][10] <= 0 or rows[0][11:] != (None,) * 6
            or expected_id is not None and rows[0][10] != expected_id):
        raise FileAccessError('result_unconfirmed')
    return rows[0][10]


class _OpenedRecycle:
    """Trusted no-link exact-mount factory archive and existing SQLite database."""

    def __init__(self, root, row, current, deadline):
        from api_file_executor import _Handle
        self.root, self.row, self.current, self.deadline = root, row, current, deadline
        self.archive, self.users, self.fds = None, None, []
        try:
            self.archive = _Handle(root, row, _ARCHIVE, directory=True)
            self.users = _Handle(root, row, '', directory=True)
            self.mount_id = self.archive.mount_id
            if self.users.mount_id != self.mount_id:
                raise FileAccessError('root_unavailable')
            # Check every actual ancestor, not only the final directory.
            for descriptor in self.archive.descriptors:
                # An O_PATH handle is an identity anchor, not an xattr fd.
                checked = os.open('/proc/self/fd/' + str(descriptor),
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    if _stable(os.fstat(checked)) != _stable(os.fstat(descriptor)):
                        raise FileAccessError('file_changed')
                    _trusted(checked)
                finally:
                    os.close(checked)
            self.archive_state = _stable(os.fstat(self.archive.fd))
            parent = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=self.users.fd)
            self.fds.append(parent)
            self.chain = []
            for component in _CACHE:
                _trusted(parent)
                parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=parent)
                self.fds.append(parent)
                _trusted(parent)
                self._mount(parent)
                self.chain.append(_stable(os.fstat(parent)))
            self.directory = parent
            self.main = os.open(_DB, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                dir_fd=self.directory)
            self.fds.append(self.main)
            self.main_state = self._ordinary(self.main)
            header = os.pread(self.main, 100, 0)
            # This fixed writer does not create/migrate schema or perform WAL
            # recovery. Rollback-mode native DBs are the audited write subset.
            if len(header) != 100 or header[:16] != b'SQLite format 3\0' or header[18:20] != b'\x01\x01':
                raise FileAccessError('write_backend_unverified')
            self.check()
        except Exception:
            self.close()
            raise

    def _mount(self, descriptor):
        if _descriptor_mount_id(self.root, descriptor) != self.mount_id:
            raise FileAccessError('root_unavailable')

    def _ordinary(self, descriptor):
        info = os.fstat(descriptor)
        self._mount(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise FileAccessError('write_backend_unverified')
        for name in ('system.posix_acl_access', 'system.posix_acl_default'):
            import errno
            try:
                os.getxattr(descriptor, name)
            except OSError as error:
                if error.errno not in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
                    raise FileAccessError('write_backend_unverified') from None
            else:
                raise FileAccessError('write_backend_unverified')
        return _stable(info)

    def check(self, *, journal=False):
        _gate(self.current, self.deadline)
        from api_file_executor import _Handle
        with _Handle(self.root, self.row, _ARCHIVE, directory=True) as archive:
            if archive.mount_id != self.mount_id or _stable(os.fstat(archive.fd)) != self.archive_state:
                raise FileAccessError('file_changed')
        with _Handle(self.root, self.row, '', directory=True) as users:
            descriptors = []
            try:
                parent = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=users.fd)
                descriptors.append(parent)
                _trusted(parent)
                self._mount(parent)
                for component, expected in zip(_CACHE, self.chain):
                    parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     dir_fd=parent)
                    descriptors.append(parent)
                    _trusted(parent)
                    self._mount(parent)
                    if _stable(os.fstat(parent)) != expected:
                        raise FileAccessError('write_backend_unverified')
                if _stable(os.fstat(parent)) != _stable(os.fstat(self.directory)):
                    raise FileAccessError('write_backend_unverified')
            finally:
                for fd in reversed(descriptors):
                    os.close(fd)
        if _stable(os.stat(_DB, dir_fd=self.directory, follow_symlinks=False)) != self.main_state:
            raise FileAccessError('write_backend_unverified')
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                fd = os.open(_DB + suffix, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=self.directory)
            except FileNotFoundError:
                continue
            try:
                if suffix != '-journal' or not journal:
                    raise FileAccessError('write_backend_unverified')
                self._ordinary(fd)
            finally:
                os.close(fd)
        _gate(self.current, self.deadline)

    def sqlite_proof(self, before, *, write, journal=False):
        """Continue proving actual SQLite main/journal descriptors, not paths."""
        self.check(journal=journal)
        expected = {self.main_state: os.O_RDWR if write else os.O_RDONLY}
        if journal:
            try:
                info = os.stat(_DB + '-journal', dir_fd=self.directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                expected[_stable(info)] = os.O_RDWR
        seen = set()
        for fd, identity in _fd_snapshot().items():
            if before.get(fd) == identity:
                continue
            if stat.S_ISREG(identity[2]) and identity not in expected:
                raise FileAccessError('write_backend_unverified')
            if identity in expected:
                flags = _fd_flags(fd)
                if (flags is None or flags & os.O_ACCMODE != expected[identity]
                        or flags & os.O_APPEND):
                    raise FileAccessError('write_backend_unverified')
                self._mount(fd)
                seen.add(identity)
        if not set(expected) <= seen:
            raise FileAccessError('write_backend_unverified')
        self.check(journal=journal)

    @contextmanager
    def connection(self, *, write=False):
        self.check()
        before = _fd_snapshot()
        connection = None
        try:
            uri = ('file:' + quote('/proc/self/fd/' + str(self.directory) + '/' + _DB, safe='/')
                   + ('?mode=rw&vfs=unix' if write else '?mode=ro&vfs=unix'))
            connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=.05)
            self.sqlite_proof(before, write=write)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)
            connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA temp_store=MEMORY')
            connection.execute('PRAGMA mmap_size=0')
            connection.execute('PRAGMA synchronous=FULL') if write else connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            steps = 0
            def progress():
                nonlocal steps
                steps += 100
                try:
                    _gate(self.current, self.deadline)
                    return int(steps > 100000)
                except FileAccessError:
                    return 1
            connection.set_progress_handler(progress, 100)
            connection.set_authorizer(_schema_authorizer)
            connection.execute('PRAGMA schema_version').fetchall()
            _schema(connection)
            self.sqlite_proof(before, write=write, journal=write)
            connection.set_authorizer(_data_authorizer(write=write, deleting=write))
            yield connection
            self.sqlite_proof(before, write=write, journal=write)
            connection.set_authorizer(None)
            connection.execute('COMMIT')
            self.sqlite_proof(before, write=write)
        finally:
            if connection is not None:
                connection.close()

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []
        for opened in (self.archive, self.users):
            if opened is not None:
                opened.close()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


@dataclass(frozen=True, repr=False)
class _Plan:
    instance: object
    root_id: str
    path: str
    native: tuple
    uuid: str
    database: tuple
    archive: tuple
    index: object


@dataclass(frozen=True, repr=False)
class _Receipt:
    plan: _Plan
    record: tuple
    content: tuple
    record_id: int


class NativeFileRecycle:
    """Only private capture/archive/verify and owner recovery are exposed."""

    def __init__(self, root='/', *, reader=None):
        self.root = root
        self._reader = reader or NativeFileIndexReader(root)
        self._instance = object()

    @_fixed_errors('write_backend_unverified')
    def preflight(self, row, path, *, current):
        _arguments(row, path)
        deadline = time.monotonic() + _TIMEOUT
        _gate(current, deadline)
        with _LOCK:
            native = _listener_signature(self.root, deadline)
            uuid = _native_uuid(self.root, row)
            index = self._reader.preflight(row, path, current=current, require_absent=False)
            if index.absent:
                raise FileAccessError('write_backend_unverified')
            with _OpenedRecycle(self.root, row, current, deadline) as opened:
                with opened.connection():
                    pass
                _gate(current, deadline)
                return _Plan(self._instance, row['id'], path, native, uuid,
                             opened.main_state, opened.archive_state, index)

    def _bound(self, plan, row, path, current, deadline):
        _arguments(row, path)
        _gate(current, deadline)
        if (type(plan) is not _Plan or plan.instance is not self._instance
                or plan.root_id != row.get('id') or plan.path != path
                or _listener_signature(self.root, deadline) != plan.native
                or _native_uuid(self.root, row) != plan.uuid):
            raise FileAccessError('write_backend_unverified')

    @staticmethod
    def _opened_bound(opened, plan):
        if opened.main_state != plan.database or opened.archive_state != plan.archive:
            raise FileAccessError('write_backend_unverified')

    def _notify(self, plan, row, path, current, deadline):
        _mount, _absolute, share, encoded = _paths(row, path)
        connection = None
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(max(.001, deadline - time.monotonic()))
            connection.connect(str(Path(self.root).absolute() / _SOCKET.lstrip('/')))
            peer = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer[0] != plan.native[0][0] or peer[1] != 0:
                raise FileAccessError('native_unavailable')
            self._bound(plan, row, path, current, deadline)
            connection.sendall(_RECORD.pack(b'admin', share, 0x2a, encoded, b''))
            self._bound(plan, row, path, current, deadline)
        except FileAccessError:
            raise
        except Exception:
            raise FileAccessError('result_unconfirmed') from None
        finally:
            if connection is not None:
                connection.close()

    @_fixed_errors('result_unconfirmed')
    def archive_captured(self, plan, row, path, descriptor, stage, *, current):
        """Same-inode no-clobber archive plus one fixed native record insert."""
        from api_file_mutations import _rename_new
        deadline = time.monotonic() + _TIMEOUT
        self._bound(plan, row, path, current, deadline)
        with _LOCK:
            stage.verify(current)
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or _content(os.stat('source', dir_fd=stage.fd, follow_symlinks=False)) != _content(before)):
                raise FileAccessError('file_changed')
            deltime = int(time.time())
            source = '/Users/' + path
            digest = hashlib.md5((source + str(deltime)).encode('utf8'), usedforsecurity=False).hexdigest()
            extension = os.path.splitext(path.rsplit('/', 1)[-1])[1]
            name = digest + extension
            record = (plan.uuid, source, path.rsplit('/', 1)[-1], 0, deltime, OWNER_ID, name,
                      before.st_size, before.st_mtime_ns // 10**9, before.st_ctime_ns // 10**9)
            with _OpenedRecycle(self.root, row, current, deadline) as opened:
                self._opened_bound(opened, plan)
                if _descriptor_mount_id(self.root, descriptor) != opened.mount_id:
                    raise FileAccessError('root_unavailable')
                manifest = _read_at(stage.fd, 'manifest.json')
                if (type(manifest) is not dict or type(manifest.get('source_sha256')) is not str
                        or re.fullmatch(r'[0-9a-f]{64}', manifest['source_sha256']) is None):
                    raise FileAccessError('write_backend_unverified')
                # The durable private locator is written before *any* move or
                # DB transaction. It permits owner recovery even after a crash.
                manifest.update(state='native_recycle_intent', native_recycle={
                    'version': 1, 'record': list(record), 'identity': list(_content(before)),
                    'root_id': row['id'], 'source_path': path, 'record_id': None,
                    'sha256': manifest['source_sha256']})
                stage.record_recovery(manifest)
                with opened.connection(write=True) as connection:
                    if connection.execute(_SELECT, (name, OWNER_ID)).fetchall():
                        raise FileAccessError('target_exists')
                    stage.verify(current)
                    if _content(os.stat('source', dir_fd=stage.fd, follow_symlinks=False)) != _content(before):
                        raise FileAccessError('file_changed')
                    self._bound(plan, row, path, current, deadline)
                    _rename_new(stage.fd, 'source', opened.archive.fd, name)
                    os.fsync(stage.fd)
                    os.fsync(opened.archive.fd)
                    manifest['state'] = 'native_recycle_archived'
                    stage.record_recovery(manifest)
                    if (_content(os.stat(name, dir_fd=opened.archive.fd, follow_symlinks=False)) != _content(before)
                            or _content(os.fstat(descriptor)) != _content(before)):
                        raise FileAccessError('result_unconfirmed')
                    _gate(current, deadline)
                    connection.execute(_INSERT, record)
                    record_id = _record_identity(connection, record)
                    manifest['native_recycle']['record_id'] = record_id
                    stage.record_recovery(manifest)
                manifest['state'] = 'native_recycle_registered'
                stage.record_recovery(manifest)
                self._notify(plan, row, path, current, deadline)
                return _Receipt(plan, record, _content(before), record_id)

    @_fixed_errors('result_unconfirmed')
    def verify(self, receipt, row, path, descriptor, *, current):
        """Observe exact archive+DB+source-index evidence, never resend a write."""
        if type(receipt) is not _Receipt:
            raise FileAccessError('write_backend_unverified')
        deadline = time.monotonic() + _TIMEOUT
        self._bound(receipt.plan, row, path, current, deadline)
        with _LOCK:
            with _OpenedRecycle(self.root, row, current, deadline) as opened:
                self._opened_bound(opened, receipt.plan)
                name = receipt.record[6]
                if (_content(os.stat(name, dir_fd=opened.archive.fd, follow_symlinks=False)) != receipt.content
                        or _content(os.fstat(descriptor)) != receipt.content):
                    raise FileAccessError('file_changed')
                with opened.connection() as connection:
                    matched = _record_identity(connection, receipt.record,
                        expected_id=receipt.record_id, absent=True) is not None
                from api_file_executor import _Handle
                parent, leaf = path.rsplit('/', 1)
                with _Handle(self.root, row, parent, directory=True) as original:
                    try:
                        os.stat(leaf, dir_fd=original.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        return False
                absent = self._reader._verify_absent(receipt.plan.index, row, path, current=current)
                if (_content(os.stat(name, dir_fd=opened.archive.fd, follow_symlinks=False)) != receipt.content
                        or _content(os.fstat(descriptor)) != receipt.content):
                    raise FileAccessError('file_changed')
                self._bound(receipt.plan, row, path, current, deadline)
                _gate(current, deadline)
                return matched and absent

    def _restore_staged(self, row, manifest, stage, saved, identity, path, current, deadline):
        """A still-staged inode never needs any factory archive/DB availability."""
        from api_file_executor import _Handle
        from api_file_mutations import _rename_new
        stage.verify(current)
        try:
            descriptor = os.open('source', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=stage.fd)
        except FileNotFoundError:
            return None
        try:
            if _content(os.fstat(descriptor)) != identity:
                return None
            _verify_bytes(descriptor, saved.get('sha256'), current, deadline)
            parent, leaf = path.rsplit('/', 1)
            with _Handle(self.root, row, parent, directory=True) as target:
                stage.verify(current)
                if (_content(os.stat('source', dir_fd=stage.fd, follow_symlinks=False)) != identity
                        or _descriptor_mount_id(self.root, descriptor) != target.mount_id):
                    raise FileAccessError('file_changed')
                previous_state = manifest.get('state')
                manifest.update(state='native_recycle_restore_intent', restore_receipt={
                    'recovery_id': stage.operation_id, 'state': 'result_unconfirmed', 'index_verified': False})
                stage.record_recovery(manifest)
                _gate(current, deadline)
                try:
                    _rename_new(stage.fd, 'source', target.fd, leaf)
                except FileAccessError as error:
                    if error.code in ('target_exists', 'not_found'):
                        manifest.pop('restore_receipt', None)
                        manifest['state'] = previous_state
                        stage.record_recovery(manifest)
                    raise
                os.fsync(stage.fd)
                os.fsync(target.fd)
                with _Handle(self.root, row, path) as final:
                    if _content(os.fstat(final.fd)) != identity:
                        raise FileAccessError('result_unconfirmed')
                _verify_bytes(descriptor, saved.get('sha256'), current, deadline)
                return self._restore_complete(row, path, descriptor, manifest, stage, current, deadline)
        finally:
            os.close(descriptor)

    def _restore_complete(self, row, path, descriptor, manifest, stage, current, deadline):
        from api_file_native_index import NativeFileIndex
        accepted = False
        try:
            bridge = NativeFileIndex(self.root)
            receipt = bridge.notify_new(row, path, descriptor, directory=False, current=current)
            accepted = bridge.verify_new(receipt, row, path, descriptor, directory=False, current=current)
        except Exception:
            pass
        result = {'recovery_id': stage.operation_id, 'state': 'restored', 'index_verified': accepted is True}
        manifest.update(state='restored', restore_receipt=result)
        stage.record_recovery(manifest)
        _gate(current, deadline)
        return {'ok': True, 'recovery': result, 'may_wake_disks': True}

    @_fixed_errors('result_unconfirmed')
    def restore_manifest(self, row, manifest, stage, *, current):
        """Owner-only exact original-path recovery; native UI recovery also works."""
        from api_file_executor import _Handle
        from api_file_mutations import _rename_new
        # Complete recovery hashes are bounded like other bulk file operations,
        # not by the short metadata/notification-only window.
        deadline = time.monotonic() + 1800
        _gate(current, deadline)
        if type(manifest.get('restore_receipt')) is dict:
            return {'ok': True, 'recovery': dict(manifest['restore_receipt']), 'may_wake_disks': True}
        saved = manifest.get('native_recycle')
        if (type(saved) is not dict or saved.get('version') != 1 or saved.get('root_id') != row.get('id')
                or type(saved.get('record')) is not list or len(saved['record']) != 10
                or type(saved.get('identity')) is not list or len(saved['identity']) != 6):
            raise FileAccessError('write_backend_unverified')
        record, identity, path = tuple(saved['record']), tuple(saved['identity']), saved.get('source_path')
        record_id = saved.get('record_id')
        _arguments(row, path)
        if (type(record[0]) is not str or re.fullmatch(r'[0-9a-f]{32}', record[0]) is None
                or record[1] != '/Users/' + path
                or record[2] != path.rsplit('/', 1)[-1] or record[3] != 0 or record[5] != OWNER_ID
                or record_id is not None and (type(record_id) is not int or record_id <= 0)
                or type(record[6]) is not str or len(record[6].encode('utf8')) > 255
                or re.fullmatch(r'[0-9a-f]{32}(?:\.[^/\\]*)?', record[6]) is None):
            raise FileAccessError('write_backend_unverified')
        staged = self._restore_staged(row, manifest, stage, saved, identity, path, current, deadline)
        if staged is not None:
            return staged
        if record[0] != _native_uuid(self.root, row):
            raise FileAccessError('write_backend_unverified')
        with _LOCK, _OpenedRecycle(self.root, row, current, deadline) as opened:
            stage.verify(current)
            parent, leaf = path.rsplit('/', 1)
            with _Handle(self.root, row, parent, directory=True) as target:
                source_parent, source_name = opened.archive.fd, record[6]
                descriptor = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                     dir_fd=source_parent)
                try:
                    if _content(os.fstat(descriptor)) != identity:
                        raise FileAccessError('file_changed')
                    _verify_bytes(descriptor, saved.get('sha256'), current, deadline)
                    with opened.connection(write=True) as connection:
                        observed_id = _record_identity(connection, record, expected_id=record_id, absent=True)
                        if observed_id is not None and record_id is None:
                            # Registration uncertainty cannot adopt a row
                            # merely because its other fields happen to match.
                            raise FileAccessError('result_unconfirmed')
                        previous_state = manifest.get('state')
                        manifest.update(state='native_recycle_restore_intent', restore_receipt={
                            'recovery_id': stage.operation_id, 'state': 'result_unconfirmed',
                            'index_verified': False})
                        stage.record_recovery(manifest)
                        _gate(current, deadline)
                        if _content(os.stat(source_name, dir_fd=source_parent, follow_symlinks=False)) != identity:
                            raise FileAccessError('file_changed')
                        try:
                            _rename_new(source_parent, source_name, target.fd, leaf)
                        except FileAccessError as error:
                            # These kernel refusals prove this rename made no
                            # change. Every post-rename failure keeps the
                            # durable unknown receipt and is never replayed.
                            if error.code in ('target_exists', 'not_found'):
                                manifest.pop('restore_receipt', None)
                                manifest['state'] = previous_state
                                stage.record_recovery(manifest)
                            raise
                        os.fsync(source_parent)
                        os.fsync(target.fd)
                        with _Handle(self.root, row, path) as final:
                            if _content(os.fstat(final.fd)) != identity:
                                raise FileAccessError('result_unconfirmed')
                        _verify_bytes(descriptor, saved.get('sha256'), current, deadline)
                        if observed_id is not None:
                            if connection.execute(_DELETE, record + (observed_id,)).rowcount != 1:
                                raise FileAccessError('result_unconfirmed')
                    return self._restore_complete(row, path, descriptor, manifest, stage, current, deadline)
                finally:
                    os.close(descriptor)

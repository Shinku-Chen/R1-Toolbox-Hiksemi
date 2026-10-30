"""Private, exact-path observation of the audited owner's factory file index.

Build 260907 uses one FILE_DB_V102 below each volume's fixed admin cache. Its
tb_file_db_all rows contain mount-relative path, type, size and second-resolution
stat times, not inode numbers or hashes. A match is index visibility evidence;
the caller must separately retain and verify the submitted file descriptor.

Existing ordinary SQLite files are prechecked. The main DB and SHM must have
actual read-only SQLite descriptors. SQLite itself opens WAL as read/write,
even for mode=ro: readonly_shm makes its WAL transactions and checkpointing
read-only. A rotation race may let that internal open create an empty WAL;
changed identity is rejected, never accepted as index proof. Root may also
fchown existing SHM. This is an explicit write-task observation, not a promise
of zero filesystem-metadata activity; no SQL writes or journal recovery run.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from urllib.parse import quote

from api_file_operations import FileAccessError, _descriptor_mount_id
from api_file_provider import audited_owner_write_rule
from api_file_roots import is_users_root_path


_TABLE = 'tb_file_db_all'
_INDEX = 'sqlite_autoindex_tb_file_db_all_1'
_DB = 'FILE_DB_V102'
_CACHE = ('.DATA_CACHE', 'Users', 'admin', 'file_db')
_COLUMNS = ('mkey', 'path', 'name', 'ctime', 'mtime', 'atime', 'isdir', 'size',
            'deep', 'sname', 'disk_mnt', 'RESERVE1', 'RESERVE2', 'RESERVE3',
            'RESERVE4', 'RESERVE5', 'RESERVE6')
_TYPES = ('INTEGER', 'VARCHAR(4096)', 'VARCHAR(256)', 'INT64', 'INT64', 'INT64',
          'INT64', 'INT64', 'INT64') + ('VARCHAR(4096)',) * 8
_FIELDS = ('path', 'ctime', 'mtime', 'isdir', 'size', 'disk_mnt', 'mkey')
_SELECT = ('SELECT path,ctime,mtime,isdir,size,disk_mnt FROM tb_file_db_all '
           'INDEXED BY sqlite_autoindex_tb_file_db_all_1 WHERE path=? LIMIT 2')
_SELECT_VERSION = ('SELECT path,ctime,mtime,isdir,size,disk_mnt,mkey FROM tb_file_db_all '
                   'INDEXED BY sqlite_autoindex_tb_file_db_all_1 WHERE path=? LIMIT 2')
_LOCK = threading.Lock()
_TIMEOUT = 1.0
_MAX_STEPS = 10000


def _stable(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_nlink


def _file_state(info):
    return _stable(info) + (info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _gate(current, deadline):
    try:
        valid = callable(current) and current() is True
    except Exception:
        valid = False
    if not valid:
        raise FileAccessError('invalid_key')
    if time.monotonic() >= deadline:
        raise FileAccessError('timeout')


def _arguments(row, path):
    if (type(row) is not dict or not is_users_root_path(row.get('mount'))
            or not audited_owner_write_rule(path)
            or '|' in path
            or any(part.startswith('.') for part in path.split('/'))
            or '.ecloud.download' in path):
        raise FileAccessError('write_backend_unverified')
    # The factory INSERT at 0x56aa53 has a 0x800-byte path buffer. Reject any
    # value that could be truncated rather than query a different indexed key.
    key = '/Users/' + path
    if len(key.encode('utf-8', 'strict')) >= 0x800:
        raise FileAccessError('write_backend_unverified')
    return row['mount'][:-6], key


def _fd_snapshot():
    values = {}
    for name in os.listdir('/proc/self/fd'):
        if name.isdecimal():
            try:
                values[int(name)] = _stable(os.fstat(int(name)))
            except OSError:
                pass
    return values


def _fd_flags(descriptor):
    try:
        raw = Path('/proc/self/fdinfo/' + str(descriptor)).read_text(encoding='ascii')
        matches = re.findall(r'^flags:\s*([0-7]+)\s*$', raw, re.MULTILINE)
        return int(matches[0], 8) if len(matches) == 1 else None
    except (OSError, ValueError):
        return None


def _readonly_fd(descriptor):
    flags = _fd_flags(descriptor)
    return flags is not None and flags & os.O_ACCMODE == os.O_RDONLY


def _wal_fd_allowed(descriptor):
    flags = _fd_flags(descriptor)
    return (flags is not None and not flags & os.O_APPEND
            and flags & os.O_ACCMODE in (os.O_RDONLY, os.O_RDWR))


@dataclass(frozen=True)
class _Receipt:
    reader: str
    mount: str
    key: str
    root_id: object
    mount_id: str
    chain: tuple
    database: tuple
    absent: bool
    generation: object = None


@dataclass(frozen=True, repr=False)
class _MutationReceipt:
    operation: str
    source: _Receipt
    target: _Receipt


class _OpenedIndex:
    """Same-volume no-link ancestry and owned DB/WAL/SHM descriptors."""

    def __init__(self, root, row, path, current, deadline):
        self.root, self.row, self.path = root, row, path
        self.current, self.deadline = current, deadline
        self.descriptors, self.components = [], []
        self.parent = None
        self.directory = None
        self.files = {}
        self.mount_id = None
        try:
            if os.name != 'posix' or not hasattr(os, 'O_PATH') or not hasattr(os, 'pread'):
                raise FileAccessError('write_backend_unverified')
            from api_file_executor import _Handle
            self.parent = _Handle(root, row, path.rpartition('/')[0], directory=True)
            self.mount_id = self.parent.mount_id
            self.parent_state = _stable(os.fstat(self.parent.fd))
            with _Handle(root, row, '', directory=True) as users:
                self.users_state = _stable(os.fstat(users.fd))
                flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                # '..' is a fixed operation on the verified Users directory,
                # never a request component. The current mount must be shared.
                parent = os.open('..', flags, dir_fd=users.fd)
                self.descriptors.append(parent)
                self._check_mount(parent)
                self.components.append(('..', _stable(os.fstat(parent))))
                for component in _CACHE:
                    parent = os.open(component, flags, dir_fd=parent)
                    self.descriptors.append(parent)
                    self._check_mount(parent)
                    self.components.append((component, _stable(os.fstat(parent))))
                self.directory = parent
                for suffix in ('', '-wal', '-shm', '-journal'):
                    try:
                        opened = os.open(_DB + suffix, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                                         dir_fd=self.directory)
                    except FileNotFoundError:
                        if not suffix:
                            raise FileAccessError('write_backend_unverified') from None
                        self.files[suffix] = None
                        continue
                    self.descriptors.append(opened)
                    self._check_mount(opened, regular=True)
                    if suffix == '-journal':
                        # Read-only SQLite must not attempt hot-journal recovery.
                        raise FileAccessError('write_backend_unverified')
                    readable = os.open('/proc/self/fd/' + str(opened),
                        os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                    self.descriptors.append(readable)
                    self._check_mount(readable, regular=True)
                    if _stable(os.fstat(opened)) != _stable(os.fstat(readable)):
                        raise FileAccessError('write_backend_unverified')
                    self.files[suffix] = readable
            info = os.fstat(self.files[''])
            header = os.pread(self.files[''], 100, 0)
            if (info.st_size < 100 or len(header) != 100 or header[:16] != b'SQLite format 3\x00'
                    or header[18] not in (1, 2) or header[19] != header[18]):
                raise FileAccessError('write_backend_unverified')
            self.wal = header[18] == 2
            if self.wal:
                if self.files['-wal'] is None or self.files['-shm'] is None:
                    raise FileAccessError('write_backend_unverified')
                wal = os.pread(self.files['-wal'], 32, 0)
                if (len(wal) < 32 or wal[:4] not in (b'\x37\x7f\x06\x82', b'\x37\x7f\x06\x83')
                        or os.fstat(self.files['-shm']).st_size < 32768):
                    raise FileAccessError('write_backend_unverified')
            elif self.files['-wal'] is not None or self.files['-shm'] is not None:
                raise FileAccessError('write_backend_unverified')
            self.file_states = {s: _stable(os.fstat(fd)) if fd is not None else None
                                for s, fd in self.files.items()}
            self.check()
        except Exception:
            self.close()
            raise

    def _check_mount(self, descriptor, *, regular=False):
        info = os.fstat(descriptor)
        wrong_type = (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1) if regular else (
            not stat.S_ISDIR(info.st_mode))
        if (_descriptor_mount_id(self.root, descriptor) != self.mount_id
                or info.st_dev != self.parent_state[0]
                or wrong_type):
            raise FileAccessError('write_backend_unverified')

    def check(self):
        _gate(self.current, self.deadline)
        from api_file_executor import _Handle
        flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        with _Handle(self.root, self.row, self.path.rpartition('/')[0], directory=True) as parent:
            if (_stable(os.fstat(parent.fd)) != self.parent_state or parent.mount_id != self.mount_id):
                raise FileAccessError('file_changed')
        with _Handle(self.root, self.row, '', directory=True) as users:
            if _stable(os.fstat(users.fd)) != self.users_state or users.mount_id != self.mount_id:
                raise FileAccessError('root_unavailable')
            descriptors = []
            try:
                parent = users.fd
                for component, identity in self.components:
                    parent = os.open(component, flags, dir_fd=parent)
                    descriptors.append(parent)
                    self._check_mount(parent)
                    if _stable(os.fstat(parent)) != identity:
                        raise FileAccessError('write_backend_unverified')
                for suffix, identity in self.file_states.items():
                    try:
                        info = os.stat(_DB + suffix, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        if identity is not None:
                            raise FileAccessError('write_backend_unverified') from None
                        continue
                    if identity is None or _stable(info) != identity:
                        raise FileAccessError('write_backend_unverified')
                if _stable(os.fstat(self.files[''])) != self.file_states['']:
                    raise FileAccessError('write_backend_unverified')
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)
        _gate(self.current, self.deadline)

    def sqlite_proof(self, before, *, sidecars=False):
        """Check actual newly opened SQLite descriptors before any row query."""
        self.check()
        after = _fd_snapshot()
        expected = {identity for suffix, identity in self.file_states.items()
                    if identity is not None and (sidecars or suffix == '')}
        admitted = {identity for identity in self.file_states.values() if identity is not None}
        wal = self.file_states.get('-wal') if sidecars and getattr(self, 'wal', False) else None
        seen = set()
        for descriptor, identity in after.items():
            changed = before.get(descriptor) != identity
            # Python's SQLite API does not export its underlying Unix handle.
            # Fail closed on any concurrent newly opened unrelated ordinary
            # file rather than mistake another thread's matching DB fd for
            # this connection while its real fd points at a replacement.
            if changed and stat.S_ISREG(identity[2]) and identity not in admitted:
                raise FileAccessError('write_backend_unverified')
            if changed and identity in admitted:
                # SQLite wal.c opens WAL with READWRITE|CREATE regardless of
                # mode=ro. Only this exact existing inode may use that mode;
                # main and SHM still require actual O_RDONLY. WAL_SHM_RDONLY
                # blocks write transactions, recovery and checkpoint/delete.
                if not (_wal_fd_allowed(descriptor) if wal is not None and identity == wal
                        else _readonly_fd(descriptor)):
                    raise FileAccessError('write_backend_unverified')
                if _descriptor_mount_id(self.root, descriptor) != self.mount_id:
                    raise FileAccessError('write_backend_unverified')
                seen.add(identity)
        if not expected <= seen:
            raise FileAccessError('write_backend_unverified')
        self.check()

    def close(self):
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)
        self.descriptors = []
        if self.parent is not None:
            self.parent.close()
            self.parent = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def _schema_authorizer(action, table, column, database, caller):
    if caller is not None:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        allowed = (database == 'main' and table in ('sqlite_master', 'sqlite_schema')
                   and column in ('type', 'name', 'tbl_name', 'sql'))
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        allowed = (table, column) in (('schema_version', None), ('table_info', _TABLE),
                                      ('table_xinfo', _TABLE), ('index_list', _TABLE),
                                      ('index_info', _INDEX))
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
    return (sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION)
            else sqlite3.SQLITE_DENY)


def _data_authorizer(action, table, column, database, caller):
    if caller is not None:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        allowed = database == 'main' and table == _TABLE and column in _FIELDS
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
    return (sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION)
            else sqlite3.SQLITE_DENY)


def _schema(connection):
    rows = connection.execute('SELECT type,name,sql FROM sqlite_master '
                              'WHERE name=? OR tbl_name=? LIMIT 8', (_TABLE, _TABLE)).fetchall()
    tables = [row for row in rows if row[:2] == ('table', _TABLE)]
    if (len(rows) > 7 or len(tables) != 1 or not isinstance(tables[0][2], str)
            or not re.match(r'CREATE\s+TABLE\s+', tables[0][2], re.IGNORECASE)
            or any(row[0] not in ('table', 'index') for row in rows)):
        raise FileAccessError('write_backend_unverified')
    columns = connection.execute('PRAGMA table_xinfo(tb_file_db_all)').fetchall()
    if (len(columns) != len(_COLUMNS)
            or any(row[0] != i or row[1] != _COLUMNS[i] or row[2].upper() != _TYPES[i]
                   or row[4] is not None or row[5] != (1 if i == 0 else 0)
                   or row[3] != (1 if i == 1 else 0) or row[6] != 0
                   for i, row in enumerate(columns))):
        raise FileAccessError('write_backend_unverified')
    indexes = connection.execute('PRAGMA index_list(tb_file_db_all)').fetchall()
    native = [row for row in indexes if row[1] == _INDEX]
    if (len(native) != 1 or native[0][2:] != (1, 'u', 0)
            or connection.execute('PRAGMA index_info(sqlite_autoindex_tb_file_db_all_1)').fetchall()
            != [(0, 1, 'path')]):
        raise FileAccessError('write_backend_unverified')


def _query(opened, key, *, versioned=False):
    """One fixed exact-index lookup; never use search, attach, functions or raw SQL."""
    connection = None
    before = _fd_snapshot()
    try:
        uri = ('file:' + quote('/proc/self/fd/' + str(opened.directory) + '/' + _DB, safe='/')
               + '?mode=ro&vfs=unix&readonly_shm=1')
        connection = sqlite3.connect(uri, uri=True, isolation_level=None,
                                     timeout=min(.05, max(0, opened.deadline - time.monotonic())))
        opened.sqlite_proof(before)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('PRAGMA temp_store=MEMORY')
        connection.execute('PRAGMA mmap_size=0')
        steps = 0

        def progress():
            nonlocal steps
            steps += 100
            try:
                _gate(opened.current, opened.deadline)
                return int(steps > _MAX_STEPS)
            except FileAccessError:
                return 1

        connection.set_progress_handler(progress, 100)
        connection.set_authorizer(_schema_authorizer)
        connection.execute('BEGIN')
        connection.execute('PRAGMA schema_version').fetchall()
        # Opening the schema primes WAL/SHM without querying user rows. Refuse
        # a sidecar swap before the exact permitted row lookup is prepared.
        opened.sqlite_proof(before, sidecars=opened.wal)
        _schema(connection)
        _gate(opened.current, opened.deadline)
        connection.set_authorizer(_data_authorizer)
        rows = connection.execute(_SELECT_VERSION if versioned else _SELECT, (key,)).fetchall()
        if (len(rows) > 1 or any(type(row) is not tuple or len(row) != (7 if versioned else 6)
                or type(row[0]) is not str or type(row[5]) is not str
                or any(type(value) is not int for value in row[1:5])
                or row[3] not in (0, 1) or row[4] < 0
                or versioned and (type(row[6]) is not int or row[6] <= 0) for row in rows)):
            raise FileAccessError('write_backend_unverified')
        opened.sqlite_proof(before, sidecars=opened.wal)
        return rows
    finally:
        if connection is not None:
            connection.close()


class NativeFileIndexReader:
    """Private helper for NativeFileIndex; it grants no file or machine access."""

    def __init__(self, root='/'):
        self.root = root
        self._identity = secrets.token_hex(16)

    def preflight(self, row, path, *, current, require_absent=True):
        if type(require_absent) is not bool:
            raise FileAccessError('write_backend_unverified')
        mount, key = _arguments(row, path)
        deadline = time.monotonic() + _TIMEOUT
        _gate(current, deadline)
        if not _LOCK.acquire(blocking=False):
            raise FileAccessError('resource_busy')
        try:
            with _OpenedIndex(self.root, row, path, current, deadline) as opened:
                rows = _query(opened, key, versioned=True)
                if require_absent and rows:
                    raise FileAccessError('write_backend_unverified')
                opened.check()
                return _Receipt(self._identity, mount, key, row.get('id'), opened.mount_id,
                                tuple(opened.components), opened.file_states[''], not rows,
                                rows[0][6] if rows else None)
        except FileAccessError:
            raise
        except Exception:
            _gate(current, deadline)
            raise FileAccessError('write_backend_unverified') from None
        finally:
            _LOCK.release()

    def verify_new(self, receipt, row, path, descriptor, *, directory, current,
                   after_generation=None):
        mount, key = _arguments(row, path)
        if (type(receipt) is not _Receipt or receipt.reader != self._identity
                or receipt.mount != mount or receipt.key != key or receipt.root_id != row.get('id')
                or type(directory) is not bool):
            raise FileAccessError('write_backend_unverified')
        deadline = time.monotonic() + _TIMEOUT
        _gate(current, deadline)
        if not _LOCK.acquire(blocking=False):
            raise FileAccessError('resource_busy')
        try:
            from api_file_executor import _Handle
            before = os.fstat(descriptor)
            if (not (stat.S_ISDIR(before.st_mode) if directory else stat.S_ISREG(before.st_mode))
                    or not directory and before.st_nlink != 1
                    or _descriptor_mount_id(self.root, descriptor) != receipt.mount_id):
                raise FileAccessError('file_changed')
            with _Handle(self.root, row, path, directory=directory) as target:
                if _file_state(os.fstat(target.fd)) != _file_state(before):
                    raise FileAccessError('file_changed')
            with _OpenedIndex(self.root, row, path, current, deadline) as opened:
                if (opened.mount_id != receipt.mount_id or tuple(opened.components) != receipt.chain
                        or opened.file_states[''] != receipt.database):
                    raise FileAccessError('write_backend_unverified')
                rows = _query(opened, key, versioned=True)
                expected = (key, before.st_ctime_ns // 1000000000,
                            before.st_mtime_ns // 1000000000, int(directory),
                            before.st_size, mount)
                # Native INSERT OR REPLACE supplies NULL for AUTOINCREMENT mkey.
                # A replacement must advance that generation even when its
                # second-resolution stat times and length equal the old file.
                matched = (len(rows) == 1 and rows[0][:6] == expected
                           and (after_generation is None or
                                type(after_generation) is int and rows[0][6] > after_generation))
                opened.check()
            with _Handle(self.root, row, path, directory=directory) as target:
                if (_file_state(os.fstat(target.fd)) != _file_state(before)
                        or _file_state(os.fstat(descriptor)) != _file_state(before)):
                    raise FileAccessError('file_changed')
            _gate(current, deadline)
            return matched
        except FileAccessError:
            raise
        except Exception:
            _gate(current, deadline)
            raise FileAccessError('write_backend_unverified') from None
        finally:
            _LOCK.release()

    def preflight_mutation(self, row, source_path, target_path, *, operation, current):
        """Bind two exact keys before publication; never enumerate a directory."""
        if (operation not in ('rename', 'move', 'replace')
                or (source_path == target_path) != (operation == 'replace')):
            raise FileAccessError('invalid_arguments')
        source = self.preflight(row, source_path, current=current, require_absent=False)
        if source.absent or type(source.generation) is not int:
            raise FileAccessError('write_backend_unverified')
        target = (source if operation == 'replace' else
                  self.preflight(row, target_path, current=current, require_absent=True))
        if (source.database != target.database or source.mount_id != target.mount_id
                or source.chain != target.chain):
            raise FileAccessError('write_backend_unverified')
        return _MutationReceipt(operation, source, target)

    def _verify_absent(self, receipt, row, path, *, current):
        mount, key = _arguments(row, path)
        if (type(receipt) is not _Receipt or receipt.reader != self._identity
                or receipt.root_id != row.get('id') or receipt.mount != mount or receipt.key != key):
            raise FileAccessError('write_backend_unverified')
        deadline = time.monotonic() + _TIMEOUT
        _gate(current, deadline)
        if not _LOCK.acquire(blocking=False):
            raise FileAccessError('resource_busy')
        try:
            with _OpenedIndex(self.root, row, path, current, deadline) as opened:
                if (opened.mount_id != receipt.mount_id or tuple(opened.components) != receipt.chain
                        or opened.file_states[''] != receipt.database):
                    raise FileAccessError('write_backend_unverified')
                absent = not _query(opened, key)
                opened.check()
            _gate(current, deadline)
            return absent
        except FileAccessError:
            raise
        except Exception:
            _gate(current, deadline)
            raise FileAccessError('write_backend_unverified') from None
        finally:
            _LOCK.release()

    def verify_mutation(self, receipt, row, source_path, target_path, descriptor,
                        *, operation, current):
        if (type(receipt) is not _MutationReceipt or receipt.operation != operation
                or receipt.source.key != '/Users/' + source_path
                or receipt.target.key != '/Users/' + target_path):
            raise FileAccessError('write_backend_unverified')
        matched = self.verify_new(receipt.target, row, target_path, descriptor,
                                  directory=False, current=current,
                                  after_generation=receipt.source.generation)
        if operation != 'replace':
            matched = self._verify_absent(receipt.source, row, source_path, current=current) and matched
        _gate(current, time.monotonic() + _TIMEOUT)
        return matched

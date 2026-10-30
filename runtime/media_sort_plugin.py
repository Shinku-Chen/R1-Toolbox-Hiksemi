"""Owner-only, explicit media sorting plugin; no discovery or SQL on GET.

The factory database remains the source of truth. Only a verified scraper's
data directory can become an opaque candidate. Rules and recovery snapshots
are never installed automatically, including after a Toolbox restart.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import copy
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import sqlite3
import stat
import threading
import time
from urllib.parse import quote

import media_sort_plugin_rules as rules
from vendor_runtime import _kernel_identity, _read


_SHA = 'de60e6e3dacc793aeaf7459837b72071fc3b202e66ea773cdbd15b06f63cb24a'
_ELF_SIZE = 7913848
_TIMEOUT = 12.0
_MESSAGES = {
    'invalid_arguments': '插件参数无效。',
    'confirmation_required': '应用或撤销需要所有者明确确认。',
    'authorization_expired': '所有者授权已失效，结果未交付。',
    'busy': '影视排序插件正在处理另一项操作。',
    'stale_candidate': '数据库候选或版本已经变化，请重新检测。',
    'unavailable': '原厂进程、数据库身份、权限或结构暂不可确认。',
    'timeout': '影视排序操作超过固定时间窗口。',
    'conflict': '检测到未知规则或旧版本冲突。',
    'backup_failed': '私有一致性备份未完成，未应用规则。',
    'operation_failed': '影视排序操作结果未确认，请重新检测实际状态。',
}
_METADATA_NAMES = ('id', 'name', 'initials', 'path', 'lid', 'poster', 'type', 'vote',
                   'timestamp', 'genres', 'country', 'year', 'resolution', 'dynamic',
                   'ids', 'tag', 'season', 'crew', 'num', 'sname',
                   'backup1', 'backup2', 'backup3', 'backup4')
_METADATA_TYPES = ('INTEGER', 'TEXT', 'TEXT', 'TEXT', 'INTEGER', 'TEXT', 'INTEGER',
                   'REAL', 'INTEGER', 'TEXT', 'TEXT', 'TEXT', 'INTEGER', 'INTEGER',
                   'TEXT', 'TEXT', 'INTEGER', 'TEXT', 'INTEGER', 'TEXT') + ('TEXT',) * 4
_LIBRARY_NAMES = ('lid', 'name', 'language', 'posters', 'sources', 'members',
                  'auto_subtitle', 'tv_available', 'timestamp', 'ctime',
                  'backup1', 'backup2', 'backup3', 'backup4')
_LIBRARY_TYPES = ('INTEGER',) + ('TEXT',) * 5 + ('INTEGER',) * 4 + ('TEXT',) * 4


class MediaSortError(Exception):
    """Only fixed safe codes/messages cross the owner HTTP boundary."""

    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'operation_failed'
        self.message = _MESSAGES[self.code]
        super().__init__(self.message)


def _stable(value):
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode), value.st_uid,
            value.st_gid, stat.S_IMODE(value.st_mode), value.st_nlink)


def _regular(value):
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise MediaSortError('unavailable')
    _owned(value)


def _owned(value, mask=0o022):
    if value.st_uid != 0 or value.st_mode & mask:
        raise MediaSortError('unavailable')


@dataclass(frozen=True)
class _Source:
    relative: str
    processes: tuple


def _process(root, pid, guard, *, expected=None):
    """Bind root-owned audited ELF, PID start and the current namespace/root."""
    guard()
    folder = Path(root).absolute() / 'proc' / str(pid)
    identity = _kernel_identity(folder)
    if identity is None or identity[1] != b'scraper' or identity[2] <= 0:
        raise MediaSortError('unavailable')
    status = _read(folder / 'status', 65536)
    match = re.search(rb'^Uid:\s+([0-9]+)\s+([0-9]+)\s+([0-9]+)\s+([0-9]+)\s*$',
                      status or b'', re.MULTILINE)
    if match is None or any(int(x) != 0 for x in match.groups()):
        raise MediaSortError('unavailable')
    base = Path(root).absolute()
    namespaces = []
    for kind in ('mnt', 'net', 'pid'):
        local = os.stat(base / 'proc/self/ns' / kind)
        remote = os.stat(folder / 'ns' / kind)
        if (local.st_dev, local.st_ino) != (remote.st_dev, remote.st_ino):
            raise MediaSortError('unavailable')
        namespaces.append((remote.st_dev, remote.st_ino))
    # A different chroot is not silently interpreted in Toolbox's root view.
    real_root, process_root = os.stat(base), os.stat(folder / 'root')
    if (real_root.st_dev, real_root.st_ino) != (process_root.st_dev, process_root.st_ino):
        raise MediaSortError('unavailable')
    executable = os.readlink(folder / 'exe')
    if not executable.startswith('/') or executable.endswith(' (deleted)'):
        raise MediaSortError('unavailable')
    descriptor = os.open(folder / 'exe', os.O_RDONLY | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        _regular(before)
        if before.st_size != _ELF_SIZE:
            raise MediaSortError('unavailable')
        binary = _stable(before) + (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if expected is None:
            digest = hashlib.sha256()
            while True:
                guard()
                block = os.read(descriptor, 65536)
                if not block:
                    break
                digest.update(block)
            if digest.hexdigest() != _SHA:
                raise MediaSortError('unavailable')
        elif binary != expected[3] or executable != expected[2]:
            raise MediaSortError('unavailable')
        final = os.fstat(descriptor)
        if _stable(final) + (final.st_size, final.st_mtime_ns, final.st_ctime_ns) != binary:
            raise MediaSortError('unavailable')
    finally:
        os.close(descriptor)
    raw = _read(folder / 'cmdline', 16384)
    if not raw or not raw.endswith(b'\0'):
        raise MediaSortError('unavailable')
    argv = raw[:-1].decode('utf-8', 'strict').split('\0')
    indices = [i for i, value in enumerate(argv) if value == '-d']
    if len(indices) != 1 or indices[0] + 1 >= len(argv):
        raise MediaSortError('unavailable')
    directory = argv[indices[0] + 1]
    if not directory or len(directory.encode('utf-8')) > 4096:
        raise MediaSortError('unavailable')
    cwd = None
    if not directory.startswith('/'):
        cwd = os.readlink(folder / 'cwd')
        if not cwd.startswith('/') or cwd.endswith(' (deleted)'):
            raise MediaSortError('unavailable')
        directory = posixpath.join(cwd, directory)
    relative = posixpath.normpath(directory).lstrip('/') + '/scraper.db'
    result = (pid, identity[2], executable, binary, tuple(namespaces),
              cwd, relative, hashlib.sha256(raw).hexdigest())
    if _kernel_identity(folder) != identity or expected is not None and result != expected:
        raise MediaSortError('unavailable')
    guard()
    return result


def _discover(root, guard):
    """Only current scraper processes supply paths; never scan media directories."""
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW'):
        raise MediaSortError('unavailable')
    found = []
    with os.scandir(Path(root).absolute() / 'proc') as entries:
        for count, entry in enumerate(entries):
            guard()
            if count >= 8192:
                raise MediaSortError('unavailable')
            if not entry.name.isdecimal() or int(entry.name) <= 0:
                continue
            if _read(Path(entry.path) / 'comm', 64) == b'scraper\n':
                process = _process(root, int(entry.name), guard)
                found.append(_Source(process[6], (process,)))
                if len(found) > 16:
                    raise MediaSortError('unavailable')
    return found


class _Opened:
    """Retain no-follow directory/main handles while SQLite uses that directory."""

    def __init__(self, root, source, guard):
        self.root, self.source, self.guard = root, source, guard
        self.fds, self.nodes, self.mutating, self.created_sidecars = [], [], False, set()
        try:
            if os.name != 'posix' or not os.path.isdir('/proc/self/fd'):
                raise MediaSortError('unavailable')
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open(Path(root).absolute(), flags)
            self.fds.append(descriptor)
            self.nodes.append(_stable(os.fstat(descriptor)))
            parts = source.relative.split('/')
            if any(not part or part in ('.', '..') for part in parts) or parts[-1] != 'scraper.db':
                raise MediaSortError('unavailable')
            for part in parts[:-1]:
                guard()
                descriptor = os.open(part, flags, dir_fd=descriptor)
                self.fds.append(descriptor)
                self.nodes.append(_stable(os.fstat(descriptor)))
            parent = os.fstat(descriptor)
            _owned(parent)
            self.directory = descriptor
            self.main = os.open('scraper.db', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=descriptor)
            self.fds.append(self.main)
            _regular(os.fstat(self.main))
            self.main_identity = _stable(os.fstat(self.main))
            if os.pread(self.main, 16, 0) != b'SQLite format 3\0':
                raise MediaSortError('unavailable')
            self.sidecars = self._sidecars()
        except Exception:
            self.close()
            raise

    def _sidecars(self):
        states = {}
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                value = os.stat('scraper.db' + suffix, dir_fd=self.directory,
                                follow_symlinks=False)
            except FileNotFoundError:
                states[suffix] = None
                continue
            _regular(value)
            if suffix == '-journal' and value.st_size and not self.mutating:
                raise MediaSortError('unavailable')
            states[suffix] = _stable(value)
        if states['-wal'] is not None and states['-shm'] is None:
            raise MediaSortError('unavailable')
        return states

    def current(self):
        self.guard()
        for process in self.source.processes:
            _process(self.root, process[0], self.guard, expected=process)
            if process[6] != self.source.relative:
                with _OpenedChain(self.root, process[6]) as alias:
                    if alias[-1] != self.main_identity:
                        raise MediaSortError('unavailable')
        # Reopen the original spelling, never follow a relocated parent.
        with _OpenedChain(self.root, self.source.relative) as values:
            if values != self.nodes + [self.main_identity]:
                raise MediaSortError('unavailable')
        after = self._sidecars()
        if after != self.sidecars:
            if not self.mutating:
                raise MediaSortError('unavailable')
            descriptors = _fd_states(self.guard)
            for suffix, identity in after.items():
                previous = self.sidecars[suffix]
                if identity == previous:
                    continue
                if previous is None and identity is not None and identity in descriptors.values():
                    self.created_sidecars.add(suffix)
                elif identity is None and suffix in self.created_sidecars:
                    pass
                else:
                    raise MediaSortError('unavailable')
            self.sidecars = after
        self.guard()

    def connect(self, write=False):
        self.current()
        before = _fd_states(self.guard)
        uri = 'file:' + quote('/proc/self/fd/' + str(self.directory) + '/scraper.db', safe='/')
        con = sqlite3.connect(uri + ('?mode=rw' if write else '?mode=ro'),
                              uri=True, timeout=0.15, isolation_level=None)
        try:
            con.execute('PRAGMA busy_timeout=150')
            if not write:
                con.execute('PRAGMA query_only=ON')
            con.set_progress_handler(self._progress, 1000)
            # The first connection must expose SQLite's actual main-file fd,
            # not merely our inspection fd. Later connections may share it.
            after = _fd_states(self.guard)
            candidates = [fd for fd, value in after.items()
                          if value == self.main_identity and fd not in before]
            known = getattr(self, 'sqlite_fd', None)
            if not candidates and (known is None or after.get(known) != self.main_identity):
                raise MediaSortError('unavailable')
            if candidates:
                self.sqlite_fd = candidates[0]
            self.current()
            return con
        except Exception:
            con.close()
            raise

    def _progress(self):
        try:
            self.guard()
            return 0
        except MediaSortError:
            return 1

    def close(self):
        for descriptor in reversed(self.fds):
            os.close(descriptor)
        self.fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_unused):
        self.close()


@contextmanager
def _OpenedChain(root, relative):
    descriptors = []
    try:
        fd = os.open(Path(root).absolute(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        values = [_stable(os.fstat(fd))]
        parts = relative.split('/')
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            fd = os.open(part, flags, dir_fd=fd)
            descriptors.append(fd)
            values.append(_stable(os.fstat(fd)))
        yield values
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _fd_states(guard=None):
    values = {}
    names = os.listdir('/proc/self/fd')
    if len(names) > 32768:
        raise MediaSortError('unavailable')
    for name in names:
        if guard is not None:
            guard()
        if name.isdecimal():
            try:
                values[int(name)] = _stable(os.fstat(int(name)))
            except OSError:
                pass
    return values


def _canonical_schema(value):
    value = re.sub(r'"([A-Za-z_][A-Za-z0-9_]*)"', r'\1', value)
    value = re.sub(r'(?i)^CREATE TABLE IF NOT EXISTS ', 'CREATE TABLE ', value.strip())
    return re.sub(r'\s+', '', value).rstrip(';').upper()


def _schema(con):
    """Exact supported column affinities/PKs, not five coincidental names."""
    expected = {'metadata': (_METADATA_NAMES, _METADATA_TYPES, {'id': 1, 'lid': 2, 'type': 3}),
                'library': (_LIBRARY_NAMES, _LIBRARY_TYPES, {'lid': 1})}
    for table, (names, types, primary) in expected.items():
        object_ = con.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (table,)).fetchone()
        if (not object_ or object_[0] != 'table' or not object_[1]
                or 'VIRTUAL' in object_[1].upper()):
            raise MediaSortError('unavailable')
        rows = con.execute('PRAGMA table_xinfo(' + table + ')').fetchall()
        if (tuple(r[1] for r in rows) != names or tuple(r[2].upper() for r in rows) != types
                or any(r[3] != 0 or r[4] is not None or r[5] != primary.get(r[1], 0)
                       or r[6] != 0 for r in rows)):
            raise MediaSortError('unavailable')
        declarations = [name + ' ' + type_ + (' PRIMARY KEY' if table == 'library' and name == 'lid' else '')
                        for name, type_ in zip(names, types)]
        if table == 'metadata':
            declarations.append('PRIMARY KEY (id,lid,type)')
        expected_sql = 'CREATE TABLE ' + table + '(' + ','.join(declarations) + ')'
        if _canonical_schema(object_[1]) != _canonical_schema(expected_sql):
            raise MediaSortError('unavailable')
        if any(row[2] and row[3] != 'pk' for row in con.execute('PRAGMA index_list(' + table + ')')):
            raise MediaSortError('unavailable')
    if con.execute("SELECT json_valid('[]'),json_type('[]'),json_array_length('[]')").fetchone() != (1, 'array', 0):
        raise MediaSortError('unavailable')
    definitions = con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                              "WHERE tbl_name IN ('metadata','library',?) "
                              "OR name LIKE 'metadata%guard%' ORDER BY type,name",
                              (rules.GUARD_TABLE,)).fetchall()
    return definitions


class MediaSortPlugin:
    """Single owner operation at a time; cached state is never authorization."""

    def __init__(self, state_dir, root='/'):
        self.state_dir, self.root = Path(state_dir), root
        self._state_lock, self._operation_lock = threading.Lock(), threading.Lock()
        self._closed, self._salt, self._sources = False, secrets.token_bytes(32), {}
        self._backups = {}
        self._cache = self._empty()

    @staticmethod
    def _empty():
        return {'schema_version': 1, 'id': 'media_sort', 'status': 'unknown', 'busy': False,
                'checked_at': None, 'message': '尚未检测原厂影视数据库。', 'candidates': [],
                'may_wake_disks': True, 'sqlite_metadata_may_change': True}

    def snapshot(self):
        with self._state_lock:
            return copy.deepcopy(self._cache)

    def close(self):
        with self._state_lock:
            self._closed = True
            self._sources.clear()
            self._cache = self._empty()

    def _guard(self, current, deadline):
        try:
            valid = not self._closed and current() is True
        except Exception:
            valid = False
        if not valid:
            raise MediaSortError('authorization_expired')
        if time.monotonic() >= deadline:
            raise MediaSortError('timeout')

    @contextmanager
    def _operation(self, current):
        if not self._operation_lock.acquire(False):
            raise MediaSortError('busy')
        deadline = time.monotonic() + _TIMEOUT
        guard = lambda: self._guard(current, deadline)
        try:
            guard()
            with self._state_lock:
                self._cache['busy'] = True
            yield guard
            guard()
        except MediaSortError:
            raise
        except rules.RuleConflictError:
            raise MediaSortError('conflict') from None
        except Exception:
            guard()
            raise MediaSortError('unavailable') from None
        finally:
            with self._state_lock:
                self._cache['busy'] = False
            self._operation_lock.release()

    def _describe(self, opened, con):
        schema = _schema(con)
        state = rules.owned_artifacts(con)
        allowed = set(rules.TRIGGER_NAMES)
        if any(row[0] == 'trigger' and row[2] == 'metadata' and row[1] not in allowed
               for row in schema):
            state = 'conflict'
        identity = (opened.source.relative, opened.source.processes,
                    opened.nodes, opened.main_identity)
        id_ = hashlib.sha256(self._salt + repr(identity).encode()).hexdigest()[:32]
        version = hashlib.sha256(repr((identity, schema, state)).encode()).hexdigest()
        return {'id': id_, 'version': version,
                'database_path': '/' + opened.source.relative, 'state': state,
                'reason': _MESSAGES['conflict'] if state == 'conflict' else '',
                'can_apply': state == 'disabled',
                'can_revert': state == 'enabled',
                'last_backup_path': self._backups.get(opened.main_identity[:2]),
                'metadata_count': con.execute('SELECT count(*) FROM metadata').fetchone()[0],
                'rule_version': rules.RULE_VERSION if state == 'enabled' else None}

    def _publish(self, rows, sources, guard):
        guard()
        ready = any(row['state'] != 'unavailable' for row in rows)
        with self._state_lock:
            guard()
            self._sources = sources
            self._cache = {**self._empty(), 'status': 'ready' if ready else 'unavailable',
                           'busy': True, 'checked_at': time.time(), 'candidates': rows,
                           'message': '请选择数据库并明确应用或撤销。' if ready
                           else '未找到身份和结构均可确认的原厂影视数据库。'}
        return self.snapshot()

    def _unavailable(self, source):
        identity = repr((source.relative, source.processes)).encode()
        return {'id': hashlib.sha256(self._salt + identity).hexdigest()[:32],
                'version': hashlib.sha256(identity).hexdigest(),
                'database_path': '/' + source.relative, 'state': 'unavailable',
                'reason': _MESSAGES['unavailable'], 'can_apply': False, 'can_revert': False,
                'last_backup_path': None, 'metadata_count': None, 'rule_version': None}

    def probe(self, *, current):
        with self._operation(current) as guard:
            try:
                discovered = _discover(self.root, guard)
            except MediaSortError as error:
                if error.code in ('authorization_expired', 'timeout'):
                    raise
                discovered = []
            rows, sources, groups = [], {}, {}
            for source in discovered:
                guard()
                try:
                    with _Opened(self.root, source, guard) as opened:
                        opened.current()
                        key = opened.main_identity[:2]
                        groups.setdefault(key, []).append(source)
                except (MediaSortError, OSError, sqlite3.Error, UnicodeError) as error:
                    if isinstance(error, MediaSortError) and error.code in ('authorization_expired', 'timeout'):
                        raise
                    guard()
                    rows.append(self._unavailable(source))
            for aliases in groups.values():
                relative = min(source.relative for source in aliases)
                processes = tuple(sorted({p for source in aliases for p in source.processes}, key=repr))
                source = _Source(relative, processes)
                try:
                    with _Opened(self.root, source, guard) as opened:
                        con = opened.connect()
                        try:
                            con.execute('BEGIN')
                            row = self._describe(opened, con)
                            opened.current()
                            con.execute('COMMIT')
                        finally:
                            con.close()
                        rows.append(row)
                        sources[row['id']] = source
                except (MediaSortError, OSError, sqlite3.Error, UnicodeError) as error:
                    if isinstance(error, MediaSortError) and error.code in ('authorization_expired', 'timeout'):
                        raise
                    guard()
                    rows.append(self._unavailable(source))
            result = self._publish(rows, sources, guard)
        result['busy'] = False
        return result

    def _backup(self, opened, guard):
        """Exclusive private destination; online backup observes committed WAL."""
        directory = self.state_dir / 'media-sort-backups'
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise MediaSortError('backup_failed')
        try:
            _owned(info, 0o077)
        except MediaSortError:
            raise MediaSortError('backup_failed') from None
        dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        name = 'scraper-' + secrets.token_hex(16) + '.sqlite3'
        descriptor = None
        source = target = None
        try:
            if _stable(os.fstat(dirfd)) != _stable(info):
                raise MediaSortError('backup_failed')
            descriptor = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR
                                 | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
            identity = _stable(os.fstat(descriptor))
            source = opened.connect()
            before_target = _fd_states(guard)
            target = sqlite3.connect('file:/proc/self/fd/' + str(dirfd) + '/' + name
                                     + '?mode=rw', uri=True, timeout=0.15, isolation_level=None)
            target.set_progress_handler(opened._progress, 1000)
            target_fds = [fd for fd, value in _fd_states(guard).items()
                          if fd not in before_target and value == identity]
            if len(target_fds) != 1:
                raise MediaSortError('backup_failed')
            source.backup(target, pages=64, progress=lambda *_args: guard(), sleep=0.01)
            guard()
            if target.execute('PRAGMA quick_check(1)').fetchone() != ('ok',):
                raise MediaSortError('backup_failed')
            if _stable(os.fstat(target_fds[0])) != identity:
                raise MediaSortError('backup_failed')
            target.close()
            target = None
            if (_stable(os.stat(name, dir_fd=dirfd, follow_symlinks=False)) != identity
                    or _stable(directory.lstat()) != _stable(info)):
                raise MediaSortError('backup_failed')
            os.fsync(descriptor)
            os.fsync(dirfd)
            opened.current()
            return str(directory / name)
        except MediaSortError:
            raise
        except Exception:
            guard()
            raise MediaSortError('backup_failed') from None
        finally:
            if target is not None:
                target.close()
            if source is not None:
                source.close()
            if descriptor is not None:
                os.close(descriptor)
            os.close(dirfd)

    def _change(self, candidate_id, version, confirmed, current, *, revert):
        if confirmed is not True:
            raise MediaSortError('confirmation_required')
        if (type(candidate_id) is not str or re.fullmatch(r'[0-9a-f]{32}', candidate_id) is None
                or type(version) is not str or re.fullmatch(r'[0-9a-f]{64}', version) is None):
            raise MediaSortError('invalid_arguments')
        with self._operation(current) as guard:
            with self._state_lock:
                source = self._sources.get(candidate_id)
            if source is None:
                raise MediaSortError('stale_candidate')
            with _Opened(self.root, source, guard) as opened:
                con = opened.connect(write=True)
                try:
                    con.execute('BEGIN IMMEDIATE')
                    row = self._describe(opened, con)
                    if row['id'] != candidate_id or row['version'] != version:
                        raise MediaSortError('stale_candidate')
                    if row['state'] == 'conflict':
                        raise MediaSortError('conflict')
                    needed = row['state'] != ('disabled' if revert else 'enabled')
                    backup = None
                    if needed:
                        backup = self._backup(opened, guard)
                        self._backups[opened.main_identity[:2]] = backup
                        opened.current()
                        opened.mutating = True
                        if revert:
                            rules.remove(con)
                        else:
                            rules.install(con)
                    guard()
                    opened.current()
                    after = self._describe(opened, con)
                    if after['state'] != ('disabled' if revert else 'enabled'):
                        raise MediaSortError('conflict')
                    con.execute('COMMIT')
                    guard()
                    opened.current()
                    after['last_backup_path'] = backup or self._backups.get(opened.main_identity[:2])
                except Exception:
                    if con.in_transaction:
                        con.execute('ROLLBACK')
                    raise
                finally:
                    con.close()
            result = self._publish([after], {after['id']: source}, guard)
        result['busy'] = False
        return result

    def apply(self, candidate_id, version, *, confirmed, current):
        return self._change(candidate_id, version, confirmed, current, revert=False)

    def revert(self, candidate_id, version, *, confirmed, current):
        return self._change(candidate_id, version, confirmed, current, revert=True)

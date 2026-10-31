"""Owner-only, explicit media sorting plugin; no discovery or SQL on GET.

The factory database remains the source of truth. Only a verified scraper's
data directory can become an opaque candidate. Rules and recovery snapshots
are never installed automatically, including after a Toolbox restart.
"""

from contextlib import contextmanager
from contextvars import ContextVar
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
_PROCESS_STAGES = ('comm', 'stat', 'uid', 'namespace_mnt', 'namespace_net',
                   'namespace_pid', 'root', 'exe_link', 'exe_file', 'elf',
                   'cmdline', 'process_final')
_DB_STAGES = ('db_chain', 'sidecars', 'sqlite_fd', 'schema', 'json1', 'artifacts', 'publication')
_STAGES = ('scan',) + _PROCESS_STAGES + _DB_STAGES
_REASONS = frozenset(('not_started', 'dependency_not_met', 'ok', 'no_process',
    'scan_unavailable', 'scan_limit', 'process_limit', 'process_rejected',
    'comm_invalid', 'stat_invalid', 'uid_unavailable', 'uid_not_root',
    'namespace_mismatch', 'root_mismatch', 'exe_link_invalid', 'exe_file_invalid',
    'elf_size_mismatch', 'elf_hash_mismatch', 'identity_changed', 'cmdline_invalid',
    'data_argument_count', 'data_argument_invalid', 'cwd_invalid',
    'metadata_unavailable', 'db_chain_invalid', 'sidecars_invalid',
    'sqlite_fd_unconfirmed', 'schema_unsupported', 'json1_unavailable',
    'artifact_conflict', 'source_recheck_failed'))
_DEFAULT_REASON = {stage: 'metadata_unavailable' for stage in _STAGES}
_DEFAULT_REASON.update(scan='scan_unavailable', comm='comm_invalid', stat='stat_invalid',
    uid='uid_unavailable', exe_link='exe_link_invalid', exe_file='exe_file_invalid',
    elf='elf_hash_mismatch', cmdline='cmdline_invalid', process_final='identity_changed',
    db_chain='db_chain_invalid', sidecars='sidecars_invalid', sqlite_fd='sqlite_fd_unconfirmed',
    schema='schema_unsupported', json1='json1_unavailable', artifacts='artifact_conflict',
    publication='source_recheck_failed')
# No caller-controlled keys, paths, exception text or command lines enter observations.
_OBSERVATION_KEYS = frozenset(('entries_seen', 'pid_entries', 'comm_matches', 'accepted',
    'rejected', 'complete', 'comm_unavailable', 'comm_complete', 'matched', 'complete_stat', 'pid_matches', 'start_positive',
    'uid_columns', 'real_uid', 'effective_uid', 'saved_uid', 'filesystem_uid',
    'matches_local', 'absolute', 'deleted', 'regular', 'uid', 'gid', 'mode', 'nlink',
    'size', 'size_matches', 'hash_matches', 'file_unchanged', 'bounded', 'nul_terminated',
    'utf8', 'data_argument_count', 'directory_valid', 'directory_kind', 'cwd_valid',
    'generation_unchanged', 'depth', 'main_header', 'wal_present', 'shm_present',
    'journal_present', 'query_only', 'read_only', 'real_fd_confirmed', 'tables',
    'json1', 'artifact_state', 'source_count', 'owner_current'))
_OBSERVER = ContextVar('media_sort_phase_observer', default=None)
_DISCOVERY_OBSERVER = ContextVar('media_sort_discovery_observer', default=None)
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

    def __init__(self, code, *, stage=None, reason=None):
        self.code = code if code in _MESSAGES else 'operation_failed'
        self.message = _MESSAGES[self.code]
        self.stage = stage if stage in _STAGES else None
        self.reason = reason if reason in _REASONS else None
        super().__init__(self.message)


def _stage_empty(reason='dependency_not_met'):
    return {'status': 'not_run', 'reason_code': reason, 'elapsed_ms': 0, 'observations': {}}


class _ProcessObserver:
    """Private, bounded stage results; never a source or authorization proof."""

    def __init__(self, pid):
        self.value = {'pid': pid, 'start_ticks': None, 'status': 'rejected',
                      'stages': {stage: _stage_empty() for stage in _PROCESS_STAGES + _DB_STAGES}}

    def note(self, stage, **values):
        if self.value['status'] == 'identity_changed':
            return
        target = self.value['stages'][stage]['observations']
        for key, value in values.items():
            if key in _OBSERVATION_KEYS and (type(value) in (bool, int) or value in
                    ('absolute', 'relative', 'disabled', 'enabled', 'conflict')):
                target[key] = value

    def changed(self):
        # Discard the old generation, including DB observations, rather than
        # join fields from a replacement process or executable.
        self.value['start_ticks'] = None
        self.value['status'] = 'identity_changed'
        self.value['stages'] = {stage: _stage_empty('identity_changed')
                               for stage in _PROCESS_STAGES + _DB_STAGES}
        self.value['stages']['process_final'] = {
            **_stage_empty('identity_changed'), 'status': 'failed'}


class _Diagnostics:
    """An explicit probe owns this collector until its guarded publication."""

    def __init__(self):
        self.value = self.empty()
        self.processes = []

    @staticmethod
    def empty():
        return {'schema_version': 1, 'status': 'unknown',
                'scan': {**_stage_empty('not_started'), 'observations': {
                    'entries_seen': 0, 'pid_entries': 0, 'comm_matches': 0,
                    'accepted': 0, 'rejected': 0, 'complete': False,
                    'comm_unavailable': 0, 'comm_complete': True}},
                'processes': [], 'publication': _stage_empty('not_started')}

    def process(self, pid):
        observer = _ProcessObserver(pid)
        if len(self.processes) < 16:
            self.processes.append(observer)
        return observer

    def snapshot(self):
        return copy.deepcopy({**self.value, 'processes': [p.value for p in self.processes]})

    def first_failure(self):
        scan = self.value['scan']
        if scan['status'] == 'failed' and scan['reason_code'] != 'process_rejected':
            return 'scan', scan['reason_code']
        for observer in self.processes:
            for stage, value in observer.value['stages'].items():
                if value['status'] == 'failed':
                    return stage, value['reason_code']
        return ('scan', 'no_process') if not self.processes else (None, None)


@contextmanager
def _phase(stage):
    """Annotate the very same checks used by production, without catching them."""
    observed = _OBSERVER.get()
    observers = observed if isinstance(observed, tuple) else (observed,) if observed else ()
    started = time.perf_counter()
    try:
        yield observed
    except Exception as error:
        if isinstance(error, MediaSortError):
            if error.stage is None:
                error.stage = stage
            if error.reason is None:
                error.reason = _DEFAULT_REASON[stage]
        else:
            # Preserve the innermost real gate for IO/SQLite/UTF8 errors too;
            # outer phase scopes must not relabel a sidecar error as db_chain.
            if not getattr(error, '_media_sort_stage', None):
                error._media_sort_stage = stage
        actual = error.stage if isinstance(error, MediaSortError) else error._media_sort_stage
        origin_pid = getattr(error, '_media_sort_pid', None)
        for observer in observers:
            if actual in _PROCESS_STAGES and origin_pid is not None and observer.value['pid'] != origin_pid:
                continue
            if isinstance(error, MediaSortError) and error.reason == 'identity_changed':
                observer.changed()
                continue
            if actual in _PROCESS_STAGES and observer.value['status'] != 'identity_changed':
                # A later source check supersedes discovery's accepted status.
                # Keep other processes and earlier real DB observations intact.
                observer.value['status'] = 'rejected'
            entry = observer.value['stages'][actual]
            if entry['status'] != 'failed':
                if origin_pid is not None:
                    # Revalidation does not publish raw/new process metadata;
                    # do not label an old UID observation as this failed read.
                    entry['observations'] = {}
                entry.update(status='failed', reason_code=(error.reason if isinstance(error, MediaSortError)
                             else _DEFAULT_REASON[actual]), elapsed_ms=min(12000, max(0, round(
                                 (time.perf_counter() - started) * 1000))))
        raise
    else:
        for observer in observers:
            entry = observer.value['stages'][stage]
            if entry['status'] != 'failed' and observer.value['status'] != 'identity_changed':
                entry.update(status='passed', reason_code='ok', elapsed_ms=min(12000, max(0, round(
                    (time.perf_counter() - started) * 1000))))


def _note(stage, **values):
    observed = _OBSERVER.get()
    for observer in observed if isinstance(observed, tuple) else (observed,) if observed else ():
        observer.note(stage, **values)


@contextmanager
def _observe_source(diagnostics, source):
    records = tuple(record for record in diagnostics.processes if any(
        record.value['pid'] == process[0] and record.value['start_ticks'] == process[1]
        for process in source.processes))
    token = _OBSERVER.set(records)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


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


def _scraper_executable(value):
    """Permit only the audited scraper's exact vendor 0777 layout before hashing.

    This exception is never used for databases, sidecars or private backups.
    A permitted descriptor still needs the fixed size/hash and lifecycle gates.
    """
    if stat.S_IMODE(value.st_mode) != 0o777:
        _regular(value)
        return
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise MediaSortError('unavailable')
    _owned(value, mask=0)


@dataclass(frozen=True)
class _Source:
    relative: str
    processes: tuple


def _process(root, pid, guard, *, expected=None, observer=None):
    """Run production gates; optional diagnostics never turn a rejection into a source."""
    token = _OBSERVER.set(observer)
    descriptor = None
    failures = []
    missing = object()

    def checked(stage, operation):
        try:
            guard()
            with _phase(stage):
                return operation()
        except (MediaSortError, OSError, UnicodeError) as error:
            if isinstance(error, MediaSortError) and error.code in ('authorization_expired', 'timeout'):
                raise
            if observer is None:
                raise
            failures.append(error)
            return missing

    try:
        guard()
        folder = Path(root).absolute() / 'proc' / str(pid)
        base = Path(root).absolute()
        if observer is not None:
            with _phase('comm'):
                matched = _read(folder / 'comm', 64) == b'scraper\n'
                _note('comm', matched=matched)
                if not matched:
                    raise MediaSortError('unavailable', reason='comm_invalid')

        def identity_gate():
            value = _kernel_identity(folder)
            valid = value is not None and value[1] == b'scraper' and value[2] > 0
            _note('stat', complete_stat=value is not None,
                  pid_matches=value is not None and value[0] == pid,
                  start_positive=value is not None and value[2] > 0)
            if not valid:
                raise MediaSortError('unavailable', reason='stat_invalid')
            if observer is not None:
                observer.value['start_ticks'] = value[2]
            return value

        identity = checked('stat', identity_gate)
        if identity is missing:
            raise failures[0]

        def generation():
            guard()
            if _kernel_identity(folder) != identity:
                if observer is not None:
                    observer.changed()
                if failures:
                    raise failures[0]
                raise MediaSortError('unavailable', stage='process_final', reason='identity_changed')

        def uid_gate():
            status = _read(folder / 'status', 65536)
            match = re.search(rb'^Uid:\s+([0-9]+)\s+([0-9]+)\s+([0-9]+)\s+([0-9]+)\s*$',
                              status or b'', re.MULTILINE)
            if match is None:
                raise MediaSortError('unavailable', reason='uid_unavailable')
            values = tuple(int(x) for x in match.groups())
            _note('uid', uid_columns=4, real_uid=values[0], effective_uid=values[1],
                  saved_uid=values[2], filesystem_uid=values[3])
            if any(values):
                raise MediaSortError('unavailable', reason='uid_not_root')
            return values

        uid = checked('uid', uid_gate)
        namespaces = []
        for kind in ('mnt', 'net', 'pid'):
            stage = 'namespace_' + kind

            def namespace_gate():
                local = os.stat(base / 'proc/self/ns' / kind)
                remote = os.stat(folder / 'ns' / kind)
                same = (local.st_dev, local.st_ino) == (remote.st_dev, remote.st_ino)
                _note(stage, matches_local=same)
                if not same:
                    raise MediaSortError('unavailable', reason='namespace_mismatch')
                return remote.st_dev, remote.st_ino

            namespaces.append(checked(stage, namespace_gate))

        def root_gate():
            real, remote = os.stat(base), os.stat(folder / 'root')
            same = (real.st_dev, real.st_ino) == (remote.st_dev, remote.st_ino)
            _note('root', matches_local=same)
            if not same:
                raise MediaSortError('unavailable', reason='root_mismatch')

        checked('root', root_gate)

        def link_gate():
            value = os.readlink(folder / 'exe')
            _note('exe_link', absolute=value.startswith('/'), deleted=value.endswith(' (deleted)'))
            if not value.startswith('/') or value.endswith(' (deleted)'):
                raise MediaSortError('unavailable', reason='exe_link_invalid')
            return value

        executable = checked('exe_link', link_gate)

        def file_gate():
            nonlocal descriptor
            descriptor = os.open(folder / 'exe', os.O_RDONLY | os.O_CLOEXEC)
            value = os.fstat(descriptor)
            _note('exe_file', regular=stat.S_ISREG(value.st_mode), uid=value.st_uid,
                  gid=value.st_gid, mode=stat.S_IMODE(value.st_mode), nlink=value.st_nlink,
                  size=value.st_size)
            _scraper_executable(value)
            return _stable(value) + (value.st_size, value.st_mtime_ns, value.st_ctime_ns)

        binary = checked('exe_file', file_gate)
        if observer is not None:
            generation()

        def elf_gate():
            size_matches = binary[-3] == _ELF_SIZE
            _note('elf', size_matches=size_matches)
            if not size_matches:
                raise MediaSortError('unavailable', reason='elf_size_mismatch')
            if expected is not None and ((pid, identity[2]) != expected[:2]
                                         or binary != expected[3] or executable != expected[2]):
                if observer is not None:
                    observer.changed()
                raise MediaSortError('unavailable', reason='identity_changed')

            # A writable vendor ELF cannot rely on the first probe's digest.
            # Revalidate this real descriptor and PID around every hash, also
            # for expected sources used by probe publication/apply/revert.
            generation()
            before = os.fstat(descriptor)
            if _stable(before) + (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != binary:
                if observer is not None:
                    observer.changed()
                raise MediaSortError('unavailable', reason='identity_changed')
            digest, read_bytes = hashlib.sha256(), 0
            while True:
                guard()
                block = os.read(descriptor, min(65536, _ELF_SIZE + 1 - read_bytes))
                if not block:
                    break
                read_bytes += len(block)
                if read_bytes > _ELF_SIZE:
                    raise MediaSortError('unavailable', reason='identity_changed')
                digest.update(block)
            matches = read_bytes == _ELF_SIZE and digest.hexdigest() == _SHA
            _note('elf', hash_matches=matches)
            if not matches:
                raise MediaSortError('unavailable', reason='elf_hash_mismatch')
            after = os.fstat(descriptor)
            unchanged = _stable(after) + (after.st_size, after.st_mtime_ns, after.st_ctime_ns) == binary
            _note('elf', file_unchanged=unchanged)
            if not unchanged:
                if observer is not None:
                    observer.changed()
                raise MediaSortError('unavailable', reason='identity_changed')
            guard()
            if os.readlink(folder / 'exe') != executable:
                if observer is not None:
                    observer.changed()
                raise MediaSortError('unavailable', reason='identity_changed')
            # Binding the current proc link to a fresh FD also rejects a switch
            # to another inode while the hashed descriptor stays unchanged.
            verification_fd = os.open(folder / 'exe', os.O_RDONLY | os.O_CLOEXEC)
            try:
                current = os.fstat(verification_fd)
                unchanged = _stable(current) + (current.st_size, current.st_mtime_ns,
                                                current.st_ctime_ns) == binary
            finally:
                os.close(verification_fd)
            if not unchanged:
                if observer is not None:
                    observer.changed()
                raise MediaSortError('unavailable', reason='identity_changed')
            generation()

        # Namespace/root checks are independent observations; failing them never
        # grants a trusted source. Untrusted UID or file metadata forbids bytes.
        elf = (checked('elf', elf_gate) if uid is not missing and binary is not missing
               and executable is not missing else missing)
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None

        def arguments_gate():
            raw = _read(folder / 'cmdline', 16384)
            valid = bool(raw and raw.endswith(b'\0'))
            _note('cmdline', bounded=raw is not None, nul_terminated=valid)
            if not valid:
                raise MediaSortError('unavailable', reason='cmdline_invalid')
            _note('cmdline', utf8=False)
            argv = raw[:-1].decode('utf-8', 'strict').split('\0')
            _note('cmdline', utf8=True)
            indices = [i for i, value in enumerate(argv) if value == '-d']
            _note('cmdline', data_argument_count=len(indices))
            if len(indices) != 1 or indices[0] + 1 >= len(argv):
                raise MediaSortError('unavailable', reason='data_argument_count')
            directory = argv[indices[0] + 1]
            valid = bool(directory) and len(directory.encode('utf-8')) <= 4096
            _note('cmdline', directory_valid=valid,
                  directory_kind='absolute' if directory.startswith('/') else 'relative')
            if not valid:
                raise MediaSortError('unavailable', reason='data_argument_invalid')
            cwd = None
            if not directory.startswith('/'):
                cwd = os.readlink(folder / 'cwd')
                valid = cwd.startswith('/') and not cwd.endswith(' (deleted)')
                _note('cmdline', cwd_valid=valid)
                if not valid:
                    raise MediaSortError('unavailable', reason='cwd_invalid')
                directory = posixpath.join(cwd, directory)
            relative = posixpath.normpath(directory).lstrip('/') + '/scraper.db'
            return cwd, relative, hashlib.sha256(raw).hexdigest()

        arguments = checked('cmdline', arguments_gate) if elf is not missing else missing
        with _phase('process_final'):
            generation()
            _note('process_final', generation_unchanged=True)
        if failures:
            raise failures[0]
        result = (pid, identity[2], executable, binary, tuple(namespaces), *arguments)
        if expected is not None and result != expected:
            if observer is not None:
                observer.changed()
            raise MediaSortError('unavailable', stage='process_final', reason='identity_changed')
        guard()
        if observer is not None:
            observer.value['status'] = 'accepted'
        return result
    except Exception as error:
        if not getattr(error, '_media_sort_pid', None):
            error._media_sort_pid = pid
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _OBSERVER.reset(token)


def _discover(root, guard, observer=None):
    """Only current scraper processes supply paths; never scan media directories."""
    observer = observer or _DISCOVERY_OBSERVER.get()
    scan = observer.value['scan'] if observer is not None else None
    counts = scan['observations'] if scan is not None else {}
    started, first_error, found = time.perf_counter(), None, []
    try:
        if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW'):
            raise MediaSortError('unavailable', stage='scan', reason='scan_unavailable')
        with os.scandir(Path(root).absolute() / 'proc') as entries:
            for count, entry in enumerate(entries):
                guard()
                counts['entries_seen'] = count + 1
                if count >= 8192:
                    raise MediaSortError('unavailable', stage='scan', reason='scan_limit')
                if not entry.name.isdecimal() or int(entry.name) <= 0:
                    continue
                counts['pid_entries'] = counts.get('pid_entries', 0) + 1
                comm = _read(Path(entry.path) / 'comm', 64)
                if comm is None:
                    counts['comm_unavailable'] = counts.get('comm_unavailable', 0) + 1
                    counts['comm_complete'] = False
                if comm == b'scraper\n':
                    counts['comm_matches'] = counts.get('comm_matches', 0) + 1
                    if counts['comm_matches'] > 16:
                        raise MediaSortError('unavailable', stage='scan', reason='process_limit')
                    record = observer.process(int(entry.name)) if observer is not None else None
                    if record is not None:
                        record.value['stages']['comm'].update(status='passed', reason_code='ok',
                                                              observations={'matched': True})
                    try:
                        process = (_process(root, int(entry.name), guard, observer=record)
                                   if record is not None else _process(root, int(entry.name), guard))
                        found.append(_Source(process[6], (process,)))
                        counts['accepted'] = counts.get('accepted', 0) + 1
                    except (MediaSortError, OSError, UnicodeError) as error:
                        if observer is None or isinstance(error, MediaSortError) and error.code in (
                                'authorization_expired', 'timeout'):
                            raise
                        counts['rejected'] = counts.get('rejected', 0) + 1
                        if first_error is None:
                            first_error = error
        counts['complete'] = True
        if first_error is not None:
            raise first_error
        if scan is not None:
            scan.update(status='passed', reason_code='ok' if found else 'no_process')
        return found
    except Exception as error:
        if scan is not None:
            scan.update(status='failed', reason_code=('process_rejected' if first_error is error
                        else error.reason if isinstance(error, MediaSortError) and error.reason
                        else 'scan_unavailable'))
        raise
    finally:
        if scan is not None:
            scan['elapsed_ms'] = min(12000, max(0, round((time.perf_counter() - started) * 1000)))


class _Opened:
    """Retain no-follow directory/main handles while SQLite uses that directory."""

    def __init__(self, root, source, guard):
        self.root, self.source, self.guard = root, source, guard
        self.fds, self.nodes, self.mutating, self.created_sidecars = [], [], False, set()
        try:
            # A cached source must pass its current executable gates before any
            # dependent database/sidecar is opened, including apply and revert.
            with _phase('process_final'):
                for process in source.processes:
                    _process(root, process[0], guard, expected=process)
            self._open_chain(root, source, guard)
            self.sidecars = self._sidecars()
        except Exception:
            self.close()
            raise

    def _open_chain(self, root, source, guard):
        """Production no-follow chain, with only its depth/header exposed."""
        with _phase('db_chain'):
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
            header = os.pread(self.main, 16, 0) == b'SQLite format 3\0'
            _note('db_chain', depth=len(parts), main_header=header)
            if not header:
                raise MediaSortError('unavailable')

    def _sidecars(self):
        with _phase('sidecars'):
            return self._sidecar_states()

    def _sidecar_states(self):
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
        _note('sidecars', wal_present=states['-wal'] is not None,
              shm_present=states['-shm'] is not None, journal_present=states['-journal'] is not None)
        return states

    def current(self):
        self.guard()
        with _phase('db_chain'):
            for process in self.source.processes:
                _process(self.root, process[0], self.guard, expected=process)
                if process[6] != self.source.relative:
                    with _OpenedChain(self.root, process[6]) as alias:
                        if alias[-1] != self.main_identity:
                            raise MediaSortError('unavailable', stage='db_chain', reason='identity_changed')
            # Reopen the original spelling, never follow a relocated parent.
            with _OpenedChain(self.root, self.source.relative) as values:
                if values != self.nodes + [self.main_identity]:
                    raise MediaSortError('unavailable', stage='db_chain', reason='identity_changed')
        after = self._sidecars()
        if after != self.sidecars:
            if not self.mutating:
                raise MediaSortError('unavailable', stage='sidecars', reason='identity_changed')
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
        with _phase('sqlite_fd'):
            return self._connect(write)

    def _connect(self, write=False):
        self.current()
        before = _fd_states(self.guard)
        uri = 'file:' + quote('/proc/self/fd/' + str(self.directory) + '/scraper.db', safe='/')
        con = sqlite3.connect(uri + ('?mode=rw' if write else '?mode=ro'),
                              uri=True, timeout=0.15, isolation_level=None)
        try:
            con.execute('PRAGMA busy_timeout=150')
            if not write:
                con.execute('PRAGMA query_only=ON')
                _note('sqlite_fd', read_only=True, query_only=True)
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
            _note('sqlite_fd', real_fd_confirmed=True)
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


def _schema_tables(con):
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
    _note('schema', tables=2)


def _schema(con):
    """Expose fixed phases around the unchanged schema and JSON1 checks."""
    with _phase('schema'):
        _schema_tables(con)
    with _phase('json1'):
        if con.execute("SELECT json_valid('[]'),json_type('[]'),json_array_length('[]')").fetchone() != (1, 'array', 0):
            raise MediaSortError('unavailable')
        _note('json1', json1=True)
    with _phase('schema'):
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
                'may_wake_disks': True, 'sqlite_metadata_may_change': True,
                'diagnostics': _Diagnostics.empty()}

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
    def _operation(self, current, rollback=None):
        if not self._operation_lock.acquire(False):
            raise MediaSortError('busy')
        deadline = time.monotonic() + _TIMEOUT
        guard = lambda: self._guard(current, deadline)
        try:
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
        except MediaSortError as error:
            # Roll back only this probe's new private cache while still owning
            # the operation lock; never clobber a subsequent authorized probe.
            if error.code in ('authorization_expired', 'timeout') and rollback:
                with self._state_lock:
                    if not self._closed:
                        self._cache, self._sources = rollback[0]
                        self._cache['busy'] = False
            raise
        finally:
            with self._state_lock:
                self._cache['busy'] = False
            self._operation_lock.release()

    def _describe(self, opened, con):
        schema = _schema(con)
        with _phase('artifacts'):
            state = rules.owned_artifacts(con)
            allowed = set(rules.TRIGGER_NAMES)
            if any(row[0] == 'trigger' and row[2] == 'metadata' and row[1] not in allowed
                   for row in schema):
                state = 'conflict'
            _note('artifacts', artifact_state=state)
        identity = (opened.source.relative, opened.source.processes,
                    opened.nodes, opened.main_identity)
        id_ = hashlib.sha256(self._salt + repr(identity).encode()).hexdigest()[:32]
        version = hashlib.sha256(repr((identity, schema, state)).encode()).hexdigest()
        with _phase('schema'):
            metadata_count = con.execute('SELECT count(*) FROM metadata').fetchone()[0]
        return {'id': id_, 'version': version,
                'database_path': '/' + opened.source.relative, 'state': state,
                'reason': _MESSAGES['conflict'] if state == 'conflict' else '',
                'can_apply': state == 'disabled',
                'can_revert': state == 'enabled',
                'last_backup_path': self._backups.get(opened.main_identity[:2]),
                'metadata_count': metadata_count,
                'rule_version': rules.RULE_VERSION if state == 'enabled' else None}

    def _publish(self, rows, sources, guard, diagnostics=None):
        guard()
        ready = any(row['state'] != 'unavailable' for row in rows)
        detail = diagnostics.snapshot() if diagnostics is not None else None
        stage, reason = diagnostics.first_failure() if diagnostics is not None else (None, None)
        with self._state_lock:
            guard()
            previous = self._cache['diagnostics']
            cache = {**self._empty(), 'status': 'ready' if ready else 'unavailable',
                           'busy': True, 'checked_at': time.time(), 'candidates': rows,
                           'diagnostics': detail if diagnostics is not None else previous,
                           'message': '请选择数据库并明确应用或撤销。' if ready
                           else ('原厂影视数据库检测未通过，首个拒绝阶段：' + stage
                                 + '（' + reason + '）。' if stage else
                                 '未找到身份和结构均可确认的原厂影视数据库。')}
            guard()
            self._sources, self._cache = sources, cache
        return self.snapshot()

    def _unavailable(self, source):
        identity = repr((source.relative, source.processes)).encode()
        return {'id': hashlib.sha256(self._salt + identity).hexdigest()[:32],
                'version': hashlib.sha256(identity).hexdigest(),
                'database_path': '/' + source.relative, 'state': 'unavailable',
                'reason': _MESSAGES['unavailable'], 'can_apply': False, 'can_revert': False,
                'last_backup_path': None, 'metadata_count': None, 'rule_version': None}

    def probe(self, *, current):
        """Discard new private results even if expiry happens after publication."""
        return self._probe_operation(current=current, rollback=[])

    def _probe_operation(self, *, current, rollback):
        with self._operation(current, rollback=rollback) as guard:
            with self._state_lock:
                rollback.append((copy.deepcopy(self._cache), dict(self._sources)))
            diagnostics = _Diagnostics()
            token = _DISCOVERY_OBSERVER.set(diagnostics)
            try:
                discovered = _discover(self.root, guard)
            except MediaSortError as error:
                if error.code in ('authorization_expired', 'timeout'):
                    raise
                discovered = []
            except (OSError, UnicodeError):
                # Keep the existing 503 response, but an owner-current cached
                # GET may inspect safe phases; no executable source survives.
                guard()
                diagnostics.value['status'] = 'complete'
                diagnostics.value['publication'].update(status='passed', reason_code='ok',
                    observations={'source_count': 0, 'owner_current': True})
                self._publish([], {}, guard, diagnostics)
                raise
            finally:
                _DISCOVERY_OBSERVER.reset(token)
            rows, sources, groups, proofs = [], {}, {}, {}
            for source in discovered:
                guard()
                try:
                    with _observe_source(diagnostics, source):
                        with _phase('db_chain'):
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
                    with _observe_source(diagnostics, source):
                        with _Opened(self.root, source, guard) as opened:
                            con = opened.connect()
                            try:
                                with _phase('sqlite_fd'):
                                    con.execute('BEGIN')
                                row = self._describe(opened, con)
                                opened.current()
                                con.execute('COMMIT')
                            finally:
                                con.close()
                            rows.append(row)
                            sources[row['id']] = source
                            proofs[row['id']] = (opened.nodes, opened.main_identity,
                                                getattr(opened, 'sidecars', None))
                except (MediaSortError, OSError, sqlite3.Error, UnicodeError) as error:
                    if isinstance(error, MediaSortError) and error.code in ('authorization_expired', 'timeout'):
                        raise
                    guard()
                    rows.append(self._unavailable(source))
            # Earlier candidates may have changed while later candidates were
            # inspected. Recheck actual process/FD sources before publishing.
            started = time.perf_counter()
            publication = diagnostics.value['publication']
            try:
                for id_, source in sources.items():
                    with _observe_source(diagnostics, source), _phase('publication'):
                        with _Opened(self.root, source, guard) as opened:
                            opened.current()
                            if (opened.nodes, opened.main_identity, getattr(opened, 'sidecars', None)) != proofs[id_]:
                                raise MediaSortError('unavailable', stage='publication', reason='identity_changed')
                        _note('publication', owner_current=True)
                guard()
                publication.update(status='passed', reason_code='ok',
                                   observations={'source_count': len(sources), 'owner_current': True})
            except MediaSortError as error:
                if error.code in ('authorization_expired', 'timeout'):
                    raise
                rows, sources = [], {}
                publication.update(status='failed', reason_code='source_recheck_failed')
            except (OSError, UnicodeError):
                guard()
                diagnostics.value['status'] = 'complete'
                publication.update(status='failed', reason_code='source_recheck_failed')
                self._publish([], {}, guard, diagnostics)
                raise
            publication['elapsed_ms'] = min(12000, max(0, round((time.perf_counter() - started) * 1000)))
            diagnostics.value['status'] = 'complete'
            result = self._publish(rows, sources, guard, diagnostics)
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

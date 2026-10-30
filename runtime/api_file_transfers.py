"""Bounded file-byte transport for an independently verified file backend.

This module never resolves or opens a pathname. Trusted backend objects supply
already authorized handles and must revalidate the native subject, ACL and root
against each exact descriptor. FileReadExecutor wires the verified read-only
provider; uploads remain unavailable until their native write protocol is proven.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import hashlib
import os
import re
import secrets
import stat
import threading
import time
from urllib.parse import quote

from api_file_operations import FileAccessError, assert_file_grant
from network_access import normalize_peer


_ID = re.compile(r'transfer_[0-9a-f]{32}\Z', re.ASCII)
_SHA = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_IDEMPOTENCY = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z', re.ASCII)
_VERSION = re.compile(r'[A-Za-z0-9_.:-]{1,256}\Z', re.ASCII)
_TERMINAL = frozenset(('completed', 'cancelled', 'expired', 'failed'))
_CHUNK = 65536
_FIRST_GET_SECONDS = 300
_TRANSFER_SECONDS = 1800
_MAX_SIZE = 8 * 1024 ** 3


@dataclass(frozen=True, repr=False)
class DownloadSource:
    """Private backend capability, never constructed from HTTP/JSON evidence.

    open_reader(principal) returns an owned O_RDONLY fd opened by the real
    backend under the verified NAS subject. authorize_opened(principal, fd,
    write=False) must validate the original ACL and live root for that same fd.
    It must not replace those checks with a root-plan lookup or bool from input.
    """

    root_id: str
    path: str
    name: str
    open_reader: object
    authorize_opened: object


@dataclass(frozen=True, repr=False)
class UploadTarget:
    """Backend-only authority for unnamed staging and native atomic install.

    create_staging must return an unnamed O_RDWR ordinary file with native ACL,
    quota and index behavior already verified. install(principal, fd,
    overwrite=..., expected_target_version=...) must atomically install that
    exact object or report an unconfirmed result; there is no os.rename/root
    fallback here. authorize_opened verifies the same live descriptor each time.
    """

    root_id: str
    path: str
    name: str
    create_staging: object
    authorize_opened: object
    install: object


def _name(value):
    return (isinstance(value, str) and 0 < len(value) <= 255 and value not in ('.', '..')
            and '/' not in value and '\\' not in value and all(char.isprintable() for char in value))


def _binding(principal):
    if not isinstance(principal, dict):
        raise FileAccessError('invalid_key')
    peer = normalize_peer(principal.get('_peer'))
    if (peer is None or not isinstance(principal.get('key_id'), str)
            or not re.fullmatch(r'[0-9a-f]{24}', principal['key_id'], re.ASCII)
            or any(type(principal.get(field)) is not int or principal[field] < 1
                   for field in ('grant_revision', 'credential_generation'))):
        raise FileAccessError('invalid_key')
    return (principal['key_id'], principal['grant_revision'], principal['credential_generation'], peer)


def _identity(descriptor):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= _MAX_SIZE:
        raise FileAccessError('file_changed')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _version(identity):
    return 'file_' + hashlib.sha256(repr(identity).encode('ascii')).hexdigest()[:48]


def _stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _byte_range(value, size):
    if value is None:
        return 200, 0, size
    if not isinstance(value, str) or len(value) > 96:
        raise FileAccessError('unsupported_range')
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', value, re.ASCII)
    if match is None or not any(match.groups()):
        raise FileAccessError('unsupported_range')
    start, end = match.groups()
    if start:
        first = int(start)
        last = int(end) if end else size - 1
        if first >= size or last < first:
            return 416, 0, 0
        return 206, first, min(last, size - 1) - first + 1
    suffix = int(end)
    if suffix == 0 or size == 0:
        return 416, 0, 0
    count = min(suffix, size)
    return 206, size - count, count


class DownloadResponse:
    """One GET's owned fd and explicit send receipt; close on HTTP disconnect.

    send(writer) counts only whole chunks accepted by writer. A completed server
    send is not proof of the client's saved bytes. The HTTP adapter must call
    close() if it decides not to consume the response or headers fail to send.
    """

    def __init__(self, manager, session, descriptor, status, headers, offset, length):
        self.status, self.headers = status, headers
        self._manager, self._session, self._descriptor = manager, session, descriptor
        self._offset, self._length, self._closed = offset, length, descriptor is None

    def send(self, writer):
        if not callable(writer) or self._closed or self._descriptor is None:
            raise FileAccessError('transfer_unavailable')
        sent = 0
        try:
            while sent < self._length:
                self._manager._check_read(self._session, self._descriptor)
                block = os.pread(self._descriptor, min(_CHUNK, self._length - sent), self._offset + sent)
                if not block:
                    raise FileAccessError('file_changed')
                self._manager._check_read(self._session, self._descriptor)
                accepted = writer(block)
                if accepted is not None and (type(accepted) is not int or accepted != len(block)):
                    raise OSError('short transport write')
                sent += len(block)
                with self._manager._lock:
                    self._session['sent_bytes'] += len(block)
            self._manager._finish_read(self._session, self._descriptor, self._offset == 0
                                       and self._length == self._session['size_bytes'])
            self._closed = True
            self._descriptor = None
            return sent
        except Exception:
            self.close()
            raise

    def close(self):
        if not self._closed:
            self._manager._interrupt(self._session, self._descriptor)
            self._descriptor = None
            self._closed = True


class FileTransfers:
    """In-memory byte sessions; restart invalidates every handle and operation."""

    def __init__(self, is_current, *, clock=time.monotonic, wall_clock=time.time):
        if not callable(is_current):
            raise TypeError('is_current callback required')
        self._is_current, self._clock, self._wall = is_current, clock, wall_clock
        self._lock = threading.RLock()
        self._sessions, self._operations, self._idempotency, self._commit_idempotency = {}, {}, {}, {}

    def _current(self, principal):
        _binding(principal)
        try:
            valid = self._is_current(principal) is True
        except Exception:
            valid = False
        if not valid:
            raise FileAccessError('invalid_key')

    def _authorize(self, principal, resource, *, write=False):
        self._current(principal)
        assert_file_grant(principal, 'prepare_file_upload' if write else 'prepare_file_download',
                          resource.root_id, resource.path, write=write)

    def _opened(self, principal, resource, *, write=False):
        self._authorize(principal, resource, write=write)
        opener = resource.create_staging if write else resource.open_reader
        if not callable(opener) or not callable(resource.authorize_opened) or not _name(resource.name):
            raise FileAccessError('secure_open_unavailable')
        descriptor = None
        try:
            descriptor = opener(principal)
            if type(descriptor) is not int or descriptor < 0:
                raise FileAccessError('secure_open_unavailable')
            os.set_inheritable(descriptor, False)
            import fcntl
            flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
            if flags & os.O_ACCMODE != (os.O_RDWR if write else os.O_RDONLY):
                raise FileAccessError('secure_open_unavailable')
            if write and (flags & os.O_APPEND or os.fstat(descriptor).st_nlink != 0
                          or os.fstat(descriptor).st_size != 0):
                raise FileAccessError('write_backend_unverified')
            identity = _identity(descriptor)
            if resource.authorize_opened(principal, descriptor, write=write) is not True:
                raise FileAccessError('acl_unverified')
            self._authorize(principal, resource, write=write)
            return descriptor, identity
        except Exception:
            if type(descriptor) is int and descriptor >= 0:
                os.close(descriptor)
            raise

    def _new(self, principal, resource, direction, size):
        now = self._clock()
        for identifier, row in list(self._sessions.items()):
            if now >= row['deadline'] and not row['active']:
                self._terminal(row, 'expired')
            if row['state'] in _TERMINAL and now - row['changed'] >= 300:
                self._sessions.pop(identifier)
                self._idempotency = {key: value for key, value in self._idempotency.items()
                                     if value[1] != identifier}
                self._commit_idempotency = {key: value for key, value in self._commit_idempotency.items()
                                            if value != identifier}
                self._operations = {key: value for key, value in self._operations.items()
                                    if value != identifier}
        active = [row for row in self._sessions.values() if row['state'] not in _TERMINAL]
        binding = _binding(principal)
        if (len(active) >= 4 or sum(row['binding'][0] == binding[0] for row in active) >= 2
                or len(self._sessions) >= 128):
            raise FileAccessError('transfer_busy')
        identifier = 'transfer_' + secrets.token_hex(16)
        row = {'id': identifier, 'binding': binding, 'principal': copy.deepcopy(principal),
               'resource': resource, 'direction': direction, 'state': 'prepared', 'active': False,
               'size_bytes': size, 'sent_bytes': 0, 'received_bytes': 0,
               'verified_sha256': None, 'object_version': None,
               'first_get_deadline': now + _FIRST_GET_SECONDS if direction == 'download' else None,
               'first_get_wall': self._wall() + _FIRST_GET_SECONDS if direction == 'download' else None,
               'transfer_deadline': None, 'transfer_wall': None,
               'deadline': now + (_FIRST_GET_SECONDS if direction == 'download' else _TRANSFER_SECONDS),
               'expires_wall': self._wall() + _TRANSFER_SECONDS if direction == 'upload' else None,
               'changed': now, 'error': None, 'fd': None, 'reader_fd': None}
        self._sessions[identifier] = row
        return row

    def _terminal(self, row, state, error=None):
        if row['fd'] is not None:
            os.close(row['fd'])
            row['fd'] = None
        row.update(state=state, active=False, changed=self._clock(), error=error)

    def _owned(self, identifier, principal, *, tool=None):
        self._current(principal)
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise FileAccessError('invalid_arguments')
        row = self._sessions.get(identifier)
        if row is None or row['binding'] != _binding(principal):
            raise FileAccessError('transfer_unavailable')
        self._authorize(principal, row['resource'], write=row['direction'] == 'upload')
        if tool:
            assert_file_grant(principal, tool, row['resource'].root_id, row['resource'].path,
                              write=row['direction'] == 'upload')
        if self._clock() >= row['deadline'] and row['state'] not in _TERMINAL:
            self._terminal(row, 'expired')
        return row

    def prepare_download(self, principal, source):
        if not isinstance(source, DownloadSource):
            raise FileAccessError('secure_open_unavailable')
        with self._lock:
            descriptor, identity = self._opened(principal, source)
            os.close(descriptor)
            row = self._new(principal, source, 'download', identity[2])
            row.update(identity=identity, object_version=_version(identity))
            return {'ok': True, 'transfer_id': row['id'], 'content_path': self._content(row),
                    'name': source.name, 'size_bytes': row['size_bytes'],
                    'object_version': row['object_version'], 'state': 'prepared',
                    'first_get_deadline': _stamp(row['first_get_wall']), 'transfer_deadline': None,
                    'may_wake_disks': True}

    @staticmethod
    def _content(row):
        return '/api/integration/files/transfers/' + row['id'] + '/content'

    def _public(self, row):
        return {'transfer_id': row['id'], 'direction': row['direction'], 'state': row['state'],
                'name': row['resource'].name, 'size_bytes': row['size_bytes'],
                'sent_bytes': row['sent_bytes'] if row['direction'] == 'download' else None,
                'received_bytes': row['received_bytes'] if row['direction'] == 'upload' else None,
                'verified_sha256': row['verified_sha256'], 'object_version': row['object_version'],
                'first_get_deadline': _stamp(row['first_get_wall']) if row['first_get_wall'] else None,
                'transfer_deadline': _stamp(row['transfer_wall']) if row['transfer_wall'] else None,
                'expires_at': _stamp(row['expires_wall']) if row['expires_wall'] else None,
                'error': copy.deepcopy(row['error'])}

    def get_transfer(self, identifier, principal):
        with self._lock:
            return {'ok': True, 'transfer': self._public(self._owned(identifier, principal, tool='get_file_transfer'))}

    def cancel_transfer(self, identifier, principal):
        with self._lock:
            row = self._owned(identifier, principal, tool='cancel_file_transfer')
            if row['state'] not in _TERMINAL:
                # Active GET owns its fd; its next authorization gate closes it.
                row.update(state='cancelled', changed=self._clock())
                if row['direction'] == 'upload' and not row['active']:
                    self._terminal(row, 'cancelled')
            return {'ok': True, 'transfer': self._public(row)}

    def open_download(self, identifier, principal, *, head=False, range_header=None):
        with self._lock:
            row = self._owned(identifier, principal)
            if row['direction'] != 'download' or row['state'] in _TERMINAL:
                raise FileAccessError('transfer_expired' if row['state'] == 'expired' else 'transfer_unavailable')
            if row['active']:
                raise FileAccessError('transfer_busy')
            descriptor, identity = self._opened(principal, row['resource'])
            if self._clock() >= row['deadline']:
                os.close(descriptor)
                self._terminal(row, 'expired')
                raise FileAccessError('transfer_expired')
            if identity != row['identity']:
                os.close(descriptor)
                self._terminal(row, 'failed', FileAccessError('file_changed').result()['error'])
                raise FileAccessError('file_changed')
            try:
                status, offset, length = (200, 0, row['size_bytes']) if head else _byte_range(range_header, row['size_bytes'])
            except Exception:
                os.close(descriptor)
                raise
            headers = {'Content-Type': 'application/octet-stream', 'Accept-Ranges': 'bytes',
                       'Content-Length': str(length), 'Cache-Control': 'no-store',
                       'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                       'Content-Disposition': 'attachment; filename="download.bin"; filename*=UTF-8\'\''
                                              + quote(row['resource'].name, safe='')}
            if status == 206:
                headers['Content-Range'] = f'bytes {offset}-{offset + length - 1}/{row["size_bytes"]}'
            if status == 416:
                headers['Content-Range'] = 'bytes */' + str(row['size_bytes'])
            if head or status == 416:
                os.close(descriptor)
                return DownloadResponse(self, row, None, status, headers, offset, length)
            if row['transfer_deadline'] is None:
                row.update(transfer_deadline=self._clock() + _TRANSFER_SECONDS,
                           transfer_wall=self._wall() + _TRANSFER_SECONDS,
                           deadline=self._clock() + _TRANSFER_SECONDS)
            row.update(active=True, state='streaming', changed=self._clock(), reader_fd=descriptor)
            return DownloadResponse(self, row, descriptor, status, headers, offset, length)

    def _check_read(self, row, descriptor):
        with self._lock:
            self._owned(row['id'], row['principal'])
            if row['state'] != 'streaming' or not row['active'] or row['reader_fd'] != descriptor:
                raise FileAccessError('transfer_unavailable')
            if _identity(descriptor) != row['identity']:
                raise FileAccessError('file_changed')
            if row['resource'].authorize_opened(row['principal'], descriptor, write=False) is not True:
                raise FileAccessError('acl_unverified')
            # Native ACL verification can block. Never release its result after
            # the token, transfer window or cancellation changed meanwhile.
            self._owned(row['id'], row['principal'])
            if row['state'] != 'streaming' or not row['active'] or row['reader_fd'] != descriptor:
                raise FileAccessError('transfer_expired' if row['state'] == 'expired' else 'transfer_unavailable')

    def _finish_read(self, row, descriptor, full):
        with self._lock:
            self._check_read(row, descriptor)
            os.close(descriptor)
            row.update(active=False, reader_fd=None, state='completed' if full else 'ready', changed=self._clock())

    def _interrupt(self, row, descriptor):
        with self._lock:
            if descriptor is not None and row['reader_fd'] == descriptor:
                os.close(descriptor)
            row.update(active=False, reader_fd=None)
            if row['state'] not in _TERMINAL:
                row.update(state='interrupted', changed=self._clock())

    def prepare_upload(self, principal, target, *, size_bytes, sha256, idempotency_key,
                       overwrite=False, expected_target_version=None, confirmed=False):
        if (not isinstance(target, UploadTarget) or not target.path or not callable(target.install)):
            raise FileAccessError('write_backend_unverified')
        if (type(size_bytes) is not int or not 0 <= size_bytes <= _MAX_SIZE
                or not isinstance(sha256, str) or not _SHA.fullmatch(sha256)
                or not isinstance(idempotency_key, str) or not _IDEMPOTENCY.fullmatch(idempotency_key)
                or type(overwrite) is not bool or type(confirmed) is not bool
                or overwrite and (not confirmed or not isinstance(expected_target_version, str)
                                  or not _VERSION.fullmatch(expected_target_version))
                or not overwrite and (confirmed or expected_target_version is not None)):
            raise FileAccessError('invalid_arguments')
        with self._lock:
            self._authorize(principal, target, write=True)
            binding = _binding(principal)
            identity = (binding, idempotency_key)
            request = (target.root_id, target.path, size_bytes, sha256, overwrite, expected_target_version)
            prior = self._idempotency.get(identity)
            if prior:
                if prior[0] != request:
                    raise FileAccessError('idempotency_conflict')
                row = self._owned(prior[1], principal)
                if row['state'] in _TERMINAL:
                    raise FileAccessError('transfer_expired' if row['state'] == 'expired' else 'transfer_unavailable')
            else:
                descriptor, _ = self._opened(principal, target, write=True)
                try:
                    row = self._new(principal, target, 'upload', size_bytes)
                    row.update(fd=descriptor, sha256=sha256, overwrite=overwrite,
                               expected_target_version=expected_target_version)
                    self._idempotency[identity] = (request, row['id'])
                except Exception:
                    os.close(descriptor)
                    raise
            return {'ok': True, 'transfer_id': row['id'], 'content_path': self._content(row),
                    'direction': 'upload', 'state': 'prepared', 'size_bytes': size_bytes,
                    'sha256': sha256, 'overwrite': overwrite, 'expires_at': _stamp(row['expires_wall']),
                    'may_wake_disks': True}

    def receive_upload(self, identifier, principal, chunks, *, content_length):
        with self._lock:
            row = self._owned(identifier, principal)
            if (row['direction'] != 'upload' or row['state'] != 'prepared'
                    or type(content_length) is not int or content_length != row['size_bytes']):
                raise FileAccessError('transfer_unavailable')
            row.update(state='receiving', active=True)
        digest, count = hashlib.sha256(), 0
        try:
            for block in chunks:
                if not isinstance(block, bytes) or not block or len(block) > _CHUNK:
                    raise FileAccessError('invalid_arguments')
                with self._lock:
                    self._check_upload(row, principal, 'receiving')
                    if count + len(block) > row['size_bytes']:
                        raise FileAccessError('limit_exceeded')
                    written = os.pwrite(row['fd'], block, count)
                    if written != len(block):
                        raise OSError('short staging write')
                    count += written
                    row['received_bytes'] = count
                    digest.update(block)
                    self._check_upload(row, principal, 'receiving')
            with self._lock:
                self._check_upload(row, principal, 'receiving')
                if count != row['size_bytes'] or digest.hexdigest() != row['sha256']:
                    raise FileAccessError('checksum_mismatch')
                os.fsync(row['fd'])
                self._check_upload(row, principal, 'receiving')
                row.update(state='verified', active=False, verified_sha256=digest.hexdigest(),
                           identity=_identity(row['fd']), changed=self._clock())
                return {'ok': True, 'transfer': self._public(row)}
        except Exception as error:
            with self._lock:
                if row['state'] not in _TERMINAL:
                    failure = error if isinstance(error, FileAccessError) else FileAccessError('result_unconfirmed')
                    self._terminal(row, 'failed', failure.result()['error'])
                elif row['fd'] is not None:
                    os.close(row['fd'])
                    row['fd'] = None
            raise

    def _check_upload(self, row, principal, state):
        self._owned(row['id'], principal)
        if row['state'] != state or row['fd'] is None:
            raise FileAccessError('transfer_expired' if row['state'] == 'expired' else 'transfer_unavailable')
        if row['resource'].authorize_opened(principal, row['fd'], write=True) is not True:
            raise FileAccessError('acl_unverified')
        self._owned(row['id'], principal)
        if row['state'] != state or row['fd'] is None:
            raise FileAccessError('transfer_expired' if row['state'] == 'expired' else 'transfer_unavailable')

    def commit_upload(self, identifier, principal, *, idempotency_key):
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY.fullmatch(idempotency_key):
            raise FileAccessError('invalid_arguments')
        with self._lock:
            row = self._owned(identifier, principal, tool='commit_file_upload')
            identity = (_binding(principal), idempotency_key)
            if identity in self._commit_idempotency and self._commit_idempotency[identity] != identifier:
                raise FileAccessError('idempotency_conflict')
            if row.get('operation'):
                operation = row['operation']
                if row['commit_key'] != idempotency_key:
                    raise FileAccessError('idempotency_conflict')
                return {'ok': operation['state'] == 'completed', 'operation': dict(operation), 'may_wake_disks': True}
            self._check_upload(row, principal, 'verified')
            if _identity(row['fd']) != row['identity']:
                raise FileAccessError('file_changed')
            digest, offset = hashlib.sha256(), 0
            while offset < row['size_bytes']:
                self._check_upload(row, principal, 'verified')
                block = os.pread(row['fd'], min(_CHUNK, row['size_bytes'] - offset), offset)
                if not block:
                    raise FileAccessError('file_changed')
                digest.update(block)
                offset += len(block)
            if digest.hexdigest() != row['sha256'] or _identity(row['fd']) != row['identity']:
                raise FileAccessError('checksum_mismatch')
            self._check_upload(row, principal, 'verified')
            operation = {'operation_id': 'operation_' + secrets.token_hex(16), 'tool': 'commit_file_upload',
                         'state': 'running', 'processed_bytes': row['size_bytes'],
                         'total_bytes': row['size_bytes'], 'result_version': None,
                         'updated_at': _stamp(self._wall()), 'error': None}
            row.update(operation=operation, commit_key=idempotency_key)
            self._commit_idempotency[identity] = identifier
            self._operations[operation['operation_id']] = row['id']
            try:
                version = row['resource'].install(principal, row['fd'], overwrite=row['overwrite'],
                    expected_target_version=row['expected_target_version'])
                self._check_upload(row, principal, 'verified')
                assert_file_grant(principal, 'commit_file_upload', row['resource'].root_id,
                                  row['resource'].path, write=True)
                if not isinstance(version, str) or not _VERSION.fullmatch(version):
                    raise FileAccessError('write_backend_unverified')
                operation.update(state='completed', result_version=version)
                self._terminal(row, 'completed')
            except Exception as error:
                operation.update(state='result_unconfirmed', error=FileAccessError('result_unconfirmed').result()['error'])
                self._terminal(row, 'failed', operation['error'])
                if isinstance(error, FileAccessError) and error.code in (
                        'invalid_key', 'permission_denied', 'transfer_expired', 'transfer_unavailable'):
                    # The native call may already have installed its object.
                    # Preserve that uncertainty privately, but discard results
                    # for the no-longer-authorized or expired request.
                    raise
            operation['updated_at'] = _stamp(self._wall())
            self._current(principal)
            return {'ok': operation['state'] == 'completed', 'operation': dict(operation), 'may_wake_disks': True}

    def get_operation(self, operation_id, principal):
        with self._lock:
            identifier = self._operations.get(operation_id) if isinstance(operation_id, str) else None
            if identifier is None:
                raise FileAccessError('transfer_unavailable')
            row = self._owned(identifier, principal, tool='get_file_operation')
            return {'ok': True, 'operation': dict(row['operation'])}

    def close(self):
        """On shutdown, close only owned staging fds; never delete final objects."""
        with self._lock:
            for row in self._sessions.values():
                if row['reader_fd'] is not None:
                    os.close(row['reader_fd'])
                    row['reader_fd'] = None
                    row.update(active=False, state='cancelled', changed=self._clock())
                if row['fd'] is not None:
                    self._terminal(row, 'cancelled')

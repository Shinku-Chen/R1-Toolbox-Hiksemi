"""Real, recoverable owner file mutations with durable no-replay receipts.

The local kernel has no conditional rename-by-inode syscall. Source capture is
therefore a reversible custody step, not a claimed source CAS: a wrong capture
is kept in a private owner-established directory and never indexed or published.
Ordinary copies never mutate their source. No path here permanently deletes a
user object, and a factory notification is never a completion acknowledgement.
"""

import copy
import ctypes
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time

from api_file_operations import FileAccessError, assert_file_grant, _descriptor_mount_id
from api_file_provider import audited_owner_write_rule
from api_file_roots import is_private_recovery_path
from api_file_transfers import (UploadTarget, PendingInstall, UnpublishedInstallError, _binding,
                                _identity, _version, _stamp, _IDEMPOTENCY, _VERSION)
from api_file_mutation_authority import FileMutationAuthority, _read_at, _write_at


TOOLS = frozenset(('create_directory', 'copy_file', 'rename_file', 'move_file', 'trash_file'))
_MAX_SIZE = 8 * 1024 ** 3
_CHUNK = 65536
_MAX_SECONDS = 1800


def _stable(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _content_identity(info):
    """Rename changes ctime; content is separately hashed before/after custody."""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns)


def _object_version(descriptor):
    info = os.fstat(descriptor)
    if stat.S_ISDIR(info.st_mode):
        return _version((info.st_dev, info.st_ino, 0, info.st_mtime_ns, info.st_ctime_ns))
    return _version(_identity(descriptor))


def _rename_new(source_parent, source, target_parent, target):
    """Linux no-clobber rename, with no portable overwrite/rename fallback."""
    if os.name != 'posix':
        raise FileAccessError('write_backend_unverified')
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, 'renameat2', None)
    if function is None:
        raise FileAccessError('write_backend_unverified')
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    result = function(source_parent, os.fsencode(source), target_parent, os.fsencode(target), 1)
    if result:
        import errno
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileAccessError('target_exists')
        if code == errno.ENOENT:
            raise FileAccessError('not_found')
        if code == errno.EXDEV:
            raise FileAccessError('unsupported_precondition')
        raise FileAccessError('write_backend_unverified')


class OperationLedger:
    """Private Toolbox-volume result journal; bindings and paths never project."""

    def __init__(self, authority):
        self.authority, self.rows = authority, None

    def load(self):
        if self.rows is None:
            with self.authority._state() as parent:
                data = _read_at(parent, 'file-mutation-operations.json', {'version': 1, 'operations': {}})
            if (type(data) is not dict or set(data) != {'version', 'operations'}
                    or data['version'] != 1 or type(data['operations']) is not dict
                    or len(data['operations']) > 4096):
                raise FileAccessError('write_backend_unverified')
            self.rows = data['operations']
            recovered = False
            for identifier, row in self.rows.items():
                if (not re.fullmatch(r'operation_[0-9a-f]{32}', identifier, re.ASCII)
                        or type(row) is not dict
                        or set(row) != {'binding', 'idempotency', 'request', 'operation'}
                        or type(row['binding']) is not list or len(row['binding']) != 4
                        or type(row['idempotency']) is not str
                        or not _IDEMPOTENCY.fullmatch(row['idempotency'])
                        or type(row['request']) is not dict or type(row['operation']) is not dict
                        or row['operation'].get('operation_id') != identifier):
                    self.rows = None
                    raise FileAccessError('write_backend_unverified')
                if row['operation']['state'] in ('pending', 'running'):
                    row['operation'].update(state='result_unconfirmed', result_version=None,
                        error=FileAccessError('result_unconfirmed').result()['error'])
                    recovered = True
            if recovered:
                self.save()
        return self.rows

    def save(self):
        with self.authority._state() as parent:
            _write_at(parent, 'file-mutation-operations.json', {'version': 1, 'operations': self.rows})

    def existing(self, principal, key, request):
        binding = list(_binding(principal))
        for row in self.load().values():
            if row['binding'] == binding and row['idempotency'] == key:
                if row['request'] != request:
                    raise FileAccessError('idempotency_conflict')
                return row
        return None

    def start(self, principal, key, request, *, identifier=None, size=None):
        rows = self.load()
        if len(rows) >= 4096:
            raise FileAccessError('limit_exceeded')
        identifier = identifier or 'operation_' + secrets.token_hex(16)
        if identifier in rows:
            raise FileAccessError('idempotency_conflict')
        row = {'binding': list(_binding(principal)), 'idempotency': key,
               'request': copy.deepcopy(request), 'operation': {
                   'operation_id': identifier, 'tool': request['tool'], 'state': 'running',
                   'processed_bytes': 0 if size is not None else None, 'total_bytes': size,
                   'result_version': None, 'updated_at': _stamp(time.time()), 'error': None}}
        rows[identifier] = row
        self.save()  # A crash after this point is unknown; never replay it.
        return row

    def update(self, row, **changes):
        row['operation'].update(changes, updated_at=_stamp(time.time()))
        self.save()


class FileMutationExecutor:
    """Concrete backend, sharing the writer's native gates and descriptor code."""

    def __init__(self, writer, *, authority=None, clock=None, recycle=None):
        self.writer = writer
        self.authority = authority or FileMutationAuthority(writer.panel, root=writer.root)
        self.ledger = OperationLedger(self.authority)
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._pending = {}
        self._recycle = recycle
        self._closed = False
        self._close_error = None

    def _current(self, principal):
        if self._closed or not self.writer._current(principal):
            raise FileAccessError('invalid_key')
        _binding(principal)

    def _grant(self, principal, tool, root_id, path, *, write=True):
        self._current(principal)
        if not audited_owner_write_rule(path):
            raise FileAccessError('unsupported_path_class')
        if (is_private_recovery_path(path) or path == '.DATA_CACHE'
                or path.startswith('.DATA_CACHE/')):
            raise FileAccessError('unsupported_path_class')
        parent, _, name = path.rpartition('/')
        if not name or len(name.encode('utf-8', 'strict')) > 255:
            raise FileAccessError('invalid_arguments')
        assert_file_grant(principal, tool, root_id, path, write=write)
        assert_file_grant(principal, tool, root_id, parent, write=write)
        return parent, name

    def _context(self, principal, tool, root_id, path, *, write=True):
        self._grant(principal, tool, root_id, path, write=write)
        authority = self.writer._authority if write else self.writer._read_authority
        row = authority.authorize(root_id, path,
                                               lambda: self.writer._current(principal))
        self._grant(principal, tool, root_id, path, write=write)
        return row

    def _check_request(self, request, principal, *, query=False):
        tool = request['tool']
        if tool in ('copy_file', 'move_file'):
            locations = [(request['source_root_id'], request['source_path'], tool != 'copy_file'),
                         (request['target_root_id'], request['target_path'], True)]
        else:
            locations = [(request['root_id'], request['path'], True)]
            if tool == 'rename_file':
                locations.append((request['root_id'],
                    (request['path'].rpartition('/')[0] + '/' if '/' in request['path'] else '')
                    + request['new_name'], True))
        for root_id, path, write in locations:
            self._context(principal, tool, root_id, path, write=write)
            if request.get('prepare_tool') == 'prepare_file_overwrite':
                self._context(principal, 'prepare_file_overwrite', root_id, path)
            if query:
                assert_file_grant(principal, 'get_file_operation', root_id, path, write=write)

    @staticmethod
    def _public(row):
        return {'ok': True, 'operation': copy.deepcopy(row['operation']), 'may_wake_disks': True}

    def owns_operation(self, identifier):
        return identifier in self.ledger.load()

    def _expire_pending(self):
        for identifier, pending in list(self._pending.items()):
            if self._clock() >= pending['deadline']:
                row = self.ledger.load()[identifier]
                self.ledger.update(row, state='result_unconfirmed', result_version=None,
                    error=FileAccessError('result_unconfirmed').result()['error'])
                os.close(pending['fd'])
                self._pending.pop(identifier)

    def get_operation(self, identifier, principal):
        with self._lock:
            self._current(principal)
            self._expire_pending()
            row = self.ledger.load().get(identifier)
            if row is None or row['binding'] != list(_binding(principal)):
                raise FileAccessError('transfer_unavailable')
            self._check_request(row['request'], principal, query=True)
            pending = self._pending.get(identifier)
            if pending is not None and row['operation']['state'] == 'pending':
                try:
                    verified = pending['verify'](principal, pending['fd'])
                    self._check_request(row['request'], principal, query=True)
                    if verified is True:
                        self.ledger.update(row, state='completed', result_version=pending['version'])
                        os.close(pending['fd'])
                        self._pending.pop(identifier)
                    elif verified is not False:
                        raise FileAccessError('write_backend_unverified')
                except Exception as error:
                    self.ledger.update(row, state='result_unconfirmed', result_version=None,
                        error=FileAccessError('result_unconfirmed').result()['error'])
                    os.close(pending['fd'])
                    self._pending.pop(identifier)
                    if isinstance(error, FileAccessError) and error.code in ('invalid_key', 'permission_denied'):
                        raise
            self._current(principal)
            return self._public(row)

    def _source(self, principal, tool, root_id, path, version):
        row = self._context(principal, tool, root_id, path, write=tool != 'copy_file')
        handle = self.writer._handle(self.writer.root, row, path, reader=True)
        try:
            if _version(_identity(handle.fd)) != version:
                raise FileAccessError('file_changed')
            return row, handle
        except Exception:
            handle.close()
            raise

    def _verify_source(self, principal, tool, root_id, path, row, descriptor, identity):
        current_row = self._context(principal, tool, root_id, path, write=tool != 'copy_file')
        if current_row != row or _identity(descriptor) != identity:
            raise FileAccessError('file_changed')
        with self.writer._handle(self.writer.root, row, path) as current:
            if _identity(current.fd) != identity:
                raise FileAccessError('file_changed')
        self._current(principal)
        if _identity(descriptor) != identity:
            raise FileAccessError('file_changed')

    def _hash(self, descriptor, gate, *, deadline):
        identity = _identity(descriptor)
        digest, offset = hashlib.sha256(), 0
        while offset < identity[2]:
            if self._clock() >= deadline:
                raise FileAccessError('timeout')
            gate()
            block = os.pread(descriptor, min(_CHUNK, identity[2] - offset), offset)
            if not block:
                raise FileAccessError('file_changed')
            gate()
            digest.update(block)
            offset += len(block)
        gate()
        if self._clock() >= deadline:
            raise FileAccessError('timeout')
        if _identity(descriptor) != identity:
            raise FileAccessError('file_changed')
        return digest.hexdigest()

    def _copy(self, args, principal, operation):
        tool = 'copy_file'
        source_row, source = self._source(principal, tool, args['source_root_id'],
            args['source_path'], args['expected_source_version'])
        descriptor = None
        published = install_started = False
        try:
            identity = _identity(source.fd)
            target_args = {'root_id': args['target_root_id'],
                           'path': args['target_path'], 'size_bytes': identity[2]}
            target = self.writer._target(principal, target_args,
                prepare_tool=tool, commit_tool=tool)
            descriptor = target.create_staging(principal)
            deadline, offset, digest = self._clock() + _MAX_SECONDS, 0, hashlib.sha256()
            def gate():
                self._verify_source(principal, tool, args['source_root_id'], args['source_path'],
                                    source_row, source.fd, identity)
                if target.authorize_opened(principal, descriptor, write=True) is not True:
                    raise FileAccessError('acl_unverified')
            while offset < identity[2]:
                if self._clock() >= deadline:
                    raise FileAccessError('timeout')
                gate()
                block = os.pread(source.fd, min(_CHUNK, identity[2] - offset), offset)
                if not block:
                    raise FileAccessError('file_changed')
                gate()
                if os.pwrite(descriptor, block, offset) != len(block):
                    raise FileAccessError('limit_exceeded')
                digest.update(block)
                offset += len(block)
            gate()
            os.fsync(descriptor)
            gate()
            self.ledger.update(operation, processed_bytes=offset, total_bytes=offset)
            target_args['sha256'] = digest.hexdigest()
            install_started = True
            receipt = target.install(principal, descriptor, overwrite=False, expected_target_version=None,
                                     operation_deadline=deadline, operation_clock=self._clock)
            published = True
            if not isinstance(receipt, PendingInstall):
                raise FileAccessError('write_backend_unverified')
            self._pending[operation['operation']['operation_id']] = {
                'fd': descriptor, 'verify': receipt.verify, 'version': receipt.version,
                'deadline': self._clock() + _MAX_SECONDS}
            descriptor = None
            self.ledger.update(operation, state='pending')
        except Exception as error:
            if (published or install_started and not isinstance(error, UnpublishedInstallError)
                    or isinstance(error, FileAccessError) and error.code == 'result_unconfirmed'):
                raise FileAccessError('result_unconfirmed') from None
            if isinstance(error, UnpublishedInstallError):
                raise FileAccessError(error.code) from None
            raise
        finally:
            source.close()
            if descriptor is not None:
                os.close(descriptor)

    def _custody(self, args, principal, operation):
        deadline = self._clock() + _MAX_SECONDS
        tool = args['tool']
        if tool == 'rename_file':
            source_root = target_root = args['root_id']
            source_path = args['path']
            target_path = (source_path.rpartition('/')[0] + '/' if '/' in source_path else '') + args['new_name']
        else:
            source_root, source_path = args['source_root_id'], args['source_path']
            target_root, target_path = args['target_root_id'], args['target_path']
        source_row, source = self._source(principal, tool, source_root, source_path,
                                         args['expected_source_version'])
        captured = False
        final_fd = None
        try:
            target_row = self._context(principal, tool, target_root, target_path)
            if source_row != target_row:
                raise FileAccessError('unsupported_precondition')
            source_parent, source_name = self._grant(principal, tool, source_root, source_path)
            target_parent, target_name = self._grant(principal, tool, target_root, target_path)
            bridge = self.writer._bridge()
            if not all(callable(getattr(bridge, n, None)) for n in
                       ('preflight_mutation', 'notify_mutation', 'verify_mutation')):
                raise FileAccessError('write_backend_unverified')
            current = lambda: self.writer._current(principal)
            prepared = bridge.preflight_mutation(source_row, source_path, target_path,
                                      operation=tool.removesuffix('_file'), current=current)
            with self.writer._handle(self.writer.root, source_row, source_parent, directory=True) as sp, \
                 self.writer._handle(self.writer.root, target_row, target_parent, directory=True) as tp, \
                 self.authority.open(source_row, operation['operation']['operation_id'], current=current) as stage:
                if sp.mount_id != tp.mount_id or os.fstat(sp.fd).st_dev != os.fstat(tp.fd).st_dev:
                    raise FileAccessError('unsupported_precondition')
                parent_ids = _stable(os.fstat(sp.fd)), _stable(os.fstat(tp.fd))
                identity = _identity(source.fd)
                def source_gate():
                    self._verify_source(principal, tool, source_root, source_path,
                                        source_row, source.fd, identity)
                digest = self._hash(source.fd, source_gate, deadline=deadline)
                manifest = {'version': 1, 'state': 'capturing', 'tool': tool,
                    'source_path': source_path, 'target_path': target_path,
                    'source_identity': list(identity), 'source_sha256': digest,
                    'result_version': None, 'restore_path': source_path,
                    'custody_name': 'source', 'custody_identity': None}
                stage.record(manifest, current=current)
                source_gate()
                self._check_request(args, principal)
                stage.verify(current)
                if self._clock() >= deadline:
                    raise FileAccessError('timeout')
                _rename_new(sp.fd, source_name, stage.fd, 'source')
                captured = True
                os.fsync(sp.fd)
                os.fsync(stage.fd)
                captured_info = os.stat('source', dir_fd=stage.fd, follow_symlinks=False)
                manifest.update(state='captured', custody_name='source', restore_path=source_path,
                                custody_identity=list(_stable(captured_info)))
                stage.record_recovery(manifest)
                if (_content_identity(captured_info) != _content_identity(os.fstat(source.fd))
                        or _stable(captured_info) != identity[:2] + (stat.S_IFREG,)):
                    raise FileAccessError('result_unconfirmed')
                if (captured_info.st_size, captured_info.st_mtime_ns) != (identity[2], identity[3]):
                    raise FileAccessError('result_unconfirmed')
                def custody_gate():
                    self._check_request(args, principal)
                    stage.verify(current)
                    named = os.stat('source', dir_fd=stage.fd, follow_symlinks=False)
                    if _content_identity(named) != _content_identity(os.fstat(source.fd)):
                        raise FileAccessError('file_changed')
                if self._hash(source.fd, custody_gate, deadline=deadline) != digest:
                    raise FileAccessError('result_unconfirmed')
                # Parent replacement is rejected before publishing into an old,
                # detached parent descriptor or an unexpected replacement tree.
                for path, held, expected in ((source_parent, sp, parent_ids[0]),
                                              (target_parent, tp, parent_ids[1])):
                    with self.writer._handle(self.writer.root, source_row, path, directory=True) as opened:
                        if _stable(os.fstat(opened.fd)) != expected or opened.mount_id != held.mount_id:
                            raise FileAccessError('file_changed')
                custody_gate()
                manifest.update(state='publish_intent', published_path=target_path,
                    published_identity=list(_stable(os.fstat(source.fd))), published_sha256=digest)
                stage.record(manifest, current=current)
                _rename_new(stage.fd, 'source', tp.fd, target_name)
                manifest['state'] = 'published_unconfirmed'
                stage.record_recovery(manifest)
                os.fsync(stage.fd)
                os.fsync(tp.fd)
                self._verify_final(args, principal, target_row, target_path, source.fd)
                published_identity = _identity(source.fd)
                def final_gate():
                    self._verify_final(args, principal, target_row, target_path,
                                       source.fd, published_identity)
                if self._hash(source.fd, final_gate, deadline=deadline) != digest:
                    raise FileAccessError('result_unconfirmed')
                # The original source may have been recreated by a third party.
                # Its presence is a conflict, not permission to delete it.
                try:
                    os.stat(source_name, dir_fd=sp.fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise FileAccessError('result_unconfirmed')
                native_operation = tool.removesuffix('_file')
                receipt = bridge.notify_mutation(source_row, source_path, target_path, source.fd,
                    operation=native_operation, current=current, prepared=prepared)
                identity_after = _identity(source.fd)
                version = _version(identity_after)
                manifest.update(state='pending', result_version=version)
                stage.record(manifest, current=current)
                def verify(key, descriptor):
                    self._verify_final(args, key, target_row, target_path, descriptor, identity_after)
                    result = bridge.verify_mutation(receipt, source_row, source_path,
                        target_path, descriptor, operation=native_operation,
                        current=lambda: self.writer._current(key))
                    self._verify_final(args, key, target_row, target_path, descriptor, identity_after)
                    if result is True:
                        # A completed ordinary rename is not silently exposed
                        # as an unconfirmed recovery candidate. Owner recovery
                        # and this state transition serialize on one registry.
                        with self.authority._lock, self.authority.open(source_row,
                                operation['operation']['operation_id'], create=False,
                                current=lambda: self.writer._current(key)) as private:
                            saved = _read_at(private.fd, 'manifest.json')
                            if saved.get('restore_receipt') is not None:
                                raise FileAccessError('file_changed')
                            saved['state'] = 'completed'
                            private.record(saved, current=lambda: self.writer._current(key))
                    return result
                final_fd = os.dup(source.fd)
                self._pending[operation['operation']['operation_id']] = {
                    'fd': final_fd, 'verify': verify, 'version': version,
                    'deadline': self._clock() + _MAX_SECONDS}
                final_fd = None
                self.ledger.update(operation, state='pending')
        except Exception:
            if captured:
                raise FileAccessError('result_unconfirmed') from None
            raise
        finally:
            source.close()
            if final_fd is not None:
                os.close(final_fd)

    def _verify_final(self, args, principal, row, path, descriptor, expected=None, *, directory=False):
        self._check_request(args, principal)
        if expected is not None and _identity(descriptor) != expected:
            raise FileAccessError('file_changed')
        with self.writer._handle(self.writer.root, row, path, directory=directory) as final:
            before, after = os.fstat(descriptor), os.fstat(final.fd)
            if (_stable(before) != _stable(after) or not directory
                    and _identity(descriptor) != _identity(final.fd)):
                raise FileAccessError('file_changed')
        self._current(principal)

    def _mkdir(self, args, principal, operation):
        row = self._context(principal, 'create_directory', args['root_id'], args['path'])
        parent, name = self._grant(principal, 'create_directory', args['root_id'], args['path'])
        bridge, current = self.writer._bridge(), lambda: self.writer._current(principal)
        bridge.preflight(row, args['path'], current=current)
        descriptor = None
        published = False
        try:
            with self.writer._handle(self.writer.root, row, parent, directory=True) as opened, \
                 self.authority.open(row, operation['operation']['operation_id'], current=current) as stage:
                parent_identity = _stable(os.fstat(opened.fd))
                self.writer._capacity(opened.fd, 0)
                stage.record({'version': 1, 'state': 'creating', 'tool': 'create_directory',
                    'target_path': args['path'], 'restore_path': args['path'],
                    'custody_name': 'directory', 'custody_identity': None}, current=current)
                self._check_request(args, principal)
                stage.verify(current)
                os.mkdir('directory', 0o700, dir_fd=stage.fd)
                descriptor = os.open('directory', os.O_RDONLY | os.O_DIRECTORY
                    | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=stage.fd)
                birth = _stable(os.fstat(descriptor))
                manifest = {'version': 1, 'state': 'created_private', 'tool': 'create_directory',
                    'target_path': args['path'], 'restore_path': args['path'],
                    'custody_name': 'directory', 'custody_identity': list(birth)}
                stage.record_recovery(manifest)
                if _descriptor_mount_id(self.writer.root, descriptor) != opened.mount_id:
                    raise FileAccessError('root_unavailable')
                self._check_request(args, principal)
                with self.writer._handle(self.writer.root, row, parent, directory=True) as check:
                    if _stable(os.fstat(check.fd)) != parent_identity:
                        raise FileAccessError('file_changed')
                stage.verify(current)
                if _stable(os.stat('directory', dir_fd=stage.fd, follow_symlinks=False)) != birth:
                    raise FileAccessError('file_changed')
                _rename_new(stage.fd, 'directory', opened.fd, name)
                published = True
                os.fsync(stage.fd)
                os.fsync(opened.fd)
                self._verify_final(args, principal, row, args['path'], descriptor, directory=True)
                receipt = bridge.notify_new(row, args['path'], descriptor, directory=True, current=current)
                version = _object_version(descriptor)
                manifest.update(state='pending', result_version=version)
                stage.record_recovery(manifest)
                def verify(key, fd):
                    self._verify_final(args, key, row, args['path'], fd, directory=True)
                    result = bridge.verify_new(receipt, row, args['path'], fd, directory=True,
                                              current=lambda: self.writer._current(key))
                    self._verify_final(args, key, row, args['path'], fd, directory=True)
                    return result
                self._pending[operation['operation']['operation_id']] = {
                    'fd': descriptor, 'verify': verify, 'version': version,
                    'deadline': self._clock() + _MAX_SECONDS}
                descriptor = None
                self.ledger.update(operation, state='pending')
        except Exception:
            if published:
                raise FileAccessError('result_unconfirmed') from None
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _trash(self, args, principal, operation):
        """Capture and prove first; only a fixed native archive can recycle it."""
        if self._recycle is None:
            try:
                from api_file_native_recycle import NativeFileRecycle
                self._recycle = NativeFileRecycle(root=self.writer.root)
            except ImportError:
                raise FileAccessError('write_backend_unverified') from None
        backend = self._recycle
        if not all(callable(getattr(backend, name, None)) for name in
                   ('preflight', 'archive_captured', 'verify')):
            raise FileAccessError('write_backend_unverified')
        tool, root_id, path = 'trash_file', args['root_id'], args['path']
        row, source = self._source(principal, tool, root_id, path, args['expected_source_version'])
        captured = False
        final_fd = None
        deadline = self._clock() + _MAX_SECONDS
        current = lambda: self.writer._current(principal)
        try:
            plan = backend.preflight(row, path, current=current)
            parent, name = self._grant(principal, tool, root_id, path)
            with self.writer._handle(self.writer.root, row, parent, directory=True) as opened, \
                 self.authority.open(row, operation['operation']['operation_id'], current=current) as stage:
                parent_identity = _stable(os.fstat(opened.fd))
                identity = _identity(source.fd)
                def source_gate():
                    self._verify_source(principal, tool, root_id, path, row, source.fd, identity)
                digest = self._hash(source.fd, source_gate, deadline=deadline)
                manifest = {'version': 1, 'state': 'capturing', 'tool': tool,
                            'source_path': path, 'restore_path': path,
                            'source_identity': list(identity), 'source_sha256': digest,
                            'custody_name': 'source', 'custody_identity': None}
                stage.record(manifest, current=current)
                source_gate()
                stage.verify(current)
                if self._clock() >= deadline:
                    raise FileAccessError('timeout')
                _rename_new(opened.fd, name, stage.fd, 'source')
                captured = True
                os.fsync(opened.fd)
                os.fsync(stage.fd)
                actual = os.stat('source', dir_fd=stage.fd, follow_symlinks=False)
                manifest.update(state='captured', custody_name='source',
                                custody_identity=list(_stable(actual)))
                stage.record_recovery(manifest)
                if (_stable(actual) != (identity[0], identity[1], stat.S_IFREG)
                        or (actual.st_size, actual.st_mtime_ns) != (identity[2], identity[3])):
                    raise FileAccessError('result_unconfirmed')
                def custody_gate():
                    self._check_request(args, principal)
                    stage.verify(current)
                    actual = os.stat('source', dir_fd=stage.fd, follow_symlinks=False)
                    if _content_identity(actual) != _content_identity(os.fstat(source.fd)):
                        raise FileAccessError('file_changed')
                if self._hash(source.fd, custody_gate, deadline=deadline) != digest:
                    raise FileAccessError('result_unconfirmed')
                with self.writer._handle(self.writer.root, row, parent, directory=True) as check:
                    if _stable(os.fstat(check.fd)) != parent_identity:
                        raise FileAccessError('file_changed')
                custody_gate()
                receipt = backend.archive_captured(plan, row, path, source.fd, stage, current=current)
                self._check_request(args, principal)
                archived_identity = _identity(source.fd)
                def archive_gate():
                    self._check_request(args, principal)
                    if _identity(source.fd) != archived_identity:
                        raise FileAccessError('file_changed')
                if self._hash(source.fd, archive_gate, deadline=deadline) != digest:
                    raise FileAccessError('result_unconfirmed')
                version = _object_version(source.fd)
                def verify(key, descriptor):
                    verify_deadline = self._clock() + _MAX_SECONDS
                    def verify_gate():
                        self._check_request(args, key)
                        if _identity(descriptor) != archived_identity:
                            raise FileAccessError('file_changed')
                    if self._hash(descriptor, verify_gate, deadline=verify_deadline) != digest:
                        raise FileAccessError('file_changed')
                    accepted = backend.verify(receipt, row, path, descriptor,
                                              current=lambda: self.writer._current(key))
                    if self._hash(descriptor, verify_gate, deadline=verify_deadline) != digest:
                        raise FileAccessError('file_changed')
                    return accepted
                final_fd = os.dup(source.fd)
                self._pending[operation['operation']['operation_id']] = {
                    'fd': final_fd, 'verify': verify, 'version': version,
                    'deadline': self._clock() + _MAX_SECONDS}
                final_fd = None
                self.ledger.update(operation, state='pending')
        except Exception:
            if captured:
                raise FileAccessError('result_unconfirmed') from None
            raise
        finally:
            source.close()
            if final_fd is not None:
                os.close(final_fd)

    def replacement_target(self, principal, args):
        """Separate exact-tool upload capability; old upload grants never expand."""
        tool, root_id, path = 'prepare_file_overwrite', args['root_id'], args['path']
        row = self._context(principal, tool, root_id, path)
        parent, name = self._grant(principal, tool, root_id, path)
        self.authority._check_registered(row)
        bridge = self.writer._bridge()
        if not all(callable(getattr(bridge, n, None)) for n in
                   ('preflight_mutation', 'notify_mutation', 'verify_mutation')):
            raise FileAccessError('write_backend_unverified')
        expected = args['expected_target_version']
        with self.writer._handle(self.writer.root, row, path, reader=True) as target, \
             self.writer._handle(self.writer.root, row, parent, directory=True) as opened:
            if _version(_identity(target.fd)) != expected:
                raise FileAccessError('file_changed')
            parent_identity, mount_id = _stable(os.fstat(opened.fd)), opened.mount_id
            self.writer._capacity(opened.fd, args['size_bytes'])
        state = {'inode': None, 'published': False, 'final': None}
        def parent_handle(key, exact_tool=tool):
            if self._context(key, exact_tool, root_id, path) != row:
                raise FileAccessError('root_unavailable')
            opened = self.writer._handle(self.writer.root, row, parent, directory=True)
            try:
                if (_stable(os.fstat(opened.fd)) != parent_identity or opened.mount_id != mount_id):
                    raise FileAccessError('file_changed')
                return opened
            except Exception:
                opened.close()
                raise
        def authorize(key, descriptor, *, write=False):
            if write is not True:
                return False
            self._current(key)
            ensure_deadline()
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or _stable(info) != state['inode']
                    or info.st_nlink != (1 if state['published'] else 0)
                    or _descriptor_mount_id(self.writer.root, descriptor) != mount_id):
                raise FileAccessError('file_changed')
            with parent_handle(key) as opened:
                self.writer._capacity(opened.fd, max(0, args['size_bytes'] - info.st_size))
                with self.writer._handle(self.writer.root, row, path, reader=True) as target:
                    if state['published']:
                        if _identity(target.fd) != state['final'] or _identity(descriptor) != state['final']:
                            raise FileAccessError('file_changed')
                    elif _version(_identity(target.fd)) != expected:
                        raise FileAccessError('file_changed')
            return self.writer._current(key)
        def ensure_deadline():
            if state.get('operation_deadline') is not None \
                    and state['operation_clock']() >= state['operation_deadline']:
                raise FileAccessError('transfer_expired')
        def create(key):
            descriptor = None
            try:
                if os.name != 'posix' or not hasattr(os, 'O_TMPFILE'):
                    raise FileAccessError('write_backend_unverified')
                with parent_handle(key) as opened:
                    bridge.preflight_mutation(row, path, path, operation='replace',
                                              current=lambda: self.writer._current(key))
                    descriptor = os.open('.', os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC
                        | os.O_NOFOLLOW, 0o600, dir_fd=opened.fd)
                    state['inode'] = _stable(os.fstat(descriptor))
                    authorize(key, descriptor, write=True)
                    return descriptor
            except Exception:
                if descriptor is not None:
                    os.close(descriptor)
                raise
        def install(key, descriptor, *, overwrite, expected_target_version,
                    operation_id, idempotency_key, operation_deadline, operation_clock):
            if overwrite is not True or expected_target_version != expected:
                raise UnpublishedInstallError('invalid_arguments')
            state.update(operation_deadline=operation_deadline, operation_clock=operation_clock)
            authorize(key, descriptor, write=True)
            request = {'tool': 'commit_file_upload', 'prepare_tool': tool,
                       'root_id': root_id, 'path': path, 'expected_target_version': expected,
                       'sha256': args['sha256'], 'size_bytes': args['size_bytes']}
            with self._lock:
                self._check_request(request, key)
                prior = self.ledger.existing(key, idempotency_key, request)
                if prior is not None:
                    raise FileAccessError('result_unconfirmed')
                operation = self.ledger.start(key, idempotency_key, request,
                                             identifier=operation_id, size=args['size_bytes'])
                captured = False
                try:
                    deadline = self._clock() + _MAX_SECONDS
                    with parent_handle(key, 'commit_file_upload') as opened, \
                         self.writer._handle(self.writer.root, row, path, reader=True) as old, \
                         self.authority.open(row, operation_id,
                             current=lambda: self.writer._current(key)) as stage:
                        identity = _identity(old.fd)
                        if _version(identity) != expected:
                            raise FileAccessError('file_changed')
                        current = lambda: self.writer._current(key)
                        prepared = bridge.preflight_mutation(row, path, path,
                            operation='replace', current=current)
                        def old_gate():
                            authorize(key, descriptor, write=True)
                            self._verify_source(key, tool, root_id, path, row, old.fd, identity)
                        digest = self._hash(old.fd, old_gate, deadline=deadline)
                        manifest = {'version': 1, 'state': 'capturing', 'tool': 'prepare_file_overwrite',
                                    'restore_path': path, 'target_path': path,
                                    'source_identity': list(identity), 'source_sha256': digest,
                                    'custody_name': 'previous', 'custody_identity': None}
                        stage.record(manifest, current=current)
                        old_gate()
                        self._check_request(request, key)
                        stage.verify(current)
                        if self._clock() >= deadline:
                            raise FileAccessError('timeout')
                        _rename_new(opened.fd, name, stage.fd, 'previous')
                        captured = True
                        os.fsync(opened.fd)
                        os.fsync(stage.fd)
                        actual = os.stat('previous', dir_fd=stage.fd, follow_symlinks=False)
                        manifest.update(state='captured', custody_name='previous',
                                        custody_identity=list(_stable(actual)))
                        stage.record_recovery(manifest)
                        if (_stable(actual) != (identity[0], identity[1], stat.S_IFREG)
                                or (actual.st_size, actual.st_mtime_ns) != (identity[2], identity[3])):
                            raise FileAccessError('result_unconfirmed')
                        def old_custody():
                            self._check_request(request, key)
                            ensure_deadline()
                            stage.verify(current)
                            named = os.stat('previous', dir_fd=stage.fd, follow_symlinks=False)
                            if _content_identity(named) != _content_identity(os.fstat(old.fd)):
                                raise FileAccessError('file_changed')
                        if self._hash(old.fd, old_custody, deadline=deadline) != digest:
                            raise FileAccessError('result_unconfirmed')
                        with parent_handle(key, 'commit_file_upload'):
                            pass
                        old_custody()
                        from api_file_write_backend import _link_new
                        _link_new(descriptor, opened.fd, name)
                        state.update(published=True, final=_identity(descriptor))
                        os.fsync(opened.fd)
                        authorize(key, descriptor, write=True)
                        if self._hash(descriptor, lambda: authorize(key, descriptor, write=True),
                                      deadline=deadline) != args['sha256']:
                            raise FileAccessError('result_unconfirmed')
                        receipt = bridge.notify_mutation(row, path, path, descriptor,
                            operation='replace', current=current, prepared=prepared)
                        state['final'] = _identity(descriptor)
                        authorize(key, descriptor, write=True)
                        version = _version(state['final'])
                        manifest.update(state='pending', result_version=version)
                        stage.record_recovery(manifest)
                        self.ledger.update(operation, state='pending', processed_bytes=args['size_bytes'])
                    def verify(key, fd):
                        try:
                            authorize(key, fd, write=True)
                            accepted = bridge.verify_mutation(receipt, row, path, path, fd,
                                operation='replace', current=lambda: self.writer._current(key))
                            authorize(key, fd, write=True)
                            if accepted is True:
                                self.ledger.update(operation, state='completed', result_version=version)
                            return accepted
                        except Exception:
                            self.ledger.update(operation, state='result_unconfirmed', result_version=None,
                                error=FileAccessError('result_unconfirmed').result()['error'])
                            raise
                    return PendingInstall(version, verify)
                except Exception as error:
                    failure = FileAccessError('result_unconfirmed') if captured else (
                        error if isinstance(error, FileAccessError) else FileAccessError('write_backend_unverified'))
                    self.ledger.update(operation, state='result_unconfirmed' if captured else 'failed',
                                       error=failure.result()['error'])
                    if captured:
                        raise failure from None
                    raise UnpublishedInstallError(failure.code) from None
        return UploadTarget(root_id, path, name, create, authorize, install,
                            prepare_tool=tool, operation_context=True)

    def execute(self, tool, args, principal):
        with self._lock:
            self._current(principal)
            self._expire_pending()
            if tool not in TOOLS or type(args) is not dict:
                raise FileAccessError('invalid_arguments')
            fields = {'root_id', 'path', 'idempotency_key'}
            if tool in ('copy_file', 'move_file'):
                fields = {'source_root_id', 'source_path', 'target_root_id', 'target_path',
                          'expected_source_version', 'idempotency_key'}
            elif tool == 'rename_file':
                fields |= {'new_name', 'expected_source_version'}
            elif tool == 'trash_file':
                fields |= {'expected_source_version', 'confirmed'}
            if set(args) != fields or not isinstance(args.get('idempotency_key'), str) \
                    or not _IDEMPOTENCY.fullmatch(args['idempotency_key']):
                raise FileAccessError('invalid_arguments')
            if tool != 'create_directory' and (not isinstance(args['expected_source_version'], str)
                    or not _VERSION.fullmatch(args['expected_source_version'])):
                raise FileAccessError('invalid_arguments')
            if tool == 'rename_file':
                from api_file_transfers import _name
                if not _name(args['new_name']):
                    raise FileAccessError('invalid_arguments')
            if tool == 'trash_file' and args['confirmed'] is not True:
                raise FileAccessError('invalid_arguments')
            request = {'tool': tool, **copy.deepcopy(args)}
            self._check_request(request, principal)
            if (tool in ('copy_file', 'move_file') and args['source_root_id'] == args['target_root_id']
                    and args['source_path'] == args['target_path'] or tool == 'rename_file'
                    and args['path'].rsplit('/', 1)[-1] == args['new_name']):
                raise FileAccessError('invalid_arguments')
            previous = self.ledger.existing(principal, args['idempotency_key'], request)
            if previous is not None:
                return self._public(previous)
            binding = list(_binding(principal))
            if (len(self._pending) >= 4 or sum(self.ledger.load()[identifier]['binding'][0] == binding[0]
                    for identifier in self._pending) >= 2):
                raise FileAccessError('transfer_busy')
            row = self.ledger.start(principal, args['idempotency_key'], request)
            try:
                if tool == 'copy_file':
                    self._copy(request, principal, row)
                elif tool == 'create_directory':
                    self._mkdir(request, principal, row)
                elif tool in ('rename_file', 'move_file'):
                    self._custody(request, principal, row)
                else:
                    self._trash(request, principal, row)
                self._check_request(request, principal)
            except Exception as error:
                failure = error if isinstance(error, FileAccessError) else FileAccessError('result_unconfirmed')
                unknown = failure.code == 'result_unconfirmed'
                self.ledger.update(row, state='result_unconfirmed' if unknown else 'failed',
                                   error=failure.result()['error'])
                if failure.code in ('invalid_key', 'permission_denied'):
                    raise failure
            self._current(principal)
            return self._public(row)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending_rows = list(self._pending.values())
            self._pending.clear()
            # Detach owned descriptors before closing: a failed close must not
            # later close an unrelated descriptor that reused the same number.
            first_error = None
            for pending in pending_rows:
                try:
                    os.close(pending['fd'])
                except Exception as error:
                    if first_error is None:
                        first_error = error
            if self.ledger.rows is not None:
                for row in self.ledger.rows.values():
                    if row['operation']['state'] in ('pending', 'running'):
                        row['operation'].update(state='result_unconfirmed', result_version=None,
                            error=FileAccessError('result_unconfirmed').result()['error'])
                try:
                    self.ledger.save()
                except Exception as error:
                    if first_error is None:
                        first_error = error
            self._close_error = first_error
            if first_error is not None:
                raise first_error

"""Owner file writes: anonymous fd staging, custody and verified native index.

No filesystem mutation is authorized by a root plan or an input proof flag.
The exact factory owner's audited write rule, current key, parent descriptor,
mount identity and native indexing bridge are independent mandatory gates.
"""

import hashlib
import os
import stat
import time

from api_file_operations import FileAccessError, assert_file_grant, _descriptor_mount_id
from api_file_provider import NativeOwnerReadAuthority, NativeOwnerWriteAuthority
from api_file_roots import valid_relative_file_path
from api_file_transfers import (UploadTarget, PendingInstall, UnpublishedInstallError,
                                _identity, _version, _SHA, _IDEMPOTENCY, _VERSION)


TOOLS = frozenset(('prepare_file_upload', 'prepare_file_overwrite', 'commit_file_upload',
                   'get_file_operation', 'create_directory', 'copy_file', 'rename_file',
                   'move_file', 'trash_file'))
_RESERVE = 5 * 1024 ** 3


def _stable(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _capacity(descriptor, remaining_bytes):
    """Audited statfs reserve, conservatively applied to every admitted disk.

    v260907 0x59e010 uses bsize*(blocks-bfree); 0x1274dc preserves the smaller
    of total/1000 and 50 MiB, and built-in storage additionally needs >5 GiB.
    Use the same verified fd, require unambiguous block sizes, and preserve
    both reserves even on external storage. This is not a per-user quota API.
    """
    if not hasattr(os, 'fstatvfs'):
        raise FileAccessError('write_backend_unverified')
    value = os.fstatvfs(descriptor)
    fields = (value.f_bsize, value.f_frsize, value.f_blocks, value.f_bfree, value.f_bavail)
    if (any(type(v) is not int or v < 0 for v in fields)
            or value.f_bsize <= 0 or value.f_bsize != value.f_frsize
            or value.f_bfree > value.f_blocks or value.f_bavail > value.f_bfree):
        raise FileAccessError('write_backend_unverified')
    total = value.f_bsize * value.f_blocks
    free = value.f_bsize * value.f_bavail
    reserve = _RESERVE + min(total // 1000, 50 * 1024 ** 2)
    if free <= remaining_bytes + reserve:
        raise FileAccessError('limit_exceeded')


def _link_new(descriptor, parent, name):
    """Publish only this owned anonymous inode; the kernel rejects any target.

    This is the documented O_TMPFILE linkat method for callers without
    CAP_DAC_READ_SEARCH. Its source is our owned fd, never user text.
    No source pathname, temporary visible filename or rename fallback exists.
    """
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 0:
        raise FileAccessError('write_backend_unverified')
    try:
        # Supplying dst_dir_fd uses linkat; following this one owned kernel fd
        # is required for O_TMPFILE and never follows a user-controlled link.
        os.link('/proc/self/fd/' + str(descriptor), name,
                dst_dir_fd=parent, follow_symlinks=True)
    except FileExistsError:
        raise FileAccessError('target_exists') from None
    except OSError:
        raise FileAccessError('write_backend_unverified') from None
    after = os.fstat(descriptor)
    if _stable(before) != _stable(after) or after.st_nlink != 1:
        raise FileAccessError('result_unconfirmed')


class FileWriteExecutor:
    """Concrete new-file tools sharing FileReadExecutor's transfer manager.

    Test seams are trusted constructor dependencies only. The HTTP/Token layer
    cannot provide an authority, handle factory, index bridge or capacity rule.
    NativeFileIndex must preflight before staging and independently confirm the
    installed object's native index before an operation becomes completed.
    """

    def __init__(self, panel, *, authorizer, transfers, root='/', authority=None,
                 handle_factory=None, index=None, capacity=None, mutation_authority=None,
                 read_authority=None):
        self.panel, self.root, self.transfers = panel, root, transfers
        self._authorizer = authorizer
        self._authority = authority or NativeOwnerWriteAuthority(panel, root=root)
        self._read_authority = read_authority or (authority if authority is not None else
                                                  NativeOwnerReadAuthority(panel, root=root))
        if handle_factory is None:
            from api_file_executor import _Handle
            handle_factory = _Handle
        self._handle = handle_factory
        self._index = index
        self._capacity = capacity or _capacity
        self._closed = False
        from api_file_mutations import FileMutationExecutor
        self.mutations = FileMutationExecutor(self, authority=mutation_authority)
        self.mutation_authority = self.mutations.authority

    def _current(self, principal):
        try:
            return not self._closed and self._authorizer(principal) is True
        except Exception:
            return False

    def _grant(self, principal, tool, root_id, path):
        if not self._current(principal):
            raise FileAccessError('invalid_key')
        parent, separator, name = path.rpartition('/') if type(path) is str else ('', '', '')
        if (not valid_relative_file_path(path) or not name
                or len(name.encode('utf-8', 'strict')) > 255):
            raise FileAccessError('invalid_arguments')
        if path == '.DATA_CACHE' or path.startswith('.DATA_CACHE/'):
            # Fixed factory bookkeeping is not a user-file write surface.
            # Only NativeFileRecycle has a narrow, audited internal exception.
            raise FileAccessError('unsupported_path_class')
        assert_file_grant(principal, tool, root_id, path, write=True)
        assert_file_grant(principal, tool, root_id, parent, write=True)
        return parent, name

    def _root(self, principal, tool, root_id, path):
        self._grant(principal, tool, root_id, path)
        row = self._authority.authorize(root_id, path, lambda: self._current(principal))
        self._grant(principal, tool, root_id, path)
        return row

    def _bridge(self):
        if self._index is None:
            try:
                from api_file_native_index import NativeFileIndex
                self._index = NativeFileIndex(root=self.root)
            except ImportError:
                raise FileAccessError('write_backend_unverified') from None
        if not all(callable(getattr(self._index, method, None))
                   for method in ('preflight', 'notify_new', 'verify_new')):
            raise FileAccessError('write_backend_unverified')
        return self._index

    def _target(self, principal, args, *, prepare_tool='prepare_file_upload',
                commit_tool='commit_file_upload'):
        root_id, path, size = args['root_id'], args['path'], args['size_bytes']
        row = self._root(principal, prepare_tool, root_id, path)
        parent, name = self._grant(principal, prepare_tool, root_id, path)
        bridge = self._bridge()
        current = lambda: self._current(principal)
        bridge.preflight(row, path, current=current)
        state = {'staging': None, 'published': False, 'final': None}
        with self._handle(self.root, row, parent, directory=True) as opened:
            parent_identity, mount_id = _stable(os.fstat(opened.fd)), opened.mount_id
            self._capacity(opened.fd, size)

        def verify_parent(key, tool):
            current_row = self._root(key, tool, root_id, path)
            if current_row != row:
                raise FileAccessError('root_unavailable')
            opened = self._handle(self.root, current_row, parent, directory=True)
            try:
                if (_stable(os.fstat(opened.fd)) != parent_identity or opened.mount_id != mount_id):
                    raise FileAccessError('file_changed')
                return opened
            except Exception:
                opened.close()
                raise

        def authorize_opened(key, descriptor, *, write=False):
            if write is not True:
                return False
            if state.get('operation_deadline') is not None \
                    and state['operation_clock']() >= state['operation_deadline']:
                raise FileAccessError('transfer_expired')
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or state['staging'] != _stable(info)
                    or _descriptor_mount_id(self.root, descriptor) != mount_id
                    or info.st_nlink != (1 if state['published'] else 0)):
                raise FileAccessError('file_changed')
            with verify_parent(key, prepare_tool) as opened:
                self._capacity(opened.fd, max(0, size - info.st_size))
                if state['published']:
                    if _identity(descriptor) != state['final']:
                        raise FileAccessError('file_changed')
                    with self._handle(self.root, row, path) as final:
                        if _stable(os.fstat(final.fd)) != state['staging']:
                            raise FileAccessError('file_changed')
            return self._current(key)

        def create_staging(key):
            if os.name != 'posix' or not hasattr(os, 'O_TMPFILE'):
                raise FileAccessError('write_backend_unverified')
            descriptor = None
            try:
                with verify_parent(key, prepare_tool) as opened:
                    bridge.preflight(row, path, current=lambda: self._current(key))
                    try:
                        os.stat(name, dir_fd=opened.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise FileAccessError('target_exists')
                    self._capacity(opened.fd, size)
                    self._grant(key, prepare_tool, root_id, path)
                    descriptor = os.open('.', os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC
                                         | os.O_NOFOLLOW, 0o600, dir_fd=opened.fd)
                    state['staging'] = _stable(os.fstat(descriptor))
                    authorize_opened(key, descriptor, write=True)
                    return descriptor
            except Exception:
                if descriptor is not None:
                    os.close(descriptor)
                raise

        def publish(key, descriptor, *, overwrite, expected_target_version,
                    operation_id=None, idempotency_key=None,
                    operation_deadline=None, operation_clock=None):
            if overwrite is not False or expected_target_version is not None:
                raise FileAccessError('invalid_arguments')
            state['operation_deadline'] = (operation_deadline if operation_deadline is not None
                                           else time.monotonic() + 1800)
            state['operation_clock'] = operation_clock or time.monotonic
            authorize_opened(key, descriptor, write=True)
            with verify_parent(key, commit_tool) as opened:
                bridge.preflight(row, path, current=lambda: self._current(key))
                self._capacity(opened.fd, 0)
                self._grant(key, commit_tool, root_id, path)
                authorize_opened(key, descriptor, write=True)
                _link_new(descriptor, opened.fd, name)
                state['published'] = True
                state['final'] = _identity(descriptor)
                os.fsync(opened.fd)
                authorize_opened(key, descriptor, write=True)
                # Linking changes ctime, so metadata alone cannot prove that
                # a concurrent writer did not change bytes in that interval.
                # Bind the published inode back to the verified upload/copy.
                if args.get('sha256') is not None:
                    digest, offset = hashlib.sha256(), 0
                    while offset < size:
                        authorize_opened(key, descriptor, write=True)
                        block = os.pread(descriptor, min(65536, size - offset), offset)
                        if not block:
                            raise FileAccessError('result_unconfirmed')
                        authorize_opened(key, descriptor, write=True)
                        digest.update(block)
                        offset += len(block)
                    authorize_opened(key, descriptor, write=True)
                    if digest.hexdigest() != args['sha256']:
                        raise FileAccessError('result_unconfirmed')
                receipt = bridge.notify_new(row, path, descriptor, directory=False,
                                             current=lambda: self._current(key))
                state['final'] = _identity(descriptor)
                authorize_opened(key, descriptor, write=True)

            def verify(key, opened_descriptor):
                authorize_opened(key, opened_descriptor, write=True)
                accepted = bridge.verify_new(receipt, row, path, opened_descriptor,
                    directory=False, current=lambda: self._current(key))
                authorize_opened(key, opened_descriptor, write=True)
                return accepted

            return PendingInstall(_version(state['final']), verify)

        def install(key, descriptor, **options):
            try:
                return publish(key, descriptor, **options)
            except FileAccessError as error:
                if not state['published'] and error.code != 'result_unconfirmed':
                    raise UnpublishedInstallError(error.code) from None
                raise

        return UploadTarget(root_id, path, name, create_staging, authorize_opened, install,
                            prepare_tool=prepare_tool, operation_context=prepare_tool == 'prepare_file_upload')

    def execute(self, tool, args, principal):
        try:
            if tool not in TOOLS or type(args) is not dict or not self._current(principal):
                raise FileAccessError('invalid_key' if not self._current(principal) else 'invalid_arguments')
            from api_file_mutations import TOOLS as MUTATION_TOOLS
            if tool in MUTATION_TOOLS:
                return self.mutations.execute(tool, args, principal)
            if tool in ('prepare_file_upload', 'prepare_file_overwrite'):
                required = {'root_id', 'path', 'size_bytes', 'sha256', 'idempotency_key'}
                if tool == 'prepare_file_overwrite':
                    required |= {'expected_target_version', 'confirmed'}
                if (not required <= set(args) or set(args) - required - {'overwrite'}
                        or args.get('overwrite', False) is not False
                        or tool == 'prepare_file_overwrite' and args['confirmed'] is not True
                        or not isinstance(args['sha256'], str) or not _SHA.fullmatch(args['sha256'])
                        or not isinstance(args['idempotency_key'], str)
                        or not _IDEMPOTENCY.fullmatch(args['idempotency_key'])
                        or tool == 'prepare_file_overwrite' and (
                            not isinstance(args['expected_target_version'], str)
                            or not _VERSION.fullmatch(args['expected_target_version']))
                        or type(args['size_bytes']) is not int or not 0 <= args['size_bytes'] <= 8 * 1024 ** 3):
                    raise FileAccessError('invalid_arguments')
                target = (self.mutations.replacement_target(principal, args)
                          if tool == 'prepare_file_overwrite' else self._target(principal, args))
                return self.transfers.prepare_upload(principal, target, size_bytes=args['size_bytes'],
                    sha256=args['sha256'], idempotency_key=args['idempotency_key'],
                    overwrite=tool == 'prepare_file_overwrite',
                    expected_target_version=args.get('expected_target_version'),
                    confirmed=args.get('confirmed', False))
            if tool == 'commit_file_upload':
                if set(args) != {'transfer_id', 'idempotency_key'}:
                    raise FileAccessError('invalid_arguments')
                return self.transfers.commit_upload(args['transfer_id'], principal,
                                                     idempotency_key=args['idempotency_key'])
            if set(args) != {'operation_id'}:
                raise FileAccessError('invalid_arguments')
            if args['operation_id'] in self.transfers._operations:
                return self.transfers.get_operation(args['operation_id'], principal)
            return self.mutations.get_operation(args['operation_id'], principal)
        except FileAccessError as error:
            return error.result()
        except Exception:
            return FileAccessError('write_backend_unverified').result()

    def close(self):
        self._closed = True
        self.mutations.close()

"""New owner files only: anonymous fd staging, no-clobber install, native index.

No filesystem mutation is authorized by a root plan or an input proof flag.
The exact factory owner's audited write rule, current key, parent descriptor,
mount identity and native indexing bridge are independent mandatory gates.
"""

import os
import stat

from api_file_operations import FileAccessError, assert_file_grant, _descriptor_mount_id
from api_file_provider import NativeOwnerWriteAuthority
from api_file_roots import valid_relative_file_path
from api_file_transfers import (UploadTarget, PendingInstall, UnpublishedInstallError,
                                _identity, _version)


TOOLS = frozenset(('prepare_file_upload', 'commit_file_upload', 'get_file_operation'))
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
                 handle_factory=None, index=None, capacity=None):
        self.panel, self.root, self.transfers = panel, root, transfers
        self._authorizer = authorizer
        self._authority = authority or NativeOwnerWriteAuthority(panel, root=root)
        if handle_factory is None:
            from api_file_executor import _Handle
            handle_factory = _Handle
        self._handle = handle_factory
        self._index = index
        self._capacity = capacity or _capacity
        self._closed = False

    def _current(self, principal):
        try:
            return not self._closed and self._authorizer(principal) is True
        except Exception:
            return False

    def _grant(self, principal, tool, root_id, path):
        if not self._current(principal):
            raise FileAccessError('invalid_key')
        parent, separator, name = path.rpartition('/') if type(path) is str else ('', '', '')
        if (not separator or not valid_relative_file_path(path) or not name
                or len(name.encode('utf-8', 'strict')) > 255):
            raise FileAccessError('invalid_arguments')
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

    def _target(self, principal, args):
        root_id, path, size = args['root_id'], args['path'], args['size_bytes']
        row = self._root(principal, 'prepare_file_upload', root_id, path)
        parent, name = self._grant(principal, 'prepare_file_upload', root_id, path)
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
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or state['staging'] != _stable(info)
                    or _descriptor_mount_id(self.root, descriptor) != mount_id
                    or info.st_nlink != (1 if state['published'] else 0)):
                raise FileAccessError('file_changed')
            with verify_parent(key, 'prepare_file_upload') as opened:
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
                with verify_parent(key, 'prepare_file_upload') as opened:
                    bridge.preflight(row, path, current=lambda: self._current(key))
                    try:
                        os.stat(name, dir_fd=opened.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise FileAccessError('target_exists')
                    self._capacity(opened.fd, size)
                    self._grant(key, 'prepare_file_upload', root_id, path)
                    descriptor = os.open('.', os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC
                                         | os.O_NOFOLLOW, 0o600, dir_fd=opened.fd)
                    state['staging'] = _stable(os.fstat(descriptor))
                    authorize_opened(key, descriptor, write=True)
                    return descriptor
            except Exception:
                if descriptor is not None:
                    os.close(descriptor)
                raise

        def publish(key, descriptor, *, overwrite, expected_target_version):
            if overwrite is not False or expected_target_version is not None:
                raise FileAccessError('invalid_arguments')
            authorize_opened(key, descriptor, write=True)
            with verify_parent(key, 'commit_file_upload') as opened:
                bridge.preflight(row, path, current=lambda: self._current(key))
                self._capacity(opened.fd, 0)
                self._grant(key, 'commit_file_upload', root_id, path)
                _link_new(descriptor, opened.fd, name)
                state['published'] = True
                state['final'] = _identity(descriptor)
                os.fsync(opened.fd)
                authorize_opened(key, descriptor, write=True)
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

        return UploadTarget(root_id, path, name, create_staging, authorize_opened, install)

    def execute(self, tool, args, principal):
        try:
            if tool not in TOOLS or type(args) is not dict or not self._current(principal):
                raise FileAccessError('invalid_key' if not self._current(principal) else 'invalid_arguments')
            if tool == 'prepare_file_upload':
                required = {'root_id', 'path', 'size_bytes', 'sha256', 'idempotency_key'}
                if (not required <= set(args) or set(args) - required - {'overwrite'}
                        or args.get('overwrite', False) is not False
                        or type(args['size_bytes']) is not int or not 0 <= args['size_bytes'] <= 8 * 1024 ** 3):
                    raise FileAccessError('invalid_arguments')
                target = self._target(principal, args)
                return self.transfers.prepare_upload(principal, target, size_bytes=args['size_bytes'],
                    sha256=args['sha256'], idempotency_key=args['idempotency_key'])
            if tool == 'commit_file_upload':
                if set(args) != {'transfer_id', 'idempotency_key'}:
                    raise FileAccessError('invalid_arguments')
                return self.transfers.commit_upload(args['transfer_id'], principal,
                                                     idempotency_key=args['idempotency_key'])
            if set(args) != {'operation_id'}:
                raise FileAccessError('invalid_arguments')
            return self.transfers.get_operation(args['operation_id'], principal)
        except FileAccessError as error:
            return error.result()
        except Exception:
            return FileAccessError('write_backend_unverified').result()

    def close(self):
        self._closed = True

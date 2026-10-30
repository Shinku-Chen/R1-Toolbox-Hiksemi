"""Real admin-root executor with native logical ACL and exact opened handles.

The fixed factory owner policy is evaluated independently from Unix authority.
Only its proved positive admin subtree is implemented. Every opened path
uses descriptor-relative no-link traversal on one live mount, with a fresh
native authorization and lifecycle gate before returning metadata or bytes.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from api_file_operations import (FileAccessError, assert_file_grant, _mounts,
                                 _descriptor_mount_id)
from api_file_provider import NativeOwnerReadAuthority
from api_file_roots import (is_admin_root_path, is_private_recovery_path,
                            physical_mount_path, valid_relative_file_path)
from api_file_transfers import DownloadSource, FileTransfers, _binding, _identity, _version
from api_file_write_backend import FileWriteExecutor, TOOLS as WRITE_TOOLS


TOOLS = frozenset(('list_directory', 'get_file_info', 'prepare_file_download',
                   'get_file_transfer', 'cancel_file_transfer'))
_CURSOR = re.compile(r'cursor_[0-9a-f]{32}\Z', re.ASCII)
_MAX_SCAN = 4096
_MAX_PAGE = 200
_MAX_BYTES = 131072


def _object_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _timestamp(nanoseconds):
    try:
        return datetime.fromtimestamp(nanoseconds / 1e9, timezone.utc).isoformat(
            timespec='milliseconds').replace('+00:00', 'Z')
    except (ValueError, OSError, OverflowError):
        return None


def _safe_name(value):
    return (isinstance(value, str) and value not in ('', '.', '..')
            and len(value) <= 255 and '/' not in value and '\\' not in value
            and all(c.isprintable() for c in value)
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


def _opened_mount_id(root, descriptor):
    """Missing mount evidence is a root failure, never a skippable child."""
    try:
        return _descriptor_mount_id(root, descriptor)
    except (OSError, ValueError):
        raise FileAccessError('root_unavailable') from None


def _deny_private_alias(root, admin, candidate):
    """Only metadata: reserve the canonical custody inode against FS aliases."""
    try:
        private = os.open('.r1-toolbox-ops', os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=admin)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        raise FileAccessError('root_unavailable') from None
    try:
        info = os.fstat(private)
        if (not stat.S_ISDIR(info.st_mode) or _opened_mount_id(root, private) is None):
            # An unknown/invalid reserved anchor cannot become a skippable
            # ordinary child error; never follow a private-name symlink.
            raise FileAccessError('root_unavailable')
        other = os.fstat(candidate)
        if (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino):
            # A bind alias is the same data inode too, even if its mount ID
            # differs. The ordinary traversal additionally rejects mount hops.
            raise FileAccessError('permission_denied')
    except OSError:
        raise FileAccessError('root_unavailable') from None
    finally:
        os.close(private)


def _entry(path, info):
    directory = stat.S_ISDIR(info.st_mode)
    return {'name': path.rsplit('/', 1)[-1] if path else 'admin', 'path': path,
            'type': 'directory' if directory else 'file',
            'size_bytes': None if directory else info.st_size,
            'modified_at': _timestamp(info.st_mtime_ns),
            'object_version': _version((info.st_dev, info.st_ino, info.st_size,
                                        info.st_mtime_ns, info.st_ctime_ns)) if not directory else None,
            'downloadable': not directory and 0 <= info.st_size <= 8 * 1024 ** 3}


class _Handle:
    """Owned fd plus no-link admin ancestry, rejecting every extra mount."""

    def __init__(self, root, row, path, *, reader=False, directory=False):
        if is_private_recovery_path(path):
            raise FileAccessError('permission_denied')
        if os.name != 'posix' or not hasattr(os, 'O_PATH'):
            raise FileAccessError('secure_open_unavailable')
        if not is_admin_root_path(row.get('mount')) or not valid_relative_file_path(path):
            raise FileAccessError('invalid_arguments')
        self.root, self.row, self.path = root, dict(row), path
        self.descriptors = []
        self.fd, self.mount_id = None, None
        try:
            try:
                mounts = _mounts(root)
            except (OSError, ValueError):
                # Fixed proc metadata failures cannot be confused with a
                # missing or unreadable child and silently filtered by lists.
                raise FileAccessError('root_unavailable') from None
            physical = physical_mount_path(row['mount'])
            expected = mounts.get(physical, [])
            target = row['mount'] + ('/' + path if path else '')
            # A sibling mount cannot invalidate the whole admin root. A mount
            # on this exact ancestry remains forbidden, including same-device
            # binds whose st_dev alone would falsely look safe.
            if len(expected) != 1:
                raise FileAccessError('root_unavailable')
            for candidate in mounts:
                if (candidate != physical and candidate.startswith(physical + '/')
                        and (target == candidate or target.startswith(candidate + '/'))):
                    descendant = candidate.startswith(row['mount'] + '/')
                    raise FileAccessError('cross_mount_denied' if descendant else 'root_unavailable')
            self.mount_id, device = expected[0]
            flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            parent = os.open(Path(root).absolute(), flags)
            self.descriptors.append(parent)
            boundary = row['mount'].split('/')[1:]
            components = boundary + (path.split('/') if path else [])
            for index, component in enumerate(components):
                final = index == len(components) - 1
                open_flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
                if index < len(boundary) or not final or directory:
                    open_flags |= os.O_DIRECTORY
                parent = os.open(component, open_flags, dir_fd=parent)
                self.descriptors.append(parent)
                info = os.fstat(parent)
                if (stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode)
                                                     or final and stat.S_ISREG(info.st_mode))):
                    raise FileAccessError('unsupported_file_type')
                if index >= 1:
                    # /drives/<disk> and every descendant must stay on the exact
                    # mount, even if st_dev is shared by an unexpected bind.
                    observed_mount = _opened_mount_id(root, parent)
                    if (observed_mount != self.mount_id
                            or f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}' != device):
                        descendant = index >= len(boundary) and observed_mount is not None
                        raise FileAccessError('cross_mount_denied' if descendant else 'root_unavailable')
                if index == len(boundary):
                    # The first descendant can be a short/case-insensitive FS
                    # alias even when its spelling passes the lexical guard.
                    _deny_private_alias(root, self.descriptors[-2], parent)
            self.fd = parent
            self.identity = _object_identity(os.fstat(self.fd))
            if stat.S_ISREG(self.identity[2]) and self.identity[3] != 1:
                raise FileAccessError('unsupported_file_type')
            if reader and not stat.S_ISREG(self.identity[2]):
                raise FileAccessError('unsupported_file_type')
            if directory and not stat.S_ISDIR(self.identity[2]):
                raise FileAccessError('unsupported_file_type')
            if reader or directory:
                # Inspect with O_PATH first: opening a device/FIFO in read mode
                # merely to fstat it could itself execute device behavior. The
                # kernel fd link reopens only this owned, inspected inode.
                readable = os.open('/proc/self/fd/' + str(self.fd),
                    os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
                    | (os.O_DIRECTORY if directory else 0))
                self.descriptors.append(readable)
                if (_object_identity(os.fstat(readable)) != self.identity
                        or _opened_mount_id(root, readable) != self.mount_id):
                    raise FileAccessError('file_changed')
                self.fd = readable
        except FileAccessError:
            self.close()
            raise
        except FileNotFoundError:
            self.close()
            raise FileAccessError('not_found') from None
        except OSError:
            self.close()
            raise FileAccessError('secure_open_unavailable') from None

    def close(self):
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)
        self.descriptors = []
        self.fd = None

    def detach(self):
        descriptor = self.fd
        self.descriptors.remove(descriptor)
        self.close()
        return descriptor

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


class _UsersHandle(_Handle):
    """Internal fixed Users ancestor; never accepts a caller-relative path.

    The public v3 admin anchor is first verified on the physical mount. Its
    already-held Users parent then serves only native root ACL proof and the
    fixed native cache protocols. Public handles never accept a v2 Users row.
    """

    def __init__(self, root, row):
        super().__init__(root, row, '')
        try:
            admin = self.descriptors.pop()
            self.fd = self.descriptors[-1]
            os.close(admin)
            self.identity = _object_identity(os.fstat(self.fd))
            readable = os.open('/proc/self/fd/' + str(self.fd),
                               os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_CLOEXEC)
            self.descriptors.append(readable)
            if (_object_identity(os.fstat(readable)) != self.identity
                    or _opened_mount_id(root, readable) != self.mount_id):
                raise FileAccessError('file_changed')
            self.fd = readable
        except Exception:
            self.close()
            raise


class FileReadExecutor:
    """Five concrete tools; native proof failure returns unavailable, no fallback.

    ``authority`` and ``handle_factory`` are internal fixture seams, never
    constructor options derived from HTTP or Token records. Production always
    uses NativeOwnerReadAuthority and the real Linux descriptor implementation.
    """

    def __init__(self, panel, *, authorizer, root='/', authority=None,
                 handle_factory=None, clock=None, mutation_authority=None):
        self.panel, self.root = panel, root
        self._authorizer = authorizer
        self._authority = authority or NativeOwnerReadAuthority(panel, root=root)
        self._handle = handle_factory or _Handle
        self._clock = clock or time.monotonic
        self._closed = False
        self._close_error = None
        self._lock = threading.RLock()
        self._cursors = {}
        self.transfers = FileTransfers(self._current)
        self.write_operations = FileWriteExecutor(panel, root=root, authorizer=self._current,
                                                   transfers=self.transfers,
                                                   mutation_authority=mutation_authority)

    def _current(self, principal):
        try:
            return not self._closed and self._authorizer(principal) is True
        except Exception:
            return False

    def _grant(self, principal, tool, root_id, path):
        if not self._current(principal):
            raise FileAccessError('invalid_key')
        if is_private_recovery_path(path):
            raise FileAccessError('permission_denied')
        assert_file_grant(principal, tool, root_id, path)

    def _root(self, principal, tool, root_id, path):
        self._grant(principal, tool, root_id, path)
        row = self._authority.authorize(root_id, path, lambda: self._current(principal))
        self._grant(principal, tool, root_id, path)
        return row

    def _verify(self, principal, tool, root_id, path, descriptor):
        row = self._root(principal, tool, root_id, path)
        before = _object_identity(os.fstat(descriptor))
        with self._handle(self.root, row, path) as current:
            if current.identity != before:
                raise FileAccessError('file_changed')
        self._grant(principal, tool, root_id, path)
        if _object_identity(os.fstat(descriptor)) != before:
            raise FileAccessError('file_changed')
        return True

    def _info(self, args, principal):
        root_id, path = args['root_id'], args['path']
        row = self._root(principal, 'get_file_info', root_id, path)
        with self._handle(self.root, row, path) as opened:
            self._verify(principal, 'get_file_info', root_id, path, opened.fd)
            entry = _entry(path, os.fstat(opened.fd))
        self._grant(principal, 'get_file_info', root_id, path)
        return {'ok': True, 'root_id': root_id, 'path': path, 'entry': entry,
                'may_wake_disks': True}

    def _directory(self, args, principal):
        root_id, path, limit = args['root_id'], args['path'], args.get('limit', 50)
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE:
            raise FileAccessError('invalid_arguments')
        cursor = args.get('cursor')
        if cursor is not None and (type(cursor) is not str or not _CURSOR.fullmatch(cursor)):
            raise FileAccessError('invalid_arguments')
        after_name = None
        row = self._root(principal, 'list_directory', root_id, path)
        deadline = self._clock() + 10
        with self._handle(self.root, row, path, directory=True) as opened:
            identity = opened.identity
            if cursor is not None:
                if type(cursor) is not str or not _CURSOR.fullmatch(cursor):
                    raise FileAccessError('invalid_arguments')
                with self._lock:
                    saved = self._cursors.get(cursor)
                if (saved is None or saved['binding'] != _binding(principal)
                        or saved['root_id'] != root_id or saved['path'] != path
                        or saved['limit'] != limit or self._clock() >= saved['deadline']):
                    raise FileAccessError('directory_changed')
                if saved['identity'] != identity:
                    raise FileAccessError('directory_changed')
                after_name = saved['after_name']
            names = []
            scanned = 0
            with os.scandir(opened.fd) as iterator:
                for item in iterator:
                    scanned += 1
                    if scanned > _MAX_SCAN or self._clock() >= deadline:
                        raise FileAccessError('limit_exceeded')
                    if _safe_name(item.name):
                        names.append(item.name)
            entries = []
            for name in sorted(names):
                if self._clock() >= deadline:
                    raise FileAccessError('limit_exceeded')
                if after_name is not None and name <= after_name:
                    continue
                child = path + '/' + name if path else name
                if is_private_recovery_path(child):
                    # Custody lives inside admin but never joins its public
                    # file projection, even for a full-root machine grant.
                    continue
                try:
                    self._grant(principal, 'list_directory', root_id, child)
                    with self._handle(self.root, row, child) as target:
                        entries.append(_entry(child, os.fstat(target.fd)))
                except FileAccessError as error:
                    if error.code in ('permission_denied', 'not_found', 'unsupported_file_type',
                                      'secure_open_unavailable', 'cross_mount_denied'):
                        continue
                    raise
                if len(entries) > limit:
                    break
            self._verify(principal, 'list_directory', root_id, path, opened.fd)
            if _object_identity(os.fstat(opened.fd)) != identity:
                raise FileAccessError('directory_changed')
            has_more = len(entries) > limit
            entries = entries[:limit]
            next_cursor = None
            if has_more:
                with self._lock:
                    self._cursors = {key: value for key, value in self._cursors.items()
                                     if self._clock() < value['deadline']}
                    if len(self._cursors) >= 128:
                        raise FileAccessError('limit_exceeded')
                    next_cursor = 'cursor_' + secrets.token_hex(16)
                    self._cursors[next_cursor] = {'binding': _binding(principal),
                        'root_id': root_id, 'path': path, 'limit': limit,
                        'identity': identity, 'after_name': entries[-1]['name'],
                        'deadline': self._clock() + 300}
        result = {'ok': True, 'root_id': root_id, 'path': path, 'entries': entries,
                  'page': {'limit': limit, 'next_cursor': next_cursor, 'total': None},
                  'visibility': 'authorized_supported_only', 'consistency': 'best_effort',
                  'may_wake_disks': True}
        if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > _MAX_BYTES:
            raise FileAccessError('limit_exceeded')
        self._grant(principal, 'list_directory', root_id, path)
        return result

    def _download(self, args, principal):
        root_id, path = args['root_id'], args['path']
        row = self._root(principal, 'prepare_file_download', root_id, path)
        with self._handle(self.root, row, path, reader=True) as opened:
            if opened.identity[4] > 8 * 1024 ** 3:
                raise FileAccessError('limit_exceeded')
            self._verify(principal, 'prepare_file_download', root_id, path, opened.fd)

        def open_reader(current):
            current_row = self._root(current, 'prepare_file_download', root_id, path)
            handle = self._handle(self.root, current_row, path, reader=True)
            try:
                self._verify(current, 'prepare_file_download', root_id, path, handle.fd)
                return handle.detach()
            finally:
                handle.close()

        def authorize_opened(current, descriptor, *, write=False):
            if write:
                return False
            return self._verify(current, 'prepare_file_download', root_id, path, descriptor)

        source = DownloadSource(root_id, path, path.rsplit('/', 1)[-1],
                                open_reader, authorize_opened)
        return self.transfers.prepare_download(principal, source)

    def execute(self, tool, args, principal, authorizer=None):
        """Strict arguments plus the same real-key authorization on every branch."""
        try:
            if tool not in TOOLS | WRITE_TOOLS or type(args) is not dict:
                raise FileAccessError('invalid_arguments')
            if authorizer is not None and authorizer(principal) is not True:
                raise FileAccessError('invalid_key')
            if tool in WRITE_TOOLS:
                return self.write_operations.execute(tool, args, principal)
            if tool in ('get_file_transfer', 'cancel_file_transfer'):
                if set(args) != {'transfer_id'}:
                    raise FileAccessError('invalid_arguments')
                function = (self.transfers.get_transfer if tool == 'get_file_transfer'
                            else self.transfers.cancel_transfer)
                return function(args['transfer_id'], principal)
            fields = {'root_id', 'path'}
            if tool == 'list_directory':
                fields |= {'limit', 'cursor'}
            if not {'root_id', 'path'} <= set(args) or set(args) - fields:
                raise FileAccessError('invalid_arguments')
            if tool == 'get_file_info':
                return self._info(args, principal)
            if tool == 'list_directory':
                return self._directory(args, principal)
            return self._download(args, principal)
        except FileAccessError as error:
            return error.result()
        except Exception:
            return FileAccessError('secure_open_unavailable').result()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            first_error = None
            for resource in (self.write_operations, self.transfers):
                try:
                    resource.close()
                except Exception as error:
                    if first_error is None:
                        first_error = error
            self._cursors.clear()
            self._close_error = first_error
            if first_error is not None:
                raise first_error

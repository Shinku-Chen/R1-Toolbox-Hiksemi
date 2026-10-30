"""Fixed factory indexing for a newly published owner file, never an RPC proxy.

The exact v260907 SMB bridge receives two fixed-size create/close records. A
successful send is only a notification; the independent fixed SQLite reader
must observe matching metadata before the file operation can be completed.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import socket
import stat
import struct
import time

from api_file_operations import FileAccessError, _read_fixed
from api_file_provider import audited_owner_write_rule, _runtime_binary_matches
from api_file_roots import is_users_root_path
from vendor_business_transport import _identity as _service_identity
from vendor_runtime import require_emhistor_listener, _socket_inodes


_SOCKET = '/tmp/UNIX.domain'
_TIMEOUT = 8.0
_RECORD = struct.Struct('<16s32sI1024s1024s')


def _authorized(current):
    try:
        accepted = current() is True
    except Exception:
        accepted = False
    if not accepted:
        raise FileAccessError('invalid_key')


def _remaining(deadline, clock):
    value = deadline - clock()
    if value <= 0:
        raise FileAccessError('timeout')
    return value


def _paths(row, path, *, directory=False):
    """Only literal admin descendants and the audited finite wire grammar."""
    if (directory is not False or type(row) is not dict
            or not is_users_root_path(row.get('mount'))
            or not audited_owner_write_rule(path) or '|' in path):
        raise FileAccessError('unsupported_path_class')
    mount = row['mount'][:-6]
    absolute = row['mount'] + '/' + path
    try:
        share, encoded = mount.encode('utf-8', 'strict'), absolute.encode('utf-8', 'strict')
    except UnicodeError:
        raise FileAccessError('invalid_arguments') from None
    # The factory handler uses fixed C strings, including the create prefix.
    if len(share) >= 32 or len(encoded) + len(b'file|create|') >= 1024:
        raise FileAccessError('limit_exceeded')
    return mount, absolute, share, encoded


def _new_file_records(row, path):
    """v260907 581a6a receives 0x834-byte records; no caller chooses event IDs.

    Header: canonical user[16], share mount[32], event uint32, source[1024],
    target[1024]. 580d45 handles create 0x14 and records its literal source;
    close 0x15 must match that create before 5806c2 maps it to insert-file (1).
    """
    _mount, _absolute, share, encoded = _paths(row, path)
    return (_RECORD.pack(b'admin', share, 0x14, encoded, b'file|create|' + encoded)
            + _RECORD.pack(b'admin', share, 0x15, encoded, b''))


def _internal_sata(root, row):
    """Conservatively admit the built-in SATA branch used by the native bridge.

    The native receiver itself checks disk_is_buildin_dev_byfilepath. This
    private preflight rejects USB, virtual and unverified transports before a
    user file can be published. The root/serial lifecycle is checked separately
    by the file authority and the canonical descriptor chain.
    """
    physical = row['mount'][:-6]
    entries = []
    for line in _read_fixed(root, 'proc/self/mountinfo').splitlines():
        halves = line.split(' - ', 1)
        if len(halves) != 2:
            return False
        before, after = halves[0].split(), halves[1].split()
        if len(before) >= 6 and len(after) >= 3 and before[4] == physical:
            entries.append((before, after))
    if len(entries) != 1 or entries[0][0][3] != '/':
        return False
    match = re.fullmatch(r'/dev/(sd[a-z]+)([1-9][0-9]*)', entries[0][1][1])
    if match is None:
        return False
    base = Path(root).absolute()
    try:
        partition = match[1] + match[2]
        number = _read_fixed(root, 'sys/block/' + match[1] + '/' + partition + '/dev', 64).strip()
        block = os.stat(base / 'dev' / partition, follow_symlinks=False)
        if (number != entries[0][0][2] or not stat.S_ISBLK(block.st_mode)
                or str(os.major(block.st_rdev)) + ':' + str(os.minor(block.st_rdev)) != number):
            return False
        device = (base / 'sys/block' / match[1] / 'device').resolve(strict=True)
        relative = device.relative_to(base / 'sys/devices')
    except (OSError, ValueError):
        return False
    parts = relative.parts
    return (any(re.fullmatch(r'ata[1-9][0-9]*', part) for part in parts)
            and not any(part.startswith(('usb', 'virtual')) for part in parts))


def _listener_signature(root, deadline):
    """Exact native ELF, namespace, fixed Unix listener and owning process."""
    identity = _service_identity(require_emhistor_listener(root=root, port=20443))
    if _runtime_binary_matches(root, identity) is not True:
        raise FileAccessError('native_unavailable')
    base = Path(root).absolute()
    local = os.stat(base / 'proc/self/ns/mnt')
    native = os.stat(base / 'proc' / str(identity[0]) / 'ns/mnt')
    if (local.st_dev, local.st_ino) != (native.st_dev, native.st_ino):
        raise FileAccessError('native_unavailable')
    target = base / _SOCKET.lstrip('/')
    before = target.lstat()
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != 0:
        raise FileAccessError('native_unavailable')
    listeners = []
    for line in _read_fixed(root, 'proc/net/unix').splitlines()[1:]:
        fields = line.split()
        if len(fields) == 8 and fields[7] == _SOCKET:
            if (fields[3] != '00010000' or fields[4] != '0001' or fields[5] != '01'
                    or re.fullmatch(r'[1-9][0-9]{0,23}', fields[6]) is None):
                raise FileAccessError('native_unavailable')
            listeners.append(fields[6])
    if len(listeners) != 1:
        raise FileAccessError('native_unavailable')
    owned, complete = _socket_inodes(base / 'proc' / str(identity[0]), deadline, [8192], strict=True)
    after = target.lstat()
    signature = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                               value.st_gid, value.st_mtime_ns, value.st_ctime_ns)
    if not complete or listeners[0] not in owned or signature(before) != signature(after):
        raise FileAccessError('native_unavailable')
    return identity, listeners[0], signature(after), (native.st_dev, native.st_ino)


def _opened_object(root, row, path, descriptor):
    from api_file_executor import _Handle
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                               value.st_mtime_ns, value.st_ctime_ns)
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise FileAccessError('file_changed')
    with _Handle(root, row, path) as opened:
        if identity(os.fstat(opened.fd)) != identity(before):
            raise FileAccessError('file_changed')
    if identity(os.fstat(descriptor)) != identity(before):
        raise FileAccessError('file_changed')
    return identity(before)


@dataclass(frozen=True, repr=False)
class _IndexReceipt:
    root_id: str
    path: str
    native: tuple
    object_identity: tuple
    reader: object


class NativeFileIndex:
    """Private new-file bridge; all seams are trusted constructor fixtures only."""

    def __init__(self, root='/', *, reader=None, listener_check=None,
                 disk_check=None, object_check=None, socket_factory=None, clock=None):
        self.root = root
        self._reader = reader
        self._listener = listener_check or (lambda deadline: _listener_signature(self.root, deadline))
        self._disk = disk_check or (lambda row: _internal_sata(self.root, row))
        self._object = object_check or (lambda row, path, fd: _opened_object(self.root, row, path, fd))
        self._socket = socket_factory or socket.socket
        self._clock = clock or time.monotonic

    def _backend(self):
        if self._reader is None:
            try:
                from api_file_native_index_reader import NativeFileIndexReader
                self._reader = NativeFileIndexReader(self.root)
            except ImportError:
                raise FileAccessError('write_backend_unverified') from None
        return self._reader

    def _preflight(self, row, path, current, *, require_absent):
        _authorized(current)
        _paths(row, path)
        deadline = self._clock() + _TIMEOUT
        if self._disk(row) is not True:
            raise FileAccessError('write_backend_unverified')
        signature = self._listener(deadline)
        context = self._backend().preflight(row, path, current=current, require_absent=require_absent)
        _remaining(deadline, self._clock)
        _authorized(current)
        return signature, context, deadline

    def preflight(self, row, path, *, current):
        """Reject unavailable native indexing or a stale same-name index row."""
        try:
            self._preflight(row, path, current, require_absent=True)
        except FileAccessError:
            raise
        except Exception:
            raise FileAccessError('write_backend_unverified') from None

    def notify_new(self, row, path, descriptor, *, directory, current):
        """Send the fixed pair once; transport uncertainty is never replayed."""
        _paths(row, path, directory=directory)
        connection = None
        try:
            signature, context, deadline = self._preflight(row, path, current, require_absent=False)
            identity = self._object(row, path, descriptor)
            connection = self._socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(_remaining(deadline, self._clock))
            connection.connect(str(Path(self.root).absolute() / _SOCKET.lstrip('/')))
            if not hasattr(socket, 'SO_PEERCRED'):
                raise FileAccessError('native_unavailable')
            peer = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer[0] != signature[0][0] or peer[1] != 0:
                raise FileAccessError('native_unavailable')
            if self._listener(deadline) != signature or self._object(row, path, descriptor) != identity:
                raise FileAccessError('file_changed')
            _authorized(current)
            connection.settimeout(_remaining(deadline, self._clock))
            connection.sendall(_new_file_records(row, path))
            _authorized(current)
            if self._listener(deadline) != signature or self._object(row, path, descriptor) != identity:
                raise FileAccessError('file_changed')
            _remaining(deadline, self._clock)
            return _IndexReceipt(row['id'], path, signature, identity, context)
        except FileAccessError:
            raise
        except Exception:
            raise FileAccessError('result_unconfirmed') from None
        finally:
            if connection is not None:
                connection.close()

    def verify_new(self, receipt, row, path, descriptor, *, directory, current):
        """Observe the index only; there is no resend or asynchronous fake ACK."""
        _paths(row, path, directory=directory)
        _authorized(current)
        if (not isinstance(receipt, _IndexReceipt) or receipt.root_id != row.get('id')
                or receipt.path != path or self._object(row, path, descriptor) != receipt.object_identity):
            raise FileAccessError('file_changed')
        deadline = self._clock() + _TIMEOUT
        if self._listener(deadline) != receipt.native:
            raise FileAccessError('native_unavailable')
        result = self._backend().verify_new(receipt.reader, row, path, descriptor,
                                           directory=directory, current=current)
        _authorized(current)
        if self._object(row, path, descriptor) != receipt.object_identity:
            raise FileAccessError('file_changed')
        _remaining(deadline, self._clock)
        return result is True

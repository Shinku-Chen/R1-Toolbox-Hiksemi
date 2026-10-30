"""Fixed factory file protocol evidence and audited logical read ACL rules.

No public path/action proxy is provided. The explicit diagnostic consumes only
admin roots from the live planner, sends bounded native metadata requests, and
never reads file bytes. Runtime ACL evidence is not converted into an execution
grant or a Unix uid. Local opened-object access remains a separate gate.
"""

from contextlib import contextmanager
import hashlib
import json
try:
    import sqlite3
except ImportError:
    sqlite3 = None
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
from urllib.parse import quote, urlencode

from api_file_roots import (collect_file_roots, is_users_root_path, is_admin_root_path,
                            physical_mount_path)
from vendor_business_transport import BusinessTransportError, _identity
from vendor_media_auth import OWNER_ID, read_session
from vendor_media_probe import (MediaProbeError, MAX_HEADERS, _certificate,
                                _first_frame, _headers, _WireReader, ssl)
from vendor_runtime import require_emhistor_listener


FIRMWARE_SHA256 = '402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26'
FIRMWARE_SIZE = 18425520
PORT = 20443
TIMEOUT = 12.0
MAX_REQUESTS = 24
MAX_ROOTS = 8
_LOCK = threading.Lock()
_HASH_LOCK = threading.Lock()
_HASH_CACHE = None
_COMPONENTS = ('', 'admin')
_ACTIONS = ('get_info', 'get_list')
_ROOT_ID = re.compile(r'root_[0-9a-f]{24}\Z', re.ASCII)
_FIELDS = ('path', 'name', 'type', 'isdir', 'occupy', 'writeable', 'file_attr',
           'size', 'ctime', 'mtime', 'count', 'totalpage',
           'disk_type', 'disk_status', 'mount_point', 'user_name', 'permissions')


def audited_owner_read_rule(relative_path):
    """Evaluate only the proven positive subset of rest_check_path_auth.

    At sample 0x281280 admin is a logical name, not a Unix uid.
    0x281689 classifies Users and its owner child; 0x281dc9 permits read
    category 2 for admin and category 3/4 for the matching owner. Public paths
    are relative to the fixed admin root, never to the Users-level subjects. The
    native root permission is independently mandatory: its successful check
    proves either the internal owner branch or current external_disk permission.
    This pure path subset does not prove either condition, subject or opened fd.
    """
    from api_file_roots import valid_relative_file_path
    if not valid_relative_file_path(relative_path):
        return False
    if not relative_path:
        return True
    # Public paths already begin at the fixed logical owner's admin directory.
    # A literal child named public/admin is not another Users-level subject.
    return True


def audited_owner_write_rule(relative_path):
    """Only the fixed logical owner's category-4 descendants may be created.

    The complete 0x281dc9 branch permits eAuth=0 for category 4 only when its
    extracted user matches. Category 3 is the user root and is read-only in
    that branch. The public root is admin itself; only nonempty relative paths
    reach category 4. Literal admin/public children remain ordinary descendants.
    This is not the get_info.writeable field, a filesystem-state check.
    """
    from api_file_roots import valid_relative_file_path
    return valid_relative_file_path(relative_path) and bool(relative_path)


def _authorized(authorizer):
    try:
        allowed = callable(authorizer) and authorizer() is True
    except Exception:
        allowed = False
    if not allowed:
        raise BusinessTransportError('authorization_denied')


def _remaining(deadline, clock):
    value = deadline - clock()
    if value <= 0:
        raise BusinessTransportError('timeout')
    return value


def _firmware_identity(root='/'):
    """Hash a fixed ordinary file, with same-object metadata before and after."""
    global _HASH_CACHE
    descriptor = None
    try:
        descriptor = os.open(Path(root).absolute() / 'histor/bin/emhistor',
                             os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != FIRMWARE_SIZE:
            return False
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        identity = (str(Path(root).absolute()),) + tuple(getattr(before, f) for f in fields)
        with _HASH_LOCK:
            if _HASH_CACHE == identity:
                return True
        digest = hashlib.sha256()
        count = 0
        while count < FIRMWARE_SIZE:
            value = os.read(descriptor, min(262144, FIRMWARE_SIZE - count))
            if not value:
                return False
            count += len(value)
            digest.update(value)
        after = os.fstat(descriptor)
        matched = (not os.read(descriptor, 1) and all(getattr(before, f) == getattr(after, f)
                   for f in fields) and digest.hexdigest() == FIRMWARE_SHA256)
        if matched:
            with _HASH_LOCK:
                _HASH_CACHE = identity
        return matched
    except Exception:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fixed_owner(root='/'):
    """Read only the fixed owner's logical name/type, never token or credential columns."""
    connection = None
    if sqlite3 is None:
        return False
    try:
        base = Path(root).resolve(strict=True)
        path = base / 'tmp/nand_config/USER_AUTH_DB_101'
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
            return False
        resolved = path.resolve(strict=True)
        resolved.relative_to(base)
        deadline = time.monotonic() + 0.5
        connection = sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True,
                                     isolation_level=None, timeout=0.2)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        connection.execute('PRAGMA query_only=ON')

        def authorize(action, table, column, database, caller):
            if action == sqlite3.SQLITE_READ:
                allowed = (database == 'main' and table == 'tb_user_auth'
                           and column in ('user_id', 'user_type', 'user_name'))
                return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
            return (sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION)
                    else sqlite3.SQLITE_DENY)

        connection.set_authorizer(authorize)
        connection.execute('BEGIN')
        rows = connection.execute('SELECT user_id,user_type,user_name FROM tb_user_auth '
                                  'WHERE user_id=? LIMIT 2', (OWNER_ID,)).fetchall()
        after = path.lstat()
        return (rows == [(OWNER_ID, 0, 'admin')] and all(type(v) is int for v in rows[0][:2])
                and time.monotonic() < deadline and stat.S_ISREG(after.st_mode)
                and path.resolve(strict=True) == resolved
                and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino))
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def _runtime_binary_matches(root, identity):
    """A matching disk file must also be the exact listener's running executable."""
    try:
        disk = os.stat(Path(root).absolute() / 'histor/bin/emhistor', follow_symlinks=False)
        running = os.stat(Path(root).absolute() / 'proc' / str(identity[0]) / 'exe')
        return (stat.S_ISREG(disk.st_mode) and stat.S_ISREG(running.st_mode)
                and (disk.st_dev, disk.st_ino, disk.st_size, disk.st_mtime_ns, disk.st_ctime_ns)
                == (running.st_dev, running.st_ino, running.st_size,
                    running.st_mtime_ns, running.st_ctime_ns)
                and _firmware_identity(root))
    except Exception:
        return False


def collect_opened_root_evidence(root, row):
    """Fixed staged directory evidence; no names, fd targets or file contents.

    Mount IDs are intentionally reported: equality failures need both observed
    values, not one flattened exception. The summaries grant no ACL authority.
    """
    from api_file_operations import _mounts, _descriptor_mount_id
    result = {'status': 'unverified', 'expected_mount_id': None,
              'expected_device': None, 'directory_stages': [],
              'namespace_fingerprints': {}, 'same_mount': False,
              'native_acl_proven': False}
    if not is_admin_root_path(row.get('mount')):
        result['reason'] = 'invalid_admin_root'
        return result
    for name, relative in (('self', 'proc/self/ns/mnt'), ('pid1', 'proc/1/ns/mnt')):
        try:
            info = os.stat(Path(root).absolute() / relative)
            result['namespace_fingerprints'][name] = hashlib.sha256(
                repr((info.st_dev, info.st_ino)).encode('ascii')).hexdigest()
        except OSError:
            result['namespace_fingerprints'][name] = None
    if os.name != 'posix' or not hasattr(os, 'O_PATH'):
        result['reason'] = 'linux_descriptor_api_unavailable'
        return result
    descriptors = []
    try:
        mounts = _mounts(root)
        expected = mounts.get(physical_mount_path(row['mount']), [])
        if len(expected) == 1:
            result['expected_mount_id'], result['expected_device'] = expected[0]
        result['nested_mount_count'] = sum(
            len(entries) for path, entries in mounts.items()
            if path == row['mount'] or path.startswith(row['mount'] + '/'))
        targets = [('host_root', Path(root).absolute())]
        targets += list(zip(('drives_directory', 'physical_mount', 'users_directory', 'admin_directory'),
                            row['mount'].split('/')[1:]))
        for stage, target in targets:
            record = {'stage': stage, 'opened': False, 'errno': None,
                      'mount_id': None, 'device': None, 'kind': None,
                      'identity_sha256': None}
            result['directory_stages'].append(record)
            try:
                descriptor = os.open(target, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
                                     | os.O_CLOEXEC,
                                     **({'dir_fd': descriptors[-1]} if descriptors else {}))
            except OSError as error:
                record['errno'] = error.errno if type(error.errno) is int else None
                result['reason'] = 'fixed_stage_open_failed'
                break
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            record.update(opened=True, device=f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}',
                          kind='directory' if stat.S_ISDIR(info.st_mode) else 'unsupported',
                          identity_sha256=hashlib.sha256(repr((info.st_dev, info.st_ino,
                              info.st_mode, info.st_ctime_ns)).encode('ascii')).hexdigest())
            try:
                record['mount_id'] = _descriptor_mount_id(root, descriptor)
                record['mount_id_readable'] = record['mount_id'] is not None
            except Exception:
                record['mount_id_readable'] = False
        if len(result['directory_stages']) == 5 and all(r['opened'] for r in result['directory_stages']):
            disk, users, admin = result['directory_stages'][-3:]
            result['same_mount'] = (result['expected_mount_id'] is not None
                and disk['mount_id'] == users['mount_id'] == admin['mount_id'] == result['expected_mount_id']
                and disk['device'] == users['device'] == admin['device'] == result['expected_device'])
            result['status'] = 'verified' if result['same_mount'] else 'unverified'
            result['reason'] = ('directory_metadata_only' if result['same_mount']
                                else 'opened_mount_identity_mismatch')
    except Exception:
        result['reason'] = 'fixed_metadata_unavailable'
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return result


@contextmanager
def _root_metadata_guard(root, row, path=''):
    """Fixed Users ACL ancestor or admin directory, never Users enumeration."""
    from api_file_executor import _Handle, _UsersHandle
    from api_file_operations import FileAccessError
    if path not in ('', 'admin'):
        raise FileAccessError('unsupported_path_class')
    def opened_handle():
        return _UsersHandle(root, row) if not path else _Handle(root, row, '', directory=True)
    with opened_handle() as opened:
        if not stat.S_ISDIR(opened.identity[2]):
            raise FileAccessError('unsupported_file_type')
        identity = opened.identity
        yield
        with opened_handle() as after:
            if identity != after.identity:
                raise FileAccessError('file_changed')


class NativeOwnerReadAuthority:
    """Current native logical permission plus the complete audited positive subset.

    An exact successful owner get_info(Users) body runs eAuth=1 through 0x281dc9. For an
    internal disk it admits the owner root; for an external disk it proves the
    current external_disk permission. In either admitted class, the same exact
    function permits the fixed admin descendants. The caller must independently
    prove those components belong to the same live opened root/object. There
    is no Unix uid claim and no local-root/boolean-ACL fallback.
    """

    def __init__(self, panel, *, root='/', client=None, root_collector=None, root_guard=None):
        self.panel, self.root = panel, root
        self.client = client or VendorFileProtocolClient(root=root)
        self.collector = root_collector or collect_file_roots
        self._guard = root_guard or _root_metadata_guard

    def authorize(self, root_id, path, current):
        from api_file_operations import FileAccessError
        # The fixed owner subset applies to both native class branches
        # after the current root request proves their respective prerequisites.
        if not audited_owner_read_rule(path):
            raise FileAccessError('unsupported_path_class')
        if not _firmware_identity(self.root) or not _fixed_owner(self.root):
            raise FileAccessError('acl_unverified')
        before = self.collector(self.panel)
        rows = before.get('roots') if type(before) is dict else None
        if (type(rows) is not list or before.get('available') is not True
                or before.get('boundary_version') != 3):
            raise FileAccessError('root_unavailable')
        matches = [r for r in rows if type(r) is dict and r.get('id') == root_id
                   and is_admin_root_path(r.get('mount'))]
        if len(matches) != 1:
            raise FileAccessError('root_unavailable')
        row = dict(matches[0])
        deadline = self.client.clock() + TIMEOUT

        def gate():
            return current() is True and self.collector(self.panel) == before

        try:
            users = physical_mount_path(row['mount']) + '/Users'
            with self._guard(self.root, row):
                frame = self.client.request(users, '', 'get_info', deadline=deadline,
                                            authorizer=gate)
            evidence = project_protocol_frame(frame, users, '', 'get_info')
            if (evidence['native_success'] is not True
                    or evidence['path_matches_requested'] is not True):
                raise FileAccessError('acl_unverified')
            if not gate() or not _fixed_owner(self.root) or not _firmware_identity(self.root):
                raise FileAccessError('acl_unverified')
            return row
        except BusinessTransportError as error:
            # Transport availability is not an observed native ACL refusal.
            # Keep fixed contract codes without exposing an upstream message.
            code = {'authorization_denied': 'invalid_key', 'timeout': 'timeout',
                    'busy': 'resource_busy'}.get(error.reason, 'native_unavailable')
            raise FileAccessError(code) from None


class NativeOwnerWriteAuthority(NativeOwnerReadAuthority):
    """Re-evaluate the narrower audited write category after current root proof.

    A successful root read proves the current internal-owner or external_disk
    prerequisite. It does not grant a write: the category-4 own-user rule and
    current writable mount are separately mandatory, then actual fd proof is
    still the executor's responsibility. No Unix uid equivalence is inferred.
    """

    def authorize(self, root_id, path, current):
        from api_file_operations import FileAccessError
        if not audited_owner_write_rule(path):
            raise FileAccessError('unsupported_path_class')
        row = super().authorize(root_id, path, current)
        if row.get('read_only') is not False:
            raise FileAccessError('permission_denied')
        return row


class VendorFileProtocolClient:
    """Private fixed GETs; a shared deadline and current authorization are required.

    Only fixed Users get_info and admin get_info/get_list are supported. This
    class cannot upload/download or accept URLs, arbitrary native
    actions, bodies, account IDs or a token. No credentials enter diagnostics.
    """

    def __init__(self, root='/', *, session_reader=None, listener_check=None,
                 certificate_reader=None, socket_factory=None, tls_context=None,
                 clock=None, binary_check=None):
        self.root = root
        self._session = session_reader or read_session
        self._listener = listener_check or require_emhistor_listener
        self._certificate = certificate_reader or _certificate
        self._socket = socket_factory or socket.socket
        self._context = tls_context
        self.clock = clock or time.monotonic
        self._binary = binary_check or _runtime_binary_matches

    def request(self, mount, component, action, *, deadline, authorizer):
        if (not is_users_root_path(mount) or component not in _COMPONENTS
                or action not in _ACTIONS or component == '' and action != 'get_info'):
            raise BusinessTransportError('invalid_parameters')
        _authorized(authorizer)
        _remaining(deadline, self.clock)
        connection = raw = None
        try:
            if ssl is None:
                raise BusinessTransportError('tls_unavailable')
            before = _identity(self._listener(root=self.root, port=PORT))
            if self._binary(self.root, before) is not True:
                raise BusinessTransportError('identity_unverified')
            certificate = self._certificate(self.root)
            context = self._context
            if context is None:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            raw = self._socket(socket.AF_INET6 if before[3] == '::1' else socket.AF_INET,
                               socket.SOCK_STREAM)
            raw.settimeout(_remaining(deadline, self.clock))
            raw.connect((before[3], PORT))
            raw.settimeout(_remaining(deadline, self.clock))
            connection = context.wrap_socket(raw, server_hostname=None)
            if connection.getpeercert(binary_form=True) != certificate:
                raise BusinessTransportError('certificate_mismatch')
            if _identity(self._listener(root=self.root, port=PORT)) != before:
                raise BusinessTransportError('identity_changed')
            if self._certificate(self.root) != certificate:
                raise BusinessTransportError('certificate_changed')
            _authorized(authorizer)
            _remaining(deadline, self.clock)
            session, status = self._session(root=self.root)
            if (session is None or getattr(session, 'user_id', None) != OWNER_ID
                    or not isinstance(status, dict) or status.get('available') is not True
                    or status.get('status') != 'ready'):
                raise BusinessTransportError('credential_unavailable')
            token = session.derive_access_token()
            if type(token) is not str or not 1 <= len(token) <= 4096:
                raise BusinessTransportError('token_unavailable')
            target = mount + ('/' + component if component else '')
            query = {'action': action, 'path': target}
            if action == 'get_list':
                # v1.0 dispatch @0x158708 requires both keys; page is 1-based.
                # limit/offset belong to a different parsed operation branch.
                query.update(page=1, pagesize=2)
            request_path = '/rest/1.0/file?' + urlencode(query, quote_via=quote)
            request_path += '&access_token=' + quote(token, safe='')
            request = ('GET ' + request_path + ' HTTP/1.1\r\n'
                       'Host: 127.0.0.1:20443\r\n'
                       'Accept: application/json, multipart/x-mixed-replace\r\n'
                       'Accept-Encoding: identity\r\nConnection: close\r\n\r\n').encode('ascii')
            if len(request) > 16384:
                raise BusinessTransportError('invalid_parameters')
            if (_identity(self._listener(root=self.root, port=PORT)) != before
                    or self._binary(self.root, before) is not True):
                raise BusinessTransportError('identity_changed')
            _authorized(authorizer)
            connection.settimeout(_remaining(deadline, self.clock))
            connection.sendall(request)
            wire = _WireReader(connection, time.monotonic() + _remaining(deadline, self.clock))
            raw_headers = wire.until(b'\r\n\r\n', MAX_HEADERS)
            http_status, headers = _headers(raw_headers, response=True)
            if 300 <= http_status <= 399:
                raise BusinessTransportError('redirect_refused', http_status)
            if http_status != 200:
                raise BusinessTransportError('http_error', http_status)
            frame = _first_frame(wire, headers, MAX_HEADERS - len(raw_headers) - 4)
            if _identity(self._listener(root=self.root, port=PORT)) != before:
                raise BusinessTransportError('identity_changed')
            if self._certificate(self.root) != certificate:
                raise BusinessTransportError('certificate_changed')
            if self._binary(self.root, before) is not True:
                raise BusinessTransportError('identity_changed')
            _authorized(authorizer)
            _remaining(deadline, self.clock)
            return frame
        except BusinessTransportError:
            raise
        except MediaProbeError as error:
            raise BusinessTransportError(error.reason, error.http_status) from None
        except (TimeoutError, socket.timeout):
            raise BusinessTransportError('timeout') from None
        except Exception:
            raise BusinessTransportError('transport_failed') from None
        finally:
            for channel in (connection, raw):
                if channel is not None:
                    try:
                        channel.close()
                    except OSError:
                        pass


def _shape(value):
    if value is None:
        return 'null'
    return {dict: 'object', list: 'array', str: 'string', int: 'integer',
            bool: 'boolean', float: 'number'}.get(type(value), 'unsupported')


def _native_list_paths(rows, mount, component):
    """Normalize exact literal child names privately; this proves no file ACL.

    v260907 list serializers emit ``name``; an absolute ``path`` is optional
    in one flagged branch. Never unquote or Unicode-normalize a native name.
    Every optional path must name that same immediate child, not another tree.
    """
    if (not is_users_root_path(mount) or component != 'admin'
            or type(rows) is not list or len(rows) > 256):
        return None
    target = mount + ('/' + component if component else '')
    output, names = [], set()
    for row in rows:
        if type(row) is not dict:
            return None
        name = row.get('name')
        if (type(name) is not str or not 1 <= len(name) <= 255 or name in ('.', '..')
                or '/' in name or '\\' in name or not all(c.isprintable() for c in name)
                or any(0xD800 <= ord(c) <= 0xDFFF for c in name) or name in names):
            return None
        if len(name.encode('utf-8', 'strict')) > 255:
            return None
        path = target + '/' + name
        if 'path' in row and (type(row['path']) is not str or row['path'] != path):
            return None
        names.add(name)
        output.append(path)
    return output


def project_protocol_frame(frame, mount, component, action):
    """Only known field shapes, fixed status and containment evidence leave host."""
    if type(frame) is not dict:
        raise BusinessTransportError('invalid_json')
    code = frame.get('code')
    code = code if type(code) is int and abs(code) <= 2 ** 53 - 1 else None
    payload = frame.get('data') if type(frame.get('data')) is dict else frame
    target = mount + ('/' + component if component else '')
    native_path = payload.get('path')
    path_matches = native_path == target if type(native_path) is str else None
    rows = payload.get('list')
    list_count = len(rows) if type(rows) is list and len(rows) <= 256 else None
    normalized = _native_list_paths(rows, mount, component) if list_count is not None else None
    children_contained = normalized is not None if list_count is not None else None
    accepted_code = (('code' not in frame or type(frame['code']) is int and frame['code'] == 0)
                     and ('code' not in payload or type(payload['code']) is int and payload['code'] == 0))
    info_shape = (action == 'get_info' and path_matches is True
                  and type(payload.get('isdir')) is int and payload['isdir'] == 1
                  and type(payload.get('size')) is int and payload['size'] == 0
                  and all(type(payload.get(k)) is int and 0 <= payload[k] <= 2 ** 53 - 1
                          for k in ('ctime', 'mtime'))
                  and accepted_code)
    list_shape = (action == 'get_list' and children_contained is True
                  and all(type(payload.get(k)) is int and 0 <= payload[k] <= 2 ** 53 - 1
                          for k in ('file_attr', 'mtime'))
                  and ('path' not in payload or path_matches is True)
                  and ('count' not in payload or type(payload['count']) is int
                       and payload['count'] == list_count)
                  and ('totalpage' not in payload or type(payload['totalpage']) is int
                       and 0 <= payload['totalpage'] <= 2 ** 31 - 1)
                  and accepted_code)
    return {'action': action, 'subject_path_class': component or 'users_root',
            'http_status': 200, 'native_code': code,
            'native_success': info_shape if action == 'get_info' else list_shape,
            'success_contract': ('get_info_structural_v260907' if info_shape else
                                 'get_list_name_children_v260907' if list_shape else None),
            'field_shapes': {k: _shape(payload[k]) for k in _FIELDS if k in payload},
            'path_matches_requested': path_matches, 'list_items_in_frame': list_count,
            'all_list_paths_contained': children_contained,
            'acl_proven': False, 'opened_object_proven': False}


def collect_file_protocol_probe(panel, *, root='/', authorizer=None,
                                root_collector=None, client=None, firmware_reader=None,
                                root_guard=None):
    """One explicit, fixed, finite probe. It enumerates native metadata, no bytes.

    Dependencies after authorizer are constructor/test seams only. The HTTP API
    may supply none of them. Every request and final projection checks the same
    authorization window and root catalog; expiration discards the entire result.
    """
    _authorized(authorizer)
    native = client or VendorFileProtocolClient(root=root)
    clock = native.clock
    deadline = clock() + TIMEOUT
    if not _LOCK.acquire(blocking=False):
        raise BusinessTransportError('busy')
    try:
        collector = root_collector or collect_file_roots
        before = collector(panel)
        rows = before.get('roots') if type(before) is dict else None
        if (type(before) is not dict or before.get('available') is not True
                or before.get('boundary_version') != 3 or type(rows) is not list
                or not 0 < len(rows) <= 128):
            rows = []
        safe = [r for r in rows if type(r) is dict and type(r.get('id')) is str
                and _ROOT_ID.fullmatch(r['id']) and is_admin_root_path(r.get('mount'))]
        if len({r['id'] for r in safe}) != len(safe):
            safe = []
        firmware_matches = (firmware_reader or _firmware_identity)(root=root) is True
        result = {'schema_version': 1, 'scope': 'file_protocol', 'available': bool(safe),
                  'complete': False, 'file_access_available': False,
                  'firmware': {'reference_sha256': FIRMWARE_SHA256,
                               'exact_runtime_file_matches': firmware_matches},
                  'roots': [], 'requests_completed': 0,
                  'request_limit': MAX_REQUESTS,
                  'fixed_owner_logical_name_matches': _fixed_owner(root),
                  'blockers': ['native_disk_classification', 'subject_to_opened_object_acl',
                               'read_only_device_acceptance', 'native_write_index_quota_atomicity'],
                  'effects': {'may_wake_disks': True, 'enumerates_user_directories': True,
                              'opens_user_files': False, 'returns_file_names': False,
                              'reads_existing_session': True, 'native_queries_may_update_state': True,
                              'writes_user_files': False, 'starts_smart': False,
                              'grants_authorization': False}}

        if not firmware_matches:
            result['available'] = False
            result['blockers'].insert(0, 'runtime_firmware_mismatch')
            _authorized(authorizer)
            return result

        def current():
            _authorized(authorizer)
            return collector(panel) == before

        timed_out = False
        for row in safe[:MAX_ROOTS]:
            if timed_out:
                break
            output = {'root_id': row['id'], 'queries': [],
                      'opened_root': collect_opened_root_evidence(root, row)}
            result['roots'].append(output)
            users = physical_mount_path(row['mount']) + '/Users'
            for component in _COMPONENTS:
                if timed_out:
                    break
                for action in _ACTIONS:
                    if not component and action != 'get_info':
                        continue
                    if clock() >= deadline:
                        timed_out = True
                        break
                    _authorized(authorizer)
                    try:
                        _remaining(deadline, clock)
                        if not current():
                            raise BusinessTransportError('identity_changed')
                        from api_file_operations import FileAccessError
                        try:
                            with (root_guard or _root_metadata_guard)(root, row, component):
                                frame = native.request(users, component, action,
                                                       deadline=deadline, authorizer=current)
                        except FileAccessError as error:
                            # A local Users/mount rejection happens before the
                            # transport. Do not label it a listener/ELF failure.
                            output['queries'].append({
                                'action': action, 'subject_path_class': component or 'users_root',
                                'http_status': None, 'reason': 'root_open_unverified',
                                'open_error_code': error.code, 'native_success': False,
                                'acl_proven': False, 'opened_object_proven': False})
                            continue
                        query = project_protocol_frame(frame, users, component, action)
                        result['requests_completed'] += 1
                    except BusinessTransportError as error:
                        if error.reason == 'authorization_denied':
                            raise
                        if error.reason == 'timeout':
                            timed_out = True
                        query = {'action': action, 'subject_path_class': component or 'users_root',
                                 'http_status': error.http_status, 'reason': error.reason,
                                 'native_success': False, 'acl_proven': False,
                                 'opened_object_proven': False}
                    output['queries'].append(query)
                    if timed_out:
                        break
        for row, output in zip(safe[:MAX_ROOTS], result['roots']):
            output['opened_root_after'] = collect_opened_root_evidence(root, row)
            output['root_topology_stable'] = output['opened_root'] == output['opened_root_after']
        try:
            identity = _identity(require_emhistor_listener(root=root, port=PORT))
            info = os.stat(Path(root).absolute() / 'proc' / str(identity[0]) / 'ns/mnt')
            result['native_mount_namespace_fingerprint'] = hashlib.sha256(
                repr((info.st_dev, info.st_ino)).encode('ascii')).hexdigest()
        except Exception:
            result['native_mount_namespace_fingerprint'] = None
        if not current():
            raise BusinessTransportError('identity_changed')
        result['complete'] = bool(safe) and len(safe) <= MAX_ROOTS and not timed_out
        if timed_out:
            result['partial_reason'] = 'timeout'
        return result
    finally:
        _LOCK.release()

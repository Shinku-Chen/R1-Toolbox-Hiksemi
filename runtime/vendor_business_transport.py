"""Internal fixed factory business requests, never an arbitrary REST proxy.

The caller supplies an authorization callback, not a Web session or an access
token. Native owner credentials are read only after authorization and remain
inside this process. All network work shares one absolute deadline.
"""

import json
import re
import socket
import threading
import time
from types import MappingProxyType
from urllib.parse import quote, urlencode

from native_capability_specs import APPLICATION_INSTALL_SPECS
from vendor_media_auth import OWNER_ID, read_session
from vendor_media_probe import (MediaProbeError, MAX_HEADERS, _certificate,
                                _first_frame, _headers, _WireReader, ssl)
from vendor_runtime import require_emhistor_listener


TIMEOUT = 10.0
PORT = 20443
MAX_REQUEST = 16 * 1024
_LOCK = threading.RLock()
# Tuple: route, action, HTTP verb, explicit mutation. Never caller-selected.
OPERATIONS = MappingProxyType({
    'albums_list': ('/rest/1.1/album', 'get_all_album_list', 'GET', False),
    'shared_albums_list': ('/rest/1.1/album', 'get_share_album_list', 'GET', False),
    'albums_create': ('/rest/1.1/album', 'create_custom_album', 'GET', True),
    'albums_rename': ('/rest/1.1/album', 'rename_custom_album', 'GET', True),
    'movie_libraries_list': ('/rest/1.1/scraper', 'lib_list', 'POST', False),
    'movie_library_rename': ('/rest/1.1/scraper', 'lib_modify', 'POST', True),
    'share_revoke': ('/rest/1.1/file', 'delete_share_link', 'POST', True),
    'apply_list': ('/rest/1.1/applycenter', 'get_apply_list', 'GET', False),
    'module_inventory': ('/rest/1.1/update', 'get_modulize_table', 'GET', False),
    'application_progress': ('/rest/1.1/applycenter', 'get_install_progress', 'POST', True),
    'application_install': ('/rest/1.1/applycenter', 'install_apply', 'POST', True),
    'backup_tasks_list': ('/rest/1.1/task', 'get_backup_task_list', 'GET', False),
    'auto_backup_get': ('/rest/1.1/config', 'get_auto_backup', 'GET', False),
    'nas_users_list': ('/rest/1.1/login', 'look_all_user', 'GET', False),
    'device_name_get': ('/rest/1.1/login', 'get_dev_name', 'GET', False),
    'device_name_set': ('/rest/1.1/login', 'set_dev_name', 'GET', True),
    'nas_ai_get': ('/rest/1.1/config', 'get_ai_switch', 'GET', False),
    'nas_ai_set': ('/rest/1.1/config', 'set_ai_switch', 'GET', True),
    'movie_scan_status': ('/rest/1.1/scraper', 'lib_scan_status', 'POST', False),
    'album_delete': ('/rest/1.1/album', 'delete_custom_album', 'GET', True),
    'backup_task_halt': ('/rest/1.1/task', 'set_task_status', 'GET', True),
    'auto_backup_disable': ('/rest/1.1/config', 'set_auto_backup', 'GET', True),
})
_REASONS = frozenset((
    'unsupported_operation', 'invalid_parameters', 'authorization_denied',
    'confirmation_required', 'busy', 'credential_unavailable',
    'identity_unverified', 'identity_changed', 'certificate_changed',
    'timeout', 'transport_failed', 'tls_unavailable', 'certificate_unavailable',
    'certificate_invalid', 'certificate_mismatch', 'headers_too_large',
    'body_too_large', 'invalid_http', 'unsupported_encoding',
    'unsupported_content_type', 'invalid_multipart', 'incomplete_response',
    'invalid_json', 'redirect_refused', 'http_error', 'token_unavailable',
    'unsupported_application_contract',
    'application_not_allowlisted', 'application_identity_changed', 'application_not_uninstalled',
    'catalog_version_changed', 'package_contract_unverified', 'installation_plan_changed',
    'idempotency_required', 'idempotency_conflict', 'idempotency_capacity',
))


class BusinessTransportError(ValueError):
    """Only fixed reason codes, without upstream strings or credentials."""

    def __init__(self, reason, http_status=None, *, request_sent=False):
        self.reason = reason if reason in _REASONS else 'transport_failed'
        self.http_status = (http_status if type(http_status) is int
                            and 100 <= http_status <= 599 else None)
        self.request_sent = request_sent is True
        super().__init__(self.reason)


def safe_text(value, maximum=255):
    """User-visible label, excluding control/surrogate and query separators.

    The scraper converts names into an internal URL without URL escaping, so
    separators are rejected even though the outer transport uses JSON.
    """
    if (type(value) is not str or not value.strip() or value != value.strip()
            or len(value.encode('utf-8', errors='replace')) > maximum
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or any(c in value for c in ('&', '#', '?', '%', '=', '\\', '/'))):
        raise BusinessTransportError('invalid_parameters')
    return value


def _integer(value, low=0, high=2 ** 53 - 1):
    if type(value) is not int or not low <= value <= high:
        raise BusinessTransportError('invalid_parameters')
    return value


def _album_id(value):
    if type(value) is not str or not re.fullmatch(r'[0-9]{1,10}', value) or int(value) <= 7:
        raise BusinessTransportError('invalid_parameters')
    return value


def _validate(operation, parameters, body):
    """Return detached canonical values before authorizing or reading auth DB."""
    if type(operation) is not str or operation not in OPERATIONS:
        raise BusinessTransportError('unsupported_operation')
    if type(parameters) is not dict or type(body) is not dict:
        raise BusinessTransportError('invalid_parameters')
    params, payload = dict(parameters), dict(body)
    if operation in ('albums_list', 'shared_albums_list', 'movie_libraries_list',
                     'backup_tasks_list', 'auto_backup_get', 'nas_users_list',
                     'device_name_get', 'nas_ai_get', 'module_inventory'):
        valid = not params and not payload
    elif operation == 'albums_create':
        valid = set(params) == {'album_name'} and not payload
        if valid:
            params['album_name'] = safe_text(params['album_name'])
    elif operation == 'albums_rename':
        valid = set(params) == {'album_id', 'album_new_name'} and not payload
        if valid:
            params['album_id'] = _album_id(params['album_id'])
            params['album_new_name'] = safe_text(params['album_new_name'])
    elif operation == 'movie_library_rename':
        valid = not params and set(payload) == {'lid', 'name'}
        if valid:
            payload = {'lid': _integer(payload['lid'], 1), 'name': safe_text(payload['name'])}
    elif operation == 'share_revoke':
        valid = not params and set(payload) == {'share_id_list'}
        if valid:
            ids = payload['share_id_list']
            valid = (type(ids) is list and len(ids) == 1 and type(ids[0]) is str
                     and re.fullmatch(r'[A-Za-z0-9_-]{1,127}', ids[0]) is not None)
            if valid:
                payload = {'share_id_list': list(ids)}
    elif operation == 'apply_list':
        valid = set(params) <= {'opttype'} and not payload
        if valid:
            params = {'opttype': _integer(params.get('opttype', 0), 0, 0)}
    elif operation == 'application_progress':
        valid = not params and set(payload) == {'apply_id'}
        if valid:
            payload = {'apply_id': _integer(payload['apply_id'], 1, 2 ** 31 - 1)}
    elif operation == 'application_install':
        # Fixed audited ordinary modules only. These fields are copied from a
        # freshly cross-checked catalog by the executor, never HTTP arguments.
        valid = not params and set(payload) == {'client_id', 'flag', 'size', 'apply_id',
            'module_name', 'vid', 'module_type', 'source', 'version', 'guest'}
        if valid:
            spec = (APPLICATION_INSTALL_SPECS.get(payload['apply_id'])
                    if type(payload['apply_id']) is int else None)
            valid = (spec is not None and payload['client_id'] == 'r1-toolbox'
                     and payload['module_name'] == spec['module_name']
                     and all(type(payload[key]) is int and payload[key] == 0
                             for key in ('flag', 'module_type', 'source', 'guest'))
                     and type(payload['vid']) is str
                     and re.fullmatch(r'[A-Za-z0-9._-]{0,63}', payload['vid']) is not None)
            if valid:
                payload['size'] = _integer(payload['size'], 0, 2 ** 31 - 1)
                payload['version'] = safe_text(payload['version'], 63)
    elif operation == 'device_name_set':
        valid = set(params) == {'name'} and not payload
        if valid:
            params = {'name': safe_text(params['name'], 63)}
    elif operation == 'nas_ai_set':
        valid = set(params) == {'mode'} and not payload
        if valid:
            params = {'mode': _integer(params['mode'], 0, 1)}
    elif operation == 'movie_scan_status':
        valid = not params and set(payload) == {'lid'}
        if valid:
            payload = {'lid': _integer(payload['lid'], 1)}
    elif operation == 'album_delete':
        valid = set(params) == {'album_id'} and not payload
        if valid:
            params = {'album_id': _album_id(params['album_id'])}
    elif operation == 'backup_task_halt':
        valid = set(params) == {'task_id', 'task_status'} and not payload
        if valid:
            params = {'task_id': _integer(params['task_id'], 1),
                      'task_status': _integer(params['task_status'], 1, 2)}
    elif operation == 'auto_backup_disable':
        valid = set(params) == {'disk_type', 'status'} and not payload
        if valid:
            valid = type(params['disk_type']) is str and params['disk_type'] in ('udisk_a', 'sd')
            params = {'disk_type': params['disk_type'], 'status': _integer(params['status'], 0, 0)}
    else:
        # Application mutations stay unavailable until their audited contract.
        raise BusinessTransportError('unsupported_application_contract')
    if not valid:
        raise BusinessTransportError('invalid_parameters')
    return params, payload


def _identity(value):
    if (type(value) is not dict or value.get('available') is not True
            or value.get('status') != 'verified'
            or type(value.get('pid')) is not int or value['pid'] <= 0
            or type(value.get('start_ticks')) is not int or value['start_ticks'] <= 0
            or type(value.get('socket_inode')) is not str
            or not re.fullmatch(r'[1-9][0-9]{0,23}', value['socket_inode'])
            or value.get('connect_host') not in ('127.0.0.1', '::1')):
        raise BusinessTransportError('identity_unverified')
    return tuple(value[k] for k in ('pid', 'start_ticks', 'socket_inode', 'connect_host'))


class VendorBusinessClient:
    """One fixed owner request at a time; test seams are constructor-only."""

    def __init__(self, root='/', *, session_reader=None, listener_check=None,
                 certificate_reader=None, socket_factory=None, tls_context=None,
                 clock=None):
        self.root = root
        self._session = session_reader or read_session
        self._listener = listener_check or require_emhistor_listener
        self._certificate = certificate_reader or _certificate
        self._socket = socket_factory or socket.socket
        self._context = tls_context
        self._clock = clock or time.monotonic

    def request(self, operation_id, parameters=None, body=None, authorizer=None,
                *, confirmed=False):
        params, payload = _validate(operation_id, {} if parameters is None else parameters,
                                    {} if body is None else body)
        route, action, method, mutation = OPERATIONS[operation_id]
        if mutation and confirmed is not True:
            raise BusinessTransportError('confirmation_required')
        if not callable(authorizer):
            raise BusinessTransportError('authorization_denied')

        def authorized():
            try:
                # Fresh copies prevent a callback from changing the sent action.
                allowed = authorizer(operation_id, dict(params), json.loads(json.dumps(payload)))
            except Exception:
                allowed = False
            if allowed is not True:
                raise BusinessTransportError('authorization_denied')

        authorized()
        deadline = self._clock() + TIMEOUT

        def remaining():
            value = deadline - self._clock()
            if value <= 0:
                raise BusinessTransportError('timeout')
            return value

        if not _LOCK.acquire(timeout=remaining()):
            raise BusinessTransportError('busy')
        connection = raw = None
        request_sent = False
        try:
            if ssl is None:
                raise BusinessTransportError('tls_unavailable')
            before = _identity(self._listener(root=self.root, port=PORT))
            remaining()
            certificate = self._certificate(self.root)
            context = self._context
            if context is None:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            host = before[3]
            raw = self._socket(socket.AF_INET6 if host == '::1' else socket.AF_INET,
                               socket.SOCK_STREAM)
            raw.settimeout(remaining())
            raw.connect((host, PORT))
            raw.settimeout(remaining())
            connection = context.wrap_socket(raw, server_hostname=None)
            if connection.getpeercert(binary_form=True) != certificate:
                raise BusinessTransportError('certificate_mismatch')
            if _identity(self._listener(root=self.root, port=PORT)) != before:
                raise BusinessTransportError('identity_changed')
            if self._certificate(self.root) != certificate:
                raise BusinessTransportError('certificate_changed')
            remaining()
            session, status = self._session(root=self.root)
            if (session is None or getattr(session, 'user_id', None) != OWNER_ID
                    or type(status) is not dict or status.get('available') is not True
                    or status.get('status') != 'ready'):
                raise BusinessTransportError('credential_unavailable')
            token = session.derive_access_token()
            if type(token) is not str or not 1 <= len(token) <= 4096:
                raise BusinessTransportError('token_unavailable')
            # Authorization can change while waiting for the lock or TLS.
            authorized()
            if _identity(self._listener(root=self.root, port=PORT)) != before:
                raise BusinessTransportError('identity_changed')
            query = urlencode({'action': action, **params}, quote_via=quote)
            request_path = route + '?' + query + '&access_token=' + quote(token, safe='')
            encoded = json.dumps(payload, ensure_ascii=True, separators=(',', ':')).encode('ascii')
            content = (b'Content-Type: application/json\r\nContent-Length: '
                       + str(len(encoded)).encode('ascii') + b'\r\n') if method == 'POST' else b''
            request = ((method + ' ' + request_path + ' HTTP/1.1\r\n'
                        'Host: 127.0.0.1:20443\r\nAccept: application/json, multipart/x-mixed-replace\r\n'
                        'Accept-Encoding: identity\r\nConnection: close\r\n').encode('ascii')
                       + content + b'\r\n' + (encoded if method == 'POST' else b''))
            if len(request) > MAX_REQUEST:
                raise BusinessTransportError('invalid_parameters')
            # Proc identity sampling may itself outlast a concurrent policy
            # edit. Check the current grant again immediately before sending.
            authorized()
            connection.settimeout(remaining())
            # sendall may fail after a partial request. Never retry that write.
            request_sent = True
            connection.sendall(request)
            # The existing bounded parser uses monotonic absolute time too.
            wire = _WireReader(connection, time.monotonic() + remaining())
            raw_headers = wire.until(b'\r\n\r\n', MAX_HEADERS)
            status_code, headers = _headers(raw_headers, response=True)
            if 300 <= status_code <= 399:
                raise BusinessTransportError('redirect_refused', status_code)
            if status_code != 200:
                raise BusinessTransportError('http_error', status_code)
            frame = _first_frame(wire, headers, MAX_HEADERS - len(raw_headers) - 4)
            if _identity(self._listener(root=self.root, port=PORT)) != before:
                raise BusinessTransportError('identity_changed')
            remaining()
            return status_code, frame
        except BusinessTransportError as error:
            error.request_sent = request_sent
            raise
        except MediaProbeError as error:
            raise BusinessTransportError(error.reason, error.http_status,
                                         request_sent=request_sent) from None
        except (TimeoutError, socket.timeout):
            raise BusinessTransportError('timeout', request_sent=request_sent) from None
        except Exception:
            raise BusinessTransportError('transport_failed', request_sent=request_sent) from None
        finally:
            for channel in (connection, raw):
                if channel is not None:
                    try:
                        channel.close()
                    except OSError:
                        pass
            _LOCK.release()

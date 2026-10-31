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
    # 固定转码诊断 getter；TV 会省略不适用的开关查询，原厂内部可能读内容/写元数据。
    'video_capacity': ('/rest/1.1/config', 'get_all_capacity', 'GET', False),
    'transcode_switch': ('/rest/1.1/config', 'get_trvideo_switch', 'GET', False),
    'offline_video_path': ('/rest/1.1/config', 'get_offlinevideo_path', 'GET', False),
})
_PROFILES = ('raw', 'high', 'mid', 'low')
MAX_MEDIA_PATH = 1024
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
# 只读诊断与其它调用方共用的公开原因词表。
REASONS = _REASONS


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


def media_path(value):
    """Absolute vendor media path inside the audited admin data boundary.

    This is string validation only: nothing is opened, stat-ed or resolved here,
    so it grants no local file access. The caller passes the value to the vendor
    as a query parameter; the vendor applies its own lookup rules.
    """
    if type(value) is not str or not 10 <= len(value) <= MAX_MEDIA_PATH:
        raise BusinessTransportError('invalid_parameters')
    if value != value.strip() or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise BusinessTransportError('invalid_parameters')
    parts = value.split('/')
    # ['', 'drives', <disk>, 'Users', 'admin', ...] 且至少一个子路径。
    if (len(parts) < 6 or parts[0] or parts[1] != 'drives' or parts[3] != 'Users'
            or parts[4] != 'admin'):
        raise BusinessTransportError('invalid_parameters')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', parts[2]):
        raise BusinessTransportError('invalid_parameters')
    if any(part in ('', '.', '..') for part in parts[1:]):
        raise BusinessTransportError('invalid_parameters')
    return value


def _profile(value):
    if type(value) is not str or value not in _PROFILES:
        raise BusinessTransportError('invalid_parameters')
    return value


def _terminal(value):
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', value) is None:
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
                     'device_name_get', 'nas_ai_get', 'module_inventory',
                     'transcode_switch'):
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
    elif operation == 'video_capacity':
        # 固定 0：只要能力摘要，不按文件展开。
        valid = set(params) <= {'capacity_detail'} and not payload
        if valid:
            params = {'capacity_detail': _integer(params.get('capacity_detail', 0), 0, 0)}
    elif operation == 'offline_video_path':
        valid = not payload and set(params) == {
            'video_capability', 'path', 'offset', 'sharpness', 'terminal'}
        if valid:
            params = {'video_capability': _integer(params['video_capability'], 1, 1),
                      'path': media_path(params['path']),
                      'offset': _integer(params['offset'], 0, 2 ** 31 - 1),
                      'sharpness': _profile(params['sharpness']),
                      'terminal': _terminal(params['terminal'])}
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


_TRACE_OPERATIONS = frozenset(('video_capacity', 'transcode_switch', 'offline_video_path'))
_LISTENER_PHASES = ('initial', 'connected', 'before_send', 'after_response')
_TRACE_PHASES = frozenset(_LISTENER_PHASES + (
    'validate', 'authorization', 'lock', 'tls', 'certificate', 'credentials',
    'send', 'response', 'deadline'))
_TRACE_FLAGS = ('request_started', 'send_attempted', 'request_sent',
                'send_completed', 'response_started', 'response_received')
_SCAN_COUNTERS = ('seen', 'read_failed', 'vanished', 'generation_changed',
    'identity_invalid', 'identity_changed', 'executable_changed', 'fd_seen', 'fd_read_failed',
    'fd_vanished', 'fd_directory_failed',
    'table_changed', 'deadline', 'pid_limit', 'fd_limit')


def _bounded_number(value, maximum=2 ** 53 - 1):
    return value if type(value) is int and 0 <= value <= maximum else None


def _enum(value, allowed, default=None):
    return value if type(value) is str and value in allowed else default


def _public_identity(value):
    if type(value) is not dict:
        return None
    pid, start = value.get('pid'), value.get('start_ticks')
    inode = value.get('socket_inode')
    if (type(pid) is not int or not 0 < pid <= 2 ** 31 - 1
            or type(start) is not int or not 0 < start <= 2 ** 53 - 1
            or type(inode) is not str or re.fullmatch(r'[1-9][0-9]{0,23}', inode) is None):
        return None
    return {'pid': pid, 'start_ticks': start, 'socket_inode': inode}


def _public_runtime(value):
    """Project only the fixed production listener observer, never hook text."""
    if type(value) is not dict or type(value.get('schema')) is not int or value['schema'] != 1:
        return None
    result = {'schema': 1}
    budget = value.get('budget')
    if type(budget) is dict:
        result['budget'] = {key: _bounded_number(budget.get(key)) for key in (
            'scan_limit_ms', 'external_remaining_ms', 'effective_limit_ms',
            'max_processes', 'max_fds', 'max_bytes')}
    scan = value.get('scan')
    if type(scan) is dict:
        # These are fixed production outcomes, not arbitrary /proc errors.
        statuses = ('not_run', 'passed', 'failed', 'complete', 'partial')
        result['scan'] = {'status': _enum(scan.get('status'), statuses, 'not_run'),
            'complete': scan.get('complete') if type(scan.get('complete')) is bool else None,
            'elapsed_ms': _bounded_number(scan.get('elapsed_ms')),
            'reason_codes': [], 'counters': {}}
        reasons = scan.get('reason_codes')
        allowed = frozenset(('fd_limit', 'deadline', 'target_fd_vanished',
            'fd_read_failed', 'fd_directory_unavailable', 'initial_table_incomplete',
            'process_identity_unavailable', 'pid_limit', 'process_records_truncated',
            'process_identity_changed', 'final_table_incomplete', 'listener_table_changed',
            'final_owner_fd_unverified', 'final_process_identity_changed',
            'proc_directory_unavailable', 'attribute_deadline', 'attribute_nonregular',
            'attribute_byte_limit', 'attribute_vanished', 'attribute_permission_denied',
            'attribute_read_failed', 'listener_snapshots_truncated'))
        if type(reasons) is list:
            result['scan']['reason_codes'] = list(dict.fromkeys(
                reason for reason in reasons[:32] if type(reason) is str and reason in allowed))
        counters = scan.get('counters')
        if type(counters) is dict:
            result['scan']['counters'] = {key: _bounded_number(counters.get(key))
                                       for key in _SCAN_COUNTERS}
    listener = value.get('listener')
    if type(listener) is dict:
        result['listener'] = {'status': _enum(listener.get('status'), (
            'not_run', 'passed', 'failed', 'verified', 'unverified', 'changed',
            'unavailable', 'missing', 'multiple', 'unknown', 'proc_unavailable',
            'proc_partial', 'not_listening', 'ambiguous', 'owner_unverified'), 'not_run'),
            'port': PORT, 'identity': _public_identity(listener.get('identity'))}
        for key in ('listener_count', 'owner_count', 'matching_owner_count'):
            result['listener'][key] = _bounded_number(listener.get(key))
        matches = listener.get('owner_matches')
        result['listener']['owner_matches'] = matches if type(matches) is bool else None
        final = listener.get('final')
        if type(final) is dict:
            result['listener']['final'] = {'status': _enum(final.get('status'), (
                'not_run', 'passed', 'failed'), 'not_run')}
            for key in ('executable_path_matches', 'generation_matches'):
                item = final.get(key)
                result['listener']['final'][key] = item if type(item) is bool else None
    tables = value.get('tables')
    if type(tables) is dict:
        result['tables'] = {key: _enum(tables.get(key), (
            'passed', 'failed', 'not_run'), 'not_run') for key in ('initial', 'final')}
    processes = value.get('processes')
    if type(processes) is list:
        result['processes'] = []
        for process in processes[:16]:
            if type(process) is not dict:
                continue
            pid, start = process.get('pid'), process.get('start_ticks')
            if (type(pid) is not int or not 0 < pid <= 2 ** 31 - 1
                    or type(start) is not int or not 0 < start <= 2 ** 53 - 1):
                continue
            item = {'pid': pid, 'start_ticks': start}
            item['final_start_ticks'] = _bounded_number(process.get('final_start_ticks'))
            matched = process.get('executable_name_matches')
            item['executable_name_matches'] = matched if type(matched) is bool else None
            for key in ('after_scan_identity', 'final_identity', 'final_fd'):
                item[key] = _enum(process.get(key), ('passed', 'failed', 'not_run'), 'not_run')
            result['processes'].append(item)
    snapshots = value.get('listener_snapshots')
    if type(snapshots) is dict:
        result['listener_snapshots'] = {}
        for phase in ('initial', 'final'):
            entries = snapshots.get(phase)
            result['listener_snapshots'][phase] = []
            if type(entries) is not list:
                continue
            for entry in entries[:16]:
                if type(entry) is not dict:
                    continue
                inode, address = entry.get('socket_inode'), entry.get('address')
                if (type(inode) is not str or re.fullmatch(r'[1-9][0-9]{0,23}', inode) is None
                        or _enum(address, ('127.0.0.1', '0.0.0.0', '::1', '::')) is None):
                    continue
                result['listener_snapshots'][phase].append({'socket_inode': inode,
                    'address': address, 'owner_count': _bounded_number(entry.get('owner_count'))})
    return result


def public_trace(value):
    """Safe detached trace for the fixed explicit diagnostic contract.

    ``request_sent`` retains the conservative mutation contract: sendall was
    attempted and may have sent only part. ``send_completed`` is stricter.
    A parsed first frame sets response_received; any received bytes separately
    set response_started, even if framing or identity subsequently rejects it.
    """
    source = value if type(value) is dict else {}
    result = {'schema': 1, **{key: source.get(key) is True for key in _TRACE_FLAGS},
              'discarded_at': None, 'error_reason': None, 'listener_checks': {}}
    if type(source.get('discarded_at')) is str and source['discarded_at'] in _TRACE_PHASES:
        result['discarded_at'] = source['discarded_at']
    if type(source.get('error_reason')) is str and source['error_reason'] in _REASONS:
        result['error_reason'] = source['error_reason']
    checks = source.get('listener_checks')
    for phase in _LISTENER_PHASES:
        item = checks.get(phase) if type(checks) is dict else None
        item = item if type(item) is dict else {}
        reason = item.get('reason_code')
        matched = item.get('expected_matches')
        result['listener_checks'][phase] = {
            'status': _enum(item.get('status'), ('passed', 'failed', 'not_run'), 'not_run'),
            'reason_code': reason if type(reason) is str and reason in _REASONS else None,
            'elapsed_ms': _bounded_number(item.get('elapsed_ms')),
            'runtime': _public_runtime(item.get('runtime')),
            'identity': _public_identity(item.get('identity')),
            'expected_matches': matched if type(matched) is bool else None}
    return result


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
                *, confirmed=False, observer=None):
        # Diagnostics are opt-in for the three audited getters only. Other
        # native business calls keep their existing response and call contract.
        trace = (public_trace(None) if type(operation_id) is str
                 and operation_id in _TRACE_OPERATIONS and type(observer) is dict else None)
        if trace is not None:
            observer.clear()
            observer.update(public_trace(trace))
        try:
            return self._perform_request(operation_id, parameters, body, authorizer,
                                         confirmed=confirmed, trace=trace)
        except BusinessTransportError as error:
            if trace is not None and trace['error_reason'] is None:
                trace.update(discarded_at=('authorization'
                    if error.reason == 'authorization_denied' else 'validate'),
                    error_reason=error.reason)
            raise
        finally:
            if trace is not None:
                observer.clear()
                observer.update(public_trace(trace))

    def _check_listener(self, phase, deadline, trace, expected=None):
        """Observe the same production predicate, never use observations as proof."""
        runtime = {} if trace is not None else None
        item = trace['listener_checks'][phase] if trace is not None else None
        started = self._clock()
        try:
            kwargs = {'root': self.root, 'port': PORT}
            if trace is not None:
                kwargs.update(observer=runtime, deadline=deadline)
            value = self._listener(**kwargs)
            identity = _identity(value)
            if item is not None:
                item['identity'] = _public_identity(value)
                item['expected_matches'] = identity == expected if expected is not None else None
            if expected is not None and identity != expected:
                raise BusinessTransportError('identity_changed')
            if item is not None:
                item['status'] = 'passed'
            return identity
        except Exception as error:
            if item is not None:
                reason = ('timeout' if isinstance(error, (TimeoutError, socket.timeout))
                          else error.reason if isinstance(error, BusinessTransportError)
                          else 'transport_failed')
                item.update(status='failed', reason_code=reason)
            raise
        finally:
            if item is not None:
                item['elapsed_ms'] = max(0, int((self._clock() - started) * 1000))
                item['runtime'] = _public_runtime(runtime)

    def _perform_request(self, operation_id, parameters, body, authorizer, *, confirmed, trace):
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

        try:
            authorized()
        except BusinessTransportError as error:
            if trace is not None:
                trace.update(discarded_at='authorization', error_reason=error.reason)
            raise
        if trace is not None:
            trace['request_started'] = True
        deadline = self._clock() + TIMEOUT

        def remaining():
            value = deadline - self._clock()
            if value <= 0:
                raise BusinessTransportError('timeout')
            return value

        try:
            if not _LOCK.acquire(timeout=remaining()):
                raise BusinessTransportError('busy')
        except BusinessTransportError as error:
            if trace is not None:
                trace.update(discarded_at='lock', error_reason=error.reason)
            raise
        connection = raw = wire = None
        request_sent = False
        phase = 'tls'
        try:
            if ssl is None:
                raise BusinessTransportError('tls_unavailable')
            phase = 'initial'
            before = self._check_listener(phase, deadline, trace)
            remaining()
            phase = 'certificate'
            certificate = self._certificate(self.root)
            context = self._context
            if context is None:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            host = before[3]
            phase = 'tls'
            raw = self._socket(socket.AF_INET6 if host == '::1' else socket.AF_INET,
                               socket.SOCK_STREAM)
            raw.settimeout(remaining())
            raw.connect((host, PORT))
            raw.settimeout(remaining())
            connection = context.wrap_socket(raw, server_hostname=None)
            if connection.getpeercert(binary_form=True) != certificate:
                raise BusinessTransportError('certificate_mismatch')
            phase = 'connected'
            self._check_listener(phase, deadline, trace, before)
            phase = 'certificate'
            if self._certificate(self.root) != certificate:
                raise BusinessTransportError('certificate_changed')
            remaining()
            phase = 'credentials'
            session, status = self._session(root=self.root)
            if (session is None or getattr(session, 'user_id', None) != OWNER_ID
                    or type(status) is not dict or status.get('available') is not True
                    or status.get('status') != 'ready'):
                raise BusinessTransportError('credential_unavailable')
            token = session.derive_access_token()
            if type(token) is not str or not 1 <= len(token) <= 4096:
                raise BusinessTransportError('token_unavailable')
            # Authorization can change while waiting for the lock or TLS.
            phase = 'authorization'
            authorized()
            phase = 'before_send'
            self._check_listener(phase, deadline, trace, before)
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
            phase = 'authorization'
            authorized()
            phase = 'deadline'
            connection.settimeout(remaining())
            # sendall may fail after a partial request. Never retry that write.
            phase = 'send'
            request_sent = True
            if trace is not None:
                trace.update(send_attempted=True, request_sent=True)
            connection.sendall(request)
            if trace is not None:
                trace['send_completed'] = True
            # The existing bounded parser uses monotonic absolute time too.
            phase = 'response'
            wire = _WireReader(connection, time.monotonic() + remaining())
            raw_headers = wire.until(b'\r\n\r\n', MAX_HEADERS)
            status_code, headers = _headers(raw_headers, response=True)
            if 300 <= status_code <= 399:
                raise BusinessTransportError('redirect_refused', status_code)
            if status_code != 200:
                raise BusinessTransportError('http_error', status_code)
            frame = _first_frame(wire, headers, MAX_HEADERS - len(raw_headers) - 4)
            if trace is not None:
                trace['response_received'] = True
            phase = 'after_response'
            self._check_listener(phase, deadline, trace, before)
            phase = 'authorization'
            authorized()
            phase = 'deadline'
            remaining()
            return status_code, frame
        except BusinessTransportError as error:
            error.request_sent = request_sent
            if trace is not None:
                trace.update(discarded_at=phase, error_reason=error.reason)
            raise
        except MediaProbeError as error:
            if trace is not None:
                trace.update(discarded_at=phase, error_reason=(error.reason
                    if error.reason in _REASONS else 'transport_failed'))
            raise BusinessTransportError(error.reason, error.http_status,
                                         request_sent=request_sent) from None
        except (TimeoutError, socket.timeout):
            if trace is not None:
                trace.update(discarded_at=phase, error_reason='timeout')
            raise BusinessTransportError('timeout', request_sent=request_sent) from None
        except Exception:
            if trace is not None:
                trace.update(discarded_at=phase, error_reason='transport_failed')
            raise BusinessTransportError('transport_failed', request_sent=request_sent) from None
        finally:
            if trace is not None and wire is not None:
                trace['response_started'] = wire.received > 0
            for channel in (connection, raw):
                if channel is not None:
                    try:
                        channel.close()
                    except OSError:
                        pass
            _LOCK.release()

    def probe_listener(self, authorizer, *, observer=None):
        """Explicit fixed proc-only check, without credentials, TLS or requests.

        It shares the native transport lock and absolute window. This is a
        diagnostic snapshot only; a future request still performs every check.
        """
        trace = public_trace(None)
        if type(observer) is dict:
            observer.clear()
            observer.update(public_trace(trace))
        phase = 'authorization'
        acquired = False
        deadline = self._clock() + TIMEOUT

        def authorized():
            try:
                allowed = authorizer('probe_listener', {}, {}) if callable(authorizer) else False
            except Exception:
                allowed = False
            if allowed is not True:
                raise BusinessTransportError('authorization_denied')

        def remaining():
            value = deadline - self._clock()
            if value <= 0:
                raise BusinessTransportError('timeout')
            return value

        try:
            authorized()
            trace['request_started'] = True
            phase = 'lock'
            acquired = _LOCK.acquire(timeout=remaining())
            if not acquired:
                raise BusinessTransportError('busy')
            phase = 'authorization'
            authorized()
            phase = 'initial'
            identity = self._check_listener(phase, deadline, trace)
            phase = 'authorization'
            authorized()
            phase = 'deadline'
            remaining()
            return {'available': True, 'status': 'verified',
                **dict(zip(('pid', 'start_ticks', 'socket_inode', 'connect_host'), identity))}
        except BusinessTransportError as error:
            trace.update(discarded_at=phase, error_reason=error.reason)
            raise
        except (TimeoutError, socket.timeout):
            trace.update(discarded_at=phase, error_reason='timeout')
            raise BusinessTransportError('timeout') from None
        except Exception:
            trace.update(discarded_at=phase, error_reason='transport_failed')
            raise BusinessTransportError('transport_failed') from None
        finally:
            if acquired:
                _LOCK.release()
            if type(observer) is dict:
                observer.clear()
                observer.update(public_trace(trace))

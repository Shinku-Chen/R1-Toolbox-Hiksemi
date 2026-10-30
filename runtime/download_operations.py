"""Explicit task operations for the two already verified local downloaders.

This is separate from passive business polling. Native paths, tracker URLs and
credentials never leave this module. A mutation requires the version obtained
from a fresh task read, an exact task ID and a caller-owned idempotency key.
No operation accepts a daemon address, RPC method, Cookie, arbitrary option,
global selector, file deletion or destination path.
"""

import copy
import hashlib
import json
import re
import socket
import threading
import time
from urllib.parse import urlencode

import vendor_download_diagnostics as wire
from api_permissions import MIN_ROLE_BY_TOOL, permission_allows


READ_TOOLS = frozenset(('list_download_tasks', 'get_download_task', 'get_download_limits'))
WRITE_TOOLS = frozenset(('pause_download_task', 'resume_download_task',
                         'remove_download_task', 'set_download_task_limits', 'set_download_limits',
                         'recheck_download_task', 'rename_download_task',
                         'set_download_task_force_start', 'set_download_task_queue_position'))
TOOLS = READ_TOOLS | WRITE_TOOLS
_QB_ID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z', re.ASCII)
_ARIA_ID = re.compile(r'[0-9a-f]{16}\Z', re.ASCII)
_VERSION = re.compile(r'download_[0-9a-f]{32}\Z', re.ASCII)
_LIMIT_VERSION = re.compile(r'download_limits_[0-9a-f]{32}\Z', re.ASCII)
_IDEMPOTENCY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z', re.ASCII)
_MAX_TASKS = 128
_MAX_RESPONSE = 512 * 1024
_ARIA_KEYS = list(wire.KEYS) + ['files', 'bittorrent', 'dir']
_ARIA_LOCK = threading.Lock()


def _error(code, *, uncertain=False):
    messages = {
        'invalid_arguments': '下载操作参数无效。',
        'invalid_key': '操作期间授权已失效。',
        'task_not_found': '当前引擎中没有该任务。',
        'task_changed': '任务身份已变化，请重新读取后操作。',
        'settings_changed': '下载器限速配置已变化，请重新读取后操作。',
        'idempotency_conflict': '此幂等标识已用于另一项操作。',
        'limit_exceeded': '任务响应或本进程操作记录达到上限。',
        'operation_busy': '该下载器正在处理另一项采集或操作。',
        'result_unconfirmed': '请求可能已发送，实际结果尚未确认；请读取任务状态，勿盲目重放。',
    }
    message = messages.get(code, wire.REASONS.get(code, '下载服务请求未完成。'))
    return {'ok': False, 'error': {'code': code, 'message': message,
                                  'retryable': False if uncertain else code in ('operation_busy', 'auth_busy')},
            **({'result_unconfirmed': True} if uncertain else {})}


class _OperationError(Exception):
    def __init__(self, code):
        self.code = code


def _int(value, maximum=2 ** 63 - 1):
    if type(value) is int and 0 <= value <= maximum:
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,19}', value) and int(value) <= maximum:
        return int(value)
    return None


def _text(value, maximum=256):
    return value if isinstance(value, str) and 0 < len(value) <= maximum and all(c.isprintable() for c in value) else None


def _qb_limit(value):
    # The torrent-info API documents -1 as unlimited. Our write API uses 0
    # for unlimited, so normalize this proven sentinel, never missing data.
    return 0 if type(value) is int and value == -1 else _int(value)


def valid_arguments(tool, arguments):
    """Validate again at the executor boundary, independently of MCP schemas."""
    if tool not in TOOLS or not isinstance(arguments, dict):
        return False
    engine = arguments.get('engine')
    if engine not in ('qbittorrent', 'aria2'):
        return False
    if tool == 'list_download_tasks':
        return (not set(arguments) - {'engine', 'offset', 'limit'}
                and type(arguments.get('offset', 0)) is int and 0 <= arguments.get('offset', 0) <= 300
                and type(arguments.get('limit', 50)) is int and 1 <= arguments.get('limit', 50) <= 100)
    if tool == 'get_download_limits':
        return set(arguments) == {'engine'}
    global_limits = tool == 'set_download_limits'
    qb_only = tool in ('recheck_download_task', 'rename_download_task',
                      'set_download_task_force_start', 'set_download_task_queue_position')
    if qb_only and engine != 'qbittorrent':
        return False
    task = arguments.get('task_id')
    if not global_limits and (not isinstance(task, str) or not (_QB_ID if engine == 'qbittorrent' else _ARIA_ID).fullmatch(task)):
        return False
    if tool == 'get_download_task':
        return set(arguments) == {'engine', 'task_id'}
    required = {'engine', 'expected_version', 'confirmed', 'idempotency_key'}
    if not global_limits:
        required.add('task_id')
    extra = {'download_limit_bps', 'upload_limit_bps'} if tool in ('set_download_task_limits', 'set_download_limits') else set()
    extra |= {'rename_download_task': {'name'}, 'set_download_task_force_start': {'enabled'},
              'set_download_task_queue_position': {'position'}}.get(tool, set())
    if (set(arguments) - (required | extra) or required - set(arguments)
            or arguments['confirmed'] is not True
            or not isinstance(arguments['expected_version'], str)
            or not (_LIMIT_VERSION if global_limits else _VERSION).fullmatch(arguments['expected_version'])
            or not isinstance(arguments['idempotency_key'], str) or not _IDEMPOTENCY.fullmatch(arguments['idempotency_key'])):
        return False
    if tool == 'rename_download_task':
        return (set(arguments) == required | extra and _text(arguments.get('name'), 255) is not None
                and not any(char in arguments['name'] for char in ('/', '\\')))
    if tool == 'set_download_task_force_start':
        return set(arguments) == required | extra and type(arguments.get('enabled')) is bool
    if tool == 'set_download_task_queue_position':
        return set(arguments) == required | extra and arguments.get('position') in ('increase', 'decrease', 'top', 'bottom')
    if tool in ('set_download_task_limits', 'set_download_limits'):
        chosen = extra & set(arguments)
        return bool(chosen) and all(type(arguments[key]) is int and 0 <= arguments[key] <= 2 ** 40 for key in chosen)
    return True


def _project(engine, identity, row):
    """Only bounded task metadata is public; absolute filesystem names stay private."""
    if not isinstance(row, dict):
        raise _OperationError('invalid_response')
    task_id = row.get('hash') if engine == 'qbittorrent' else row.get('gid')
    pattern = _QB_ID if engine == 'qbittorrent' else _ARIA_ID
    if not isinstance(task_id, str) or not pattern.fullmatch(task_id):
        raise _OperationError('invalid_response')
    if engine == 'qbittorrent':
        state = row.get('state')
        if state not in wire.QB_STATES:
            raise _OperationError('invalid_response')
        total, done = _int(row.get('total_size', row.get('size'))), _int(row.get('completed'))
        name = _text(row.get('name'))
        immutable = [row.get(key) for key in ('hash', 'added_on', 'name', 'total_size', 'size', 'save_path')]
        down, up = _int(row.get('dlspeed')), _int(row.get('upspeed'))
    else:
        state = row.get('status')
        if state not in ('active', 'waiting', 'paused', 'error', 'complete', 'removed'):
            raise _OperationError('invalid_response')
        total, done = _int(row.get('totalLength')), _int(row.get('completedLength'))
        bittorrent = row.get('bittorrent')
        info = bittorrent.get('info') if isinstance(bittorrent, dict) else None
        name = _text(info.get('name')) if isinstance(info, dict) else None
        immutable = [row.get(key) for key in ('gid', 'totalLength', 'dir')]
        files = row.get('files')
        if files is not None and (not isinstance(files, list) or len(files) > 4096):
            raise _OperationError('limit_exceeded')
        # Include path identities only in the private version digest. Never
        # disclose paths, URI lists or derive an absolute filename for output.
        immutable.append([[f.get('index'), f.get('path'), f.get('length')] for f in files or [] if isinstance(f, dict)])
        down, up = _int(row.get('downloadSpeed')), _int(row.get('uploadSpeed'))
    if total is not None and done is not None and done > total:
        raise _OperationError('invalid_response')
    version = hashlib.sha256(json.dumps([engine, identity, immutable], sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:32]
    return {'task_id': task_id, 'version': 'download_' + version, 'engine': engine,
            'name': name, 'state': state, 'total_bytes': total, 'completed_bytes': done,
            'progress': done / total if total and done is not None else None,
            'download_bytes_per_second': down, 'upload_bytes_per_second': up,
            'configured_download_limit_bytes_per_second': _qb_limit(row.get('dl_limit')) if engine == 'qbittorrent' else None,
            'configured_upload_limit_bytes_per_second': _qb_limit(row.get('up_limit')) if engine == 'qbittorrent' else None,
            'location': None, 'location_available': False}


class DownloadOperations:
    """Real fixed-protocol executor; callbacks and addresses are never HTTP input."""

    def __init__(self, *, authorizer, root='/', ownership_check=None,
                 socket_factory=None, monotonic=None, auth_state=None):
        self.authorizer, self.root = authorizer, root
        if ownership_check is None:
            from vendor_runtime import require_one_listener
            ownership_check = require_one_listener
        self.ownership_check = ownership_check
        self.socket_factory = socket_factory or socket.create_connection
        self.clock = monotonic or time.monotonic
        self.auth_state = wire._QB_AUTH_STATE if auth_state is None else auth_state
        self._records, self._record_lock = {}, threading.Lock()

    def _gate(self, principal):
        if self.authorizer(principal) is not True:
            raise _OperationError('invalid_key')

    def _identity(self, engine):
        return wire._identity(self.ownership_check, self.root,
                              port=wire.QB_PORT if engine == 'qbittorrent' else wire.PORT,
                              comm='qbittorrent-nox' if engine == 'qbittorrent' else 'aria2c-nox')

    def _verify(self, engine, identity, principal):
        self._gate(principal)
        if self._identity(engine) != identity:
            if engine == 'qbittorrent':
                self.auth_state._forget()
            raise _OperationError('identity_changed')
        self._gate(principal)

    def _qb(self, method, endpoint, values, deadline, identity, principal, *, category='torrents'):
        self._verify('qbittorrent', identity, principal)
        host, sid = identity[3], self.auth_state._sid
        authority = ('[::1]' if host == '::1' else host) + ':' + str(wire.QB_PORT)
        encoded = urlencode(values).encode('ascii')
        if category not in ('torrents', 'transfer'):
            raise _OperationError('invalid_arguments')
        path = '/api/v2/' + category + '/' + endpoint
        if method == 'GET' and values:
            path += '?' + encoded.decode('ascii')
        body = encoded if method == 'POST' else b''
        cookie = 'Cookie: SID=' + sid + '\r\n' if sid else ''
        request = (method + ' ' + path + ' HTTP/1.1\r\nHost: ' + authority + '\r\n'
                   + 'Origin: http://' + authority + '\r\nReferer: http://' + authority + '/\r\n'
                   + cookie + 'Accept: application/json, text/plain\r\nAccept-Encoding: identity\r\n'
                   + 'Content-Type: application/x-www-form-urlencoded\r\nConnection: close\r\n'
                   + 'Content-Length: ' + str(len(body)) + '\r\n\r\n').encode('ascii') + body
        response = wire._http_exchange(request, wire.QB_PORT, _MAX_RESPONSE, deadline,
                                       self.clock, self.socket_factory, host=host,
                                       before_send=lambda: self._verify('qbittorrent', identity, principal))
        self._verify('qbittorrent', identity, principal)
        return response

    def _qb_session(self, identity, deadline, principal):
        credentials, credential_key = wire._qb_credentials(None)
        state = self.auth_state
        state._bind(identity, credential_key)
        self._verify('qbittorrent', identity, principal)
        try:
            version = wire._qb_get(wire.QB_PATHS[0], deadline, self.clock, self.socket_factory,
                                   host=identity[3], sid=state._sid,
                                   before_send=lambda: self._verify('qbittorrent', identity, principal))
        except wire._Failure as error:
            if error.status != 'auth_required':
                raise
            state._forget()
            self._verify('qbittorrent', identity, principal)
            if self.clock() < state._retry_at:
                raise _OperationError('auth_backoff')
            try:
                sid = wire._qb_login(credentials, deadline, self.clock, self.socket_factory, host=identity[3],
                                     before_send=lambda: self._verify('qbittorrent', identity, principal))
                self._verify('qbittorrent', identity, principal)
                state._sid = sid
                version = wire._qb_get(wire.QB_PATHS[0], deadline, self.clock, self.socket_factory,
                                       host=identity[3], sid=sid,
                                       before_send=lambda: self._verify('qbittorrent', identity, principal))
            except _OperationError as error:
                state._forget()
                if error.code != 'invalid_key':
                    state._backoff(self.clock())
                raise
            except Exception:
                state._backoff(self.clock())
                raise
        self._verify('qbittorrent', identity, principal)
        try:
            value = version.decode('ascii').strip()
        except UnicodeError:
            raise _OperationError('invalid_response') from None
        match = re.fullmatch(r'v([0-9]{1,3})\.[0-9]{1,3}(?:\.[0-9]{1,3})?(?:[A-Za-z0-9.+_-]{0,24})', value)
        if not match or int(match[1]) not in (4, 5):
            raise _OperationError('invalid_response')
        return int(match[1])

    def _aria(self, method, params, deadline, identity, principal):
        self._verify('aria2', identity, principal)
        request_id = 'r1-task-operation'
        frame = wire._http_rpc({'jsonrpc': '2.0', 'id': request_id,
                                'method': method, 'params': params},
                               deadline, self.clock, self.socket_factory,
                               before_send=lambda: self._verify('aria2', identity, principal))
        self._verify('aria2', identity, principal)
        return wire._result(frame, request_id)

    def _one(self, engine, task, deadline, identity, principal):
        if engine == 'qbittorrent':
            rows = wire._json(self._qb('GET', 'info', {'hashes': task, 'limit': 2}, deadline, identity, principal))
            if rows == []:
                raise _OperationError('task_not_found')
            if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('hash') != task:
                raise _OperationError('invalid_response')
            return rows[0]
        value = self._aria('aria2.tellStatus', [task, _ARIA_KEYS], deadline, identity, principal)
        if not isinstance(value, dict) or value.get('gid') != task:
            raise _OperationError('invalid_response')
        return value

    def _list(self, arguments, deadline, identity, principal):
        engine, offset, limit = arguments['engine'], arguments.get('offset', 0), arguments.get('limit', 50)
        truncated = False
        if engine == 'qbittorrent':
            rows = wire._json(self._qb('GET', 'info', {'filter': 'all', 'sort': 'hash',
                                     'limit': limit + 1, 'offset': offset}, deadline, identity, principal))
            if not isinstance(rows, list) or len(rows) > limit + 1:
                raise _OperationError('invalid_response')
            truncated = len(rows) > limit
            selected = rows[:limit]
        else:
            rows = self._aria('aria2.tellActive', [_ARIA_KEYS], deadline, identity, principal)
            if not isinstance(rows, list) or len(rows) > _MAX_TASKS:
                raise _OperationError('limit_exceeded')
            for method in ('aria2.tellWaiting', 'aria2.tellStopped'):
                group = self._aria(method, [0, _MAX_TASKS + 1, _ARIA_KEYS], deadline, identity, principal)
                if not isinstance(group, list) or len(group) > _MAX_TASKS + 1:
                    raise _OperationError('limit_exceeded')
                truncated |= len(group) > _MAX_TASKS
                rows += group[:_MAX_TASKS]
            selected = rows[offset:offset + limit]
            truncated |= len(rows) > offset + limit
        projected = [_project(engine, identity, row) for row in selected]
        if len({row['task_id'] for row in projected}) != len(projected):
            raise _OperationError('invalid_response')
        return {'ok': True, 'engine': engine, 'cached': False, 'tasks': projected,
                'offset': offset, 'limit': limit, 'has_more': truncated,
                'next_offset': offset + len(projected) if truncated else None}

    def _limits(self, engine, deadline, identity, principal):
        """Project configured engine limits only, never preferences or secrets."""
        if engine == 'qbittorrent':
            values = []
            for endpoint in ('downloadLimit', 'uploadLimit'):
                values.append(_int(wire._json(self._qb('GET', endpoint, {}, deadline, identity, principal, category='transfer'))))
        else:
            native = self._aria('aria2.getGlobalOption', [], deadline, identity, principal)
            if not isinstance(native, dict):
                raise _OperationError('invalid_response')
            values = [_int(native.get(name)) for name in ('max-overall-download-limit', 'max-overall-upload-limit')]
        version = hashlib.sha256(json.dumps([engine, identity, values], sort_keys=True).encode()).hexdigest()[:32]
        return {'engine': engine, 'download_limit_bps': values[0], 'upload_limit_bps': values[1],
                'version': 'download_limits_' + version}

    def execute(self, tool, arguments, principal):
        if not valid_arguments(tool, arguments):
            return _error('invalid_arguments')
        if not permission_allows(principal, 'downloads', MIN_ROLE_BY_TOOL[tool], tool_name=tool):
            return _error('permission_denied')
        try:
            self._gate(principal)
        except _OperationError as error:
            return _error(error.code)
        engine, writing = arguments['engine'], tool in WRITE_TOOLS
        record_key = None
        if writing:
            fields = ('key_id', 'grant_revision', 'credential_generation')
            if any(principal.get(key) is None for key in fields):
                return _error('invalid_key')
            record_key = tuple(principal[key] for key in fields) + (arguments['idempotency_key'],)
        lock = self.auth_state._lock if engine == 'qbittorrent' else _ARIA_LOCK
        if not lock.acquire(blocking=False):
            return _error('operation_busy')
        sent = False
        reserved = False
        fingerprint = None
        try:
            fingerprint = hashlib.sha256(json.dumps([tool, arguments], sort_keys=True).encode()).hexdigest()
            if record_key is not None:
                with self._record_lock:
                    previous = self._records.get(record_key)
                    if previous:
                        self._gate(principal)
                        if previous[0] != fingerprint:
                            return _error('idempotency_conflict')
                        return copy.deepcopy(previous[1]) if previous[1] is not None else _error('operation_busy')
                    if len(self._records) >= 256:
                        return _error('limit_exceeded')
                    self._records[record_key] = (fingerprint, None)
                    reserved = True
            deadline = self.clock() + (wire.QB_NETWORK_SECONDS if engine == 'qbittorrent' else wire.NETWORK_SECONDS)
            identity = self._identity(engine)
            major = self._qb_session(identity, deadline, principal) if engine == 'qbittorrent' else None
            if tool == 'list_download_tasks':
                result = self._list(arguments, deadline, identity, principal)
            elif tool in ('get_download_limits', 'set_download_limits'):
                limits = self._limits(engine, deadline, identity, principal)
                result = {'ok': True, 'cached': False, 'limits': limits}
                if writing:
                    if limits['version'] != arguments['expected_version']:
                        raise _OperationError('settings_changed')
                    selected = [name for name in ('download_limit_bps', 'upload_limit_bps') if name in arguments]
                    if any(limits[name] is None for name in selected):
                        raise _OperationError('invalid_response')
                    self._verify(engine, identity, principal)
                    sent = True
                    if engine == 'qbittorrent':
                        for name, endpoint in (('download_limit_bps', 'setDownloadLimit'), ('upload_limit_bps', 'setUploadLimit')):
                            if name in arguments:
                                response = self._qb('POST', endpoint, {'limit': arguments[name]}, deadline,
                                                    identity, principal, category='transfer')
                                if response.strip() not in (b'', b'Ok.'):
                                    raise _OperationError('invalid_response')
                    else:
                        changes = {native: str(arguments[name]) for name, native in
                                   (('download_limit_bps', 'max-overall-download-limit'),
                                    ('upload_limit_bps', 'max-overall-upload-limit')) if name in arguments}
                        if self._aria('aria2.changeGlobalOption', [changes], deadline, identity, principal) != 'OK':
                            raise _OperationError('invalid_response')
                    result = {'ok': True, 'engine': engine, 'accepted': True, 'effect_confirmed': False,
                              'next_step': 'get_download_limits'}
            else:
                task = arguments['task_id']
                current = self._one(engine, task, deadline, identity, principal)
                projected = _project(engine, identity, current)
                if not writing:
                    result = {'ok': True, 'cached': False, 'task': projected}
                else:
                    if projected['version'] != arguments['expected_version']:
                        raise _OperationError('task_changed')
                    self._verify(engine, identity, principal)
                    # Latch before handing any mutation to the transport. Any
                    # later exception is uncertain and cannot be auto-replayed.
                    sent = True
                    if engine == 'qbittorrent':
                        changes = []
                        if tool == 'set_download_task_limits':
                            for field, endpoint in (('download_limit_bps', 'setDownloadLimit'), ('upload_limit_bps', 'setUploadLimit')):
                                if field in arguments:
                                    changes.append((endpoint, {'hashes': task, 'limit': arguments[field]}))
                        elif tool in ('rename_download_task', 'set_download_task_force_start', 'set_download_task_queue_position'):
                            if tool == 'rename_download_task':
                                changes = [('rename', {'hash': task, 'name': arguments['name']})]
                            elif tool == 'set_download_task_force_start':
                                changes = [('setForceStart', {'hashes': task, 'value': str(arguments['enabled']).lower()})]
                            else:
                                endpoint = {'increase': 'increasePrio', 'decrease': 'decreasePrio',
                                            'top': 'topPrio', 'bottom': 'bottomPrio'}[arguments['position']]
                                changes = [(endpoint, {'hashes': task})]
                        else:
                            endpoint = {'pause_download_task': 'stop' if major == 5 else 'pause',
                                        'resume_download_task': 'start' if major == 5 else 'resume',
                                        'remove_download_task': 'delete', 'recheck_download_task': 'recheck'}[tool]
                            changes = [(endpoint, {'hashes': task, **({'deleteFiles': 'false'} if tool == 'remove_download_task' else {})})]
                        for endpoint, values in changes:
                            response = self._qb('POST', endpoint, values, deadline, identity, principal)
                            if response.strip() not in (b'', b'Ok.'):
                                raise _OperationError('invalid_response')
                    else:
                        if tool == 'set_download_task_limits':
                            options = {native: str(arguments[key]) for key, native in
                                       (('download_limit_bps', 'max-download-limit'), ('upload_limit_bps', 'max-upload-limit')) if key in arguments}
                            value = self._aria('aria2.changeOption', [task, options], deadline, identity, principal)
                            if value != 'OK':
                                raise _OperationError('invalid_response')
                        else:
                            method = {'pause_download_task': 'aria2.pause', 'resume_download_task': 'aria2.unpause',
                                      'remove_download_task': 'aria2.remove'}[tool]
                            stopped_removal = tool == 'remove_download_task' and current.get('status') in ('complete', 'error', 'removed')
                            if stopped_removal:
                                method = 'aria2.removeDownloadResult'
                            value = self._aria(method, [task], deadline, identity, principal)
                            if value != ('OK' if stopped_removal else task):
                                raise _OperationError('invalid_response')
                    # Native acceptance is not proof that an async state change
                    # has settled. Clients obtain actual state with the read tool.
                    result = {'ok': True, 'engine': engine, 'task_id': task, 'accepted': True,
                              'effect_confirmed': False, 'delete_files': False,
                              'next_step': 'get_download_task'}
            self._verify(engine, identity, principal)
        except _OperationError as error:
            result = _error('result_unconfirmed' if sent else error.code, uncertain=sent)
        except wire._Failure as error:
            if engine == 'qbittorrent' and error.status == 'auth_required':
                self.auth_state._forget()
            result = _error('result_unconfirmed' if sent else error.status, uncertain=sent)
        except (TimeoutError, socket.timeout):
            result = _error('result_unconfirmed' if sent else 'timeout', uncertain=sent)
        except Exception:
            result = _error('result_unconfirmed' if sent else 'transport_error', uncertain=sent)
        finally:
            if reserved:
                with self._record_lock:
                    if sent:
                        self._records[record_key] = (fingerprint, copy.deepcopy(result))
                    else:
                        self._records.pop(record_key, None)
            lock.release()
        try:
            self._gate(principal)
        except _OperationError:
            return _error('invalid_key')
        return result

"""Bounded Docker operations on owner-registered projects, without shell/exec.

Plans/tasks are process-local, credential-bound records. Engine JSON, Compose,
environment and host paths remain private; public records contain fixed fields.
"""

import copy
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
import time
from urllib.parse import quote, unquote

import api_docker_projects as projects


PLAN_SECONDS = 120
JOB_SECONDS = 120
HEALTH_SECONDS = 30
MAX_RECORDS = 128
MAX_ACTIVE_TASKS = 8
_LOCKS, _LOCKS_GUARD = {}, threading.Lock()
_CID = re.compile(r'[0-9a-f]{64}\Z')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_CONTAINER_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_IDEMPOTENCY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_TOOLS = {'control_docker_project', 'plan_docker_project_update',
          'execute_docker_project_update', 'get_docker_project_task'}
_MESSAGES = {
    **projects._MESSAGES,
    'invalid_key': '操作期间授权已失效，请先核对实际状态。',
    'plan_expired': '更新计划已过期，需要重新规划。',
    'plan_conflict': '更新计划或项目状态已变化，未重复执行。',
    'task_unavailable': '当前密钥没有可读取的对应任务。',
    'idempotency_conflict': '幂等标识已用于不同操作。',
    'write_backend_unverified': '该项目配置或容器能力超出已核验的安全更新范围。',
    'result_unconfirmed': '已发起操作但最终状态尚未确认；不得盲目重试。',
}


class OperationError(Exception):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'native_unavailable'
        super().__init__(_MESSAGES[self.code])


def _error(code):
    code = code if code in _MESSAGES else 'native_unavailable'
    return {'code': code, 'message': _MESSAGES[code],
            'retryable': code in ('resource_busy', 'timeout')}


def _failure(code):
    return {'ok': False, 'error': _error(code)}


def _exchange_function():
    """Require the Linux atomic exchange primitive; never emulate with replace."""
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
        function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        function.restype = ctypes.c_int
        return function
    except (AttributeError, OSError):
        raise OperationError('write_backend_unverified') from None


def _rename_exchange(source_name, target_name, *, directory_fd):
    """Swap both existing entries atomically, retaining the displaced version."""
    result = _exchange_function()(directory_fd, os.fsencode(source_name), directory_fd,
                                  os.fsencode(target_name), 2)  # RENAME_EXCHANGE
    if result != 0:
        raise OSError(ctypes.get_errno(), 'configuration exchange failed')


def _stamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat(
        timespec='milliseconds').replace('+00:00', 'Z')


def _owner(principal):
    if (not isinstance(principal, dict) or not isinstance(principal.get('key_id'), str)
            or not re.fullmatch(r'[0-9a-f]{24}', principal['key_id'])
            or any(type(principal.get(key)) is not int or principal[key] < 1
                   for key in ('credential_generation', 'grant_revision'))):
        raise OperationError('permission_denied')
    return tuple(principal[key] for key in ('key_id', 'credential_generation', 'grant_revision'))


def _repository(reference):
    if not isinstance(reference, str) or not projects._IMAGE.fullmatch(reference):
        raise OperationError('invalid_arguments')
    name = reference.split('@', 1)[0]
    if ':' in name.rsplit('/', 1)[-1]:
        name = name.rsplit(':', 1)[0]
    return name


class LocalDockerWriter(projects.LocalDockerReader):
    """Use the reader's fixed socket/peer proof for allowlisted Engine writes."""

    def context(self):
        return {'peer': None, 'bytes': 0, 'mutations': 0, 'deadline': self._clock() + JOB_SECONDS}

    @staticmethod
    def _allowed(method, path):
        if method == 'GET' and (path == '/info' or re.fullmatch(r'/containers/[0-9a-f]{64}/json', path)):
            return True
        if method == 'GET' and path.startswith('/images/') and path.endswith('/json'):
            image = unquote(path[8:-5])
            return bool(_DIGEST.fullmatch(image) or projects._IMAGE.fullmatch(image))
        if method == 'POST' and re.fullmatch(r'/containers/[0-9a-f]{64}/(?:start|stop\?t=10|restart\?t=10)', path):
            return True
        if method == 'POST' and re.fullmatch(r'/containers/[0-9a-f]{64}/rename\?name=[A-Za-z0-9_.-]{1,128}', path):
            return True
        if method == 'POST' and re.fullmatch(r'/containers/create\?name=[A-Za-z0-9_.-]{1,128}', path):
            return True
        if method == 'POST' and path.startswith('/images/create?fromImage='):
            image = unquote(path[len('/images/create?fromImage='):])
            return bool(projects._IMAGE.fullmatch(image) and '@sha256:' in image)
        return method == 'DELETE' and bool(re.fullmatch(r'/containers/[0-9a-f]{64}\?v=0&force=0', path))

    def request(self, method, path, context, *, body=None, authorizer=None, stream=False):
        if not self._allowed(method, path) or method != 'POST' and body is not None:
            raise OperationError('invalid_arguments')
        payload = b'' if body is None else json.dumps(body, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(payload) > projects.MAX_BODY:
            raise OperationError('limit_exceeded')
        connection = self._connection(context['deadline'])
        try:
            peer = self._peer_check(connection)
            if context['peer'] is not None and context['peer'] != peer:
                raise OperationError('project_binding_changed')
            context['peer'] = peer
            if method != 'GET' and (authorizer is None or authorizer() is not True):
                raise OperationError('invalid_key')
            headers = (f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\n'
                       'Accept-Encoding: identity\r\nConnection: close\r\n'
                       f'Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n')
            connection.settimeout(projects._remaining(context['deadline'], self._clock))
            if method != 'GET':
                context['mutations'] += 1
            connection.sendall(headers.encode('ascii') + payload)
            buffer = bytearray()

            def receive():
                connection.settimeout(projects._remaining(context['deadline'], self._clock))
                chunk = connection.recv(8192)
                if not chunk:
                    raise OperationError('native_unavailable')
                context['bytes'] += len(chunk)
                if context['bytes'] > projects.MAX_TOTAL:
                    raise OperationError('limit_exceeded')
                buffer.extend(chunk)

            def line(limit=projects.MAX_HEADER):
                while b'\r\n' not in buffer:
                    if len(buffer) > limit:
                        raise OperationError('limit_exceeded')
                    receive()
                end = buffer.index(b'\r\n')
                if end > limit:
                    raise OperationError('limit_exceeded')
                result = bytes(buffer[:end])
                del buffer[:end + 2]
                return result

            match = re.fullmatch(rb'HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?', line(128))
            if not match:
                raise OperationError('native_unavailable')
            status = int(match[1])
            headers, header_size = {}, 0
            while True:
                item = line()
                header_size += len(item) + 2
                if header_size > projects.MAX_HEADER:
                    raise OperationError('limit_exceeded')
                if not item:
                    break
                key, separator, value = item.partition(b':')
                key, value = key.lower(), value.strip()
                if (not separator or not re.fullmatch(rb'[a-z0-9-]+', key) or key in headers
                        or any(char < 32 or char == 127 for char in value)):
                    raise OperationError('native_unavailable')
                headers[key] = value
            if status not in (200, 201, 204, 304):
                # Never parse or reflect an engine error body or headers.
                raise OperationError('plan_conflict' if status in (404, 409) else 'native_unavailable')
            if headers.get(b'content-encoding', b'identity').lower() != b'identity':
                raise OperationError('native_unavailable')

            def exact(length):
                if length > projects.MAX_BODY:
                    raise OperationError('limit_exceeded')
                while len(buffer) < length:
                    receive()
                value = bytes(buffer[:length])
                del buffer[:length]
                return value

            if status in (204, 304):
                if headers.get(b'content-length', b'0') != b'0' or b'transfer-encoding' in headers:
                    raise OperationError('native_unavailable')
                raw = b''
            elif b'content-length' in headers and b'transfer-encoding' not in headers:
                size = headers[b'content-length']
                if not re.fullmatch(rb'[0-9]{1,10}', size):
                    raise OperationError('native_unavailable')
                raw = exact(int(size))
            elif headers.get(b'transfer-encoding', b'').lower() == b'chunked' and b'content-length' not in headers:
                raw = bytearray()
                while True:
                    size = line(32)
                    if not re.fullmatch(rb'[0-9a-fA-F]{1,8}', size):
                        raise OperationError('native_unavailable')
                    size = int(size, 16)
                    if len(raw) + size > projects.MAX_BODY:
                        raise OperationError('limit_exceeded')
                    if not size:
                        if line(2):
                            raise OperationError('native_unavailable')
                        break
                    raw.extend(exact(size))
                    if exact(2) != b'\r\n':
                        raise OperationError('native_unavailable')
            else:
                raise OperationError('native_unavailable')
            if buffer or self._peer_check(connection) != peer:
                raise OperationError('project_binding_changed')
            projects._remaining(context['deadline'], self._clock)
            if not raw:
                return {}
            if headers.get(b'content-type', b'').split(b';', 1)[0].strip().lower() != b'application/json':
                raise OperationError('native_unavailable')
            if stream:
                rows = bytes(raw).splitlines()
                if len(rows) > 4096:
                    raise OperationError('limit_exceeded')
                for row in rows:
                    value = projects._json(row)
                    if not isinstance(value, dict) or 'error' in value or 'errorDetail' in value:
                        raise OperationError('native_unavailable')
                return {}
            return projects._json(raw)
        except (TimeoutError, socket.timeout):
            raise OperationError('timeout') from None
        finally:
            connection.close()


class DockerProjectOperations:
    def __init__(self, registry, *, authorizer, backend=None, clock=None):
        self.registry = registry
        self.authorizer = authorizer
        reader = registry.backend
        self.backend = backend or LocalDockerWriter(connect=getattr(reader, '_connect', None),
            peer_check=getattr(reader, '_peer_check', None), config_identity=getattr(reader, '_config_identity', None))
        self.clock = clock or time.monotonic
        self._state_lock = threading.RLock()
        self._plans, self._tasks, self._idempotency = {}, {}, {}
        self._threads, self._closed = set(), threading.Event()

    def close(self):
        """Prevent subsequent background side effects; never undo unknown work."""
        self._closed.set()

    def _current(self, principal):
        try:
            valid = self.authorizer(principal)
        except Exception:
            valid = False
        if valid is not True or self._closed.is_set():
            raise OperationError('invalid_key')
        return True

    def _grant(self, principal, tool, project_id, operation, *, write=True):
        self._current(principal)
        owner = _owner(principal)
        if (tool not in _TOOLS or type(principal.get('policy_version')) is not int
                or principal['policy_version'] != 3 or principal.get('role') not in ('readonly', 'readwrite', 'admin')
                or write and principal['role'] != 'admin'
                or not isinstance(principal.get('scopes'), list) or 'docker' not in principal['scopes']
                or not isinstance(principal.get('tool_grants'), list) or tool not in principal['tool_grants']
                or not isinstance(project_id, str)
                or not projects._PROJECT.fullmatch(project_id)):
            raise OperationError('permission_denied')
        grants = principal.get('docker_projects')
        if not isinstance(grants, list):
            raise OperationError('permission_denied')
        matches = [row for row in grants if isinstance(row, dict) and row.get('id') == project_id]
        if len(matches) != 1 or not isinstance(matches[0].get('operations'), list) or operation not in matches[0]['operations']:
            raise OperationError('permission_denied')
        return owner

    @staticmethod
    def _protected(detail):
        config = detail.get('Config') if isinstance(detail, dict) else None
        if not isinstance(config, dict):
            return True
        image = str(config.get('Image') or '').split('@', 1)[0].rsplit('/', 1)[-1].split(':', 1)[0]
        labels = config.get('Labels') or {}
        return (image in ('r1-toolbox', 'r1-boot-once')
                or isinstance(labels, dict) and labels.get('org.opencontainers.image.title') == 'R1 Toolbox'
                or any(isinstance(row, dict) and row.get('Name') == 'r1-toolbox-data'
                       for row in detail.get('Mounts') or []))

    def _project(self, project_id, deadline=None):
        with self.registry._lock:
            rows = [row for row in self.registry._read()['projects'] if row['id'] == project_id and not row['revoked']]
            if len(rows) != 1 or rows[0]['protected']:
                raise OperationError('permission_denied')
            registered = copy.deepcopy(rows[0])
        live = [row for row in self.backend.snapshot(deadline=deadline) if row['binding'] == registered['binding']]
        if len(live) != 1 or live[0]['protected']:
            raise OperationError('project_binding_changed')
        return registered, live[0]

    def _project_lock(self, project_id):
        key = (os.path.normcase(os.path.abspath(self.registry.path)), project_id)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise OperationError('resource_busy')
        return lock

    def _task(self, project_id, owner):
        with self._state_lock:
            if len(self._tasks) >= MAX_RECORDS:
                raise OperationError('limit_exceeded')
            task = {'task_id': 'task_' + secrets.token_hex(16), 'project_id': project_id,
                    'state': 'pending', 'running': None, 'verified_target_digest': None,
                    'verified_config_digest': None, 'verified_image_id': None,
                    'updated_at': _stamp(), 'error': None}
            self._tasks[task['task_id']] = {'owner': owner, 'public': task}
            return task

    def _finish(self, task, state, *, error=None, **fields):
        with self._state_lock:
            task.update(state=state, error=_error(error) if error else None, updated_at=_stamp(), **fields)

    def _publish(self, principal, task):
        try:
            self._current(principal)
            with self._state_lock:
                return {'ok': True, 'task': copy.deepcopy(task)}
        except OperationError as error:
            return _failure(error.code)

    def _idempotent(self, principal, project_id, operation, key):
        if not isinstance(key, str) or not _IDEMPOTENCY.fullmatch(key):
            raise OperationError('invalid_arguments')
        identity = (_owner(principal), key)
        signature = projects._hash([project_id, operation])
        with self._state_lock:
            old = self._idempotency.get(identity)
            if old is not None:
                if old['signature'] != signature:
                    raise OperationError('idempotency_conflict')
                return copy.deepcopy(self._tasks[old['task_id']]['public']), identity, signature
        return None, identity, signature

    def _reserve_task(self, principal, project_id, operation, key):
        """Atomically bind a credential/key to exactly one request and task.

        Project locks do not serialize different projects or control/update
        calls. Recheck the shared key while retaining the state lock through
        task creation and association, before any side effect is possible.
        """
        with self._state_lock:
            previous, identity, signature = self._idempotent(principal, project_id, operation, key)
            if previous is not None:
                return previous, False
            self._current(principal)
            task = self._task(project_id, _owner(principal))
            self._idempotency[identity] = {'signature': signature, 'task_id': task['task_id']}
            return task, True

    def _engine(self, context, engine):
        if self.backend.request('GET', '/info', context).get('ID') != engine:
            raise OperationError('project_binding_changed')

    def control(self, principal, project_id, operation, confirmed, idempotency_key):
        lock, task, mutated = None, None, False
        try:
            self._grant(principal, 'control_docker_project', project_id, 'control')
            if operation not in ('start', 'stop', 'restart') or confirmed is not True:
                raise OperationError('invalid_arguments')
            lock = self._project_lock(project_id)
            previous, unused, unused2 = self._idempotent(principal, project_id, operation, idempotency_key)
            if previous:
                return self._publish(principal, previous)
            registered, live = self._project(project_id)
            task, reserved = self._reserve_task(principal, project_id, operation, idempotency_key)
            if not reserved:
                return self._publish(principal, task)
            self._finish(task, 'running')
            context = self.backend.context()
            services = registered['identity']['services']
            for service in services:
                self._current(principal)
                current, unused = self._project(project_id, context['deadline'])
                if current['binding'] != registered['binding']:
                    raise OperationError('project_binding_changed')
                self._engine(context, registered['identity']['engine'])
                detail = self.backend.request('GET', '/containers/' + service['container'] + '/json', context)
                if self._protected(detail):
                    raise OperationError('permission_denied')
                path = '/containers/' + service['container'] + '/' + operation + ('?t=10' if operation != 'start' else '')
                mutated = True  # An ambiguous transport failure may follow acceptance.
                self.backend.request('POST', path, context, authorizer=lambda: self._current(principal))
            current, actual = self._project(project_id, context['deadline'])
            expected = operation != 'stop'
            if actual['running'] is not expected:
                raise OperationError('result_unconfirmed')
            self._finish(task, 'completed', running=actual['running'])
            self._current(principal)
            return self._publish(principal, task)
        except (OperationError, projects.ProjectError) as error:
            if task is not None:
                self._finish(task, 'result_unconfirmed' if mutated else 'failed', error=error.code)
                if error.code != 'invalid_key':
                    return self._publish(principal, task)
            return _failure(error.code)
        except Exception:
            if task is not None:
                self._finish(task, 'result_unconfirmed' if mutated else 'failed', error='native_unavailable')
                return self._publish(principal, task)
            return _failure('native_unavailable')
        finally:
            if lock is not None:
                lock.release()

    def get_task(self, principal, project_id, task_id):
        try:
            owner = self._grant(principal, 'get_docker_project_task', project_id, 'inspect', write=False)
            with self.registry._lock:
                if not any(row['id'] == project_id and not row['revoked'] for row in self.registry._read()['projects']):
                    raise OperationError('permission_denied')
            with self._state_lock:
                item = self._tasks.get(task_id) if isinstance(task_id, str) else None
                if item is None or item['owner'] != owner or item['public']['project_id'] != project_id:
                    raise OperationError('task_unavailable')
                result = copy.deepcopy(item['public'])
            self._current(principal)
            return {'ok': True, 'task': result}
        except (OperationError, projects.ProjectError) as error:
            return _failure(error.code)

    def _compose(self, registered):
        """Audit one identity-bound service without executing Compose/YAML code."""
        _exchange_function()
        expected = registered['identity']['configuration']
        path = expected['path']
        if self.backend._config_identity(path) != expected:
            raise OperationError('project_binding_changed')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        try:
            info = os.fstat(descriptor)
            identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022
                    or identity != expected['identity']):
                raise OperationError('write_backend_unverified')
            raw = os.read(descriptor, projects.MAX_BODY + 1)
            if len(raw) != info.st_size or len(raw) > projects.MAX_BODY or b'${' in raw:
                raise OperationError('write_backend_unverified')
            if self.backend._config_identity(path) != expected:
                raise OperationError('project_binding_changed')
            try:
                value = projects._json(raw)
            except projects.ProjectError:
                value = self._yaml_image(raw, registered['identity']['services'][0]['name'])
            if (not isinstance(value, dict) or set(value) - {'name', 'version', 'services', 'volumes', 'networks', '_yaml'}
                    or not isinstance(value.get('services'), dict) or len(value['services']) != 1):
                raise OperationError('write_backend_unverified')
            service_name = registered['identity']['services'][0]['name']
            service = value['services'].get(service_name)
            allowed = {'image', 'container_name', 'restart', 'environment', 'ports', 'volumes', 'networks',
                       'command', 'entrypoint', 'user', 'working_dir', 'read_only', 'hostname',
                       'labels', 'healthcheck', 'logging', 'tmpfs', 'stop_grace_period', 'cap_drop', 'security_opt'}
            if not isinstance(service, dict) or set(service) - allowed:
                raise OperationError('write_backend_unverified')
            return raw, value, info
        finally:
            os.close(descriptor)

    @staticmethod
    def _yaml_image(raw, service_name):
        """Recognize a conservative block mapping and one literal image line.

        Other bytes are retained exactly, not reinterpreted. Merge keys, tags,
        aliases, multiple documents, interpolation and unfamiliar service keys
        fail closed. This is deliberately not a general-purpose YAML parser.
        """
        if b'${' in raw:
            raise OperationError('write_backend_unverified')
        try:
            lines = raw.decode('utf-8', 'strict').splitlines(keepends=True)
        except UnicodeError:
            raise OperationError('write_backend_unverified') from None
        top, services, service_keys, image_line, active = set(), [], set(), None, None
        allowed = {'image', 'container_name', 'restart', 'environment', 'ports', 'volumes', 'networks',
                   'command', 'entrypoint', 'user', 'working_dir', 'read_only', 'hostname',
                   'labels', 'healthcheck', 'logging', 'tmpfs', 'stop_grace_period', 'cap_drop', 'security_opt'}
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if ('\t' in line or stripped in ('---', '...') or '<<:' in line
                    or re.search(r'(^|\s)[&*!][^\s]*', line)):
                raise OperationError('write_backend_unverified')
            indent = len(line) - len(line.lstrip(' '))
            if indent == 0:
                match = re.fullmatch(r'([a-z_]+):(?:\s+(.*))?\s*', line.rstrip('\r\n'))
                if not match or match[1] not in ('version', 'name', 'services', 'volumes', 'networks') or match[1] in top:
                    raise OperationError('write_backend_unverified')
                active = match[1]
                top.add(active)
                if active == 'services' and match[2] and not match[2].lstrip().startswith('#'):
                    raise OperationError('write_backend_unverified')
            elif active == 'services' and indent == 2:
                match = re.fullmatch(r'  ([a-z0-9][a-z0-9_-]{0,63}):\s*(?:#.*)?', line.rstrip('\r\n'))
                if not match:
                    raise OperationError('write_backend_unverified')
                services.append(match[1])
            elif active == 'services' and indent == 4:
                match = re.fullmatch(r'    ([a-z_]+):(?:\s+(.*))?\s*', line.rstrip('\r\n'))
                if not match or match[1] not in allowed or match[1] in service_keys:
                    raise OperationError('write_backend_unverified')
                service_keys.add(match[1])
                if match[1] == 'image':
                    text = (match[2] or '').split(' #', 1)[0].strip()
                    if text.startswith('"'):
                        try:
                            text = json.loads(text)
                        except ValueError:
                            raise OperationError('write_backend_unverified') from None
                    elif text.startswith("'") and text.endswith("'"):
                        text = text[1:-1].replace("''", "'")
                    if not isinstance(text, str) or not projects._IMAGE.fullmatch(text):
                        raise OperationError('write_backend_unverified')
                    image_line = (index, text)
            elif active == 'services' and indent < 6:
                raise OperationError('write_backend_unverified')
        if services != [service_name] or image_line is None:
            raise OperationError('write_backend_unverified')
        return {'services': {service_name: {'image': image_line[1]}},
                '_yaml': {'lines': lines, 'image_line': image_line[0]}}

    @staticmethod
    def _compose_bytes(document):
        if '_yaml' in document:
            lines = list(document['_yaml']['lines'])
            service = next(iter(document['services'].values()))
            index = document['_yaml']['image_line']
            newline = '\r\n' if lines[index].endswith('\r\n') else '\n'
            lines[index] = '    image: ' + service['image'] + newline
            return ''.join(lines).encode('utf-8')
        return json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8') + b'\n'

    @staticmethod
    def _network_configuration(detail):
        # Network connect/disconnect changes inspect outside HostConfig. Bind the
        # stable endpoint configuration, excluding dynamic IP/endpoint counters.
        networks = detail.get('NetworkSettings', {}).get('Networks') or {}
        if isinstance(networks, dict):
            return {name: {**{key: network.get(key) for key in
                ('NetworkID', 'IPAMConfig', 'Links', 'DriverOpts')},
                'Aliases': network.get('Aliases') or [], 'GwPriority': network.get('GwPriority') or 0}
                if isinstance(network, dict) else network for name, network in networks.items()}
        return networks

    @staticmethod
    def _mount_configuration(detail):
        mounts = detail.get('Mounts')
        if not isinstance(mounts, list) or any(not isinstance(mount, dict) for mount in mounts):
            raise OperationError('write_backend_unverified')
        return sorted(({key: mount.get(key) for key in
            ('Type', 'Name', 'Source', 'Destination', 'Driver', 'RW', 'Propagation')}
            for mount in mounts), key=lambda value: str(value['Destination']))

    @staticmethod
    def _fingerprint(detail):
        value = {key: detail.get(key) for key in ('Id', 'Name', 'Image', 'Config', 'HostConfig', 'Mounts')}
        value['Networks'] = DockerProjectOperations._network_configuration(detail)
        return projects._hash(value)

    def _prepare(self, principal, registered, live, target, context):
        if len(registered['identity']['services']) != 1:
            raise OperationError('write_backend_unverified')
        self._current(principal)
        old = registered['identity']['services'][0]
        self._engine(context, registered['identity']['engine'])
        detail = self.backend.request('GET', '/containers/' + old['container'] + '/json', context)
        projects._container(detail, old['container'], registered['identity']['name'], self.backend._config_identity)
        config, host = detail.get('Config'), detail.get('HostConfig')
        if not isinstance(config, dict) or not isinstance(host, dict):
            raise OperationError('write_backend_unverified')
        if (self._protected(detail) or host.get('Privileged') is True or host.get('Devices') or host.get('DeviceRequests') or host.get('CapAdd')
                or host.get('ContainerIDFile') or host.get('VolumesFrom') or host.get('Links')
                or any(host.get(key) == 'host' or isinstance(host.get(key), str) and host[key].startswith('container:')
                       for key in ('NetworkMode', 'PidMode', 'IpcMode', 'UTSMode', 'CgroupnsMode'))
                or any('unconfined' in str(item) for item in host.get('SecurityOpt') or [])):
            raise OperationError('write_backend_unverified')
        current = config.get('Image')
        if _repository(current) != _repository(target):
            raise OperationError('permission_denied')
        raw, document, info = self._compose(registered)
        name = old['name']
        if document['services'][name].get('image') != current:
            raise OperationError('project_binding_changed')
        mounts = detail.get('Mounts')
        if not isinstance(mounts, list):
            raise OperationError('write_backend_unverified')
        for mount in mounts:
            # A bind source is resolved anew by dockerd during create. Existing
            # inspect strings cannot prove the same inode/mount is still bound.
            if (not isinstance(mount, dict) or mount.get('Type') not in ('volume', 'tmpfs')
                    or mount.get('Name') == 'r1-toolbox-data'):
                raise OperationError('write_backend_unverified')
        bindings = host.get('Binds') or []
        if not isinstance(bindings, list) or len(bindings) > 128:
            raise OperationError('write_backend_unverified')
        for binding in bindings:
            parts = binding.split(':') if isinstance(binding, str) else []
            if len(parts) not in (2, 3):
                raise OperationError('write_backend_unverified')
            if parts[0].startswith('/'):
                matched = False
            else:
                matched = bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', parts[0])) and any(
                    row.get('Type') == 'volume' and row.get('Name') == parts[0]
                    and row.get('Destination') == parts[1] for row in mounts)
            if not matched:
                raise OperationError('write_backend_unverified')
        for mount in host.get('Mounts') or []:
            if (not isinstance(mount, dict) or not any(
                    row.get('Type') == mount.get('Type') and row.get('Destination') == mount.get('Target')
                    and (row.get('Name') if row.get('Type') == 'volume' else row.get('Source')) == mount.get('Source')
                    for row in mounts)):
                raise OperationError('write_backend_unverified')
        name = detail.get('Name')
        if not isinstance(name, str) or not name.startswith('/') or not _CONTAINER_NAME.fullmatch(name[1:]):
            raise OperationError('write_backend_unverified')
        prepared = {'detail': detail, 'raw': raw, 'document': document, 'mode': stat.S_IMODE(info.st_mode),
                'uid': info.st_uid, 'gid': info.st_gid,
                'fingerprint': self._fingerprint(detail)}
        # Prove the supported creation shape during planning, before any pull
        # or staging-container side effect. No daemon request occurs here.
        self._replacement(prepared, target, 'task_' + '0' * 32)
        return prepared

    def plan_update(self, principal, project_id, service_id, image_reference):
        lock = None
        try:
            owner = self._grant(principal, 'plan_docker_project_update', project_id, 'update_image')
            if (not isinstance(image_reference, str) or not projects._IMAGE.fullmatch(image_reference)
                    or '@sha256:' not in image_reference):
                raise OperationError('invalid_arguments')
            lock = self._project_lock(project_id)
            registered, live = self._project(project_id)
            if len(live['services']) != 1 or live['services'][0]['service_id'] != service_id:
                raise OperationError('invalid_arguments')
            prepared = self._prepare(principal, registered, live, image_reference, self.backend.context())
            with self._state_lock:
                for identity in list(self._plans):
                    if self._plans[identity]['expires'] < self.clock() and self._plans[identity]['task_id'] is None:
                        del self._plans[identity]
                if len(self._plans) >= MAX_RECORDS:
                    raise OperationError('limit_exceeded')
                public = {'plan_id': 'plan_' + secrets.token_hex(16), 'project_id': project_id,
                          'service_id': service_id, 'current_image': live['services'][0]['image_reference'],
                          'target_image': image_reference, 'target_digest': image_reference.split('@', 1)[1],
                          'change': 'service_image_only', 'expires_at': _stamp(time.time() + PLAN_SECONDS),
                          'requires_confirmation': True, 'restart_invalidates': True}
                self._plans[public['plan_id']] = {'public': public, 'owner': owner, 'registered': registered,
                    'prepared': prepared, 'expires': self.clock() + PLAN_SECONDS, 'task_id': None}
            self._current(principal)
            return {'ok': True, 'plan': copy.deepcopy(public)}
        except (OperationError, projects.ProjectError) as error:
            return _failure(error.code)
        except Exception:
            return _failure('write_backend_unverified')
        finally:
            if lock is not None:
                lock.release()

    def _verify(self, principal, plan, context, *, new_id=None, old_name=None, config_identity=None):
        self._current(principal)
        registered = plan['registered']
        with self.registry._lock:
            rows = [row for row in self.registry._read()['projects'] if row['id'] == registered['id'] and not row['revoked']]
            if len(rows) != 1 or rows[0]['binding'] != registered['binding'] or rows[0]['revision'] != registered['revision']:
                raise OperationError('project_binding_changed')
        self._engine(context, registered['identity']['engine'])
        old = registered['identity']['services'][0]['container']
        detail = self.backend.request('GET', '/containers/' + old + '/json', context)
        expected_running = False if plan.get('_old_stopped') else plan['prepared']['detail'].get('State', {}).get('Running')
        if detail.get('State', {}).get('Running') is not expected_running:
            raise OperationError('project_binding_changed')
        if old_name is not None:
            if detail.get('Name') != '/' + old_name:
                raise OperationError('project_binding_changed')
            detail = copy.deepcopy(detail)
            detail['Name'] = plan['prepared']['detail']['Name']
        if self._fingerprint(detail) != plan['prepared']['fingerprint']:
            raise OperationError('project_binding_changed')
        expected = config_identity or registered['identity']['configuration']
        if self.backend._config_identity(expected['path']) != expected:
            raise OperationError('project_binding_changed')
        rows = self.backend._get('/containers/json?all=1&limit=129', context['deadline'], context)
        if not isinstance(rows, list) or len(rows) > projects.MAX_CONTAINERS:
            raise OperationError('project_binding_changed')
        members = [row.get('Id') for row in rows if isinstance(row, dict)
                   and isinstance(row.get('Labels'), dict)
                   and row['Labels'].get(projects._LABEL + 'project') == registered['identity']['name']]
        if sorted(members) != sorted([old] + ([new_id] if new_id else [])):
            raise OperationError('project_binding_changed')
        if new_id is not None:
            new = self.backend.request('GET', '/containers/' + new_id + '/json', context)
            if (new.get('Image') != plan.get('_target_image_id')
                    or new.get('Config', {}).get('Image') != plan['public']['target_image']
                    or new.get('Name') != plan.get('_new_name')
                    or new.get('State', {}).get('Running') is not bool(plan.get('_new_started'))
                    or self._network_configuration(new) != self._network_configuration(plan['prepared']['detail'])
                    or self._mount_configuration(new) != self._mount_configuration(plan['prepared']['detail'])):
                raise OperationError('project_binding_changed')
            if plan.get('_health_verified') and not self._health_complete(new):
                raise OperationError('result_unconfirmed')
            new = copy.deepcopy(new)
            new['Name'] = '/r1-stage'
            fingerprint = self._fingerprint(new)
            if '_new_fingerprint' in plan and fingerprint != plan['_new_fingerprint']:
                raise OperationError('project_binding_changed')
            plan['_new_fingerprint'] = fingerprint

    def _image(self, principal, target, context, registered):
        path = '/images/' + quote(target, safe='') + '/json'
        try:
            image = self.backend.request('GET', path, context)
        except OperationError as error:
            if error.code != 'plan_conflict':
                raise
            self._current(principal)
            self._engine(context, registered['identity']['engine'])
            self.backend.request('POST', '/images/create?fromImage=' + quote(target, safe=''), context,
                                 authorizer=lambda: self._current(principal), stream=True)
            image = self.backend.request('GET', path, context)
        layers = image.get('RootFS', {}).get('Layers') if isinstance(image.get('RootFS'), dict) else None
        if (not isinstance(image.get('Id'), str) or not _DIGEST.fullmatch(image['Id'])
                or not isinstance(image.get('RepoDigests'), list) or target not in image['RepoDigests']
                or not isinstance(layers, list) or len(layers) > 256
                or any(not isinstance(layer, str) or not _DIGEST.fullmatch(layer) for layer in layers)):
            raise OperationError('native_unavailable')
        return image

    @staticmethod
    def _health_complete(detail):
        state, config = detail.get('State', {}), detail.get('Config', {})
        health = state.get('Health', {}).get('Status')
        definition = config.get('Healthcheck') or {}
        test = definition.get('Test') if isinstance(definition, dict) else None
        expected = bool(test) and isinstance(test, list) and test[0] != 'NONE'
        return health == 'healthy' or health is None and not expected

    def _healthy(self, principal, plan, context, new_id, was_running):
        """Wait for real health within the job deadline; never restart/retry start."""
        deadline = min(context['deadline'], self.backend._clock() + HEALTH_SECONDS)
        while True:
            self._current(principal)
            actual = self.backend.request('GET', '/containers/' + new_id + '/json', context)
            state = actual.get('State', {})
            if (actual.get('Image') != plan['_target_image_id']
                    or actual.get('Config', {}).get('Image') != plan['public']['target_image']
                    or state.get('Running') is not was_running):
                raise OperationError('result_unconfirmed')
            health = state.get('Health', {}).get('Status')
            if not was_running:
                return
            if self._health_complete(actual):
                plan['_health_verified'] = True
                return
            remaining = deadline - self.backend._clock()
            if health != 'starting' or remaining <= 0:
                raise OperationError('result_unconfirmed')
            time.sleep(min(.25, remaining))

    def _replace_compose(self, principal, registered, prepared, document):
        expected = registered['identity']['configuration']
        path = Path(expected['path'])
        if self.backend._config_identity(str(path)) != expected:
            raise OperationError('project_binding_changed')
        raw = self._compose_bytes(document)
        if not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW'):
            raise OperationError('write_backend_unverified')
        _exchange_function()
        temporary, directory = None, os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            # Hold the actual parent directory. A renamed ancestor or symlink
            # race cannot redirect the temporary write/replace to another path.
            for part in path.parent.parts[1:]:
                current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = current
            self._current(principal)
            temporary = '.r1-image-' + secrets.token_hex(16)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'wb') as stream:
                if hasattr(os, 'fchown'):
                    os.fchown(stream.fileno(), prepared['uid'], prepared['gid'])
                os.fchmod(stream.fileno(), prepared['mode'])
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                staged = os.fstat(stream.fileno())
            self._current(principal)
            opened = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            actual = [opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns]
            if actual != expected['identity'] or self.backend._config_identity(str(path)) != expected:
                raise OperationError('project_binding_changed')
            # The comparison above is not CAS: a third party can still replace
            # or rewrite the original here. Exchange retains exactly what was
            # displaced, allowing us to detect that race without losing bytes.
            # Keep the recovery entry even on failure; never blindly unlink or
            # restore a name another writer may have changed in the meantime.
            self._current(principal)
            prepared['_configuration_recovery_entry'] = temporary
            _rename_exchange(temporary, path.name, directory_fd=directory)
            os.fsync(directory)

            def read_entry(name):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    before = os.fstat(descriptor)
                    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                            or before.st_size > projects.MAX_BODY):
                        raise OperationError('result_unconfirmed')
                    content = os.read(descriptor, projects.MAX_BODY + 1)
                    after = os.fstat(descriptor)
                    identity = lambda item: [item.st_dev, item.st_ino, item.st_size,
                                             item.st_mtime_ns, item.st_ctime_ns]
                    if identity(before) != identity(after) or len(content) != before.st_size:
                        raise OperationError('result_unconfirmed')
                    return before, content
                finally:
                    os.close(descriptor)

            displaced, previous = read_entry(temporary)
            # renameat2 changes ctime itself. All other original identity fields
            # and the complete bounded content must still match the plan.
            if ([displaced.st_dev, displaced.st_ino, displaced.st_size, displaced.st_mtime_ns]
                    != expected['identity'][:4] or previous != prepared['raw']
                    or stat.S_IMODE(displaced.st_mode) != prepared['mode']
                    or (displaced.st_uid, displaced.st_gid) != (prepared['uid'], prepared['gid'])):
                raise OperationError('result_unconfirmed')
            installed, content = read_entry(path.name)
            if ((installed.st_dev, installed.st_ino) != (staged.st_dev, staged.st_ino)
                    or content != raw):
                raise OperationError('result_unconfirmed')
            configuration = self.backend._config_identity(str(path))
            identity = [installed.st_dev, installed.st_ino, installed.st_size,
                        installed.st_mtime_ns, installed.st_ctime_ns]
            if configuration['identity'] != identity:
                raise OperationError('result_unconfirmed')
            return configuration, 'sha256:' + hashlib.sha256(raw).hexdigest()
        finally:
            # Even a failed exchange may leave a useful staged version. Entries
            # are private owner-only recovery material, not automatic rollback.
            os.close(directory)

    @staticmethod
    def _replacement(prepared, target, task_id):
        detail = prepared['detail']
        allowed = {'Hostname', 'Domainname', 'User', 'AttachStdin', 'AttachStdout', 'AttachStderr',
                   'ExposedPorts', 'Tty', 'OpenStdin', 'StdinOnce', 'Env', 'Cmd', 'Healthcheck',
                   'Image', 'Volumes', 'WorkingDir', 'Entrypoint', 'NetworkDisabled', 'Labels',
                   'StopSignal', 'StopTimeout', 'Shell'}
        body = {key: copy.deepcopy(value) for key, value in detail['Config'].items() if key in allowed}
        body['Image'] = target
        document = copy.deepcopy(prepared['document'])
        next(iter(document['services'].values()))['image'] = target
        body['Labels'][projects._LABEL + 'config-hash'] = hashlib.sha256(
            DockerProjectOperations._compose_bytes(document)).hexdigest()
        body['HostConfig'] = copy.deepcopy(detail['HostConfig'])
        # Rebind existing named volumes explicitly; never request volume deletion
        # or let image VOLUME declarations allocate unreviewed anonymous volumes.
        existing = {row.get('Destination') for row in detail['Mounts']}
        if any(destination not in existing for destination in (body.get('Volumes') or {})):
            raise OperationError('write_backend_unverified')
        explicit = body['HostConfig'].setdefault('Mounts', [])
        destinations = {row.get('Target') for row in explicit if isinstance(row, dict)}
        destinations.update(binding.split(':')[1] for binding in body['HostConfig'].get('Binds') or [])
        for mount in detail['Mounts']:
            if mount.get('Type') == 'volume' and mount.get('Destination') not in destinations:
                if not projects._plain(mount.get('Name'), 255):
                    raise OperationError('write_backend_unverified')
                explicit.append({'Type': 'volume', 'Source': mount['Name'], 'Target': mount['Destination'],
                                 'ReadOnly': mount.get('RW') is not True})
        networks = detail.get('NetworkSettings', {}).get('Networks') or {}
        if not isinstance(networks, dict) or len(networks) > 16:
            raise OperationError('write_backend_unverified')
        endpoints = {}
        for name, network in networks.items():
            if (not projects._plain(name, 128) or not isinstance(network, dict)
                    or not isinstance(network.get('NetworkID'), str) or not _CID.fullmatch(network['NetworkID'])
                    or network.get('IPAMConfig') or network.get('Links') or network.get('DriverOpts')
                    or network.get('GwPriority') not in (None, 0)):
                raise OperationError('write_backend_unverified')
            aliases = network.get('Aliases') or []
            if not isinstance(aliases, list) or len(aliases) > 64 or any(not projects._plain(alias, 128) for alias in aliases):
                raise OperationError('write_backend_unverified')
            endpoints[name] = {'NetworkID': network['NetworkID'], 'Aliases': aliases}
        if endpoints:
            body['NetworkingConfig'] = {'EndpointsConfig': endpoints}
        return body

    def execute_update(self, principal, plan_id, confirmed, idempotency_key):
        """Consume a confirmed plan and return its real task before Engine I/O."""
        lock, task = None, None
        try:
            self._current(principal)
            with self._state_lock:
                plan = self._plans.get(plan_id) if isinstance(plan_id, str) else None
                if plan is None or plan['owner'] != _owner(principal):
                    raise OperationError('plan_conflict')
                project_id = plan['public']['project_id']
            self._grant(principal, 'execute_docker_project_update', project_id, 'update_image')
            if 'plan_docker_project_update' not in principal['tool_grants'] or confirmed is not True:
                raise OperationError('permission_denied')
            previous, unused, unused2 = self._idempotent(principal, project_id, plan_id, idempotency_key)
            if previous:
                return self._publish(principal, previous)
            lock = self._project_lock(project_id)
            previous, unused, unused2 = self._idempotent(principal, project_id, plan_id, idempotency_key)
            if previous:
                return self._publish(principal, previous)
            if self.clock() >= plan['expires']:
                raise OperationError('plan_expired')
            if plan['task_id'] is not None:
                raise OperationError('plan_conflict')
            with self._state_lock:
                if len(self._threads) >= MAX_ACTIVE_TASKS:
                    raise OperationError('resource_busy')
                self._current(principal)
                task, reserved = self._reserve_task(principal, project_id, plan_id, idempotency_key)
                if not reserved:
                    return self._publish(principal, task)
                plan['task_id'] = task['task_id']
                worker = threading.Thread(target=self._run_update,
                    args=(copy.deepcopy(principal), plan, task, lock),
                    name='r1-docker-update-' + task['task_id'][5:13], daemon=True)
                self._threads.add(worker)
                try:
                    worker.start()
                except Exception:
                    self._threads.discard(worker)
                    self._finish(task, 'failed', error='native_unavailable')
                    raise OperationError('native_unavailable') from None
                lock = None  # The worker now owns the shared project lock.
            return self._publish(principal, task)
        except (OperationError, projects.ProjectError) as error:
            return _failure(error.code)
        except Exception:
            return _failure('write_backend_unverified')
        finally:
            if lock is not None:
                lock.release()

    def _run_update(self, principal, plan, task, lock):
        """Bounded worker body; the submitter already consumed plan/idempotency."""
        context = None
        try:
            project_id = plan['public']['project_id']
            context = self.backend.context()
            self._verify(principal, plan, context)
            target = plan['public']['target_image']
            self._finish(task, 'running')
            # Pulling is itself a side effect. Consume the plan and retain its
            # task before a pull, even if its transport result becomes unknown.
            image = self._image(principal, target, context, plan['registered'])
            plan['_target_image_id'] = image['Id']
            self._verify(principal, plan, context)
            body = self._replacement(plan['prepared'], target, task['task_id'])
            image_volumes = image.get('Config', {}).get('Volumes') if isinstance(image.get('Config'), dict) else None
            destinations = {row.get('Destination') for row in plan['prepared']['detail']['Mounts']}
            if image_volumes and (not isinstance(image_volumes, dict) or set(image_volumes) - destinations):
                raise OperationError('write_backend_unverified')
            self._verify(principal, plan, context)
            plan['_new_name'] = '/r1-stage-' + task['task_id'][5:29]
            created = self.backend.request('POST', '/containers/create?name=' + plan['_new_name'][1:],
                context, body=body, authorizer=lambda: self._current(principal))
            new_id = created.get('Id')
            if not isinstance(new_id, str) or not _CID.fullmatch(new_id):
                raise OperationError('result_unconfirmed')
            self._verify(principal, plan, context, new_id=new_id)
            document = copy.deepcopy(plan['prepared']['document'])
            document['services'][plan['registered']['identity']['services'][0]['name']]['image'] = target
            configuration, config_digest = self._replace_compose(principal, plan['registered'], plan['prepared'], document)
            old_id = plan['registered']['identity']['services'][0]['container']
            was_running = plan['prepared']['detail'].get('State', {}).get('Running')
            if type(was_running) is not bool:
                raise OperationError('result_unconfirmed')
            self._verify(principal, plan, context, new_id=new_id, config_identity=configuration)
            if was_running:
                self.backend.request('POST', '/containers/' + old_id + '/stop?t=10', context,
                                     authorizer=lambda: self._current(principal))
                plan['_old_stopped'] = True
            self._verify(principal, plan, context, new_id=new_id, config_identity=configuration)
            backup = 'r1-backup-' + task['task_id'][5:29]
            self.backend.request('POST', '/containers/' + old_id + '/rename?name=' + backup, context,
                                 authorizer=lambda: self._current(principal))
            self._verify(principal, plan, context, new_id=new_id, old_name=backup, config_identity=configuration)
            original_name = plan['prepared']['detail']['Name'][1:]
            self.backend.request('POST', '/containers/' + new_id + '/rename?name=' + original_name, context,
                                 authorizer=lambda: self._current(principal))
            plan['_new_name'] = '/' + original_name
            self._verify(principal, plan, context, new_id=new_id, old_name=backup, config_identity=configuration)
            if was_running:
                self.backend.request('POST', '/containers/' + new_id + '/start', context,
                                     authorizer=lambda: self._current(principal))
                plan['_new_started'] = True
            self._healthy(principal, plan, context, new_id, was_running)
            self._verify(principal, plan, context, new_id=new_id, old_name=backup, config_identity=configuration)
            self.backend.request('DELETE', '/containers/' + old_id + '?v=0&force=0', context,
                                 authorizer=lambda: self._current(principal))
            self._current(principal)
            live = [row for row in self.backend.snapshot(deadline=context['deadline'])
                    if row['identity']['name'] == plan['registered']['identity']['name']]
            if (len(live) != 1 or live[0]['identity']['services'][0]['container'] != new_id
                    or live[0]['identity']['configuration'] != configuration):
                raise OperationError('result_unconfirmed')
            self._current(principal)
            self.registry._adopt_verified_update(project_id, plan['registered']['binding'], live[0],
                                                 previous_revision=plan['registered']['revision'])
            self._finish(task, 'completed', running=was_running,
                         verified_target_digest=plan['public']['target_digest'],
                         verified_config_digest=config_digest, verified_image_id=image['Id'])
        except (OperationError, projects.ProjectError) as error:
            mutated = context is not None and context.get('mutations', 0) > 0
            self._finish(task, 'result_unconfirmed' if mutated else 'failed', error=error.code)
        except Exception:
            mutated = context is not None and context.get('mutations', 0) > 0
            self._finish(task, 'result_unconfirmed' if mutated else 'failed', error='native_unavailable')
        finally:
            lock.release()
            with self._state_lock:
                self._threads.discard(threading.current_thread())

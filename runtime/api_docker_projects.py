"""Owner-registered, identity-bound Docker project reads, never deployment.

Only the already fixed local Docker Unix socket is used. Every request is a
bounded GET to an allowlisted engine endpoint. Compose labels locate metadata,
not an arbitrary read API: only regular, no-symlink configuration identities
are recorded; their contents, environment, commands and host paths never leave
this module. Native REST authentication is not yet verified here, so native
status fields remain null and summaries explicitly use engine evidence.
"""

import copy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import struct
import tempfile
import threading
import time


SOCKET_PATH = '/var/run/docker.sock'
NETWORK_SECONDS = 8.0
MAX_CONTAINERS = 128
MAX_BODY = 1024 * 1024
MAX_TOTAL = 8 * MAX_BODY
MAX_HEADER = 16384
MAX_STATE = 512 * 1024
MAX_STATS_SERVICES = 32
MAX_STATS_INTERFACES = 64
MAX_STATS_BLOCK_ROWS = 1024
MAX_STATS_BLOCK_DEVICES = 128
MAX_STATS_OUTPUT = 128 * 1024
_SAFE_INTEGER = 2 ** 53 - 1
_NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z', re.ASCII)
_HEX = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_PROJECT = re.compile(r'project_[0-9a-f]{24}\Z', re.ASCII)
_CANDIDATE = re.compile(r'candidate_[0-9a-f]{24}\Z', re.ASCII)
_IMAGE = re.compile(r'[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?'
                    r'(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*'
                    r'(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})?\Z')
_ENGINE = re.compile(r'[A-Za-z0-9:_-]{16,128}\Z', re.ASCII)
_LABEL = 'com.docker.compose.'
_READ_LOCK = threading.Lock()
_MESSAGES = {
    'invalid_key': '项目读取期间密钥授权已失效。',
    'permission_denied': '当前密钥未获授该项目读取权限。',
    'invalid_arguments': '项目参数格式无效。',
    'project_unavailable': '未取得身份完整且已登记的项目。',
    'project_binding_changed': '项目绑定已变化，需要所有者重新登记。',
    'native_unavailable': '本机 Docker 来源身份或响应无法确认。',
    'resource_busy': '另一项项目读取正在进行。',
    'limit_exceeded': '项目读取超过条目或响应上限。',
    'timeout': '项目读取超过总时间限制。',
}


class ProjectError(Exception):
    """A fixed public error, never interpolated daemon or filesystem text."""

    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'native_unavailable'
        super().__init__(_MESSAGES[self.code])


def _failure(code):
    return {'ok': False, 'error': {'code': code, 'message': _MESSAGES[code],
                                  'retryable': code in ('resource_busy', 'timeout')}}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()


def _plain(value, maximum=128):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and value == value.strip() and all(c.isprintable() for c in value))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProjectError('native_unavailable')
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ProjectError('native_unavailable') from None


def _remaining(deadline, clock):
    left = deadline - clock()
    if left <= 0:
        raise ProjectError('timeout')
    return left


def _authorized(authorizer):
    """Only a trusted in-process callback can renew an authenticated read."""
    try:
        accepted = callable(authorizer) and authorizer() is True
    except Exception:
        accepted = False
    if not accepted:
        raise ProjectError('invalid_key')


def _locked(function):
    """Reject overlapping work instead of accumulating unbounded HTTP waiters."""
    @wraps(function)
    def guarded(self, *args, **kwargs):
        if not self._lock.acquire(blocking=False):
            return _failure('resource_busy')
        try:
            return function(self, *args, **kwargs)
        finally:
            self._lock.release()
    return guarded


def _configuration_identity(path):
    """Open metadata handles component-by-component; never read Compose bytes.

    Linux O_PATH is required. No symlink is followed, including ancestors. The
    final inode, filesystem, size and nanosecond mtime form a binding, not an ACL
    claim. Changes while holding the handle are rejected.
    """
    if (not _plain(path, 1024) or not path.startswith('/') or '\\' in path
            or any(part in ('', '.', '..') for part in path.split('/')[1:])
            or not path.endswith(('.yml', '.yaml')) or not hasattr(os, 'O_PATH')
            or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY')):
        raise ProjectError('project_unavailable')
    descriptor = os.open('/', os.O_PATH | os.O_DIRECTORY)
    try:
        parts = path.split('/')[1:]
        for index, part in enumerate(parts):
            flags = os.O_PATH | os.O_NOFOLLOW
            if index != len(parts) - 1:
                flags |= os.O_DIRECTORY
            parent_info = os.fstat(descriptor)
            current = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = current
            info = os.fstat(descriptor)
            if index == len(parts) - 1:
                if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BODY
                        or info.st_dev != parent_info.st_dev):
                    raise ProjectError('project_unavailable')
                again = os.fstat(descriptor)
                identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
                if identity != [again.st_dev, again.st_ino, again.st_size, again.st_mtime_ns, again.st_ctime_ns]:
                    raise ProjectError('project_binding_changed')
                return {'path': path, 'identity': identity}
    except (OSError, ValueError):
        raise ProjectError('project_unavailable') from None
    finally:
        os.close(descriptor)


def _peer_identity(connection):
    """Prove the Unix peer is one stable root-owned dockerd executable."""
    if not hasattr(socket, 'SO_PEERCRED'):
        raise ProjectError('native_unavailable')
    pid, uid, _gid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if pid <= 1 or uid != 0:
        raise ProjectError('native_unavailable')
    folder = Path('/proc') / str(pid)
    try:
        name = (folder / 'comm').read_text(encoding='ascii').strip()
        executable = os.readlink(folder / 'exe')
        info = (folder / 'exe').stat()
        fields = (folder / 'stat').read_text(encoding='ascii').rsplit(')', 1)[1].split()
        ticks = int(fields[19])
        if (name != 'dockerd' or PurePosixPath(executable).name != 'dockerd'
                or not executable.startswith('/') or ticks <= 0
                or not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise ProjectError('native_unavailable')
        return [pid, ticks, info.st_dev, info.st_ino]
    except (OSError, ValueError, IndexError, UnicodeError):
        raise ProjectError('native_unavailable') from None


class LocalDockerReader:
    """Read one bounded engine snapshot; hooks are internal fixture seams only."""

    def __init__(self, *, connect=None, peer_check=None, config_identity=None, clock=None):
        self._connect = connect
        self._peer_check = peer_check or _peer_identity
        self._config_identity = config_identity or _configuration_identity
        self._clock = clock or time.monotonic

    def _connection(self, deadline):
        if self._connect is not None:
            return self._connect(_remaining(deadline, self._clock))
        path = os.path.realpath(SOCKET_PATH)
        if path not in ('/run/docker.sock', SOCKET_PATH):
            raise ProjectError('native_unavailable')
        info = os.lstat(path)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o002:
            raise ProjectError('native_unavailable')
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(_remaining(deadline, self._clock))
            connection.connect(path)
            current = os.lstat(path)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise ProjectError('native_unavailable')
            return connection
        except BaseException:
            connection.close()
            raise

    def _get(self, path, deadline, context, *, authorizer=None, stats=False):
        ordinary = path in ('/info', '/containers/json?all=1&limit=129') or re.fullmatch(
                r'/containers/[0-9a-f]{64}/json', path, re.ASCII)
        # Existing callers keep exactly their old GET allowlist. Only the
        # resource sampler opts into this one literal, non-streaming query.
        sample = stats is True and re.fullmatch(
            r'/containers/[0-9a-f]{64}/stats\?stream=false&one-shot=true', path, re.ASCII)
        if not ordinary and not sample:
            raise ProjectError('invalid_arguments')
        if authorizer is not None:
            _authorized(authorizer)
        connection = self._connection(deadline)
        try:
            peer = self._peer_check(connection)
            if context['peer'] is not None and context['peer'] != peer:
                raise ProjectError('project_binding_changed')
            context['peer'] = peer
            if authorizer is not None:
                _authorized(authorizer)
            connection.settimeout(_remaining(deadline, self._clock))
            connection.sendall(('GET ' + path + ' HTTP/1.1\r\nHost: localhost\r\n'
                                'Accept: application/json\r\nAccept-Encoding: identity\r\n'
                                'Connection: close\r\n\r\n').encode('ascii'))
            buffer = bytearray()

            def receive():
                connection.settimeout(_remaining(deadline, self._clock))
                data = connection.recv(8192)
                if not data:
                    raise ProjectError('native_unavailable')
                context['bytes'] += len(data)
                if context['bytes'] > MAX_TOTAL:
                    raise ProjectError('limit_exceeded')
                buffer.extend(data)

            def line(limit=MAX_HEADER):
                while b'\r\n' not in buffer:
                    if len(buffer) > limit:
                        raise ProjectError('limit_exceeded')
                    receive()
                end = buffer.index(b'\r\n')
                if end > limit:
                    raise ProjectError('limit_exceeded')
                value = bytes(buffer[:end])
                del buffer[:end + 2]
                return value

            status_line = line(128)
            if not re.fullmatch(rb'HTTP/1\.[01] 200(?: [\x20-\x7e]*)?', status_line):
                raise ProjectError('native_unavailable')
            headers, header_size = {}, 0
            while True:
                value = line()
                header_size += len(value) + 2
                if header_size > MAX_HEADER:
                    raise ProjectError('limit_exceeded')
                if not value:
                    break
                key, separator, item = value.partition(b':')
                key, item = key.lower(), item.strip()
                if (not separator or not re.fullmatch(rb'[a-z0-9-]+', key)
                        or key in headers or any(c < 32 or c == 127 for c in item)):
                    raise ProjectError('native_unavailable')
                headers[key] = item
            if (headers.get(b'content-type', b'').split(b';', 1)[0].strip().lower() != b'application/json'
                    or headers.get(b'content-encoding', b'identity').lower() != b'identity'):
                raise ProjectError('native_unavailable')

            def exact(length):
                if length > MAX_BODY:
                    raise ProjectError('limit_exceeded')
                while len(buffer) < length:
                    receive()
                value = bytes(buffer[:length])
                del buffer[:length]
                return value

            if b'content-length' in headers and b'transfer-encoding' not in headers:
                raw_length = headers[b'content-length']
                if not re.fullmatch(rb'[0-9]{1,10}', raw_length):
                    raise ProjectError('native_unavailable')
                body = exact(int(raw_length))
            elif headers.get(b'transfer-encoding', b'').lower() == b'chunked' and b'content-length' not in headers:
                body = bytearray()
                while True:
                    chunk = line(32)
                    if not re.fullmatch(rb'[0-9a-fA-F]{1,8}', chunk):
                        raise ProjectError('native_unavailable')
                    length = int(chunk, 16)
                    if len(body) + length > MAX_BODY:
                        raise ProjectError('limit_exceeded')
                    if length == 0:
                        if line(2):
                            raise ProjectError('native_unavailable')
                        break
                    body.extend(exact(length))
                    if exact(2) != b'\r\n':
                        raise ProjectError('native_unavailable')
            else:
                raise ProjectError('native_unavailable')
            if buffer or self._peer_check(connection) != peer:
                raise ProjectError('project_binding_changed')
            _remaining(deadline, self._clock)
            value = _json(body)
            if authorizer is not None:
                _authorized(authorizer)
            return value
        finally:
            connection.close()

    def snapshot(self, *, deadline=None):
        if not _READ_LOCK.acquire(blocking=False):
            raise ProjectError('resource_busy')
        try:
            deadline = min(self._clock() + NETWORK_SECONDS, deadline) if deadline is not None else self._clock() + NETWORK_SECONDS
            context = {'bytes': 0, 'peer': None}
            info = self._get('/info', deadline, context)
            if not isinstance(info, dict) or not isinstance(info.get('ID'), str) or not _ENGINE.fullmatch(info['ID']):
                raise ProjectError('native_unavailable')
            engine = info['ID']
            rows = self._get('/containers/json?all=1&limit=129', deadline, context)
            if not isinstance(rows, list):
                raise ProjectError('native_unavailable')
            if len(rows) > MAX_CONTAINERS:
                raise ProjectError('limit_exceeded')
            projects, invalid, seen = {}, set(), set()
            for row in rows:
                _remaining(deadline, self._clock)
                if not isinstance(row, dict) or not isinstance(row.get('Id'), str) or not _HEX.fullmatch(row['Id']):
                    raise ProjectError('native_unavailable')
                if row['Id'] in seen:
                    raise ProjectError('native_unavailable')
                seen.add(row['Id'])
                labels = row.get('Labels')
                if not isinstance(labels, dict) or _LABEL + 'project' not in labels:
                    continue
                name = labels[_LABEL + 'project']
                if not isinstance(name, str) or not _NAME.fullmatch(name):
                    continue
                try:
                    detail = self._get('/containers/' + row['Id'] + '/json', deadline, context)
                    item = _container(detail, row['Id'], name, self._config_identity)
                    projects.setdefault(name, []).append(item)
                except ProjectError as error:
                    if error.code in ('timeout', 'limit_exceeded', 'project_binding_changed'):
                        raise
                    invalid.add(name)
            # Re-read list and engine identity: do not publish a mixed rebuild.
            final_rows = self._get('/containers/json?all=1&limit=129', deadline, context)
            def membership(values):
                if not isinstance(values, list) or len(values) > MAX_CONTAINERS:
                    raise ProjectError('project_binding_changed')
                return sorted([_hash({'Id': value.get('Id'), 'Labels': value.get('Labels')})
                               for value in values if isinstance(value, dict)])
            if (len(final_rows) != len(rows) or membership(final_rows) != membership(rows)
                    or self._get('/info', deadline, context).get('ID') != engine):
                raise ProjectError('project_binding_changed')
            result = []
            for name, items in sorted(projects.items()):
                if name in invalid or len({_hash(item['configuration']) for item in items}) != 1:
                    continue
                service_names = [item['service']['name'] for item in items]
                # Replicas need a separate reviewed aggregation contract.
                if len(set(service_names)) != len(service_names):
                    continue
                configuration = items[0]['configuration']
                if self._config_identity(configuration['path']) != configuration:
                    raise ProjectError('project_binding_changed')
                _remaining(deadline, self._clock)
                identity = {'engine': engine, 'name': name, 'configuration': items[0]['configuration'],
                            'services': sorted([item['identity'] for item in items], key=lambda item: item['name'])}
                binding = _hash(identity)
                for item in items:
                    item['service']['service_id'] = 'service_' + _hash([binding, item['identity']])[:24]
                result.append({'binding': binding, 'identity': identity,
                               'protected': name in ('r1-toolbox', 'r1-boot-once') or any(item['protected'] for item in items),
                               'services': [item['service'] for item in items],
                               'running': _running(items), 'health': _health(items)})
            return result
        except ProjectError:
            raise
        except (TimeoutError, socket.timeout):
            raise ProjectError('timeout') from None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
            raise ProjectError('native_unavailable') from None
        finally:
            _READ_LOCK.release()

    def resource_usage(self, identity, *, authorizer, deadline=None):
        """Sample only one already-registered project, with one stats per CID.

        All frames share the same peer, engine, eight-second deadline and byte
        budget. No cross-request cache or daemon stream supplies invented rates.
        """
        if not _valid_identity(identity):
            raise ProjectError('project_binding_changed')
        if len(identity['services']) > MAX_STATS_SERVICES:
            raise ProjectError('limit_exceeded')
        if not _READ_LOCK.acquire(blocking=False):
            raise ProjectError('resource_busy')
        try:
            _authorized(authorizer)
            deadline = min(self._clock() + NETWORK_SECONDS, deadline) if deadline is not None else self._clock() + NETWORK_SECONDS
            context = {'bytes': 0, 'peer': None}
            fetch = lambda path, **kwargs: self._get(path, deadline, context,
                authorizer=authorizer, **kwargs)
            if fetch('/info').get('ID') != identity['engine']:
                raise ProjectError('project_binding_changed')
            _stats_membership(fetch('/containers/json?all=1&limit=129'), identity)
            configuration = identity['configuration']
            if self._config_identity(configuration['path']) != configuration:
                raise ProjectError('project_binding_changed')
            initial, output = {}, []

            def inspect(service):
                value = fetch('/containers/' + service['container'] + '/json')
                parsed = _container(value, service['container'], identity['name'], self._config_identity)
                if parsed['identity'] != service or parsed['configuration'] != configuration:
                    raise ProjectError('project_binding_changed')
                return _stats_lifecycle(value)

            for service in identity['services']:
                initial[service['container']] = inspect(service)
            for service in identity['services']:
                lifecycle = initial[service['container']]
                native = fetch('/containers/' + service['container'] + '/stats?stream=false&one-shot=true', stats=True)
                # Docker may return an empty object for a stopped container.
                # Only that exact no-counters case can lack an echoed ID; the
                # same stopped lifecycle is still inspected before and after.
                empty_stopped = native == {} and lifecycle['running'] is False
                if (not isinstance(native, dict) or not empty_stopped and native.get('id') != service['container']
                        or 'name' in native and native['name'] != lifecycle['name']):
                    raise ProjectError('project_binding_changed')
                if inspect(service) != lifecycle:
                    raise ProjectError('project_binding_changed')
                read_at = _stats_time(native.get('read'))
                started_at = _stats_time(lifecycle['started_at'], historical=True)
                if read_at is not None and read_at < started_at:
                    raise ProjectError('project_binding_changed')
                record = _stats_projection(native, running=lifecycle['running'], started_at=started_at)
                record.update(service_id='service_' + _hash([_hash(identity), service])[:24],
                              name=service['name'], running=lifecycle['running'])
                output.append(record)
            # Revalidate every service, not just the last one sampled, so a
            # restart during another service's read cannot escape the batch.
            for service in identity['services']:
                if inspect(service) != initial[service['container']]:
                    raise ProjectError('project_binding_changed')
            _stats_membership(fetch('/containers/json?all=1&limit=129'), identity)
            if (fetch('/info').get('ID') != identity['engine']
                    or self._config_identity(configuration['path']) != configuration):
                raise ProjectError('project_binding_changed')
            _remaining(deadline, self._clock)
            _authorized(authorizer)
            if len(json.dumps(output, ensure_ascii=True, allow_nan=False).encode('ascii')) > MAX_STATS_OUTPUT:
                raise ProjectError('limit_exceeded')
            return output
        except ProjectError:
            raise
        except (TimeoutError, socket.timeout):
            raise ProjectError('timeout') from None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
            raise ProjectError('native_unavailable') from None
        finally:
            _READ_LOCK.release()


def _stats_membership(rows, identity):
    if not isinstance(rows, list) or len(rows) > MAX_CONTAINERS:
        raise ProjectError('limit_exceeded')
    selected, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('Id'), str)
                or not _HEX.fullmatch(row['Id']) or row['Id'] in seen):
            raise ProjectError('native_unavailable')
        seen.add(row['Id'])
        labels = row.get('Labels')
        if isinstance(labels, dict) and labels.get(_LABEL + 'project') == identity['name']:
            selected.append(row['Id'])
    if sorted(selected) != sorted(service['container'] for service in identity['services']):
        raise ProjectError('project_binding_changed')


def _stats_lifecycle(detail):
    state = detail.get('State')
    image, name = detail.get('Image'), detail.get('Name')
    if (not isinstance(state, dict) or type(state.get('Running')) is not bool
            or _stats_integer(state.get('Pid'), 2 ** 31 - 1) is None
            or state['Running'] and state['Pid'] < 1
            or _stats_integer(detail.get('RestartCount')) is None
            or not isinstance(name, str) or not re.fullmatch(r'/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name)
            or not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image)
            or _stats_time(state.get('StartedAt'), historical=True) is None):
        raise ProjectError('project_binding_changed')
    return {'name': name, 'image': image, 'running': state['Running'], 'pid': state['Pid'],
            'started_at': state['StartedAt'], 'restart_count': detail['RestartCount']}


def _stats_integer(value, maximum=_SAFE_INTEGER):
    return value if type(value) is int and 0 <= value <= maximum else None


def _stats_time(value, *, historical=False):
    if not isinstance(value, str) or not re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})', value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if historical or parsed.year >= 1970 else None
    except ValueError:
        return None


def _stats_cpu_count(frame):
    if not isinstance(frame, dict):
        return None
    online = frame.get('online_cpus')
    if online is not None:
        return online if type(online) is int and 1 <= online <= 4096 else None
    usage = frame.get('cpu_usage')
    per_cpu = usage.get('percpu_usage') if isinstance(usage, dict) else None
    if (not isinstance(per_cpu, list) or not 1 <= len(per_cpu) <= 4096
            or any(_stats_integer(value) is None for value in per_cpu)):
        return None
    return len(per_cpu)


def _stats_projection(native, *, running, started_at=None):
    """Only raw counters and valid Docker frame deltas; never add aggregates."""
    cpu = native.get('cpu_stats') if isinstance(native.get('cpu_stats'), dict) else {}
    previous = native.get('precpu_stats') if isinstance(native.get('precpu_stats'), dict) else {}
    usage = cpu.get('cpu_usage') if isinstance(cpu.get('cpu_usage'), dict) else {}
    before_usage = previous.get('cpu_usage') if isinstance(previous.get('cpu_usage'), dict) else {}
    total, system = _stats_integer(usage.get('total_usage')), _stats_integer(cpu.get('system_cpu_usage'))
    before_total = _stats_integer(before_usage.get('total_usage'))
    before_system = _stats_integer(previous.get('system_cpu_usage'))
    count, before_count = _stats_cpu_count(cpu), _stats_cpu_count(previous)
    invalid_previous_count = (previous.get('online_cpus') is not None
        or before_usage.get('percpu_usage') is not None) and before_count is None
    read, preread = _stats_time(native.get('read')), _stats_time(native.get('preread'))
    percent = None
    if (running is True and read is not None and count is not None
            and all(value is not None for value in (total, system, before_total, before_system))
            and before_system > 0 and system > before_system and total >= before_total
            and total - before_total <= system - before_system
            and not invalid_previous_count
            and (before_count is None or before_count == count)
            and preread is not None and preread < read
            and (started_at is None or preread >= started_at)):
        percent = round((total - before_total) / (system - before_system) * count * 100.0, 6)
    memory = native.get('memory_stats') if isinstance(native.get('memory_stats'), dict) else {}
    memory_fields = memory.get('stats') if isinstance(memory.get('stats'), dict) else {}
    cache_field = 'cache' if 'cache' in memory_fields else 'file'
    inactive_field = 'total_inactive_file' if 'total_inactive_file' in memory_fields else 'inactive_file'
    cache, inactive = _stats_integer(memory_fields.get(cache_field)), _stats_integer(memory_fields.get(inactive_field))
    networks = native.get('networks')
    interfaces = None
    if isinstance(networks, dict):
        if len(networks) > MAX_STATS_INTERFACES:
            raise ProjectError('limit_exceeded')
        interfaces = []
        for name, value in sorted(networks.items()):
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}', name) or not isinstance(value, dict):
                raise ProjectError('native_unavailable')
            interfaces.append({'name': name, **{field: _stats_integer(value.get(field)) for field in
                ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets', 'rx_errors', 'tx_errors', 'rx_dropped', 'tx_dropped')}})
    block = native.get('blkio_stats') if isinstance(native.get('blkio_stats'), dict) else {}
    rows, devices = block.get('io_service_bytes_recursive'), None
    if isinstance(rows, list):
        if len(rows) > MAX_STATS_BLOCK_ROWS:
            raise ProjectError('limit_exceeded')
        indexed, seen = {}, set()
        for row in rows:
            if not isinstance(row, dict):
                raise ProjectError('native_unavailable')
            operation = row.get('op')
            if operation not in ('Read', 'Write'):
                continue
            major, minor = _stats_integer(row.get('major'), 2 ** 32 - 1), _stats_integer(row.get('minor'), 2 ** 32 - 1)
            if major is None or minor is None or (major, minor, operation) in seen:
                raise ProjectError('native_unavailable')
            seen.add((major, minor, operation))
            value = indexed.setdefault((major, minor), {'major': major, 'minor': minor,
                                                       'read_bytes': None, 'write_bytes': None})
            value['read_bytes' if operation == 'Read' else 'write_bytes'] = _stats_integer(row.get('value'))
            if len(indexed) > MAX_STATS_BLOCK_DEVICES:
                raise ProjectError('limit_exceeded')
        # Nonempty overlap-only/unknown rows cannot establish zero directional
        # I/O. Preserve unknown instead of turning filtering into an empty sum.
        devices = [indexed[key] for key in sorted(indexed)] if indexed or not rows else None
    return {'read_at': native.get('read') if read is not None else None,
            'cpu': {'usage_percent': percent, 'online_cpus': count,
                    'total_usage_ns': total, 'system_usage_ns': system},
            'memory': {'usage_bytes': _stats_integer(memory.get('usage')),
                       'limit_bytes': _stats_integer(memory.get('limit')),
                       'cache_bytes': cache, 'cache_source': cache_field if cache is not None else None,
                       'inactive_file_bytes': inactive,
                       'inactive_file_source': inactive_field if inactive is not None else None},
            'network': {'interfaces': interfaces}, 'block_io': {'devices': devices}}


def _running(items):
    values = [item['running'] for item in items]
    return all(values) if all(type(value) is bool for value in values) else None


def _health(items):
    values = [item['health'] for item in items]
    for value in ('unhealthy', 'starting'):
        if value in values:
            return value
    return values[0] if len(set(values)) == 1 else None


def _ports(value):
    if not isinstance(value, dict) or len(value) > 128:
        return []
    result = []
    for target, bindings in sorted(value.items()):
        match = re.fullmatch(r'([0-9]{1,5})/(tcp|udp)', target)
        if not match or not 1 <= int(match[1]) <= 65535:
            continue
        if bindings is None:
            bindings = [{'HostIp': '', 'HostPort': None}]
        if not isinstance(bindings, list) or len(bindings) > 128:
            raise ProjectError('limit_exceeded')
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            port, host = binding.get('HostPort'), binding.get('HostIp')
            published = int(port) if isinstance(port, str) and port.isascii() and port.isdigit() and 1 <= int(port) <= 65535 else None
            exposure = None
            try:
                address = ipaddress.ip_address(host)
                exposure = 'loopback' if address.is_loopback else 'all_interfaces' if address.is_unspecified else 'lan' if address.is_private else None
            except (ValueError, TypeError):
                pass
            result.append({'published': published, 'target': int(match[1]), 'protocol': match[2], 'exposure': exposure})
            if len(result) > 128:
                raise ProjectError('limit_exceeded')
    return result


def _container(detail, container_id, name, configuration_identity):
    if not isinstance(detail, dict) or detail.get('Id') != container_id:
        raise ProjectError('project_unavailable')
    config, host, state = detail.get('Config'), detail.get('HostConfig'), detail.get('State')
    if not all(isinstance(value, dict) for value in (config, host, state)):
        raise ProjectError('project_unavailable')
    labels = config.get('Labels')
    if not isinstance(labels, dict) or labels.get(_LABEL + 'project') != name:
        raise ProjectError('project_unavailable')
    service, config_hash = labels.get(_LABEL + 'service'), labels.get(_LABEL + 'config-hash')
    path, working = labels.get(_LABEL + 'project.config_files'), labels.get(_LABEL + 'project.working_dir')
    if (not isinstance(service, str) or not _NAME.fullmatch(service)
            or not isinstance(config_hash, str) or not _HEX.fullmatch(config_hash)
            or not isinstance(path, str) or ',' in path or not isinstance(working, str)
            or str(PurePosixPath(path).parent) != working
            or labels.get(_LABEL + 'oneoff', '').lower() not in ('false', '')):
        raise ProjectError('project_unavailable')
    configuration = configuration_identity(path)
    image = config.get('Image')
    image = image if isinstance(image, str) and len(image) <= 512 and _IMAGE.fullmatch(image) else None
    identity = {'name': service, 'container': container_id, 'config_hash': config_hash}
    # Engine objects cannot prove that every declared Compose service exists.
    risk = ['unresolved_configuration']
    if host.get('Privileged') is True:
        risk.append('privileged')
    if host.get('NetworkMode') == 'host':
        risk.append('host_network')
    if host.get('Devices') or host.get('DeviceRequests'):
        risk.append('devices')
    if host.get('CapAdd'):
        risk.append('capabilities')
    mounts = detail.get('Mounts')
    if not isinstance(mounts, list) or len(mounts) > 128:
        raise ProjectError('project_unavailable')
    projection = []
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get('Type') not in ('bind', 'volume', 'tmpfs'):
            risk.append('unresolved_configuration')
            continue
        if mount['Type'] == 'bind':
            risk.append('host_mounts')
        projection.append({'type': mount['Type'], 'access': 'readwrite' if mount.get('RW') is True else 'readonly' if mount.get('RW') is False else None,
                           'label': None})
    if image is None:
        risk.append('unresolved_configuration')
    restart = host.get('RestartPolicy', {})
    restart = restart.get('Name') if isinstance(restart, dict) else None
    if restart not in ('no', 'always', 'unless-stopped', 'on-failure'):
        restart = None
    network = detail.get('NetworkSettings', {})
    network = network if isinstance(network, dict) else {}
    health = state.get('Health')
    health = health.get('Status') if isinstance(health, dict) else 'not_configured'
    if health not in ('healthy', 'unhealthy', 'starting', 'not_configured'):
        health = None
    return {'configuration': configuration, 'identity': identity,
            'protected': service in ('r1-toolbox', 'r1-boot-once'),
            'running': state.get('Running') if type(state.get('Running')) is bool else None, 'health': health,
            'service': {'service_id': 'service_' + _hash([name, identity])[:24], 'name': service,
                        'image_reference': image, 'ports': _ports(network.get('Ports', {})),
                        'mounts': projection, 'restart_policy': restart, 'risk_flags': sorted(set(risk))}}


def _valid_identity(value):
    """The private record contains only binding metadata, never arbitrary JSON."""
    if (not isinstance(value, dict) or set(value) != {'engine', 'name', 'configuration', 'services'}
            or not isinstance(value['engine'], str) or not _ENGINE.fullmatch(value['engine'])
            or not isinstance(value['name'], str) or not _NAME.fullmatch(value['name'])):
        return False
    config = value['configuration']
    if (not isinstance(config, dict) or set(config) != {'path', 'identity'}
            or not _plain(config['path'], 1024) or not config['path'].startswith('/')
            or '\\' in config['path'] or not config['path'].endswith(('.yml', '.yaml'))
            or any(part in ('', '.', '..') for part in config['path'].split('/')[1:])
            or not isinstance(config['identity'], list) or len(config['identity']) != 5
            or any(type(item) is not int or item < 0 for item in config['identity'])
            or config['identity'][2] > MAX_BODY
            or not isinstance(value['services'], list) or not 0 < len(value['services']) <= MAX_CONTAINERS):
        return False
    names, containers = set(), set()
    for service in value['services']:
        if (not isinstance(service, dict) or set(service) != {'name', 'container', 'config_hash'}
                or not isinstance(service['name'], str) or not _NAME.fullmatch(service['name'])
                or service['name'] in names or not isinstance(service['container'], str)
                or not _HEX.fullmatch(service['container']) or service['container'] in containers
                or not isinstance(service['config_hash'], str) or not _HEX.fullmatch(service['config_hash'])):
            return False
        names.add(service['name'])
        containers.add(service['container'])
    return True


class DockerProjectRegistry:
    """Owner methods mutate only this private registry; machine methods read.

    Callers must supply the owner's existing HTTP session/CSRF gates for owner
    methods. Machine calls repeat the policy-v3 resource/tool checks here; a
    role, domain or administrator status alone never selects projects.
    """

    def __init__(self, state_dir, *, backend=None):
        self.path = Path(state_dir) / 'docker-projects.json'
        self.backend = backend or LocalDockerReader()
        self._lock = threading.RLock()
        self._write_failed = False

    def _read(self):
        if self._write_failed:
            raise ProjectError('project_unavailable')
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            return {'version': 1, 'revision': 0, 'projects': []}
        except OSError:
            raise ProjectError('project_unavailable') from None
        try:
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or self.path.is_symlink() or info.st_size > MAX_STATE
                        or os.name != 'nt' and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid())):
                    raise ProjectError('project_unavailable')
                raw = stream.read(MAX_STATE + 1)
                if len(raw) > MAX_STATE:
                    raise ProjectError('limit_exceeded')
                data = _json(raw)
            if (not isinstance(data, dict) or set(data) != {'version', 'revision', 'projects'}
                    or type(data['version']) is not int or data['version'] != 1
                    or type(data['revision']) is not int or data['revision'] < 0
                    or not isinstance(data['projects'], list) or len(data['projects']) > 128):
                raise ProjectError('project_unavailable')
            ids = set()
            for row in data['projects']:
                if (not isinstance(row, dict) or set(row) != {'id', 'label', 'revision', 'revoked', 'protected', 'binding', 'identity'}
                        or not isinstance(row['id'], str) or not _PROJECT.fullmatch(row['id'])
                        or row['id'] in ids or not _plain(row['label']) or type(row['revision']) is not int or row['revision'] < 1
                        or type(row['revoked']) is not bool or type(row['protected']) is not bool
                        or not isinstance(row['binding'], str) or not _HEX.fullmatch(row['binding'])
                        or not _valid_identity(row['identity']) or _hash(row['identity']) != row['binding']):
                    raise ProjectError('project_unavailable')
                ids.add(row['id'])
            return data
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise ProjectError('project_unavailable') from None

    def _write(self, data):
        temporary = None
        try:
            raw = json.dumps(data, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
            if len(raw) > MAX_STATE:
                raise OSError()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise OSError()
            descriptor, temporary = tempfile.mkstemp(prefix='.docker-projects-', dir=self.path.parent)
            with os.fdopen(descriptor, 'wb') as stream:
                os.chmod(temporary, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            if os.name != 'nt':
                descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError:
            self._write_failed = True
            raise ProjectError('project_unavailable') from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @staticmethod
    def _public(row, live=None):
        available = live is not None and not row['revoked']
        return {'id': row['id'], 'label': row['label'], 'revision': row['revision'],
                'operations': ['inspect'], 'protected': row['protected'], 'revoked': row['revoked'],
                'available': available, 'reason': None if available else '项目已撤销或当前绑定无法确认。'}

    def catalog(self):
        try:
            live = self.backend.snapshot()
            candidates = [{'id': 'candidate_' + row['binding'][:24], 'label': row['identity']['name'],
                           'service_count': len(row['services']), 'protected': row['protected'],
                           'available': True, 'reason': None, 'source': 'engine'} for row in live]
            return {'ok': True, 'available': bool(candidates), 'status': 'available' if candidates else 'unavailable',
                    'reason': None if candidates else '未发现身份完整的 Compose 项目。', 'candidates': candidates}
        except ProjectError as error:
            return {**_failure(error.code), 'available': False, 'status': error.code, 'reason': _MESSAGES[error.code], 'candidates': []}

    @_locked
    def metadata(self):
        """Registry-only owner metadata; never probe Docker or configuration paths."""
        try:
            data = self._read()
            rows = [dict(self._public(row), available=None, verification='not_checked',
                         reason='尚未进行实时核验。') for row in data['projects']]
            return {'ok': True, 'revision': data['revision'], 'projects': rows}
        except ProjectError as error:
            return _failure(error.code)

    @_locked
    def snapshot(self):
        with self._lock:
            try:
                data = self._read()
                try:
                    live = {row['binding']: row for row in self.backend.snapshot()} if data['projects'] else {}
                except ProjectError:
                    live = {}
                return {'ok': True, 'revision': data['revision'],
                        'projects': [self._public(row, live.get(row['binding'])) for row in data['projects']]}
            except ProjectError as error:
                return _failure(error.code)

    @_locked
    def register(self, body):
        if (not isinstance(body, dict) or not {'candidate_id'} <= set(body) <= {'candidate_id', 'label'}
                or not isinstance(body['candidate_id'], str) or not _CANDIDATE.fullmatch(body['candidate_id'])
                or 'label' in body and not _plain(body['label'])):
            return _failure('invalid_arguments')
        with self._lock:
            try:
                data = self._read()
                current = [row for row in self.backend.snapshot() if 'candidate_' + row['binding'][:24] == body['candidate_id']]
                if len(current) != 1:
                    raise ProjectError('project_unavailable')
                current = current[0]
                if any(row['binding'] == current['binding'] and not row['revoked'] for row in data['projects']):
                    raise ProjectError('invalid_arguments')
                if len(data['projects']) >= 128:
                    raise ProjectError('limit_exceeded')
                row = {'id': 'project_' + os.urandom(12).hex(), 'label': body.get('label', current['identity']['name']),
                       'revision': 1, 'revoked': False, 'protected': current['protected'],
                       'binding': current['binding'], 'identity': copy.deepcopy(current['identity'])}
                data['projects'].append(row)
                data['revision'] += 1
                self._write(data)
                return {'ok': True, 'project': self._public(row, current)}
            except ProjectError as error:
                return _failure(error.code)

    @_locked
    def _edit(self, body, revoke):
        fields = {'project_id'} if revoke else {'project_id', 'label'}
        if (not isinstance(body, dict) or set(body) != fields or not isinstance(body['project_id'], str)
                or not _PROJECT.fullmatch(body['project_id']) or not revoke and not _plain(body['label'])):
            return _failure('invalid_arguments')
        with self._lock:
            try:
                data = self._read()
                rows = [row for row in data['projects'] if row['id'] == body['project_id'] and not row['revoked']]
                if len(rows) != 1:
                    raise ProjectError('project_unavailable')
                row = rows[0]
                if revoke:
                    row['revoked'] = True
                else:
                    row['label'] = body['label']
                row['revision'] += 1
                data['revision'] += 1
                self._write(data)
                return {'ok': True, 'project': self._public(row)}
            except ProjectError as error:
                return _failure(error.code)

    def update(self, body):
        return self._edit(body, False)

    def revoke(self, body):
        return self._edit(body, True)

    @staticmethod
    def _grants(principal, tool):
        if (not isinstance(principal, dict) or type(principal.get('policy_version')) is not int
                or principal['policy_version'] != 3 or principal.get('role') not in ('readonly', 'readwrite', 'admin')
                or not isinstance(principal.get('scopes'), list) or 'docker' not in principal['scopes']
                or not isinstance(principal.get('tool_grants'), list) or tool not in principal['tool_grants']
                or not isinstance(principal.get('docker_projects'), list) or len(principal['docker_projects']) > 128):
            raise ProjectError('permission_denied')
        ids = set()
        for grant in principal['docker_projects']:
            if (not isinstance(grant, dict) or set(grant) != {'id', 'operations'}
                    or not isinstance(grant['id'], str) or not _PROJECT.fullmatch(grant['id'])
                    or grant['id'] in ids or not isinstance(grant['operations'], list)
                    or not 1 <= len(grant['operations']) <= 3
                    or any(not isinstance(item, str) for item in grant['operations'])
                    or len(set(grant['operations'])) != len(grant['operations'])
                    or any(item not in ('inspect', 'control', 'update_image') for item in grant['operations'])
                    or 'inspect' not in grant['operations']
                    or any(item != 'inspect' for item in grant['operations']) and principal['role'] != 'admin'):
                raise ProjectError('permission_denied')
            ids.add(grant['id'])
        return ids

    def _adopt_verified_update(self, project_id, previous_binding, live, *, previous_revision=None):
        """Internal CAS after an image-only update; never an owner HTTP mutation.

        The executor has already verified the replacement CID/config/image. Keep
        the registered project ID so existing exact grants do not migrate to an
        unrelated same-name project or silently gain another service.
        """
        with self._lock:
            data = self._read()
            rows = [row for row in data['projects'] if row['id'] == project_id and not row['revoked']]
            if (len(rows) != 1 or rows[0]['protected'] or rows[0]['binding'] != previous_binding
                    or previous_revision is not None and rows[0]['revision'] != previous_revision):
                raise ProjectError('project_binding_changed')
            row = rows[0]
            identity = live.get('identity') if isinstance(live, dict) else None
            old = row['identity']
            if (not _valid_identity(identity) or live.get('protected') is not False
                    or live.get('binding') != _hash(identity)
                    or identity['engine'] != old['engine'] or identity['name'] != old['name']
                    or identity['configuration']['path'] != old['configuration']['path']
                    or [item['name'] for item in identity['services']] != [item['name'] for item in old['services']]):
                raise ProjectError('project_binding_changed')
            row.update(identity=copy.deepcopy(identity), binding=live['binding'], revision=row['revision'] + 1)
            data['revision'] += 1
            self._write(data)

    @_locked
    def list_projects(self, principal):
        try:
            ids = self._grants(principal, 'list_docker_projects')
            if not ids:
                return {'ok': True, 'projects': []}
            with self._lock:
                rows = [row for row in self._read()['projects'] if row['id'] in ids and not row['revoked']]
                if not rows:
                    return {'ok': True, 'projects': []}
                try:
                    live = {row['binding']: row for row in self.backend.snapshot()}
                except ProjectError:
                    live = {}
                return {'ok': True, 'projects': [
                    {'project_id': row['id'], 'label': row['label'], 'available': row['binding'] in live,
                     'reason': None if row['binding'] in live else '项目绑定或来源当前无法确认。'} for row in rows]}
        except ProjectError as error:
            return _failure(error.code)

    def _selected(self, principal, project_id, tool):
        ids = self._grants(principal, tool)
        if not isinstance(project_id, str) or project_id not in ids:
            raise ProjectError('permission_denied')
        rows = [row for row in self._read()['projects'] if row['id'] == project_id and not row['revoked']]
        if len(rows) != 1:
            raise ProjectError('permission_denied')
        row = rows[0]
        current = [item for item in self.backend.snapshot() if item['binding'] == row['binding']]
        if len(current) != 1:
            raise ProjectError('project_binding_changed')
        return row, current[0]

    @_locked
    def get_status(self, principal, project_id):
        with self._lock:
            try:
                row, live = self._selected(principal, project_id, 'get_docker_project_status')
                return {'ok': True, 'project': {'project_id': row['id'], 'label': row['label'],
                        'native_status': None, 'pop_status': None, 'running': live['running'], 'health': live['health'],
                        'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'source': 'engine'}}
            except ProjectError as error:
                return _failure(error.code)

    @_locked
    def get_summary(self, principal, project_id):
        with self._lock:
            try:
                row, live = self._selected(principal, project_id, 'get_docker_project_summary')
                return {'ok': True, 'project_id': row['id'], 'label': row['label'],
                        'services': copy.deepcopy(live['services']),
                        'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
            except ProjectError as error:
                return _failure(error.code)

    def get_resource_usage(self, principal, project_id, *, authorizer):
        """Return current counters only after exact grant/lifecycle rechecks.

        The callback is supplied by the integration's trusted current-key gate,
        never by a JSON field. Registry locks are held only while copying each
        revision, so an owner can revoke registration during network reads.
        """
        try:
            _authorized(lambda: authorizer(principal))
            ids = self._grants(principal, 'get_docker_project_resource_usage')
            if not isinstance(project_id, str) or project_id not in ids:
                raise ProjectError('permission_denied')
            with self._lock:
                rows = [row for row in self._read()['projects'] if row['id'] == project_id and not row['revoked']]
                if len(rows) != 1:
                    raise ProjectError('permission_denied')
                selected = copy.deepcopy(rows[0])

            def current():
                _authorized(lambda: authorizer(principal))
                if project_id not in self._grants(principal, 'get_docker_project_resource_usage'):
                    return False
                with self._lock:
                    active = [row for row in self._read()['projects'] if row['id'] == project_id and not row['revoked']]
                    return len(active) == 1 and active[0] == selected

            services = self.backend.resource_usage(selected['identity'], authorizer=current)
            _authorized(current)
            return {'ok': True, 'project_id': selected['id'], 'label': selected['label'],
                    'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'source': 'engine_stats', 'consistency': 'per_service_observation',
                    'services': services}
        except ProjectError as error:
            return _failure(error.code)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
            return _failure('native_unavailable')

"""Explicit one-shot factory storage query; never a passive polling source.

The existing owner session is read without authentication writes. The native
space_manage action itself can change device links, SMART state and caches.
Only an explicitly authorized caller may invoke this fixed local request.
"""
import json
import math
import re
import socket
import time
import unicodedata
from urllib.parse import quote
import zlib


OWNER_ID = 100000
HOST, PORT = '127.0.0.1', 80
SOURCE = 'histor.space_manage.loopback_http'
REQUEST_PATH = '/rest/1.2/config?action=space_manage&check_warning=1&access_token='
TOTAL_TIMEOUT = 15.0
MAX_HEADERS = 32 * 1024
MAX_BODY = 1024 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_ITEMS = 32768
MAX_ROWS, MAX_SPACES, MAX_CHUNKS = 256, 128, 4096
MAX_SAFE_INTEGER = 2 ** 53 - 1
_DEVICE = re.compile(r'(?:[hs]d[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)\Z')
_ERRORS = frozenset((
    'credential_read_failed', 'credential_unavailable', 'token_unavailable',
    'timeout', 'transport_failed', 'headers_too_large', 'body_too_large',
    'invalid_http', 'incomplete_response', 'unsupported_encoding',
    'unsupported_content_type', 'redirect_refused', 'http_error',
    'invalid_json', 'invalid_transport_result', 'schema_unavailable',
    'native_code_unavailable', 'native_request_rejected', 'incomplete_fields',
))


class StorageProbeError(Exception):
    """Only fixed classifications; never retain a URL, body or vendor message."""
    def __init__(self, reason, http_status=None):
        self.reason = reason if reason in _ERRORS else 'transport_failed'
        self.http_status = _http_status(http_status)
        super().__init__(self.reason)


def _http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise StorageProbeError('timeout')
    return value


def _valid_token(value):
    return (isinstance(value, str) and 0 < len(value) <= 4096
            and not any(unicodedata.category(char) in ('Cc', 'Cs') or char.isspace() for char in value))


class _Wire:
    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline
        self.buffer, self.received = bytearray(), 0

    def more(self, *, eof=False):
        self.connection.settimeout(_remaining(self.deadline))
        ceiling = MAX_HEADERS + MAX_BODY + 64 * 1024
        part = self.connection.recv(min(8192, ceiling + 1 - self.received))
        _remaining(self.deadline)
        self.received += len(part)
        if self.received > ceiling:
            raise StorageProbeError('body_too_large')
        if not part:
            if eof:
                return False
            raise StorageProbeError('incomplete_response')
        self.buffer.extend(part)
        return True

    def until(self, delimiter, limit, reason='headers_too_large'):
        while True:
            index = self.buffer.find(delimiter)
            if index >= 0:
                end = index + len(delimiter)
                if end > limit:
                    raise StorageProbeError(reason)
                value = bytes(self.buffer[:index])
                del self.buffer[:end]
                return value
            if len(self.buffer) >= limit:
                raise StorageProbeError(reason)
            self.more()

    def exact(self, count):
        if count > MAX_BODY:
            raise StorageProbeError('body_too_large')
        while len(self.buffer) < count:
            self.more()
        value = bytes(self.buffer[:count])
        del self.buffer[:count]
        return value


def _headers(raw):
    try:
        lines = raw.decode('ascii').split('\r\n')
    except UnicodeError:
        raise StorageProbeError('invalid_http') from None
    match = re.fullmatch(r'HTTP/1\.[01] ([1-5][0-9]{2})(?: [\x20-\x7e]*)?', lines[0])
    if not match or len(lines) > 129:
        raise StorageProbeError('invalid_http')
    headers = {}
    for line in lines[1:]:
        if ':' not in line or line[:1].isspace():
            raise StorageProbeError('invalid_http')
        key, value = line.split(':', 1)
        key, value = key.lower(), value.strip(' \t')
        if (not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", key) or key in headers
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise StorageProbeError('invalid_http')
        headers[key] = value
    return int(match[1]), headers


def _body(wire, headers):
    encoding = headers.get('transfer-encoding')
    if encoding not in (None, 'chunked'):
        raise StorageProbeError('unsupported_encoding')
    if encoding and 'content-length' in headers:
        raise StorageProbeError('invalid_http')
    if encoding:
        value = bytearray()
        for _ in range(MAX_CHUNKS):
            size = wire.until(b'\r\n', 128, 'invalid_http')
            if not re.fullmatch(rb'[0-9a-fA-F]{1,8}', size):
                raise StorageProbeError('invalid_http')
            size = int(size, 16)
            if size == 0:
                # This fixed native response has no permitted trailer fields.
                if wire.until(b'\r\n', MAX_HEADERS, 'invalid_http'):
                    raise StorageProbeError('invalid_http')
                break
            if len(value) + size > MAX_BODY:
                raise StorageProbeError('body_too_large')
            value.extend(wire.exact(size))
            if wire.exact(2) != b'\r\n':
                raise StorageProbeError('invalid_http')
        else:
            raise StorageProbeError('body_too_large')
        value = bytes(value)
    elif 'content-length' in headers:
        length = headers['content-length']
        if not re.fullmatch(r'[0-9]{1,10}', length):
            raise StorageProbeError('invalid_http')
        value = wire.exact(int(length))
    else:
        # Connection-close framing is complete only at EOF, never merely when
        # a prefix happens to parse as JSON.
        value = bytearray()
        while True:
            value.extend(wire.buffer)
            wire.buffer.clear()
            if len(value) > MAX_BODY:
                raise StorageProbeError('body_too_large')
            if not wire.more(eof=True):
                break
        value = bytes(value)
    if wire.buffer:
        raise StorageProbeError('invalid_http')
    content_encoding = headers.get('content-encoding', 'identity').lower()
    if content_encoding == 'gzip':
        try:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decoder.decompress(value, MAX_BODY + 1)
            if len(decoded) > MAX_BODY or decoder.unconsumed_tail:
                raise StorageProbeError('body_too_large')
            if not decoder.eof or decoder.unused_data:
                raise StorageProbeError('unsupported_encoding')
            value = decoded
        except zlib.error:
            raise StorageProbeError('unsupported_encoding') from None
    elif content_encoding != 'identity':
        raise StorageProbeError('unsupported_encoding')
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StorageProbeError('invalid_json')
        result[key] = value
    return result


def _constant(_):
    raise StorageProbeError('invalid_json')


def _validate_json(value):
    if not isinstance(value, dict):
        raise StorageProbeError('invalid_json')
    pending, count = [(value, 1)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > MAX_JSON_DEPTH or count > MAX_JSON_ITEMS:
            raise StorageProbeError('invalid_json')
        if isinstance(item, (dict, list)):
            pending.extend((child, depth + 1) for child in
                           (item.values() if isinstance(item, dict) else item))
        elif isinstance(item, float) and not math.isfinite(item):
            raise StorageProbeError('invalid_json')
    return value


def _json(raw):
    try:
        return _validate_json(json.loads(raw.decode('utf8'), object_pairs_hook=_object,
                                         parse_constant=_constant))
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise StorageProbeError('invalid_json') from None


def query_storage_frame(access_token, *, root='/'):
    """Exactly one fixed loopback HTTP request, with an independent parser."""
    if not _valid_token(access_token):
        raise StorageProbeError('token_unavailable')
    deadline = time.monotonic() + TOTAL_TIMEOUT
    connection = None
    status = None
    try:
        connection = socket.create_connection((HOST, PORT), timeout=_remaining(deadline))
        path = REQUEST_PATH + quote(access_token, safe='')
        request = ('GET ' + path + ' HTTP/1.1\r\nHost: 127.0.0.1:80\r\n'
                   'Accept: application/json\r\nAccept-Encoding: identity\r\n'
                   'Connection: close\r\n\r\n').encode('ascii')
        connection.settimeout(_remaining(deadline))
        connection.sendall(request)
        wire = _Wire(connection, deadline)
        status, headers = _headers(wire.until(b'\r\n\r\n', MAX_HEADERS))
        if 300 <= status <= 399:
            raise StorageProbeError('redirect_refused', status)
        if status != 200:
            raise StorageProbeError('http_error', status)
        if headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise StorageProbeError('unsupported_content_type', status)
        value = _json(_body(wire, headers))
        _remaining(deadline)
        return status, value
    except StorageProbeError as error:
        if error.http_status is None:
            error.http_status = status
        raise
    except (TimeoutError, socket.timeout):
        raise StorageProbeError('timeout', status) from None
    except Exception:
        raise StorageProbeError('transport_failed', status) from None
    finally:
        if connection is not None:
            connection.close()


def _text(value, limit=512, *, serial=False, forbidden=()):
    if (not isinstance(value, str) or not value.strip()
            or len(value.encode('utf8', 'surrogatepass')) > limit
            or any(unicodedata.category(char) in ('Cc', 'Cs') for char in value)
            or '/' in value or '\\' in value
            or any(secret and secret in value for secret in forbidden)):
        return None
    if serial and not re.fullmatch(r'[A-Za-z0-9 ._:+-]+', value):
        return None
    return value


def _port(value):
    return value if type(value) is int and 0 <= value <= 65535 else None


def _project(frame, *, forbidden=()):
    # The audited serializer has no data/result wrapper. Only these three
    # physical-record paths may expose a serial, never arbitrary recursion.
    payload = frame
    if not any(key in payload for key in ('space_list', 'unused_disk_list')):
        raise StorageProbeError('schema_unavailable')
    disks, spaces, partial = [], [], False

    def disk(node, nickname, name, location):
        nonlocal partial
        if not isinstance(node, dict):
            raise StorageProbeError('schema_unavailable')
        serial = _text(node.get('disk_sn'), serial=True, forbidden=forbidden)
        device = node.get('disk_name')
        disks.append({'disk_sn': serial, 'port': _port(node.get('port')),
                      'disk_name': device if isinstance(device, str) and _DEVICE.fullmatch(device) else None,
                      'space_nickname': nickname, 'space_name': name,
                      'location': location, 'conflict': False})
        if len(disks) > MAX_ROWS:
            raise StorageProbeError('body_too_large')
        partial |= serial is None

    for key in ('space_list', 'unused_disk_list'):
        rows = payload.get(key)
        if rows is None:
            partial = True
            continue
        if not isinstance(rows, list) or len(rows) > MAX_SPACES or any(not isinstance(row, dict) for row in rows):
            raise StorageProbeError('schema_unavailable')
        for row in rows:
            nickname = _text(row.get('space_nickname'), forbidden=forbidden) if key == 'space_list' else None
            name = _text(row.get('space_name'), forbidden=forbidden) if key == 'space_list' else None
            if key == 'space_list':
                spaces.append({'space_nickname': nickname, 'space_name': name})
                before = len(disks)
                members = row.get('disk_info')
                if members is not None:
                    if not isinstance(members, list) or len(members) > MAX_ROWS:
                        raise StorageProbeError('schema_unavailable')
                    for member in members:
                        disk(member, nickname, name, key)
                expanding = row.get('expanding_disk_info')
                if expanding is not None:
                    disk(expanding, nickname, name, key)
                partial |= len(disks) == before
            else:
                disk(row, None, None, key)
    # A port, kernel name or serial collision is not resolved by array order.
    for row in disks:
        others = [other for other in disks if other is not row]
        conflict = any(row['disk_sn'] is not None and other['disk_sn'] is not None and (
            row['port'] is not None and row['port'] == other['port'] and row['disk_sn'] != other['disk_sn']
            or row['disk_name'] is not None and row['disk_name'] == other['disk_name'] and row['disk_sn'] != other['disk_sn']
            or row['disk_sn'] == other['disk_sn'] and row['port'] is not None and other['port'] is not None
            and row['port'] != other['port']
            or row['disk_sn'] == other['disk_sn'] and row['disk_name'] is not None and other['disk_name'] is not None
            and row['disk_name'] != other['disk_name']) for other in others)
        row['conflict'] = conflict
    for row in disks:
        if row['conflict']:
            row['disk_sn'] = row['port'] = row['disk_name'] = None
            partial = True
    unique = {json.dumps(row, sort_keys=True, ensure_ascii=True): row for row in disks}
    return {'disks': list(unique.values()), 'spaces': spaces}, partial


def collect_storage_probe(root='/', *, session_reader=None, transport=None):
    """One explicit native query using only the existing fixed-owner session."""
    started = time.monotonic()
    result = {'schema_version': 1, 'source': SOURCE, 'available': False,
              'status': 'unavailable', 'reason': None, 'attempted': False,
              'http_status': None, 'native_code': None, 'elapsed_ms': None,
              'credential_read_only': True, 'native_query_may_update_state': True, 'data': None}
    try:
        try:
            if session_reader is None:
                from vendor_media_auth import read_session
                session_reader = read_session
            session, credential = session_reader(root=root)
            user_id = getattr(session, 'user_id', None)
            valid = (session is not None and isinstance(credential, dict)
                     and credential.get('available') is True and credential.get('status') == 'ready'
                     and type(credential.get('user_id')) is int and credential['user_id'] == OWNER_ID
                     and type(user_id) is int and user_id == OWNER_ID)
        except Exception:
            raise StorageProbeError('credential_read_failed') from None
        if not valid:
            raise StorageProbeError('credential_unavailable')
        try:
            token = session.derive_access_token()
        except Exception:
            raise StorageProbeError('token_unavailable') from None
        if not _valid_token(token):
            raise StorageProbeError('token_unavailable')
        result['attempted'] = True
        response = (transport or query_storage_frame)(token, root=root)
        if not isinstance(response, tuple) or len(response) != 2 or _http_status(response[0]) is None:
            raise StorageProbeError('invalid_transport_result')
        status, frame = response
        result['http_status'] = status
        if 300 <= status <= 399:
            raise StorageProbeError('redirect_refused', status)
        if status != 200:
            raise StorageProbeError('http_error', status)
        frame = _validate_json(frame)
        # The audited success serializer emits the storage object directly,
        # without code/msg. Error envelopes may still carry an explicit code.
        if 'code' in frame:
            code = frame['code']
            if type(code) is not int or abs(code) > MAX_SAFE_INTEGER:
                raise StorageProbeError('native_code_unavailable')
            result['native_code'] = code
            if code != 0:
                result.update(status='native_error', reason='native_request_rejected')
                return result
        data, partial = _project(frame, forbidden=(token, quote(token, safe='')))
        result.update(available=True, status='partial' if partial else 'ok',
                      reason='incomplete_fields' if partial else None, data=data)
    except StorageProbeError as error:
        result['reason'] = error.reason
        if error.http_status is not None:
            result['http_status'] = error.http_status
        if error.reason in ('invalid_json', 'schema_unavailable', 'native_code_unavailable', 'invalid_transport_result'):
            result['status'] = 'invalid_response'
    except Exception:
        result['reason'] = 'transport_failed'
    finally:
        result['elapsed_ms'] = round(max(0, time.monotonic() - started) * 1000, 3)
    return result

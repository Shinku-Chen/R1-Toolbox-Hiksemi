"""显式开发探测：固定主账号、固定原厂相册接口，只取第一个 JSON 分段。

这不是被动只读监控。读取现有会话不修改凭据，但原厂查询可能读取相册
数据库、报告计数、更新完成标记及升级状态。不得用于后台轮询。
"""

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import time
from types import MappingProxyType
from urllib.parse import quote

try:
    import ssl
except ImportError:
    ssl = None


OWNER_ID = 100000
HOST = '127.0.0.1'
PORT = 20443
ACTION = 'get_face_ai_status'
REQUEST_PATH = '/rest/1.1/fcabm?action=' + ACTION + '&access_token='
# 调用方只能选择经审计的状态查询；路径与原厂动作均不能来自请求参数。
MEDIA_QUERIES = MappingProxyType({
    'face': ('/rest/1.1/fcabm', ACTION),
    'ocr': ('/rest/1.1/file', 'get_ocr_status'),
    'thumbnail': ('/rest/1.1/file', 'get_thumb_convert_process'),
    'similar': ('/rest/1.1/file', 'get_similar_status'),
    'ai': ('/rest/1.1/fcabm', 'get_ai_status'),
    'scraper': ('/rest/1.1/scraper', 'lib_scan_status'),
    'shared_album': ('/rest/1.1/album', 'share_album_progress'),
    'shared_album_catalog': ('/rest/1.1/album', 'get_share_album_list'),
})
TOTAL_TIMEOUT = 10.0
MAX_HEADERS = 32 * 1024
MAX_BODY = 64 * 1024
MAX_CERT = 64 * 1024
MAX_JSON_DEPTH = 16
MAX_SAFE_INTEGER = 2 ** 53 - 1
_COUNTERS = ('total', 'totalpic', 'totalvid', 'processed', 'processedpic', 'processedvid')
_NOTES = (
    'credential_read_only', 'native_query_may_update_state',
    'scope_is_owner_configured_recognition_ranges',
    'sync_percent_is_not_overall_completion',
    'zero_total_does_not_prove_completion',
    'remaining_estimate_unit_unverified',
    'range_status_is_constant_zero_in_audited_firmware',
)
_ERRORS = frozenset((
    'tls_unavailable', 'certificate_unavailable', 'certificate_invalid', 'certificate_mismatch',
    'timeout', 'transport_failed', 'headers_too_large', 'body_too_large',
    'invalid_http', 'unsupported_encoding', 'unsupported_content_type',
    'invalid_multipart', 'incomplete_response', 'invalid_json',
    'redirect_refused', 'http_error', 'invalid_transport_result',
    'credential_unavailable', 'credential_read_failed', 'token_unavailable',
    'unsupported_query',
    'invalid_parameters',
))


class MediaProbeError(Exception):
    """只携带固定错误码；不能包含请求地址、凭据或原厂消息。"""

    def __init__(self, reason, http_status=None):
        self.reason = reason if reason in _ERRORS else 'transport_failed'
        self.http_status = _http_status(http_status)
        super().__init__(self.reason)


def _http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def _integer(value):
    return value if type(value) is int and 0 <= value <= MAX_SAFE_INTEGER else None


def _native_code(value):
    # 原厂返回码为有符号整数；限制到 JSON 可精确表达的范围。
    return value if type(value) is int and abs(value) <= MAX_SAFE_INTEGER else None


def _ratio(processed, total):
    return processed / total if (processed is not None and total is not None
                                 and total > 0 and processed <= total) else None


def project_face_frame(frame):
    """只投影已审计字段；ratio 为 0..1，percent 仅代表范围同步阶段。"""
    if not isinstance(frame, dict):
        raise MediaProbeError('invalid_json')
    values = {key: _integer(frame.get(key)) for key in _COUNTERS}
    percent = frame.get('percent')
    values.update({
        'picture_completion_ratio': _ratio(values['processedpic'], values['totalpic']),
        'video_completion_ratio': _ratio(values['processedvid'], values['totalvid']),
        'sync_percent': (percent if type(percent) in (int, float)
                         and 0 <= percent <= 100 and math.isfinite(percent) else None),
        'upgrade_status': frame.get('status') if type(frame.get('status')) is int
                          and frame['status'] in (0, 1) else None,
        'range_status': 0 if type(frame.get('range_status')) is int
                       and frame['range_status'] == 0 else None,
        'estimated_remaining': {'value': _integer(frame.get('left')), 'unit': None},
    })
    return values


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise MediaProbeError('timeout')
    return value


def _certificate(root):
    """只读固定公开证书的第一个 PEM 叶证书，绝不读取私钥。"""
    path = Path(root) / 'tmp' / 'Cert.pem'
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
                             | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise MediaProbeError('certificate_unavailable')
            value = stream.read(MAX_CERT + 1)
    except MediaProbeError:
        raise
    except (OSError, ValueError):
        raise MediaProbeError('certificate_unavailable') from None
    if len(value) > MAX_CERT:
        raise MediaProbeError('certificate_invalid')
    match = re.search(rb'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+'
                      rb'-----END CERTIFICATE-----', value)
    if match is None:
        raise MediaProbeError('certificate_invalid')
    try:
        result = ssl.PEM_cert_to_DER_cert(match.group().decode('ascii'))
    except (ValueError, UnicodeError, ssl.SSLError):
        raise MediaProbeError('certificate_invalid') from None
    if not result:
        raise MediaProbeError('certificate_invalid')
    return result


class _WireReader:
    """增量 socket 读取器；每次 recv 都重新扣除总时间，不等满缓冲区。"""

    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline
        self.buffer = bytearray()
        self.received = 0
        self.limit = MAX_HEADERS + MAX_BODY + 8192

    def _recv(self):
        self.connection.settimeout(_remaining(self.deadline))
        value = self.connection.recv(min(4096, self.limit - self.received + 1))
        self.received += len(value)
        if self.received > self.limit:
            raise MediaProbeError('body_too_large')
        if not value:
            raise MediaProbeError('incomplete_response')
        self.buffer.extend(value)

    def until(self, delimiter, limit, error='headers_too_large'):
        while True:
            index = self.buffer.find(delimiter)
            if index >= 0:
                end = index + len(delimiter)
                if end > limit:
                    raise MediaProbeError(error)
                result = bytes(self.buffer[:index])
                del self.buffer[:end]
                return result
            if len(self.buffer) >= limit:
                raise MediaProbeError(error)
            self._recv()

    def exact(self, count):
        while len(self.buffer) < count:
            self._recv()
        value = bytes(self.buffer[:count])
        del self.buffer[:count]
        return value

    def some(self, count):
        if not self.buffer:
            self._recv()
        value = bytes(self.buffer[:count])
        del self.buffer[:count]
        return value


def _headers(value, *, response=False):
    try:
        lines = value.decode('ascii').split('\r\n')
    except UnicodeError:
        raise MediaProbeError('invalid_http') from None
    status_code = None
    if response:
        match = re.fullmatch(r'HTTP/1\.[01] ([1-5][0-9]{2})(?: [\x20-\x7e]*)?', lines.pop(0))
        if match is None:
            raise MediaProbeError('invalid_http')
        status_code = int(match.group(1))
    result = {}
    for line in lines:
        if ':' not in line or line[:1].isspace():
            raise MediaProbeError('invalid_http')
        key, item = line.split(':', 1)
        key = key.lower()
        if not re.fullmatch(r'[a-z0-9!#$%&\'*+.^_`|~-]+', key) or key in result:
            raise MediaProbeError('invalid_http')
        item = item.strip(' \t')
        if any(ord(char) < 32 and char != '\t' or ord(char) == 127 for char in item):
            raise MediaProbeError('invalid_http')
        result[key] = item
    return status_code, result


def _content_length(value):
    if value is None:
        return None
    if not re.fullmatch(r'[0-9]{1,10}', value):
        raise MediaProbeError('invalid_http')
    count = int(value)
    if count > MAX_BODY:
        raise MediaProbeError('body_too_large')
    return count


class _BodyReader:
    """限制解码后正文，并增量解析 HTTP chunk framing，不消费后续分段。"""

    def __init__(self, wire, headers):
        self.wire = wire
        self.buffer = bytearray()
        self.count = 0
        self.chunk_left = 0
        self.chunk_end = False
        encoding = headers.get('transfer-encoding')
        if encoding not in (None, 'chunked') or headers.get('content-encoding', 'identity') != 'identity':
            raise MediaProbeError('unsupported_encoding')
        if encoding and 'content-length' in headers:
            raise MediaProbeError('invalid_http')
        self.chunked = encoding == 'chunked'
        self.left = _content_length(headers.get('content-length'))

    def _more(self):
        if self.chunked:
            if self.chunk_end:
                if self.wire.exact(2) != b'\r\n':
                    raise MediaProbeError('invalid_http')
                self.chunk_end = False
            if not self.chunk_left:
                line = self.wire.until(b'\r\n', 128, 'invalid_http')
                if not re.fullmatch(rb'[0-9a-fA-F]{1,8}', line):
                    raise MediaProbeError('invalid_http')
                self.chunk_left = int(line, 16)
                if not self.chunk_left:
                    raise MediaProbeError('incomplete_response')
                if self.count + self.chunk_left > MAX_BODY:
                    raise MediaProbeError('body_too_large')
            value = self.wire.some(min(4096, self.chunk_left))
            self.chunk_left -= len(value)
            self.chunk_end = not self.chunk_left
        else:
            if self.left == 0:
                raise MediaProbeError('incomplete_response')
            value = self.wire.some(min(4096, self.left) if self.left is not None else 4096)
            if self.left is not None:
                self.left -= len(value)
        self.count += len(value)
        if self.count > MAX_BODY:
            raise MediaProbeError('body_too_large')
        self.buffer.extend(value)

    def until(self, delimiter, limit):
        while True:
            index = self.buffer.find(delimiter)
            if index >= 0:
                end = index + len(delimiter)
                if end > limit:
                    raise MediaProbeError('headers_too_large')
                result = bytes(self.buffer[:index])
                del self.buffer[:end]
                return result
            if len(self.buffer) >= limit:
                raise MediaProbeError('headers_too_large')
            self._more()

    def exact(self, count):
        if count > MAX_BODY:
            raise MediaProbeError('body_too_large')
        while len(self.buffer) < count:
            self._more()
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('nonfinite')


def _strict_json(value):
    try:
        result = json.loads(value.decode('utf-8'), object_pairs_hook=_no_duplicates,
                            parse_constant=_reject_constant)
        if not isinstance(result, dict):
            raise ValueError('object_required')
        pending = [(result, 1)]
        while pending:
            item, depth = pending.pop()
            if depth > MAX_JSON_DEPTH:
                raise ValueError('depth')
            if isinstance(item, (dict, list)):
                pending.extend((child, depth + 1) for child in
                               (item.values() if isinstance(item, dict) else item))
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError('nonfinite')
        return result
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise MediaProbeError('invalid_json') from None


def _json_object_end(value):
    """找到第一个完整对象，不能把截断或深层响应当成部分有效数据。"""
    depth = 0
    quoted = escaped = started = False
    for index, char in enumerate(value):
        if not started:
            if char in b' \t\r\n':
                continue
            if char != ord('{'):
                raise MediaProbeError('invalid_json')
            started = True
        if quoted:
            if escaped:
                escaped = False
            elif char == ord('\\'):
                escaped = True
            elif char == ord('"'):
                quoted = False
        elif char == ord('"'):
            quoted = True
        elif char in (ord('{'), ord('[')):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise MediaProbeError('invalid_json')
        elif char in (ord('}'), ord(']')):
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _first_frame(wire, headers, header_budget=MAX_HEADERS):
    body = _BodyReader(wire, headers)
    content_type = headers.get('content-type', '')
    if content_type.split(';', 1)[0].strip().lower() == 'application/json':
        length = _content_length(headers.get('content-length'))
        if length is None:
            # 非流式原厂错误也可能使用 chunked；只读到完整 JSON 对象。
            while True:
                body._more()
                end = _json_object_end(body.buffer)
                if end is not None:
                    return _strict_json(bytes(body.buffer[:end]))
        return _strict_json(body.exact(length))
    match = re.fullmatch(r'multipart/x-mixed-replace\s*;\s*boundary\s*=\s*'
                         r'(?:"(--myboundary|myboundary)"|(--myboundary|myboundary))',
                         content_type, re.IGNORECASE)
    if match is None:
        raise MediaProbeError('unsupported_content_type')
    boundary = (match.group(1) or match.group(2)).encode('ascii')
    opening = body.until(b'\r\n', 128)
    # 原厂把 -- 写进 boundary 参数，同时在正文直接使用 --myboundary。
    if opening not in (boundary, b'--' + boundary) or not opening.startswith(b'--'):
        raise MediaProbeError('invalid_multipart')
    _, part = _headers(body.until(b'\r\n\r\n', header_budget - len(opening) - 2))
    if part.get('content-type', '').strip().lower() != 'application/json':
        raise MediaProbeError('invalid_multipart')
    length = _content_length(part.get('content-length'))
    if length is None or not length:
        raise MediaProbeError('invalid_multipart')
    return _strict_json(body.exact(length))


def _query_parameters(query_id, parameters):
    """共享相册参数只由宿主同账户目录发现传入，不接受通用参数转发。"""
    if query_id != 'shared_album':
        if parameters is not None:
            raise MediaProbeError('invalid_parameters')
        return ''
    if type(parameters) is not dict or set(parameters) != {'album_id', 'type'}:
        raise MediaProbeError('invalid_parameters')
    album_id, album_type = parameters['album_id'], parameters['type']
    if (type(album_id) is not str or not re.fullmatch(r'share_[0-9]{1,10}', album_id)
            or type(album_type) is not str or not 1 <= len(album_type) <= 31
            or not all(32 <= ord(char) <= 126 for char in album_type)):
        raise MediaProbeError('invalid_parameters')
    return '&album_id=' + quote(album_id, safe='') + '&type=' + quote(album_type, safe='')


def query_first_media_frame(access_token, query_id, *, root='/', parameters=None):
    """固定连接和叶证书 pin；返回 (HTTP 状态, 首个 JSON 对象)。

    access_token 只存在内存中。测试可将本函数整体替换为相同签名的 transport，
    或使用自有 socket fixture；不得向调用者开放 host、端口、动作或路径参数。
    parameters 仅供宿主传入已发现的共享相册 ID／类型，不得接受网页输入。
    """
    if type(query_id) is not str or query_id not in MEDIA_QUERIES:
        raise MediaProbeError('unsupported_query')
    route, action = MEDIA_QUERIES[query_id]
    query_parameters = _query_parameters(query_id, parameters)
    deadline = time.monotonic() + TOTAL_TIMEOUT
    if ssl is None:
        raise MediaProbeError('tls_unavailable')
    expected_certificate = _certificate(root)
    connection = raw = None
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(_remaining(deadline))
        raw.connect((HOST, PORT))
        raw.settimeout(_remaining(deadline))
        connection = context.wrap_socket(raw, server_hostname=None)
        if connection.getpeercert(binary_form=True) != expected_certificate:
            raise MediaProbeError('certificate_mismatch')
        if not isinstance(access_token, str) or not 1 <= len(access_token) <= 4096:
            raise MediaProbeError('token_unavailable')
        # 证书匹配之前不构造或发送带签名的请求；quote 禁止头注入。
        request_path = route + '?action=' + action + query_parameters + '&access_token='
        # 刮削状态 handler 要求 JSON 对象；空对象查询默认状态，不接受库 ID。
        method = 'POST' if query_id == 'scraper' else 'GET'
        payload = 'Content-Type: application/json\r\nContent-Length: 2\r\n' if query_id == 'scraper' else ''
        request = (method + ' ' + request_path + quote(access_token, safe='') + ' HTTP/1.1\r\n'
                   'Host: 127.0.0.1:20443\r\nAccept: multipart/x-mixed-replace, application/json\r\n'
                   'Accept-Encoding: identity\r\nConnection: close\r\n' + payload
                   + '\r\n' + ('{}' if query_id == 'scraper' else '')).encode('ascii')
        connection.settimeout(_remaining(deadline))
        connection.sendall(request)
        wire = _WireReader(connection, deadline)
        raw_headers = wire.until(b'\r\n\r\n', MAX_HEADERS)
        status_code, headers = _headers(raw_headers, response=True)
        if 300 <= status_code <= 399:
            raise MediaProbeError('redirect_refused', status_code)
        if status_code != 200:
            # 错误正文可能回显凭据；不读取、不记录、不重试。
            raise MediaProbeError('http_error', status_code)
        return status_code, _first_frame(wire, headers, MAX_HEADERS - len(raw_headers) - 4)
    except MediaProbeError:
        raise
    except (socket.timeout, TimeoutError):
        raise MediaProbeError('timeout') from None
    except Exception:
        raise MediaProbeError('transport_failed') from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass


def query_first_face_frame(access_token, *, root='/'):
    """保留原有单项探测传输契约，固定转发到 face 状态查询。"""
    return query_first_media_frame(access_token, 'face', root=root)


def _safe_credential(value):
    # 候选与计数是固定读取器产物，凭据本身及源标识绝不对外投影。
    if not isinstance(value, dict):
        return {'available': False, 'status': 'unavailable', 'reason': 'credential_read_failed',
                'user_id': None, 'records_checked': None, 'candidate_count': None,
                'selection': None}
    states = {'ready': None, 'db_missing': 'credential_database_missing',
              'sqlite_unavailable': 'credential_sqlite_unavailable',
              'permission_denied': 'credential_permission_denied',
              'invalid_database': 'credential_database_invalid',
              'identity_unconfirmed': 'credential_owner_unconfirmed',
              'no_session': 'credential_session_missing',
              'ambiguous_session': 'credential_session_ambiguous',
              'busy': 'credential_database_busy', 'data_unavailable': 'credential_data_unavailable'}
    status_code = value.get('status')
    status_code = status_code if isinstance(status_code, str) and status_code in states else 'unavailable'
    result = {'available': value.get('available') is True and status_code == 'ready',
              'status': status_code,
              'reason': states.get(status_code, 'credential_read_failed'),
              'user_id': OWNER_ID if value.get('user_id') == OWNER_ID else None,
              'records_checked': _integer(value.get('records_checked')),
              'candidate_count': _integer(value.get('candidate_count')),
              'selection': ('newest_in_vendor_window' if value.get('selection')
                            == 'newest_in_vendor_window' else None)}
    return result


def collect_media_probe(root='/', *, session_reader=None, transport=None):
    """一次显式探测；注入 reader(root=...) 与 transport(token, root=...)。

    reader 返回 (私有 session 或 None, 安全状态)。session 须属于固定 100000，
    且提供无参数 derive_access_token()。任何异常只映射固定码，不记录异常文本。
    """
    result = {'schema_version': 1, 'available': False, 'status': 'unavailable',
              'reason': None, 'sampled_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
              'scope': {'user_id': OWNER_ID}, 'credential': _safe_credential(None),
              'credential_read_only': True, 'native_query_may_update_state': True,
              'native': {'action': ACTION, 'http_status': None, 'code': None,
                         'first_frame_only': True},
              'data': None, 'notes': list(_NOTES)}
    try:
        if session_reader is None:
            try:
                from vendor_media_auth import read_session
            except ImportError:
                from .vendor_media_auth import read_session
            session_reader = read_session
        session, credential = session_reader(root=root)
        result['credential'] = _safe_credential(credential)
    except Exception:
        result['reason'] = 'credential_read_failed'
        return result
    if (session is None or not result['credential']['available']
            or result['credential']['user_id'] != OWNER_ID
            or type(getattr(session, 'user_id', None)) is not int or session.user_id != OWNER_ID):
        result['reason'] = 'credential_unavailable'
        return result
    try:
        token = session.derive_access_token()
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise MediaProbeError('token_unavailable')
        status_code, frame = (transport or query_first_face_frame)(token, root=root)
        if _http_status(status_code) is None or not isinstance(frame, dict):
            raise MediaProbeError('invalid_transport_result')
        result['native']['http_status'] = status_code
        if status_code != 200:
            raise MediaProbeError('redirect_refused' if 300 <= status_code <= 399
                                  else 'http_error', status_code)
        code = _native_code(frame.get('code'))
        result['native']['code'] = code
        if code is None:
            result['status'] = 'invalid_response'
            result['reason'] = 'native_code_unavailable'
        elif code != 0:
            result['status'] = 'native_error'
            result['reason'] = 'native_request_rejected'
        else:
            data = project_face_frame(frame)
            result.update(available=True, status='ok', data=data)
            if any(data[key] is None for key in _COUNTERS):
                result['status'] = 'partial'
                result['reason'] = 'counts_unavailable'
        return result
    except MediaProbeError as error:
        result['reason'] = error.reason
        if error.http_status is not None:
            result['native']['http_status'] = error.http_status
    except Exception:
        result['reason'] = 'transport_failed'
    return result

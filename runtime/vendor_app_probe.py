"""Explicit authenticated factory app catalog refresh, never passive polling.

Build 260907 opttype=0 serializes the current appcenter list without its
opttype=1 synchronization branch. Common native authentication middleware
can still start owner resources; the owner must explicitly request refresh.
"""

from datetime import datetime, timezone
import socket
import time
from urllib.parse import quote

from vendor_app_diagnostics import (APPLICATION_ERRORS, PROBE_SOURCE, _CacheError,
                                    project_application_list)
from vendor_media_auth import read_session
import vendor_media_probe as wire


ROUTE = '/rest/1.1/applycenter'
ACTION = 'get_apply_list'
OPTTYPE = 0
_NOTES = ('explicit_owner_refresh_only', 'existing_owner_credential_read_only',
          'native_auth_may_start_owner_resources', 'opttype_zero_skips_app_sync',
          'catalog_entries_are_not_all_installed_or_running',
          'unknown_status_enum_is_null', 'no_install_progress_or_installer_calls')


def query_application_frame(access_token, *, root='/'):
    """One fixed native GET over pinned TLS, reusing the bounded media parser.

    No host, route, action, opttype, user ID or other native argument can be
    supplied by a web request. Signing material never leaves the host.
    """
    deadline = time.monotonic() + wire.TOTAL_TIMEOUT
    if wire.ssl is None:
        raise wire.MediaProbeError('tls_unavailable')
    expected_certificate = wire._certificate(root)
    connection = raw = None
    try:
        context = wire.ssl.SSLContext(wire.ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = wire.ssl.CERT_NONE
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(wire._remaining(deadline))
        raw.connect((wire.HOST, wire.PORT))
        raw.settimeout(wire._remaining(deadline))
        connection = context.wrap_socket(raw, server_hostname=None)
        if connection.getpeercert(binary_form=True) != expected_certificate:
            raise wire.MediaProbeError('certificate_mismatch')
        if type(access_token) is not str or not 1 <= len(access_token) <= 4096:
            raise wire.MediaProbeError('token_unavailable')
        # Only build/send the credential-bearing URI after the exact leaf pin.
        path = ROUTE + '?action=' + ACTION + '&opttype=0&access_token=' + quote(access_token, safe='')
        request = ('GET ' + path + ' HTTP/1.1\r\nHost: 127.0.0.1:20443\r\n'
                   'Accept: application/json\r\nAccept-Encoding: identity\r\n'
                   'Connection: close\r\n\r\n').encode('ascii')
        connection.settimeout(wire._remaining(deadline))
        connection.sendall(request)
        reader = wire._WireReader(connection, deadline)
        raw_headers = reader.until(b'\r\n\r\n', wire.MAX_HEADERS)
        status, headers = wire._headers(raw_headers, response=True)
        if 300 <= status <= 399:
            raise wire.MediaProbeError('redirect_refused', status)
        if status != 200:
            # Do not read a native error body: it could echo the signed URI.
            raise wire.MediaProbeError('http_error', status)
        return status, wire._first_frame(reader, headers, wire.MAX_HEADERS - len(raw_headers) - 4)
    except wire.MediaProbeError:
        raise
    except (socket.timeout, TimeoutError):
        raise wire.MediaProbeError('timeout') from None
    except Exception:
        raise wire.MediaProbeError('transport_failed') from None
    finally:
        for stream in (connection, raw):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def collect_application_probe(root='/', *, session_reader=None, transport=None):
    """Return one safe attempt; injection hooks are private test dependencies.

    Uses only the already-existing fixed owner session. It never logs in,
    retries credentials, synchronizes applications, or invokes installation.
    """
    sampled_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    result = {'schema_version': 1, 'sampled_at': sampled_at, 'available': False,
              'status': 'unavailable', 'source': PROBE_SOURCE, 'reason': None,
              'error_code': None, 'attempted': False, 'elapsed_ms': None,
              'credential': wire._safe_credential(None),
              'credential_read_only': True, 'native_query_may_update_state': True,
              'native': {'route': ROUTE, 'action': ACTION, 'opttype': OPTTYPE,
                         'http_status': None, 'code': None, 'code_present': False},
              'data': None, 'notes': list(_NOTES)}
    try:
        session, credential = (session_reader or read_session)(root=root)
        result['credential'] = wire._safe_credential(credential)
    except Exception:
        result.update(error_code='credential_read_failed', reason='原厂既有管理员会话暂不可只读确认')
        return result
    if (session is None or not result['credential']['available']
            or result['credential']['user_id'] != wire.OWNER_ID
            or type(getattr(session, 'user_id', None)) is not int or session.user_id != wire.OWNER_ID):
        result.update(error_code='credential_unavailable', reason='没有可确认的原厂既有管理员会话；未发起应用查询')
        return result
    started = time.monotonic()
    try:
        token = session.derive_access_token()
        if type(token) is not str or not 1 <= len(token) <= 4096:
            raise wire.MediaProbeError('token_unavailable')
        result['attempted'] = True
        status, frame = (transport or query_application_frame)(token, root=root)
        if (type(status) is not int or not 100 <= status <= 599
                or not isinstance(frame, dict)):
            raise wire.MediaProbeError('invalid_transport_result')
        result['native']['http_status'] = status
        if status != 200:
            raise wire.MediaProbeError('redirect_refused' if 300 <= status <= 399 else 'http_error', status)
        # The audited list serializer has no code field. Validate it when the
        # common REST layer does supply one; absent is not invented as zero.
        result['native']['code_present'] = 'code' in frame
        if 'code' in frame:
            code = wire._native_code(frame['code'])
            result['native']['code'] = code
            if code is None:
                result.update(status='invalid_response', error_code='native_code_unavailable')
                return result
            if code != 0:
                result.update(status='native_error', error_code='native_request_rejected')
                return result
        data = project_application_list(frame, sampled_at)
        result.update(available=True, status='ok', data=data,
                      reason='已显式读取原厂应用目录；运行状态与未知安装状态保持未知')
    except _CacheError as error:
        result.update(status='invalid_response', error_code=str(error))
    except wire.MediaProbeError as error:
        result.update(error_code=error.reason)
        result['native']['http_status'] = error.http_status
    except Exception:
        result.update(error_code='transport_failed')
    finally:
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        if result['error_code'] is not None:
            if result['error_code'] not in APPLICATION_ERRORS:
                result['error_code'] = 'transport_failed'
            result['reason'] = '原厂应用目录刷新未完成；未重试其他会话或调用同步、安装接口'
    return result

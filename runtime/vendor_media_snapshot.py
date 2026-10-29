"""Passive projections of the last explicit media probe, never a native query.

Only already-projected numeric fields are accepted, independently validated and
written into Toolbox's own state directory. Reading this cache does not import
the vendor session reader, open its database, connect to media services or retry
a failed query. Every item carries its own capture time after partial refreshes.
"""

from datetime import datetime, timezone
import copy
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading


_NAME = 'media-snapshot.json'
_SOURCE = 'toolbox.media_snapshot'
_MAX_BYTES = 65536
_MAX_INTEGER = 2 ** 53 - 1
_IDS = ('face', 'ocr', 'thumbnail', 'similar', 'ai', 'scraper', 'shared_album')
_STATUSES = frozenset(('ok', 'partial', 'unavailable', 'native_error',
                       'invalid_response', 'not_applicable', 'not_collected'))
_REASONS = frozenset((
    'tls_unavailable', 'certificate_unavailable', 'certificate_invalid', 'certificate_mismatch',
    'timeout', 'transport_failed', 'headers_too_large', 'body_too_large',
    'invalid_http', 'unsupported_encoding', 'unsupported_content_type',
    'invalid_multipart', 'incomplete_response', 'invalid_json', 'redirect_refused',
    'http_error', 'invalid_transport_result', 'credential_unavailable',
    'credential_read_failed', 'token_unavailable', 'unsupported_query', 'invalid_parameters',
    'native_code_unavailable', 'native_request_rejected', 'fields_unavailable',
    'counts_unavailable', 'native_schema_unconfirmed', 'album_catalog_invalid',
    'album_context_invalid', 'album_context_ambiguous', 'no_existing_owner_album',
    'invalid_result', 'cache_item_invalid', 'not_collected',
))
_ROW_FIELDS = frozenset(('id', 'captured_at', 'available', 'status', 'reason',
                         'query_success', 'data', 'last_failure'))
_FAILURE_FIELDS = frozenset(('captured_at', 'status', 'reason'))
_STAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}\+00:00\Z')
_LOCK = threading.RLock()


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _valid_stamp(value):
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def _integer(value, *, signed=False):
    return (value if type(value) is int
            and (-_MAX_INTEGER if signed else 0) <= value <= _MAX_INTEGER else None)


def _ratio(done, total):
    return done / total if done is not None and total is not None and 0 <= done <= total and total else None


def _reason(value, fallback='invalid_result'):
    return value if isinstance(value, str) and value in _REASONS else fallback


def _project(query, values):
    """Return a detached exact numeric schema and the required counting fields."""
    if not isinstance(values, dict):
        return None, []
    fields = {
        'face': ('total', 'totalpic', 'totalvid', 'processed', 'processedpic', 'processedvid'),
        'ocr': ('totalpic', 'processedpic', 'remainpic'),
        'thumbnail': ('b', 'st', 'pt', 'sc', 'cc'),
        'similar': ('totalpic', 'processedpic', 'remainpic'),
        'scraper': ('scaned', 'total', 'timestamp', 'num'),
        'shared_album': ('scan_num', 'failed_num', 'await_num'),
    }
    data = {field: _integer(values.get(field)) for field in fields.get(query, ())}
    required = list(data)
    if query == 'face':
        percent = values.get('sync_percent')
        remaining = values.get('estimated_remaining')
        data.update(picture_completion_ratio=_ratio(data['processedpic'], data['totalpic']),
                    video_completion_ratio=_ratio(data['processedvid'], data['totalvid']),
                    sync_percent=percent if type(percent) in (int, float)
                    and math.isfinite(percent) and 0 <= percent <= 100 else None,
                    upgrade_status=values.get('upgrade_status') if type(values.get('upgrade_status')) is int
                    and values['upgrade_status'] in (0, 1) else None,
                    range_status=0 if type(values.get('range_status')) is int
                    and values['range_status'] == 0 else None,
                    estimated_remaining={'value': _integer(remaining.get('value'))
                                         if isinstance(remaining, dict) else None, 'unit': None})
    elif query == 'ocr':
        data['picture_completion_ratio'] = _ratio(data['processedpic'], data['totalpic'])
    elif query == 'similar':
        data.update(pending_hashes=data['remainpic'], completion_ratio=None)
    elif query == 'ai':
        count = _integer(values.get('recognition_range_count'))
        data = {'configuration_status': _integer(values.get('configuration_status'), signed=True),
                'recognition_range_count': count if count is not None and count <= 256 else None}
        required = list(data)
    elif query == 'scraper':
        data['native_status'] = _integer(values.get('native_status'), signed=True)
        required = ['scaned', 'total', 'timestamp', 'native_status']
    elif query == 'shared_album':
        status = values.get('native_status')
        data['native_status'] = status if type(status) is int and status in (0, 1, 2) else None
        required.append('native_status')
    return data, [data[field] for field in required]


def _empty_item(query):
    return {'id': query, 'captured_at': None, 'available': False,
            'status': 'not_collected', 'reason': 'not_collected',
            'query_success': False, 'data': None, 'last_failure': None}


def _failed_item(query, captured_at, reason, status='unavailable'):
    failure = {'captured_at': captured_at, 'status': status, 'reason': reason}
    return {'id': query, 'captured_at': captured_at, 'available': False,
            'status': status, 'reason': reason, 'query_success': False,
            'data': None, 'last_failure': failure}


def _record_item(query, result, captured_at, previous, *, face=False):
    if (not isinstance(result, dict) or not isinstance(result.get('status'), str)
            or result['status'] not in _STATUSES):
        return _failed_item(query, captured_at, 'invalid_result')
    status = result['status']
    if (query == 'shared_album' and status == 'not_applicable'
            and result.get('reason') == 'no_existing_owner_album'):
        return {'id': query, 'captured_at': captured_at, 'available': False,
                'status': 'not_applicable', 'reason': 'no_existing_owner_album',
                'query_success': False, 'data': None, 'last_failure': previous.get('last_failure')}
    successful = (status in ('ok', 'partial') and result.get('available') is True
                  and (face or result.get('query_success') is True))
    if not successful:
        status = status if status in ('unavailable', 'native_error', 'invalid_response') else 'invalid_response'
        return _failed_item(query, captured_at, _reason(result.get('reason')), status)
    data, required = _project(query, result.get('data'))
    if data is None or not required:
        return _failed_item(query, captured_at, 'invalid_result', 'invalid_response')
    complete = status == 'ok' and all(value is not None for value in required)
    reason = None if complete else _reason(result.get('reason'), 'fields_unavailable')
    return {'id': query, 'captured_at': captured_at,
            'available': any(value is not None for value in required),
            'status': 'ok' if complete else 'partial',
            'reason': reason,
            'query_success': True, 'data': data,
            'last_failure': copy.deepcopy(previous.get('last_failure')) if complete else {
                'captured_at': captured_at, 'status': 'partial', 'reason': reason}}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate cache field')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('invalid cache number')


def _valid_failure(value):
    return (value is None or isinstance(value, dict) and set(value) == _FAILURE_FIELDS
            and _valid_stamp(value['captured_at']) and isinstance(value['status'], str)
            and value['status'] in ('partial', 'unavailable', 'native_error', 'invalid_response')
            and isinstance(value['reason'], str) and value['reason'] in _REASONS)


def _cache_item(query, row, captured_at):
    """One bad cached item cannot hide valid observations from other items."""
    if (not isinstance(row, dict) or set(row) != _ROW_FIELDS or row.get('id') != query
            or type(row.get('available')) is not bool or type(row.get('query_success')) is not bool
            or not isinstance(row.get('status'), str) or row['status'] not in _STATUSES
            or row.get('reason') is not None and (not isinstance(row['reason'], str)
                                                 or row['reason'] not in _REASONS)
            or not _valid_failure(row.get('last_failure'))):
        return _failed_item(query, captured_at, 'cache_item_invalid', 'invalid_response')
    stamp = row['captured_at']
    if row['status'] == 'not_collected':
        return (_empty_item(query) if row == _empty_item(query)
                else _failed_item(query, captured_at, 'cache_item_invalid', 'invalid_response'))
    if not _valid_stamp(stamp):
        return _failed_item(query, captured_at, 'cache_item_invalid', 'invalid_response')
    normalized = _record_item(query, row, stamp, row)
    # Persisted fields must already be canonical. Unknown extra data fields or
    # changed ratios are not silently trusted as successfully recorded values.
    return (normalized if normalized == row
            else _failed_item(query, stamp, 'cache_item_invalid', 'invalid_response'))


def _read(state_dir):
    path = Path(state_dir) / _NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None, 'cache_invalid' if path.is_symlink() else 'not_collected'
    except OSError:
        return None, 'cache_invalid' if path.is_symlink() else 'cache_unavailable'
    try:
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES
                    or os.name != 'nt' and stat.S_IMODE(info.st_mode) != 0o600):
                return None, 'cache_invalid'
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return None, 'cache_invalid'
        state = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_reject_constant)
        if (not isinstance(state, dict) or set(state) != {'version', 'captured_at', 'kind', 'results'}
                or type(state['version']) is not int or state['version'] != 1
                or state['kind'] not in ('face', 'batch') or not _valid_stamp(state['captured_at'])
                or not isinstance(state['results'], list) or len(state['results']) != len(_IDS)):
            return None, 'cache_invalid'
        rows = []
        for query, row in zip(_IDS, state['results']):
            rows.append(_cache_item(query, row, state['captured_at']))
        return {**state, 'results': rows}, None
    except (OSError, ValueError, UnicodeError, RecursionError, TypeError):
        return None, 'cache_invalid'


def _write(state_dir, state):
    path = Path(state_dir) / _NAME
    serialized = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(serialized) > _MAX_BYTES:
        raise OSError('media snapshot exceeds bound')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise OSError('unsafe media snapshot target')
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.media-snapshot-', dir=str(path.parent))
        with os.fdopen(descriptor, 'wb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != 'nt':
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def record_media_snapshot(state_dir, result, kind='batch'):
    """Record one explicit query's safe numeric projection; return a safe receipt.

    An item failure replaces that item's old successful data. A face-only refresh
    preserves other items with their original timestamps, and cannot claim a new
    complete batch. Cache write failure does not turn an already executed native
    query into an apparent unexecuted operation that clients might repeat.
    """
    if kind not in ('face', 'batch'):
        raise ValueError('不支持的媒体快照类别')
    with _LOCK:
        previous, _ = _read(state_dir)
        before = previous['results'] if previous is not None else [_empty_item(query) for query in _IDS]
        captured_at = _stamp()
        rows = list(before)
        if kind == 'face':
            rows[0] = _record_item('face', result, captured_at, before[0], face=True)
        else:
            incoming = result.get('results') if isinstance(result, dict) else None
            incoming = incoming if isinstance(incoming, list) and len(incoming) <= len(_IDS) else []
            for index, query in enumerate(_IDS):
                candidates = [item for item in incoming if isinstance(item, dict) and item.get('id') == query]
                row = candidates[0] if len(candidates) == 1 else None
                rows[index] = _record_item(query, row, captured_at, before[index])
        state = {'version': 1, 'captured_at': captured_at, 'kind': kind, 'results': rows}
        try:
            _write(state_dir, state)
        except (OSError, ValueError, UnicodeError, TypeError):
            return {'stored': False, 'status': 'cache_unavailable', 'reason': '媒体查询已完成，但快照未能保存。'}
        return {'stored': True, 'status': 'cached', 'reason': None}


def collect_media_snapshot(state_dir, *, root='/'):
    """Read only Toolbox's fixed cache; ``root`` is provider/fixture compatibility.

    Complete means all applicable items in the latest batch have complete
    projected fields. It never means all NAS users, albums or media tasks were
    enumerated, nor that the observations are current after a restart.
    """
    with _LOCK:
        state, error = _read(state_dir)
    if state is None:
        reasons = {'not_collected': '尚未主动采集媒体状态。',
                   'cache_invalid': '媒体快照格式或文件身份不可确认。',
                   'cache_unavailable': '媒体快照暂不可读取。'}
        return {'available': False, 'status': error, 'source': _SOURCE,
                'reason': reasons[error], 'data': None}
    rows = state['results']
    applicable = [row for row in rows if row['status'] != 'not_applicable']
    complete = (state['kind'] == 'batch' and bool(applicable)
                and all(row['captured_at'] == state['captured_at'] for row in rows)
                and all(row['status'] == 'ok' and row['query_success'] and row['available']
                        for row in applicable))
    available = any(row['available'] for row in rows)
    coverage = {'total': len(rows), 'applicable': len(applicable),
                'not_applicable': sum(row['status'] == 'not_applicable' for row in rows),
                'succeeded': sum(row['query_success'] for row in rows),
                'data_available': sum(row['available'] for row in rows),
                'data_complete': sum(row['status'] == 'ok' for row in rows)}
    data = {'captured_at': state['captured_at'], 'live': False, 'explicit_refresh': True,
            'complete': complete, 'coverage': coverage, 'results': copy.deepcopy(rows)}
    return {'available': available,
            'status': 'cached' if complete else 'partial' if available else 'cached_empty',
            'source': _SOURCE, 'reason': '上次显式采集的媒体状态，不代表实时进度。', 'data': data}

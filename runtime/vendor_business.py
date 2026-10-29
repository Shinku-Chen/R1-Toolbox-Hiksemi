"""Private business diagnostics using passive caches and fixed verified RPC.

This does not proxy factory REST. Media task getters remain blocked until an
independent safe source is verified; their kernel runtime observations are real
observations, never substituted for task counts or progress.
"""
import datetime
import json
import math
import re

from vendor_app_diagnostics import collect as collect_applications
from vendor_download_diagnostics import collect as collect_downloads
from vendor_runtime import collect_runtime

_SERVICES = {'applications': frozenset(('appcenter',)),
             'downloads': frozenset(('aria2c-nox', 'qbittorrent-nox')),
             'media': frozenset(('intelligent', 'image_ocr', 'image_convert', 'image_similar'))}
_PORTS = {'applications': frozenset((8888,)), 'downloads': frozenset((6800, 5050)),
          'media': frozenset((7786, 7778, 7769, 7799, 9660))}
# A second projection fence prevents future providers from returning credentials,
# user paths, torrent identifiers or raw upstream JSON by accident.
_DATA_FIELDS = {
    'applications': frozenset(('apps', 'name', 'version', 'installation_status_code', 'running',
                              'cached_entries', 'live', 'complete', 'cache_modified_at')),
    'downloads': frozenset(('version', 'complete', 'engine', 'tasks', 'counts', 'limits',
                           'active', 'waiting', 'stopped', 'status', 'total_bytes', 'completed_bytes',
                           'progress_percent', 'download_bytes_per_second', 'upload_bytes_per_second')),
}


def _text(value, limit=256):
    return value if isinstance(value, str) and len(value) <= limit and not any(
        ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value) else None


def _data(value, fields, depth=0):
    if depth > 6:
        raise ValueError('diagnostic nesting limit')
    if isinstance(value, dict):
        return {key: _data(item, fields, depth + 1) for key, item in value.items() if key in fields}
    if isinstance(value, list):
        if len(value) > 384:
            raise ValueError('diagnostic item limit')
        return [_data(item, fields, depth + 1) for item in value]
    if value is None or isinstance(value, bool):
        return value
    if type(value) in (int, float):
        if not math.isfinite(value) or not -1 <= value <= 2**64:
            raise ValueError('diagnostic number limit')
        return value
    text = _text(value)
    if text is None:
        raise ValueError('diagnostic text limit')
    return text


def _result(provider, root, category):
    failure = {'available': False, 'status': 'diagnostic_error',
               'source': None, 'reason': '业务诊断暂不可确认', 'data': None}
    try:
        value = provider(root=root)
        if (not isinstance(value, dict) or type(value.get('available')) is not bool
                or not isinstance(value.get('status'), str)
                or not re.fullmatch(r'[a-z_]{1,48}', value['status'])):
            return failure
        result = {'available': value['available'], 'status': value['status'],
                  'source': _text(value.get('source'), 128),
                  'reason': _text(value.get('reason')), 'data': None}
        if result['available']:
            if not isinstance(value.get('data'), dict):
                return failure
            result['data'] = _data(value['data'], _DATA_FIELDS[category])
        # Auth/socket/engine errors use fixed provider error codes, not upstream
        # bodies, HTTP headers or exception text.
        code = value.get('error_code')
        if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,48}', code):
            result['error_code'] = code
        if len(json.dumps(result, ensure_ascii=True, allow_nan=False)) > 256 * 1024:
            return failure
        return result
    except Exception:
        return failure


def collect_business_diagnostics(root='/', *, applications=None, downloads=None, runtime=None):
    """One fixed snapshot. Injection hooks are internal fixtures, never HTTP input."""
    state = (runtime or collect_runtime)(root=root)
    categories = {
        'applications': _result(applications or collect_applications, root, 'applications'),
        'downloads': _result(downloads or collect_downloads, root, 'downloads'),
        'media': {'available': False, 'status': 'unverified_read_only',
                  'source': 'histor.media_tasks', 'data': None,
                  'reason': '尚未确认安全的一次性媒体任务来源；已跳过用户资源启动、相册查询、事件写入和状态刷新代理',
                  'blocked_queries': ['get_thumb_convert_process', 'get_ocr_status', 'lib_scan_status']},
    }
    for name, result in categories.items():
        # These are process/socket observations, not task data. Incomplete proc
        # coverage must not be interpreted as proof that a service is stopped.
        result['runtime'] = {
            'available': state.get('available') is True, 'complete': state.get('complete') is True,
            'processes': [{key: row.get(key) for key in ('pid', 'name', 'exe_name', 'start_ticks', 'identity_confirmed')}
                          for row in state.get('processes', []) if row.get('name') in _SERVICES[name]],
            'listeners': [{key: row.get(key) for key in ('address', 'port', 'socket_inode', 'owner_pids')}
                          for row in state.get('listeners', []) if row.get('port') in _PORTS[name]],
        }
    return {'schema_version': 1, 'read_only': True,
            'sampled_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
            'readable_sources': sum(row['available'] for row in categories.values()),
            'categories': categories}

"""Explicit, bounded validation of the seven audited factory media getters.

Credentials and native album identities never leave this module. Factory
getters may read databases, report completion, or repair their own resources;
this is an on-demand developer operation, never a passive monitoring source.
"""

from datetime import datetime, timezone
import re
import time

from vendor_media_auth import read_session
from vendor_media_probe import (MediaProbeError, OWNER_ID, MEDIA_QUERIES,
                                _safe_credential, project_face_frame,
                                query_first_media_frame)


QUERY_IDS = ('face', 'ocr', 'thumbnail', 'similar', 'ai', 'scraper', 'shared_album')
MAX_ALBUMS = 256
MAX_NUMBER = 2 ** 53 - 1
_NOTES = {
    'face': ['scope_is_owner_configured_recognition_ranges',
             'sync_percent_is_not_overall_completion', 'remaining_estimate_unit_unverified'],
    'ocr': ['scope_is_owner', 'native_query_may_publish_completion_event'],
    'thumbnail': ['native_field_units_and_state_enums_unverified',
                  'st_and_pt_are_not_verified_task_counters'],
    'similar': ['factory_total_equals_remaining_in_audited_firmware',
                'use_remaining_queue_only_not_completion_ratio'],
    'ai': ['configuration_not_task_progress', 'paths_and_user_identity_omitted'],
    'scraper': ['downstream_state_enums_and_side_effects_unverified',
                'scaned_is_the_factory_field_spelling'],
    'shared_album': ['one_existing_owner_created_album',
                     'native_query_may_write_completion_state', 'album_identity_omitted',
                     'factory_count_errors_may_be_zero_no_idle_inference'],
}


def _integer(value, *, signed=False):
    lower = -MAX_NUMBER if signed else 0
    return value if type(value) is int and lower <= value <= MAX_NUMBER else None


def _ratio(processed, total):
    return processed / total if (processed is not None and total is not None
                                 and total > 0 and processed <= total) else None


def _project(query_id, frame):
    """Return (public fields, required values); no arbitrary native strings."""
    if query_id == 'face':
        data = project_face_frame(frame)
        return data, [data[key] for key in
                      ('total', 'totalpic', 'totalvid', 'processed', 'processedpic', 'processedvid')]
    if query_id == 'ocr':
        data = {key: _integer(frame.get(key)) for key in ('totalpic', 'processedpic', 'remainpic')}
        data['picture_completion_ratio'] = _ratio(data['processedpic'], data['totalpic'])
        return data, [data[key] for key in ('totalpic', 'processedpic', 'remainpic')]
    if query_id == 'thumbnail':
        data = {key: _integer(frame.get(key)) for key in ('b', 'st', 'pt', 'sc', 'cc')}
        return data, list(data.values())
    if query_id == 'similar':
        data = {key: _integer(frame.get(key)) for key in ('totalpic', 'processedpic', 'remainpic')}
        data['pending_hashes'] = data['remainpic']
        data['completion_ratio'] = None
        return data, [data[key] for key in ('totalpic', 'processedpic', 'remainpic')]
    if query_id == 'ai':
        ranges = frame.get('list')
        data = {'configuration_status': _integer(frame.get('status'), signed=True),
                'recognition_range_count': len(ranges) if isinstance(ranges, list)
                                           and len(ranges) <= MAX_ALBUMS
                                           and all(isinstance(row, dict) for row in ranges) else None}
        return data, list(data.values())
    if query_id == 'scraper':
        data = {key: _integer(frame.get(key)) for key in ('scaned', 'total', 'timestamp', 'num')}
        data['native_status'] = _integer(frame.get('status'), signed=True)
        return data, [data[key] for key in ('native_status', 'scaned', 'total', 'timestamp')]
    if query_id == 'shared_album':
        data = {key: _integer(frame.get(key)) for key in ('scan_num', 'failed_num', 'await_num')}
        data['native_status'] = (frame.get('status') if type(frame.get('status')) is int
                                 and frame['status'] in (0, 1, 2) else None)
        return data, list(data.values())
    raise ValueError('unsupported projection')


def _native(query_id):
    route, action = MEDIA_QUERIES[query_id]
    return {'route': route, 'action': action,
            'method': 'POST' if query_id == 'scraper' else 'GET',
            'http_status': None, 'code': None, 'code_present': False,
            'first_frame_only': True}


def _item(query_id):
    return {'id': query_id, 'attempted': False, 'query_success': False,
            'available': False, 'status': 'unavailable', 'reason': None,
            'elapsed_ms': None, 'native': _native(query_id), 'data': None,
            'notes': list(_NOTES[query_id])}


def _check_native(item, status, frame, *, code_required=True):
    if type(status) is not int or not 100 <= status <= 599 or not isinstance(frame, dict):
        item.update(status='invalid_response', reason='invalid_transport_result')
        return False
    item['native']['http_status'] = status
    if status != 200:
        item.update(status='native_error', reason='redirect_refused' if 300 <= status < 400 else 'http_error')
        return False
    item['native']['code_present'] = 'code' in frame
    code = _integer(frame.get('code'), signed=True)
    item['native']['code'] = code
    if (code_required or 'code' in frame) and code is None:
        item.update(status='invalid_response', reason='native_code_unavailable')
        return False
    if code is not None and code != 0:
        item.update(status='native_error', reason='native_request_rejected')
        return False
    return True


def _query(item, session, transport, root, *, parameters=None):
    """Sign each fixed request once; never retry another credential or user."""
    item['attempted'] = True
    started = time.monotonic()
    try:
        token = session.derive_access_token()
        if type(token) is not str or not 1 <= len(token) <= 4096:
            raise MediaProbeError('token_unavailable')
        kwargs = {'root': root}
        if parameters is not None:
            kwargs['parameters'] = parameters
        status, frame = transport(token, item['id'], **kwargs)
        return status, frame
    except MediaProbeError as error:
        item['reason'] = error.reason
        item['native']['http_status'] = error.http_status
    except Exception:
        item['reason'] = 'transport_failed'
    finally:
        item['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return None


def _album_parameters(row):
    if not isinstance(row, dict) or type(row.get('creater')) is not int or row['creater'] != 1:
        return None
    album_id, kind = row.get('album_id'), row.get('type')
    if (type(album_id) is not str or not re.fullmatch(r'share_[0-9]{1,10}', album_id)
            or type(kind) is not str or not 1 <= len(kind) <= 31
            or not all(32 <= ord(char) <= 126 for char in kind)):
        return None
    return {'album_id': album_id, 'type': kind}


def _discover_album(session, transport, root):
    """Only use an owner-created album returned by the authenticated getter."""
    discovery = {'id': 'shared_album_catalog', 'attempted': False, 'query_success': False,
                 'available': False, 'status': 'unavailable', 'reason': None,
                 'elapsed_ms': None, 'native': _native('shared_album_catalog'),
                 'owner_album_count': None, 'selected_existing_album': False}
    response = _query(discovery, session, transport, root)
    if response is None or not _check_native(discovery, *response):
        return None, discovery
    rows = response[1].get('share_album_list')
    if not isinstance(rows, list) or len(rows) > MAX_ALBUMS:
        discovery.update(status='invalid_response', reason='album_catalog_invalid')
        return None, discovery
    candidates = [_album_parameters(row) for row in rows]
    if any(isinstance(row, dict) and type(row.get('creater')) is int
           and row['creater'] == 1 and parameters is None
           for row, parameters in zip(rows, candidates)):
        discovery.update(status='invalid_response', reason='album_context_invalid')
        return None, discovery
    candidates = [row for row in candidates if row is not None]
    discovery.update(query_success=True, status='ok', owner_album_count=len(candidates))
    if not candidates:
        discovery.update(status='not_applicable', reason='no_existing_owner_album')
        return None, discovery
    selected = candidates[0]
    if any(row['album_id'] == selected['album_id'] and row != selected for row in candidates):
        discovery.update(status='invalid_response', reason='album_context_ambiguous')
        return None, discovery
    discovery.update(available=True, selected_existing_album=True)
    return selected, discovery


def collect_media_batch(root='/', *, session_reader=None, transport=None):
    """Probe seven fixed state getters; root/reader/transport are fixture-only.

    At most eight native requests (one owner-album discovery) take about 81 s
    when every request times out. The browser cannot supply actions or IDs.
    """
    result = {'schema_version': 1, 'sampled_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
              'available': False, 'status': 'unavailable', 'all_queries_succeeded': False,
              'scope': {'user_id': OWNER_ID}, 'credential': _safe_credential(None),
              'credential_read_only': True, 'native_query_may_update_state': True,
              'discovery': None, 'results': [_item(query_id) for query_id in QUERY_IDS]}
    session = None
    try:
        session, credential = (session_reader or read_session)(root=root)
        result['credential'] = _safe_credential(credential)
    except Exception:
        pass
    ready = (session is not None and result['credential']['available']
             and result['credential']['user_id'] == OWNER_ID
             and type(getattr(session, 'user_id', None)) is int and session.user_id == OWNER_ID)
    transport = transport or query_first_media_frame
    for item in result['results']:
        if not ready:
            item['reason'] = 'credential_unavailable'
            continue
        parameters = None
        if item['id'] == 'shared_album':
            parameters, result['discovery'] = _discover_album(session, transport, root)
            if parameters is None:
                discovery = result['discovery']
                item.update(status=discovery['status'], reason=discovery['reason'])
                continue
        response = _query(item, session, transport, root, parameters=parameters)
        if response is None or not _check_native(item, *response,
                code_required=item['id'] not in ('thumbnail', 'shared_album')):
            continue
        data, required = _project(item['id'], response[1])
        # Code-less getters use a complete integer schema instead of code=0.
        # Negative factory counters remain null, not a fabricated zero.
        if item['id'] in ('thumbnail', 'shared_album'):
            fields = ('b', 'st', 'pt', 'sc', 'cc') if item['id'] == 'thumbnail' else (
                'status', 'scan_num', 'failed_num', 'await_num')
            if any(_integer(response[1].get(key), signed=True) is None for key in fields):
                item.update(status='invalid_response', reason='native_schema_unconfirmed')
                continue
        item.update(query_success=True, available=any(value is not None for value in required),
                    status='ok' if all(value is not None for value in required) else 'partial',
                    reason=None if all(value is not None for value in required) else 'fields_unavailable', data=data)
    items = result['results']
    succeeded = sum(item['query_success'] for item in items)
    attempted = sum(item['attempted'] for item in items)
    result.update(available=any(item['available'] for item in items),
                  status='ok' if all(item['status'] == 'ok' for item in items) else 'partial' if succeeded else
                         'failed' if attempted else 'unavailable',
                  all_queries_succeeded=succeeded == len(items),
                  coverage={'total': len(items), 'attempted': attempted, 'succeeded': succeeded,
                            'data_available': sum(item['available'] for item in items),
                            'data_complete': sum(item['status'] == 'ok' for item in items),
                            'not_applicable': sum(item['status'] == 'not_applicable' for item in items)})
    return result

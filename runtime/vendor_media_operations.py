"""Authenticated, explicit media business operations for the MCP dispatcher.

Only fixed owner operations audited in Build 260907 are implemented. This
module does not register tools, bypass a Token policy, or grant an owner Web
session machine privileges. The dispatcher must supply an authorizer that
checks role, scope, exact tool and object for every call.
"""

from datetime import datetime, timezone
import hashlib
import json
import re
import threading
from types import MappingProxyType
import unicodedata

from vendor_business_transport import (BusinessTransportError,
                                       VendorBusinessClient, safe_text, _validate)


MAX_ITEMS = 256
MAX_NUMBER = 2 ** 53 - 1


class IdempotencyStore:
    """Bounded process-lifetime results; never evict an uncertain write.

    A principal is an internal nonsecret key/revision/credential-generation
    dictionary. It must not come from tool arguments. A full store rejects new
    writes rather than permitting an old ambiguous request to execute again.
    """

    def __init__(self, maximum=1024):
        self.maximum = maximum
        self._lock = threading.Lock()
        self._entries = {}

    def begin(self, principal, key, digest):
        if type(principal) is not dict:
            raise BusinessTransportError('idempotency_required')
        principal = tuple(principal.get(k) for k in ('key_id', 'grant_revision', 'credential_generation'))
        if (len(principal) != 3
                or any(not ((type(v) is str and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', v))
                            or type(v) is int and 0 <= v <= MAX_NUMBER) for v in principal)
                or type(key) is not str or re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', key) is None):
            raise BusinessTransportError('idempotency_required')
        identity = (principal, key)
        with self._lock:
            entry = self._entries.get(identity)
            if entry is not None:
                if entry['digest'] != digest:
                    raise BusinessTransportError('idempotency_conflict')
                if entry['result'] is None:
                    raise BusinessTransportError('busy')
                result = json.loads(json.dumps(entry['result']))
                result['idempotent_replay'] = True
                return identity, result
            if len(self._entries) >= self.maximum:
                raise BusinessTransportError('idempotency_capacity')
            self._entries[identity] = {'digest': digest, 'result': None}
            return identity, None

    def finish(self, identity, result):
        with self._lock:
            if result.get('mutation_outcome') == 'not_sent':
                self._entries.pop(identity, None)
            else:
                self._entries[identity]['result'] = json.loads(json.dumps(result))


_IDEMPOTENCY = IdempotencyStore()
# Public tool names are proposals for the central catalog, not auto-registration.
TOOLS = MappingProxyType({
    'list_albums': {'operation': 'albums_list', 'scope': 'albums', 'write': False},
    'list_shared_albums': {'operation': 'shared_albums_list', 'scope': 'albums', 'write': False},
    'create_album': {'operation': 'albums_create', 'scope': 'albums', 'write': True},
    'rename_album': {'operation': 'albums_rename', 'scope': 'albums', 'write': True},
    'list_movie_libraries': {'operation': 'movie_libraries_list', 'scope': 'movie', 'write': False},
    'rename_movie_library': {'operation': 'movie_library_rename', 'scope': 'movie', 'write': True},
    'revoke_share': {'operation': 'share_revoke', 'scope': 'sharing', 'write': True},
})
PENDING = MappingProxyType({
    'create_share': 'factory_cloud_share_id_and_external_link_contract_unverified',
    'create_movie_library': 'factory_source_and_member_contract_unverified',
    'configure_movie_sources': 'factory_source_and_member_contract_unverified',
    'list_album_files': 'factory_pagination_and_file_resource_boundary_unverified',
})


def _number(value):
    return value if type(value) is int and 0 <= value <= MAX_NUMBER else None


def _label(value, maximum=255):
    if (type(value) is not str or not value.strip()
            or len(value.encode('utf-8', errors='replace')) > maximum
            or any(unicodedata.category(c) in ('Cc', 'Cs') for c in value)):
        return None
    return value


def _id(value, *, shared=False):
    pattern = r'share_[0-9]{1,10}' if shared else r'[0-9]{1,10}'
    return value if type(value) is str and re.fullmatch(pattern, value) else None


def _kind(value):
    return value if (type(value) is str and re.fullmatch(r'[A-Za-z0-9_]{1,31}', value)) else None


def _arguments(tool, arguments):
    """Map strict public schemas into the separately validated native schema."""
    if type(arguments) is not dict:
        raise BusinessTransportError('invalid_parameters')
    if tool in ('list_albums', 'list_shared_albums', 'list_movie_libraries'):
        if arguments:
            raise BusinessTransportError('invalid_parameters')
        return {}, {}, False
    expected = {'create_album': {'name', 'confirmed', 'idempotency_key'},
                'rename_album': {'album_id', 'name', 'confirmed', 'idempotency_key'},
                'rename_movie_library': {'library_id', 'name', 'confirmed', 'idempotency_key'},
                'revoke_share': {'share_id', 'confirmed', 'idempotency_key'}}[tool]
    if set(arguments) != expected or arguments.get('confirmed') is not True:
        raise BusinessTransportError('confirmation_required' if arguments.get('confirmed') is not True
                                     else 'invalid_parameters')
    if (type(arguments.get('idempotency_key')) is not str
            or re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', arguments['idempotency_key']) is None):
        raise BusinessTransportError('idempotency_required')
    if tool == 'create_album':
        return {'album_name': safe_text(arguments['name'])}, {}, True
    if tool == 'rename_album':
        return {'album_id': arguments['album_id'],
                'album_new_name': safe_text(arguments['name'])}, {}, True
    if tool == 'rename_movie_library':
        return {}, {'lid': arguments['library_id'], 'name': safe_text(arguments['name'])}, True
    return {}, {'share_id_list': [arguments['share_id']]}, True


def _albums(frame, *, shared=False):
    rows = frame.get('share_album_list' if shared else 'all_album_list')
    if type(rows) is not list or len(rows) > MAX_ITEMS:
        raise BusinessTransportError('invalid_json')
    result, seen = [], set()
    for row in rows:
        if type(row) is not dict:
            raise BusinessTransportError('invalid_json')
        identifier = _id(row.get('album_id'), shared=shared)
        kind, name = _kind(row.get('type')), _label(row.get('name'))
        if identifier is None or kind is None or name is None or identifier in seen:
            raise BusinessTransportError('invalid_json')
        seen.add(identifier)
        item = {'album_id': identifier, 'name': name, 'type': kind,
                'count': _number(row.get('count'))}
        if shared:
            item['owner_created'] = (row['creater'] == 1 if type(row.get('creater')) is int
                                     and row['creater'] in (0, 1) else None)
        else:
            item['editable'] = kind == 'custom' and int(identifier) > 7
        result.append(item)
    return {'items': result, 'returned_count': len(result), 'complete': None,
            'covers_and_file_paths_omitted': True}


def _movies(frame):
    rows = frame.get('list')
    if type(rows) is not list or len(rows) > MAX_ITEMS:
        raise BusinessTransportError('invalid_json')
    result, seen = [], set()
    for row in rows:
        if type(row) is not dict:
            raise BusinessTransportError('invalid_json')
        name = _label(row.get('name'))
        if name is None:
            raise BusinessTransportError('invalid_json')
        # lid is the audited REST identity. Do not reinterpret arbitrary id,
        # path, source/member identities, URLs or downstream configuration.
        lid = _number(row.get('lid'))
        if lid is not None:
            if lid == 0 or lid in seen:
                raise BusinessTransportError('invalid_json')
            seen.add(lid)
        result.append({'library_id': lid, 'name': name,
                       'identity_available': lid is not None})
    return {'items': result, 'returned_count': len(result), 'complete': None,
            'source_paths_and_members_omitted': True}


def _project(operation, frame):
    if operation == 'albums_list':
        return _albums(frame)
    if operation == 'shared_albums_list':
        return _albums(frame, shared=True)
    if operation == 'movie_libraries_list':
        return _movies(frame)
    if operation in ('albums_create', 'albums_rename'):
        identifier = _id(frame.get('album_id'))
        name, kind = _label(frame.get('name')), _kind(frame.get('type'))
        if identifier is None or int(identifier) <= 7 or name is None or kind != 'custom':
            raise BusinessTransportError('invalid_json')
        return {'album_id': identifier, 'name': name, 'type': kind,
                'count': _number(frame.get('count')), 'accepted': True,
                'verification': 'factory_acknowledgement_only'}
    return {'accepted': True, 'verification': 'factory_acknowledgement_only'}


class MediaOperations:
    """Backend for exact media tools; no background refresh or implicit writes."""

    def __init__(self, root='/', *, client=None, idempotency_store=None):
        self.client = client or VendorBusinessClient(root=root)
        self._idempotency = idempotency_store or _IDEMPOTENCY

    def execute(self, tool, arguments=None, *, authorizer=None, principal=None):
        result = {'schema_version': 1, 'available': False, 'status': 'unavailable',
                  'reason': None, 'data': None,
                  'sampled_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                  'native_query_may_update_state': True, 'credential_read_only': True,
                  'native': {'http_status': None, 'code': None},
                  'mutation_outcome': None}
        mutation = False
        responded = False
        pending = None
        try:
            if type(tool) is not str or tool not in TOOLS:
                raise BusinessTransportError('unsupported_operation')
            definition = TOOLS[tool]
            mutation = definition['write']
            params, body, confirmed = _arguments(tool, {} if arguments is None else arguments)
            params, body = _validate(definition['operation'], params, body)
            # Cached results are still protected by the current policy. Never
            # use a past successful authorization as a credential or grant.
            if not callable(authorizer):
                raise BusinessTransportError('authorization_denied')
            try:
                allowed = authorizer(definition['operation'], dict(params), json.loads(json.dumps(body)))
            except Exception:
                allowed = False
            if allowed is not True:
                raise BusinessTransportError('authorization_denied')
            if mutation:
                digest = hashlib.sha256(json.dumps(
                    {'tool': tool, 'parameters': params, 'body': body}, sort_keys=True,
                    ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()
                pending, replay = self._idempotency.begin(principal, arguments['idempotency_key'], digest)
                if replay is not None:
                    pending = None
                    return replay
                result['idempotent_replay'] = False
            status, frame = self.client.request(definition['operation'], params, body,
                                                authorizer, confirmed=confirmed)
            responded = True
            result['native']['http_status'] = status if type(status) is int else None
            if status != 200 or type(frame) is not dict or type(frame.get('code')) is not int:
                raise BusinessTransportError('invalid_json')
            code = frame['code']
            result['native']['code'] = code if abs(code) <= MAX_NUMBER else None
            if result['native']['code'] is None:
                raise BusinessTransportError('invalid_json')
            if code != 0:
                result.update(status='native_error', reason='native_request_rejected',
                              mutation_outcome='rejected' if mutation else None)
                return result
            data = _project(definition['operation'], frame)
            # A successful factory response must refer to the requested object.
            if tool == 'rename_album' and data['album_id'] != arguments['album_id']:
                raise BusinessTransportError('invalid_json')
            if tool in ('create_album', 'rename_album') and data['name'] != arguments['name']:
                raise BusinessTransportError('invalid_json')
            result.update(available=True, status='ok', data=data,
                          mutation_outcome='acknowledged' if mutation else None)
        except BusinessTransportError as error:
            result['reason'] = error.reason
            if error.http_status is not None:
                result['native']['http_status'] = error.http_status
            # Never automatically retry a request with an unknown write outcome.
            if mutation:
                result['mutation_outcome'] = ('unknown' if responded or error.request_sent else 'not_sent')
                if result['mutation_outcome'] == 'unknown':
                    result['failure_reason'] = result['reason']
                    result['reason'] = 'result_unconfirmed'
        except Exception:
            result['reason'] = 'transport_failed'
            result['mutation_outcome'] = 'unknown' if mutation else None
            if mutation:
                result['failure_reason'] = 'transport_failed'
                result['reason'] = 'result_unconfirmed'
        finally:
            if pending is not None:
                self._idempotency.finish(pending, result)
        return result


def collect_media_operations_probe(root='/', *, authorizer=None, client=None):
    """One explicit fixed developer item: list schemas, never create samples.

    The caller still checks the actual peer/developer window and must explicitly
    authorize this item. Responses contain only projected business metadata;
    no native JSON, credential, cover URL, file path or speculative share URL.
    """
    backend = MediaOperations(root, client=client)
    items = []
    for tool in ('list_albums', 'list_shared_albums', 'list_movie_libraries'):
        item = backend.execute(tool, {}, authorizer=authorizer)
        items.append({'tool': tool, **item})
    return {'schema_version': 1, 'explicit_only': True,
            'native_query_may_update_state': True, 'may_wake_disks': True, 'items': items,
            'pending': dict(PENDING)}

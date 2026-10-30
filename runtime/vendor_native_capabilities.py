"""Fixed native application, backup, account and system business executors.

These are explicit authorized operations, not background collectors. Paths,
native actions, credentials, package URLs and SQL are never tool parameters.
"""

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import re
import threading
from types import MappingProxyType
from urllib.parse import urlsplit

from native_capability_specs import TOOL_SPECS
from vendor_business_transport import BusinessTransportError, VendorBusinessClient, _validate
from vendor_media_auth import OWNER_ID
from vendor_media_operations import IdempotencyStore, _albums, _label, _number


_SPECS = {item['name']: item for item in TOOL_SPECS}
_OPERATIONS = MappingProxyType({
    'list_native_applications': 'apply_list',
    'get_application_install_progress': 'application_progress',
    'list_backup_tasks': 'backup_tasks_list',
    'get_auto_backup_settings': 'auto_backup_get',
    'disable_auto_backup': 'auto_backup_disable',
    'pause_backup_task': 'backup_task_halt', 'cancel_backup_task': 'backup_task_halt',
    'list_nas_users': 'nas_users_list',
    'get_nas_name': 'device_name_get', 'set_nas_name': 'device_name_set',
    'get_nas_ai_settings': 'nas_ai_get', 'set_nas_ai_enabled': 'nas_ai_set',
    'get_movie_library_scan_status': 'movie_scan_status', 'delete_album': 'album_delete',
})
_WRITES = frozenset(('get_application_install_progress', 'set_nas_name',
                     'set_nas_ai_enabled', 'delete_album', 'disable_auto_backup',
                     'pause_backup_task', 'cancel_backup_task'))
_OPTIONAL_CODE = frozenset(('apply_list', 'backup_tasks_list'))
_LOCK = threading.RLock()
_STORE = IdempotencyStore()
PENDING = MappingProxyType({
    'install_application': 'fixed_native_package_identity_and_install_task_contract_unverified',
    'remove_application': 'trusted_installed_package_identity_and_removal_effects_required',
    'control_application': 'per_application_lifecycle_contract_unverified',
    'create_share': 'factory_cloud_share_id_and_external_link_contract_unverified',
    'album_contents': 'users_root_and_final_object_acl_binding_required',
    'album_members': 'existing_shared_album_owner_and_member_contract_required',
    'movie_sources': 'users_root_and_source_acl_binding_required',
    'movie_playback_and_subtitles': 'users_root_and_final_object_acl_binding_required',
    'resume_backup_task': 'existing_destination_acl_and_file_boundary_required',
    'backup_schedule': 'existing_destination_acl_and_backup_type_contract_required',
    'account_mutation': 'per_object_privilege_and_credential_change_contract_required',
})


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=True))


def _authorize(callback, operation, parameters, body):
    try:
        allowed = callback(operation, dict(parameters), _json_copy(body)) if callable(callback) else False
    except Exception:
        allowed = False
    if allowed is not True:
        raise BusinessTransportError('authorization_denied')


@contextmanager
def _operation_lock():
    """Bound lock waiting; a two-request operation has at most three windows."""
    if not _LOCK.acquire(timeout=10.0):
        raise BusinessTransportError('busy')
    try:
        yield
    finally:
        _LOCK.release()


def _arguments(tool, arguments):
    if type(tool) is not str or tool not in _SPECS:
        raise BusinessTransportError('unsupported_operation')
    if type(arguments) is not dict:
        raise BusinessTransportError('invalid_parameters')
    if set(arguments) != set(_SPECS[tool]['inputSchema']['required']):
        raise BusinessTransportError('invalid_parameters')
    if tool in _WRITES:
        if arguments.get('confirmed') is not True:
            raise BusinessTransportError('confirmation_required')
        if (type(arguments.get('idempotency_key')) is not str
                or re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', arguments['idempotency_key']) is None):
            raise BusinessTransportError('idempotency_required')
    params, body = {}, {}
    if tool == 'get_application_install_progress':
        body = {'apply_id': arguments['application_id']}
    elif tool == 'set_nas_name':
        params = {'name': arguments['name']}
    elif tool == 'set_nas_ai_enabled':
        if type(arguments['enabled']) is not bool:
            raise BusinessTransportError('invalid_parameters')
        params = {'mode': int(arguments['enabled'])}
    elif tool == 'disable_auto_backup':
        if type(arguments['source']) is not str or arguments['source'] not in ('usb', 'sd'):
            raise BusinessTransportError('invalid_parameters')
        params = {'disk_type': 'udisk_a' if arguments['source'] == 'usb' else 'sd', 'status': 0}
    elif tool in ('pause_backup_task', 'cancel_backup_task'):
        if (type(arguments['expected_version']) is not str or
                re.fullmatch(r'backup_[0-9a-f]{32}', arguments['expected_version']) is None):
            raise BusinessTransportError('invalid_parameters')
        params = {'task_id': arguments['task_id'],
                  'task_status': 2 if tool == 'pause_backup_task' else 1}
    elif tool == 'get_movie_library_scan_status':
        body = {'lid': arguments['library_id']}
    elif tool == 'delete_album':
        value = arguments['album_id']
        if type(value) is not str or re.fullmatch(r'[1-9][0-9]{0,9}', value) is None or int(value) <= 7:
            raise BusinessTransportError('invalid_parameters')
        params = {'album_id': value}
    return _validate(_OPERATIONS[tool], params, body)


def _native_code(operation, frame):
    """Two audited serializers may omit code; missing never becomes fake zero."""
    if type(frame) is not dict:
        raise BusinessTransportError('invalid_json')
    if 'code' not in frame:
        if operation in _OPTIONAL_CODE:
            return None
        raise BusinessTransportError('invalid_json')
    code = frame['code']
    if type(code) is not int or abs(code) > 2 ** 53 - 1:
        raise BusinessTransportError('invalid_json')
    return code


def _applications(frame):
    rows = frame.get('list')
    if type(rows) is not list or len(rows) > 256:
        raise BusinessTransportError('invalid_json')
    items, seen = [], set()
    for row in rows:
        if type(row) is not dict:
            raise BusinessTransportError('invalid_json')
        name = _label(row.get('module_name'), 63)
        identifier = _number(row.get('apply_id'))
        if name is None or identifier is None or not 1 <= identifier <= 2 ** 31 - 1 or identifier in seen:
            raise BusinessTransportError('invalid_json')
        seen.add(identifier)
        raw_status = row.get('status')
        status = raw_status if type(raw_status) is int and -(2 ** 31) <= raw_status < 2 ** 31 else None
        items.append({'application_id': identifier, 'name': name,
                      'version': _label(row.get('version'), 63),
                      'installation_status_code': status, 'running': None})
    return {'items': items, 'returned_count': len(items), 'complete': False,
            'catalog_is_installed_inventory': False, 'package_sources_omitted': True}


def _backup_tasks(frame):
    groups, counts, observed, all_seen = {}, {}, 0, set()
    for source, target in (('run_list', 'running'), ('wait_list', 'waiting'), ('done_list', 'done')):
        value = frame.get(source)
        if value is None:
            groups[target], counts[target] = None, None
            continue
        if type(value) is not list or len(value) > 256:
            raise BusinessTransportError('invalid_json')
        observed += 1
        items = []
        for row in value:
            if type(row) is not dict:
                raise BusinessTransportError('invalid_json')
            task_id = _number(row.get('task_id'))
            if task_id is None or task_id == 0 or task_id in all_seen:
                raise BusinessTransportError('invalid_json')
            all_seen.add(task_id)
            # Do not let a future native serializer leak another account's
            # task metadata through an existing owner-only contract.
            if type(row.get('user')) is not int or row['user'] != OWNER_ID:
                raise BusinessTransportError('invalid_json')
            status = row.get('task_status')
            status = status if type(status) is int and -(2 ** 31) <= status < 2 ** 31 else None
            material = {key: row.get(key) for key in ('task_id', 'task_status', 'user',
                       'atid', 'from', 'to', 'disk_uuid', 'done_time')}
            for key in ('atid', 'from', 'to', 'disk_uuid'):
                if material[key] is not None and (type(material[key]) is not str
                        or len(material[key].encode('utf8')) > 4096):
                    raise BusinessTransportError('invalid_json')
            # Native paths bind the version inside the host, never the result.
            # Downloaded byte counts are intentionally not version components.
            version = 'backup_' + hashlib.sha256(json.dumps(material, sort_keys=True,
                       ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()[:32]
            items.append({'task_id': task_id, 'status_code': status, 'version': version,
                          'total_bytes': _number(row.get('total_size')),
                          'completed_at_native': _number(row.get('done_time'))})
        groups[target], counts[target] = items, len(items)
    if not observed:
        raise BusinessTransportError('invalid_json')
    return {'groups': groups, 'counts': counts, 'complete': None,
            'paths_and_disk_identity_omitted': True}


def _users(frame):
    rows = frame.get('user_list')
    if type(rows) is not list or len(rows) > 256:
        raise BusinessTransportError('invalid_json')
    items, seen = [], set()
    for row in rows:
        if type(row) is not dict:
            raise BusinessTransportError('invalid_json')
        identifier, name = _number(row.get('user_id')), _label(row.get('user_name'), 63)
        if identifier is None or identifier == 0 or identifier in seen or name is None:
            raise BusinessTransportError('invalid_json')
        seen.add(identifier)
        items.append({'user_id': identifier, 'name': name, 'nickname': _label(row.get('nickname'), 63)})
    return {'items': items, 'returned_count': len(items),
            'accounts_permissions_and_credentials_omitted': True}


def _project(tool, frame, arguments):
    if tool == 'list_native_applications':
        return _applications(frame)
    if tool == 'list_backup_tasks':
        return _backup_tasks(frame)
    if tool == 'get_auto_backup_settings':
        return {key: _number(frame.get(key)) for key in
                ('udisk_a_status', 'sd_status', 'udisk_a_type', 'sd_type')}
    if tool == 'list_nas_users':
        return _users(frame)
    if tool == 'get_nas_name':
        name = _label(frame.get('name'), 63)
        if name is None:
            raise BusinessTransportError('invalid_json')
        return {'name': name}
    if tool == 'get_nas_ai_settings':
        status = frame.get('status')
        enabled = bool(status) if type(status) is int and status in (0, 1) else None
        return {'enabled': enabled, 'status_code': status if type(status) is int
                and -(2 ** 31) <= status < 2 ** 31 else None}
    if tool == 'get_movie_library_scan_status':
        value = {key: _number(frame.get(key)) for key in ('status', 'scaned', 'total', 'timestamp', 'num')}
        if value['status'] is None:
            raise BusinessTransportError('invalid_json')
        return {'library_id': arguments['library_id'], 'native': value,
                'progress_percent': None, 'state_interpretation': 'native_enum_unmapped'}
    if tool == 'get_application_install_progress':
        identifier = frame.get('apply_id')
        if identifier is not None and (type(identifier) is not int
                                       or identifier != arguments['application_id']):
            raise BusinessTransportError('invalid_json')
        return {'application_id': arguments['application_id'],
                'status_code': _number(frame.get('status')),
                'downloaded_bytes': _number(frame.get('size')),
                'total_bytes': _number(frame.get('total_size')),
                'progress_percent': None, 'accepted': True,
                'verification': 'factory_acknowledgement_only'}
    return {'accepted': True, 'verification': 'factory_acknowledgement_only',
            'asynchronous_completion_verified': False}


class NativeCapabilities:
    """One explicit fixed business request, plus narrowly bounded preflight.

    No native write is retried. Cached write results still require the current
    original operation authorization; a process restart drops memory receipts.
    """

    def __init__(self, root='/', *, client=None, idempotency_store=None):
        self.client = client or VendorBusinessClient(root=root)
        self._idempotency = idempotency_store or _STORE

    def execute(self, tool, arguments=None, *, principal=None, authorizer=None):
        result = {'schema_version': 1, 'available': False, 'status': 'unavailable',
                  'reason': None, 'data': None, 'native': {'http_status': None, 'code': None},
                  'sampled_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                  'native_query_may_update_state': True, 'credential_read_only': True,
                  'mutation_outcome': None}
        pending, responded, mutation = None, False, tool in _WRITES if type(tool) is str else False
        try:
            arguments = {} if arguments is None else arguments
            params, body = _arguments(tool, arguments)
            operation = _OPERATIONS[tool]
            _authorize(authorizer, operation, params, body)
            if mutation:
                digest = hashlib.sha256(json.dumps({'tool': tool, 'params': params, 'body': body},
                            sort_keys=True, separators=(',', ':')).encode('utf8')).hexdigest()
                pending, replay = self._idempotency.begin(principal, arguments['idempotency_key'], digest)
                if replay is not None:
                    pending = None
                    _authorize(authorizer, operation, params, body)
                    return replay
                result['idempotent_replay'] = False
            # A catalog id or album id is not itself an ownership proof.
            # Before native status mutation or deletion, find the exact object
            # in the fixed existing-owner catalog. No HTTP supplied UID.
            with _operation_lock():
                if tool in ('get_application_install_progress', 'delete_album',
                            'pause_backup_task', 'cancel_backup_task'):
                    pre_op = ('apply_list' if tool == 'get_application_install_progress'
                              else 'albums_list' if tool == 'delete_album' else 'backup_tasks_list')
                    # Preflight is part of this tool's authority, not a grant
                    # for the caller to invoke the independent list tool.
                    def pre_authorize(_op, _params, _body):
                        _authorize(authorizer, operation, params, body)
                        return True
                    status, frame = self.client.request(pre_op, {}, {}, pre_authorize)
                    code = _native_code(pre_op, frame)
                    if status != 200 or code not in (None, 0):
                        raise BusinessTransportError('invalid_json')
                    if tool == 'get_application_install_progress':
                        items = _applications(frame)['items']
                        found = any(row['application_id'] == arguments['application_id'] for row in items)
                    elif tool == 'delete_album':
                        items = _albums(frame)['items']
                        found = any(row['album_id'] == arguments['album_id'] and row['editable'] is True
                                    for row in items)
                    else:
                        groups = _backup_tasks(frame)['groups']
                        keys = ('running',) if tool == 'pause_backup_task' else ('running', 'waiting')
                        found = any(row['task_id'] == arguments['task_id']
                                    and row['version'] == arguments['expected_version']
                                    for key in keys for row in (groups[key] or []))
                    if not found:
                        result['reason'] = 'object_not_in_owner_catalog'
                        result['mutation_outcome'] = 'not_sent'
                        return result
                _authorize(authorizer, operation, params, body)
                status, frame = self.client.request(operation, params, body, authorizer,
                                                    confirmed=mutation)
                responded = True
                code = _native_code(operation, frame)
                result['native'] = {'http_status': status if type(status) is int else None,
                                    'code': code}
                if status != 200 or code not in (None, 0):
                    result.update(status='native_error', reason='native_request_rejected',
                                  mutation_outcome='rejected' if mutation else None)
                    _authorize(authorizer, operation, params, body)
                    return result
                data = _project(tool, frame, arguments)
                _authorize(authorizer, operation, params, body)
                result.update(available=True, status='ok', data=data,
                              mutation_outcome='acknowledged' if mutation else None)
        except BusinessTransportError as error:
            result['reason'] = error.reason
            if error.http_status is not None:
                result['native']['http_status'] = error.http_status
            if mutation:
                result['mutation_outcome'] = 'unknown' if responded or error.request_sent else 'not_sent'
                if result['mutation_outcome'] == 'unknown':
                    result['failure_reason'], result['reason'] = result['reason'], 'result_unconfirmed'
        except Exception:
            result['reason'] = 'transport_failed'
            if mutation:
                result.update(reason='result_unconfirmed', failure_reason='transport_failed',
                              mutation_outcome='unknown')
        finally:
            if pending is not None:
                self._idempotency.finish(pending, result)
        return result


def collect_native_capabilities_probe(root='/', *, authorizer=None, client=None):
    """Explicit no-sample diagnostic for fixed catalogs and safe field shapes.

    The host must enforce the developer peer, absolute window and confirmation.
    This never calls application progress, setters, deletion or task control.
    """
    backend = NativeCapabilities(root, client=client)
    names = ('list_native_applications', 'list_backup_tasks', 'get_auto_backup_settings',
             'list_nas_users', 'get_nas_name', 'get_nas_ai_settings')
    return {'schema_version': 1, 'explicit_only': True,
            'native_query_may_update_state': True, 'may_wake_disks': True,
            'items': [{'tool': name, **backend.execute(name, {}, authorizer=authorizer)} for name in names],
            'pending': dict(PENDING)}


def _package_field_shape(row, key):
    """Describe a fixed catalog field without returning private native values."""
    value = row.get(key)
    kind = ('missing' if key not in row else 'null' if value is None
            else 'boolean' if type(value) is bool else 'integer' if type(value) is int
            else 'number' if type(value) is float else 'string' if type(value) is str
            else 'array' if type(value) is list else 'object')
    result = {'type': kind}
    if type(value) is str:
        result['utf8_bytes'] = len(value.encode('utf8'))
    return result


def _package_url_shape(value):
    """Report protocol/address classes, never hostname, path, query or userinfo."""
    if type(value) is not str:
        return None
    result = {'scheme': 'invalid', 'host_category': 'invalid',
              'has_userinfo': False, 'has_query': False, 'has_fragment': False,
              'port_present': False, 'source_validated': False}
    try:
        if len(value.encode('utf8')) > 8192 or any(ord(c) < 32 for c in value):
            return result
        parsed = urlsplit(value)
        result['scheme'] = parsed.scheme.lower() if parsed.scheme.lower() in (
            'http', 'https', 'file', 'ftp') else 'other' if parsed.scheme else 'relative'
        result.update(has_userinfo=parsed.username is not None or parsed.password is not None,
                      has_query=bool(parsed.query), has_fragment=bool(parsed.fragment),
                      port_present=parsed.port is not None)
        host = parsed.hostname
        if not host:
            result['host_category'] = 'absent'
        else:
            try:
                address = ipaddress.ip_address(host)
                result['host_category'] = ('loopback_ip' if address.is_loopback else
                    'private_ip' if address.is_private else 'public_ip' if address.is_global
                    else 'other_ip')
            except ValueError:
                result['host_category'] = ('localhost' if host.lower() == 'localhost' else
                                          'dns_name' if '.' in host else 'single_label')
    except (TypeError, ValueError):
        pass
    return result


def _package_checksum_shape(value, field):
    """Algorithm names and lengths are hints; they never verify package bytes."""
    if type(value) is not str:
        return None
    length = len(value)
    hex_value = bool(value) and re.fullmatch(r'[0-9a-fA-F]+', value) is not None
    algorithm = {'md5': 'md5', 'sha1': 'sha1', 'sha256': 'sha256',
                 'sha512': 'sha512'}.get(field)
    result = {'algorithm_label': algorithm, 'format': 'hex' if hex_value else 'other',
              'characters': length, 'verified': False}
    if algorithm:
        result['matches_algorithm_hex_length'] = hex_value and length == {
            'md5': 32, 'sha1': 40, 'sha256': 64, 'sha512': 128}[algorithm]
    elif hex_value:
        result['length_algorithm_hint'] = {32: 'md5', 40: 'sha1', 64: 'sha256',
                                          128: 'sha512'}.get(length)
    return result


def collect_application_package_context(root='/', *, authorizer=None, client=None):
    """One explicit owner catalog yields a complete, bounded contract diagnostic.

    Integer enum codes are evidence, not guessed labels. Fixed optional URL and
    checksum fields report only classes/format, never native values. Native
    package verification and asynchronous completion remain unproven.
    """
    operation, params, body = 'apply_list', {}, {}
    result = {'schema_version': 2, 'available': False, 'status': 'unavailable',
              'reason': None, 'items': None, 'explicit_only': True,
              'native_query_may_update_state': True, 'may_wake_disks': True,
              'trusted_package_manifest_verified': False,
              'package_execution_available': False,
              'catalog_is_installed_inventory': False,
              'protocol': {'catalog_action': 'get_apply_list', 'opttype': 0,
                           'source_is_native_integer_enum': True,
                           'install_flag': 0, 'update_flag': 1,
                           'native_worker_is_asynchronous': True,
                           'independent_signature_requirement': False,
                           'pending_reason': PENDING['install_application']}}
    try:
        _authorize(authorizer, operation, params, body)
        native = client or VendorBusinessClient(root=root)
        status, frame = native.request(operation, params, body, authorizer)
        code = _native_code(operation, frame)
        if status != 200 or code not in (None, 0):
            result['reason'] = 'native_request_rejected'
            return result
        catalog = _applications(frame)['items']
        shapes = []
        for metadata, row in zip(catalog, frame['list']):
            enum_keys = ('module_type', 'source', 'status', 'guest', 'apply_opt',
                         'upgradable', 'support_terminal', 'support_ecloud')
            url_keys = ('download_url', 'package_url', 'url')
            checksum_keys = ('md5', 'sha1', 'sha256', 'sha512', 'checksum', 'hash')
            field_keys = ('apply_id', 'module_name', 'vid', 'version', 'size', 'path',
                          'install_time', 'paramList', 'signature') + enum_keys + url_keys + checksum_keys
            fields = {key: _package_field_shape(row, key) for key in field_keys}
            enums = {key: {'code': row[key] if type(row.get(key)) is int and
                         -(2 ** 31) <= row[key] < 2 ** 31 else None,
                          'meaning': 'native_enum_unmapped'} for key in enum_keys}
            urls = {key: _package_url_shape(row.get(key)) for key in url_keys}
            # Some firmware variants could serialize source as text. Describe
            # its URL class, but do not reinterpret it as an accepted source.
            if type(row.get('source')) is str:
                urls['source'] = _package_url_shape(row['source'])
            checksums = {key: _package_checksum_shape(row.get(key), key) for key in checksum_keys}
            shapes.append({'application_id': metadata['application_id'], 'name': metadata['name'],
                           'version': metadata['version'], 'field_shapes': fields,
                           'enum_fields': enums, 'url_fields': urls,
                           'checksum_fields': checksums,
                           'trusted_source_verified': False, 'signature_verified': False})
        _authorize(authorizer, operation, params, body)
        result.update(available=True, status='ok', items=shapes)
    except BusinessTransportError as error:
        result['reason'] = error.reason
    except Exception:
        result['reason'] = 'transport_failed'
    return result

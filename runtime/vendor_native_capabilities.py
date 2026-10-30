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

from native_capability_specs import APPLICATION_INSTALL_SPECS, TOOL_SPECS
from vendor_business_transport import BusinessTransportError, VendorBusinessClient, _validate
from vendor_media_auth import OWNER_ID
from vendor_media_operations import IdempotencyStore, _albums, _label, _number


_SPECS = {item['name']: item for item in TOOL_SPECS}
_OPERATIONS = MappingProxyType({
    'list_native_applications': 'apply_list',
    'list_native_modules': 'module_inventory',
    'plan_native_application_install': 'module_inventory',
    'install_native_application': 'application_install',
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
                     'pause_backup_task', 'cancel_backup_task', 'install_native_application'))
_OPTIONAL_CODE = frozenset(('apply_list', 'backup_tasks_list', 'application_install'))
_LOCK = threading.RLock()
_STORE = IdempotencyStore()
PENDING = MappingProxyType({
    'install_other_applications': 'only_audited_new_install_allowlist_supported',
    'update_application': 'new_install_only_updates_unverified',
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
_INSTALL_TOOLS = frozenset(('plan_native_application_install', 'install_native_application'))
_INSTALL_ALLOWLIST = APPLICATION_INSTALL_SPECS
# Empty presentation labels for these two IDs are observed in the matched
# firmware. These audited registration names are display-only, never package
# identity or permission to progress/install a previously quarantined object.
_EMPTY_APPLICATION_DISPLAY_NAMES = MappingProxyType({2: 'ipc', 35: '66_print'})


def _installation_resources(principal):
    """Missing old resources preserve only ID34; explicit empty grants nothing.

    Owner Key storage supplies current grants. This second check keeps adapters
    and every continuation from treating a broader software allowlist as access.
    """
    values = principal.get('application_ids', [34]) if type(principal) is dict else None
    if (type(values) is not list or len(values) > len(APPLICATION_INSTALL_SPECS)
            or any(type(value) is not int or value not in APPLICATION_INSTALL_SPECS for value in values)
            or len(set(values)) != len(values)):
        raise BusinessTransportError('authorization_denied')
    return sorted(values)


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


def _application_display_name(row):
    """Fixed registration-name fallback for exactly observed empty labels.

    Missing, null, control-bearing or unknown labels remain invalid. Caller
    supplied alternative fields never become a trusted native identity.
    """
    if type(row) is not dict:
        return None
    name = _label(row.get('module_name'), 63)
    if name is None and row.get('module_name') == '' and type(row.get('apply_id')) is int:
        return _EMPTY_APPLICATION_DISPLAY_NAMES.get(row['apply_id'])
    return name


def _applications(frame):
    """Isolate bad records while rejecting every ambiguous native identity.

    Count IDs before checking labels: an invalid duplicate node must not make
    another record with that ID look uniquely owned during write preflight.
    """
    rows = frame.get('list')
    if type(rows) is not list or len(rows) > 256:
        raise BusinessTransportError('invalid_json')
    identities = {}
    for row in rows:
        identifier = _number(row.get('apply_id')) if type(row) is dict else None
        if identifier is not None and 1 <= identifier <= 2 ** 31 - 1:
            identities[identifier] = identities.get(identifier, 0) + 1
    items, rejected = [], {}
    for row in rows:
        identifier = _number(row.get('apply_id')) if type(row) is dict else None
        name = _application_display_name(row)
        reason = ('unsupported_application_entry' if type(row) is not dict else
                  'invalid_application_id' if identifier is None or not 1 <= identifier <= 2 ** 31 - 1 else
                  'duplicate_application_id' if identities[identifier] != 1 else
                  'invalid_application_label' if name is None else None)
        if reason is not None:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        raw_status = row.get('status')
        status = raw_status if type(raw_status) is int and -(2 ** 31) <= raw_status < 2 ** 31 else None
        item = {'application_id': identifier, 'name': name,
                'version': _label(row.get('version'), 63),
                'installation_status_code': status, 'running': None}
        if row.get('module_name') == '':
            item['name_source'] = 'audited_module_registration'
        items.append(item)
    if rows and not items:
        raise BusinessTransportError('invalid_json')
    return {'items': items, 'returned_count': len(items), 'complete': False,
            'accepted_entries': len(items), 'rejected_entries': sum(rejected.values()),
            'partial': bool(rejected),
            'rejections': [{'code': code, 'count': rejected[code]} for code in (
                'unsupported_application_entry', 'invalid_application_id',
                'duplicate_application_id', 'invalid_application_label') if code in rejected],
            'catalog_is_installed_inventory': False, 'package_sources_omitted': True}


def _module_text(value, maximum=63):
    """Native module labels/versions cannot contain a path or URL grammar."""
    label = _label(value, maximum)
    if label is None or label != label.strip() or any(char in label for char in ('/', '\\', ':', '?', '#', '%')):
        return None
    return label


def _module_integer(value):
    """Preserve the native enum code without treating unknown values as zero."""
    return value if type(value) is int and -(2 ** 31) <= value < 2 ** 31 else None


def _installation_candidate(identifier, frames):
    """Bind one audited ordinary app to a unique current uninstalled module.

    Both authenticated factory catalogs remain authoritative for package
    selection; no URL/manifest/signature is invented or accepted from callers.
    Private native paths participate only in the opaque version, not requests.
    """
    spec = _INSTALL_ALLOWLIST[identifier]
    expected_name, source, kind, native_type = (spec[key] for key in
        ('module_name', 'source', 'module_type', 'native_type'))
    apps, modules = frames['apply_list'], frames['module_inventory']
    accepted_apps = _applications(apps)['items']
    _native_modules(modules)
    if not any(row['application_id'] == identifier and row['name'] == expected_name for row in accepted_apps):
        return None, 'application_identity_changed'
    for rows, field in ((apps['list'], 'apply_id'), (modules['available_list'], 'id'),
                        (modules['installed_list'], 'id')):
        if any(type(row) is dict and row.get('module_name') == expected_name
               and (type(row.get(field)) is not int or row[field] != identifier) for row in rows):
            return None, 'application_identity_changed'
    app_rows = [row for row in apps['list'] if type(row) is dict and type(row.get('apply_id')) is int
                and row['apply_id'] == identifier]
    available = [row for row in modules['available_list'] if type(row) is dict and type(row.get('id')) is int
                 and row['id'] == identifier]
    if any(type(row) is dict and (row.get('id') == identifier or row.get('module_name') == expected_name)
           for row in modules['installed_list']):
        return None, 'application_not_uninstalled'
    if len(app_rows) != 1 or len(available) != 1:
        return None, 'application_identity_changed'
    app, module = app_rows[0], available[0]
    if (app.get('module_name') != expected_name or module.get('module_name') != expected_name
            or type(app.get('source')) is not int or app['source'] != source
            or type(app.get('module_type')) is not int or app['module_type'] != kind
            or module.get('type') != native_type):
        return None, 'application_identity_changed'
    if (type(app.get('status')) is not int or app['status'] != 0
            or type(app.get('apply_opt')) is not int or app['apply_opt'] != 0
            or type(module.get('status')) is not int or module['status'] != 0
            or module.get('curr_ver') != '0'):
        return None, 'application_not_uninstalled'
    version = _module_text(module.get('latest_ver'))
    if version is None or version != _module_text(app.get('version')):
        return None, 'catalog_version_changed'
    size, vid = app.get('size'), app.get('vid')
    if (type(size) is not int or not 0 <= size <= 2 ** 31 - 1
            or type(module.get('size')) is not int or module['size'] != size
            or type(vid) is not str or re.fullmatch(r'[A-Za-z0-9._-]{0,63}', vid) is None
            or type(app.get('guest')) is not int or app['guest'] != 0):
        return None, 'package_contract_unverified'
    for row, field in ((app, 'path'), (module, 'inst_path')):
        value = row.get(field)
        if value is not None and (type(value) is not str or len(value.encode('utf8', errors='replace')) > 4096):
            return None, 'package_contract_unverified'
    body = {'client_id': 'r1-toolbox', 'flag': 0, 'apply_id': identifier,
            'module_name': expected_name, 'module_type': kind, 'source': source,
            'version': version, 'vid': vid, 'size': size, 'guest': 0}
    _validate('application_install', {}, body)
    return {'body': body, 'app_identity': {key: app.get(key) for key in (
        'path', 'install_time', 'status', 'apply_opt')},
        'module_identity': {key: module.get(key) for key in ('id', 'module_name',
            'type', 'curr_ver', 'latest_ver', 'size', 'status', 'upgradable',
            'usable', 'device_need_update', 'inst_path')}, 'installed': False}, None


def _native_modules(frame):
    """Project the two audited owner module tables, never a write grant.

    Membership is evidence of the specific returned table, not a running-state
    or package trust verdict. Do not merge rows across those two observations.
    """
    groups = {}
    for field, group in (('available_list', 'available'), ('installed_list', 'installed')):
        rows = frame.get(field)
        if type(rows) is not list or len(rows) > 256:
            raise BusinessTransportError('invalid_json')
        identities = {}
        for row in rows:
            identifier = _number(row.get('id')) if type(row) is dict else None
            if identifier is not None and 1 <= identifier <= 2 ** 31 - 1:
                identities[identifier] = identities.get(identifier, 0) + 1
        accepted, rejected = [], {}
        for row in rows:
            identifier = _number(row.get('id')) if type(row) is dict else None
            name = _module_text(row.get('module_name')) if type(row) is dict else None
            reason = ('unsupported_module_entry' if type(row) is not dict else
                      'invalid_module_id' if identifier is None or not 1 <= identifier <= 2 ** 31 - 1 else
                      'duplicate_module_id' if identities[identifier] != 1 else
                      'invalid_module_label' if name is None else None)
            if reason is not None:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            native_type = row.get('type')
            native_type = native_type if type(native_type) is str and re.fullmatch(
                r'[A-Za-z][A-Za-z0-9_-]{0,31}', native_type) else None
            accepted.append({'module_id': identifier, 'name': name,
                             'current_version': _module_text(row.get('curr_ver')),
                             'available_version': _module_text(row.get('latest_ver')),
                             'native_status_code': _module_integer(row.get('status')),
                             'native_type': native_type,
                             'upgradable_code': _module_integer(row.get('upgradable')),
                             'usable_code': _module_integer(row.get('usable')),
                             'device_update_required_code': _module_integer(row.get('device_need_update')),
                             'running': None})
        if rows and not accepted:
            raise BusinessTransportError('invalid_json')
        groups[group] = {'items': accepted, 'returned_count': len(accepted),
                         'accepted_entries': len(accepted), 'rejected_entries': sum(rejected.values()),
                         'partial': bool(rejected),
                         'rejections': [{'code': code, 'count': rejected[code]} for code in (
                             'unsupported_module_entry', 'invalid_module_id',
                             'duplicate_module_id', 'invalid_module_label') if code in rejected]}
    return {'groups': groups, 'complete': False,
            'installed_membership_source': 'native_installed_list',
            'catalog_confers_write_identity': False, 'running_state_available': False,
            'paths_parameters_and_package_sources_omitted': True}


_STRUCTURE_FIELDS = frozenset((
    'code', 'msg', 'data', 'result', 'value', 'list', 'count', 'support_ecloud',
    'apply_id', 'module_name', 'vid', 'module_type', 'source', 'version', 'size',
    'status', 'guest', 'apply_opt', 'upgradable', 'support_terminal', 'path',
    'install_time', 'paramList', 'download_url', 'package_url', 'url', 'md5',
    'sha1', 'sha256', 'sha512', 'checksum', 'hash', 'signature',
    'run_list', 'wait_list', 'done_list', 'list_cm', 'task_id', 'task_status', 'user',
    'total_size', 'done_time', 'atid', 'from', 'to', 'disk_uuid'))


def _native_response_structure(frame):
    """Describe one already-returned frame without disclosing any native value.

    Only fixed schema field names may appear. Unknown names and their values
    are represented by counts, including inside arrays and future wrappers.
    Array variants are grouped by identical shape while inspecting every
    bounded entry; no arbitrary keys, native paths or exception text escape.
    """
    budget, bounded = [8192], [False]

    def shape(value, depth=0):
        budget[0] -= 1
        if budget[0] < 0 or depth > 6:
            bounded[0] = True
            return {'type': 'uninspected', 'reason': 'structure_bounds'}
        if value is None:
            return {'type': 'null'}
        if type(value) is bool:
            return {'type': 'boolean'}
        if type(value) is int:
            return {'type': 'integer'}
        if type(value) is float:
            return {'type': 'number'}
        if type(value) is str:
            return {'type': 'string', 'utf8_bytes': len(value.encode('utf8', errors='replace'))}
        if type(value) is dict:
            fields = {key: shape(value[key], depth + 1) for key in sorted(_STRUCTURE_FIELDS.intersection(value))}
            return {'type': 'object', 'fields': fields,
                    'omitted_field_count': len(value) - len(fields)}
        if type(value) is list:
            bounded[0] |= len(value) > 256
            variants = {}
            for item in value[:256]:
                item_shape = shape(item, depth + 1)
                identity = json.dumps(item_shape, sort_keys=True, separators=(',', ':'))
                if identity not in variants:
                    variants[identity] = {'count': 0, 'shape': item_shape}
                variants[identity]['count'] += 1
            return {'type': 'array', 'count': len(value), 'inspected_count': min(256, len(value)),
                    'item_shapes': list(variants.values())}
        return {'type': 'unsupported'}

    structure = shape(frame)
    return {'schema_version': 1, 'values_omitted': True, 'structure': structure,
            'complete': not bounded[0]}


def _catalog_projection_checks(operation, frame):
    """Fixed per-record rejection codes for the two native schema failures."""
    issues = []
    if type(frame) is not dict:
        return [{'location': 'frame', 'reason': 'expected_object'}]
    if operation == 'apply_list':
        groups = (('list', frame.get('list')),)
    else:
        groups = tuple((key, _backup_group_rows(frame.get(key))) for key in
                       ('run_list', 'wait_list', 'done_list'))
    seen, application_ids = set(), {}
    if operation == 'apply_list' and type(frame.get('list')) is list:
        for row in frame['list'][:256]:
            identifier = _number(row.get('apply_id')) if type(row) is dict else None
            if identifier is not None and 1 <= identifier <= 2 ** 31 - 1:
                application_ids[identifier] = application_ids.get(identifier, 0) + 1
    for group, rows in groups:
        if type(rows) is not list:
            issues.append({'location': group, 'reason': 'expected_array'})
            continue
        if len(rows) > 256:
            issues.append({'location': group, 'reason': 'array_limit'})
        for index, row in enumerate(rows[:256]):
            location = {'location': group, 'row_index': index}
            if type(row) is not dict:
                issues.append({**location, 'reason': 'expected_object'})
                continue
            field = 'apply_id' if operation == 'apply_list' else 'task_id'
            identifier = _number(row.get(field))
            if identifier is None or identifier == 0 or operation == 'apply_list' and identifier > 2 ** 31 - 1:
                issues.append({**location, 'field': field, 'reason': 'invalid_object_id'})
            elif identifier in seen or operation == 'apply_list' and application_ids.get(identifier, 0) > 1:
                issues.append({**location, 'field': field, 'reason': 'duplicate_object_id'})
            else:
                seen.add(identifier)
            if operation == 'apply_list':
                if _application_display_name(row) is None:
                    issues.append({**location, 'field': 'module_name', 'reason': 'invalid_label'})
            else:
                if type(row.get('user')) is not int or row['user'] != OWNER_ID:
                    issues.append({**location, 'field': 'user', 'reason': 'owner_identity_unverified'})
                for field in ('atid', 'from', 'to', 'disk_uuid'):
                    value = row.get(field)
                    if value is not None and (type(value) is not str or len(value.encode('utf8', errors='replace')) > 4096):
                        issues.append({**location, 'field': field, 'reason': 'invalid_private_identity_shape'})
    return issues


def _backup_group_rows(value):
    """Build 260907 wraps each backup group in its fixed list_cm object.

    Bare arrays are retained for the existing documented fixture/older shape;
    no generic data/result wrapper or unknown owner normalization is allowed.
    """
    return value.get('list_cm') if type(value) is dict else value


def _backup_tasks(frame):
    groups, counts, observed, all_seen = {}, {}, 0, set()
    for source, target in (('run_list', 'running'), ('wait_list', 'waiting'), ('done_list', 'done')):
        raw = frame.get(source)
        if raw is None:
            groups[target], counts[target] = None, None
            continue
        value = _backup_group_rows(raw)
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
    if tool == 'list_native_modules':
        return _native_modules(frame)
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

    def __init__(self, root='/', *, client=None, idempotency_store=None, diagnostic=False):
        self.client = client or VendorBusinessClient(root=root)
        self._idempotency = idempotency_store or _STORE
        self._diagnostic = diagnostic is True

    def _installation(self, tool, arguments, principal, authorizer, result):
        """Finite plan/revalidate/install; native ACK is never task completion."""
        mutation = tool == 'install_native_application'
        pending, responded, attempted = None, False, False
        operation = _OPERATIONS[tool]
        result.update(may_contact_vendor=True, mutation_outcome='not_sent' if mutation else None)
        try:
            if type(arguments) is not dict or set(arguments) != set(_SPECS[tool]['inputSchema']['required']):
                raise BusinessTransportError('invalid_parameters')
            identifier = arguments.get('application_id')
            if type(identifier) is not int or not 1 <= identifier <= 2 ** 31 - 1:
                raise BusinessTransportError('invalid_parameters')
            if mutation and (arguments.get('confirmed') is not True
                    or type(arguments.get('expected_version')) is not str
                    or re.fullmatch(r'app_install_[0-9a-f]{32}', arguments['expected_version']) is None):
                raise BusinessTransportError('invalid_parameters')
            if mutation and (type(arguments.get('idempotency_key')) is not str or
                    re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', arguments['idempotency_key']) is None):
                raise BusinessTransportError('idempotency_required')
            if (type(principal) is not dict or type(principal.get('key_id')) is not str
                    or re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', principal['key_id']) is None
                    or any(type(principal.get(key)) is not int or not 1 <= principal[key] <= 2 ** 53 - 1
                           for key in ('grant_revision', 'credential_generation'))):
                raise BusinessTransportError('authorization_denied')
            def authorize_install(native_operation, parameters, body):
                _authorize(authorizer, native_operation, parameters, body)
                if identifier in _INSTALL_ALLOWLIST and identifier not in _installation_resources(principal):
                    raise BusinessTransportError('authorization_denied')
                return True

            authorize_install(operation, {}, {})
            if mutation:
                digest = hashlib.sha256(json.dumps({'tool': tool, 'arguments': arguments},
                    sort_keys=True, separators=(',', ':')).encode('utf8')).hexdigest()
                pending, replay = self._idempotency.begin(principal, arguments['idempotency_key'], digest)
                if replay is not None:
                    pending = None
                    authorize_install(operation, {}, {})
                    return replay
                result['idempotent_replay'] = False
            if identifier not in _INSTALL_ALLOWLIST:
                if mutation:
                    raise BusinessTransportError('application_not_allowlisted')
                result.update(available=True, status='ok', data={'application_id': identifier,
                    'allowed': False, 'reason': 'application_not_allowlisted', 'expected_version': None})
                return result
            with _operation_lock():
                def authorize_catalog(_operation, _params, _body):
                    authorize_install(operation, {}, {})
                    return True
                frames = {}
                for query in ('apply_list', 'module_inventory'):
                    status, frame = self.client.request(query, {}, {}, authorize_catalog)
                    authorize_install(operation, {}, {})
                    if status != 200 or _native_code(query, frame) not in (None, 0):
                        raise BusinessTransportError('invalid_json')
                    frames[query] = frame
                candidate, reason = _installation_candidate(identifier, frames)
                if reason is not None:
                    authorize_install(operation, {}, {})
                    if mutation:
                        raise BusinessTransportError(reason)
                    result.update(available=True, status='ok', data={'application_id': identifier,
                        'allowed': False, 'reason': reason, 'expected_version': None})
                    return result
                material = {'candidate': candidate, 'application_ids': _installation_resources(principal),
                    'owner': {key: principal.get(key) for key in
                    ('key_id', 'grant_revision', 'credential_generation', '_service_generation')}}
                version = 'app_install_' + hashlib.sha256(json.dumps(material, sort_keys=True,
                    ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()[:32]
                public = {'application_id': identifier, 'name': candidate['body']['module_name'],
                          'target_version': candidate['body']['version'], 'expected_version': version,
                          'allowed': True, 'operation': 'new_install_only', 'reason': None,
                          'resource_effects': _json_copy(_INSTALL_ALLOWLIST[identifier]['resource_effects']),
                          'package_selection': 'factory_current_module_id',
                          'version_is_observed_not_final_attestation': True,
                          'effect_summary': _INSTALL_ALLOWLIST[identifier]['effect_summary']}
                authorize_install(operation, {}, {})
                if not mutation:
                    result.update(available=True, status='ok', data=public)
                    return result
                if arguments['expected_version'] != version:
                    raise BusinessTransportError('installation_plan_changed')
                params, body = _validate('application_install', {}, candidate['body'])
                authorize_install(operation, params, body)
                attempted = True
                status, frame = self.client.request(operation, params, body, authorize_install, confirmed=True)
                responded = True
                code = _native_code(operation, frame)
                result['native'] = {'http_status': status, 'code': code}
                if status != 200 or code not in (None, 0):
                    authorize_install(operation, params, body)
                    result.update(status='native_error', reason='native_request_rejected', mutation_outcome='rejected')
                    return result
                # Audited appcenter success may omit code, but must echo both
                # exact object fields. No current-id or synthetic task is ACK.
                if (type(frame.get('apply_id')) is not int or frame['apply_id'] != identifier
                        or frame.get('module_name') != candidate['body']['module_name']):
                    raise BusinessTransportError('invalid_json')
                authorize_install(operation, params, body)
                result.update(available=True, status='ok', mutation_outcome='acknowledged', data={
                    'application_id': identifier, 'name': candidate['body']['module_name'],
                    'target_version': candidate['body']['version'], 'accepted': True,
                    'effect_confirmed': False, 'factory_acknowledgement_only': True,
                    'native_task_id': None, 'next_step': 'get_application_install_progress'})
        except BusinessTransportError as error:
            result['reason'] = error.reason
            if mutation:
                unknown = responded or error.request_sent
                result['mutation_outcome'] = 'unknown' if unknown else 'not_sent'
                if unknown:
                    result.update(reason='result_unconfirmed', failure_reason=error.reason)
        except Exception:
            result['reason'] = 'transport_failed'
            if mutation and attempted:
                # An unknown adapter failure must not authorize retrying a write.
                result.update(reason='result_unconfirmed', mutation_outcome='unknown')
        finally:
            if pending is not None:
                self._idempotency.finish(pending, result)
        return result

    def execute(self, tool, arguments=None, *, principal=None, authorizer=None):
        result = {'schema_version': 1, 'available': False, 'status': 'unavailable',
                  'reason': None, 'data': None, 'native': {'http_status': None, 'code': None},
                  'sampled_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                  'native_query_may_update_state': True, 'credential_read_only': True,
                  'mutation_outcome': None}
        if tool == 'list_native_modules':
            result['may_contact_vendor'] = True
        pending, responded, mutation = None, False, tool in _WRITES if type(tool) is str else False
        try:
            arguments = {} if arguments is None else arguments
            if type(tool) is str and tool in _INSTALL_TOOLS:
                return self._installation(tool, arguments, principal, authorizer, result)
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
                        found = any(row['application_id'] == arguments['application_id']
                                    and row.get('name_source') != 'audited_module_registration' for row in items)
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
                if self._diagnostic and operation in ('apply_list', 'backup_tasks_list'):
                    result['response_structure'] = _native_response_structure(frame)
                    result['projection_issues'] = _catalog_projection_checks(operation, frame)
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
            if error.reason == 'authorization_denied':
                result.pop('response_structure', None)
                result.pop('projection_issues', None)
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
    backend = NativeCapabilities(root, client=client, diagnostic=True)
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
              'response_structure': None, 'projection_issues': None,
              'catalog_projection_valid': False,
              'native_query_may_update_state': True, 'may_wake_disks': True,
              'trusted_package_manifest_verified': False,
              'package_execution_available': False,
              'catalog_is_installed_inventory': False,
              'protocol': {'catalog_action': 'get_apply_list', 'opttype': 0,
                           'source_is_native_integer_enum': True,
                           'install_flag': 0, 'update_flag': 1,
                           'native_worker_is_asynchronous': True,
                           'independent_signature_requirement': False,
                           'pending_reason': PENDING['install_other_applications']}}
    try:
        _authorize(authorizer, operation, params, body)
        native = client or VendorBusinessClient(root=root)
        status, frame = native.request(operation, params, body, authorizer)
        _authorize(authorizer, operation, params, body)
        structure = _native_response_structure(frame)
        issues = _catalog_projection_checks(operation, frame)
        result.update(response_structure=structure, projection_issues=issues)
        code = _native_code(operation, frame)
        if status != 200 or code not in (None, 0):
            result['reason'] = 'native_request_rejected'
            return result
        rows = frame.get('list')
        if type(rows) is not list or len(rows) > 256:
            _authorize(authorizer, operation, params, body)
            result.update(status='diagnostic_only', reason='catalog_projection_rejected',
                          response_structure=structure, projection_issues=issues)
            return result
        try:
            catalog = _applications(frame)
            projection_valid = not catalog['partial']
        except BusinessTransportError:
            projection_valid = False
        shapes = []
        for index, row in enumerate(rows):
            if type(row) is not dict:
                continue
            identifier = _number(row.get('apply_id'))
            identifier = identifier if identifier is not None and 1 <= identifier < 2 ** 31 else None
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
            shapes.append({'row_index': index, 'application_id': identifier,
                           'name': _application_display_name(row),
                           'version': _label(row.get('version'), 63), 'field_shapes': fields,
                           'enum_fields': enums, 'url_fields': urls,
                           'checksum_fields': checksums,
                           'trusted_source_verified': False, 'signature_verified': False})
        _authorize(authorizer, operation, params, body)
        result.update(available=True, status='ok' if projection_valid else 'diagnostic_only', items=shapes,
                      reason=None if projection_valid else 'catalog_projection_rejected',
                      catalog_projection_valid=projection_valid,
                      response_structure=structure, projection_issues=issues)
    except BusinessTransportError as error:
        result['reason'] = error.reason
        if error.reason == 'authorization_denied':
            result.update(items=None, response_structure=None, projection_issues=None,
                          catalog_projection_valid=False)
    except Exception:
        result['reason'] = 'transport_failed'
    return result

"""Describe eligible storage roots using mount metadata and cached disk identity.

This is an authorization planner, not a filesystem browser. It never stats a
data path, enumerates a directory, reads file content or starts a SMART query.
Future file operations must revalidate live identity, ACLs and opened handles;
this metadata alone is not proof that a path is safe to open.
"""

import hashlib
import json
from pathlib import Path
import re


_MAX_MOUNTINFO = 262144
_MAX_ROOTS = 128
_DEVICE = re.compile(r'/dev/((?:sd[a-z]+|vd[a-z]+|xvd[a-z]+))(?:[0-9]+)?\Z')
_NVME = re.compile(r'/dev/(nvme[0-9]+n[0-9]+)(?:p[0-9]+)?\Z')
_FILESYSTEMS = frozenset(('ext2', 'ext3', 'ext4', 'btrfs', 'xfs', 'ntfs',
                          'ntfs3', 'fuseblk', 'exfat', 'vfat', 'f2fs', 'jfs'))
_ESCAPE = re.compile(r'\\(040|011|012|134)')
_ROOT_ID = re.compile(r'root_[0-9a-f]{24}\Z', re.ASCII)
_BOUNDARY_VERSION = 3
_MAX_TOPOLOGY_EVIDENCE = 512
_MAX_CACHE_METADATA = 1048576
_KNOWN_FILESYSTEMS = _FILESYSTEMS | frozenset((
    'tmpfs', 'devtmpfs', 'overlay', 'squashfs', 'proc', 'sysfs', 'nsfs',
    'cgroup', 'cgroup2', 'fuse', 'fusectl', 'tracefs', 'securityfs', 'debugfs',
    'bpf', 'autofs', 'efivarfs', 'pstore', 'mqueue', 'devpts', 'hugetlbfs',
    'rpc_pipefs', 'binfmt_misc', 'nfs', 'nfs4', 'cifs', 'iso9660', 'udf'))
_OTHER_BLOCK_NAME = re.compile(
    r'/dev/(?:mmcblk[0-9]+(?:p[0-9]+)?|loop[0-9]+|dm-[0-9]+|md[0-9]+)\Z', re.ASCII)
_INPUT_ERRORS = (OSError, ValueError, TypeError, AttributeError, UnicodeError)


def _plain(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and value == value.strip() and all(c.isprintable() for c in value))


def _unescape(value):
    return _ESCAPE.sub(lambda match: chr(int(match[1], 8)), value)


def _data_path(value):
    """Only a directly mounted /drives/<disk> may supply an admin root."""
    return (_plain(value, 1024) and value.startswith('/drives/')
            and '\\' not in value and not value.endswith('/')
            and len(value.split('/')) == 3
            and all(part not in ('', '.', '..') for part in value.split('/')[1:]))


def is_users_root_path(value):
    """Legacy/native Users metadata only; never validate a public file grant."""
    return (isinstance(value, str) and value.endswith('/Users')
            and _data_path(value[:-6]))


def is_admin_root_path(value):
    """Recognize the exact v3 admin boundary without resolving any data path."""
    return (isinstance(value, str) and value.endswith('/Users/admin')
            and _data_path(value[:-12]))


def physical_mount_path(admin_root):
    """Return the canonical physical mount for a v3 root; reject other roots."""
    if not is_admin_root_path(admin_root):
        raise ValueError('admin_root_invalid')
    return admin_root[:-12]


def is_private_recovery_path(value):
    """Reserve every custody component, including moved admin and name variants."""
    return (isinstance(value, str)
            and any(part.casefold() == '.r1-toolbox-ops' for part in value.split('/')))


def valid_relative_file_path(value):
    """Accept literal POSIX paths below a selected admin root, without decoding."""
    if (not isinstance(value, str) or len(value) > 1024 or '\\' in value
            or any(not char.isprintable() for char in value)
            or (value and any(part in ('', '.', '..') for part in value.split('/')))):
        return False
    try:
        return len(value.encode('utf-8', 'strict')) <= 4096
    except UnicodeError:
        return False


def _empty(status='unavailable', reason='尚无已核验的数据盘目录；未浏览文件或唤醒硬盘。'):
    return {'available': False, 'status': status, 'reason': reason, 'roots': [],
            'boundary_version': _BOUNDARY_VERSION}


def _read_mountinfo(panel):
    """Read bounded proc metadata through EOF; a short read is not an EOF."""
    raw = bytearray()
    eof, status = False, 'unavailable'
    try:
        with (Path(panel.proc_root) / 'self/mountinfo').open('rb') as stream:
            status = 'read_failed'
            while len(raw) <= _MAX_MOUNTINFO:
                part = stream.read(min(65536, _MAX_MOUNTINFO + 1 - len(raw)))
                if not isinstance(part, bytes):
                    raise TypeError('invalid proc read')
                if not part:
                    eof, status = True, 'ok'
                    break
                raw.extend(part)
                if len(raw) > _MAX_MOUNTINFO:
                    status = 'limit_exceeded'
                    break
    except _INPUT_ERRORS:
        pass
    return bytes(raw), status, eof


def _parse_mountinfo(text):
    """Keep all valid rows, including hidden/Users rows, for both projections."""
    mounts, full_sources, full_devices = {}, {}, {}
    parsed, malformed = 0, 0
    for line in text.splitlines():
        fields = line.split(' - ', 1)
        if len(fields) != 2:
            malformed += 1
            continue
        before, after = fields[0].split(), fields[1].split()
        if (len(before) < 6 or len(after) < 3
                or not before[0].isdigit() or not before[1].isdigit()
                or not re.fullmatch(r'[0-9]+:[0-9]+', before[2])):
            malformed += 1
            continue
        mount = _unescape(before[4])
        if not mount.startswith('/'):
            malformed += 1
            continue
        parsed += 1
        mounts.setdefault(mount, []).append((before, after))
        source = _unescape(after[1])
        # Complete canonical aliases alone affect eligibility. External aliases
        # cannot be grants or revoke one. Child mounts remain diagnostic evidence;
        # the real opener rejects entering a different mount, not this planner.
        if before[3] == '/' and _data_path(mount):
            full_sources.setdefault(source, []).append(mount)
            full_devices.setdefault(before[2], []).append(mount)
    return mounts, full_sources, full_devices, parsed, malformed


def _cache_fingerprint(disks):
    """Compare consumed cache fields/record structure, ignoring live telemetry."""
    digest, size = hashlib.sha256(), 0
    try:
        def kind(value):
            for cls, label in ((type(None), 'null'), (bool, 'bool'), (int, 'int'),
                               (float, 'float'), (str, 'str'), (list, 'list'), (dict, 'dict')):
                if type(value) is cls:
                    return label
            return 'other'

        # Preserve record order, invalid-record sentinels, field presence/type
        # and every value used by qualification, labels or cache diagnostics.
        # Temperature/SMART timestamps/capacity/I/O are not topology inputs.
        compared = []
        for disk in disks:
            if not isinstance(disk, dict):
                compared.append(['invalid_record', kind(disk)])
                continue
            compared.append(['disk', [[field, field in disk, kind(disk.get(field)), disk.get(field)]
                                     for field in ('name', 'serial', 'mounts', 'user_name', 'volume')]])
        encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=True,
                                   separators=(',', ':'), allow_nan=False)
        for piece in encoder.iterencode(compared):
            raw = piece.encode('ascii')
            size += len(raw)
            if size > _MAX_CACHE_METADATA:
                return None, 'cached_disks_metadata_limit_exceeded'
            digest.update(raw)
        return digest.hexdigest(), None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None, 'cached_disks_metadata_invalid'


def _plan_inputs(panel):
    """Sample only proc metadata and the existing private identity cache."""
    raw, status, eof = _read_mountinfo(panel)
    mount_report = {'status': status, 'eof': eof, 'bytes_read': len(raw),
                    'parsed_records': 0, 'malformed_records': 0}
    cache_report = {'status': 'not_collected', 'records': None,
                    'valid_identity_records': None, 'invalid_identity_records': None}
    errors = []
    if status != 'ok':
        errors.append('mountinfo_' + status)
    try:
        text = raw.decode('utf-8', 'strict')
    except UnicodeError:
        text = raw.decode('utf-8', 'replace')
        if status == 'ok':
            mount_report['status'] = 'invalid_encoding'
        errors.append('mountinfo_invalid_encoding')
    # An incomplete final line is not evidence of a complete mount record.
    if not eof and text and not text.endswith('\n'):
        text = text.rsplit('\n', 1)[0] if '\n' in text else ''
    mounts, sources, devices, parsed, malformed = _parse_mountinfo(text)
    mount_report.update(parsed_records=parsed, malformed_records=malformed)
    if malformed:
        errors.append('mountinfo_malformed')

    identities, serials, observed, cached_mounts = {}, {}, {}, {}
    cache_fingerprint = None
    disks, cache_failed = None, True
    # Preserve the catalog's original input order: oversized/unreadable proc
    # metadata must not cause a cache acquisition that the old planner skipped.
    if status == 'ok' and 'mountinfo_invalid_encoding' not in errors:
        try:
            snapshot = panel.overview(include_identity=True)
            disks = snapshot.get('disks_detail') if isinstance(snapshot, dict) else None
            if not isinstance(disks, list):
                cache_report['status'] = 'invalid'
            elif len(disks) > _MAX_ROOTS:
                cache_report.update(status='limit_exceeded', records=len(disks))
            else:
                cache_report.update(status='ok', records=len(disks),
                                    valid_identity_records=0, invalid_identity_records=0)
                cache_failed = False
                cache_fingerprint, fingerprint_error = _cache_fingerprint(disks)
                if fingerprint_error:
                    errors.append(fingerprint_error)
                for disk in disks:
                    if not isinstance(disk, dict):
                        cache_report['invalid_identity_records'] += 1
                        cache_report['status'] = 'partial'
                        continue
                    name, serial, values = disk.get('name'), disk.get('serial'), disk.get('mounts')
                    if _plain(name, 64):
                        observed[name] = observed.get(name, 0) + 1
                    if (not isinstance(values, list) or len(values) > _MAX_ROOTS
                            or any(not isinstance(value, str) for value in values)):
                        cache_report['status'] = 'partial'
                    if isinstance(values, list) and len(values) <= _MAX_ROOTS:
                        for value in values:
                            if isinstance(value, str):
                                path = _unescape(value)
                                if _data_path(path):
                                    cached_mounts.setdefault(path, []).append(disk)
                    if (not _plain(name, 64) or not _plain(serial, 120)
                            or not isinstance(values, list) or len(values) > _MAX_ROOTS):
                        cache_report['invalid_identity_records'] += 1
                        continue
                    identities.setdefault(name, []).append(disk)
                    serials.setdefault(serial, []).append(name)
                    cache_report['valid_identity_records'] += 1
        except _INPUT_ERRORS:
            cache_failed = True
            cache_report.update(status='unavailable', records=None,
                                valid_identity_records=None, invalid_identity_records=None)
        if cache_report['status'] != 'ok':
            errors.append('cached_disks_' + cache_report['status'])
    else:
        errors.append('cached_disks_not_collected')
    return {'mounts': mounts, 'sources': sources, 'devices': devices,
            'identities': identities, 'serials': serials, 'observed': observed,
            'cached_mounts': cached_mounts,
            'mountinfo_fingerprint': hashlib.sha256(raw).hexdigest(),
            'cached_disks_fingerprint': cache_fingerprint,
            'mountinfo': mount_report, 'cached_disks': cache_report,
            'cache_failed': cache_failed, 'errors': errors}


def _qualification(state, mount, entries):
    """The single eligibility predicate used by catalog and diagnostic output."""
    common, details = [], []
    if not _data_path(mount):
        common.append('candidate_path_invalid')
    if not entries:
        common.append('mountinfo_not_present')
    elif len(entries) != 1:
        common.append('candidate_mount_stacked')
    for ancestor, code in (('/', 'root_ancestor_stacked'), ('/drives', 'drives_ancestor_stacked')):
        if len(state['mounts'].get(ancestor, [])) > 1:
            common.append(code)
    users = mount + '/Users'
    children = [(path, rows) for path, rows in state['mounts'].items()
                if path == users or path.startswith(users + '/')]
    for before, after in entries:
        source, filesystem = _unescape(after[1]), after[0]
        match = _DEVICE.fullmatch(source) or _NVME.fullmatch(source)
        mount_flags = set(before[5].split(',')) & {'ro', 'rw'}
        super_flags = set(after[2].split(',')) & {'ro', 'rw'}
        reasons = []
        for failed, code in (
                (not match, 'source_unsupported'),
                (filesystem not in _FILESYSTEMS, 'filesystem_unsupported'),
                (before[3] != '/', 'filesystem_root_not_full'),
                (len(state['sources'].get(source, [])) != 1, 'canonical_source_not_unique'),
                (len(state['devices'].get(before[2], [])) != 1, 'canonical_device_not_unique'),
                (len(mount_flags) != 1, 'mount_flags_invalid'),
                (len(super_flags) != 1, 'super_flags_invalid')):
            if failed:
                reasons.append(code)
        candidates = state['identities'].get(match[1], []) if match else []
        cache_known = not state['cache_failed']
        cache = {'observed_device_records': (state['observed'].get(match[1], 0) if match else 0) if cache_known else None,
                 'valid_identity_records': len(candidates) if cache_known else None,
                 'identity_available': len(candidates) == 1 if cache_known else None,
                 'serial_unique': None, 'mount_matches': None}
        disk = candidates[0] if len(candidates) == 1 else None
        if match:
            if not cache_known:
                reasons.append('cached_device_identity_unavailable')
            elif not candidates:
                reasons.append('cached_device_identity_missing')
            elif len(candidates) != 1:
                reasons.append('cached_device_identity_ambiguous')
        if disk is not None and cache_known:
            cache['serial_unique'] = len(state['serials'][disk['serial']]) == 1
            cache['mount_matches'] = mount in [_unescape(value) for value in disk['mounts']
                                               if isinstance(value, str)]
            if not cache['serial_unique']:
                reasons.append('cached_serial_not_unique')
            if not cache['mount_matches']:
                reasons.append('cached_mount_unmatched')
        details.append({'before': before, 'after': after, 'source': source,
                        'mount_flags': mount_flags, 'super_flags': super_flags,
                        'cache': cache, 'disk': disk, 'reasons': reasons})
    reasons = list(dict.fromkeys(common + [code for row in details for code in row['reasons']]))
    root = None
    if not reasons:
        detail = details[0]
        disk, before, after = detail['disk'], detail['before'], detail['after']
        admin = users + '/admin'
        identity = ['r1-file-admin-root-v3', disk['serial'], detail['source'], before[2], after[0], admin]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True,
                                           separators=(',', ':')).encode('ascii')).hexdigest()
        label = disk.get('user_name')
        if not _plain(label, 128):
            label = disk.get('volume')
        if not _plain(label, 128):
            label = mount.rsplit('/', 1)[-1]
        root = {'id': 'root_' + digest[:24], 'label': label, 'mount': admin,
                'read_only': 'ro' in detail['mount_flags'] or 'ro' in detail['super_flags']}
    return {'mount': mount, 'details': details, 'children': children, 'reasons': reasons, 'root': root}


def _analyze_plan(panel):
    state = _plan_inputs(panel)
    # Include malformed lexical direct candidates in diagnostics, but never in
    # grants. Nested mount paths are evidence attached to their canonical root.
    paths = {mount for mount in state['mounts']
             if mount.startswith('/drives/') and len(mount.split('/')) == 3}
    paths.update(state['cached_mounts'])
    candidates = [_qualification(state, mount, state['mounts'].get(mount, []))
                  for mount in sorted(paths)]
    report = state['mountinfo']
    if report['status'] == 'limit_exceeded':
        catalog = _empty('limit_exceeded', '挂载信息超过授权规划上限。')
    elif report['status'] != 'ok' or state['cache_failed']:
        catalog = _empty()
    elif report['malformed_records']:
        catalog = _empty(reason='挂载信息格式无效；未确认文件范围。')
    else:
        roots = [candidate['root'] for candidate in candidates if candidate['root'] is not None]
        if len(roots) > _MAX_ROOTS:
            catalog = _empty('limit_exceeded', '数据盘目录超过授权规划上限。')
            state['errors'].append('catalog_limit_exceeded')
        elif not roots:
            catalog = _empty()
        else:
            catalog = {'available': True, 'status': 'authorization_plan', 'roots': roots,
                       'boundary_version': _BOUNDARY_VERSION,
                       'reason': '仅规划数据盘 Users/admin 子目录；实际访问仍逐级核验原厂权限、挂载身份与真实句柄，拒绝跨入子挂载。'}
    return state, candidates, catalog


def collect_file_roots(panel):
    """Plan admin grants using shared metadata qualification; never open data.

    Physical identity, source, device, filesystem and the v3 admin boundary bind
    IDs independently of legacy whole-disk/Users grants. Child mounts do not
    reject the entire root and never prove an actual path safe to open.
    """
    return _analyze_plan(panel)[2]


def _mount_fingerprint(path):
    """Full SHA256 of the exact UTF-8 unescaped physical mount path, no /Users."""
    return hashlib.sha256(path.encode('utf-8')).hexdigest()


def _source_projection(source):
    match = _DEVICE.fullmatch(source) or _NVME.fullmatch(source)
    if match and len(source) <= 69:
        return {'class': 'supported_block_name', 'device_name': source[5:]}
    if len(source) <= 69 and _OTHER_BLOCK_NAME.fullmatch(source):
        return {'class': 'unsupported_block_name', 'device_name': source[5:]}
    if source.startswith('/dev/'):
        return {'class': 'device_alias', 'device_name': None}
    return {'class': 'virtual_or_unknown', 'device_name': None}


def _entry_projection(before, after, errors):
    # Whitelist names; arbitrary source/filesystem text may contain identifiers.
    filesystem = after[0] if after[0] in _KNOWN_FILESYSTEMS else None
    ids = []
    for value in before[:2]:
        if re.fullmatch(r'[0-9]{1,24}', value, re.ASCII):
            ids.append(int(value))
        else:
            ids.append(None)
            errors.append('mountinfo_projection_invalid')
    device = before[2] if re.fullmatch(r'[0-9]{1,24}:[0-9]{1,24}', before[2], re.ASCII) else None
    if device is None:
        errors.append('mountinfo_projection_invalid')
    return {'mount_id': ids[0], 'parent_mount_id': ids[1], 'device': device,
            'filesystem': filesystem,
            'filesystem_class': 'supported' if after[0] in _FILESYSTEMS else 'unsupported',
            'filesystem_root': 'full' if before[3] == '/' else 'subdirectory',
            'source': _source_projection(_unescape(after[1]))}


def collect_file_root_topology(panel):
    """Return bounded, sanitized reasons for every canonical root candidate.

    ``complete`` describes acquisition, not eligibility or Users existence. Two
    bounded topology-input samples must agree, including rejected mounts and
    consumed cache fields/record structure, but excluding unrelated telemetry.
    ``qualification_passed`` describes observed local checks (null if unknown);
    ``eligible`` is null for incomplete inputs. ``catalog_included`` is the
    actual unchanged planner decision, even when diagnostics expose a partial
    cache that the existing planner could safely skip.
    ``mount_fingerprint`` is SHA256(exact unescaped mount.encode('utf-8'));
    candidate_id is a separate non-grant prefix plus its first 24 hex digits.
    The catalog fingerprint is SHA256 of ASCII canonical JSON with sort_keys,
    ensure_ascii, separators=(',', ':') and allow_nan=False. IDs retain catalog
    mount order. This function never probes a data path or performs native I/O.
    """
    state, candidates, catalog = _analyze_plan(panel)
    errors = list(state['errors'])
    included = {row['mount']: row['id'] for row in catalog['roots']}
    budget = [_MAX_TOPOLOGY_EVIDENCE]

    def bounded(values):
        count = min(len(values), budget[0])
        budget[0] -= count
        if count != len(values):
            errors.append('topology_limit_exceeded')
        return values[:count]

    def aliases(paths):
        return {'count': len(paths), 'mount_fingerprints': [_mount_fingerprint(path)
                                                         for path in bounded(paths)]}

    def cache_associations(mount):
        if state['cache_failed']:
            return {'records': None, 'items': []}
        disks = state['cached_mounts'].get(mount, [])
        rows = []
        for disk in bounded(disks):
            name, serial = disk.get('name'), disk.get('serial')
            valid = (_plain(name, 64) and _plain(serial, 120)
                     and isinstance(disk.get('mounts'), list) and len(disk['mounts']) <= _MAX_ROOTS)
            source = _source_projection('/dev/' + name) if _plain(name, 64) else {'device_name': None}
            rows.append({'device_name': source['device_name'], 'identity_available': valid,
                         'device_identity_unique': len(state['identities'].get(name, [])) == 1 if valid else None,
                         'serial_unique': len(state['serials'].get(serial, [])) == 1 if valid else None,
                         'mount_matches': True})
        return {'records': len(disks), 'items': rows}

    output = []
    if len(candidates) > _MAX_ROOTS:
        errors.append('topology_limit_exceeded')
    for candidate in candidates[:_MAX_ROOTS]:
        mount = candidate['mount']
        fingerprint = _mount_fingerprint(mount)
        entries = []
        for detail in bounded(candidate['details']):
            before, after = detail['before'], detail['after']
            row = _entry_projection(before, after, errors)
            row.update(canonical_source_aliases=aliases(state['sources'].get(detail['source'], [])),
                       canonical_device_aliases=aliases(state['devices'].get(before[2], [])),
                       cache=detail['cache'], reason_codes=detail['reasons'])
            entries.append(row)
        children = []
        child_count = sum(len(rows) for unused, rows in candidate['children'])
        for path, rows in sorted(candidate['children']):
            for before, after in bounded(rows):
                row = _entry_projection(before, after, errors)
                # No fsroot/source spelling or dynamic Users path is returned.
                children.append({'mount_fingerprint': _mount_fingerprint(path),
                                 'class': 'users_root' if path == mount + '/Users' else 'users_descendant',
                                 'mount_id': row['mount_id'], 'parent_mount_id': row['parent_mount_id'],
                                 'device': row['device'], 'filesystem': row['filesystem'],
                                 'filesystem_class': row['filesystem_class']})
        root_id = included.get(mount + '/Users/admin')
        reason_codes = list(dict.fromkeys(candidate['reasons'] + state['errors']))
        local_rejections = [code for code in candidate['reasons']
                            if code != 'cached_device_identity_unavailable']
        qualification = (False if local_rejections else None) if state['cache_failed'] else not candidate['reasons']
        output.append({'candidate_id': 'candidate_' + fingerprint[:24],
                       'mount_fingerprint': fingerprint, 'root_id': root_id,
                       'origin': ('mountinfo_and_cache' if mount in state['cached_mounts'] else 'mountinfo')
                                 if candidate['details'] else 'cache_only',
                       'cached_mount_associations': cache_associations(mount),
                       'qualification_passed': qualification,
                       'eligible': not candidate['reasons'] if not state['errors'] else None,
                       'catalog_included': root_id is not None,
                       'entry_count': len(candidate['details']), 'entries': entries,
                       'users_mount_count': child_count, 'users_mounts': children,
                       'reason_codes': reason_codes})
    # Recheck the complete inputs, not just approved roots: changes to a rejected
    # source, fsroot, options or cached association must not look stable merely
    # because both samples produce the same (possibly empty) catalog.
    after = _plan_inputs(panel)
    errors.extend('recheck_' + code for code in after['errors'])
    mount_verifiable = all(sample['mountinfo']['status'] == 'ok'
                           and sample['mountinfo']['eof'] for sample in (state, after))
    mount_unchanged = (state['mountinfo_fingerprint'] == after['mountinfo_fingerprint']) if mount_verifiable else None
    cache_verifiable = all(sample['cached_disks_fingerprint'] is not None for sample in (state, after))
    cache_unchanged = (state['cached_disks_fingerprint'] == after['cached_disks_fingerprint']) if cache_verifiable else None
    if mount_unchanged is not True:
        errors.append('mountinfo_changed' if mount_unchanged is False else 'mountinfo_stability_unavailable')
    if cache_unchanged is not True:
        errors.append('cached_disks_changed' if cache_unchanged is False else 'cached_disks_stability_unavailable')
    errors = list(dict.fromkeys(errors))
    if errors:
        for candidate in output:
            candidate['eligible'] = None
    fingerprint = hashlib.sha256(json.dumps(catalog, sort_keys=True, ensure_ascii=True,
                                           separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()
    return {'schema_version': 1, 'scope': 'file_root_topology',
            'available': state['mountinfo']['eof'] or state['mountinfo']['bytes_read'] > 0,
            'complete': not errors, 'reason_codes': errors, 'boundary_version': _BOUNDARY_VERSION,
            'catalog_fingerprint': fingerprint,
            'catalog_root_ids': [row['id'] for row in catalog['roots']],
            'inputs': {'mountinfo': state['mountinfo'], 'cached_disks': state['cached_disks'],
                       'verification': {'mountinfo': after['mountinfo'], 'cached_disks': after['cached_disks'],
                                        'mountinfo_unchanged': mount_unchanged, 'cached_disks_unchanged': cache_unchanged}},
            'candidate_count': len(candidates), 'omitted_candidates': max(0, len(candidates) - len(output)),
            'candidates': output,
            'effects': {'metadata_only': True, 'reads_proc_mountinfo': True,
                        'uses_existing_identity_cache': True, 'opens_data_paths': False,
                        'stats_data_paths': False, 'enumerates_user_directories': False,
                        'opens_user_files': False, 'calls_native_actions': False,
                        'starts_smart': False, 'reads_credentials': False,
                        'grants_authorization': False}}


def validate_file_grants(grants, catalog):
    """Check owner-selected root IDs against this request's metadata snapshot."""
    if not isinstance(grants, list) or len(grants) > 32:
        raise ValueError('文件范围格式无效')
    if (not isinstance(catalog, dict) or type(catalog.get('boundary_version')) is not int
            or catalog['boundary_version'] != _BOUNDARY_VERSION
            or catalog.get('available') is not True or catalog.get('status') != 'authorization_plan'
            or not isinstance(catalog.get('roots'), list)
            or not 0 < len(catalog['roots']) <= _MAX_ROOTS):
        raise ValueError('当前无法确认 Users/admin 文件边界，请刷新后选择')
    roots = {}
    for row in catalog['roots']:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not _ROOT_ID.fullmatch(row['id']) or row['id'] in roots
                or not is_admin_root_path(row.get('mount'))
                or type(row.get('read_only')) is not bool):
            raise ValueError('当前无法确认 Users/admin 文件边界，请刷新后选择')
        roots[row['id']] = row
    selected = set()
    for grant in grants:
        if (not isinstance(grant, dict) or set(grant) != {'id', 'path', 'access'}
                or not valid_relative_file_path(grant.get('path'))
                or grant.get('access') not in ('readonly', 'readwrite')):
            raise ValueError('文件范围格式无效')
        if is_private_recovery_path(grant['path']):
            raise ValueError('内部恢复区仅由所有者恢复界面管理，不能授予文件访问')
        root = roots.get(grant['id']) if isinstance(grant.get('id'), str) else None
        if root is None:
            raise ValueError('文件范围对应的数据盘目录当前无法确认，请刷新后选择')
        binding = (grant['id'], grant['path'])
        if binding in selected:
            raise ValueError('文件范围重复')
        selected.add(binding)
        if root['read_only'] and grant.get('access') == 'readwrite':
            raise ValueError('只读挂载不能授予文件写入权限')

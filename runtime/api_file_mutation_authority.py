"""Owner-established admin-root custody for recoverable file changes.

Machine requests can consume a registered directory, never create or adopt one.
The first mkdir is permitted only below a complete root-owned, non-writable,
ACL-free descriptor chain. That trusted parent closes mkdir's missing birth-fd
problem; checking a random name after mkdir in an untrusted parent does not.
"""

from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from api_file_operations import FileAccessError, _descriptor_mount_id
from api_file_provider import NativeOwnerWriteAuthority
from api_file_roots import (collect_file_roots, is_users_root_path, is_admin_root_path,
                            is_private_recovery_path, physical_mount_path)


_DIRECTORY = '.r1-toolbox-ops'
_REGISTRY = 'file-mutation-admin-authorities.json'
_LEGACY_REGISTRY = 'file-mutation-authorities.json'
_LAYOUT = 'admin_custody_v1'
_ROOT = re.compile(r'root_[0-9a-f]{24}\Z', re.ASCII)
_OPERATION = re.compile(r'operation_[0-9a-f]{32}\Z', re.ASCII)
_MAX_STATE = 4 * 1024 ** 2
_RECOVERY_TOOLS = frozenset(('create_directory', 'rename_file', 'move_file', 'copy_file',
                            'prepare_file_overwrite', 'trash_file', 'owner_restore_conflict'))


def _authorized(current):
    try:
        accepted = current() is True
    except Exception:
        accepted = False
    if not accepted:
        raise FileAccessError('invalid_key')


def _stable(info):
    return [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)]


def _same_root(first, second):
    """A display-label change cannot orphan the owner's preserved objects."""
    return (type(first) is dict and type(second) is dict
            and {k: v for k, v in first.items() if k != 'label'}
            == {k: v for k, v in second.items() if k != 'label'})


def _restore_signature(info):
    """Pin bytes and access metadata; only our rename may establish new ctime."""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
            info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _trusted(descriptor, *, private=False):
    """Reject every extended ACL rather than infer its effective write grants."""
    info = os.fstat(descriptor)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            or private and stat.S_IMODE(info.st_mode) != 0o700):
        raise FileAccessError('write_backend_unverified')
    if not hasattr(os, 'getxattr'):
        raise FileAccessError('write_backend_unverified')
    for name in ('system.posix_acl_access', 'system.posix_acl_default'):
        try:
            os.getxattr(descriptor, name)
        except OSError as error:
            if error.errno not in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise FileAccessError('write_backend_unverified') from None
        else:
            raise FileAccessError('write_backend_unverified')
    return info


def _write_at(parent, name, value):
    """Atomic durable private metadata; never accepts a user path component."""
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False,
                     separators=(',', ':')).encode('ascii')
    if len(raw) > _MAX_STATE:
        raise FileAccessError('limit_exceeded')
    temporary = '.state-' + secrets.token_hex(16)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except Exception:
        # Preserve failed private writes for inspection; no user object is
        # permanently removed as a cleanup side effect.
        raise FileAccessError('write_backend_unverified') from None


def _read_at(parent, name, default=None):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=parent)
    except FileNotFoundError:
        return default
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > _MAX_STATE or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise FileAccessError('write_backend_unverified')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(_MAX_STATE + 1)
        if len(raw) > _MAX_STATE:
            raise ValueError()
        return json.loads(raw.decode('ascii'))
    except (ValueError, UnicodeError, OSError):
        raise FileAccessError('write_backend_unverified') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class CustodyStage:
    """Owned descriptor for one operation, below a verified private authority."""

    def __init__(self, authority, row, authority_fd, descriptor, operation_id, *, allow_revoked=False):
        self.authority, self.row = authority, dict(row)
        self.authority_fd, self.fd = authority_fd, descriptor
        self.operation_id = operation_id
        self.identity = _stable(os.fstat(descriptor))
        self.allow_revoked = allow_revoked
        self.revision = authority._check_registered(row, allow_revoked=allow_revoked)['revision']

    def verify(self, current):
        _authorized(current)
        entry = self.authority._check_registered(self.row, allow_revoked=self.allow_revoked)
        if entry['revision'] != self.revision:
            raise FileAccessError('write_backend_unverified')
        self.authority._verify_anchor(self.row, entry)
        _trusted(self.fd, private=True)
        if _stable(os.fstat(self.fd)) != self.identity:
            raise FileAccessError('file_changed')
        named = os.stat(self.operation_id, dir_fd=self.authority_fd, follow_symlinks=False)
        if _stable(named) != self.identity:
            raise FileAccessError('file_changed')

    def record(self, value, *, current):
        self.verify(current)
        _write_at(self.fd, 'manifest.json', value)
        self.verify(current)

    def record_recovery(self, value):
        """Record actual custody even after the requesting token was revoked.

        This only writes trusted private recovery metadata through already-held
        descriptors. It does not authorize another file action or return data.
        """
        _trusted(self.fd, private=True)
        if _stable(os.fstat(self.fd)) != self.identity:
            raise FileAccessError('file_changed')
        _write_at(self.fd, 'manifest.json', value)

    def close(self):
        for name in ('fd', 'authority_fd'):
            value = getattr(self, name, None)
            if value is not None:
                os.close(value)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


class FileMutationAuthority:
    """Private registry; initialize/revoke belong only to owner HTTP routes.

    ``catalog`` reads Toolbox state only. ``initialize`` explicitly touches the
    selected admin root and may wake its disk. Existing unknown directories are
    never adopted, chmodded, cleaned or recursively traversed.
    """

    def __init__(self, panel, *, root='/', state_dir=None, authority=None,
                 collector=None, handle_factory=None):
        self.panel, self.root = panel, root
        self.state_dir = (Path(state_dir) if state_dir is not None else
                          getattr(getattr(panel, 'store', None), 'state_dir', None))
        self._authority = authority or NativeOwnerWriteAuthority(panel, root=root)
        self._collector = collector or (lambda: collect_file_roots(panel))
        self._handle_factory = handle_factory
        self._lock = threading.RLock()
        self._entries = None
        self._closed = False

    @contextmanager
    def _state(self):
        if os.name != 'posix' or self.state_dir is None or not hasattr(os, 'O_NOFOLLOW'):
            raise FileAccessError('write_backend_unverified')
        parent = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(parent)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise FileAccessError('write_backend_unverified')
            yield parent
        finally:
            os.close(parent)

    def _load(self):
        if self._closed:
            raise FileAccessError('write_backend_unverified')
        if self._entries is None:
            with self._state() as parent:
                legacy = _read_at(parent, _LEGACY_REGISTRY, {'version': 1, 'entries': {}})
                current = _read_at(parent, _REGISTRY, {'version': 2, 'entries': {}})
            entries = {}
            for data, version in ((legacy, 1), (current, 2)):
                if (type(data) is not dict or set(data) != {'version', 'entries'}
                        or type(data['version']) is not int or data['version'] != version
                        or type(data['entries']) is not dict or len(data['entries']) > 128):
                    raise FileAccessError('write_backend_unverified')
                fields = {'row', 'identity', 'mount_id', 'enabled', 'revision'}
                if version == 2:
                    fields.add('layout')
                for identifier, entry in data['entries'].items():
                    if (type(identifier) is not str or not _ROOT.fullmatch(identifier)
                            or identifier in entries or type(entry) is not dict or set(entry) != fields
                            or type(entry['row']) is not dict or entry['row'].get('id') != identifier
                            or not (is_admin_root_path(entry['row'].get('mount')) or version == 1
                                    and is_users_root_path(entry['row'].get('mount')))
                            or version == 2 and entry['layout'] != _LAYOUT
                            or type(entry['identity']) is not list or len(entry['identity']) != 3
                            or any(type(v) is not int or v < 0 for v in entry['identity'])
                            or type(entry['mount_id']) is not str or not entry['mount_id'].isdigit()
                            or type(entry['enabled']) is not bool or type(entry['revision']) is not int
                            or entry['revision'] < 1):
                        raise FileAccessError('write_backend_unverified')
                    entries[identifier] = entry
            if len(entries) > 128:
                raise FileAccessError('write_backend_unverified')
            self._entries = entries
        return self._entries

    def _save(self):
        # A new registration must never rewrite the old file, even when its
        # root ID already used the public admin-v3 boundary. Old inode bindings
        # describe Users custody and are not authority over the new location.
        entries = {identifier: entry for identifier, entry in self._entries.items()
                   if entry.get('layout') == _LAYOUT}
        with self._state() as parent:
            _write_at(parent, _REGISTRY, {'version': 2, 'entries': entries})

    @staticmethod
    def _location(*, legacy=False):
        return {'boundary': 'users' if legacy else 'admin', 'relative_path': _DIRECTORY,
                'layout': 'legacy_users_custody_v1' if legacy else _LAYOUT,
                'anchor_component': 'users_root' if legacy else 'admin_root'}

    @staticmethod
    def _public(identifier, entry):
        if is_users_root_path(entry['row'].get('mount')):
            return {'root_id': identifier, 'enabled': False, 'revision': entry['revision'],
                    'status': 'legacy_boundary', 'legacy': True, 'reason': 'legacy_users_boundary_v2',
                    'recovery_location': FileMutationAuthority._location(legacy=True)}
        if entry.get('layout') != _LAYOUT:
            return {'root_id': identifier, 'enabled': False, 'revision': entry['revision'],
                    'status': 'legacy_layout', 'legacy': True, 'reason': 'legacy_recovery_layout',
                    'recovery_location': FileMutationAuthority._location(legacy=True)}
        return {'root_id': identifier, 'enabled': entry['enabled'],
                'revision': entry['revision'], 'status': 'registered' if entry['enabled'] else 'revoked',
                'recovery_location': FileMutationAuthority._location()}

    def _reject_legacy(self, root_id, row=None):
        """Keep both legacy boundaries/layouts untouched; never adopt their IDs."""
        entries = self._load()
        entry = entries.get(root_id)
        if entry is not None and is_users_root_path(entry['row'].get('mount')):
            raise FileAccessError('legacy_users_boundary_v2')
        if entry is not None and entry.get('layout') != _LAYOUT:
            raise FileAccessError('legacy_recovery_layout')
        if row is not None and is_admin_root_path(row.get('mount')):
            users = physical_mount_path(row['mount']) + '/Users'
            for item in entries.values():
                if item.get('layout') != _LAYOUT:
                    if item['row'].get('mount') == users:
                        raise FileAccessError('legacy_users_boundary_v2')
                    if item['row'].get('mount') == row['mount']:
                        raise FileAccessError('legacy_recovery_layout')

    def catalog(self, *, current):
        _authorized(current)
        with self._lock:
            result = [self._public(k, v) for k, v in self._load().items()]
        _authorized(current)
        return {'ok': True, 'authorities': result}

    def preflight(self, root_id, *, current):
        """Explicit owner evidence, including every rejected mode/ACL boundary.

        This never creates, chmods or adopts custody. Unlike catalog it opens
        the selected admin ancestry and can wake that disk. Metadata is useful
        when a factory image uses writable parents, which must stay rejected.
        """
        _authorized(current)
        if type(root_id) is not str or not _ROOT.fullmatch(root_id):
            raise FileAccessError('invalid_arguments')
        result = {'ok': True, 'root_id': root_id, 'ready': False, 'checks': [],
            'recovery_location': self._location(), 'recovery_anchor': None,
            'source_binding': {'native_owner': 'unverified', 'root_metadata_matches': False,
                               'same_mount': False},
            'existing_directory': 'unavailable', 'blockers': [], 'may_wake_disks': True}
        if os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() != 0:
            result['blockers'].append('root_process_required')
        try:
            self._reject_legacy(root_id)
            row = self._authority.authorize(root_id, '.r1-authority-proof', current)
            self._reject_legacy(root_id, row)
            result['source_binding']['native_owner'] = 'verified'
            result['source_binding']['root_metadata_matches'] = True
            with self._handle(row) as opened:
                result['source_binding']['same_mount'] = True
                seen = set()
                for index, held in enumerate(opened.descriptors):
                    descriptor = os.open('/proc/self/fd/' + str(held),
                                         os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                    try:
                        info = os.fstat(descriptor)
                        identity = tuple(_stable(info))
                        if identity in seen:
                            continue
                        seen.add(identity)
                        components = ('namespace_root', 'drives_directory', 'physical_mount',
                                      'users_root', 'admin_root')
                        observed_mount = _descriptor_mount_id(self.root, descriptor)
                        same_mount = (observed_mount == opened.mount_id
                                      and info.st_dev == os.fstat(opened.fd).st_dev) if index >= 2 else None
                        if same_mount is False:
                            result['source_binding']['same_mount'] = False
                        check = {'component': components[index] if index < len(components)
                                 else 'ancestor_' + str(index),
                                 'uid': info.st_uid, 'mode': format(stat.S_IMODE(info.st_mode), '04o'),
                                 'mount_id': observed_mount, 'same_mount': same_mount,
                                 'group_other_writable': bool(stat.S_IMODE(info.st_mode) & 0o022),
                                 'access_acl': 'unavailable', 'default_acl': 'unavailable',
                                 'same_object': _stable(info) == _stable(os.fstat(held)), 'trusted': False}
                        for field, name in (('access_acl', 'system.posix_acl_access'),
                                            ('default_acl', 'system.posix_acl_default')):
                            try:
                                os.getxattr(descriptor, name)
                            except OSError as error:
                                check[field] = ('absent' if error.errno in
                                    (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP) else 'unavailable')
                            else:
                                check[field] = 'present'
                        check['trusted'] = (check['uid'] == 0 and not check['group_other_writable']
                            and check['same_object'] and check['access_acl'] == 'absent'
                            and check['default_acl'] == 'absent' and same_mount is not False)
                        result['checks'].append(check)
                        if check['component'] == 'admin_root':
                            result['recovery_anchor'] = dict(check)
                        if not check['trusted']:
                            result['blockers'].append(check['component'] + '_untrusted_mode_or_acl')
                    finally:
                        os.close(descriptor)
                try:
                    os.stat(_DIRECTORY, dir_fd=opened.fd, follow_symlinks=False)
                except FileNotFoundError:
                    result['existing_directory'] = 'absent'
                else:
                    entry = self._load().get(root_id)
                    if entry is None:
                        result['existing_directory'] = 'unknown'
                        result['blockers'].append('existing_directory_not_registered')
                    else:
                        if not _same_root(entry['row'], row):
                            raise FileAccessError('root_unavailable')
                        self._verify_anchor(row, entry)
                        result['existing_directory'] = 'registered'
            result['ready'] = not result['blockers']
        except FileAccessError as error:
            if error.code == 'invalid_key':
                raise
            result['blockers'].append(error.code)
        except (OSError, AttributeError):
            result['blockers'].append('write_backend_unverified')
        _authorized(current)
        return result

    def _check_registered(self, row, *, allow_revoked=False):
        self._reject_legacy(row.get('id'), row)
        entry = self._load().get(row.get('id'))
        if entry is None or not entry['enabled'] and not allow_revoked or not _same_root(entry['row'], row):
            raise FileAccessError('write_backend_unverified')
        return entry

    def _handle(self, row):
        if self._handle_factory is None:
            from api_file_executor import _Handle
            return _Handle(self.root, row, '', directory=True)
        return self._handle_factory(self.root, row, '', directory=True)

    def _trusted_chain(self, opened):
        for descriptor in opened.descriptors:
            # O_PATH cannot be used with fgetxattr. Reopen our held, inspected
            # kernel fd; never resolve a caller-provided pathname again.
            inspected = os.open('/proc/self/fd/' + str(descriptor),
                                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                if _stable(os.fstat(inspected)) != _stable(os.fstat(descriptor)):
                    raise FileAccessError('file_changed')
                _trusted(inspected)
            finally:
                os.close(inspected)

    def _verify_anchor(self, row, entry):
        with self._handle(row) as opened:
            self._trusted_chain(opened)
            descriptor = os.open(_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY
                | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=opened.fd)
            try:
                if (_stable(_trusted(descriptor, private=True)) != entry['identity']
                        or _descriptor_mount_id(self.root, descriptor) != opened.mount_id):
                    raise FileAccessError('file_changed')
            finally:
                os.close(descriptor)

    def initialize(self, root_id, *, confirmed, current):
        _authorized(current)
        if confirmed is not True or type(root_id) is not str or not _ROOT.fullmatch(root_id):
            raise FileAccessError('invalid_arguments')
        if os.name != 'posix' or os.geteuid() != 0:
            raise FileAccessError('write_backend_unverified')
        with self._lock:
            entries = self._load()
            self._reject_legacy(root_id)
            row = self._authority.authorize(root_id, '.r1-authority-proof', current)
            self._reject_legacy(root_id, row)
            with self._handle(row) as opened:
                self._trusted_chain(opened)
                _authorized(current)
                entry = entries.get(root_id)
                if entry is None:
                    if len(entries) >= 128:
                        raise FileAccessError('limit_exceeded')
                    try:
                        os.mkdir(_DIRECTORY, 0o700, dir_fd=opened.fd)
                    except FileExistsError:
                        raise FileAccessError('write_backend_unverified') from None
                    os.fsync(opened.fd)
                descriptor = os.open(_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY
                    | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=opened.fd)
                try:
                    info = _trusted(descriptor, private=True)
                    if _descriptor_mount_id(self.root, descriptor) != opened.mount_id:
                        raise FileAccessError('root_unavailable')
                    identity = _stable(info)
                    if entry is not None and (entry['identity'] != identity or not _same_root(entry['row'], row)):
                        raise FileAccessError('file_changed')
                    entries[root_id] = {'row': dict(row), 'identity': identity, 'layout': _LAYOUT,
                        'mount_id': opened.mount_id, 'enabled': True,
                        'revision': (entry['revision'] + 1) if entry is not None else 1}
                    _authorized(current)
                    self._save()
                finally:
                    os.close(descriptor)
            return {'ok': True, 'authority': self._public(root_id, entries[root_id]),
                    'may_wake_disks': True}

    def open(self, row, operation_id, *, current, create=True, allow_revoked=False):
        """Machine-only consumption of a prior owner registration and exact fd."""
        _authorized(current)
        if type(operation_id) is not str or not _OPERATION.fullmatch(operation_id):
            raise FileAccessError('invalid_arguments')
        with self._lock:
            entry = self._check_registered(row, allow_revoked=allow_revoked)
            with self._handle(row) as opened:
                self._trusted_chain(opened)
                parent = os.open(_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY
                    | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=opened.fd)
                descriptor = None
                try:
                    if (_stable(_trusted(parent, private=True)) != entry['identity']
                            or _descriptor_mount_id(self.root, parent) != opened.mount_id):
                        raise FileAccessError('file_changed')
                    _authorized(current)
                    if create:
                        # Exclusive, trusted private parent proves this birth.
                        os.mkdir(operation_id, 0o700, dir_fd=parent)
                        os.fsync(parent)
                    descriptor = os.open(operation_id, os.O_RDONLY | os.O_DIRECTORY
                        | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                    _trusted(descriptor, private=True)
                    stage = CustodyStage(self, row, parent, descriptor, operation_id,
                                         allow_revoked=allow_revoked)
                    stage.verify(current)
                    return stage
                except Exception:
                    if descriptor is not None:
                        os.close(descriptor)
                    os.close(parent)
                    raise

    def snapshot(self, root_id, *, current):
        """Owner recovery inventory: only registered operation IDs, no paths."""
        _authorized(current)
        with self._lock:
            self._reject_legacy(root_id)
            entry = self._load().get(root_id)
            if entry is None:
                raise FileAccessError('root_unavailable')
            # Catalog is deliberately cached. A detailed owner inspection is
            # explicit and may open the selected disk.
            row = self._authority.authorize(root_id, '.r1-authority-proof', current)
            if not _same_root(row, entry['row']):
                raise FileAccessError('root_unavailable')
            records = []
            with self._handle(row) as opened:
                parent = os.open(_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY
                    | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=opened.fd)
                try:
                    if _stable(_trusted(parent, private=True)) != entry['identity']:
                        raise FileAccessError('file_changed')
                    for name in os.listdir(parent):
                        if len(records) >= 4096:
                            raise FileAccessError('limit_exceeded')
                        if _OPERATION.fullmatch(name):
                            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY
                                | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                            try:
                                _trusted(fd, private=True)
                                manifest = _read_at(fd, 'manifest.json', {})
                                objects = [n for n in ('source', 'previous', 'directory')
                                           if n in os.listdir(fd)]
                                if type(manifest.get('native_recycle')) is dict:
                                    objects.append('native_recycle')
                                if self._published_candidate(manifest) and 'source' not in objects:
                                    objects.append('published_target')
                                display = self._recovery_display(manifest)
                                records.append({'recovery_id': name,
                                    'state': manifest.get('state', 'result_unconfirmed'),
                                    'objects': objects, **display})
                            finally:
                                os.close(fd)
                finally:
                    os.close(parent)
            _authorized(current)
            return {'ok': True, 'authority': self._public(root_id, entry),
                    'recoveries': records, 'may_wake_disks': True}

    @staticmethod
    def _recovery_display(manifest):
        """Owner-only review text from validated manifest fields, never raw data."""
        from api_file_provider import audited_owner_write_rule
        path = manifest.get('restore_path')
        if (not audited_owner_write_rule(path) or is_private_recovery_path(path) or path == '.DATA_CACHE'
                or path.startswith('.DATA_CACHE/')):
            path = None
        tool = manifest.get('tool')
        return {'path': path, 'tool': tool if type(tool) is str and tool in _RECOVERY_TOOLS else 'unknown'}

    @staticmethod
    def _published_candidate(manifest):
        """Only an incomplete audited rename/move can name a recovery target."""
        from api_file_provider import audited_owner_write_rule
        source, target = manifest.get('source_path'), manifest.get('target_path')
        identity, digest = manifest.get('published_identity'), manifest.get('published_sha256')
        return (manifest.get('tool') in ('rename_file', 'move_file')
            and manifest.get('state') in ('publish_intent', 'published_unconfirmed', 'pending',
                                         'result_unconfirmed')
            and audited_owner_write_rule(source) and audited_owner_write_rule(target)
            and not is_private_recovery_path(source) and not is_private_recovery_path(target)
            and source != target and manifest.get('restore_path') == source
            and manifest.get('published_path') == target
            and type(identity) is list and len(identity) == 3
            and all(type(v) is int and v >= 0 for v in identity)
            and identity[2] == stat.S_IFREG and type(digest) is str
            and re.fullmatch(r'[0-9a-f]{64}', digest, re.ASCII) is not None)

    def _capture_published(self, row, manifest, stage, *, current):
        """Owner recovery captures the fixed published name, then proves bytes.

        A wrong capture receives a separate fixed recovery ID, preserving both
        the original no-replay receipt and an owner route for that other object.
        Neither object is copied, overwritten or silently moved back on a race.
        """
        if not self._published_candidate(manifest):
            raise FileAccessError('write_backend_unverified')
        from api_file_executor import _Handle
        from api_file_transfers import _identity
        from api_file_mutations import _rename_new
        target, original = manifest['published_path'], manifest['restore_path']
        if self._authority.authorize(row['id'], target, current) != row \
                or self._authority.authorize(row['id'], original, current) != row:
            raise FileAccessError('root_unavailable')
        parent, _, leaf = target.rpartition('/')
        original_parent, _, original_leaf = original.rpartition('/')
        with _Handle(self.root, row, original_parent, directory=True) as checked:
            try:
                os.stat(original_leaf, dir_fd=checked.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileAccessError('target_exists')
        deadline = time.monotonic() + 1800
        with _Handle(self.root, row, target, reader=True) as source, \
             _Handle(self.root, row, parent, directory=True) as opened:
            if _stable(os.fstat(source.fd)) != manifest['published_identity']:
                raise FileAccessError('file_changed')
            before = _identity(source.fd)
            def digest_current(*, captured=False):
                pinned = _identity(source.fd)
                result, offset = hashlib.sha256(), 0
                while offset < pinned[2]:
                    _authorized(current)
                    stage.verify(current)
                    if time.monotonic() >= deadline or _identity(source.fd) != pinned:
                        raise FileAccessError('file_changed')
                    if captured:
                        named = os.stat('source', dir_fd=stage.fd, follow_symlinks=False)
                        if _stable(named) != _stable(os.fstat(source.fd)):
                            raise FileAccessError('file_changed')
                    else:
                        with _Handle(self.root, row, target) as named:
                            if _identity(named.fd) != before:
                                raise FileAccessError('file_changed')
                    block = os.pread(source.fd, min(65536, pinned[2] - offset), offset)
                    if not block:
                        raise FileAccessError('file_changed')
                    result.update(block)
                    offset += len(block)
                _authorized(current)
                if _identity(source.fd) != pinned:
                    raise FileAccessError('file_changed')
                return result.hexdigest()
            if digest_current() != manifest['published_sha256']:
                raise FileAccessError('file_changed')
            prior_state = manifest['state']
            manifest.update(state='published_restore_intent', restore_receipt={
                'recovery_id': stage.operation_id, 'state': 'result_unconfirmed', 'index_verified': False})
            stage.record(manifest, current=current)
            if self._authority.authorize(row['id'], target, current) != row:
                raise FileAccessError('root_unavailable')
            stage.verify(current)
            try:
                _rename_new(opened.fd, leaf, stage.fd, 'source')
            except FileAccessError as error:
                if error.code in ('target_exists', 'not_found'):
                    manifest.pop('restore_receipt', None)
                    manifest['state'] = prior_state
                    stage.record_recovery(manifest)
                raise
            os.fsync(opened.fd)
            os.fsync(stage.fd)
            actual = os.stat('source', dir_fd=stage.fd, follow_symlinks=False)
            if _stable(actual) != manifest['published_identity']:
                # The mistaken object gets its own no-clobber restoration,
                # rather than making a repeated original request mutate it.
                conflict_id = self._fork_recovery(row, stage, 'source', target, actual, current=current)
                manifest['conflict_recovery_id'] = conflict_id
                stage.record_recovery(manifest)
                raise FileAccessError('result_unconfirmed')
            manifest.update(state='published_target_captured', custody_name='source',
                            custody_identity=_stable(actual))
            stage.record_recovery(manifest)
            if ((actual.st_size, actual.st_mtime_ns) != (before[2], before[3])
                    or digest_current(captured=True) != manifest['published_sha256']):
                raise FileAccessError('result_unconfirmed')
            # Continue the same explicitly confirmed restore. The intent is
            # already durable and remains unknown if any later step fails.
            return manifest

    def _fork_recovery(self, row, stage, name, path, info, *, current):
        """Give a preserved conflict its own fixed, explicitly confirmed event."""
        from api_file_mutations import _rename_new
        identifier = 'operation_' + secrets.token_hex(16)
        with self.open(row, identifier, current=current, allow_revoked=True) as conflict:
            conflict.record({'version': 1, 'state': 'captured_conflict',
                'tool': 'owner_restore_conflict', 'restore_path': path,
                'custody_name': 'source', 'custody_identity': _stable(info),
                'origin_recovery_id': stage.operation_id}, current=current)
            if _stable(os.stat(name, dir_fd=stage.fd, follow_symlinks=False)) != _stable(info):
                raise FileAccessError('file_changed')
            _rename_new(stage.fd, name, conflict.fd, 'source')
            os.fsync(stage.fd)
            os.fsync(conflict.fd)
        return identifier

    def _restore_digest(self, descriptor, pinned, row, path, stage, name, *,
                        published, current, deadline):
        """Hash one held readable inode, renewing owner/path/ctime every block."""
        from api_file_executor import _Handle
        if not 0 <= pinned[6] <= 8 * 1024 ** 3:
            raise FileAccessError('limit_exceeded')
        def gate():
            _authorized(current)
            if time.monotonic() >= deadline:
                raise FileAccessError('timeout')
            if not _same_root(self._authority.authorize(row['id'], path, current), row):
                raise FileAccessError('root_unavailable')
            stage.verify(current)
            if _restore_signature(os.fstat(descriptor)) != pinned:
                raise FileAccessError('file_changed')
            if published:
                with _Handle(self.root, row, path) as opened:
                    named = os.fstat(opened.fd)
            else:
                named = os.stat(name, dir_fd=stage.fd, follow_symlinks=False)
            if _restore_signature(named) != pinned:
                raise FileAccessError('file_changed')
            _authorized(current)
            if (_restore_signature(os.fstat(descriptor)) != pinned
                    or time.monotonic() >= deadline):
                raise FileAccessError('file_changed')
        digest, offset = hashlib.sha256(), 0
        gate()
        while offset < pinned[6]:
            gate()
            block = os.pread(descriptor, min(65536, pinned[6] - offset), offset)
            if not block:
                raise FileAccessError('file_changed')
            gate()
            digest.update(block)
            offset += len(block)
        gate()
        return digest.hexdigest()

    @staticmethod
    def _recorded_restore_hashes(manifest, info):
        """Expected-source hashes do not describe a differently captured inode."""
        expected = []
        for identity_field, hash_field in (('source_identity', 'source_sha256'),
                                            ('published_identity', 'published_sha256')):
            identity = manifest.get(identity_field)
            if type(identity) is list and identity[:2] == [info.st_dev, info.st_ino]:
                digest = manifest.get(hash_field)
                if type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}', digest, re.ASCII) is None:
                    raise FileAccessError('write_backend_unverified')
                expected.append(digest)
        if 'restore_sha256' in manifest:
            digest = manifest['restore_sha256']
            if type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}', digest, re.ASCII) is None:
                raise FileAccessError('write_backend_unverified')
            expected.append(digest)
        return expected

    def restore(self, root_id, recovery_id, *, confirmed, current):
        """Owner-only restore to the recorded original name, never an input path.

        A previous target is retained after overwrite until the owner clears
        that name through a separately authorized operation. Restore refuses an
        occupied destination; it never exchanges, deletes or merges objects.
        """
        _authorized(current)
        if (confirmed is not True or type(root_id) is not str or not _ROOT.fullmatch(root_id)
                or type(recovery_id) is not str or not _OPERATION.fullmatch(recovery_id)):
            raise FileAccessError('invalid_arguments')
        with self._lock:
            self._reject_legacy(root_id)
            entry = self._load().get(root_id)
            if entry is None:
                raise FileAccessError('root_unavailable')
            row = self._authority.authorize(root_id, '.r1-authority-proof', current)
            if not _same_root(row, entry['row']):
                raise FileAccessError('root_unavailable')
            with self.open(row, recovery_id, current=current, create=False, allow_revoked=True) as stage:
                manifest = _read_at(stage.fd, 'manifest.json')
                if type(manifest) is not dict:
                    raise FileAccessError('write_backend_unverified')
                receipt = manifest.get('restore_receipt')
                if type(receipt) is dict:
                    _authorized(current)
                    return {'ok': True, 'recovery': dict(receipt), 'may_wake_disks': True}
                if type(manifest.get('native_recycle')) is dict:
                    if is_private_recovery_path(manifest['native_recycle'].get('source_path')):
                        raise FileAccessError('write_backend_unverified')
                    from api_file_native_recycle import NativeFileRecycle
                    receipt = NativeFileRecycle(root=self.root).restore_manifest(
                        row, manifest, stage, current=current)
                    _authorized(current)
                    return receipt
                if self._published_candidate(manifest):
                    try:
                        os.stat('source', dir_fd=stage.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        manifest = self._capture_published(row, manifest, stage, current=current)
                from api_file_provider import audited_owner_write_rule
                path, name = manifest.get('restore_path'), manifest.get('custody_name')
                if (not audited_owner_write_rule(path) or is_private_recovery_path(path)
                        or name not in ('source', 'previous', 'directory')
                        or manifest.get('custody_identity') is not None
                        and type(manifest.get('custody_identity')) is not list):
                    raise FileAccessError('write_backend_unverified')
                if self._authority.authorize(root_id, path, current) != row:
                    raise FileAccessError('root_unavailable')
                parent, _, leaf = path.rpartition('/')
                from api_file_executor import _Handle
                try:
                    descriptor = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=stage.fd)
                except FileNotFoundError:
                    raise FileAccessError('not_found') from None
                try:
                    info = os.fstat(descriptor)
                    directory = stat.S_ISDIR(info.st_mode)
                    if manifest.get('custody_identity') is None:
                        # A crash between capture/mkdir and its first fstat
                        # leaves a known protected name, not an unknowable or
                        # externally writable birth. Bind that actual object.
                        if manifest.get('state') not in ('capturing', 'creating'):
                            raise FileAccessError('write_backend_unverified')
                        manifest['custody_identity'] = _stable(info)
                        stage.record(manifest, current=current)
                    if (_stable(info) != manifest['custody_identity']
                            or not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
                        raise FileAccessError('file_changed')
                    # Inspect with O_PATH, then reopen only that owned kernel
                    # inode for hashing. No user path can substitute a device.
                    if not directory:
                        readable = os.open('/proc/self/fd/' + str(descriptor),
                            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                        if _restore_signature(os.fstat(readable)) != _restore_signature(info):
                            os.close(readable)
                            raise FileAccessError('file_changed')
                        os.close(descriptor)
                        descriptor = readable
                    deadline = time.monotonic() + 1800
                    pinned = _restore_signature(os.fstat(descriptor))
                    before_hash = None
                    with _Handle(self.root, row, parent, directory=True) as destination:
                        if _descriptor_mount_id(self.root, descriptor) != destination.mount_id:
                            raise FileAccessError('root_unavailable')
                        stage.verify(current)
                        if not directory:
                            before_hash = self._restore_digest(descriptor, pinned, row, path, stage, name,
                                published=False, current=current, deadline=deadline)
                            if any(digest != before_hash
                                   for digest in self._recorded_restore_hashes(manifest, info)):
                                raise FileAccessError('file_changed')
                            # A wrong capture without an original digest is
                            # bound to this explicit attempt before any move.
                            manifest['restore_sha256'] = before_hash
                        # Persist intent before changing a user-visible name.
                        manifest['restore_receipt'] = {'recovery_id': recovery_id,
                            'state': 'result_unconfirmed', 'index_verified': False}
                        stage.record(manifest, current=current)
                        if not _same_root(self._authority.authorize(root_id, path, current), row):
                            raise FileAccessError('root_unavailable')
                        stage.verify(current)
                        if (_stable(os.stat(name, dir_fd=stage.fd, follow_symlinks=False)) != _stable(info)
                                or not directory and (
                                    _restore_signature(os.fstat(descriptor)) != pinned
                                    or _restore_signature(os.stat(name, dir_fd=stage.fd,
                                        follow_symlinks=False)) != pinned)):
                            raise FileAccessError('file_changed')
                        from api_file_mutations import _rename_new
                        try:
                            _rename_new(stage.fd, name, destination.fd, leaf)
                        except FileAccessError as error:
                            if error.code in ('target_exists', 'not_found'):
                                if manifest.get('state') == 'published_target_captured':
                                    manifest['conflict_recovery_id'] = self._fork_recovery(
                                        row, stage, name, path, info, current=current)
                                    stage.record_recovery(manifest)
                                    raise FileAccessError('result_unconfirmed') from None
                                manifest.pop('restore_receipt', None)
                                stage.record_recovery(manifest)
                            raise
                        os.fsync(destination.fd)
                        os.fsync(stage.fd)
                        with _Handle(self.root, row, path, directory=directory) as final:
                            if _stable(os.fstat(final.fd)) != _stable(info):
                                raise FileAccessError('result_unconfirmed')
                        after = _restore_signature(os.fstat(descriptor))
                        if not directory:
                            if after[:-1] != pinned[:-1]:
                                raise FileAccessError('result_unconfirmed')
                            try:
                                actual_hash = self._restore_digest(descriptor, after, row, path, stage, name,
                                    published=True, current=current, deadline=deadline)
                                if actual_hash != before_hash:
                                    raise FileAccessError('file_changed')
                            except FileAccessError as error:
                                if error.code == 'invalid_key':
                                    raise
                                raise FileAccessError('result_unconfirmed') from None
                        # A fixed native create/close or mkdir notification is
                        # attempted once. Its absence never rolls back restore.
                        accepted = False
                        try:
                            from api_file_native_index import NativeFileIndex
                            bridge = NativeFileIndex(root=self.root)
                            native = bridge.notify_new(row, path, descriptor,
                                                       directory=directory, current=current)
                            accepted = bridge.verify_new(native, row, path, descriptor,
                                                         directory=directory, current=current) is True
                        except Exception:
                            pass
                        if not directory:
                            # Native notification can block while an old write
                            # fd remains open. Never publish a restored receipt
                            # after bytes or ctime changed during that interval.
                            try:
                                actual_hash = self._restore_digest(descriptor, after, row, path, stage, name,
                                    published=True, current=current, deadline=deadline)
                                if actual_hash != before_hash:
                                    raise FileAccessError('file_changed')
                            except FileAccessError as error:
                                if error.code == 'invalid_key':
                                    raise
                                raise FileAccessError('result_unconfirmed') from None
                        receipt = {'recovery_id': recovery_id,
                            'state': 'restored', 'index_verified': accepted}
                        manifest.update(state='restored', restore_receipt=receipt)
                        stage.record_recovery(manifest)
                        _authorized(current)
                        return {'ok': True, 'recovery': receipt, 'may_wake_disks': True}
                finally:
                    os.close(descriptor)

    def revoke(self, root_id, *, confirmed, current):
        _authorized(current)
        if confirmed is not True:
            raise FileAccessError('invalid_arguments')
        with self._lock:
            self._reject_legacy(root_id)
            entry = self._load().get(root_id)
            if entry is None:
                raise FileAccessError('root_unavailable')
            entry.update(enabled=False, revision=entry['revision'] + 1)
            self._save()
            _authorized(current)
            return {'ok': True, 'authority': self._public(root_id, entry)}

    def close(self):
        self._closed = True

"""Owner-delegated, structured Docker deployments on fixed local resources.

Future deployments are not fabricated Compose projects. Their private owner
contracts bind one name, repository, numeric user, existing network/volumes and
literal published ports. Machine callers select a contract, never an Engine
body, command, Compose document, host path or network address.
"""

import copy
import ipaddress
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from urllib.parse import quote

import api_docker_projects as projects
import api_docker_operations as operations


_DEPLOYMENT = re.compile(r'deployment_[0-9a-f]{24}\Z')
_RESOURCE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_REPOSITORY = re.compile(r'[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?'
                         r'(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z')
_TOOLS = {'list_docker_deployments', 'plan_docker_deployment',
          'execute_docker_deployment', 'get_docker_deployment_task'}
_OPERATIONS = {'deploy': 'deploy', 'update': 'deploy', 'start': 'control', 'stop': 'control',
               'restart': 'control', 'remove': 'remove'}
_LABEL = 'io.r1-toolbox.deployment'
_LOCKS, _LOCKS_GUARD = {}, threading.Lock()


def _deny(code='invalid_arguments'):
    raise operations.OperationError(code)


def _target(value):
    """Mount targets are literal container directories, never host paths."""
    if (not isinstance(value, str) or len(value) > 256 or not value.startswith('/')
            or value == '/' or '\\' in value or any(not part or part in ('.', '..')
            for part in value.split('/')[1:]) or not all(c.isprintable() for c in value)
            or value in ('/proc', '/sys', '/dev', '/run', '/var/run')
            or any(value.startswith(prefix + '/') for prefix in ('/proc', '/sys', '/dev', '/run', '/var/run'))):
        _deny()
    return value


def _volume_path_identity(path):
    """Bind existing local volume directory metadata without reading contents."""
    if (not isinstance(path, str) or not path.startswith('/') or '\\' in path
            or any(part in ('', '.', '..') for part in path.split('/')[1:])
            or not hasattr(os, 'O_PATH') or not hasattr(os, 'O_NOFOLLOW')):
        _deny('write_backend_unverified')
    descriptor = os.open('/', os.O_PATH | os.O_DIRECTORY)
    try:
        for part in path.split('/')[1:]:
            child = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            _deny('write_backend_unverified')
        lines = Path('/proc/self/fdinfo/' + str(descriptor)).read_text(encoding='ascii').splitlines()
        mounts = [int(line.split(':', 1)[1]) for line in lines if line.startswith('mnt_id:')]
        if len(mounts) != 1:
            _deny('write_backend_unverified')
        return [info.st_dev, info.st_ino, mounts[0]]
    except (OSError, ValueError, UnicodeError):
        _deny('write_backend_unverified')
    finally:
        os.close(descriptor)


class LocalDockerDeploymentWriter(operations.LocalDockerWriter):
    """Extend only fixed reads used to verify delegated deployment resources."""

    @staticmethod
    def _allowed(method, path):
        if method == 'GET' and path in ('/volumes', '/networks', '/containers/json?all=1&limit=129'):
            return True
        if method == 'GET' and (re.fullmatch(r'/volumes/[A-Za-z0-9_.-]{1,128}', path)
                or re.fullmatch(r'/networks/[0-9a-f]{64}', path)):
            return True
        return operations.LocalDockerWriter._allowed(method, path)


class DockerDeploymentRegistry(projects.DockerProjectRegistry):
    """Owner-only persistent future-resource contracts, independent of Compose."""

    def __init__(self, state_dir, *, backend=None, volume_identity=None):
        super().__init__(state_dir, backend=backend or LocalDockerDeploymentWriter())
        self.path = Path(state_dir) / 'docker-deployments.json'
        self._volume_identity = volume_identity or _volume_path_identity

    def _read(self):
        if self._write_failed:
            _deny('project_unavailable')
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                                 | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            return {'version': 1, 'revision': 0, 'deployments': []}
        except OSError:
            _deny('project_unavailable')
        try:
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or self.path.is_symlink()
                        or info.st_size > projects.MAX_STATE or os.name != 'nt'
                        and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid())):
                    _deny('project_unavailable')
                value = projects._json(stream.read(projects.MAX_STATE + 1))
            if (not isinstance(value, dict) or set(value) != {'version', 'revision', 'deployments'}
                    or type(value['version']) is not int or value['version'] != 1
                    or type(value['revision']) is not int or value['revision'] < 0
                    or not isinstance(value['deployments'], list) or len(value['deployments']) > 128):
                _deny('project_unavailable')
            seen, names = set(), set()
            for row in value['deployments']:
                if (not isinstance(row, dict) or set(row) != {'id', 'label', 'revision', 'revoked',
                        'contract', 'binding', 'instance'} or not isinstance(row['id'], str)
                        or not _DEPLOYMENT.fullmatch(row['id']) or row['id'] in seen
                        or not projects._plain(row['label']) or type(row['revision']) is not int
                        or row['revision'] < 1 or type(row['revoked']) is not bool
                        or not isinstance(row['contract'], dict)
                        or projects._hash(row['contract']) != row['binding']):
                    _deny('project_unavailable')
                self._validate_contract(row['contract'])
                instance = row['instance']
                if instance is not None and (not isinstance(instance, dict)
                        or set(instance) != {'container_id', 'image_reference', 'image_id', 'body_hash', 'contract_binding'}
                        or not isinstance(instance['container_id'], str)
                        or not projects._HEX.fullmatch(instance['container_id'])
                        or not isinstance(instance['image_reference'], str)
                        or not projects._IMAGE.fullmatch(instance['image_reference'])
                        or '@sha256:' not in instance['image_reference']
                        or not isinstance(instance['image_id'], str)
                        or not operations._DIGEST.fullmatch(instance['image_id'])
                        or not isinstance(instance['body_hash'], str)
                        or not projects._HEX.fullmatch(instance['body_hash'])
                        or not isinstance(instance['contract_binding'], str)
                        or not projects._HEX.fullmatch(instance['contract_binding'])):
                    _deny('project_unavailable')
                name = row['contract']['spec']['name']
                if not row['revoked'] and name in names:
                    _deny('project_unavailable')
                seen.add(row['id'])
                if not row['revoked']:
                    names.add(name)
            return value
        except (ValueError, TypeError, KeyError, OSError, UnicodeError, RecursionError):
            _deny('project_unavailable')

    @staticmethod
    def _spec(body):
        fields = {'name', 'image_repository', 'network', 'volumes', 'ports', 'user', 'memory_bytes', 'nano_cpus'}
        if not isinstance(body, dict) or set(body) != fields:
            _deny()
        spec = copy.deepcopy(body)
        name, repository, network = (spec[key] for key in ('name', 'image_repository', 'network'))
        if (not isinstance(name, str) or not projects._NAME.fullmatch(name)
                or name in ('r1-toolbox', 'r1-boot-once') or not isinstance(repository, str)
                or len(repository) > 256 or not _REPOSITORY.fullmatch(repository)
                or repository.rsplit('/', 1)[-1] in ('r1-toolbox', 'r1-boot-once')
                or not isinstance(network, str) or not _RESOURCE_NAME.fullmatch(network)
                or network in ('host', 'container')
                or not isinstance(spec['user'], str)
                or not re.fullmatch(r'[1-9][0-9]{0,9}:[1-9][0-9]{0,9}', spec['user'])
                or any(int(part) > 2147483647 for part in spec['user'].split(':'))
                or type(spec['memory_bytes']) is not int or not 16 * 1024 * 1024 <= spec['memory_bytes'] <= 16 * 1024**3
                or type(spec['nano_cpus']) is not int or not 10_000_000 <= spec['nano_cpus'] <= 8_000_000_000
                or not isinstance(spec['volumes'], list) or len(spec['volumes']) > 8
                or not isinstance(spec['ports'], list) or len(spec['ports']) > 16):
            _deny()
        names, targets, ports = set(), set(), set()
        for row in spec['volumes']:
            if (not isinstance(row, dict) or set(row) != {'name', 'target', 'readonly'}
                    or not isinstance(row['name'], str) or not _RESOURCE_NAME.fullmatch(row['name'])
                    or row['name'] == 'r1_toolbox_data' or type(row['readonly']) is not bool):
                _deny()
            _target(row['target'])
            if row['target'] in targets or row['name'] in names:
                _deny()
            names.add(row['name'])
            targets.add(row['target'])
        # Nested mounts obscure resource boundaries and are rejected.
        if any(a.startswith(b + '/') for a in targets for b in targets if a != b):
            _deny()
        for row in spec['ports']:
            if (not isinstance(row, dict) or set(row) != {'host_ip', 'published', 'target', 'protocol'}
                    or row['protocol'] not in ('tcp', 'udp') or any(type(row[key]) is not int
                    or not 1 <= row[key] <= 65535 for key in ('published', 'target'))):
                _deny()
            try:
                address = ipaddress.ip_address(row['host_ip'])
            except (ValueError, TypeError):
                _deny()
            private = (address.is_loopback or address.version == 4 and any(address in net for net in
                (ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
                 ipaddress.ip_network('192.168.0.0/16'))) or address.version == 6
                 and address in ipaddress.ip_network('fc00::/7'))
            if (not private or address.is_unspecified or address.is_multicast
                    or str(address) != row['host_ip'] or (row['host_ip'], row['published'], row['protocol']) in ports):
                _deny()
            ports.add((row['host_ip'], row['published'], row['protocol']))
        if network == 'none' and spec['ports']:
            _deny()
        return spec

    @classmethod
    def _validate_contract(cls, value):
        if (set(value) != {'engine', 'peer', 'spec', 'network', 'volumes'}
                or not isinstance(value['engine'], str) or not projects._ENGINE.fullmatch(value['engine'])
                or not isinstance(value['peer'], list) or len(value['peer']) != 4
                or any(type(item) is not int or item < 0 for item in value['peer'])
                or not isinstance(value['network'], dict) or not isinstance(value['volumes'], list)):
            _deny('project_unavailable')
        cls._spec(value['spec'])

    def _resources(self, spec, context):
        info = self.backend.request('GET', '/info', context)
        engine = info.get('ID') if isinstance(info, dict) else None
        if not isinstance(engine, str) or not projects._ENGINE.fullmatch(engine):
            _deny('native_unavailable')
        networks = self.backend.request('GET', '/networks', context)
        if not isinstance(networks, list) or len(networks) > 128:
            _deny('limit_exceeded')
        matches = [row for row in networks if isinstance(row, dict) and row.get('Name') == spec['network']]
        if len(matches) != 1:
            _deny('project_binding_changed')
        network = matches[0]
        if (not isinstance(network.get('Id'), str) or not projects._HEX.fullmatch(network['Id'])
                or network.get('Driver') not in ('bridge', 'null')
                or network.get('Driver') == 'null' and spec['network'] != 'none'
                or network.get('Scope') != 'local' or network.get('Ingress') is True
                or network.get('ConfigOnly') is True or network.get('Options') not in (None, {})):
            _deny('write_backend_unverified')
        network = {key: network.get(key) for key in ('Id', 'Name', 'Driver', 'Scope',
                   'Internal', 'EnableIPv6', 'IPAM', 'Options', 'Labels', 'Created')}
        protected_volumes = operations._toolbox_data_volumes(self.backend, context)
        volumes = []
        for expected in spec['volumes']:
            row = self.backend.request('GET', '/volumes/' + expected['name'], context)
            if (expected['name'] in protected_volumes or operations._toolbox_volume(row)
                    or not isinstance(row, dict) or row.get('Name') != expected['name']
                    or row.get('Driver') != 'local' or row.get('Scope') != 'local'
                    or row.get('Options') not in (None, {}) or not projects._plain(row.get('CreatedAt'), 128)
                    or not isinstance(row.get('Mountpoint'), str)):
                _deny('write_backend_unverified')
            identity = {key: row.get(key) for key in ('Name', 'Driver', 'Scope', 'CreatedAt', 'Mountpoint', 'Options', 'Labels')}
            identity['directory'] = self._volume_identity(row['Mountpoint'])
            volumes.append(identity)
        if self.backend.request('GET', '/info', context).get('ID') != engine:
            _deny('project_binding_changed')
        return {'engine': engine, 'peer': copy.deepcopy(context['peer']), 'spec': spec,
                'network': network, 'volumes': volumes}

    @staticmethod
    def _public(row):
        return {'id': row['id'], 'label': row['label'], 'revision': row['revision'],
                'revoked': row['revoked'], 'name': row['contract']['spec']['name'],
                'image_repository': row['contract']['spec']['image_repository'],
                'operations': ['inspect', 'deploy', 'control', 'remove'],
                'deployed': row['instance'] is not None, 'verification': 'not_checked'}

    def metadata(self):
        with self._lock:
            try:
                data = self._read()
                return {'ok': True, 'revision': data['revision'],
                        'deployments': [self._public(row) for row in data['deployments']]}
            except (operations.OperationError, projects.ProjectError) as error:
                return operations._failure(error.code)

    def catalog(self):
        """Explicit owner check; inspect native resource names, never file data."""
        try:
            context = self.backend.context()
            context['deadline'] = min(context['deadline'], self.backend._clock() + projects.NETWORK_SECONDS)
            info = self.backend.request('GET', '/info', context)
            if not isinstance(info.get('ID'), str) or not projects._ENGINE.fullmatch(info['ID']):
                _deny('native_unavailable')
            volumes = self.backend.request('GET', '/volumes', context)
            networks = self.backend.request('GET', '/networks', context)
            protected_volumes = operations._toolbox_data_volumes(self.backend, context)
            volumes = volumes.get('Volumes') if isinstance(volumes, dict) else None
            if (not isinstance(volumes, list) or len(volumes) > 128
                    or not isinstance(networks, list) or len(networks) > 128):
                _deny('limit_exceeded')
            return {'ok': True, 'volumes': [{'name': row['Name']} for row in volumes
                        if isinstance(row, dict) and isinstance(row.get('Name'), str)
                        and _RESOURCE_NAME.fullmatch(row['Name']) and row['Name'] not in protected_volumes
                        and not operations._toolbox_volume(row)
                        and row.get('Driver') == 'local' and row.get('Scope') == 'local'
                        and row.get('Options') in (None, {})],
                    'networks': [{'name': row['Name']} for row in networks if isinstance(row, dict)
                        and isinstance(row.get('Name'), str) and _RESOURCE_NAME.fullmatch(row['Name'])
                        and row.get('Driver') in ('bridge', 'null') and row.get('Scope') == 'local'
                        and row.get('Options') in (None, {})]}
        except (operations.OperationError, projects.ProjectError) as error:
            return operations._failure(error.code)

    def register(self, body):
        try:
            if not isinstance(body, dict) or set(body) != {'label', 'spec'} or not projects._plain(body['label']):
                _deny()
            spec = self._spec(body['spec'])
            with self._lock:
                data = self._read()
                if len(data['deployments']) >= 128:
                    _deny('limit_exceeded')
                if any(not row['revoked'] and row['contract']['spec']['name'] == spec['name']
                       for row in data['deployments']):
                    _deny('plan_conflict')
                context = self.backend.context()
                context['deadline'] = min(context['deadline'], self.backend._clock() + projects.NETWORK_SECONDS)
                contract = self._resources(spec, context)
                rows = self.backend.request('GET', '/containers/json?all=1&limit=129', context)
                self._names_free(rows, spec['name'])
                row = {'id': 'deployment_' + secrets.token_hex(12), 'label': body['label'],
                       'revision': 1, 'revoked': False, 'contract': contract,
                       'binding': projects._hash(contract), 'instance': None}
                data['deployments'].append(row)
                data['revision'] += 1
                self._write(data)
                return {'ok': True, 'deployment': self._public(row)}
        except (operations.OperationError, projects.ProjectError) as error:
            return operations._failure(error.code)

    @staticmethod
    def _names_free(rows, name, *, container_id=None):
        if not isinstance(rows, list) or len(rows) > 128:
            _deny('limit_exceeded')
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('Id'), str) or not projects._HEX.fullmatch(row['Id']):
                _deny('native_unavailable')
            names = row.get('Names')
            if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
                _deny('native_unavailable')
            if '/' + name in names and row['Id'] != container_id:
                _deny('plan_conflict')

    def revoke(self, body):
        try:
            if not isinstance(body, dict) or set(body) != {'deployment_id'} or not isinstance(body['deployment_id'], str):
                _deny()
            with self._lock:
                data = self._read()
                rows = [row for row in data['deployments'] if row['id'] == body['deployment_id'] and not row['revoked']]
                if len(rows) != 1:
                    _deny('project_unavailable')
                rows[0]['revoked'] = True
                rows[0]['revision'] += 1
                data['revision'] += 1
                self._write(data)
                return {'ok': True, 'deployment': self._public(rows[0])}
        except (operations.OperationError, projects.ProjectError) as error:
            return operations._failure(error.code)

    def refresh(self, body):
        """Explicit owner lifecycle re-verification, never machine self-renewal."""
        try:
            if (not isinstance(body, dict) or set(body) != {'deployment_id', 'confirm'}
                    or body['confirm'] is not True or not isinstance(body['deployment_id'], str)):
                _deny()
            # The same per-resource lock excludes in-flight mutation while an
            # owner replaces lifecycle evidence. Revocation itself stays prompt.
            identity = (os.path.normcase(os.path.abspath(self.path)), body['deployment_id'])
            with _LOCKS_GUARD:
                lock = _LOCKS.setdefault(identity, threading.Lock())
            if not lock.acquire(blocking=False):
                _deny('resource_busy')
            try:
                with self._lock:
                    data = self._read()
                    rows = [row for row in data['deployments'] if row['id'] == body['deployment_id'] and not row['revoked']]
                    if len(rows) != 1:
                        _deny('project_unavailable')
                    previous = copy.deepcopy(rows[0])
                    context = self.backend.context()
                    context['deadline'] = min(context['deadline'], self.backend._clock() + projects.NETWORK_SECONDS)
                    contract = self._resources(previous['contract']['spec'], context)
                    # A different engine cannot inherit the existing resource.
                    if contract['engine'] != previous['contract']['engine']:
                        _deny('project_binding_changed')
                    current = copy.deepcopy(previous)
                    current['contract'], current['binding'] = contract, projects._hash(contract)
                    if current['instance'] is not None:
                        instance = current['instance']
                        detail = self.backend.request('GET', '/containers/' + instance['container_id'] + '/json', context)
                        if detail.get('Id') != instance['container_id']:
                            _deny('project_binding_changed')
                        DockerDeploymentOperations._check_container(current, detail,
                            instance['image_reference'], instance['image_id'],
                            contract_binding=instance['contract_binding'])
                    else:
                        records = self.backend.request('GET', '/containers/json?all=1&limit=129', context)
                        self._names_free(records, current['contract']['spec']['name'])
                    if self._resources(previous['contract']['spec'], context) != contract:
                        _deny('project_binding_changed')
                    current['revision'] += 1
                    rows[0].update(current)
                    data['revision'] += 1
                    self._write(data)
                    return {'ok': True, 'deployment': self._public(current)}
            finally:
                lock.release()
        except (operations.OperationError, projects.ProjectError) as error:
            return operations._failure(error.code)


class DockerDeploymentOperations(operations.DockerProjectOperations):
    """Fixed plan/execute/task flow sharing existing task/idempotency state."""

    def __init__(self, registry, *, authorizer, backend=None, shared_operations=None, clock=None):
        super().__init__(registry, authorizer=authorizer, backend=backend or registry.backend, clock=clock)
        if shared_operations is not None:
            self._state_lock = shared_operations._state_lock
            self._plans, self._tasks, self._idempotency = (shared_operations._plans,
                shared_operations._tasks, shared_operations._idempotency)
            self._threads, self._closed = shared_operations._threads, shared_operations._closed

    def _grant(self, principal, tool, deployment_id, operation, *, write=True):
        self._current(principal)
        owner = operations._owner(principal)
        if (tool not in _TOOLS or principal.get('policy_version') != 3
                or type(principal.get('policy_version')) is not int
                or principal.get('role') not in ('readonly', 'readwrite', 'admin')
                or write and principal['role'] != 'admin'
                or not isinstance(principal.get('scopes'), list) or 'docker' not in principal['scopes']
                or not isinstance(principal.get('tool_grants'), list) or tool not in principal['tool_grants']
                or not isinstance(deployment_id, str) or not _DEPLOYMENT.fullmatch(deployment_id)):
            _deny('permission_denied')
        grants = principal.get('docker_deployments')
        matches = [row for row in grants if isinstance(row, dict) and row.get('id') == deployment_id] if isinstance(grants, list) else []
        if len(matches) != 1 or not isinstance(matches[0].get('operations'), list) or operation not in matches[0]['operations']:
            _deny('permission_denied')
        return owner

    def _registered(self, deployment_id, *, expected=None):
        with self.registry._lock:
            rows = [row for row in self.registry._read()['deployments'] if row['id'] == deployment_id and not row['revoked']]
            if len(rows) != 1:
                _deny('permission_denied')
            row = copy.deepcopy(rows[0])
        if expected is not None and row != expected:
            _deny('project_binding_changed')
        return row

    def _lock(self, deployment_id):
        identity = (os.path.normcase(os.path.abspath(self.registry.path)), deployment_id)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(identity, threading.Lock())
        if not lock.acquire(blocking=False):
            _deny('resource_busy')
        return lock

    def list_deployments(self, principal):
        try:
            self._current(principal)
            grants = principal.get('docker_deployments')
            if not isinstance(grants, list):
                _deny('permission_denied')
            result = []
            for item in grants:
                identity = item.get('id') if isinstance(item, dict) else None
                self._grant(principal, 'list_docker_deployments', identity, 'inspect', write=False)
                row = self._registered(identity)
                public = self.registry._public(row)
                public['operations'] = [name for name in public['operations'] if name in item['operations']]
                result.append(public)
            self._current(principal)
            return {'ok': True, 'deployments': result}
        except (operations.OperationError, projects.ProjectError) as error:
            return operations._failure(error.code)

    def _verify(self, principal, row, context, *, allowed_id=None):
        self._current(principal)
        self._registered(row['id'], expected=row)
        contract = self.registry._resources(row['contract']['spec'], context)
        if contract != row['contract']:
            _deny('project_binding_changed')
        records = self.backend.request('GET', '/containers/json?all=1&limit=129', context)
        self.registry._names_free(records, row['contract']['spec']['name'], container_id=allowed_id)
        self._current(principal)

    @staticmethod
    def _body(row, image):
        spec = row['contract']['spec']
        ports = {}
        for port in spec['ports']:
            key = str(port['target']) + '/' + port['protocol']
            ports.setdefault(key, []).append({'HostIp': port['host_ip'], 'HostPort': str(port['published'])})
        body = {'Image': image, 'User': spec['user'],
                'Labels': {_LABEL: row['id'], _LABEL + '.binding': row['binding']},
                'ExposedPorts': {key: {} for key in ports},
                'HostConfig': {'Privileged': False, 'ReadonlyRootfs': True,
                    'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges:true'],
                    'Memory': spec['memory_bytes'], 'MemorySwap': spec['memory_bytes'],
                    'NanoCpus': spec['nano_cpus'], 'PidsLimit': 256,
                    'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0},
                    'NetworkMode': row['contract']['network']['Id'] if spec['network'] != 'none' else 'none',
                    'PortBindings': ports,
                    'Mounts': [{'Type': 'volume', 'Source': item['name'], 'Target': item['target'],
                                'ReadOnly': item['readonly'], 'VolumeOptions': {'NoCopy': True}}
                               for item in spec['volumes']]}}
        if spec['network'] != 'none':
            body['NetworkingConfig'] = {'EndpointsConfig': {spec['network']: {'NetworkID': row['contract']['network']['Id']}}}
        return body

    def _inspect(self, row, context, *, allowed_name=None):
        instance = row['instance']
        if instance is None:
            _deny('project_unavailable')
        detail = self.backend.request('GET', '/containers/' + instance['container_id'] + '/json', context)
        if not isinstance(detail, dict) or detail.get('Id') != instance['container_id']:
            _deny('project_binding_changed')
        checked = copy.deepcopy(detail)
        if allowed_name is not None:
            if checked.get('Name') != '/' + allowed_name:
                _deny('project_binding_changed')
            checked['Name'] = '/' + row['contract']['spec']['name']
        self._check_container(row, checked, instance['image_reference'], instance['image_id'],
                              contract_binding=instance['contract_binding'])
        return detail

    @staticmethod
    def _check_container(row, detail, image, image_id, *, contract_binding=None):
        spec, body = row['contract']['spec'], DockerDeploymentOperations._body(row, image)
        if contract_binding is not None:
            body['Labels'][_LABEL + '.binding'] = contract_binding
        if (not isinstance(detail, dict) or detail.get('Name') != '/' + spec['name']
                or not isinstance(detail.get('Id'), str) or not projects._HEX.fullmatch(detail['Id'])
                or detail.get('Image') != image_id or operations.DockerProjectOperations._protected(detail)):
            _deny('project_binding_changed')
        config, host = detail.get('Config'), detail.get('HostConfig')
        if (not isinstance(config, dict) or not isinstance(host, dict)
                or any(config.get(key) != body[key] for key in ('Image', 'User'))
                or not isinstance(config.get('Labels'), dict)
                or any(config['Labels'].get(key) != value for key, value in body['Labels'].items())
                or any(host.get(key) != value for key, value in body['HostConfig'].items() if key != 'Mounts')
                or host.get('Binds') or host.get('VolumesFrom') or host.get('Devices')
                or host.get('DeviceRequests') or host.get('CapAdd')
                or host.get('PidMode') not in (None, '') or host.get('IpcMode') in ('host',)
                or host.get('CgroupnsMode') == 'host'):
            _deny('project_binding_changed')
        # Engine versions add null/default mount-option fields to inspect.
        # Compare semantic options and still refuse any new resource source.
        actual_mounts = host.get('Mounts')
        if not isinstance(actual_mounts, list) or len(actual_mounts) != len(body['HostConfig']['Mounts']):
            _deny('project_binding_changed')
        for actual_mount, expected_mount in zip(actual_mounts, body['HostConfig']['Mounts']):
            if (not isinstance(actual_mount, dict)
                    or any(actual_mount.get(key) != expected_mount[key] for key in ('Type', 'Source', 'Target', 'ReadOnly'))
                    or actual_mount.get('BindOptions') not in (None, {})
                    or actual_mount.get('TmpfsOptions') not in (None, {})
                    or not isinstance(actual_mount.get('VolumeOptions'), dict)
                    or actual_mount['VolumeOptions'].get('NoCopy') is not True
                    or actual_mount['VolumeOptions'].get('DriverConfig') not in (None, {})
                    or actual_mount['VolumeOptions'].get('Labels') not in (None, {})
                    or actual_mount['VolumeOptions'].get('Subpath') not in (None, '')):
                _deny('project_binding_changed')
        mounts = detail.get('Mounts')
        if not isinstance(mounts, list) or len(mounts) != len(spec['volumes']):
            _deny('project_binding_changed')
        actual = sorted([(mount.get('Type'), mount.get('Name'), mount.get('Destination'), mount.get('RW'), mount.get('Source'))
                         for mount in mounts if isinstance(mount, dict)], key=repr)
        by_name = {item['Name']: item for item in row['contract']['volumes']}
        expected = sorted([('volume', item['name'], item['target'], not item['readonly'], by_name[item['name']]['Mountpoint'])
                           for item in spec['volumes']], key=repr)
        if actual != expected:
            _deny('project_binding_changed')
        networks = detail.get('NetworkSettings', {}).get('Networks')
        if not isinstance(networks, dict):
            _deny('project_binding_changed')
        if spec['network'] == 'none':
            if (set(networks) not in (set(), {'none'}) or 'none' in networks
                    and networks['none'].get('NetworkID') != row['contract']['network']['Id']):
                _deny('project_binding_changed')
        elif (set(networks) != {spec['network']}
                or networks[spec['network']].get('NetworkID') != row['contract']['network']['Id']):
            _deny('project_binding_changed')

    def plan(self, principal, deployment_id, operation, image_reference=None):
        lock = None
        try:
            if operation not in _OPERATIONS:
                _deny()
            owner = self._grant(principal, 'plan_docker_deployment', deployment_id, _OPERATIONS[operation])
            lock = self._lock(deployment_id)
            row = self._registered(deployment_id)
            if operation in ('deploy', 'update'):
                if (row['instance'] is not None) != (operation == 'update'):
                    _deny('plan_conflict')
                if (not isinstance(image_reference, str) or not projects._IMAGE.fullmatch(image_reference)
                        or '@sha256:' not in image_reference
                        or image_reference.split('@', 1)[0] != row['contract']['spec']['image_repository']):
                    _deny()
            elif image_reference is not None or row['instance'] is None:
                _deny()
            context = self.backend.context()
            context['deadline'] = min(context['deadline'], self.backend._clock() + projects.NETWORK_SECONDS)
            self._verify(principal, row, context, allowed_id=(row['instance'] or {}).get('container_id'))
            detail = self._inspect(row, context) if row['instance'] else None
            if operation == 'remove' and detail.get('State', {}).get('Running') is not False:
                _deny('plan_conflict')
            public = {'plan_id': 'plan_' + secrets.token_hex(16), 'deployment_id': deployment_id,
                      'operation': operation, 'image_reference': image_reference,
                      'requires_confirmation': True, 'restart_invalidates': True,
                      'expires_at': operations._stamp(time.time() + operations.PLAN_SECONDS),
                      'preserves_volumes': True}
            with self._state_lock:
                for identity in list(self._plans):
                    if self._plans[identity]['expires'] <= self.clock() and self._plans[identity]['task_id'] is None:
                        del self._plans[identity]
                if len(self._plans) >= operations.MAX_RECORDS:
                    _deny('limit_exceeded')
                self._plans[public['plan_id']] = {'kind': 'deployment', 'public': public, 'owner': owner,
                    'registered': row, 'fingerprint': operations.DockerProjectOperations._fingerprint(detail)
                    if detail else None, 'expires': self.clock() + operations.PLAN_SECONDS, 'task_id': None}
            self._current(principal)
            return {'ok': True, 'plan': public}
        except (operations.OperationError, projects.ProjectError) as error:
            return operations._failure(error.code)
        except Exception:
            return operations._failure('native_unavailable')
        finally:
            if lock is not None:
                lock.release()

    def execute(self, principal, plan_id, confirmed, idempotency_key):
        lock = None
        try:
            self._current(principal)
            with self._state_lock:
                plan = self._plans.get(plan_id) if isinstance(plan_id, str) else None
                if plan is None or plan.get('kind') != 'deployment' or plan['owner'] != operations._owner(principal):
                    _deny('plan_conflict')
                identity = plan['public']['deployment_id']
            self._grant(principal, 'execute_docker_deployment', identity, _OPERATIONS[plan['public']['operation']])
            if confirmed is not True or 'plan_docker_deployment' not in principal['tool_grants']:
                _deny('permission_denied')
            previous, _, _ = self._idempotent(principal, identity, plan_id, idempotency_key)
            if previous is not None:
                return self._publish(principal, previous)
            lock = self._lock(identity)
            if self.clock() >= plan['expires']:
                _deny('plan_expired')
            with self._state_lock:
                if plan['task_id'] is not None:
                    _deny('plan_conflict')
                if len(self._threads) >= operations.MAX_ACTIVE_TASKS:
                    _deny('resource_busy')
                task, reserved = self._reserve_task(principal, identity, plan_id, idempotency_key)
                if not reserved:
                    return self._publish(principal, task)
                task['deployment_id'] = identity
                task['operation'] = plan['public']['operation']
                plan['task_id'] = task['task_id']
                worker = threading.Thread(target=self._run, args=(copy.deepcopy(principal), plan, task, lock),
                    name='r1-docker-deploy-' + task['task_id'][5:13], daemon=True)
                self._threads.add(worker)
                try:
                    worker.start()
                except Exception:
                    self._threads.discard(worker)
                    self._finish(task, 'failed', error='native_unavailable')
                    _deny('native_unavailable')
                lock = None
            return self._publish(principal, task)
        except (operations.OperationError, projects.ProjectError) as error:
            return operations._failure(error.code)
        finally:
            if lock is not None:
                lock.release()

    def get_task(self, principal, deployment_id, task_id):
        try:
            owner = self._grant(principal, 'get_docker_deployment_task', deployment_id, 'inspect', write=False)
            self._registered(deployment_id)
            with self._state_lock:
                task = self._tasks.get(task_id) if isinstance(task_id, str) else None
                if task is None or task['owner'] != owner or task['public'].get('deployment_id') != deployment_id:
                    _deny('task_unavailable')
                public = copy.deepcopy(task['public'])
            self._current(principal)
            return {'ok': True, 'task': public}
        except (operations.OperationError, projects.ProjectError) as error:
            return operations._failure(error.code)

    def _record_instance(self, row, instance):
        with self.registry._lock:
            data = self.registry._read()
            found = [item for item in data['deployments'] if item['id'] == row['id'] and item == row]
            if len(found) != 1:
                _deny('project_binding_changed')
            found[0]['instance'] = instance
            data['revision'] += 1
            self.registry._write(data)
            return copy.deepcopy(found[0])

    def _run(self, principal, plan, task, lock):
        row, public, mutated = plan['registered'], plan['public'], False
        previous_container, previous_detail = None, None
        container_id = (row['instance'] or {}).get('container_id')
        expected_name = row['contract']['spec']['name']
        try:
            # Construction itself may fail (socket/clock/backend lifecycle).
            # The task and resource lock still require the same terminal path.
            context = self.backend.context()
            self._finish(task, 'running')
            self._verify(principal, row, context, allowed_id=container_id)
            operation = public['operation']
            if container_id:
                detail = self._inspect(row, context)
                if operations.DockerProjectOperations._fingerprint(detail) != plan['fingerprint']:
                    _deny('plan_conflict')
            gate = lambda: self._write_gate(principal, row, context, allowed_id=container_id,
                                            allowed_name=expected_name)
            if operation in ('deploy', 'update'):
                image = public['image_reference']
                # Pulling is explicit delegated work. Each request repeats the
                # peer, owner contract and current key check before send.
                mutated = True
                self.backend.request('POST', '/images/create?fromImage=' + quote(image, safe=''),
                    context, stream=True, authorizer=gate)
                image_info = self.backend.request('GET', '/images/' + quote(image, safe='') + '/json', context)
                if (not isinstance(image_info, dict) or image not in (image_info.get('RepoDigests') or [])
                        or not isinstance(image_info.get('Id'), str)
                        or not operations._DIGEST.fullmatch(image_info['Id'])
                        or not isinstance(image_info.get('Config'), dict)
                        or image_info['Config'].get('Volumes')):
                    _deny('write_backend_unverified')
                self._verify(principal, row, context, allowed_id=container_id)
                if operation == 'update':
                    previous_container, previous_detail = container_id, copy.deepcopy(detail)
                    # Keep the old container and every named volume until the
                    # replacement has real running/health evidence. Any failure
                    # leaves the stopped backup for explicit owner recovery.
                    self._verify(principal, row, context, allowed_id=container_id)
                    if detail.get('State', {}).get('Running') is True:
                        self.backend.request('POST', '/containers/' + container_id + '/stop?t=10',
                            context, authorizer=gate)
                    backup = row['contract']['spec']['name'] + '-r1-old-' + task['task_id'][5:13]
                    self.backend.request('POST', '/containers/' + container_id + '/rename?name=' + backup,
                        context, authorizer=gate)
                    expected_name = backup
                    self._finish(task, 'running', recovery_required=True)
                body = self._body(row, image)
                created = self.backend.request('POST', '/containers/create?name=' + row['contract']['spec']['name'],
                    context, body=body, authorizer=gate)
                container_id = created.get('Id') if isinstance(created, dict) else None
                if not isinstance(container_id, str) or not projects._HEX.fullmatch(container_id):
                    _deny('result_unconfirmed')
                # Persist the exact acknowledged container before starting it.
                # A crash or revocation cannot cause another blind create.
                detail = self.backend.request('GET', '/containers/' + container_id + '/json', context)
                self._check_container(row, detail, image, image_info['Id'])
                row = self._record_instance(row, {'container_id': container_id, 'image_reference': image,
                    'image_id': image_info['Id'], 'body_hash': projects._hash(body),
                    'contract_binding': row['binding']})
                expected_name = row['contract']['spec']['name']
                self._verify(principal, row, context, allowed_id=container_id)
                self.backend.request('POST', '/containers/' + container_id + '/start', context, authorizer=gate)
            elif operation == 'remove':
                if detail.get('State', {}).get('Running') is not False:
                    _deny('plan_conflict')
                mutated = True
                self.backend.request('DELETE', '/containers/' + container_id + '?v=0&force=0',
                    context, authorizer=gate)
                records = self.backend.request('GET', '/containers/json?all=1&limit=129', context)
                if any(item.get('Id') == container_id for item in records):
                    _deny('result_unconfirmed')
                self._verify(principal, row, context)
                row = self._record_instance(row, None)
                self._finish(task, 'completed', running=False)
                return
            else:
                mutated = True
                suffix = '' if operation == 'start' else '?t=10'
                self.backend.request('POST', '/containers/' + container_id + '/' + operation + suffix,
                    context, authorizer=gate)
            self._verify(principal, row, context, allowed_id=container_id)
            detail = self._inspect(row, context)
            expected_running = operation != 'stop'
            if detail.get('State', {}).get('Running') is not expected_running:
                _deny('result_unconfirmed')
            if expected_running:
                health_deadline = min(context['deadline'], self.clock() + operations.HEALTH_SECONDS)
                while detail.get('State', {}).get('Health', {}).get('Status') == 'starting':
                    self._current(principal)
                    if self.clock() >= health_deadline:
                        _deny('result_unconfirmed')
                    time.sleep(min(.1, max(0, health_deadline - self.clock())))
                    self._verify(principal, row, context, allowed_id=container_id)
                    detail = self._inspect(row, context)
                    if detail.get('State', {}).get('Running') is not True:
                        _deny('result_unconfirmed')
                if detail.get('State', {}).get('Health', {}).get('Status') not in (None, 'healthy'):
                    _deny('result_unconfirmed')
            if previous_container is not None:
                detail = self._inspect(row, context)
                if (detail.get('State', {}).get('Running') is not True
                        or detail.get('State', {}).get('Health', {}).get('Status') not in (None, 'healthy')):
                    _deny('result_unconfirmed')
                old = self.backend.request('GET', '/containers/' + previous_container + '/json', context)
                comparable = copy.deepcopy(old)
                comparable['Name'] = previous_detail['Name']
                if (not isinstance(old, dict) or old.get('Id') != previous_container
                        or old.get('State', {}).get('Running') is not False
                        or operations.DockerProjectOperations._fingerprint(comparable)
                        != operations.DockerProjectOperations._fingerprint(previous_detail)):
                    _deny('result_unconfirmed')
                self.backend.request('DELETE', '/containers/' + previous_container + '?v=0&force=0',
                    context, authorizer=lambda: self._backup_delete_gate(principal, row, context,
                        container_id, previous_container, previous_detail))
                rows = self.backend.request('GET', '/containers/json?all=1&limit=129', context)
                if any(item.get('Id') == previous_container for item in rows):
                    _deny('result_unconfirmed')
            # Publish the last actual read, never the requested target. A
            # replacement may exit after its earlier healthy observation.
            detail = self._inspect(row, context)
            actual_running = detail.get('State', {}).get('Running')
            if (actual_running is not expected_running or expected_running
                    and detail.get('State', {}).get('Health', {}).get('Status') not in (None, 'healthy')):
                _deny('result_unconfirmed')
            self._current(principal)
            self._finish(task, 'completed', running=actual_running,
                verified_target_digest=row['instance']['image_reference'].split('@', 1)[1],
                verified_image_id=row['instance']['image_id'], recovery_required=False)
        except (operations.OperationError, projects.ProjectError) as error:
            self._finish(task, 'result_unconfirmed' if mutated else 'failed', error=error.code)
        except Exception:
            self._finish(task, 'result_unconfirmed' if mutated else 'failed', error='native_unavailable')
        finally:
            lock.release()
            with self._state_lock:
                self._threads.discard(threading.current_thread())

    def _write_gate(self, principal, row, context, *, allowed_id=None, allowed_name=None):
        """Repeat native and policy proof after connect, immediately before send."""
        self._verify(principal, row, context, allowed_id=allowed_id)
        if row['instance'] is not None:
            self._inspect(row, context, allowed_name=allowed_name)
        projects._remaining(context['deadline'], self.clock)
        return self._current(principal)

    def _backup_delete_gate(self, principal, row, context, container_id, old_id, original):
        self._write_gate(principal, row, context, allowed_id=container_id,
                         allowed_name=row['contract']['spec']['name'])
        old = self.backend.request('GET', '/containers/' + old_id + '/json', context)
        if not isinstance(old, dict) or old.get('Id') != old_id or old.get('State', {}).get('Running') is not False:
            _deny('result_unconfirmed')
        checked = copy.deepcopy(old)
        checked['Name'] = original['Name']
        if (operations.DockerProjectOperations._fingerprint(checked)
                != operations.DockerProjectOperations._fingerprint(original)):
            _deny('result_unconfirmed')
        current = self._inspect(row, context)
        if (current.get('State', {}).get('Running') is not True
                or current.get('State', {}).get('Health', {}).get('Status') not in (None, 'healthy')):
            _deny('result_unconfirmed')
        projects._remaining(context['deadline'], self.clock)
        return self._current(principal)

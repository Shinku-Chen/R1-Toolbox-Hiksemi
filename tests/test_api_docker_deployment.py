"""Isolated real HTTP/Unix Engine fixtures; never touch the host daemon."""

import copy
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import unquote, urlsplit, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_docker_deployment as deployment
import api_docker_operations as operations


ENGINE = 'isolated-engine-0000001'
NETWORK = 'e' * 64
CID = 'a' * 64
IMAGE = 'example/service@sha256:' + 'b' * 64
IMAGE_ID = 'sha256:' + 'c' * 64
LINUX = sys.platform.startswith('linux') and hasattr(os, 'O_PATH') and hasattr(socket, 'SO_PEERCRED')


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        connection = self.request
        connection.settimeout(4)
        raw = b''
        while b'\r\n\r\n' not in raw:
            chunk = connection.recv(8192)
            if not chunk:
                return
            raw += chunk
        head, payload = raw.split(b'\r\n\r\n', 1)
        method, path, _ = head.split(b'\r\n', 1)[0].decode().split(' ')
        headers = dict(line.lower().split(b':', 1) for line in head.split(b'\r\n')[1:])
        length = int(headers.get(b'content-length', b'0'))
        while len(payload) < length:
            payload += connection.recv(8192)
        body = json.loads(payload) if payload else None
        fixture = self.server.fixture
        with fixture.lock:
            fixture.requests.append((method, path, copy.deepcopy(body)))
        try:
            response = fixture.before(method, path, body) if fixture.before else None
            status, value = response if response else fixture.response(method, path, body)
            if status == 0:
                return
            payload = b'' if status == 204 else (value if isinstance(value, bytes) else json.dumps(value).encode())
            raw = (f'HTTP/1.1 {status} fixture\r\nContent-Type: application/json\r\n'
                   f'Content-Length: {len(payload)}\r\n\r\n').encode() + payload
            connection.sendall(raw)
        except (OSError, ConnectionError):
            pass


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer if hasattr(socketserver, 'UnixStreamServer') else socketserver.TCPServer):
    daemon_threads = True


class Engine:
    def __init__(self, directory, *, unix=False):
        self.unix = unix
        self.networks = [{'Id': NETWORK, 'Name': 'test-net', 'Driver': 'bridge', 'Scope': 'local',
            'Ingress': False, 'ConfigOnly': False, 'Internal': False, 'EnableIPv6': False,
            'IPAM': {'Driver': 'default', 'Options': None, 'Config': []}, 'Options': {}, 'Labels': {},
            'Created': '2026-10-30T00:00:00Z'}]
        self.volumes = {'test-data': {'Name': 'test-data', 'Driver': 'local', 'Scope': 'local',
            'CreatedAt': '2026-10-30T00:00:00Z', 'Options': {}, 'Labels': {},
            'Mountpoint': '/var/lib/docker/volumes/test-data/_data'}}
        self.engine_id, self.peer = ENGINE, [os.getpid(), 100, 1, 2]
        self.containers, self.requests, self.before, self.on_connect = {}, [], None, None
        self.lock = threading.RLock()
        self.image_volumes = {}
        self.next_id = 1
        address = str(Path(directory) / 'engine.sock') if unix else ('127.0.0.1', 0)
        self.server = (UnixServer if unix else Server)(address, Handler)
        self.server.fixture = self
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()
        self.writer = deployment.LocalDockerDeploymentWriter(connect=self.connect, peer_check=self.check_peer)

    def connect(self, timeout):
        connection = socket.socket(socket.AF_UNIX if self.unix else socket.AF_INET, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        connection.connect(self.server.server_address)
        if self.on_connect:
            self.on_connect()
        return connection

    def check_peer(self, connection):
        if self.unix:
            pid, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != os.getpid() or uid != os.geteuid():
                raise operations.OperationError('native_unavailable')
        return list(self.peer)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def response(self, method, path, body):
        url, query = urlsplit(path), parse_qs(urlsplit(path).query)
        if method == 'GET' and path == '/info':
            return 200, {'ID': self.engine_id, 'Private': 'must-not-leak'}
        if method == 'GET' and path == '/networks':
            return 200, copy.deepcopy(self.networks)
        if method == 'GET' and path == '/volumes':
            return 200, {'Volumes': copy.deepcopy(list(self.volumes.values()))}
        if method == 'GET' and path.startswith('/volumes/'):
            value = self.volumes.get(path[len('/volumes/'):])
            return (200, copy.deepcopy(value)) if value else (404, {'message': 'private daemon failure'})
        if method == 'GET' and path == '/containers/json?all=1&limit=129':
            return 200, [{'Id': identity, 'Names': [row['Name']]} for identity, row in self.containers.items()]
        if method == 'POST' and url.path == '/images/create':
            return 200, b'{"status":"pulled"}\n'
        if method == 'GET' and path.startswith('/images/'):
            return 200, {'Id': IMAGE_ID, 'RepoDigests': [unquote(path[8:-5])], 'Config': {'Volumes': self.image_volumes}}
        if method == 'POST' and url.path == '/containers/create':
            identity = format(self.next_id, '064x')
            self.next_id += 1
            host = copy.deepcopy(body['HostConfig'])
            mounts = [{'Type': 'volume', 'Name': row['Source'], 'Destination': row['Target'],
                'RW': not row['ReadOnly'], 'Source': self.volumes[row['Source']]['Mountpoint']}
                for row in host['Mounts']]
            self.containers[identity] = {'Id': identity, 'Name': '/' + query['name'][0], 'Image': IMAGE_ID,
                'Config': {key: copy.deepcopy(value) for key, value in body.items() if key not in ('HostConfig', 'NetworkingConfig')},
                'HostConfig': host, 'State': {'Running': False}, 'Mounts': mounts,
                'NetworkSettings': {'Networks': copy.deepcopy(body.get('NetworkingConfig', {}).get('EndpointsConfig', {}))}}
            return 201, {'Id': identity, 'Warnings': ['private daemon message']}
        match = re.fullmatch(r'/containers/([0-9a-f]{64})(?:/(json|start|stop|restart|rename))?', url.path)
        if match and match[1] in self.containers:
            row = self.containers[match[1]]
            if method == 'GET' and match[2] == 'json':
                return 200, copy.deepcopy(row)
            if method == 'DELETE' and not match[2] and query == {'v': ['0'], 'force': ['0']}:
                if row['State']['Running']:
                    return 409, {'message': 'running'}
                del self.containers[match[1]]
                return 204, None
            if method == 'POST' and match[2] in ('start', 'stop', 'restart'):
                row['State']['Running'] = match[2] != 'stop'
                return 204, None
            if method == 'POST' and match[2] == 'rename':
                row['Name'] = '/' + query['name'][0]
                return 204, None
        return 404, {'message': 'private daemon failure'}


def spec():
    return {'name': 'test-service', 'image_repository': 'example/service', 'network': 'test-net',
            'volumes': [{'name': 'test-data', 'target': '/data', 'readonly': False}],
            'ports': [{'host_ip': '127.0.0.1', 'published': 18080, 'target': 8080, 'protocol': 'tcp'}],
            'user': '1000:1000', 'memory_bytes': 64 * 1024**2, 'nano_cpus': 500_000_000}


class DeploymentTests(unittest.TestCase):
    unix = False

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = Engine(self.directory.name, unix=self.unix)
        self.valid = True
        self.registry = deployment.DockerDeploymentRegistry(self.directory.name, backend=self.engine.writer,
            volume_identity=lambda path: [101, 102, 103])
        result = self.registry.register({'label': 'Test service', 'spec': spec()})
        self.assertTrue(result['ok'], result)
        self.identity = result['deployment']['id']
        self.principal = {'key_id': '0' * 24, 'credential_generation': 1, 'grant_revision': 1,
            'policy_version': 3, 'role': 'admin', 'scopes': ['docker'],
            'tool_grants': sorted(deployment._TOOLS),
            'docker_deployments': [{'id': self.identity, 'operations': ['inspect', 'deploy', 'control', 'remove']}]}
        self.service = deployment.DockerDeploymentOperations(self.registry,
            authorizer=lambda principal: self.valid)

    def tearDown(self):
        self.service.close()
        with self.service._state_lock:
            workers = list(self.service._threads)
        for worker in workers:
            worker.join(timeout=4)
        self.engine.close()
        self.directory.cleanup()

    def plan(self, operation='deploy'):
        result = self.service.plan(self.principal, self.identity, operation, IMAGE if operation in ('deploy', 'update') else None)
        self.assertTrue(result['ok'], result)
        return result['plan']['plan_id']

    def execute(self, plan_id=None, key='test-key'):
        plan_id = plan_id or self.plan()
        result = self.service.execute(self.principal, plan_id, True, key)
        self.assertTrue(result['ok'], result)
        identity = result['task']['task_id']
        end = time.monotonic() + 5
        while time.monotonic() < end:
            result = self.service.get_task(self.principal, self.identity, identity)
            if result.get('task', {}).get('state') not in ('pending', 'running'):
                return result
            time.sleep(.005)
        self.fail('isolated task did not terminate')

    def test_owner_catalog_metadata_and_strict_spec(self):
        self.assertEqual(self.registry.catalog()['volumes'], [{'name': 'test-data'}])
        result = self.registry.metadata()
        self.assertEqual(result['deployments'][0]['id'], self.identity)
        text = json.dumps(result)
        self.assertNotIn('/var/lib/', text)
        self.assertNotIn('must-not-leak', text)
        for field, value in [('privileged', True), ('command', ['sh']), ('environment', {'PASSWORD': 'private'})]:
            candidate = spec()
            candidate[field] = value
            self.assertFalse(self.registry.register({'label': 'x', 'spec': candidate})['ok'])

    def test_plan_is_metadata_only_and_real_deploy_preserves_resources(self):
        before = len(self.engine.requests)
        plan_id = self.plan()
        self.assertFalse(any(method != 'GET' for method, _, _ in self.engine.requests[before:]))
        result = self.execute(plan_id)
        self.assertEqual(result['task']['state'], 'completed', result)
        self.assertTrue(result['task']['running'])
        self.assertEqual(len(self.engine.containers), 1)
        self.assertEqual(set(self.engine.volumes), {'test-data'})
        create = [body for method, path, body in self.engine.requests if method == 'POST' and path.startswith('/containers/create')][0]
        self.assertTrue(create['HostConfig']['ReadonlyRootfs'])
        self.assertEqual(create['HostConfig']['CapDrop'], ['ALL'])
        self.assertEqual(create['User'], '1000:1000')
        self.assertEqual(create['HostConfig']['Mounts'][0]['VolumeOptions'], {'NoCopy': True})
        self.assertNotIn('private daemon', json.dumps(result))

    def test_stop_start_restart_remove_keep_named_volume(self):
        self.assertEqual(self.execute()['task']['state'], 'completed')
        self.assertFalse(self.service.plan(self.principal, self.identity, 'remove')['ok'])
        for index, operation in enumerate(('stop', 'start', 'restart', 'stop', 'remove')):
            result = self.execute(self.plan(operation), key='op-' + str(index))
            self.assertEqual(result['task']['state'], 'completed', result)
        self.assertFalse(self.engine.containers)
        self.assertEqual(set(self.engine.volumes), {'test-data'})
        self.assertFalse(self.registry.metadata()['deployments'][0]['deployed'])
        self.assertFalse(any('/volumes/' in path and method != 'GET' for method, path, _ in self.engine.requests))

    def test_readonly_exact_resource_and_old_keys_denied(self):
        self.principal['role'] = 'readonly'
        self.assertTrue(self.service.list_deployments(self.principal)['ok'])
        self.assertFalse(self.service.plan(self.principal, self.identity, 'deploy', IMAGE)['ok'])
        self.principal['role'] = 'admin'
        for field, value in [('scopes', []), ('tool_grants', []), ('docker_deployments', []), ('policy_version', 2)]:
            principal = copy.deepcopy(self.principal)
            principal[field] = value
            self.assertFalse(self.service.plan(principal, self.identity, 'deploy', IMAGE)['ok'])
        principal = copy.deepcopy(self.principal)
        principal['docker_deployments'][0]['operations'] = ['inspect']
        self.assertFalse(self.service.plan(principal, self.identity, 'deploy', IMAGE)['ok'])

    def test_repo_tags_commands_and_other_resource_rejected(self):
        for image in ('example/service:latest', 'evil/service@sha256:' + 'b' * 64, IMAGE + '\n'):
            self.assertFalse(self.service.plan(self.principal, self.identity, 'deploy', image)['ok'])
        self.assertFalse(self.service.plan(self.principal, 'deployment_' + 'f' * 24, 'deploy', IMAGE)['ok'])
        self.assertFalse(self.service.plan(self.principal, self.identity, 'exec', IMAGE)['ok'])

    def test_spec_rejects_host_bind_root_toolbox_and_wildcard_ports(self):
        changes = [('network', 'host'), ('user', '0:0'), ('name', 'r1-toolbox'), ('image_repository', 'example/r1-toolbox')]
        for key, value in changes:
            body = spec()
            body[key] = value
            self.assertFalse(self.registry.register({'label': 'test', 'spec': body})['ok'])
        for value in ('0.0.0.0', '::', '8.8.8.8', '127.0.0.1\n'):
            body = spec()
            body['ports'][0]['host_ip'] = value
            self.assertFalse(self.registry.register({'label': 'test', 'spec': body})['ok'])
        for value in ('r1_toolbox_data', '/host/path'):
            body = spec()
            body['volumes'][0]['name'] = value
            self.assertFalse(self.registry.register({'label': 'test', 'spec': body})['ok'])

    def test_volume_replacement_and_network_change_fail_before_pull(self):
        for kind in ('volume', 'network'):
            plan_id = self.plan()
            if kind == 'volume':
                self.engine.volumes['test-data']['CreatedAt'] = 'replacement'
            else:
                self.engine.networks[0]['Id'] = 'f' * 64
            before = len(self.engine.requests)
            result = self.execute(plan_id, key=kind)
            self.assertEqual(result['task']['state'], 'failed', result)
            self.assertFalse(any(method != 'GET' for method, _, _ in self.engine.requests[before:]))
            self.engine.volumes['test-data']['CreatedAt'] = '2026-10-30T00:00:00Z'
            self.engine.networks[0]['Id'] = NETWORK

    def test_name_conflict_never_creates_or_adopts_other_container(self):
        plan_id = self.plan()
        self.engine.containers[CID] = {'Name': '/test-service'}
        result = self.execute(plan_id)
        self.assertEqual(result['task']['state'], 'failed')
        self.assertEqual(set(self.engine.containers), {CID})

    def test_implicit_image_volumes_are_rejected_after_pull_before_create(self):
        self.engine.image_volumes = {'/unexpected': {}}
        result = self.execute()
        self.assertEqual(result['task']['state'], 'result_unconfirmed')
        self.assertFalse(self.engine.containers)

    def test_owner_revocation_invalidates_existing_plan(self):
        plan_id = self.plan()
        self.assertTrue(self.registry.revoke({'deployment_id': self.identity})['ok'])
        result = self.service.execute(self.principal, plan_id, True, 'key')
        # Background acceptance is not success; the task cannot send a mutation.
        if result['ok']:
            with self.service._state_lock:
                threads = list(self.service._threads)
            for thread in threads:
                thread.join(timeout=3)
        self.assertFalse(any(method != 'GET' for method, _, _ in self.engine.requests))

    def test_idempotent_replay_and_conflict_cross_operations(self):
        plan_id = self.plan()
        result = self.execute(plan_id, key='same')
        replay = self.service.execute(self.principal, plan_id, True, 'same')
        self.assertEqual(replay['task']['task_id'], result['task']['task_id'])
        other = self.plan('stop')
        denied = self.service.execute(self.principal, other, True, 'same')
        self.assertEqual(denied['error']['code'], 'idempotency_conflict')
        self.assertEqual(sum(method == 'POST' and path.startswith('/containers/create') for method, path, _ in self.engine.requests), 1)

    def test_confirmation_expiration_rotation_and_shutdown(self):
        plan_id = self.plan()
        self.assertFalse(self.service.execute(self.principal, plan_id, 1, 'x')['ok'])
        self.service._plans[plan_id]['expires'] = 0
        self.assertEqual(self.service.execute(self.principal, plan_id, True, 'x')['error']['code'], 'plan_expired')
        other = self.plan()
        changed = copy.deepcopy(self.principal)
        changed['credential_generation'] += 1
        self.assertFalse(self.service.execute(changed, other, True, 'x')['ok'])
        self.service.close()
        self.assertFalse(self.service.execute(self.principal, other, True, 'x')['ok'])

    def test_ambiguous_create_never_replayed_or_rolled_back(self):
        def before(method, path, body):
            if method == 'POST' and path.startswith('/containers/create'):
                self.engine.response(method, path, body)
                return 0, None
        self.engine.before = before
        plan_id = self.plan()
        result = self.execute(plan_id)
        self.assertEqual(result['task']['state'], 'result_unconfirmed', result)
        self.assertEqual(len(self.engine.containers), 1)
        self.service.execute(self.principal, plan_id, True, 'test-key')
        self.assertEqual(sum(method == 'POST' and path.startswith('/containers/create') for method, path, _ in self.engine.requests), 1)
        self.assertFalse(any(method == 'DELETE' for method, _, _ in self.engine.requests))

    def test_revocation_at_create_connection_closes_without_body(self):
        plan_id = self.plan()
        # Observe fixed verification steps rather than allowing a revoked body
        # through a mocked dispatcher. The real HTTP socket is already open.
        original = self.engine.writer.request
        def request(method, path, context, **kwargs):
            if method == 'POST' and path.startswith('/containers/create'):
                self.engine.on_connect = lambda: setattr(self, 'valid', False)
            return original(method, path, context, **kwargs)
        self.engine.writer.request = request
        result = self.service.execute(self.principal, plan_id, True, 'revoked')
        self.assertTrue(result['ok'], result)
        with self.service._state_lock:
            threads = list(self.service._threads)
        for thread in threads:
            thread.join(timeout=4)
        self.assertFalse(self.engine.containers)
        self.assertFalse(self.service.get_task(self.principal, self.identity, result['task']['task_id'])['ok'])

    def test_update_replaces_container_and_preserves_volume(self):
        self.assertEqual(self.execute()['task']['state'], 'completed')
        old = next(iter(self.engine.containers))
        image = 'example/service@sha256:' + 'd' * 64
        plan = self.service.plan(self.principal, self.identity, 'update', image)
        self.assertTrue(plan['ok'], plan)
        result = self.execute(plan['plan']['plan_id'], key='update-key')
        self.assertEqual(result['task']['state'], 'completed', result)
        self.assertNotIn(old, self.engine.containers)
        self.assertEqual(len(self.engine.containers), 1)
        self.assertEqual(set(self.engine.volumes), {'test-data'})
        self.assertFalse(result['task']['recovery_required'])

    def test_failed_update_keeps_stopped_backup_without_blind_rollback(self):
        self.assertEqual(self.execute()['task']['state'], 'completed')
        old = next(iter(self.engine.containers))
        def before(method, path, body):
            if method == 'POST' and path.startswith('/containers/create'):
                return 500, {'message': 'private daemon failure'}
        self.engine.before = before
        result = self.execute(self.plan('update'), key='update-key')
        self.assertEqual(result['task']['state'], 'result_unconfirmed', result)
        self.assertFalse(self.engine.containers[old]['State']['Running'])
        self.assertIn('-r1-old-', self.engine.containers[old]['Name'])
        self.assertTrue(result['task']['recovery_required'])
        self.assertEqual(set(self.engine.volumes), {'test-data'})

    def test_shared_global_idempotency_and_task_capacity(self):
        other_registry = type('Registry', (), {'backend': self.engine.writer})()
        shared = operations.DockerProjectOperations(other_registry, authorizer=lambda principal: self.valid,
                                                     backend=self.engine.writer)
        service = deployment.DockerDeploymentOperations(self.registry, authorizer=lambda principal: self.valid,
                                                        shared_operations=shared)
        self.assertIs(service._idempotency, shared._idempotency)
        self.assertIs(service._tasks, shared._tasks)
        self.assertIs(service._threads, shared._threads)
        shared._reserve_task(self.principal, 'project_' + 'f' * 24, 'stop', 'taken-key')
        plan = service.plan(self.principal, self.identity, 'deploy', IMAGE)
        self.assertTrue(plan['ok'], plan)
        conflict = service.execute(self.principal, plan['plan']['plan_id'], True, 'taken-key')
        self.assertEqual(conflict['error']['code'], 'idempotency_conflict')
        self.assertFalse(any(method != 'GET' for method, _, _ in self.engine.requests))
        service.close()
        self.assertTrue(shared._closed.is_set())

    def test_configuration_tamper_after_stop_connect_is_not_sent(self):
        self.assertEqual(self.execute()['task']['state'], 'completed')
        identity = next(iter(self.engine.containers))
        plan_id = self.plan('stop')
        original = self.engine.writer.request
        def request(method, path, context, **kwargs):
            if method == 'POST' and path.endswith('/stop?t=10'):
                self.engine.on_connect = lambda: self.engine.containers[identity]['HostConfig'].update(Privileged=True)
            return original(method, path, context, **kwargs)
        self.engine.writer.request = request
        result = self.execute(plan_id, key='tampered')
        self.assertEqual(result['task']['state'], 'result_unconfirmed')
        self.assertTrue(self.engine.containers[identity]['State']['Running'])
        self.assertFalse(any(method == 'POST' and path.endswith('/stop?t=10') for method, path, _ in self.engine.requests))

    def test_peer_and_engine_replacement_do_not_pull(self):
        for kind in ('peer', 'engine'):
            plan_id = self.plan()
            if kind == 'peer':
                self.engine.peer[1] += 1
            else:
                self.engine.engine_id = 'replacement-engine-0001'
            before = len(self.engine.requests)
            result = self.execute(plan_id, key=kind)
            self.assertEqual(result['task']['state'], 'failed')
            self.assertFalse(any(method != 'GET' for method, _, _ in self.engine.requests[before:]))
            self.engine.peer[1] = 100
            self.engine.engine_id = ENGINE

    def test_concurrent_same_resource_has_one_worker_and_one_create(self):
        plan_id = self.plan()
        entered, release = threading.Event(), threading.Event()
        def before(method, path, body):
            if method == 'POST' and path.startswith('/images/create'):
                entered.set()
                release.wait(3)
        self.engine.before = before
        first = self.service.execute(self.principal, plan_id, True, 'concurrent')
        self.assertTrue(first['ok'], first)
        self.assertTrue(entered.wait(2))
        replay = self.service.execute(self.principal, plan_id, True, 'concurrent')
        self.assertEqual(first['task']['task_id'], replay['task']['task_id'])
        denied = self.service.execute(self.principal, plan_id, True, 'other-key')
        self.assertFalse(denied['ok'])
        self.assertEqual(denied['error']['code'], 'resource_busy')
        release.set()
        with self.service._state_lock:
            workers = list(self.service._threads)
        for worker in workers:
            worker.join(timeout=4)
        self.assertEqual(sum(method == 'POST' and path.startswith('/containers/create') for method, path, _ in self.engine.requests), 1)

    def test_deadline_expiring_after_connect_sends_no_mutation(self):
        tick = [0.0]
        self.engine.writer._clock = lambda: tick[0]
        self.service.clock = lambda: tick[0]
        plan_id = self.plan()
        self.engine.on_connect = lambda: tick.__setitem__(0, 121.0)
        result = self.execute(plan_id, key='deadline')
        self.assertEqual(result['task']['state'], 'failed')
        self.assertFalse(any(method != 'GET' for method, _, _ in self.engine.requests))

    def test_health_starting_waits_for_real_healthy_state(self):
        reads = [0]
        def before(method, path, body):
            if method == 'POST' and path.endswith('/start'):
                identity = path.split('/')[2]
                self.engine.containers[identity]['State']['Health'] = {'Status': 'starting'}
            if method == 'GET' and path.endswith('/json') and '/containers/' in path:
                identity = path.split('/')[2]
                if self.engine.containers[identity]['State']['Running']:
                    reads[0] += 1
                    if reads[0] >= 3:
                        self.engine.containers[identity]['State']['Health'] = {'Status': 'healthy'}
        self.engine.before = before
        result = self.execute()
        self.assertEqual(result['task']['state'], 'completed', result)
        self.assertGreaterEqual(reads[0], 3)

    def test_unhealthy_update_keeps_both_actual_containers(self):
        self.assertEqual(self.execute()['task']['state'], 'completed')
        old = next(iter(self.engine.containers))
        def before(method, path, body):
            if method == 'POST' and path.endswith('/start'):
                identity = path.split('/')[2]
                self.engine.containers[identity]['State']['Health'] = {'Status': 'unhealthy'}
        self.engine.before = before
        result = self.execute(self.plan('update'), key='health-failed')
        self.assertEqual(result['task']['state'], 'result_unconfirmed', result)
        self.assertTrue(result['task']['recovery_required'])
        self.assertEqual(len(self.engine.containers), 2)
        self.assertFalse(self.engine.containers[old]['State']['Running'])
        self.assertFalse(any(method == 'DELETE' for method, _, _ in self.engine.requests))

    def test_default_engine_mount_option_fields_are_accepted_without_new_sources(self):
        original = self.engine.response
        def response(method, path, body):
            result = original(method, path, body)
            if method == 'POST' and path.startswith('/containers/create'):
                for container in self.engine.containers.values():
                    container['HostConfig']['Mounts'][0]['VolumeOptions'].update(
                        Labels=None, DriverConfig=None, Subpath='')
            return result
        self.engine.response = response
        self.assertEqual(self.execute()['task']['state'], 'completed')

    def test_owner_refresh_rebinds_lifecycle_but_not_other_engine_or_container(self):
        self.assertEqual(self.execute()['task']['state'], 'completed')
        old_plan = self.plan('stop')
        self.engine.peer[1] += 1
        self.assertFalse(self.service.plan(self.principal, self.identity, 'stop')['ok'])
        refreshed = self.registry.refresh({'deployment_id': self.identity, 'confirm': True})
        self.assertTrue(refreshed['ok'], refreshed)
        result = self.execute(old_plan, key='old-plan')
        self.assertEqual(result['task']['state'], 'failed')
        self.assertEqual(self.execute(self.plan('stop'), key='fresh-plan')['task']['state'], 'completed')
        self.engine.engine_id = 'replacement-engine-0001'
        self.assertFalse(self.registry.refresh({'deployment_id': self.identity, 'confirm': True})['ok'])

    def test_refresh_rejects_changed_instance_and_non_boolean_confirmation(self):
        self.assertFalse(self.registry.refresh({'deployment_id': self.identity, 'confirm': 1})['ok'])
        self.assertEqual(self.execute()['task']['state'], 'completed')
        identity = next(iter(self.engine.containers))
        self.engine.containers[identity]['Image'] = 'sha256:' + 'f' * 64
        self.assertFalse(self.registry.refresh({'deployment_id': self.identity, 'confirm': True})['ok'])

    def test_actual_toolbox_data_volume_is_protected_without_compose_label(self):
        name = 'owner-custom-state'
        volume = copy.deepcopy(self.engine.volumes['test-data'])
        volume.update(Name=name, Mountpoint='/var/lib/docker/volumes/' + name + '/_data')
        self.engine.volumes[name] = volume
        self.engine.containers[CID] = {'Id': CID, 'Name': '/custom-toolbox',
            'Config': {'Image': 'ghcr.io/shinku-chen/r1-toolbox:preview', 'Labels': {}},
            'Mounts': [{'Type': 'volume', 'Name': name, 'Destination': '/data'}]}
        self.assertNotIn({'name': name}, self.registry.catalog()['volumes'])
        body = spec()
        body['name'] = 'other-service'
        body['volumes'][0]['name'] = name
        self.assertFalse(self.registry.register({'label': 'test', 'spec': body})['ok'])

    def test_similar_volume_name_is_not_mistaken_for_toolbox_state(self):
        name = 'user_r1_toolbox_data'
        volume = copy.deepcopy(self.engine.volumes['test-data'])
        volume.update(Name=name, Mountpoint='/var/lib/docker/volumes/' + name + '/_data',
                      Labels={'com.docker.compose.volume': 'independent-media-data'})
        self.engine.volumes[name] = volume
        body = spec()
        body['name'] = 'other-service'
        body['volumes'][0]['name'] = name
        result = self.registry.register({'label': 'test', 'spec': body})
        self.assertTrue(result['ok'], result)
        self.assertIn({'name': name}, self.registry.catalog()['volumes'])

    def test_toolbox_mount_change_after_plan_is_rechecked_before_pull(self):
        plan_id = self.plan()
        self.engine.containers[CID] = {'Id': CID, 'Name': '/custom-toolbox',
            'Config': {'Image': 'custom/toolbox-image@sha256:' + 'f' * 64,
                       'Labels': {'org.opencontainers.image.title': 'R1 Toolbox'}},
            'Mounts': [{'Type': 'volume', 'Name': 'test-data', 'Destination': '/data'}]}
        result = self.execute(plan_id)
        self.assertEqual(result['task']['state'], 'failed')
        self.assertFalse(any(method != 'GET' for method, _, _ in self.engine.requests))

    def _assert_worker_context_failure(self, failure):
        plan_id = self.plan()
        original = self.engine.writer.context
        def failed_context():
            raise failure
        self.engine.writer.context = failed_context
        try:
            result = self.execute(plan_id, key='context-failed')
            self.assertEqual(result['task']['state'], 'failed', result)
            with self.service._state_lock:
                workers = list(self.service._threads)
            for worker in workers:
                worker.join(timeout=2)
            self.assertFalse(self.service._threads)
            self.assertFalse(any(method != 'GET' for method, _, _ in self.engine.requests))
        finally:
            self.engine.writer.context = original
        # This is a new plan/task, not replay of the consumed failed plan. The
        # actual submitter must reacquire the original resource lock and run.
        fresh_plan = self.plan()
        self.assertEqual(self.execute(fresh_plan, key='context-recovered')['task']['state'], 'completed')

    def test_worker_oserror_context_failure_releases_lock_and_task(self):
        self._assert_worker_context_failure(OSError('private backend error'))

    def test_worker_project_context_failure_releases_lock_and_task(self):
        self._assert_worker_context_failure(operations.projects.ProjectError('native_unavailable'))

    def test_registry_corruption_is_fail_closed(self):
        self.registry.path.write_text('{"version":1,"revision":0,"deployments":[{}]}', encoding='utf-8')
        self.assertFalse(self.registry.metadata()['ok'])
        self.assertFalse(self.service.list_deployments(self.principal)['ok'])


@unittest.skipUnless(LINUX, 'requires Linux Unix peer credentials and directory handles')
class LinuxDeploymentTests(DeploymentTests):
    unix = True

    def test_volume_directory_identity_requires_same_real_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / 'first'
            second = Path(folder) / 'second'
            first.mkdir()
            second.mkdir()
            original = deployment._volume_path_identity(str(first))
            self.assertEqual(original, deployment._volume_path_identity(str(first)))
            self.assertNotEqual(original, deployment._volume_path_identity(str(second)))
            link = Path(folder) / 'linked'
            link.symlink_to(first, target_is_directory=True)
            with self.assertRaises(operations.OperationError):
                deployment._volume_path_identity(str(link))


if __name__ == '__main__':
    unittest.main()

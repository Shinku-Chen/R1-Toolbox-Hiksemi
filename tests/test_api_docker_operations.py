"""Real isolated Engine HTTP fixtures; never bind the host Docker socket.

Portable tests exercise bounded HTTP over loopback. Linux lifecycle tests use
actual AF_UNIX, O_PATH configuration identity, files and real container records.
The fixture is explicitly not evidence that a NAS or Docker daemon was tested.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_docker_projects as projects
import api_docker_operations as operations
from api_contracts import contract_catalog


CID = 'a' * 64
NEW = 'b' * 64
ENGINE = 'isolated-engine-0000001'
CURRENT = 'example/app:old'
TARGET = 'example/app@sha256:' + 'd' * 64
IMAGE_ID = 'sha256:' + 'e' * 64
SECRET = 'private-fixture-env-and-command'
LINUX = sys.platform.startswith('linux') and hasattr(os, 'O_PATH') and hasattr(socket, 'AF_UNIX')


class Engine:
    def __init__(self, directory, *, unix=False):
        self.directory = Path(directory)
        self.file = self.directory / 'compose.yml'
        self.file.write_text(json.dumps({'services': {'web': {'image': CURRENT,
                              'environment': {'SECRET': SECRET}}}}), encoding='utf-8')
        os.chmod(self.file, 0o600)
        self.compose = str(self.file) if unix else '/fixture/compose.yml'
        self.unix = unix
        self.requests, self.containers, self.errors = [], {}, []
        self.before = None
        self.engine_id = ENGINE
        self.peer = [os.getpid(), 100, 1, 2]
        self.present_image = True
        self.chunked = False
        self.container(CID, '/demo-web-1', CURRENT, 'sha256:' + 'c' * 64, True)
        self.listener = socket.socket(socket.AF_UNIX if unix else socket.AF_INET, socket.SOCK_STREAM)
        if unix:
            self.address = str(self.directory / 'fixture.sock')
        else:
            self.address = ('127.0.0.1', 0)
        self.listener.bind(self.address)
        if not unix:
            self.address = self.listener.getsockname()
        self.listener.listen(16)
        self.listener.settimeout(.1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        self.writer = operations.LocalDockerWriter(connect=self.connect,
            peer_check=self.peer_identity,
            config_identity=None if unix else self.configuration)

    def container(self, identity, name, image, image_id, running):
        labels = {projects._LABEL + 'project': 'demo', projects._LABEL + 'service': 'web',
                  projects._LABEL + 'config_files': self.compose,
                  projects._LABEL + 'project.config_files': self.compose,
                  projects._LABEL + 'project.working_dir': str(Path(self.compose).parent).replace('\\', '/'),
                  projects._LABEL + 'config-hash': 'f' * 64,
                  projects._LABEL + 'oneoff': 'False'}
        self.containers[identity] = {'Id': identity, 'Name': name, 'Image': image_id,
            'Config': {'Image': image, 'Labels': labels, 'Env': ['SECRET=' + SECRET],
                       'Cmd': ['server', SECRET], 'Volumes': {}},
            'HostConfig': {'Privileged': False, 'NetworkMode': 'bridge', 'CapAdd': [],
                           'Devices': [], 'RestartPolicy': {'Name': 'unless-stopped'}, 'Mounts': []},
            'State': {'Running': running}, 'Mounts': [], 'NetworkSettings': {'Ports': {}}}

    def configuration(self, path):
        if path != self.compose:
            raise projects.ProjectError('project_binding_changed')
        info = self.file.stat()
        return {'path': path, 'identity': [info.st_dev, info.st_ino, info.st_size,
                                          info.st_mtime_ns, info.st_ctime_ns]}

    def peer_identity(self, connection):
        if self.unix:
            pid, uid, gid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != os.getpid() or uid != os.geteuid():
                raise projects.ProjectError('native_unavailable')
        return list(self.peer)

    def connect(self, timeout):
        connection = socket.socket(socket.AF_UNIX if self.unix else socket.AF_INET, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        connection.connect(self.address)
        return connection

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)
        self.listener.close()
        if self.unix:
            Path(self.address).unlink(missing_ok=True)

    def run(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                try:
                    connection.settimeout(3)
                    raw = b''
                    while b'\r\n\r\n' not in raw:
                        chunk = connection.recv(8192)
                        if not chunk:
                            break
                        raw += chunk
                    if b'\r\n\r\n' not in raw:
                        continue  # A fail-closed client can close before sending.
                    header, body = raw.split(b'\r\n\r\n', 1)
                    method, path, _ = header.split(b'\r\n')[0].decode().split(' ')
                    headers = dict(line.lower().split(b':', 1) for line in header.split(b'\r\n')[1:])
                    size = int(headers.get(b'content-length', b'0'))
                    while len(body) < size:
                        body += connection.recv(8192)
                    value = json.loads(body) if body else None
                    self.requests.append((method, path, value))
                    result = self.before(method, path, value) if self.before else None
                    status, value = result if result is not None else self.response(method, path, value)
                    body = b'' if status in (204, 304) else (value if isinstance(value, bytes)
                            else json.dumps(value, separators=(',', ':')).encode())
                    if self.chunked and status not in (204, 304):
                        response = (f'HTTP/1.1 {status} fixture\r\nContent-Type: application/json\r\n'
                                    'Transfer-Encoding: chunked\r\n\r\n').encode()
                        response += format(len(body), 'x').encode() + b'\r\n' + body + b'\r\n0\r\n\r\n'
                    else:
                        response = (f'HTTP/1.1 {status} fixture\r\nContent-Type: application/json\r\n'
                                    f'Content-Length: {len(body)}\r\n\r\n').encode() + body
                    connection.sendall(response)
                except (ConnectionError, BrokenPipeError):
                    pass
                except Exception as error:
                    self.errors.append(type(error).__name__)

    def response(self, method, path, body):
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if method == 'GET' and path == '/info':
            return 200, {'ID': self.engine_id, 'RegistryConfig': {SECRET: SECRET}}
        if method == 'GET' and path == '/containers/json?all=1&limit=129':
            return 200, [{'Id': identity, 'Labels': detail['Config']['Labels']}
                         for identity, detail in self.containers.items()]
        if method == 'GET' and parsed.path.startswith('/volumes/'):
            return 200, {'Name': parsed.path[len('/volumes/'):], 'Labels': {}}
        if method == 'GET' and parsed.path.startswith('/images/'):
            if not self.present_image:
                return 404, {'message': SECRET}
            return 200, {'Id': IMAGE_ID, 'RepoDigests': [TARGET],
                         'RootFS': {'Layers': ['sha256:' + '1' * 64]}, 'Config': {'Volumes': {}}}
        if method == 'POST' and parsed.path == '/images/create':
            self.present_image = True
            return 200, b'{"status":"complete"}\n'
        if method == 'POST' and parsed.path == '/containers/create':
            self.container(NEW, '/' + query['name'][0], body['Image'], IMAGE_ID, False)
            self.containers[NEW]['Config'] = {key: value for key, value in body.items()
                                            if key not in ('HostConfig', 'NetworkingConfig')}
            self.containers[NEW]['HostConfig'] = body['HostConfig']
            destinations = {mount.get('Target') for mount in body['HostConfig'].get('Mounts', [])}
            destinations.update(binding.split(':')[1] for binding in body['HostConfig'].get('Binds', []))
            self.containers[NEW]['Mounts'] = [copy.deepcopy(mount) for mount in self.containers[CID]['Mounts']
                                             if mount.get('Destination') in destinations]
            if body.get('NetworkingConfig'):
                self.containers[NEW]['NetworkSettings']['Networks'] = copy.deepcopy(
                    body['NetworkingConfig']['EndpointsConfig'])
            return 201, {'Id': NEW, 'Warnings': [SECRET]}
        match = re.fullmatch(r'/containers/([0-9a-f]{64})(?:/(json|start|stop|restart|rename))?', parsed.path)
        if not match or match[1] not in self.containers:
            return 404, {'message': SECRET}
        identity, action = match[1], match[2]
        if method == 'GET' and action == 'json':
            return 200, copy.deepcopy(self.containers[identity])
        if method == 'DELETE' and action is None:
            if self.containers[identity]['State']['Running']:
                return 409, {'message': SECRET}
            del self.containers[identity]
            return 204, None
        if method == 'POST' and action in ('start', 'stop', 'restart'):
            self.containers[identity]['State']['Running'] = action != 'stop'
            return 204, None
        if method == 'POST' and action == 'rename':
            name = '/' + query['name'][0]
            if any(detail['Name'] == name for key, detail in self.containers.items() if key != identity):
                return 409, {'message': SECRET}
            self.containers[identity]['Name'] = name
            return 204, None
        return 400, {'message': SECRET}


class ControlHTTPTests(unittest.TestCase):
    unix = False

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-dop-')
        self.addCleanup(temporary.cleanup)
        self.engine = Engine(temporary.name, unix=self.unix)
        self.addCleanup(self.engine.close)
        self.registry = projects.DockerProjectRegistry(temporary.name, backend=self.engine.writer)
        candidate = self.registry.catalog()['candidates'][0]
        self.project = self.registry.register({'candidate_id': candidate['id']})['project']['id']
        self.active = True
        self.ops = operations.DockerProjectOperations(self.registry, authorizer=lambda _: self.active,
                                                      backend=self.engine.writer)
        self.addCleanup(self.shutdown_operations)
        self.principal = {'key_id': '1' * 24, 'credential_generation': 1, 'grant_revision': 1,
            'policy_version': 3, 'role': 'admin', 'scopes': ['docker'],
            'tool_grants': ['control_docker_project', 'plan_docker_project_update',
                            'execute_docker_project_update', 'get_docker_project_task'],
            'docker_projects': [{'id': self.project, 'operations': ['inspect', 'control', 'update_image']}]}
        self.schemas = {row['name']: row['outputSchema'] for row in contract_catalog()['tools']}

    def shutdown_operations(self):
        self.ops.close()
        with self.ops._state_lock:
            threads = list(self.ops._threads)
        for worker in threads:
            worker.join(timeout=5)

    def control(self, action='stop', key='once', principal=None):
        return self.ops.control(principal or self.principal, self.project, action, True, key)

    def test_start_stop_restart_real_http_and_safe_task_shape(self):
        for action in ('stop', 'start', 'restart'):
            value = self.control(action, action)
            self.assertTrue(value['ok'], value)
            self.assertEqual(value['task']['state'], 'completed', value)
            self.assertEqual(value['task']['running'], action != 'stop')
            result = self.ops.get_task(self.principal, self.project, value['task']['task_id'])
            Draft202012Validator(self.schemas['get_docker_project_task'], format_checker=FormatChecker()).validate(result)
            self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.engine.errors, [])

    def test_idempotency_is_shared_for_the_instance_and_conflicting_keys_never_repeat(self):
        first = self.control()
        count = len(self.engine.requests)
        self.assertEqual(self.control(), first)
        self.assertEqual(len(self.engine.requests), count)
        self.assertEqual(self.control('start')['error']['code'], 'idempotency_conflict')
        restarted = operations.DockerProjectOperations(self.registry, authorizer=lambda _: True, backend=self.engine.writer)
        self.assertEqual(restarted.get_task(self.principal, self.project, first['task']['task_id'])['error']['code'], 'task_unavailable')

    def test_roles_domains_exact_tools_project_operations_and_confirmation_all_gate_writes(self):
        invalid = [dict(self.principal, role='readonly'), dict(self.principal, role='readwrite'),
                   dict(self.principal, policy_version=2), dict(self.principal, tool_grants=[]),
                   dict(self.principal, scopes=[]), dict(self.principal, docker_projects=[]),
                   dict(self.principal, docker_projects=[{'id': self.project, 'operations': ['inspect']}])]
        for principal in invalid:
            self.assertFalse(self.control(principal=principal)['ok'])
        self.assertFalse(self.ops.control(self.principal, self.project, 'stop', False, 'bad')['ok'])
        self.assertFalse(self.ops.control(self.principal, self.project, 'exec', True, 'bad')['ok'])
        self.assertFalse(any(method != 'GET' for method, unused, unused2 in self.engine.requests))

    def test_revoke_before_send_or_during_response_stops_later_mutations_and_discards_result(self):
        def hook(method, path, body):
            if method == 'POST':
                self.active = False
        self.engine.before = hook
        value = self.control()
        self.assertEqual(value['error']['code'], 'invalid_key')
        self.assertEqual(sum(method == 'POST' for method, unused, unused2 in self.engine.requests), 1)
        self.assertFalse(self.engine.containers[CID]['State']['Running'])

    def test_protected_volume_and_binding_change_are_not_operated(self):
        self.engine.containers[CID]['Mounts'] = [{'Type': 'volume', 'Name': 'r1-toolbox-data', 'RW': True}]
        value = self.control()
        self.assertIn(value.get('error', value.get('task', {}).get('error'))['code'],
                      ('permission_denied', 'project_binding_changed'))
        self.assertFalse(any(method != 'GET' for method, unused, unused2 in self.engine.requests))

    def test_transport_allowlist_and_safe_error_body_rejection(self):
        for method, path in (('POST', '/containers/' + CID + '/exec'), ('GET', '/etc/shadow'),
                             ('DELETE', '/containers/' + CID + '?v=1&force=1')):
            with self.assertRaises(operations.OperationError):
                self.engine.writer.request(method, path, self.engine.writer.context(), authorizer=lambda: True)
        self.engine.before = lambda method, path, body: (500, {'message': SECRET}) if method == 'POST' else None
        value = self.control()
        self.assertEqual(value['task']['state'], 'result_unconfirmed')
        self.assertNotIn(SECRET, json.dumps(value))
        count = len(self.engine.requests)
        self.assertEqual(self.control(), value)
        self.assertEqual(len(self.engine.requests), count)


@unittest.skipUnless(LINUX, 'requires Linux AF_UNIX/O_PATH; fixture never uses NAS Docker')
class UnixUpdateTests(ControlHTTPTests):
    unix = True

    def plan(self):
        service = self.engine.writer.snapshot()[0]['services'][0]['service_id']
        return self.ops.plan_update(self.principal, self.project, service, TARGET)

    def execute(self, plan_id, confirmed, key):
        result = self.ops.execute_update(self.principal, plan_id, confirmed, key)
        if not result['ok']:
            return result
        with self.ops._state_lock:
            threads = list(self.ops._threads)
        for worker in threads:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), 'isolated update task exceeded test deadline')
        return self.ops.get_task(self.principal, self.project, result['task']['task_id'])

    def test_image_plan_execute_config_cas_task_and_volume_safe_retirement(self):
        planned = self.plan()
        self.assertTrue(planned['ok'], planned)
        Draft202012Validator(self.schemas['plan_docker_project_update'], format_checker=FormatChecker()).validate(planned)
        self.assertFalse(any(method != 'GET' for method, unused, unused2 in self.engine.requests))
        executed = self.execute(planned['plan']['plan_id'], True, 'image-once')
        self.assertEqual(executed['task']['state'], 'completed', executed)
        Draft202012Validator(self.schemas['execute_docker_project_update'], format_checker=FormatChecker()).validate(executed)
        self.assertEqual(executed['task']['verified_image_id'], IMAGE_ID)
        self.assertEqual(executed['task']['verified_target_digest'], TARGET.split('@')[1])
        self.assertEqual(json.loads(self.engine.file.read_text())['services']['web']['image'], TARGET)
        self.assertNotIn(CID, self.engine.containers)
        self.assertEqual(self.engine.containers[NEW]['Name'], '/demo-web-1')
        self.assertEqual(self.engine.requests[-1][0], 'GET')
        self.assertIn(('DELETE', '/containers/' + CID + '?v=0&force=0', None), self.engine.requests)
        self.assertNotIn(SECRET, json.dumps(executed))
        count = len(self.engine.requests)
        self.assertEqual(self.execute(planned['plan']['plan_id'], True, 'image-once'), executed)
        self.assertEqual(len(self.engine.requests), count)

    def test_yaml_image_change_preserves_other_bytes_and_works_without_yaml_dependency(self):
        self.engine.file.write_text('services:\n  web:\n    image: example/app:old\n'
                                   '    environment:\n      SECRET: ' + SECRET + '\n', encoding='utf-8')
        self.registry.revoke({'project_id': self.project})
        candidate = self.registry.catalog()['candidates'][0]
        self.project = self.registry.register({'candidate_id': candidate['id']})['project']['id']
        self.principal['docker_projects'][0]['id'] = self.project
        planned = self.plan()
        self.assertTrue(planned['ok'], planned)
        result = self.execute(planned['plan']['plan_id'], True, 'yaml-update')
        self.assertEqual(result['task']['state'], 'completed', result)
        self.assertIn('    image: ' + TARGET, self.engine.file.read_text())
        self.assertIn('      SECRET: ' + SECRET, self.engine.file.read_text())

    def test_expired_conflicting_restarted_or_cross_credential_plan_never_mutates(self):
        planned = self.plan()['plan']['plan_id']
        self.ops._plans[planned]['expires'] = self.ops.clock() - 1
        self.assertEqual(self.ops.execute_update(self.principal, planned, True, 'expired')['error']['code'], 'plan_expired')
        restarted = operations.DockerProjectOperations(self.registry, authorizer=lambda _: True, backend=self.engine.writer)
        self.assertEqual(restarted.execute_update(self.principal, planned, True, 'new')['error']['code'], 'plan_conflict')
        changed = dict(self.principal, credential_generation=2)
        self.assertEqual(self.ops.execute_update(changed, planned, True, 'other')['error']['code'], 'plan_conflict')
        self.assertFalse(any(method != 'GET' for method, unused, unused2 in self.engine.requests))

    def test_config_change_after_plan_is_rejected_before_any_engine_write(self):
        planned = self.plan()['plan']['plan_id']
        self.engine.file.write_text('{}')
        result = self.execute(planned, True, 'changed')
        self.assertEqual(result['task']['state'], 'failed')
        self.assertEqual(result['task']['error']['code'], 'project_binding_changed')
        self.assertFalse(any(method != 'GET' for method, unused, unused2 in self.engine.requests))

    def test_pull_failure_or_revoke_after_create_keeps_original_and_never_retries(self):
        planned = self.plan()['plan']['plan_id']
        def hook(method, path, body):
            if method == 'POST' and path.startswith('/containers/create'):
                self.active = False
        self.engine.before = hook
        result = self.execute(planned, True, 'revoked')
        self.assertEqual(result['error']['code'], 'invalid_key')
        self.assertIn(CID, self.engine.containers)
        self.assertTrue(self.engine.containers[CID]['State']['Running'])
        self.assertEqual(json.loads(self.engine.file.read_text())['services']['web']['image'], CURRENT)
        self.assertEqual(sum(method != 'GET' for method, unused, unused2 in self.engine.requests), 1)

    def test_real_unix_peer_is_bound_and_non_dockerd_peer_fails_closed(self):
        connection = self.engine.connect(2)
        try:
            with self.assertRaises(projects.ProjectError):
                projects._peer_identity(connection)
        finally:
            connection.close()

    def test_in_place_write_at_exchange_retains_concurrent_bytes_and_original_container(self):
        plan = self.plan()['plan']['plan_id']
        original = operations._rename_exchange
        concurrent = b'concurrent-owner-version-do-not-delete\n'
        def race(source_name, target_name, *, directory_fd):
            self.engine.file.write_bytes(concurrent)
            return original(source_name, target_name, directory_fd=directory_fd)
        with patch.object(operations, '_rename_exchange', side_effect=race):
            result = self.execute(plan, True, 'exchange-rewrite')
        self.assertEqual(result['task']['state'], 'result_unconfirmed', result)
        self.assertIn(concurrent, [path.read_bytes() for path in self.engine.directory.glob('.r1-image-*')])
        self.assertTrue(self.engine.containers[CID]['State']['Running'])
        self.assertFalse(any(method == 'DELETE' or '/stop?' in path
                             for method, path, body in self.engine.requests))


class ComposeGrammarTests(unittest.TestCase):
    def test_literal_single_service_yaml_only_and_no_alias_interpolation_or_merged_images(self):
        good = b'services:\n  web:\n    image: "example/app:old"\n    restart: unless-stopped\n'
        value = operations.DockerProjectOperations._yaml_image(good, 'web')
        value['services']['web']['image'] = TARGET
        self.assertIn(TARGET.encode(), operations.DockerProjectOperations._compose_bytes(value))
        for raw in (b'services: {web: {image: foo}}\n',
                    b'services:\n  web: &base\n    image: example/app:old\n',
                    b'services:\n  web:\n    image: example/app:old\n    image: example/app:new\n',
                    b'services:\n  web:\n    image: example/app:old\n  another:\n    image: x:y\n',
                    b'services:\n  web:\n    image: example/app:old\n    environment:\n      X: ${SECRET}\n',
                    b'services:\n  web:\n    build: .\n    image: example/app:old\n'):
            with self.assertRaises(operations.OperationError):
                operations.DockerProjectOperations._yaml_image(raw, 'web')


class PortableUpdateTests(ControlHTTPTests):
    """Run lifecycle HTTP on Windows with an explicit fixture file adapter.

    This adapter does not validate Linux filesystem safety. UnixUpdateTests
    above run the production file provider unchanged on Linux.
    """
    def setUp(self):
        super().setUp()
        engine = self.engine
        class FixtureFiles(operations.DockerProjectOperations):
            def _compose(self, registered):
                expected = registered['identity']['configuration']
                if engine.configuration(engine.compose) != expected:
                    raise operations.OperationError('project_binding_changed')
                raw = engine.file.read_bytes()
                try:
                    document = projects._json(raw)
                except projects.ProjectError:
                    document = self._yaml_image(raw, 'web')
                return raw, document, engine.file.stat()

            def _replace_compose(self, principal, registered, prepared, document):
                self._current(principal)
                if engine.configuration(engine.compose) != registered['identity']['configuration']:
                    raise operations.OperationError('project_binding_changed')
                raw = self._compose_bytes(document)
                engine.file.write_bytes(raw)
                return engine.configuration(engine.compose), 'sha256:' + hashlib.sha256(raw).hexdigest()
        self.ops = FixtureFiles(self.registry, authorizer=lambda _: self.active, backend=self.engine.writer)

    plan = UnixUpdateTests.plan
    execute = UnixUpdateTests.execute
    test_image_plan_execute_config_cas_task_and_volume_safe_retirement = UnixUpdateTests.test_image_plan_execute_config_cas_task_and_volume_safe_retirement
    test_yaml_image_change_preserves_other_bytes_and_works_without_yaml_dependency = UnixUpdateTests.test_yaml_image_change_preserves_other_bytes_and_works_without_yaml_dependency
    test_expired_conflicting_restarted_or_cross_credential_plan_never_mutates = UnixUpdateTests.test_expired_conflicting_restarted_or_cross_credential_plan_never_mutates
    test_config_change_after_plan_is_rejected_before_any_engine_write = UnixUpdateTests.test_config_change_after_plan_is_rejected_before_any_engine_write
    test_pull_failure_or_revoke_after_create_keeps_original_and_never_retries = UnixUpdateTests.test_pull_failure_or_revoke_after_create_keeps_original_and_never_retries

    def test_pinned_pull_and_chunked_json_stream_then_verified_replacement(self):
        self.engine.present_image = False
        self.engine.chunked = True
        planned = self.plan()
        self.assertTrue(planned['ok'], planned)
        result = self.execute(planned['plan']['plan_id'], True, 'pull')
        self.assertEqual(result['task']['state'], 'completed', result)
        pulls = [path for method, path, body in self.engine.requests if method == 'POST' and path.startswith('/images/create')]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(unquote(pulls[0]), '/images/create?fromImage=' + TARGET)

    def test_foreign_repository_privilege_or_unbound_host_mount_cannot_be_planned(self):
        service = self.engine.writer.snapshot()[0]['services'][0]['service_id']
        foreign = self.ops.plan_update(self.principal, self.project, service,
                                      'other/app@sha256:' + 'd' * 64)
        self.assertEqual(foreign['error']['code'], 'permission_denied')
        self.engine.containers[CID]['HostConfig']['Privileged'] = True
        self.assertEqual(self.plan()['error']['code'], 'write_backend_unverified')
        self.engine.containers[CID]['HostConfig']['Privileged'] = False
        self.engine.containers[CID]['HostConfig']['Binds'] = ['/var/run/docker.sock:/socket']
        self.assertEqual(self.plan()['error']['code'], 'write_backend_unverified')

    def test_even_existing_drive_bind_is_rejected_because_source_cannot_be_rebound_safely(self):
        self.engine.containers[CID]['Mounts'] = [{'Type': 'bind', 'Source': '/drives/disk/data',
                                                 'Destination': '/data', 'RW': True}]
        self.engine.containers[CID]['HostConfig']['Binds'] = ['/drives/disk/data:/data']
        self.assertEqual(self.plan()['error']['code'], 'write_backend_unverified')
        self.assertFalse(any(method != 'GET' for method, path, body in self.engine.requests))

    def test_named_volume_and_network_configuration_are_retained(self):
        self.engine.containers[CID]['Mounts'] = [{'Type': 'volume', 'Name': 'existing-data',
            'Source': '/var/lib/docker/volumes/existing-data/_data', 'Destination': '/data',
            'RW': True, 'Driver': 'local', 'Propagation': ''}]
        network = {'NetworkID': '9' * 64, 'Aliases': ['web'], 'IPAddress': '172.20.0.2'}
        self.engine.containers[CID]['NetworkSettings']['Networks'] = {'demo': network}
        plan = self.plan()
        self.assertTrue(plan['ok'], plan)
        result = self.execute(plan['plan']['plan_id'], True, 'volume-network')
        self.assertEqual(result['task']['state'], 'completed', result)
        body = next(body for method, path, body in self.engine.requests if path.startswith('/containers/create'))
        self.assertEqual(body['HostConfig']['Mounts'][0]['Source'], 'existing-data')
        self.assertEqual(body['NetworkingConfig']['EndpointsConfig']['demo']['Aliases'], ['web'])
        self.assertTrue(self.engine.containers[NEW]['Mounts'])

    def test_network_change_after_plan_is_rejected_before_engine_write(self):
        plan = self.plan()['plan']['plan_id']
        self.engine.containers[CID]['NetworkSettings']['Networks'] = {
            'attached-later': {'NetworkID': '9' * 64, 'Aliases': ['other']}}
        result = self.execute(plan, True, 'network-race')
        self.assertEqual(result['task']['state'], 'failed', result)
        self.assertEqual(result['task']['error']['code'], 'project_binding_changed')
        self.assertFalse(any(method != 'GET' for method, path, body in self.engine.requests))

    def test_submission_returns_task_while_daemon_blocked_and_same_key_never_resubmits(self):
        plan = self.plan()['plan']['plan_id']
        entered, release = threading.Event(), threading.Event()
        def block(method, path, body):
            if method == 'GET' and path == '/info':
                entered.set()
                release.wait(timeout=3)
        self.engine.before = block
        started = time.monotonic()
        submitted = self.ops.execute_update(self.principal, plan, True, 'background')
        try:
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(submitted['ok'], submitted)
            self.assertIn(submitted['task']['state'], ('pending', 'running'))
            self.assertTrue(entered.wait(timeout=2))
            queried = self.ops.get_task(self.principal, self.project, submitted['task']['task_id'])
            self.assertIn(queried['task']['state'], ('pending', 'running'))
            repeated = self.ops.execute_update(self.principal, plan, True, 'background')
            self.assertEqual(repeated['task']['task_id'], submitted['task']['task_id'])
            self.assertEqual(len(self.ops._threads), 1)
        finally:
            release.set()
        result = self.execute(plan, True, 'background')
        self.assertEqual(result['task']['state'], 'completed', result)

    def test_service_close_during_blocked_read_prevents_subsequent_writes(self):
        plan = self.plan()['plan']['plan_id']
        entered, release = threading.Event(), threading.Event()
        def block(method, path, body):
            if method == 'GET' and path == '/info':
                entered.set()
                release.wait(timeout=3)
        self.engine.before = block
        submitted = self.ops.execute_update(self.principal, plan, True, 'closed')
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.ops.close()
        finally:
            release.set()
        self.shutdown_operations()
        self.assertFalse(any(method != 'GET' for method, path, body in self.engine.requests))
        self.assertEqual(self.ops.get_task(self.principal, self.project,
                                          submitted['task']['task_id'])['error']['code'], 'invalid_key')

    def test_health_starting_then_healthy_is_waited_without_second_start(self):
        states = iter(('starting', 'healthy'))
        def health(method, path, body):
            if method == 'GET' and path == '/containers/' + NEW + '/json' and self.engine.containers[NEW]['State']['Running']:
                self.engine.containers[NEW]['State']['Health'] = {'Status': next(states, 'healthy')}
        self.engine.before = health
        result = self.execute(self.plan()['plan']['plan_id'], True, 'health')
        self.assertEqual(result['task']['state'], 'completed', result)
        self.assertEqual(sum(path == '/containers/' + NEW + '/start'
                             for method, path, body in self.engine.requests), 1)

    def _health_failure(self, status):
        def health(method, path, body):
            if method == 'GET' and path == '/containers/' + NEW + '/json' and self.engine.containers[NEW]['State']['Running']:
                self.engine.containers[NEW]['State']['Health'] = {'Status': status}
        self.engine.before = health
        with patch.object(operations, 'HEALTH_SECONDS', .02):
            result = self.execute(self.plan()['plan']['plan_id'], True, 'health-failed')
        self.assertEqual(result['task']['state'], 'result_unconfirmed', result)
        self.assertIn(CID, self.engine.containers)
        self.assertTrue(self.engine.containers[CID]['Name'].startswith('/r1-backup-'))
        self.assertFalse(any(method == 'DELETE' for method, path, body in self.engine.requests))

    def test_unhealthy_preserves_old_container_and_unknown_task(self):
        self._health_failure('unhealthy')

    def test_starting_timeout_preserves_old_container_and_unknown_task(self):
        self._health_failure('starting')

    def test_configured_healthcheck_without_status_cannot_retire_original(self):
        self.engine.containers[CID]['Config']['Healthcheck'] = {'Test': ['CMD', 'fixture-check']}
        result = self.execute(self.plan()['plan']['plan_id'], True, 'missing-health')
        self.assertEqual(result['task']['state'], 'result_unconfirmed', result)
        self.assertIn(CID, self.engine.containers)
        self.assertFalse(any(method == 'DELETE' for method, path, body in self.engine.requests))

    def _second_project(self):
        identity = 'c' * 64
        self.engine.container(identity, '/other-web-1', CURRENT, 'sha256:' + 'c' * 64, True)
        self.engine.containers[identity]['Config']['Labels'][projects._LABEL + 'project'] = 'other'
        candidate = next(row for row in self.registry.catalog()['candidates'] if row['label'] == 'other')
        project_id = self.registry.register({'candidate_id': candidate['id']})['project']['id']
        self.principal['docker_projects'].append({'id': project_id,
                                                 'operations': ['inspect', 'control', 'update_image']})
        return project_id, identity

    def test_concurrent_cross_project_control_key_reserves_exactly_one_task_and_write(self):
        other, unused = self._second_project()
        barrier = threading.Barrier(2)
        original = self.ops._project
        visited, guard, read_guard, results = set(), threading.Lock(), threading.Lock(), {}
        def synchronized(project_id, deadline=None):
            # The production reader intentionally rejects simultaneous reads.
            # Serialize the real HTTP snapshots, then align both key misses at
            # the mutation boundary to exercise the distinct project locks.
            with read_guard:
                result = original(project_id, deadline)
            with guard:
                first = threading.get_ident() not in visited
                visited.add(threading.get_ident())
            if first:
                barrier.wait(timeout=3)
            return result
        def call(project_id):
            results[project_id] = self.ops.control(self.principal, project_id, 'stop', True, 'shared-key')
        with patch.object(self.ops, '_project', side_effect=synchronized):
            workers = [threading.Thread(target=call, args=(project_id,)) for project_id in (self.project, other)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(result['ok'] for result in results.values()), 1, results)
        rejected = next(result for result in results.values() if not result['ok'])
        self.assertEqual(rejected['error']['code'], 'idempotency_conflict')
        self.assertEqual(len(self.ops._tasks), 1)
        self.assertEqual(sum(method == 'POST' for method, path, body in self.engine.requests), 1)
        self.assertEqual(len(self.ops._idempotency), 1)

    def test_concurrent_same_control_request_returns_existing_task_or_busy_without_second_write(self):
        entered, release, results = threading.Event(), threading.Event(), []
        def blocked(method, path, body):
            if method == 'POST':
                entered.set()
                release.wait(timeout=3)
        self.engine.before = blocked
        worker = threading.Thread(target=lambda: results.append(self.control(key='same-request')))
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            repeated = self.control(key='same-request')
        finally:
            release.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['task']['state'], 'completed', results)
        if repeated['ok']:
            self.assertEqual(repeated['task']['task_id'], results[0]['task']['task_id'])
        else:
            self.assertEqual(repeated['error']['code'], 'resource_busy')
        self.assertEqual(len(self.ops._tasks), 1)
        self.assertEqual(sum(method == 'POST' for method, path, body in self.engine.requests), 1)

    def test_update_reservation_wins_over_control_that_already_passed_initial_key_check(self):
        other, other_cid = self._second_project()
        plan = self.plan()['plan']['plan_id']
        control_ready, release_control = threading.Event(), threading.Event()
        update_ready, release_update, results = threading.Event(), threading.Event(), []
        original_project, original_run = self.ops._project, self.ops._run_update
        def pause_control(project_id, deadline=None):
            result = original_project(project_id, deadline)
            if project_id == other and not control_ready.is_set():
                control_ready.set()
                release_control.wait(timeout=3)
            return result
        def pause_update(*args):
            update_ready.set()
            release_update.wait(timeout=3)
            original_run(*args)
        with patch.object(self.ops, '_project', side_effect=pause_control), \
                patch.object(self.ops, '_run_update', side_effect=pause_update):
            worker = threading.Thread(target=lambda: results.append(
                self.ops.control(self.principal, other, 'stop', True, 'mixed-key')))
            worker.start()
            try:
                self.assertTrue(control_ready.wait(timeout=2))
                submitted = self.ops.execute_update(self.principal, plan, True, 'mixed-key')
                self.assertTrue(submitted['ok'], submitted)
                self.assertTrue(update_ready.wait(timeout=2))
                release_control.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results[0]['error']['code'], 'idempotency_conflict', results)
            finally:
                release_control.set()
                release_update.set()
                worker.join(timeout=5)
            result = self.execute(plan, True, 'mixed-key')
        self.assertEqual(result['task']['state'], 'completed', result)
        self.assertEqual(len(self.ops._tasks), 1)
        self.assertFalse(any(method == 'POST' and path.startswith('/containers/' + other_cid + '/')
                             for method, path, body in self.engine.requests))

    def test_control_reservation_rejects_update_without_consuming_plan_or_starting_worker(self):
        other, other_cid = self._second_project()
        plan = self.plan()['plan']['plan_id']
        entered, release, results = threading.Event(), threading.Event(), []
        def blocked(method, path, body):
            if method == 'POST' and path.startswith('/containers/' + other_cid + '/'):
                entered.set()
                release.wait(timeout=3)
        self.engine.before = blocked
        worker = threading.Thread(target=lambda: results.append(
            self.ops.control(self.principal, other, 'stop', True, 'mixed-key')))
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            rejected = self.ops.execute_update(self.principal, plan, True, 'mixed-key')
            self.assertEqual(rejected['error']['code'], 'idempotency_conflict')
            self.assertIsNone(self.ops._plans[plan]['task_id'])
            self.assertEqual(len(self.ops._threads), 0)
        finally:
            release.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['task']['state'], 'completed', results)
        self.assertEqual(len(self.ops._tasks), 1)
        self.assertEqual(sum(method == 'POST' for method, path, body in self.engine.requests), 1)


if __name__ == '__main__':
    unittest.main()

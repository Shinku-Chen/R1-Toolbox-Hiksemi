"""Isolated engine GET fixtures; no daemon, native login or NAS is contacted."""

import copy
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_docker_projects as projects
from api_contracts import contract_catalog


CID = 'a' * 64
ENGINE = 'fixture-engine-0000000001'
COMPOSE = '/drives/fixture/docker-compose.yml'
SECRET = 'fixture-secret-do-not-export'
TOOLS = ['list_docker_projects', 'get_docker_project_status', 'get_docker_project_summary']


def detail(name='demo', service='web'):
    return {'Id': CID, 'Config': {'Image': 'example/app:fixture',
            'Env': ['PASSWORD=' + SECRET], 'Cmd': ['secret-command', SECRET],
            'Labels': {'com.docker.compose.project': name, 'com.docker.compose.service': service,
                       'com.docker.compose.project.config_files': COMPOSE,
                       'com.docker.compose.project.working_dir': '/drives/fixture',
                       'com.docker.compose.config-hash': 'b' * 64,
                       'com.docker.compose.oneoff': 'False', 'sensitive': SECRET}},
            'HostConfig': {'Privileged': False, 'NetworkMode': 'bridge', 'CapAdd': [],
                           'Binds': ['/private/' + SECRET + ':/data'],
                           'RestartPolicy': {'Name': 'unless-stopped'}},
            'State': {'Running': True, 'Health': {'Status': 'healthy', 'Log': [SECRET]}},
            'Mounts': [{'Type': 'bind', 'Source': '/private/' + SECRET,
                        'Destination': '/secret/' + SECRET, 'RW': False}],
            'NetworkSettings': {'Ports': {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18080'}]}}}


class FixtureSocket:
    def __init__(self, fixture):
        self.fixture = fixture
        self.data = b''
        self.closed = False

    def settimeout(self, value):
        if value <= 0:
            raise AssertionError('deadline not enforced')

    def sendall(self, request):
        self.fixture.requests.append(request)
        method, path, _version = request.split(b'\r\n', 1)[0].decode('ascii').split(' ')
        if method != 'GET':
            raise AssertionError('only engine GET is allowed')
        body = self.fixture.response(path)
        if isinstance(body, bytes):
            self.data = body
            return
        body = json.dumps(body, separators=(',', ':')).encode('utf-8')
        if self.fixture.chunked:
            self.data = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n'
                         + format(len(body), 'x').encode('ascii') + b'\r\n' + body + b'\r\n0\r\n\r\n')
        else:
            self.data = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: '
                         + str(len(body)).encode('ascii') + b'\r\n\r\n' + body)

    def recv(self, size):
        size = min(size, self.fixture.chunk_size)
        value, self.data = self.data[:size], self.data[size:]
        return value

    def close(self):
        self.closed = True


class EngineFixture:
    def __init__(self):
        self.item = detail()
        self.requests, self.sockets = [], []
        self.config_stamp = [5, 9, 130, 42, 61]
        self.chunked, self.chunk_size = False, 8192
        self.peer = [100, 1234, 8, 15]
        self.hook = None
        self.reader = projects.LocalDockerReader(connect=self.connect, peer_check=lambda _sock: list(self.peer),
                                                 config_identity=self.configuration)

    def connect(self, _timeout):
        connection = FixtureSocket(self)
        self.sockets.append(connection)
        return connection

    def configuration(self, path):
        if path != COMPOSE:
            raise projects.ProjectError('project_unavailable')
        return {'path': path, 'identity': list(self.config_stamp)}

    def response(self, path):
        if self.hook is not None:
            result = self.hook(path)
            if result is not None:
                return result
        if path == '/info':
            return {'ID': ENGINE, 'RegistryConfig': {SECRET: SECRET}}
        if path == '/containers/json?all=1&limit=129':
            return [{'Id': CID, 'Labels': copy.deepcopy(self.item['Config']['Labels'])}]
        if path == '/containers/' + CID + '/json':
            return copy.deepcopy(self.item)
        raise AssertionError('unexpected engine endpoint')


class DockerReaderTests(unittest.TestCase):
    def setUp(self):
        self.engine = EngineFixture()

    def test_fixed_gets_and_engine_projection_never_export_secrets(self):
        result = self.engine.reader.snapshot()
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['running'])
        self.assertEqual(result[0]['health'], 'healthy')
        service = result[0]['services'][0]
        self.assertEqual(service['image_reference'], 'example/app:fixture')
        self.assertEqual(service['mounts'], [{'type': 'bind', 'access': 'readonly', 'label': None}])
        self.assertEqual(service['ports'], [{'published': 18080, 'target': 8080, 'protocol': 'tcp', 'exposure': 'loopback'}])
        self.assertIn('unresolved_configuration', service['risk_flags'])
        self.assertNotIn(SECRET, repr(result))
        self.assertTrue(all(sock.closed for sock in self.engine.sockets))
        paths = [request.split(b' ')[1] for request in self.engine.requests]
        self.assertEqual(paths, [b'/info', b'/containers/json?all=1&limit=129',
                                ('/containers/' + CID + '/json').encode('ascii'),
                                b'/containers/json?all=1&limit=129', b'/info'])
        self.assertTrue(all(request.startswith(b'GET ') for request in self.engine.requests))
        self.assertNotIn(b'project_list', b''.join(self.engine.requests))

    def test_chunked_and_split_responses_are_supported(self):
        self.engine.chunked, self.engine.chunk_size = True, 7
        self.assertEqual(len(self.engine.reader.snapshot()), 1)

    def test_missing_or_forged_compose_labels_never_become_candidates(self):
        mutations = [
            lambda labels: labels.pop('com.docker.compose.config-hash'),
            lambda labels: labels.update({'com.docker.compose.project': 'demo;id'}),
            lambda labels: labels.update({'com.docker.compose.project': '-demo'}),
            lambda labels: labels.update({'com.docker.compose.service': 'bad/name'}),
            lambda labels: labels.update({'com.docker.compose.oneoff': 'True'}),
            lambda labels: labels.update({'com.docker.compose.project.config_files': COMPOSE + ',/etc/other.yml'}),
            lambda labels: labels.update({'com.docker.compose.project.working_dir': '/etc'}),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.engine.item = detail()
                mutate(self.engine.item['Config']['Labels'])
                self.assertEqual(self.engine.reader.snapshot(), [])

    def test_config_identity_unverified_never_registers(self):
        self.engine.reader._config_identity = mock.Mock(side_effect=projects.ProjectError('project_unavailable'))
        self.assertEqual(self.engine.reader.snapshot(), [])

    def test_container_identity_mismatch_never_registers(self):
        self.engine.item['Id'] = 'c' * 64
        self.assertEqual(self.engine.reader.snapshot(), [])

    def test_unresolved_image_and_unknown_running_remain_null(self):
        self.engine.item['Config']['Image'] = '${SECRET_IMAGE}'
        self.engine.item['State']['Running'] = 'yes'
        result = self.engine.reader.snapshot()[0]
        self.assertIsNone(result['running'])
        self.assertIsNone(result['services'][0]['image_reference'])

    def test_actual_state_and_risk_flags(self):
        self.engine.item['State'] = {'Running': False}
        self.engine.item['HostConfig'].update(Privileged=True, NetworkMode='host', Devices=[{'PathOnHost': SECRET}], CapAdd=['SYS_ADMIN'])
        result = self.engine.reader.snapshot()[0]
        self.assertFalse(result['running'])
        self.assertEqual(result['health'], 'not_configured')
        self.assertEqual(set(result['services'][0]['risk_flags']), {'privileged', 'host_network', 'devices', 'capabilities', 'host_mounts', 'unresolved_configuration'})

    def test_protection_survives_project_rename_when_service_is_toolbox(self):
        self.engine.item = detail(name='renamed', service='r1-toolbox')
        self.assertTrue(self.engine.reader.snapshot()[0]['protected'])

    def test_peer_change_and_engine_change_abort_entire_snapshot(self):
        def change_peer(path):
            if path.startswith('/containers/') and path.endswith('/json'):
                self.engine.peer[1] += 1
        self.engine.hook = change_peer
        with self.assertRaises(projects.ProjectError) as raised:
            self.engine.reader.snapshot()
        self.assertEqual(raised.exception.code, 'project_binding_changed')
        self.engine = EngineFixture()
        calls = []
        def engine_change(path):
            if path == '/info':
                calls.append(path)
                return {'ID': ENGINE if len(calls) == 1 else ENGINE + '-new'}
        self.engine.hook = engine_change
        with self.assertRaises(projects.ProjectError) as raised:
            self.engine.reader.snapshot()
        self.assertEqual(raised.exception.code, 'project_binding_changed')

    def test_rebuild_or_configuration_change_during_snapshot_aborts(self):
        calls = []
        def rebuild(path):
            if path == '/containers/json?all=1&limit=129':
                calls.append(path)
                if len(calls) == 2:
                    return []
        self.engine.hook = rebuild
        with self.assertRaises(projects.ProjectError) as raised:
            self.engine.reader.snapshot()
        self.assertEqual(raised.exception.code, 'project_binding_changed')
        self.engine = EngineFixture()
        def changed_config(path):
            if path == '/info' and self.engine.requests and len(self.engine.requests) > 1:
                self.engine.config_stamp[3] += 1
        self.engine.hook = changed_config
        with self.assertRaises(projects.ProjectError) as raised:
            self.engine.reader.snapshot()
        self.assertEqual(raised.exception.code, 'project_binding_changed')

    def test_limits_and_redirect_and_duplicate_json_fail_closed(self):
        responses = [
            b'HTTP/1.1 302 Found\r\nLocation: http://evil/\r\nContent-Length: 0\r\n\r\n',
            b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 999999999\r\n\r\n',
            b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}',
            b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{"ID":1,"ID":2}',
            b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}',
        ]
        for response in responses:
            with self.subTest(response=response[:20]):
                self.engine.hook = lambda _path: response
                with self.assertRaises(projects.ProjectError):
                    self.engine.reader.snapshot()

    def test_aggregate_body_and_container_count_are_bounded(self):
        self.engine.hook = lambda path: [{'Id': format(index, '064x')} for index in range(129)] if path.startswith('/containers/json?') else None
        with self.assertRaises(projects.ProjectError) as raised:
            self.engine.reader.snapshot()
        self.assertEqual(raised.exception.code, 'limit_exceeded')
        self.engine = EngineFixture()
        with mock.patch.object(projects, 'MAX_TOTAL', 100):
            with self.assertRaises(projects.ProjectError) as raised:
                self.engine.reader.snapshot()
        self.assertEqual(raised.exception.code, 'limit_exceeded')

    def test_absolute_deadline_and_global_concurrency_are_bounded(self):
        value = [0]
        def clock():
            value[0] += .25
            return value[0]
        self.engine.reader._clock = clock
        self.engine.chunk_size = 1
        with self.assertRaises(projects.ProjectError) as raised:
            self.engine.reader.snapshot()
        self.assertEqual(raised.exception.code, 'timeout')
        projects._READ_LOCK.acquire()
        try:
            with self.assertRaises(projects.ProjectError) as raised:
                self.engine.reader.snapshot()
            self.assertEqual(raised.exception.code, 'resource_busy')
        finally:
            projects._READ_LOCK.release()

    def test_transport_error_message_does_not_leak_daemon_or_path(self):
        self.engine.reader._connect = mock.Mock(side_effect=OSError(SECRET))
        with self.assertRaises(projects.ProjectError) as raised:
            self.engine.reader.snapshot()
        self.assertNotIn(SECRET, str(raised.exception))

    def test_arbitrary_engine_paths_and_nonroot_peer_rejected(self):
        with self.assertRaises(projects.ProjectError):
            self.engine.reader._get('/containers/create', 10**12, {'peer': None, 'bytes': 0})
        self.assertEqual(self.engine.requests, [])
        if hasattr(socket, 'SO_PEERCRED'):
            connection = mock.Mock()
            connection.getsockopt.return_value = struct.pack('3i', 111, 1000, 1000)
            with self.assertRaises(projects.ProjectError):
                projects._peer_identity(connection)

    @unittest.skipUnless(hasattr(os, 'O_PATH'), 'Linux metadata-only open is required')
    def test_real_configuration_handles_reject_symlink_and_nonregular(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'compose.yml'
            path.write_text('sensitive: ' + SECRET, encoding='utf8')
            result = projects._configuration_identity(str(path))
            self.assertEqual(result['path'], str(path))
            self.assertEqual(len(result['identity']), 5)
            self.assertNotIn(SECRET, repr(result))
            link = Path(directory) / 'alias.yml'
            link.symlink_to(path)
            with self.assertRaises(projects.ProjectError):
                projects._configuration_identity(str(link))
            folder = Path(directory) / 'directory.yml'
            folder.mkdir()
            with self.assertRaises(projects.ProjectError):
                projects._configuration_identity(str(folder))


class DockerProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.engine = EngineFixture()
        self.registry = projects.DockerProjectRegistry(self.temporary.name, backend=self.engine.reader)
        self.schemas = {row['name']: row['outputSchema'] for row in contract_catalog()['tools']}

    def registered(self):
        candidate = self.registry.catalog()['candidates'][0]
        result = self.registry.register({'candidate_id': candidate['id'], 'label': '测试应用'})
        self.assertTrue(result['ok'], result)
        return result['project']['id']

    def principal(self, identity):
        return {'policy_version': 3, 'role': 'readonly', 'scopes': ['docker'], 'tool_grants': TOOLS,
                'docker_projects': [{'id': identity, 'operations': ['inspect']}]}

    def valid(self, tool, value):
        Draft202012Validator(self.schemas[tool], format_checker=FormatChecker()).validate(value)

    def test_owner_catalog_register_and_machine_outputs_match_contracts(self):
        self.assertFalse(self.registry.path.exists())
        catalog = self.registry.catalog()
        self.assertFalse(self.registry.path.exists())
        self.assertTrue(catalog['available'])
        identity = self.registered()
        principal = self.principal(identity)
        listing = self.registry.list_projects(principal)
        status = self.registry.get_status(principal, identity)
        summary = self.registry.get_summary(principal, identity)
        for tool, output in zip(TOOLS, [listing, status, summary]):
            self.valid(tool, output)
        self.assertIsNone(status['project']['native_status'])
        self.assertIsNone(status['project']['pop_status'])
        self.assertEqual(status['project']['source'], 'engine')
        self.assertTrue(status['project']['running'])
        self.assertNotIn(SECRET, repr([catalog, listing, status, summary, self.registry.snapshot()]))
        self.assertNotIn('/drives/', repr([catalog, listing, status, summary, self.registry.snapshot()]))
        stored = self.registry.path.read_text(encoding='utf8')
        self.assertNotIn(SECRET, stored)
        self.assertNotIn('Env', stored)
        self.assertNotIn('compose:', stored)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.registry.path.stat().st_mode), 0o600)

    def test_every_owner_mutation_rejects_raw_name_path_compose_or_update(self):
        identity = self.registered()
        bad = [{'name': 'demo'}, {'path': COMPOSE}, {'candidate_id': 'demo'},
               {'candidate_id': 'candidate_' + 'f' * 24, 'compose': SECRET},
               {'candidate_id': 'candidate_' + 'f' * 24, 'operations': ['update_image']},
               {'candidate_id': 'candidate_' + 'f' * 24, 'label': '\nsecret'}]
        for body in bad:
            self.assertEqual(self.registry.register(body)['error']['code'], 'invalid_arguments')
        self.assertEqual(self.registry.update({'project_id': identity, 'operations': ['update_image']})['error']['code'], 'invalid_arguments')
        self.assertEqual(self.registry.revoke({'project_id': identity, 'name': 'demo'})['error']['code'], 'invalid_arguments')

    def test_register_rechecks_live_binding_and_rejects_replayed_candidate(self):
        candidate = self.registry.catalog()['candidates'][0]['id']
        self.engine.config_stamp[3] += 1
        self.assertEqual(self.registry.register({'candidate_id': candidate})['error']['code'], 'project_unavailable')
        identity = self.registered()
        candidate = self.registry.catalog()['candidates'][0]['id']
        self.assertFalse(self.registry.register({'candidate_id': candidate})['ok'])
        self.assertEqual(len(self.registry.snapshot()['projects']), 1)
        self.assertTrue(self.registry.get_status(self.principal(identity), identity)['ok'])

    def test_ungranted_fake_and_revoked_ids_have_same_denial_without_engine_io(self):
        identity = self.registered()
        fake = 'project_' + 'f' * 24
        principal = self.principal(identity)
        requests_before = len(self.engine.requests)
        self.assertEqual(self.registry.get_status(principal, fake)['error']['code'], 'permission_denied')
        self.assertEqual(len(self.engine.requests), requests_before)
        self.registry.revoke({'project_id': identity})
        self.assertEqual(self.registry.get_status(principal, identity)['error']['code'], 'permission_denied')
        self.assertEqual(len(self.engine.requests), requests_before)
        self.assertEqual(self.registry.list_projects(principal), {'ok': True, 'projects': []})

    def test_old_keys_bad_resources_or_missing_domain_or_tool_do_not_gain_reads(self):
        identity = self.registered()
        valid = self.principal(identity)
        bad = [dict(valid, policy_version=2), dict(valid, policy_version=True), dict(valid, role='legacy'),
               dict(valid, scopes=[]), dict(valid, tool_grants=[]), dict(valid, docker_projects=[]),
               dict(valid, docker_projects=[{'project_id': identity, 'operations': ['inspect']}]),
               dict(valid, docker_projects=[{'id': identity, 'operations': ['update_image']}]),
               dict(valid, docker_projects=valid['docker_projects'] * 2)]
        before = len(self.engine.requests)
        for principal in bad:
            with self.subTest(principal=principal):
                result = self.registry.get_summary(principal, identity)
                self.assertEqual(result['error']['code'], 'permission_denied')
                self.valid('get_docker_project_summary', result)
        self.assertEqual(len(self.engine.requests), before)

    def test_admin_cannot_bypass_project_resource_and_empty_list_needs_no_engine(self):
        identity = self.registered()
        principal = dict(self.principal(identity), role='admin', docker_projects=[])
        before = len(self.engine.requests)
        self.assertEqual(self.registry.list_projects(principal), {'ok': True, 'projects': []})
        self.assertEqual(self.registry.get_status(principal, identity)['error']['code'], 'permission_denied')
        self.assertEqual(len(self.engine.requests), before)

    def test_binding_changes_never_silently_rebind_same_name(self):
        identity = self.registered()
        self.engine.config_stamp[1] += 1
        result = self.registry.get_summary(self.principal(identity), identity)
        self.assertEqual(result['error']['code'], 'project_binding_changed')
        listing = self.registry.list_projects(self.principal(identity))
        self.assertFalse(listing['projects'][0]['available'])
        new_identity = self.registered()
        self.assertNotEqual(new_identity, identity)

    def test_metadata_edit_is_offline_and_revocation_never_reenables_old_id(self):
        identity = self.registered()
        self.registry.backend = mock.Mock()
        self.registry.backend.snapshot.side_effect = projects.ProjectError('native_unavailable')
        result = self.registry.update({'project_id': identity, 'label': '新的名称'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['project']['revision'], 2)
        self.registry.backend.snapshot.assert_not_called()
        self.assertTrue(self.registry.revoke({'project_id': identity})['ok'])
        self.assertFalse(self.registry.update({'project_id': identity, 'label': '恢复'})['ok'])
        self.assertFalse(self.registry.revoke({'project_id': identity})['ok'])

    def test_owner_metadata_never_queries_live_sources_or_claims_availability(self):
        identity = self.registered()
        self.registry.backend = mock.Mock()
        self.registry.backend.snapshot.side_effect = AssertionError('metadata must not query')
        result = self.registry.metadata()
        self.assertEqual(result['projects'][0]['id'], identity)
        self.assertIsNone(result['projects'][0]['available'])
        self.assertEqual(result['projects'][0]['verification'], 'not_checked')
        self.registry.backend.snapshot.assert_not_called()

    def test_registry_concurrency_does_not_queue_unbounded_waiters(self):
        identity = self.registered()
        ready, release = threading.Event(), threading.Event()
        def hold():
            with self.registry._lock:
                ready.set()
                release.wait(2)
        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(ready.wait(1))
        try:
            calls = [lambda: self.registry.metadata(), lambda: self.registry.snapshot(),
                     lambda: self.registry.get_status(self.principal(identity), identity),
                     lambda: self.registry.get_summary(self.principal(identity), identity),
                     lambda: self.registry.list_projects(self.principal(identity))]
            for call in calls:
                self.assertEqual(call()['error']['code'], 'resource_busy')
        finally:
            release.set()
            worker.join(2)

    def test_unavailable_backend_never_becomes_empty_success_or_leaks_exception(self):
        identity = self.registered()
        self.registry.backend = mock.Mock()
        self.registry.backend.snapshot.side_effect = projects.ProjectError('native_unavailable')
        result = self.registry.get_status(self.principal(identity), identity)
        self.assertFalse(result['ok'])
        self.valid('get_docker_project_status', result)
        self.assertFalse(self.registry.catalog()['ok'])
        self.assertFalse(self.registry.list_projects(self.principal(identity))['projects'][0]['available'])

    def test_read_does_not_rewrite_registry_and_restarted_registry_preserves_binding(self):
        identity = self.registered()
        original = self.registry.path.read_bytes()
        self.registry = projects.DockerProjectRegistry(self.temporary.name, backend=self.engine.reader)
        self.assertTrue(self.registry.get_summary(self.principal(identity), identity)['ok'])
        self.assertEqual(original, self.registry.path.read_bytes())

    def test_malformed_or_replaced_state_fails_closed_and_is_not_repaired(self):
        identity = self.registered()
        self.registry.path.write_text('{"version":1,"version":1}', encoding='utf8')
        original = self.registry.path.read_bytes()
        self.assertFalse(self.registry.snapshot()['ok'])
        self.assertFalse(self.registry.get_status(self.principal(identity), identity)['ok'])
        self.assertEqual(original, self.registry.path.read_bytes())

    def test_atomic_persistence_failure_invalidates_instance(self):
        identity = self.registered()
        with mock.patch.object(projects.os, 'replace', side_effect=OSError(SECRET)):
            result = self.registry.update({'project_id': identity, 'label': '修改'})
        self.assertFalse(result['ok'])
        self.assertNotIn(SECRET, repr(result))
        self.assertFalse(self.registry.get_status(self.principal(identity), identity)['ok'])
        self.assertEqual(list(Path(self.temporary.name).glob('.docker-projects-*')), [])


if __name__ == '__main__':
    unittest.main()

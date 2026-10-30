"""Bounded project stats over real isolated HTTP and Linux AF_UNIX sockets.

Fixture seams prove protocol/policy handling, not NAS or production dockerd
identity. No actual Docker daemon, user file or business task is contacted.
"""

import copy
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_docker_projects as projects
import test_api_docker_operations as fixture


TOOL = 'get_docker_project_resource_usage'
PATH = '/containers/' + fixture.CID + '/stats?stream=false&one-shot=true'
SECRET = 'private-counter-fixture-must-not-escape'
LINUX = sys.platform.startswith('linux') and hasattr(os, 'O_PATH') and hasattr(socket, 'SO_PEERCRED')


def sample():
    return {'id': fixture.CID, 'name': '/demo-web-1',
        'read': '2026-10-30T00:00:02.123456789Z', 'preread': '2026-10-30T00:00:01.123456789Z',
        'cpu_stats': {'cpu_usage': {'total_usage': 3000, 'percpu_usage': [1000, 1000, 1000, 0]},
                      'system_cpu_usage': 10000, 'online_cpus': 4},
        'precpu_stats': {'cpu_usage': {'total_usage': 2000, 'percpu_usage': [500, 500, 1000, 0]},
                         'system_cpu_usage': 8000, 'online_cpus': 4},
        'memory_stats': {'usage': 1000, 'limit': 8000,
                         'stats': {'cache': 100, 'total_inactive_file': 60, 'private': SECRET}},
        'networks': {'eth0': {'rx_bytes': 50, 'tx_bytes': 100, 'rx_packets': 1, 'tx_packets': 2,
                            'rx_errors': 0, 'tx_errors': 0, 'rx_dropped': 0, 'tx_dropped': 0},
                     'eth1': {'rx_bytes': 1000, 'tx_bytes': 2000}},
        'blkio_stats': {'io_service_bytes_recursive': [
            {'major': 8, 'minor': 0, 'op': 'Read', 'value': 512},
            {'major': 8, 'minor': 0, 'op': 'Write', 'value': 1024},
            {'major': 8, 'minor': 0, 'op': 'Sync', 'value': 9999},
            {'major': 8, 'minor': 0, 'op': 'Total', 'value': 99999}]},
        'environment': SECRET, 'private_path': '/private/' + SECRET}


class StatsEngine(fixture.Engine):
    def __init__(self, directory, *, unix=False):
        self.frame = sample()
        self.frames = {}
        super().__init__(directory, unix=unix)

    def container(self, identity, name, image, image_id, running):
        super().container(identity, name, image, image_id, running)
        self.containers[identity]['State'].update(Pid=4242 if running else 0,
            StartedAt='2026-10-29T23:00:00.000000000Z')
        self.containers[identity]['RestartCount'] = 0

    def response(self, method, path, body):
        if method == 'GET' and path == PATH:
            return 200, copy.deepcopy(self.frame)
        match = re.fullmatch(r'/containers/([0-9a-f]{64})/stats\?stream=false&one-shot=true', path)
        if method == 'GET' and match and match[1] in self.frames:
            return 200, copy.deepcopy(self.frames[match[1]])
        return super().response(method, path, body)


class ResourceUsageTests(unittest.TestCase):
    unix = False

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = StatsEngine(self.directory.name, unix=self.unix)
        self.registry = projects.DockerProjectRegistry(self.directory.name, backend=self.engine.writer)
        candidate = self.registry.catalog()['candidates'][0]['id']
        result = self.registry.register({'candidate_id': candidate, 'label': 'Resource sample'})
        self.assertTrue(result['ok'], result)
        self.identity = result['project']['id']
        self.current = True
        self.principal = {'policy_version': 3, 'role': 'readonly', 'scopes': ['docker'],
            'tool_grants': [TOOL], 'docker_projects': [{'id': self.identity, 'operations': ['inspect']}]}
        self.engine.requests.clear()

    def tearDown(self):
        self.engine.close()
        self.directory.cleanup()

    def read(self, principal=None, identity=None):
        return self.registry.get_resource_usage(principal or self.principal,
            identity or self.identity, authorizer=lambda principal: self.current)

    def service(self):
        result = self.read()
        self.assertTrue(result['ok'], result)
        return result['services'][0]

    def test_one_real_fixed_stats_request_returns_whitelist_without_totals(self):
        result = self.read()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['source'], 'engine_stats')
        self.assertEqual(result['consistency'], 'per_service_observation')
        self.assertEqual(result['project_id'], self.identity)
        value = result['services'][0]
        self.assertRegex(value['service_id'], r'^service_[0-9a-f]{24}$')
        self.assertEqual(value['name'], 'web')
        self.assertEqual(value['cpu']['usage_percent'], 200.0)
        self.assertEqual(value['memory']['usage_bytes'], 1000)
        self.assertEqual(value['memory']['limit_bytes'], 8000)
        self.assertEqual(value['memory']['cache_bytes'], 100)
        self.assertEqual(value['memory']['cache_source'], 'cache')
        self.assertEqual(value['memory']['inactive_file_bytes'], 60)
        self.assertEqual(value['block_io']['devices'], [{'major': 8, 'minor': 0, 'read_bytes': 512, 'write_bytes': 1024}])
        self.assertEqual(len(value['network']['interfaces']), 2)
        self.assertEqual(value['network']['interfaces'][0]['rx_bytes'], 50)
        self.assertIsNone(value['network']['interfaces'][1]['rx_packets'])
        self.assertNotIn('total', value['network'])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(fixture.CID, json.dumps(result))
        self.assertEqual(sum(path == PATH for _, path, _ in self.engine.requests), 1)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.engine.requests))

    def test_cgroup_v2_online_count_and_memory_fields_without_estimation(self):
        self.engine.frame['cpu_stats']['cpu_usage'].pop('percpu_usage')
        self.engine.frame['precpu_stats']['cpu_usage'].pop('percpu_usage')
        self.engine.frame['memory_stats']['stats'] = {'file': 200, 'inactive_file': 120}
        value = self.service()
        self.assertEqual(value['cpu']['usage_percent'], 200)
        self.assertEqual(value['memory'], {'usage_bytes': 1000, 'limit_bytes': 8000,
            'cache_bytes': 200, 'cache_source': 'file', 'inactive_file_bytes': 120,
            'inactive_file_source': 'inactive_file'})
        self.assertNotIn('working_set_bytes', value['memory'])

    def test_older_percpu_count_is_used_only_when_online_is_absent(self):
        self.engine.frame['cpu_stats'].pop('online_cpus')
        self.engine.frame['precpu_stats'].pop('online_cpus')
        self.assertEqual(self.service()['cpu']['usage_percent'], 200)
        self.engine.frame['cpu_stats']['online_cpus'] = False
        self.assertIsNone(self.service()['cpu']['usage_percent'])

    def test_missing_invalid_or_cold_cpu_frames_stay_unknown(self):
        alterations = [lambda row: row.pop('precpu_stats'),
            lambda row: row['precpu_stats'].update(system_cpu_usage=0),
            lambda row: row['cpu_stats'].update(system_cpu_usage=8000),
            lambda row: row['cpu_stats']['cpu_usage'].update(total_usage=1),
            lambda row: row['cpu_stats']['cpu_usage'].update(total_usage=True),
            lambda row: row['cpu_stats']['cpu_usage'].update(total_usage=2 ** 53),
            lambda row: row['cpu_stats'].update(online_cpus=0),
            lambda row: row['precpu_stats'].update(online_cpus=False),
            lambda row: row['precpu_stats'].update(online_cpus=8),
            lambda row: row.pop('preread'),
            lambda row: row.update(preread='0001-01-01T00:00:00Z'),
            lambda row: row.update(preread=row['read']),
            lambda row: row.update(read='not-a-time')]
        for alter in alterations:
            with self.subTest(alteration=alter):
                self.engine.frame = sample()
                alter(self.engine.frame)
                self.assertIsNone(self.service()['cpu']['usage_percent'])

    def test_missing_and_invalid_counters_are_null_not_zero(self):
        self.engine.frame = {'id': fixture.CID}
        value = self.service()
        self.assertIsNone(value['cpu']['usage_percent'])
        self.assertTrue(all(value['memory'][key] is None for key in value['memory']))
        self.assertIsNone(value['network']['interfaces'])
        self.assertIsNone(value['block_io']['devices'])
        self.engine.frame = sample()
        self.engine.frame['memory_stats'].update(usage=True, limit='8000')
        self.engine.frame['memory_stats']['stats'].update(cache=-1)
        self.engine.frame['networks']['eth0'].update(rx_bytes=-1, tx_bytes=2 ** 53)
        value = self.service()
        self.assertIsNone(value['memory']['usage_bytes'])
        self.assertIsNone(value['memory']['cache_bytes'])
        self.assertIsNone(value['network']['interfaces'][0]['rx_bytes'])
        self.assertIsNone(value['network']['interfaces'][0]['tx_bytes'])

    def test_zero_is_preserved_only_when_explicit_counter_is_zero(self):
        self.engine.frame['cpu_stats']['cpu_usage']['total_usage'] = 2000
        self.engine.frame['memory_stats'].update(usage=0, limit=0)
        value = self.service()
        self.assertEqual(value['cpu']['usage_percent'], 0.0)
        self.assertEqual(value['memory']['usage_bytes'], 0)
        self.assertEqual(value['memory']['limit_bytes'], 0)

    def test_stopped_container_does_not_claim_observed_zero_cpu(self):
        self.engine.containers[fixture.CID]['State'].update(Running=False, Pid=0)
        value = self.service()
        self.assertFalse(value['running'])
        self.assertIsNone(value['cpu']['usage_percent'])
        self.engine.frame = {}
        value = self.service()
        self.assertIsNone(value['memory']['usage_bytes'])
        self.assertIsNone(value['network']['interfaces'])
        self.assertIsNone(value['read_at'])
        self.engine.containers[fixture.CID]['State'].update(Running=True, Pid=4242)
        self.assertFalse(self.read()['ok'])

    def test_cached_frame_before_current_container_start_is_rejected(self):
        self.engine.frame['read'] = '2026-10-28T00:00:00Z'
        result = self.read()
        self.assertEqual(result['error']['code'], 'project_binding_changed')
        self.engine.frame = sample()
        self.engine.frame['preread'] = '2026-10-28T00:00:00Z'
        self.assertIsNone(self.service()['cpu']['usage_percent'])

    def test_exact_tool_project_inspect_and_policy_required_without_native_io(self):
        changes = [('policy_version', 2), ('scopes', []), ('tool_grants', []),
                   ('docker_projects', []), ('role', 'legacy')]
        for key, change in changes:
            principal = copy.deepcopy(self.principal)
            principal[key] = change
            self.assertFalse(self.read(principal)['ok'])
        principal = copy.deepcopy(self.principal)
        principal['docker_projects'][0]['operations'] = ['control']
        self.assertFalse(self.read(principal)['ok'])
        self.assertFalse(self.read(identity='project_' + 'f' * 24)['ok'])
        self.assertFalse(self.engine.requests)

    def test_current_key_callback_is_mandatory_before_native_request(self):
        self.current = False
        self.assertEqual(self.read()['error']['code'], 'invalid_key')
        self.assertFalse(self.engine.requests)
        result = self.registry.get_resource_usage(self.principal, self.identity, authorizer=None)
        self.assertFalse(result['ok'])

    def test_old_get_and_writer_allowlists_still_reject_stats_by_default(self):
        with self.assertRaises(projects.ProjectError):
            self.engine.writer._get(PATH, time.monotonic() + 2, {'bytes': 0, 'peer': None})
        self.assertFalse(self.engine.writer._allowed('GET', PATH))
        for path in (PATH.replace('stream=false', 'stream=true'),
                     PATH.replace('&one-shot=true', ''), PATH + '&path=/private',
                     PATH.replace('/' + fixture.CID + '/', '/all/')):
            with self.assertRaises(projects.ProjectError):
                self.engine.writer._get(path, time.monotonic() + 2, {'bytes': 0, 'peer': None},
                    authorizer=lambda: True, stats=True)
        self.assertFalse(self.engine.requests)

    def test_forged_frame_cid_or_name_is_rejected_without_data(self):
        for field, value in [('id', 'f' * 64), ('id', None), ('name', '/another-container')]:
            self.engine.frame = sample()
            self.engine.frame[field] = value
            result = self.read()
            self.assertEqual(result['error']['code'], 'project_binding_changed')
            self.assertNotIn('services', result)

    def test_container_restart_pid_or_image_change_mid_read_is_rejected(self):
        for kind in ('started', 'pid', 'restart', 'image'):
            original = copy.deepcopy(self.engine.containers[fixture.CID])
            def before(method, path, body):
                if path != PATH:
                    return
                target = self.engine.containers[fixture.CID]
                if kind == 'started': target['State']['StartedAt'] = '2026-10-30T00:00:00Z'
                if kind == 'pid': target['State']['Pid'] += 1
                if kind == 'restart': target['RestartCount'] += 1
                if kind == 'image': target['Image'] = 'sha256:' + 'f' * 64
            self.engine.before = before
            result = self.read()
            self.assertEqual(result['error']['code'], 'project_binding_changed', result)
            self.engine.containers[fixture.CID] = original

    def test_engine_or_peer_change_mid_read_is_rejected(self):
        for kind in ('engine', 'peer'):
            def before(method, path, body):
                if path == PATH:
                    if kind == 'engine': self.engine.engine_id = 'replacement-engine-0001'
                    else: self.engine.peer[1] += 1
            self.engine.before = before
            self.assertEqual(self.read()['error']['code'], 'project_binding_changed')
            self.engine.engine_id = fixture.ENGINE
            self.engine.peer[1] = 100

    def test_owner_revocation_and_key_revocation_during_response_discard_results(self):
        def before(method, path, body):
            if path == PATH:
                self.current = False
        self.engine.before = before
        result = self.read()
        self.assertEqual(result['error']['code'], 'invalid_key')
        self.assertNotIn('services', result)
        self.current = True
        def revoke(method, path, body):
            if path == PATH:
                self.assertTrue(self.registry.revoke({'project_id': self.identity})['ok'])
        self.engine.before = revoke
        self.assertEqual(self.read()['error']['code'], 'invalid_key')

    def test_revocation_after_connect_sends_no_stats_request(self):
        connect = self.engine.writer._connect
        count = [0]
        def connection(timeout):
            result = connect(timeout)
            count[0] += 1
            if count[0] == 4:
                self.current = False
            return result
        self.engine.writer._connect = connection
        self.assertEqual(self.read()['error']['code'], 'invalid_key')
        self.assertFalse(any(path == PATH for _, path, _ in self.engine.requests))

    def test_configuration_change_and_extra_project_member_are_rejected(self):
        original = self.engine.file.read_bytes()
        def changed(method, path, body):
            if path == PATH:
                self.engine.file.write_bytes(original + b' ')
        self.engine.before = changed
        self.assertEqual(self.read()['error']['code'], 'project_binding_changed')

    def _add_worker(self):
        identity = 'd' * 64
        self.engine.container(identity, '/demo-worker-1', fixture.CURRENT,
                              'sha256:' + 'e' * 64, True)
        self.engine.containers[identity]['Config']['Labels'][projects._LABEL + 'service'] = 'worker'
        self.engine.frames[identity] = dict(sample(), id=identity, name='/demo-worker-1')
        return identity

    def test_extra_member_during_sample_is_not_silently_aggregated(self):
        def before(method, path, body):
            if path == PATH:
                self._add_worker()
        self.engine.before = before
        result = self.read()
        self.assertEqual(result['error']['code'], 'project_binding_changed')
        self.assertNotIn('services', result)

    def test_multiple_services_stay_separate_and_earlier_lifecycle_is_rechecked(self):
        worker = self._add_worker()
        candidate = self.registry.catalog()['candidates'][0]['id']
        result = self.registry.register({'candidate_id': candidate})
        self.assertTrue(result['ok'], result)
        self.identity = result['project']['id']
        self.principal['docker_projects'][0]['id'] = self.identity
        result = self.read()
        self.assertTrue(result['ok'], result)
        self.assertEqual([row['name'] for row in result['services']], ['web', 'worker'])
        self.assertNotIn('totals', result)
        def before(method, path, body):
            if path == '/containers/' + worker + '/stats?stream=false&one-shot=true':
                self.engine.containers[fixture.CID]['State']['Pid'] += 1
        self.engine.before = before
        self.assertEqual(self.read()['error']['code'], 'project_binding_changed')

    def test_network_and_block_lists_are_bounded_and_not_double_counted(self):
        self.engine.frame['blkio_stats']['io_service_bytes_recursive'] = [
            {'major': 8, 'minor': 0, 'op': 'Total', 'value': 9999}]
        self.assertIsNone(self.service()['block_io']['devices'])
        self.engine.frame = sample()
        self.engine.frame['networks'] = {'eth' + str(index): {} for index in range(65)}
        self.assertEqual(self.read()['error']['code'], 'limit_exceeded')
        self.engine.frame = sample()
        rows = self.engine.frame['blkio_stats']['io_service_bytes_recursive']
        rows.append(copy.deepcopy(rows[0]))
        self.assertEqual(self.read()['error']['code'], 'native_unavailable')
        self.engine.frame['blkio_stats']['io_service_bytes_recursive'] = [
            {'major': 8, 'minor': index, 'op': 'Read', 'value': index} for index in range(129)]
        self.assertEqual(self.read()['error']['code'], 'limit_exceeded')
        self.engine.frame['blkio_stats']['io_service_bytes_recursive'] = [{}] * 1025
        self.assertEqual(self.read()['error']['code'], 'limit_exceeded')

    def test_oversized_or_malformed_http_json_is_not_projected(self):
        for body in (b'{"private":"' + SECRET.encode() + b'"' + b' ' * projects.MAX_BODY + b'}',
                     b'{"id":"a","id":"b"}', b'{not-json}'):
            self.engine.before = lambda method, path, value: (200, body) if path == PATH else None
            result = self.read()
            self.assertFalse(result['ok'])
            self.assertNotIn(SECRET, repr(result))

    def test_deadline_and_output_size_fail_closed(self):
        tick = [0.0]
        self.engine.writer._clock = lambda: tick[0]
        def before(method, path, body):
            if path == PATH: tick[0] = 9.0
        self.engine.before = before
        self.assertEqual(self.read()['error']['code'], 'timeout')
        self.engine.writer._clock = time.monotonic
        self.engine.before = None
        with patch.object(projects, 'MAX_STATS_OUTPUT', 10):
            self.assertEqual(self.read()['error']['code'], 'limit_exceeded')

    def test_shared_read_lock_and_service_limit_release_cleanly(self):
        self.assertTrue(projects._READ_LOCK.acquire(blocking=False))
        try:
            self.assertEqual(self.read()['error']['code'], 'resource_busy')
        finally:
            projects._READ_LOCK.release()
        with self.registry._lock:
            identity = copy.deepcopy(self.registry._read()['projects'][0]['identity'])
        identity['services'] = [{'name': 'service' + str(index), 'container': format(index + 1, '064x'),
                                 'config_hash': 'f' * 64} for index in range(33)]
        with self.assertRaises(projects.ProjectError) as caught:
            self.engine.writer.resource_usage(identity, authorizer=lambda: True)
        self.assertEqual(caught.exception.code, 'limit_exceeded')
        self.assertFalse(self.engine.requests)
        self.assertTrue(self.read()['ok'])


@unittest.skipUnless(LINUX, 'requires Linux O_PATH and actual Unix peer credentials')
class LinuxResourceUsageTests(ResourceUsageTests):
    unix = True


if __name__ == '__main__':
    unittest.main()

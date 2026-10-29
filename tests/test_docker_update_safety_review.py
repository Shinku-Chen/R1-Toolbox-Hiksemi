"""Independent update regressions; only temporary files, never a Docker daemon."""

import copy
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_docker_operations as operations
import api_docker_projects as projects


class DockerUpdateIndependentReviewTests(unittest.TestCase):
    def detail(self):
        return {'Id': '1' * 64, 'Name': '/fixture', 'Image': 'sha256:' + '2' * 64,
                'Config': {}, 'HostConfig': {}, 'Mounts': [],
                'NetworkSettings': {'Networks': {'private-net': {
                    'NetworkID': '3' * 64, 'Aliases': ['fixture'], 'IPAMConfig': None,
                    'IPAddress': '172.20.0.2', 'EndpointID': '4' * 64}}}}

    def test_plan_identity_tracks_stable_network_configuration(self):
        original = self.detail()
        before = operations.DockerProjectOperations._fingerprint(original)
        changed = []
        added = copy.deepcopy(original)
        added['NetworkSettings']['Networks']['other-net'] = {'NetworkID': '5' * 64}
        changed.append(added)
        removed = copy.deepcopy(original)
        removed['NetworkSettings']['Networks'].clear()
        changed.append(removed)
        for field, value in (('NetworkID', '6' * 64), ('Aliases', ['different']),
                             ('IPAMConfig', {'IPv4Address': '172.20.0.19'})):
            row = copy.deepcopy(original)
            row['NetworkSettings']['Networks']['private-net'][field] = value
            changed.append(row)
        for row in changed:
            with self.subTest(networks=row['NetworkSettings']['Networks']):
                self.assertNotEqual(operations.DockerProjectOperations._fingerprint(row), before)

    def test_ephemeral_addresses_do_not_make_a_false_configuration_change(self):
        before = self.detail()
        after = copy.deepcopy(before)
        after['NetworkSettings']['Networks']['private-net'].update(IPAddress='172.20.0.9',
                                                                   EndpointID='9' * 64)
        self.assertEqual(operations.DockerProjectOperations._fingerprint(before),
                         operations.DockerProjectOperations._fingerprint(after))

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_PATH'), 'Linux renameat2/O_PATH required')
    def test_exchange_boundary_preserves_an_unreviewed_concurrent_compose_version(self):
        original_exchange = getattr(operations, '_rename_exchange', None)
        self.assertIsNotNone(original_exchange, 'atomic exchange helper is required')
        with tempfile.TemporaryDirectory(prefix='r1-compose-review-') as temporary:
            folder = Path(temporary)
            path = folder / 'docker-compose.yml'
            path.write_bytes(b'{"services":{"web":{"image":"example/app:old"}}}\n')
            path.chmod(0o600)
            expected = projects._configuration_identity(str(path))
            original = path.stat()
            prepared = {'uid': original.st_uid, 'gid': original.st_gid, 'mode': 0o600,
                        'raw': path.read_bytes()}
            registered = {'identity': {'configuration': expected}}
            document = {'services': {'web': {'image': 'example/app:new'}}}
            executor = operations.DockerProjectOperations.__new__(operations.DockerProjectOperations)
            executor.backend = SimpleNamespace(_config_identity=projects._configuration_identity)
            executor.authorizer = lambda unused: True
            executor._closed = threading.Event()
            concurrent = b'UNREVIEWED-EXTERNAL-COMPOSE-VERSION\n'
            injected = []

            def exchange(source_name, target_name, *, directory_fd):
                # A different client replaces the target immediately before the
                # atomic mutation. Its bytes must not be silently discarded.
                descriptor = os.open('.review-external', os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                     0o600, dir_fd=directory_fd)
                try:
                    self.assertEqual(os.write(descriptor, concurrent), len(concurrent))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.rename('.review-external', target_name, src_dir_fd=directory_fd,
                          dst_dir_fd=directory_fd)
                injected.append(True)
                return original_exchange(source_name, target_name, directory_fd=directory_fd)

            with mock.patch.object(operations, '_rename_exchange', new=exchange):
                with self.assertRaises((operations.OperationError, projects.ProjectError)) as caught:
                    executor._replace_compose({}, registered, prepared, document)
            self.assertEqual(injected, [True])
            self.assertIn(caught.exception.code, ('result_unconfirmed', 'project_binding_changed', 'plan_conflict'))
            preserved = [row.read_bytes() for row in folder.iterdir() if row.is_file()]
            self.assertIn(concurrent, preserved, 'the external version must survive the conflict')


if __name__ == '__main__':
    unittest.main()

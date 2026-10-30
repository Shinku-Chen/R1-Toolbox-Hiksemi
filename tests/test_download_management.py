"""Fixed-protocol downloader settings and task controls, with no NAS writes."""
import json
import unittest

from tests import test_download_operations as download_fixture
from tests.test_download_operations import IDENTITY, ROW, mutation, principal
from tests.test_vendor_download_diagnostics import frame
import download_operations as ops


class DownloadManagementTests(unittest.TestCase):
    client = download_fixture.DownloadOperationTests.client

    @staticmethod
    def limits(engine='qbittorrent', down=1024, up=2048):
        import hashlib
        identity = IDENTITY
        version = hashlib.sha256(json.dumps([engine, identity, [down, up]], sort_keys=True).encode()).hexdigest()[:32]
        return {'engine': engine, 'expected_version': 'download_limits_' + version,
                'confirmed': True, 'idempotency_key': 'global-one', 'download_limit_bps': 0}

    def test_get_qb_limits_uses_only_fixed_transfer_queries_and_preserves_real_zero(self):
        client, factory = self.client([frame(b'v5.1.0'), frame(0), frame(2048)])
        result = client.execute('get_download_limits', {'engine': 'qbittorrent'}, principal('readonly'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['limits']['download_limit_bps'], 0)
        self.assertEqual(result['limits']['upload_limit_bps'], 2048)
        self.assertRegex(result['limits']['version'], '^download_limits_[0-9a-f]{32}$')
        self.assertIn(b'GET /api/v2/transfer/downloadLimit ', factory.sockets[1].sent)
        self.assertIn(b'GET /api/v2/transfer/uploadLimit ', factory.sockets[2].sent)
        self.assertFalse(any(b'preferences' in sock.sent for sock in factory.sockets))

    def test_global_setting_requires_admin_and_exact_prior_limits_version(self):
        client, factory = self.client([frame(b'v5.1.0'), frame(1024), frame(2048), frame(b'')])
        arguments = self.limits()
        self.assertEqual(client.execute('set_download_limits', arguments, principal())['error']['code'], 'permission_denied')
        self.assertEqual(factory.sockets, [])
        result = client.execute('set_download_limits', arguments, principal('admin'))
        self.assertTrue(result['ok'])
        self.assertFalse(result['effect_confirmed'])
        self.assertIn(b'POST /api/v2/transfer/setDownloadLimit ', factory.sockets[-1].sent)
        self.assertIn(b'limit=0', factory.sockets[-1].sent)
        self.assertEqual(client.execute('set_download_limits', arguments, principal('admin')), result)
        self.assertEqual(len(factory.sockets), 4)

    def test_changed_or_unknown_limits_prevent_writes(self):
        for current in (None, 5):
            with self.subTest(current=current):
                client, factory = self.client([frame(b'v5.1.0'), frame(current), frame(2048)])
                result = client.execute('set_download_limits', self.limits(), principal('admin'))
                self.assertFalse(result['ok'])
                self.assertEqual(result['error']['code'], 'settings_changed')
                self.assertFalse(any(sock.sent.startswith(b'POST ') for sock in factory.sockets))

    def test_aria_global_options_project_only_limits_and_write_only_whitelisted_fields(self):
        rpc = lambda value: frame({'jsonrpc': '2.0', 'id': 'r1-task-operation', 'result': value})
        native = {'max-overall-download-limit': '1024', 'max-overall-upload-limit': '2048',
                  'dir': '/private/secret', 'rpc-secret': 'fixture-secret'}
        client, factory = self.client([rpc(native), rpc('OK')])
        result = client.execute('set_download_limits', self.limits('aria2'), principal('admin'))
        self.assertTrue(result['ok'])
        packet = json.loads(factory.sockets[-1].sent.split(b'\r\n\r\n', 1)[1])
        self.assertEqual(packet['method'], 'aria2.changeGlobalOption')
        self.assertEqual(packet['params'], [{'max-overall-download-limit': '0'}])
        client, unused = self.client([rpc(native)])
        result = client.execute('get_download_limits', {'engine': 'aria2'}, principal('readonly'))
        serialized = json.dumps(result)
        self.assertNotIn('/private', serialized)
        self.assertNotIn('fixture-secret', serialized)
        self.assertEqual(result['limits']['download_limit_bps'], 1024)

    def test_four_qb_task_controls_have_exact_endpoints_and_no_filesystem_options(self):
        cases = [('recheck_download_task', {}, b'/torrents/recheck ', b'hashes='),
                 ('rename_download_task', {'name': '新的显示名'}, b'/torrents/rename ', b'hash='),
                 ('set_download_task_force_start', {'enabled': True}, b'/torrents/setForceStart ', b'value=true'),
                 ('set_download_task_queue_position', {'position': 'top'}, b'/torrents/topPrio ', b'hashes=')]
        for tool, extra, endpoint, required in cases:
            with self.subTest(tool=tool):
                client, factory = self.client([frame(b'v5.1.0'), frame([ROW]), frame(b'')])
                result = client.execute(tool, {**mutation(), **extra}, principal())
                self.assertTrue(result['ok'])
                self.assertFalse(result['effect_confirmed'])
                packet = factory.sockets[-1].sent
                self.assertIn(endpoint, packet)
                self.assertIn(required, packet)
                self.assertNotIn(b'setLocation', packet)
                self.assertNotIn(b'renameFile', packet)

    def test_new_argument_shapes_reject_engine_selectors_and_arbitrary_preferences(self):
        client, factory = self.client([])
        invalid = [('set_download_limits', {**self.limits(), 'dir': '/host'}),
                   ('set_download_limits', {**self.limits(), 'download_limit_bps': True}),
                   ('recheck_download_task', mutation(engine='aria2')),
                   ('rename_download_task', {**mutation(), 'name': '../elsewhere'}),
                   ('set_download_task_force_start', {**mutation(), 'enabled': 'true'}),
                   ('set_download_task_queue_position', {**mutation(), 'position': 'all'})]
        for tool, arguments in invalid:
            with self.subTest(tool=tool):
                result = client.execute(tool, arguments, principal('admin'))
                self.assertEqual(result['error']['code'], 'invalid_arguments')
        self.assertEqual(factory.sockets, [])


if __name__ == '__main__':
    unittest.main()

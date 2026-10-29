"""Private name diagnostics have fixed inputs and never request SMART refresh."""
import copy
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import web_config


class StorageDebugTests(unittest.TestCase):
    def setUp(self):
        self.server = web_config.WebConfigServer.__new__(web_config.WebConfigServer)
        self.server.dev_commands = True
        self.server._dev_authorized = lambda ip: ip == '192.0.2.5'
        self.server.auth = types.SimpleNamespace(required=lambda: False)
        self.server.password_session = lambda headers: None
        self.server.storage_diagnostic_lock = threading.Lock()
        self.storage = {'names': {'available': True, 'reason': None}, 'items': [
            {'id': 'sda', 'user_name': '测试盘名', 'name_source': 'hiksemi_storage_socket'}]}
        self.collector = mock.Mock()
        self.collector.storage_name_diagnostics.return_value = copy.deepcopy(self.storage)
        self.collector.snapshot.return_value = {'device': {'identity': {
            'name': '测试设备昵称', 'hostname': 'HikSemi-fixture',
            'name_source': 'histor.dev_info.dev_name', 'name_reason': None,
            'credentials': 'must-not-leave-cache'}}}
        self.server.insights = self.collector

    def request(self, body=None, ip='192.0.2.5'):
        return self.server.panel_post('/api/dev/storage',
                                      {'action': 'snapshot'} if body is None else body, {}, ip)

    def test_requires_both_developer_ip_and_command_flag(self):
        self.assertEqual(self.request(ip='192.0.2.6')[0], 403)
        self.server.dev_commands = False
        self.assertEqual(self.request()[0], 403)
        self.collector.storage_name_diagnostics.assert_not_called()

    def test_password_login_does_not_replace_developer_access(self):
        self.server.auth.required = lambda: True
        self.assertEqual(self.request()[0], 401)
        self.server.password_session = lambda headers: {'id': 'fixture'}
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request(ip='192.0.2.6')[0], 403)

    def test_accepts_only_fixed_snapshot_without_command_path_or_serial(self):
        for body in ({}, {'action': 'refresh'}, {'action': 'get_space_list'},
                     {'action': 'snapshot', 'serial': 'override'},
                     {'action': 'snapshot', 'path': '/tmp/private'},
                     {'action': 'snapshot', 'cmd': 'id'}, [], 'snapshot'):
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.collector.storage_name_diagnostics.assert_not_called()

    def test_uses_cache_only_diagnostics_and_projects_device_fields(self):
        status, payload, cookie = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertIsNone(cookie)
        self.assertEqual(payload['device']['name'], '测试设备昵称')
        self.assertEqual(payload['device']['hostname'], 'HikSemi-fixture')
        self.assertNotIn('credentials', payload['device'])
        self.assertEqual(payload['storage'], self.storage)
        self.collector.storage_name_diagnostics.assert_called_once_with()
        self.collector._smart.assert_not_called()
        self.collector._disks.assert_not_called()

    def test_concurrent_ipc_is_bounded(self):
        with self.server.storage_diagnostic_lock:
            self.assertEqual(self.request()[0], 409)
        self.collector.storage_name_diagnostics.assert_not_called()
        self.assertEqual(self.request()[0], 200)

    def test_errors_do_not_leak_paths_or_hold_the_lock(self):
        self.collector.storage_name_diagnostics.side_effect = OSError('/private/config.txt')
        status, value, _ = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn('/private', str(value))
        self.assertFalse(self.server.storage_diagnostic_lock.locked())

    def test_new_route_is_private_post_only(self):
        self.assertIn('/api/dev/storage', web_config.PANEL_POST_ROUTES)
        self.assertNotIn('/api/dev/storage', web_config.PUBLIC_ROUTES)
        self.assertNotIn('/api/dev/storage', web_config.PANEL_GET_ROUTES)


if __name__ == '__main__':
    unittest.main()

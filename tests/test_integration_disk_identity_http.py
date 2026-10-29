"""Owner Web disk identities through real HTTP, with entirely fake storage.

The shared fixture is imported after panel reload tests in normal discovery.
No disk, SMART command, vendor session or NAS connection is used here.
"""

import copy
import json
import unittest

import test_integration_http as fixture


VERIFIED = 'FIXTURE-VERIFIED-SERIAL'
UNVERIFIED = 'FIXTURE-UNVERIFIED-SERIAL'


class DiskIdentityHTTPTests(unittest.TestCase):
    _patch = fixture.IntegrationHTTPTests._patch
    request = fixture.IntegrationHTTPTests.request
    browser = fixture.IntegrationHTTPTests.browser
    rest_action = fixture.IntegrationHTTPTests.rest_action
    key = fixture.IntegrationHTTPTests.key

    def setUp(self):
        fixture.IntegrationHTTPTests.setUp(self)
        snapshot = copy.deepcopy(self.insights.snapshot.return_value)
        snapshot['disks'] = {'available': True, 'items': [
            {'id': 'sda', 'device': '/dev/sda', 'model': 'Fixture drive',
             'type': 'HDD', 'size_bytes': 1024, 'temp_c': 32,
             'health': {'passed': True}, 'standby': False}]}
        snapshot['device']['storage'] = {'devices': [
            {'name': 'sda', 'serial': UNVERIFIED, 'rotational': 1}]}
        self.insights.snapshot.return_value = snapshot
        self.insights.storage_identity_cached.return_value = {
            'available': True, 'items': [
                {'id': 'sda', 'serial': VERIFIED, 'serial_status': 'ok',
                 'serial_sources': ['smart_identity_cache', 'hiksemi_storage_socket'],
                 'matched': True}]}
        self.insights.smart_cached.return_value = {
            'smart_data': {'serial_number': UNVERIFIED}}

    def assert_serial_hidden(self, response):
        text = json.dumps(response[2])
        self.assertNotIn(VERIFIED, text)
        self.assertNotIn(UNVERIFIED, text)

    def test_owner_overview_and_smart_show_verified_cached_identity(self):
        for path in ('/api/overview', '/api/smart?dev=sda'):
            response = self.browser('GET', path)
            self.assertEqual(response[0], 200, response[2])
            value = response[2]
            serial = value['disks_detail'][0]['serial'] if path == '/api/overview' else value['serial']
            self.assertEqual(serial, VERIFIED)
            self.assertNotIn(UNVERIFIED, json.dumps(value))

    def test_password_protected_anonymous_requests_do_not_read_identity_cache(self):
        for path in ('/api/overview', '/api/smart?dev=sda'):
            response = self.browser('GET', path, cookie=False)
            self.assertEqual(response[0], 401)
            self.assert_serial_hidden(response)
        self.insights.storage_identity_cached.assert_not_called()

    def test_bearer_does_not_upgrade_web_identity_even_with_owner_cookie(self):
        key = self.key()
        for path in ('/api/overview', '/api/smart?dev=sda'):
            response = self.browser('GET', path,
                                    headers={'Authorization': 'Bearer ' + key['token']})
            self.assertEqual(response[0], 200, response[2])
            self.assert_serial_hidden(response)
        self.insights.storage_identity_cached.assert_not_called()

    def test_machine_overview_and_storage_keep_serials_redacted(self):
        key = self.key()
        for section in ('overview', 'storage'):
            response = self.rest_action(key, 'get_nas_status', {'section': section})
            self.assertEqual(response[0], 200, response[2])
            self.assertFalse(response[2]['isError'], response[2])
            self.assert_serial_hidden(response)
        self.insights.storage_identity_cached.assert_not_called()

    def test_public_status_never_loads_owner_identity_cache(self):
        response = self.request('GET', '/api/v1/status', private=False)
        self.assertEqual(response[0], 200)
        self.assert_serial_hidden(response)
        self.insights.storage_identity_cached.assert_not_called()

    def test_first_use_open_web_has_owner_visibility(self):
        self.server.auth.set_enabled(False)
        response = self.browser('GET', '/api/overview', cookie=False)
        self.assertEqual(response[0], 200, response[2])
        self.assertEqual(response[2]['disks_detail'][0]['serial'], VERIFIED)

    def test_developer_allowlist_alone_does_not_disclose_serials(self):
        self.server.auth.set_enabled(False)
        self.server.auth_mode = 'pair'
        self.assertTrue(self.server._dev_authorized('127.0.0.1'))
        for path in ('/api/overview', '/api/smart?dev=sda'):
            response = self.browser('GET', path, cookie=False)
            self.assertEqual(response[0], 200, response[2])
            self.assert_serial_hidden(response)
        self.insights.storage_identity_cached.assert_not_called()

    def test_unavailable_verified_cache_never_falls_back_to_inventory_or_smart(self):
        self.insights.storage_identity_cached.return_value = {
            'available': False, 'items': []}
        for path in ('/api/overview', '/api/smart?dev=sda'):
            response = self.browser('GET', path)
            self.assertEqual(response[0], 200, response[2])
            self.assert_serial_hidden(response)


if __name__ == '__main__':
    unittest.main()

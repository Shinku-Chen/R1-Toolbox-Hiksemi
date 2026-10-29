"""Persistent optional-service settings, using only temporary fixture state."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import service_settings as settings


ENV_DEFAULTS = {
    'R1_SMALL_SCREEN_ENABLED': '1', 'R1_DEV_NO_PAIRING': '0',
    'R1_DEV_ALLOW_IPS': '', 'R1_DEV_MINUTES': '60', 'R1_DEV_ALLOW_COMMANDS': '0',
}


class ServiceSettingsTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, ENV_DEFAULTS)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(prefix='r1-service-settings-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.now = 1800000000
        self.store = settings.ServiceSettingsStore(self.directory, clock=lambda: self.now)

    def developer(self, **updates):
        return {'enabled': True, 'allowed_ips': ['192.168.31.193/24'],
                'minutes': 5, 'allow_commands': False, **updates}

    def save_developer(self, **updates):
        return self.store.configure_developer(self.developer(**updates))

    def write_state(self, value):
        self.store.path.write_text(json.dumps(value), encoding='utf-8')
        os.chmod(self.store.path, 0o600)

    def assert_closed(self):
        state = self.store.snapshot()
        self.assertFalse(state['storage_ok'])
        self.assertFalse(state['screen_enabled'])
        self.assertFalse(state['developer']['enabled'])
        self.assertFalse(state['developer']['active'])
        self.assertTrue(state['error'])
        self.assertFalse(self.store.authorized('127.0.0.1'))
        return state

    def test_default_screen_on_developer_off_and_pure_reads_create_no_files(self):
        state = self.store.snapshot()
        self.assertEqual(set(state), {'storage_ok', 'screen_enabled', 'developer', 'error'})
        self.assertTrue(state['storage_ok'])
        self.assertTrue(state['screen_enabled'])
        self.assertIsNone(state['error'])
        self.assertFalse(state['developer']['enabled'])
        self.assertFalse(state['developer']['active'])
        self.assertEqual(state['developer']['allowed_ips'], [])
        self.assertEqual(state['developer']['minutes'], 60)
        self.assertIsNone(state['developer']['expires_at'])
        self.assertFalse(state['developer']['allow_commands'])
        self.assertFalse(self.store.authorized('127.0.0.1'))
        self.assertFalse(self.store.path.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_snapshots_are_detached_from_bootstrap_and_persisted_settings(self):
        first = self.store.snapshot()
        first['developer']['allowed_ips'].append('127.0.0.1')
        first['developer']['enabled'] = True
        self.assertFalse(self.store.snapshot()['developer']['enabled'])
        self.assertEqual(self.store.snapshot()['developer']['allowed_ips'], [])
        self.save_developer()
        state = self.store.snapshot()
        state['developer']['allowed_ips'].clear()
        state['developer']['enabled'] = False
        self.assertEqual(self.store.snapshot()['developer']['allowed_ips'], ['192.168.31.0/24'])
        self.assertTrue(self.store.authorized('192.168.31.1'))

    def test_missing_file_can_bootstrap_legacy_environment_with_normalized_sources(self):
        with mock.patch.dict(os.environ, {
                'R1_SMALL_SCREEN_ENABLED': '0', 'R1_DEV_NO_PAIRING': '1',
                'R1_DEV_ALLOW_IPS': '192.168.31.193/24, ::ffff:127.0.0.1 , FD00::1/64',
                'R1_DEV_MINUTES': '7', 'R1_DEV_ALLOW_COMMANDS': '1'}):
            store = settings.ServiceSettingsStore(self.directory, clock=lambda: self.now)
        state = store.snapshot()
        self.assertFalse(state['screen_enabled'])
        self.assertTrue(state['developer']['enabled'])
        self.assertTrue(state['developer']['active'])
        self.assertTrue(state['developer']['allow_commands'])
        self.assertEqual(state['developer']['allowed_ips'], ['192.168.31.0/24', '127.0.0.1', 'fd00::/64'])
        self.assertEqual(state['developer']['expires_at'], self.now + 7 * 60)
        self.assertTrue(store.authorized('192.168.31.99'))
        self.assertTrue(store.authorized('::ffff:127.0.0.1'))
        self.assertTrue(store.authorized('fd00::abcd'))
        self.assertFalse(store.authorized('192.168.30.1'))
        self.assertFalse(store.path.exists())

    def test_invalid_environment_never_enables_developer_access_or_echoes_its_values(self):
        cases = ({'R1_DEV_ALLOW_IPS': ''}, {'R1_DEV_ALLOW_IPS': '0.0.0.0/0'},
                 {'R1_DEV_ALLOW_IPS': '::/0'}, {'R1_DEV_ALLOW_IPS': 'private-marker.invalid'},
                 {'R1_DEV_ALLOW_IPS': '127.0.0.1,::ffff:127.0.0.1'},
                 {'R1_DEV_ALLOW_IPS': '192.168.1.8/24,192.168.1.9/24'},
                 {'R1_DEV_MINUTES': '0'}, {'R1_DEV_MINUTES': '1441'},
                 {'R1_DEV_MINUTES': '1.5'}, {'R1_DEV_MINUTES': 'private-marker'},
                 {'R1_DEV_ALLOW_COMMANDS': '2'})
        for change in cases:
            with self.subTest(change=change):
                with mock.patch.dict(os.environ, {
                        'R1_DEV_NO_PAIRING': '1', 'R1_DEV_ALLOW_IPS': '127.0.0.1', **change}):
                    store = settings.ServiceSettingsStore(self.directory, clock=lambda: self.now)
                state = store.snapshot()
                self.assertTrue(state['storage_ok'])
                self.assertFalse(state['developer']['enabled'])
                self.assertFalse(state['developer']['active'])
                self.assertTrue(state['developer']['reason'])
                self.assertNotIn('private-marker', json.dumps(state))
                self.assertFalse(store.authorized('127.0.0.1'))
                self.assertFalse(store.path.exists())

    def test_explicit_saved_settings_override_valid_or_invalid_environment_after_restart(self):
        self.store.configure_screen({'enabled': False})
        self.store.configure_developer(self.developer(enabled=False, allowed_ips=[]))
        before = self.store.path.read_bytes()
        for ips in ('127.0.0.1', 'private-marker.invalid'):
            with self.subTest(ips=ips):
                with mock.patch.dict(os.environ, {
                        'R1_SMALL_SCREEN_ENABLED': '1', 'R1_DEV_NO_PAIRING': '1',
                        'R1_DEV_ALLOW_IPS': ips, 'R1_DEV_MINUTES': '1440',
                        'R1_DEV_ALLOW_COMMANDS': '1'}):
                    reopened = settings.ServiceSettingsStore(self.directory, clock=lambda: self.now)
                state = reopened.snapshot()
                self.assertFalse(state['screen_enabled'])
                self.assertFalse(state['developer']['enabled'])
                self.assertFalse(state['developer']['allow_commands'])
                self.assertFalse(reopened.authorized('127.0.0.1'))
                self.assertNotIn('环境', state['developer']['reason'])
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_saved_window_expires_at_the_exact_boundary_and_restarts_never_extend_it(self):
        saved = self.save_developer(minutes=2)
        deadline = self.now + 120
        self.assertEqual(saved['developer']['expires_at'], deadline)
        before = self.store.path.read_bytes()
        self.now = deadline - 1
        reopened = settings.ServiceSettingsStore(self.directory, clock=lambda: self.now)
        self.assertTrue(reopened.authorized('192.168.31.1'))
        self.now = deadline
        self.assertFalse(reopened.authorized('192.168.31.1'))
        self.assertFalse(reopened.snapshot()['developer']['active'])
        self.assertIn('过期', reopened.snapshot()['developer']['reason'])
        self.now += 3600
        with mock.patch.dict(os.environ, {'R1_DEV_NO_PAIRING': '1',
                'R1_DEV_ALLOW_IPS': '192.168.31.0/24', 'R1_DEV_MINUTES': '1440'}):
            restarted = settings.ServiceSettingsStore(self.directory, clock=lambda: self.now)
        state = restarted.snapshot()
        self.assertTrue(state['developer']['enabled'])
        self.assertFalse(state['developer']['active'])
        self.assertEqual(state['developer']['expires_at'], deadline)
        self.assertFalse(restarted.authorized('192.168.31.1'))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_screen_changes_preserve_the_saved_developer_deadline(self):
        deadline = self.save_developer(minutes=2)['developer']['expires_at']
        self.now += 60
        changed = self.store.configure_screen({'enabled': False})
        self.assertFalse(changed['screen_enabled'])
        self.assertEqual(changed['developer']['expires_at'], deadline)
        self.assertTrue(changed['developer']['active'])
        self.now = deadline
        self.assertFalse(self.store.snapshot()['developer']['active'])

    def test_disabling_developer_clears_expiry_and_never_disables_the_web_or_screen(self):
        self.save_developer(allow_commands=True)
        state = self.save_developer(enabled=False, allowed_ips=[], allow_commands=False)
        self.assertTrue(state['screen_enabled'])
        self.assertFalse(state['developer']['enabled'])
        self.assertFalse(state['developer']['active'])
        self.assertIsNone(state['developer']['expires_at'])
        self.assertFalse(self.store.authorized('192.168.31.1'))
        reopened = settings.ServiceSettingsStore(self.directory, clock=lambda: self.now)
        self.assertFalse(reopened.snapshot()['developer']['enabled'])

    def test_actual_peer_requires_a_canonical_matching_lan_rule(self):
        self.save_developer(allowed_ips=['127.1.2.3/8', 'fd00:1234::1/64'])
        for peer in ('127.0.0.1', '::ffff:127.0.0.1', 'fd00:1234::99'):
            with self.subTest(peer=peer):
                self.assertTrue(self.store.authorized(peer))
        for peer in ('192.168.31.1', 'fd00:1235::1', '8.8.8.8',
                     '127.0.0.0/8', 'localhost', '127.0.0.1, 8.8.8.8', None):
            with self.subTest(peer=peer):
                self.assertFalse(self.store.authorized(peer))
        self.assertEqual(self.store.snapshot()['developer']['allowed_ips'], ['127.0.0.0/8', 'fd00:1234::/64'])

    def test_invalid_screen_fields_do_not_write_or_change_saved_state(self):
        self.store.configure_screen({'enabled': True})
        before = self.store.path.read_bytes()
        for body in (None, [], {}, {'enabled': 1}, {'enabled': 'true'},
                     {'enabled': False, 'developer': 'private-marker'}):
            with self.subTest(body=body):
                with self.assertRaises(ValueError) as raised:
                    self.store.configure_screen(body)
                self.assertNotIn('private-marker', str(raised.exception))
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_developer_shapes_types_rules_and_self_supplied_expiry_are_rejected(self):
        self.save_developer()
        before = self.store.path.read_bytes()
        valid = self.developer()
        bad = (None, [], {}, {**valid, 'enabled': 1}, {**valid, 'allow_commands': 1},
               {key: value for key, value in valid.items() if key != 'allow_commands'},
               {**valid, 'expires_at': self.now + 999999}, {**valid, 'command': 'private-marker'},
               *({**valid, 'minutes': value} for value in (None, True, 0, 1441, '60', 1.5)),
               *({**valid, 'allowed_ips': value} for value in (
                   [], None, {}, ['0.0.0.0/0'], ['::/0'], ['192.168.0.0/15'],
                   ['private-marker.invalid'], ['127.0.0.1', '127.0.0.1/32'],
                   ['127.0.1.2/24', '127.0.1.8/24'])))
        for body in bad:
            with self.subTest(body=body):
                with self.assertRaises(ValueError) as raised:
                    self.store.configure_developer(body)
                self.assertNotIn('private-marker', str(raised.exception))
                self.assertEqual(self.store.path.read_bytes(), before)
                self.assertTrue(self.store.authorized('192.168.31.1'))

    def test_malformed_persistent_schema_fails_closed_without_overwriting(self):
        self.save_developer()
        valid = json.loads(self.store.path.read_text(encoding='utf-8'))
        dev = valid['developer']
        bad_states = ([], {**valid, 'version': True}, {**valid, 'version': 2},
                      {**valid, 'screen_enabled': 1}, {**valid, 'extra': 'private-marker'},
                      {**valid, 'developer': []},
                      *({**valid, 'developer': bad} for bad in (
                          {**dev, 'extra': True}, {**dev, 'enabled': 'true'},
                          {**dev, 'allow_commands': 1}, {**dev, 'minutes': True},
                          {**dev, 'minutes': 0}, {**dev, 'minutes': 1441},
                          {**dev, 'expires_at': None}, {**dev, 'expires_at': True},
                          {**dev, 'expires_at': -1}, {**dev, 'expires_at': 253402300800},
                          {**dev, 'enabled': False}, {**dev, 'allowed_ips': []},
                          {**dev, 'allowed_ips': ['192.168.31.9/24']},
                          {**dev, 'allowed_ips': ['127.0.0.1', '127.0.0.1']},
                          {**dev, 'allowed_ips': ['private-marker.invalid']},
                          {key: value for key, value in dev.items() if key != 'allow_commands'})))
        for state in bad_states:
            with self.subTest(state=state):
                self.write_state(state)
                before = self.store.path.read_bytes()
                closed = self.assert_closed()
                self.assertNotIn('private-marker', json.dumps(closed))
                with self.assertRaises(OSError):
                    self.store.configure_screen({'enabled': True})
                with self.assertRaises(OSError):
                    self.save_developer()
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_json_duplicate_fields_nonfinite_utf8_and_oversize_are_closed(self):
        valid = settings.ServiceSettingsStore._defaults()
        for raw in (b'{', b'null', b'[]', b'\xff',
                    b'{"version":1,"version":1,"screen_enabled":true,"developer":{}}',
                    b'{"version":1,"screen_enabled":NaN,"developer":{}}',
                    json.dumps(valid).encode('utf-8') + b'\n{}',
                    b'x' * (settings._MAX_BYTES + 1)):
            with self.subTest(raw_length=len(raw)):
                self.store.path.write_bytes(raw)
                os.chmod(self.store.path, 0o600)
                self.assert_closed()
                with self.assertRaises(OSError):
                    self.store.configure_screen({'enabled': True})
                self.assertEqual(self.store.path.read_bytes(), raw)

    def test_directory_in_place_of_state_file_is_not_treated_as_missing(self):
        self.store.path.mkdir()
        self.assert_closed()
        with self.assertRaises(OSError):
            self.store.configure_screen({'enabled': False})
        self.assertTrue(self.store.path.is_dir())

    @unittest.skipIf(os.name == 'nt', 'Unix state permissions and symbolic links')
    def test_insecure_permissions_and_regular_or_dangling_symlinks_fail_closed(self):
        self.save_developer(allowed_ips=['127.0.0.1'])
        os.chmod(self.store.path, 0o644)
        self.assert_closed()
        os.chmod(self.store.path, 0o600)
        target = self.store.path.with_name('saved-settings.json')
        self.store.path.rename(target)
        self.store.path.symlink_to(target)
        self.assert_closed()
        with self.assertRaises(OSError):
            self.store.configure_screen({'enabled': False})
        self.store.path.unlink()
        self.store.path.symlink_to(self.directory / 'missing-settings.json')
        # An enabled legacy bootstrap must never turn a dangling link into a
        # missing-file fallback that silently authorizes developer requests.
        with mock.patch.dict(os.environ, {'R1_DEV_NO_PAIRING': '1', 'R1_DEV_ALLOW_IPS': '127.0.0.1'}):
            self.store = settings.ServiceSettingsStore(self.directory, clock=lambda: self.now)
        self.assert_closed()

    @unittest.skipIf(os.name == 'nt' or not hasattr(os, 'mkfifo'), 'Unix nonblocking special file')
    def test_fifo_state_is_rejected_without_blocking_or_reading_a_writer(self):
        os.mkfifo(self.store.path)
        self.assert_closed()
        with self.assertRaises(OSError):
            self.store.configure_screen({'enabled': False})

    def test_read_io_failure_closes_access_without_falling_back_to_environment(self):
        self.save_developer(allowed_ips=['127.0.0.1'])
        before = self.store.path.read_bytes()
        with mock.patch.object(settings.os, 'open', side_effect=PermissionError('private-marker')):
            self.assert_closed()
            with self.assertRaises(OSError) as raised:
                self.store.configure_screen({'enabled': False})
            self.assertNotIn('private-marker', str(raised.exception))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_atomic_write_io_failures_keep_old_bytes_remove_temporary_and_latch_closed(self):
        for stage in ('mkstemp', 'chmod', 'fsync', 'replace'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(prefix='r1-service-write-') as folder:
                store = settings.ServiceSettingsStore(folder, clock=lambda: self.now)
                store.configure_developer(self.developer(allowed_ips=['127.0.0.1']))
                before = store.path.read_bytes()
                target = settings.tempfile if stage == 'mkstemp' else settings.os
                with mock.patch.object(target, stage, side_effect=OSError('private-marker')):
                    with self.assertRaises(OSError) as raised:
                        store.configure_screen({'enabled': False})
                self.assertNotIn('private-marker', str(raised.exception))
                self.assertEqual(store.path.read_bytes(), before)
                self.assertFalse(store.snapshot()['storage_ok'])
                self.assertFalse(store.snapshot()['screen_enabled'])
                self.assertFalse(store.authorized('127.0.0.1'))
                with self.assertRaises(OSError):
                    store.configure_screen({'enabled': True})
                with self.assertRaises(OSError):
                    store.configure_developer(self.developer())
                self.assertEqual(list(Path(folder).glob('.service-settings-*')), [])
                reopened = settings.ServiceSettingsStore(folder, clock=lambda: self.now)
                self.assertTrue(reopened.snapshot()['storage_ok'])
                self.assertTrue(reopened.authorized('127.0.0.1'))

    def test_successful_writes_are_private_and_restart_state_reads_do_not_rewrite(self):
        self.save_developer()
        before = self.store.path.read_bytes()
        stored = json.loads(before.decode('utf-8'))
        self.assertEqual(set(stored), {'version', 'screen_enabled', 'developer'})
        self.assertNotIn('active', stored['developer'])
        self.assertNotIn('reason', stored['developer'])
        self.assertEqual(list(self.directory.glob('.service-settings-*')), [])
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        reopened = settings.ServiceSettingsStore(self.directory, clock=lambda: self.now)
        self.assertTrue(reopened.snapshot()['storage_ok'])
        self.assertTrue(reopened.authorized('192.168.31.1'))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_clock_cannot_create_a_new_developer_window(self):
        for value in (None, True, -1, float('nan'), float('inf'), 253402214400):
            with self.subTest(value=value):
                with mock.patch.object(self.store, 'clock', return_value=value):
                    with self.assertRaises(ValueError):
                        self.save_developer()
                self.assertFalse(self.store.path.exists())


if __name__ == '__main__':
    unittest.main()

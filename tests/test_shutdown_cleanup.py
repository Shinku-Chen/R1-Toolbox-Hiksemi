"""Shutdown failure injection with real descriptors, HTTP and capture threads."""

import copy
import errno
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from api_file_operations import FileAccessError
from api_file_transfers import DownloadSource, UploadTarget
from toolbox import DEFAULTS
from web_config import WebConfigServer


class Controller:
    def snapshot(self):
        return {'settings': copy.deepcopy(DEFAULTS), 'settings_error': None,
                'capabilities': {'screen': {'available': False}, 'rgb': {'available': False}},
                'screen': {}, 'rgb': {}}


class ShutdownCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {'R1_WEB_AUTH_MODE': 'pair'})
        environment.start()
        self.addCleanup(environment.stop)
        self.web = WebConfigServer(Controller(), host='127.0.0.1', port=0,
            asset_dir=ROOT / 'ui/web', state_dir=self.directory / 'state').start()
        self.addCleanup(self.web.stop)
        self.data_path = self.directory / 'retained-data.bin'
        self.data_path.write_bytes(b'real fixture content remains unchanged')
        self.descriptors = []
        self.addCleanup(self._close_remaining_descriptors)

    def _close_remaining_descriptors(self):
        for descriptor in self.descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _descriptor(self, *, write=False):
        descriptor = os.open(self.data_path, os.O_RDWR if write else os.O_RDONLY)
        self.descriptors.append(descriptor)
        return descriptor

    def _request(self, method, path, body=None, cookie=None):
        origin = 'http://127.0.0.1:' + str(self.web.port)
        headers = {'Origin': origin}
        if cookie is not None:
            headers['Cookie'] = cookie
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body).encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.web.port, timeout=3)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            response.read()
            return response.status, dict(response.getheaders())
        finally:
            connection.close()

    def _live_resources(self):
        """Represent already authorized work; only ledger persistence is faked."""
        code = self.web.pairing_info(rotate=True)['code']
        status, headers = self._request('POST', '/api/pair', {'code': code})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.assertEqual(self._request('GET', '/api/session', cookie=cookie)[0], 200)
        self.web.pairing_info(rotate=True)
        self.assertIsNotNone(self.web.pair_hash)

        integration = self.web.integration
        integration.access.configure({'enabled': True})
        root_id = 'root_' + 'a' * 24
        key = integration.access.create_key({'name': 'shutdown fixture', 'role': 'readwrite',
            'scopes': ['files'], 'allowed_ips': ['127.0.0.1'],
            'tool_grants': ['prepare_file_download', 'get_file_transfer', 'cancel_file_transfer',
                            'prepare_file_upload', 'commit_file_upload', 'get_file_operation'],
            'file_roots': [{'id': root_id, 'path': 'admin', 'access': 'readwrite'}]})
        principal, reason = integration.access.authorize('Bearer ' + key['token'], '127.0.0.1', 'files')
        self.assertEqual(reason, 'ok')
        principal.update(_peer='127.0.0.1', _service_generation=integration._service_generation)
        self.assertTrue(integration._is_current(principal))

        files = integration.file_operations
        mutations = files.write_operations.mutations
        operation_id = 'operation_' + 'b' * 32
        operation = {'operation_id': operation_id, 'state': 'pending', 'result_version': 'fixture-version'}
        mutations.ledger.rows = {operation_id: {'operation': operation}}
        # Real held FDs exercise the shutdown owner, independent of the Linux
        # native ACL/O_TMPFILE gates covered by the filesystem execution suites.
        for index in range(2):
            mutations._pending[str(index)] = {'fd': self._descriptor()}
        upload = files.transfers._new(principal,
            UploadTarget(root_id, 'admin/upload.bin', 'upload.bin', None, None, None), 'upload', 4)
        upload.update(fd=self._descriptor(write=True), state='committing',
                      operation={'state': 'pending', 'result_version': 'fixture-version'})
        download = files.transfers._new(principal,
            DownloadSource(root_id, 'admin/download.bin', 'download.bin', None, None), 'download', 4)
        download.update(reader_fd=self._descriptor(), active=True, state='streaming')
        files._cursors['fixture'] = {'private': 'cursor'}

        capture = self.web.fan_diagnostics.start(60, 2000)
        self.assertTrue(self.web.fan_diagnostics._thread.is_alive())
        return integration, files, mutations, operation, upload, download, capture, cookie, key, principal

    def test_ledger_save_failure_closes_real_resources_and_restart_keeps_old_grants_dead(self):
        integration, files, mutations, operation, upload, download, capture, cookie, key, principal = self._live_resources()
        authority = integration.file_authority
        listener, web_thread = self.web.server, self.web.thread
        capture_thread = self.web.fan_diagnostics._thread
        port = self.web.port
        failure = OSError(errno.ENOSPC, '/private/fixture ledger full')
        with mock.patch.object(mutations.ledger, 'save', side_effect=failure) as save:
            with self.assertRaises(OSError) as caught:
                self.web.stop()
            self.assertIs(caught.exception, failure)
            self.assertIs(mutations._close_error, failure)
            self.assertIs(files._close_error, failure)
            self.assertIs(integration._close_error, failure)
            self.assertIs(self.web._stop_error, failure)
            self.assertNotIn('/private/', self.web.reason)
            self.assertEqual(operation['state'], 'result_unconfirmed')
            self.assertIsNone(operation['result_version'])
            self.assertEqual(upload['operation']['state'], 'result_unconfirmed')
            self.assertIsNone(upload['fd'])
            self.assertIsNone(download['reader_fd'])
            self.assertEqual(files._cursors, {})
            self.assertEqual(mutations._pending, {})
            self.assertTrue(authority._closed)
            self.assertTrue(integration.docker_operations._closed.is_set())
            self.assertTrue(integration.deployment_operations._closed.is_set())
            self.assertFalse(integration._is_current(principal))
            self.assertFalse(files._current(principal))
            with self.assertRaises(FileAccessError):
                mutations._current(principal)
            for descriptor in self.descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
            self.descriptors.clear()
            self.assertEqual(self.data_path.read_bytes(), b'real fixture content remains unchanged')

            self.assertIsNone(self.web.server)
            self.assertIsNone(self.web.thread)
            self.assertEqual(listener.socket.fileno(), -1)
            self.assertFalse(web_thread.is_alive())
            self.assertFalse(capture_thread.is_alive())
            self.assertEqual(self.web.fan_diagnostics.result(capture['id'])['state'], 'stopped')
            self.assertEqual(self.web.sessions, {})
            self.assertIsNone(self.web.pair_hash)
            self.assertEqual(self.web.pair_until, 0)
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', port), timeout=0.2)

            # A repeated close must not close a new owner of a recycled fd.
            replacement = self._descriptor()
            self.web.stop()
            files.close()
            mutations.close()
            os.fstat(replacement)
            save.assert_called_once_with()
            self.assertIs(self.web._stop_error, failure)

            self.web.start()
            self.assertIsNone(self.web._stop_error)
            self.assertIsNot(integration.file_operations, files)
            self.assertIsNot(integration.file_authority, authority)
            self.assertFalse(integration._is_current(principal))
            self.assertFalse(files._current(principal))
            with self.assertRaises(FileAccessError):
                mutations._current(principal)
            fresh, reason = integration.access.authorize('Bearer ' + key['token'], '127.0.0.1', 'files')
            self.assertEqual(reason, 'ok')
            fresh['_service_generation'] = integration._service_generation
            self.assertTrue(integration._is_current(fresh))
            self.assertEqual(self._request('GET', '/api/session', cookie=cookie)[0], 401)
            self.web.stop()
            save.assert_called_once_with()

    def test_later_cleanup_errors_do_not_replace_ledger_error_or_leave_listener_open(self):
        unused, files, mutations, *_ = self._live_resources()
        listener, web_thread = self.web.server, self.web.thread
        capture_thread = self.web.fan_diagnostics._thread
        ledger_error = OSError(errno.ENOSPC, 'fixture ledger full')
        stop_capture, shutdown = self.web.fan_diagnostics.stop, listener.shutdown

        def failed_capture_stop():
            stop_capture()
            raise RuntimeError('fixture capture stop error')

        def failed_server_shutdown():
            shutdown()
            raise RuntimeError('fixture shutdown error')

        with mock.patch.object(mutations.ledger, 'save', side_effect=ledger_error), \
                mock.patch.object(self.web.fan_diagnostics, 'stop', side_effect=failed_capture_stop), \
                mock.patch.object(listener, 'shutdown', side_effect=failed_server_shutdown):
            with self.assertRaises(OSError) as caught:
                self.web.stop()
        self.assertIs(caught.exception, ledger_error)
        self.assertEqual(listener.socket.fileno(), -1)
        self.assertFalse(web_thread.is_alive())
        self.assertFalse(capture_thread.is_alive())
        self.assertTrue(self.web.integration.file_authority._closed)
        for descriptor in self.descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.descriptors.clear()
        self.assertEqual(files._cursors, {})
        self.web.stop()

    def test_pending_fd_error_still_closes_other_fds_and_attempts_receipt_save_once(self):
        mutations = self.web.integration.file_operations.write_operations.mutations
        invalid = self._descriptor()
        retained = self._descriptor()
        mutations._pending = {'first': {'fd': invalid}, 'second': {'fd': retained}}
        mutations.ledger.rows = {'fixture': {'operation': {'state': 'running'}}}
        os.close(invalid)
        failure = OSError(errno.ENOSPC, 'fixture ledger full')
        with mock.patch.object(mutations.ledger, 'save', side_effect=failure) as save:
            with self.assertRaises(OSError) as caught:
                mutations.close()
            self.assertEqual(caught.exception.errno, errno.EBADF)
            self.assertIs(mutations._close_error, caught.exception)
            with self.assertRaises(OSError):
                os.fstat(retained)
            self.descriptors.clear()
            self.assertEqual(mutations.ledger.rows['fixture']['operation']['state'], 'result_unconfirmed')
            self.assertEqual(mutations._pending, {})
            mutations.close()
            save.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

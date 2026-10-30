"""Fixed native tools, process-lifetime receipts and actual HTTP fixtures."""

import importlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
ops = importlib.import_module('vendor_native_capabilities')
transport = importlib.import_module('vendor_business_transport')
specs = importlib.import_module('native_capability_specs')

PRINCIPAL = {'key_id': 'native-fixture', 'grant_revision': 4, 'credential_generation': 2}


def allow(*_args):
    return True


def app(identifier=5, name='应用', **extra):
    return {'apply_id': identifier, 'module_name': name, 'version': '1.0', 'status': 4, **extra}


def album(identifier='8', kind='custom'):
    return {'album_id': identifier, 'type': kind, 'name': '相册', 'count': 2}


class Client:
    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = []
        self.before_return = None

    def request(self, operation, parameters, body, authorizer, *, confirmed=False):
        self.calls.append((operation, parameters, body, confirmed))
        self_outer = self
        if callable(self.before_return):
            self.before_return(len(self_outer.calls))
        value = self.frames.pop(0)
        if isinstance(value, Exception):
            raise value
        return 200, value


class NativeCapabilityTests(unittest.TestCase):
    def backend(self, *frames):
        client = Client(frames)
        return ops.NativeCapabilities(client=client, idempotency_store=ops.IdempotencyStore()), client

    def execute(self, backend, name, args=None, authorizer=allow, principal=None):
        return backend.execute(name, args or {}, authorizer=authorizer,
                               principal=PRINCIPAL if principal is None else principal)

    def write(self, **values):
        return {'confirmed': True, 'idempotency_key': 'operation:1', **values}

    def test_specs_are_pure_and_exactly_implemented(self):
        self.assertEqual(len(specs.TOOL_SPECS), 17)
        self.assertEqual(set(ops._OPERATIONS), {s['name'] for s in specs.TOOL_SPECS})
        for item in specs.TOOL_SPECS:
            self.assertFalse(item['annotations']['readOnlyHint'])
            self.assertFalse(item['annotations']['idempotentHint'])
            self.assertFalse(item['inputSchema']['additionalProperties'])
            self.assertIn(item['min_role'], ('readwrite', 'admin'))

    def test_application_catalog_has_ids_but_no_native_paths_or_sources(self):
        backend, _ = self.backend({'list': [app(path='/PRIVATE', source='SECRET', vid='SECRET')]})
        value = self.execute(backend, 'list_native_applications')
        self.assertTrue(value['available'])
        self.assertIsNone(value['native']['code'])
        self.assertEqual(value['data']['items'][0]['application_id'], 5)
        self.assertIsNone(value['data']['items'][0]['running'])
        self.assertNotIn('PRIVATE', json.dumps(value))
        self.assertNotIn('SECRET', json.dumps(value))

    def test_catalog_duplicate_and_boolean_ids_are_rejected(self):
        for rows in ([app(), app()], [app(True)], [app(0)], [app(2 ** 31)], [app(name='\x00')]):
            backend, _ = self.backend({'list': rows})
            self.assertEqual(self.execute(backend, 'list_native_applications')['reason'], 'invalid_json')

    def test_application_progress_requires_current_catalog_and_explicit_confirmation(self):
        backend, client = self.backend({'list': [app()]}, {'code': 0, 'apply_id': 5,
                                                      'size': 10, 'total_size': 20, 'status': 3,
                                                      'path': 'SECRET'})
        result = self.execute(backend, 'get_application_install_progress', self.write(application_id=5))
        self.assertTrue(result['available'])
        self.assertEqual([x[0] for x in client.calls], ['apply_list', 'application_progress'])
        self.assertTrue(client.calls[1][3])
        self.assertEqual(result['data']['downloaded_bytes'], 10)
        self.assertIsNone(result['data']['progress_percent'])
        self.assertNotIn('SECRET', json.dumps(result))
        replay = self.execute(backend, 'get_application_install_progress', self.write(application_id=5))
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(len(client.calls), 2)

    def test_progress_absent_app_or_changed_response_does_not_succeed(self):
        backend, client = self.backend({'list': [app(4)]})
        result = self.execute(backend, 'get_application_install_progress', self.write(application_id=5))
        self.assertEqual(result['reason'], 'object_not_in_owner_catalog')
        self.assertEqual(len(client.calls), 1)
        backend, _ = self.backend({'list': [app()]}, {'code': 0, 'apply_id': 9})
        result = self.execute(backend, 'get_application_install_progress', self.write(application_id=5))
        self.assertEqual(result['reason'], 'result_unconfirmed')

    def test_backup_tasks_only_current_owner_metadata_and_true_counts(self):
        task = {'task_id': 19, 'task_status': 0, 'user': 100000, 'total_size': 90,
                'from': '/PRIVATE-SOURCE', 'to': '/PRIVATE-TARGET', 'disk_uuid': 'SECRET'}
        backend, _ = self.backend({'run_list': [task], 'done_list': []})
        result = self.execute(backend, 'list_backup_tasks')
        self.assertTrue(result['available'])
        self.assertEqual(result['data']['counts'], {'running': 1, 'waiting': None, 'done': 0})
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('SECRET', json.dumps(result))
        task['user'] = 100001
        backend, _ = self.backend({'run_list': [task]})
        self.assertFalse(self.execute(backend, 'list_backup_tasks')['available'])
        backend, _ = self.backend({})
        self.assertFalse(self.execute(backend, 'list_backup_tasks')['available'])

    def test_auto_backup_preserves_unknowns_and_does_not_change_setting(self):
        backend, client = self.backend({'code': 0, 'udisk_a_status': 1,
                                       'sd_status': None, 'udisk_a_type': 2})
        value = self.execute(backend, 'get_auto_backup_settings')
        self.assertEqual(value['data'], {'udisk_a_status': 1, 'sd_status': None,
                                        'udisk_a_type': 2, 'sd_type': None})
        self.assertEqual(client.calls, [('auto_backup_get', {}, {}, False)])

    def test_user_directory_does_not_return_cloud_accounts_or_permissions(self):
        backend, _ = self.backend({'code': 0, 'user_list': [{'user_id': 100000,
                                  'user_name': 'admin', 'nickname': '所有者',
                                  'account_email': 'SECRET', 'permit': {'SECRET': True},
                                  'icon': '/PRIVATE', 'account_name': 'SECRET'}]})
        result = self.execute(backend, 'list_nas_users')
        self.assertTrue(result['available'])
        self.assertEqual(result['data']['items'][0], {'user_id': 100000, 'name': 'admin', 'nickname': '所有者'})
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_device_name_get_and_set_are_fixed_and_do_not_expose_serial(self):
        backend, client = self.backend({'code': 0, 'name': '小可爱NAS', 'serial_number': 'PRIVATE'}, {'code': 0})
        value = self.execute(backend, 'get_nas_name')
        self.assertEqual(value['data'], {'name': '小可爱NAS'})
        result = self.execute(backend, 'set_nas_name', self.write(name='NAS新名字'))
        self.assertTrue(result['available'])
        self.assertEqual(client.calls[1], ('device_name_set', {'name': 'NAS新名字'}, {}, True))
        self.assertNotIn('PRIVATE', json.dumps(value))
        self.assertFalse(result['data']['asynchronous_completion_verified'])

    def test_ai_settings_and_boolean_only_persistent_toggle(self):
        backend, client = self.backend({'code': 0, 'status': 0}, {'code': 0})
        self.assertFalse(self.execute(backend, 'get_nas_ai_settings')['data']['enabled'])
        result = self.execute(backend, 'set_nas_ai_enabled', self.write(enabled=True))
        self.assertTrue(result['available'])
        self.assertEqual(client.calls[1], ('nas_ai_set', {'mode': 1}, {}, True))
        backend, _ = self.backend({'code': 0, 'status': 7})
        self.assertIsNone(self.execute(backend, 'get_nas_ai_settings')['data']['enabled'])
        backend, client = self.backend()
        self.assertEqual(self.execute(backend, 'set_nas_ai_enabled', self.write(enabled=1))['reason'], 'invalid_parameters')
        self.assertFalse(client.calls)

    def test_scan_status_requires_exact_library_and_no_fake_progress(self):
        backend, client = self.backend({'code': 0, 'status': 1, 'scaned': 5, 'total': 20,
                                       'timestamp': 123, 'num': 2, 'path': '/PRIVATE'})
        result = self.execute(backend, 'get_movie_library_scan_status', {'library_id': 3})
        self.assertTrue(result['available'])
        self.assertEqual(client.calls, [('movie_scan_status', {}, {'lid': 3}, False)])
        self.assertEqual(result['data']['native']['scaned'], 5)
        self.assertIsNone(result['data']['progress_percent'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_disable_auto_backup_never_allows_enabling_or_arbitrary_sources(self):
        for source, native in (('usb', 'udisk_a'), ('sd', 'sd')):
            backend, client = self.backend({'code': 0, 'ErrCode': 0})
            result = self.execute(backend, 'disable_auto_backup', self.write(source=source))
            self.assertTrue(result['available'])
            self.assertEqual(client.calls, [('auto_backup_disable', {'disk_type': native, 'status': 0}, {}, True)])
        backend, client = self.backend()
        self.assertFalse(self.execute(backend, 'disable_auto_backup', self.write(source='/drives/private'))['available'])
        self.assertFalse(self.execute(backend, 'disable_auto_backup', self.write(source='usb', enabled=True))['available'])
        self.assertFalse(client.calls)
        for params in ({'disk_type': 'udisk_a', 'status': 1}, {'disk_type': 'all', 'status': 0}):
            with self.assertRaises(transport.BusinessTransportError):
                transport._validate('auto_backup_disable', params, {})

    def test_backup_halt_requires_current_owner_task_version_and_exact_state(self):
        task = {'task_id': 19, 'task_status': 0, 'user': 100000, 'total_size': 90,
                'from': '/PRIVATE-SOURCE', 'to': '/PRIVATE-TARGET', 'atid': 'uuid1'}
        frame = {'run_list': [task], 'wait_list': [], 'done_list': []}
        version = ops._backup_tasks(frame)['groups']['running'][0]['version']
        self.assertRegex(version, r'^backup_[0-9a-f]{32}$')
        for tool, state in (('pause_backup_task', 2), ('cancel_backup_task', 1)):
            backend, client = self.backend(frame, {'code': 0})
            result = self.execute(backend, tool, self.write(task_id=19, expected_version=version))
            self.assertTrue(result['available'])
            self.assertEqual(client.calls[1], ('backup_task_halt', {'task_id': 19, 'task_status': state}, {}, True))
            self.assertNotIn('PRIVATE', json.dumps(result))
        backend, client = self.backend(frame)
        stale = 'backup_' + '0' * 32
        result = self.execute(backend, 'pause_backup_task', self.write(task_id=19, expected_version=stale))
        self.assertEqual(result['reason'], 'object_not_in_owner_catalog')
        self.assertEqual(len(client.calls), 1)
        changed = {**task, 'to': '/different-private-destination'}
        self.assertNotEqual(ops._backup_tasks({'run_list': [changed]})['groups']['running'][0]['version'], version)

    def test_waiting_backup_can_only_cancel_and_resume_is_not_a_fixed_action(self):
        frame = {'run_list': [], 'wait_list': [{'task_id': 20, 'task_status': 3, 'user': 100000}], 'done_list': []}
        version = ops._backup_tasks(frame)['groups']['waiting'][0]['version']
        backend, client = self.backend(frame)
        result = self.execute(backend, 'pause_backup_task', self.write(task_id=20, expected_version=version))
        self.assertEqual(result['reason'], 'object_not_in_owner_catalog')
        self.assertEqual(len(client.calls), 1)
        backend, client = self.backend(frame, {'code': 0})
        self.assertTrue(self.execute(backend, 'cancel_backup_task', self.write(task_id=20, expected_version=version))['available'])
        with self.assertRaises(transport.BusinessTransportError):
            transport._validate('backup_task_halt', {'task_id': 20, 'task_status': 0}, {})

    def test_delete_only_owner_normal_custom_album_and_never_shared_or_reserved(self):
        for row in (album('7'), album('8', 'share_custom'), album('9')):
            backend, client = self.backend({'code': 0, 'all_album_list': [row]})
            result = self.execute(backend, 'delete_album', self.write(album_id='8'))
            self.assertEqual(result['reason'], 'object_not_in_owner_catalog')
            self.assertEqual(len(client.calls), 1)
        backend, client = self.backend({'code': 0, 'all_album_list': [album()]}, {'code': 0})
        result = self.execute(backend, 'delete_album', self.write(album_id='8'))
        self.assertTrue(result['available'])
        self.assertEqual(client.calls[1], ('album_delete', {'album_id': '8'}, {}, True))
        self.assertEqual(result['data']['verification'], 'factory_acknowledgement_only')

    def test_extra_fields_and_arbitrary_native_arguments_never_reach_transport(self):
        backend, client = self.backend()
        cases = [('list_native_applications', {'action': 'install_apply'}),
                 ('set_nas_name', self.write(name='a&action=reboot')),
                 ('set_nas_name', self.write(name='字' * 22)),
                 ('delete_album', self.write(album_id='08')),
                 ('get_movie_library_scan_status', {'library_id': True}),
                 ('set_nas_ai_enabled', {'enabled': True, 'confirmed': 1, 'idempotency_key': 'x'}),
                 ('delete_album', self.write(album_id='8', user_id=100001))]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                self.assertFalse(self.execute(backend, name, args)['available'])
        self.assertFalse(client.calls)

    def test_current_authorization_required_before_send_after_response_and_replay(self):
        active = [True]
        backend, client = self.backend({'code': 0})
        callback = lambda *_: active[0]
        self.assertEqual(self.execute(backend, 'set_nas_ai_enabled', self.write(enabled=True),
                                    authorizer=lambda *_: 1)['reason'], 'authorization_denied')
        self.assertFalse(client.calls)
        client.before_return = lambda _n: active.__setitem__(0, False)
        result = self.execute(backend, 'set_nas_ai_enabled', self.write(enabled=True), authorizer=callback)
        self.assertEqual(result['reason'], 'result_unconfirmed')
        self.assertIsNone(result['data'])
        result = self.execute(backend, 'set_nas_ai_enabled', self.write(enabled=True), authorizer=callback)
        self.assertEqual(result['reason'], 'authorization_denied')
        self.assertEqual(len(client.calls), 1)

    def test_unknown_write_is_not_replayed_and_key_conflict_is_global(self):
        error = transport.BusinessTransportError('timeout', request_sent=True)
        backend, client = self.backend(error)
        args = self.write(enabled=True)
        first = self.execute(backend, 'set_nas_ai_enabled', args)
        self.assertEqual(first['mutation_outcome'], 'unknown')
        second = self.execute(backend, 'set_nas_ai_enabled', args)
        self.assertTrue(second['idempotent_replay'])
        self.assertEqual(len(client.calls), 1)
        conflict = self.execute(backend, 'set_nas_name', self.write(name='another'))
        self.assertEqual(conflict['reason'], 'idempotency_conflict')

    def test_native_code_must_be_exact_int_and_errors_never_echo_message(self):
        for frame in ({'code': True}, {'code': '0'}, {'msg': 'PRIVATE'}):
            backend, _ = self.backend(frame)
            self.assertFalse(self.execute(backend, 'get_nas_ai_settings')['available'])
        backend, _ = self.backend({'code': -1, 'msg': 'PRIVATE access_token'})
        result = self.execute(backend, 'get_nas_ai_settings')
        self.assertEqual(result['native']['code'], -1)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_explicit_probe_never_calls_progress_or_mutations(self):
        client = Client([{'list': []}, {'run_list': [], 'wait_list': [], 'done_list': []},
                         {'code': 0}, {'code': 0, 'user_list': []},
                         {'code': 0, 'name': 'NAS'}, {'code': 0, 'status': 1}])
        result = ops.collect_native_capabilities_probe(client=client, authorizer=allow)
        self.assertEqual(len(result['items']), 6)
        self.assertTrue(result['may_wake_disks'])
        self.assertTrue(all(not x[3] for x in client.calls))
        self.assertNotIn('application_progress', [x[0] for x in client.calls])

    def test_package_context_only_reports_field_shapes_without_claiming_trust(self):
        row = app(source='https://PRIVATE-PACKAGE', vid='PRIVATE-VID',
                  signature='PRIVATE-SIGNATURE', module_type=2)
        client = Client([{'list': [row]}])
        result = ops.collect_application_package_context(client=client, authorizer=allow)
        self.assertTrue(result['available'])
        self.assertFalse(result['trusted_package_manifest_verified'])
        self.assertFalse(result['package_execution_available'])
        fields = result['items'][0]['field_shapes']
        self.assertEqual(fields['module_type'], {'type': 'integer'})
        self.assertEqual(fields['hash'], {'type': 'missing'})
        self.assertEqual(fields['signature']['type'], 'string')
        self.assertNotIn('PRIVATE', json.dumps(result))
        active = [True]
        client = Client([{'list': [row]}])
        client.before_return = lambda _n: active.__setitem__(0, False)
        result = ops.collect_application_package_context(client=client, authorizer=lambda *_: active[0])
        self.assertEqual(result['reason'], 'authorization_denied')
        self.assertIsNone(result['items'])

    def test_package_context_enumerates_native_codes_without_guessing_sources(self):
        row = app(source=2, module_type=7, apply_opt=1, guest=0, upgradable=1,
                  support_terminal=3, support_ecloud=True, vid='PRIVATE-VID',
                  path='/PRIVATE/PACKAGE', paramList='PRIVATE-PARAMETERS')
        client = Client([{'list': [row]}])
        result = ops.collect_application_package_context(client=client, authorizer=allow)
        self.assertEqual(result['schema_version'], 2)
        self.assertEqual(client.calls, [('apply_list', {}, {}, False)])
        self.assertFalse(result['package_execution_available'])
        self.assertFalse(result['protocol']['independent_signature_requirement'])
        self.assertTrue(result['protocol']['source_is_native_integer_enum'])
        item = result['items'][0]
        self.assertEqual(item['enum_fields']['source'], {'code': 2, 'meaning': 'native_enum_unmapped'})
        self.assertEqual(item['enum_fields']['module_type']['code'], 7)
        self.assertIsNone(item['enum_fields']['support_ecloud']['code'])
        self.assertNotIn('source', item['url_fields'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_package_context_url_and_checksum_shapes_do_not_expose_values(self):
        row = app(download_url='https://PRIVATE-USER:PRIVATE-PASS@private.example:443/PRIVATE?token=PRIVATE#PRIVATE',
                  package_url='http://127.0.0.1/PRIVATE', url='file:///PRIVATE',
                  md5='a' * 32, sha1='b' * 40, sha256='c' * 64, sha512='d' * 128,
                  checksum='e' * 64, hash='PRIVATE-HASH', signature='PRIVATE-SIGNATURE')
        result = ops.collect_application_package_context(client=Client([{'list': [row]}]), authorizer=allow)
        self.assertTrue(result['available'])
        item = result['items'][0]
        self.assertEqual(item['url_fields']['download_url'], {
            'scheme': 'https', 'host_category': 'dns_name', 'has_userinfo': True,
            'has_query': True, 'has_fragment': True, 'port_present': True,
            'source_validated': False})
        self.assertEqual(item['url_fields']['package_url']['host_category'], 'loopback_ip')
        self.assertEqual(item['url_fields']['url']['host_category'], 'absent')
        for field in ('md5', 'sha1', 'sha256', 'sha512'):
            self.assertTrue(item['checksum_fields'][field]['matches_algorithm_hex_length'])
            self.assertFalse(item['checksum_fields'][field]['verified'])
        self.assertEqual(item['checksum_fields']['checksum']['length_algorithm_hint'], 'sha256')
        self.assertEqual(item['checksum_fields']['hash']['format'], 'other')
        encoded = json.dumps(result)
        self.assertNotIn('PRIVATE', encoded)
        self.assertNotIn('private.example', encoded)
        self.assertNotIn('a' * 32, encoded)

    def test_package_context_invalid_optional_values_remain_unknown(self):
        row = app(source=2 ** 40, module_type=False, download_url='http://[broken',
                  package_url='http://private.example:bad/PRIVATE', url='PRIVATE\x00',
                  sha256='a' * 32, checksum=None)
        result = ops.collect_application_package_context(client=Client([{'list': [row]}]), authorizer=allow)
        self.assertTrue(result['available'])
        item = result['items'][0]
        self.assertIsNone(item['enum_fields']['source']['code'])
        self.assertIsNone(item['enum_fields']['module_type']['code'])
        self.assertEqual(item['url_fields']['download_url']['scheme'], 'invalid')
        self.assertFalse(item['checksum_fields']['sha256']['matches_algorithm_hex_length'])
        self.assertIsNone(item['checksum_fields']['checksum'])
        self.assertNotIn('PRIVATE', json.dumps(result))


class NativeHTTPFixtureTests(unittest.TestCase):
    def test_fixed_routes_methods_body_and_pinned_transport_over_real_http(self):
        events = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(length)) if length else {}
                action = query['action'][0]
                events.append((self.command, parsed.path, action, query, body))
                frames = {'get_apply_list': {'list': [app()]},
                          'get_install_progress': {'code': 0, 'apply_id': body.get('apply_id'),
                                                   'size': 10, 'total_size': 20, 'status': 3},
                          'set_dev_name': {'code': 0}, 'set_ai_switch': {'code': 0},
                          'get_dev_name': {'code': 0, 'name': 'NAS', 'serial_number': 'PRIVATE'}}
                data = json.dumps(frames[action]).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        identity = {'available': True, 'status': 'verified', 'pid': 10, 'start_ticks': 20,
                    'socket_inode': '123', 'connect_host': '127.0.0.1'}
        fixed_connects = []

        class Connection:
            def __init__(self, family, kind):
                self.raw = socket.socket(family, kind)

            def connect(self, address):
                fixed_connects.append(address)
                self.raw.connect(server.server_address)

            def settimeout(self, value):
                self.raw.settimeout(value)

            def getpeercert(self, **_kwargs):
                return b'FIXTURE-CERT'

            def sendall(self, value):
                self.raw.sendall(value)

            def recv(self, count):
                return self.raw.recv(count)

            def close(self):
                self.raw.close()

        try:
            native = transport.VendorBusinessClient(
                session_reader=lambda **_kwargs: (SimpleNamespace(user_id=100000,
                    derive_access_token=lambda: 'PRIVATE-FIXTURE'), {'available': True, 'status': 'ready'}),
                listener_check=lambda **_kwargs: identity,
                certificate_reader=lambda _root: b'FIXTURE-CERT', socket_factory=Connection,
                tls_context=SimpleNamespace(wrap_socket=lambda raw, **_kwargs: raw))
            backend = ops.NativeCapabilities(client=native, idempotency_store=ops.IdempotencyStore())
            first = backend.execute('get_application_install_progress', {'application_id': 5,
                                    'confirmed': True, 'idempotency_key': 'fixture:1'},
                                    authorizer=allow, principal=PRINCIPAL)
            self.assertTrue(first['available'])
            second = backend.execute('set_nas_name', {'name': '名字', 'confirmed': True,
                                      'idempotency_key': 'fixture:2'}, authorizer=allow, principal=PRINCIPAL)
            self.assertTrue(second['available'])
            third = backend.execute('get_nas_name', {}, authorizer=allow)
            self.assertEqual(third['data'], {'name': 'NAS'})
            self.assertEqual([x[:3] for x in events], [
                ('GET', '/rest/1.1/applycenter', 'get_apply_list'),
                ('POST', '/rest/1.1/applycenter', 'get_install_progress'),
                ('GET', '/rest/1.1/login', 'set_dev_name'),
                ('GET', '/rest/1.1/login', 'get_dev_name')])
            self.assertEqual(events[1][4], {'apply_id': 5})
            self.assertEqual(events[2][3]['name'], ['名字'])
            self.assertTrue(all(x == ('127.0.0.1', 20443) for x in fixed_connects))
            self.assertNotIn('PRIVATE', json.dumps(first))
            self.assertNotIn('PRIVATE', json.dumps(third))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()

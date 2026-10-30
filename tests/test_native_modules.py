"""Fixed native module inventory, independent table observations and transport."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
native = importlib.import_module('vendor_native_capabilities')
transport = importlib.import_module('vendor_business_transport')
specs = importlib.import_module('native_capability_specs')


def module(identifier=23, name='thunder', **extra):
    return {'id': identifier, 'module_name': name, 'curr_ver': 'V1.0.0',
            'latest_ver': 'V1.0.1', 'status': 4, 'type': 'local',
            'upgradable': 0, 'usable': 1, 'device_need_update': 0, **extra}


def inventory(available=None, installed=None):
    return {'code': 0, 'available_list': [] if available is None else available,
            'installed_list': [] if installed is None else installed}


class Client:
    def __init__(self, frame, after=None):
        self.frame, self.after, self.calls = frame, after, []

    def request(self, operation, parameters, body, authorizer, *, confirmed=False):
        self.calls.append((operation, parameters, body, confirmed))
        if self.after:
            self.after()
        return 200, self.frame


class NativeModuleTests(unittest.TestCase):
    def execute(self, frame, *, arguments=None):
        client = Client(frame)
        result = native.NativeCapabilities(client=client).execute(
            'list_native_modules', {} if arguments is None else arguments,
            authorizer=lambda *_: True)
        return result, client

    def test_spec_uses_explicit_apps_readwrite_without_side_effect_free_claim(self):
        item = next(x for x in specs.TOOL_SPECS if x['name'] == 'list_native_modules')
        self.assertEqual(item['scope'], 'apps')
        self.assertEqual(item['min_role'], 'readwrite')
        self.assertFalse(item['annotations']['readOnlyHint'])
        self.assertFalse(item['annotations']['idempotentHint'])
        self.assertEqual(item['inputSchema']['properties'], {})
        self.assertFalse(item['inputSchema']['additionalProperties'])

    def test_tables_are_not_merged_or_interpreted_as_running(self):
        result, client = self.execute(inventory([module(23, 'available')], [module(23, 'installed', status=99)]))
        self.assertTrue(result['available'])
        self.assertTrue(result['may_contact_vendor'])
        self.assertEqual(client.calls, [('module_inventory', {}, {}, False)])
        data = result['data']
        self.assertEqual(data['groups']['available']['items'][0]['name'], 'available')
        self.assertEqual(data['groups']['installed']['items'][0]['name'], 'installed')
        self.assertEqual(data['groups']['installed']['items'][0]['native_status_code'], 99)
        self.assertIsNone(data['groups']['installed']['items'][0]['running'])
        self.assertFalse(data['catalog_confers_write_identity'])
        self.assertFalse(data['running_state_available'])

    def test_unknown_fields_are_null_and_paths_never_become_text(self):
        result, _ = self.execute(inventory(installed=[module(
            curr_ver='/PRIVATE/PATH', latest_ver='https://PRIVATE-HOST/version',
            type='https://PRIVATE', status=True, upgradable='1', usable=None,
            inst_path='/PRIVATE/INSTALL', paramList='PRIVATE-PARAMETERS',
            download_url='https://PRIVATE/OBJECT', access_token='PRIVATE-TOKEN')]))
        self.assertTrue(result['available'])
        row = result['data']['groups']['installed']['items'][0]
        for field in ('current_version', 'available_version', 'native_type', 'native_status_code',
                      'upgradable_code', 'usable_code'):
            self.assertIsNone(row[field])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('access_token', json.dumps(result))

    def test_missing_table_or_wrong_table_type_fails_instead_of_inventing_empty(self):
        for frame in ({'code': 0}, {'code': 0, 'available_list': []},
                      inventory(available={}), inventory(installed='PRIVATE'),
                      inventory(installed={'list_cm': []})):
            result, _ = self.execute(frame)
            self.assertFalse(result['available'])
            self.assertEqual(result['reason'], 'invalid_json')
            self.assertIsNone(result['data'])

    def test_duplicate_id_in_invalid_node_rejects_both_and_preserves_other_node(self):
        result, _ = self.execute(inventory(installed=[module(23), module(7, 'valid'),
                                                     module(7, '\x00PRIVATE')]))
        self.assertTrue(result['available'])
        group = result['data']['groups']['installed']
        self.assertEqual([x['module_id'] for x in group['items']], [23])
        self.assertEqual(group['accepted_entries'], 1)
        self.assertEqual(group['rejected_entries'], 2)
        self.assertTrue(group['partial'])
        self.assertEqual(group['rejections'], [{'code': 'duplicate_module_id', 'count': 2}])

    def test_invalid_node_types_ids_and_path_names_are_isolated_with_fixed_codes(self):
        result, _ = self.execute(inventory(available=[module(), None, module(True),
            module(0), module(7, '/PRIVATE/PATH'), module(8, 'PRIVATE\\PATH')]))
        self.assertTrue(result['available'])
        group = result['data']['groups']['available']
        self.assertEqual(group['accepted_entries'], 1)
        self.assertEqual(group['rejected_entries'], 5)
        self.assertEqual({x['code']: x['count'] for x in group['rejections']}, {
            'unsupported_module_entry': 1, 'invalid_module_id': 2, 'invalid_module_label': 2})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_nonempty_all_bad_table_remains_unavailable(self):
        for rows in ([module(0)], [module(7), module(7)], [module(name='/PRIVATE')]):
            result, _ = self.execute(inventory(installed=rows))
            self.assertFalse(result['available'])
            self.assertEqual(result['reason'], 'invalid_json')
        result, _ = self.execute(inventory())
        self.assertTrue(result['available'])
        self.assertEqual(result['data']['groups']['installed']['items'], [])

    def test_bounds_and_parameters_are_rejected_before_any_unfixed_action(self):
        result, _ = self.execute(inventory(available=[module(i + 1) for i in range(257)]))
        self.assertEqual(result['reason'], 'invalid_json')
        for args in ({'id': 23}, {'action': 'modulize_set_oper'}, {'confirmed': True}):
            result, client = self.execute(inventory(), arguments=args)
            self.assertEqual(result['reason'], 'invalid_parameters')
            self.assertEqual(client.calls, [])
        for parameters, body in (({'id': 23}, {}), ({}, {'operation': 'run'})):
            with self.assertRaises(transport.BusinessTransportError):
                transport._validate('module_inventory', parameters, body)

    def test_authorization_denial_and_late_revocation_discard_projection(self):
        client = Client(inventory(installed=[module()]))
        backend = native.NativeCapabilities(client=client)
        result = backend.execute('list_native_modules', {}, authorizer=lambda *_: False)
        self.assertEqual(result['reason'], 'authorization_denied')
        self.assertEqual(client.calls, [])
        active = [True]
        client.after = lambda: active.__setitem__(0, False)
        result = backend.execute('list_native_modules', {}, authorizer=lambda *_: active[0])
        self.assertEqual(result['reason'], 'authorization_denied')
        self.assertIsNone(result['data'])

    def test_native_code_must_be_exact_zero_and_never_returns_upstream_message(self):
        for code in (None, True, '0', -1):
            result, _ = self.execute({**inventory(), 'code': code, 'msg': 'PRIVATE'})
            self.assertFalse(result['available'])
            self.assertNotIn('PRIVATE', json.dumps(result))
        frame = inventory()
        frame.pop('code')
        result, _ = self.execute(frame)
        self.assertEqual(result['reason'], 'invalid_json')


class ModuleInventoryHttpTests(unittest.TestCase):
    def test_actual_http_uses_only_fixed_owner_route_and_projects_finite_fields(self):
        events, connections = [], []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                events.append((self.command, parsed.path, query.get('action'), sorted(query)))
                raw = json.dumps(inventory(installed=[module(inst_path='/PRIVATE', paramList='PRIVATE')])).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        class Connection:
            def __init__(self, *_args):
                self.raw = socket.socket()

            def settimeout(self, value):
                self.raw.settimeout(value)

            def connect(self, target):
                connections.append(target)
                self.raw.connect(server.server_address)

            def getpeercert(self, **_kwargs):
                return b'FIXTURE-CERT'

            def sendall(self, data):
                self.raw.sendall(data)

            def recv(self, count):
                return self.raw.recv(count)

            def close(self):
                self.raw.close()

        identity = {'available': True, 'status': 'verified', 'pid': 10,
                    'start_ticks': 20, 'socket_inode': '30', 'connect_host': '127.0.0.1'}
        try:
            client = transport.VendorBusinessClient(
                session_reader=lambda **_kwargs: (SimpleNamespace(user_id=100000,
                    derive_access_token=lambda: 'PRIVATE-FIXTURE'), {'available': True, 'status': 'ready'}),
                listener_check=lambda **_kwargs: identity,
                certificate_reader=lambda _root: b'FIXTURE-CERT', socket_factory=Connection,
                tls_context=SimpleNamespace(wrap_socket=lambda raw, **_kwargs: raw))
            result = native.NativeCapabilities(client=client).execute('list_native_modules', {},
                                                                      authorizer=lambda *_: True)
            self.assertTrue(result['available'], result)
            self.assertEqual(events, [('GET', '/rest/1.1/update', ['get_modulize_table'],
                                       ['access_token', 'action'])])
            self.assertEqual(connections, [('127.0.0.1', 20443)])
            self.assertNotIn('PRIVATE', json.dumps(result))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()

"""Audited new native install plans, exact catalogs and uncertain receipts."""

import copy
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

PRINCIPAL = {'key_id': 'install-fixture', 'grant_revision': 1, 'credential_generation': 1,
             '_service_generation': 1}


def catalogs():
    app = {'apply_id': 34, 'module_name': 'netdisk_bd2', 'version': 'V1.0.14',
           'source': 0, 'module_type': 0, 'status': 0, 'size': 1234, 'vid': '',
           'guest': 0, 'path': '/PRIVATE/APPLICATION', 'install_time': 0, 'apply_opt': 0}
    module = {'id': 34, 'module_name': 'netdisk_bd2', 'type': 'extension',
              'curr_ver': '0', 'latest_ver': 'V1.0.14', 'size': 1234, 'status': 0,
              'upgradable': -1, 'usable': 0, 'device_need_update': 0,
              'inst_path': '/PRIVATE/PROGRAM', 'paramList': 'PRIVATE-PARAMETERS'}
    return {'apply_list': {'list': [app]}, 'module_inventory': {'code': 0,
            'available_list': [module], 'installed_list': []}}


class Client:
    def __init__(self):
        self.frames, self.calls = catalogs(), []
        self.reply = {'apply_id': 34, 'module_name': 'netdisk_bd2'}
        self.after = None

    def request(self, operation, parameters, body, authorizer, *, confirmed=False):
        self.calls.append((operation, copy.deepcopy(parameters), copy.deepcopy(body), confirmed))
        value = self.reply if operation == 'application_install' else self.frames[operation]
        if self.after:
            self.after(operation)
        if isinstance(value, Exception):
            raise value
        return 200, copy.deepcopy(value)


class NativeApplicationInstallTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.backend = native.NativeCapabilities(client=self.client,
                                                idempotency_store=native.IdempotencyStore())
        self.active = True

    def call(self, tool, arguments, principal=None):
        return self.backend.execute(tool, arguments, principal=principal or PRINCIPAL,
                                    authorizer=lambda *_: self.active)

    def plan(self):
        return self.call('plan_native_application_install', {'application_id': 34})

    def install(self, version, **extra):
        return self.call('install_native_application', {'application_id': 34,
            'expected_version': version, 'confirmed': True, 'idempotency_key': 'install:1', **extra})

    def test_plan_only_queries_two_fixed_catalogs_and_lists_actual_effects(self):
        result = self.plan()
        self.assertTrue(result['available'], result)
        self.assertTrue(result['data']['allowed'])
        self.assertRegex(result['data']['expected_version'], '^app_install_[0-9a-f]{32}$')
        self.assertEqual([x[0] for x in self.client.calls], ['apply_list', 'module_inventory'])
        self.assertTrue(result['data']['resource_effects']['network_download'])
        self.assertTrue(result['data']['resource_effects']['may_start_background_process'])
        self.assertFalse(result['data']['resource_effects']['file_tool_sandbox_applies'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_install_revalidates_catalogs_and_only_sends_current_fixed_payload(self):
        version = self.plan()['data']['expected_version']
        self.client.calls.clear()
        result = self.install(version)
        self.assertTrue(result['available'], result)
        self.assertTrue(result['data']['accepted'])
        self.assertFalse(result['data']['effect_confirmed'])
        self.assertIsNone(result['data']['native_task_id'])
        self.assertIsNone(result['native']['code'])  # Audited success omits code.
        self.assertEqual([x[0] for x in self.client.calls], ['apply_list', 'module_inventory', 'application_install'])
        operation, parameters, body, confirmed = self.client.calls[-1]
        self.assertTrue(confirmed)
        self.assertEqual(parameters, {})
        self.assertEqual(body, {'client_id': 'r1-toolbox', 'flag': 0, 'apply_id': 34,
            'module_name': 'netdisk_bd2', 'module_type': 0, 'source': 0,
            'version': 'V1.0.14', 'size': 1234, 'vid': '', 'guest': 0})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_all_nonallowlisted_core_or_other_modules_are_refused_without_query(self):
        for identifier in (2, 3, 23, 35, 38, 42, 43, 45):
            result = self.call('plan_native_application_install', {'application_id': identifier})
            self.assertTrue(result['available'])
            self.assertFalse(result['data']['allowed'])
            self.assertEqual(result['data']['reason'], 'application_not_allowlisted')
            self.assertIsNone(result['data']['expected_version'])
        self.assertEqual(self.client.calls, [])

    def test_exact_name_source_type_and_native_extension_identity_are_required(self):
        for app_changes, module_changes in (({'module_name': 'docker'}, {}), ({'source': 1}, {}),
            ({'module_type': 1}, {}), ({'source': False}, {}), ({}, {'module_name': 'docker'}),
            ({}, {'type': 'shadow'}), ({}, {'id': 38})):
            self.client.frames = catalogs()
            self.client.frames['apply_list']['list'][0].update(app_changes)
            self.client.frames['module_inventory']['available_list'][0].update(module_changes)
            result = self.plan()
            self.assertFalse(result.get('data', {}).get('allowed', False))

    def test_installed_or_nonzero_current_version_is_not_a_new_install(self):
        for app_changes, module_changes, installed in (({'status': 4}, {}, False),
            ({}, {'curr_ver': 'V1.0.13'}, False), ({'apply_opt': 1}, {}, False),
            ({}, {'status': 7}, False), ({}, {}, True)):
            self.client.frames = catalogs()
            self.client.frames['apply_list']['list'][0].update(app_changes)
            self.client.frames['module_inventory']['available_list'][0].update(module_changes)
            if installed:
                self.client.frames['module_inventory']['installed_list'] = [copy.deepcopy(
                    self.client.frames['module_inventory']['available_list'][0])]
            result = self.plan()
            self.assertFalse(result['data']['allowed'])
            self.assertEqual(result['data']['reason'], 'application_not_uninstalled')

    def test_duplicate_identity_and_same_name_alias_never_get_write_eligibility(self):
        for source in ('apply_list', 'module_inventory'):
            self.client.frames = catalogs()
            key = 'list' if source == 'apply_list' else 'available_list'
            row = copy.deepcopy(self.client.frames[source][key][0])
            row['module_name'] = '\x00PRIVATE'
            self.client.frames[source][key].append(row)
            self.assertFalse(self.plan()['available'])
        self.client.frames = catalogs()
        alias = copy.deepcopy(self.client.frames['apply_list']['list'][0])
        alias['apply_id'] = 99
        self.client.frames['apply_list']['list'].append(alias)
        self.assertFalse(self.plan()['data']['allowed'])

    def test_versions_size_vid_and_guest_cannot_be_fabricated_from_missing_values(self):
        for field, value in (('version', 'V1.0.13'), ('size', True), ('size', 2 ** 31),
                             ('vid', 'https://PRIVATE'), ('guest', 1), ('guest', True)):
            self.client.frames = catalogs()
            self.client.frames['apply_list']['list'][0][field] = value
            result = self.plan()
            self.assertFalse(result['data']['allowed'])
        self.client.frames = catalogs()
        self.client.frames['module_inventory']['available_list'][0]['size'] += 1
        self.assertFalse(self.plan()['data']['allowed'])

    def test_expected_version_binds_catalog_private_path_and_credential_revision(self):
        version = self.plan()['data']['expected_version']
        self.client.frames['module_inventory']['available_list'][0]['inst_path'] = '/OTHER-PRIVATE'
        result = self.install(version)
        self.assertEqual(result['reason'], 'installation_plan_changed')
        self.assertEqual(result['mutation_outcome'], 'not_sent')
        self.assertNotIn('application_install', [x[0] for x in self.client.calls])
        self.client.frames = catalogs()
        for changes in ({'key_id': 'another'}, {'grant_revision': 2}, {'credential_generation': 2},
                        {'_service_generation': 2}):
            result = self.call('install_native_application', {'application_id': 34,
                'expected_version': version, 'confirmed': True, 'idempotency_key': 'changed:1'},
                principal={**PRINCIPAL, **changes})
            self.assertEqual(result['reason'], 'installation_plan_changed')

    def test_success_and_unknown_writes_are_not_repeated_by_same_key(self):
        version = self.plan()['data']['expected_version']
        self.client.reply = transport.BusinessTransportError('timeout', request_sent=True)
        first = self.install(version)
        self.assertEqual(first['reason'], 'result_unconfirmed')
        self.assertEqual(first['mutation_outcome'], 'unknown')
        self.client.reply = {'apply_id': 34, 'module_name': 'netdisk_bd2'}
        second = self.install(version)
        self.assertTrue(second['idempotent_replay'])
        self.assertEqual(second['mutation_outcome'], 'unknown')
        self.assertEqual(len([x for x in self.client.calls if x[0] == 'application_install']), 1)
        conflict = self.install('app_install_' + '0' * 32)
        self.assertEqual(conflict['reason'], 'idempotency_conflict')

    def test_preflight_failure_releases_unsent_key_and_unhandled_write_stays_unknown(self):
        version = self.plan()['data']['expected_version']
        saved = copy.deepcopy(self.client.frames)
        self.client.frames['apply_list'] = RuntimeError('PRIVATE')
        result = self.install(version)
        self.assertEqual(result['reason'], 'transport_failed')
        self.assertEqual(result['mutation_outcome'], 'not_sent')
        self.client.frames = saved
        self.client.reply = RuntimeError('PRIVATE')
        result = self.install(version)
        self.assertEqual(result['reason'], 'result_unconfirmed')
        self.assertEqual(result['mutation_outcome'], 'unknown')
        self.client.reply = {'apply_id': 34, 'module_name': 'netdisk_bd2'}
        replay = self.install(version)
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(len([x for x in self.client.calls if x[0] == 'application_install']), 1)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_ack_requires_both_exact_identity_fields_and_never_echoes_message(self):
        version = self.plan()['data']['expected_version']
        for index, reply in enumerate(({}, {'code': 0}, {'apply_id': True, 'module_name': 'netdisk_bd2'},
            {'apply_id': 34, 'module_name': 'docker'}, {'code': '0', 'apply_id': 34,
                'module_name': 'netdisk_bd2', 'msg': 'PRIVATE'})):
            self.client.reply = reply
            result = self.install(version, idempotency_key='ack:' + str(index))
            self.assertEqual(result['mutation_outcome'], 'unknown')
            self.assertNotIn('PRIVATE', json.dumps(result))
        self.client.reply = {'code': -8001, 'curr_id': 38, 'msg': 'PRIVATE'}
        result = self.install(version, idempotency_key='reject:1')
        self.assertEqual(result['mutation_outcome'], 'rejected')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_revocation_before_send_and_after_ack_discards_results(self):
        version = self.plan()['data']['expected_version']
        self.client.after = lambda op: setattr(self, 'active', False) if op == 'module_inventory' else None
        result = self.install(version)
        self.assertEqual(result['mutation_outcome'], 'not_sent')
        self.assertIsNone(result['data'])
        self.assertNotIn('application_install', [x[0] for x in self.client.calls])
        self.active = True
        self.client.after = lambda op: setattr(self, 'active', False) if op == 'application_install' else None
        result = self.install(version)
        self.assertEqual(result['mutation_outcome'], 'unknown')
        self.assertIsNone(result['data'])
        self.active = True
        self.client.after = None
        result = self.install(version)
        self.assertTrue(result['idempotent_replay'])
        self.assertEqual(result['mutation_outcome'], 'unknown')

    def test_confirmation_idempotency_and_all_extra_native_arguments_fail_closed(self):
        for changes in ({'confirmed': False}, {'idempotency_key': ''}, {'url': 'https://PRIVATE'},
                        {'flag': 1}, {'path': '/PRIVATE'}, {'expected_version': 'bad'}):
            result = self.install('app_install_' + '0' * 32, **changes)
            self.assertFalse(result['available'])
        self.assertEqual(self.client.calls, [])
        valid = {'client_id': 'r1-toolbox', 'flag': 0, 'size': 1234, 'apply_id': 34,
                 'module_name': 'netdisk_bd2', 'vid': '', 'module_type': 0,
                 'source': 0, 'version': 'V1.0.14', 'guest': 0}
        for changes in ({'flag': 1}, {'apply_id': 38}, {'module_name': 'docker'},
                        {'source': 1}, {'url': 'https://PRIVATE'}, {'paramList': 'PRIVATE'}):
            with self.assertRaises(transport.BusinessTransportError):
                transport._validate('application_install', {}, {**valid, **changes})


class NativeInstallHttpTests(unittest.TestCase):
    def test_real_http_keeps_fixed_routes_get_planning_and_exact_post_body(self):
        frames, events, connects = catalogs(), [], []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def respond(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0')))) if self.command == 'POST' else {}
                action = query['action'][0]
                events.append((self.command, parsed.path, action, body))
                result = (frames['apply_list'] if action == 'get_apply_list' else frames['module_inventory']
                          if action == 'get_modulize_table' else {'apply_id': 34, 'module_name': 'netdisk_bd2'})
                data = json.dumps(result).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            do_GET = do_POST = respond
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        class Connection:
            def __init__(self, *args): self.raw = socket.socket(*args)
            def settimeout(self, seconds): self.raw.settimeout(seconds)
            def connect(self, address): connects.append(address); self.raw.connect(server.server_address)
            def getpeercert(self, **_kwargs): return b'FIXTURE-CERT'
            def sendall(self, data): self.raw.sendall(data)
            def recv(self, count): return self.raw.recv(count)
            def close(self): self.raw.close()
        identity = {'available': True, 'status': 'verified', 'pid': 10,
                    'start_ticks': 20, 'socket_inode': '30', 'connect_host': '127.0.0.1'}
        try:
            client = transport.VendorBusinessClient(
                session_reader=lambda **_kwargs: (SimpleNamespace(user_id=100000,
                    derive_access_token=lambda: 'PRIVATE-TOKEN'), {'available': True, 'status': 'ready'}),
                listener_check=lambda **_kwargs: identity,
                certificate_reader=lambda _root: b'FIXTURE-CERT', socket_factory=Connection,
                tls_context=SimpleNamespace(wrap_socket=lambda raw, **_kwargs: raw))
            backend = native.NativeCapabilities(client=client, idempotency_store=native.IdempotencyStore())
            plan = backend.execute('plan_native_application_install', {'application_id': 34},
                                   principal=PRINCIPAL, authorizer=lambda *_: True)
            self.assertTrue(plan['data']['allowed'], plan)
            result = backend.execute('install_native_application', {'application_id': 34,
                'expected_version': plan['data']['expected_version'], 'confirmed': True,
                'idempotency_key': 'http:1'}, principal=PRINCIPAL, authorizer=lambda *_: True)
            self.assertTrue(result['data']['accepted'], result)
            self.assertEqual([x[:3] for x in events], [
                ('GET', '/rest/1.1/applycenter', 'get_apply_list'),
                ('GET', '/rest/1.1/update', 'get_modulize_table'),
                ('GET', '/rest/1.1/applycenter', 'get_apply_list'),
                ('GET', '/rest/1.1/update', 'get_modulize_table'),
                ('POST', '/rest/1.1/applycenter', 'install_apply')])
            self.assertTrue(all(x == ('127.0.0.1', 20443) for x in connects))
            self.assertEqual(events[-1][3]['flag'], 0)
            self.assertNotIn('PRIVATE', json.dumps(result))
        finally:
            server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__': unittest.main()

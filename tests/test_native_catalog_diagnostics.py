"""One-shot native schema diagnostics retain shapes without private values."""

import importlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
native = importlib.import_module('vendor_native_capabilities')


class Client:
    def __init__(self, frame, after=None):
        self.frame, self.after, self.calls = frame, after, []

    def request(self, operation, parameters, body, authorizer, *, confirmed=False):
        self.calls.append((operation, parameters, body, confirmed))
        if self.after:
            self.after()
        return 200, self.frame


class CatalogDiagnosticTests(unittest.TestCase):
    def collect(self, frame):
        client = Client(frame)
        result = native.collect_application_package_context(client=client, authorizer=lambda *_: True)
        self.assertEqual(client.calls, [('apply_list', {}, {}, False)])
        self.assertFalse(result['package_execution_available'])
        return result

    def test_invalid_catalog_node_does_not_erase_all_package_evidence(self):
        frame = {'list': [{'apply_id': 0, 'module_name': 'core', 'source': 2},
                          {'apply_id': 5, 'module_name': 'Application', 'version': '1.0', 'source': 1},
                          {'apply_id': 'PRIVATE-ID', 'module_name': '\x00PRIVATE-LABEL',
                           'path': '/PRIVATE-PATH', 'access_token': 'PRIVATE-TOKEN'}]}
        result = self.collect(frame)
        self.assertTrue(result['available'])
        self.assertEqual(result['status'], 'diagnostic_only')
        self.assertFalse(result['catalog_projection_valid'])
        self.assertEqual(len(result['items']), 3)
        self.assertIsNone(result['items'][0]['application_id'])
        self.assertEqual(result['items'][1]['application_id'], 5)
        self.assertIsNone(result['items'][2]['name'])
        issues = result['projection_issues']
        self.assertIn({'location': 'list', 'row_index': 0, 'field': 'apply_id',
                       'reason': 'invalid_object_id'}, issues)
        self.assertIn({'location': 'list', 'row_index': 2, 'field': 'module_name',
                       'reason': 'invalid_label'}, issues)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('access_token', json.dumps(result))

    def test_nested_or_nonlist_response_retains_whitelisted_complete_structure(self):
        result = self.collect({'data': {'list': [{'apply_id': 5, 'module_name': 'PRIVATE'}]},
                               'PRIVATE-KEY': {'access_token': 'PRIVATE-TOKEN'}})
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'catalog_projection_rejected')
        structure = result['response_structure']
        self.assertTrue(structure['complete'])
        root = structure['structure']
        self.assertEqual(root['omitted_field_count'], 1)
        self.assertEqual(root['fields']['data']['fields']['list']['count'], 1)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_wrong_native_code_still_reports_its_type_without_normalization(self):
        result = self.collect({'code': None, 'msg': 'PRIVATE-TEXT', 'list': []})
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'invalid_json')
        self.assertEqual(result['response_structure']['structure']['fields']['code'], {'type': 'null'})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_default_tool_output_never_includes_developer_structure(self):
        backend = native.NativeCapabilities(client=Client({'list': [{'apply_id': 0, 'module_name': 'core'}]}))
        result = backend.execute('list_native_applications', {}, authorizer=lambda *_: True)
        self.assertEqual(result['reason'], 'invalid_json')
        self.assertNotIn('response_structure', result)
        self.assertNotIn('projection_issues', result)

    def test_application_catalog_keeps_unique_valid_nodes_and_fixed_rejection_counts(self):
        frame = {'list': [{'apply_id': 5, 'module_name': 'Application', 'version': '1.0'},
                          {'apply_id': 0, 'module_name': 'core'},
                          {'module_name': 'without ID'},
                          {'apply_id': 6, 'module_name': '\x00PRIVATE-LABEL'},
                          None, {'apply_id': True, 'module_name': 'boolean ID'}]}
        result = native.NativeCapabilities(client=Client(frame)).execute(
            'list_native_applications', {}, authorizer=lambda *_: True)
        self.assertTrue(result['available'])
        data = result['data']
        self.assertTrue(data['partial'])
        self.assertEqual(data['accepted_entries'], 1)
        self.assertEqual(data['rejected_entries'], 5)
        self.assertEqual([item['application_id'] for item in data['items']], [5])
        self.assertEqual({item['code']: item['count'] for item in data['rejections']}, {
            'invalid_application_id': 3, 'invalid_application_label': 1,
            'unsupported_application_entry': 1})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_duplicate_id_rejects_every_node_even_when_other_name_is_invalid(self):
        for duplicates in ([{'apply_id': 7, 'module_name': 'Plausible'},
                            {'apply_id': 7, 'module_name': '\x00PRIVATE'}],
                           [{'apply_id': 7, 'module_name': '\x00PRIVATE'},
                            {'apply_id': 7, 'module_name': 'Plausible'}]):
            frame = {'list': [{'apply_id': 5, 'module_name': 'Application'}, *duplicates]}
            result = native.NativeCapabilities(client=Client(frame)).execute(
                'list_native_applications', {}, authorizer=lambda *_: True)
            self.assertTrue(result['available'])
            self.assertEqual([item['application_id'] for item in result['data']['items']], [5])
            self.assertEqual(result['data']['rejections'], [
                {'code': 'duplicate_application_id', 'count': 2}])
            packages = self.collect(frame)
            self.assertEqual(len(packages['items']), 3)
            self.assertFalse(packages['catalog_projection_valid'])
            self.assertFalse(packages['package_execution_available'])

    def test_all_rejected_catalog_remains_unavailable_but_true_empty_is_observable(self):
        for rows in ([{'apply_id': 0, 'module_name': 'core'}],
                     [{'apply_id': 7, 'module_name': 'one'},
                      {'apply_id': 7, 'module_name': 'two'}]):
            result = native.NativeCapabilities(client=Client({'list': rows})).execute(
                'list_native_applications', {}, authorizer=lambda *_: True)
            self.assertFalse(result['available'])
            self.assertEqual(result['reason'], 'invalid_json')
            self.assertIsNone(result['data'])
        result = native.NativeCapabilities(client=Client({'list': []})).execute(
            'list_native_applications', {}, authorizer=lambda *_: True)
        self.assertTrue(result['available'])
        self.assertFalse(result['data']['partial'])
        self.assertEqual(result['data']['accepted_entries'], 0)

    def test_install_progress_refuses_duplicate_identity_hidden_behind_invalid_label(self):
        frame = {'list': [{'apply_id': 5, 'module_name': 'Application'},
                          {'apply_id': 7, 'module_name': 'Plausible'},
                          {'apply_id': 7, 'module_name': '\x00PRIVATE'}]}
        client = Client(frame)
        result = native.NativeCapabilities(client=client, idempotency_store=native.IdempotencyStore()).execute(
            'get_application_install_progress', {'application_id': 7, 'confirmed': True,
                'idempotency_key': 'duplicate:1'},
            principal={'key_id': 'native-test', 'grant_revision': 1, 'credential_generation': 1},
            authorizer=lambda *_: True)
        self.assertEqual(result['reason'], 'object_not_in_owner_catalog')
        self.assertEqual(result['mutation_outcome'], 'not_sent')
        self.assertEqual(client.calls, [('apply_list', {}, {}, False)])

    def test_install_progress_uses_only_accepted_unique_record_in_partial_catalog(self):
        class SequenceClient(Client):
            def request(self, operation, parameters, body, authorizer, *, confirmed=False):
                self.calls.append((operation, parameters, body, confirmed))
                return (200, self.frame if operation == 'apply_list' else
                        {'code': 0, 'apply_id': 5, 'status': 3, 'size': 10, 'total_size': 20})
        client = SequenceClient({'list': [{'apply_id': 0, 'module_name': 'core'},
                                         {'apply_id': 5, 'module_name': 'Application'}]})
        result = native.NativeCapabilities(client=client, idempotency_store=native.IdempotencyStore()).execute(
            'get_application_install_progress', {'application_id': 5, 'confirmed': True,
                'idempotency_key': 'accepted:1'},
            principal={'key_id': 'native-test', 'grant_revision': 1, 'credential_generation': 1},
            authorizer=lambda *_: True)
        self.assertTrue(result['available'])
        self.assertEqual([call[0] for call in client.calls], ['apply_list', 'application_progress'])

    def test_diagnostic_backup_owner_failure_is_specific_without_paths_or_uid(self):
        frame = {'run_list': [{'task_id': 8, 'task_status': 0, 'user': 'PRIVATE-UID',
                               'from': '/PRIVATE-FROM', 'to': '/PRIVATE-TO'}],
                 'wait_list': [], 'done_list': []}
        backend = native.NativeCapabilities(client=Client(frame), diagnostic=True)
        result = backend.execute('list_backup_tasks', {}, authorizer=lambda *_: True)
        self.assertEqual(result['reason'], 'invalid_json')
        self.assertIn({'location': 'run_list', 'row_index': 0, 'field': 'user',
                       'reason': 'owner_identity_unverified'}, result['projection_issues'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertTrue(result['response_structure']['complete'])

    def test_firmware_backup_list_cm_groups_are_projected_without_weakening_owner(self):
        task = {'task_id': 8, 'task_status': 0, 'user': 100000,
                'from': '/PRIVATE-FROM', 'to': '/PRIVATE-TO', 'total_size': 1234}
        frame = {'run_list': {'list_cm': [task]}, 'wait_list': {'list_cm': []},
                 'done_list': {'list_cm': []}}
        backend = native.NativeCapabilities(client=Client(frame), diagnostic=True)
        result = backend.execute('list_backup_tasks', {}, authorizer=lambda *_: True)
        self.assertTrue(result['available'])
        self.assertEqual(result['data']['counts'], {'running': 1, 'waiting': 0, 'done': 0})
        self.assertEqual(result['projection_issues'], [])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertRegex(result['data']['groups']['running'][0]['version'], r'^backup_[0-9a-f]{32}$')
        task['user'] = 100001
        result = backend.execute('list_backup_tasks', {}, authorizer=lambda *_: True)
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'invalid_json')
        self.assertIn('owner_identity_unverified', json.dumps(result['projection_issues']))

    def test_backup_list_cm_does_not_accept_arbitrary_wrappers_or_null_array(self):
        for value in ({'data': []}, {'list_cm': None}, {'list_cm': 'PRIVATE'}):
            backend = native.NativeCapabilities(client=Client({'run_list': value}), diagnostic=True)
            result = backend.execute('list_backup_tasks', {}, authorizer=lambda *_: True)
            self.assertFalse(result['available'])
            self.assertEqual(result['reason'], 'invalid_json')

    def test_duplicate_ids_and_nonobject_nodes_have_exact_locations(self):
        result = self.collect({'list': [{'apply_id': 5, 'module_name': 'Application'},
                                       {'apply_id': 5, 'module_name': 'Application'}, None]})
        self.assertIn({'location': 'list', 'row_index': 1, 'field': 'apply_id',
                       'reason': 'duplicate_object_id'}, result['projection_issues'])
        self.assertIn({'location': 'list', 'row_index': 2,
                       'reason': 'expected_object'}, result['projection_issues'])

    def test_every_bounded_array_node_is_counted_without_values(self):
        structure = native._native_response_structure({'list': [
            {'apply_id': i, 'module_name': 'PRIVATE'} for i in range(256)]})
        array = structure['structure']['fields']['list']
        self.assertTrue(structure['complete'])
        self.assertEqual(array['count'], 256)
        self.assertEqual(array['inspected_count'], 256)
        self.assertEqual(sum(row['count'] for row in array['item_shapes']), 256)
        self.assertNotIn('PRIVATE', json.dumps(structure))
        too_large = native._native_response_structure({'list': [None] * 257})
        self.assertFalse(too_large['complete'])

    def test_nested_shape_bounds_are_reported_not_claimed_complete(self):
        value = {}
        for _ in range(10):
            value = {'data': value}
        result = native._native_response_structure(value)
        self.assertFalse(result['complete'])
        self.assertIn('structure_bounds', json.dumps(result))

    def test_revocation_before_result_discards_all_structure(self):
        active = [True]
        client = Client({'list': [{'apply_id': 5, 'module_name': 'PRIVATE'}]},
                        after=lambda: active.__setitem__(0, False))
        result = native.collect_application_package_context(client=client,
                                                            authorizer=lambda *_: active[0])
        self.assertEqual(result['reason'], 'authorization_denied')
        self.assertIsNone(result['response_structure'])
        self.assertIsNone(result['projection_issues'])
        self.assertIsNone(result['items'])


if __name__ == '__main__':
    unittest.main()

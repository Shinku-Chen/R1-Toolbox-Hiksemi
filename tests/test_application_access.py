"""Independent installation resources, migration and in-flight revocation."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from api_access import ApiAccessStore
from api_permissions import permission_allows
from native_capability_specs import APPLICATION_INSTALL_SPECS
from test_mcp_completion_http import CompletionHTTPBase
from api_file_executor import FileAccessError

PLAN, INSTALL = 'plan_native_application_install', 'install_native_application'


class ApplicationAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ApiAccessStore(temporary.name, clock=lambda: 1800000000)
        self.store.configure({'enabled': True})

    def create(self, **changes):
        return self.store.create_key({'name': 'installation resource', 'role': 'admin',
            'scopes': ['apps'], 'tool_grants': [PLAN, INSTALL], **changes})

    def principal(self, key):
        principal, reason = self.store.authorize('Bearer ' + key['token'], '127.0.0.1')
        self.assertEqual(reason, 'ok')
        return principal

    def test_new_keys_never_receive_implicit_install_resources(self):
        key = self.create()
        self.assertEqual(key['application_ids'], [])
        self.assertEqual(self.principal(key)['application_ids'], [])
        # A tool's existence grants neither an application nor execution.
        self.assertTrue(permission_allows(self.principal(key), 'apps', 'admin', tool_name=PLAN))

    def test_resource_edit_invalidates_inflight_and_metadata_preserves_resources(self):
        key = self.create(application_ids=[34])
        old = self.principal(key)
        renamed = self.store.update_key({'id': key['id'], 'name': 'renamed'})
        self.assertEqual(renamed['application_ids'], [34])
        self.assertTrue(self.store.is_current(old))
        changed = self.store.update_key({'id': key['id'], 'application_ids': [7, 35]})
        self.assertEqual(changed['grant_revision'], old['grant_revision'] + 1)
        self.assertFalse(self.store.is_current(old))
        projected = self.principal(key)
        projected['application_ids'].append(34)
        self.assertEqual(self.store.snapshot()['keys'][0]['application_ids'], [7, 35])
        self.store.update_key({'id': key['id'], 'application_ids': []})
        self.assertEqual(self.principal(key)['application_ids'], [])

    def test_invalid_resources_and_role_or_domain_changes_are_atomic(self):
        key = self.create(application_ids=[34])
        before = self.store.path.read_bytes()
        for value in (None, {}, [True], [34.0], ['34'], [34, 34], [2], [-1], list(APPLICATION_INSTALL_SPECS) + [999]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.update_key({'id': key['id'], 'application_ids': value})
            self.assertEqual(self.store.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.store.update_key({'id': key['id'], 'scopes': ['read'], 'tool_grants': ['get_nas_status']})
        changed = self.store.update_key({'id': key['id'], 'role': 'readonly',
            'scopes': ['apps'], 'tool_grants': ['get_app_status'], 'application_ids': []})
        self.assertEqual(changed['application_ids'], [])

    def test_v5_read_keeps_bytes_and_old_install_scope_remains_id34_only(self):
        key = self.create(application_ids=[34])
        state = json.loads(self.store.path.read_text(encoding='utf-8'))
        state['version'] = 5
        for row in state['keys']:
            del row['application_ids']
        self.store.path.write_text(json.dumps(state), encoding='utf-8')
        os.chmod(self.store.path, 0o600)
        before = self.store.path.read_bytes()
        self.assertEqual(self.principal(key)['application_ids'], [34])
        self.assertEqual(self.store.snapshot()['keys'][0]['application_ids'], [34])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.update_key({'id': key['id'], 'name': 'old installer'})
        migrated = json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertEqual(migrated['version'], 6)
        self.assertEqual(migrated['keys'][0]['application_ids'], [34])

    def test_v5_noninstaller_does_not_acquire_id34_on_later_tool_edit(self):
        key = self.create(tool_grants=['get_app_status'])
        state = json.loads(self.store.path.read_text(encoding='utf-8'))
        state['version'] = 5
        for row in state['keys']:
            del row['application_ids']
        self.store.path.write_text(json.dumps(state), encoding='utf-8')
        os.chmod(self.store.path, 0o600)
        changed = self.store.update_key({'id': key['id'], 'tool_grants': [PLAN, INSTALL]})
        self.assertEqual(changed['application_ids'], [])

    def test_rotation_preserves_exact_application_resources_and_invalidates_old_principal(self):
        key = self.create(application_ids=[7, 35])
        principal = self.principal(key)
        rotated = self.store.rotate_key(key['id'])
        self.assertEqual(rotated['application_ids'], [7, 35])
        self.assertEqual(rotated['grant_revision'], key['grant_revision'])
        self.assertEqual(rotated['credential_generation'], key['credential_generation'] + 1)
        self.assertFalse(self.store.is_current(principal))
        self.assertEqual(self.store.authorize('Bearer ' + key['token'], '127.0.0.1')[1], 'invalid_key')
        self.assertEqual(self.principal(rotated)['application_ids'], [7, 35])

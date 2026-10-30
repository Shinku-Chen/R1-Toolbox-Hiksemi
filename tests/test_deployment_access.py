"""Storage-v5 migration and independent deployment-resource authorization."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from api_access import ApiAccessStore
from api_permissions import permission_allows


DEPLOYMENT = 'deployment_' + '1' * 24
READ = ['list_docker_deployments', 'get_docker_deployment_task']
WRITE = ['plan_docker_deployment', 'execute_docker_deployment']


class DeploymentAccessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = ApiAccessStore(temp.name, clock=lambda: 1800000000)
        self.store.configure({'enabled': True})

    def body(self, **updates):
        return {'name': '部署专用', 'role': 'readonly', 'scopes': ['docker'],
                'file_roots': [], 'docker_projects': [], 'tool_grants': READ,
                'docker_deployments': [{'id': DEPLOYMENT, 'operations': ['inspect']}],
                'allowed_ips': ['192.168.31.22'], **updates}

    def principal(self, key):
        principal, reason = self.store.authorize('Bearer ' + key['token'], '192.168.31.22')
        self.assertEqual(reason, 'ok')
        return principal

    def test_deployment_only_key_can_read_without_inheriting_existing_projects(self):
        key = self.store.create_key(self.body())
        self.assertEqual(key['docker_projects'], [])
        self.assertEqual(json.loads(self.store.path.read_text(encoding='utf8'))['version'], 5)
        principal = self.principal(key)
        self.assertTrue(permission_allows(principal, 'docker', 'readonly', tool_name=READ[0]))
        self.assertFalse(permission_allows(principal, 'docker', 'readonly', tool_name='list_docker_projects'))
        self.assertFalse(permission_allows(principal, 'docker', 'admin', tool_name=WRITE[0]))
        principal['docker_deployments'][0]['operations'].append('deploy')
        self.assertEqual(self.store.snapshot()['keys'][0]['docker_deployments'][0]['operations'], ['inspect'])

    def test_admin_still_requires_scope_tool_resource_and_plan_dependency(self):
        body = self.body(role='admin', tool_grants=READ + WRITE,
            docker_deployments=[{'id': DEPLOYMENT, 'operations': ['inspect', 'deploy']}])
        key = self.store.create_key(body)
        self.assertTrue(permission_allows(self.principal(key), 'docker', 'admin', tool_name=WRITE[1]))
        for changes in ({'role': 'readwrite'}, {'scopes': ['read']}, {'docker_deployments': []},
                        {'tool_grants': [WRITE[1]]}, {'docker_deployments': None},
                        {'docker_deployments': [{'id': DEPLOYMENT, 'operations': ['inspect', 'shell']}]},
                        {'docker_deployments': [{'id': 'project_' + '1' * 24, 'operations': ['inspect', 'deploy']}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.create_key({**body, **changes})

    def test_resource_edit_revokes_inflight_principal_and_omission_preserves_grant(self):
        key = self.store.create_key(self.body(role='admin', tool_grants=READ + WRITE,
            docker_deployments=[{'id': DEPLOYMENT, 'operations': ['inspect', 'deploy']}]))
        old = self.principal(key)
        changed = self.store.update_key({'id': key['id'], 'name': '只改名称'})
        self.assertEqual(changed['docker_deployments'], key['docker_deployments'])
        self.assertTrue(self.store.is_current(old))
        changed = self.store.update_key({'id': key['id'], 'role': 'readonly', 'tool_grants': READ,
            'docker_deployments': [{'id': DEPLOYMENT, 'operations': ['inspect']}]})
        self.assertEqual(changed['grant_revision'], old['grant_revision'] + 1)
        self.assertFalse(self.store.is_current(old))
        self.assertEqual(self.principal(key)['docker_deployments'][0]['operations'], ['inspect'])
        with self.assertRaises(ValueError):
            self.store.update_key({'id': key['id'], 'docker_deployments': None})

    def test_v4_reads_retain_exact_bytes_then_write_v5_without_expanding_old_key(self):
        key = self.store.create_key({'name': '旧工具', 'role': 'readonly', 'scopes': ['read'],
                                    'file_roots': [], 'tool_grants': ['get_nas_status']})
        data = json.loads(self.store.path.read_text(encoding='utf8'))
        data['version'] = 4
        for row in data['keys']:
            del row['docker_deployments']
        before = json.dumps(data, ensure_ascii=False).encode('utf8')
        self.store.path.write_bytes(before)
        self.assertTrue(self.store.snapshot()['storage_ok'])
        principal, reason = self.store.authorize('Bearer ' + key['token'], '192.168.31.22')
        self.assertEqual(reason, 'ok')
        self.assertEqual(principal['docker_deployments'], [])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.update_key({'id': key['id'], 'name': '重命名旧工具'})
        saved = json.loads(self.store.path.read_text(encoding='utf8'))
        self.assertEqual(saved['version'], 5)
        self.assertEqual(saved['keys'][0]['digest'], data['keys'][0]['digest'])
        self.assertEqual(saved['keys'][0]['tool_grants'], ['get_nas_status'])
        self.assertEqual(saved['keys'][0]['docker_deployments'], [])
        self.assertTrue(self.store.is_current(principal))

    def test_malformed_v5_resource_does_not_rewrite_or_reauthorize(self):
        key = self.store.create_key(self.body())
        data = json.loads(self.store.path.read_text(encoding='utf8'))
        for value in (None, {}, [{'id': DEPLOYMENT, 'operations': ['inspect', 'deploy']}],
                      [{'id': DEPLOYMENT, 'operations': ['inspect']}] * 2):
            with self.subTest(value=value):
                broken = copy.deepcopy(data)
                broken['keys'][0]['docker_deployments'] = value
                raw = json.dumps(broken).encode()
                self.store.path.write_bytes(raw)
                self.assertFalse(self.store.snapshot()['storage_ok'])
                self.assertEqual(self.store.authorize('Bearer ' + key['token'], '192.168.31.22')[1], 'storage_unavailable')
                with self.assertRaises(OSError):
                    self.store.update_key({'id': key['id'], 'name': '不能掩盖坏状态'})
                self.assertEqual(self.store.path.read_bytes(), raw)
        self.store.path.write_text(json.dumps(data), encoding='utf8')


if __name__ == '__main__':
    unittest.main()

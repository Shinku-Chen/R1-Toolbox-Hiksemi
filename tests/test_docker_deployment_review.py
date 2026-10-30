"""Independent deployment safety review using isolated local Engine HTTP."""

import copy
import unittest

import test_api_docker_deployment as fixture


class DeploymentHealthReviewTests(unittest.TestCase):
    setUp = fixture.DeploymentTests.setUp
    tearDown = fixture.DeploymentTests.tearDown
    plan = fixture.DeploymentTests.plan
    execute = fixture.DeploymentTests.execute
    unix = False

    def test_replacement_stopped_during_health_wait_does_not_delete_old_container(self):
        self.assertEqual(self.execute()['task']['state'], 'completed')
        original_id = next(iter(self.engine.containers))
        observations = []

        def before(method, path, _body):
            if method == 'GET' and path.startswith('/containers/') and path.endswith('/json'):
                identity = path.split('/')[2]
                row = self.engine.containers.get(identity)
                if row is not None and identity != original_id and row['State']['Running']:
                    row['State']['Health'] = {'Status': 'starting'}
                    observations.append(identity)
                    if len(observations) >= 2:
                        row['State'].update(Running=False, Health={'Status': 'healthy'})
            return None

        self.engine.before = before
        result = self.execute(self.plan('update'), key='review:health-stopped')
        self.assertGreaterEqual(len(observations), 2)
        self.assertEqual(result['task']['state'], 'result_unconfirmed', result)
        self.assertIn(original_id, self.engine.containers)
        self.assertFalse(any(method == 'DELETE' and path.startswith('/containers/' + original_id)
                             for method, path, _body in self.engine.requests))

    def test_real_compose_named_toolbox_data_volume_cannot_be_delegated(self):
        name = 'r1project_r1-toolbox-data'
        volume = copy.deepcopy(self.engine.volumes['test-data'])
        volume.update(Name=name, Mountpoint='/var/lib/docker/volumes/' + name + '/_data',
                      Labels={'com.docker.compose.project': 'r1project',
                              'com.docker.compose.volume': 'r1-toolbox-data'})
        self.engine.volumes[name] = volume
        catalog = self.registry.catalog()
        self.assertTrue(catalog['ok'], catalog)
        candidate = fixture.spec()
        candidate['name'] = 'review-other-service'
        candidate['volumes'][0]['name'] = name
        result = self.registry.register({'label': 'Must not delegate Toolbox state', 'spec': candidate})
        self.assertFalse(result['ok'], result)
        self.assertNotIn({'name': name}, catalog['volumes'])


if __name__ == '__main__':
    unittest.main()

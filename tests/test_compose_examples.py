"""Verify reproducible standalone examples without Docker or a NAS."""
import copy
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ('composer.yml', 'tools/boot-once/compose.yaml', 'composer.dev.yml')
DEVELOPER_ENV = {
    'R1_DEV_NO_PAIRING': '1',
    'R1_DEV_ALLOW_IPS': '${R1_DEV_ALLOW_IPS:?请设置开发者白名单IP或CIDR}',
    'R1_DEV_MINUTES': '1440',
    'R1_DEV_ALLOW_COMMANDS': '1',
}


class ComposeExampleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='r1-compose-examples-')
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.inputs = self.repo / 'tools/boot-once'
        self.inputs.mkdir(parents=True)
        for name in ('build-compose.py', 'screen-actions.sh', 'host-job.sh.in', 'runner.sh.in'):
            shutil.copyfile(ROOT / 'tools/boot-once' / name, self.inputs / name)
        self.generate()

    def generate(self, environment=None):
        result = subprocess.run([sys.executable, str(self.inputs / 'build-compose.py')],
            cwd=self.repo, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def read(self, name):
        return yaml.safe_load((self.repo / name).read_text(encoding='utf-8'))

    def test_three_shipped_templates_match_generated_output_and_are_reproducible(self):
        before = {name: (self.repo / name).read_bytes() for name in OUTPUTS}
        for name in OUTPUTS:
            with self.subTest(path=name):
                self.assertEqual(before[name], (ROOT / name).read_bytes())
        self.generate()
        self.assertEqual({name: (self.repo / name).read_bytes() for name in OUTPUTS}, before)
        self.assertEqual(before['composer.yml'], before['tools/boot-once/compose.yaml'])

    def test_developer_variant_differs_only_by_explicit_developer_environment(self):
        demo, developer = self.read('composer.yml'), self.read('composer.dev.yml')
        normalized = copy.deepcopy(developer)
        environment = normalized['services']['r1-toolbox']['environment']
        for name, value in DEVELOPER_ENV.items():
            self.assertEqual(environment.pop(name), value)
        self.assertEqual(normalized, demo)
        self.assertEqual(developer['volumes'], {'r1-toolbox-data': {}})
        service = developer['services']['r1-toolbox']
        self.assertEqual(service['container_name'], 'r1-toolbox')
        self.assertEqual(service['stop_grace_period'], '3m')
        self.assertEqual(service['restart'], 'always')
        self.assertEqual(service['image'], '${R1_TOOLBOX_IMAGE:-ghcr.io/shinku-chen/r1-toolbox:preview}')
        self.assertIn('r1-toolbox-data:/data', service['volumes'])
        self.assertNotIn('name', developer, '模板不能静默切换现有项目名')
        self.assertNotIn('include', developer)
        self.assertNotIn('extends', service)

    def test_generic_demo_has_no_developer_authorization(self):
        for name in ('composer.yml', 'tools/boot-once/compose.yaml'):
            with self.subTest(path=name):
                contents = (self.repo / name).read_text(encoding='utf-8')
                self.assertIn('通用 demo', contents)
                environment = self.read(name)['services']['r1-toolbox']['environment']
                self.assertEqual(environment, {'UI_MODE': 'image', 'R1_WEB_AUTH_MODE': 'password'})
                self.assertFalse(set(DEVELOPER_ENV) & set(environment))

    def test_developer_whitelist_is_required_without_personal_ip_or_open_fallback(self):
        contents = (self.repo / 'composer.dev.yml').read_text(encoding='utf-8')
        value = self.read('composer.dev.yml')['services']['r1-toolbox']['environment']['R1_DEV_ALLOW_IPS']
        self.assertEqual(value, DEVELOPER_ENV['R1_DEV_ALLOW_IPS'])
        self.assertTrue(value.startswith('${R1_DEV_ALLOW_IPS:?'))
        self.assertNotIn(':-', value)
        self.assertNotIn('0.0.0.0/0', contents)
        self.assertNotIn('::/0', contents)
        self.assertEqual(re.findall(r'(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])', contents), [])
        self.assertIn('网页已保存的设置优先', contents)
        # Building examples must not freeze the developer's local environment.
        before = (self.repo / 'composer.dev.yml').read_bytes()
        self.generate(dict(os.environ, R1_DEV_ALLOW_IPS='TEST-VALUE-MUST-NOT-BE-EMBEDDED'))
        self.assertEqual((self.repo / 'composer.dev.yml').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

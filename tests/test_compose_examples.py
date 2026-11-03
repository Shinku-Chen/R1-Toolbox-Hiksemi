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
PUBLIC_ENV = {
    'UI_MODE', 'UI_DIR', 'TASK_CONTAINER', 'R1_WEB_BIND', 'R1_WEB_PORT',
    'R1_WEB_AUTH_MODE', 'R1_SMALL_SCREEN_ENABLED', 'R1_DEV_NO_PAIRING',
    'R1_DEV_ALLOW_IPS', 'R1_DEV_MINUTES', 'R1_DEV_ALLOW_COMMANDS',
}
PLACEHOLDER = re.compile(r'^( {6})# ([A-Z][A-Z0-9_]*: .+)$', re.MULTILINE)


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys that a normal safe_load would overwrite."""
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ValueError('Duplicate YAML key: ' + str(key))
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


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
        return yaml.load((self.repo / name).read_text(encoding='utf-8'), Loader=UniqueKeyLoader)

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
        self.assertEqual(demo['services']['r1-toolbox']['image'],
                         '${R1_TOOLBOX_IMAGE:-ghcr.io/shinku-chen/r1-toolbox:latest}')
        # The developer template intentionally defaults to preview.
        normalized['services']['r1-toolbox']['image'] = demo['services']['r1-toolbox']['image']
        self.assertEqual(normalized, demo)
        self.assertEqual(developer['volumes'], {'r1_toolbox_data': {}})
        service = developer['services']['r1-toolbox']
        self.assertEqual(service['container_name'], 'r1-toolbox')
        self.assertEqual(service['stop_grace_period'], '3m')
        self.assertEqual(service['restart'], 'always')
        self.assertIs(service['privileged'], True)
        for name in ('pid', 'ipc', 'network_mode'):
            self.assertEqual(service[name], 'host')
        self.assertEqual(service['image'], '${R1_TOOLBOX_IMAGE:-ghcr.io/shinku-chen/r1-toolbox:preview}')
        self.assertEqual(service['volumes'], [
            '/:/host', '/dev:/host/dev', '/proc:/host/proc', '/sys:/host/sys',
            '/run:/host/run', 'r1_toolbox_data:/data',
        ])
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
                self.assertEqual(self.read(name)['services']['r1-toolbox']['image'],
                                 '${R1_TOOLBOX_IMAGE:-ghcr.io/shinku-chen/r1-toolbox:latest}')

    def test_developer_whitelist_is_required_without_personal_ip_or_open_fallback(self):
        contents = (self.repo / 'composer.dev.yml').read_text(encoding='utf-8')
        value = self.read('composer.dev.yml')['services']['r1-toolbox']['environment']['R1_DEV_ALLOW_IPS']
        self.assertEqual(value, DEVELOPER_ENV['R1_DEV_ALLOW_IPS'])
        self.assertTrue(value.startswith('${R1_DEV_ALLOW_IPS:?'))
        self.assertNotIn(':-', value)
        self.assertNotIn('0.0.0.0/0', contents)
        self.assertNotIn('::/0', contents)
        addresses = set(re.findall(r'(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])', contents))
        self.assertLessEqual(addresses, {'0.0.0.0', '127.0.0.1'})
        self.assertIn('网页已保存的设置优先', contents)
        # Building examples must not freeze the developer's local environment.
        before = (self.repo / 'composer.dev.yml').read_bytes()
        self.generate(dict(os.environ, R1_DEV_ALLOW_IPS='TEST-VALUE-MUST-NOT-BE-EMBEDDED'))
        self.assertEqual((self.repo / 'composer.dev.yml').read_bytes(), before)

    def test_every_public_environment_option_has_one_active_or_commented_entry(self):
        for name in OUTPUTS:
            with self.subTest(path=name):
                contents = (self.repo / name).read_text(encoding='utf-8')
                active = self.read(name)['services']['r1-toolbox']['environment']
                placeholders = [entry.split(':', 1)[0] for _, entry in PLACEHOLDER.findall(contents)]
                self.assertEqual(len(placeholders), len(set(placeholders)), '注释占位不得重复')
                self.assertFalse(set(active) & set(placeholders), '已有生效项不得另设同名注释占位')
                self.assertEqual(set(active) | set(placeholders), PUBLIC_ENV)
                self.assertEqual(len(placeholders), 5 if name == 'composer.dev.yml' else 9)

    def test_uncommenting_every_optional_entry_produces_valid_yaml_without_duplicate_keys(self):
        for name in OUTPUTS:
            with self.subTest(path=name):
                contents = (self.repo / name).read_text(encoding='utf-8')
                expanded = PLACEHOLDER.sub(r'\1\2', contents)
                configuration = yaml.load(expanded, Loader=UniqueKeyLoader)
                environment = configuration['services']['r1-toolbox']['environment']
                self.assertEqual(set(environment), PUBLIC_ENV)
                self.assertEqual(environment['R1_WEB_BIND'], '0.0.0.0')
                self.assertEqual(environment['R1_WEB_PORT'], '18732')
                self.assertEqual(environment['R1_SMALL_SCREEN_ENABLED'], '1')
                self.assertEqual(environment['UI_MODE'], 'image')
                self.assertEqual(environment['R1_WEB_AUTH_MODE'], 'password')
                if name == 'composer.dev.yml':
                    for key, value in DEVELOPER_ENV.items():
                        self.assertEqual(environment[key], value)
                else:
                    self.assertEqual(environment['R1_DEV_NO_PAIRING'], '1')
                    self.assertEqual(environment['R1_DEV_MINUTES'], '60')
                    self.assertEqual(environment['R1_DEV_ALLOW_COMMANDS'], '0')
                    self.assertEqual(environment['R1_DEV_ALLOW_IPS'], '填写授权客户端IP或局域网网段')

    def test_yaml_duplicate_key_guard_rejects_hidden_overrides(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate YAML key: R1_DEV_NO_PAIRING'):
            yaml.load('environment:\n  R1_DEV_NO_PAIRING: "0"\n  R1_DEV_NO_PAIRING: "1"\n',
                      Loader=UniqueKeyLoader)

    def test_help_explains_boundaries_defaults_and_persistent_initialization(self):
        for name in OUTPUTS:
            with self.subTest(path=name):
                contents = (self.repo / name).read_text(encoding='utf-8')
                for explanation in (
                    'image 使用镜像内置界面', 'directory 使用下方 UI_DIR',
                    'diagnostic 不接管小屏', '须含非空 index.html',
                    'image 模式不能同时设置 UI_DIR', '默认 r1-toolbox',
                    '修改 container_name 时必须同步修改',
                    '默认 0.0.0.0', '127.0.0.1', '范围 1024–65535',
                    '常驻服务不支持 0', '18731 是本机只读 API 的保留端口',
                    'pair 仅用于旧版配对兼容', '小屏接管首次默认值',
                    '开发模式首次默认关闭（0）', '1 是开启示例',
                    '白名单默认不设置', '开启开发模式时必填', 'CIDR 或点分掩码网段',
                    '多个地址以逗号分隔', '默认 60，范围 1–1440',
                    '固定诊断命令首次默认关闭（0）', '不提供任意 Shell',
                    '网页已保存的设置优先', '不替代 MCP 独立密钥',
                    'MCP 开关／Token、网页口令、风扇／灯光策略均在网页管理',
                ):
                    self.assertIn(explanation, contents)

    def test_compose_interpolation_is_only_in_active_supported_fields(self):
        for name in OUTPUTS:
            with self.subTest(path=name):
                contents = (self.repo / name).read_text(encoding='utf-8')
                for line in contents.splitlines():
                    if line.lstrip().startswith('#'):
                        self.assertNotIn('${', line, '注释占位应直接说明填写方式，避免混入额外必填变量表达式')
                environment = self.read(name)['services']['r1-toolbox']['environment']
                self.assertNotIn('R1_TOOLBOX_IMAGE', environment)
                self.assertIn('外部 Compose 变量', contents)
                self.assertIn('preview（开发预览）', contents)
                self.assertIn('latest（主分支）', contents)
                self.assertIn('sha-提交哈希（固定版本）', contents)
                variables = re.findall(r'\$\{([A-Z][A-Z0-9_]*)[^}]*\}', contents)
                self.assertEqual(variables, ['R1_TOOLBOX_IMAGE', 'R1_DEV_ALLOW_IPS']
                                 if name == 'composer.dev.yml' else ['R1_TOOLBOX_IMAGE'])

    def test_existing_compose_fields_and_each_mount_have_usage_comments(self):
        anchors = (
            '    image:', '    container_name:', '    restart:', '    stop_grace_period:',
            '    privileged:', '    pid:', '    ipc:', '    network_mode:',
            '    - /:/host', '    - /dev:/host/dev', '    - /proc:/host/proc',
            '    - /sys:/host/sys', '    - /run:/host/run', '    - r1_toolbox_data:/data',
        )
        for name in OUTPUTS:
            with self.subTest(path=name):
                contents = (self.repo / name).read_text(encoding='utf-8')
                lines = contents.splitlines()
                for anchor in anchors:
                    index = next(i for i, line in enumerate(lines) if line.startswith(anchor))
                    self.assertTrue(lines[index - 1].startswith('    # '), anchor)
                for explanation in (
                    '随 Docker 启动并在异常退出后重启', '停止宽限期 3 分钟',
                    '等待官方小屏与风扇恢复', '无需再增加 ports 端口映射',
                    '宿主 RAM 运行目录', '升级必须保留原数据卷',
                ):
                    self.assertIn(explanation, contents)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Verify image UI archives and RAM caches without touching a NAS or Docker."""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from test_mount import load_actions


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ui_builder', ROOT / 'scripts/build-ui.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='r1-ui-bundle-test-'))
        self.addCleanup(self.recycle)
        (self.root / 'ui/assets').mkdir(parents=True)
        (self.root / 'ui/index.html').write_text('<p>Own UI</p>')
        (self.root / 'ui/assets/app.js').write_text('const value = 1;')
        (self.root / 'runtime').mkdir()
        (self.root / 'runtime/ui_service.py').write_text('print("local service")\n')
        (self.root / 'runtime/toolbox.py').write_text('class ToolboxController: pass\n')
        (self.root / 'runtime/insights.py').write_text('class InsightsCollector: pass\n')
        (self.root / 'runtime/device_info.py').write_text('def collect_device_info(*args): return {}\n')
        (self.root / 'runtime/histor.py').write_text('class HistorAdapter: pass\n')
        (self.root / 'runtime/web_config.py').write_text('class WebConfigServer: pass\n')
        for name in ('fan_policy.py', 'fan_native.py', 'fan_watchdog.py', 'temperature_sources.py',
                     'light_policy.py', 'monitoring.py', 'fnm_auth.py', 'fnm_compat.py', 'fan_diagnostics.py', 'dev_diagnostics.py', 'screen_diagnostics.py', 'disk_cache.py', 'disk_names.py', 'vendor_device.py',
                     'vendor_business.py', 'vendor_runtime.py', 'vendor_app_diagnostics.py', 'vendor_download_diagnostics.py', 'download_operations.py', 'vendor_media_operations.py', 'vendor_business_transport.py', 'native_capability_specs.py', 'vendor_native_capabilities.py',
                     'vendor_media_auth.py', 'vendor_media_probe.py', 'vendor_media_batch.py', 'vendor_transcode_probe.py',
                     'vendor_media_snapshot.py', 'vendor_app_probe.py', 'vendor_storage_probe.py',
                     'api_monitoring.py', 'api_access.py', 'api_permissions.py', 'api_contracts.py', 'api_docker_projects.py', 'api_docker_operations.py', 'api_docker_deployment.py', 'api_file_operations.py', 'api_file_provider.py', 'api_file_executor.py', 'api_file_write_backend.py', 'api_file_mutations.py', 'api_file_mutation_authority.py', 'api_file_native_index.py', 'api_file_native_index_reader.py', 'api_file_native_recycle.py', 'api_file_transfers.py', 'api_file_readiness.py', 'api_file_roots.py', 'mcp_service.py', 'integration_api.py', 'media_sort_plugin.py', 'media_sort_plugin_rules.py',
                     'network_access.py', 'service_settings.py', 'screen_manager.py'):
            (self.root / 'runtime' / name).write_text('# fixture only\n')
        (self.root / 'ui/web').mkdir()
        for name in ('index.html', 'login.html'):
            (self.root / 'ui/web' / name).write_text('web fixture')
        self.runtime = load_actions()
        self.runtime.runtime_root = self.root / 'ram'
        self.runtime.runtime_root.mkdir()
        self.digest = builder.build_bundle(self.root)
        self.archive = self.root / 'build/ui-bundle.tar'

    def recycle(self):
        trash = Path.home() / '.Trash'
        if sys.platform == 'darwin' and trash.is_dir():
            shutil.move(str(self.root), str(trash / self.root.name))
        else:
            print('测试临时目录已保留：', self.root)

    def read(self):
        return self.runtime.read_bundle(self.archive, hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def change_tar(self, operation):
        with tarfile.open(self.archive) as archive:
            members = [(member, archive.extractfile(member).read()) for member in archive]
        operation(members)
        with tarfile.open(self.archive, 'w') as archive:
            for member, data in members:
                archive.addfile(member, io.BytesIO(data) if member.isfile() else None)

    def test_build_is_reproducible_and_runtime_verifies_every_file(self):
        first = self.archive.read_bytes()
        self.assertEqual(builder.build_bundle(self.root), self.digest)
        self.assertEqual(self.archive.read_bytes(), first)
        manifest, files = self.read()
        self.assertEqual(set(manifest['files']), set(files) - {'manifest.json'})
        self.assertEqual(files['frontend/index.html'], b'<p>Own UI</p>')
        self.assertEqual(files['frontend/web/index.html'], b'web fixture')
        self.assertIn('runtime/web_config.py', files)
        self.assertIn('runtime/fan_diagnostics.py', files)
        self.assertIn('runtime/dev_diagnostics.py', files)
        self.assertIn('runtime/vendor_media_auth.py', files)
        self.assertIn('runtime/vendor_media_probe.py', files)
        self.assertIn('runtime/vendor_media_batch.py', files)
        self.assertIn('runtime/vendor_transcode_probe.py', files)
        self.assertIn('runtime/vendor_media_snapshot.py', files)
        self.assertIn('runtime/vendor_app_probe.py', files)
        self.assertIn('runtime/vendor_storage_probe.py', files)
        for name in ('api_access', 'api_permissions', 'api_file_roots', 'mcp_service', 'integration_api',
                     'network_access', 'service_settings', 'screen_manager'):
            self.assertIn('runtime/' + name + '.py', files)
        self.assertIn('runtime/disk_names.py', files)
        self.assertIn('runtime/vendor_device.py', files)
        for name in ('vendor_business', 'vendor_runtime', 'vendor_app_diagnostics', 'vendor_download_diagnostics'):
            self.assertIn('runtime/' + name + '.py', files)
        self.assertEqual((self.root / 'build/ui-bundle.sha256').read_text(), self.digest + '\n')

    def test_storage_probe_is_required_by_builder_and_runtime_manifest(self):
        (self.root / 'runtime/vendor_storage_probe.py').unlink()
        with self.assertRaises(ValueError):
            builder.build_bundle(self.root)

        def omit_probe(members):
            members[:] = [(member, data) for member, data in members
                          if member.name != 'runtime/vendor_storage_probe.py']
            for index, (member, data) in enumerate(members):
                if member.name == 'manifest.json':
                    manifest = json.loads(data)
                    manifest['files'].pop('runtime/vendor_storage_probe.py')
                    data = json.dumps(manifest).encode('utf8')
                    member.size = len(data)
                    members[index] = (member, data)

        self.change_tar(omit_probe)
        with self.assertRaises(ValueError):
            self.read()

    def test_archive_digest_and_payload_hash_corruption_are_rejected(self):
        with self.assertRaises(ValueError):
            self.runtime.read_bundle(self.archive, '0' * 64)
        def corrupt(members):
            for index, (member, _) in enumerate(members):
                if member.name == 'frontend/index.html':
                    members[index] = (member, b'X' * member.size)
                    break
        self.change_tar(corrupt)
        with self.assertRaises(ValueError):
            self.read()

    def test_native_adapter_is_bundled_and_bound_to_payload_integrity(self):
        guard = self.root / 'fixture.so'
        # Header-only fixture is never loaded; executable ABI tests run on Linux.
        header = bytearray(64)
        header[:7] = b'\x7fELF\x02\x01\x01'
        header[16:20] = b'\x03\x00\x3e\x00'
        guard.write_bytes(header)
        builder.build_bundle(self.root, native_guard=guard)
        manifest, files = self.read()
        self.assertEqual(files['runtime/fan_guard.so'], bytes(header))
        self.assertEqual(manifest['files']['runtime/fan_guard.so'], hashlib.sha256(header).hexdigest())
        def corrupt(members):
            for index, (member, data) in enumerate(members):
                if member.name == 'runtime/fan_guard.so':
                    members[index] = (member, b'X' * len(data))
        self.change_tar(corrupt)
        with self.assertRaises(ValueError):
            self.read()

    def test_native_adapter_rejects_wrong_architecture_or_symlink(self):
        guard = self.root / 'fixture.so'
        for value in (b'MZ' + bytes(62), b'\x7fELF\x01\x01\x01' + bytes(57),
                      b'\x7fELF\x02\x01\x01' + bytes(9) + b'\x03\x00\xb7\x00' + bytes(44)):
            guard.write_bytes(value)
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                builder.build_bundle(self.root, native_guard=guard)
        link = self.root / 'linked.so'
        link.symlink_to(guard)
        with self.assertRaises(ValueError):
            builder.build_bundle(self.root, native_guard=link)

    def test_untrusted_archive_paths_duplicates_and_links_are_rejected(self):
        original = self.archive.read_bytes()
        for name, kind in (('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
                           ('frontend/index.html', tarfile.REGTYPE),
                           ('frontend/assets/link', tarfile.SYMTYPE),
                           ('frontend/assets/fifo', tarfile.FIFOTYPE)):
            with self.subTest(name=name, kind=kind):
                self.archive.write_bytes(original)
                def add(members):
                    member = tarfile.TarInfo(name)
                    member.type, member.linkname = kind, '/etc/passwd' if kind == tarfile.SYMTYPE else ''
                    member.size = 1 if kind == tarfile.REGTYPE else 0
                    members.append((member, b'x'))
                self.change_tar(add)
                with self.assertRaises(ValueError):
                    self.read()
                self.assertFalse((self.root / 'escape').exists())

    def test_cache_is_bound_to_original_tar_and_rechecked_on_reuse(self):
        _, files = self.read()
        stage = self.runtime.runtime_root / 'stage'
        stage.mkdir()
        cache = self.runtime.write_cache(stage, files, self.archive, self.digest)
        self.runtime.verify_cache(cache, self.digest)
        (cache / 'frontend/index.html').write_text('changed')
        # An internally consistent replacement manifest must still fail because
        # the retained original archive defines the expected bytes.
        manifest = json.loads((cache / 'manifest.json').read_text())
        manifest['files']['frontend/index.html'] = hashlib.sha256(b'changed').hexdigest()
        (cache / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            self.runtime.verify_cache(cache, self.digest)

    def test_real_bootstrap_imports_do_not_pollute_verified_cache_across_restarts(self):
        # Model the bootstrap imports and one transitive companion using real
        # interpreter processes. No NAS locks, migration or hardware run here.
        (self.root / 'runtime/ui_service.py').write_text(
            'import toolbox\nfrom screen_manager import migrate_legacy_runtime\n'
            'def acquire_runtime_lock(): return None\n', encoding='utf-8')
        (self.root / 'runtime/screen_manager.py').write_text(
            'import toolbox\ndef migrate_legacy_runtime(): return None\n', encoding='utf-8')
        self.digest = builder.build_bundle(self.root)
        _, files = self.read()
        stage = self.runtime.runtime_root / 'stage'
        stage.mkdir()
        cache = self.runtime.write_cache(stage, files, self.archive, self.digest)
        original = {path.relative_to(cache).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in cache.rglob('*') if path.is_file()}
        shell = (ROOT / 'tools/boot-once/screen-actions.sh').read_text(encoding='utf-8')
        entry = next(line for line in shell.splitlines() if line.startswith('exec python3 '))
        # Use the shipped entrypoint's flags, rather than hard-coding -B in the
        # test. Removing it recreates the failed second-start CI behavior.
        arguments = shlex.split(entry.split('<<', 1)[0])[2:]
        self.assertEqual(arguments[-1], '-')
        environment = os.environ.copy()
        environment.pop('PYTHONDONTWRITEBYTECODE', None)
        environment.pop('PYTHONPYCACHEPREFIX', None)
        source = ('import sys\nsys.path.insert(0, sys.argv[1])\n'
                  'from ui_service import acquire_runtime_lock\n'
                  'from screen_manager import migrate_legacy_runtime\n'
                  'assert sys.dont_write_bytecode is True\n')
        for restart in range(2):
            with self.subTest(restart=restart):
                result = subprocess.run([sys.executable, *arguments, str(cache / 'runtime')],
                    input=source, capture_output=True, text=True, env=environment, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(list(cache.rglob('__pycache__')))
                self.runtime.verify_cache(cache, self.digest)
                current = {path.relative_to(cache).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in cache.rglob('*') if path.is_file()}
                self.assertEqual(current, original)

    def test_verified_cache_still_rejects_an_unexpected_bytecode_directory(self):
        _, files = self.read()
        stage = self.runtime.runtime_root / 'stage'
        stage.mkdir()
        cache = self.runtime.write_cache(stage, files, self.archive, self.digest)
        (cache / 'runtime/__pycache__').mkdir()
        with self.assertRaisesRegex(ValueError, '意外目录'):
            self.runtime.verify_cache(cache, self.digest)

    def test_cache_rejects_extra_files_and_source_symlinks(self):
        _, files = self.read()
        stage = self.runtime.runtime_root / 'stage'
        stage.mkdir()
        cache = self.runtime.write_cache(stage, files, self.archive, self.digest)
        (cache / 'unexpected').write_text('extra')
        with self.assertRaises(ValueError):
            self.runtime.verify_cache(cache, self.digest)
        (self.root / 'ui/assets/link').symlink_to(self.root / 'ui/index.html')
        with self.assertRaises(ValueError):
            builder.build_bundle(self.root)

    def test_tmpfs_check_uses_the_deepest_covering_mount(self):
        path = (self.root / 'ram').resolve()
        mountinfo = self.root / 'mountinfo'
        prefix = '1 0 1:1 / / rw - ext4 /dev/root rw\n'
        mountinfo.write_text(prefix + '2 1 0:2 / ' + str(path) + ' rw - tmpfs tmpfs rw\n')
        self.runtime.require_tmpfs(path, mountinfo)
        mountinfo.write_text(prefix)
        with self.assertRaises(ValueError):
            self.runtime.require_tmpfs(path, mountinfo)
        nested = path / 'nested'
        nested.mkdir()
        mountinfo.write_text(prefix + '2 1 0:2 / ' + str(path) + ' rw - tmpfs tmpfs rw\n'
                            + '3 2 1:2 / ' + str(nested) + ' rw - ext4 /dev/disk rw\n')
        with self.assertRaises(ValueError):
            self.runtime.require_tmpfs(nested, mountinfo)

    def test_mode_selection_preserves_diagnostics_and_explicit_image_mode(self):
        self.assertEqual(self.runtime.select_mode({}), ('diagnostic', ''))
        self.assertEqual(self.runtime.select_mode({'UI_DIR': '/drives/data/ui'}),
                         ('directory', '/drives/data/ui'))
        self.assertEqual(self.runtime.select_mode({'UI_MODE': 'image'}), ('image', ''))
        for env in ({'UI_MODE': 'image', 'UI_DIR': '/data/ui'}, {'UI_MODE': 'directory'},
                    {'UI_MODE': 'invalid'}, {'NEW_HTML': '/data/index.html'}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                self.runtime.select_mode(env)

    def test_real_bundle_closure_covers_every_runtime_import(self):
        """The shipped bundle must import cleanly:每层 import 都在包内或标准库。"""
        output = Path(tempfile.mkdtemp(prefix='r1-ui-bundle-real-'))
        self.addCleanup(self.recycle_path, output)
        digest = builder.build_bundle(ROOT, output=output)
        archive = output / 'ui-bundle.tar'
        runtime = load_actions()
        manifest, files = runtime.read_bundle(archive, digest)
        self.assertEqual(set(manifest['files']), set(files) - {'manifest.json'})
        self.assertIn('runtime/vendor_transcode_probe.py', files)
        self.assertIn('runtime/vendor_business_transport.py', files)
        # web_config 无条件 import 该模块；若它不在 manifest，闭包检查必须失败。
        self.assertIn('vendor_transcode_probe', module_imports(files['runtime/web_config.py']))

        closure, missing = runtime_import_closure(files)
        self.assertEqual(missing, [])
        self.assertIn('vendor_transcode_probe', closure)
        self.assertIn('vendor_business_transport', closure)

        # 受控导入：只用解包出来的 RAM 缓存内容导入网页模块，证明依赖闭包真实可导入。
        extracted = output / 'extracted'
        extracted.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(extracted)
        environment = dict(os.environ, PYTHONPATH=str(extracted / 'runtime'), PYTHONDONTWRITEBYTECODE='1')
        completed = subprocess.run(
            [sys.executable, '-B', '-c', 'import web_config, vendor_transcode_probe'],
            cwd=str(extracted), env=environment, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        self.assertFalse(any(extracted.rglob('__pycache__')))

    def test_real_bundle_rejects_an_unexpected_runtime_module(self):
        """Manifest 齐全且哈希正确时，额外模块仍必须被 RAM 允许清单拒绝。"""
        output = Path(tempfile.mkdtemp(prefix='r1-ui-bundle-extra-'))
        self.addCleanup(self.recycle_path, output)
        digest = builder.build_bundle(ROOT, output=output)
        archive = output / 'ui-bundle.tar'
        with tarfile.open(archive) as bundle:
            members = [(member, bundle.extractfile(member).read()) for member in bundle]
        manifest = json.loads(dict((m.name, d) for m, d in members)['manifest.json'])
        payload = b'print("unexpected")\n'
        manifest['files']['runtime/vendor_unexpected_extra.py'] = hashlib.sha256(payload).hexdigest()
        manifest_data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode() + b'\n'
        members = [(m, manifest_data if m.name == 'manifest.json' else d) for m, d in members]
        for member, data in members:
            member.size = len(data)
        info = tarfile.TarInfo('runtime/vendor_unexpected_extra.py')
        info.size = len(payload)
        members.insert(-1, (info, payload))
        with tarfile.open(archive, 'w') as bundle:
            for member, data in members:
                bundle.addfile(member, io.BytesIO(data))
        # 重算改动后归档的真实摘要：拒绝必须来自允许清单，而不是外层 SHA 不匹配。
        mutated = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertNotEqual(mutated, digest)
        with self.assertRaises(ValueError) as raised:
            load_actions().read_bundle(archive, mutated)
        message = str(raised.exception)
        self.assertIn('runtime/vendor_unexpected_extra.py', message)
        self.assertNotIn('SHA256', message)
        # 沿用旧摘要时只证明外层哈希校验，用来固定两条拒绝边界的区别。
        with self.assertRaises(ValueError) as stale:
            load_actions().read_bundle(archive, digest)
        self.assertIn('SHA256', str(stale.exception))

    def test_closure_detects_a_missing_module_even_in_a_deferred_branch(self):
        """反例：不允许闭包把未打包的 import 静默跳过。"""
        files = {
            'runtime/ui_service.py': b'import web_config\n',
            'runtime/web_config.py': (b'import vendor_missing\n'
                                      b'def later():\n'
                                      b'    import vendor_deferred\n'),
        }
        closure, missing = runtime_import_closure(files)
        self.assertEqual(closure, {'ui_service', 'web_config'})
        self.assertEqual(missing, ['vendor_deferred', 'vendor_missing'])

    def recycle_path(self, path):
        if sys.platform == 'darwin' and (Path.home() / '.Trash').is_dir():
            shutil.move(str(path), str(Path.home() / '.Trash' / path.name))
        else:
            print('测试临时目录已保留：', path)


def runtime_import_closure(files):
    """Resolve the runtime import closure of the shipped web modules.

    返回 `(closure, missing)`：`missing` 记录**所有**被 import 过的名字中既不在
    bundle 内、也不属于标准库的部分，因此函数内的延迟导入同样会被检出，不会被
    静默跳过。
    """
    import ast  # noqa: F401  (保留在函数内，避免模块导入期依赖)
    bundled = {name.split('/')[-1][:-3] for name in files
               if name.startswith('runtime/') and name.endswith('.py')}
    pending = {'ui_service', 'web_config'}
    closure = set()
    requested = set()
    while pending:
        name = pending.pop()
        if name in closure or name not in bundled:
            continue
        closure.add(name)
        for imported in module_imports(files['runtime/' + name + '.py']):
            requested.add(imported)
            if imported in bundled:
                pending.add(imported)
    missing = sorted(requested - bundled - set(sys.stdlib_module_names))
    return closure, missing


def module_imports(source):
    """Collect absolute module names imported by one bundled runtime file."""
    import ast
    names = set()
    for node in ast.walk(ast.parse(source.decode('utf-8'))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split('.')[0])
    return names


if __name__ == '__main__':
    unittest.main()

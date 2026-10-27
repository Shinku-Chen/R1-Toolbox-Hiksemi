#!/usr/bin/env python3
"""Verify image UI archives and RAM caches without touching a NAS or Docker."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
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
        for name in ('fan_policy.py', 'fan_native.py', 'fan_watchdog.py', 'temperature_sources.py', 'light_policy.py', 'monitoring.py'):
            (self.root / 'runtime' / name).write_text('# fixture only\n')
        (self.root / 'ui/web').mkdir()
        for name in ('index.html', 'config.css', 'config.js', 'monitor.js'):
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
        self.assertEqual((self.root / 'build/ui-bundle.sha256').read_text(), self.digest + '\n')

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


if __name__ == '__main__':
    unittest.main()

"""Mock OS boundaries while exercising the real serial display worker."""
import ast
import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('r1_screen_manager_test', ROOT / 'runtime/screen_manager.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Backend:
    def __init__(self):
        self.events = []
        self.alive = False
        self.failure = None
        self.entered = threading.Event()
        self.blocked = False
        self.release_failure = None

    def availability(self):
        return True, None

    def takeover(self, cancelled):
        self.events.append('takeover')
        self.entered.set()
        if self.blocked:
            cancelled.wait(2)
        if cancelled.is_set():
            raise ValueError('小屏切换已取消')
        if self.failure:
            raise ValueError(self.failure)
        self.alive = True

    def active(self):
        return self.alive

    def release(self):
        self.events.append('release')
        if self.release_failure:
            raise ValueError(self.release_failure)
        self.alive = False


class ScreenManagerTests(unittest.TestCase):
    def setUp(self):
        self.controller = mock.Mock()
        self.backend = Backend()
        self.manager = MODULE.ScreenManager(self.controller, backend=self.backend)
        self.addCleanup(self.manager.stop)

    def settled(self, predicate=lambda value: not value['busy'], timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.manager.snapshot()
            if predicate(value):
                return value
            time.sleep(0.01)
        self.fail('小屏状态未收敛：' + repr(self.manager.snapshot()))

    def test_initially_releases_screen_without_stopping_other_controller_work(self):
        self.assertEqual(self.manager.snapshot(), {'enabled': False, 'active': False,
            'busy': False, 'available': True, 'last_error': None})
        self.controller.set_screen_managed.assert_called_once_with(False)
        self.controller.stop.assert_not_called()

    def test_unreadable_display_metadata_does_not_prevent_manager_or_web_startup(self):
        backend = Backend()
        backend.availability = mock.Mock(side_effect=OSError('fixture unreadable display'))
        manager = MODULE.ScreenManager(self.controller, backend=backend)
        try:
            self.assertFalse(manager.snapshot()['available'])
            self.assertIn('Web 服务继续运行', manager.snapshot()['last_error'])
            self.assertTrue(manager.thread.is_alive())
        finally:
            manager.stop()

    def test_enable_disable_serially_restore_without_stopping_controller(self):
        self.manager.set_enabled(True)
        self.assertTrue(self.settled()['active'])
        self.controller.set_screen_managed.assert_called_with(True)
        self.manager.set_enabled(False)
        self.assertFalse(self.settled()['active'])
        self.assertEqual(self.backend.events, ['takeover', 'release'])
        self.controller.set_screen_managed.assert_called_with(False)
        self.controller.stop.assert_not_called()

    def test_enable_returns_while_preflight_is_running_and_disable_cancels_it(self):
        self.backend.blocked = True
        value = self.manager.set_enabled(True)
        self.assertTrue(value['busy'])
        self.assertTrue(self.backend.entered.wait(1))
        self.assertFalse(self.manager.snapshot()['active'])
        self.manager.set_enabled(False)
        value = self.settled()
        self.assertFalse(value['enabled'])
        self.assertFalse(value['active'])
        self.assertIsNone(value['last_error'])

    def test_failed_takeover_is_not_retried_until_a_new_explicit_request(self):
        self.backend.failure = '只读挂载未确认'
        self.manager.set_enabled(True)
        value = self.settled()
        self.assertTrue(value['enabled'])
        self.assertFalse(value['active'])
        self.assertIn('只读挂载', value['last_error'])
        time.sleep(1.1)
        self.assertEqual(self.backend.events.count('takeover'), 1)
        self.backend.failure = None
        self.manager.set_enabled(True)
        self.assertTrue(self.settled()['active'])
        self.assertEqual(self.backend.events.count('takeover'), 2)

    def test_unexpected_gui_exit_restores_official_without_retrying_gui(self):
        self.manager.set_enabled(True)
        self.assertTrue(self.settled()['active'])
        self.backend.alive = False
        value = self.settled(lambda state: not state['active'] and state['last_error'])
        self.assertTrue(value['enabled'])
        self.assertIn('Web 服务继续运行', value['last_error'])
        self.assertEqual(self.backend.events.count('takeover'), 1)
        self.assertIn('release', self.backend.events)

    def test_stop_cancels_worker_and_releases_before_returning(self):
        self.backend.blocked = True
        self.manager.set_enabled(True)
        self.assertTrue(self.backend.entered.wait(1))
        self.manager.stop()
        self.assertFalse(self.manager.thread.is_alive())
        self.assertFalse(self.manager.snapshot()['active'])
        self.assertEqual(self.backend.events[-1], 'release')
        with self.assertRaisesRegex(ValueError, '正在停止'):
            self.manager.set_enabled(True)

    def test_repeated_off_retries_failed_official_restoration(self):
        self.manager.set_enabled(True)
        self.assertTrue(self.settled()['active'])
        self.backend.release_failure = '官方窗口暂未恢复'
        self.manager.set_enabled(False)
        failed = self.settled()
        self.assertFalse(failed['enabled'])
        self.assertIn('官方窗口', failed['last_error'])
        self.assertTrue(failed['active'], '失败关闭时仍存活的 Toolbox 窗口必须如实显示')
        self.backend.release_failure = None
        self.manager.set_enabled(False)
        restored = self.settled()
        self.assertFalse(restored['active'])
        self.assertIsNone(restored['last_error'])
        self.assertEqual(self.backend.events.count('release'), 2)

    def test_repeated_off_retries_even_if_gui_is_already_gone_but_recovery_failed(self):
        self.manager.set_enabled(True)
        self.assertTrue(self.settled()['active'])
        self.backend.alive = False
        self.backend.release_failure = '官方恢复仍未确认'
        self.manager.set_enabled(False)
        self.assertFalse(self.settled()['active'])
        self.backend.release_failure = None
        self.manager.set_enabled(False)
        self.assertIsNone(self.settled()['last_error'])
        self.assertEqual(self.backend.events.count('release'), 2)

    def test_only_actual_boolean_switches_are_accepted(self):
        for value in (1, 0, 'true', None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.set_enabled(value)


class ScreenBoundaryTests(unittest.TestCase):
    def test_partial_original_stop_keeps_recovery_responsibility_and_restores_official(self):
        backend = MODULE.ScreenBackend('/fixture/frontend', 'private-test-token')
        original = [MODULE.Process(123, str(MODULE.app / 'hiksemi-sub'), '42', True, 1, None)]
        with mock.patch.object(backend, 'availability', return_value=(True, None)), \
                mock.patch.object(MODULE, 'resolve_source', return_value=Path('/fixture/frontend')), \
                mock.patch.object(MODULE, 'processes', side_effect=[original, original, []]), \
                mock.patch.object(MODULE, 'graphics_environment', return_value=(original[0], {'DISPLAY': ':0'})), \
                mock.patch.object(MODULE, 'preflight_binding'), \
                mock.patch.object(MODULE, 'run', return_value=types.SimpleNamespace(returncode=0, stdout='')) as command, \
                mock.patch.object(MODULE, 'stop_verified_family', side_effect=ValueError('部分停止后检查失败')), \
                mock.patch.object(MODULE, 'start') as launch, \
                self.assertRaisesRegex(ValueError, '部分停止'):
            backend.takeover(threading.Event())
        launch.assert_called_once_with(backend.unit + '-official', {'DISPLAY': ':0'})
        self.assertFalse(backend.owned)
        self.assertIn(mock.call('systemctl', 'reset-failed', backend.unit + '-check'), command.call_args_list)

    def test_failed_official_restore_retains_responsibility_for_explicit_retry(self):
        backend = MODULE.ScreenBackend('/fixture/frontend', 'private-test-token')
        backend.owned, backend.environment = True, {'DISPLAY': ':0'}
        with mock.patch.object(MODULE, 'processes', return_value=[]), \
                mock.patch.object(MODULE, 'start', side_effect=[RuntimeError('窗口恢复失败'), None]) as launch:
            with self.assertRaisesRegex(RuntimeError, '窗口恢复失败'):
                backend.release()
            self.assertTrue(backend.owned)
            backend.release()
        self.assertEqual(launch.call_count, 2)
        self.assertFalse(backend.owned)

    def test_failed_gui_unit_is_reset_before_the_same_private_name_is_launched(self):
        with mock.patch.object(MODULE, 'run', return_value=types.SimpleNamespace(returncode=1)) as command, \
                self.assertRaises(RuntimeError):
            MODULE.start('r1-toolbox-screen-fixture', {'DISPLAY': ':0'})
        self.assertEqual(command.call_args_list[0], mock.call('systemctl', 'reset-failed', 'r1-toolbox-screen-fixture'))
        self.assertEqual(command.call_args_list[1].args[0], 'systemd-run')

    def test_core_identity_and_mount_checks_match_previously_verified_installer(self):
        shell = (ROOT / 'tools/boot-once/screen-actions.sh').read_text(encoding='utf-8')
        old = ast.parse(shell.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0])
        new = ast.parse((ROOT / 'runtime/screen_manager.py').read_text(encoding='utf-8'))
        old_functions = {node.name: ast.dump(node) for node in old.body if isinstance(node, ast.FunctionDef)}
        new_functions = {node.name: ast.dump(node) for node in new.body if isinstance(node, ast.FunctionDef)}
        for name in ('resolve_source', 'binding_property', 'service_options', 'preflight_binding',
                     'verify_binding', 'process_type', 'read_process_stat', 'read_identity',
                     'processes', 'identify_family', 'read_environment', 'process_summary'):
            with self.subTest(name=name):
                self.assertEqual(new_functions[name], old_functions[name])

    def test_headless_availability_does_not_require_a_running_graphical_session(self):
        backend = MODULE.ScreenBackend(None, 'private-test-token')
        with mock.patch.object(MODULE, 'processes') as processes:
            available, reason = backend.availability()
        self.assertFalse(available)
        self.assertIn('Web 服务继续运行', reason)
        processes.assert_not_called()

    def test_named_non_toolbox_unit_is_never_stopped(self):
        def run(*args):
            value = ('histor-custom-12.service loaded active running\n'
                     'r1-toolbox-ui.service loaded active running\n') if args[1] == 'list-units' else '/usr/bin/unrelated'
            return types.SimpleNamespace(returncode=0, stdout=value)
        with mock.patch.object(MODULE.shutil, 'which', return_value='/fixture/systemctl'), \
                mock.patch.object(MODULE, 'run', side_effect=run) as command:
            MODULE.migrate_legacy_runtime()
        self.assertFalse(any(call.args[1] == 'stop' for call in command.call_args_list))

    def test_verified_old_runtime_must_confirm_stop_before_migration_finishes(self):
        def run(*args):
            if args[1] == 'list-units':
                value = 'histor-custom-12.service loaded active running\n'
            elif args[1] == 'show':
                value = '/usr/bin/python3 /run/r1-toolbox/ui-' + 'a' * 64 + '/runtime/ui_service.py --app /screen/squashfs-root'
            else:
                value = 'active'
            return types.SimpleNamespace(returncode=1 if args[1] == 'stop' else 0, stdout=value)
        with mock.patch.object(MODULE.shutil, 'which', return_value='/fixture/systemctl'), \
                mock.patch.object(MODULE, 'run', side_effect=run), \
                mock.patch.object(MODULE, 'graphics_environment', side_effect=ValueError('无 DISPLAY')), \
                mock.patch.object(MODULE, 'processes', return_value=[]), \
                self.assertRaisesRegex(ValueError, '未确认停止'):
            MODULE.migrate_legacy_runtime()


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'screen'
        self.root.mkdir()
        self.runtime = self.root / 'squashfs-root'
        self.mountinfo = Path(self.temp.name) / 'mountinfo'
        self.mountinfo.write_text(
            '1 1 0:1 / {} rw - ext4 /dev/mmcblk0p7 rw\n'.format(self.root), encoding='utf-8')

    def appimage(self):
        path = self.root / 'HistorSub-Linux-1.1.15.AppImage'
        path.write_bytes(b'fixture AppImage')
        path.chmod(0o755)
        return path

    def test_complete_runtime_is_used_without_extraction(self):
        (self.runtime / 'resources/app/dist').mkdir(parents=True)
        (self.runtime / 'resources/app/dist/index.html').write_text('fixture')
        (self.runtime / 'hiksemi-sub').write_text('fixture')
        (self.runtime / 'AppRun').write_text('fixture')
        (self.runtime / 'AppRun').chmod(0o755)
        with mock.patch.object(MODULE.subprocess, 'run') as command:
            MODULE.provision_screen_runtime(root=self.root, runtime=self.runtime,
                                             mountinfo=self.mountinfo, wait_seconds=0)
        command.assert_not_called()

    def test_missing_screen_mount_is_not_written(self):
        (self.runtime).parent.mkdir(exist_ok=True)
        empty = Path(self.temp.name) / 'empty-mountinfo'
        empty.write_text('', encoding='utf-8')
        with mock.patch.object(MODULE.subprocess, 'run') as command,                 self.assertRaisesRegex(ValueError, '/screen 分区尚未挂载'):
            MODULE.provision_screen_runtime(root=self.root, runtime=self.runtime,
                                             mountinfo=empty, wait_seconds=0)
        command.assert_not_called()

    def test_missing_runtime_is_extracted_from_official_appimage(self):
        self.appimage()
        def run(args, **kwargs):
            self.assertEqual(args[1], '--appimage-extract')
            extracted = Path(kwargs['cwd']) / 'squashfs-root'
            (extracted / 'resources/app/dist').mkdir(parents=True)
            (extracted / 'resources/app/dist/index.html').write_text('fixture')
            (extracted / 'hiksemi-sub').write_text('fixture')
            (extracted / 'AppRun').write_text('fixture')
            (extracted / 'AppRun').chmod(0o755)
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.object(MODULE.subprocess, 'run', side_effect=run):
            MODULE.provision_screen_runtime(root=self.root, runtime=self.runtime,
                                             mountinfo=self.mountinfo, wait_seconds=0)
        self.assertTrue(MODULE.runtime_complete(self.runtime))
        self.assertEqual(self.runtime.parent, self.root)
        self.assertFalse(any(p.name.startswith('.r1-toolbox-screen-') for p in self.root.iterdir()))

    def test_incomplete_existing_runtime_is_never_overwritten(self):
        self.runtime.mkdir()
        self.appimage()
        with mock.patch.object(MODULE.subprocess, 'run') as command,                 self.assertRaisesRegex(ValueError, '解包目录不完整'):
            MODULE.provision_screen_runtime(root=self.root, runtime=self.runtime,
                                             mountinfo=self.mountinfo, wait_seconds=0)
        command.assert_not_called()



if __name__ == '__main__':
    unittest.main()

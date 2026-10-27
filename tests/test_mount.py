#!/usr/bin/env python3
"""Simulate private UI mounts; never run Docker, systemd, or NAS commands.

Run: python3 -m unittest discover -s tests -p 'test_mount.py'
"""

import ast
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_actions():
    """Load functions from the shipped shell payload without executing main."""
    shell = (ROOT / "tools/boot-once/screen-actions.sh").read_text(encoding="utf-8")
    python = shell.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    tree = ast.parse(python)
    tree.body = [node for node in tree.body if isinstance(node, (
        ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef,
        ast.Assign, ast.AnnAssign,
    ))]
    module = types.ModuleType("mount_actions_under_test")
    exec(compile(tree, "screen-actions.sh::<python>", "exec"), module.__dict__)
    return module


class MountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(tempfile.mkdtemp(prefix="r1-mount-test-"))
        self.addCleanup(self.recycle_fixture)
        self.module = load_actions()
        self.app = self.temporary / "screen/squashfs-root"
        self.target = self.app / "resources/app/dist"
        self.target.mkdir(parents=True)
        (self.target / "index.html").write_text("original page", encoding="utf-8")
        (self.app / "hiksemi-sub").write_bytes(b"original application")
        (self.app / "AppRun").write_text("#!/bin/sh\nexit 0\n")
        (self.app / "AppRun").chmod(0o755)
        self.source = self.temporary / "custom-ui"
        (self.source / "assets").mkdir(parents=True)
        (self.source / "index.html").write_text("custom page", encoding="utf-8")
        (self.source / "assets/main.js").write_text("custom script", encoding="utf-8")
        self.proc = self.temporary / "proc"
        self.pid = 31001
        (self.proc / str(self.pid)).mkdir(parents=True)
        (self.proc / str(self.pid) / "environ").write_bytes(b"DISPLAY=:0\0LANG=C\0")
        (self.proc / str(self.pid) / "stat").write_text(
            str(self.pid) + " (hiksemi-sub) " + " ".join(["S", "1", *(["0"] * 17), "1000"]) + "\n"
        )
        self.module.app = self.app
        self.module.target = self.target
        self.module.unit = "histor-custom-test"
        self.module.pathlib = types.SimpleNamespace(Path=self.mapped_path)
        self.module.shutil = types.SimpleNamespace(which=lambda name: "/fake/" + name)
        self.clock = 0
        self.module.time = types.SimpleNamespace(
            monotonic=self.tick, sleep=lambda seconds: None,
        )
        self.live = True
        self.events = []
        self.module.processes = self.processes
        self.module.run = self.fake_run
        self.module.preflight_binding = mock.Mock()
        self.module.start = mock.Mock(side_effect=self.start)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.module.os, "kill", side_effect=self.kill).start()
        self.original = self.snapshot()

    def recycle_fixture(self):
        trash = Path.home() / ".Trash"
        if sys.platform == "darwin" and trash.is_dir():
            destination = trash / self.temporary.name
            if not destination.exists():
                shutil.move(str(self.temporary), str(destination))
                return
        print("测试临时目录已保留：", self.temporary)

    def mapped_path(self, *parts):
        path = Path(*parts)
        if path.is_absolute() and path.parts[1:2] == ("proc",):
            return self.proc.joinpath(*path.parts[2:])
        return path

    def tick(self):
        self.clock += 0.5
        return self.clock

    def snapshot(self):
        return {
            str(path.relative_to(self.temporary)): (
                path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns,
            )
            for directory in (self.app, self.source)
            for path in directory.rglob("*") if path.is_file()
        }

    def processes(self):
        return [self.module.Process(self.pid, str(self.app / "hiksemi-sub"), "1000", True, 1, None)] if self.live else []

    def kill(self, pid, sig):
        self.assertEqual(pid, self.pid)
        self.assertIn(sig, (signal.SIGTERM, signal.SIGKILL))
        self.events.append(("kill", pid, sig))
        self.live = False

    def fake_run(self, *args, env=None):
        self.events.append(("command", args))
        if args[:2] == ("systemctl", "stop"):
            self.live = False
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def start(self, name, environment, bind_source=None):
        self.events.append(("start", name, bind_source))

    def main(self, environment):
        output = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(output):
            result = self.module.main()
        self.assertEqual(self.snapshot(), self.original,
                         "the host app and custom UI must never be copied or rewritten")
        return result, output.getvalue()

    def assert_not_stopped(self):
        self.assertFalse(any(event[0] == "kill" for event in self.events), self.events)
        self.module.start.assert_not_called()

    def test_empty_ui_dir_only_diagnoses(self):
        result, output = self.main({"UI_DIR": ""})
        self.assertEqual(result, 0, output)
        self.assert_not_stopped()
        self.module.preflight_binding.assert_not_called()
        self.assertEqual(self.events, [])

    def test_invalid_ui_directory_does_not_stop_the_screen(self):
        for value in ("relative/path", str(self.temporary / "missing"),
                      str(self.source / "index.html"), str(self.target)):
            with self.subTest(value=value):
                result, output = self.main({"UI_DIR": value})
                self.assertEqual(result, 1, output)
                self.assert_not_stopped()
                self.module.preflight_binding.assert_not_called()

    def test_legacy_html_argument_is_rejected_before_stopping(self):
        for ui_dir in ("", str(self.source)):
            with self.subTest(ui_dir=ui_dir):
                result, output = self.main({
                    "UI_DIR": ui_dir, "NEW_HTML": str(self.source / "index.html"),
                })
                self.assertEqual(result, 1, output)
                self.assert_not_stopped()
                self.module.preflight_binding.assert_not_called()

    def test_failed_bind_preflight_does_not_stop_the_screen(self):
        self.module.preflight_binding.side_effect = RuntimeError("private mount unavailable")
        result, output = self.main({"UI_DIR": str(self.source)})
        self.assertEqual(result, 1, output)
        self.module.preflight_binding.assert_called_once()
        self.assert_not_stopped()

    def test_success_starts_screen_with_the_validated_ui_directory(self):
        result, output = self.main({"UI_DIR": str(self.source)})
        self.assertEqual(result, 0, output)
        self.module.preflight_binding.assert_called_once()
        self.assertTrue(any(event[0] == "kill" for event in self.events))
        starts = [event for event in self.events if event[0] == "start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][2], self.source.resolve())

    def test_six_process_electron_family_passes_preflight_and_is_stopped_together(self):
        records = [self.module.Process(self.pid, str(self.app / "hiksemi-sub"), "1000", True, 1, None)]
        for offset, parent, kind in ((1, self.pid, "zygote"), (2, self.pid, "zygote"),
                                     (3, self.pid + 1, "gpu-process"),
                                     (4, self.pid, "utility"), (5, self.pid, "renderer")):
            records.append(self.module.Process(self.pid + offset, str(self.app / "hiksemi-sub"),
                                               "1000", False, parent, kind))
        killed = set()
        self.module.processes = lambda: [record for record in records if record.pid not in killed]

        def stop_family(pid, sig):
            self.events.append(("kill", pid, sig))
            killed.add(pid)
        self.module.os.kill.side_effect = stop_family
        result, output = self.main({"UI_DIR": str(self.source)})
        self.assertEqual(result, 0, output)
        self.assertEqual(killed, {record.pid for record in records})
        self.module.preflight_binding.assert_called_once()
        self.module.start.assert_called_once()

    def test_unsafe_electron_snapshot_fails_preflight_without_stopping(self):
        root = self.module.Process(self.pid, str(self.app / "hiksemi-sub"), "1000", True, 1, None)
        for is_main, parent, kind in ((True, self.pid, None), (False, 99999, "renderer"),
                                      (False, self.pid, "?")):
            with self.subTest(is_main=is_main, parent=parent, kind=kind):
                other = self.module.Process(self.pid + 1, root.exe, "1000", is_main, parent, kind)
                self.module.processes = lambda: [root, other]
                result, output = self.main({"UI_DIR": str(self.source)})
                self.assertEqual(result, 1, output)
                self.assert_not_stopped()
                self.module.preflight_binding.assert_not_called()

    def test_transient_process_title_ambiguity_can_settle_before_preflight(self):
        root = self.module.Process(self.pid, str(self.app / "hiksemi-sub"), "1000", True, 1, None)
        unresolved = self.module.Process(self.pid + 1, root.exe, "1000", False, root.pid, "?")
        waits = []

        def settle(seconds):
            waits.append(seconds)
        self.module.time.sleep = settle
        self.module.processes = lambda: ([root] if waits else [root, unresolved]) if self.live else []
        result, output = self.main({"UI_DIR": str(self.source)})
        self.assertEqual(result, 0, output)
        self.assertTrue(waits, "the incomplete Electron family should be allowed to settle")
        self.module.preflight_binding.assert_called_once()
        self.module.start.assert_called_once()

    def test_failed_mounted_launch_stops_it_then_recovers_without_bind(self):
        def failing_start(name, environment, bind_source=None):
            self.start(name, environment, bind_source)
            if bind_source is not None:
                self.live = True
                raise RuntimeError("read-only bind validation failed")
        self.module.start.side_effect = failing_start
        result, output = self.main({"UI_DIR": str(self.source)})
        self.assertEqual(result, 1, output)
        starts = [event for event in self.events if event[0] == "start"]
        self.assertEqual([event[2] for event in starts], [self.source.resolve(), None])
        first = self.events.index(starts[0])
        second = self.events.index(starts[1])
        self.assertTrue(any(
            event[0] == "command" and event[1][:2] == ("systemctl", "stop")
            for event in self.events[first + 1:second]
        ), "the failed private namespace must be stopped before recovery")

    def test_preflight_and_launch_use_the_same_private_bind_property(self):
        actions = load_actions()
        actions.app, actions.target = self.app, self.target
        actions.unit = "histor-custom-test"
        actions.pathlib = types.SimpleNamespace(Path=self.mapped_path)
        actions.time = self.module.time
        calls = []

        def command(*args, env=None):
            calls.append(args)
            if args[0] == "systemd-run":
                name = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--unit="))
                group = "/system.slice/" + name + ".service"
                (self.proc / str(self.pid) / "cgroup").write_text("0::" + group + "\n")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[:2] == ("systemctl", "show"):
                value = "/system.slice/" + args[2] + ".service\n"
            elif args[:2] == ("systemctl", "is-active"):
                value = "active\n"
            else:
                self.fail("unexpected host command: " + repr(args))
            return types.SimpleNamespace(returncode=0, stdout=value, stderr="")

        actions.run = command
        actions.processes = self.processes
        actions.verify_binding = mock.Mock(return_value=True)
        actions.preflight_binding("binding-check", self.source)
        actions.start("bound-screen", {"DISPLAY": ":0"}, self.source)
        actions.start("recovery-screen", {"DISPLAY": ":0"})
        services = [args for args in calls if args[0] == "systemd-run"]
        self.assertEqual(len(services), 3)
        property_value = actions.binding_property(self.source)
        self.assertIn("BindReadOnlyPaths=", property_value)
        self.assertIn(str(self.source) + ":" + str(self.target) + ":norbind", property_value)
        self.assertIn(property_value, services[0])
        self.assertIn(property_value, services[1])
        self.assertFalse(any("BindReadOnlyPaths=" in value for value in services[2]))
        self.assertIn("--wait", services[0])
        self.assertTrue(actions.verify_binding.called,
                        "the launched process namespace must be verified")

        actions.run = mock.Mock(return_value=types.SimpleNamespace(returncode=1))
        with self.assertRaises((ValueError, RuntimeError)):
            actions.preflight_binding("binding-check-failed", self.source)

    def test_launch_rejects_wrong_cgroup_or_unverified_mount(self):
        actions = load_actions()
        actions.app, actions.target = self.app, self.target
        actions.pathlib = types.SimpleNamespace(Path=self.mapped_path)
        actions.time = self.module.time
        actions.processes = self.processes

        def command(*args, env=None):
            if args[0] == "systemd-run":
                value = ""
            elif args[:2] == ("systemctl", "show"):
                value = "/system.slice/expected.service\n"
            elif args[:2] == ("systemctl", "is-active"):
                value = "active\n"
            else:
                self.fail("unexpected host command: " + repr(args))
            return types.SimpleNamespace(returncode=0, stdout=value, stderr="")

        actions.run = command
        for correct_group, verified_mount in ((False, True), (True, False)):
            with self.subTest(correct_group=correct_group, verified_mount=verified_mount):
                group = "/system.slice/expected.service" if correct_group else "/unrelated.service"
                (self.proc / str(self.pid) / "cgroup").write_text("0::" + group + "\n")
                actions.verify_binding = mock.Mock(return_value=verified_mount)
                with self.assertRaises(RuntimeError):
                    actions.start("expected", {"DISPLAY": ":0"}, self.source)

    def timed_launch(self, snapshot):
        actions = load_actions()
        actions.app, actions.target = self.app, self.target
        actions.pathlib = types.SimpleNamespace(Path=self.mapped_path)
        clock = types.SimpleNamespace(now=0)

        def advance(seconds):
            clock.now += seconds
        actions.time = types.SimpleNamespace(monotonic=lambda: clock.now, sleep=advance)
        actions.processes = lambda: snapshot(actions, clock)
        actions.verify_binding = mock.Mock(return_value=True)
        (self.proc / str(self.pid) / "cgroup").write_text("0::/system.slice/expected.service\n")

        def command(*args, env=None):
            if args[0] == "systemd-run":
                value = ""
            elif args[:2] == ("systemctl", "show"):
                value = "/system.slice/expected.service\n"
            elif args[:2] == ("systemctl", "is-active"):
                value = "active\n"
            else:
                self.fail("unexpected host command: " + repr(args))
            return types.SimpleNamespace(returncode=0, stdout=value, stderr="")
        actions.run = command
        return actions, clock

    def test_launch_restarts_three_second_timer_when_pid_identity_changes(self):
        def snapshot(actions, clock):
            start = "1000" if clock.now < 1.5 else "2000"
            return [actions.Process(self.pid, str(self.app / "hiksemi-sub"), start, True, 1, None)]
        actions, clock = self.timed_launch(snapshot)
        actions.start("expected", {"DISPLAY": ":0"}, self.source)
        self.assertGreaterEqual(clock.now, 4.5,
                                "a reused PID must survive three seconds after its new starttime")
        self.assertLess(clock.now, 10)

    def test_launch_rejects_nested_second_main_or_unowned_typed_process(self):
        for is_main, parent, kind in ((True, self.pid, None), (False, 99999, "renderer")):
            with self.subTest(is_main=is_main, parent=parent, kind=kind):
                def snapshot(actions, clock):
                    binary = str(self.app / "hiksemi-sub")
                    return [actions.Process(self.pid, binary, "1000", True, 1, None),
                            actions.Process(self.pid + 1, binary, "1000", is_main, parent, kind)]
                actions, _ = self.timed_launch(snapshot)
                with self.assertRaises((ValueError, RuntimeError)):
                    actions.start("expected", {"DISPLAY": ":0"}, self.source)

    def test_settings_use_only_the_explicit_docker_data_volume(self):
        actions = load_actions()
        actions.app = self.app
        volume = self.temporary / "docker-volume"
        volume.mkdir()
        mounted = {"Destination": "/data", "Type": "volume", "RW": True, "Source": str(volume)}
        actions.run = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stdout=json.dumps([mounted])))
        self.assertEqual(actions.settings_directory("r1-toolbox"), volume.resolve())
        self.assertEqual(actions.settings_directory("r1-toolbox", required=True), volume.resolve())
        self.assertEqual(list(volume.iterdir()), [], "volume discovery must not write settings")
        with mock.patch.object(actions.os, "statvfs", return_value=types.SimpleNamespace(f_flag=os.ST_RDONLY)):
            with self.assertRaisesRegex(ValueError, "持久化设置卷"):
                actions.settings_directory("r1-toolbox", required=True)
        for invalid in ({**mounted, "Type": "bind"}, {**mounted, "RW": False},
                        {**mounted, "Source": str(self.app)}, {**mounted, "Source": "/"}):
            with self.subTest(invalid=invalid):
                actions.run.return_value.stdout = json.dumps([invalid])
                self.assertEqual(actions.settings_directory("r1-toolbox"), actions.runtime_root / "settings")
                with self.assertRaisesRegex(ValueError, "持久化设置卷"):
                    actions.settings_directory("r1-toolbox", required=True)
        actions.run.return_value.stdout = "[]"
        with self.assertRaisesRegex(ValueError, "持久化设置卷"):
            actions.settings_directory("r1-toolbox", required=True)
        self.assertEqual(self.snapshot(), self.original)

    def test_binding_verification_requires_identity_and_read_only(self):
        actions = load_actions()
        actions.target = self.target
        actions.pathlib = types.SimpleNamespace(Path=self.mapped_path)
        for index, (same_directory, read_only) in enumerate(((True, True), (False, True), (True, False))):
            with self.subTest(same_directory=same_directory, read_only=read_only):
                pid = self.pid + index
                visible = self.proc / str(pid) / "root" / self.target.relative_to("/")
                visible.parent.mkdir(parents=True, exist_ok=True)
                visible.symlink_to(self.source if same_directory else self.target,
                                   target_is_directory=True)
                flags = os.ST_RDONLY if read_only else 0
                with mock.patch.object(actions.os, "statvfs", return_value=types.SimpleNamespace(f_flag=flags)):
                    self.assertEqual(actions.verify_binding(pid, self.source),
                                     same_directory and read_only)

    def test_host_python_does_not_load_the_electron_library_path(self):
        actions = load_actions()
        actions.app, actions.target = self.app, self.target
        actions.run = mock.Mock(return_value=types.SimpleNamespace(returncode=1))
        backend = self.temporary / 'runtime/ui_service.py'
        with self.assertRaises(RuntimeError):
            actions.start('bundled-ui', {'DISPLAY': ':0', 'LD_LIBRARY_PATH': '/electron/lib'},
                          self.source, backend=backend)
        command = actions.run.call_args.args
        self.assertIn('--setenv=R1_TOOLBOX_APP_LD_LIBRARY_PATH=/electron/lib', command)
        self.assertIn('--setenv=LD_LIBRARY_PATH=', command)
        self.assertNotIn('--setenv=LD_LIBRARY_PATH=/electron/lib', command)
        self.assertIn('-B', command)
        self.assertIn(str(backend), command)


if __name__ == "__main__":
    unittest.main()

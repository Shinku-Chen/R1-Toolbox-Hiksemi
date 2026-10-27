#!/usr/bin/env python3
"""Exercise Electron process discovery with synthetic Linux proc files.

Run: python3 -m unittest discover -s tests -p 'test_processes.py'
No real process is inspected or signalled.
"""

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from test_mount import load_actions


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(tempfile.mkdtemp(prefix="r1-process-test-"))
        self.addCleanup(self.recycle_fixture)
        self.proc = self.temporary / "proc"
        self.proc.mkdir()
        self.exe = self.temporary / "screen/squashfs-root/hiksemi-sub"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"fixture Electron binary")
        self.actions = load_actions()

    def recycle_fixture(self):
        trash = Path.home() / ".Trash"
        if sys.platform == "darwin" and trash.is_dir():
            destination = trash / self.temporary.name
            if not destination.exists():
                shutil.move(str(self.temporary), str(destination))
                return
        print("测试临时目录已保留：", self.temporary)

    def write_process(self, pid, ppid, argv, *, start="1000", display=False, proc=None, executable=None):
        folder = (proc or self.proc) / str(pid)
        folder.mkdir(parents=True, exist_ok=True)
        link = folder / "exe"
        if not link.is_symlink():
            link.symlink_to(executable or self.exe)
        folder.joinpath("cmdline").write_bytes(b"\0".join(argv) + b"\0")
        # Linux fields 3..22: state, PPID, 17 unused fields, starttime.
        fields = ["S", str(ppid), *("0" for _ in range(17)), str(start)]
        folder.joinpath("stat").write_text(f"{pid} (hiksemi sub (worker)) " + " ".join(fields) + "\n")
        folder.joinpath("status").write_text(f"Name:\thiksemi-sub\nPPid:\t{ppid}\n")
        environment = b"DISPLAY=:0\0LANG=C\0" if display else b"LANG=C\0"
        folder.joinpath("environ").write_bytes(environment)

    def write_family(self, *, flattened=True, proc=None):
        proc = proc or self.proc
        proc.mkdir(parents=True, exist_ok=True)
        binary = str(self.exe).encode()
        self.write_process(976, 959, [binary, b"--no-sandbox"], display=True, proc=proc)
        for pid, ppid, kind in (
            (1012, 976, "zygote"), (1013, 976, "zygote"),
            (1103, 1012, "gpu-process"), (1117, 976, "utility"),
            (1133, 976, "renderer"),
        ):
            if flattened:
                argv = [binary + b" --type=" + kind.encode() + b" --no-sandbox"]
            else:
                argv = [binary, b"--type=" + kind.encode(), b"--no-sandbox"]
            self.write_process(pid, ppid, argv, proc=proc)

    def test_real_nul_argv_and_flattened_chromium_titles_form_one_family(self):
        for flattened in (False, True):
            with self.subTest(flattened=flattened):
                proc = self.temporary / ("flattened-proc" if flattened else "normal-proc")
                self.write_family(flattened=flattened, proc=proc)
                snapshot = self.actions.processes(proc)
                self.assertEqual({process.pid for process in snapshot}, {976, 1012, 1013, 1103, 1117, 1133})
                self.assertEqual([process.pid for process in snapshot if process.is_main], [976])
                family = self.actions.identify_family(snapshot)
                self.assertEqual(family.pid, 976)
                self.assertEqual(family.ppid, 959)
                self.assertEqual(family.start, "1000")
                self.assertEqual({process.pid: process.process_type for process in snapshot}, {
                    976: None, 1012: "zygote", 1013: "zygote", 1103: "gpu-process",
                    1117: "utility", 1133: "renderer",
                })

    def test_type_text_inside_an_ordinary_argument_is_not_a_child_flag(self):
        binary = str(self.exe).encode()
        for value in (b"--note=literal --type=renderer", b"https://example.invalid/?--type=utility",
                      b"--typewriter=gpu-process"):
            with self.subTest(value=value):
                raw = b"\0".join([binary, value, b"--no-sandbox"]) + b"\0"
                self.assertIsNone(self.actions.process_type(raw, str(self.exe)))

    def test_ambiguous_process_titles_and_type_flags_are_rejected(self):
        binary = str(self.exe).encode()
        cases = (
            [binary, b"--type", b"renderer"],
            [binary, b"--type="],
            [binary, b"--type=renderer", b"--type=utility"],
            [b"unrecognized renamed title --type=renderer"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                raw = b"\0".join(argv) + b"\0"
                self.assertEqual(self.actions.process_type(raw, str(self.exe)), "?")
                self.write_family()
                self.write_process(1117, 976, argv)
                with self.assertRaises(ValueError):
                    self.actions.identify_family(self.actions.processes(self.proc))

    def test_two_real_mains_are_rejected_even_when_one_is_nested(self):
        for parent in (1, 976):
            with self.subTest(second_main_parent=parent):
                proc = self.temporary / ("independent" if parent == 1 else "nested")
                self.write_family(proc=proc)
                self.write_process(2000, parent, [str(self.exe).encode(), b"--no-sandbox"], proc=proc)
                with self.assertRaises(ValueError):
                    self.actions.identify_family(self.actions.processes(proc))

    def test_orphaned_typed_process_and_parent_cycle_are_rejected(self):
        for cycle in (False, True):
            with self.subTest(cycle=cycle):
                proc = self.temporary / ("cycle" if cycle else "orphan")
                self.write_family(proc=proc)
                self.write_process(2000, 2001, [str(self.exe).encode(), b"--type=renderer"], proc=proc)
                if cycle:
                    self.write_process(2001, 2000, [str(self.exe).encode(), b"--type=utility"], proc=proc)
                with self.assertRaises(ValueError):
                    self.actions.identify_family(self.actions.processes(proc))

    def test_typed_process_without_a_main_is_rejected(self):
        self.write_process(1012, 976, [str(self.exe).encode(), b"--type=zygote"])
        with self.assertRaises(ValueError):
            self.actions.identify_family(self.actions.processes(self.proc))

    def test_mixed_appimage_and_extracted_executables_are_rejected(self):
        self.write_family()
        mounted = "/tmp/.mount_HistorFixture/hiksemi-sub"
        self.write_process(2000, 976, [mounted.encode(), b"--type=renderer"], executable=mounted)
        with self.assertRaises(ValueError):
            self.actions.identify_family(self.actions.processes(self.proc))

    def test_proc_identity_parses_parent_and_starttime_despite_parentheses(self):
        self.write_process(976, 959, [str(self.exe).encode()], start="123456")
        self.assertEqual(self.actions.read_identity(976, self.proc), (959, "123456"))

    def test_environment_read_is_guarded_against_pid_reuse_and_reparenting(self):
        self.write_family()
        root = self.actions.identify_family(self.actions.processes(self.proc))
        self.assertIn(b"DISPLAY=:0", self.actions.read_environment(root, self.proc))
        identity = (root.ppid, root.start)
        for replacement in ((root.ppid, "2000"), (1, root.start)):
            with self.subTest(replacement=replacement):
                with mock.patch.object(self.actions, "read_identity", side_effect=[identity, replacement]):
                    with self.assertRaises(ValueError):
                        self.actions.read_environment(root, self.proc)


if __name__ == "__main__":
    unittest.main()

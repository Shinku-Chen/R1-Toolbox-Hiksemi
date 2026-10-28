#!/usr/bin/env python3
"""Exercise the shell lifecycle with fake host tools, never real Docker/NAS.

Run with: python3 test_runner.py
This verifies shell behavior, not Linux PID 1 or a NAS/Docker boot sequence.
"""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent


class BootOnceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r1-boot-once-test-"))
        self.addCleanup(self.recycle_temporary)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.events = self.root / "events.jsonl"
        self.pid_file = self.root / "runner.pid"
        self.action_file = self.root / "actions"
        self.state_dir = self.root / "state"
        self.boot_file = self.root / "boot_id"
        self.boot_file.write_text("boot-a\n")
        self.env = os.environ.copy()
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.defpath,
            FAKE_BIN=str(self.bin),
            EVENT_FILE=str(self.events),
            RUNNER_PID_FILE=str(self.pid_file),
            TASK_CONTAINER="r1-boot-once-test",
            FAKE_IMAGE_ID="sha256:" + "1" * 64,
        )
        self.install_fake_tools()

    def recycle_temporary(self):
        # Preserve the user's Trash preference even for test fixtures.
        trash = Path.home() / ".Trash"
        if sys.platform == "darwin" and trash.is_dir():
            shutil.move(str(self.root), str(trash / self.root.name))
        else:
            print("测试临时目录已保留：", self.root)

    def install_fake_tools(self):
        # The fake chroot never opens /host. It dispatches only the two calls
        # allowed by the runner to local fake tools while preserving stdin/env.
        scripts = {
            "chroot": r'''
import os, sys
script = sys.argv[4]
if "exec docker " in script:
    os.execv(os.environ["FAKE_BIN"] + "/docker", ["docker"] + sys.argv[6:])
if "exec nsenter " in script:
    os.execv(os.environ["FAKE_BIN"] + "/nsenter", ["nsenter"])
raise SystemExit("fake chroot rejected unexpected operation")
''',
            "nsenter": r'''
import os
os.execv("/bin/sh", ["/bin/sh", "-s"])
''',
            "docker": r'''
import json, os, signal, sys, time
args = sys.argv[1:]
with open(os.environ["EVENT_FILE"], "a") as f:
    f.write(json.dumps(["docker"] + args) + "\n")
if "inspect" in args:
    print(os.environ["FAKE_IMAGE_ID"] if "{{.Image}}" in args else "true")
    raise SystemExit(0)
if "stop" not in args:
    raise SystemExit("unexpected fake docker operation")
if os.environ.get("STOP_FAIL") == "1":
    raise SystemExit(1)
for _ in range(100):
    try:
        with open(os.environ["RUNNER_PID_FILE"]) as f:
            runner_pid = int(f.read())
        break
    except (FileNotFoundError, ValueError):
        time.sleep(0.01)
else:
    raise SystemExit("missing runner PID")
# Give the shell time to enter its background-child wait, then behave like
# docker stop sending TERM. Keep the command alive briefly after the signal.
time.sleep(0.05)
os.kill(runner_pid, signal.SIGTERM)
time.sleep(0.05)
''',
            "sleep": r'''
import json, os, signal, sys, time
if sys.argv[1:] == ["infinity"]:
    with open(os.environ["EVENT_FILE"], "a") as f:
        f.write(json.dumps(["waiting"]) + "\n")
    while True:
        signal.pause()
time.sleep(0.01)
''',
            "mountpoint": "raise SystemExit(0)\n",
            "sync": "raise SystemExit(0)\n",
            "sha256sum": "import hashlib, sys\nprint(hashlib.sha256(sys.stdin.buffer.read()).hexdigest() + '  -')\n",
        }
        for name, body in scripts.items():
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + body.lstrip())
            path.chmod(0o755)

    def events_read(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def actions_read(self):
        return self.action_file.read_text().splitlines() if self.action_file.exists() else []

    def actions(self, returncode=0):
        return (
            "printf 'executed\\n' >> " + shlex.quote(str(self.action_file)) + "\n"
            + "exit " + str(returncode) + "\n"
        )

    def start_runner(self, returncode=0, stop_fail=False):
        runner = (ROOT / "runner.sh.in").read_text(encoding="utf-8").replace(
            "__HOST_JOB__", self.actions(returncode).rstrip()
        )
        path = self.root / "runner.sh"
        path.write_text(runner)
        env = self.env | ({"STOP_FAIL": "1"} if stop_fail else {})
        process = subprocess.Popen(
            ["/bin/sh", str(path)], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        self.pid_file.write_text(str(process.pid))
        self.addCleanup(self.stop_process_group, process)
        return process

    @staticmethod
    def stop_process_group(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=2)
        if process.stdout:
            process.stdout.close()

    def docker_stops(self):
        return [e for e in self.events_read() if e[0] == "docker" and "stop" in e]

    def test_successful_job_stops_and_returns_business_status(self):
        process = self.start_runner()
        output, _ = process.communicate(timeout=4)
        self.assertEqual(process.returncode, 0, output)
        self.assertEqual(self.actions_read(), ["executed"])
        self.assertEqual(len(self.docker_stops()), 1)
        self.assertNotIn(["waiting"], self.events_read())

    def test_failed_job_still_stops_once_and_preserves_failure_status(self):
        process = self.start_runner(returncode=23)
        output, _ = process.communicate(timeout=4)
        self.assertEqual(process.returncode, 23, output)
        self.assertEqual(self.actions_read(), ["executed"])
        self.assertEqual(len(self.docker_stops()), 1)
        self.assertNotIn(["waiting"], self.events_read())

    def test_stop_failure_waits_without_repeating_job_and_can_be_terminated(self):
        process = self.start_runner(stop_fail=True)
        deadline = time.monotonic() + 4
        while ["waiting"] not in self.events_read():
            if process.poll() is not None or time.monotonic() > deadline:
                self.fail("runner did not enter fallback wait")
            time.sleep(0.01)
        # Verify it remains alive after entering the wait instead of returning
        # normally and asking the restart policy to repeat the business job.
        time.sleep(0.12)
        self.assertIsNone(process.poll())
        self.assertEqual(self.actions_read(), ["executed"])
        self.assertEqual(len(self.docker_stops()), 1)
        process.terminate()
        output, _ = process.communicate(timeout=2)
        self.assertEqual(process.returncode, -signal.SIGTERM, output)

    def fingerprint(self, mode="diagnostic", ui_dir="", bind="0.0.0.0", port="18732",
                    dev_no_pairing="0", dev_allow_ips="", dev_minutes="60", dev_commands="0"):
        values = ("r1-boot-once-test", mode, ui_dir, bind, port,
                  "pair", dev_no_pairing, dev_allow_ips, dev_minutes, dev_commands)
        end = chr(10)
        return hashlib.sha256((end.join(values) + end).encode()).hexdigest()

    def run_host_job(self, returncode=0, image=None, mode=None, ui_dir=None, web_port=None):
        # Only these two host paths are redirected in this temporary copy.
        # All real payload operations are replaced by a harmless event append.
        script = (ROOT / "host-job.sh.in").read_text(encoding="utf-8").replace(
            "state_dir=/run/r1-toolbox",
            "state_dir=" + shlex.quote(str(self.state_dir)),
        ).replace(
            "cat /proc/sys/kernel/random/boot_id",
            "cat " + shlex.quote(str(self.boot_file)),
        ).replace("__SCREEN_ACTIONS__", self.actions(returncode).rstrip())
        env = self.env.copy()
        if image is not None:
            env["FAKE_IMAGE_ID"] = image
        if mode is not None:
            env["UI_MODE"] = mode
        if ui_dir is not None:
            env["UI_DIR"] = ui_dir
        if web_port is not None:
            env["R1_WEB_PORT"] = web_port
        return subprocess.run(
            ["/bin/sh", "-s"], input=script, env=env,
            capture_output=True, text=True, timeout=4,
        )

    def test_same_boot_and_fingerprint_skip_after_success(self):
        first = self.run_host_job()
        second = self.run_host_job()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.actions_read(), ["executed"])
        expected = f"boot-a:{self.fingerprint()}:sha256:" + "1" * 64 + ":diagnostic::0.0.0.0:18732" + chr(10)
        self.assertEqual((self.state_dir / "last-success").read_text(), expected)

    def test_new_boot_runs_again_with_same_fingerprint(self):
        first = self.run_host_job()
        self.boot_file.write_text("boot-b" + chr(10))
        second = self.run_host_job()
        for result in (first, second):
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.actions_read(), ["executed"] * 2)
        expected = f"boot-b:{self.fingerprint()}:sha256:" + "1" * 64 + ":diagnostic::0.0.0.0:18732" + chr(10)
        self.assertEqual((self.state_dir / "last-success").read_text(), expected)

    def test_changed_web_configuration_runs_again_in_the_same_boot(self):
        for port in ("18732", "18733", "0"):
            result = self.run_host_job(web_port=port)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.actions_read(), ["executed"] * 3)
        self.assertTrue((self.state_dir / "last-success").read_text().endswith(":0.0.0.0:0\n"))

    def test_failure_does_not_mark_success_and_can_be_retried(self):
        failed = self.run_host_job(returncode=23)
        self.assertEqual(failed.returncode, 23, failed.stderr)
        self.assertFalse((self.state_dir / "last-success").exists())
        retry = self.run_host_job()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.actions_read(), ["executed", "executed"])
        self.assertEqual((self.state_dir / "last-success").read_text(),
                         f"boot-a:{self.fingerprint()}:sha256:" + "1" * 64 + ":diagnostic::0.0.0.0:18732" + chr(10))

    def test_new_image_in_the_same_boot_runs_without_config_change(self):
        first = self.run_host_job()
        second = self.run_host_job(image="sha256:" + "2" * 64)
        third = self.run_host_job(image="sha256:" + "2" * 64)
        self.assertTrue(all(r.returncode == 0 for r in (first, second, third)))
        self.assertEqual(self.actions_read(), ["executed", "executed"])

    def test_mode_and_source_changes_have_separate_success_keys(self):
        results = [self.run_host_job(mode="image"),
                   self.run_host_job(mode="directory", ui_dir="/drives/data/ui"),
                   self.run_host_job(mode="directory", ui_dir="/drives/data/ui-next")]
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertEqual(self.actions_read(), ["executed"] * 3)

    def test_invalid_image_id_cannot_mark_success_or_execute_actions(self):
        result = self.run_host_job(image="sha256:wrong")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.actions_read(), [])
        self.assertFalse((self.state_dir / "last-success").exists())

    def test_failed_update_cannot_skip_a_return_to_the_previous_image(self):
        first = self.run_host_job(mode="image")
        failed = self.run_host_job(mode="image", image="sha256:" + "2" * 64, returncode=23)
        self.assertFalse((self.state_dir / "last-success").exists())
        restored = self.run_host_job(mode="image")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(failed.returncode, 23)
        self.assertEqual(restored.returncode, 0)
        self.assertEqual(self.actions_read(), ["executed"] * 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

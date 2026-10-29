#!/usr/bin/env python3
"""Exercise resident shell supervision without Docker, systemd or NAS access."""
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent


class ResidentContractTests(unittest.TestCase):
    def test_host_bootstrap_does_not_skip_runtime_or_wait_for_a_display(self):
        host = (ROOT / 'host-job.sh.in').read_text(encoding='utf-8')
        self.assertIn('__SCREEN_ACTIONS__', host)
        self.assertNotIn('last-success', host)
        self.assertNotIn('mountpoint -q /screen', host)
        payload = (ROOT / 'screen-actions.sh').read_text(encoding='utf-8')
        self.assertIn('sys.exit(resident_main())', payload)
        self.assertIn('os.execve(command[0], command, environment)', payload)

    def test_runner_has_a_real_child_instead_of_self_stop_or_infinite_sleep(self):
        runner = (ROOT / 'runner.sh.in').read_text(encoding='utf-8')
        self.assertIn('nsenter --no-fork', runner)
        self.assertIn('wait "$child_pid"', runner)
        self.assertIn('kill -TERM "$child_pid"', runner)
        self.assertNotIn('sleep infinity', runner)
        self.assertNotIn('host_docker stop', runner)


@unittest.skipUnless(sys.platform.startswith('linux'), '实际信号／POSIX shell 验收在 Linux CI 执行')
class ResidentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='r1-resident-runner-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.events = self.root / 'events'
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.defpath,
                        FAKE_BIN=str(self.bin), EVENT_FILE=str(self.events), TASK_CONTAINER='r1-toolbox-fixture')
        scripts = {
            'chroot': '''import os, sys
script = sys.argv[4]
if "exec docker " in script:
    os.execv(os.environ['FAKE_BIN'] + '/docker', ['docker'] + sys.argv[6:])
if "exec nsenter " in script:
    os.execv(os.environ['FAKE_BIN'] + '/nsenter', ['nsenter'])
raise SystemExit('unexpected chroot command')
''',
            'nsenter': '''import os
os.execv('/bin/sh', ['/bin/sh', '-s'])
''',
            'docker': '''import os, sys
with open(os.environ['EVENT_FILE'], 'a') as stream:
    stream.write('docker:' + ' '.join(sys.argv[1:]) + '\\n')
if 'inspect' not in sys.argv or os.environ.get('DOCKER_FAIL') == '1':
    raise SystemExit(1)
print('true')
''',
            'sleep': 'import time\ntime.sleep(0.01)\n',
        }
        for name, body in scripts.items():
            path = self.bin / name
            path.write_text('#!' + sys.executable + '\n' + body, encoding='utf-8')
            path.chmod(0o755)

    def read_events(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def start(self, body, **environment):
        runner = (ROOT / 'runner.sh.in').read_text(encoding='utf-8').replace('__HOST_JOB__', body)
        path = self.root / 'runner.sh'
        path.write_text(runner, encoding='utf-8')
        process = subprocess.Popen(['/bin/sh', str(path)], env=self.env | environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()

    def body(self):
        path = self.root / 'resident.py'
        path.write_text('''import os, signal, threading, time
def record(value):
    with open(os.environ['EVENT_FILE'], 'a') as stream:
        stream.write(value + '\\n')
stopped = threading.Event()
signal.signal(signal.SIGTERM, lambda signum, frame: stopped.set())
record('started')
stopped.wait()
record('term')
time.sleep(.06)
record('restored')
''', encoding='utf-8')
        return 'exec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(path))

    def wait_started(self, process):
        deadline = time.monotonic() + 3
        while 'started' not in self.read_events():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail('resident child did not start: ' + str(process.poll()))
            time.sleep(.01)

    def test_container_remains_running_and_term_waits_for_real_child_restoration(self):
        process = self.start(self.body())
        self.wait_started(process)
        self.assertIsNone(process.poll())
        process.terminate()
        output, _ = process.communicate(timeout=4)
        self.assertEqual(process.returncode, 0, output)
        self.assertEqual(self.read_events()[-2:], ['term', 'restored'])
        self.assertFalse(any(' stop ' in value for value in self.read_events()))

    def test_child_failure_is_propagated_instead_of_waiting_as_if_healthy(self):
        process = self.start('exit 23')
        output, _ = process.communicate(timeout=4)
        self.assertEqual(process.returncode, 23, output)

    def test_unexpected_clean_child_exit_is_a_runtime_failure(self):
        process = self.start('exit 0')
        output, _ = process.communicate(timeout=4)
        self.assertEqual(process.returncode, 1, output)

    def test_unavailable_docker_exits_nonzero_without_running_the_child(self):
        process = self.start(self.body(), DOCKER_FAIL='1')
        output, _ = process.communicate(timeout=4)
        self.assertEqual(process.returncode, 1, output)
        self.assertNotIn('started', self.read_events())

    def test_each_container_restart_starts_its_runtime_without_a_boot_success_skip(self):
        for _ in range(2):
            before = self.read_events().count('started')
            process = self.start(self.body())
            deadline = time.monotonic() + 3
            while self.read_events().count('started') == before:
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail('restart did not start resident runtime')
                time.sleep(.01)
            process.terminate()
            output, _ = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 0, output)
        self.assertEqual(self.read_events().count('started'), 2)
        self.assertEqual(self.read_events().count('restored'), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)

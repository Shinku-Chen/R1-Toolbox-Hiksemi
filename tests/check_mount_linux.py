#!/usr/bin/env python3
"""Exercise the real bind helpers on a disposable Linux CI host, without NAS files."""

import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def wait_for_binding(runtime, unit, source):
    """Wait for Type=simple to finish the child's mount namespace setup."""
    deadline = time.monotonic() + 10
    last = {}
    while time.monotonic() < deadline:
        result = subprocess.run(
            ['systemctl', 'show', unit, '--property=MainPID', '--property=ActiveState'],
            capture_output=True, text=True, timeout=5)
        last = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        value = last.get('MainPID', '')
        if result.returncode == 0 and last.get('ActiveState') == 'active' and value.isdigit():
            pid = int(value)
            if pid > 0 and runtime['verify_binding'](pid, source):
                return pid
        time.sleep(0.1)
    raise AssertionError('Running service did not get the read-only bind within 10 seconds: ' + str(last))


def main():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise SystemExit('此检查需要临时 Linux CI 主机和 root，不在 NAS 上执行。')
    script = Path(__file__).resolve().parents[1] / 'tools/boot-once/screen-actions.sh'
    payload = script.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    runtime = {'__name__': 'r1_mount_ci'}
    exec(compile(payload, str(script), 'exec'), runtime)

    fixture = Path(tempfile.mkdtemp(prefix='r1-toolbox-bind-ci-'))
    source, target = fixture / 'custom', fixture / 'app/resources/app/dist'
    source.mkdir()
    target.mkdir(parents=True)
    (source / 'index.html').write_text('custom')
    official = target / 'index.html'
    official.write_text('official')
    original = official.stat()
    runtime['app'], runtime['target'] = fixture / 'app', target
    unit = 'r1-toolbox-bind-ci-' + str(os.getpid()) + '-' + str(time.monotonic_ns())
    started = False
    try:
        runtime['preflight_binding'](unit, source)
        assert official.read_text() == 'official', 'Preflight changed the host view'
        subprocess.run(runtime['service_options'](unit, source) + ['/usr/bin/sleep', '30'],
                       check=True, timeout=15)
        started = True
        pid = wait_for_binding(runtime, unit, source)
        assert runtime['verify_binding'](pid, source), 'Running service did not get the read-only bind'
        mounted = Path('/proc', str(pid), 'root') / str(target).lstrip('/')
        assert (mounted / 'index.html').read_text() == 'custom'
        try:
            (mounted / 'index.html').write_text('must fail')
        except OSError as error:
            assert error.errno == errno.EROFS, error
        else:
            raise AssertionError('The service mount allowed a write')
        (source / 'index.html').write_text('updated custom')
        assert (mounted / 'index.html').read_text() == 'updated custom'
        assert official.read_text() == 'official', 'The host original was changed'
        assert (official.stat().st_dev, official.stat().st_ino) == (original.st_dev, original.st_ino)
        print('PASS: real systemd preflight, private mount, read-only enforcement, process view and unchanged host files')
    finally:
        if started:
            subprocess.run(['systemctl', 'stop', unit], check=True, timeout=15)
        print('CI 临时测试目录保留至 runner 回收：', fixture)


if __name__ == '__main__':
    main()

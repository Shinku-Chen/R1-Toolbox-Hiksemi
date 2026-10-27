#!/usr/bin/env python3
"""Verify the built image's RAM UI and host API on a disposable Linux runner."""

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
PORT = 18731
STATUS_URL = f'http://127.0.0.1:{PORT}/api/status'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def command(*arguments, check=True, timeout=20):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('CI 命令失败：' + ' '.join(arguments) + '\n' + result.stdout + result.stderr)
    return result


def cleanup_command(*arguments):
    try:
        return command(*arguments, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        print('[WARN] 测试资源清理命令未完成：', arguments[0], str(error))
        return None


def assert_port_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        connection.bind(('127.0.0.1', PORT))
        connection.listen(1)


def wait_for_port_release():
    deadline = time.monotonic() + 5
    while True:
        try:
            assert_port_free()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise AssertionError('停止测试服务后，状态 API 端口仍未释放') from None
            time.sleep(0.1)


def wait_for_status():
    deadline = time.monotonic() + 10
    last_error = '状态 API 未响应'
    while time.monotonic() < deadline:
        try:
            with HTTP.open(STATUS_URL, timeout=1) as response:
                status = json.load(response)
                if (response.status == 200 and isinstance(status, dict)
                        and status.get('schema_version') == 1
                        and status.get('available') is True):
                    return status
                last_error = '状态 API 未返回 schema_version=1 与 available=true'
        except (OSError, urllib.error.URLError, ValueError) as error:
            last_error = str(error)
        time.sleep(0.1)
    raise AssertionError('状态 API 检查超时：' + last_error)


def service_pid(unit):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = command('systemctl', 'show', unit, '--property=MainPID', '--value', check=False)
        if result.returncode == 0 and result.stdout.strip().isdigit():
            pid = int(result.stdout.strip())
            if pid > 0 and Path('/proc', str(pid)).exists():
                return pid
        time.sleep(0.1)
    raise AssertionError('测试服务没有产生存活的 MainPID')


def check_view(runtime, pid, frontend, expected_hash, original, original_identity):
    assert runtime['verify_binding'](pid, frontend), '测试服务没有取得预期的只读 UI 挂载'
    mounted = Path('/proc', str(pid), 'root') / str(runtime['target']).lstrip('/')
    page = mounted / 'index.html'
    assert hashlib.sha256(page.read_bytes()).hexdigest() == expected_hash, '服务中的 UI 与镜像清单不一致'
    assert os.statvfs(mounted).f_flag & os.ST_RDONLY, '服务 UI 挂载没有只读标志'
    try:
        page.write_bytes(b'this write must fail')
    except OSError as error:
        assert error.errno == errno.EROFS, 'UI 写入没有因 EROFS 被拒绝：' + str(error)
    else:
        raise AssertionError('服务中的 UI 挂载允许写入')
    assert original.read_bytes() == b'official', '宿主原始页面发生变化'
    assert (original.stat().st_dev, original.stat().st_ino) == original_identity, '宿主原始页面被替换'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='本次 CI 构建的完整 SHA 镜像引用')
    args = parser.parse_args()
    # This test deliberately starts host services. Restrict it to the hosted CI
    # checkout layout rather than offering a runnable maintenance command on NAS.
    ci_checkout = Path('/home/runner/work') in ROOT.parents
    if (sys.platform != 'linux' or os.geteuid() != 0 or not ci_checkout
            or not Path('/run/systemd/system').is_dir() or Path('/screen').exists()):
        parser.exit(1, '此检查仅允许临时 GitHub Linux CI runner 的 root 执行，不能在 NAS 或本机运行。\n')
    if not re.fullmatch(r'ghcr\.io/shinku-chen/r1-toolbox:sha-[0-9a-f]{40,64}', args.image):
        parser.error('--image 必须是本项目完整 SHA 标签')
    if not shutil.which('docker') or not shutil.which('systemd-run'):
        parser.error('CI runner 缺少 Docker 或 systemd-run')
    assert_port_free()
    command('docker', 'image', 'inspect', args.image)

    script = ROOT / 'tools/boot-once/screen-actions.sh'
    payload = script.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    runtime = {'__name__': 'r1_image_ui_ci'}
    exec(compile(payload, str(script), 'exec'), runtime)
    runtime['require_tmpfs'](Path('/run'))
    runtime_root = Path(tempfile.mkdtemp(prefix='r1-toolbox-image-ci-', dir='/run'))
    runtime['runtime_root'] = runtime_root

    fixture = Path(tempfile.mkdtemp(prefix='r1-toolbox-image-ui-ci-'))
    target = fixture / 'app/resources/app/dist'
    target.mkdir(parents=True)
    original = target / 'index.html'
    original.write_bytes(b'official')
    original_identity = (original.stat().st_dev, original.stat().st_ino)
    app = fixture / 'app'
    app_run = app / 'AppRun'
    app_run.write_text('#!/bin/sh\nexec /usr/bin/sleep 60\n', encoding='utf-8')
    app_run.chmod(0o755)
    runtime['app'], runtime['target'] = app, target

    unique = str(os.getpid()) + '-' + str(time.monotonic_ns())
    container = 'r1-toolbox-image-ci-' + unique
    unit = 'r1-toolbox-image-ui-ci-' + unique
    container_attempted = service_attempted = False
    try:
        container_attempted = True
        command('docker', 'run', '--detach', '--pull=never', '--name', container,
                '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--pids-limit', '32',
                '--entrypoint', '/bin/sh', args.image, '-c', 'sleep 120')
        cache = runtime['prepare_image_bundle'](container)
        tarhash = cache.name.removeprefix('ui-')
        runtime['verify_cache'](cache, tarhash)
        manifest, _ = runtime['read_bundle'](cache / 'ui.tar', tarhash)
        frontend = cache / 'frontend'
        expected_hash = manifest['files']['frontend/index.html']
        runtime['require_tmpfs'](cache)
        runtime['preflight_binding'](unit, frontend)
        assert original.read_bytes() == b'official', '挂载预检修改了宿主原始页面'

        service_attempted = True
        command(*runtime['service_options'](unit, frontend), sys.executable, '-B',
                str(cache / 'runtime/ui_service.py'), '--app', str(app))
        # Type=simple publishes MainPID before the child finishes setting up its
        # mount namespace. The API proves that our backend has actually exec'd.
        wait_for_status()
        pid = service_pid(unit)
        check_view(runtime, pid, frontend, expected_hash, original, original_identity)

        # The UI and API belong to the host service, not this source container.
        command('docker', 'stop', '--timeout', '2', container)
        command('docker', 'rm', container)
        container_attempted = False
        assert command('docker', 'inspect', container, check=False).returncode != 0, '测试容器未被移除'
        assert command('systemctl', 'is-active', unit).stdout.strip() == 'active', '停止容器导致宿主 UI 服务停止'
        assert service_pid(unit) == pid, '停止容器后宿主 UI 服务被意外重启'
        wait_for_status()
        check_view(runtime, pid, frontend, expected_hash, original, original_identity)
        runtime['verify_cache'](cache, tarhash)

        command('systemctl', 'stop', unit)
        service_attempted = False
        wait_for_port_release()
        assert original.read_bytes() == b'official', '停止服务后宿主原始页面发生变化'
        assert (original.stat().st_dev, original.stat().st_ino) == original_identity, '宿主原始页面标识发生变化'
        print('PASS: built image Docker copy, verified tmpfs UI cache, real read-only service mount, '
              'live API surviving container stop/removal, released API port and unchanged host files')
    finally:
        if service_attempted:
            logs = cleanup_command('journalctl', '--unit', unit, '--no-pager', '--lines', '40')
            if logs is not None and logs.stdout:
                print(logs.stdout)
            cleanup_command('systemctl', 'stop', unit)
        cleanup_command('systemctl', 'stop', unit + '-check')
        if container_attempted:
            cleanup_command('docker', 'rm', '--force', container)
        print('CI 临时测试目录保留至 runner 回收：', fixture, runtime_root)


if __name__ == '__main__':
    main()

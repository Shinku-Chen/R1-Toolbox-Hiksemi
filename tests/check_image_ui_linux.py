#!/usr/bin/env python3
"""Verify the built image's RAM UI and host API on a disposable Linux runner."""

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
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
WEB_PORT = 18732
STATUS_URL = f'http://127.0.0.1:{PORT}/api/status'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FAN_FIXTURE_CONFIG = {'schema_version': 1, 'channels': {
    'cpu': {'mode': 'temperature', 'sources': ['ci:cpu-package', 'ci:disk-a'],
            'quiet_below': 33, 'normal_at': 57, 'full_at': 74,
            'quiet_percent': 55, 'normal_percent': 79, 'manual_percent': 70},
    'system': {'mode': 'temperature', 'sources': ['ci:disk-a', 'ci:disk-b'],
               'quiet_below': 28, 'normal_at': 43, 'full_at': 58,
               'quiet_percent': 60, 'normal_percent': 80, 'manual_percent': 75},
}}
LIGHT_FIXTURE_CONFIG = {'schema_version': 1, 'enabled': False, 'mode': 'temperature',
    'sources': ['ci:cpu-package', 'ci:disk-a'],
    'stops': [{'temperature': 30, 'color': '#20BFA9'}, {'temperature': 55, 'color': '#EDB353'},
              {'temperature': 75, 'color': '#E36464'}],
    'brightness': 65, 'manual': {'mode': 1, 'color': '#208070', 'brightness': 40},
    'schedule': {'enabled': True, 'start': '22:30', 'end': '06:30', 'brightness': 15}}


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
    for port in (PORT, WEB_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            connection.bind(('127.0.0.1', port))
            connection.listen(1)


def wait_for_port_release():
    deadline = time.monotonic() + 5
    while True:
        try:
            assert_port_free()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise AssertionError('停止测试服务后，状态或网页端口仍未释放') from None
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


def check_web_config():
    with HTTP.open(f'http://127.0.0.1:{WEB_PORT}/', timeout=2) as response:
        assert response.status == 200 and b'<!doctype html>' in response.read().lower(), '网页监控页面不可用'
    with HTTP.open(f'http://127.0.0.1:{WEB_PORT}/login.html', timeout=2) as response:
        assert response.status == 200 and len(response.read()) > 100, '登录页未交付'
    request = urllib.request.Request(f'http://127.0.0.1:{WEB_PORT}/api/v1/status',
                                    headers={'Origin': 'https://third-party.example'})
    with HTTP.open(request, timeout=2) as response:
        value = json.load(response)
        assert response.status == 200 and value.get('schema_version') == 1, '未配对结构化监控接口不可用'
        assert response.headers.get('Access-Control-Allow-Origin') == '*', '公开只读接口未允许第三方读取'
        assert 'csrf' not in value and 'settings' not in value.get('controls', {}), '公开状态包含设置授权材料'
    try:
        HTTP.open(f'http://127.0.0.1:{WEB_PORT}/api/session', timeout=2)
    except urllib.error.HTTPError as error:
        assert error.code == 401, '未登录网页没有明确拒绝设置访问'
    else:
        raise AssertionError('未配对网页可以读取设备设置')


def save_fixture_settings(token_file):
    deadline, token = time.monotonic() + 5, ''
    while time.monotonic() < deadline:
        try:
            token = token_file.read_text().strip()
        except OSError:
            token = ''
        if re.fullmatch(r'[0-9a-f]{64}', token):
            break
        time.sleep(0.05)
    assert re.fullmatch(r'[0-9a-f]{64}', token), '测试小屏没有收到控制令牌'
    body = json.dumps({'action': 'settings.update', 'settings': {
        'theme': 'jade', 'font_scale': 120}}).encode()
    request = urllib.request.Request(f'http://127.0.0.1:{PORT}/api/action', data=body,
                                    headers={'Content-Type': 'application/json', 'X-R1-Token': token,
                                             'Origin': 'null'})
    with HTTP.open(request, timeout=3) as response:
        assert response.status == 200 and json.load(response).get('ok') is True, '测试设置没有保存成功'
    return token


def check_native_bundle(cache, manifest):
    name = 'runtime/fan_guard.so'
    expected = manifest.get('files', {}).get(name)
    assert isinstance(expected, str), '镜像 UI 清单缺少风扇保护适配层'
    path = cache / name
    assert not path.is_symlink() and path.is_file(), '风扇保护适配层不是普通文件'
    value = path.read_bytes()
    assert hashlib.sha256(value).hexdigest() == expected, '风扇保护适配层与镜像清单不一致'
    assert (len(value) >= 64 and value[:7] == b'\x7fELF\x02\x01\x01'
            and value[16:20] == b'\x03\x00\x3e\x00'), '镜像未包含 Linux x86-64 ELF 共享库'


def read_fixture_fan_policy():
    with HTTP.open(f'http://127.0.0.1:{PORT}/api/fan-policy', timeout=2) as response:
        assert response.status == 200, '独立温控设置接口不可用'
        state = json.load(response)
    # Hosted CI has no verified NAS firmware/device. Save-only must work here
    # without applying a rule or attempting to start the native watchdog.
    assert state.get('capability', {}).get('available') is False, 'CI fixture 意外识别为可接管硬件'
    assert state.get('active') is False and state.get('busy') is False, '仅保存配置触发了风扇接管'
    assert state.get('active_on_boot') is False, '仅保存配置意外启用了开机接管'
    return state


def save_fixture_fan_policy(token, data_directory):
    read_fixture_fan_policy()
    body = json.dumps({'action': 'fan.policy.update', 'config': FAN_FIXTURE_CONFIG, 'apply': False}).encode()
    request = urllib.request.Request(f'http://127.0.0.1:{PORT}/api/action', data=body,
                                    headers={'Content-Type': 'application/json', 'X-R1-Token': token,
                                             'Origin': 'null'})
    with HTTP.open(request, timeout=3) as response:
        result = json.load(response)
        assert (response.status == 200 and result.get('ok') is True
                and result.get('settings_saved') is True and result.get('applied') is False
                and result.get('pending') is False), '两路温控配置没有明确完成仅保存'
    check_fixture_fan_policy(data_directory)


def check_fixture_fan_policy(data_directory):
    state = read_fixture_fan_policy()
    assert state.get('config') == FAN_FIXTURE_CONFIG, '重建后没有读回两路独立温控配置'
    assert state.get('pending_changes') is True, '仅保存配置没有保持待应用状态'
    path = data_directory / 'fan-policy.json'
    assert path.is_file() and not path.is_symlink(), '数据卷没有保存普通温控配置文件'
    saved = json.loads(path.read_text())
    assert saved.get('config') == FAN_FIXTURE_CONFIG, '数据卷内温控配置与保存结果不一致'
    assert saved.get('resume_on_boot') is False, '未应用的温控配置意外持久启用'
    active = saved.get('active_config', {}).get('channels', {})
    assert (set(active) == {'cpu', 'system'}
            and all(item.get('mode') == 'vendor' for item in active.values())), '仅保存覆盖了原厂活动策略'


def save_fixture_auxiliary_settings(token, data_directory):
    for action, payload in (
        ('light.policy.update', {'config': LIGHT_FIXTURE_CONFIG, 'apply': False}),
        ('monitor.settings.update', {'settings': {'retention_days': 7}}),
    ):
        body = json.dumps({'action': action, **payload}).encode()
        request = urllib.request.Request(f'http://127.0.0.1:{PORT}/api/action', data=body,
            headers={'Content-Type': 'application/json', 'X-R1-Token': token, 'Origin': 'null'})
        with HTTP.open(request, timeout=3) as response:
            value = json.load(response)
            assert response.status == 200 and value.get('settings_saved') is True, '灯光或统计设置未明确保存'
    check_fixture_auxiliary_settings(data_directory)


def check_fixture_auxiliary_settings(data_directory):
    with HTTP.open(f'http://127.0.0.1:{PORT}/api/light-policy', timeout=2) as response:
        value = json.load(response)
        assert value.get('config') == LIGHT_FIXTURE_CONFIG and value.get('active') is False, '灯光草稿未保留或意外执行'
    with HTTP.open(f'http://127.0.0.1:{PORT}/api/monitoring', timeout=2) as response:
        assert json.load(response).get('settings', {}).get('retention_days') == 7, '统计配置没有从卷恢复'
    for name in ('light-policy.json', 'monitoring.json'):
        assert (data_directory / name).is_file(), '数据卷缺少灯光或统计配置：' + name


def check_fixture_settings():
    with HTTP.open(f'http://127.0.0.1:{PORT}/api/toolbox', timeout=2) as response:
        settings = json.load(response).get('settings', {})
        assert settings.get('theme') == 'jade' and settings.get('font_scale') == 120, '重建服务没有读回持久化设置'


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
    token_file = fixture / 'fixture-control-token'
    app_run.write_text('#!/bin/sh\numask 077\nprintf %s "$R1_TOOLBOX_CONTROL_TOKEN" > '
                       + shlex.quote(str(token_file)) + '\nexec /usr/bin/sleep 60\n', encoding='utf-8')
    app_run.chmod(0o755)
    runtime['app'], runtime['target'] = app, target

    unique = str(os.getpid()) + '-' + str(time.monotonic_ns())
    container = 'r1-toolbox-image-ci-' + unique
    volume = container + '-data'
    unit = 'r1-toolbox-image-ui-ci-' + unique
    container_attempted = service_attempted = False
    try:
        container_attempted = True
        command('docker', 'run', '--detach', '--pull=never', '--name', container,
                '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--pids-limit', '32',
                '--mount', 'type=volume,source=' + volume + ',target=/data',
                '--entrypoint', '/bin/sh', args.image, '-c', 'sleep 120')
        data_directory = runtime['settings_directory'](container, required=True)
        cache = runtime['prepare_image_bundle'](container)
        tarhash = cache.name.removeprefix('ui-')
        runtime['verify_cache'](cache, tarhash)
        manifest, _ = runtime['read_bundle'](cache / 'ui.tar', tarhash)
        check_native_bundle(cache, manifest)
        frontend = cache / 'frontend'
        expected_hash = manifest['files']['frontend/index.html']
        runtime['require_tmpfs'](cache)
        runtime['preflight_binding'](unit, frontend)
        assert original.read_bytes() == b'official', '挂载预检修改了宿主原始页面'

        service_attempted = True
        command(*runtime['service_options'](unit, frontend), '--setenv=R1_WEB_BIND=127.0.0.1',
                '--setenv=R1_WEB_PORT=' + str(WEB_PORT), '--setenv=R1_TOOLBOX_DATA_DIR=' + str(data_directory), sys.executable, '-B',
                str(cache / 'runtime/ui_service.py'), '--app', str(app))
        # Type=simple publishes MainPID before the child finishes setting up its
        # mount namespace. The API proves that our backend has actually exec'd.
        wait_for_status()
        check_web_config()
        pid = service_pid(unit)
        check_view(runtime, pid, frontend, expected_hash, original, original_identity)
        token = save_fixture_settings(token_file)
        check_fixture_settings()
        save_fixture_fan_policy(token, data_directory)
        save_fixture_auxiliary_settings(token, data_directory)

        # The UI and API belong to the host service, not this source container.
        command('docker', 'stop', '--timeout', '2', container)
        command('docker', 'rm', container)
        container_attempted = False
        assert command('docker', 'inspect', container, check=False).returncode != 0, '测试容器未被移除'
        assert command('systemctl', 'is-active', unit).stdout.strip() == 'active', '停止容器导致宿主 UI 服务停止'
        assert service_pid(unit) == pid, '停止容器后宿主 UI 服务被意外重启'
        wait_for_status()
        check_web_config()
        check_fixture_settings()
        check_fixture_fan_policy(data_directory)
        check_fixture_auxiliary_settings(data_directory)
        check_view(runtime, pid, frontend, expected_hash, original, original_identity)
        runtime['verify_cache'](cache, tarhash)

        command('systemctl', 'stop', unit)
        service_attempted = False
        wait_for_port_release()
        assert (data_directory / 'settings.json').is_file(), '容器删除后设置数据卷文件丢失'
        assert (data_directory / 'fan-policy.json').is_file(), '容器删除后温控设置数据卷文件丢失'
        assert command('docker', 'volume', 'inspect', volume).returncode == 0, '容器删除误删了设置数据卷'
        # Recreate a host service with the retained volume, without the source
        # container. This verifies the actual storage boundary, not a mock save.
        unit += '-reload'
        service_attempted = True
        command(*runtime['service_options'](unit, frontend), '--setenv=R1_WEB_BIND=127.0.0.1',
                '--setenv=R1_WEB_PORT=' + str(WEB_PORT), '--setenv=R1_TOOLBOX_DATA_DIR=' + str(data_directory),
                sys.executable, '-B', str(cache / 'runtime/ui_service.py'), '--app', str(app))
        wait_for_status()
        check_fixture_settings()
        check_fixture_fan_policy(data_directory)
        check_fixture_auxiliary_settings(data_directory)
        check_web_config()
        check_view(runtime, service_pid(unit), frontend, expected_hash, original, original_identity)
        command('systemctl', 'stop', unit)
        service_attempted = False
        wait_for_port_release()
        assert original.read_bytes() == b'official', '停止服务后宿主原始页面发生变化'
        assert (original.stat().st_dev, original.stat().st_ino) == original_identity, '宿主原始页面标识发生变化'
        print('PASS: built image Docker copy, verified tmpfs UI cache and x86-64 native guard, real read-only service mount, '
              'live API and authenticated web config surviving container stop/removal, '
              'persistent Docker volume settings and independent save-only fan rules after service recreation '
              'without hardware capability or takeover, released ports and unchanged host files')
    finally:
        if service_attempted:
            logs = cleanup_command('journalctl', '--unit', unit, '--no-pager', '--lines', '40')
            if logs is not None and logs.stdout:
                print(logs.stdout)
            cleanup_command('systemctl', 'stop', unit)
        cleanup_command('systemctl', 'stop', unit + '-check')
        if container_attempted:
            cleanup_command('docker', 'rm', '--force', container)
        cleanup_command('docker', 'volume', 'rm', volume)
        print('CI 临时测试目录保留至 runner 回收：', fixture, runtime_root)


if __name__ == '__main__':
    main()

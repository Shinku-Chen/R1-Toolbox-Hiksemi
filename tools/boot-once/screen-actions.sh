#!/bin/sh
# Run in the host namespaces/root. Empty NEW_HTML means diagnostics only.
set -eu
command -v python3 >/dev/null || { echo '宿主缺少 python3，未修改小屏'; exit 1; }
exec python3 - <<'PY'
import os, pathlib, shutil, signal, subprocess, sys, time

app = pathlib.Path('/screen/squashfs-root')
page = app / 'resources/app/dist/index.html'
source = os.environ.get('NEW_HTML', '')
unit = 'histor-custom-' + str(os.getpid())
base_env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'}

def require(condition, message):
    if not condition:
        raise ValueError(message)

def run(*args, env=None):
    try:
        return subprocess.run(args, env=env or base_env, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('宿主命令失败或超时：' + args[0]) from None

def processes():
    found = []
    for p in pathlib.Path('/proc').glob('[0-9]*'):
        try:
            exe = os.readlink(p / 'exe')
            if pathlib.Path(exe).name != 'hiksemi-sub':
                continue
            args = (p / 'cmdline').read_bytes().split(b'\0')
            start = (p / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            found.append((int(p.name), exe, start, not any(x.startswith(b'--type=') for x in args)))
        except (OSError, ValueError, IndexError):
            pass
    return found

def start(name, environment):
    options = ['systemd-run', '--quiet', '--unit=' + name,
               '--property=WorkingDirectory=' + str(app), '--property=Restart=no',
               '--property=TimeoutStopSec=5']
    options += ['--setenv=' + k + '=' + v for k, v in environment.items()]
    if run(*options, str(app / 'AppRun'), '--no-sandbox').returncode:
        raise RuntimeError('宿主 systemd-run 启动失败（请查 journalctl -u ' + name + '）')
    deadline, stable = time.monotonic() + 10, None
    while time.monotonic() < deadline:
        group = run('systemctl', 'show', name, '--property=ControlGroup', '--value').stdout.strip()
        owned, seen = [], processes()
        for pid, exe, _, main in seen:
            try:
                groups = pathlib.Path('/proc', str(pid), 'cgroup').read_text().splitlines()
                if main and exe == str(app / 'hiksemi-sub') and group and any(
                    row.split(':', 2)[-1] == group for row in groups):
                    owned.append(pid)
            except OSError:
                pass
        active = run('systemctl', 'is-active', name).stdout.strip() == 'active'
        stable = (stable or time.monotonic()) if active and len(owned) == 1 and sum(p[3] for p in seen) == 1 else None
        if stable and time.monotonic() - stable >= 3:
            return
        time.sleep(0.5)
    raise RuntimeError('新程序未通过 3 秒存活及独立 cgroup 检查')

current = processes()
print('解包目录:', '存在' if app.is_dir() else '不存在')
print('目标页面:', '存在' if page.is_file() else '不存在')
print('宿主依赖:', ', '.join(k + '=' + ('有' if shutil.which(k) else '无')
      for k in ('systemd-run', 'systemctl')))
print('小屏进程数:', len(current))
if not source:
    print('NEW_HTML 未设置：仅诊断，未停止进程、未替换文件。')
    sys.exit(0)
if not pathlib.Path(source).is_absolute():
    print('预检失败，未停止原程序：NEW_HTML 必须是宿主绝对路径。')
    sys.exit(1)

# Docker may start before the screen app or the shared disk containing the HTML.
print('等待页面文件和官方小屏主进程就绪（最多 120 秒）。', flush=True)
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    current = processes()
    if pathlib.Path(source).is_file() and page.is_file() and any(p[3] for p in current):
        break
    time.sleep(1)

try:
    incoming = pathlib.Path(source)
    require(incoming.is_absolute() and incoming.is_file() and incoming.stat().st_size > 0,
            'NEW_HTML 必须是宿主已有的非空 HTML 绝对路径')
    require(page.is_file() and (app / 'hiksemi-sub').is_file() and os.access(app / 'AppRun', os.X_OK),
            '请先解包 AppImage，确认 AppRun 可执行')
    require(incoming.resolve() != page.resolve(), 'NEW_HTML 请指向独立的待安装页面')
    require(all(shutil.which(k) for k in ('systemd-run', 'systemctl')), '宿主需要 systemd-run 和 systemctl')
    mains = [p for p in current if p[3]]
    require(len(mains) == 1, '必须找到唯一正在运行的小屏主进程，才能继承图形环境')
    require(all(p[1] == str(app / 'hiksemi-sub') or (
        p[1].startswith('/tmp/.mount_Histor') and pathlib.Path(p[1]).parent.parent == pathlib.Path('/tmp'))
        for p in current), '发现来源不明的同名进程，已停止操作')
    raw = pathlib.Path('/proc', str(mains[0][0]), 'environ').read_bytes()
    allowed = {'DISPLAY', 'XAUTHORITY', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LANGUAGE',
               'LC_ALL', 'LC_CTYPE', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'LD_LIBRARY_PATH'}
    environment = dict(base_env, APPDIR=str(app))
    environment.update((k, v) for item in raw.split(b'\0') if b'=' in item
                       for k, v in [item.decode(errors='surrogateescape').split('=', 1)] if k in allowed)
    old_root = str(pathlib.Path(mains[0][1]).parent)
    if 'LD_LIBRARY_PATH' in environment:
        environment['LD_LIBRARY_PATH'] = ':'.join(str(app) + p[len(old_root):] if p == old_root or p.startswith(old_root + '/') else p
                                                 for p in environment['LD_LIBRARY_PATH'].split(':'))
    require(environment.get('DISPLAY'), '现有小屏没有 DISPLAY；未停止原程序')
    require(not environment.get('XAUTHORITY') or pathlib.Path(environment['XAUTHORITY']).is_file(),
            'XAUTHORITY 文件不存在')
    require(run('systemd-run', '--quiet', '--wait', '--unit=' + unit + '-check', '/bin/true').returncode == 0,
            '宿主无法创建 transient service')
    backup = page.with_name(page.name + '.before-docker-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid()))
    staged = page.with_name(page.name + '.incoming-' + str(os.getpid()))
    backup.touch(exist_ok=False)
    shutil.copy2(page, backup)
    staged.touch(exist_ok=False)
    shutil.copy2(incoming, staged)
    os.chmod(staged, page.stat().st_mode & 0o777)
    for saved in (backup, staged):
        with saved.open('rb') as handle:
            os.fsync(handle.fileno())
except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
    print('预检失败，未停止原程序：', error)
    sys.exit(1)

try:
    for sig, wait in ((signal.SIGTERM, 4), (signal.SIGKILL, 2)):
        live = {(p[0], p[2]) for p in processes()}
        for pid, _, start_time, _ in current:
            if (pid, start_time) in live:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + wait
        while processes() and time.monotonic() < deadline:
            time.sleep(0.1)
    if processes():
        raise RuntimeError('小屏仍在运行或被官方守护程序拉起；未替换页面')
    os.replace(staged, page)
    start(unit, environment)
    print('页面已替换，备份:', backup)
    print('小屏已由宿主 transient service 接管:', unit + '.service')
except (RuntimeError, OSError, subprocess.SubprocessError) as error:
    print('执行失败：', error)
    try:
        run('systemctl', 'stop', unit)
    except RuntimeError:
        print('未能确认新服务已停止，继续恢复原页面文件。')
    restore = page.with_name(page.name + '.restore-' + str(os.getpid()))
    shutil.copy2(backup, restore)
    os.replace(restore, page)
    if not any(p[3] for p in processes()):
        try:
            start(unit + '-recovery', environment)
            print('已恢复原页面，并由宿主服务重新启动。')
        except (RuntimeError, OSError, subprocess.SubprocessError) as recovery_error:
            print('原页面已恢复，重启未成功：', recovery_error)
    else:
        print('原页面已恢复；已有小屏进程在运行。')
    sys.exit(1)
PY

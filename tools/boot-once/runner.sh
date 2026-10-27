#!/bin/sh
# Docker PID 1. Stop through the Engine API; a normal exit would restart us.
set -u
task_rc=1
trap 'exit "$task_rc"' TERM INT

host_docker() {
  chroot /host /bin/sh -c '
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/histor/bin:/histor/sbin
    export PATH
    exec docker --host unix:///var/run/docker.sock "$@"
  ' docker "$@"
}

host_shell() {
  chroot /host /bin/sh -c '
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/histor/bin:/histor/sbin
    export PATH
    exec nsenter --target 1 --mount --uts --pid --cgroup --root=/proc/1/root --wd=/ -- /bin/sh -s
  '
}

# Allow Docker to finish starting and monitoring this container.
sleep 12
ready=0
attempt=0
while [ "$attempt" -lt 10 ]; do
  if [ "$(host_docker inspect --format '{{.State.Running}}' "$TASK_CONTAINER" 2>/dev/null)" = true ]; then
    ready=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo '[ERROR] 无法调用宿主 Docker 或找不到本容器；未执行任务。'
  echo '[ERROR] 保持等待以避免反复执行，请查看宿主 docker 命令和容器名称。'
  exec sleep infinity
fi

if host_shell <<'R1_HOST_JOB'
#!/bin/sh
# Runs in the NAS host namespaces, with the host filesystem as its root.
set -eu
command -v mountpoint >/dev/null || {
  echo '[ERROR] 宿主缺少 mountpoint，未执行任务。'
  exit 1
}
attempt=0
until mountpoint -q /screen; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo '[ERROR] 等待 /screen 持久分区挂载超时，未执行任务。'
    exit 1
  fi
  sleep 2
done

state_dir=/run/r1-toolbox
mkdir -p "$state_dir"
boot_id=$(cat /proc/sys/kernel/random/boot_id)
run_key="$boot_id:${TASK_REVISION:-1}"
last_key=$(cat "$state_dir/last-success" 2>/dev/null || true)
if [ "$last_key" = "$run_key" ]; then
  echo '[SKIP] 本次 NAS 启动已经成功执行过当前任务版本。'
  exit 0
fi

echo '[START] 开始执行宿主任务。'
(
#!/bin/sh
# Runs in the host namespaces/root. UI_DIR is private to the new screen service.
set -eu
command -v python3 >/dev/null || { echo '宿主缺少 python3，未修改小屏'; exit 1; }
exec python3 - <<'PY'
import os, pathlib, shutil, signal, subprocess, sys, time, unicodedata

app = pathlib.Path('/screen/squashfs-root')
target = app / 'resources/app/dist'
unit = 'histor-custom-' + str(os.getpid())
base_env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/histor/bin:/histor/sbin'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(*args, env=None):
    try:
        return subprocess.run(args, env=env or base_env, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('宿主命令失败或超时：' + args[0]) from None


def resolve_source(value):
    """Use canonical paths, and reject systemd property syntax/escaping hazards."""
    def safe_path(path):
        return not any(c.isspace() or c in ':\\%$\'"' or unicodedata.category(c) == 'Cc'
                       for c in str(path))

    require(value and safe_path(value), 'UI_DIR 路径不能包含空白、冒号、反斜杠、%、$、引号或控制字符')
    incoming = pathlib.Path(value)
    require(incoming.is_absolute(), 'UI_DIR 必须是宿主绝对目录路径')
    incoming = incoming.resolve()
    require(safe_path(incoming), 'UI_DIR 解析后的真实路径含不支持的字符')
    destination = target.resolve()
    require(incoming != destination and incoming not in destination.parents and destination not in incoming.parents,
            'UI_DIR 与官方 dist 目录必须独立，不能相同或互为祖先目录')
    return incoming


def binding_property(bind_source):
    return '--property=BindReadOnlyPaths=' + str(bind_source) + ':' + str(target) + ':norbind'


def service_options(name, bind_source=None):
    options = ['systemd-run', '--quiet', '--unit=' + name,
               '--property=WorkingDirectory=' + str(app), '--property=Restart=no',
               '--property=TimeoutStopSec=5']
    if bind_source is not None:
        options.append(binding_property(bind_source))
    return options


def preflight_binding(name, bind_source):
    """Fail before stopping the current app if systemd cannot provide this mount."""
    identity = bind_source.stat()
    check = '''import os, sys
path, device, inode = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
actual = os.stat(path)
if (actual.st_dev, actual.st_ino) != (device, inode):
    raise SystemExit("UI_DIR mount identity mismatch")
if not (os.statvfs(path).f_flag & os.ST_RDONLY):
    raise SystemExit("UI_DIR mount is not read-only")
'''
    python = shutil.which('python3')
    require(python is not None, '宿主缺少 python3，无法验证只读挂载')
    result = run(*service_options(name + '-check', bind_source), '--wait', python, '-c', check,
                 str(target), str(identity.st_dev), str(identity.st_ino))
    require(result.returncode == 0,
            '宿主只读挂载预检失败：需 systemd 233+ 支持 BindReadOnlyPaths，且目标挂载必须只读并指向 UI_DIR')


def verify_binding(pid, bind_source):
    """Inspect the running process's mount view without changing the host view."""
    try:
        expected = bind_source.stat()
        mounted = pathlib.Path('/proc', str(pid), 'root') / str(target).lstrip('/')
        actual = mounted.stat()
        return ((actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
                and bool(os.statvfs(mounted).f_flag & os.ST_RDONLY))
    except OSError:
        return False


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


def start(name, environment, bind_source=None):
    options = service_options(name, bind_source)
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
                    if bind_source is None or verify_binding(pid, bind_source):
                        owned.append(pid)
            except OSError:
                pass
        active = run('systemctl', 'is-active', name).stdout.strip() == 'active'
        healthy = active and len(owned) == 1 and sum(p[3] for p in seen) == 1
        stable = (stable if stable is not None else time.monotonic()) if healthy else None
        if stable is not None and time.monotonic() - stable >= 3:
            return
        time.sleep(0.5)
    raise RuntimeError('新程序未通过 3 秒存活、独立 cgroup 及只读挂载检查')


def main():
    current = processes()
    print('解包目录:', '存在' if app.is_dir() else '不存在')
    print('官方 dist:', '存在' if target.is_dir() else '不存在')
    print('宿主依赖:', ', '.join(k + '=' + ('有' if shutil.which(k) else '无')
          for k in ('systemd-run', 'systemctl')))
    print('小屏进程数:', len(current))
    if os.environ.get('NEW_HTML', ''):
        print('预检失败，未停止原程序：NEW_HTML 已停用；请移除此配置，改用 UI_DIR 指向完整前端目录。')
        return 1
    source_value = os.environ.get('UI_DIR', '')
    if not source_value:
        print('UI_DIR 未设置：仅诊断，未停止进程、未挂载目录。')
        return 0

    try:
        incoming = resolve_source(source_value)
        print('等待 UI_DIR、官方 dist 和小屏主进程就绪（最多 120 秒）。', flush=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = processes()
            if incoming.is_dir() and (incoming / 'index.html').is_file() and target.is_dir() and any(p[3] for p in current):
                break
            time.sleep(1)

        # Resolve again after the shared disk is ready, including any symlinks.
        incoming = resolve_source(source_value)
        require(incoming.is_dir() and (incoming / 'index.html').is_file()
                and (incoming / 'index.html').stat().st_size > 0,
                'UI_DIR 必须是宿主已有的前端目录，且包含非空 index.html')
        require(target.is_dir() and (target / 'index.html').is_file()
                and (app / 'hiksemi-sub').is_file() and os.access(app / 'AppRun', os.X_OK),
                '请先解包 AppImage，确认原 dist/index.html 存在且 AppRun 可执行')
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
        preflight_binding(unit, incoming)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('预检失败，未停止原程序：', error)
        return 1

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
            raise RuntimeError('小屏仍在运行或被官方守护程序拉起；未启动新界面')
        start(unit, environment, bind_source=incoming)
        print('前端目录已在新小屏服务内只读挂载:', incoming)
        print('宿主原 dist 保持原样；小屏服务:', unit + '.service')
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('执行失败：', error)
        try:
            stopped = run('systemctl', 'stop', unit)
            if stopped.returncode:
                print('未能确认新服务已停止；请检查 journalctl -u ' + unit)
        except RuntimeError:
            print('未能确认新服务已停止；请检查 journalctl -u ' + unit)
        if not any(p[3] for p in processes()):
            try:
                start(unit + '-recovery', environment)
                print('已使用官方原 dist 重新启动解包程序。')
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as recovery_error:
                print('官方文件保持原样；原程序重启未成功：', recovery_error)
        else:
            print('官方文件保持原样；已有小屏进程在运行。')
        return 1


if __name__ == '__main__':
    sys.exit(main())
PY
)

# Record success only after all actions finish. Failed runs remain retryable.
printf '%s\n' "$run_key" > "$state_dir/last-success.new"
mv "$state_dir/last-success.new" "$state_dir/last-success"
sync
echo '[OK] 已记录本次启动的成功状态。'
R1_HOST_JOB
then
  task_rc=0
  echo '[OK] 本次任务完成，正在停止容器。'
else
  task_rc=$?
  echo "[ERROR] 本次任务失败（状态 ${task_rc}），停止容器，不自动循环执行。"
fi

# Background + interruptible wait lets PID 1 handle the stop signal immediately.
host_docker stop --time 5 "$TASK_CONTAINER" &
stop_pid=$!
wait "$stop_pid" || true
echo '[ERROR] 自停请求未能终止容器；保持等待，不重复执行任务。'
exec sleep infinity

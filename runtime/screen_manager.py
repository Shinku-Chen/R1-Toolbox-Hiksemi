"""Independent, bounded ownership of the NAS display, never the Web lifecycle.

The GUI uses its own private read-only systemd mount.  Process identity and
mount checks mirror the installer; no factory script or frontend is replaced.
"""

import collections
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

app = pathlib.Path('/screen/squashfs-root')
target = app / 'resources/app/dist'
base_env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/histor/bin:/histor/sbin'}
Process = collections.namedtuple('Process', 'pid exe start is_main ppid process_type')
APPIMAGE_GLOB = 'HistorSub-Linux-*.AppImage'
SCREEN_MOUNT_WAIT_SECONDS = 20.0


def require(condition, message):
    if not condition:
        raise ValueError(message)

def _mount_text(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)

def screen_mount_source(mountpoint=None, mountinfo=None):
    """Return the device backing /screen; None means the path is not a mount."""
    mountpoint = str(mountpoint or app.parent)
    path = pathlib.Path(mountinfo or '/proc/self/mountinfo')
    try:
        rows = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    for row in rows:
        before, separator, after = row.partition(' - ')
        fields = before.split()
        if not separator or len(fields) < 5:
            continue
        if _mount_text(fields[4]) == mountpoint:
            backing = after.split()
            return backing[1] if len(backing) > 1 else ''
    return None

def runtime_complete(runtime=None):
    """Check the extracted AppImage layout Toolbox needs, without executing it."""
    runtime = pathlib.Path(runtime or app)
    dist = runtime / 'resources/app/dist'
    try:
        return (runtime.is_dir() and not runtime.is_symlink()
                and dist.is_dir() and (dist / 'index.html').is_file()
                and (runtime / 'hiksemi-sub').is_file()
                and (runtime / 'AppRun').is_file()
                and os.access(runtime / 'AppRun', os.X_OK))
    except OSError:
        return False

def official_appimage(root=None):
    """Pick the newest root-owned official AppImage; never follow a link."""
    root = pathlib.Path(root or app.parent)
    candidates = []
    try:
        paths = list(root.glob(APPIMAGE_GLOB))
    except OSError:
        return None
    for path in paths:
        try:
            info = path.lstat()
        except OSError:
            continue
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                or (os.name == 'posix' and os.geteuid() == 0 and info.st_uid != 0)
                or not os.access(path, os.X_OK)):
            continue
        candidates.append((info.st_mtime, path))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None

def provision_screen_runtime(*, root=None, runtime=None, mountinfo=None,
                             wait_seconds=SCREEN_MOUNT_WAIT_SECONDS, cancelled=None):
    """Restore the derived AppImage runtime only when /screen is a real mount.

    The official startup script runs the AppImage directly and never creates
    squashfs-root. Toolbox needs that extracted layout for AppRun and the
    read-only frontend bind, so a missing layout is rebuilt from the official
    root-owned AppImage. Existing or incomplete directories are never
    overwritten; the original AppImage and startup script stay untouched.
    """
    root = pathlib.Path(root or app.parent)
    runtime = pathlib.Path(runtime or app)
    if runtime_complete(runtime):
        return
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        source = screen_mount_source(root, mountinfo)
        if source and source.startswith('/dev/'):
            break
        if time.monotonic() >= deadline:
            raise ValueError('原厂 /screen 分区尚未挂载；Web 服务继续运行')
        if cancelled is not None:
            if cancelled.wait(min(0.5, max(0.0, deadline - time.monotonic()))):
                raise ValueError('小屏切换已取消')
        else:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    if runtime_complete(runtime):
        return
    if runtime.exists():
        raise ValueError('原厂小屏解包目录不完整；未自动覆盖，请检查 /screen/squashfs-root')
    appimage = official_appimage(root)
    if appimage is None:
        raise ValueError('原厂小屏 AppImage 未找到；Web 服务继续运行')
    stage = pathlib.Path(tempfile.mkdtemp(prefix='.r1-toolbox-screen-', dir=str(root)))
    try:
        try:
            result = subprocess.run([str(appimage), '--appimage-extract'], cwd=stage,
                                    env=base_env, capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError('原厂小屏 AppImage 解包失败：' + type(error).__name__) from None
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '').strip().splitlines()
            suffix = ('：' + detail[-1][:160]) if detail else ''
            raise ValueError('原厂小屏 AppImage 解包失败（退出码 %s）%s' % (result.returncode, suffix))
        extracted = stage / 'squashfs-root'
        if not runtime_complete(extracted):
            raise ValueError('原厂小屏 AppImage 解包结果不完整')
        if cancelled is not None and cancelled.is_set():
            raise ValueError('小屏切换已取消')
        if runtime.exists():
            raise ValueError('原厂小屏解包目录已变化；未自动覆盖，请重试')
        os.replace(str(extracted), str(runtime))
    finally:
        shutil.rmtree(str(stage), ignore_errors=True)

def run(*args, env=None):
    try:
        return subprocess.run(args, env=env or base_env, capture_output=True, text=True, timeout=45)
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
               '--property=TimeoutStopSec=25']
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
    raise SystemExit("frontend mount identity mismatch")
if not (os.statvfs(path).f_flag & os.ST_RDONLY):
    raise SystemExit("frontend mount is not read-only")
'''
    python = shutil.which('python3')
    require(python is not None, '宿主缺少 python3，无法验证只读挂载')
    result = run(*service_options(name + '-check', bind_source), '--wait', python, '-c', check,
                 str(target), str(identity.st_dev), str(identity.st_ino))
    require(result.returncode == 0,
            '宿主只读挂载预检失败：需 systemd 233+ 支持 BindReadOnlyPaths，且目标挂载必须只读并指向已验证的前端目录')

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

def process_type(raw_cmdline, exe):
    """Chromium can flatten argv into one Linux process title; do not split real args."""
    args = [arg for arg in raw_cmdline.split(b'\0') if arg]
    if not args:
        return '?'
    if len(args) == 1:
        executable = os.fsencode(exe)
        if args[0] == executable:
            tokens = []
        elif args[0].startswith(executable + b' '):
            # Only inspect switch words: setproctitle has discarded quoting.
            tokens = args[0][len(executable) + 1:].split()
        else:
            return '?'
    else:
        tokens = args[1:]
    kinds = []
    for token in tokens:
        if token == b'--':
            break
        if token in (b'--type', b'-type'):
            return '?'
        if token.startswith((b'--type=', b'-type=')):
            value = token.split(b'=', 1)[1]
            if not re.fullmatch(rb'[A-Za-z0-9_-]+', value):
                return '?'
            kinds.append(value.decode('ascii'))
    return kinds[0] if len(kinds) == 1 else '?' if kinds else None

def read_process_stat(path):
    fields = path.read_text().rsplit(')', 1)[1].split()
    require(len(fields) >= 20 and fields[19].isdigit(), '无法读取小屏进程身份')
    return fields[0], int(fields[1]), fields[19]

def read_identity(pid, proc_root=None):
    root = pathlib.Path('/proc') if proc_root is None else pathlib.Path(proc_root)
    _, ppid, start = read_process_stat(root / str(pid) / 'stat')
    return ppid, start

def processes(proc_root=None):
    root = pathlib.Path('/proc') if proc_root is None else pathlib.Path(proc_root)
    found = []
    for p in root.glob('[0-9]*'):
        exe, before = None, None
        try:
            exe = os.readlink(p / 'exe')
            if pathlib.Path(exe).name != 'hiksemi-sub':
                continue
            before = read_process_stat(p / 'stat')
            if before[0] == 'Z':
                continue
            raw = (p / 'cmdline').read_bytes()
            after = read_process_stat(p / 'stat')
            if after[0] == 'Z':
                continue
            kind = process_type(raw, exe) if before[1:] == after[1:] else '?'
            found.append(Process(int(p.name), exe, after[2], kind is None, after[1], kind))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError):
            # Do not hide a live matching process whose identity is unreadable.
            if exe is not None and pathlib.Path(exe).name == 'hiksemi-sub':
                found.append(Process(int(p.name), exe, before[2] if before else '?', False,
                                     before[1] if before else -1, '?'))
    return found

def identify_family(snapshot):
    """Require one browser and a complete, unambiguous same-executable PPID tree."""
    require(snapshot and all(p.process_type != '?' for p in snapshot), '小屏进程类型或身份不明确，请稍后重试')
    mains = [p for p in snapshot if p.is_main]
    require(len(mains) == 1, '必须找到唯一正在运行的小屏主进程，才能继承图形环境')
    main = mains[0]
    require(all(p.exe == main.exe for p in snapshot), '小屏进程来自不同程序，未停止原程序')
    by_pid = {p.pid: p for p in snapshot}
    require(len(by_pid) == len(snapshot) and main.ppid not in by_pid, '小屏进程父子关系不明确')
    for process in snapshot:
        if process.pid == main.pid:
            continue
        seen = {process.pid}
        parent = process.ppid
        while parent != main.pid:
            require(parent in by_pid and parent not in seen, '小屏子进程不属于唯一主进程，未停止原程序')
            seen.add(parent)
            parent = by_pid[parent].ppid
    return main

def read_environment(process, proc_root=None):
    root = pathlib.Path('/proc') if proc_root is None else pathlib.Path(proc_root)
    identity = process.ppid, process.start
    require(read_identity(process.pid, root) == identity, '小屏主进程身份已变化，请重试')
    raw = (root / str(process.pid) / 'environ').read_bytes()
    require(read_identity(process.pid, root) == identity, '读取环境时小屏主进程身份已变化，请重试')
    return raw

def process_summary(snapshot, proc_root=None):
    root = pathlib.Path('/proc') if proc_root is None else pathlib.Path(proc_root)
    entries = []
    for process in sorted(snapshot, key=lambda item: item.pid):
        argc, display = '?', '?'
        try:
            argc = len([part for part in (root / str(process.pid) / 'cmdline').read_bytes().split(b'\0') if part])
        except OSError:
            pass
        try:
            display = any(part.startswith(b'DISPLAY=')
                          for part in (root / str(process.pid) / 'environ').read_bytes().split(b'\0'))
        except OSError:
            pass
        entries.append('PID={} PPID={} 类型={} exe={} argc={} DISPLAY存在={}'.format(
            process.pid, process.ppid, process.process_type or 'browser', process.exe, argc, display))
    return '; '.join(entries) or '无'

def start(name, environment, bind_source=None, backend=None, cancelled=None):
    # Failed transient units survive until reset.  Only our internally named
    # unit is reset; a failed explicit retry must not restart arbitrary units.
    run('systemctl', 'reset-failed', name)
    options = service_options(name, bind_source)
    launch_environment = dict(environment)
    if backend is not None:
        # The host Python interpreter must not load Electron's bundled libs.
        if 'LD_LIBRARY_PATH' in launch_environment:
            launch_environment['R1_TOOLBOX_APP_LD_LIBRARY_PATH'] = launch_environment['LD_LIBRARY_PATH']
        launch_environment['LD_LIBRARY_PATH'] = ''
    options += ['--setenv=' + k + '=' + v for k, v in launch_environment.items()]
    if backend is None:
        command = [str(app / 'AppRun'), '--no-sandbox']
    else:
        python = shutil.which('python3')
        require(python is not None, '宿主缺少 python3，无法启动内置界面服务')
        command = [python, '-B', str(backend), '--app', str(app)]
    if run(*options, *command).returncode:
        raise RuntimeError('宿主 systemd-run 启动失败（请查 journalctl -u ' + name + '）')
    deadline, stable, stable_identity = time.monotonic() + 35, None, None
    seen = []
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError('小屏切换已取消')
        group = run('systemctl', 'show', name, '--property=ControlGroup', '--value').stdout.strip()
        seen, owned, identity = processes(), False, None
        try:
            main = identify_family(seen)
            groups = pathlib.Path('/proc', str(main.pid), 'cgroup').read_text().splitlines()
            owned = (all(p.exe == str(app / 'hiksemi-sub') for p in seen) and bool(group) and any(
                row.split(':', 2)[-1] == group for row in groups)
                and (bind_source is None or verify_binding(main.pid, bind_source)))
            identity = main.pid, main.start
        except (ValueError, OSError):
            pass
        active = run('systemctl', 'is-active', name).stdout.strip() == 'active'
        if active and owned:
            if stable is None or identity != stable_identity:
                stable, stable_identity = time.monotonic(), identity
        else:
            stable, stable_identity = None, None
        if stable is not None and time.monotonic() - stable >= 3:
            return
        time.sleep(0.5)
    raise RuntimeError('新程序未通过 3 秒存活、独立 cgroup 及只读挂载检查；' + process_summary(seen))


_GRAPHICS_ENV = frozenset(('DISPLAY', 'XAUTHORITY', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LANGUAGE',
                           'LC_ALL', 'LC_CTYPE', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'LD_LIBRARY_PATH'))


def graphics_environment(current):
    """Capture only the verified browser's graphics context, never its tokens."""
    main = identify_family(current)
    require(all(p.exe == str(app / 'hiksemi-sub') or (
        p.exe.startswith('/tmp/.mount_Histor') and pathlib.Path(p.exe).parent.parent == pathlib.Path('/tmp'))
        for p in current), '发现来源不明的小屏同名进程，未停止原程序')
    environment = dict(base_env, APPDIR=str(app))
    for field in read_environment(main).split(b'\0'):
        if b'=' not in field:
            continue
        key, value = field.decode(errors='surrogateescape').split('=', 1)
        if key in _GRAPHICS_ENV:
            environment[key] = value
    previous = str(pathlib.Path(main.exe).parent)
    if 'LD_LIBRARY_PATH' in environment:
        environment['LD_LIBRARY_PATH'] = ':'.join(str(app) + path[len(previous):]
            if path == previous or path.startswith(previous + '/') else path
            for path in environment['LD_LIBRARY_PATH'].split(':'))
    require(environment.get('DISPLAY'), '现有小屏没有 DISPLAY，未停止原程序')
    require(not environment.get('XAUTHORITY') or pathlib.Path(environment['XAUTHORITY']).is_file(),
            'XAUTHORITY 文件不存在，未停止原程序')
    return main, environment


def stop_verified_family(current):
    """Signal only the captured PID/start-time family; refuse a replacement."""
    for sig, wait in ((signal.SIGTERM, 4), (signal.SIGKILL, 2)):
        live = {(p.pid, p.start) for p in processes()}
        for process in current:
            if (process.pid, process.start) in live:
                try:
                    os.kill(process.pid, sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + wait
        while processes() and time.monotonic() < deadline:
            time.sleep(0.1)
    require(not processes(), '小屏仍在运行或已被官方守护程序拉起，未启动新界面')


def migrate_legacy_runtime():
    """Stop only identified Toolbox units before binding the resident ports.

    Official recovery units are deliberately left running.  Neither a matching
    name nor a port number alone authorizes stopping a service.
    """
    if not shutil.which('systemctl'):
        return
    listed = run('systemctl', 'list-units', '--all', '--type=service', '--no-legend', '--plain')
    require(listed.returncode == 0, '无法核对旧 Toolbox 服务，未启动第二套服务')
    restored = None
    for line in listed.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[0]
        if not (re.fullmatch(r'histor-custom-[0-9]+\.service', name)
                or re.fullmatch(r'r1-toolbox-(?:ui|screen(?:-[0-9]+)?)\.service', name)):
            continue
        command = run('systemctl', 'show', name, '--property=ExecStart', '--value').stdout
        legacy_runtime = bool(re.search(r'/run/r1-toolbox/ui-[0-9a-f]{64}/runtime/ui_service\.py(?:[ ;}]|$)', command))
        gui = name.startswith('r1-toolbox-screen') and str(app / 'AppRun') in command
        if gui:
            bindings = run('systemctl', 'show', name, '--property=BindReadOnlyPaths', '--value').stdout
            gui = bool(re.search(r'/run/r1-toolbox/ui-[0-9a-f]{64}/frontend:', bindings))
        if not legacy_runtime and not gui:
            continue
        if run('systemctl', 'is-active', name).stdout.strip() != 'active':
            continue
        try:
            _, restored = graphics_environment(processes())
        except (ValueError, OSError):
            restored = None
        stopped = run('systemctl', 'stop', name)
        require(stopped.returncode == 0 and run('systemctl', 'is-active', name).stdout.strip() != 'active',
                '旧 Toolbox 服务未确认停止，未启动第二套 Web／控制服务')
    if restored is not None and not processes():
        try:
            start('r1-toolbox-official-migration-' + str(os.getpid()), restored)
        except (ValueError, RuntimeError, OSError):
            # A missing display must not prevent the resident Web from starting.
            print('[WARN] 旧服务已停止，官方窗口暂未恢复；Web 将保留小屏诊断。', flush=True)


class ScreenBackend:
    """The only OS-facing display implementation; GUI and core have separate units."""

    def __init__(self, frontend, control_token):
        self.frontend = pathlib.Path(frontend) if frontend else None
        self.control_token = control_token
        self.unit = 'r1-toolbox-screen-' + str(os.getpid())
        self.environment = None
        self.owned = False
        self.gui_attempted = False
        self.original_family = []
        self._provision_lock = threading.Lock()

    def _provision(self, cancelled):
        if runtime_complete():
            return
        if not self._provision_lock.acquire(blocking=False):
            raise ValueError('原厂小屏解包正在进行，请稍后重试')
        try:
            provision_screen_runtime(cancelled=cancelled)
        finally:
            self._provision_lock.release()

    def availability(self):
        if not self.frontend or not self.frontend.is_dir() or not (self.frontend / 'index.html').is_file():
            return False, '内置小屏前端不存在；Web 服务继续运行'
        if not target.is_dir() or not (target / 'index.html').is_file() or not (app / 'hiksemi-sub').is_file() or not os.access(app / 'AppRun', os.X_OK):
            return False, '原厂小屏 AppRun 或解包目录尚不可用；Web 服务继续运行'
        if not all(shutil.which(command) for command in ('systemd-run', 'systemctl')):
            return False, '小屏切换需要 systemd-run 和 systemctl；Web 服务继续运行'
        return True, None

    def takeover(self, cancelled):
        if self.owned:
            # A prior failed recovery must finish before a new takeover can
            # claim a fresh, verified official process family.
            self.release()
        available, reason = self.availability()
        if not available and not runtime_complete():
            self._provision(cancelled)
            available, reason = self.availability()
        require(available, reason)
        frontend = resolve_source(str(self.frontend))
        current = processes()
        main, environment = graphics_environment(current)
        run('systemctl', 'reset-failed', self.unit + '-check')
        preflight_binding(self.unit, frontend)
        latest = processes()
        latest_main = identify_family(latest)
        require((latest_main.pid, latest_main.start, latest_main.exe) == (main.pid, main.start, main.exe),
                '只读挂载预检期间小屏主进程已变化，未停止原程序')
        require(not cancelled.is_set(), '小屏切换已取消')
        self.environment = environment
        self.original_family = list(latest)
        self.gui_attempted = False
        self.owned = True
        try:
            # The recovery obligation begins before the first signal, even if
            # shutdown only partially succeeds or its final check raises.
            stop_verified_family(latest)
            require(not cancelled.is_set(), '小屏切换已取消')
            custom_environment = dict(environment, R1_TOOLBOX_CONTROL_TOKEN=self.control_token)
            self.gui_attempted = True
            start(self.unit, custom_environment, bind_source=frontend, cancelled=cancelled)
        except Exception:
            self.release()
            raise

    def active(self):
        return self.owned and run('systemctl', 'is-active', self.unit).stdout.strip() == 'active'

    def release(self):
        if not self.owned:
            return
        if self.gui_attempted:
            result = run('systemctl', 'stop', self.unit)
            active = run('systemctl', 'is-active', self.unit).stdout.strip() == 'active'
            require(not active and (result.returncode == 0 or not processes()),
                    'Toolbox 小屏服务尚未确认停止，未启动另一套官方窗口')
            self.gui_attempted = False
        current = processes()
        if current:
            try:
                graphics_environment(current)
            except (ValueError, OSError):
                # Finish only captured members after a partial original stop.
                # New/unknown processes are never signalled by this recovery.
                stop_verified_family(self.original_family)
            else:
                self.owned = False
                return
        require(self.environment is not None, '缺少已核验的官方图形环境，无法恢复窗口')
        start(self.unit + '-official', self.environment)
        self.owned = False


class ScreenManager:
    """Serialize quick Web requests and restore the official display on shutdown.

    A failed enable is attempted once.  It does not stop the resident runtime or
    automatically repeat takeover; an explicit subsequent request can retry.
    """

    def __init__(self, controller, frontend=None, control_token=None, *, backend=None):
        self.controller = controller
        self.backend = backend or ScreenBackend(frontend, control_token)
        self.condition = threading.Condition()
        self.enabled = False
        self.active = False
        self.busy = False
        try:
            self.available, self.last_error = self.backend.availability()
        except Exception:
            self.available, self.last_error = False, '小屏文件状态暂不可确认；Web 服务继续运行'
        self.generation = 0
        self.finished = 0
        self.stopping = False
        self.cancelled = threading.Event()
        self.controller.set_screen_managed(False)
        self.thread = threading.Thread(target=self._loop, name='r1-screen-manager', daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.condition:
            return {key: getattr(self, key) for key in ('enabled', 'active', 'busy', 'available', 'last_error')}

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('小屏开关必须为布尔值')
        with self.condition:
            if self.stopping:
                raise ValueError('小屏管理正在停止')
            if enabled == self.enabled and (self.busy or (self.active == enabled and self.last_error is None)):
                return self.snapshot()
            self.enabled = enabled
            self.generation += 1
            self.busy = True
            self.cancelled.set()
            self.condition.notify_all()
            return self.snapshot()

    def _release(self):
        try:
            self.controller.set_screen_managed(False)
        finally:
            self.backend.release()

    def _loop(self):
        while True:
            with self.condition:
                if self.stopping:
                    break
                pending = self.generation != self.finished
                if pending:
                    generation, enabled = self.generation, self.enabled
                    self.cancelled = threading.Event()
                    cancelled = self.cancelled
                else:
                    self.condition.wait(timeout=1)
                    if self.generation != self.finished or self.stopping:
                        continue
                    generation, enabled = self.generation, None
            if not pending:
                if self.active:
                    try:
                        if not self.backend.active():
                            self._release()
                            with self.condition:
                                self.active = False
                                self.last_error = 'Toolbox 小屏已退出，已尝试恢复官方窗口；Web 服务继续运行'
                    except Exception as error:
                        with self.condition:
                            self.active = False
                            self.last_error = '小屏退出恢复失败：' + str(error)[:200]
                continue
            error = None
            try:
                if enabled:
                    if not self.active:
                        self.backend.takeover(cancelled)
                    if not cancelled.is_set():
                        self.controller.set_screen_managed(True)
                    else:
                        self._release()
                else:
                    self._release()
            except Exception as failure:
                error = str(failure)[:240]
                try:
                    self.controller.set_screen_managed(False)
                except Exception as restore_error:
                    error += '；背光恢复失败：' + str(restore_error)[:120]
                error = error[:240]
            actual_active = bool(enabled and error is None and not cancelled.is_set())
            if error is not None:
                try:
                    actual_active = bool(self.backend.active())
                except Exception:
                    actual_active = self.active
            with self.condition:
                self.finished = generation
                self.active = actual_active
                try:
                    self.available, unavailable = self.backend.availability()
                except Exception:
                    self.available, unavailable = False, '小屏文件状态暂不可确认；Web 服务继续运行'
                if generation == self.generation:
                    self.busy = False
                    self.last_error = error or (unavailable if enabled else None)
                self.condition.notify_all()
        try:
            self._release()
        except Exception as error:
            with self.condition:
                self.last_error = '停止小屏恢复失败：' + str(error)[:200]
                print('[WARN] ' + self.last_error, file=sys.stderr, flush=True)
        with self.condition:
            self.enabled = self.active = self.busy = False
            self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.stopping = True
            self.cancelled.set()
            self.condition.notify_all()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=150)
            if self.thread.is_alive():
                raise RuntimeError('小屏切换未在停机期限内结束；请检查宿主服务')

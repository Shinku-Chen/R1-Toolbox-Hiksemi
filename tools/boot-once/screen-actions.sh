#!/bin/sh
# Runs in the host namespaces/root. UI is private to the new screen service.
set -eu
command -v python3 >/dev/null || { echo '宿主缺少 python3，未修改小屏'; exit 1; }
exec python3 - <<'PY'
import collections, hashlib, ipaddress, json, os, pathlib, re, shutil, signal, stat, subprocess, sys, tarfile, tempfile, time, unicodedata

app = pathlib.Path('/screen/squashfs-root')
target = app / 'resources/app/dist'
unit = 'histor-custom-' + str(os.getpid())
base_env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/histor/bin:/histor/sbin'}
runtime_root = pathlib.Path('/run/r1-toolbox')
MAX_FILES = 512
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_MANIFEST = 128 * 1024
MAX_ARCHIVE = 40 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(*args, env=None):
    try:
        return subprocess.run(args, env=env or base_env, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('宿主命令失败或超时：' + args[0]) from None


def select_mode(environ):
    require(not environ.get('NEW_HTML', ''),
            'NEW_HTML 已停用；请移除此配置，使用 UI_MODE=image 或 UI_MODE=directory 与 UI_DIR')
    source = environ.get('UI_DIR', '')
    mode = environ.get('UI_MODE', '') or ('directory' if source else 'diagnostic')
    require(mode in ('image', 'directory', 'diagnostic'), 'UI_MODE 只能是 image、directory 或 diagnostic')
    require(mode != 'image' or not source, 'UI_MODE=image 不接受 UI_DIR；请清空 UI_DIR 或改用 directory 模式')
    require(mode != 'directory' or source, 'UI_MODE=directory 需要设置宿主 UI_DIR 目录')
    return mode, source


def require_tmpfs(path=pathlib.Path('/run'), mountinfo=pathlib.Path('/proc/self/mountinfo')):
    """Find the deepest mount covering the path, including escaped mount names."""
    resolved, candidates = path.resolve(), []
    for order, row in enumerate(mountinfo.read_text().splitlines()):
        before, separator, after = row.partition(' - ')
        fields, filesystem = before.split(), after.split()
        if not separator or len(fields) < 5 or not filesystem:
            continue
        mount = pathlib.Path(re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[4]))
        if mount == resolved or mount in resolved.parents:
            candidates.append((len(mount.parts), order, filesystem[0]))
    require(candidates and max(candidates, key=lambda item: item[:2])[2] == 'tmpfs',
            str(path) + ' 必须位于 tmpfs；内置界面只允许在 RAM 中暂存和展开')


def regular_file(path, maximum):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, '内置界面文件必须是无链接的普通文件：' + str(path))
    require(info.st_size <= maximum, '内置界面文件超过大小限制：' + str(path))
    return info


def read_bundle(tar_path, expected_sha):
    """Validate the entire bounded archive in memory; never use tar extraction."""
    require(re.fullmatch(r'[0-9a-f]{64}', expected_sha), '内置界面 SHA256 格式错误')
    regular_file(tar_path, MAX_ARCHIVE)
    require(hashlib.sha256(tar_path.read_bytes()).hexdigest() == expected_sha, '内置 ui.tar 的 SHA256 不匹配')
    files, names, total = {}, set(), 0
    with tarfile.open(tar_path, mode='r:') as archive:
        for member in archive:
            name = member.name
            if member.isdir() and name.endswith('/'):
                name = name[:-1]
            parts = pathlib.PurePosixPath(name)
            require(name and not parts.is_absolute() and str(parts) == name
                    and '..' not in parts.parts and '\\' not in name
                    and not any(unicodedata.category(c) == 'Cc' for c in name),
                    'ui.tar 包含不安全的路径')
            require(name not in names, 'ui.tar 包含重复路径：' + name)
            names.add(name)
            require(len(names) <= MAX_FILES * 2, 'ui.tar 条目过多')
            require(member.isfile() or member.isdir(), 'ui.tar 不允许链接或特殊文件：' + name)
            require(not member.issparse(), 'ui.tar 不允许稀疏文件：' + name)
            if member.isdir():
                require(name in ('frontend', 'frontend/assets', 'frontend/web', 'runtime') or name.startswith('frontend/assets/'),
                        'ui.tar 包含不支持的目录：' + name)
                continue
            require(name in ('manifest.json', 'frontend/index.html', 'runtime/ui_service.py',
                             'runtime/toolbox.py', 'runtime/insights.py', 'runtime/device_info.py', 'runtime/histor.py',
                             'runtime/web_config.py', 'runtime/fan_policy.py', 'runtime/fan_native.py',
                             'runtime/fan_watchdog.py', 'runtime/temperature_sources.py', 'runtime/fan_guard.so', 'frontend/web/index.html',
                             'frontend/web/login.html', 'runtime/fnm_auth.py', 'runtime/fnm_compat.py',
                             'runtime/light_policy.py', 'runtime/monitoring.py')
                    or name.startswith('frontend/assets/'), 'ui.tar 包含不支持的文件：' + name)
            require(len(files) < MAX_FILES, 'ui.tar 文件数超过 512')
            limit = MAX_MANIFEST if name == 'manifest.json' else MAX_FILE
            require(0 <= member.size <= limit, 'ui.tar 单文件超过限制：' + name)
            total += member.size
            require(total <= MAX_TOTAL, 'ui.tar 展开后超过 32 MiB')
            handle = archive.extractfile(member)
            require(handle is not None, 'ui.tar 文件不可读取：' + name)
            with handle:
                data = handle.read(member.size + 1)
            require(len(data) == member.size, 'ui.tar 文件长度错误：' + name)
            files[name] = data
    require('manifest.json' in files, 'ui.tar 缺少 manifest.json')
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'manifest 包含重复字段：' + key)
            result[key] = value
        return result
    manifest = json.loads(files['manifest.json'].decode('utf-8'), object_pairs_hook=unique_object)
    require(isinstance(manifest, dict) and type(manifest.get('schema_version')) is int
            and manifest['schema_version'] == 1 and isinstance(manifest.get('files'), dict),
            '内置界面 manifest 格式不支持')
    hashes = manifest['files']
    require(set(hashes) == set(files) - {'manifest.json'}, 'manifest 必须列出所有且仅列出非 manifest 文件')
    for name, digest in hashes.items():
        require(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest)
                and hashlib.sha256(files[name]).hexdigest() == digest, 'manifest 文件校验失败：' + name)
    require(all(files.get(name) for name in ('frontend/index.html', 'runtime/ui_service.py',
                                           'runtime/toolbox.py', 'runtime/insights.py', 'runtime/device_info.py', 'runtime/histor.py',
                                           'runtime/web_config.py', 'runtime/fan_policy.py', 'runtime/fan_native.py',
                                           'runtime/fan_watchdog.py', 'runtime/temperature_sources.py', 'frontend/web/index.html',
                                           'frontend/web/login.html', 'runtime/fnm_auth.py', 'runtime/fnm_compat.py',
                                           'runtime/light_policy.py', 'runtime/monitoring.py')),
            '内置界面缺少页面、采集或控制模块')
    return manifest, files


def verify_cache(cache, expected_tarhash):
    """Anchor cached extracted bytes to the hash of the retained original tar."""
    require(cache.is_dir() and not cache.is_symlink(), '内置界面缓存不是普通目录')
    _, files = read_bundle(cache / 'ui.tar', expected_tarhash)
    expected = set(files) | {'ui.tar'}
    directories = {str(parent) for name in files for parent in pathlib.PurePosixPath(name).parents
                   if str(parent) != '.'}
    for item in cache.rglob('*'):
        name, info = item.relative_to(cache).as_posix(), item.lstat()
        if stat.S_ISDIR(info.st_mode):
            require(name in directories, '内置界面缓存包含意外目录：' + name)
        else:
            require(name in expected, '内置界面缓存包含意外文件：' + name)
            maximum = MAX_ARCHIVE if name == 'ui.tar' else len(files[name])
            regular_file(item, maximum)
    for name, data in files.items():
        installed = cache / name
        require(regular_file(installed, len(data)).st_size == len(data)
                and hashlib.sha256(installed.read_bytes()).digest() == hashlib.sha256(data).digest(),
                '内置界面缓存校验失败：' + name)


def write_cache(stage, files, tar_path, tarhash):
    assembled = stage / 'cache'
    assembled.mkdir(mode=0o700)
    for name, data in files.items():
        output = assembled / name
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('xb') as handle:
            handle.write(data)
    with tar_path.open('rb') as source, (assembled / 'ui.tar').open('xb') as destination:
        shutil.copyfileobj(source, destination)
    verify_cache(assembled, tarhash)
    cache = runtime_root / ('ui-' + tarhash)
    if cache.exists() or cache.is_symlink():
        verify_cache(cache, tarhash)
        return cache
    assembled.rename(cache)
    verify_cache(cache, tarhash)
    return cache


def prepare_image_bundle(container):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container or ''), 'TASK_CONTAINER 不是有效容器名称或 ID')
    require_tmpfs(pathlib.Path('/run'))
    require(not runtime_root.is_symlink(), 'RAM 暂存目录不能是符号链接')
    runtime_root.mkdir(mode=0o700, exist_ok=True)
    require_tmpfs(runtime_root)
    stage = pathlib.Path(tempfile.mkdtemp(prefix='stage-', dir=runtime_root))
    print('内置界面 RAM 暂存目录:', stage)
    bundle = stage / 'bundle'
    bundle.mkdir()
    result = run('docker', '--host', 'unix:///var/run/docker.sock', 'cp',
                 container + ':/opt/r1-toolbox/ui-bundle/.', str(bundle))
    require(result.returncode == 0, '无法从当前镜像复制内置 UI 包；暂存目录保留至重启')
    require({item.name for item in bundle.iterdir()} == {'ui.tar', 'ui.sha256'}, '内置 UI 包目录必须仅包含 ui.tar 与 ui.sha256')
    regular_file(bundle / 'ui.sha256', 65)
    checksum = (bundle / 'ui.sha256').read_bytes()
    require(re.fullmatch(rb'[0-9a-fA-F]{64}\n?', checksum), 'ui.sha256 必须为一行 64 位十六进制 SHA256')
    tarhash = checksum.decode('ascii').strip().lower()
    _, files = read_bundle(bundle / 'ui.tar', tarhash)
    cache = runtime_root / ('ui-' + tarhash)
    if cache.exists() or cache.is_symlink():
        verify_cache(cache, tarhash)
        return cache
    return write_cache(stage, files, bundle / 'ui.tar', tarhash)


def settings_directory(container, required=False):
    """Resolve the public Docker data-volume mount, never an image layer path."""
    fallback = runtime_root / 'settings'
    try:
        result = run('docker', '--host', 'unix:///var/run/docker.sock', 'inspect',
                     '--format', '{{json .Mounts}}', container)
        require(result.returncode == 0, '无法读取数据卷')
        mounts = json.loads(result.stdout)
        require(isinstance(mounts, list), '数据卷信息不是数组')
        matches = [mount for mount in mounts if isinstance(mount, dict) and mount.get('Destination') == '/data']
        if not matches:
            require(not required, '未配置 /data 数据卷')
            print('未配置设置数据卷，设置仅在本次开机保留。')
            return fallback
        require(len(matches) == 1 and matches[0].get('Type') == 'volume'
                and matches[0].get('RW') is True, '/data 必须是可写 Docker 数据卷')
        source = pathlib.Path(matches[0].get('Source', ''))
        require(source.is_absolute() and source.is_dir(), 'Docker 数据卷源目录无效')
        source = source.resolve(strict=True)
        require(not os.statvfs(source).f_flag & os.ST_RDONLY, 'Docker 数据卷所在文件系统只读')
        require(source != runtime_root and runtime_root not in source.parents,
                '设置卷不能位于 Toolbox 的易失运行目录')
        official = app.resolve(strict=True)
        require(source != pathlib.Path('/') and source != official and source not in official.parents
                and official not in source.parents and not any(c in str(source) for c in '\n\r\x00'),
                'Docker 设置数据卷必须与官方程序目录独立')
        print('设置将保存在 Docker 独立数据卷中。')
        return source
    except (ValueError, OSError, RuntimeError, TypeError):
        if required:
            raise ValueError('未找到可写的持久化设置卷；请使用包含 r1-toolbox-data:/data 的新版 Compose，未停止原小屏') from None
        print('未找到可用的设置数据卷，设置仅在本次开机保留。')
        return fallback


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


Process = collections.namedtuple('Process', 'pid exe start is_main ppid process_type')


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


def start(name, environment, bind_source=None, backend=None):
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


def main():
    current = processes()
    print('解包目录:', '存在' if app.is_dir() else '不存在')
    print('官方 dist:', '存在' if target.is_dir() else '不存在')
    print('宿主依赖:', ', '.join(k + '=' + ('有' if shutil.which(k) else '无')
          for k in ('systemd-run', 'systemctl')))
    print('小屏进程数:', len(current))
    try:
        mode, source_value = select_mode(os.environ)
    except ValueError as error:
        print('预检失败，未停止原程序：', error)
        return 1
    print('界面模式:', mode)
    if mode == 'diagnostic':
        print('诊断模式：未停止进程、未挂载目录。')
        return 0

    try:
        backend = None
        if mode == 'image':
            cache = prepare_image_bundle(os.environ.get('TASK_CONTAINER', 'r1-toolbox'))
            incoming = resolve_source(str(cache / 'frontend'))
            backend = cache / 'runtime/ui_service.py'
        else:
            incoming = resolve_source(source_value)
        print('等待前端目录、官方 dist 和小屏主进程就绪（最多 120 秒）。', flush=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = processes()
            try:
                identify_family(current)
                family_ready = True
            except ValueError:
                family_ready = False
            if incoming.is_dir() and (incoming / 'index.html').is_file() and target.is_dir() and family_ready:
                break
            time.sleep(1)

        # Resolve again after the shared disk is ready, including any symlinks.
        incoming = resolve_source(source_value if mode == 'directory' else str(incoming))
        require(incoming.is_dir() and (incoming / 'index.html').is_file()
                and (incoming / 'index.html').stat().st_size > 0,
                'UI_DIR 必须是宿主已有的前端目录，且包含非空 index.html')
        require(target.is_dir() and (target / 'index.html').is_file()
                and (app / 'hiksemi-sub').is_file() and os.access(app / 'AppRun', os.X_OK),
                '请先解包 AppImage，确认原 dist/index.html 存在且 AppRun 可执行')
        require(all(shutil.which(k) for k in ('systemd-run', 'systemctl')), '宿主需要 systemd-run 和 systemctl')
        main_process = identify_family(current)
        require(all(p.exe == str(app / 'hiksemi-sub') or (
            p.exe.startswith('/tmp/.mount_Histor') and pathlib.Path(p.exe).parent.parent == pathlib.Path('/tmp'))
            for p in current), '发现来源不明的同名进程，已停止操作')
        raw = read_environment(main_process)
        allowed = {'DISPLAY', 'XAUTHORITY', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LANGUAGE',
                   'LC_ALL', 'LC_CTYPE', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'LD_LIBRARY_PATH'}
        environment = dict(base_env, APPDIR=str(app))
        environment.update((k, v) for item in raw.split(b'\0') if b'=' in item
                           for k, v in [item.decode(errors='surrogateescape').split('=', 1)] if k in allowed)
        if backend is not None:
            environment['R1_TOOLBOX_DATA_DIR'] = str(settings_directory(os.environ.get('TASK_CONTAINER', 'r1-toolbox'), required=True))
            environment['R1_TOOLBOX_VERSION'] = os.environ.get('TOOLBOX_VERSION', 'dev')
            web_bind = os.environ.get('R1_WEB_BIND', '0.0.0.0')
            web_port = os.environ.get('R1_WEB_PORT', '18732')
            require(web_bind in ('0.0.0.0', '127.0.0.1'), 'R1_WEB_BIND 只能是 0.0.0.0 或 127.0.0.1')
            require(web_port.isascii() and web_port.isdigit() and len(web_port) <= 5
                    and (int(web_port) == 0 or 1024 <= int(web_port) <= 65535),
                    'R1_WEB_PORT 需要为 1024–65535，或 0 禁用网页')
            environment['R1_WEB_BIND'] = web_bind
            environment['R1_WEB_PORT'] = web_port
            dev_no_pairing = os.environ.get('R1_DEV_NO_PAIRING', '')
            if dev_no_pairing not in ('', '0', '1'):
                raise SystemExit('R1_DEV_NO_PAIRING 只能是 0 或 1')
            if dev_no_pairing == '1':
                dev_ips = [item.strip() for item in os.environ.get('R1_DEV_ALLOW_IPS', '').split(',') if item.strip()]
                if not dev_ips:
                    raise SystemExit('开发者免配对模式需要设置 R1_DEV_ALLOW_IPS（仅允许指定客户端 IP）')
                for item in dev_ips:
                    try:
                        ipaddress.ip_address(item)
                    except ValueError:
                        raise SystemExit('R1_DEV_ALLOW_IPS 包含无效 IP') from None
                try:
                    dev_minutes = int(os.environ.get('R1_DEV_MINUTES', '60'))
                except ValueError:
                    raise SystemExit('R1_DEV_MINUTES 必须是整数') from None
                if not 1 <= dev_minutes <= 1440:
                    raise SystemExit('R1_DEV_MINUTES 需要在 1–1440 分钟')
                environment['R1_DEV_NO_PAIRING'] = '1'
                environment['R1_DEV_ALLOW_IPS'] = ','.join(dev_ips)
                environment['R1_DEV_MINUTES'] = str(dev_minutes)
        old_root = str(pathlib.Path(main_process.exe).parent)
        if 'LD_LIBRARY_PATH' in environment:
            environment['LD_LIBRARY_PATH'] = ':'.join(str(app) + p[len(old_root):] if p == old_root or p.startswith(old_root + '/') else p
                                                     for p in environment['LD_LIBRARY_PATH'].split(':'))
        require(environment.get('DISPLAY'), '现有小屏没有 DISPLAY；未停止原程序')
        require(not environment.get('XAUTHORITY') or pathlib.Path(environment['XAUTHORITY']).is_file(),
                'XAUTHORITY 文件不存在')
        preflight_binding(unit, incoming)
        current = processes()
        latest_main = identify_family(current)
        require((latest_main.pid, latest_main.start) == (main_process.pid, main_process.start),
                '只读挂载预检期间小屏主进程已变化，未停止原程序')
        require(all(p.exe == main_process.exe for p in current), '小屏进程来源已变化，未停止原程序')
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        print('预检失败，未停止原程序：', error)
        print('小屏进程诊断:', process_summary(current))
        return 1

    try:
        for sig, wait in ((signal.SIGTERM, 4), (signal.SIGKILL, 2)):
            live = {(p.pid, p.start) for p in processes()}
            for process in current:
                pid, start_time = process.pid, process.start
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
        if backend is None:
            start(unit, environment, bind_source=incoming)
        else:
            start(unit, environment, bind_source=incoming, backend=backend)
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
        if not processes():
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

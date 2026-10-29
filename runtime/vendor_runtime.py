"""Bounded kernel observations for fixed vendor services; never start or probe.

Socket ownership is evidence for a fixed local client, not task telemetry.
Only kernel proc attributes are opened. No command line, environment, user
configuration, executable bytes, media files or download directories are read.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import time

SERVICES = frozenset(('emhistor', 'appcenter', 'aria2c-nox', 'qbittorrent-nox',
                      'intelligent', 'image_ocr', 'image_convert', 'image_similar'))
PORTS = frozenset((5050, 6800, 8181, 8888, 9660, 7786, 7778, 7769, 7799))
MAX_PROCESSES, MAX_FDS, MAX_BYTES = 4096, 32768, 1024 * 1024


def _read(path, limit, deadline=None):
    descriptor = None
    try:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
                             | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        result = bytearray()
        while len(result) <= limit:
            if deadline is not None and time.monotonic() >= deadline:
                return None
            chunk = os.read(descriptor, min(8192, limit + 1 - len(result)))
            if not chunk:
                return bytes(result) if deadline is None or time.monotonic() < deadline else None
            result.extend(chunk)
        return None
    except (OSError, ValueError, OverflowError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _kernel_identity(folder, deadline=None):
    """Validate a complete Linux stat record against this PID and comm file."""
    comm = _read(folder / 'comm', 64, deadline)
    raw = _read(folder / 'stat', 4096, deadline)
    if not comm or not comm.endswith(b'\n') or not raw or not raw.endswith(b'\n'):
        return None
    try:
        name = comm[:-1]
        prefix = folder.name.encode('ascii') + b' ('
        closing = raw.rfind(b') ')
        if not raw.startswith(prefix) or closing < len(prefix) or raw[len(prefix):closing] != name:
            return None
        fields = raw[closing + 2:].split()
        # Linux 3.5+ has 52 fields; the supported firmware uses this layout.
        # A short EOF after starttime must not impersonate a complete stat file.
        if (len(fields) < 50 or fields[0] not in (b'R', b'S', b'D', b'Z', b'T', b't', b'W', b'X', b'x', b'K', b'I', b'P')
                or not re.fullmatch(rb'[0-9]{1,20}', fields[19])):
            return None
        start = int(fields[19])
        if start > 2 ** 64 - 1:
            return None
        return (int(folder.name), name, start)
    except (ValueError, UnicodeError, IndexError):
        return None


def _process(folder, deadline=None, kernel_identity=None):
    identity = kernel_identity or _kernel_identity(folder, deadline)
    if identity is None:
        return None
    pid, comm, start = identity
    try:
        name = comm.decode('ascii', 'strict')
    except UnicodeError:
        return None
    if name not in SERVICES:
        return None
    try:
        executable = os.readlink(folder / 'exe')
        # A deleted image is not the currently verified executable.
        exe_name = Path(executable).name
        token = hashlib.sha256(os.fsencode(executable)).digest()
    except (OSError, ValueError, UnicodeError):
        exe_name, token = None, None
    return {'pid': pid, 'name': name, 'exe_name': exe_name if exe_name in SERVICES else None,
            'start_ticks': start, 'identity_confirmed': exe_name == name,
            '_exe_token': token}


def _listener_table(proc, name, deadline=None):
    raw = _read(proc / ('net/' + name), MAX_BYTES, deadline)
    if raw is None:
        return [], False
    result = []
    lines = raw.splitlines()
    # Linux tcp4_seq_show and tcp6_seq_show use different remote column names.
    remote_column = b'remote_address' if name == 'tcp6' else b'rem_address'
    if (not lines or len(lines) > 16384 or not raw.endswith(b'\n')
            or lines[0].split()[:4] != [b'sl', b'local_address', remote_column, b'st']):
        return [], False
    complete = True
    for line in lines[1:]:
        if deadline is not None and time.monotonic() >= deadline:
            return result, False
        fields = line.split()
        if len(fields) < 10:
            complete = False
            continue
        try:
            address, port_text = fields[1].split(b':')
            width = 32 if name == 'tcp6' else 8
            if (len(address) != width or not re.fullmatch(rb'[0-9A-Fa-f]+', address)
                    or not re.fullmatch(rb'[0-9A-Fa-f]{4}', port_text)
                    or not re.fullmatch(rb'[0-9A-Fa-f]{2}', fields[3])):
                complete = False
                continue
            port = int(port_text, 16)
            if fields[3].upper() != b'0A' or port not in PORTS:
                continue
            # Linux exposes IPv6 addresses as four native-endian words.
            # Only fixed loopback/wildcard endpoints qualify, never LAN binds.
            address = address.upper()
            addresses = ({b'0100007F': '127.0.0.1', b'00000000': '0.0.0.0'} if name == 'tcp'
                         else {b'00000000000000000000000001000000': '::1', b'0' * 32: '::'})
            if address not in addresses:
                continue
            inode = fields[9].decode('ascii', 'strict')
            if not re.fullmatch(r'[1-9][0-9]{0,23}', inode):
                complete = False
                continue
            result.append({'address': addresses[address],
                           'port': port, 'socket_inode': inode, 'owner_pids': []})
        except (ValueError, UnicodeError):
            complete = False
    return result, complete


def _listeners(proc, deadline=None):
    """Read IPv4 plus an optional kernel IPv6 table, never infer dual-stack."""
    ipv4, complete = _listener_table(proc, 'tcp', deadline)
    try:
        (proc / 'net/tcp6').lstat()
    except FileNotFoundError:
        return ipv4, complete
    except OSError:
        return ipv4, False
    ipv6, complete6 = _listener_table(proc, 'tcp6', deadline)
    return ipv4 + ipv6, complete and complete6


def _socket_inodes(folder, deadline, budget=None, *, strict=True):
    found, complete = set(), True
    budget = [MAX_FDS] if budget is None else budget
    try:
        with os.scandir(folder / 'fd') as entries:
            for entry in entries:
                if budget[0] <= 0 or time.monotonic() >= deadline:
                    complete = False
                    break
                budget[0] -= 1
                if not entry.name.isdigit():
                    continue
                try:
                    target = os.readlink(entry.path)
                    match = re.fullmatch(r'socket:\[([1-9][0-9]{0,23})\]', target)
                    if match:
                        found.add(match[1])
                except FileNotFoundError:
                    # A vanished unrelated FD cannot still own the listener.
                    # Target services remain strict and are checked again.
                    if strict:
                        complete = False
                except OSError:
                    complete = False
    except FileNotFoundError:
        if strict or not _gone(folder):
            complete = False
    except OSError:
        complete = False
    return found, complete


def _gone(folder):
    """Only a confirmed absent PID may be ignored after normal process exit."""
    try:
        folder.stat()
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


def collect_runtime(root='/'):
    """Observe fixed process names and listeners, with bounded scan coverage."""
    proc = Path(root).absolute() / 'proc'
    deadline = time.monotonic() + 1.0
    listeners, table_complete = _listeners(proc, deadline)
    result = {'available': False, 'complete': False, 'processes': [], 'listeners': listeners,
              'reason': '原厂服务运行现场暂不可读取'}
    processes, tokens, scan_complete, pid_count, fd_budget = [], {}, True, 0, [MAX_FDS]
    try:
        with os.scandir(proc) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    scan_complete = False
                    break
                if not re.fullmatch(r'[1-9][0-9]{0,9}', entry.name):
                    continue
                pid_count += 1
                if pid_count > MAX_PROCESSES:
                    scan_complete = False
                    break
                folder = proc / entry.name
                before_identity = _kernel_identity(folder, deadline)
                if before_identity is None:
                    if not _gone(folder):
                        scan_complete = False
                    continue
                before = _process(folder, deadline, before_identity)
                # Other processes are scanned only to reject shared ownership.
                # Their names, executables and file paths never leave this module.
                inodes, fd_complete = _socket_inodes(folder, deadline, fd_budget,
                                                     strict=before is not None)
                after_identity = _kernel_identity(folder, deadline)
                after = _process(folder, deadline, after_identity) if before is not None else None
                if after_identity != before_identity or after != before:
                    if before is not None or not _gone(folder):
                        scan_complete = False
                    continue
                if before is not None:
                    processes.append({key: value for key, value in before.items() if not key.startswith('_')})
                    tokens[before['pid']] = before
                if not fd_complete:
                    scan_complete = False
                for listener in listeners:
                    if listener['socket_inode'] in inodes:
                        listener['owner_pids'].append(before_identity[0])
        # Compare only fixed listening sockets, never unrelated established
        # connections that change naturally while this bounded scan runs.
        final_listeners, final_complete = _listeners(proc, deadline)
        signature = lambda rows: sorted((row['address'], row['port'], row['socket_inode']) for row in rows)
        if not final_complete or signature(final_listeners) != signature(listeners):
            scan_complete = False
        for process in processes:
            folder = proc / str(process['pid'])
            owned = {row['socket_inode'] for row in listeners if process['pid'] in row['owner_pids']}
            if owned:
                final_inodes, fd_complete = _socket_inodes(folder, deadline, fd_budget)
                if not fd_complete or not owned.issubset(final_inodes):
                    scan_complete = False
                for listener in listeners:
                    if process['pid'] in listener['owner_pids'] and listener['socket_inode'] not in final_inodes:
                        listener['owner_pids'].remove(process['pid'])
            if _process(folder, deadline) != tokens[process['pid']]:
                process['identity_confirmed'] = False
                scan_complete = False
        if time.monotonic() >= deadline:
            scan_complete = False
        for listener in listeners:
            listener['owner_pids'].sort()
        result.update(available=True, complete=table_complete and scan_complete,
                      processes=sorted(processes, key=lambda row: row['pid']),
                      reason=None if table_complete and scan_complete else '运行现场仅部分可确认')
    except OSError:
        pass
    return result


def require_one_listener(root='/', port=6800, comm='aria2c-nox'):
    """Require one stable matching process owning the fixed local listener.

    Call before and after the fixed RPC and compare pid/start_ticks/inode.
    Internal arguments are fixture hooks; HTTP cannot select a port or name.
    """
    if (port, comm) not in ((6800, 'aria2c-nox'), (8181, 'qbittorrent-nox')):
        raise ValueError('仅允许已核实的下载服务监听身份')
    state = collect_runtime(root)
    result = {'available': False, 'status': 'proc_unavailable',
              'reason': '下载服务监听身份暂不可确认',
              'pid': None, 'start_ticks': None, 'socket_inode': None}
    if not state['available']:
        return result
    if not state['complete']:
        result.update(status='proc_partial', reason='下载运行现场仅部分可确认，未发送请求')
        return result
    listeners = [row for row in state['listeners'] if row['port'] == port]
    if not listeners:
        result.update(status='not_listening', reason='未观察到原厂下载服务的固定监听')
        return result
    if len(listeners) != 1:
        result.update(status='ambiguous', reason='下载服务监听存在竞争，未发送请求')
        return result
    listener = listeners[0]
    if comm == 'aria2c-nox' and listener['address'] not in ('127.0.0.1', '0.0.0.0'):
        result.update(status='owner_unverified', reason='aria2 未确认固定 IPv4 回环监听')
        return result
    matches = [row for row in state['processes'] if row['name'] == comm
               and row['identity_confirmed'] and row['pid'] in listener['owner_pids']]
    if len(matches) != 1 or listener['owner_pids'] != [matches[0]['pid']]:
        result.update(status='owner_unverified', reason='固定监听未归属唯一已核实的原厂下载进程')
        return result
    process = matches[0]
    result.update(available=True, status='verified', reason=None,
                  pid=process['pid'], start_ticks=process['start_ticks'],
                  socket_inode=listener['socket_inode'])
    if listener['address'] in ('::', '::1'):
        # Connect to IPv6 loopback directly. The system-wide bindv6only value
        # cannot prove the per-socket option and is deliberately not inferred.
        result['connect_host'] = '::1'
    return result

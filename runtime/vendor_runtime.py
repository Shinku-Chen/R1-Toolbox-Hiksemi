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
PORTS = frozenset((5050, 6800, 8181, 8888, 9660, 7786, 7778, 7769, 7799, 20443))
MAX_PROCESSES, MAX_FDS, MAX_BYTES = 4096, 32768, 1024 * 1024


def _observe_counter(observer, key):
    if observer is not None:
        observer['scan']['counters'][key] += 1


def _observe_reason(observer, reason):
    if observer is not None and reason not in observer['scan']['reason_codes']:
        observer['scan']['reason_codes'].append(reason)


def _observe_start(observer, started, deadline):
    """Reset a private per-check observer; its fields never authorize a client."""
    if observer is None:
        return
    remaining = None if deadline is None else max(0, int((deadline - started) * 1000))
    observer.clear()
    observer.update(schema=1, budget={
        'scan_limit_ms': 1000, 'external_remaining_ms': remaining,
        'effective_limit_ms': min(1000, remaining) if remaining is not None else 1000,
        'max_processes': MAX_PROCESSES, 'max_fds': MAX_FDS, 'max_bytes': MAX_BYTES},
        scan={'status': 'running', 'complete': False, 'elapsed_ms': 0,
              'reason_codes': [], 'counters': dict.fromkeys((
                  'seen', 'read_failed', 'vanished', 'generation_changed',
                  'identity_invalid', 'identity_changed', 'executable_changed',
                  'fd_seen', 'fd_read_failed', 'fd_vanished', 'fd_directory_failed',
                  'table_changed', 'deadline', 'pid_limit', 'fd_limit'), 0)},
        tables={'initial': 'not_run', 'final': 'not_run'}, processes=[],
        listener_snapshots={'initial': [], 'final': []},
        listener={'status': 'not_run', 'port': 20443, 'listener_count': 0,
                  'owner_count': None, 'matching_owner_count': None,
                  'owner_matches': None, 'identity': None,
                  'final': {'status': 'not_run', 'executable_path_matches': None,
                            'generation_matches': None}})


def _observe_listeners(observer, state):
    """Expose counts for the fixed TLS listener, never other service identities."""
    if observer is None:
        return
    rows = [row for row in state['listeners'] if row['port'] == 20443]
    item = observer['listener']
    item['listener_count'] = len(rows)
    if len(rows) == 1:
        owners = rows[0]['owner_pids']
        matches = [row for row in state['processes'] if row['name'] == 'emhistor'
                   and row['identity_confirmed'] and row['pid'] in owners]
        item.update(owner_count=len(owners), matching_owner_count=len(matches),
                    owner_matches=len(matches) == 1 and owners == [matches[0]['pid']])


def _observe_listener_snapshot(observer, phase, rows, *, owners=False):
    if observer is None:
        return
    fixed = [row for row in rows if row['port'] == 20443]
    observer['listener_snapshots'][phase] = [
        {'socket_inode': row['socket_inode'], 'address': row['address'],
         'owner_count': len(row['owner_pids']) if owners else None} for row in fixed[:16]]
    if len(fixed) > 16:
        _observe_reason(observer, 'listener_snapshots_truncated')


def _read(path, limit, deadline=None, *, observer=None):
    descriptor = None
    try:
        if deadline is not None and time.monotonic() >= deadline:
            _observe_reason(observer, 'attribute_deadline')
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
                             | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _observe_reason(observer, 'attribute_nonregular')
            return None
        result = bytearray()
        while len(result) <= limit:
            if deadline is not None and time.monotonic() >= deadline:
                _observe_reason(observer, 'attribute_deadline')
                return None
            chunk = os.read(descriptor, min(8192, limit + 1 - len(result)))
            if not chunk:
                if deadline is not None and time.monotonic() >= deadline:
                    _observe_reason(observer, 'attribute_deadline')
                    return None
                return bytes(result)
            result.extend(chunk)
        _observe_reason(observer, 'attribute_byte_limit')
        return None
    except (OSError, ValueError, OverflowError) as error:
        _observe_counter(observer, 'read_failed')
        _observe_reason(observer, 'attribute_vanished' if isinstance(error, FileNotFoundError)
                        else 'attribute_permission_denied' if isinstance(error, PermissionError)
                        else 'attribute_read_failed')
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _kernel_identity(folder, deadline=None, *, observer=None):
    """Validate a complete Linux stat record against this PID and comm file."""
    kwargs = {'observer': observer} if observer is not None else {}
    comm = _read(folder / 'comm', 64, deadline, **kwargs)
    raw = _read(folder / 'stat', 4096, deadline, **kwargs)
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


def _process(folder, deadline=None, kernel_identity=None, *, observer=None, identity_observation=None):
    kwargs = {'observer': observer} if observer is not None else {}
    identity = kernel_identity or _kernel_identity(folder, deadline, **kwargs)
    if identity_observation is not None:
        identity_observation['start_ticks'] = identity[2] if identity is not None else None
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


def _listener_table(proc, name, deadline=None, *, observer=None):
    kwargs = {'observer': observer} if observer is not None else {}
    raw = _read(proc / ('net/' + name), MAX_BYTES, deadline, **kwargs)
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


def _listeners(proc, deadline=None, *, observer=None):
    """Read IPv4 plus an optional kernel IPv6 table, never infer dual-stack."""
    kwargs = {'observer': observer} if observer is not None else {}
    ipv4, complete = _listener_table(proc, 'tcp', deadline, **kwargs)
    try:
        (proc / 'net/tcp6').lstat()
    except FileNotFoundError:
        return ipv4, complete
    except OSError:
        return ipv4, False
    ipv6, complete6 = _listener_table(proc, 'tcp6', deadline, **kwargs)
    return ipv4 + ipv6, complete and complete6


def _socket_inodes(folder, deadline, budget=None, *, strict=True, observer=None):
    found, complete = set(), True
    budget = [MAX_FDS] if budget is None else budget
    try:
        with os.scandir(folder / 'fd') as entries:
            for entry in entries:
                if budget[0] <= 0 or time.monotonic() >= deadline:
                    key = 'fd_limit' if budget[0] <= 0 else 'deadline'
                    _observe_counter(observer, key)
                    _observe_reason(observer, key)
                    complete = False
                    break
                budget[0] -= 1
                _observe_counter(observer, 'fd_seen')
                if not entry.name.isdigit():
                    continue
                try:
                    target = os.readlink(entry.path)
                    match = re.fullmatch(r'socket:\[([1-9][0-9]{0,23})\]', target)
                    if match:
                        found.add(match[1])
                except FileNotFoundError:
                    _observe_counter(observer, 'fd_vanished')
                    # A vanished unrelated FD cannot still own the listener.
                    # Target services remain strict and are checked again.
                    if strict:
                        complete = False
                        _observe_reason(observer, 'target_fd_vanished')
                except OSError:
                    _observe_counter(observer, 'fd_read_failed')
                    _observe_reason(observer, 'fd_read_failed')
                    complete = False
    except FileNotFoundError:
        _observe_counter(observer, 'fd_directory_failed')
        if strict or not _gone(folder):
            _observe_reason(observer, 'fd_directory_unavailable')
            complete = False
    except OSError:
        _observe_counter(observer, 'fd_directory_failed')
        _observe_reason(observer, 'fd_directory_unavailable')
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


def collect_runtime(root='/', *, observer=None, deadline=None):
    """Observe fixed services; optional diagnostics reuse this exact fail-closed scan.

    The one-second scan budget is never extended. An enclosing operation may
    supply an earlier absolute deadline. Only fixed emhistor identity records
    and aggregate coverage counters enter the private observer.
    """
    proc = Path(root).absolute() / 'proc'
    started = time.monotonic()
    _observe_start(observer, started, deadline)
    deadline = min(started + 1.0, deadline) if deadline is not None else started + 1.0
    observation = {'observer': observer} if observer is not None else {}
    listeners, table_complete = _listeners(proc, deadline, **observation)
    if observer is not None:
        observer['tables']['initial'] = 'passed' if table_complete else 'failed'
    _observe_listener_snapshot(observer, 'initial', listeners)
    if not table_complete:
        _observe_reason(observer, 'initial_table_incomplete')
    result = {'available': False, 'complete': False, 'processes': [], 'listeners': listeners,
              'reason': '原厂服务运行现场暂不可读取'}
    processes, tokens, scan_complete, pid_count, fd_budget = [], {}, True, 0, [MAX_FDS]
    try:
        with os.scandir(proc) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    _observe_counter(observer, 'deadline')
                    _observe_reason(observer, 'deadline')
                    scan_complete = False
                    break
                if not re.fullmatch(r'[1-9][0-9]{0,9}', entry.name):
                    continue
                pid_count += 1
                _observe_counter(observer, 'seen')
                if pid_count > MAX_PROCESSES:
                    _observe_counter(observer, 'pid_limit')
                    _observe_reason(observer, 'pid_limit')
                    scan_complete = False
                    break
                folder = proc / entry.name
                before_identity = _kernel_identity(folder, deadline, **observation)
                if before_identity is None:
                    if _gone(folder):
                        _observe_counter(observer, 'vanished')
                    else:
                        _observe_counter(observer, 'identity_invalid')
                        _observe_reason(observer, 'process_identity_unavailable')
                        scan_complete = False
                    continue
                before = _process(folder, deadline, before_identity)
                record = None
                if observer is not None and before is not None and before['name'] == 'emhistor':
                    if len(observer['processes']) < 16:
                        record = {'pid': before['pid'], 'start_ticks': before['start_ticks'],
                                  'executable_name_matches': before['identity_confirmed'],
                                  'after_scan_identity': 'not_run', 'final_identity': 'not_run',
                                  'final_start_ticks': None, 'final_fd': 'not_run'}
                        observer['processes'].append(record)
                    else:
                        _observe_reason(observer, 'process_records_truncated')
                # Other processes are scanned only to reject shared ownership.
                # Their names, executables and file paths never leave this module.
                inodes, fd_complete = _socket_inodes(folder, deadline, fd_budget,
                                                     strict=before is not None, observer=observer)
                after_identity = _kernel_identity(folder, deadline, **observation)
                after = _process(folder, deadline, after_identity, **observation) if before is not None else None
                if after_identity != before_identity or after != before:
                    gone = _gone(folder)
                    _observe_counter(observer, 'identity_changed')
                    if after_identity is None:
                        _observe_counter(observer, 'vanished' if gone else 'identity_invalid')
                    elif after_identity != before_identity:
                        _observe_counter(observer, 'generation_changed')
                    else:
                        _observe_counter(observer, 'executable_changed')
                    if record is not None:
                        record['after_scan_identity'] = 'failed'
                    if before is not None or not gone:
                        _observe_reason(observer, 'process_identity_changed')
                        scan_complete = False
                    continue
                if record is not None:
                    record['after_scan_identity'] = 'passed'
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
        _observe_listener_snapshot(observer, 'initial', listeners, owners=True)
        final_listeners, final_complete = _listeners(proc, deadline, **observation)
        _observe_listener_snapshot(observer, 'final', final_listeners)
        if observer is not None:
            observer['tables']['final'] = 'passed' if final_complete else 'failed'
        signature = lambda rows: sorted((row['address'], row['port'], row['socket_inode']) for row in rows)
        if not final_complete or signature(final_listeners) != signature(listeners):
            if not final_complete:
                _observe_reason(observer, 'final_table_incomplete')
            if signature(final_listeners) != signature(listeners):
                _observe_counter(observer, 'table_changed')
                _observe_reason(observer, 'listener_table_changed')
            scan_complete = False
        for process in processes:
            folder = proc / str(process['pid'])
            record = next((row for row in observer['processes'] if row['pid'] == process['pid']), None) if observer is not None else None
            owned = {row['socket_inode'] for row in listeners if process['pid'] in row['owner_pids']}
            if owned:
                final_inodes, fd_complete = _socket_inodes(folder, deadline, fd_budget, observer=observer)
                if record is not None:
                    record['final_fd'] = 'passed' if fd_complete and owned.issubset(final_inodes) else 'failed'
                if not fd_complete or not owned.issubset(final_inodes):
                    _observe_reason(observer, 'final_owner_fd_unverified')
                    scan_complete = False
                for listener in listeners:
                    if process['pid'] in listener['owner_pids'] and listener['socket_inode'] not in final_inodes:
                        listener['owner_pids'].remove(process['pid'])
            final_observation = {} if record is not None else None
            final_process = _process(folder, deadline, identity_observation=final_observation, **observation)
            if record is not None:
                record['final_start_ticks'] = final_observation['start_ticks']
            if final_process != tokens[process['pid']]:
                _observe_counter(observer, 'identity_changed')
                if final_process is not None and final_process['start_ticks'] != process['start_ticks']:
                    _observe_counter(observer, 'generation_changed')
                elif final_process is not None:
                    _observe_counter(observer, 'executable_changed')
                _observe_reason(observer, 'final_process_identity_changed')
                if record is not None:
                    record['final_identity'] = 'failed'
                process['identity_confirmed'] = False
                scan_complete = False
            elif record is not None:
                record['final_identity'] = 'passed'
        if time.monotonic() >= deadline:
            _observe_counter(observer, 'deadline')
            _observe_reason(observer, 'deadline')
            scan_complete = False
        for listener in listeners:
            listener['owner_pids'].sort()
        result.update(available=True, complete=table_complete and scan_complete,
                      processes=sorted(processes, key=lambda row: row['pid']),
                      reason=None if table_complete and scan_complete else '运行现场仅部分可确认')
    except OSError:
        _observe_counter(observer, 'read_failed')
        _observe_reason(observer, 'proc_directory_unavailable')
    if observer is not None:
        observer['scan'].update(status='passed' if result['complete'] else 'failed',
                                complete=result['complete'],
                                elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
        _observe_listeners(observer, result)
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


def require_emhistor_listener(root='/', port=20443, *, observer=None, deadline=None):
    """Require the unique factory TLS listener and exact executable pathname.

    This dedicated gate does not authorize a request. Its identity is compared
    before credential transmission and after a fixed business operation; the
    peer certificate is independently pinned by the transport.
    """
    if type(port) is not int or port != 20443:
        raise ValueError('仅允许原厂固定 TLS 监听')
    state = collect_runtime(root, observer=observer, deadline=deadline) if observer is not None or deadline is not None else collect_runtime(root)
    result = {'available': False, 'status': 'proc_unavailable', 'pid': None,
              'start_ticks': None, 'socket_inode': None, 'connect_host': None}

    def finish():
        if observer is not None:
            observer['listener']['status'] = result['status']
            if result['available']:
                observer['listener']['identity'] = {key: result[key] for key in ('pid', 'start_ticks', 'socket_inode')}
        return result

    if not state.get('available'):
        return finish()
    if not state.get('complete'):
        result['status'] = 'proc_partial'
        return finish()
    listeners = [row for row in state['listeners'] if row['port'] == port]
    if not listeners:
        result['status'] = 'not_listening'
        return finish()
    if len(listeners) != 1:
        result['status'] = 'ambiguous'
        return finish()
    listener = listeners[0]
    matches = [row for row in state['processes'] if row['name'] == 'emhistor'
               and row['identity_confirmed'] and row['pid'] in listener['owner_pids']]
    if len(matches) != 1 or listener['owner_pids'] != [matches[0]['pid']]:
        result['status'] = 'owner_unverified'
        return finish()
    process = matches[0]
    folder = Path(root).absolute() / 'proc' / str(process['pid'])
    try:
        executable = os.readlink(folder / 'exe')
    except (OSError, ValueError):
        executable = None
    identity = _kernel_identity(folder, deadline, observer=observer) if observer is not None else _kernel_identity(folder, deadline)
    if observer is not None:
        observer['listener']['final'].update(
            status='passed' if executable == '/histor/bin/emhistor'
            and identity == (process['pid'], b'emhistor', process['start_ticks']) else 'failed',
            executable_path_matches=executable == '/histor/bin/emhistor',
            generation_matches=identity == (process['pid'], b'emhistor', process['start_ticks']))
    if (executable != '/histor/bin/emhistor'
            or identity != (process['pid'], b'emhistor', process['start_ticks'])):
        result['status'] = 'owner_unverified'
        return finish()
    result.update(available=True, status='verified', pid=process['pid'],
                  start_ticks=process['start_ticks'], socket_inode=listener['socket_inode'],
                  connect_host='::1' if listener['address'] in ('::', '::1') else '127.0.0.1')
    return finish()

"""Real proc fixture files plus fake readlink; never inspect this host or NAS."""

import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('vendor_runtime_under_test', ROOT / 'runtime/vendor_runtime.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)
HEADER = '  sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n'
HEADER6 = '  sl local_address remote_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n'


def tcp(inode='12345', address='0100007F', port=6800, state='0A', slot=0):
    return f' {slot}: {address}:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000 0 0 {inode} 1 0000000000000000\n'


def tcp6(inode='12345', address='0' * 32, port=8181, state='0A', slot=0):
    return tcp(inode, address, port, state, slot).replace(' 00000000:0000 ', ' ' + '0' * 32 + ':0000 ')


def process_stat(pid=123, comm='aria2c-nox', start=100):
    fields = ['S', *(['0'] * 49)]
    fields[19] = str(start)
    return f'{pid} ({comm}) ' + ' '.join(fields) + '\n'


class VendorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='r1-proc-test-'))
        self.proc = self.root / 'proc'
        (self.proc / 'net').mkdir(parents=True)
        self.table(HEADER + tcp())
        self.links, self.opens = {}, []
        self.readlink_hook = None
        original_open = os.open

        def restricted_open(path, flags, *args, **kwargs):
            candidate = Path(path)
            self.opens.append(candidate)
            self.assertTrue(candidate.is_relative_to(self.proc), str(candidate))
            self.assertIn(candidate.name, ('tcp', 'tcp6', 'comm', 'stat'))
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            return original_open(path, flags, *args, **kwargs)

        def readlink(path, *args, **kwargs):
            candidate = Path(path)
            self.assertTrue(candidate.is_relative_to(self.proc), str(candidate))
            self.assertTrue(candidate.name == 'exe' or candidate.parent.name == 'fd')
            if self.readlink_hook:
                self.readlink_hook(candidate)
            value = self.links.get(candidate)
            if isinstance(value, BaseException):
                raise value
            if value is None:
                raise FileNotFoundError('fixture link missing')
            return value

        self.addCleanup(mock.patch.stopall)
        mock.patch.object(runtime.os, 'open', side_effect=restricted_open).start()
        mock.patch.object(runtime.os, 'readlink', side_effect=readlink).start()
        mock.patch.object(socket, 'socket', side_effect=AssertionError('禁止连接端口')).start()
        mock.patch.object(subprocess, 'run', side_effect=AssertionError('禁止运行命令')).start()
        mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('禁止启动进程')).start()
        self.process()

    def table(self, data):
        (self.proc / 'net/tcp').write_bytes(data.encode('ascii'))

    def process(self, pid=123, comm='aria2c-nox', exe=None, start=100, inodes=('12345',)):
        folder = self.proc / str(pid)
        (folder / 'fd').mkdir(parents=True, exist_ok=True)
        (folder / 'comm').write_bytes((comm + '\n').encode('utf-8'))
        (folder / 'stat').write_bytes(process_stat(pid, comm, start).encode('utf-8'))
        (folder / 'cmdline').write_text('PRIVATE-ARGS', encoding='ascii')
        (folder / 'environ').write_text('SECRET-TOKEN', encoding='ascii')
        self.links[folder / 'exe'] = exe or '/usr/bin/' + comm
        for index, inode in enumerate(inodes):
            descriptor = folder / 'fd' / str(index)
            descriptor.write_bytes(b'')
            self.links[descriptor] = f'socket:[{inode}]'
        return folder

    def gate(self):
        return runtime.require_one_listener(self.root)

    def assert_partial(self):
        result = self.gate()
        self.assertFalse(result['available'], result)
        self.assertEqual(result['status'], 'proc_partial')
        self.assertIsNone(result['pid'])
        self.assertIsNone(result['socket_inode'])

    def test_verified_loopback_and_wildcard_listener_has_exact_public_identity(self):
        for address in ('0100007F', '00000000'):
            self.table(HEADER + tcp(address=address))
            self.assertEqual(self.gate(), {'available': True, 'status': 'verified', 'reason': None,
                                          'pid': 123, 'start_ticks': 100, 'socket_inode': '12345'})
        state = runtime.collect_runtime(self.root)
        self.assertTrue(state['complete'])
        self.assertEqual(state['processes'], [{'pid': 123, 'name': 'aria2c-nox', 'exe_name': 'aria2c-nox',
                                               'start_ticks': 100, 'identity_confirmed': True}])
        self.assertEqual(state['listeners'][0]['owner_pids'], [123])
        payload = json.dumps(state)
        for private in ('/usr/bin', '_exe_token', 'SECRET-TOKEN', 'PRIVATE-ARGS', 'cmdline', 'environ'):
            self.assertNotIn(private, payload)

    def test_gate_only_accepts_fixed_port_and_comm_before_reading_proc(self):
        with mock.patch.object(runtime, 'collect_runtime', side_effect=AssertionError('参数不合法时不得采集')):
            for port, comm in ((5050, 'aria2c-nox'), (6800, 'emhistor'), ('6800', 'aria2c-nox'),
                               (True, 'aria2c-nox'), (8181, 'aria2c-nox'), (6800, 'qbittorrent-nox')):
                with self.subTest(port=port, comm=comm), self.assertRaises(ValueError):
                    runtime.require_one_listener(self.root, port=port, comm=comm)

    def test_fixed_qb_listener_requires_its_own_process_and_executable(self):
        self.table(HEADER + tcp(port=8181))
        self.process(pid=456, comm='qbittorrent-nox', inodes=('12345',))
        self.links[self.proc / '123/fd/0'] = 'socket:[54321]'
        result = runtime.require_one_listener(self.root, port=8181, comm='qbittorrent-nox')
        self.assertTrue(result['available'], result)
        self.assertEqual(result['pid'], 456)
        self.links[self.proc / '456/exe'] = '/usr/bin/aria2c-nox'
        self.assertEqual(runtime.require_one_listener(self.root, port=8181, comm='qbittorrent-nox')['status'],
                         'owner_unverified')

    def test_ipv6_qb_wildcard_and_loopback_use_fixed_ipv6_not_dual_stack_assumption(self):
        self.table(HEADER)
        self.process(pid=456, comm='qbittorrent-nox', inodes=('12345',))
        self.links[self.proc / '123/fd/0'] = 'socket:[54321]'
        for address in ('0' * 32, '00000000000000000000000001000000'):
            (self.proc / 'net/tcp6').write_bytes((HEADER6 + tcp6(address=address)).encode('ascii'))
            result = runtime.require_one_listener(self.root, port=8181, comm='qbittorrent-nox')
            self.assertTrue(result['available'], result)
            self.assertEqual(result['pid'], 456)
            self.assertEqual(result['connect_host'], '::1')
            state = runtime.collect_runtime(self.root)
            self.assertIn(state['listeners'][0]['address'], ('::', '::1'))

    def test_ipv4_ipv6_conflicting_fixed_listeners_are_ambiguous(self):
        self.table(HEADER + tcp(port=8181))
        (self.proc / 'net/tcp6').write_bytes((HEADER6 + tcp6('67890')).encode('ascii'))
        self.process(pid=456, comm='qbittorrent-nox', inodes=('67890',))
        # Even one process owning both candidates does not erase ambiguity.
        for inodes in (('67890',), ('12345', '67890')):
            self.process(pid=456, comm='qbittorrent-nox', inodes=inodes)
            result = runtime.require_one_listener(self.root, port=8181, comm='qbittorrent-nox')
            self.assertEqual(result['status'], 'ambiguous')

    def test_ipv6_external_bind_and_malformed_table_do_not_authorize_qb(self):
        self.table(HEADER)
        self.process(pid=456, comm='qbittorrent-nox', inodes=('12345',))
        (self.proc / 'net/tcp6').write_bytes((HEADER6 + tcp6(address='20010DB8' + '0' * 24)).encode('ascii'))
        self.assertEqual(runtime.require_one_listener(self.root, port=8181, comm='qbittorrent-nox')['status'],
                         'not_listening')
        (self.proc / 'net/tcp6').write_bytes(b'malformed\n')
        self.assert_partial()

    def test_protocol_specific_remote_header_is_required(self):
        for name, header, wrong_header, row in (
                ('tcp', HEADER, HEADER6, tcp()), ('tcp6', HEADER6, HEADER, tcp6())):
            with self.subTest(protocol=name):
                path = self.proc / 'net' / name
                path.write_bytes((header + row).encode('ascii'))
                listeners, complete = runtime._listener_table(self.proc, name)
                self.assertTrue(complete)
                self.assertEqual(len(listeners), 1)
                path.write_bytes((wrong_header + row).encode('ascii'))
                self.assertEqual(runtime._listener_table(self.proc, name), ([], False))

    def test_ipv4_header_on_ipv6_table_is_partial_not_an_empty_verified_inventory(self):
        self.table(HEADER)
        self.process(pid=456, comm='qbittorrent-nox', inodes=('12345',))
        (self.proc / 'net/tcp6').write_bytes((HEADER + tcp6()).encode('ascii'))
        result = runtime.require_one_listener(self.root, port=8181, comm='qbittorrent-nox')
        self.assertFalse(result['available'])
        self.assertEqual(result['status'], 'proc_partial')
        self.assertIsNone(result['pid'])

    def test_unavailable_proc_and_partial_inventory_are_distinct(self):
        with mock.patch.object(runtime, 'collect_runtime', return_value={
                'available': False, 'complete': False, 'processes': [], 'listeners': []}):
            self.assertEqual(self.gate()['status'], 'proc_unavailable')
        with mock.patch.object(runtime, 'collect_runtime', return_value={
                'available': True, 'complete': False, 'processes': [], 'listeners': []}):
            self.assertEqual(self.gate()['status'], 'proc_partial')

    def test_wrong_or_deleted_executable_is_not_confirmed_and_never_leaks_basename(self):
        for executable in ('/private/user/SECRET-PHOTO.jpg', '/usr/bin/image_ocr', '/usr/bin/aria2c-nox (deleted)'):
            self.links[self.proc / '123/exe'] = executable
            self.assertEqual(self.gate()['status'], 'owner_unverified')
            state = runtime.collect_runtime(self.root)
            self.assertFalse(state['processes'][0]['identity_confirmed'])
            self.assertNotIn('SECRET-PHOTO', json.dumps(state))
            self.assertNotIn('/private', json.dumps(state))
            self.assertNotIn('(deleted)', json.dumps(state))

    def test_missing_executable_link_cannot_authorize_rpc(self):
        self.links[self.proc / '123/exe'] = PermissionError('/private/SECRET')
        result = self.gate()
        self.assertFalse(result['available'])
        self.assertEqual(result['status'], 'owner_unverified')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_external_address_other_port_and_nonlistening_state_do_not_match(self):
        for row in (tcp(address='05001FAC'), tcp(port=9660), tcp(state='01')):
            self.table(HEADER + row)
            self.assertEqual(self.gate()['status'], 'not_listening')

    def test_no_task_or_no_listener_is_not_reported_as_scan_failure(self):
        self.table(HEADER)
        state = runtime.collect_runtime(self.root)
        self.assertTrue(state['available'])
        self.assertTrue(state['complete'])
        self.assertEqual(self.gate()['status'], 'not_listening')

    def test_multiple_fixed_listeners_are_ambiguous_even_with_one_service(self):
        self.table(HEADER + tcp() + tcp('67890', address='00000000', slot=1))
        self.process(inodes=('12345', '67890'))
        self.assertEqual(self.gate()['status'], 'ambiguous')

    def test_unlisted_process_sharing_socket_is_only_an_owner_rejection(self):
        self.process(pid=456, comm='SECRET-USER-JOB', exe='/private/SECRET-USER-JOB')
        state = runtime.collect_runtime(self.root)
        self.assertTrue(state['complete'])
        self.assertEqual(state['listeners'][0]['owner_pids'], [123, 456])
        self.assertEqual(len(state['processes']), 1)
        self.assertNotIn('SECRET-USER', json.dumps(state))
        self.assertEqual(self.gate()['status'], 'owner_unverified')

    def test_nonmatching_or_malformed_fd_link_cannot_fake_ownership(self):
        for target in ('socket:[99999]', 'socket:[12345]/private/file', '/private/socket:[12345]', 'socket:[012345]'):
            self.links[self.proc / '123/fd/0'] = target
            self.assertEqual(self.gate()['status'], 'owner_unverified')

    def test_inconsistent_pid_comm_and_short_stat_records_are_partial(self):
        file = self.proc / '123/stat'
        good = process_stat()
        bad = (process_stat(pid=999), process_stat(comm='image_ocr'), good.rstrip('\n'),
               '123 (aria2c-nox) S ' + '0 ' * 19 + '\n', good.replace(' S ', ' QQ ', 1),
               good.replace(' 100 ', ' -1 ', 1), good.replace(' 100 ', ' 18446744073709551616 ', 1))
        for body in bad:
            file.write_bytes(body.encode('ascii'))
            self.assert_partial()

    def test_process_replacement_during_fd_scan_is_not_published_as_current(self):
        changed = False

        def replace(path):
            nonlocal changed
            if path.parent.name == 'fd' and not changed:
                changed = True
                (self.proc / '123/stat').write_bytes(process_stat(start=999).encode('ascii'))

        self.readlink_hook = replace
        self.assert_partial()

    def test_exec_path_replacement_with_same_pid_comm_basename_and_ticks_is_detected(self):
        def replace(path):
            if path.parent.name == 'fd':
                self.links[self.proc / '123/exe'] = '/private/replacement/aria2c-nox'

        self.readlink_hook = replace
        self.assert_partial()

    def test_fd_disappearance_or_unreadable_directory_marks_partial_not_stopped(self):
        self.links[self.proc / '123/fd/0'] = FileNotFoundError('vanished')
        self.assert_partial()
        self.links[self.proc / '123/fd/0'] = 'socket:[12345]'
        original = os.scandir

        def denied(path):
            if Path(path).name == 'fd':
                raise PermissionError('fixture')
            return original(path)

        with mock.patch.object(runtime.os, 'scandir', side_effect=denied):
            self.assert_partial()

    def test_vanished_unrelated_fd_does_not_invalidate_stable_download_owner(self):
        self.process(pid=456, comm='ordinary', inodes=('67890',))
        self.links[self.proc / '456/fd/0'] = FileNotFoundError('normal unrelated FD close')
        self.assertTrue(self.gate()['available'])
        # Permission failures could hide an owner and must remain fail-closed.
        self.links[self.proc / '456/fd/0'] = PermissionError('unreadable descriptor')
        self.assert_partial()

    def test_unrelated_process_exit_is_ignored_but_target_exit_remains_partial(self):
        folder = self.process(pid=456, comm='ordinary', inodes=('67890',))

        def disappear(process):
            # Linux rmtree opens directory FDs through the global os.open mock.
            # Remove only our known fixture nodes, retaining the read fence.
            self.assertEqual(process.parent, self.proc)
            for name in ('comm', 'stat', 'cmdline', 'environ'):
                (process / name).unlink()
            for descriptor in (process / 'fd').iterdir():
                descriptor.unlink()
            (process / 'fd').rmdir()
            process.rmdir()

        def exit_ordinary(path):
            if path.parent == folder / 'fd':
                disappear(folder)

        self.readlink_hook = exit_ordinary
        self.assertTrue(self.gate()['available'])
        target = self.proc / '123'

        def exit_target(path):
            if path.parent == target / 'fd' and target.exists():
                disappear(target)

        self.readlink_hook = exit_target
        self.assert_partial()

    def test_unrelated_live_process_identity_change_still_invalidates_coverage(self):
        folder = self.process(pid=456, comm='ordinary', inodes=('67890',))

        def replace(path):
            if path.parent == folder / 'fd':
                (folder / 'stat').write_bytes(process_stat(456, 'ordinary', 999).encode('ascii'))

        self.readlink_hook = replace
        self.assert_partial()

    def test_fd_limit_is_global_across_allowed_and_other_processes(self):
        self.process(pid=456, comm='ordinary', inodes=('67890',))
        with mock.patch.object(runtime, 'MAX_FDS', 1):
            self.assert_partial()

    def test_process_limit_reports_partial_instead_of_assuming_other_owners_absent(self):
        self.process(pid=456, comm='ordinary', inodes=())
        with mock.patch.object(runtime, 'MAX_PROCESSES', 1):
            self.assert_partial()

    def test_short_reads_are_reassembled_through_eof_and_oversize_is_rejected(self):
        original = os.read
        calls = []

        def short_read(descriptor, length):
            calls.append(length)
            return original(descriptor, min(length, 3))

        with mock.patch.object(runtime.os, 'read', side_effect=short_read):
            self.assertTrue(self.gate()['available'])
        self.assertGreater(len(calls), 10)
        size = (self.proc / 'net/tcp').stat().st_size
        with mock.patch.object(runtime, 'MAX_BYTES', size):
            self.assertTrue(self.gate()['available'])
        with mock.patch.object(runtime, 'MAX_BYTES', size - 1):
            self.assert_partial()

    def test_missing_table_incomplete_header_or_trailing_row_never_authorizes_rpc(self):
        for body in ('garbage\n' + tcp(), HEADER + tcp().rstrip('\n'), HEADER + 'partial\n', HEADER + tcp(state='GG')):
            self.table(body)
            self.assert_partial()
        (self.proc / 'net/tcp').unlink()
        self.assert_partial()

    def test_table_line_limit_rejects_a_partial_inventory(self):
        self.table(HEADER + tcp() + (tcp(port=80, state='01') * 16383))
        self.assert_partial()

    def test_listener_rebind_without_process_restart_is_rejected_by_final_table(self):
        def rebind(path):
            if path.parent.name == 'fd':
                self.table(HEADER + tcp('67890'))

        self.readlink_hook = rebind
        self.assert_partial()

    def test_unrelated_established_connections_do_not_invalidate_fixed_listener(self):
        original = runtime._read
        reads = 0

        def table_changes(path, limit, deadline=None):
            nonlocal reads
            if Path(path).name == 'tcp':
                reads += 1
                if reads == 2:
                    self.table(HEADER + tcp() + tcp('67890', port=443, state='01', slot=1))
            return original(path, limit, deadline)

        with mock.patch.object(runtime, '_read', side_effect=table_changes):
            self.assertTrue(self.gate()['available'])

    def test_final_process_identity_is_checked_after_other_process_scanning(self):
        original = runtime._read
        reads = 0

        def replace_on_final_table(path, limit, deadline=None):
            nonlocal reads
            result = original(path, limit, deadline)
            if Path(path).name == 'tcp':
                reads += 1
                if reads == 2:
                    (self.proc / '123/stat').write_bytes(process_stat(start=999).encode('ascii'))
            return result

        with mock.patch.object(runtime, '_read', side_effect=replace_on_final_table):
            self.assert_partial()

    def test_final_owner_fd_is_rechecked_even_when_pid_and_listener_table_stay_equal(self):
        original = runtime._read
        reads = 0

        def release_fd_after_table(path, limit, deadline=None):
            nonlocal reads
            result = original(path, limit, deadline)
            if Path(path).name == 'tcp':
                reads += 1
                if reads == 2:
                    self.links[self.proc / '123/fd/0'] = '/dev/null'
            return result

        with mock.patch.object(runtime, '_read', side_effect=release_fd_after_table):
            self.assert_partial()

    def test_total_deadline_also_covers_initial_table_read(self):
        now = [0.0]
        original = os.read

        def slow_read(descriptor, size):
            data = original(descriptor, size)
            now[0] = 2.0
            return data

        with mock.patch.object(runtime.time, 'monotonic', side_effect=lambda: now[0]), \
                mock.patch.object(runtime.os, 'read', side_effect=slow_read):
            self.assert_partial()

    def test_deadline_during_fd_scan_is_partial(self):
        now = [0.0]

        def delayed(path):
            if path.parent.name == 'fd':
                now[0] = 2.0

        self.readlink_hook = delayed
        with mock.patch.object(runtime.time, 'monotonic', side_effect=lambda: now[0]):
            self.assert_partial()

    def test_nonregular_proc_attribute_is_rejected_and_descriptor_closed(self):
        original = os.fstat

        def directory(descriptor):
            info = original(descriptor)
            return os.stat_result((stat.S_IFDIR | 0o700, *info[1:]))

        with mock.patch.object(runtime.os, 'fstat', side_effect=directory):
            self.assert_partial()

    def test_emhistor_business_gate_requires_exact_executable_and_fixed_tls_port(self):
        self.table(HEADER + tcp(port=20443))
        self.process(pid=456, comm='emhistor', exe='/histor/bin/emhistor', inodes=('12345',))
        self.links[self.proc / '123/fd/0'] = 'socket:[54321]'
        result = runtime.require_emhistor_listener(self.root)
        self.assertEqual(result, {'available': True, 'status': 'verified', 'pid': 456,
            'start_ticks': 100, 'socket_inode': '12345', 'connect_host': '127.0.0.1'})
        self.links[self.proc / '456/exe'] = '/untrusted/emhistor'
        self.assertEqual(runtime.require_emhistor_listener(self.root)['status'], 'owner_unverified')
        self.assertFalse(runtime.require_emhistor_listener(self.root)['available'])
        for port in (80, 8181, '20443', True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                runtime.require_emhistor_listener(self.root, port)

    def test_emhistor_gate_rejects_competitors_and_uses_verified_ipv6_family(self):
        self.table(HEADER)
        (self.proc / 'net/tcp6').write_text(HEADER6 + tcp6(port=20443), encoding='ascii')
        self.process(pid=456, comm='emhistor', exe='/histor/bin/emhistor', inodes=('12345',))
        self.links[self.proc / '123/fd/0'] = 'socket:[54321]'
        self.assertEqual(runtime.require_emhistor_listener(self.root)['connect_host'], '::1')
        self.table(HEADER + tcp(port=20443, inode='777'))
        self.assertEqual(runtime.require_emhistor_listener(self.root)['status'], 'ambiguous')
        self.table(HEADER)
        self.links[self.proc / '123/fd/0'] = 'socket:[12345]'
        self.assertEqual(runtime.require_emhistor_listener(self.root)['status'], 'owner_unverified')


@unittest.skipUnless(sys.platform.startswith('linux'), '需要 Linux 原生 /proc 网络表头')
class NativeProcHeaderTests(unittest.TestCase):
    def test_native_kernel_headers_parse_with_synthetic_fixed_listeners(self):
        # Read only the header, not this host's connection rows or process FDs.
        with tempfile.TemporaryDirectory(prefix='r1-native-proc-header-') as folder:
            proc = Path(folder) / 'proc'
            (proc / 'net').mkdir(parents=True)
            for name, remote, row in (('tcp', b'rem_address', tcp()),
                                       ('tcp6', b'remote_address', tcp6())):
                try:
                    with Path('/proc/net', name).open('rb') as stream:
                        header = stream.readline(4097)
                except (FileNotFoundError, PermissionError) as error:
                    if name == 'tcp6':
                        self.skipTest('宿主 IPv6 网络表不可读取：' + type(error).__name__)
                    raise
                self.assertLessEqual(len(header), 4096)
                self.assertEqual(header.split()[:4], [b'sl', b'local_address', remote, b'st'])
                (proc / 'net' / name).write_bytes(header + row.encode('ascii'))
                listeners, complete = runtime._listener_table(proc, name)
                self.assertTrue(complete, name)
                self.assertEqual(len(listeners), 1, name)


if __name__ == '__main__':
    unittest.main()

"""Real worker methods with fixed OS seams; never run SMART or real statvfs."""
import array
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('capacity_worker_diagnostic_service', ROOT/'runtime/ui_service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class Forward:
    def __init__(self, original, **overrides):
        self.original, self.overrides = original, overrides

    def __getattr__(self, name):
        return self.overrides[name] if name in self.overrides else getattr(self.original, name)


class FixedWorker:
    """Real production JSON/measure/probe bodies; only external boundaries fake."""
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-capacity-worker-metadata-')
        self.root = Path(self.temp.name)
        self.proc, self.sys = self.root/'proc', self.root/'sys'
        (self.proc/'self/fdinfo').mkdir(parents=True)
        self.sys.mkdir()
        (self.proc/'self/mountinfo').write_text(
            '11 1 8:1 / /drives/fixture rw - ext4 /dev/sda1 rw\n', encoding='utf-8', newline='\n')
        self.identity = ('/sys/devices/pci/block/sda/sda1', '/sys/devices/pci/block/sda',
            (2, 31, 101), (2, 32, 102), (2, 30, 100), '8:0', '17', 'sda')
        self.fds, self.next_fd, self.fstat_calls = {}, 10000, {}
        self.measurements, self.inventory_reads, self.fd_reads = 0, 0, 0
        self.child_mnt, self.received_mnt, self.after_mnt = 11, 11, None
        self.change_after_measure, self.non_directory = False, False
        self.actual_device, self.last_request, self.worker_json = 2049, None, None
        self.bad_diagnostic, self.entry_failure = None, None
        self.response_override, self.received = None, False
        self.evidence = []
        self.seams = contextlib.ExitStack()
        self.true_inventory, self.true_fd_mount = service._capacity_inventory, service._capacity_fd_mount
        self.true_os, self.true_sys, self.true_socket = service.os, service.sys, service.socket

    def newfd(self, mnt=11):
        fd = self.next_fd
        self.next_fd += 1
        self.fds[fd] = {'mnt': mnt, 'ino': fd, 'dev': self.actual_device}
        (self.proc/'self/fdinfo'/str(fd)).write_text(
            f'pos:\t0\nflags:\t0100000\nmnt_id:\t{mnt}\n', encoding='utf-8', newline='\n')
        return fd

    def open(self, path, flags, *, dir_fd=None):
        if str(path) == '/' or dir_fd in self.fds:
            return self.newfd(self.child_mnt)
        return self.true_os.open(path, self.true_os.O_RDONLY | getattr(self.true_os, 'O_BINARY', 0))

    def close(self, fd):
        if fd in self.fds:
            del self.fds[fd]
        else:
            return self.true_os.close(fd)

    def fstat(self, fd):
        if fd not in self.fds:
            return self.true_os.fstat(fd)
        row = self.fds[fd]
        count = self.fstat_calls.get(fd, 0)+1
        self.fstat_calls[fd] = count
        inode = row['ino']+(100 if self.change_after_measure and count > 1 else 0)
        return types.SimpleNamespace(st_mode=(stat.S_IFREG if self.non_directory else stat.S_IFDIR)|0o755,
                                     st_dev=row['dev'], st_ino=inode)

    def fstatvfs(self, fd):
        assert fd in self.fds
        self.measurements += 1
        return types.SimpleNamespace(f_frsize=4096, f_bsize=4096, f_blocks=100, f_bfree=25)

    def inventory(self, proc_root, sys_root):
        self.inventory_reads += 1
        if self.entry_failure is not None:
            raise self.entry_failure
        return self.true_inventory(self.proc, self.sys)

    def fd_mount(self, fd, proc_root='/proc'):
        self.fd_reads += 1
        if self.after_mnt is not None and self.fd_reads == 2:
            return self.after_mnt
        return self.true_fd_mount(fd, self.proc)

    def __enter__(self):
        fixed_os = Forward(self.true_os, open=self.open, close=self.close, fstat=self.fstat,
            dup=lambda fd: self.newfd(self.fds[fd]['mnt']), fstatvfs=self.fstatvfs,
            major=lambda dev: dev//256, minor=lambda dev: dev%256,
            set_inheritable=lambda fd, value: None, O_DIRECTORY=65536, O_NOFOLLOW=131072,
            O_CLOEXEC=262144, O_NONBLOCK=524288)
        self.seams.enter_context(mock.patch.object(service, 'os', fixed_os))
        self.seams.enter_context(mock.patch.object(service, 'sys', Forward(self.true_sys, platform='linux')))
        self.seams.enter_context(mock.patch.object(service, '_capacity_inventory', side_effect=self.inventory))
        self.seams.enter_context(mock.patch.object(service, '_capacity_device', side_effect=lambda *_: self.identity))
        self.seams.enter_context(mock.patch.object(service, '_capacity_fd_mount', side_effect=self.fd_mount))
        def channel_close():
            if self.payload is not None and not self.received:
                handles = array.array('i')
                handles.frombytes(self.payload[1][0][2])
                for fd in handles:
                    if fd in self.fds:
                        self.close(fd)
        self.channel = types.SimpleNamespace(setblocking=lambda flag: None, close=channel_close)
        self.child = types.SimpleNamespace(fileno=lambda: 72, close=lambda: None)
        self.payload = None
        def sendmsg(buffers, ancillary):
            received = self.newfd(self.received_mnt)
            self.payload = (b''.join(buffers), [(1, 1, array.array('i', [received]).tobytes())], 0, None)
        self.worker_channel = types.SimpleNamespace(sendmsg=sendmsg, close=lambda: None)
        def receive(*args):
            self.received = True
            return self.payload
        self.channel.recvmsg = receive
        facade = Forward(self.true_socket, socketpair=lambda: (self.channel, self.child),
            socket=lambda fileno: self.worker_channel, SCM_RIGHTS=1, SOL_SOCKET=1, CMSG_SPACE=lambda size: size+32)
        self.seams.enter_context(mock.patch.object(service, 'socket', facade))
        fixture = self
        class Input(io.BytesIO):
            def close(self):
                if not self.closed:
                    raw = self.getvalue()
                    fixture.last_request = raw
                    out = io.StringIO()
                    worker_sys = Forward(service.sys, stdin=types.SimpleNamespace(buffer=io.BytesIO(raw)))
                    def emit(*args, **kwargs):
                        kwargs['file'] = out
                        print(*args, **kwargs)
                    with mock.patch.object(service, 'sys', worker_sys), mock.patch.dict(service.__dict__, {'print': emit}):
                        fixture.worker.returncode = service._capacity_worker_main()
                    fixture.worker_json = json.loads(out.getvalue())
                    if fixture.bad_diagnostic is not None:
                        fixture.worker_json['diagnostic'] = fixture.bad_diagnostic
                    fixture.worker.stdout = io.BytesIO(fixture.response_override if fixture.response_override is not None
                        else json.dumps(fixture.worker_json).encode()+b'\n')
                super().close()
        self.worker = types.SimpleNamespace(stdin=Input(), stdout=io.BytesIO(), returncode=None,
            poll=lambda: self.worker.returncode, kill=lambda: setattr(self.worker, 'returncode', -9))
        self.seams.enter_context(mock.patch.object(service.subprocess, 'Popen', return_value=self.worker))
        self.sampler = service.StatusSampler(self.proc, self.sys)
        self.sampler._capacity_attempt_step = lambda stage, **fields: self.evidence.append((stage, fields))
        return self

    def __exit__(self, *args):
        self.sampler.close_capacity()
        self.seams.close()
        self.temp.cleanup()

    def entry(self):
        rows, complete = self.inventory(self.proc, self.sys)
        assert complete and len(rows) == 1 and rows[0]['_key']
        return rows[0]

    def probe(self, entry=None):
        return self.sampler._capacity_probe(self.entry() if entry is None else entry,
                                            time.monotonic()+3, lambda: None)


class CapacityWorkerDiagnosticTests(unittest.TestCase):
    def rejected(self, configure, phase, reason, *, measurements, fd_reads):
        with FixedWorker() as f:
            configure(f)
            with self.assertRaises(service.CapacityRefreshError) as caught:
                f.probe()
            self.assertEqual(caught.exception.reason, 'identity_changed')
            diagnostic = f.worker_json['diagnostic']
            self.assertEqual(diagnostic['steps'][phase]['reason'], reason)
            self.assertEqual(diagnostic['steps'][phase]['status'], 'failed')
            self.assertEqual(f.measurements, measurements)
            self.assertEqual(f.fd_reads, fd_reads)
            self.assertFalse(f.fds)
            return diagnostic

    def test_real_method_pipeline_records_all_phases_without_new_reads(self):
        with FixedWorker() as f:
            entry = f.entry()
            entry.capacity_identity = ('private-marker-not-serialized-'*200,)*8
            values, fd = f.probe(entry)
            self.assertEqual(values, {'total_bytes': 409600, 'used_bytes': 307200, 'usage_percent': 75.0})
            steps = f.worker_json['diagnostic']['steps']
            self.assertEqual(set(steps), set(service._CAPACITY_WORKER_PHASES))
            self.assertTrue(all(step['status'] == 'passed' for step in steps.values()))
            self.assertEqual(f.inventory_reads, 4)
            self.assertEqual(f.fd_reads, 3)
            self.assertEqual(f.measurements, 1)
            self.assertNotIn(b'private-marker', f.last_request)
            self.assertLessEqual(len(json.dumps(f.worker_json).encode())+1, 4096)
            self.assertTrue(any(stage == 'parent_scm' and item['status'] == 'passed' for stage, item in f.evidence))
            f.close(fd)
            self.assertFalse(f.fds)

    def test_pre_fd_mount_rejection_and_dependencies_not_run(self):
        value = self.rejected(lambda f: setattr(f, 'child_mnt', 12), 'fd_before', 'mount_mismatch',
                              measurements=0, fd_reads=1)
        self.assertEqual(value['steps']['measure']['status'], 'not_run')
        self.assertEqual(value['steps']['inventory_opened']['status'], 'not_run')

    def test_non_directory_short_circuit_does_not_read_fdinfo(self):
        self.rejected(lambda f: setattr(f, 'non_directory', True), 'fd_before', 'fd_not_directory',
                      measurements=0, fd_reads=0)

    def test_device_mismatch_short_circuit_does_not_read_fdinfo(self):
        self.rejected(lambda f: setattr(f, 'actual_device', 2050), 'fd_before', 'device_mismatch',
                      measurements=0, fd_reads=0)

    def test_post_stat_change_short_circuit_does_not_read_second_fdinfo(self):
        self.rejected(lambda f: setattr(f, 'change_after_measure', True), 'fd_after', 'metadata_changed',
                      measurements=1, fd_reads=1)

    def test_post_fd_mount_rejection_is_distinct(self):
        self.rejected(lambda f: setattr(f, 'after_mnt', 12), 'fd_after', 'mount_mismatch',
                      measurements=1, fd_reads=2)

    def test_parent_received_fd_mismatch_preserves_successful_child_evidence(self):
        with FixedWorker() as f:
            f.received_mnt = 12
            with self.assertRaises(service.CapacityRefreshError) as caught:
                f.probe()
            self.assertEqual(caught.exception.reason, 'identity_changed')
            self.assertTrue(f.worker_json['ok'])
            self.assertTrue(all(row['status'] == 'passed' for row in f.worker_json['diagnostic']['steps'].values()))
            record = [row for phase, row in f.evidence if phase == 'parent_scm'][-1]
            self.assertEqual(record['reason'], 'mount_mismatch')
            self.assertEqual(record['observations']['actual_mount_id'], 12)
            self.assertFalse(f.fds)

    def test_observer_and_digest_faults_do_not_add_reads_or_change_result(self):
        for target in ('_capacity_worker_step', '_capacity_worker_stat_digest'):
            with self.subTest(target=target), FixedWorker() as f, \
                    mock.patch.object(service, target, side_effect=ValueError('private-observer-canary')):
                values, fd = f.probe()
                self.assertEqual(values['usage_percent'], 75.0)
                self.assertEqual((f.inventory_reads, f.fd_reads, f.measurements), (4, 3, 1))
                self.assertEqual(f.worker_json['diagnostic']['observation_status'], 'observer_error')
                self.assertNotIn('private-observer-canary', json.dumps(f.worker_json))
                f.close(fd)

    def test_ledger_entry_fault_does_not_change_parent_result_or_fd_cleanup(self):
        with FixedWorker() as f:
            f.sampler._capacity_attempt_step = mock.Mock(side_effect=ValueError('private-ledger-canary'))
            values, fd = f.probe()
            self.assertEqual(values['usage_percent'], 75.0)
            self.assertEqual(f.fd_reads, 3)
            f.close(fd)
            self.assertFalse(f.fds)

    def test_parent_observer_boundary_fault_does_not_change_business_or_read_counts(self):
        for target in ('boundary', 'forward', 'both'):
            with self.subTest(target=target), FixedWorker() as f, contextlib.ExitStack() as stack:
                if target in ('boundary', 'both'):
                    stack.enter_context(mock.patch.object(f.sampler, '_capacity_observe',
                        side_effect=ValueError('private-boundary-canary')))
                if target in ('forward', 'both'):
                    stack.enter_context(mock.patch.object(service, '_capacity_worker_forward',
                        side_effect=ValueError('private-forward-canary')))
                values, fd = f.probe()
                self.assertEqual(values, {'total_bytes': 409600, 'used_bytes': 307200,
                                          'usage_percent': 75.0})
                self.assertEqual((f.inventory_reads, f.fd_reads, f.measurements), (4, 3, 1))
                self.assertNotIn('private-', json.dumps(f.worker_json))
                f.close(fd)
                self.assertFalse(f.fds)

    def test_observer_boundary_fault_does_not_mask_business_json_rejection(self):
        with FixedWorker() as f:
            f.response_override = b'{'
            with mock.patch.object(f.sampler, '_capacity_observe', side_effect=ValueError('observer')):
                with self.assertRaises(service.CapacityRefreshError) as caught:
                    f.probe()
            self.assertEqual(caught.exception.reason, 'probe_failed')
            self.assertEqual(f.measurements, 1)
            self.assertFalse(f.fds)

    def test_observer_fault_preserves_successful_authorization_call_order(self):
        for fail_observer in (False, True):
            with self.subTest(fail_observer=fail_observer), FixedWorker() as f, contextlib.ExitStack() as stack:
                calls = []
                if fail_observer:
                    stack.enter_context(mock.patch.object(f.sampler, '_capacity_observe',
                        side_effect=ValueError('private-boundary')))
                entry = f.entry()
                values, fd = f.sampler._capacity_probe(entry, time.monotonic()+3,
                    lambda: calls.append((f.last_request is not None, f.received)))
                self.assertEqual(calls, [(False, False), (False, False), (True, False), (True, True)])
                self.assertEqual(values['usage_percent'], 75.0)
                self.assertEqual((f.inventory_reads, f.fd_reads, f.measurements), (4, 3, 1))
                f.close(fd)
                self.assertFalse(f.fds)

    def test_diagnostic_construction_and_encoding_faults_fall_back_to_business_json(self):
        for target in ('_capacity_worker_trace', '_capacity_worker_encode', '_capacity_worker_diagnostic'):
            with self.subTest(target=target), FixedWorker() as f, \
                    mock.patch.object(service, target, side_effect=ValueError('private-encode-canary')):
                values, fd = f.probe()
                self.assertEqual(values['usage_percent'], 75.0)
                self.assertTrue(f.worker_json['ok'])
                self.assertNotIn('private-encode-canary', json.dumps(f.worker_json))
                f.close(fd)

    def test_diagnostic_constructor_and_encoder_double_fault_keeps_business_result(self):
        with FixedWorker() as f, \
                mock.patch.object(service, '_capacity_worker_trace', side_effect=ValueError('private-constructor')), \
                mock.patch.object(service, '_capacity_worker_encode', side_effect=ValueError('private-encoder')):
            values, fd = f.probe()
            self.assertEqual(values['usage_percent'], 75.0)
            self.assertEqual((f.inventory_reads, f.fd_reads, f.measurements), (4, 3, 1))
            self.assertNotIn('diagnostic', f.worker_json)
            self.assertNotIn('private-', json.dumps(f.worker_json))
            f.close(fd)
            self.assertFalse(f.fds)

    def test_parent_private_decoder_fault_is_observation_only(self):
        real_decoder = service._capacity_worker_diagnostic
        calls = []

        def decode(value):
            calls.append(value)
            if len(calls) == 2:
                raise ValueError('private-parent-decoder-canary')
            return real_decoder(value)

        with FixedWorker() as f, mock.patch.object(service, '_capacity_worker_diagnostic', side_effect=decode):
            values, fd = f.probe()
            self.assertEqual(values['usage_percent'], 75.0)
            self.assertEqual(len(calls), 2)
            self.assertEqual((f.inventory_reads, f.fd_reads, f.measurements), (4, 3, 1))
            self.assertTrue(any(item['reason'] == 'observer_error' for _, item in f.evidence))
            self.assertNotIn('private-parent', json.dumps(f.evidence))
            f.close(fd)
            self.assertFalse(f.fds)

    def test_late_worker_after_lifecycle_revoke_cannot_attach_to_new_attempt(self):
        with FixedWorker() as f:
            entry = f.entry()
            store = f.sampler._capacity_attempts
            original_epoch = f.sampler._capacity_epoch
            first = store.begin(original_epoch, 0, 'web_owner_refresh', 'visible_refresh')
            f.sampler._capacity_context.attempt_id = first
            f.sampler._capacity_attempt_step = service.StatusSampler._capacity_attempt_step.__get__(f.sampler)
            f.sampler._capacity_attempt_row(entry)
            next_attempt, guard_calls = [], []
            original_poll = f.worker.poll
            denial = service.CapacityRefreshError('unavailable')

            def poll():
                result = original_poll()
                if result == 0 and not next_attempt:
                    # Model a real lifecycle revocation after child completion,
                    # before the existing parent authorization check resumes.
                    f.sampler._capacity_epoch += 1
                    store.reset()
                    next_attempt.append(store.begin(f.sampler._capacity_epoch, 0,
                        'web_owner_refresh', 'visible_refresh'))
                    f.sampler._capacity_context.attempt_id = next_attempt[0]
                return result

            def authorized():
                guard_calls.append(f.sampler._capacity_epoch)
                if f.sampler._capacity_epoch != original_epoch:
                    raise denial

            f.worker.poll = poll
            with self.assertRaises(service.CapacityRefreshError) as caught:
                f.sampler._capacity_probe(entry, time.monotonic()+3, authorized)
            self.assertIs(caught.exception, denial)
            self.assertEqual(len(guard_calls), 3)
            self.assertEqual(f.measurements, 1)
            self.assertEqual(f.fd_reads, 2)
            self.assertFalse(f.received)
            self.assertNotEqual(first, next_attempt[0])
            self.assertEqual(store._attempts[next_attempt[0]]['rows'], {})
            self.assertFalse(f.fds)

    def test_diagnostic_json_encode_fault_does_not_change_business_json(self):
        original = json.dumps
        def guarded(value, *args, **kwargs):
            if type(value) is dict and 'diagnostic' in value:
                raise ValueError('private-json-canary')
            return original(value, *args, **kwargs)
        with FixedWorker() as f, mock.patch.object(service.json, 'dumps', side_effect=guarded):
            values, fd = f.probe()
            self.assertEqual(values['usage_percent'], 75.0)
            self.assertNotIn('diagnostic', f.worker_json)
            f.close(fd)

    def test_original_business_json_encode_error_is_not_turned_into_success(self):
        original_dumps = json.dumps
        error = ValueError('private-business-json-canary')

        def encode(value, *args, **kwargs):
            if type(value) is dict and 'ok' in value and 'diagnostic' not in value:
                raise error
            return original_dumps(value, *args, **kwargs)

        with FixedWorker() as f:
            request = original_dumps({'entry': f.entry(), 'deadline': time.monotonic()+3, 'channel': 72}).encode()
            worker_sys = Forward(service.sys, stdin=types.SimpleNamespace(buffer=io.BytesIO(request)))
            output = io.StringIO()

            def emit(*args, **kwargs):
                kwargs['file'] = output
                print(*args, **kwargs)

            with mock.patch.object(service, 'sys', worker_sys), \
                    mock.patch.object(service.json, 'dumps', side_effect=encode), \
                    mock.patch.dict(service.__dict__, {'print': emit}), \
                    mock.patch.object(service, '_capacity_worker_encode', wraps=service._capacity_worker_encode) as observer:
                with self.assertRaises(ValueError) as caught:
                    service._capacity_worker_main()
            self.assertIs(caught.exception, error)
            observer.assert_not_called()
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(f.measurements, 1)
            f.channel.close()
            self.assertFalse(f.fds)

    def test_missing_or_malformed_diagnostic_never_rejects_valid_business(self):
        for value in ({'schema': 1, 'private_path': '/private/worker-canary'}, {'schema': True},
                      {'schema': 1, 'observation_status': 'observed', 'steps': {}}):
            with self.subTest(value=value), FixedWorker() as f:
                f.bad_diagnostic = value
                values, fd = f.probe()
                self.assertEqual(values['usage_percent'], 75.0)
                serialized = json.dumps(f.evidence)
                self.assertIn('observer_error', serialized)
                self.assertNotIn('worker-canary', serialized)
                f.close(fd)

    def test_inventory_error_preserves_exact_exception_object_when_observer_fails(self):
        with FixedWorker() as f:
            entry = f.entry()
            error = OSError('private-OS-canary')
            f.entry_failure = error
            with self.assertRaises(OSError) as caught:
                service._capacity_measure_entry(entry, time.monotonic()+2,
                    observer=lambda *_: (_ for _ in ()).throw(ValueError('observer')))
            self.assertIs(caught.exception, error)
            self.assertEqual(f.measurements, 0)
            self.assertFalse(f.fds)

    def test_each_real_inventory_guard_records_its_phase_and_stops_dependencies(self):
        for target, phase, measurement_count, fd_count in (
                (2, 'inventory_before', 0, 0), (3, 'inventory_opened', 0, 1),
                (4, 'inventory_after', 1, 2)):
            with self.subTest(phase=phase), FixedWorker() as f:
                original = f.inventory
                def incomplete(*args):
                    rows, complete = original(*args)
                    return ([], False) if f.inventory_reads == target else (rows, complete)
                with mock.patch.object(service, '_capacity_inventory', side_effect=incomplete):
                    with self.assertRaises(service.CapacityRefreshError) as caught:
                        f.probe()
                self.assertEqual(caught.exception.reason, 'identity_changed')
                steps = f.worker_json['diagnostic']['steps']
                self.assertEqual(steps[phase]['reason'], 'inventory_incomplete')
                self.assertFalse(steps[phase]['observations']['comparison_performed'])
                self.assertEqual(steps['transfer']['status'], 'not_run')
                self.assertEqual((f.measurements, f.fd_reads), (measurement_count, fd_count))
                self.assertFalse(f.fds)

    def test_changed_child_key_is_distinct_from_incomplete_inventory(self):
        with FixedWorker() as f:
            original = f.inventory
            def changed(*args):
                rows, complete = original(*args)
                if f.inventory_reads == 2:
                    rows[0]['_key'] = 'c'*64
                return rows, complete
            with mock.patch.object(service, '_capacity_inventory', side_effect=changed):
                with self.assertRaises(service.CapacityRefreshError) as caught:
                    f.probe()
            self.assertEqual(caught.exception.reason, 'identity_changed')
            step = f.worker_json['diagnostic']['steps']['inventory_before']
            self.assertEqual(step['reason'], 'key_mismatch')
            self.assertTrue(step['observations']['inventory_complete'])
            self.assertFalse(step['observations']['matched'])
            self.assertEqual(step['observations']['actual_key'], 'c'*64)
            self.assertNotEqual(step['observations']['expected_key'], step['observations']['actual_key'])
            self.assertEqual((f.measurements, f.fd_reads), (0, 0))

    def test_fdinfo_parse_failure_preserves_identity_rejection_and_fixed_reason(self):
        with FixedWorker() as f:
            original = f.fd_mount
            def missing(fd, proc_root='/proc'):
                (f.proc/'self/fdinfo'/str(fd)).write_bytes(b'pos:\t0\n')
                return original(fd, proc_root)
            with mock.patch.object(service, '_capacity_fd_mount', side_effect=missing):
                with self.assertRaises(service.CapacityRefreshError) as caught:
                    f.probe()
            self.assertEqual(caught.exception.reason, 'identity_changed')
            self.assertEqual(f.worker_json['diagnostic']['steps']['fd_before']['reason'], 'fd_mount_unavailable')
            self.assertEqual(f.measurements, 0)
            self.assertFalse(f.fds)

    def test_sendmsg_failure_is_observed_without_exporting_exception(self):
        with FixedWorker() as f:
            f.worker_channel.sendmsg = mock.Mock(side_effect=OSError('private-sendmsg-canary'))
            with self.assertRaises(service.CapacityRefreshError) as caught:
                f.probe()
            self.assertEqual(caught.exception.reason, 'probe_failed')
            self.assertEqual(f.worker_json['diagnostic']['steps']['transfer']['reason'], 'scm_send_failed')
            self.assertNotIn('private-sendmsg-canary', json.dumps(f.worker_json))
            self.assertFalse(f.fds)

    def test_measurement_error_preserves_exact_exception_object(self):
        with FixedWorker() as f:
            entry = f.entry()
            error = OSError('private-measure-canary')
            with mock.patch.object(service.os.overrides['fstatvfs'].__self__, 'fstatvfs', side_effect=error):
                # The facade bound method is fixed at creation; replace that OS
                # seam directly rather than replacing the production function.
                service.os.overrides['fstatvfs'] = mock.Mock(side_effect=error)
                with self.assertRaises(OSError) as caught:
                    service._capacity_measure_entry(entry, time.monotonic()+2,
                        observer=lambda *_: (_ for _ in ()).throw(ValueError('observer')))
            self.assertIs(caught.exception, error)
            self.assertFalse(f.fds)

    def test_parent_request_budget_unchanged_and_does_not_measure(self):
        with FixedWorker() as f:
            entry = f.entry()
            entry['source'] = 'x'*4096
            with self.assertRaises(service.CapacityRefreshError) as caught:
                f.probe(entry)
            self.assertEqual(caught.exception.reason, 'unavailable')
            self.assertEqual(f.measurements, 0)
            self.assertTrue(any(item['reason'] == 'request_limit' for _, item in f.evidence))

    def test_original_response_rejections_keep_fixed_protocol_attribution(self):
        examples = ((b'x'*4097, 'response_limit'), (b'{', 'response_json_invalid'),
            (b'{}', 'response_shape_invalid'),
            (b'{"ok":true,"values":{"raw":"private-value-canary"}}', 'response_values_invalid'))
        for raw, reason in examples:
            with self.subTest(reason=reason), FixedWorker() as f:
                f.response_override = raw
                with self.assertRaises(service.CapacityRefreshError) as caught:
                    f.probe()
                self.assertEqual(caught.exception.reason, 'probe_failed')
                self.assertTrue(any(item['reason'] == reason for _, item in f.evidence))
                self.assertNotIn('private-value-canary', json.dumps(f.evidence))
                self.assertFalse(f.fds)

    def test_original_child_16384_limit_does_not_measure_or_export_input(self):
        with FixedWorker() as f:
            out = io.StringIO()
            def emit(*args, **kwargs):
                kwargs['file'] = out
                print(*args, **kwargs)
            worker_sys = Forward(service.sys, stdin=types.SimpleNamespace(buffer=io.BytesIO(b'x'*16385)))
            with mock.patch.object(service, 'sys', worker_sys), mock.patch.dict(service.__dict__, {'print': emit}):
                rc = service._capacity_worker_main()
            value = json.loads(out.getvalue())
            self.assertEqual(rc, 0)
            self.assertEqual({key: item for key, item in value.items() if key != 'diagnostic'},
                             {'ok': False, 'reason': 'probe_failed'})
            self.assertTrue(all(row['status'] == 'not_run' for row in value['diagnostic']['steps'].values()))
            self.assertLessEqual(len(out.getvalue().encode()), 4096)
            self.assertEqual(f.measurements, 0)

    def test_worst_legal_trace_and_int63_values_fit_original_stdout_gate(self):
        trace = service._capacity_worker_trace()
        fields = {'expected_mount_id': 2**53-1, 'actual_mount_id': 2**53-1,
            'expected_device': '9999999:9999999', 'actual_device': '9999999:9999999',
            'expected_key': 'a'*64, 'inventory_complete': True, 'matched': True, 'executed': True,
            'fd_checked': True, 'before_digest': 'b'*64, 'after_digest': 'c'*64}
        for phase in trace['steps']:
            trace['steps'][phase] = {'status': 'passed', 'reason': None, 'observations': dict(fields)}
        result = {'ok': True, 'values': {'total_bytes': 2**63-1, 'used_bytes': 2**63-2,
                                        'usage_percent': (2**63-2)/(2**63-1)*100}}
        encoded = service._capacity_worker_encode(result, trace, json.dumps(result))
        self.assertLessEqual(len(encoded.encode())+1, 4096)
        value = json.loads(encoded)
        self.assertEqual(value['values'], result['values'])
        self.assertEqual(value['diagnostic'], {'schema': 1, 'observation_status': 'size_exceeded', 'steps': {}})

    def test_worker_trace_rejects_free_text_and_bad_fixed_scalar_types(self):
        for field, value in (('actual_mount_id', True), ('expected_device', '/private/device'),
                             ('before_digest', 'private-key'), ('executed', 1), ('raw', 'private-secret')):
            trace = service._capacity_worker_trace()
            trace['steps']['fd_before']['observations'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                service._capacity_worker_diagnostic(trace)


if __name__ == '__main__':
    unittest.main()

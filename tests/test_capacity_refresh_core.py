"""Passive metadata and explicit capacity cache; no NAS or user media access."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('capacity_service_under_test', ROOT / 'runtime/ui_service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class CapacityCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-capacity-core-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc, self.sys = self.root / 'proc', self.root / 'sys'
        (self.proc / 'self').mkdir(parents=True)
        self.sys.mkdir()
        self.mountinfo = self.proc / 'self/mountinfo'
        self.generation = 1
        device = mock.patch.object(service, '_capacity_device', side_effect=lambda *_: ('physical', self.generation, 'sdb'))
        device.start()
        self.addCleanup(device.stop)
        fdinfo = mock.patch.object(service, '_capacity_fd_mount', return_value=11)
        fdinfo.start()
        self.addCleanup(fdinfo.stop)
        self.write_mount()
        self.sampler = service.StatusSampler(self.proc, self.sys)
        self.addCleanup(self.sampler.close_capacity)
        self.watch = types.SimpleNamespace(changed=mock.Mock(return_value=False), close=mock.Mock())
        watch_factory = mock.patch.object(service, '_CapacityMountWatch', return_value=self.watch)
        watch_factory.start()
        self.addCleanup(watch_factory.stop)
        self.values = {'total_bytes': 409600, 'used_bytes': 307200, 'usage_percent': 75.0}

    def write_mount(self, mount_id=11, device='8:2', target='/drives/data', fs='ext4', root='/'):
        self.mountinfo.write_text(f'{mount_id} 1 {device} {root} {target} rw - {fs} /dev/sdb1 rw\n', encoding='utf8')

    def probe(self, *_):
        descriptor = os.open(os.devnull, os.O_RDONLY)
        return dict(self.values), descriptor

    def refresh(self, **kwargs):
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.probe):
            return self.sampler.refresh_capacity(authorizer=kwargs.get('authorizer', lambda: True))

    def test_cold_inventory_never_probes_or_resolves_user_mounts(self):
        original_stat, original_realpath = os.stat, os.path.realpath
        def checked_stat(path, *args, **kwargs):
            if str(path).startswith('/drives'):
                self.fail('passive stat of user filesystem')
            return original_stat(path, *args, **kwargs)
        def checked_realpath(path, *args, **kwargs):
            if str(path).startswith('/drives'):
                self.fail('passive realpath of user filesystem')
            return original_realpath(path, *args, **kwargs)
        with mock.patch.object(service.subprocess, 'Popen', side_effect=AssertionError('auto worker')), \
                mock.patch.object(service.os, 'stat', side_effect=checked_stat), \
                mock.patch.object(service.os.path, 'realpath', side_effect=checked_realpath), \
                mock.patch.object(service.os, 'statvfs', create=True, side_effect=AssertionError('auto statvfs')):
            row = self.sampler.sample()['disks'][0]
            service.read_disks(self.proc, statvfs=lambda *_: self.fail('legacy injection ran'), sys_root=self.sys)
        self.assertIsNone(row['total_bytes'])
        self.assertIsNone(row['capacity_sampled_at'])
        self.assertEqual(row['capacity_status'], 'unknown')
        self.assertEqual(row['capacity_source'], 'manual_statvfs_cache')

    def test_explicit_refresh_is_memory_only_cached_projection(self):
        result = self.refresh()
        self.assertEqual(set(result), {'ok', 'status', 'started_at', 'finished_at', 'results'})
        self.assertEqual(result['status'], 'complete')
        row = result['results'][0]
        self.assertEqual(set(row), {'mount_id', 'status', 'reason', 'sampled_at'})
        self.assertEqual(row['mount_id'], 11)
        self.assertEqual(row['status'], 'refreshed')
        self.assertGreater(row['sampled_at'], 0)
        self.assertNotIn('/drives', json.dumps(result))
        cached = self.sampler.capacity_snapshot()[0]
        self.assertEqual(cached['capacity_status'], 'cached')
        self.assertEqual(cached['usage_percent'], 75)

    def test_monitor_snapshot_reprojects_without_waiting_for_background_sample(self):
        self.refresh()
        monitor = service.StatusMonitor(self.sampler)
        monitor.status, monitor.updated_at = self.sampler.sample(), time.monotonic()
        self.generation = 2
        self.assertIsNone(monitor.snapshot()['disks'][0]['total_bytes'])
        self.assertEqual(self.sampler._capacity_cache, {})

    def test_monitor_refresh_delegates_the_exact_authorizer(self):
        authorizer = lambda: True
        monitor = service.StatusMonitor(self.sampler)
        with mock.patch.object(self.sampler, 'refresh_capacity', return_value={'ok': True}) as refresh:
            self.assertEqual(monitor.refresh_capacity(authorizer=authorizer), {'ok': True})
        refresh.assert_called_once_with(authorizer=authorizer)

    def test_generation_change_prunes_value_cache_without_mount_pin(self):
        self.refresh()
        self.assertNotIn('anchor', self.sampler._capacity_cache[11])
        self.generation += 1
        row = self.sampler.capacity_snapshot()[0]
        self.assertEqual(row['capacity_status'], 'identity_changed')
        self.assertIsNone(row['total_bytes'])
        self.assertFalse(self.sampler._capacity_cache)

    def test_observed_unmount_cannot_revive_cache(self):
        self.refresh()
        self.mountinfo.write_text('')
        self.assertEqual(self.sampler.capacity_snapshot(), [])
        self.write_mount()
        self.assertIsNone(self.sampler.capacity_snapshot()[0]['total_bytes'])

    def test_remount_id_root_device_and_filesystem_changes_reject_old_cache(self):
        for change in ({'mount_id': 12}, {'device': '8:3'}, {'target': '/drives/new'}, {'fs': 'xfs'}, {'root': '/share'}):
            with self.subTest(change=change):
                self.write_mount()
                self.refresh()
                self.write_mount(**change)
                self.assertTrue(all(row['total_bytes'] is None for row in self.sampler.capacity_snapshot()))

    def test_duplicate_alias_or_stacked_target_is_unavailable(self):
        self.refresh()
        self.mountinfo.write_text(self.mountinfo.read_text() + '12 1 8:2 / /drives/alias rw - ext4 /dev/sdb1 rw\n')
        self.assertTrue(all(row['total_bytes'] is None for row in self.sampler.capacity_snapshot()))
        with mock.patch.object(self.sampler, '_capacity_probe') as probe:
            result = self.sampler.refresh_capacity(authorizer=lambda: True)
        probe.assert_not_called()
        self.assertEqual(result['status'], 'partial')

    def test_ignored_fuse_bind_and_parent_mounts_cannot_shadow_cached_target(self):
        for line in ('12 1 0:9 / /drives/data rw - fuse.mock none rw\n',
                     '12 1 8:3 /share /drives/data rw - ext4 /dev/sdc1 rw\n',
                     '12 1 8:3 / /drives rw - ext4 /dev/sdc1 rw\n'):
            with self.subTest(line=line):
                self.write_mount()
                self.refresh()
                self.mountinfo.write_text(self.mountinfo.read_text() + line)
                rows = self.sampler.capacity_snapshot()
                self.assertTrue(all(row['total_bytes'] is None for row in rows))
                self.assertFalse(self.sampler._capacity_cache)

    def test_observed_remount_options_invalidate_cache(self):
        self.refresh()
        self.mountinfo.write_text(self.mountinfo.read_text().replace(' rw - ', ' ro - '))
        self.assertIsNone(self.sampler.capacity_snapshot()[0]['total_bytes'])

    def test_transient_unmount_during_refresh_never_revives_old_cache(self):
        for at in (1, 2):
            with self.subTest(scan=at):
                self.refresh()
                real, calls = service._capacity_inventory, [0]
                def inventory(*args):
                    calls[0] += 1
                    return ([], True) if calls[0] == at else real(*args)
                with mock.patch.object(service, '_capacity_inventory', side_effect=inventory):
                    self.refresh()
                self.assertFalse(self.sampler._capacity_cache)
                self.assertIsNone(self.sampler.capacity_snapshot()[0]['total_bytes'])

    def test_transient_detach_of_earlier_pending_disk_is_latched(self):
        self.mountinfo.write_text(self.mountinfo.read_text() + '12 1 8:3 / /drives/other rw - ext4 /dev/sdc1 rw\n')
        real, calls = service._capacity_inventory, [0]
        def inventory(*args):
            rows, complete = real(*args)
            calls[0] += 1
            if calls[0] == 3:
                rows = [row for row in rows if row['mount_id'] != 11]
            return rows, complete
        with mock.patch.object(service, '_capacity_inventory', side_effect=inventory):
            result = self.refresh()
        self.assertEqual(result['results'][0]['status'], 'identity_changed')
        self.assertNotIn(11, self.sampler._capacity_cache)
        self.assertFalse(self.sampler._capacity_cache)

    def test_passive_snapshot_observation_fences_inflight_pending(self):
        def probe(*_):
            result = self.probe()
            self.mountinfo.write_text('')
            self.sampler.capacity_snapshot()
            self.write_mount()
            return result
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            result = self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(result['results'][0]['status'], 'identity_changed')
        self.assertFalse(self.sampler._capacity_cache)

    def test_namespace_event_invalidates_even_identical_mountinfo(self):
        self.refresh()
        self.watch.changed.return_value = True
        row = self.sampler.capacity_snapshot()[0]
        self.assertIsNone(row['total_bytes'])
        self.assertFalse(self.sampler._capacity_cache)
        self.assertIsNone(self.sampler._capacity_watch)
        self.watch.close.assert_called_once()

    def test_namespace_event_after_measurement_discards_short_handle_and_values(self):
        def probe(*_):
            result = self.probe()
            self.watch.changed.return_value = True
            return result
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            result = self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(result['results'][0]['status'], 'identity_changed')
        self.assertFalse(self.sampler._capacity_cache)

    def test_absent_kernel_event_proof_never_starts_measurement(self):
        with mock.patch.object(service, '_CapacityMountWatch', side_effect=service.CapacityRefreshError('unavailable')), \
                mock.patch.object(self.sampler, '_capacity_probe') as probe:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        probe.assert_not_called()
        self.assertEqual(caught.exception.reason, 'unavailable')

    def test_unknown_fuse_and_missing_physical_identity_are_not_measured(self):
        for fs in ('fuseblk', 'fuse.mergerfs', 'unknown'):
            self.write_mount(fs=fs)
            with mock.patch.object(self.sampler, '_capacity_probe') as probe:
                result = self.sampler.refresh_capacity(authorizer=lambda: True)
            probe.assert_not_called()
            self.assertEqual(result['results'][0]['reason'], 'unsupported_filesystem')
        self.write_mount()
        with mock.patch.object(service, '_capacity_device', return_value=None), \
                mock.patch.object(self.sampler, '_capacity_probe') as probe:
            result = self.sampler.refresh_capacity(authorizer=lambda: True)
        probe.assert_not_called()
        self.assertEqual(result['results'][0]['status'], 'unavailable')

    def test_nonblocking_batch_lock(self):
        self.sampler._capacity_refresh_lock.acquire()
        try:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
            self.assertEqual(caught.exception.reason, 'busy')
        finally:
            self.sampler._capacity_refresh_lock.release()

    def test_denial_or_authorizer_exception_does_not_start_worker(self):
        for authorizer in (None, lambda: False, mock.Mock(side_effect=RuntimeError('SECRET'))):
            with mock.patch.object(self.sampler, '_capacity_probe') as probe:
                with self.assertRaises(service.CapacityRefreshError) as caught:
                    self.sampler.refresh_capacity(authorizer=authorizer)
            probe.assert_not_called()
            self.assertEqual(str(caught.exception), 'authorization_denied')

    def test_revoke_mid_batch_discards_all_new_records_and_closes_pins(self):
        allowed, descriptors = [True], []
        def probe(*_):
            values, fd = self.probe()
            descriptors.append(fd)
            allowed[0] = False
            return values, fd
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: allowed[0])
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        self.assertFalse(self.sampler._capacity_cache)
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_revocation_while_closing_temporary_handle_discards_new_batch(self):
        self.refresh()
        old = dict(self.sampler._capacity_cache[11])
        allowed, allocated = [True], []
        close = os.close
        def revoking_close(fd):
            close(fd)
            if fd in allocated:
                allowed[0] = False
        def probe(*_):
            result = self.probe()
            allocated.append(result[1])
            return result
        with mock.patch.object(service.os, 'close', side_effect=revoking_close), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: allowed[0])
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        self.assertEqual(self.sampler._capacity_cache[11], old)
        for fd in allocated:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_denial_is_latched_even_if_test_restores_permission(self):
        state = [True]
        def probe(entry, deadline, authorized):
            state[0] = False
            with self.assertRaises(service.CapacityRefreshError):
                authorized()
            state[0] = True
            authorized()
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: state[0])
        self.assertEqual(caught.exception.reason, 'authorization_denied')

    def test_source_change_after_probe_rejects_result(self):
        def probe(*_):
            value = self.probe()
            self.generation += 1
            return value
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            result = self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(result['results'][0]['status'], 'identity_changed')
        self.assertFalse(self.sampler._capacity_cache)

    def test_change_at_commit_rejects_earlier_success(self):
        real = service._capacity_inventory
        calls = [0]
        def inventory(*args):
            calls[0] += 1
            if calls[0] == 3:
                self.generation += 1
            return real(*args)
        with mock.patch.object(service, '_capacity_inventory', side_effect=inventory):
            result = self.refresh()
        self.assertEqual(result['results'][0]['status'], 'identity_changed')

    def test_timeout_discards_pending_batch(self):
        with mock.patch.object(service, '_CAPACITY_WINDOW', 0):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.refresh()
        self.assertEqual(caught.exception.reason, 'timeout')
        self.assertFalse(self.sampler._capacity_cache)

    def test_close_and_restart_cannot_publish_old_request(self):
        def probe(*_):
            value = self.probe()
            self.sampler.close_capacity()
            self.sampler.start_capacity()
            return value
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(caught.exception.reason, 'unavailable')
        self.assertFalse(self.sampler._capacity_cache)

    def test_incomplete_or_oversized_metadata_discards_previous_cache(self):
        for text in ('malformed\n', '11 1 8:2 / /drives/data rw - ext4 /dev/sdb1 rw\n' * 2, 'x' * 262145):
            self.write_mount()
            self.refresh()
            self.mountinfo.write_text(text)
            self.assertEqual(self.sampler.capacity_snapshot(), [])
            self.assertFalse(self.sampler._capacity_cache)

    def test_short_reads_are_consumed_to_eof_and_overflow_rejected(self):
        real_read = os.read
        with mock.patch.object(service.os, 'read', side_effect=lambda fd, size: real_read(fd, min(size, 3))):
            entries, complete = service._capacity_inventory(self.proc, self.sys)
        self.assertTrue(complete)
        self.assertEqual(entries[0]['mount_id'], 11)
        self.assertIsNone(service._capacity_read(self.mountinfo, 3))

    def test_mountinfo_change_during_sysfs_sampling_rejects_scan(self):
        def identity(*_):
            self.write_mount(mount_id=12)
            return ('physical', 1)
        with mock.patch.object(service, '_capacity_device', side_effect=identity):
            self.assertEqual(service._capacity_inventory(self.proc, self.sys), ([], False))

    def test_lingering_worker_prevents_another_batch(self):
        worker = types.SimpleNamespace(poll=lambda: None)
        self.sampler._capacity_worker = worker
        try:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.refresh()
            self.assertEqual(caught.exception.reason, 'busy')
        finally:
            self.sampler._capacity_worker = None

    def test_idle_reaper_closes_exited_worker_and_blocks_replacement_until_cleanup(self):
        finished = threading.Event()
        worker = types.SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(),
            wait=lambda: finished.wait(2), poll=lambda: 0 if finished.is_set() else None)
        self.sampler._capacity_worker = worker
        self.sampler._capacity_schedule_reap(worker)
        reaper = self.sampler._capacity_reaper
        with mock.patch.object(self.sampler, '_capacity_probe') as probe:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(caught.exception.reason, 'busy')
        probe.assert_not_called()
        finished.set()
        reaper.join(2)
        self.assertFalse(reaper.is_alive())
        self.assertTrue(worker.stdin.closed)
        self.assertTrue(worker.stdout.closed)
        self.assertIsNone(self.sampler._capacity_worker)
        self.assertIsNone(self.sampler._capacity_reaper)

    def test_reaper_thread_start_failure_keeps_live_worker_busy_then_passively_reaps(self):
        exited = [False]
        worker = types.SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(),
                                       poll=lambda: 0 if exited[0] else None)
        self.sampler._capacity_worker = worker
        with mock.patch.object(service.threading.Thread, 'start', side_effect=RuntimeError('no thread resource')):
            self.sampler._capacity_schedule_reap(worker)
        self.assertIsNone(self.sampler._capacity_reaper)
        self.assertIs(self.sampler._capacity_worker, worker)
        self.assertFalse(worker.stdin.closed)
        with mock.patch.object(self.sampler, '_capacity_probe') as probe:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        probe.assert_not_called()
        self.assertEqual(caught.exception.reason, 'busy')
        exited[0] = True
        with mock.patch.object(service.os, 'statvfs', create=True, side_effect=AssertionError('passive statvfs')), \
                mock.patch.object(service.subprocess, 'Popen', side_effect=AssertionError('passive child')):
            self.sampler.capacity_snapshot()
        self.assertTrue(worker.stdin.closed)
        self.assertTrue(worker.stdout.closed)
        self.assertIsNone(self.sampler._capacity_worker)
        self.assertEqual(self.refresh()['status'], 'complete')

    def test_all_mount_handles_close_before_cache_commit_and_stop_closes_proc_watch(self):
        descriptors = []
        def probe(*_):
            result = self.probe()
            descriptors.append(result[1])
            return result
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            self.sampler.refresh_capacity(authorizer=lambda: True)
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertTrue(all('anchor' not in row for row in self.sampler._capacity_cache.values()))
        self.sampler.close_capacity()
        self.watch.close.assert_called_once()
        self.assertFalse(self.sampler._capacity_cache)

    def fake_child(self, result=None, *, marker=b'1', flags=0, alive=False):
        """Portable IPC facade, while Linux cases separately check real FDs."""
        import array
        received = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(lambda: self.close_if_open(received))
        channel = mock.Mock()
        channel.recvmsg.return_value = (marker, [(socket.SOL_SOCKET, 1, array.array('i', [received]).tobytes())], flags, None)
        child_channel = mock.Mock()
        child_channel.fileno.return_value = 72
        result = result if result is not None else {'ok': True, 'values': self.values}
        worker = types.SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(json.dumps(result).encode()),
                                      returncode=0, poll=mock.Mock(return_value=None if alive else 0),
                                      kill=mock.Mock(), wait=mock.Mock(return_value=0))
        context = contextlib.ExitStack()
        context.enter_context(mock.patch.object(service.sys, 'platform', 'linux'))
        context.enter_context(mock.patch.object(service.socket, 'SCM_RIGHTS', 1, create=True))
        context.enter_context(mock.patch.object(service.socket, 'CMSG_SPACE', lambda size: size + 32, create=True))
        context.enter_context(mock.patch.object(service.socket, 'socketpair', return_value=(channel, child_channel)))
        spawn = context.enter_context(mock.patch.object(service.subprocess, 'Popen', return_value=worker))
        return context, worker, spawn, received

    @staticmethod
    def close_if_open(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    def test_private_worker_is_bounded_and_uses_B_and_mount_pin(self):
        entry = service._capacity_inventory(self.proc, self.sys)[0][0]
        context, worker, spawn, received = self.fake_child()
        with context:
            values, anchor = self.sampler._capacity_probe(entry, time.monotonic() + 2, lambda: None)
        self.assertEqual(values, self.values)
        self.assertEqual(anchor, received)
        self.assertEqual(spawn.call_args.args[0][1], '-B')
        self.assertEqual(spawn.call_args.args[0][-1], '--capacity-worker')
        self.assertEqual(spawn.call_args.kwargs['pass_fds'], (72,))
        self.assertIsNone(self.sampler._capacity_worker)

    def test_revocation_during_spawn_sends_no_measurement_request(self):
        entry = service._capacity_inventory(self.proc, self.sys)[0][0]
        context, worker, spawn, received = self.fake_child(alive=True)
        worker.stdin = mock.Mock(wraps=io.BytesIO())
        allowed = [True]
        def popen(*args, **kwargs):
            allowed[0] = False
            return worker
        def authorized():
            if not allowed[0]:
                raise service.CapacityRefreshError('authorization_denied')
        spawn.side_effect = popen
        with context:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler._capacity_probe(entry, time.monotonic() + 2, authorized)
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        worker.stdin.write.assert_not_called()
        worker.kill.assert_called_once()
        self.sampler._capacity_worker = None

    def fill(self, eligibility=None):
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.probe):
            return self.sampler.refresh_capacity_missing(authorizer=lambda: True,
                eligibility=eligibility if eligibility is not None else lambda name: None)

    def test_foreground_fill_only_missing_active_same_lifetime_capacity(self):
        eligibility = mock.Mock(return_value=None)
        result = self.fill(eligibility)
        self.assertEqual(result['results'][0]['status'], 'refreshed')
        self.assertTrue(all(call.args == ('sdb',) for call in eligibility.call_args_list))
        with mock.patch.object(self.sampler, '_capacity_probe') as probe:
            result = self.sampler.refresh_capacity_missing(authorizer=lambda: True,
                                                           eligibility=lambda _: self.fail('cached power probe'))
        probe.assert_not_called()
        self.assertEqual(result['results'][0]['status'], 'cached')
        self.assertGreater(result['results'][0]['sampled_at'], 0)

    def test_foreground_standby_unknown_or_missing_eligibility_does_not_measure(self):
        for eligibility, status, reason in ((lambda _: 'standby', 'skipped_standby', 'standby'),
                (lambda _: 'power_state_unverified', 'unavailable', 'power_state_unverified'),
                (None, 'unavailable', 'power_state_unverified'),
                (mock.Mock(side_effect=RuntimeError('SECRET')), 'unavailable', 'power_state_unverified')):
            with self.subTest(status=status, eligibility=eligibility), \
                    mock.patch.object(self.sampler, '_capacity_probe') as probe:
                result = self.sampler.refresh_capacity_missing(authorizer=lambda: True, eligibility=eligibility)
            probe.assert_not_called()
            self.assertEqual(result['results'][0]['status'], status)
            self.assertEqual(result['results'][0]['reason'], reason)
            self.assertEqual(result['status'], 'complete' if status == 'skipped_standby' else 'partial')

    def test_real_missing_capacity_standby_skip_keeps_web_fill_combination_complete(self):
        # Exercise the actual Web composition and actual sampler missing
        # implementation. Only the SMART result is a memory-only fixture.
        with mock.patch.object(sys, 'path', [str(ROOT / 'runtime')] + sys.path):
            import web_config
        smart = {'ok': True, 'force': False, 'status': 'complete',
                 'started_at': time.time(), 'finished_at': time.time(),
                 'results': [{'device': 'sdb', 'status': 'skipped_standby',
                              'reason': 'standby', 'sampled_at': None}]}
        web = object.__new__(web_config.WebConfigServer)
        web._storage_fill_lock = threading.Lock()
        web.insights = types.SimpleNamespace(fill_smart_missing=lambda **_: smart,
                                             capacity_fill_eligibility=lambda _: 'standby')
        web.monitor = service.StatusMonitor(self.sampler)
        with mock.patch.object(self.sampler, '_capacity_probe') as probe:
            result = web_config.WebConfigServer.storage_fill(web, authorizer=lambda: True)
        probe.assert_not_called()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['capacity']['status'], 'complete')
        self.assertEqual(result['capacity']['results'][0]['status'], 'skipped_standby')
        self.assertEqual(result['capacity']['results'][0]['reason'], 'standby')

    def test_visible_capacity_ignores_positive_cache_and_delegates_exact_grants(self):
        self.refresh()
        old = dict(self.sampler._capacity_cache[11])
        self.values = dict(self.values, used_bytes=102400, usage_percent=25.0)
        authorizer, eligibility = lambda: True, mock.Mock(return_value=None)
        monitor = service.StatusMonitor(self.sampler)
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.probe) as probe, \
                mock.patch.object(service.time, 'time', return_value=old['sampled_at'] + 5):
            result = monitor.refresh_capacity_visible(authorizer=authorizer, eligibility=eligibility)
        probe.assert_called_once()
        self.assertEqual(result['results'][0]['status'], 'refreshed')
        self.assertEqual(self.sampler.capacity_snapshot()[0]['usage_percent'], 25)
        self.assertEqual(self.sampler.capacity_snapshot()[0]['capacity_sampled_at'], old['sampled_at'] + 5)
        self.assertGreaterEqual(eligibility.call_count, 3)

    def test_visible_standby_unknown_or_failure_preserves_historical_capacity(self):
        self.refresh()
        original = self.sampler.capacity_snapshot()[0]
        for reason, expected in (('standby', 'skipped_standby'), ('power_state_unverified', 'unavailable')):
            with self.subTest(reason=reason), mock.patch.object(self.sampler, '_capacity_probe') as probe:
                result = self.sampler.refresh_capacity_visible(authorizer=lambda: True, eligibility=lambda _: reason)
            probe.assert_not_called()
            self.assertEqual(result['results'][0]['status'], expected)
            self.assertEqual(result['results'][0]['sampled_at'], original['capacity_sampled_at'])
            self.assertEqual(self.sampler.capacity_snapshot()[0], original)
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=service.CapacityRefreshError('probe_failed')):
            result = self.sampler.refresh_capacity_visible(authorizer=lambda: True, eligibility=lambda _: None)
        self.assertEqual(result['results'][0]['status'], 'failed')
        self.assertEqual(result['results'][0]['sampled_at'], original['capacity_sampled_at'])
        self.assertEqual(self.sampler.capacity_snapshot()[0], original)
        with mock.patch.object(self.sampler, '_capacity_probe') as probe:
            repeated = self.sampler.refresh_capacity_visible(authorizer=lambda: True, eligibility=lambda _: None)
        probe.assert_not_called()
        self.assertEqual(repeated['results'], result['results'])

    def test_visible_receipt_does_not_attach_history_to_changed_identity(self):
        self.refresh()
        original = self.sampler.capacity_snapshot()[0]
        def eligibility(_):
            self.generation += 1
            return 'standby'
        with mock.patch.object(self.sampler, '_capacity_probe') as probe:
            result = self.sampler.refresh_capacity_visible(authorizer=lambda: True, eligibility=eligibility)
        probe.assert_not_called()
        self.assertEqual(result['results'][0]['status'], 'identity_changed')
        self.assertIsNone(result['results'][0]['sampled_at'])
        self.assertIsNone(self.sampler.capacity_snapshot()[0]['total_bytes'])

    def test_visible_power_revocation_discards_entire_round(self):
        self.refresh()
        before = dict(self.sampler._capacity_cache[11])
        active = [True]
        def eligibility(_):
            active[0] = False
            return None
        with mock.patch.object(self.sampler, '_capacity_probe') as probe:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity_visible(authorizer=lambda: active[0], eligibility=eligibility)
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        probe.assert_not_called()
        self.assertEqual(self.sampler._capacity_cache[11], before)

    def test_visible_postpublication_revocation_or_timeout_restores_valid_history(self):
        for failure in ('authorization_denied', 'timeout'):
            with self.subTest(failure=failure):
                self.refresh()
                original = self.sampler.capacity_snapshot()[0]
                saved_values = dict(self.values)
                self.values = dict(self.values, used_bytes=102400, usage_percent=25.0)
                state, clock = [True], [time.monotonic()]
                replace = self.sampler._capacity_replace_cache
                def publish(records):
                    replace(records)
                    if records and records.get(11, {}).get('values', {}).get('usage_percent') == 25:
                        if failure == 'authorization_denied':
                            state[0] = False
                        else:
                            clock[0] += 20
                with mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.probe), \
                        mock.patch.object(self.sampler, '_capacity_replace_cache', side_effect=publish), \
                        mock.patch.object(service.time, 'monotonic', side_effect=lambda: clock[0]):
                    with self.assertRaises(service.CapacityRefreshError) as caught:
                        self.sampler.refresh_capacity_visible(authorizer=lambda: state[0], eligibility=lambda _: None)
                self.assertEqual(caught.exception.reason, failure)
                self.assertEqual(self.sampler.capacity_snapshot()[0], original)
                self.values = saved_values

    def test_visible_postpublication_failure_cannot_restore_identity_invalidated_history(self):
        self.refresh()
        self.values = dict(self.values, used_bytes=102400, usage_percent=25.0)
        replace = self.sampler._capacity_replace_cache
        def publish(records):
            replace(records)
            if records and records.get(11, {}).get('values', {}).get('usage_percent') == 25:
                self.watch.changed.return_value = True
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.probe), \
                mock.patch.object(self.sampler, '_capacity_replace_cache', side_effect=publish):
            with self.assertRaises(service.CapacityRefreshError):
                self.sampler.refresh_capacity_visible(authorizer=lambda: True, eligibility=lambda _: None)
        self.assertFalse(self.sampler._capacity_cache)

    def visible_combination(self, response):
        """Actual root Web/SMART/capacity chain; only commands and worker fake."""
        import test_smart_refresh_core as smart_fixture
        with mock.patch.object(sys, 'path', [str(ROOT / 'runtime')] + sys.path):
            import web_config
        case = smart_fixture.SmartRefreshCoreTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.block('sdb', '8:16', 'disk-b')
        case.refresh()
        case.behavior = response
        web = object.__new__(web_config.WebConfigServer)
        web._storage_fill_lock = threading.Lock()
        web.insights = case.collector
        web.monitor = service.StatusMonitor(self.sampler)
        return web, case

    def test_actual_web_visible_chain_refreshes_both_positive_caches(self):
        import test_smart_refresh_core as smart_fixture
        self.refresh()
        response = {'power_mode': {'string': 'ACTIVE or IDLE'}, **smart_fixture.SmartRefreshCoreTests.good_response(48)}
        web, case = self.visible_combination((0, response))
        self.values = dict(self.values, used_bytes=102400, usage_percent=25.0)
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.probe) as probe:
            result = web.storage_refresh(authorizer=lambda: True)
        self.assertEqual(len(case.calls), 4)  # Two real fixture disks, old + new ordinary queries.
        self.assertTrue(all(call[0][1:3] == ['-n', 'standby'] for call in case.calls))
        probe.assert_called_once()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['capacity']['results'][0]['status'], 'refreshed')
        self.assertEqual(self.sampler.capacity_snapshot()[0]['usage_percent'], 25)

    def test_actual_web_standby_unknown_and_failure_cannot_measure_capacity(self):
        import subprocess
        import test_smart_refresh_core as smart_fixture
        for response, status, reason in (
                ((2, {'power_mode': {'string': 'STANDBY'}}), 'skipped_standby', 'standby'),
                ((0, {'power_mode': {'string': 'UNKNOWN'}, **smart_fixture.SmartRefreshCoreTests.good_response()}),
                 'unavailable', 'power_state_unverified'),
                (subprocess.TimeoutExpired('fake-command', 3), 'unavailable', 'power_state_unverified')):
            with self.subTest(status=status, reason=reason):
                self.refresh()
                old = self.sampler.capacity_snapshot()[0]
                web, case = self.visible_combination(response)
                before = case.collector.smart_cached('sdb')
                with mock.patch.object(self.sampler, '_capacity_probe') as probe:
                    result = web.storage_refresh(authorizer=lambda: True)
                probe.assert_not_called()
                row = result['capacity']['results'][0]
                self.assertEqual((row['status'], row['reason']), (status, reason))
                self.assertEqual(row['sampled_at'], old['capacity_sampled_at'])
                self.assertEqual(self.sampler.capacity_snapshot()[0], old)
                self.assertIsNone(case.collector.smart_cached('sdb')['smart_temperature_c'])
                if status != 'skipped_standby':
                    self.assertEqual(case.collector.smart_cached('sdb')['smart_sampled_at'], before['smart_sampled_at'])

    def test_eligibility_rechecked_after_measurement_and_before_commit(self):
        for sequence in ([None, 'standby'], [None, None, 'power_state_unverified']):
            with self.subTest(sequence=sequence):
                result = self.fill(mock.Mock(side_effect=sequence))
                self.assertIn(result['results'][0]['status'], ('skipped_standby', 'unavailable'))
                self.assertFalse(self.sampler._capacity_cache)

    def test_same_lifetime_failures_are_negatively_cached_for_30_seconds(self):
        error = service.CapacityRefreshError('probe_failed')
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=error) as probe:
            first = self.sampler.refresh_capacity_missing(authorizer=lambda: True, eligibility=lambda _: None)
            second = self.sampler.refresh_capacity_missing(authorizer=lambda: True, eligibility=lambda _: None)
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(first['results'], second['results'])
            expiry = next(iter(self.sampler._capacity_negative.values()))[0]
            with mock.patch.object(service.time, 'monotonic', return_value=expiry + .1):
                self.sampler.refresh_capacity_missing(authorizer=lambda: True, eligibility=lambda _: None)
            self.assertEqual(probe.call_count, 2)
        self.generation += 1
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=error) as probe:
            self.sampler.refresh_capacity_missing(authorizer=lambda: True, eligibility=lambda _: None)
        probe.assert_called_once()

    def test_negative_failure_is_discarded_when_batch_authority_is_revoked(self):
        allowed = [True]
        def probe(*_):
            allowed[0] = False
            raise service.CapacityRefreshError('probe_failed')
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            with self.assertRaises(service.CapacityRefreshError):
                self.sampler.refresh_capacity_missing(authorizer=lambda: allowed[0], eligibility=lambda _: None)
        self.assertFalse(self.sampler._capacity_negative)

    def test_passive_unmount_observation_clears_negative_cache_generation(self):
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=service.CapacityRefreshError('probe_failed')):
            self.sampler.refresh_capacity_missing(authorizer=lambda: True, eligibility=lambda _: None)
        self.assertTrue(self.sampler._capacity_negative)
        self.mountinfo.write_text('')
        self.sampler.capacity_snapshot()
        self.assertFalse(self.sampler._capacity_negative)
        self.write_mount()
        with mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.probe) as probe:
            self.sampler.refresh_capacity_missing(authorizer=lambda: True, eligibility=lambda _: None)
        probe.assert_called_once()

    def test_post_publication_revocation_discards_new_cache(self):
        allowed = [True]
        replace = self.sampler._capacity_replace_cache
        def publish(records):
            replace(records)
            if records:
                allowed[0] = False
        with mock.patch.object(self.sampler, '_capacity_replace_cache', side_effect=publish), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=self.probe):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: allowed[0])
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        self.assertFalse(self.sampler._capacity_cache)

    def test_private_worker_failure_text_is_not_exported(self):
        entry = service._capacity_inventory(self.proc, self.sys)[0][0]
        context, *_ = self.fake_child({'ok': False, 'reason': '/private/SECRET exception'})
        with context:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler._capacity_probe(entry, time.monotonic() + 2, lambda: None)
        self.assertEqual(str(caught.exception), 'probe_failed')

    def test_private_worker_validates_values_not_only_ok_flag(self):
        entry = service._capacity_inventory(self.proc, self.sys)[0][0]
        for value in (dict(self.values, total_bytes=True), dict(self.values, used_bytes=-1),
                      dict(self.values, usage_percent=0), dict(self.values, usage_percent=float('nan'))):
            with self.subTest(value=value):
                context, *_ = self.fake_child({'ok': True, 'values': value})
                with context:
                    with self.assertRaises(service.CapacityRefreshError) as caught:
                        self.sampler._capacity_probe(entry, time.monotonic() + 2, lambda: None)
                self.assertEqual(caught.exception.reason, 'probe_failed')

    def test_malformed_fd_message_closes_delivered_handle(self):
        entry = service._capacity_inventory(self.proc, self.sys)[0][0]
        context, worker, spawn, received = self.fake_child(marker=b'bad')
        with context:
            with self.assertRaises(service.CapacityRefreshError):
                self.sampler._capacity_probe(entry, time.monotonic() + 2, lambda: None)
        with self.assertRaises(OSError):
            os.fstat(received)

    def test_worker_timeout_does_not_wait_and_keeps_lingering_handle_busy(self):
        entry = service._capacity_inventory(self.proc, self.sys)[0][0]
        context, worker, *_ = self.fake_child(alive=True)
        with context:
            before = time.monotonic()
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler._capacity_probe(entry, before + .025, lambda: None)
        self.assertEqual(caught.exception.reason, 'timeout')
        self.assertLess(time.monotonic() - before, .5)
        worker.kill.assert_called_once()
        self.assertIs(self.sampler._capacity_worker, worker)
        with self.assertRaises(service.CapacityRefreshError) as caught:
            self.refresh()
        self.assertEqual(caught.exception.reason, 'busy')
        self.sampler._capacity_worker = None

    def test_worker_revocation_kills_and_discards_result(self):
        entry = service._capacity_inventory(self.proc, self.sys)[0][0]
        context, worker, *_ = self.fake_child(alive=True)
        calls = [0]
        def authorized():
            calls[0] += 1
            if calls[0] > 1:
                raise service.CapacityRefreshError('authorization_denied')
        with context:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler._capacity_probe(entry, time.monotonic() + 2, authorized)
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        worker.kill.assert_called_once()
        self.sampler._capacity_worker = None


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux real mount/FD fixture')
class CapacityLinuxHandleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-capacity-real-')
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / 'drives/data'
        self.target.mkdir(parents=True)
        descriptor = os.open(self.target, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.mount_id = service._capacity_fd_mount(descriptor)
            device = os.fstat(descriptor).st_dev
        finally:
            os.close(descriptor)
        self.entry = {'mount': str(self.target), 'mount_id': self.mount_id,
                      'device': f'{os.major(device)}:{os.minor(device)}', '_key': 'fixture-key'}
        self.root = mock.patch.object(service, '_CAPACITY_DRIVES_ROOT', str(self.target.parent))
        inventory = mock.patch.object(service, '_capacity_inventory', return_value=([dict(self.entry)], True))
        self.root.start()
        self.inventory = inventory.start()
        self.addCleanup(self.root.stop)
        self.addCleanup(inventory.stop)

    def test_actual_directory_fd_and_kernel_mnt_id_measurement(self):
        result = service._capacity_measure_entry(self.entry, time.monotonic() + 2)
        self.assertGreater(result['total_bytes'], 0)
        self.assertGreaterEqual(result['used_bytes'], 0)

    def test_wrong_mount_id_never_runs_fstatvfs(self):
        entry = dict(self.entry, mount_id=self.mount_id + 1)
        self.inventory.return_value = ([entry], True)
        with mock.patch.object(service.os, 'fstatvfs') as measure:
            with self.assertRaises(service.CapacityRefreshError):
                service._capacity_measure_entry(entry, time.monotonic() + 2)
        measure.assert_not_called()

    def test_wrong_device_never_runs_fstatvfs(self):
        entry = dict(self.entry, device='999:999')
        self.inventory.return_value = ([entry], True)
        with mock.patch.object(service.os, 'fstatvfs') as measure:
            with self.assertRaises(service.CapacityRefreshError):
                service._capacity_measure_entry(entry, time.monotonic() + 2)
        measure.assert_not_called()

    def test_symlink_target_is_rejected(self):
        alias = self.target.parent / 'alias'
        alias.symlink_to(self.target, target_is_directory=True)
        entry = dict(self.entry, mount=str(alias))
        self.inventory.return_value = ([entry], True)
        with mock.patch.object(service.os, 'fstatvfs') as measure:
            with self.assertRaises(OSError):
                service._capacity_measure_entry(entry, time.monotonic() + 2)
        measure.assert_not_called()

    def test_mount_change_during_measurement_rejects_and_closes_fd(self):
        real = os.fstatvfs
        descriptors = []
        def measure(fd):
            descriptors.append(fd)
            result = real(fd)
            self.inventory.return_value = ([], True)
            return result
        with mock.patch.object(service.os, 'fstatvfs', side_effect=measure):
            with self.assertRaises(service.CapacityRefreshError):
                service._capacity_measure_entry(self.entry, time.monotonic() + 2)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    def test_mount_reference_transfer_is_real_scm_rights(self):
        values, fd = service._capacity_measure_entry(self.entry, time.monotonic() + 2, keep_handle=True)
        import array
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        try:
            left.sendmsg([b'1'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))])
            _, ancillary, _, _ = right.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize))
            received = array.array('i')
            received.frombytes(ancillary[0][2])
            try:
                self.assertEqual(service._capacity_fd_mount(received[0]), self.mount_id)
                self.assertEqual(os.fstat(fd).st_ino, os.fstat(received[0]).st_ino)
            finally:
                os.close(received[0])
        finally:
            os.close(fd)
        self.assertGreater(values['total_bytes'], 0)

    def test_private_child_protocol_transfers_a_real_verified_mount_pin(self):
        """Execute Python only, with a temp directory and fixed inventory fixture."""
        real_popen = service.subprocess.Popen
        bootstrap = (
            'import importlib.util,sys;'
            f'spec=importlib.util.spec_from_file_location("capacity_child_fixture",{str(ROOT / "runtime/ui_service.py")!r});'
            's=importlib.util.module_from_spec(spec);spec.loader.exec_module(s);'
            f's._CAPACITY_DRIVES_ROOT={str(self.target.parent)!r};'
            f's._capacity_inventory=lambda *_:([{self.entry!r}],True);'
            'sys.exit(s._capacity_worker_main())')
        launches = []
        def spawn(args, **kwargs):
            launches.append(args)
            return real_popen([sys.executable, '-B', '-c', bootstrap], **kwargs)
        sampler = service.StatusSampler()
        self.addCleanup(sampler.close_capacity)
        with mock.patch.object(service.subprocess, 'Popen', side_effect=spawn):
            values, anchor = sampler._capacity_probe(self.entry, time.monotonic() + 5, lambda: None)
        try:
            self.assertEqual(service._capacity_fd_mount(anchor), self.mount_id)
            self.assertGreater(values['total_bytes'], 0)
            self.assertEqual(launches[0][1], '-B')
            self.assertIsNone(sampler._capacity_worker)
        finally:
            os.close(anchor)


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux sysfs symlink lifetime fixture')
class CapacitySysfsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='r1-capacity-sysfs-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.physical = self.root / 'devices/pci/block/sdb'
        self.partition = self.physical / 'sdb1'
        self.partition.mkdir(parents=True)
        self.links = self.root / 'dev/block'
        self.links.mkdir(parents=True)
        (self.links / '8:17').symlink_to(self.partition, target_is_directory=True)
        (self.physical / 'dev').write_text('8:16\n')
        (self.physical / 'diskseq').write_text('123\n')
        (self.partition / 'dev').write_text('8:17\n')
        (self.partition / 'partition').write_text('1\n')

    def test_real_sysfs_identity_includes_physical_generation_and_diskseq(self):
        first = service._capacity_device(self.root, '8:17')
        self.assertIsNotNone(first)
        self.assertEqual(first[-1], 'sdb')
        (self.physical / 'diskseq').write_text('124\n')
        self.assertNotEqual(service._capacity_device(self.root, '8:17'), first)

    def test_virtual_unknown_and_malformed_partition_identities_are_unavailable(self):
        (self.partition / 'partition').write_text('not-an-index\n')
        self.assertIsNone(service._capacity_device(self.root, '8:17'))
        self.assertIsNone(service._capacity_device(self.root, '8:99'))

    def test_replacement_while_sampling_cannot_publish_mixed_generation(self):
        real = service._capacity_read
        def read(path, limit):
            result = real(path, limit)
            if Path(path) == self.partition / 'dev':
                self.partition.rename(self.partition.with_name('removed'))
                self.partition.mkdir()
                (self.partition / 'dev').write_text('8:17\n')
                (self.partition / 'partition').write_text('1\n')
            return result
        with mock.patch.object(service, '_capacity_read', side_effect=read):
            self.assertIsNone(service._capacity_device(self.root, '8:17'))


if __name__ == '__main__':
    unittest.main()

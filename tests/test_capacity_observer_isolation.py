"""Pure capacity evidence faults cannot change the underlying safety decision."""
import contextlib
import copy
import json
import os
from pathlib import Path, PurePosixPath
import stat
import time
import types
import unittest
from unittest import mock

import test_capacity_refresh_core as core

service = core.service
REAL_DEVICE = service._capacity_device
REAL_WATCH = service._CapacityMountWatch


class _BrokenEvidence(dict):
    """A private observer sink fails while real kernel operations remain valid."""
    def __setitem__(self, *_):
        raise ValueError('PRIVATE_OBSERVER_CANARY')

    def update(self, *_, **__):
        raise ValueError('PRIVATE_OBSERVER_CANARY')

    def setdefault(self, *_):
        raise ValueError('PRIVATE_OBSERVER_CANARY')


class _BrokenContext:
    """The optional thread-local observer metadata cannot become a business gate."""
    def __getattribute__(self, name):
        raise ValueError('PRIVATE_OBSERVER_CANARY')

    def __setattr__(self, name, value):
        raise ValueError('PRIVATE_OBSERVER_CANARY')


class _KernelPath:
    """Portable OS seam for the real sysfs identity algorithm, not a substitute."""
    changed = False
    target_reads = 0
    base = PurePosixPath('/fixture/sys')
    link = base / 'dev/block/8:2'
    physical = base / 'devices/pci/block/sdb'
    target = physical / 'sdb1'

    def __init__(self, value):
        self.path = PurePosixPath(str(value))

    def __str__(self):
        return str(self.path)

    def __truediv__(self, value):
        return type(self)(self.path / value)

    def __eq__(self, other):
        return isinstance(other, type(self)) and self.path == other.path

    def __hash__(self):
        return hash(self.path)

    @property
    def parents(self):
        return tuple(type(self)(parent) for parent in self.path.parents)

    @property
    def parent(self):
        return type(self)(self.path.parent)

    @property
    def name(self):
        return self.path.name

    def relative_to(self, other):
        return self.path.relative_to(other.path)

    def resolve(self, strict=False):
        if self.path == self.link:
            return type(self)(self.target)
        if self.path == self.base:
            return self
        raise OSError('unexpected fixture path')

    def lstat(self):
        if self.path != self.link:
            raise OSError('unexpected fixture link')
        return types.SimpleNamespace(st_dev=8, st_ino=17, st_ctime_ns=101,
                                     st_mode=stat.S_IFLNK | 0o777)

    def stat(self):
        if self.path == self.target:
            type(self).target_reads += 1
            changed = type(self).changed and type(self).target_reads > 1
            return types.SimpleNamespace(st_dev=8, st_ino=18, st_ctime_ns=102 + changed,
                                         st_mode=stat.S_IFDIR | 0o755)
        if self.path == self.physical:
            return types.SimpleNamespace(st_dev=8, st_ino=19, st_ctime_ns=103,
                                         st_mode=stat.S_IFDIR | 0o755)
        raise OSError('unexpected fixture identity')


class CapacityObserverIsolationTests(unittest.TestCase):
    """Exercise the real sampler with only its existing external OS seams replaced."""

    def setUp(self):
        self.case = core.CapacityCoreTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.sampler = self.case.sampler

    def assert_observer_error(self):
        value = self.sampler.capacity_cache_diagnostics()
        encoded = json.dumps(value)
        self.assertIn('observer_error', encoded)
        self.assertNotIn('PRIVATE_OBSERVER_CANARY', encoded)
        return value

    def assert_closed(self, descriptors):
        self.assertTrue(descriptors)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def assert_refresh_again(self):
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())
        self.assertEqual(self.case.refresh()['results'][0]['status'], 'refreshed')

    def test_native_event_fault_preserves_projection_and_refresh_lock(self):
        self.case.refresh()
        expected = self.sampler.capacity_snapshot()
        with mock.patch.object(self.sampler._capacity_ledger, 'event',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            self.assertEqual(self.sampler.capacity_snapshot(), expected)
            self.assertEqual(self.case.refresh()['results'][0]['status'], 'refreshed')
            self.assertFalse(self.sampler._capacity_refresh_lock.locked())
        self.assert_observer_error()
        self.assert_refresh_again()

    def test_record_entry_fault_does_not_escape_snapshot_or_acquired_batch(self):
        self.case.refresh()
        expected = self.sampler.capacity_snapshot()
        with mock.patch.object(self.sampler, '_capacity_record',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            self.assertEqual(self.sampler.capacity_snapshot(), expected)
            self.assertEqual(self.case.refresh()['results'][0]['status'], 'refreshed')
        self.assert_observer_error()
        self.assert_refresh_again()

    def test_ledger_construction_fault_does_not_prevent_sampler_lifecycle(self):
        with mock.patch.object(service, '_CapacityLedger',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            sampler = service.StatusSampler(self.case.proc, self.case.sys)
        self.addCleanup(sampler.close_capacity)
        with mock.patch.object(sampler, '_capacity_probe', side_effect=self.case.probe):
            self.assertEqual(sampler.refresh_capacity(authorizer=lambda: True)['results'][0]['status'],
                             'refreshed')
        self.assertEqual(sampler.capacity_snapshot()[0]['usage_percent'], 75.0)
        self.assertFalse(sampler._capacity_refresh_lock.locked())
        evidence = json.dumps(sampler.capacity_cache_diagnostics())
        self.assertIn('observer_error', evidence)
        self.assertNotIn('PRIVATE_OBSERVER_CANARY', evidence)
        sampler.close_capacity()
        self.assertFalse(sampler._capacity_cache)

    def test_thread_observation_context_fault_preserves_dated_projection_and_refresh(self):
        self.case.refresh()
        expected = self.sampler.capacity_snapshot()
        with mock.patch.object(self.sampler, '_capacity_context', _BrokenContext()):
            self.assertEqual(self.sampler.capacity_snapshot(), expected)
            self.assertEqual(self.case.refresh()['results'][0]['status'], 'refreshed')
        self.assert_observer_error()
        self.assert_refresh_again()

    def test_ledger_record_digest_fault_cannot_block_real_invalidation(self):
        self.case.refresh()
        old_epoch = self.sampler._capacity_mount_epoch
        with mock.patch.object(self.sampler._capacity_ledger, 'records',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            self.case.generation += 1
            self.assertIsNone(self.sampler.capacity_snapshot()[0]['total_bytes'])
        self.assertFalse(self.sampler._capacity_cache)
        self.assertGreater(self.sampler._capacity_mount_epoch, old_epoch)
        self.assert_observer_error()
        self.assert_refresh_again()

    def test_inventory_component_digest_fault_preserves_actual_keys_and_rows(self):
        expected, complete = service._capacity_inventory(self.case.proc, self.case.sys)
        self.assertTrue(complete)
        with mock.patch.object(service, '_capacity_digest',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            observed, actual_complete = self.sampler._capacity_inventory_observe()
            self.assertEqual(list(observed), expected)
            self.assertIs(actual_complete, True)
            self.assertEqual(self.case.refresh()['results'][0]['status'], 'refreshed')
        self.assert_observer_error()
        self.assert_refresh_again()

    def test_inventory_evidence_sink_failure_preserves_inventory_decision(self):
        expected = service._capacity_inventory(self.case.proc, self.case.sys)
        previous = getattr(service._CAPACITY_TRACE, 'inventory', None)
        service._CAPACITY_TRACE.inventory = _BrokenEvidence()
        try:
            self.assertEqual(service._capacity_inventory(self.case.proc, self.case.sys), expected)
        finally:
            service._CAPACITY_TRACE.inventory = previous

    def test_private_inventory_wrapper_fault_is_not_a_business_scan_failure(self):
        self.case.refresh()
        expected = self.sampler.capacity_snapshot()
        with mock.patch.object(service, '_CapacityEntries',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            self.assertEqual(self.sampler.capacity_snapshot(), expected)
            self.assertEqual(self.case.refresh()['results'][0]['status'], 'refreshed')
        self.assert_observer_error()
        self.assert_refresh_again()

    def test_traced_read_sink_failure_does_not_change_bounded_read_result(self):
        path = self.case.root / 'fixed-kernel-metadata'
        path.write_bytes(b'8:2\n')
        self.assertEqual(service._capacity_traced_read(path, 64, _BrokenEvidence()), '8:2\n')
        path.write_bytes(b'\xff')
        self.assertIsNone(service._capacity_traced_read(path, 64, _BrokenEvidence()))
        path.write_bytes(b'x' * 65)
        self.assertIsNone(service._capacity_traced_read(path, 64, _BrokenEvidence()))
        self.assertIsNone(service._capacity_traced_read(self.case.root / 'missing', 64, _BrokenEvidence()))

    def test_watch_evidence_formatter_failure_keeps_real_event_gate(self):
        self.case.refresh()
        expected = self.sampler.capacity_snapshot()
        with mock.patch.object(self.sampler, '_capacity_watch_evidence',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            self.case.watch.observation = {'reason': 'unchanged', 'mask': 0}
            self.assertEqual(self.sampler.capacity_snapshot(), expected)
            old_epoch = self.sampler._capacity_mount_epoch
            self.case.watch.changed.return_value = True
            self.case.watch.observation = {'reason': 'mount_event', 'mask': 2}
            self.assertIsNone(self.sampler.capacity_snapshot()[0]['total_bytes'])
        self.assertFalse(self.sampler._capacity_cache)
        self.assertGreater(self.sampler._capacity_mount_epoch, old_epoch)
        self.assertIsNone(self.sampler._capacity_watch)
        self.assert_observer_error()

    def test_watch_observation_getter_fault_does_not_suppress_real_mount_event(self):
        class Watch:
            def __init__(self):
                self.changed = mock.Mock(return_value=False)
                self.close = mock.Mock()

            @property
            def observation(self):
                raise ValueError('PRIVATE_OBSERVER_CANARY')
        self.case.refresh()
        expected = self.sampler.capacity_snapshot()
        watch = Watch()
        self.sampler._capacity_watch = watch
        self.assertEqual(self.sampler.capacity_snapshot(), expected)
        before = self.sampler._capacity_mount_epoch
        watch.changed.return_value = True
        self.assertIsNone(self.sampler.capacity_snapshot()[0]['total_bytes'])
        self.assertFalse(self.sampler._capacity_cache)
        self.assertGreater(self.sampler._capacity_mount_epoch, before)
        watch.close.assert_called_once_with()
        self.assert_observer_error()

    def test_real_watcher_observation_sanitizer_cannot_override_poll_or_namespace(self):
        watch = REAL_WATCH.__new__(REAL_WATCH)
        watch.descriptor = 41
        watch.namespace = Path('/proc/self/ns/mnt')
        watch.identity = 'mnt:[123]'
        watch.poller = types.SimpleNamespace(poll=mock.Mock(return_value=[]))
        with mock.patch.object(service.os, 'readlink', return_value='mnt:[123]') as readlink, \
                mock.patch.object(service.re, 'fullmatch', side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            self.assertIs(watch.changed(), False)
        readlink.assert_called_once_with(watch.namespace)
        watch.poller.poll.assert_called_once_with(0)
        watch.poller.poll.reset_mock()
        with mock.patch.object(service.os, 'readlink', return_value='mnt:[124]'), \
                mock.patch.object(service.re, 'fullmatch', side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            self.assertIs(watch.changed(), True)
        watch.poller.poll.assert_not_called()

    def test_lost_descriptor_short_circuits_even_when_observed_identity_getter_fails(self):
        class Watch(REAL_WATCH):
            @property
            def identity(self):
                raise RuntimeError('PRIVATE_OBSERVER_CANARY')
        watch = Watch.__new__(Watch)
        watch.descriptor = None
        watch.namespace = Path('/proc/self/ns/mnt')
        watch.poller = types.SimpleNamespace(poll=mock.Mock())
        with mock.patch.object(service.os, 'readlink') as readlink:
            self.assertIs(watch.changed(), True)
        readlink.assert_not_called()
        watch.poller.poll.assert_not_called()
        self.assertEqual(watch.observation['observation_status'], 'observer_error')
        self.assertNotIn('PRIVATE_OBSERVER_CANARY', json.dumps(watch.observation))

    def test_real_identity_getter_exception_is_not_swallowed_after_valid_descriptor(self):
        business_error = RuntimeError('controlled true namespace identity failure')
        class Watch(REAL_WATCH):
            @property
            def identity(self):
                raise business_error
        watch = Watch.__new__(Watch)
        watch.descriptor = 41
        watch.namespace = Path('/proc/self/ns/mnt')
        watch.poller = types.SimpleNamespace(poll=mock.Mock())
        with mock.patch.object(service.os, 'readlink', return_value='mnt:[123]') as readlink:
            with self.assertRaises(RuntimeError) as caught:
                watch.changed()
        self.assertIs(caught.exception, business_error)
        readlink.assert_called_once_with(watch.namespace)
        watch.poller.poll.assert_not_called()

    def test_bad_poll_evidence_mask_fold_does_not_change_true_event_result(self):
        for events in ([(41, object())], [object()], [(41, 'PRIVATE_OBSERVER_CANARY')]):
            with self.subTest(event_shape=type(events[0]).__name__):
                watch = REAL_WATCH.__new__(REAL_WATCH)
                watch.descriptor = 41
                watch.namespace = Path('/proc/self/ns/mnt')
                watch.identity = 'mnt:[123]'
                watch.poller = types.SimpleNamespace(poll=mock.Mock(return_value=events))
                with mock.patch.object(service.os, 'readlink', return_value='mnt:[123]') as readlink:
                    self.assertIs(watch.changed(), True)
                readlink.assert_called_once_with(watch.namespace)
                watch.poller.poll.assert_called_once_with(0)
                self.assertEqual(watch.observation['observation_status'], 'observer_error')
                self.assertNotIn('PRIVATE_OBSERVER_CANARY', json.dumps(watch.observation))

    def test_watch_observation_and_failure_marker_setters_cannot_override_false(self):
        class Watch(REAL_WATCH):
            @property
            def observation(self):
                return None

            @observation.setter
            def observation(self, value):
                raise ValueError('PRIVATE_OBSERVER_CANARY')

            @property
            def _capacity_observation_failed(self):
                return False

            @_capacity_observation_failed.setter
            def _capacity_observation_failed(self, value):
                raise ValueError('PRIVATE_OBSERVER_CANARY')
        watch = Watch.__new__(Watch)
        watch.descriptor = 41
        watch.namespace = Path('/proc/self/ns/mnt')
        watch.identity = 'mnt:[123]'
        watch.poller = types.SimpleNamespace(poll=mock.Mock(return_value=[]))
        with mock.patch.object(service.os, 'readlink', return_value='mnt:[123]') as readlink:
            self.assertIs(watch.changed(), False)
        readlink.assert_called_once_with(watch.namespace)
        watch.poller.poll.assert_called_once_with(0)

    def test_broken_trace_root_getter_preserves_real_read_and_inventory(self):
        path = self.case.root / 'fixed-trace-metadata'
        path.write_bytes(b'8:2\n')
        expected = service._capacity_inventory(self.case.proc, self.case.sys)
        with mock.patch.object(service, '_CAPACITY_TRACE', _BrokenContext()):
            self.assertEqual(service._capacity_read(path, 64), '8:2\n')
            self.assertEqual(service._capacity_traced_read(path, 64, _BrokenEvidence()), '8:2\n')
            self.assertEqual(service._capacity_inventory(self.case.proc, self.case.sys), expected)
            path.write_bytes(b'\xff')
            self.assertIsNone(service._capacity_read(path, 64))

    def test_watch_creation_evidence_failure_preserves_exact_real_error(self):
        error = service.CapacityRefreshError('unavailable')
        error.watch_observation = {'reason': 'watch_error', 'stage': 'open_mountinfo'}
        with mock.patch.object(service, '_CapacityMountWatch', side_effect=error), \
                mock.patch.object(self.sampler, '_capacity_watch_evidence',
                                  side_effect=ValueError('PRIVATE_OBSERVER_CANARY')), \
                mock.patch.object(self.sampler, '_capacity_probe') as probe:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertIs(caught.exception, error)
        probe.assert_not_called()
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())

    def test_commit_observer_fault_does_not_drop_success_or_leave_mount_pin(self):
        event = self.sampler._capacity_ledger.event
        descriptors = []
        def observe(sampler, reason, **kwargs):
            if reason == 'commit_success':
                raise ValueError('PRIVATE_OBSERVER_CANARY')
            return event(sampler, reason, **kwargs)
        def probe(*args):
            value = self.case.probe(*args)
            descriptors.append(value[1])
            return value
        with mock.patch.object(self.sampler._capacity_ledger, 'event', side_effect=observe), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            result = self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(result['results'][0]['status'], 'refreshed')
        self.assertEqual(self.sampler.capacity_snapshot()[0]['capacity_sampled_at'],
                         result['results'][0]['sampled_at'])
        self.assert_closed(descriptors)
        self.assert_observer_error()
        self.assert_refresh_again()

    def test_rollback_observer_failure_keeps_real_timeout_and_original_date(self):
        self.case.refresh()
        original = copy.deepcopy(self.sampler._capacity_cache)
        old_rows = self.sampler.capacity_snapshot()
        replace, event = self.sampler._capacity_replace_cache, self.sampler._capacity_ledger.event
        tick, descriptors = [time.monotonic()], []
        self.case.values.update(used_bytes=204800, usage_percent=50.0)
        def replace_and_expire(records, *args, **kwargs):
            value = replace(records, *args, **kwargs)
            if kwargs.get('reason') == 'commit_prepare':
                tick[0] += 20
            return value
        def observe(sampler, reason, **kwargs):
            if reason.startswith('rollback_'):
                raise ValueError('PRIVATE_OBSERVER_CANARY')
            return event(sampler, reason, **kwargs)
        def probe(*args):
            value = self.case.probe(*args)
            descriptors.append(value[1])
            return value
        with mock.patch.object(self.sampler, '_capacity_replace_cache', side_effect=replace_and_expire), \
                mock.patch.object(self.sampler._capacity_ledger, 'event', side_effect=observe), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe), \
                mock.patch.object(service.time, 'monotonic', side_effect=lambda: tick[0]):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(caught.exception.reason, 'timeout')
        self.assertEqual(self.sampler._capacity_cache, original)
        self.assertEqual(self.sampler.capacity_snapshot(), old_rows)
        self.assert_closed(descriptors)
        self.assert_observer_error()
        self.assert_refresh_again()

    def test_authorization_denial_with_observer_fault_never_starts_worker(self):
        self.case.refresh()
        old = copy.deepcopy(self.sampler._capacity_cache)
        with mock.patch.object(self.sampler, '_capacity_record',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')), \
                mock.patch.object(self.sampler, '_capacity_probe') as probe:
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: False)
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        probe.assert_not_called()
        self.assertEqual(self.sampler._capacity_cache, old)
        self.assertEqual(self.sampler.capacity_cache_diagnostics()['state']['pending'], 0)
        self.assert_refresh_again()

    def test_pending_denial_closes_real_fd_despite_observer_failure(self):
        allowed, descriptors = [True], []
        def probe(*args):
            value = self.case.probe(*args)
            descriptors.append(value[1])
            allowed[0] = False
            return value
        with mock.patch.object(self.sampler, '_capacity_record',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=probe):
            with self.assertRaises(service.CapacityRefreshError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: allowed[0])
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        self.assertFalse(self.sampler._capacity_cache)
        self.assert_closed(descriptors)
        self.assertEqual(self.sampler.capacity_cache_diagnostics()['state']['pending'], 0)
        self.assert_refresh_again()

    def test_business_exception_object_survives_observer_failure_and_unlocks(self):
        business_error = RuntimeError('controlled real worker error')
        with mock.patch.object(self.sampler, '_capacity_record',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=business_error):
            with self.assertRaises(RuntimeError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertIs(caught.exception, business_error)
        self.assertFalse(self.sampler._capacity_cache)
        self.assert_refresh_again()

    def test_production_key_hash_failure_is_not_swallowed_as_observer_error(self):
        real_hash = service.hashlib.sha256
        business_error = ValueError('controlled production key digest failure')
        def hash_value(value, *args, **kwargs):
            # Diagnostic digests are domain separated by a NUL. The original
            # production key hashes the JSON material directly, without one.
            if b'\0' not in value:
                raise business_error
            return real_hash(value, *args, **kwargs)
        with mock.patch.object(service.hashlib, 'sha256', side_effect=hash_value), \
                mock.patch.object(self.sampler, '_capacity_probe') as probe:
            with self.assertRaises(ValueError) as caught:
                self.sampler.refresh_capacity(authorizer=lambda: True)
        self.assertIs(caught.exception, business_error)
        probe.assert_not_called()
        self.assertFalse(self.sampler._capacity_cache)
        self.assert_refresh_again()

    def test_busy_fault_cannot_release_another_batch_or_clear_its_pending(self):
        self.sampler._capacity_refresh_lock.acquire()
        self.sampler._capacity_pending = 1
        try:
            with mock.patch.object(self.sampler, '_capacity_record',
                                   side_effect=ValueError('PRIVATE_OBSERVER_CANARY')), \
                    mock.patch.object(self.sampler, '_capacity_probe') as probe:
                with self.assertRaises(service.CapacityRefreshError) as caught:
                    self.sampler.refresh_capacity(authorizer=lambda: self.fail('busy authorization'))
            self.assertEqual(caught.exception.reason, 'busy')
            probe.assert_not_called()
            self.assertTrue(self.sampler._capacity_refresh_lock.locked())
            self.assertEqual(self.sampler._capacity_pending, 1)
        finally:
            self.sampler._capacity_pending = 0
            self.sampler._capacity_refresh_lock.release()

    def test_real_incomplete_inventory_still_clears_values_when_digest_fails(self):
        self.case.refresh()
        self.case.mountinfo.write_text('malformed mountinfo\n')
        with mock.patch.object(service, '_capacity_digest',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            self.assertEqual(self.sampler.capacity_snapshot(), [])
        self.assertFalse(self.sampler._capacity_cache)
        self.assertFalse(self.sampler._capacity_negative)

    def test_lifecycle_mutation_is_not_stopped_by_record_digest_failure(self):
        self.case.refresh()
        old_epoch = self.sampler._capacity_epoch
        with mock.patch.object(self.sampler._capacity_ledger, 'records',
                               side_effect=ValueError('PRIVATE_OBSERVER_CANARY')):
            self.sampler.close_capacity()
            self.assertTrue(self.sampler._capacity_closed)
            self.assertFalse(self.sampler._capacity_cache)
            self.assertIsNone(self.sampler._capacity_watch)
            self.sampler.start_capacity()
        self.assertFalse(self.sampler._capacity_closed)
        self.assertEqual(self.sampler._capacity_epoch, old_epoch + 2)
        self.assert_refresh_again()


class CapacityDeviceObserverIsolationTests(unittest.TestCase):
    """Real device generation code, controlled portable kernel metadata facade."""
    def device(self, evidence=None, changed=False, fault=None, broken_trace=False):
        _KernelPath.changed, _KernelPath.target_reads = changed, 0
        values = {str(_KernelPath.target / 'partition'): '1\n',
                  str(_KernelPath.target / 'dev'): '8:2\n',
                  str(_KernelPath.physical / 'dev'): '8:0\n',
                  str(_KernelPath.physical / 'diskseq'): '15\n'}
        previous = getattr(service._CAPACITY_TRACE, 'device', None)
        service._CAPACITY_TRACE.device = evidence
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(service, 'Path', _KernelPath))
                stack.enter_context(mock.patch.object(service, '_capacity_read',
                                                     side_effect=lambda path, _: values[str(path)]))
                if fault is not None:
                    stack.enter_context(mock.patch.object(service, fault,
                        side_effect=ValueError('PRIVATE_OBSERVER_CANARY')))
                if broken_trace:
                    stack.enter_context(mock.patch.object(service, '_CAPACITY_TRACE', _BrokenContext()))
                return REAL_DEVICE('/fixture/sys', '8:2')
        finally:
            service._CAPACITY_TRACE.device = previous

    def test_device_diagnostic_digest_failure_preserves_real_identity(self):
        expected = self.device()
        self.assertIsNotNone(expected)
        self.assertEqual(self.device({}, fault='_capacity_digest'), expected)

    def test_device_stat_evidence_failure_preserves_real_identity(self):
        expected = self.device()
        self.assertIsNotNone(expected)
        self.assertEqual(self.device({}, fault='_capacity_stat_evidence'), expected)

    def test_device_evidence_sink_failure_preserves_real_identity(self):
        expected = self.device()
        self.assertEqual(self.device(_BrokenEvidence()), expected)

    def test_real_device_generation_failure_is_not_ignored_with_digest_fault(self):
        self.assertIsNone(self.device({}, changed=True, fault='_capacity_digest'))

    def test_broken_trace_root_does_not_change_real_device_identity_or_rejection(self):
        expected = self.device()
        self.assertIsNotNone(expected)
        self.assertEqual(self.device(broken_trace=True), expected)
        self.assertIsNone(self.device(changed=True, broken_trace=True))


if __name__ == '__main__':
    unittest.main()

"""Page fills and applied-policy SMART demand use only fake commands/devices."""
import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

import test_smart_refresh_core as core_fixture
import fan_policy
from test_fan_policy import Adapter


class SmartFillPolicyTests(unittest.TestCase):
    write = core_fixture.SmartRefreshCoreTests.write
    block = core_fixture.SmartRefreshCoreTests.block
    runner = core_fixture.SmartRefreshCoreTests.runner
    good_response = staticmethod(core_fixture.SmartRefreshCoreTests.good_response)
    row = core_fixture.SmartRefreshCoreTests.row
    publish_disks = core_fixture.SmartRefreshCoreTests.publish_disks
    refresh = core_fixture.SmartRefreshCoreTests.refresh

    def setUp(self):
        core_fixture.SmartRefreshCoreTests.setUp(self)
        self.write(self.sys / 'class/hwmon/hwmon0/name', 'coretemp')
        self.write(self.sys / 'class/hwmon/hwmon0/temp1_input', '55000')
        self.publish_disks()
        self.disk_id = 'disk:' + self.row()['stable_identity']
        self.adapter = Adapter()
        from temperature_sources import TemperatureSources
        self.controller = fan_policy.FanPolicyController(self.temporary / 'policy',
            TemperatureSources(self.collector, self.sys, clock=lambda: self.now), self.adapter,
            clock=lambda: self.now, smart_collector=self.collector)
        self.addCleanup(self.finish_worker)

    def fill(self):
        return self.collector.fill_smart_missing(authorizer=lambda: self.active)

    def serial_alias_fixture(self, *, keep_wwid=False):
        """Real disk lifecycle plus a portable fixed udev text-file read seam.

        Windows cannot create the Linux b<major>:<minor> filename. Only that
        exact metadata-file read is substituted; its production parser, all
        sysfs lifecycle reads and the actual fan controller stay real.
        """
        if not keep_wwid:
            (self.folder / 'wwid').unlink(missing_ok=True)
        serial = core_fixture.PRIVATE
        self.disk_id = 'disk:serial:' + hashlib.sha256(serial.encode()).hexdigest()[:24]
        self.udev = {str(self.collector.run_root / 'udev/data/b8:0'): 'E:ID_SERIAL_SHORT=' + serial}
        import insights
        original = insights._text
        def metadata(path, *args, **kwargs):
            if str(path) in self.udev:
                return self.udev[str(path)]
            return original(path, *args, **kwargs)
        patch = mock.patch.object(insights, '_text', side_effect=metadata)
        patch.start()
        self.addCleanup(patch.stop)
        self.publish_disks()
        return serial

    def selected_row(self):
        rows = [row for row in self.controller.sources.snapshot() if row['id'] == self.disk_id]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_saved_owner_serial_source_restores_on_cold_collector_and_consumes_alias(self):
        serial = self.serial_alias_fixture()
        self.refresh()  # The historical source came from an owner full SMART read.
        self.publish_disks()
        self.assertEqual(self.row()['stable_identity'], self.disk_id[5:])
        settings = copy.deepcopy(fan_policy.DEFAULT_CONFIG)
        settings['channels']['cpu'].update(mode='temperature', sources=[self.disk_id])
        state_dir = self.controller.state_dir
        original_open = os.open
        def portable_open(path, flags, *args, **kwargs):
            if os.name == 'nt' and Path(path) == state_dir:
                # Preserve real _save/write/replace/fsync behavior; Windows has
                # no directory FD, so its final fsync uses the saved fixture.
                return original_open(state_dir / 'fan-policy.json', os.O_RDWR)
            return original_open(path, flags, *args, **kwargs)
        with mock.patch.object(fan_policy.os, 'open', side_effect=portable_open):
            result = self.controller.action('fan.policy.update',
                {'config': settings, 'apply': True, 'confirmed': True})
            self.assertTrue(result['ok'], result)
            self.controller.worker.join(timeout=2)
        saved = json.loads((state_dir / 'fan-policy.json').read_text('utf8'))
        self.assertEqual(saved['active_config']['channels']['cpu']['sources'], [self.disk_id])
        self.assertTrue(saved['resume_on_boot'])
        self.controller.stop()
        self.collector.stop()
        old = self.collector
        self.collector = type(old)(self.proc, self.sys, runner=self.runner,
            command_finder=lambda _: '/fixture/bin/smartctl', clock=lambda: self.now,
            page_size=4096, clock_ticks=100, ipv4_provider=lambda: {}, storage_client=old.storage_client)
        self.addCleanup(self.collector.stop)
        self.publish_disks()
        self.assertIsNone(self.row()['stable_identity'])
        self.adapter = Adapter()
        from temperature_sources import TemperatureSources
        self.controller = fan_policy.FanPolicyController(state_dir,
            TemperatureSources(self.collector, self.sys, clock=lambda: self.now), self.adapter,
            clock=lambda: self.now, smart_collector=self.collector, interval=60)
        self.addCleanup(self.controller.stop)
        self.calls.clear()
        self.controller._request_disk_temperatures()
        self.assertEqual(self.calls, [])  # persisted intent alone is not a grant
        with mock.patch.object(fan_policy.os, 'open', side_effect=portable_open):
            self.controller.start()
            self.controller.worker.join(timeout=2)
        self.assertTrue(self.controller.channels['cpu']['active'])
        self.assertEqual(self.calls, [])
        self.behavior = (0, self.good_response(44))
        self.policy_tick()
        self.assertEqual(self.calls[0][0][1:], ['-n', 'standby', '-j', '-a', '/dev/sda'])
        self.assertEqual(self.selected_row()['temperature_c'], 44)
        self.policy_tick()
        self.assertEqual(self.controller.channels['cpu']['reference_c'], 44)
        self.assertEqual(self.controller.channels['cpu']['level'], 'quiet')
        self.assertIsNone(self.row()['stable_identity'])
        self.assertEqual(self.collector._smart_cache, {})
        self.assertEqual(self.collector._disk_serial_cache, {})
        self.assertNotIn(serial, json.dumps(self.collector.snapshot()))
        self.assertNotIn('smart_data', self.collector.smart_cached('sda'))
        self.refresh()  # A natural owner identity must not duplicate the alias ID.
        self.assertEqual(self.collector.snapshot()['disks']['items'][0]['stable_identity'], self.disk_id[5:])
        self.assertEqual(self.selected_row()['status'], 'ok')

    def test_cold_serial_alias_requires_current_matching_metadata(self):
        self.serial_alias_fixture()
        key = str(self.collector.run_root / 'udev/data/b8:0')
        for value in ('', 'E:ID_SERIAL_SHORT=WRONG'):
            with self.subTest(value=bool(value)):
                self.udev[key] = value
                self.publish_disks()
                self.apply_policy()
                self.policy_tick()
                self.assertEqual(self.calls, [])
                self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')

    def test_cold_serial_alias_rejects_duplicate_metadata_and_conflict(self):
        serial = self.serial_alias_fixture()
        other = self.block('sdb', '8:16', 'other-wwid')
        self.udev[str(self.collector.run_root / 'udev/data/b8:16')] = 'E:ID_SERIAL_SHORT=' + serial
        self.publish_disks()
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.calls, [])
        self.udev[str(self.collector.run_root / 'udev/data/b8:16')] = ''
        self.write(self.folder / 'device/serial', 'CONFLICT')
        self.publish_disks()
        self.policy_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.collector.policy_temperature_aliases()['by_device'], {})

    def test_same_lifetime_stale_owner_and_prior_serial_caches_only_veto_alias(self):
        self.serial_alias_fixture()
        response = self.good_response()
        response['serial_number'] = 'CONFLICT'
        self.behavior = (0, response)
        self.refresh()
        self.now += 31
        self.calls.clear()
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.collector.policy_temperature_aliases()['by_device'], {})
        self.collector._smart_cache.clear()
        identity = self.collector._smart_context('sda')
        self.collector._disk_serial_cache['sda'] = {'cache_key': identity, 'serial': 'CONFLICT'}
        self.policy_tick()
        self.assertEqual(self.calls, [])

    def test_existing_measurement_binds_cold_alias_without_command_or_timestamp_renewal(self):
        self.serial_alias_fixture()
        self.fill()
        stamp = self.row()['smart_sampled_monotonic']
        self.now += 2
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.selected_row()['sampled_monotonic'], stamp)
        self.assertEqual(self.selected_row()['status'], 'ok')

    def test_cold_alias_standby_is_measurement_only_and_keeps_original_sample(self):
        self.serial_alias_fixture()
        self.behavior = (2, {'power_mode': {'string': 'STANDBY'}})
        self.fill()
        stamp = self.row()['smart_sampled_monotonic']
        self.now += 2
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.selected_row()['status'], 'standby')
        self.assertEqual(self.selected_row()['sampled_monotonic'], stamp)
        self.policy_tick()
        self.assertEqual(self.controller.channels['cpu']['level'], 'quiet')
        self.assertEqual(self.collector._smart_cache, {})
        self.assertIsNone(self.row()['stable_identity'])

    def test_cold_alias_is_revoked_on_metadata_change_replacement_and_release(self):
        serial = self.serial_alias_fixture()
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.selected_row()['status'], 'ok')
        self.write(self.folder / 'dev', '8:16')
        self.udev[str(self.collector.run_root / 'udev/data/b8:16')] = 'E:ID_SERIAL_SHORT=OTHER'
        self.publish_disks()
        self.assertEqual(self.selected_row()['status'], 'unavailable')
        self.policy_tick()
        self.assertEqual(len(self.calls), 1)
        with mock.patch.object(self.controller, '_save'):
            result = self.controller.action('fan.policy.release', {'confirmed': True})
            self.assertTrue(result['ok'])
            self.controller.worker.join(timeout=2)
        self.assertEqual(self.selected_row()['status'], 'unavailable')
        self.assertEqual(self.collector._smart_policy_aliases, {})
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.selected_row()['status'], 'unavailable')

    def test_two_channels_base_and_serial_alias_share_one_real_smart_probe(self):
        self.serial_alias_fixture(keep_wwid=True)
        base_id = 'disk:' + self.row()['stable_identity']
        settings = copy.deepcopy(fan_policy.DEFAULT_CONFIG)
        settings['channels']['cpu'].update(mode='temperature', sources=[self.disk_id])
        settings['channels']['system'].update(mode='temperature', sources=[base_id])
        with mock.patch.object(self.controller, '_save'):
            result = self.controller.action('fan.policy.update',
                {'config': settings, 'apply': True, 'confirmed': True})
            self.assertTrue(result['ok'])
            self.controller.worker.join(timeout=2)
        self.policy_tick()
        self.assertEqual(len(self.calls), 1)
        self.policy_tick()
        self.assertEqual(self.controller.channels['cpu']['reference_c'], 37)
        self.assertEqual(self.controller.channels['system']['reference_c'], 37)
        self.assertEqual(self.controller.channels['cpu']['source_id'], self.disk_id)
        self.assertEqual(self.controller.channels['system']['source_id'], base_id)

    def test_cold_alias_metadata_change_during_probe_discards_all_pending_data(self):
        self.serial_alias_fixture()
        def changed(argv, timeout):
            self.udev[str(self.collector.run_root / 'udev/data/b8:0')] = 'E:ID_SERIAL_SHORT=OTHER'
            return 0, self.good_response()
        self.behavior = changed
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.collector._smart_measurements, {})
        self.assertEqual(self.collector._smart_attempts, {})
        self.assertEqual(self.collector._smart_policy_aliases, {})
        self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')

    def test_alias_and_base_on_different_disks_stay_ambiguous_in_actual_catalog(self):
        serial = self.serial_alias_fixture()
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.selected_row()['status'], 'ok')
        second = self.block('sdb', '8:16', 'placeholder')
        (second / 'wwid').unlink()
        self.write(second / 'device/serial', serial)
        self.publish_disks()
        self.fill()  # Real production ordinary fill with a fake command, on new sdb only.
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.selected_row()['status'], 'unavailable')
        self.policy_tick()
        self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')
        self.assertEqual(len(self.calls), 2)

    def test_release_revokes_warm_alias_and_new_policy_cannot_borrow_it_without_metadata(self):
        self.serial_alias_fixture()
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.selected_row()['status'], 'ok')
        with mock.patch.object(self.controller, '_save'):
            result = self.controller.action('fan.policy.release', {'confirmed': True})
            self.assertTrue(result['ok'])
            self.controller.worker.join(timeout=2)
        self.assertEqual(self.selected_row()['status'], 'unavailable')
        self.assertEqual(self.collector._smart_policy_aliases, {})
        self.udev[str(self.collector.run_root / 'udev/data/b8:0')] = ''
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.selected_row()['status'], 'unavailable')
        self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')
        self.assertEqual(len(self.calls), 1)

    def test_old_alias_catalog_cannot_cross_completed_reapply_of_same_source(self):
        self.serial_alias_fixture()
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.selected_row()['status'], 'ok')
        old_epoch = self.controller._smart_policy_epoch
        def reapply_after_catalog_snapshot():
            self.udev[str(self.collector.run_root / 'udev/data/b8:0')] = ''
            self.apply_policy(sources=[self.disk_id])
            self.assertGreater(self.controller._smart_policy_epoch, old_epoch)
            self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')
        with mock.patch.object(self.controller, '_refresh_capability', side_effect=reapply_after_catalog_snapshot):
            self.controller.tick()
        self.finish_worker()
        self.assertEqual(self.selected_row()['status'], 'unavailable')
        self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')
        self.assertIsNone(self.controller.channels['cpu']['reference_c'])

    def assert_replacement_alias_projection(self, *, owner_identity=False):
        self.serial_alias_fixture()
        self.behavior = (0, self.good_response(44))
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.selected_row()['temperature_c'], 44)
        if owner_identity:
            self.refresh()
        record = self.collector._smart_policy_aliases
        demand, epoch = record['demand'], record['epoch']
        original_snapshot = self.collector.snapshot
        old_identity = self.collector._smart_context('sda')
        swapped = []
        def replace_between_public_snapshot_and_private_projection():
            old = original_snapshot()
            self.assertEqual(old['disks']['items'][0]['smart_temperature_c'], 44)
            self.assertEqual(self.folder.resolve().parent.parent, self.sys.resolve())
            self.folder.rename(self.temporary / 'detached-old-sda')
            self.folder = self.block('sda', '8:0', 'temporary-wwid')
            (self.folder / 'wwid').unlink()
            self.assertNotEqual(self.collector._smart_context('sda'), old_identity)
            self.collector._publish_integration('disks', self.collector._disks)
            self.behavior = (0, self.good_response(60))
            result = self.collector._smart_batch(False, demand.authorized, missing_only=True,
                policy_demand=demand, collector_epoch=epoch)
            self.assertEqual(result['results'][0]['status'], 'refreshed')
            self.assertEqual(original_snapshot()['disks']['items'][0]['smart_temperature_c'], 60)
            swapped.append(True)
            return old
        with mock.patch.object(self.collector, 'snapshot', side_effect=replace_between_public_snapshot_and_private_projection):
            rows = self.controller.sources.snapshot()
        self.assertEqual(swapped, [True])
        selected = [row for row in rows if row['id'] == self.disk_id]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['status'], 'ok')
        self.assertEqual(selected[0]['temperature_c'], 60)

    def test_alias_projection_cannot_join_old_sdname_temperature_to_new_generation(self):
        self.assert_replacement_alias_projection()

    def test_natural_owner_serial_row_also_uses_private_current_generation_measurement(self):
        self.assert_replacement_alias_projection(owner_identity=True)

    def test_hidden_129th_duplicate_serial_blocks_cold_alias_without_smart(self):
        serial = self.serial_alias_fixture()
        for index in range(128):
            name = 'sdx' + chr(97 + index // 26) + chr(97 + index % 26)
            self.block(name, '8:' + str(index + 1), 'identity-' + str(index))
        self.udev[str(self.collector.run_root / 'udev/data/b8:128')] = 'E:ID_SERIAL_SHORT=' + serial
        self.publish_disks()
        self.assertEqual(len(self.collector._state['disks']['items']), 128)
        self.assertNotIn('sdxex', [row['id'] for row in self.collector._state['disks']['items']])
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.collector._smart_measurements, {})
        projection = self.collector.policy_temperature_aliases()
        self.assertEqual(projection['by_device'], {})
        self.assertEqual(projection['items'], [])
        self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')

    def test_omitted_current_kernel_disk_blocks_alias_uniqueness(self):
        self.serial_alias_fixture()
        self.block('sdb', '8:16', 'other-identity')
        self.publish_disks()
        self.collector._state['disks']['items'] = [row for row in self.collector._state['disks']['items']
                                                   if row['id'] != 'sdb']
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.collector.policy_temperature_aliases()['by_device'], {})

    def test_truncated_inventory_does_not_change_existing_direct_wwid_policy(self):
        for index in range(128):
            name = 'sdx' + chr(97 + index // 26) + chr(97 + index % 26)
            self.block(name, '8:' + str(index + 1), 'identity-' + str(index))
        self.publish_disks()
        self.apply_policy()
        self.policy_tick()
        self.assertEqual([call[0][-1] for call in self.calls], ['/dev/sda'])

    def apply_policy(self, mode='temperature', sources=None):
        settings = copy.deepcopy(fan_policy.DEFAULT_CONFIG)
        settings['channels']['cpu'].update(mode=mode, sources=sources or [self.disk_id])
        with mock.patch.object(self.controller, '_save'):
            result = self.controller.action('fan.policy.update',
                {'config': settings, 'apply': True, 'confirmed': True})
            self.assertTrue(result['ok'], result)
            self.controller.worker.join(timeout=2)
            self.assertFalse(self.controller.worker.is_alive())
        self.assertTrue(self.controller.channels['cpu']['active'])

    def finish_worker(self):
        worker = self.collector._smart_policy_worker
        if worker is not None:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def policy_tick(self):
        self.controller.tick()
        self.finish_worker()

    def test_fill_only_missing_ordinary_and_cached_hit_does_not_renew_timestamp(self):
        first = self.fill()
        self.assertEqual(first['results'][0]['status'], 'refreshed')
        self.assertEqual(self.calls[0][0][1:], ['-n', 'standby', '-j', '-a', '/dev/sda'])
        sampled = self.row()['smart_sampled_monotonic']
        self.now += 29.999
        result = self.fill()
        self.assertEqual(result['results'][0]['status'], 'cached')
        self.assertEqual(self.row()['smart_sampled_monotonic'], sampled)
        self.assertEqual(len(self.calls), 1)
        self.now += .001
        self.fill()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(first['force'])
        self.assertEqual(self.collector._smart_cache, {})
        self.assertNotIn('smart_data', self.collector.smart_cached('sda'))
        self.assertIsNone(self.collector.smart_cached('sda').get('smart_identity'))

    def test_fill_negative_cache_same_lifetime_expires_without_extending(self):
        self.behavior = (1, {'smartctl': {'messages': [{'string': 'unsupported'}]}})
        first = self.fill()['results'][0]
        self.now += 29
        self.assertEqual(self.fill()['results'][0], first)
        self.assertEqual(len(self.calls), 1)
        self.now += 1
        self.fill()
        self.assertEqual(len(self.calls), 2)

    def test_negative_cache_cannot_survive_replacement(self):
        self.behavior = (1, {})
        self.fill()
        self.write(self.folder / 'dev', '8:16')
        self.write(self.folder / 'wwid', 'new-disk')
        self.fill()
        self.assertEqual(len(self.calls), 2)

    def test_standby_fill_is_reused_and_cannot_authorize_capacity(self):
        self.behavior = (2, {'power_mode': {'string': 'STANDBY'}})
        self.assertEqual(self.fill()['results'][0]['status'], 'skipped_standby')
        self.assertEqual(self.fill()['results'][0]['status'], 'skipped_standby')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'standby')
        self.now += 30
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')

    def test_capacity_eligibility_requires_ordinary_current_lifetime_success(self):
        # Success alone is not power proof: this positive fixture supplies the
        # official name/value pair while force and lifecycle checks stay exact.
        self.behavior = (0, dict(self.good_response(), power_mode={'name': 'ACTIVE or IDLE', 'ata_value': 255}))
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')
        self.refresh(force=True)
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')
        self.refresh()
        self.assertIsNone(self.collector.capacity_fill_eligibility('sda'))
        self.now += 30
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')
        self.refresh()
        self.write(self.folder / 'dev', '8:16')
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')

    def test_fill_revocation_discards_success_and_negative_cache(self):
        def revoke(argv, timeout):
            self.active = False
            return 0, self.good_response()
        self.behavior = revoke
        from insights import SmartRefreshError
        with self.assertRaisesRegex(SmartRefreshError, 'authorization_denied'):
            self.fill()
        self.assertEqual(self.collector._smart_cache, {})
        self.assertEqual(self.collector._smart_attempts, {})

    def test_fill_shares_manual_lock(self):
        from insights import SmartRefreshError
        self.collector._smart_refresh_lock.acquire()
        try:
            with self.assertRaisesRegex(SmartRefreshError, 'busy'):
                self.fill()
        finally:
            self.collector._smart_refresh_lock.release()
        self.assertEqual(self.calls, [])

    def test_front_fill_unknown_power_has_bounded_negative_reuse_and_no_force(self):
        self.behavior = (0, {'power_mode': {'string': 'UNKNOWN'}, **self.good_response()})
        first = self.fill()['results'][0]
        self.assertEqual((first['status'], first['reason']), ('unavailable', 'power_mode_unverified'))
        self.assertEqual(self.fill()['results'][0], first)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('-n', self.calls[0][0])
        self.assertEqual(self.collector._smart_measurements, {})
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')

    def test_saved_draft_inactive_manual_and_cpu_only_never_probe_disks(self):
        self.controller.config['channels']['cpu'].update(mode='temperature', sources=[self.disk_id])
        self.policy_tick()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.collector._smart_policy_worker)
        self.apply_policy(mode='manual')
        self.policy_tick()
        self.assertEqual(self.calls, [])
        cpu_id = next(row['id'] for row in self.controller.sources.snapshot() if row['kind'] == 'cpu')
        self.apply_policy(sources=[cpu_id])
        self.policy_tick()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.collector._smart_policy_worker)

    def test_applied_temperature_demand_reads_only_selected_disk_without_raw_or_serial(self):
        self.block('sdb', '8:16', 'unselected')
        self.publish_disks()
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][-1], '/dev/sda')
        self.assertEqual(self.collector._smart_cache, {})
        self.assertEqual(set(self.collector._smart_measurements), {'sda'})
        cached = self.collector.smart_cached('sda')
        self.assertNotIn('smart_data', cached)
        self.assertIsNone(cached.get('smart_identity'))
        self.assertEqual(cached['smart_temperature_c'], 37)
        for _ in range(3):
            self.now += 1
            self.policy_tick()
        self.assertEqual(len(self.calls), 1)
        self.now += 27
        self.policy_tick()
        self.assertEqual(len(self.calls), 2)

    def test_expired_measurement_fails_safe_until_async_result_without_blocking_tick(self):
        entered, release = threading.Event(), threading.Event()
        def wait(argv, timeout):
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return 0, self.good_response()
        self.behavior = wait
        self.apply_policy()
        started = time.monotonic()
        try:
            self.controller.tick()
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(entered.wait(timeout=1))
            self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')
        finally:
            release.set()
            self.finish_worker()
        self.policy_tick()
        self.assertEqual(self.controller.channels['cpu']['level'], 'quiet')

    def test_auto_never_overwrites_manual_detail_or_authorizes_serial_from_auto(self):
        self.refresh()
        manual = copy.deepcopy(self.collector._smart_cache)
        self.now += 30
        self.behavior = (0, self.good_response(44))
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.collector._smart_cache, manual)
        value = self.collector.smart_cached('sda')
        self.assertEqual(value['smart_temperature_c'], 44)
        self.assertNotIn('smart_data', value)
        self.assertEqual(value.get('smart_identity'), manual['sda'][1]['smart_identity'])

    def test_native_inactive_or_expired_ack_never_grants_demand(self):
        self.apply_policy()
        self.adapter.channels.clear()
        self.controller._request_disk_temperatures()
        self.assertIsNone(self.collector._smart_policy_worker)
        self.adapter.channels.add('cpu')
        self.now += 5.001
        self.controller._request_disk_temperatures()
        self.assertIsNone(self.collector._smart_policy_worker)

    def test_native_control_lock_contention_does_not_block_demand_check(self):
        self.apply_policy()
        self.adapter.lock = threading.Lock()
        self.adapter.lock.acquire()
        try:
            began = time.monotonic()
            self.controller._request_disk_temperatures()
            self.assertLess(time.monotonic() - began, .1)
            self.assertIsNone(self.collector._smart_policy_worker)
        finally:
            self.adapter.lock.release()

    def test_optional_smart_worker_start_failure_keeps_fan_failsafe_active(self):
        self.apply_policy()
        with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError('fixture unavailable')):
            self.controller.tick()
        self.assertTrue(self.controller.channels['cpu']['active'])
        self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')
        self.assertIsNone(self.collector._smart_policy_worker)
        self.assertEqual(self.calls, [])

    def test_policy_change_during_command_discards_all_results_and_cannot_revive_token(self):
        tokens = []
        original = self.collector.request_policy_smart
        def schedule(demand):
            tokens.append(demand)
            return original(demand)
        def change(argv, timeout):
            with self.controller.lock:
                self.controller._smart_policy_epoch += 1
                self.controller.active_config['channels']['cpu']['sources'] = ['disk:wwn:' + 'f' * 24]
            return 0, self.good_response()
        self.behavior = change
        self.apply_policy()
        with mock.patch.object(self.collector, 'request_policy_smart', side_effect=schedule):
            self.policy_tick()
        self.assertEqual(self.collector._smart_measurements, {})
        self.assertEqual(self.collector._smart_attempts, {})
        self.controller.active_config = copy.deepcopy(tokens[0].settings)
        self.assertFalse(tokens[0].authorized())

    def test_replacement_during_auto_command_drops_entire_device_result(self):
        def replace(argv, timeout):
            self.write(self.folder / 'dev', '8:16')
            self.write(self.folder / 'wwid', 'replacement')
            return 0, self.good_response()
        self.behavior = replace
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.collector._smart_measurements, {})
        self.assertEqual(self.collector._smart_attempts, {})

    def test_final_publication_policy_revocation_discards_measurements_and_attempts(self):
        returned, after = [False], [0]
        original = self.collector._smart_context
        def response(argv, timeout):
            returned[0] = True
            return 0, self.good_response()
        def context(name):
            identity = original(name)
            if returned[0]:
                after[0] += 1
                if after[0] == 2:  # publication lifetime recheck, after row capture
                    with self.controller.lock:
                        self.controller._smart_policy_epoch += 1
            return identity
        self.behavior = response
        self.apply_policy()
        with mock.patch.object(self.collector, '_smart_context', side_effect=context):
            self.policy_tick()
        self.assertGreaterEqual(after[0], 2)
        self.assertEqual(self.collector._smart_measurements, {})
        self.assertEqual(self.collector._smart_attempts, {})

    def test_auto_failure_negative_reuse_keeps_full_fan_protection(self):
        self.behavior = (1, {})
        self.apply_policy()
        self.policy_tick()
        self.now += 1
        self.policy_tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')
        self.assertEqual(self.controller.channels['cpu']['target_percent'], 100)

    def test_draft_sources_do_not_change_applied_demand(self):
        self.block('sdb', '8:16', 'draft-disk')
        self.publish_disks()
        self.apply_policy()
        self.controller.config['channels']['cpu']['sources'] = [
            'disk:' + self.collector._disk_identity('sdb', self.sys / 'block/sdb')]
        self.policy_tick()
        self.assertEqual([call[0][-1] for call in self.calls], ['/dev/sda'])

    def test_auto_timeout_and_stop_restart_discard_results(self):
        def expire(argv, timeout):
            self.now += 31
            return 0, self.good_response()
        self.behavior = expire
        self.apply_policy()
        self.policy_tick()
        self.assertEqual(self.collector._smart_measurements, {})
        self.assertEqual(self.collector._smart_attempts, {})
        def restart(argv, timeout):
            self.collector.stop()
            self.collector._stop.clear()
            return 0, self.good_response()
        self.behavior = restart
        self.controller.channels['cpu']['applied_monotonic'] = self.now
        self.policy_tick()
        self.assertEqual(self.collector._smart_measurements, {})

    def test_actual_source_reapply_during_command_revokes_old_and_reads_new_disk_only(self):
        self.block('sdb', '8:16', 'new-selected')
        self.publish_disks()
        new_source = 'disk:' + self.collector._disk_identity('sdb', self.sys / 'block/sdb')
        entered, release = threading.Event(), threading.Event()
        def wait_first(argv, timeout):
            if argv[-1] == '/dev/sda':
                entered.set()
                self.assertTrue(release.wait(timeout=2))
            return 0, self.good_response()
        self.behavior = wait_first
        self.apply_policy()
        try:
            self.controller.tick()
            self.assertTrue(entered.wait(timeout=1))
            self.apply_policy(sources=[new_source])
        finally:
            release.set()
            self.finish_worker()
        self.assertEqual(self.collector._smart_measurements, {})
        self.policy_tick()
        self.assertEqual([call[0][-1] for call in self.calls], ['/dev/sda', '/dev/sdb'])
        self.assertEqual(set(self.collector._smart_measurements), {'sdb'})

    def test_freshness_near_32bit_uptime_still_expires_at_thirty_seconds(self):
        self.now = float(2 ** 32 + 100)
        self.behavior = (0, dict(self.good_response(), power_mode={'name': 'ACTIVE or IDLE', 'ata_value': 255}))
        self.fill()
        self.now += 29.5
        self.assertIsNone(self.collector.capacity_fill_eligibility('sda'))
        self.assertEqual(self.fill()['results'][0]['status'], 'cached')
        self.now += .5
        self.assertEqual(self.collector.capacity_fill_eligibility('sda'), 'power_state_unverified')
        self.fill()
        self.assertEqual(len(self.calls), 2)

    def test_duplicate_stable_disk_source_never_authorizes_either_physical_disk(self):
        self.block('sdb', '8:16', 'disk-a')  # same WWID as sda, distinct real fixture lifetime
        self.publish_disks()
        selected = [row for row in self.controller.sources.snapshot() if row['id'] == self.disk_id]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['status'], 'unavailable')
        self.apply_policy()
        self.assertTrue(self.controller.channels['cpu']['active'])
        self.policy_tick()
        self.assertEqual(self.controller.channels['cpu']['level'], 'failsafe')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.collector._smart_measurements, {})
        # A failed lifecycle on the second ambiguous row is not permission to
        # silently select the first. Inventory must establish a unique source.
        second = next(row for row in self.collector._state['disks']['items'] if row['id'] == 'sdb')
        second['_lifecycle'] = ('invalid-generation',)
        self.policy_tick()
        self.assertEqual(self.calls, [])

    def test_policy_worker_queued_before_stop_cannot_rebind_to_restarted_collector(self):
        queued = []
        class DeferredWorker:
            def __init__(worker, *, target, args, **kwargs):
                worker.target, worker.args, worker.alive = target, args, False
                queued.append(worker)
            def start(worker):
                worker.alive = True
            def is_alive(worker):
                return worker.alive
            def join(worker, timeout=None):
                pass
            def run(worker):
                try:
                    worker.target(*worker.args)
                finally:
                    worker.alive = False
        self.apply_policy()
        scheduled_epoch = self.collector._smart_epoch
        with mock.patch.object(threading, 'Thread', DeferredWorker):
            self.controller._request_disk_temperatures()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].args[1], scheduled_epoch)
        self.assertEqual(self.calls, [])
        self.collector.stop()
        self.collector.start()  # actual restart, before the deferred worker enters its target
        self.assertGreater(self.collector._smart_epoch, scheduled_epoch)
        with mock.patch.object(self.collector, '_smart_batch', wraps=self.collector._smart_batch) as batch:
            queued[0].run()
        batch.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.collector._smart_measurements, {})
        self.assertEqual(self.collector._smart_attempts, {})


if __name__ == '__main__':
    unittest.main()

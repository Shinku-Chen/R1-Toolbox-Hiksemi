"""Audited smartctl 7.0-7.4 text-in-JSON through real cache/capacity projection.

Only the command output, kernel proof and capacity worker are fixture seams.
No smartctl, hardware, network, Linux mount or physical-sleep claim is made.
The source prints ``is:    ACTIVE or IDLE`` and ``was:   IDLE[_A/B/C]``;
there is no invented power_mode object in the legacy positive fixtures.
"""
import copy
import json
import unittest
from unittest import mock

import test_smart_power_compat as power_fixture


MISSING = object()
RAW_MARKER = "SMART-OUTPUT-PRIVATE-LINE-SENTINEL"


class SmartLegacyOutputTests(unittest.TestCase):
    # Reuse fixture mechanics without inheriting/re-running its test methods.
    setUp = power_fixture.SmartPowerCompatTests.setUp
    write = power_fixture.SmartPowerCompatTests.write
    block = power_fixture.SmartPowerCompatTests.block
    good_response = staticmethod(power_fixture.SmartPowerCompatTests.good_response)
    install = power_fixture.SmartPowerCompatTests.install
    runner = power_fixture.SmartPowerCompatTests.runner
    capacity_device = power_fixture.SmartPowerCompatTests.capacity_device
    capacity_probe = power_fixture.SmartPowerCompatTests.capacity_probe
    project = power_fixture.SmartPowerCompatTests.project
    refresh_visible = power_fixture.SmartPowerCompatTests.refresh_visible
    seed_capacity = power_fixture.SmartPowerCompatTests.seed_capacity
    assert_closed_handles = power_fixture.SmartPowerCompatTests.assert_closed_handles

    def legacy(self, output=MISSING, *, code=0, version=(7, 4), protocol="ATA",
               power=MISSING, pre_release=False, temperature=37, messages=None):
        data = self.good_response(temperature)
        data["device"] = {"protocol": protocol}
        data["smartctl"] = {"version": list(version), "pre_release": pre_release}
        if output is not MISSING:
            data["smartctl"]["output"] = copy.deepcopy(output)
        if messages is not None:
            data["smartctl"]["messages"] = [{"string": text} for text in messages]
        if power is not MISSING:
            data["power_mode"] = copy.deepcopy(power)
        return code, data

    def active_data(self, **kwargs):
        return self.legacy(["Power mode is:    ACTIVE or IDLE", RAW_MARKER], **kwargs)

    def mix(self):
        self.responses = {
            "sda": self.legacy(["Device is in STANDBY mode, exit(2)", RAW_MARKER], code=2),
            "sdb": self.active_data(temperature=34),
            "sdc": self.legacy(["Power mode was:   IDLE", RAW_MARKER], temperature=35),
            "sdd": self.legacy(["Power mode was:   IDLE_A", RAW_MARKER], temperature=36)}

    def round(self, value):
        # New same-lifetime batch; short negative reuse cannot hide input.
        self.now += 31
        self.wall += 31
        self.responses = {name: self.active_data() for name in self.mounts}
        self.responses["sda"] = value
        self.capacity_calls.clear()
        smart, capacity, eligibility, overview = self.refresh_visible()
        self.assertEqual(set(self.capacity_calls) - {"sda"}, {"sdb", "sdc", "sdd"})
        return (next(row for row in smart["results"] if row["device"] == "sda"),
                next(row for row in capacity["results"] if row["mount_id"] == 11),
                eligibility, next(row for row in overview["disks_detail"] if row["name"] == "sda"))

    def no_raw_output(self, *values):
        values = [*values, self.collector._smart_cache, self.collector._smart_measurements,
                  self.collector._smart_attempts, self.collector._smart_visible_observations,
                  self.collector._disk_serial_cache, self.collector.snapshot(),
                  self.collector.smart_cached("sda"), self.panel.smart("sda", include_identity=True)]
        rendered = json.dumps(values)
        self.assertNotIn(RAW_MARKER, rendered)
        self.assertNotIn('"output"', rendered)

    def assert_no_capacity(self, value):
        smart, capacity, eligible, row = self.round(value)
        self.assertEqual(eligible("sda"), "power_state_unverified")
        self.assertNotIn("sda", self.capacity_calls)
        self.assertEqual((capacity["status"], capacity["reason"]),
                         ("unavailable", "power_state_unverified"))
        self.assertIsNone(row["percent"])
        self.assertIsNot(row["standby"], False)
        self.no_raw_output(smart, capacity, row)

    def test_original_output_standby_and_active_disks_reach_real_overview_independently(self):
        self.mix()
        smart, capacity, eligible, overview = self.refresh_visible()
        statuses = {row["device"]: row for row in smart["results"]}
        self.assertEqual(statuses["sda"]["status"], "skipped_standby")
        self.assertEqual(statuses["mmcblk0"]["reason"], "smart_unsupported")
        self.assertEqual(eligible("sda"), "standby")
        self.assertEqual(eligible("mmcblk0"), "power_state_unverified")
        self.assertEqual(self.capacity_calls, ["sdb", "sdc", "sdd"])
        rows = {row["name"]: row for row in overview["disks_detail"]}
        self.assertIsNone(rows["sda"]["percent"])
        for name in ("sdb", "sdc", "sdd"):
            self.assertEqual(rows[name]["percent"], 25.0)
            self.assertEqual(rows[name]["health"], "PASS")
            self.assertIs(rows[name]["standby"], False)
        self.assertEqual((overview["disk_size"], overview["disk_used"], overview["disk_total"]),
                         (9000, 2250, 25.0))
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(argv[1:] == ["-n", "standby", "--json=o", "-a", argv[-1]]
                            for argv, _ in self.calls))
        self.no_raw_output(smart, capacity, overview)
        self.assertFalse(self.collector._smart_cache)
        self.assertFalse(self.collector._disk_serial_cache)
        self.assert_closed_handles()

    def test_original_idle_variants_and_active_or_idle_authorize_current_batch(self):
        lines = ["Power mode is:    ACTIVE or IDLE"]
        lines += ["Power mode was:   " + state for state in ("IDLE", "IDLE_A", "IDLE_B", "IDLE_C")]
        for line in lines:
            with self.subTest(line=line):
                smart, capacity, eligible, row = self.round(self.legacy([line, RAW_MARKER]))
                self.assertEqual(smart["status"], "refreshed")
                self.assertIsNone(eligible("sda"))
                self.assertEqual(capacity["status"], "refreshed")
                self.assertEqual((row["temp"], row["health"], row["percent"]), (37, "PASS", 25.0))
                self.assertIs(row["standby"], False)
                self.no_raw_output(smart, capacity, row)

    def audited_version(self, minor):
        for line in ("Power mode is:    ACTIVE or IDLE", "Power mode was:   IDLE"):
            with self.subTest(version=(7, minor), line=line):
                smart, capacity, eligible, row = self.round(self.legacy([line, RAW_MARKER], version=(7, minor)))
                self.assertEqual(smart["status"], "refreshed")
                self.assertIsNone(eligible("sda"))
                self.assertEqual(capacity["status"], "refreshed")
                self.assertIs(row["standby"], False)
                self.assertEqual(row["percent"], 25.0)
                self.no_raw_output(smart, capacity, row)

    def test_audited_smartctl_7_0_original_power_output(self):
        self.audited_version(0)

    def test_audited_smartctl_7_1_original_power_output(self):
        self.audited_version(1)

    def test_audited_smartctl_7_2_original_power_output(self):
        self.audited_version(2)

    def test_audited_smartctl_7_3_original_power_output(self):
        self.audited_version(3)

    def test_audited_smartctl_7_4_original_power_output(self):
        self.audited_version(4)

    def test_original_early_sleep_standby_and_standby_y_skip_capacity(self):
        for state in ("SLEEP", "STANDBY", "STANDBY_Y"):
            with self.subTest(state=state):
                _, capacity, eligible, row = self.round(self.legacy(
                    ["Device is in " + state + " mode, exit(2)", RAW_MARKER], code=2))
                self.assertEqual(eligible("sda"), "standby")
                self.assertEqual((capacity["status"], capacity["reason"]), ("skipped_standby", "standby"))
                self.assertNotIn("sda", self.capacity_calls)
                self.assertIs(row["standby"], True)

    def test_standby_keeps_original_capacity_date_while_other_disks_refresh(self):
        old = self.seed_capacity()
        self.mix()
        _, capacity, _, overview = self.refresh_visible()
        rows = {row["name"]: row for row in overview["disks_detail"]}
        self.assertEqual((rows["sda"]["percent"], rows["sda"]["capacity_sampled_at"]),
                         (25.0, old[11]["capacity_sampled_at"]))
        self.assertEqual(capacity["results"][0]["sampled_at"], old[11]["capacity_sampled_at"])
        for name in ("sdb", "sdc", "sdd"):
            self.assertEqual(rows[name]["percent"], 50.0)
            self.assertGreater(rows[name]["capacity_sampled_at"], old[self.mounts[name][0]]["capacity_sampled_at"])
        self.assertEqual(overview["disk_total"], 47.5)

    def test_unknown_legacy_power_preserves_history_without_new_capacity_date(self):
        old = self.seed_capacity()
        smart, capacity, eligible, row = self.round(self.legacy(["Power mode is:    UNKNOWN", RAW_MARKER]))
        self.assertEqual(eligible("sda"), "power_state_unverified")
        self.assertNotIn("sda", self.capacity_calls)
        self.assertEqual((row["total"], row["used"], row["percent"], row["capacity_sampled_at"]),
                         (1000, 250, 25.0, old[11]["capacity_sampled_at"]))
        self.assertEqual((capacity["reason"], capacity["sampled_at"]),
                         ("power_state_unverified", old[11]["capacity_sampled_at"]))
        self.assertIsNot(row["standby"], False)
        self.no_raw_output(smart, capacity, row)

    def test_missing_output_and_unknown_line_do_not_authorize_from_health_or_temperature(self):
        for output in (MISSING, [], ["Power mode is:    UNKNOWN"], [RAW_MARKER],
                       ["Power mode is:    ACTIVE"], ["Power mode was:   ACTIVE_NV_DOWN"],
                       ["Power mode was:   ACTIVE_NV_UP"]):
            with self.subTest(output=output):
                self.assert_no_capacity(self.legacy(output))

    def test_exact_version_protocol_and_prerelease_scope_is_enforced(self):
        for version, protocol, pre_release in (((7, 2, 1), "ATA", False), ((6, 6), "ATA", False),
                ((8, 0), "ATA", False), ((7, 5), "ATA", False), ((7, 99), "ATA", False), ((7, 4), "SCSI", False),
                ((7, 4), "NVMe", False), ((7, 4), "ata", False), ((7, 4), None, False),
                ((7, 4), "ATA", True), ((7, 4), "ATA", "false"),
                ((True, 4), "ATA", False), ((7.0, 4), "ATA", False), ((7, "4"), "ATA", False)):
            with self.subTest(version=version, protocol=protocol, pre_release=pre_release):
                self.assert_no_capacity(self.active_data(version=version, protocol=protocol, pre_release=pre_release))

    def test_missing_version_or_device_protocol_is_not_audited_text_proof(self):
        for missing in ("smartctl.version", "device", "device.protocol"):
            with self.subTest(missing=missing):
                code, data = self.active_data()
                if missing == "smartctl.version":
                    data["smartctl"].pop("version")
                elif missing == "device":
                    data.pop("device")
                else:
                    data["device"].pop("protocol")
                self.assert_no_capacity((code, data))

    def test_output_container_lines_and_bounds_cannot_smuggle_power_proof(self):
        cases = [None, True, "Power mode is:    ACTIVE or IDLE", {}, [None], [True],
                 [{"string": "Power mode is:    ACTIVE or IDLE"}],
                 ["Power mode is:    ACTIVE or IDLE\n"],
                 ["Power mode is:    ACTIVE or IDLE\r"],
                 ["Power mode is:    ACTIVE or IDLE\0"],
                 ["Power mode is:    ACTIVE or IDLE", "control\x1bcode"],
                 ["Power mode is:    ACTIVE or IDLE", "x" * 1024],
                 ["Power mode is:    ACTIVE or IDLE"] + ["x"] * 1024,
                 ["Power mode is:    ACTIVE or IDLE"] + ["x" * 1023] * 129]
        for output in cases:
            with self.subTest(kind=type(output).__name__, count=len(output) if isinstance(output, list) else None):
                self.assert_no_capacity(self.legacy(output))

    def test_only_exact_audited_print_forms_and_state_association_are_proof(self):
        for line in ("Power mode is:   ACTIVE or IDLE", "Power mode was:    IDLE",
                     "Power mode was:   ACTIVE or IDLE", "Power mode is:    IDLE",
                     " Power mode is:    ACTIVE or IDLE", "Power mode is:    active or idle",
                     "Power mode is:    ACTIVE or IDLE suffix", "Power mode was:   IDLE mode"):
            with self.subTest(line=line):
                self.assert_no_capacity(self.legacy([line, RAW_MARKER]))

    def test_multiple_power_lines_even_identical_do_not_choose_a_favorable_one(self):
        for other in ("Power mode is:    ACTIVE or IDLE", "Power mode was:   IDLE",
                      "Device is in STANDBY mode, exit(2)"):
            with self.subTest(other=other):
                self.assert_no_capacity(self.legacy(["Power mode is:    ACTIVE or IDLE", other, RAW_MARKER]))

    def test_ignored_n_or_unknown_warning_in_raw_output_vetoes_active_proof(self):
        for version, warning in (((7, 0), "CHECK POWER MODE not implemented, ignoring -n option"),
                ((7, 2), "CHECK POWER MODE returned unknown value 0x02, ignoring -n option"),
                ((7, 4), "Device power state unknown; ignoring -n")):
            with self.subTest(version=version, warning=warning):
                # 7.0-7.2 use pout, so there is deliberately no messages[]
                # duplicate to save a parser that examines only that field.
                self.assert_no_capacity(self.legacy(["Power mode is:    ACTIVE or IDLE", warning, RAW_MARKER],
                                                   version=version))

    def test_structured_and_legacy_conflict_never_selects_favorable_state(self):
        for power in ({"name": "STANDBY", "ata_value": 0}, {"name": "IDLE", "ata_value": 128},
                      {"name": "ACTIVE or IDLE", "ata_value": 0}, {"name": "UNKNOWN"},
                      {"name": "ACTIVE or IDLE", "string": "STANDBY"},
                      {"name": "future_mode"}, {"string": ""}, {"name": "ACTIVE"}):
            with self.subTest(power=power):
                self.assert_no_capacity(self.active_data(power=power))

    def test_matching_structured_evidence_and_absent_output_keep_original_contract(self):
        for output in (MISSING, ["Power mode is:    ACTIVE or IDLE", RAW_MARKER]):
            with self.subTest(output=output):
                _, capacity, eligible, row = self.round(self.legacy(output,
                    power={"name": "ACTIVE or IDLE", "ata_value": 255}))
                self.assertIsNone(eligible("sda"))
                self.assertEqual(capacity["status"], "refreshed")
                self.assertIs(row["standby"], False)
                self.no_raw_output(row)

    def test_empty_structured_mapping_allows_audited_legacy_fallback(self):
        _, capacity, eligible, row = self.round(self.active_data(power={}))
        self.assertIsNone(eligible("sda"))
        self.assertEqual(capacity["status"], "refreshed")
        self.assertIs(row["standby"], False)

    def test_legacy_version_scope_does_not_remove_valid_formal_power_authority(self):
        for version, protocol in (((8, 0), "ATA"), ((7, 5), "ATA"), ((7, 4), "NVMe")):
            for output in (MISSING, ["Power mode is:    ACTIVE or IDLE", RAW_MARKER]):
                with self.subTest(version=version, protocol=protocol, output=output):
                    _, capacity, eligible, row = self.round(self.legacy(output, version=version,
                        protocol=protocol, power={"name": "ACTIVE or IDLE", "ata_value": 255}))
                    self.assertIsNone(eligible("sda"))
                    self.assertEqual(capacity["status"], "refreshed")
                    self.assertIs(row["standby"], False)
                    self.no_raw_output(row)

    def test_malformed_output_cannot_hide_behind_valid_structured_evidence(self):
        for output in (None, ["Power mode is:    ACTIVE or IDLE", None],
                       ["Power mode is:    UNKNOWN", RAW_MARKER]):
            with self.subTest(output=output):
                self.assert_no_capacity(self.legacy(output,
                    power={"name": "ACTIVE or IDLE", "ata_value": 255}))

    def test_owner_full_cache_keeps_authorized_identity_but_never_full_output(self):
        self.responses = {name: self.active_data() for name in self.mounts}
        receipt = self.collector.refresh_smart(force=False, authorizer=lambda: self.active_owner())
        cached = self.collector.smart_cached("sda")
        self.assertIsNotNone(cached["smart_identity"])
        self.assertTrue(self.collector._smart_cache)
        self.no_raw_output(receipt, self.project())
        self.assertTrue(all(argv[1:] == ["-n", "standby", "--json=o", "-a", argv[-1]]
                            for argv, _ in self.calls))

    def active_owner(self):
        return self.active

    def test_force_collects_no_ordinary_capacity_authority_or_output_cache(self):
        self.responses = {name: self.active_data() for name in self.mounts}
        result = self.collector.refresh_smart(force=True, authorizer=lambda: self.active_owner())
        self.assertTrue(all(argv[1:] == ["--json=o", "-a", argv[-1]] for argv, _ in self.calls))
        self.assertEqual(self.collector.capacity_fill_eligibility("sda"), "power_state_unverified")
        self.monitor.refresh_capacity_missing(authorizer=lambda: self.active_owner(),
            eligibility=self.collector.capacity_fill_eligibility)
        self.assertEqual(self.capacity_calls, [])
        self.no_raw_output(result)

    def test_error_output_is_removed_before_private_result_or_cache_publication(self):
        self.responses = {"sda": self.legacy(["Power mode is:    ACTIVE or IDLE", RAW_MARKER], code=1)}
        value, status, _ = self.collector._probe_smart("sda", require_power_state=True)
        self.assertNotEqual(status, "refreshed")
        self.no_raw_output(value)

    def test_private_output_is_removed_before_first_production_deepcopy(self):
        # Build deliberate raw fixture inputs before intercepting the shared
        # copy module, so only actual collector copies are checked below.
        cases = [
            ("normal", self.active_data(), False, False),
            ("force", self.active_data(), True, False),
            ("measurement_only", self.active_data(), False, True),
            ("command_error", self.active_data(code=1), False, False),
            ("malformed_output", self.legacy({"private": RAW_MARKER}), False, False)]
        actual_module = power_fixture.smart_fixture.insights
        original_deepcopy = actual_module.copy.deepcopy
        for case, response, force, measurement_only in cases:
            with self.subTest(case=case):
                self.responses = {"sda": response}
                copied_smart_documents = []

                def checked_deepcopy(value, *args, **kwargs):
                    if isinstance(value, dict) and isinstance(value.get("smartctl"), dict):
                        self.assertNotIn("output", value["smartctl"])
                        self.assertNotIn(RAW_MARKER, json.dumps(value))
                        copied_smart_documents.append(True)
                    return original_deepcopy(value, *args, **kwargs)

                with mock.patch.object(actual_module.copy, "deepcopy", new=checked_deepcopy):
                    value, _, _ = self.collector._probe_smart(
                        "sda", force=force, measurement_only=measurement_only,
                        require_power_state=not force)
                self.assertEqual(bool(copied_smart_documents), not measurement_only)
                self.no_raw_output(value)

    def test_copied_receipt_epoch_and_replacement_do_not_reuse_text_proof(self):
        self.mix()
        receipt = self.collector.refresh_smart_visible(authorizer=lambda: self.active_owner())
        copied = self.collector.capacity_visible_eligibility(copy.deepcopy(receipt))
        self.monitor.refresh_capacity_visible(authorizer=lambda: self.active_owner(), eligibility=copied)
        self.assertEqual(self.capacity_calls, [])
        eligible = self.collector.capacity_visible_eligibility(receipt)
        self.assertIsNone(eligible("sdb"))
        self.write(self.sys / "block/sdb/wwid", "replacement\n")
        self.assertEqual(eligible("sdb"), "power_state_unverified")
        self.collector._smart_epoch += 1
        self.assertEqual(eligible("sdc"), "power_state_unverified")

    def test_device_change_during_command_drops_that_measurement_and_capacity(self):
        self.mix()
        def replacement(_argv, _timeout):
            self.write(self.sys / "block/sdb/wwid", "replacement-during-read\n")
            return self.active_data()
        self.responses["sdb"] = replacement
        smart, _, eligible, overview = self.refresh_visible()
        self.assertEqual(next(row for row in smart["results"] if row["device"] == "sdb")["status"],
                         "identity_changed")
        self.assertEqual(eligible("sdb"), "power_state_unverified")
        self.assertNotIn("sdb", self.capacity_calls)
        row = next(row for row in overview["disks_detail"] if row["name"] == "sdb")
        self.assertIsNone(row["percent"])
        self.assertIsNone(row["temp"])
        self.no_raw_output(smart, overview)

    def test_owner_revocation_discards_entire_batch_and_text_proof(self):
        self.mix()
        def revoke(_argv, _timeout):
            self.active = False
            return self.active_response
        self.active_response = self.active_data()
        self.responses["sda"] = revoke
        with self.assertRaises(power_fixture.smart_fixture.insights.SmartRefreshError) as raised:
            self.collector.refresh_smart_visible(authorizer=lambda: self.active_owner())
        self.assertEqual(raised.exception.reason, "authorization_denied")
        self.assertFalse(self.collector._smart_cache)
        self.assertFalse(self.collector._smart_measurements)
        self.assertFalse(self.collector._smart_attempts)
        self.assertEqual(self.capacity_calls, [])
        self.no_raw_output()


if __name__ == "__main__":
    unittest.main()

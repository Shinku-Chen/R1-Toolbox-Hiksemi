"""Portable lease ABI boundaries; no Linux imports, device IO or fan control."""
import importlib.util
from pathlib import Path
import struct
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fan_lease_under_test", ROOT / "runtime/fan_native.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class LeaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.now = 100_000_000_000

    def test_ten_percent_and_full_scale_use_existing_exact_32_byte_abi(self):
        raw = native.encode_lease({"cpu", "system"}, {"cpu": 26, "system": 255}, 7, self.now)
        self.assertEqual(len(raw), 32)
        self.assertEqual(struct.unpack("<8sQIIII", raw),
                         (b"R1FAN01\0", self.now + 5_000_000_000, 3, 26, 255, 7))
        self.assertEqual(native.decode_lease(raw, self.now, 3)["targets"], {"cpu": 26, "system": 255})

    def test_existing_higher_targets_still_decode_without_schema_migration(self):
        for value in (26, 27, 127, 128, 191, 255):
            with self.subTest(value=value):
                raw = native.encode_lease({"cpu"}, {"cpu": value}, 1, self.now)
                self.assertEqual(native.decode_lease(raw, self.now, 1)["targets"], {"cpu": value})

    def test_encode_rejects_below_floor_stop_overflow_bool_and_fractional_targets(self):
        for value in (25, 0, -1, 256, True, False, 26.0):
            for channel in ("cpu", "system"):
                with self.subTest(value=value, channel=channel), self.assertRaises(ValueError):
                    native.encode_lease({channel}, {channel: value}, 1, self.now)

    def test_decode_rejects_untrusted_active_channel_targets_outside_26_to_255(self):
        for value in (25, 0, 256, 0xffffffff):
            for mask, cpu, system in ((1, value, 0), (2, 0, value), (3, 26, value), (3, value, 26)):
                raw = native.LEASE.pack(native.MAGIC, self.now + native.LEASE_NS, mask, cpu, system, 1)
                with self.subTest(value=value, mask=mask), self.assertRaises(ValueError):
                    native.decode_lease(raw, self.now, mask)

    def test_inactive_zero_slot_is_not_a_stop_request_or_owned_channel(self):
        for channel, mask in (("cpu", 1), ("system", 2)):
            raw = native.encode_lease({channel}, {channel: 26}, 1, self.now)
            self.assertEqual(native.decode_lease(raw, self.now, mask)["targets"], {channel: 26})

    def test_low_target_does_not_relax_expiry_identity_sequence_or_packet_length(self):
        raw = native.encode_lease({"cpu"}, {"cpu": 26}, 1, self.now)
        zero_sequence = native.LEASE.pack(native.MAGIC, self.now + native.LEASE_NS, 1, 26, 0, 0)
        for packet, tick, mask in ((raw, self.now + native.LEASE_NS, 1), (raw, self.now, 2),
                                   (raw, self.now - 1, 1), (raw[:-1], self.now, 1),
                                   (b"BADMAGIC" + raw[8:], self.now, 1), (zero_sequence, self.now, 1)):
            with self.subTest(tick=tick, mask=mask), self.assertRaises(ValueError):
                native.decode_lease(packet, tick, mask)


if __name__ == "__main__":
    unittest.main()

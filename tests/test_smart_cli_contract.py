"""Parse real collector argv with GNU getopt, never execute smartctl.

Audited smartctl 7.0--7.4 parse_options() passes the exact SHORT_OPTIONS
below to getopt_long(). Short ``j`` takes no argument; only long ``json``
has optional_argument. Therefore ``--json=o`` passes ``o`` as optarg,
whereas ``-j=o`` is rejected for the unknown short option ``=``. A fake
SMART runner alone cannot establish this command-line compatibility.

Pinned official smartctl.cpp sources were read and SHA-256 checked during
the audit. The immutable commit/path and digest table documents provenance
without fetching source or depending on a developer's TEMP files in CI.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
SPEC = importlib.util.spec_from_file_location(
    "smart_cli_contract_insights", ROOT / "runtime/insights.py")
insights = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(insights)

# All five audited release sources contain this identical literal, the
# json/optional_argument -> 'j' long-option entry, and getopt_long() loop.
SHORT_OPTIONS = "h?Vq:d:T:b:r:s:o:S:HcAl:iaxv:P:t:CXF:n:B:f:g:j"
LONG_OPTIONS = "json::,nocheck:,all"
OFFICIAL_SOURCES = (
    ("7.0", "51e8d5e8ffb4039e0ef4b4f72323b87c7ef5217a",
     "c32f4482ba5f6bc7589fd58c37538b51c4fed4e6b5af3819f13aa59379dfc699"),
    ("7.1", "249b38c426330f5d267c8a9895d1ede591ee8519",
     "c32f4482ba5f6bc7589fd58c37538b51c4fed4e6b5af3819f13aa59379dfc699"),
    ("7.2", "59efad4c0000cf40e3441acdc8b0ee58adac7a11",
     "3dcb98f45d73cdd7f0c8a6770ef9d1bd91b13d5402abc2b3256f0dd22d3b7cec"),
    ("7.3", "ade59e8d9460e5e2b057855c6858d557dd4935ad",
     "dbe4370e6eec5e8f986a508ad29c46ae18b6cf10339fefecc7143a3742188a6e"),
    ("7.4", "0c0bada5eedb759c9d7bb50ff54959cf0809c5a9",
     "752a9b0490a9fe55d2137db135f474c17f078e50d25a330099ff482fb0c007b9"),
)
# Source URL for each row:
# https://github.com/smartmontools/smartmontools/blob/<commit>/smartmontools/smartctl.cpp


class SmartCliContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = dict(os.environ, LC_ALL="C")
        # These GNU-getopt-specific environment settings must not silently
        # select compatibility mode or change the audited parser ordering.
        cls.environment.pop("GETOPT_COMPATIBLE", None)
        cls.environment.pop("POSIXLY_CORRECT", None)
        candidates = [shutil.which("getopt")]
        if sys.platform == "win32":
            candidates.append(r"C:\Program Files\Git\usr\bin\getopt.exe")
        cls.getopt = None
        for candidate in dict.fromkeys(value for value in candidates if value):
            try:
                probe = subprocess.run(
                    [candidate, "--test"], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, timeout=3,
                    check=False, env=cls.environment)
            except (OSError, subprocess.TimeoutExpired):
                continue
            # Enhanced GNU getopt documents exactly status 4 for --test.
            if probe.returncode == 4:
                cls.getopt = candidate
                break
        if cls.getopt is None:
            raise unittest.SkipTest("enhanced GNU getopt unavailable; real CLI contract requires Linux CI")

    def parse(self, arguments):
        """Use the real parser with audited option arities and fixed argv."""
        return subprocess.run(
            [self.getopt, "--shell", "sh", "--options", SHORT_OPTIONS,
             "--longoptions", LONG_OPTIONS, "--", *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=3, check=False, env=self.environment)

    def collector_arguments(self, *, force):
        calls = []

        def capture(argv, timeout):
            # Only this command seam is fake. No executable named smartctl is
            # located or invoked; the production _run/_probe_smart form argv.
            calls.append((list(argv), timeout))
            return SimpleNamespace(returncode=0, stdout=json.dumps({}))

        collector = object.__new__(insights.InsightsCollector)
        collector.command_finder = lambda name: "/fixture/smartctl" if name == "smartctl" else None
        collector.runner = capture
        collector.clock = lambda: 100.0
        collector._probe_smart("sda", force=force, require_power_state=not force)
        self.assertEqual(len(calls), 1, "one probe must remain one command")
        self.assertEqual(calls[0][0][0], "/fixture/smartctl")
        self.assertEqual(calls[0][1], 3)
        return calls[0][0][1:]

    def test_ordinary_collector_argv_passes_original_output_optarg_to_real_parser(self):
        arguments = self.collector_arguments(force=False)
        parsed = self.parse(arguments)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertEqual(shlex.split(parsed.stdout),
                         ["-n", "standby", "--json", "o", "-a", "--", "/dev/sda"])
        self.assertEqual(arguments,
                         ["-n", "standby", "--json=o", "-a", "/dev/sda"])

    def test_force_only_omits_standby_check_and_keeps_original_output_optarg(self):
        arguments = self.collector_arguments(force=True)
        parsed = self.parse(arguments)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertEqual(shlex.split(parsed.stdout), ["--json", "o", "-a", "--", "/dev/sda"])
        self.assertEqual(arguments, ["--json=o", "-a", "/dev/sda"])

    def test_old_short_json_equals_form_is_rejected_by_real_getopt(self):
        # This negative control passes the old bug to the actual parser,
        # independently of any collector assertion or fake SMART response.
        parsed = self.parse(["-n", "standby", "-j=o", "-a", "/dev/sda"])
        self.assertNotEqual(parsed.returncode, 0)
        self.assertIn("=", parsed.stderr)
        self.assertNotIn("--json", shlex.split(parsed.stdout))

    def test_plain_short_json_has_no_optarg_in_audited_short_option_contract(self):
        parsed = self.parse(["-n", "standby", "-j", "-a", "/dev/sda"])
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertEqual(shlex.split(parsed.stdout), ["-n", "standby", "-j", "-a", "--", "/dev/sda"])


if __name__ == "__main__":
    unittest.main()

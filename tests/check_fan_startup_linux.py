#!/usr/bin/env python3
"""Exercise startup/readback races using only our own temporary Linux helper.

No vendor executable, systemd unit, socket, device or hardware ioctl is used.
The real backend's readiness, spawn, synchronization and recovery logic runs
against a private proc tree containing links to this test's own processes only.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SIGNAL_MASK = (1 << (signal.SIGUSR1 - 1)) | (1 << (signal.SIGUSR2 - 1))
HELPER = r'''
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static int acknowledge = -1;
static volatile sig_atomic_t exit_on_signal;

static void received(int number) {
    if (exit_on_signal) _exit(23);
    unsigned char value = (unsigned char)number;
    if (acknowledge >= 0 && write(acknowledge, &value, 1) != 1) _exit(24);
}

static void delay(long nanoseconds) {
    struct timespec value = {0, nanoseconds};
    while (nanosleep(&value, &value) < 0 && errno == EINTR) {}
}

int main(int argc, char **argv) {
    if (argc != 4) return 2;
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = SIG_DFL;
    if (sigaction(SIGUSR1, &action, 0) || sigaction(SIGUSR2, &action, 0)) return 3;
    acknowledge = open(argv[2], O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    if (acknowledge < 0) return 4;
    if (write(acknowledge, "S", 1) != 1) return 5;
    exit_on_signal = !strcmp(argv[3], "exit-on-sync");
    alarm(20); /* Every fixture child is bounded even if Python is interrupted. */
    delay(250000000L);
    /* The old-readiness regression keeps this private gate closed, making the
       pre-handler window deterministic even on a heavily loaded CI runner. */
    while (access(argv[1], F_OK) != 0) delay(5000000L);
    action.sa_handler = received;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGUSR1, &action, 0) || sigaction(SIGUSR2, &action, 0)) return 6;
    /* Stand in for the vendor's initial grade-2 output request, never ioctl. */
    if (write(acknowledge, "I", 1) != 1) return 7;
    for (;;) pause();
}
'''


def load_runtime():
    for name in ("fan_native", "histor", "fan_watchdog"):
        spec = importlib.util.spec_from_file_location(name, ROOT / "runtime" / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules["fan_native"], sys.modules["fan_watchdog"]


def caught(path):
    for line in path.read_text().splitlines():
        if line.startswith("SigCgt:"):
            return int(line.split(":", 1)[1].strip(), 16)
    raise AssertionError("fixture proc status did not contain SigCgt")


def main():
    if sys.platform != "linux":
        print("SKIP: fan startup readiness fixture requires Linux; no process or hardware action performed")
        return 0
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        raise RuntimeError("Linux C compiler is required for the pure startup fixture")
    folder = Path(tempfile.mkdtemp(prefix="r1-fan-startup-ci-"))
    folder.chmod(0o700)
    source, program, guard = folder / "helper.c", folder / "temp_control", folder / "guard.so"
    source.write_text(HELPER)
    flags = [compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror"]
    subprocess.run([*flags, str(source), "-o", str(program)], check=True, timeout=15)
    # This is the production interposer, without its regular-file test macro.
    # Our helper never calls ioctl, so neither this .so nor Python opens devices.
    subprocess.run([*flags, "-fPIC", "-shared", str(ROOT / "native/fan_guard.c"),
                    "-ldl", "-o", str(guard)], check=True, timeout=15)
    native, watchdog = load_runtime()
    real_popen = subprocess.Popen
    processes, checks = {}, []

    class FixtureBackend(watchdog.SystemBackend):
        def __init__(self, directory):
            # Do not resolve any real firmware/program/device path, even while
            # constructing the fixture backend. All proc aliases are ours.
            self.root, self.proc = directory, directory / "proc"
            self.proc.mkdir()
            self.program = self.program_link = program
            self.guard_path = guard
            self._signature = self._device = None
            self.children, self.writes = [], []
            self.clock, self.sleep = time.monotonic, time.sleep
            (self.proc / str(os.getpid())).symlink_to(Path("/proc") / str(os.getpid()), target_is_directory=True)

        def verify_live(self):
            pass  # Firmware/loaded-module/character-device gates are fake only here.

        def _firmware_signature(self):
            return None

        def mode(self):
            return "auto"

        def write(self, targets):
            self.writes.append(dict(targets))  # No hardware/device file is opened.

        def _histor(self, unused_folder):
            backend = self

            class FixtureRPC:
                def _has_socket(self):
                    return True

                def _config_mode(self):
                    return 5

                def _get_mode(self):
                    vendors = backend._vendors()
                    assert len(vendors) == 1, "fixture RPC cannot signal an ambiguous process"
                    identity = vendors[0]
                    child, expected, arguments = processes[identity["pid"]]
                    if child.poll() is not None or identity != expected or not backend.alive(expected):
                        raise RuntimeError("fixture RPC refuses a changed or exited process identity")
                    assert caught(backend.proc / str(child.pid) / "status") & SIGNAL_MASK == SIGNAL_MASK
                    # Match the vendor getter's auto-mode synchronization, but
                    # signal only the PID/starttime/exe from our own Popen handle.
                    os.kill(child.pid, signal.SIGUSR1)
                    if arguments[-1] == "exit-on-sync":
                        assert child.wait(timeout=2) == 23
                    else:
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            if Path(arguments[2]).read_bytes().endswith(bytes([signal.SIGUSR1])):
                                break
                            time.sleep(0.005)
                        else:
                            raise AssertionError("our fixture helper did not acknowledge its synchronization signal")
                    return 5  # Deliberately not proof that the helper survived.

            return FixtureRPC()

    backends = []

    def new_backend(name):
        directory = folder / name
        directory.mkdir(mode=0o700)
        backend = FixtureBackend(directory)
        backends.append(backend)
        return backend

    def launch_for(backend):
        def launch(arguments, **options):
            if Path(options.get("executable", arguments[0])) != program:
                raise RuntimeError("fixture refuses to execute anything except its own compiled helper")
            child = real_popen(arguments, **options)
            (backend.proc / str(child.pid)).symlink_to(Path("/proc") / str(child.pid), target_is_directory=True)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                identity = native.process_identity(child.pid, backend.proc)
                if identity is not None and identity["exe"] == str(program):
                    processes[child.pid] = (child, identity, arguments)
                    return child
                if child.poll() is not None:
                    break
                time.sleep(0.002)
            raise AssertionError("own fixture helper did not exec its expected temporary executable")
        return launch

    def original(backend, *, gate_open=True, exits=False):
        gate, acknowledgement = backend.root / "start-gate", backend.root / "acknowledgement"
        if gate_open:
            gate.write_text("ready")
        return {"identity": {"pid": 0, "starttime": 0, "exe": str(program)},
                "argv": [str(program), str(gate), str(acknowledgement), "exit-on-sync" if exits else "stay"],
                "env": {"PATH": "/usr/bin:/bin", "DISK_AUTOSLEEP": "preserved"}, "cwd": str(backend.root)}

    def stop_owned(backend, child):
        if child.poll() is not None:
            return
        identity = processes[child.pid][1]
        if not backend.alive(identity):
            raise RuntimeError("fixture cleanup refuses a changed process identity")
        os.kill(child.pid, signal.SIGTERM)
        child.wait(timeout=2)

    try:
        with patch.object(watchdog.fcntl, "ioctl", side_effect=AssertionError("startup fixture must never call ioctl")):
            # Reproduce the old exec/maps-only readiness bug deterministically.
            for guarded in (False, True):
                backend = new_backend("old-guarded" if guarded else "old-original")
                saved = original(backend, gate_open=False)
                environment = dict(saved["env"])
                if guarded:
                    environment.update(LD_PRELOAD=str(guard), R1_FAN_LEASE=str(backend.root / "lease.bin"), R1_FAN_MASK="3")
                child = launch_for(backend)(saved["argv"], executable=str(program), cwd=saved["cwd"], env=environment)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    maps = (backend.proc / str(child.pid) / "maps").read_text()
                    if Path(saved["argv"][2]).exists() and (not guarded or str(guard) in maps):
                        break
                    time.sleep(0.002)
                else:
                    raise AssertionError("old-ready fixture did not enter its intentional pre-handler window")
                assert caught(backend.proc / str(child.pid) / "status") & SIGNAL_MASK == 0
                assert backend.alive(processes[child.pid][1])
                os.kill(child.pid, signal.SIGUSR1)
                assert child.wait(timeout=2) == -signal.SIGUSR1, "exec/maps readiness failed to reproduce the fatal signal race"
            checks.append("old exec/maps-only readiness reproducibly loses original and guarded helper to unhandled USR1")

            # Exercise production spawn/wait_ready with normal and LD_PRELOAD paths.
            for guarded in (False, True):
                backend = new_backend("new-guarded" if guarded else "new-original")
                saved = original(backend)
                before = time.monotonic()
                with patch.object(watchdog.subprocess, "Popen", side_effect=launch_for(backend)):
                    identity = backend.spawn(saved, lease=backend.root / "lease.bin" if guarded else None,
                                             mask=3 if guarded else None)
                assert time.monotonic() - before >= 0.20, "production spawn returned before delayed handlers were installed"
                assert caught(backend.proc / str(identity["pid"]) / "status") & SIGNAL_MASK == SIGNAL_MASK
                assert backend._vendors() == [identity] and backend.alive(identity)
                backend.sync_mode(backend.root, expected="auto", apply_manual=False)
                assert backend.alive(identity), "handler-ready helper died during the fake vendor GET"
                stop_owned(backend, processes[identity["pid"]][0])
            checks.append("production original/guarded spawn waits for both caught signals, stable identity and synchronization survival")

            # A real helper startup and signaling GET must not add a second
            # safe-full request after the restored owner's initial request.
            # The grade file is fixture bookkeeping, never a real /tmp/fan.
            backend = new_backend("auto-restored-with-grade-cache")
            saved = original(backend)
            grade = backend.root / "vendor-grade"
            grade.write_bytes((2).to_bytes(4, "little"))
            native.atomic_json(backend.root / "descriptor.json", {
                "version": 1, "channels": ["cpu", "system"], "original": saved,
                "guarded": None, "old_mode": "auto", "daemon_changed": True})
            recovery = watchdog.FanWatchdog(backend.root, backend)
            with patch.object(watchdog.subprocess, "Popen", side_effect=launch_for(backend)):
                assert recovery.restore("release_requested") is True
            status = native.read_json(backend.root / "status.json")
            assert status["phase"] == "released" and status["restore_ok"] is True
            assert backend.writes == [{"cpu": 255, "system": 255}], "auto sync overwrote the restored owner's output"
            assert grade.read_bytes() == (2).to_bytes(4, "little"), "recovery must not force a vendor grade change"
            identity = backend._vendors()[0]
            acknowledgement = Path(processes[identity["pid"]][2][2]).read_bytes()
            assert b"I" in acknowledgement and acknowledgement.endswith(bytes([signal.SIGUSR1]))
            stop_owned(backend, processes[identity["pid"]][0])
            checks.append("successful auto recovery keeps pre-handoff safe-full without a post-startup output override")

            # Simulate the vendor GET's precise false-positive: return 5 even
            # though its signal just killed the process. Recovery must fail and
            # retain the private original descriptor for an explicit retry.
            backend = new_backend("exits-during-sync")
            saved = original(backend, exits=True)
            native.atomic_json(backend.root / "descriptor.json", {
                "version": 1, "channels": ["cpu", "system"], "original": saved,
                "guarded": None, "old_mode": "auto", "daemon_changed": True})
            recovery = watchdog.FanWatchdog(backend.root, backend)
            with patch.object(watchdog.subprocess, "Popen", side_effect=launch_for(backend)):
                assert recovery.restore("lease_or_identity_failed") is False
            status = native.read_json(backend.root / "status.json")
            assert status["phase"] == "recovery_failed" and status["restore_ok"] is False
            assert status["recovery_errors"], "helper death was incorrectly accepted as successful recovery"
            assert native.read_json(backend.root / "descriptor.json").get("original") == saved
            assert backend._vendors() == [], "exit-on-sync helper unexpectedly survived the fixture getter"
            checks.append("GET mode 5 after helper exit cannot produce restore_ok; recovery descriptor remains available")
    finally:
        for backend in backends:
            for child in list(processes.values()):
                handle, identity, _ = child
                if handle.poll() is None and native.process_identity(handle.pid, backend.proc) == identity:
                    stop_owned(backend, handle)
    for check in checks:
        print("PASS:", check)
    print("Startup fixture artifacts retained until CI runner disposal:", folder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

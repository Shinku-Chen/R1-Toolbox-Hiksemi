#!/usr/bin/env python3
"""Compile our interposer against a fake ioctl provider, never a device driver.

The fixture-only macro allows regular files. The production build has no
environment switch that relaxes /dev/hikio identity or character-device checks.
Run directly on Linux CI; no vendor binaries, mounts, privileges or IO required.
"""
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time


def main():
    if sys.platform != "linux":
        print("SKIP: fan guard ABI fixture requires Linux; no hardware action performed")
        return 0
    compiler = shutil.which("cc") or shutil.which("gcc")
    if not compiler:
        raise RuntimeError("Linux C compiler is required for the pure fixture")
    root = Path(__file__).resolve().parents[1]
    folder = Path(tempfile.mkdtemp(prefix="r1-fan-guard-abi-"))
    stub = folder / "stub.c"
    stub.write_text(r'''
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
int ioctl(int fd, unsigned long request, ...) {
    (void)fd;
    va_list args; va_start(args, request);
    unsigned long argument = va_arg(args, unsigned long); va_end(args);
    printf("%lu %lu %d\n", request, argument, errno);
    errno = 11; return 37;
}
''')
    program = folder / "program.c"
    program.write_text(r'''
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--child")) {
        if (getenv("LD_PRELOAD") || getenv("R1_FAN_MASK") || getenv("R1_FAN_LEASE")) return 4;
        const char *value = getenv("DISK_AUTOSLEEP");
        if (!value || strcmp(value, "preserved")) return 5;
        FILE *maps = fopen("/proc/self/maps", "r"); if (!maps) return 6;
        char row[4096];
        while (fgets(row, sizeof(row), maps))
            if (strstr(row, "guard-fixture.so") || strstr(row, "/guard.so")) return 7;
        fclose(maps); puts("child-clean"); return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "--spawn-child")) {
        pid_t child = fork(); if (child < 0) return 8;
        if (!child) { execl(argv[0], argv[0], "--child", (char *)NULL); return 9; }
        int status = 0; if (waitpid(child, &status, 0) != child) return 10;
        return WIFEXITED(status) ? WEXITSTATUS(status) : 11;
    }
    if (argc != 4) return 2;
    int fd = open(argv[1], O_RDONLY); if (fd < 0) return 3;
    unsigned long request = strtoul(argv[2], NULL, 0), argument = strtoul(argv[3], NULL, 0);
    errno = 7;
    int result = ioctl(fd, request, argument);
    printf("%d %d\n", result, errno); close(fd); return 0;
}
''')
    common = [compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror"]
    subprocess.run(common + ["-fPIC", "-shared", str(stub), "-o", str(folder / "libstub.so")], check=True)
    subprocess.run(common + [str(program), "-L" + str(folder), "-lstub", "-Wl,-rpath," + str(folder),
                              "-o", str(folder / "program")], check=True)
    for name, flags in (("guard.so", []), ("guard-fixture.so", ["-DR1_FAN_GUARD_FIXTURE"])):
        subprocess.run(common + ["-fPIC", "-shared", *flags, str(root / "native/fan_guard.c"),
                                  "-ldl", "-o", str(folder / name)], check=True)
    device, other = folder / "fake-hikio", folder / "other-file"
    device.write_bytes(b"not-a-device")
    other.write_bytes(b"not-a-device")
    lease = folder / "lease.bin"
    environment = dict(os.environ, R1_FAN_MASK="1", R1_FAN_LEASE=str(lease), R1_FIXTURE_DEVICE=str(device),
                       LD_PRELOAD=str(folder / "guard-fixture.so"))

    def renew(*, mask=1, cpu=188, system=0, delta=4_000_000_000):
        lease.write_bytes(struct.pack("<8sQIIII", b"R1FAN01\0", time.monotonic_ns() + delta, mask, cpu, system, 1))
        lease.chmod(0o600)

    def call(path, request, argument, expected, env=None):
        result = subprocess.run([str(folder / "program"), str(path), str(request), str(argument)],
                                env=env or environment, capture_output=True, text=True, timeout=3, check=True)
        rows = result.stdout.strip().splitlines()
        assert rows == [f"{request} {expected} 7", "37 11"], result.stdout

    renew()
    call(device, 0x5798, 99, 188)
    call(device, 0x5799, 211, 211)  # CPU-only never touches system requests.
    call(other, 0x5798, 99, 99)
    call(device, 0x5796, 0x12345678ABCD, 0x12345678ABCD)  # Pointer-sized unrelated ABI stays intact.
    renew(delta=-1)
    call(device, 0x5798, 99, 255)
    renew(mask=3, system=222)
    call(device, 0x5798, 99, 255)  # Wrong lease ownership is never accepted.
    renew(cpu=127)
    call(device, 0x5798, 99, 255)
    renew()
    lease.chmod(0o644)
    call(device, 0x5798, 99, 255)
    renew(mask=3, cpu=180, system=230)
    both = dict(environment, R1_FAN_MASK="3")
    call(device, 0x5798, 99, 180, both)
    call(device, 0x5799, 99, 230, both)
    # Even with the fixture environment present, the production binary rejects
    # a regular file. Nothing can opt into the test profile at runtime.
    production = dict(environment, LD_PRELOAD=str(folder / "guard.so"))
    call(device, 0x5798, 99, 99, production)
    inherited = subprocess.run([str(folder / "program"), "--spawn-child"],
                              env=dict(environment, DISK_AUTOSLEEP="preserved"),
                              capture_output=True, text=True, timeout=3, check=True)
    assert inherited.stdout.strip() == "child-clean", inherited.stdout
    print("PASS: native guard ABI, scope, lease, fail-high, pointer/errno transparency; fixture:", folder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read bounded local device facts without invoking commands or probing hardware.

This module reads only documented proc/sys attributes and allowlisted release
metadata.  It does not infer a fan's physical role from channel numbers.  Callers
should refresh this comparatively static inventory at most every 30 seconds;
there is no process-global cache that could mix injected fixture roots.
"""

import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat


_ITEM_LIMIT = 256
_SCAN_LIMIT = 4096
_TEXT_LIMIT = 4096
_DMI_FIELDS = (
    "sys_vendor", "product_name", "product_version", "product_serial",
    "board_vendor", "board_name", "board_version", "board_serial",
    "bios_vendor", "bios_version", "bios_date", "chassis_vendor", "chassis_type",
)
_OS_FIELDS = {"NAME", "PRETTY_NAME", "VERSION", "VERSION_ID", "ID", "BUILD_ID"}
_FIRMWARE_FILES = ("firmware_version", "hiksemi-release", "histor-release", "version")
_FIRMWARE_JSON_FILES = ("histor/bin/temp/sim_version.json", "histor/basis/temp/sim_version_R1.json")
_FIRMWARE_JSON_FIELDS = ("version", "build", "build_date", "build_time", "model", "product_name")


class _Reader:
    def __init__(self, roots):
        self.roots = tuple(Path(root).resolve() for root in roots)

    def allowed(self, path):
        try:
            resolved = Path(path).resolve()
            return any(resolved == root or root in resolved.parents for root in self.roots)
        except (OSError, RuntimeError, ValueError):
            return False

    def text(self, path, limit=_TEXT_LIMIT):
        if not self.allowed(path):
            return None
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = os.read(fd, limit + 1)
            if len(raw) > limit:
                return None
            text = raw.decode("utf-8", "replace").replace("\x00", "").strip()
            return "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
        except (OSError, ValueError, OverflowError):
            return None
        finally:
            if fd is not None:
                os.close(fd)

    def names(self, path, pattern, limit=_ITEM_LIMIT):
        if not self.allowed(path):
            return []
        result = []
        try:
            with os.scandir(path) as entries:
                for index, entry in enumerate(entries):
                    if index >= _SCAN_LIMIT:
                        break
                    if re.fullmatch(pattern, entry.name) and self.allowed(Path(path) / entry.name):
                        result.append(entry.name)
                        if len(result) >= limit:
                            break
        except (OSError, ValueError):
            pass
        return sorted(result)

    def number(self, path, minimum=0, maximum=None, scale=1):
        raw = self.text(path, 128)
        try:
            value = int(raw) if scale == 1 else int(raw) / scale
            if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
                return None
            return int(value) if scale == 1 else value
        except (ValueError, TypeError, OverflowError):
            return None


def _key_values(text):
    values = {}
    for line in (text or "").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in _OS_FIELDS:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value[:256]
    return values


def _firmware_metadata_paths(etc):
    """Allow two exact metadata files, without exposing the histor directory."""
    base = etc.parent.resolve()
    paths = []
    try:
        histor = (base / "histor").resolve()
        if histor != base and base not in histor.parents:
            return ()
        for name in _FIRMWARE_JSON_FILES:
            path = base / name
            resolved = path.resolve()
            if not path.is_symlink() and histor in resolved.parents:
                paths.append(path)
    except (OSError, RuntimeError, ValueError):
        return ()
    return tuple(paths)


def _official_firmware(reader, etc):
    values = {}
    for path in _firmware_metadata_paths(etc):
        try:
            data = json.loads(reader.text(path, 16384) or "null")
        except (ValueError, TypeError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        for name in _FIRMWARE_JSON_FIELDS:
            value = data.get(name)
            # Explicitly exclude opaque versionID and all unknown/nested data.
            # This is version/model metadata, never an arbitrary JSON export.
            if type(value) is int and 0 <= value <= 2**63 - 1:
                value = str(value)
            if not isinstance(value, str) or not 1 <= len(value) <= 128:
                continue
            if not re.fullmatch(r"[\w .:/+()-]+", value):
                continue
            if name == "version" and not re.fullmatch(r"[vV]?[0-9][\w .:+()-]*", value):
                continue
            values.setdefault(name, value)
    return values


def _identity(reader, proc, sys, etc):
    release = _key_values(reader.text(etc / "os-release", 16384)
                          or reader.text(etc.parent / "usr/lib/os-release", 16384))
    dmi = {name: value for name in _DMI_FIELDS
           if (value := reader.text(sys / "class/dmi/id" / name, 512))}
    firmware = {}
    for name in _FIRMWARE_FILES:
        value = reader.text(etc / name, 1024)
        # Release files are single version lines, not configuration dumps.
        if value and "\n" not in value and len(value) <= 256 and re.fullmatch(r"[\w .:/+()@-]+", value):
            firmware[name] = value
    firmware.update(_official_firmware(reader, etc))
    return {
        "hostname": reader.text(proc / "sys/kernel/hostname", 256) or reader.text(etc / "hostname", 256),
        "kernel": reader.text(proc / "sys/kernel/osrelease", 256),
        "os": release, "firmware": firmware, "dmi": dmi,
        "pci": _pci(reader, sys, etc),
    }


def _pci_names(reader, etc):
    """Use an existing local pci.ids only; never download a vendor database."""
    text = None
    for candidate in (etc / "pci.ids", etc.parent / "usr/share/misc/pci.ids",
                      etc.parent / "usr/share/hwdata/pci.ids"):
        text = reader.text(candidate, 4 * 1024 * 1024)
        if text:
            break
    vendors, devices = {}, {}
    current = None
    for line in (text or "").splitlines()[:120000]:
        match = re.fullmatch(r"([0-9a-fA-F]{4})\s+(.{1,200})", line)
        device = re.fullmatch(r"\t([0-9a-fA-F]{4})\s+(.{1,200})", line)
        if match:
            current = match[1].lower()
            vendors[current] = match[2].strip()
        elif device and current:
            devices[(current, device[1].lower())] = device[2].strip()
    return vendors, devices


def _pci(reader, sys, etc):
    vendors, devices = _pci_names(reader, etc)
    result = []
    base = sys / "bus/pci/devices"
    for name in reader.names(base, r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]"):
        root = base / name
        row = {"address": name}
        for field in ("vendor", "device", "class", "subsystem_vendor", "subsystem_device"):
            value = reader.text(root / field, 32)
            row[field] = value.lower() if value and re.fullmatch(r"0x[0-9a-fA-F]{4,8}", value) else None
        vendor, device = row["vendor"], row["device"]
        row["vendor_name"] = vendors.get(vendor[2:]) if vendor else None
        row["device_name"] = devices.get((vendor[2:], device[2:])) if vendor and device else None
        try:
            target = (root / "driver").resolve(strict=True)
            row["driver"] = target.name if reader.allowed(target) else None
        except (OSError, ValueError, RuntimeError):
            row["driver"] = None
        result.append(row)
    return result


def _cpu(reader, proc, sys):
    blocks, block = [], {}
    for line in (reader.text(proc / "cpuinfo", 1024 * 1024) or "").splitlines():
        if not line.strip():
            if block:
                blocks.append(block)
            block = {}
        else:
            key, separator, value = line.partition(":")
            if separator:
                block[key.strip()] = value.strip()[:256]
    if block:
        blocks.append(block)
    blocks = blocks[:_ITEM_LIMIT]
    topology, packages, frequencies = set(), set(), []
    cpus = reader.names(sys / "devices/system/cpu", r"cpu\d+")
    for name in cpus:
        root = sys / "devices/system/cpu" / name
        core = reader.number(root / "topology/core_id", maximum=1048576)
        package = reader.number(root / "topology/physical_package_id", maximum=1048576)
        if package is not None:
            packages.add(package)
        if core is not None and package is not None:
            topology.add((package, core))
        current = reader.number(root / "cpufreq/scaling_cur_freq", maximum=100000, scale=1000)
        if current is None:
            current = reader.number(root / "cpufreq/cpuinfo_cur_freq", maximum=100000, scale=1000)
        frequencies.append({
            "id": int(name[3:]), "current": current,
            "minimum": reader.number(root / "cpufreq/cpuinfo_min_freq", maximum=100000, scale=1000),
            "maximum": reader.number(root / "cpufreq/cpuinfo_max_freq", maximum=100000, scale=1000),
        })
    if not topology:
        for item in blocks:
            package, core = item.get("physical id"), item.get("core id")
            if package and core and package.isdecimal() and core.isdecimal():
                topology.add((int(package), int(core)))
                packages.add(int(package))
    if not cpus:
        for index, item in enumerate(blocks):
            try:
                mhz = float(item.get("cpu MHz", "nan"))
            except ValueError:
                mhz = math.nan
            frequencies.append({"id": index, "current": mhz if math.isfinite(mhz) and 0 <= mhz <= 100000 else None,
                                "minimum": None, "maximum": None})
    first = blocks[0] if blocks else {}
    architecture = reader.text(proc / "sys/kernel/arch", 64)
    if not architecture:
        flags = set(first.get("flags", "").split())
        if first.get("vendor_id") in ("GenuineIntel", "AuthenticAMD"):
            architecture = "x86_64" if "lm" in flags else None
        elif "aarch64" in first.get("Processor", "").lower():
            architecture = "aarch64"
        elif first.get("CPU architecture") == "8":
            architecture = "ARMv8"
    return {
        "model": first.get("model name") or first.get("Processor") or first.get("Hardware"),
        "vendor": first.get("vendor_id") or first.get("CPU implementer"),
        "logical_cores": len(cpus) or len(blocks) or None,
        "physical_cores": len(topology) or None, "sockets": len(packages) or None,
        "architecture": architecture,
        "frequencies_mhz": sorted(frequencies, key=lambda item: item["id"]),
    }


def _sensors(reader, sys):
    thermal, fans = [], []
    base = sys / "class/hwmon"
    for name in reader.names(base, r"hwmon\d+", 64):
        root = base / name
        driver = reader.text(root / "name", 128) or name
        for attribute in reader.names(root, r"temp\d+_input", 64):
            channel = attribute[:-6]
            thermal.append({
                "id": f"{name}:{channel}", "driver": driver, "channel": channel,
                "label": reader.text(root / (channel + "_label"), 128) or channel,
                "celsius": reader.number(root / attribute, -100, 250, 1000),
                "max_celsius": reader.number(root / (channel + "_max"), -100, 250, 1000),
                "critical_celsius": reader.number(root / (channel + "_crit"), -100, 250, 1000),
                "source": "hwmon",
            })
            if len(thermal) >= _ITEM_LIMIT:
                break
        for attribute in reader.names(root, r"fan\d+_input", 64):
            channel = attribute[:-6]
            number = channel[3:]
            fans.append({
                "id": f"{name}:{channel}", "driver": driver, "channel": channel,
                "label": reader.text(root / (channel + "_label"), 128) or channel,
                "rpm": reader.number(root / attribute, maximum=1000000),
                "pwm": reader.number(root / ("pwm" + number), maximum=255),
                "pwm_enable": reader.number(root / ("pwm" + number + "_enable"), maximum=255),
                "physical_role": None, "role_verified": False, "source": "hwmon",
            })
            if len(fans) >= _ITEM_LIMIT:
                break
        if len(thermal) >= _ITEM_LIMIT and len(fans) >= _ITEM_LIMIT:
            break
    for name in reader.names(sys / "class/thermal", r"thermal_zone\d+", 64):
        if len(thermal) >= _ITEM_LIMIT:
            break
        root = sys / "class/thermal" / name
        thermal.append({"id": name, "driver": "thermal", "channel": name,
                        "label": reader.text(root / "type", 128) or name,
                        "celsius": reader.number(root / "temp", -100, 250, 1000),
                        "max_celsius": None, "critical_celsius": None, "source": "thermal"})
    return thermal[:_ITEM_LIMIT], fans[:_ITEM_LIMIT]


def _backlights(reader, sys):
    items = []
    base = sys / "class/backlight"
    for name in reader.names(base, r"[A-Za-z0-9_.:-]+", 16):
        root = base / name
        value = reader.number(root / "brightness", maximum=2**32)
        maximum = reader.number(root / "max_brightness", minimum=1, maximum=2**32)
        items.append({
            "id": name, "brightness": value,
            "actual_brightness": reader.number(root / "actual_brightness", maximum=2**32),
            "max_brightness": maximum, "power": reader.number(root / "bl_power", maximum=255),
            "percent": round(value * 100 / maximum, 1) if value is not None and maximum and value <= maximum else None,
            "sleep_state": "unknown",  # A raw zero is not proof that this panel is off.
        })
    return items


def _storage(reader, proc, sys):
    devices, raid = [], []
    base = sys / "block"
    for name in reader.names(base, r"(?:sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+|md\d+|dm-\d+)"):
        root = base / name
        sectors = reader.number(root / "size", maximum=2**64)
        devices.append({
            "name": name, "model": reader.text(root / "device/model", 256),
            "vendor": reader.text(root / "device/vendor", 128),
            "serial": reader.text(root / "device/serial", 256),
            "sectors": sectors, "bytes": sectors * 512 if sectors is not None else None,
            "logical_block_size": reader.number(root / "queue/logical_block_size", maximum=1048576),
            "removable": reader.number(root / "removable", maximum=1),
            "rotational": reader.number(root / "queue/rotational", maximum=1),
            "state": reader.text(root / "device/state", 64),
            "read_only": reader.number(root / "ro", maximum=1),
        })
        if name.startswith("md"):
            md = root / "md"
            raid.append({"name": name, "level": reader.text(md / "level", 64),
                         "state": reader.text(md / "array_state", 64),
                         "devices": reader.number(md / "raid_disks", maximum=4096),
                         "degraded": reader.number(md / "degraded", maximum=4096),
                         "sync_action": reader.text(md / "sync_action", 64),
                         "sync_completed": reader.text(md / "sync_completed", 128),
                         "members": reader.names(md, r"dev-[A-Za-z0-9_.:-]+", 64)})
    mdstat = reader.text(proc / "mdstat", 16384)
    return {"devices": devices, "raid": raid, "mdstat": mdstat}


def _network(reader, proc, sys):
    addresses = {}
    for line in (reader.text(proc / "net/if_inet6", 65536) or "").splitlines()[:1024]:
        fields = line.split()
        if len(fields) != 6 or not re.fullmatch(r"[0-9a-fA-F]{32}", fields[0]):
            continue
        try:
            prefix = int(fields[2], 16)
            if not 0 <= prefix <= 128:
                continue
            address = str(ipaddress.IPv6Address(int(fields[0], 16))) + "/" + str(prefix)
            addresses.setdefault(fields[5], []).append(address)
        except ValueError:
            continue
    items = []
    base = sys / "class/net"
    for name in reader.names(base, r"[A-Za-z0-9_.:@-]{1,64}", 128):
        root = base / name
        mac = reader.text(root / "address", 64)
        items.append({
            "name": name, "mac": mac if mac and re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac) else None,
            "state": reader.text(root / "operstate", 64),
            "carrier": reader.number(root / "carrier", maximum=1),
            "speed_mbps": reader.number(root / "speed", minimum=1, maximum=10000000),
            "duplex": reader.text(root / "duplex", 64),
            "mtu": reader.number(root / "mtu", minimum=1, maximum=2**24),
            "ipv6": addresses.get(name, [])[:16],
        })
    return items


def collect_device_info(proc_root="/proc", sys_root="/sys", etc_root="/etc", run_root="/run"):
    """Return JSON-safe local facts; missing values remain null and lists bounded.

    Serial numbers and MAC addresses are for local display only.  No network,
    shell, hardware writes, automatic driver loading, or socket calls occur.
    ``run_root`` is reserved for future allowlisted runtime metadata; arbitrary
    files from it are deliberately not enumerated or read.
    """
    proc, sys, etc, run = map(Path, (proc_root, sys_root, etc_root, run_root))
    reader = _Reader((proc, sys, etc, run, etc.parent / "usr/share/misc", etc.parent / "usr/share/hwdata",
                      etc.parent / "usr/lib/os-release", *_firmware_metadata_paths(etc)))
    thermal, fans = _sensors(reader, sys)
    backlight = _backlights(reader, sys)
    storage = _storage(reader, proc, sys)
    network = _network(reader, proc, sys)
    return {
        "identity": _identity(reader, proc, sys, etc), "cpu": _cpu(reader, proc, sys),
        "thermal": thermal, "fans": fans, "backlight": backlight,
        "storage": storage, "network": network,
        "capabilities": {
            "thermal": any(item["celsius"] is not None for item in thermal),
            "fan_rpm": any(item["rpm"] is not None for item in fans),
            "fan_pwm": any(item["pwm"] is not None for item in fans),
            "fan_roles_verified": False,
            "backlight": any(item["max_brightness"] for item in backlight),
            "mdraid": bool(storage["raid"]), "pci": bool(reader.names(sys / "bus/pci/devices", r"[0-9a-fA-F:.]+", 1)),
        },
    }

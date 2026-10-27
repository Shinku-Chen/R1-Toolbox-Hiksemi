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


_INTERFACE_PATTERN = r"[A-Za-z0-9_.:@-]{1,64}"
_DNS_LIMIT = 16


def _parse_status(valid, invalid):
    return "partial" if valid and invalid else "malformed" if invalid else "ok"


def _default_routes(reader, proc, version):
    """Parse kernel default routes; unreadable is different from an empty table."""
    filename = "route" if version == 4 else "ipv6_route"
    text = reader.text(proc / "net" / filename, 262144)
    if text is None:
        return [], "unavailable"
    lines = text.splitlines()
    valid = invalid = 0
    if version == 4:
        if not lines or lines[0].split()[:3] != ["Iface", "Destination", "Gateway"]:
            return [], "malformed"
        lines = lines[1:]
    routes, limited = [], len(lines) > _SCAN_LIMIT
    for line in lines[:4096]:
        if not line.strip():
            continue
        fields = line.split()
        try:
            if version == 4:
                if len(fields) < 8 or not re.fullmatch(_INTERFACE_PATTERN, fields[0]):
                    raise ValueError()
                if not all(re.fullmatch(r"[0-9a-fA-F]{8}", fields[index]) for index in (1, 2, 7)):
                    raise ValueError()
                if not re.fullmatch(r"[0-9a-fA-F]{1,8}", fields[3]) or not re.fullmatch(r"[0-9]{1,10}", fields[6]):
                    raise ValueError()
                flags, metric = int(fields[3], 16), int(fields[6])
                if metric > 2**32 - 1:
                    raise ValueError()
                interface = fields[0]
                default = int(fields[1], 16) == 0 and int(fields[7], 16) == 0
                gateway = str(ipaddress.IPv4Address(int(fields[2], 16).to_bytes(4, "little")))
                source_prefix = None
            else:
                if len(fields) != 10 or not re.fullmatch(_INTERFACE_PATTERN, fields[9]):
                    raise ValueError()
                if not all(re.fullmatch(r"[0-9a-fA-F]{32}", fields[index]) for index in (0, 2, 4)):
                    raise ValueError()
                if not all(re.fullmatch(r"[0-9a-fA-F]{1,8}", fields[index]) for index in (1, 3, 5, 6, 7, 8)):
                    raise ValueError()
                prefix, source_length = int(fields[1], 16), int(fields[3], 16)
                if prefix > 128 or source_length > 128:
                    raise ValueError()
                interface, metric, flags = fields[9], int(fields[5], 16), int(fields[8], 16)
                default = int(fields[0], 16) == 0 and prefix == 0
                gateway = str(ipaddress.IPv6Address(int(fields[4], 16)))
                source_prefix = str(ipaddress.IPv6Address(int(fields[2], 16))) + "/" + str(source_length)
            valid += 1
            # UP routes only. A reject default is not an operational gateway.
            if not default or not flags & 1 or flags & 0x200:
                continue
            row = {"interface": interface, "gateway": None if gateway in ("0.0.0.0", "::") else gateway,
                   "metric": metric, "source": "/proc/net/" + filename}
            if source_prefix is not None:
                row["source_prefix"] = source_prefix
            if row not in routes and len(routes) < _ITEM_LIMIT:
                routes.append(row)
            elif row not in routes:
                limited = True
        except (ValueError, OverflowError):
            invalid += 1
    status = "partial" if limited else _parse_status(valid, invalid)
    return sorted(routes, key=lambda row: (row["metric"], row["interface"], row["gateway"] or "")), status


def _dns_address(value):
    address, separator, zone = value.partition("%")
    try:
        parsed = ipaddress.ip_address(address)
        if separator and (parsed.version != 6 or not re.fullmatch(_INTERFACE_PATTERN, zone)):
            return None
        return str(parsed) + ("%" + zone if separator else "")
    except ValueError:
        return None


def _dns_domain(value):
    if value == ".":
        return value
    try:
        domain = value.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    if len(domain) > 253:
        return None
    labels = domain.rstrip(".").split(".")
    return domain if all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels) else None


def _unique(values, limit=_DNS_LIMIT):
    return list(dict.fromkeys(values))[:limit]


def _resolv_data(text):
    servers, search, domains = [], [], []
    valid = invalid = 0
    lines = text.splitlines()
    limited = len(lines) > 1024
    for line in lines[:1024]:
        fields = re.split(r"[;#]", line, maxsplit=1)[0].split()
        if not fields:
            continue
        keyword, values = fields[0], fields[1:]
        if keyword == "nameserver":
            value = _dns_address(values[0]) if len(values) == 1 else None
            if value is None:
                invalid += 1
            else:
                valid += 1
                if value in servers:
                    continue
                if len(servers) < _DNS_LIMIT:
                    servers.append(value)
                else:
                    limited = True
        elif keyword in ("search", "domain"):
            parsed = [_dns_domain(value) for value in values[:_DNS_LIMIT]]
            if not values or any(value is None for value in parsed) or keyword == "domain" and len(values) != 1:
                invalid += 1
                continue
            valid += 1
            limited = limited or len(values) > _DNS_LIMIT
            if keyword == "search":
                search = parsed
            else:
                domains = parsed
        # All other configuration, comments and credentials are discarded.
    return {"servers": _unique(servers), "search": _unique(search), "domains": _unique(domains),
            "status": "partial" if limited else _parse_status(valid, invalid)}


def _dns(reader, etc, run):
    """Read fixed resolver state files only, never NetworkManager profiles."""
    etc_base, run_base = etc.resolve(), run.resolve()
    files = (
        ("resolv.conf", "/etc/resolv.conf", etc_base / "resolv.conf"),
        ("systemd-resolved", "/run/systemd/resolve/resolv.conf", run_base / "systemd/resolve/resolv.conf"),
        ("systemd-stub", "/run/systemd/resolve/stub-resolv.conf", run_base / "systemd/resolve/stub-resolv.conf"),
        ("NetworkManager", "/run/NetworkManager/resolv.conf", run_base / "NetworkManager/resolv.conf"),
        ("NetworkManager-upstream", "/run/NetworkManager/no-stub-resolv.conf", run_base / "NetworkManager/no-stub-resolv.conf"),
    )
    allowed = {path for _, _, path in files}
    sources = []
    resolved_primary = None
    for name, display, path in files:
        entry = {"source": name, "path": display, "servers": [], "search": [], "domains": [],
                 "status": "unavailable", "reason": "文件不存在、不可读或超出读取限制"}
        try:
            resolved = path.resolve()
            # A resolv.conf symlink may target the known stub/upstream files,
            # but never arbitrary /etc or /run files, even inside fixture roots.
            if resolved not in allowed:
                entry["reason"] = "符号链接目标不在已知 DNS 状态文件范围"
            else:
                text = reader.text(resolved, 65536)
                if name == "resolv.conf":
                    resolved_primary = resolved
                if text is not None:
                    entry.update(_resolv_data(text))
                    entry["reason"] = "部分 DNS 字段格式无效或超出采集限制" if entry["status"] in ("partial", "malformed") else None
        except (OSError, RuntimeError, ValueError):
            entry["reason"] = "DNS 状态路径无法确认"
        sources.append(entry)
    primary = sources[0]
    selected = primary if primary["status"] != "unavailable" else next(
        (item for item in sources[1:] if item["servers"]), primary)
    servers = selected["servers"]
    stub = [value for value in servers if ipaddress.ip_address(value.split("%", 1)[0]).is_loopback]
    upstream = [value for value in servers if value not in stub]
    # Associate runtime upstreams with the selected resolver, rather than merge
    # stale configuration from several unrelated resolver services.
    additional_source = None
    if any(value.split("%", 1)[0] in ("127.0.0.53", "127.0.0.54") for value in stub) or resolved_primary == files[2][2]:
        additional_source = sources[1]
    elif resolved_primary == files[3][2]:
        additional_source = sources[4]
    additional = additional_source["servers"] if additional_source else []
    upstream = _unique(upstream + [value for value in additional
                                  if not ipaddress.ip_address(value.split("%", 1)[0]).is_loopback])
    status, reason = selected["status"], selected["reason"]
    if selected is not primary and servers:
        status, reason = "partial", "主 resolv.conf 不可读，以下是运行时报告的 DNS，未确认系统实际选择"
    elif not servers:
        status = "unavailable" if all(item["status"] == "unavailable" for item in sources) else "malformed" if selected["status"] == "malformed" else "partial"
        reason = "未获得可确认的 DNS 服务器；不能据此判断系统未配置 DNS"
    elif stub and not upstream:
        status, reason = "partial", "已读取本地 DNS 代理，上游服务器尚无法确认"
    elif additional_source and additional_source["status"] in ("partial", "malformed") and status == "ok":
        status, reason = "partial", "已读取本地 DNS 代理，关联上游 DNS 状态仅部分可确认"
    return {"servers": servers, "upstream_servers": upstream, "stub_servers": stub,
            "search": selected["search"], "domains": selected["domains"], "sources": sources,
            "status": status, "reason": reason}


def _network(reader, proc, sys, default_routes, route_status):
    addresses = {}
    ipv6_text = reader.text(proc / "net/if_inet6", 65536)
    valid = invalid = 0
    lines = (ipv6_text or "").splitlines()
    limited = len(lines) > 1024
    for line in lines[:1024]:
        if not line.strip():
            continue
        fields = line.split()
        if (len(fields) != 6 or not re.fullmatch(r"[0-9a-fA-F]{32}", fields[0])
                or not all(re.fullmatch(r"[0-9a-fA-F]{1,8}", value) for value in fields[1:5])
                or not re.fullmatch(_INTERFACE_PATTERN, fields[5])):
            invalid += 1
            continue
        try:
            index, prefix, scope, flags = [int(value, 16) for value in fields[1:5]]
            if not 0 <= prefix <= 128 or not 0 <= scope <= 255 or not 0 <= flags <= 2**32 - 1 or not 0 <= index <= 2**32 - 1:
                raise ValueError()
            address = {"address": str(ipaddress.IPv6Address(int(fields[0], 16))), "prefix_length": prefix,
                       "scope": {0: "global", 0x10: "host", 0x20: "link", 0x40: "site", 0x80: "compat"}.get(scope, "unknown"),
                       "scope_id": scope, "flags": flags}
            current = addresses.setdefault(fields[5], [])
            if address not in current and len(current) < 16:
                current.append(address)
            elif address not in current:
                limited = True
            valid += 1
        except ValueError:
            invalid += 1
            continue
    address_status = "unavailable" if ipv6_text is None else "partial" if limited else _parse_status(valid, invalid)
    items = []
    base = sys / "class/net"
    for name in reader.names(base, _INTERFACE_PATTERN, 128):
        root = base / name
        mac = reader.text(root / "address", 64)
        items.append({
            "name": name, "mac": mac if mac and re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac) else None,
            "state": reader.text(root / "operstate", 64),
            "carrier": reader.number(root / "carrier", maximum=1),
            "speed_mbps": reader.number(root / "speed", minimum=1, maximum=10000000),
            "duplex": reader.text(root / "duplex", 64),
            "mtu": reader.number(root / "mtu", minimum=1, maximum=2**24),
            "ipv6": [item["address"] + "/" + str(item["prefix_length"]) for item in addresses.get(name, [])],
            "ipv6_addresses": addresses.get(name, []), "address_status": {"ipv6": address_status},
            "default_gateways": {family: [dict(item) for item in rows if item["interface"] == name]
                                 for family, rows in default_routes.items()},
            "route_status": dict(route_status),
        })
    return items


def collect_device_info(proc_root="/proc", sys_root="/sys", etc_root="/etc", run_root="/run"):
    """Return JSON-safe local facts; missing values remain null and lists bounded.

    Serial numbers and MAC addresses are for local display only.  No network,
    shell, hardware writes, automatic driver loading, or socket calls occur.
    ``run_root`` supplies exact resolver state filenames only; arbitrary files,
    NetworkManager connection profiles and credentials are never enumerated.
    """
    proc, sys, etc, run = map(Path, (proc_root, sys_root, etc_root, run_root))
    reader = _Reader((proc, sys, etc, run, etc.parent / "usr/share/misc", etc.parent / "usr/share/hwdata",
                      etc.parent / "usr/lib/os-release", *_firmware_metadata_paths(etc)))
    thermal, fans = _sensors(reader, sys)
    backlight = _backlights(reader, sys)
    storage = _storage(reader, proc, sys)
    routes4, status4 = _default_routes(reader, proc, 4)
    routes6, status6 = _default_routes(reader, proc, 6)
    default_routes = {"ipv4": routes4, "ipv6": routes6}
    route_status = {"ipv4": status4, "ipv6": status6}
    network = _network(reader, proc, sys, default_routes, route_status)
    return {
        "identity": _identity(reader, proc, sys, etc), "cpu": _cpu(reader, proc, sys),
        "thermal": thermal, "fans": fans, "backlight": backlight,
        "storage": storage, "network": network,
        "network_config": {"default_routes": default_routes, "route_status": route_status,
                           "dns": _dns(reader, etc, run)},
        "capabilities": {
            "thermal": any(item["celsius"] is not None for item in thermal),
            "fan_rpm": any(item["rpm"] is not None for item in fans),
            "fan_pwm": any(item["pwm"] is not None for item in fans),
            "fan_roles_verified": False,
            "backlight": any(item["max_brightness"] for item in backlight),
            "mdraid": bool(storage["raid"]), "pci": bool(reader.names(sys / "bus/pci/devices", r"[0-9a-fA-F:.]+", 1)),
        },
    }

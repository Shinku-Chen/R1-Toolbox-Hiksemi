#!/usr/bin/env python3
"""Serve resident NAS Web and controls with an independently managed screen.

The container supervises this script in the host namespaces until it stops.
It does not install software or overwrite official screen files. Explicit user
actions can adjust supported hardware, manage workloads and request host power.
"""

import argparse
import array
import copy
import contextlib
import datetime
import errno
import hashlib
import http.server
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

try:
    import fcntl
except ImportError:  # Allow non-Linux development hosts to import the module.
    fcntl = None


HOST = "127.0.0.1"
PORT = 18731
SAMPLE_INTERVAL = 2.0


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def read_integer(path):
    text = read_text(path)
    try:
        return int(text.strip()) if text is not None else None
    except ValueError:
        return None


def read_cpu_ticks(proc_root):
    text = read_text(Path(proc_root) / "stat")
    if text is None:
        return None
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "cpu":
            continue
        try:
            # guest and guest_nice are already included in user/nice.
            ticks = [int(value) for value in fields[1:9]]
            if len(ticks) < 4 or any(value < 0 for value in ticks):
                return None
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
            return sum(ticks), idle
        except ValueError:
            return None
    return None


def read_memory(proc_root):
    text = read_text(Path(proc_root) / "meminfo")
    values = {}
    if text is not None:
        for line in text.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[0] in ("MemTotal:", "MemAvailable:"):
                try:
                    if fields[2] == "kB" and int(fields[1]) >= 0:
                        values[fields[0][:-1]] = int(fields[1]) * 1024
                except ValueError:
                    pass
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    used = None
    if total is not None and total > 0 and available is not None and available <= total:
        used = total - available
    return {
        "total_bytes": total if total and total > 0 else None,
        "used_bytes": used,
        "usage_percent": used / total * 100 if used is not None else None,
    }


def read_uptime(proc_root):
    text = read_text(Path(proc_root) / "uptime")
    try:
        value = float(text.split()[0]) if text else None
        return value if value is not None and math.isfinite(value) and value >= 0 else None
    except (ValueError, IndexError):
        return None


def default_interface(proc_root):
    text = read_text(Path(proc_root) / "net/route")
    candidates = []
    if text is None:
        return None
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000":
            continue
        try:
            if not int(fields[3], 16) & 1:
                continue
            iface, metric = fields[0], int(fields[6])
            if iface in ("lo", ".", "..") or "/" in iface or len(iface.encode()) > 15:
                continue
            candidates.append((metric, iface))
        except ValueError:
            continue
    return min(candidates)[1] if candidates else None


def interface_ipv4(iface):
    if iface is None or fcntl is None or not sys.platform.startswith("linux"):
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            request = struct.pack("256s", iface.encode("utf-8")[:15])
            result = fcntl.ioctl(connection.fileno(), 0x8915, request)  # SIOCGIFADDR
            return socket.inet_ntoa(result[20:24])
    except (OSError, ValueError, UnicodeError):
        return None


def read_network_counters(sys_root, iface):
    if iface is None:
        return None
    folder = Path(sys_root) / "class/net" / iface / "statistics"
    rx, tx = read_integer(folder / "rx_bytes"), read_integer(folder / "tx_bytes")
    return (rx, tx) if rx is not None and tx is not None and rx >= 0 and tx >= 0 else None


def read_cpu_temperature(sys_root):
    temperatures = []
    for folder in sorted((Path(sys_root) / "class/hwmon").glob("hwmon*")):
        name = read_text(folder / "name")
        if name is None or name.strip() not in ("coretemp", "k10temp"):
            continue
        driver = name.strip()
        preferred, fallback = [], []
        for sensor in folder.glob("temp*_input"):
            value = read_integer(sensor)
            if value is None:
                continue
            label = read_text(sensor.with_name(sensor.name.replace("_input", "_label")))
            label = label.strip() if label is not None else ""
            if (driver == "coretemp" and label.startswith("Package id")) or (
                driver == "k10temp" and label == "Tdie"
            ):
                preferred.append(value / 1000)
            elif sensor.name == "temp1_input" or (driver == "k10temp" and label == "Tctl"):
                fallback.append(value / 1000)
        if preferred or fallback:
            temperatures.append(max(preferred or fallback))
    return max(temperatures) if temperatures else None


def unescape_mount_field(text):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), text)


_CAPACITY_FILESYSTEMS = frozenset(('ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'ntfs3', 'exfat', 'vfat'))
_CAPACITY_SOURCE = 'manual_statvfs_cache'
_CAPACITY_WINDOW = 12.0
_CAPACITY_DRIVES_ROOT = '/drives'


class CapacityRefreshError(RuntimeError):
    """Safe fixed reason for the owner HTTP adapter; never expose OS errors."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


_CAPACITY_TRACE = threading.local()


def _capacity_trace_get(name):
    try:
        return getattr(_CAPACITY_TRACE, name, None)
    except Exception:
        return None


@contextlib.contextmanager
def _capacity_observation(evidence=None, *, owner=None):
    """Isolate only diagnostic work; never put a business read/gate here."""
    try:
        yield
    except Exception:
        if owner is not None:
            try:
                object.__setattr__(owner, '_capacity_observation_failed', True)
            except Exception:
                pass
        # Fixed RAM-only failure marker. Never retain the exception or claim a
        # partial collection is complete. Bypass custom evidence dict methods.
        if isinstance(evidence, dict):
            dict.update(evidence, observation_status='observer_error',
                        complete=False, reason='observer_error')
        # A child read/device failure makes the enclosing evidence incomplete,
        # without changing the production inventory's independent completeness.
        for name in ('read', 'device', 'inventory'):
            try:
                parent = getattr(_CAPACITY_TRACE, name, None)
                if isinstance(parent, dict):
                    dict.update(parent, observation_status='observer_error',
                                complete=False, reason='observer_error')
            except Exception:
                pass
    finally:
        if isinstance(evidence, dict) and dict.get(evidence, 'observation_status') == 'observer_error':
            dict.update(evidence, complete=False, reason='observer_error')


class _CapacityEntries(list):
    """The inventory result carries its own observation across interleaving calls."""
    def __init__(self, entries, observation):
        super().__init__(entries)
        self.observation = observation


def _capacity_digest(domain, value):
    """Domain-separated evidence only; never export mount paths or raw lines."""
    return hashlib.sha256((domain + '\0' + json.dumps(value, ensure_ascii=True,
                          separators=(',', ':'))).encode()).hexdigest()


def _capacity_stat_evidence(value):
    # Kernel inode/ctime values must survive JavaScript's 53-bit number limit.
    evidence = {}
    for key, attr in (('dev', 'st_dev'), ('ino', 'st_ino'), ('ctime_ns', 'st_ctime_ns')):
        text = str(getattr(value, attr))
        evidence[key] = text if len(text) <= 32 and re.fullmatch(r'-?[0-9]+', text) else None
    return evidence


def _capacity_rows_evidence(rows):
    """Summarize an already returned projection, without paths or another read."""
    if not isinstance(rows, list):
        return {'row_count': None, 'measured_count': None, 'date_digest': None,
                'dates_count': None, 'date_min': None, 'date_max': None, 'truncated': False}
    measured, dates = 0, []
    def number(value):
        return type(value) in (int, float) and math.isfinite(value)
    for index, row in enumerate(rows[:128]):
        if not isinstance(row, dict):
            continue
        total, used = row.get('total_bytes'), row.get('used_bytes')
        measured += int(number(total) and number(used))
        stamp = row.get('capacity_sampled_at')
        if number(stamp) and stamp > 0:
            dates.append((index, stamp))
    return {'row_count': len(rows), 'measured_count': measured,
            'date_digest': _capacity_digest('projection-dates', dates), 'dates_count': len(dates),
            'date_min': min((date for _, date in dates), default=None),
            'date_max': max((date for _, date in dates), default=None), 'truncated': len(rows) > 128}


class _CapacityLedger:
    """Bounded RAM provenance, updated only by actual production decisions.

    The ring stores summaries; full inventories are retained for latest/commit
    and the first loss only. Reading this object never repeats a safety check.
    Caller holds StatusSampler._capacity_lock for all access.
    """
    LIMIT = 32

    def __init__(self):
        self.instance = secrets.token_hex(8)
        self.seq, self.generation, self.dropped = 0, 0, 0
        self.events, self.latest, self.last_commit, self.first = [], None, None, None
        self.baseline = {}
        self.reconciled_inventory = None

    @staticmethod
    def changes(before, after):
        """None means evidence missing/incomplete, never 'proved unchanged'."""
        if not before or not after or not before.get('complete') or not after.get('complete'):
            return {'complete': False, 'mounts': [], 'reason': 'inventory_evidence_incomplete'}
        old = {row['mount_id']: row for row in before['entries']}
        new = {row['mount_id']: row for row in after['entries']}
        mounts = []
        for identity in sorted(set(old) | set(new)):
            left, right = old.get(identity), new.get(identity)
            fields = {}
            if left is not None and right is not None:
                for key in ('parent_id', 'major_minor', 'filesystem', 'device', 'key_digest', 'reason'):
                    fields[key] = left.get(key) != right.get(key)
                for key in ('mountline', 'target', 'root', 'source', 'mount_options',
                            'optional_fields', 'super_options', 'ancestry', 'sysfs_target',
                            'sysfs_parent', 'device_material'):
                    a, b = left['components'].get(key), right['components'].get(key)
                    fields[key] = a != b if a is not None and b is not None else None
                for key in ('link_before', 'target_before', 'parent_before', 'diskseq',
                            'target_digest', 'physical_digest', 'parent_major_minor'):
                    a, b = left['device_identity'].get(key), right['device_identity'].get(key)
                    fields[key] = a != b if a is not None and b is not None else None
            mounts.append({'mount_id': identity, 'presence_changed': (left is None) != (right is None),
                           'changed': fields})
        return {'complete': True, 'mounts': mounts, 'reason': None}

    @staticmethod
    def records(records):
        return [{'mount_id': key, 'key_digest': value['key'],
                 'sampled_at': value.get('sampled_at'),
                 'record_digest': _capacity_digest('capacity-record',
                    (value['key'], value.get('sampled_at'), value.get('values')))}
                for key, value in sorted(records.items())]

    def event(self, sampler, reason, *, before=None, before_epoch=None, commit=False):
        self.seq += 1
        state = sampler._capacity_memory_state()
        source = getattr(sampler._capacity_context, 'source', 'get_projection')
        stage = getattr(sampler._capacity_context, 'stage', 'projection')
        if source not in ('get_projection', 'sample', 'start', 'close', 'explicit_refresh',
                          'visible_refresh', 'missing_fill'):
            source = 'not_observed'
        if stage not in ('projection', 'lifecycle', 'refresh_begin', 'measure_entry',
                         'final_inventory', 'commit_prepare', 'commit_complete'):
            stage = 'not_observed'
        observation = getattr(sampler._capacity_context, 'inventory', None)
        now, wall = time.monotonic(), time.time()
        after = self.records(sampler._capacity_cache)
        before = after if before is None else before
        prior = {row['mount_id']: row['record_digest'] for row in before}
        current = {row['mount_id']: row['record_digest'] for row in after}
        lost = sorted(key for key in prior if prior[key] != current.get(key))
        event = {'seq': self.seq, 'generation': self.generation, 'source': source,
                 'stage': stage, 'reason': reason, 'sampled_mono': now, 'sampled_at': wall,
                 'capacity_epoch': state['capacity_epoch'], 'mount_epoch': state['mount_epoch'],
                 'before_epoch': before_epoch or {'capacity_epoch': state['capacity_epoch'],
                                                 'mount_epoch': state['mount_epoch']},
                 'counts': {key: state[key] for key in ('positive', 'negative', 'pending')},
                 'positive_before': len(before), 'changed_mount_ids': lost,
                 'inventory_seq': observation['seq'] if observation else None,
                 'watch': copy.deepcopy(sampler._capacity_last_watch)}
        # A provisional commit is invisible under the same capacity lock. A
        # rollback that restores every historical record is not a cache loss.
        baseline_lost = [key for key, value in self.baseline.items() if current.get(key) != value]
        if (self.last_commit is not None and self.first is None and lost and baseline_lost
                and reason not in ('commit_prepare', 'commit_success')):
            self.first = dict(event, before={'records': copy.deepcopy(before),
                               'inventory': copy.deepcopy(self.reconciled_inventory),
                               'inventory_role': 'last_reconciled_observation'},
                               after={'records': copy.deepcopy(after),
                               'inventory': copy.deepcopy(observation)},
                               component_changes=self.changes(self.reconciled_inventory, observation))
        if commit:
            self.generation += 1
            self.first = None
            self.baseline = current
            event['generation'] = self.generation
            self.last_commit = dict(event, records=copy.deepcopy(after), inventory=copy.deepcopy(observation))
        self.events.append(event)
        if len(self.events) > self.LIMIT:
            self.events.pop(0)
            self.dropped += 1


class _CapacityMountWatch:
    """Namespace event proof only; this proc FD never pins a user filesystem."""

    def __init__(self, proc_root):
        self.descriptor = None
        with _capacity_observation(owner=self):
            self.observation = {'reason': 'not_checked', 'mask': 0, 'namespace': None,
                                'namespace_current': None, 'namespace_read': False, 'polled': False}
        if not sys.platform.startswith('linux') or not hasattr(select, 'poll'):
            error = CapacityRefreshError('unavailable')
            with _capacity_observation(owner=self):
                error.watch_observation = dict(self.observation, stage='platform')
            raise error
        self.namespace = Path(proc_root) / 'self/ns/mnt'
        stage = 'namespace'
        try:
            self.identity = os.readlink(self.namespace)
            if not re.fullmatch(r'mnt:\[[1-9][0-9]*\]', self.identity):
                raise ValueError()
            stage = 'open_mountinfo'
            self.descriptor = os.open(Path(proc_root) / 'self/mountinfo',
                                      os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(self.descriptor).st_mode):
                raise ValueError()
            stage = 'register_poll'
            self.poller = select.poll()
            self.poller.register(self.descriptor, select.POLLPRI | select.POLLERR)
            stage = 'initial_check'
            if self.changed():
                raise ValueError()
        except (OSError, ValueError):
            self.close()
            error = CapacityRefreshError('unavailable')
            with _capacity_observation(owner=self):
                error.watch_observation = dict(self.observation, stage=stage)
            raise error from None

    def changed(self):
        observation = {'reason': 'unchanged', 'mask': 0, 'namespace_read': False, 'polled': False,
                       'namespace': None, 'namespace_current': None}
        with _capacity_observation(observation, owner=self):
            observation['namespace'] = getattr(self, 'identity', None)
        try:
            if self.descriptor is None:
                with _capacity_observation(observation, owner=self):
                    observation['reason'] = 'descriptor_lost'
                return True
            current = os.readlink(self.namespace)
            with _capacity_observation(observation, owner=self):
                observation.update(namespace_current=current, namespace_read=True)
            if current != self.identity:
                with _capacity_observation(observation, owner=self):
                    observation['reason'] = 'namespace_changed'
                return True
            events = self.poller.poll(0)
            with _capacity_observation(observation, owner=self):
                observation['polled'] = True
                observation['mask'] = 0
                for _, mask in events:
                    observation['mask'] |= mask
            if events:
                with _capacity_observation(observation, owner=self):
                    observation['reason'] = 'mount_event'
            return bool(events)
        except (OSError, ValueError):
            with _capacity_observation(observation, owner=self):
                observation['reason'] = 'watch_error'
            return True
        finally:
            # Only canonical namespace identities are public, never readlink
            # error text or any unexpected path returned by a test/OS seam.
            with _capacity_observation(observation, owner=self):
                for key in ('namespace', 'namespace_current'):
                    value = observation[key]
                    observation[key] = value if isinstance(value, str) and re.fullmatch(r'mnt:\[[1-9][0-9]{0,24}\]', value) else None
                self.observation = observation

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def _capacity_read(path, limit):
    """Read kernel metadata to bounded EOF, including short proc/sys reads."""
    descriptor = None
    evidence = _capacity_trace_get('read')
    with _capacity_observation(evidence):
        if evidence is not None:
            evidence.update(reason='open_failed', bytes=0, complete=False)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
                             | getattr(os, 'O_CLOEXEC', 0))
        before = os.fstat(descriptor)
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence['reason'] = 'not_regular'
        if not stat.S_ISREG(before.st_mode):
            return None
        chunks, size = [], 0
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence['reason'] = 'read_failed'
        while True:
            chunk = os.read(descriptor, min(16384, limit + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            with _capacity_observation(evidence):
                if evidence is not None:
                    evidence.update(bytes=size, reason='read_limit' if size > limit else 'read_failed')
            if size > limit:
                return None
            chunks.append(chunk)
        after = os.fstat(descriptor)
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence['reason'] = 'read_identity_changed'
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return None
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence['reason'] = 'decode_failed'
        value = b''.join(chunks).decode('utf-8', 'strict')
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence.update(reason=None, complete=True)
        return value
    except (OSError, ValueError, UnicodeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _capacity_traced_read(path, limit, evidence):
    previous = None
    with _capacity_observation(evidence):
        previous = getattr(_CAPACITY_TRACE, 'read', None)
        _CAPACITY_TRACE.read = evidence
    try:
        return _capacity_read(path, limit)
    finally:
        with _capacity_observation(evidence):
            _CAPACITY_TRACE.read = previous


def _capacity_device(sys_root, device):
    """Identity of a real sysfs block lifetime; never inspect a /drives path."""
    evidence = _capacity_trace_get('device')
    def phase(value):
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence.update(stage=value, reason='device_identity_unavailable', complete=False)
    def rejected(reason):
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence['reason'] = reason
        return None
    phase('resolve')
    try:
        base = Path(sys_root).resolve(strict=True)
        link = base / 'dev/block' / device
        link_before = link.lstat()
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence['link_before'] = _capacity_stat_evidence(link_before)
        target = link.resolve(strict=True)
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence['target_digest'] = _capacity_digest('sysfs-target', str(target))
        if base not in target.parents or 'virtual' in target.relative_to(base).parts:
            return rejected('target_untrusted')
        # Partitions are bound to their parent physical device as well.
        before = target.stat()
        phase('target')
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence['target_before'] = _capacity_stat_evidence(before)
        if not stat.S_ISDIR(before.st_mode):
            return rejected('target_not_directory')
        partition = _capacity_read(target / 'partition', 64)
        if partition is not None and not re.fullmatch(r'[1-9][0-9]{0,9}\n?', partition):
            return rejected('partition_invalid')
        physical = target.parent if partition is not None else target
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence['physical_digest'] = _capacity_digest('sysfs-parent', str(physical))
        if not re.fullmatch(r'(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+)', physical.name):
            return rejected('physical_name_invalid')
        parent_before = physical.stat()
        phase('parent')
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence['parent_before'] = _capacity_stat_evidence(parent_before)
        if not stat.S_ISDIR(parent_before.st_mode):
            return rejected('parent_not_directory')
        phase('attributes')
        reads = {}
        with _capacity_observation(evidence):
            if evidence is not None:
                reads = evidence.setdefault('reads', {})
        def read_attribute(path, name):
            observed = None
            with _capacity_observation(evidence):
                observed = reads.setdefault(name, {})
            return _capacity_traced_read(path, 64, observed)
        declared = read_attribute(target / 'dev', 'device')
        parent_dev = read_attribute(physical / 'dev', 'parent_device')
        diskseq = read_attribute(physical / 'diskseq', 'diskseq')
        if declared is None or declared.strip() != device or parent_dev is None:
            return rejected('device_number_mismatch')
        if not re.fullmatch(r'[0-9]{1,7}:[0-9]{1,7}', parent_dev.strip()):
            return rejected('parent_number_invalid')
        if diskseq is not None and not re.fullmatch(r'[1-9][0-9]{0,19}\n?', diskseq):
            return rejected('diskseq_invalid')
        after, parent_after = target.stat(), physical.stat()
        link_after = link.lstat()
        phase('lifecycle')
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence.update(target_after=_capacity_stat_evidence(after), parent_after=_capacity_stat_evidence(parent_after),
                                link_after=_capacity_stat_evidence(link_after), diskseq=diskseq.strip() if diskseq else None,
                                parent_major_minor=parent_dev.strip())
        def generation(value):
            return (value.st_dev, value.st_ino, value.st_ctime_ns)
        if (generation(before), generation(parent_before), generation(link_before)) != (
                generation(after), generation(parent_after), generation(link_after)):
            return rejected('device_generation_changed')
        if link.resolve(strict=True) != target:
            return rejected('device_link_changed')
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence.update(stage='complete', reason=None, complete=True)
                if any(field is None for name in ('link_before', 'target_before', 'parent_before',
                                                  'target_after', 'parent_after', 'link_after')
                       for field in evidence[name].values()):
                    evidence.update(complete=False, reason='identity_evidence_out_of_range')
        return (str(target), str(physical), generation(link_before), generation(before),
                generation(parent_before), parent_dev.strip(), diskseq.strip() if diskseq else None, physical.name)
    except (OSError, ValueError, RuntimeError):
        return rejected('metadata_error')


def _capacity_inventory(proc_root, sys_root):
    """Only bounded mountinfo/sysfs metadata, never stat/resolve mount targets."""
    evidence = _capacity_trace_get('inventory')
    def reject(stage, reason):
        with _capacity_observation(evidence):
            if evidence is not None:
                evidence.update(stage=stage, reason=reason, complete=False,
                                count=len(evidence.get('entries', [])),
                                truncated=reason in ('line_limit', 'entry_limit')
                                or evidence.get('read_before', {}).get('reason') == 'read_limit'
                                or evidence.get('read_after', {}).get('reason') == 'read_limit')
        return [], False
    with _capacity_observation(evidence):
        if evidence is not None:
            evidence.update(stage='mountinfo', reason=None, complete=False, entries=[],
                            limits={'bytes': 262144, 'lines': 4096, 'entries': 128}, read_before={})
    read_before = None
    with _capacity_observation(evidence):
        if evidence is not None:
            read_before = evidence['read_before']
    text = _capacity_traced_read(Path(proc_root) / 'self/mountinfo', 262144, read_before)
    if text is None or '\x00' in text:
        return reject('mountinfo', 'mountinfo_unreadable' if text is None else 'mountinfo_malformed')
    lines = text.splitlines()
    if len(lines) > 4096:
        return reject('parse', 'line_limit')
    with _capacity_observation(evidence):
        if evidence is not None:
            evidence['line_count'] = len(lines)
    entries, mounts, seen_ids, devices = [], [], set(), {}
    for line in lines:
        parts = line.split(' - ')
        if len(parts) != 2:
            return reject('parse', 'mountinfo_malformed')
        fields, extra = parts[0].split(), parts[1].split()
        if (len(fields) < 6 or len(extra) < 3 or not re.fullmatch(r'[1-9][0-9]{0,9}', fields[0])
                or not re.fullmatch(r'[0-9]{1,10}', fields[1])):
            return reject('parse', 'mountinfo_malformed')
        mount_id = int(fields[0])
        if mount_id in seen_ids:
            return reject('parse', 'duplicate_mount_id')
        seen_ids.add(mount_id)
        mount, root = unescape_mount_field(fields[4]), unescape_mount_field(fields[3])
        mounts.append((mount_id, int(fields[1]), mount, root, fields, extra, line))
    parents = {item[0]: item[1] for item in mounts}
    targets = {}
    for item in mounts:
        targets.setdefault(item[2], []).append(item[0])
    for mount_id, parent_id, mount, root, fields, extra, line in mounts:
        if not mount.startswith(_CAPACITY_DRIVES_ROOT + '/') or root != '/':
            continue
        components = mount.split('/')[1:]
        if (any(part in ('', '.', '..') for part in components) or len(mount.encode()) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in mount)):
            return reject('target', 'mount_target_invalid')
        if extra[0] in ('tmpfs', 'devtmpfs', 'proc', 'sysfs', 'overlay', 'squashfs'):
            continue
        if len(entries) >= 128:
            return reject('target', 'entry_limit')
        row = {'mount_id': mount_id, 'mount': mount, 'device': fields[2], 'root': root,
               'filesystem': extra[0], 'source': unescape_mount_field(extra[1]), '_key': None,
               '_reason': 'unsupported_filesystem', '_device_name': None}
        identity = None
        if re.fullmatch(r'[1-9][0-9]{0,6}:[0-9]{1,7}', fields[2]) and extra[0] in _CAPACITY_FILESYSTEMS:
            previous_device = None
            device_evidence = {'complete': False, 'stage': 'not_observed', 'reason': None}
            with _capacity_observation(evidence):
                previous_device = getattr(_CAPACITY_TRACE, 'device', None)
                _CAPACITY_TRACE.device = device_evidence
            try:
                identity = _capacity_device(sys_root, fields[2])
            finally:
                with _capacity_observation(evidence):
                    _CAPACITY_TRACE.device = previous_device
            row['_reason'] = 'device_identity_unavailable'
            if identity is not None:
                # Include mount options/parentage, so an observed remount is
                # not silently treated as the previous capacity generation.
                material = (line, identity)
                row['_key'] = hashlib.sha256(json.dumps(material, ensure_ascii=True).encode()).hexdigest()
                row['_reason'] = None
                row['_device_name'] = identity[-1]
        # Two full-root aliases or stacked targets are ambiguous; none may
        # inherit a cache entry simply because their device number matches.
        ancestors, current = set(), parent_id
        while current in parents and current not in ancestors:
            ancestors.add(current)
            current = parents[current]
        # Even an ignored FUSE/subdirectory bind may shadow an old target.
        # Require every enclosing mount in this namespace to be a real
        # ancestor, and reject stacked names without opening either target.
        obscured = len(targets[mount]) != 1 or any(
            target != mount and (target == '/' or mount.startswith(target.rstrip('/') + '/'))
            and any(identity not in ancestors for identity in identities)
            for target, identities in targets.items())
        previous = devices.get(fields[2])
        if previous is not None or obscured:
            row['_key'], row['_reason'] = None, 'identity_changed'
            if previous is not None:
                previous['_key'], previous['_reason'] = None, 'identity_changed'
        devices[fields[2]] = row
        entries.append(row)
        with _capacity_observation(evidence):
            if evidence is not None:
                # Every key component is covered, including unrecognised options.
                components = {'mountline': line, 'target': mount, 'root': root, 'source': extra[1],
                              'mount_options': fields[5], 'optional_fields': fields[6:],
                              'super_options': extra[2:], 'ancestry': sorted(ancestors),
                              'device_material': identity,
                              'sysfs_target': identity[0] if identity is not None and len(identity) == 8 else None,
                              'sysfs_parent': identity[1] if identity is not None and len(identity) == 8 else None}
                evidence['entries'].append({'mount_id': mount_id, 'parent_id': parent_id,
                    'major_minor': fields[2] if re.fullmatch(r'[0-9]{1,7}:[0-9]{1,7}', fields[2]) else None,
                    'filesystem': extra[0] if extra[0] in _CAPACITY_FILESYSTEMS else 'unsupported',
                    'device': row['_device_name'], 'key_digest': row['_key'], 'reason': row['_reason'],
                    'components': {name: _capacity_digest(name, value) if value is not None else None
                                   for name, value in components.items()},
                    'device_identity': copy.deepcopy(device_evidence) if extra[0] in _CAPACITY_FILESYSTEMS
                        and re.fullmatch(r'[1-9][0-9]{0,6}:[0-9]{1,7}', fields[2]) else {'complete': False, 'stage': 'not_run'}})
    read_after = {}
    if _capacity_traced_read(Path(proc_root) / 'self/mountinfo', 262144, read_after) != text:
        reason = 'mountinfo_unreadable'
        with _capacity_observation(evidence):
            reason = 'mountinfo_changed' if read_after.get('complete') else 'mountinfo_unreadable'
            if evidence is not None:
                evidence['read_after'] = read_after
        return reject('verify_mountinfo', reason)
    with _capacity_observation(evidence):
        if evidence is not None:
            by_id = {row['mount_id']: row for row in entries}
            for row in evidence['entries']:
                row['key_digest'], row['reason'] = by_id[row['mount_id']]['_key'], by_id[row['mount_id']]['_reason']
            evidence.update(stage='complete', reason=None, complete=True, read_after=read_after,
                            count=len(entries), truncated=False)
    return sorted(entries, key=lambda row: row['mount']), True


def _capacity_projection(entries, cache):
    rows = []
    for entry in entries:
        row = {'mount': entry['mount'], 'mount_id': entry['mount_id'],
               'total_bytes': None, 'used_bytes': None, 'usage_percent': None,
               'capacity_sampled_at': None, 'capacity_source': _CAPACITY_SOURCE,
               'capacity_status': 'unknown' if entry['_key'] else 'unavailable'}
        record = cache.get(entry['mount_id'])
        if record is not None:
            if entry['_key'] and record['key'] == entry['_key']:
                row.update(record['values'])
                row.update(capacity_sampled_at=record['sampled_at'], capacity_status='cached')
            else:
                row['capacity_status'] = 'identity_changed'
        rows.append(row)
    return rows


def read_disks(proc_root, statvfs=None, *, sys_root='/sys', capacity_cache=None):
    """Passive inventory/cache only. Legacy statvfs injection never runs."""
    entries, _ = _capacity_inventory(proc_root, sys_root)
    return _capacity_projection(entries, capacity_cache or {})


def _capacity_fd_mount(descriptor, proc_root='/proc'):
    text = _capacity_read(Path(proc_root) / 'self/fdinfo' / str(descriptor), 4096)
    found = re.findall(r'^mnt_id:\s*([1-9][0-9]*)$', text or '', re.M)
    if len(found) != 1:
        raise CapacityRefreshError('identity_changed')
    return int(found[0])


def _capacity_measure_entry(entry, deadline, proc_root='/proc', sys_root='/sys', *, keep_handle=False):
    """Explicit worker only: no-follow real mount FD before/after fstatvfs."""
    def current():
        if time.monotonic() >= deadline:
            raise CapacityRefreshError('timeout')
        rows, complete = _capacity_inventory(proc_root, sys_root)
        if not complete or not entry['_key'] or not any(
                row['mount_id'] == entry['mount_id'] and row['_key'] == entry['_key'] for row in rows):
            raise CapacityRefreshError('identity_changed')
    current()
    if not sys.platform.startswith('linux') or not hasattr(os, 'fstatvfs'):
        raise CapacityRefreshError('unavailable')
    parts = entry['mount'].split('/')[1:]
    if (not entry['mount'].startswith(_CAPACITY_DRIVES_ROOT + '/')
            or any(part in ('', '.', '..') for part in parts)):
        raise CapacityRefreshError('identity_changed')
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        descriptors.append(os.open('/', flags))
        for part in parts:
            if time.monotonic() >= deadline:
                raise CapacityRefreshError('timeout')
            descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
        descriptor = descriptors[-1]
        before = os.fstat(descriptor)
        wanted = tuple(int(value) for value in entry['device'].split(':'))
        if (not stat.S_ISDIR(before.st_mode) or (os.major(before.st_dev), os.minor(before.st_dev)) != wanted
                or _capacity_fd_mount(descriptor, proc_root) != entry['mount_id']):
            raise CapacityRefreshError('identity_changed')
        current()
        stats = os.fstatvfs(descriptor)
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode)) !=
                (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
                or _capacity_fd_mount(descriptor, proc_root) != entry['mount_id']):
            raise CapacityRefreshError('identity_changed')
        current()
        block = stats.f_frsize or stats.f_bsize
        total, used = stats.f_blocks * block, (stats.f_blocks - stats.f_bfree) * block
        if not (type(total) is int and type(used) is int and 0 < total <= 2**63 - 1 and 0 <= used <= total):
            raise CapacityRefreshError('invalid_measurement')
        values = {'total_bytes': total, 'used_bytes': used, 'usage_percent': used / total * 100}
        # The real mount FD lives only through measurement and commit checks.
        # Cached values retain a proc namespace event watch, never this FD.
        return (values, os.dup(descriptor)) if keep_handle else values
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _capacity_worker_main():
    """Private subprocess protocol; public APIs never accept worker arguments."""
    anchor, channel = None, None
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError()
        request = json.loads(raw)
        if set(request) != {'entry', 'deadline', 'channel'} or type(request['channel']) is not int:
            raise ValueError()
        channel = socket.socket(fileno=request['channel'])
        value, anchor = _capacity_measure_entry(request['entry'], request['deadline'], keep_handle=True)
        channel.sendmsg([b'1'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [anchor]))])
        result = {'ok': True, 'values': value}
    except CapacityRefreshError as error:
        result = {'ok': False, 'reason': error.reason}
    except Exception:
        result = {'ok': False, 'reason': 'probe_failed'}
    finally:
        if anchor is not None:
            os.close(anchor)
        if channel is not None:
            channel.close()
    print(json.dumps(result, allow_nan=False), flush=True)
    return 0


def empty_status():
    return {
        "schema_version": 1,
        "available": False,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "hostname": None,
        "uptime_seconds": None,
        "cpu": {"usage_percent": None, "temp_c": None},
        "memory": {"total_bytes": None, "used_bytes": None, "usage_percent": None},
        "network": {"interface": None, "ipv4": None, "rx_bytes_per_sec": None, "tx_bytes_per_sec": None},
        "disks": [],
    }


class StatusSampler:
    """Sampling is separate from serving and accepts fixture proc/sys roots."""

    def __init__(self, proc_root="/proc", sys_root="/sys", clock=None):
        self.proc_root, self.sys_root = Path(proc_root), Path(sys_root)
        self.clock = clock or time.monotonic
        self.previous_cpu = None
        self.previous_network = None
        self._capacity_lock = threading.RLock()
        self._capacity_refresh_lock = threading.Lock()
        self._capacity_cache = {}
        self._capacity_negative = {}
        self._capacity_watch = None
        self._capacity_mount_epoch = 0
        self._capacity_observed_keys = None
        self._capacity_worker = None
        self._capacity_reaper = None
        self._capacity_epoch = 0
        self._capacity_closed = False
        self._capacity_context = threading.local()
        self._capacity_observation_errors = 0
        self._capacity_ledger = self._capacity_observe(_CapacityLedger)
        self._capacity_pending = 0
        self._capacity_pending_records = None
        self._capacity_last_watch = None

    def _capacity_observe(self, operation, *args, **kwargs):
        """Best effort diagnostic boundary; business predicates stay outside."""
        try:
            return operation(*args, **kwargs)
        except Exception:
            try:
                self._capacity_observation_errors += 1
            except Exception:
                pass
            return None

    def _capacity_note(self, reason, **kwargs):
        # Protect the entry point as well as ledger.event: instrumentation may
        # be replaced by a failing observer, but may never strand a real lock.
        return self._capacity_observe(lambda: self._capacity_record(reason, **kwargs))

    @contextlib.contextmanager
    def _capacity_scope(self, source=None, stage=None):
        previous, previous_inventory, previous_pending = None, None, None
        def enter():
            nonlocal previous, previous_inventory, previous_pending
            previous = (getattr(self._capacity_context, 'source', 'get_projection'),
                        getattr(self._capacity_context, 'stage', 'projection'))
            previous_inventory = getattr(self._capacity_context, 'inventory', None)
            previous_pending = getattr(self._capacity_context, 'pending', None)
            self._capacity_context.source = source or previous[0]
            self._capacity_context.stage = stage or previous[1]
            if source is not None:
                self._capacity_context.inventory = None
                self._capacity_observe(lambda: setattr(self._capacity_context, 'pending', None))
        self._capacity_observe(enter)
        try:
            yield
        finally:
            def leave():
                if previous is not None:
                    self._capacity_context.source, self._capacity_context.stage = previous
                    self._capacity_context.inventory = previous_inventory
                    self._capacity_context.pending = previous_pending
            self._capacity_observe(leave)

    def _capacity_memory_state(self):
        return {'capacity_epoch': self._capacity_epoch, 'mount_epoch': self._capacity_mount_epoch,
                'positive': len(self._capacity_cache), 'negative': len(self._capacity_negative),
                'pending': len(self._capacity_pending_records) if self._capacity_pending_records is not None
                           else self._capacity_pending, 'closed': self._capacity_closed,
                'watch_present': self._capacity_watch is not None,
                'worker_present': self._capacity_worker is not None,
                'reaper_present': self._capacity_reaper is not None}

    def _capacity_record(self, reason, **kwargs):
        fixed = {'inventory_observed', 'projection', 'cache_replaced', 'invalidated',
                 'watch_missing', 'watch_create_failed', 'active_keys_changed', 'inventory_incomplete',
                 'reconcile', 'start', 'close', 'scan_failed', 'row_identity_changed',
                 'commit_prepare', 'commit_success', 'batch_no_new_values', 'batch_finished',
                 'batch_started', 'pending_added', 'pending_discarded', 'pending_lifecycle_changed',
                 'batch_exception'}
        failures = {'authorization_denied', 'unavailable', 'timeout', 'busy', 'probe_failed',
                    'invalid_measurement', 'identity_changed'}
        watch_reasons = {'unchanged', 'descriptor_lost', 'namespace_changed', 'mount_event',
                         'watch_error', 'changed_unclassified', 'observer_error'}
        if (reason not in fixed and reason not in {'watch_' + item for item in watch_reasons}
                and reason not in {prefix + item for prefix in ('batch_', 'probe_', 'rollback_', 'pending_rejected_')
                                   for item in failures}):
            reason = 'unclassified_decision'
        pending = getattr(self._capacity_context, 'pending', None)
        if pending is not None:
            self._capacity_pending = len(pending)
        self._capacity_observe(lambda: self._capacity_ledger.event(self, reason, **kwargs))

    @staticmethod
    def _capacity_watch_evidence(observation):
        if not isinstance(observation, dict):
            return None
        value = {key: observation.get(key) is True for key in ('namespace_read', 'polled')}
        value['reason'] = observation.get('reason') if observation.get('reason') in (
            'unchanged', 'descriptor_lost', 'namespace_changed', 'mount_event', 'watch_error') else 'changed_unclassified'
        mask = observation.get('mask')
        value['mask'] = mask if type(mask) is int and 0 <= mask <= 65535 else None
        stage = observation.get('stage')
        value['stage'] = stage if stage in ('platform', 'namespace', 'open_mountinfo',
                                            'register_poll', 'initial_check') else None
        for key in ('namespace', 'namespace_current'):
            namespace = observation.get(key)
            value[key] = namespace if isinstance(namespace, str) and re.fullmatch(r'mnt:\[[1-9][0-9]{0,24}\]', namespace) else None
        return value

    def _capacity_inventory_observe(self):
        """Attach an optional observer to the same production read, not a rescan."""
        evidence = {}
        self._capacity_observe(lambda: setattr(self._capacity_context, 'inventory', None))
        previous = None
        with _capacity_observation(evidence):
            previous = getattr(_CAPACITY_TRACE, 'inventory', None)
            _CAPACITY_TRACE.inventory = evidence
        try:
            result = _capacity_inventory(self.proc_root, self.sys_root)
        finally:
            with _capacity_observation(evidence):
                _CAPACITY_TRACE.inventory = previous
        def publish():
            if evidence.get('observation_status') == 'observer_error':
                self._capacity_observation_errors += 1
            if not evidence:
                # OS seams used by portable fixtures may supply an inventory
                # without observing it. Do not invent complete provenance.
                evidence.update(stage='not_observed', reason='observer_unavailable',
                                complete=False, entries=[], truncated=False)
            evidence.update(seq=self._capacity_ledger.seq + 1, sampled_mono=time.monotonic(),
                            sampled_at=time.time(), capacity_epoch=self._capacity_epoch,
                            mount_epoch=self._capacity_mount_epoch)
            self._capacity_ledger.latest = evidence
            self._capacity_context.inventory = evidence
            self._capacity_note('inventory_observed')
            return True
        with self._capacity_lock:
            if self._capacity_observe(publish) is not True:
                with _capacity_observation(evidence):
                    evidence.update(observation_status='observer_error', complete=False, reason='observer_error')
        observed = self._capacity_observe(lambda: _CapacityEntries(result[0], evidence))
        return (result[0] if observed is None else observed), result[1]

    def capacity_cache_diagnostics(self):
        """Copy only RAM. In particular never reap, scan, poll, or project disks."""
        with self._capacity_lock:
            ledger = self._capacity_ledger
            return copy.deepcopy({'schema_version': 1, 'instance': ledger.instance if ledger else None,
                'seq': ledger.seq if ledger else None, 'generation': ledger.generation if ledger else None,
                'ring_limit': 32, 'dropped_events': ledger.dropped if ledger else None,
                'events': ledger.events if ledger else [], 'latest_inventory': ledger.latest if ledger else None,
                'last_commit': ledger.last_commit if ledger else None,
                'first_invalidation': ledger.first if ledger else None,
                'observation_status': 'observer_error' if self._capacity_observation_errors else 'observed',
                'observation_errors': self._capacity_observation_errors,
                'state': self._capacity_memory_state(), 'watch': self._capacity_last_watch,
                'numeric_encoding': 'kernel_identity_decimal_strings',
                'inventory_limit': 128, 'read_only_memory': True})

    def capacity_snapshot(self):
        """Recheck kernel identities immediately, independent of the 2s sample."""
        with self._capacity_lock:
            self._capacity_reap_exited()
            entries, complete = self._capacity_inventory_observe()
            previous = set(self._capacity_cache)
            self._capacity_check_watch()
            # Observing an unmount/incomplete scan irrevocably discards the old
            # record; a later same-name mount cannot revive it.
            self._capacity_reconcile(entries, complete)
            rows = _capacity_projection(entries, self._capacity_cache)
            for row in rows:
                if row['mount_id'] in previous and row['mount_id'] not in self._capacity_cache:
                    row['capacity_status'] = 'identity_changed'
            self._capacity_note('projection')
            def publish():
                if self._capacity_observation_errors:
                    self._capacity_context.returned_projection = None
                    return
                event = self._capacity_ledger.events[-1]
                self._capacity_context.returned_projection = (rows, {
                    'status': 'observed', 'sequence': event['seq'], 'generation': event['generation'],
                    'capacity_epoch': self._capacity_epoch, 'mount_epoch': self._capacity_mount_epoch,
                    'sampled_monotonic': event['sampled_mono'], 'sampled_at': event['sampled_at'],
                    'atomic_with_capacity': True})
            # Clear first so a failed observer cannot retain a prior row's
            # provenance. Both operations remain independent of the real rows.
            self._capacity_observe(lambda: setattr(self._capacity_context, 'returned_projection', None))
            self._capacity_observe(publish)
            return rows

    def capacity_projection_observation(self, rows):
        """Return metadata only for this thread's exact original rows object."""
        def observe():
            pair = getattr(self._capacity_context, 'returned_projection', None)
            if pair is not None and pair[0] is rows:
                return copy.deepcopy(pair[1])
        return self._capacity_observe(observe) or {'status': 'not_observed'}

    def _capacity_replace_cache(self, records, reason='cache_replaced', before_epoch=None):
        """Caller holds the lock; records contain numbers, not mounted FS FDs."""
        before = self._capacity_observe(lambda: self._capacity_ledger.records(self._capacity_cache))
        self._capacity_cache = records
        self._capacity_note(reason, before=before, before_epoch=before_epoch)

    def _capacity_invalidate(self, reason='invalidated'):
        before = self._capacity_observe(lambda: self._capacity_ledger.records(self._capacity_cache))
        before_epoch = {'capacity_epoch': self._capacity_epoch, 'mount_epoch': self._capacity_mount_epoch}
        self._capacity_mount_epoch += 1
        self._capacity_cache = {}
        self._capacity_negative = {}
        self._capacity_note(reason, before=before, before_epoch=before_epoch)

    def _capacity_check_watch(self, *, create=False):
        """Any event or loss of proof rejects all namespace-derived values."""
        watch = self._capacity_watch
        if watch is not None and watch.changed():
            self._capacity_last_watch = self._capacity_observe(lambda: self._capacity_watch_observe(watch))
            reason = self._capacity_last_watch['reason'] if self._capacity_last_watch else 'changed_unclassified'
            self._capacity_invalidate('watch_' + reason)
            watch.close()
            self._capacity_watch = None
        if self._capacity_watch is None:
            if self._capacity_cache or self._capacity_negative:
                self._capacity_invalidate('watch_missing')
            if create:
                try:
                    self._capacity_watch = _CapacityMountWatch(self.proc_root)
                except CapacityRefreshError as error:
                    self._capacity_last_watch = self._capacity_observe(lambda: self._capacity_watch_evidence(getattr(error, 'watch_observation', None)))
                    self._capacity_note('watch_create_failed')
                    raise
        if self._capacity_watch is not None:
            self._capacity_last_watch = self._capacity_observe(
                lambda: self._capacity_watch_observe(self._capacity_watch))
        return self._capacity_watch is not None

    def _capacity_watch_observe(self, watch):
        if getattr(watch, '_capacity_observation_failed', False):
            self._capacity_observation_errors += 1
            return {'reason': 'observer_error', 'complete': False}
        return self._capacity_watch_evidence(getattr(watch, 'observation', None))

    def _capacity_reconcile(self, entries, complete):
        """An observed detach is permanent for an old cache, even if it returns."""
        active = {row['mount_id']: row['_key'] for row in entries if row['_key']} if complete else {}
        with self._capacity_lock:
            self._capacity_observe(lambda: setattr(self._capacity_context, 'inventory', getattr(entries, 'observation', None)))
            if self._capacity_observed_keys is not None and active != self._capacity_observed_keys:
                self._capacity_invalidate('active_keys_changed' if complete else 'inventory_incomplete')
            self._capacity_observed_keys = active
            self._capacity_replace_cache({key: value for key, value in self._capacity_cache.items()
                                          if active.get(key) == value['key']}, reason='reconcile')
            self._capacity_observe(lambda: setattr(self._capacity_ledger, 'reconciled_inventory', getattr(self._capacity_context, 'inventory', None)))
            keys = set(active.values())
            self._capacity_negative = {key: value for key, value in self._capacity_negative.items()
                                       if key in keys and value[0] > time.monotonic()}
        return active

    def start_capacity(self):
        with self._capacity_scope('start', 'lifecycle'), self._capacity_lock:
            before_epoch = {'capacity_epoch': self._capacity_epoch, 'mount_epoch': self._capacity_mount_epoch}
            self._capacity_epoch += 1
            self._capacity_closed = False
            self._capacity_negative = {}
            self._capacity_mount_epoch += 1
            self._capacity_replace_cache({}, reason='start', before_epoch=before_epoch)
            self._capacity_observed_keys = None
            if self._capacity_watch is not None:
                self._capacity_watch.close()
                self._capacity_watch = None

    def close_capacity(self):
        with self._capacity_scope('close', 'lifecycle'), self._capacity_lock:
            before_epoch = {'capacity_epoch': self._capacity_epoch, 'mount_epoch': self._capacity_mount_epoch}
            self._capacity_epoch += 1
            self._capacity_closed = True
            self._capacity_negative = {}
            self._capacity_mount_epoch += 1
            self._capacity_replace_cache({}, reason='close', before_epoch=before_epoch)
            self._capacity_observed_keys = None
            if self._capacity_watch is not None:
                self._capacity_watch.close()
                self._capacity_watch = None
            worker = self._capacity_worker
            if worker is not None and worker.poll() is None:
                # SIGKILL may remain pending during an uninterruptible kernel
                # call. Keep this handle and reject another batch until exit.
                try:
                    worker.kill()
                except OSError:
                    pass
            if worker is not None and worker.poll() is not None:
                for stream in (worker.stdin, worker.stdout):
                    if stream is not None:
                        stream.close()
                self._capacity_worker = None
            elif worker is not None:
                self._capacity_schedule_reap(worker)

    def _capacity_probe(self, entry, deadline, authorized):
        """Bound the request, not the kernel: a lingering worker stays busy."""
        authorized()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CapacityRefreshError('timeout')
        if not sys.platform.startswith('linux') or not hasattr(socket, 'SCM_RIGHTS'):
            raise CapacityRefreshError('unavailable')
        channel, child_channel = socket.socketpair()
        channel.setblocking(False)
        try:
            worker = subprocess.Popen(
                [sys.executable, '-B', os.path.abspath(__file__), '--capacity-worker'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(child_channel.fileno(),))
        except OSError:
            channel.close()
            child_channel.close()
            raise CapacityRefreshError('unavailable') from None
        with self._capacity_lock:
            self._capacity_worker = worker
        anchor, transferred = None, False
        try:
            request = json.dumps({'entry': entry, 'deadline': deadline,
                                  'channel': child_channel.fileno()}, ensure_ascii=True).encode()
            child_channel.close()
            if len(request) > 4096:
                raise CapacityRefreshError('unavailable')
            authorized()
            if time.monotonic() >= deadline:
                raise CapacityRefreshError('timeout')
            worker.stdin.write(request)
            worker.stdin.close()
            while worker.poll() is None:
                authorized()
                if time.monotonic() >= deadline:
                    raise CapacityRefreshError('timeout')
                threading.Event().wait(min(.02, max(0, deadline - time.monotonic())))
            authorized()
            if time.monotonic() >= deadline:
                raise CapacityRefreshError('timeout')
            raw = worker.stdout.read(4097)
            if len(raw) > 4096 or worker.returncode != 0:
                raise CapacityRefreshError('probe_failed')
            result = json.loads(raw)
            if type(result) is not dict or type(result.get('ok')) is not bool:
                raise ValueError()
            if not result['ok']:
                reason = result.get('reason')
                if reason not in ('timeout', 'identity_changed', 'unavailable', 'invalid_measurement', 'probe_failed'):
                    reason = 'probe_failed'
                raise CapacityRefreshError(reason)
            values = result.get('values')
            if type(values) is not dict or set(values) != {'total_bytes', 'used_bytes', 'usage_percent'}:
                raise ValueError()
            total, used, percent = (values[key] for key in ('total_bytes', 'used_bytes', 'usage_percent'))
            if (type(total) is not int or type(used) is not int or not 0 < total <= 2**63 - 1
                    or not 0 <= used <= total or type(percent) not in (int, float)
                    or not math.isfinite(percent) or percent != used / total * 100):
                raise ValueError()
            marker, ancillary, flags, _ = channel.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize))
            handles = array.array('i')
            for level, kind, payload in ancillary:
                if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
                    handles.frombytes(payload[:len(payload) - len(payload) % handles.itemsize])
            if (marker != b'1' or flags or len(ancillary) != 1 or len(handles) != 1
                    or ancillary[0][:2] != (socket.SOL_SOCKET, socket.SCM_RIGHTS)):
                for handle in handles:
                    os.close(handle)
                raise ValueError()
            anchor = handles[0]
            os.set_inheritable(anchor, False)
            if _capacity_fd_mount(anchor, self.proc_root) != entry['mount_id']:
                raise CapacityRefreshError('identity_changed')
            authorized()
            if time.monotonic() >= deadline:
                raise CapacityRefreshError('timeout')
            transferred = True
            return values, anchor
        except (ValueError, TypeError, UnicodeError, OSError):
            raise CapacityRefreshError('probe_failed') from None
        finally:
            channel.close()
            child_channel.close()
            if anchor is not None and not transferred:
                os.close(anchor)
            if worker.poll() is None:
                try:
                    worker.kill()
                except OSError:
                    pass
            if worker.poll() is not None:
                for stream in (worker.stdin, worker.stdout):
                    if stream is not None:
                        stream.close()
                with self._capacity_lock:
                    if self._capacity_worker is worker:
                        self._capacity_worker = None
            else:
                self._capacity_schedule_reap(worker)

    def _capacity_schedule_reap(self, worker):
        """One daemon waits for an already-killed worker; no sampling or retry."""
        def reap():
            try:
                worker.wait()
            except OSError:
                return
            finally:
                with self._capacity_lock:
                    if worker.poll() is not None:
                        for stream in (worker.stdin, worker.stdout):
                            if stream is not None:
                                stream.close()
                        if self._capacity_worker is worker:
                            self._capacity_worker = None
                    if self._capacity_reaper is threading.current_thread():
                        self._capacity_reaper = None
        with self._capacity_lock:
            if self._capacity_reaper is None:
                reaper = None
                try:
                    reaper = threading.Thread(target=reap, name='r1-capacity-reap', daemon=True)
                    self._capacity_reaper = reaper
                    reaper.start()
                except RuntimeError:
                    # Thread resource exhaustion must not leave a permanent
                    # busy sentinel. A live worker still prevents overlap;
                    # passive snapshots can reap it after its real exit.
                    if self._capacity_reaper is reaper:
                        self._capacity_reaper = None
                    self._capacity_reap_exited()

    def _capacity_reap_exited(self):
        """Nonblocking waitpid/pipe cleanup only; never probes a filesystem."""
        with self._capacity_lock:
            worker = self._capacity_worker
            if self._capacity_reaper is not None or worker is None or worker.poll() is None:
                return
            for stream in (worker.stdin, worker.stdout):
                if stream is not None:
                    stream.close()
            self._capacity_worker = None

    def refresh_capacity(self, *, authorizer=None):
        return self._refresh_capacity(authorizer=authorizer)

    def refresh_capacity_missing(self, *, authorizer=None, eligibility=None):
        """Foreground fill only: active same-lifetime disks and missing cache."""
        return self._refresh_capacity(authorizer=authorizer, eligibility=eligibility, missing=True)

    def refresh_capacity_visible(self, *, authorizer=None, eligibility=None):
        """Remeasure positive cache only with this visible batch's power proof."""
        return self._refresh_capacity(authorizer=authorizer, eligibility=eligibility, missing=True, visible=True)

    def _refresh_capacity(self, *, authorizer=None, eligibility=None, missing=False, visible=False):
        source = 'visible_refresh' if visible else 'missing_fill' if missing else 'explicit_refresh'
        with self._capacity_scope(source, 'refresh_begin'):
            try:
                return self._capacity_refresh_batch(authorizer=authorizer, eligibility=eligibility,
                                                    missing=missing, visible=visible)
            except CapacityRefreshError as error:
                with self._capacity_lock:
                    self._capacity_note('batch_' + error.reason)
                raise
            finally:
                with self._capacity_lock:
                    self._capacity_note('batch_finished')

    def _capacity_refresh_batch(self, *, authorizer=None, eligibility=None, missing=False, visible=False):
        """One explicit owner batch; only verified same-lifetime results commit."""
        if not self._capacity_refresh_lock.acquire(blocking=False):
            raise CapacityRefreshError('busy')
        try:
            return self._capacity_refresh_acquired(authorizer=authorizer, eligibility=eligibility,
                                                   missing=missing, visible=visible)
        finally:
            self._capacity_refresh_lock.release()

    def _capacity_refresh_acquired(self, *, authorizer=None, eligibility=None, missing=False, visible=False):
        """Refresh-lock owner only; retain the original deadline and cleanup."""
        started, deadline = time.time(), time.monotonic() + _CAPACITY_WINDOW
        denied, pending, negative, results, history, committed = False, {}, {}, [], {}, False
        mount_generation, batch_changed = None, False
        with self._capacity_lock:
            epoch = self._capacity_epoch
            self._capacity_observe(lambda: setattr(self._capacity_context, 'pending', pending))
            self._capacity_pending_records = pending
            self._capacity_note('batch_started')

        def authorized():
            nonlocal denied
            if denied:
                raise CapacityRefreshError('authorization_denied')
            try:
                valid = authorizer is not None and bool(authorizer())
            except Exception:
                valid = False
            if not valid:
                denied = True
                raise CapacityRefreshError('authorization_denied')
            with self._capacity_lock:
                if self._capacity_closed or epoch != self._capacity_epoch:
                    self._capacity_note('pending_lifecycle_changed')
                    raise CapacityRefreshError('unavailable')

        def permitted(entry):
            authorized()
            try:
                reason = eligibility(entry['_device_name']) if callable(eligibility) else 'power_state_unverified'
            except Exception:
                reason = 'power_state_unverified'
            authorized()
            return reason if reason in (None, 'standby') else 'power_state_unverified'

        def skipped(row, reason):
            row.update(status='skipped_standby' if reason == 'standby' else 'unavailable',
                       reason=reason, sampled_at=history.get(row['mount_id']) if visible else None)

        def close_pending(record):
            anchor = record.pop('anchor', None)
            if anchor is not None:
                os.close(anchor)

        def check_batch():
            authorized()
            with self._capacity_lock:
                self._capacity_check_watch()
                if mount_generation != self._capacity_mount_epoch or self._capacity_watch is None:
                    raise CapacityRefreshError('identity_changed')

        def observed(entries, complete):
            nonlocal batch_changed
            with self._capacity_lock:
                self._capacity_observe(lambda: setattr(self._capacity_context, 'inventory', getattr(entries, 'observation', None)))
                self._capacity_check_watch()
            active = self._capacity_reconcile(entries, complete)
            batch_changed = batch_changed or (mount_generation is not None and (
                mount_generation != self._capacity_mount_epoch or self._capacity_watch is None))
            for row in results:
                record = pending.get(row['mount_id'])
                if record is not None and (batch_changed or active.get(row['mount_id']) != record['key']):
                    close_pending(pending.pop(row['mount_id']))
                    row.update(status='identity_changed', reason='identity_changed', sampled_at=None)
                if visible and row['mount_id'] in history and batch_changed:
                    row.update(status='identity_changed', reason='identity_changed', sampled_at=None)
            return active

        try:
            authorized()
            with self._capacity_lock:
                self._capacity_check_watch(create=True)
            if missing:
                self.capacity_snapshot()
                authorized()
            with self._capacity_lock:
                if self._capacity_reaper is not None:
                    raise CapacityRefreshError('busy')
                worker = self._capacity_worker
                if worker is not None:
                    if worker.poll() is None:
                        raise CapacityRefreshError('busy')
                    for stream in (worker.stdin, worker.stdout):
                        if stream is not None:
                            stream.close()
                    self._capacity_worker = None
            entries, complete = self._capacity_inventory_observe()
            observed(entries, complete)
            authorized()
            if not complete or self._capacity_watch is None:
                with self._capacity_lock:
                    self._capacity_replace_cache({}, reason='scan_failed')
                raise CapacityRefreshError('unavailable')
            mount_generation = self._capacity_mount_epoch
            for entry in entries:
                self._capacity_observe(lambda: setattr(self._capacity_context, 'stage', 'measure_entry'))
                authorized()
                if time.monotonic() >= deadline:
                    raise CapacityRefreshError('timeout')
                row = {'mount_id': entry['mount_id'], 'status': 'unavailable',
                       'reason': entry['_reason'], 'sampled_at': None}
                results.append(row)
                if batch_changed:
                    row.update(status='identity_changed', reason='identity_changed')
                    continue
                if not entry['_key']:
                    if entry['_reason'] == 'identity_changed':
                        row['status'] = 'identity_changed'
                    continue
                if missing:
                    with self._capacity_lock:
                        cached = self._capacity_cache.get(entry['mount_id'])
                        failure = self._capacity_negative.get(entry['_key'])
                    if visible and cached is not None and cached['key'] == entry['_key']:
                        history[entry['mount_id']] = cached['sampled_at']
                    if not visible and cached is not None and cached['key'] == entry['_key']:
                        row.update(status='cached', reason=None, sampled_at=cached['sampled_at'])
                        continue
                    reason = permitted(entry)
                    if reason is not None:
                        skipped(row, reason)
                        continue
                    if failure is not None and failure[0] > time.monotonic():
                        row.update(status=failure[1], reason=failure[2],
                                   sampled_at=history.get(entry['mount_id'], failure[3]) if visible else failure[3])
                        continue
                try:
                    values, anchor = self._capacity_probe(entry, deadline, check_batch)
                    # Register ownership immediately, so a revoked/changed
                    # source also closes the freshly transferred mount pin.
                    pending[entry['mount_id']] = {'key': entry['_key'], 'values': values,
                                                  'anchor': anchor, 'sampled_at': None}
                    with self._capacity_lock:
                        self._capacity_note('pending_added')
                    authorized()
                    current, valid = self._capacity_inventory_observe()
                    observed(current, valid)
                    authorized()
                    if batch_changed or not valid or not any(item['mount_id'] == entry['mount_id']
                                            and item['_key'] == entry['_key'] for item in current):
                        raise CapacityRefreshError('identity_changed')
                    if time.monotonic() >= deadline:
                        raise CapacityRefreshError('timeout')
                    if missing:
                        reason = permitted(entry)
                        if reason is not None:
                            close_pending(pending.pop(entry['mount_id']))
                            skipped(row, reason)
                            continue
                    sampled_at = time.time()
                    pending[entry['mount_id']]['sampled_at'] = sampled_at
                    row.update(status='refreshed', reason=None, sampled_at=sampled_at)
                except CapacityRefreshError as error:
                    with self._capacity_lock:
                        self._capacity_note('probe_' + error.reason)
                    record = pending.pop(entry['mount_id'], None)
                    if record is not None:
                        close_pending(record)
                    if error.reason == 'identity_changed':
                        with self._capacity_lock:
                            self._capacity_replace_cache({key: value for key, value in self._capacity_cache.items()
                                                          if key != entry['mount_id']}, reason='row_identity_changed')
                    if error.reason in ('authorization_denied', 'timeout', 'busy') or (error.reason == 'unavailable' and not missing):
                        raise
                    row.update(status='identity_changed' if error.reason == 'identity_changed' else
                               ('unavailable' if error.reason == 'unavailable' else 'failed'),
                               reason=error.reason,
                               sampled_at=history.get(entry['mount_id']) if visible and error.reason != 'identity_changed' else None)
                    if missing and error.reason != 'identity_changed':
                        negative[entry['_key']] = (time.monotonic() + 30, row['status'], row['reason'], row['sampled_at'])
            authorized()
            self._capacity_observe(lambda: setattr(self._capacity_context, 'stage', 'final_inventory'))
            with self._capacity_lock:
                current, complete = self._capacity_inventory_observe()
                active = observed(current, complete)
                authorized()
                if time.monotonic() >= deadline:
                    raise CapacityRefreshError('timeout')
                for row in results:
                    record = pending.get(row['mount_id'])
                    entry = next(item for item in entries if item['mount_id'] == row['mount_id'])
                    if record is not None and missing:
                        reason = permitted(entry)
                        if reason is not None:
                            close_pending(pending.pop(row['mount_id']))
                            skipped(row, reason)
                            record = None
                    if record is not None and active.get(row['mount_id']) != record['key']:
                        close_pending(pending.pop(row['mount_id']))
                        row.update(status='identity_changed', reason='identity_changed', sampled_at=None)
                    if (row['status'] == 'cached' or visible and row['sampled_at'] is not None) and (
                            batch_changed or active.get(row['mount_id']) != entry['_key']):
                        row.update(status='identity_changed', reason='identity_changed', sampled_at=None)
                # Real filesystem handles are strictly temporary. Release
                # before commit, then prove no namespace event/identity change.
                for record in pending.values():
                    close_pending(record)
                authorized()
                current, complete = self._capacity_inventory_observe()
                active = observed(current, complete)
                authorized()
                retained = {key: value for key, value in self._capacity_cache.items()
                            if active.get(key) == value['key']}
                retained.update(pending)
                authorized()
                if time.monotonic() >= deadline:
                    raise CapacityRefreshError('timeout')
                previous_cache = dict(self._capacity_cache)
                self._capacity_observe(lambda: setattr(self._capacity_context, 'stage', 'commit_prepare'))
                self._capacity_replace_cache(retained, reason='commit_prepare')
                try:
                    # Recheck after publication while readers remain locked
                    # out; failed grants discard all values from this batch.
                    authorized()
                    self._capacity_check_watch()
                    if not batch_changed and (mount_generation != self._capacity_mount_epoch or self._capacity_watch is None):
                        raise CapacityRefreshError('unavailable')
                    if time.monotonic() >= deadline:
                        raise CapacityRefreshError('timeout')
                except CapacityRefreshError as error:
                    self._capacity_check_watch()
                    historical = ({key: value for key, value in previous_cache.items()
                                   if active.get(key) == value['key']}
                                  if self._capacity_watch is not None
                                  and mount_generation == self._capacity_mount_epoch else {})
                    self._capacity_replace_cache(historical, reason='rollback_' + error.reason)
                    pending.clear()
                    raise
                committed = True
                self._capacity_pending = 0
                self._capacity_pending_records = None
                self._capacity_observe(lambda: setattr(self._capacity_context, 'pending', None))
                self._capacity_observe(lambda: setattr(self._capacity_context, 'stage', 'commit_complete'))
                if pending:
                    self._capacity_note('commit_success', commit=True)
                else:
                    self._capacity_note('batch_no_new_values')
                if not batch_changed:
                    self._capacity_negative.update({key: value for key, value in negative.items() if key in set(active.values())})
            successful = ('refreshed', 'cached', 'skipped_standby') if missing else ('refreshed', 'cached')
            return {'ok': True, 'status': 'complete' if all(row['status'] in successful for row in results) else 'partial',
                    'started_at': started, 'finished_at': time.time(), 'results': results}
        except CapacityRefreshError as error:
            with self._capacity_lock:
                self._capacity_note('pending_rejected_' + error.reason)
            raise
        except Exception:
            with self._capacity_lock:
                self._capacity_note('batch_exception')
            raise
        finally:
            if not committed:
                for record in pending.values():
                    close_pending(record)
            with self._capacity_lock:
                self._capacity_pending = 0
                self._capacity_pending_records = None
                self._capacity_observe(lambda: setattr(self._capacity_context, 'pending', None))
                if not committed:
                    self._capacity_note('pending_discarded')

    def sample(self):
        status = empty_status()
        ticks = read_cpu_ticks(self.proc_root)
        if ticks is not None and self.previous_cpu is not None:
            total, idle = ticks[0] - self.previous_cpu[0], ticks[1] - self.previous_cpu[1]
            if total > 0 and 0 <= idle <= total:
                status["cpu"]["usage_percent"] = (total - idle) / total * 100
        self.previous_cpu = ticks
        status["cpu"]["temp_c"] = read_cpu_temperature(self.sys_root)
        status["memory"] = read_memory(self.proc_root)
        status["uptime_seconds"] = read_uptime(self.proc_root)
        try:
            status["hostname"] = socket.gethostname()
        except OSError:
            pass
        iface, now = default_interface(self.proc_root), self.clock()
        counters = read_network_counters(self.sys_root, iface)
        status["network"].update(interface=iface, ipv4=interface_ipv4(iface))
        if counters is not None and self.previous_network is not None:
            old_iface, old_rx, old_tx, old_time = self.previous_network
            elapsed = now - old_time
            if iface == old_iface and elapsed > 0 and counters[0] >= old_rx and counters[1] >= old_tx:
                status["network"].update(
                    rx_bytes_per_sec=(counters[0] - old_rx) / elapsed,
                    tx_bytes_per_sec=(counters[1] - old_tx) / elapsed,
                )
        self.previous_network = (iface, *counters, now) if counters is not None else None
        with self._capacity_scope('sample', 'projection'):
            status["disks"] = self.capacity_snapshot()
        status["available"] = ticks is not None or status["memory"]["total_bytes"] is not None or status["uptime_seconds"] is not None
        return status


class StatusMonitor:
    def __init__(self, sampler=None, interval=SAMPLE_INTERVAL):
        self.sampler = sampler or StatusSampler()
        self.interval = interval
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.status = empty_status()
        self.updated_at = None
        self._capacity_heartbeat = {'result': 'not_run', 'sampled_at': None, 'sampled_mono': None}
        self._capacity_projection_state = {'result': 'not_run', 'sampled_mono': None}
        self._capacity_projection_local = threading.local()
        self._capacity_projection_seq = 0
        self._capacity_projection_operation = 0
        self._capacity_projection_events = []
        self._capacity_projection_dropped = 0
        self._capacity_projection_first_loss = None
        self._capacity_projection_last_positive = None
        self.thread = threading.Thread(target=self._run, name="r1-metrics", daemon=True)

    def _run(self):
        while not self.stopped.is_set():
            try:
                status = self.sampler.sample()
                result = 'sampled'
            except Exception as error:
                print("[WARN] 数据采样失败：" + str(error), flush=True)
                status = empty_status()
                result = 'sample_error'
            with self.lock:
                self.status, self.updated_at = status, time.monotonic()
                self._capacity_heartbeat = {'result': result, 'sampled_mono': self.updated_at,
                                            'sampled_at': time.time()}
            self.stopped.wait(self.interval)

    def snapshot(self):
        operation = None
        try:
            self._capacity_projection_local.returned = None
            with self.lock:
                self._capacity_projection_operation += 1
                operation = self._capacity_projection_operation
        except Exception:
            pass
        try:
            with self.lock:
                if self.updated_at is None or time.monotonic() - self.updated_at > max(6, self.interval * 3):
                    value = empty_status()
                    result = 'heartbeat_missing' if self.updated_at is None else 'heartbeat_stale'
                else:
                    value = copy.deepcopy(self.status)
                    result = 'heartbeat_fresh'
                self._capacity_projection_state = {'result': result, 'sampled_mono': time.monotonic(),
                                                  'heartbeat_mono': self.updated_at}
        except Exception:
            self._try_capacity_projection_note(None, 'snapshot_error', {'status': 'not_observed'}, operation)
            raise
        # Dated capacity has its own mount/lifetime proof, not the basic
        # heartbeat's freshness. Always revalidate it, including when the
        # basic sampler has stalled; this projection never measures capacity.
        project = getattr(self.sampler, 'capacity_snapshot', None)
        source = {'status': 'not_observed'}
        try:
            if callable(project):
                value['disks'] = project()
                try:
                    observe = getattr(self.sampler, 'capacity_projection_observation', None)
                    source = observe(value['disks']) if callable(observe) else source
                except Exception:
                    pass
        except Exception:
            self._try_capacity_projection_note(None, 'projection_error', source, operation)
            raise
        self._try_capacity_projection_note(value, 'returned' if callable(project) else 'not_observed',
                                           source, operation)
        return value

    def _try_capacity_projection_note(self, value, result, source, operation):
        """Observation failure is independent from the original business result."""
        try:
            self._capacity_projection_note(value, result, source, operation)
        except Exception:
            pass

    def _capacity_projection_note(self, value, result, source, operation):
        rows = value.get('disks') if isinstance(value, dict) else None
        evidence = _capacity_rows_evidence(rows)
        verified = isinstance(source, dict) and source.get('status') == 'observed'
        with self.lock:
            self._capacity_projection_seq += 1
            event = dict(evidence, status='observed', sequence=self._capacity_projection_seq,
                operation_id=operation, sampled_monotonic=time.monotonic(), sampled_at=time.time(),
                result=result, capacity_epoch=source.get('capacity_epoch') if verified else 'not_observed',
                mount_epoch=source.get('mount_epoch') if verified else 'not_observed',
                capacity_generation=source.get('generation') if verified else 'not_observed',
                sampler_projection_seq=source.get('sequence') if verified else 'not_observed',
                atomic_with_capacity=verified and source.get('atomic_with_capacity') is True,
                atomic_scope='sampler_projection_rows' if verified else 'not_observed')
            previous = self._capacity_projection_last_positive
            if evidence['measured_count'] is not None and evidence['measured_count'] > 0:
                if (previous is not None and event['capacity_generation'] != 'not_observed'
                        and event['capacity_generation'] != previous['capacity_generation']):
                    self._capacity_projection_first_loss = None
                self._capacity_projection_last_positive = event
            elif previous is not None and self._capacity_projection_first_loss is None:
                self._capacity_projection_first_loss = {'before': copy.deepcopy(previous), 'after': copy.deepcopy(event)}
            self._capacity_projection_events.append(event)
            if len(self._capacity_projection_events) > 16:
                self._capacity_projection_events.pop(0)
                self._capacity_projection_dropped += 1
            if value is not None:
                self._capacity_projection_local.returned = (value, event)

    def capacity_projection_observation(self, value):
        """Do not associate a different call's latest observation with this value."""
        pair = getattr(self._capacity_projection_local, 'returned', None)
        if pair is not None and pair[0] is value:
            return copy.deepcopy(pair[1])
        return {'status': 'not_observed'}

    def capacity_cache_diagnostics(self):
        """Two independently locked RAM views, explicitly not an atomic sample."""
        with self.lock:
            heartbeat = copy.deepcopy({'last_sample': self._capacity_heartbeat,
                'last_projection': self._capacity_projection_state,
                'updated_mono': self.updated_at, 'interval_seconds': self.interval,
                'basic_available': self.status.get('available') is True,
                'projection': {'status': 'observed' if self._capacity_projection_events else 'not_observed',
                    'events': self._capacity_projection_events, 'ring_limit': 16,
                    'latest': self._capacity_projection_events[-1] if self._capacity_projection_events
                              else {'status': 'not_observed'},
                    'dropped_events': self._capacity_projection_dropped,
                    'first_loss': self._capacity_projection_first_loss}})
        value = self.sampler.capacity_cache_diagnostics()
        value['monitor'] = dict(heartbeat, atomic_with_capacity=False, read_order=['monitor', 'capacity'])
        return value

    def refresh_capacity(self, *, authorizer=None):
        return self.sampler.refresh_capacity(authorizer=authorizer)

    def refresh_capacity_missing(self, *, authorizer=None, eligibility=None):
        return self.sampler.refresh_capacity_missing(authorizer=authorizer, eligibility=eligibility)

    def refresh_capacity_visible(self, *, authorizer=None, eligibility=None):
        return self.sampler.refresh_capacity_visible(authorizer=authorizer, eligibility=eligibility)

    def start(self):
        start = getattr(self.sampler, 'start_capacity', None)
        if callable(start):
            start()
        self.thread.start()

    def stop(self):
        self.stopped.set()
        close = getattr(self.sampler, 'close_capacity', None)
        if callable(close):
            close()
        self.thread.join(timeout=0.25)


def allowed_origin(origin, port):
    if origin is None or origin == "null":
        return True
    try:
        parsed = urllib.parse.urlsplit(origin)
        return (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
            and parsed.port == port and not parsed.username and not parsed.password
            and not parsed.path and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False


def make_http_server(monitor, port=PORT, controller=None, insights=None, control_token=None, web_config=None,
                     fan_policy=None, light_policy=None, monitoring=None):
    class StatusHandler(http.server.BaseHTTPRequestHandler):
        def request_origin(self):
            origin = self.headers.get("Origin")
            try:
                host = urllib.parse.urlsplit("http://" + self.headers.get("Host", ""))
                valid_host = (host.hostname in ("127.0.0.1", "localhost", "::1")
                              and host.port == self.server.server_port and not host.username
                              and not host.password and not host.path and not host.query and not host.fragment)
            except ValueError:
                valid_host = False
            if not valid_host or not allowed_origin(origin, self.server.server_port):
                self.send_json(403, {"error": "请求来源不允许"})
                return False, None
            return True, origin

        def do_GET(self):
            valid, origin = self.request_origin()
            if not valid:
                return
            route = urllib.parse.urlsplit(self.path).path
            if route == "/api/status":
                value = monitor.snapshot()
            elif route == "/api/toolbox" and controller is not None:
                value = controller.snapshot()
            elif route == "/api/insights" and insights is not None:
                value = insights.snapshot()
            elif route == "/api/web-config":
                value = web_config.snapshot() if web_config is not None else {
                    "enabled": False, "reason": "网页配置未启用", "addresses": []}
            elif route == "/api/fan-policy" and fan_policy is not None:
                value = fan_policy.snapshot()
            elif route == "/api/light-policy" and light_policy is not None:
                value = light_policy.snapshot()
            elif route == "/api/monitoring" and monitoring is not None:
                value = monitoring.snapshot()
            elif route == "/api/history" and monitoring is not None:
                try:
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True, max_num_fields=4)
                except ValueError:
                    self.send_json(400, {"error": "查询参数无效"}, origin)
                    return
                if set(query) - {"range"} or any(len(values) != 1 for values in query.values()):
                    self.send_json(400, {"error": "查询参数无效"}, origin)
                    return
                span = query.get("range", ["1h"])[0]
                if span not in ("10m", "1h", "6h", "24h", "7d", "30d"):
                    self.send_json(400, {"error": "时间范围无效"}, origin)
                    return
                value = monitoring.history(span)
            else:
                self.send_json(404, {"error": "接口不存在"}, origin)
                return
            self.send_json(200, value, origin)

        def do_POST(self):
            if urllib.parse.urlsplit(self.path).path == '/api/storage/fill':
                self.storage_fill()
                return
            if controller is None or not control_token:
                self.reject_method()
                return
            valid, origin = self.request_origin()
            if not valid:
                return
            if urllib.parse.urlsplit(self.path).path != "/api/action":
                self.send_json(404, {"error": "接口不存在"}, origin)
                return
            supplied = self.headers.get("X-R1-Token", "")
            if not secrets.compare_digest(supplied.encode("utf-8"), control_token.encode("utf-8")):
                self.send_json(403, {"ok": False, "error": "控制凭据无效，请在小屏操作"}, origin)
                return
            length = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") or not length.isascii() or not length.isdigit() or len(length) > 5 or not 0 < int(length) <= 16384:
                self.send_json(413, {"ok": False, "error": "请求内容过大或长度无效"}, origin)
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self.send_json(415, {"ok": False, "error": "控制请求需要 JSON"}, origin)
                return
            try:
                def unique_fields(pairs):
                    value = {}
                    for key, item in pairs:
                        if key in value:
                            raise ValueError("重复字段")
                        value[key] = item
                    return value
                payload = json.loads(self.rfile.read(int(length)), object_pairs_hook=unique_fields)
                if not isinstance(payload, dict):
                    raise ValueError("需要对象")
                action = payload.pop("action", None)
                if action in ("power.request", "container.action", "vm.action", "fan.official") and payload.get("confirmed") is not True:
                    raise ValueError("此操作需要在界面明确确认")
                if not isinstance(action, str):
                    raise ValueError("缺少操作名称")
            except (ValueError, UnicodeError):
                self.send_json(400, {"ok": False, "error": "请求格式无效或缺少明确确认"}, origin)
                return
            if action == "web.pairing":
                if web_config is None:
                    self.send_json(400, {"ok": False, "error": "网页配置未启用"}, origin)
                elif set(payload) - {"rotate"} or type(payload.get("rotate", False)) is not bool:
                    self.send_json(400, {"ok": False, "error": "配对参数无效"}, origin)
                else:
                    try:
                        value = web_config.pairing_info(rotate=payload.get("rotate", False))
                        self.send_json(200, {"ok": True, "web": value}, origin)
                    except Exception:
                        self.send_json(400, {"ok": False, "error": "网页配对暂不可用，请检查网页服务状态"}, origin)
                return
            policy_actions = ("fan.policy.update", "fan.policy.release", "fan.policy.quick")
            if action in policy_actions or (action == "fan.official" and fan_policy is not None):
                if fan_policy is None:
                    self.send_json(400, {"ok": False, "error": "独立温控服务未启用"}, origin)
                else:
                    value = fan_policy.action(action, payload)
                    self.send_json(200 if value.get("ok") else 400, value, origin)
                return
            if action in ("light.policy.update", "light.policy.quick", "monitor.settings.update"):
                destination = light_policy if action in ("light.policy.update", "light.policy.quick") else monitoring
                if destination is None:
                    self.send_json(400, {"ok": False, "error": "配置服务未启用"}, origin)
                else:
                    value = destination.action(action, payload)
                    self.send_json(200 if value.get("ok") else 400, value, origin)
                return
            if action in ("fan.set", "fan.restore") and fan_policy is not None:
                state = fan_policy.snapshot()
                if state.get("active") or state.get("busy") or state.get("recovery_confirmed") is False:
                    self.send_json(409, {"ok": False, "error": "请先恢复原厂管理，再使用原厂固定档位或其他风扇控制"}, origin)
                    return
            allowed_controls = {"settings.update", "screen.brightness", "screen.sleep", "screen.wake",
                                "screen.activity", "fan.set", "fan.restore", "fan.official", "rgb.set", "rgb.probe", "power.request", "power.cancel"}
            if action in allowed_controls:
                destination = controller
            elif action in ("container.action", "vm.action") and insights is not None:
                destination = insights
            else:
                self.send_json(400, {"ok": False, "error": "不支持此操作"}, origin)
                return
            try:
                value = destination.action(action, payload)
                if not isinstance(value, dict) or type(value.get("ok")) is not bool:
                    raise ValueError("控制器返回格式错误")
                if action in ("container.action", "vm.action") and value["ok"]:
                    value = {**value, "operation": value.get("state"),
                             "state": controller.snapshot(), "insights": insights.snapshot()}
                self.send_json(200 if value["ok"] else 400, value, origin)
            except Exception:
                # Keep privileged command output and paths out of the browser.
                self.send_json(500, {"ok": False, "error": "操作未完成，请检查宿主服务日志"}, origin)

        def storage_fill(self):
            """Screen-only explicit fill; never reuse owner/Bearer authority."""
            valid, origin = self.request_origin()
            if not valid:
                return
            supplied = self.headers.get('X-R1-Token', '')
            if (origin is None or self.headers.get('Authorization') is not None or not control_token
                    or not secrets.compare_digest(supplied.encode('utf8'), control_token.encode('utf8'))):
                self.send_json(403, {'ok': False, 'reason': 'authorization_denied'}, origin)
                return
            if self.path != '/api/storage/fill':
                self.send_json(400, {'ok': False, 'reason': 'invalid_request'}, origin)
                return
            if web_config is None or not callable(getattr(web_config, 'storage_fill', None)):
                self.send_json(503, {'ok': False, 'reason': 'unavailable'}, origin)
                return
            length = self.headers.get('Content-Length', '')
            if (self.headers.get('Transfer-Encoding') is not None or not length.isascii()
                    or not length.isdigit() or len(length) > 2 or not 0 < int(length) <= 64):
                self.send_json(400, {'ok': False, 'reason': 'invalid_request'}, origin)
                return
            if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                self.send_json(415, {'ok': False, 'reason': 'invalid_request'}, origin)
                return
            try:
                payload = json.loads(self.rfile.read(int(length)))
                if type(payload) is not dict or payload:
                    raise ValueError()
            except (ValueError, UnicodeError):
                self.send_json(400, {'ok': False, 'reason': 'invalid_request'}, origin)
                return
            with self.server.storage_guard:
                epoch = self.server.storage_epoch
            denied = False
            def current():
                nonlocal denied
                with self.server.storage_guard:
                    active = not self.server.storage_closed and epoch == self.server.storage_epoch
                stopped = getattr(monitor, 'stopped', None)
                active = active and not (stopped is not None and stopped.is_set())
                denied = denied or not active
                return not denied
            try:
                if not current():
                    raise CapacityRefreshError('authorization_denied')
                value = web_config.storage_fill(authorizer=current)
                if not current():
                    raise CapacityRefreshError('authorization_denied')
                self.send_json(200, value, origin, authorizer=current)
            except Exception as error:
                reason = getattr(error, 'reason', 'unavailable')
                if reason not in ('authorization_denied', 'busy', 'timeout', 'unavailable'):
                    reason = 'unavailable'
                self.send_json(403 if reason == 'authorization_denied' else 409 if reason == 'busy' else 503,
                               {'ok': False, 'reason': reason}, origin)

        def send_json(self, code, value, origin=None, *, authorizer=None):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if authorizer is not None and not authorizer():
                self.send_json(403, {'ok': False, 'reason': 'authorization_denied'}, origin)
                return
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Vary", "Origin")
            if origin is not None and allowed_origin(origin, self.server.server_port):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def reject_method(self):
            self.send_json(405, {"error": "only GET is supported"})

        def do_OPTIONS(self):
            # Newer Chromium versions can preflight file:// -> loopback reads.
            # Grant only this read route, never a control method or extra headers.
            valid, origin = self.request_origin()
            if not valid:
                return
            route = urllib.parse.urlsplit(self.path).path
            method = self.headers.get("Access-Control-Request-Method")
            headers = {part.strip().lower() for part in self.headers.get("Access-Control-Request-Headers", "").split(",") if part.strip()}
            can_read = route in ("/api/status", "/api/toolbox", "/api/insights", "/api/web-config", "/api/fan-policy",
                                 "/api/light-policy", "/api/monitoring", "/api/history") and method == "GET" and not headers
            can_control = ((route == "/api/action" and controller is not None
                            or route == '/api/storage/fill' and web_config is not None) and control_token
                           and method == "POST" and headers <= {"content-type", "x-r1-token"})
            if (
                origin is None or not allowed_origin(origin, self.server.server_port)
                or not (can_read or can_control)
            ):
                self.send_json(403, {"error": "preflight not allowed"})
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST" if can_control else "GET")
            if can_control:
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-R1-Token")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        do_PUT = do_PATCH = do_DELETE = do_HEAD = reject_method

        def log_message(self, format_string, *args):
            pass

    class LocalServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def __init__(self, *args, **kwargs):
            self.storage_guard = threading.RLock()
            self.storage_epoch, self.storage_closed = 0, False
            super().__init__(*args, **kwargs)

        def _close_storage(self):
            with self.storage_guard:
                self.storage_closed = True
                self.storage_epoch += 1

        def shutdown(self):
            self._close_storage()
            super().shutdown()

        def server_close(self):
            self._close_storage()
            super().server_close()

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(5)
            return connection, address

    return LocalServer((HOST, port), StatusHandler)


def bind_server(monitor, port, stopped, timeout=20, **options):
    deadline = time.monotonic() + timeout
    while not stopped.is_set():
        try:
            return make_http_server(monitor, port, **options)
        except OSError as error:
            remaining = deadline - time.monotonic()
            if error.errno != errno.EADDRINUSE or remaining <= 0:
                raise
            stopped.wait(min(0.2, remaining))
    return None


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    try:
        child.terminate()
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except ProcessLookupError:
            return
        child.wait(timeout=1)


def create_fan_policy(controller, insights):
    from fan_native import FanNativeAdapter
    from fan_policy import FanPolicyController
    from temperature_sources import TemperatureSources
    native = FanNativeAdapter(controller.state_dir, "/run/r1-toolbox/fan-control",
                              guard_path=Path(__file__).resolve().parent / "fan_guard.so",
                              histor=controller.histor, allow_writes=True)
    return FanPolicyController(controller.state_dir, TemperatureSources(insights), native,
                               histor=controller.histor, smart_collector=insights)


def create_light_policy(controller, fan_policy):
    from light_policy import LightPolicyController
    return LightPolicyController(controller, fan_policy.sources, state_dir=controller.state_dir)


def create_monitoring(controller, insights, monitor, fan_policy):
    from monitoring import MonitoringService
    return MonitoringService(controller.state_dir, insights, monitor, fan_policy.sources, fan_policy=fan_policy)


def create_web_config(controller, insights, fan_policy=None, *, light_policy=None, monitoring=None, monitor=None):
    """Use a separate authenticated LAN endpoint; never share the Electron token."""
    raw_port = os.environ.get("R1_WEB_PORT", "18732")
    host = os.environ.get("R1_WEB_BIND", "0.0.0.0")
    if (host not in ("0.0.0.0", "127.0.0.1") or not raw_port.isascii()
            or not raw_port.isdigit() or len(raw_port) > 5
            or (int(raw_port) != 0 and not 1024 <= int(raw_port) <= 65535)):
        raise ValueError("网页监听配置无效")
    if int(raw_port) == 0:
        return None
    from web_config import WebConfigServer
    return WebConfigServer(controller, insights, fan_policy=fan_policy, light_policy=light_policy,
                           monitoring=monitoring, monitor=monitor, host=host, port=int(raw_port),
                           state_dir=getattr(controller, 'state_dir', None),
                           version=os.environ.get('R1_TOOLBOX_VERSION', 'dev'))


def run_service(app, port=PORT, controller=None, insights=None):
    app = Path(app).resolve()
    executable = app / "AppRun"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        print("[ERROR] AppRun 不存在或不可执行：" + str(executable), file=sys.stderr, flush=True)
        return 1
    # Companions live beside this verified script in the RAM bundle, including
    # when tests import ui_service.py without using it as the process entrypoint.
    runtime_dir = str(Path(__file__).resolve().parent)
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    if controller is None:
        from toolbox import ToolboxController
        from histor import HistorAdapter
        controller = ToolboxController(state_dir=os.environ.get("R1_TOOLBOX_DATA_DIR", "/run/r1-toolbox/settings"),
                                       histor=HistorAdapter(allow_writes=True))
    if insights is None:
        from insights import InsightsCollector
        insights = InsightsCollector()
    stopped, monitor = threading.Event(), StatusMonitor()
    control_token = secrets.token_hex(32)
    server = server_thread = child = web = fan = light = analytics = None
    handlers = {}

    def request_stop(signum, frame):
        stopped.set()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, request_stop)
        fan = create_fan_policy(controller, insights)
        try:
            light = create_light_policy(controller, fan)
        except Exception as error:
            print("[WARN] 灯光联动不可用：" + type(error).__name__, file=sys.stderr, flush=True)
        try:
            analytics = create_monitoring(controller, insights, monitor, fan)
        except Exception as error:
            print("[WARN] 历史统计不可用：" + type(error).__name__, file=sys.stderr, flush=True)
        try:
            web = create_web_config(controller, insights, fan_policy=fan, light_policy=light,
                                    monitoring=analytics, monitor=monitor)
        except (OSError, ValueError) as error:
            print("[WARN] 网页配置未启用：" + str(error), file=sys.stderr, flush=True)
        server = bind_server(monitor, port, stopped, controller=controller, insights=insights,
                             control_token=control_token, web_config=web, fan_policy=fan,
                             light_policy=light, monitoring=analytics)
        if server is None:
            return 0
        server_thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.1), name="r1-status-api", daemon=True
        )
        monitor.start()
        controller.start()
        insights.start()
        fan.start()
        for name, companion in (("灯光联动", light), ("历史统计", analytics)):
            if companion is None:
                continue
            try:
                companion.start()
            except Exception as error:
                # Optional monitoring or lighting must never hide the display.
                print("[WARN] " + name + "未启动：" + type(error).__name__, file=sys.stderr, flush=True)
        if web is not None:
            try:
                web.start()
                print("[OK] 局域网网页配置已启动；请在小屏生成配对码登录。", flush=True)
            except (OSError, ValueError) as error:
                # An occupied optional LAN port must not black out the screen.
                print("[WARN] 网页配置启动失败，小屏继续运行：" + str(error), file=sys.stderr, flush=True)
        server_thread.start()
        if stopped.is_set():
            return 0
        environment = dict(os.environ, APPDIR=str(app))
        environment["R1_TOOLBOX_CONTROL_TOKEN"] = control_token
        app_library_path = environment.pop("R1_TOOLBOX_APP_LD_LIBRARY_PATH", None)
        if app_library_path is not None:
            environment["LD_LIBRARY_PATH"] = app_library_path
        child = subprocess.Popen([str(executable), "--no-sandbox"], cwd=app, env=environment)
        print("[OK] 只读状态接口：http://" + HOST + ":" + str(server.server_port) + "/api/status", flush=True)
        while not stopped.wait(0.1):
            result = child.poll()
            if result is not None:
                return result if result >= 0 else 128 - result
        return 0
    except (OSError, ValueError) as error:
        print("[ERROR] 小屏服务启动失败：" + str(error), file=sys.stderr, flush=True)
        return 1
    finally:
        # Keep the port bound until hardware is restored, so a replacement
        # service cannot race the previous instance's cleanup.
        def stop_http():
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
                server_thread.join(timeout=0.5)
        cleanup = [("停止接收控制请求", stop_http)]
        if web is not None:
            cleanup.append(("停止网页配置", web.stop))
        if light is not None:
            cleanup.append(("恢复灯光接管", light.stop))
        if analytics is not None:
            cleanup.append(("保存历史统计", analytics.stop))
        if fan is not None:
            cleanup.append(("恢复原厂风扇管理", fan.stop))
        cleanup += [("恢复硬件", controller.stop),
                   ("停止小屏进程", lambda: stop_child(child)), ("停止详细采集", insights.stop)]
        if monitor.thread.is_alive():
            cleanup.append(("停止指标采集", monitor.stop))
        if server is not None:
            cleanup.append(("释放本地接口", server.server_close))
        for description, action in cleanup:
            try:
                action()
            except Exception as error:
                # One failure must not skip fan/screen restoration or leave
                # the remaining service resources alive.
                print("[WARN] " + description + "未完成：" + type(error).__name__, file=sys.stderr, flush=True)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def acquire_runtime_lock():
    """Hold one host-runtime lease across exec and all hardware cleanup."""
    if fcntl is None:
        raise OSError('常驻服务需要 Linux 文件锁')
    directory = Path('/run/r1-toolbox')
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise OSError('宿主运行目录身份或写权限不安全')
    path = directory / 'resident.lock'
    inherited = os.environ.pop('R1_TOOLBOX_RUNTIME_LOCK_FD', None)
    if inherited is not None:
        if not re.fullmatch(r'[0-9]{1,5}', inherited):
            raise OSError('常驻服务文件锁描述符无效')
        descriptor = int(inherited)
        actual, expected = os.fstat(descriptor), path.lstat()
        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError('常驻服务文件锁身份不匹配')
    else:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError('常驻服务文件锁权限不安全')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(descriptor, False)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def create_screen_manager(controller, frontend, control_token):
    from screen_manager import ScreenManager
    return ScreenManager(controller, frontend, control_token)


def run_resident_service(frontend=None, port=PORT, controller=None, insights=None, *, stop_event=None):
    """Run Web and hardware independently of the optional, switchable GUI."""
    runtime_dir = str(Path(__file__).resolve().parent)
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    stopped, monitor = stop_event or threading.Event(), StatusMonitor()
    server = server_thread = web = fan = light = analytics = manager = None
    descriptor, handlers = None, {}
    try:
        descriptor = acquire_runtime_lock()
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, lambda signum, frame: stopped.set())
        if controller is None:
            from toolbox import ToolboxController
            from histor import HistorAdapter
            controller = ToolboxController(state_dir=os.environ.get('R1_TOOLBOX_DATA_DIR', '/run/r1-toolbox/settings'),
                                           histor=HistorAdapter(allow_writes=True))
        if insights is None:
            from insights import InsightsCollector
            insights = InsightsCollector()
        controller.set_screen_managed(False)
        fan = create_fan_policy(controller, insights)
        for name, factory in (('灯光联动', lambda: create_light_policy(controller, fan)),
                              ('历史统计', lambda: create_monitoring(controller, insights, monitor, fan))):
            try:
                if name == '灯光联动':
                    light = factory()
                else:
                    analytics = factory()
            except Exception as error:
                print('[WARN] ' + name + '不可用：' + type(error).__name__, file=sys.stderr, flush=True)
        web = create_web_config(controller, insights, fan_policy=fan, light_policy=light,
                                monitoring=analytics, monitor=monitor)
        if web is None:
            raise ValueError('常驻模式需要启用 Web 服务，请设置有效的 R1_WEB_PORT')
        control_token = secrets.token_hex(32)
        manager = create_screen_manager(controller, frontend, control_token)
        server = bind_server(monitor, port, stopped, controller=controller, insights=insights,
                             control_token=control_token, web_config=web, fan_policy=fan,
                             light_policy=light, monitoring=analytics)
        if server is None:
            raise RuntimeError('本机状态接口未完成绑定，常驻服务未就绪')
        server_thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.1),
                                         name='r1-status-api', daemon=True)
        monitor.start()
        controller.start()
        insights.start()
        fan.start()
        for name, companion in (('灯光联动', light), ('历史统计', analytics)):
            if companion is not None:
                try:
                    companion.start()
                except Exception as error:
                    print('[WARN] ' + name + '未启动：' + type(error).__name__, file=sys.stderr, flush=True)
        # Web binding is required.  A failed listener must make Docker report a
        # failed runtime, not an apparently healthy keepalive container.
        web.start()
        server_thread.start()
        web.attach_screen_manager(manager)
        print('[OK] Web 常驻服务已启动；小屏与 MCP 按各自持久开关独立运行。', flush=True)
        while not stopped.wait(0.25):
            if not server_thread.is_alive():
                raise RuntimeError('本地状态接口已退出，常驻服务停止')
            web_thread = getattr(web, 'thread', None)
            if isinstance(web_thread, threading.Thread) and not web_thread.is_alive():
                raise RuntimeError('Web 服务已退出，常驻服务停止')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print('[ERROR] 常驻服务启动或运行失败：' + str(error), file=sys.stderr, flush=True)
        return 1
    finally:
        def stop_http():
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
                server_thread.join(timeout=1)
        cleanup = [('停止接收本机控制', stop_http)]
        if web is not None:
            cleanup.append(('停止 Web', web.stop))
        if manager is not None:
            cleanup.append(('恢复官方小屏', manager.stop))
        if light is not None:
            cleanup.append(('恢复灯光', light.stop))
        if analytics is not None:
            cleanup.append(('保存历史统计', analytics.stop))
        if fan is not None:
            cleanup.append(('恢复原厂风扇', fan.stop))
        if controller is not None:
            cleanup.append(('恢复硬件', controller.stop))
        if insights is not None:
            cleanup.append(('停止详细采集', insights.stop))
        if monitor.thread.is_alive():
            cleanup.append(('停止指标采集', monitor.stop))
        if server is not None:
            cleanup.append(('释放本机接口', server.server_close))
        for description, action in cleanup:
            try:
                action()
            except Exception as error:
                print('[WARN] ' + description + '未完成：' + type(error).__name__, file=sys.stderr, flush=True)
        if descriptor is not None:
            os.close(descriptor)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    if (sys.argv[1:] if argv is None else argv) == ['--capacity-worker']:
        return _capacity_worker_main()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=Path("/screen/squashfs-root"), help="宿主解包版 AppImage 目录")
    parser.add_argument("--port", type=int, default=PORT, help="本机只读状态端口（默认 18731）")
    parser.add_argument("--check", action="store_true", help="只读取一次指标，不启动小屏或 HTTP 服务")
    parser.add_argument('--resident', action='store_true', help='常驻运行 Web，独立管理可选小屏')
    parser.add_argument('--frontend', type=Path, help='已校验的小屏前端目录，仅在启用小屏时使用')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1–65535 范围内")
    if args.check:
        print(json.dumps(StatusSampler().sample(), ensure_ascii=False, allow_nan=False))
        return 0
    return run_resident_service(args.frontend, args.port) if args.resident else run_service(args.app, args.port)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build the deterministic, verified UI payload copied into the Toolbox image."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[1]
MAX_FILES = 512
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def build_bundle(root=ROOT, output=None, native_guard=None):
    root = Path(root)
    output = Path(output) if output else root / 'build'
    frontend = root / 'ui'
    if frontend.is_symlink() or not frontend.is_dir():
        raise ValueError('缺少 ui 目录，或该目录是符号链接')
    sources = {'frontend/index.html': frontend / 'index.html',
               **{'runtime/' + name: root / 'runtime' / name
                  for name in ('ui_service.py', 'toolbox.py', 'insights.py', 'disk_names.py', 'device_info.py', 'vendor_device.py', 'histor.py', 'web_config.py',
                               'fnm_auth.py', 'fnm_compat.py', 'fan_diagnostics.py', 'dev_diagnostics.py', 'screen_diagnostics.py', 'gpu_probe.py', 'disk_cache.py',
                               'vendor_media_auth.py', 'vendor_media_probe.py', 'vendor_media_batch.py', 'vendor_transcode_probe.py',
                               'vendor_media_snapshot.py', 'vendor_app_probe.py', 'vendor_storage_probe.py',
                               'api_monitoring.py', 'api_access.py', 'api_permissions.py', 'api_contracts.py', 'api_docker_projects.py', 'api_docker_operations.py', 'api_docker_deployment.py', 'api_file_operations.py', 'api_file_provider.py', 'api_file_executor.py', 'api_file_write_backend.py', 'api_file_mutations.py', 'api_file_mutation_authority.py', 'api_file_native_index.py', 'api_file_native_index_reader.py', 'api_file_native_recycle.py', 'api_file_transfers.py', 'api_file_readiness.py', 'api_file_roots.py', 'mcp_service.py', 'integration_api.py', 'media_sort_plugin.py', 'media_sort_plugin_rules.py',
                               'network_access.py', 'service_settings.py', 'screen_manager.py',
                               'vendor_business.py', 'vendor_runtime.py', 'vendor_app_diagnostics.py', 'vendor_download_diagnostics.py', 'download_operations.py', 'vendor_media_operations.py', 'vendor_business_transport.py', 'native_capability_specs.py', 'vendor_native_capabilities.py',
                               'fan_policy.py', 'fan_native.py', 'fan_watchdog.py', 'temperature_sources.py',
                               'light_policy.py', 'monitoring.py')}}
    if native_guard is not None:
        guard = Path(native_guard)
        if guard.is_symlink() or not guard.is_file() or guard.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('风扇适配层不是有界普通文件')
        header = guard.read_bytes()[:20]
        if len(header) < 20 or header[:7] != b'\x7fELF\x02\x01\x01' or header[16:20] != b'\x03\x00\x3e\x00':
            raise ValueError('风扇适配层必须是 Linux x86-64 ELF 共享库')
        sources['runtime/fan_guard.so'] = guard
    web = frontend / 'web'
    if web.is_symlink():
        raise ValueError('网页配置目录不允许符号链接')
    for name in ('index.html', 'login.html'):
        sources['frontend/web/' + name] = web / name
    assets = frontend / 'assets'
    if assets.is_symlink():
        raise ValueError('UI assets 不允许符号链接')
    if assets.exists():
        for path in sorted(assets.rglob('*')):
            if path.is_symlink():
                raise ValueError('UI 资源不允许符号链接：' + str(path))
            if path.is_file() and not any(part.startswith('.') for part in path.relative_to(assets).parts):
                sources['frontend/assets/' + path.relative_to(assets).as_posix()] = path
    if len(sources) + 1 > MAX_FILES:
        raise ValueError('UI 包文件过多')
    files = {}
    for name, path in sorted(sources.items()):
        if path.is_symlink() or not path.is_file():
            raise ValueError('缺少 UI 必需文件或存在符号链接：' + str(path))
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('UI 单文件超过 16 MiB：' + str(path))
        data = path.read_bytes()
        if not data:
            raise ValueError('UI 文件不能为空：' + str(path))
        files[name] = data
    if sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        raise ValueError('UI 包总内容超过 32 MiB')
    manifest = {'schema_version': 1, 'files': {
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()
    }}
    files['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    if len(files['manifest.json']) > 128 * 1024 or sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        raise ValueError('UI 清单或包含清单的总内容超过限制')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'ui-bundle.tar'
    temporary = output / 'ui-bundle.tar.incoming'
    with tarfile.open(temporary, mode='w', format=tarfile.USTAR_FORMAT) as bundle:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(data), 0o644, 0
            bundle.addfile(member, io.BytesIO(data))
    temporary.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / 'ui-bundle.sha256').write_text(digest + '\n', encoding='ascii')
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build')
    parser.add_argument('--native-guard', type=Path, help='已编译的 Linux amd64 风扇适配层；生产镜像由 Bookworm 阶段提供')
    args = parser.parse_args()
    try:
        digest = build_bundle(output=args.output, native_guard=args.native_guard)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, 'UI 打包失败：' + str(error) + '\n')
    print('Built UI bundle sha256:' + digest)


if __name__ == '__main__':
    main()

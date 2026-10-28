#!/usr/bin/env python3
"""Build the fnmonitor-based panel into the two files the image serves.

The panel is vendored from https://github.com/MisiteQ/fnmonitor (MIT).  The
readable sources live in ``ui/panel-src`` and are concatenated and minified
exactly like fnmonitor's own ``tools/build_ui.sh`` does.  R1-specific modules
and settings live in ``ui/panel-src/50-r1.js`` and are loaded before the boot
file, so fnmonitor's own rendering code stays untouched.

Outputs:

* ``ui/web/index.html``  – single-file panel (HTML + CSS + JS)
* ``ui/web/login.html``  – password login page (copied with R1 branding)

Run ``python scripts/build-panel.py`` after editing anything in ``ui/panel-src``.
"""

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'ui' / 'panel-src'
OUTPUT = ROOT / 'ui' / 'web'

# Readable source order, mirroring fnmonitor tools/build_ui.sh.  44-toast must
# load before 40-hero; 50-r1 runs after the stock modules and before boot.
JS_FILES = (
    '30-core.js', '31-line.js', '32-bars.js', '33-state.js', '44-toast.js',
    '40-hero.js', '41-modules.js', '42-modules.js', '43-modules.js',
    '45-sheets.js', '46-docker.js', '46-vms.js', '47-settings.js',
    '48-layout.js', '50-r1.js', '49-app.js',
)
CSS_FILES = ('10-style.css', '11-style-add.css', '12-style-r1.css')

# The HTML is fnmonitor's own product copy.  Only the product name changes; all
# feature wording stays as upstream so later ports keep matching the styles.
BRANDING = (
    ('飞牛监控', 'R1 Toolbox'),
    ('飞牛桌面', '桌面'),
    ('飞牛窗口', '小屏窗口'),
    ('飞牛 iframe', '小屏窗口'),
    ('飞牛应用', '应用'),
    ('飞牛防火墙', '防火墙'),
    ('飞牛存储空间', '存储空间'),
    ('飞牛文件', '文件'),
    ('飞牛的内网穿透', 'R1 的内网穿透'),
    ('飞牛未开启', 'R1 未开启'),
    ('飞牛「虚拟机」', 'R1「虚拟机」'),
    ('飞牛 fnOS 版本', 'R1 固件版本'),
    ("'fnOS '", "'固件 '"),
    ('飞牛', 'R1'),
)


# R1 reports only the RAPL CPU-package reading and deliberately does not
# estimate whole-machine power.  fnmonitor's copy assumes RAPL + base/disks
# estimation, so these exact sentences are corrected at build time; the
# rendering code stays untouched.
POWER_COPY = (
    ('实测 + 估算', 'CPU 封装实测'),
    ("title: '整机功耗'", "title: 'CPU 功耗'"),
    ('整机 = CPU 封装（RAPL 实测）+ 内存 + 主板底座 + 活动硬盘',
     '仅显示 CPU 封装（RAPL 实测）；R1 不估算整机功耗'),
    ('RAPL 实测 + 底座 / 硬盘估算', 'RAPL CPU 封装实测（非整机）'),
    ('无法读取 RAPL，也无法估算整机功耗', '无法读取 RAPL 功耗'),
    ('CSV · 整机功耗序列', 'CSV · CPU 功耗序列'),
    ('用于电费计算，以及无 RAPL 时的整机功耗估算。', 'R1 只显示 RAPL 实测的 CPU 封装功耗，不估算整机功耗。'),
    ('接了 iDRAC 时直接用电源实测的整机输出，这是最准的口径（含主板、内存、网卡与全部硬盘）。'
     '没有 iDRAC 才会退回 RAPL：它只测得 CPU 封装（与内存）功耗，整机值再叠加主板底座与活动硬盘的典型功耗；'
     '无 RAPL 时按 TDP × CPU 占用估算。休眠硬盘不计入。',
     'R1 Toolbox 只显示 RAPL 实测的 CPU 封装功耗，不叠加主板、硬盘或网卡估算，也不把 CPU 封装当作整机功耗；'
     '没有 RAPL 时不显示功耗。休眠硬盘不参与任何计算。'),
)


def rebrand(text):
    for old, new in BRANDING:
        text = text.replace(old, new)
    for old, new in POWER_COPY:
        text = text.replace(old, new)
    return text


def minify_css(source):
    source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
    source = re.sub(r'\s+', ' ', source)
    source = re.sub(r'\s*([{};,>])\s*', r'\1', source)
    source = re.sub(r'\{([^{}]*)\}', lambda m: '{' + re.sub(r'\s*:\s*', ':', m.group(1)) + '}', source)
    source = source.replace(';}', '}').replace('}', '}\n')
    return source.strip()


def minify_js(source):
    """Drop whole-line comments and indentation; keep line structure.

    fnmonitor's minifier is line based, which keeps automatic semicolons intact.
    """
    lines, in_block = [], False
    for line in source.split('\n'):
        stripped = line.strip()
        if in_block:
            if '*/' in stripped:
                in_block = False
                rest = stripped.split('*/', 1)[1].strip()
                if rest:
                    lines.append(rest)
            continue
        if stripped.startswith('/*'):
            if '*/' in stripped:
                rest = stripped.split('*/', 1)[1].strip()
                if rest:
                    lines.append(rest)
            else:
                in_block = True
            continue
        if stripped.startswith('//') or not stripped:
            continue
        lines.append(stripped)
    return '\n'.join(lines)


NL = chr(10)


def license_banner():
    """The delivered single file carries fnmonitor's MIT notice."""
    text = (ROOT / 'vendor' / 'fnmonitor' / 'LICENSE').read_text(encoding='utf-8').strip()
    lines = NL.join(('  ' + line).rstrip() for line in text.splitlines())
    return ('<!--' + NL + '  Panel framework: fnMonitor (https://github.com/MisiteQ/fnmonitor),' + NL +
            '  commit d002d1d2e898222e989786ba607dad9810dcdb9a.' + NL + NL + lines + NL + '-->' + NL)


def with_license(document):
    match = re.match(r'<!doctype html>', document, re.I)
    if not match:
        raise ValueError('面板文档必须以 doctype 开头')
    return document[:match.end()] + NL + license_banner() + document[match.end():]


def read(name):
    path = SOURCE / name
    if path.is_symlink() or not path.is_file():
        raise ValueError('缺少面板源文件或存在符号链接：' + str(path))
    return path.read_text(encoding='utf-8')


def build_index():
    head = rebrand(read('00-head.html'))
    if not head.rstrip().endswith('<style>'):
        raise ValueError('00-head.html 必须以 <style> 结尾')
    css = minify_css('\n'.join(read(name) for name in CSS_FILES))
    body = rebrand(read('20-icons.svg') + read('21-body.html'))
    scripts = minify_js(rebrand('\n'.join(read(name) for name in JS_FILES)))
    document = (
        head + '\n' + css + '\n</style>\n</head>\n<body>\n' + body +
        '\n<script>\n(function () {\n\'use strict\';\n' + scripts +
        '\n})();\n</script>\n</body>\n</html>\n'
    )
    return with_license(document)


def build_login():
    return with_license(rebrand(read('login.html')))


def check_javascript(document, workdir):
    """Fail the build on a syntax error, so a broken panel never ships."""
    if shutil.which('node') is None:
        return '未安装 node，跳过语法检查'
    start = document.rindex('<script>\n') + len('<script>\n')
    end = document.rindex('</script>')
    script = pathlib.Path(workdir) / 'panel-check.js'
    script.write_text(document[start:end], encoding='utf-8')
    result = subprocess.run(['node', '--check', str(script)], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError('面板脚本语法检查失败：' + (result.stderr or result.stdout).strip()[:2000])
    return 'node --check 通过'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=pathlib.Path, default=OUTPUT)
    parser.add_argument('--no-check', action='store_true', help='跳过 node 语法检查')
    args = parser.parse_args()
    try:
        index, login = build_index(), build_login()
        with tempfile.TemporaryDirectory() as workdir:
            note = '跳过语法检查' if args.no_check else check_javascript(index, workdir)
        for text in (index, login):
            if len(text.encode('utf-8')) > 2 * 1024 * 1024:
                raise ValueError('面板文件超过 2 MiB，请检查是否误嵌入大图')
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'index.html').write_text(index, encoding='utf-8')
        (args.output / 'login.html').write_text(login, encoding='utf-8')
    except (OSError, ValueError) as error:
        parser.exit(1, '面板构建失败：' + str(error) + '\n')
    print('Built panel: %s (%d bytes), login (%d bytes); %s'
          % (args.output / 'index.html', len(index.encode('utf-8')),
             len(login.encode('utf-8')), note))


if __name__ == '__main__':
    main()

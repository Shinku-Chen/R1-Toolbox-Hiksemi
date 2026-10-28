#!/usr/bin/env python3
"""Run the R1 Toolbox panels against a fake NAS on this computer.

    python tools/demo/run.py                 # local only
    python tools/demo/run.py --host 0.0.0.0  # reachable from the LAN

Open the printed web URL for the fnMonitor panel and the small-screen URL for
the touch UI.  There is no Docker, no hardware access and no real power action.
"""

import argparse
from pathlib import Path
import sys
import time
import webbrowser

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from server import create_demo, _lan_addresses  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='127.0.0.1',
                        help='监听地址；局域网访问用 0.0.0.0（默认 127.0.0.1）')
    parser.add_argument('--screen-port', type=int, default=18731, help='小屏页面端口（默认 18731）')
    parser.add_argument('--web-port', type=int, default=18732, help='网页面板端口（默认 18732）')
    parser.add_argument('--data-dir', type=Path, help='演示状态目录（默认临时目录，重启即清空）')
    parser.add_argument('--open', action='store_true', help='启动后自动打开网页面板')
    args = parser.parse_args()
    if args.host not in ('127.0.0.1', '0.0.0.0'):
        parser.error('--host 只能是 127.0.0.1 或 0.0.0.0')
    demo = None
    try:
        demo = create_demo(host=args.host, screen_port=args.screen_port, web_port=args.web_port,
                           data_dir=args.data_dir)
        local = '127.0.0.1'
        print('')
        print('  R1 Toolbox 本地演示已启动')
        print('  网页面板   http://%s:%d/' % (local, demo.web.port))
        print('  小屏触摸   http://%s:%d/' % (local, demo.screen.server_port))
        if args.host == '0.0.0.0':
            for address in _lan_addresses():
                print('  局域网     http://%s:%d/  ·  http://%s:%d/' % (
                    address, demo.web.port, address, demo.screen.server_port))
        print('')
        print('  配对码：小屏「更多（齿轮）→ 网页配置 → 生成一次性配对码」，再在网页输入。')
        print('  演示数据在内存中；退出后重置。按 Ctrl+C 停止。')
        print('')
        if args.open:
            webbrowser.open('http://%s:%d/' % (local, demo.web.port))
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n正在停止演示…')
    except OSError as error:
        print('演示启动失败：%s。请换端口：--web-port 28732 --screen-port 28731' % error, file=sys.stderr)
        return 1
    except RuntimeError as error:
        print('演示启动失败：%s' % error, file=sys.stderr)
        return 1
    finally:
        if demo is not None:
            demo.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())

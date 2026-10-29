#!/usr/bin/env python3
"""Export the owner-visible definitions without registering live MCP tools."""

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from api_contracts import contract_catalog


def render():
    """Keep the offline artifact byte-for-byte identical to runtime definitions."""
    return (json.dumps(contract_catalog(), ensure_ascii=False, sort_keys=True,
                       indent=2, allow_nan=False) + '\n').encode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='拒绝未同步的接口定义产物')
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'docs/api/file-docker-contracts.json')
    args = parser.parse_args()
    expected = render()
    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != expected:
            parser.exit(1, '接口定义产物未同步，请运行 python scripts/build-api-contracts.py。\n')
        print('API contract artifact is current.')
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(expected)
    print('Built API contract definitions:', args.output)


if __name__ == '__main__':
    main()

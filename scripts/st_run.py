"""Headless schemathesis runner (bypasses rich Win32 console crash).

Usage:
    python scripts/st_run.py --schema openapi-fuzz.json -u http://127.0.0.1:8899 \
        --include-path-regex "^/v1/(backtest|trace|risk)" -n 15 --checks not_a_server_error
"""
import re
import sys

import rich.console

# O线：Windows gbk 控制台下 rich legacy renderer 必崩（✅无法编码）。
# 直接声明非终端，让 rich 走纯文本输出。
rich.console.Console.is_terminal = property(lambda self: False)


def main(argv):
    from schemathesis.cli import schemathesis as cli

    # click standalone_mode 会 SystemExit，按原样传递退出码
    cli.main(args=argv[1:], standalone_mode=False)


if __name__ == "__main__":
    try:
        main(sys.argv)
        print("ST_RUN_EXIT=0", flush=True)
    except SystemExit as e:
        print("ST_RUN_EXIT=%s" % e.code, flush=True)
        raise
    except BaseException as e:
        print("ST_RUN_ERROR %r" % (e,), flush=True)
        raise

#!/usr/bin/env python3
"""`loop` console entry point；lite 版只公開 Dashboard 子命令。"""
import argparse

from engine import dashboard


def build_parser() -> argparse.ArgumentParser:
    """建立唯一公開命令的 CLI 契約。"""
    parser = argparse.ArgumentParser(prog="loop", description="loop-agent-lite 本機 Dashboard")
    subcommands = parser.add_subparsers(dest="command", required=True)
    command = subcommands.add_parser("dashboard", help="啟動本機 Loop Dashboard")
    command.add_argument("--name", default="", help="預選 workspace（頁面內仍可切換）")
    command.add_argument("--port", type=int, default=8765, help="被占用會自動往上找（最多 +20）")
    command.add_argument("--read-only", action="store_true", help="擋所有 POST 並隱藏操作按鈕")
    command.set_defaults(func=dashboard.run_dashboard)
    return parser


def main(argv=None) -> int:
    """解析 `loop dashboard` 並將已驗證參數交給 Dashboard runtime。"""
    args = build_parser().parse_args(argv)
    return args.func(name=args.name, port=args.port, read_only=args.read_only)


if __name__ == "__main__":
    raise SystemExit(main())

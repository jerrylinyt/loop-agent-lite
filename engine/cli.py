#!/usr/bin/env python3
"""Installed `loop` entry point for Dashboard, status, and parallel fleet coordination."""
import argparse
import sys

from engine import dashboard, fleet, status


def build_parser() -> argparse.ArgumentParser:
    """Build top-level discovery; fleet/status delegate their complete parsers to runtime modules."""
    parser = argparse.ArgumentParser(prog="loop", description="loop-agent-lite coordinator")
    subcommands = parser.add_subparsers(dest="command", required=True)
    command = subcommands.add_parser("dashboard", help="啟動本機 Loop Dashboard")
    command.add_argument("--name", default="", help="預選 workspace（頁面內仍可切換）")
    command.add_argument("--port", type=int, default=8765, help="被占用會自動往上找（最多 +20）")
    command.set_defaults(func=dashboard.run_dashboard)
    subcommands.add_parser("status", add_help=False, help="唯讀查詢 standalone 與 parallel run 狀態")
    subcommands.add_parser("fleet", add_help=False, help="啟動或續跑 parallel track coordinator")
    return parser


def main(argv=None) -> int:
    """Route rich subcommand arguments without maintaining duplicate parsers."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "status":
        return status.main(raw[1:])
    if raw and raw[0] == "fleet":
        fleet.main(raw[1:])
        return 0
    args = build_parser().parse_args(raw)
    return args.func(name=args.name, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from .app_server import AppServerError, get_live_status
from .scheduler import SchedulerError, schedule
from .storage import StorageError, load_statuses_since, save_status


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-usage", description="Read Codex account rate limits."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Read current Codex rate limits as JSON.")
    collect = commands.add_parser(
        "collect", help="Save a live status snapshot and print it as JSON."
    )
    collect.add_argument("--db", metavar="PATH", help="SQLite database path.")
    history = commands.add_parser("history", help="Read saved status snapshots.")
    history.add_argument(
        "--hours", required=True, type=_positive_finite_float, metavar="N"
    )
    history.add_argument("--db", metavar="PATH", help="SQLite database path.")
    scheduler = commands.add_parser(
        "schedule", help="Manage the 30-minute scheduled collector."
    )
    scheduler.add_argument("action", choices=("install", "status", "uninstall"))
    return parser


def _positive_finite_float(value: str) -> float:
    try:
        hours = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("hours must be a number") from exc
    if not math.isfinite(hours) or hours <= 0:
        raise argparse.ArgumentTypeError("hours must be a finite positive number")
    return hours


def _print_json(result: Any) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "status":
            _print_json(get_live_status())
        elif args.command == "collect":
            status = get_live_status()
            save_status(status, args.db)
            _print_json(status)
        elif args.command == "history":
            generated_at = datetime.now(UTC)
            cutoff = generated_at - timedelta(hours=args.hours)
            samples = load_statuses_since(cutoff, args.db)
            _print_json(
                {
                    "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
                    "rangeHours": args.hours,
                    "samples": samples,
                }
            )
        else:
            _print_json(schedule(args.action))
    except (AppServerError, SchedulerError, StorageError) as exc:
        print(f"codex-usage: {exc}", file=sys.stderr)
        return 1
    return 0

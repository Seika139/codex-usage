from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from .app_server import AppServerError, get_live_status
from .scheduler import (
    DEFAULT_INTERVAL_MINUTES,
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    SchedulerError,
    schedule,
)
from .storage import StorageError, get_database_stats, load_statuses_since, save_status


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
    db_stats = commands.add_parser(
        "db-stats", help="Show the saved snapshot count and database file size."
    )
    db_stats.add_argument("--db", metavar="PATH", help="SQLite database path.")
    scheduler = commands.add_parser("schedule", help="Manage the scheduled collector.")
    schedule_commands = scheduler.add_subparsers(dest="schedule_action", required=True)
    install = schedule_commands.add_parser(
        "install", help="Install or update the schedule."
    )
    install.add_argument(
        "--interval-minutes",
        type=_positive_interval_minutes,
        metavar="N",
        help=(
            "Collection interval in minutes "
            f"({MIN_INTERVAL_MINUTES} to {MAX_INTERVAL_MINUTES}; "
            f"default: {DEFAULT_INTERVAL_MINUTES})."
        ),
    )
    schedule_commands.add_parser("status", help="Show the installed schedule.")
    schedule_commands.add_parser("uninstall", help="Remove the installed schedule.")
    return parser


def _positive_finite_float(value: str) -> float:
    try:
        hours = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("hours must be a number") from exc
    if not math.isfinite(hours) or hours <= 0:
        raise argparse.ArgumentTypeError("hours must be a finite positive number")
    return hours


def _positive_interval_minutes(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError(
            "interval-minutes must be an integer from "
            f"{MIN_INTERVAL_MINUTES} to {MAX_INTERVAL_MINUTES}"
        )
    minutes = int(value)
    if not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
        raise argparse.ArgumentTypeError(
            "interval-minutes must be an integer from "
            f"{MIN_INTERVAL_MINUTES} to {MAX_INTERVAL_MINUTES}"
        )
    return minutes


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
        elif args.command == "db-stats":
            _print_json(get_database_stats(args.db))
        else:
            _print_json(
                schedule(
                    args.schedule_action,
                    interval_minutes=getattr(args, "interval_minutes", None),
                )
            )
    except (AppServerError, SchedulerError, StorageError) as exc:
        print(f"codex-usage: {exc}", file=sys.stderr)
        return 1
    return 0

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from .app_server import AppServerError, get_live_status
from .notifications import (
    NotificationError,
    find_rate_limit_alerts,
    send_rate_limit_alerts,
)
from .scheduler import (
    DEFAULT_INTERVAL_MINUTES,
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    SchedulerError,
    schedule,
)
from .storage import (
    StorageError,
    get_database_stats,
    load_latest_rate_limit_snapshot,
    load_statuses_since,
    save_status,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-usage", description="Read Codex usage and account rate limits."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Read current Codex usage as JSON.")
    collect = commands.add_parser(
        "collect", help="Save a live usage snapshot and print it as JSON."
    )
    collect.add_argument("--db", metavar="PATH", help="SQLite database path.")
    collect.add_argument(
        "--active-within-hours",
        type=_positive_finite_float,
        default=24.0,
        metavar="N",
        help=(
            "Read per-thread usage for threads whose recencyAt is within N hours; "
            "fall back to updatedAt when recencyAt is absent (default: 24)."
        ),
    )
    history = commands.add_parser("history", help="Read saved usage snapshots.")
    history.add_argument(
        "-H", "--hours", required=True, type=_positive_finite_float, metavar="N"
    )
    history.add_argument("--db", metavar="PATH", help="SQLite database path.")
    history.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Include per-thread model usage details in each snapshot.",
    )
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
        "-I",
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


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo is not None else None


def _model_usage_estimate(
    samples: list[dict[str, Any]], cutoff: datetime, generated_at: datetime
) -> dict[str, Any]:
    totals: dict[str | None, dict[str, Any]] = {}
    baseline_count = 0
    counter_reset_count = 0
    invalid_interval_count = 0
    partial_collection_count = 0

    for sample in samples:
        usage = sample.get("modelUsage")
        if usage is None:
            continue
        if usage["status"] != "complete":
            partial_collection_count += 1
        for thread in usage["threads"]:
            for model_usage in thread["models"]:
                estimate = model_usage["periodUsageEstimate"]
                interval_end = _parse_timestamp(estimate.get("intervalEnd"))
                if interval_end is None or not cutoff <= interval_end <= generated_at:
                    continue
                estimate_status = estimate["status"]
                if estimate_status == "baseline":
                    baseline_count += 1
                    continue
                if estimate_status == "counter_reset":
                    counter_reset_count += 1
                    continue
                if estimate_status != "estimated":
                    invalid_interval_count += 1
                    continue
                credits = estimate.get("estimatedUsageCreditsMicros")
                if not isinstance(credits, int) or isinstance(credits, bool):
                    invalid_interval_count += 1
                    continue

                model = model_usage.get("model")
                total = totals.setdefault(
                    model,
                    {
                        "model": model,
                        "estimatedUsageCreditsMicros": 0,
                        "inputTokens": 0,
                        "cachedInputTokens": 0,
                        "outputTokens": 0,
                        "totalTokens": 0,
                        "contributingThreads": set(),
                        "intervalCount": 0,
                        "nullableTokenFields": set(),
                    },
                )
                total["estimatedUsageCreditsMicros"] += credits
                total["contributingThreads"].add(thread["threadId"])
                total["intervalCount"] += 1
                for field in (
                    "inputTokens",
                    "cachedInputTokens",
                    "outputTokens",
                    "totalTokens",
                ):
                    value = estimate.get(field)
                    if value is None:
                        total["nullableTokenFields"].add(field)
                    else:
                        total[field] += value

    by_model: list[dict[str, Any]] = []
    for model in sorted(totals, key=lambda value: value or ""):
        total = totals[model]
        by_model.append(
            {
                "model": total["model"],
                "estimatedUsageCreditsMicros": total["estimatedUsageCreditsMicros"],
                "inputTokens": (
                    None
                    if "inputTokens" in total["nullableTokenFields"]
                    else total["inputTokens"]
                ),
                "cachedInputTokens": (
                    None
                    if "cachedInputTokens" in total["nullableTokenFields"]
                    else total["cachedInputTokens"]
                ),
                "outputTokens": (
                    None
                    if "outputTokens" in total["nullableTokenFields"]
                    else total["outputTokens"]
                ),
                "totalTokens": (
                    None
                    if "totalTokens" in total["nullableTokenFields"]
                    else total["totalTokens"]
                ),
                "contributingThreadCount": len(total["contributingThreads"]),
                "intervalCount": total["intervalCount"],
            }
        )

    return {
        "rangeStart": cutoff.isoformat().replace("+00:00", "Z"),
        "rangeEnd": generated_at.isoformat().replace("+00:00", "Z"),
        "attribution": "detected_at_interval_end",
        "intervalBoundaryPolicy": "include_full_interval_when_end_is_in_range",
        "coverage": "observed_intervals_only",
        "subagentDoubleCountingPossible": True,
        "partialCollectionCount": partial_collection_count,
        "baselineIntervalCount": baseline_count,
        "counterResetIntervalCount": counter_reset_count,
        "invalidIntervalCount": invalid_interval_count,
        "byModel": by_model,
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "status":
            _print_json(get_live_status())
        elif args.command == "collect":
            previous = load_latest_rate_limit_snapshot(args.db)
            status = get_live_status(
                include_model_usage=True,
                active_within_hours=args.active_within_hours,
            )
            alerts = find_rate_limit_alerts(previous, status)
            send_rate_limit_alerts(alerts)
            save_status(status, args.db)
            _print_json(status)
        elif args.command == "history":
            generated_at = datetime.now(UTC)
            cutoff = generated_at - timedelta(hours=args.hours)
            snapshots = load_statuses_since(cutoff, args.db)
            model_usage_estimate = _model_usage_estimate(
                snapshots, cutoff, generated_at
            )
            if args.verbose:
                displayed_snapshots = snapshots
            else:
                displayed_snapshots = []
                for snapshot in snapshots:
                    displayed_snapshot = dict(snapshot)
                    model_usage = displayed_snapshot.get("modelUsage")
                    if isinstance(model_usage, dict):
                        displayed_model_usage = dict(model_usage)
                        displayed_model_usage.pop("threads", None)
                        displayed_snapshot["modelUsage"] = displayed_model_usage
                    displayed_snapshots.append(displayed_snapshot)
            _print_json(
                {
                    "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
                    "rangeHours": args.hours,
                    "modelUsageEstimate": model_usage_estimate,
                    "snapshots": displayed_snapshots,
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
    except (AppServerError, NotificationError, SchedulerError, StorageError) as exc:
        print(f"codex-usage: {exc}", file=sys.stderr)
        return 1
    return 0

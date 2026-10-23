from __future__ import annotations

import math
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .storage import resolve_db_path

WEBHOOK_ENV_VAR = "CODEX_USAGE_DISCORD_WEBHOOK_URL"
WEBHOOK_FILENAME = "discord-webhook-url"


class NotificationError(RuntimeError):
    """An error while preparing or sending a Discord notification."""


@dataclass(frozen=True)
class RateLimitAlert:
    window_name: str
    threshold: int
    remaining_percent: float
    collected_at: str | None
    resets_at: str | None


_WINDOWS = (
    ("fiveHour", "5時間枠（5h）"),
    ("weekly", "週間枠（1w）"),
)
_THRESHOLDS = (50, 20, 5, 0)


def find_rate_limit_alerts(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> list[RateLimitAlert]:
    """Find each threshold crossed since the most recent saved snapshot."""
    if previous is None:
        return []

    alerts: list[RateLimitAlert] = []
    for key, label in _WINDOWS:
        old_window = previous.get(key)
        new_window = current.get(key)
        old_remaining = _remaining_percent(old_window)
        new_remaining = _remaining_percent(new_window)
        if old_remaining is None or new_remaining is None:
            continue

        old_resets_at = _parse_timestamp(old_window.get("resetsAt"))
        new_resets_at = _parse_timestamp(new_window.get("resetsAt"))
        if (
            old_resets_at is not None
            and new_resets_at is not None
            and old_resets_at != new_resets_at
        ):
            # A changed reset time marks a new quota period; its previous amount
            # starts at 100%, even if the collector missed the exact rollover.
            old_remaining = 100.0

        crossed = [
            threshold
            for threshold in _THRESHOLDS
            if (
                old_remaining > 0 and new_remaining <= 0
                if threshold == 0
                else old_remaining >= threshold and new_remaining < threshold
            )
        ]
        for threshold in crossed:
            alerts.append(
                RateLimitAlert(
                    window_name=label,
                    threshold=threshold,
                    remaining_percent=new_remaining,
                    collected_at=_optional_string(current.get("collectedAt")),
                    resets_at=_optional_string(new_window.get("resetsAt")),
                )
            )
    return alerts


def send_rate_limit_alerts(alerts: list[RateLimitAlert]) -> bool:
    """Send all crossed thresholds as one Discord message if configured."""
    if not alerts:
        return False
    webhook_url = _resolve_webhook_url()
    if webhook_url is None:
        return False

    try:
        from discord_notify import DiscordWebhook

        statuses = DiscordWebhook(webhook_url).send(
            "\n".join(_format_alert(alert) for alert in alerts)
        )
    except Exception:
        # HTTP exceptions can contain the webhook URL, which includes its token.
        raise NotificationError(
            "Could not send Discord rate-limit notification."
        ) from None
    if not statuses or any(not 200 <= status < 300 for status in statuses):
        raise NotificationError("Discord rejected the rate-limit notification.")
    return True


def _resolve_webhook_url() -> str | None:
    configured_url = os.environ.get(WEBHOOK_ENV_VAR)
    if configured_url is not None:
        url = configured_url.strip()
        if not url:
            raise NotificationError(f"{WEBHOOK_ENV_VAR} is set but empty.")
    else:
        path = resolve_db_path().parent / WEBHOOK_FILENAME
        url = _read_webhook_secret_file(path)
        if url is None:
            return None
        if not url:
            raise NotificationError("The Discord webhook secret file is empty.")

    _validate_webhook_url(url)
    return url


def _read_webhook_secret_file(path: Path) -> str | None:
    if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
        raise NotificationError(
            "The Discord webhook secret file must not be a symlink."
        )

    flags = os.O_RDONLY
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise NotificationError(
            "Could not read the Discord webhook secret file."
        ) from None

    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise NotificationError(
                "The Discord webhook secret file must be a regular file."
            )
        if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise NotificationError(
                "The Discord webhook secret file must be private (mode 600)."
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as secret_file:
            descriptor = -1
            return secret_file.read().strip()
    except NotificationError:
        raise
    except OSError:
        raise NotificationError(
            "Could not read the Discord webhook secret file."
        ) from None
    except UnicodeError:
        raise NotificationError(
            "Could not read the Discord webhook secret file."
        ) from None
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _validate_webhook_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        path_segments = parsed.path.split("/")
        is_webhook_path = (
            len(path_segments) == 5
            and path_segments[0] == ""
            and path_segments[1] == "api"
            and path_segments[2] == "webhooks"
            and path_segments[3]
            and path_segments[4]
        )
        is_valid = (
            parsed.scheme == "https"
            and parsed.hostname == "discord.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and not parsed.query
            and not parsed.fragment
            and is_webhook_path
        )
    except ValueError:
        is_valid = False
    if not is_valid:
        raise NotificationError(
            "The Discord webhook URL must be an HTTPS discord.com webhook URL."
        )


def _remaining_percent(window: Any) -> float | None:
    if not isinstance(window, dict):
        return None
    used_percent = window.get("usedPercent")
    if (
        not isinstance(used_percent, (int, float))
        or isinstance(used_percent, bool)
        or not math.isfinite(used_percent)
    ):
        return None
    return 100.0 - used_percent


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo is not None else None


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _format_alert(alert: RateLimitAlert) -> str:
    if alert.threshold == 0:
        condition = "使い切りました"
    else:
        condition = f"残り{alert.threshold}%未満"
    measured_at = f"（計測: {alert.collected_at}）" if alert.collected_at else ""
    reset_at = alert.resets_at or "不明"
    return (
        f"【{alert.window_name}】{condition} — "
        f"計測時点の残り: {_format_percent(alert.remaining_percent)}%{measured_at}、"
        f"リセット日時: {reset_at}"
    )


def _format_percent(value: float) -> str:
    rounded = round(value, 1)
    return str(int(rounded)) if rounded.is_integer() else f"{rounded:.1f}"

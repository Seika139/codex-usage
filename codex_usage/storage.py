from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DATABASE_FILENAME = "usage.sqlite3"
SCHEMA_VERSION = 2


class StorageError(RuntimeError):
    """An error while resolving or accessing the usage database."""


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve an explicit, environment-provided, or platform-default DB path."""
    if db_path is not None:
        return Path(db_path).expanduser()

    configured_path = os.environ.get("CODEX_USAGE_DB")
    if configured_path:
        return Path(configured_path).expanduser()

    home = Path.home()
    if os.name == "nt":
        data_dir = os.environ.get("LOCALAPPDATA")
        base = Path(data_dir).expanduser() if data_dir else home / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        data_dir = os.environ.get("XDG_DATA_HOME")
        configured_data_dir = Path(data_dir) if data_dir else None
        base = (
            configured_data_dir
            if configured_data_dir is not None and configured_data_dir.is_absolute()
            else home / ".local" / "share"
        )
    return base / "codex-usage" / DATABASE_FILENAME


def save_status(status: dict[str, Any], db_path: str | Path | None = None) -> None:
    """Commit one normalized live status snapshot to SQLite."""
    path = resolve_db_path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_schema(connection)
            _insert_status(connection, status)
            connection.commit()
    except StorageError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise StorageError(f"Could not save usage snapshot to '{path}': {exc}") from exc


def load_statuses_since(
    since: datetime, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Load snapshots at or after a UTC instant without creating a database."""
    path = resolve_db_path(db_path)
    if not path.exists():
        return []

    cutoff = _format_utc(since)
    try:
        database_uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(database_uri, uri=True)) as connection:
            version = _schema_version(connection)
            if version > SCHEMA_VERSION:
                raise StorageError(
                    f"Database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}."
                )
            if version not in (0, 1, SCHEMA_VERSION):
                raise StorageError(
                    f"Database schema version {version} is not supported."
                )
            if version == 0:
                _reject_unversioned_user_tables(connection)
                return []
            individual_limit_columns = (
                ", individual_limit_present, individual_limit_limit, "
                "individual_limit_used, individual_limit_remaining_percent, "
                "individual_limit_resets_at"
                if version == SCHEMA_VERSION
                else ""
            )
            rows = connection.execute(
                "SELECT collected_at, five_hour_present, five_hour_used_percent, "
                "five_hour_resets_at, weekly_present, weekly_used_percent, "
                "weekly_resets_at" + individual_limit_columns + " "
                "FROM usage_snapshots WHERE collected_at >= ? "
                "ORDER BY collected_at ASC, id ASC",
                (cutoff,),
            )
            return [_status_from_row(row) for row in rows]
    except StorageError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise StorageError(
            f"Could not read usage history from '{path}': {exc}"
        ) from exc


def get_database_stats(db_path: str | Path | None = None) -> dict[str, Any]:
    """Return snapshot count and the main SQLite file's on-disk size."""
    path = resolve_db_path(db_path).absolute()
    try:
        size_bytes = path.stat().st_size
    except FileNotFoundError:
        return _database_stats_result(path, False, 0, 0)
    except OSError as exc:
        raise StorageError(f"Could not inspect usage database '{path}': {exc}") from exc

    try:
        database_uri = f"{path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(database_uri, uri=True)) as connection:
            version = _schema_version(connection)
            if version > SCHEMA_VERSION:
                raise StorageError(
                    f"Database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}."
                )
            if version not in (0, 1, SCHEMA_VERSION):
                raise StorageError(
                    f"Database schema version {version} is not supported."
                )
            if version == 0:
                _reject_unversioned_user_tables(connection)
                snapshot_count = 0
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM usage_snapshots"
                ).fetchone()
                if row is None:
                    raise StorageError("Could not count usage snapshots.")
                snapshot_count = int(row[0])
        return _database_stats_result(path, True, snapshot_count, size_bytes)
    except StorageError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise StorageError(f"Could not inspect usage database '{path}': {exc}") from exc


def _database_stats_result(
    path: Path, exists: bool, snapshot_count: int, size_bytes: int
) -> dict[str, Any]:
    return {
        "databasePath": str(path),
        "exists": exists,
        "usageSnapshots": {"snapshotCount": snapshot_count},
        "sizeBytes": size_bytes,
        "sizeHumanReadable": _format_file_size(size_bytes),
    }


def _format_file_size(size_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    size = float(size_bytes)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{size_bytes} B"
    return f"{size:.1f} {units[unit_index]}"


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise StorageError("Could not read the usage database schema version.")
    return int(row[0])


def _ensure_schema(connection: sqlite3.Connection) -> None:
    version = _schema_version(connection)
    if version > SCHEMA_VERSION:
        raise StorageError(
            f"Database schema version {version} is newer than supported version "
            f"{SCHEMA_VERSION}; it was left unchanged."
        )
    if version not in (0, 1, SCHEMA_VERSION):
        raise StorageError(f"Database schema version {version} is not supported.")
    if version == SCHEMA_VERSION:
        return
    if version == 0:
        _reject_unversioned_user_tables(connection)
        connection.execute("""
            CREATE TABLE usage_snapshots (
                id INTEGER PRIMARY KEY,
                collected_at TEXT NOT NULL,
                five_hour_present INTEGER NOT NULL CHECK (five_hour_present IN (0, 1)),
                five_hour_used_percent REAL,
                five_hour_resets_at TEXT,
                weekly_present INTEGER NOT NULL CHECK (weekly_present IN (0, 1)),
                weekly_used_percent REAL,
                weekly_resets_at TEXT,
                individual_limit_present INTEGER NOT NULL
                    CHECK (individual_limit_present IN (0, 1)),
                individual_limit_limit TEXT,
                individual_limit_used TEXT,
                individual_limit_remaining_percent INTEGER,
                individual_limit_resets_at TEXT
            )
            """)
        connection.execute(
            "CREATE INDEX usage_snapshots_collected_at_id "
            "ON usage_snapshots (collected_at, id)"
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return

    connection.execute("""
        ALTER TABLE usage_snapshots ADD COLUMN individual_limit_present
        INTEGER NOT NULL DEFAULT 0 CHECK (individual_limit_present IN (0, 1))
        """)
    connection.execute(
        "ALTER TABLE usage_snapshots ADD COLUMN individual_limit_limit TEXT"
    )
    connection.execute(
        "ALTER TABLE usage_snapshots ADD COLUMN individual_limit_used TEXT"
    )
    connection.execute(
        "ALTER TABLE usage_snapshots "
        "ADD COLUMN individual_limit_remaining_percent INTEGER"
    )
    connection.execute(
        "ALTER TABLE usage_snapshots ADD COLUMN individual_limit_resets_at TEXT"
    )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _insert_status(connection: sqlite3.Connection, status: dict[str, Any]) -> None:
    five_hour = status["fiveHour"]
    weekly = status["weekly"]
    individual_limit = status.get("individualLimit")
    connection.execute(
        """
        INSERT INTO usage_snapshots (
            collected_at,
            five_hour_present,
            five_hour_used_percent,
            five_hour_resets_at,
            weekly_present,
            weekly_used_percent,
            weekly_resets_at,
            individual_limit_present,
            individual_limit_limit,
            individual_limit_used,
            individual_limit_remaining_percent,
            individual_limit_resets_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            status["collectedAt"],
            int(five_hour is not None),
            None if five_hour is None else five_hour["usedPercent"],
            None if five_hour is None else five_hour["resetsAt"],
            int(weekly is not None),
            None if weekly is None else weekly["usedPercent"],
            None if weekly is None else weekly["resetsAt"],
            int(individual_limit is not None),
            None if individual_limit is None else individual_limit["limit"],
            None if individual_limit is None else individual_limit["used"],
            None if individual_limit is None else individual_limit["remainingPercent"],
            None if individual_limit is None else individual_limit["resetsAt"],
        ),
    )


def _status_from_row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    collected_at = row[0]
    return {
        "schemaVersion": 1,
        "source": "live",
        "collectedAt": collected_at,
        "fiveHour": _window_from_row(row[1], row[2], row[3]),
        "weekly": _window_from_row(row[4], row[5], row[6]),
        "individualLimit": (
            None
            if len(row) == 7
            else _individual_limit_from_row(row[7], row[8], row[9], row[10], row[11])
        ),
    }


def _individual_limit_from_row(
    present: int,
    limit: str | None,
    used: str | None,
    remaining_percent: int | None,
    resets_at: str | None,
) -> dict[str, Any] | None:
    if not present:
        return None
    return {
        "limit": limit,
        "used": used,
        "remainingPercent": remaining_percent,
        "resetsAt": resets_at,
    }


def _window_from_row(
    present: int, used_percent: float | None, resets_at: str | None
) -> dict[str, Any] | None:
    if not present:
        return None
    return {
        "usedPercent": used_percent,
        "remainingPercent": None if used_percent is None else 100 - used_percent,
        "resetsAt": resets_at,
    }


def _has_user_tables(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _reject_unversioned_user_tables(connection: sqlite3.Connection) -> None:
    if _has_user_tables(connection):
        raise StorageError(
            "The database contains tables but has no supported schema version."
        )


def _format_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("The history cutoff must be timezone-aware.")
    utc_moment = moment.astimezone(UTC)
    if utc_moment.microsecond:
        # Snap up to the next stored second so a sample cannot fall outside the
        # requested range just because collectedAt is serialized to seconds.
        utc_moment = utc_moment.replace(microsecond=0) + timedelta(seconds=1)
    return utc_moment.isoformat(timespec="seconds").replace("+00:00", "Z")

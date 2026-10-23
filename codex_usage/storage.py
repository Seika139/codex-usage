from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DATABASE_FILENAME = "usage.sqlite3"
SCHEMA_VERSION = 4


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
    """Load snapshots or thread observations at or after a UTC instant."""
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
            if version not in (0, 1, 2, 3, SCHEMA_VERSION):
                raise StorageError(
                    f"Database schema version {version} is not supported."
                )
            if version == 0:
                _reject_unversioned_user_tables(connection)
                return []
            if version == SCHEMA_VERSION:
                rows = connection.execute(
                    "SELECT usage_snapshots.id, collected_at, five_hour_present, "
                    "five_hour_used_percent, five_hour_resets_at, weekly_present, "
                    "weekly_used_percent, weekly_resets_at, individual_limit_present, "
                    "individual_limit_limit, individual_limit_used, "
                    "individual_limit_remaining_percent, individual_limit_resets_at, "
                    "model_usage_collections.status, "
                    "model_usage_collections.observed_at, "
                    "model_usage_collections.thread_list_complete, "
                    "model_usage_collections.listed_thread_count, "
                    "model_usage_collections.targeted_thread_count, "
                    "model_usage_collections.not_targeted_thread_count, "
                    "model_usage_collections.attempted_thread_count, "
                    "model_usage_collections.not_attempted_thread_count, "
                    "model_usage_collections.retrieved_thread_count, "
                    "model_usage_collections.unavailable_thread_count, "
                    "model_usage_collections.failed_thread_count, "
                    "model_usage_collections.active_within_hours, "
                    "thread_observations.thread_id, "
                    "thread_observations.parent_thread_id, "
                    "thread_observations.source, thread_observations.updated_at, "
                    "thread_observations.recency_at, thread_observations.archived, "
                    "thread_observations.usage_status, "
                    "thread_observations.observed_at, "
                    "thread_model_usage.model, "
                    "thread_model_usage.estimated_usage_credits_micros, "
                    "thread_model_usage.input_tokens, "
                    "thread_model_usage.cached_input_tokens, "
                    "thread_model_usage.output_tokens, "
                    "thread_model_usage.total_tokens, "
                    "thread_model_usage.period_status, "
                    "thread_model_usage.interval_start, "
                    "thread_model_usage.interval_end, "
                    "thread_model_usage.interval_seconds, "
                    "thread_model_usage.delta_credits_micros, "
                    "thread_model_usage.delta_input_tokens, "
                    "thread_model_usage.delta_cached_input_tokens, "
                    "thread_model_usage.delta_output_tokens, "
                    "thread_model_usage.delta_total_tokens "
                    "FROM usage_snapshots "
                    "LEFT JOIN model_usage_collections "
                    "ON usage_snapshots.id = model_usage_collections.snapshot_id "
                    "LEFT JOIN thread_observations "
                    "ON usage_snapshots.id = thread_observations.snapshot_id "
                    "AND thread_observations.observed_at >= ? "
                    "LEFT JOIN thread_model_usage "
                    "ON thread_observations.snapshot_id = "
                    "thread_model_usage.snapshot_id "
                    "AND thread_observations.thread_id = thread_model_usage.thread_id "
                    "WHERE collected_at >= ? OR EXISTS ("
                    "SELECT 1 FROM thread_observations "
                    "WHERE thread_observations.snapshot_id = usage_snapshots.id "
                    "AND thread_observations.observed_at >= ?) "
                    "ORDER BY collected_at ASC, usage_snapshots.id ASC, "
                    "thread_observations.updated_at DESC, "
                    "thread_observations.recency_at DESC, "
                    "thread_observations.thread_id ASC, thread_model_usage.model ASC",
                    (cutoff, cutoff, cutoff),
                )
                return _statuses_from_thread_rows(rows)

            individual_limit_columns = (
                ", individual_limit_present, individual_limit_limit, "
                "individual_limit_used, individual_limit_remaining_percent, "
                "individual_limit_resets_at"
                if version in (2, 3)
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


def load_latest_rate_limit_snapshot(
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Load the newest saved snapshot's rate-limit fields without modifying the DB."""
    path = resolve_db_path(db_path)
    if not path.exists():
        return None

    try:
        database_uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(database_uri, uri=True)) as connection:
            version = _schema_version(connection)
            if version > SCHEMA_VERSION:
                raise StorageError(
                    f"Database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}."
                )
            if version not in (0, 1, 2, 3, SCHEMA_VERSION):
                raise StorageError(
                    f"Database schema version {version} is not supported."
                )
            if version == 0:
                _reject_unversioned_user_tables(connection)
                return None
            row = connection.execute(
                "SELECT collected_at, five_hour_present, five_hour_used_percent, "
                "five_hour_resets_at, weekly_present, weekly_used_percent, "
                "weekly_resets_at FROM usage_snapshots "
                "ORDER BY collected_at DESC, id DESC LIMIT 1"
            ).fetchone()
            return None if row is None else _status_from_row(row)
    except StorageError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise StorageError(
            f"Could not read the latest usage snapshot from '{path}': {exc}"
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
            if version not in (0, 1, 2, 3, SCHEMA_VERSION):
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
    if version not in (0, 1, 2, 3, SCHEMA_VERSION):
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
                individual_limit_resets_at TEXT,
                model_usage_status TEXT,
                model_usage_thread_count INTEGER,
                model_usage_retrieved_thread_count INTEGER,
                model_usage_unavailable_thread_count INTEGER,
                model_usage_failed_thread_count INTEGER
            )
            """)
        connection.execute(
            "CREATE INDEX usage_snapshots_collected_at_id "
            "ON usage_snapshots (collected_at, id)"
        )
        _create_model_usage_table(connection)
        _create_thread_usage_tables(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return

    if version == 1:
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
        connection.execute("PRAGMA user_version = 2")
        version = 2

    if version == 2:
        _add_model_usage_columns(connection)
        _create_model_usage_table(connection)
        connection.execute("PRAGMA user_version = 3")
        version = 3

    if version == 3:
        _create_thread_usage_tables(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _add_model_usage_columns(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE usage_snapshots ADD COLUMN model_usage_status TEXT")
    connection.execute(
        "ALTER TABLE usage_snapshots ADD COLUMN model_usage_thread_count INTEGER"
    )
    connection.execute(
        "ALTER TABLE usage_snapshots "
        "ADD COLUMN model_usage_retrieved_thread_count INTEGER"
    )
    connection.execute(
        "ALTER TABLE usage_snapshots "
        "ADD COLUMN model_usage_unavailable_thread_count INTEGER"
    )
    connection.execute(
        "ALTER TABLE usage_snapshots ADD COLUMN model_usage_failed_thread_count INTEGER"
    )


def _create_model_usage_table(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE model_usage (
            snapshot_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            estimated_usage_credits_micros INTEGER NOT NULL,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            PRIMARY KEY (snapshot_id, model),
            FOREIGN KEY (snapshot_id) REFERENCES usage_snapshots (id)
        )
        """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS delete_model_usage_after_snapshot
        AFTER DELETE ON usage_snapshots
        BEGIN
            DELETE FROM model_usage WHERE snapshot_id = OLD.id;
        END
        """)


def _create_thread_usage_tables(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE model_usage_collections (
            snapshot_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL CHECK (
                status IN ('complete', 'partial', 'unavailable')
            ),
            observed_at TEXT NOT NULL,
            thread_list_complete INTEGER NOT NULL CHECK (
                thread_list_complete IN (0, 1)
            ),
            listed_thread_count INTEGER,
            targeted_thread_count INTEGER,
            not_targeted_thread_count INTEGER,
            attempted_thread_count INTEGER NOT NULL,
            not_attempted_thread_count INTEGER,
            retrieved_thread_count INTEGER NOT NULL,
            unavailable_thread_count INTEGER NOT NULL,
            failed_thread_count INTEGER NOT NULL,
            active_within_hours REAL NOT NULL,
            FOREIGN KEY (snapshot_id) REFERENCES usage_snapshots (id)
        )
        """)
    connection.execute("""
        CREATE TABLE thread_observations (
            snapshot_id INTEGER NOT NULL,
            thread_id TEXT NOT NULL,
            parent_thread_id TEXT,
            source TEXT,
            updated_at INTEGER,
            recency_at INTEGER,
            archived INTEGER CHECK (archived IN (0, 1) OR archived IS NULL),
            usage_status TEXT NOT NULL CHECK (
                usage_status IN (
                    'success', 'unavailable', 'failed', 'not_attempted', 'not_targeted'
                )
            ),
            observed_at TEXT,
            PRIMARY KEY (snapshot_id, thread_id),
            FOREIGN KEY (snapshot_id) REFERENCES usage_snapshots (id)
        )
        """)
    connection.execute("""
        CREATE TABLE thread_model_usage (
            snapshot_id INTEGER NOT NULL,
            thread_id TEXT NOT NULL,
            model TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            estimated_usage_credits_micros INTEGER NOT NULL,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            period_status TEXT NOT NULL CHECK (
                period_status IN (
                    'baseline', 'estimated', 'counter_reset', 'invalid_interval'
                )
            ),
            interval_start TEXT,
            interval_end TEXT NOT NULL,
            interval_seconds REAL,
            delta_credits_micros INTEGER,
            delta_input_tokens INTEGER,
            delta_cached_input_tokens INTEGER,
            delta_output_tokens INTEGER,
            delta_total_tokens INTEGER,
            PRIMARY KEY (snapshot_id, thread_id, model),
            FOREIGN KEY (snapshot_id, thread_id)
                REFERENCES thread_observations (snapshot_id, thread_id)
        )
        """)
    connection.execute(
        "CREATE INDEX thread_model_usage_thread_model_observed "
        "ON thread_model_usage (thread_id, model, observed_at, snapshot_id)"
    )
    connection.execute("""
        CREATE TRIGGER delete_thread_usage_after_snapshot
        AFTER DELETE ON usage_snapshots
        BEGIN
            DELETE FROM thread_model_usage WHERE snapshot_id = OLD.id;
            DELETE FROM thread_observations WHERE snapshot_id = OLD.id;
            DELETE FROM model_usage_collections WHERE snapshot_id = OLD.id;
        END
        """)


def _insert_status(connection: sqlite3.Connection, status: dict[str, Any]) -> None:
    five_hour = status["fiveHour"]
    weekly = status["weekly"]
    individual_limit = status.get("individualLimit")
    cursor = connection.execute(
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
            individual_limit_resets_at,
            model_usage_status,
            model_usage_thread_count,
            model_usage_retrieved_thread_count,
            model_usage_unavailable_thread_count,
            model_usage_failed_thread_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            None
            if status.get("modelUsage") is None
            else status["modelUsage"]["status"],
            None
            if status.get("modelUsage") is None
            else status["modelUsage"]["threadCount"],
            None
            if status.get("modelUsage") is None
            else status["modelUsage"]["retrievedThreadCount"],
            None
            if status.get("modelUsage") is None
            else status["modelUsage"]["unavailableThreadCount"],
            None
            if status.get("modelUsage") is None
            else status["modelUsage"]["failedThreadCount"],
        ),
    )
    model_usage = status.get("modelUsage")
    if model_usage is None:
        return
    snapshot_id = cursor.lastrowid
    connection.execute(
        """
        INSERT INTO model_usage_collections (
            snapshot_id,
            status,
            observed_at,
            thread_list_complete,
            listed_thread_count,
            targeted_thread_count,
            not_targeted_thread_count,
            attempted_thread_count,
            not_attempted_thread_count,
            retrieved_thread_count,
            unavailable_thread_count,
            failed_thread_count,
            active_within_hours
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            model_usage["status"],
            model_usage["observedAt"],
            int(model_usage["threadListComplete"]),
            model_usage["listedThreadCount"],
            model_usage["targetedThreadCount"],
            model_usage["notTargetedThreadCount"],
            model_usage["attemptedThreadCount"],
            model_usage["notAttemptedThreadCount"],
            model_usage["retrievedThreadCount"],
            model_usage["unavailableThreadCount"],
            model_usage["failedThreadCount"],
            model_usage["activeWithinHours"],
        ),
    )
    for thread in model_usage["threads"]:
        connection.execute(
            """
            INSERT INTO thread_observations (
                snapshot_id,
                thread_id,
                parent_thread_id,
                source,
                updated_at,
                recency_at,
                archived,
                usage_status,
                observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                thread["threadId"],
                thread["parentThreadId"],
                thread["source"],
                thread["updatedAt"],
                thread["recencyAt"],
                None if thread["archived"] is None else int(thread["archived"]),
                thread["usageStatus"],
                thread["observedAt"],
            ),
        )
        for model in thread["models"]:
            model_key = model["model"] or ""
            observed_at = thread["observedAt"] or model_usage["observedAt"]
            period = _estimate_period(
                connection,
                thread["threadId"],
                model_key,
                observed_at,
                model,
            )
            period["attribution"] = "detected_at_interval_end"
            connection.execute(
                """
                INSERT INTO thread_model_usage (
                    snapshot_id,
                    thread_id,
                    model,
                    observed_at,
                    estimated_usage_credits_micros,
                    input_tokens,
                    cached_input_tokens,
                    output_tokens,
                    total_tokens,
                    period_status,
                    interval_start,
                    interval_end,
                    interval_seconds,
                    delta_credits_micros,
                    delta_input_tokens,
                    delta_cached_input_tokens,
                    delta_output_tokens,
                    delta_total_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    thread["threadId"],
                    model_key,
                    observed_at,
                    model["estimatedUsageCreditsMicros"],
                    model["inputTokens"],
                    model["cachedInputTokens"],
                    model["outputTokens"],
                    model["totalTokens"],
                    period["status"],
                    period["intervalStart"],
                    period["intervalEnd"],
                    period["intervalSeconds"],
                    period["estimatedUsageCreditsMicros"],
                    period["inputTokens"],
                    period["cachedInputTokens"],
                    period["outputTokens"],
                    period["totalTokens"],
                ),
            )
            model["cumulativeUsage"] = {
                "estimatedUsageCreditsMicros": model["estimatedUsageCreditsMicros"],
                "inputTokens": model["inputTokens"],
                "cachedInputTokens": model["cachedInputTokens"],
                "outputTokens": model["outputTokens"],
                "totalTokens": model["totalTokens"],
            }
            model["periodUsageEstimate"] = period
            for key in (
                "estimatedUsageCreditsMicros",
                "inputTokens",
                "cachedInputTokens",
                "outputTokens",
                "totalTokens",
            ):
                del model[key]


def _estimate_period(
    connection: sqlite3.Connection,
    thread_id: str,
    model: str,
    observed_at: str,
    current: dict[str, Any],
) -> dict[str, Any]:
    previous = connection.execute(
        """
        SELECT observed_at, estimated_usage_credits_micros, input_tokens,
            cached_input_tokens, output_tokens, total_tokens
        FROM thread_model_usage
        WHERE thread_id = ? AND model = ?
        ORDER BY observed_at DESC, snapshot_id DESC
        LIMIT 1
        """,
        (thread_id, model),
    ).fetchone()
    estimate: dict[str, Any] = {
        "status": "baseline",
        "intervalStart": None,
        "intervalEnd": observed_at,
        "intervalSeconds": None,
        "estimatedUsageCreditsMicros": None,
        "inputTokens": None,
        "cachedInputTokens": None,
        "outputTokens": None,
        "totalTokens": None,
    }
    if previous is None:
        return estimate

    interval_start = previous[0]
    estimate["intervalStart"] = interval_start
    try:
        end = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        start = datetime.fromisoformat(interval_start.replace("Z", "+00:00"))
        interval_seconds = (end - start).total_seconds()
    except AttributeError, TypeError, ValueError:
        interval_seconds = None
    estimate["intervalSeconds"] = interval_seconds
    if interval_seconds is None or interval_seconds <= 0:
        estimate["status"] = "invalid_interval"
        return estimate

    fields = (
        ("estimatedUsageCreditsMicros", "estimated_usage_credits_micros"),
        ("inputTokens", "input_tokens"),
        ("cachedInputTokens", "cached_input_tokens"),
        ("outputTokens", "output_tokens"),
        ("totalTokens", "total_tokens"),
    )
    if any(
        current[name] is not None
        and previous[index + 1] is not None
        and current[name] < previous[index + 1]
        for index, (name, _) in enumerate(fields)
    ):
        estimate["status"] = "counter_reset"
        return estimate

    estimate["status"] = "estimated"
    for index, (name, _) in enumerate(fields):
        old_value = previous[index + 1]
        new_value = current[name]
        estimate[name] = (
            None if old_value is None or new_value is None else new_value - old_value
        )
    return estimate


def _status_from_row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    collected_at = row[0]
    return {
        "schemaVersion": 4,
        "source": "live",
        "collectedAt": collected_at,
        "fiveHour": _window_from_row(row[1], row[2], row[3]),
        "weekly": _window_from_row(row[4], row[5], row[6]),
        "individualLimit": (
            None
            if len(row) == 7
            else _individual_limit_from_row(row[7], row[8], row[9], row[10], row[11])
        ),
        "modelUsage": _model_usage_from_row(row[12:17]) if len(row) >= 17 else None,
    }


def _statuses_from_thread_rows(rows: Any) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    previous_snapshot_id: int | None = None
    previous_thread_id: str | None = None
    for row in rows:
        snapshot_id = row[0]
        if snapshot_id != previous_snapshot_id:
            status = _status_from_row(row[1:13])
            if row[13] is None:
                status["modelUsage"] = None
            else:
                status["modelUsage"] = {
                    "status": row[13],
                    "observedAt": row[14],
                    "threadListComplete": bool(row[15]),
                    "threadObservationScope": "history_range",
                    "threadCount": row[16],
                    "listedThreadCount": row[16],
                    "targetedThreadCount": row[17],
                    "notTargetedThreadCount": row[18],
                    "attemptedThreadCount": row[19],
                    "notAttemptedThreadCount": row[20],
                    "retrievedThreadCount": row[21],
                    "unavailableThreadCount": row[22],
                    "failedThreadCount": row[23],
                    "activeWithinHours": row[24],
                    "threads": [],
                }
            statuses.append(status)
            previous_snapshot_id = snapshot_id
            previous_thread_id = None

        thread_id = row[25]
        if thread_id is None:
            continue
        model_usage = statuses[-1]["modelUsage"]
        if model_usage is None:
            continue
        if thread_id != previous_thread_id:
            recency_at = row[29]
            updated_at = row[28]
            activity_at = recency_at if recency_at is not None else updated_at
            activity_at_field = (
                "recencyAt"
                if recency_at is not None
                else "updatedAt"
                if updated_at is not None
                else None
            )
            thread = {
                "threadId": thread_id,
                "parentThreadId": row[26],
                "source": row[27],
                "updatedAt": row[28],
                "recencyAt": row[29],
                "activityAt": activity_at,
                "activityAtField": activity_at_field,
                "archived": None if row[30] is None else bool(row[30]),
                "usageStatus": row[31],
                "observedAt": row[32],
                "models": [],
            }
            model_usage["threads"].append(thread)
            previous_thread_id = thread_id
        else:
            thread = model_usage["threads"][-1]

        model = row[33]
        if model is not None:
            thread["models"].append(
                {
                    "model": model or None,
                    "cumulativeUsage": {
                        "estimatedUsageCreditsMicros": row[34],
                        "inputTokens": row[35],
                        "cachedInputTokens": row[36],
                        "outputTokens": row[37],
                        "totalTokens": row[38],
                    },
                    "periodUsageEstimate": {
                        "status": row[39],
                        "intervalStart": row[40],
                        "intervalEnd": row[41],
                        "intervalSeconds": row[42],
                        "estimatedUsageCreditsMicros": row[43],
                        "inputTokens": row[44],
                        "cachedInputTokens": row[45],
                        "outputTokens": row[46],
                        "totalTokens": row[47],
                        "attribution": "detected_at_interval_end",
                    },
                }
            )
    return statuses


def _model_usage_from_row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any] | None:
    status = row[0]
    if status is None:
        return None
    thread_count = row[1]
    retrieved_thread_count = row[2]
    unavailable_thread_count = row[3]
    failed_thread_count = row[4]
    attempted_thread_count = (
        retrieved_thread_count + unavailable_thread_count + failed_thread_count
    )
    return {
        "status": status,
        "threadListComplete": thread_count is not None,
        "threadCount": thread_count,
        "listedThreadCount": thread_count,
        "attemptedThreadCount": attempted_thread_count,
        "notAttemptedThreadCount": (
            None if thread_count is None else thread_count - attempted_thread_count
        ),
        "retrievedThreadCount": retrieved_thread_count,
        "unavailableThreadCount": unavailable_thread_count,
        "failedThreadCount": failed_thread_count,
        "models": [],
    }


def _statuses_from_current_rows(rows: Any) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    previous_snapshot_id: int | None = None
    for row in rows:
        snapshot_id = row[0]
        if snapshot_id != previous_snapshot_id:
            status = _status_from_row(row[1:18])
            statuses.append(status)
            previous_snapshot_id = snapshot_id
        else:
            status = statuses[-1]
        model = row[18]
        if model is not None:
            model_usage = status["modelUsage"]
            if model_usage is not None:
                model_usage["models"].append(
                    {
                        "model": model,
                        "estimatedUsageCreditsMicros": row[19],
                        "inputTokens": row[20],
                        "cachedInputTokens": row[21],
                        "outputTokens": row[22],
                        "totalTokens": row[23],
                    }
                )
    return statuses


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

from __future__ import annotations

import contextlib
import html
import math
import os
import secrets
import sqlite3
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from statistics import median_low
from typing import Any

from .storage import StorageError, load_statuses_since, resolve_db_path

DISPLAY_TZ = None
TZ_LABEL = datetime.now().astimezone().tzname()
FIVE_HOUR_RANGE = timedelta(hours=48)
WEEKLY_RANGE = timedelta(days=14)


class DashboardError(RuntimeError):
    """An error while generating the static usage dashboard."""


def resolve_dashboard_path(db_path: str | Path | None = None) -> Path:
    """Return an isolated dashboard path associated with one usage database."""
    database_path = resolve_db_path(db_path).absolute()
    dashboard_directory = database_path.with_name(f"{database_path.name}.dashboard")
    return dashboard_directory / "index.html"


def generate_dashboard(
    db_path: str | Path | None = None, *, generated_at: datetime | None = None
) -> Path:
    """Write a self-contained HTML dashboard from the last seven days of history."""
    moment = datetime.now(UTC) if generated_at is None else generated_at
    if moment.tzinfo is None:
        raise DashboardError("The dashboard generation time must be timezone-aware.")
    moment = moment.astimezone(UTC)
    try:
        statuses = load_statuses_since(moment - WEEKLY_RANGE, db_path)
        document = _render_dashboard(statuses, moment)
        destination = resolve_dashboard_path(db_path)
        _write_private_file(destination, document)
        return destination
    except DashboardError:
        raise
    except StorageError as exc:
        raise DashboardError(
            f"Could not load usage history for the dashboard: {exc}"
        ) from exc
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise DashboardError(f"Could not generate the usage dashboard: {exc}") from exc


def _write_private_file(destination: Path, content: str) -> None:
    directory = destination.parent
    directory.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        _write_posix_private_file(destination, content)
        return

    with contextlib.suppress(FileExistsError):
        directory.mkdir(mode=0o700)
    directory_info = directory.lstat()
    if not stat.S_ISDIR(directory_info.st_mode) or _is_reparse_point(directory_info):
        raise PermissionError("The dashboard output path must be a real directory.")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=directory,
            prefix=".index-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    except OSError:
        if temporary_path is not None:
            with contextlib.suppress(OSError):
                temporary_path.unlink()
        raise


def _write_posix_private_file(destination: Path, content: str) -> None:
    directory = destination.parent
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, flag) for flag in required_flags):
        raise OSError("Secure dashboard directory access is unavailable.")
    if os.open not in os.supports_dir_fd or os.rename not in os.supports_dir_fd:
        raise OSError("Secure dashboard directory-relative operations are unavailable.")

    with contextlib.suppress(FileExistsError):
        os.mkdir(directory, 0o700)

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(directory, directory_flags)
    try:
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode):
            raise PermissionError("The dashboard output path must be a real directory.")
        if directory_info.st_uid != os.geteuid():
            raise PermissionError(
                "The dashboard output directory must be owned by the current user."
            )
        os.fchmod(directory_fd, 0o700)

        temporary_name: str | None = None
        temporary_fd: int | None = None
        try:
            for _ in range(10):
                candidate = f".index-{secrets.token_hex(12)}.tmp"
                try:
                    temporary_fd = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    temporary_name = candidate
                    break
                except FileExistsError:
                    continue
            if temporary_fd is None or temporary_name is None:
                raise FileExistsError(
                    "Could not reserve a private dashboard temp file."
                )

            temporary_file = os.fdopen(
                temporary_fd, "w", encoding="utf-8", newline="\n"
            )
            temporary_fd = None
            with temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(
                temporary_name,
                destination.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary_name = None
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary_name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _is_reparse_point(info: os.stat_result) -> bool:
    if os.name != "nt":
        return False
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & reparse_attribute)


def _render_dashboard(statuses: list[dict[str, Any]], generated_at: datetime) -> str:
    latest_status = statuses[-1] if statuses else None
    latest_measured_at = (
        latest_status.get("collectedAt") if latest_status is not None else None
    )
    five_hour = _latest_window(latest_status, "fiveHour")
    weekly = _latest_window(latest_status, "weekly")
    five_hour_rows = _window_rows(statuses, "fiveHour", generated_at - FIVE_HOUR_RANGE)
    weekly_rows = _window_rows(statuses, "weekly", generated_at - WEEKLY_RANGE)
    generated_label = _format_timestamp(generated_at.isoformat())
    measured_label = _format_timestamp(latest_measured_at)

    five_hour_chart = _render_chart(
        chart_id="five-hour-chart",
        accessible_title=f"過去{FIVE_HOUR_RANGE.total_seconds() // 3600:.0f}"
        + "時間の5時間枠残率",
        rows=five_hour_rows,
        start=generated_at - FIVE_HOUR_RANGE,
        end=generated_at,
        accent="#8fe1bd",
        empty_label="この期間の5時間枠データはありません。",
    )
    weekly_chart = _render_chart(
        chart_id="weekly-chart",
        accessible_title=f"過去{WEEKLY_RANGE.days}日間の週間枠残率",
        rows=weekly_rows,
        start=generated_at - WEEKLY_RANGE,
        end=generated_at,
        accent="#f0b66e",
        empty_label="この期間の週間枠データはありません。",
    )

    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Codex 利用枠の推移</title>
  <style>
    :root {{
      color-scheme: dark;
      --background: #0b1517;
      --surface: #111f21;
      --surface-raised: #17282a;
      --border: #2c4140;
      --text: #eef3ec;
      --muted: #9aaea7;
      --mint: #8fe1bd;
      --amber: #f0b66e;
      --grid: #2c4140;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background:
        radial-gradient(ellipse at 12% 0%, rgba(81, 143, 123, .16), transparent 38rem),
        linear-gradient(145deg, #0b1517, #0e191b 58%, #111e1f);
      font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      -webkit-font-smoothing: antialiased;
    }}
    main {{
      width: min(100% - 32px, 1040px);
      margin: 0 auto;
      padding: 28px 0 36px;
    }}
    .masthead {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 26px;
    }}
    .eyebrow {{
      margin: 0 0 8px;
      color: var(--mint);
      font: 600 11px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: .16em;
      text-transform: uppercase;
    }}
    h1, h2 {{
      margin: 0;
      font-family: ui-serif, "Iowan Old Style", "Palatino Linotype", Georgia, serif;
      font-weight: 500;
      letter-spacing: -.035em;
    }}
    h1 {{ font-size: clamp(30px, 7vw, 48px); line-height: 1.02; }}
    .subhead {{
      margin: 10px 0 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
    }}
    .refresh {{
      display: grid;
      gap: 6px;
      min-width: 156px;
      padding: 12px 14px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: rgba(17, 31, 33, .8);
    }}
    .refresh span:first-child {{
      color: var(--muted);
      font: 600 10px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    .refresh time {{
      font: 500 13px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    .card {{
      min-width: 0;
      overflow: hidden;
      padding: 19px 18px 14px;
      border: 1px solid var(--border);
      border-radius: 16px;
      background: linear-gradient(
        160deg,
        rgba(23, 40, 42, .94),
        rgba(15, 29, 31, .96)
      );
      box-shadow: 0 16px 44px rgba(0, 0, 0, .16);
    }}
    .card-head {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
    }}
    .window {{
      margin: 0 0 5px;
      color: var(--muted);
      font: 600 10px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: .14em;
      text-transform: uppercase;
    }}
    h2 {{ font-size: 22px; line-height: 1.1; }}
    .range {{
      flex: 0 0 auto;
      padding: 6px 8px;
      border: 1px solid var(--border);
      border-radius: 999px;
      color: var(--muted);
      font: 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .metric-row {{
      display: flex;
      align-items: baseline;
      gap: 8px;
      margin: 18px 0 3px;
    }}
    .metric {{
      color: var(--text);
      font: 500 clamp(34px, 8vw, 48px)/1
        ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: -.07em;
    }}
    .metric span {{
      color: var(--muted);
      font: 13px/1 ui-sans-serif, system-ui, sans-serif;
      letter-spacing: 0;
    }}
    .reset {{
      min-height: 18px;
      margin: 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .chart {{
      display: block;
      width: 100%;
      height: auto;
      margin-top: 12px;
      overflow: visible;
    }}
    .chart text {{
      fill: var(--muted);
      font: 10px ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .chart .axis {{ stroke: var(--grid); stroke-width: 1; }}
    .chart .series {{
      fill: none;
      stroke: currentColor;
      stroke-width: 2.5;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}
    .chart .dot {{ fill: var(--surface); stroke: currentColor; stroke-width: 2; }}
    .empty {{
      display: grid;
      min-height: 140px;
      place-items: center;
      padding: 16px;
      color: var(--muted);
      font-size: 13px;
      text-align: center;
    }}
    .footnote {{
      margin: 18px 2px 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
    }}
    .footnote strong {{ color: var(--text); font-weight: 600; }}
    footer {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 8px 20px;
      margin-top: 24px;
      padding-top: 13px;
      border-top: 1px solid rgba(44, 65, 64, .75);
      color: var(--muted);
      font: 10px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    @media (max-width: 700px) {{
      main {{ width: min(100% - 24px, 560px); padding-top: 20px; }}
      .masthead {{ display: grid; align-items: start; gap: 15px; margin-bottom: 18px; }}
      .refresh {{ display: flex; align-items: center; justify-content: space-between; }}
      .cards {{ grid-template-columns: 1fr; gap: 12px; }}
      .card {{ padding: 17px 14px 11px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{ scroll-behavior: auto !important; }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="masthead">
      <div>
        <p class="eyebrow">Codex · 利用枠モニター</p>
        <h1>残り利用枠</h1>
        <p class="subhead">5時間枠と週間枠の最近の推移です。</p>
      </div>
    </header>
    <div class="cards">
      <section class="card" aria-labelledby="five-hour-title">
        <div class="card-head">
          <div>
            <h2 id="five-hour-title">5時間枠</h2>
          </div>
          <span class="range">{FIVE_HOUR_RANGE.total_seconds() // 3600:.0f}時間</span>
        </div>
        <div class="metric-row">
          <strong class="metric">{_format_remaining(five_hour)}</strong>
          <span>残り</span>
        </div>
        <p class="reset">{_reset_caption(five_hour)}</p>
        {five_hour_chart}
      </section>
      <section class="card" aria-labelledby="weekly-title">
        <div class="card-head">
          <div>
            <h2 id="weekly-title">週間枠</h2>
          </div>
          <span class="range">{WEEKLY_RANGE.days}日間</span>
        </div>
        <div class="metric-row">
          <strong class="metric">{_format_remaining(weekly)}</strong>
          <span>残り</span>
        </div>
        <p class="reset">{_reset_caption(weekly)}</p>
        {weekly_chart}
      </section>
    </div>
    <p class="footnote">
      このページは {html.escape(generated_label)} {TZ_LABEL} に生成された
      静的スナップショットです。
    </p>
    <footer>
      <span>生成時刻 {html.escape(generated_label)} {TZ_LABEL}</span>
      <span>最終計測 {html.escape(measured_label)} {TZ_LABEL}</span>
    </footer>
  </main>
</body>
</html>
"""


def _latest_window(
    latest_status: dict[str, Any] | None, key: str
) -> dict[str, Any] | None:
    if latest_status is None:
        return None
    window = latest_status.get(key)
    if not isinstance(window, dict):
        return None
    return window


def _remaining_percent(window: dict[str, Any] | None) -> float | None:
    if window is None:
        return None
    used_percent = window.get("usedPercent")
    if (
        not isinstance(used_percent, (int, float))
        or isinstance(used_percent, bool)
        or not math.isfinite(used_percent)
    ):
        return None
    return 100.0 - used_percent


def _format_remaining(window: dict[str, Any] | None) -> str:
    remaining = _remaining_percent(window)
    if remaining is None:
        return "—"
    return f"{remaining:.0f}%"


def _reset_caption(window: dict[str, Any] | None) -> str:
    if window is None:
        return "最新の計測で利用枠を取得できませんでした。"
    reset_at = _format_timestamp(window.get("resetsAt"))
    if reset_at == "—":
        return "リセット時刻は取得できませんでした。"
    return f"リセット {reset_at} {TZ_LABEL}"


def _window_rows(
    statuses: list[dict[str, Any]], key: str, since: datetime
) -> list[tuple[datetime, float | None]]:
    rows: list[tuple[datetime, float | None]] = []
    for status in statuses:
        moment = _parse_timestamp(status.get("collectedAt"))
        if moment is None or moment < since:
            continue
        window = status.get(key)
        if not isinstance(window, dict):
            rows.append((moment, None))
            continue
        rows.append((moment, _remaining_percent(window)))
    return rows


def _render_chart(
    *,
    chart_id: str,
    accessible_title: str,
    rows: list[tuple[datetime, float | None]],
    start: datetime,
    end: datetime,
    accent: str,
    empty_label: str,
) -> str:
    width = 760
    height = 248
    left = 54.0
    right = 742.0
    top = 16.0
    bottom = 207.0
    plot_width = right - left
    plot_height = bottom - top
    span_seconds = max((end - start).total_seconds(), 1.0)
    valid_values = [value for _, value in rows if value is not None]
    if not valid_values:
        label = html.escape(empty_label)
        return f'<div class="empty" role="img" aria-label="{label}">{label}</div>'

    all_times = [moment for moment, _ in rows]
    expected_gap = _expected_gap_seconds(all_times)
    segments: list[list[tuple[datetime, float]]] = []
    current: list[tuple[datetime, float]] = []
    previous_time: datetime | None = None
    for moment, value in rows:
        if value is None:
            if current:
                segments.append(current)
                current = []
            previous_time = moment
            continue
        if (
            current
            and previous_time is not None
            and (moment - previous_time).total_seconds() > expected_gap
        ):
            segments.append(current)
            current = []
        current.append((moment, value))
        previous_time = moment
    if current:
        segments.append(current)

    def x_position(moment: datetime) -> float:
        ratio = (moment - start).total_seconds() / span_seconds
        return left + min(max(ratio, 0.0), 1.0) * plot_width

    def y_position(value: float) -> float:
        bounded = min(max(value, 0.0), 100.0)
        return bottom - (bounded / 100.0) * plot_height

    grid: list[str] = []
    for value in (0, 25, 50, 75, 100):
        y = y_position(float(value))
        grid.append(
            f'<line class="axis" x1="{left:.1f}" y1="{y:.1f}" '
            f'x2="{right:.1f}" y2="{y:.1f}" />'
        )
        grid.append(
            f'<text x="{left - 10:.1f}" y="{y + 3.5:.1f}" '
            f'text-anchor="end">{value}</text>'
        )

    x_labels = []
    for fraction in (0.0, 0.5, 1.0):
        position = left + fraction * plot_width
        tick = start + (end - start) * fraction
        label = tick.astimezone(DISPLAY_TZ).strftime("%m/%d %H:%M")
        anchor = "start" if fraction == 0 else "end" if fraction == 1 else "middle"
        x_labels.append(
            f'<text x="{position:.1f}" y="{height - 12}" '
            f'text-anchor="{anchor}">{html.escape(label)}</text>'
        )

    paths: list[str] = []
    dots: list[str] = []
    for segment in segments:
        coordinates = [
            (x_position(moment), y_position(value)) for moment, value in segment
        ]
        if len(coordinates) > 1:
            points = " ".join(f"{x:.1f},{y:.1f}" for x, y in coordinates)
            paths.append(f'<polyline class="series" points="{points}" />')
        for x, y in coordinates:
            dots.append(f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="3" />')

    title = html.escape(accessible_title)
    chart_label = html.escape(
        f"{accessible_title}; percentages from zero to one hundred"
    )
    svg_content = "\n      ".join([*grid, *x_labels, *paths, *dots])
    return f"""<svg
  id="{chart_id}"
  class="chart"
  viewBox="0 0 {width} {height}"
  role="img"
  aria-label="{chart_label}"
  style="color: {accent}"
>
  <title>{title}</title>
  <desc>
    Remaining quota percentage over time. Missing observations and long gaps
    break the line.
  </desc>
  {svg_content}
</svg>"""


def _expected_gap_seconds(times: list[datetime]) -> float:
    differences = sorted(
        (right - left).total_seconds()
        for left, right in pairwise(times)
        if right > left
    )
    if not differences:
        return float("inf")
    return max(median_low(differences) * 2.5, 60.0)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo is not None else None


def _format_timestamp(value: Any) -> str:
    moment = _parse_timestamp(value)
    if moment is None:
        return "—"
    return moment.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")

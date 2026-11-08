import unittest
from datetime import UTC, datetime, timedelta

from codex_usage.notifications import find_rate_limit_alerts


class FindRateLimitAlertsTests(unittest.TestCase):
    def test_one_second_reset_time_jitter_does_not_repeat_alerts(self) -> None:
        reset_at = datetime(2026, 11, 7, 16, 6, 13, tzinfo=UTC)

        for window in ("fiveHour", "weekly"):
            for jitter in (timedelta(seconds=1), timedelta(seconds=-1)):
                with self.subTest(window=window, jitter=jitter):
                    previous = _status(
                        remaining=4, reset_at=reset_at, window=window
                    )
                    current = _status(
                        remaining=4,
                        reset_at=reset_at + jitter,
                        window=window,
                    )

                    self.assertEqual(find_rate_limit_alerts(previous, current), [])

    def test_threshold_crossing_during_reset_time_jitter_still_alerts(self) -> None:
        reset_at = datetime(2026, 11, 7, 16, 6, 13, tzinfo=UTC)
        for window in ("fiveHour", "weekly"):
            with self.subTest(window=window):
                previous = _status(remaining=8, reset_at=reset_at, window=window)
                current = _status(
                    remaining=4,
                    reset_at=reset_at + timedelta(seconds=1),
                    window=window,
                )

                alerts = find_rate_limit_alerts(previous, current)

                self.assertEqual([alert.threshold for alert in alerts], [5])

    def test_two_second_reset_time_change_rebases_previous_remaining(self) -> None:
        reset_at = datetime(2026, 11, 7, 16, 6, 13, tzinfo=UTC)
        previous = _status(remaining=4, reset_at=reset_at)
        current = _status(
            remaining=4, reset_at=reset_at + timedelta(seconds=2)
        )

        alerts = find_rate_limit_alerts(previous, current)

        self.assertEqual([alert.threshold for alert in alerts], [50, 20, 5])

    def test_five_hour_and_weekly_rollovers_start_new_periods(self) -> None:
        windows = (
            (
                "fiveHour",
                datetime(2026, 11, 7, 16, 6, 13, tzinfo=UTC),
                timedelta(hours=5),
            ),
            (
                "weekly",
                datetime(2026, 11, 11, 22, 25, 45, tzinfo=UTC),
                timedelta(days=7),
            ),
        )

        for window, reset_at, period in windows:
            with self.subTest(window=window):
                previous = _status(remaining=4, reset_at=reset_at, window=window)
                current = _status(
                    remaining=45,
                    reset_at=reset_at + period,
                    window=window,
                )

                alerts = find_rate_limit_alerts(previous, current)

                self.assertEqual(
                    [(alert.window_name, alert.threshold) for alert in alerts],
                    [
                        (
                            "5時間枠" if window == "fiveHour" else "1週間枠",
                            50,
                        )
                    ],
                )


def _status(
    remaining: float,
    reset_at: datetime,
    window: str = "fiveHour",
) -> dict[str, object]:
    return {
        "collectedAt": "2026-11-07T16:00:00Z",
        "fiveHour": _window(remaining, reset_at) if window == "fiveHour" else None,
        "weekly": _window(remaining, reset_at) if window == "weekly" else None,
    }


def _window(remaining: float, reset_at: datetime) -> dict[str, object]:
    return {
        "usedPercent": 100 - remaining,
        "resetsAt": reset_at.isoformat().replace("+00:00", "Z"),
    }


if __name__ == "__main__":
    unittest.main()

"""Backfill kalendarza — zakres sesji, wznawianie, odporność na awarie."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest

from emscan.db import open_memory_db
from emscan.engine.backfill import run_backfill, trading_days
from emscan.models import Timing
from emscan.sources.base import SourceUnavailable
from fakes import FakeCalendar, record

FETCHED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

MON = date(2026, 8, 17)
TUE = date(2026, 8, 18)
WED = date(2026, 8, 19)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with open_memory_db() as connection:
        yield connection


def run(
    conn: sqlite3.Connection,
    calendars: list[FakeCalendar],
    *,
    start: date = MON,
    end: date = WED,
    resume: bool = True,
) -> object:
    return run_backfill(
        start=start,
        end=end,
        conn=conn,
        calendars=calendars,
        fetched_at=FETCHED_AT,
        resume=resume,
    )


# ------------------------------------------------------------------ zakres dni


def test_only_trading_days_are_visited() -> None:
    """Publikacja w weekend u spółek amerykańskich jest niespotykana, a to 230 zapytań mniej."""
    days = trading_days(date(2026, 8, 14), date(2026, 8, 18))
    assert days == [date(2026, 8, 14), MON, TUE]


def test_holiday_is_skipped() -> None:
    """3 lipca 2026 to obchodzony Dzień Niepodległości."""
    days = trading_days(date(2026, 7, 2), date(2026, 7, 6))
    assert days == [date(2026, 7, 2), date(2026, 7, 6)]


def test_range_is_inclusive_on_both_ends() -> None:
    assert trading_days(MON, MON) == [MON]


def test_range_without_any_session_is_empty() -> None:
    assert trading_days(date(2026, 8, 15), date(2026, 8, 16)) == []


# ------------------------------------------------------------------ zapis


def test_events_from_every_day_land_in_the_database(conn: sqlite3.Connection) -> None:
    calendar = FakeCalendar(
        [
            record("AMAT", MON, Timing.AMC),
            record("NVDA", TUE, Timing.BMO),
            record("LOW", WED, Timing.UNKNOWN),
        ]
    )
    result = run(conn, [calendar])

    stored = {
        (row["ticker"], row["event_date"])
        for row in conn.execute("SELECT ticker, event_date FROM earnings_events")
    }
    assert stored == {("AMAT", "2026-08-17"), ("NVDA", "2026-08-18"), ("LOW", "2026-08-19")}
    assert result.days_considered == 3  # type: ignore[attr-defined]
    assert result.events_written == 3  # type: ignore[attr-defined]
    assert result.complete is True  # type: ignore[attr-defined]


def test_day_without_publications_is_not_a_failure(conn: sqlite3.Connection) -> None:
    result = run(conn, [FakeCalendar([])])
    assert result.days_fetched == 3  # type: ignore[attr-defined]
    assert result.events_written == 0  # type: ignore[attr-defined]
    assert result.complete is True  # type: ignore[attr-defined]


def test_two_sources_are_merged_within_a_day(conn: sqlite3.Connection) -> None:
    """Scalanie jest per dzień, tak jak w skanie — zgodne źródła dają jedno zdarzenie."""
    first = FakeCalendar([record("AMAT", MON, Timing.AMC)], name="nasdaq")
    second = FakeCalendar([record("AMAT", MON, Timing.AMC)], name="finnhub")
    run(conn, [first, second], end=MON)

    row = conn.execute("SELECT timing_confidence, sources_json FROM earnings_events").fetchone()
    assert row["timing_confidence"] == "HIGH"
    assert "nasdaq" in row["sources_json"] and "finnhub" in row["sources_json"]


# ------------------------------------------------------------------ wznawianie


def test_resume_skips_days_already_in_the_database(conn: sqlite3.Connection) -> None:
    """Backfill dwóch lat to kilka minut — przerwany run musi dać się dokończyć."""
    calendar = FakeCalendar([record("AMAT", MON, Timing.AMC), record("NVDA", TUE, Timing.BMO)])
    run(conn, [calendar], start=MON, end=MON)
    assert calendar.days_fetched == [MON]

    second = FakeCalendar([record("AMAT", MON, Timing.AMC), record("NVDA", TUE, Timing.BMO)])
    result = run(conn, [second], start=MON, end=TUE)
    assert second.days_fetched == [TUE]  # poniedziałek pominięty
    assert result.days_skipped == 1  # type: ignore[attr-defined]


def test_no_resume_refetches_everything(conn: sqlite3.Connection) -> None:
    calendar = FakeCalendar([record("AMAT", MON, Timing.AMC)])
    run(conn, [calendar], start=MON, end=MON)
    second = FakeCalendar([record("AMAT", MON, Timing.AMC)])
    run(conn, [second], start=MON, end=MON, resume=False)
    assert second.days_fetched == [MON]


def test_progress_callback_fires_once_per_day(conn: sqlite3.Connection) -> None:
    seen: list[tuple[date, int, int]] = []
    run_backfill(
        start=MON,
        end=WED,
        conn=conn,
        calendars=[FakeCalendar([])],
        fetched_at=FETCHED_AT,
        on_day=lambda day, index, total: seen.append((day, index, total)),
    )
    assert [item[0] for item in seen] == [MON, TUE, WED]
    assert seen[-1][1:] == (3, 3)


# ------------------------------------------------------------------ awarie


def test_one_broken_day_does_not_stop_the_run(conn: sqlite3.Connection) -> None:
    calendar = FakeCalendar(
        [record("AMAT", MON, Timing.AMC), record("NVDA", WED, Timing.BMO)],
        failing_days=frozenset({TUE}),
    )
    result = run(conn, [calendar])
    assert result.days_fetched == 2  # type: ignore[attr-defined]
    assert len(result.failures) == 1  # type: ignore[attr-defined]
    assert result.complete is False  # type: ignore[attr-defined]
    assert "2026-08-18" in result.failures[0]  # type: ignore[attr-defined]


def test_a_failed_day_is_retried_on_the_next_run(conn: sqlite3.Connection) -> None:
    """Dzień, który padł, nie ma wierszy w bazie, więc wznowienie go nie pominie."""
    records = [record("AMAT", MON, Timing.AMC), record("NVDA", TUE, Timing.BMO)]
    first = FakeCalendar(records, failing_days=frozenset({TUE}))
    run(conn, [first], start=MON, end=TUE)

    second = FakeCalendar(records)
    result = run(conn, [second], start=MON, end=TUE)
    assert second.days_fetched == [TUE]  # poniedziałek już jest, wtorek dopobrany
    assert result.events_written == 1  # type: ignore[attr-defined]


def test_everything_failing_raises(conn: sqlite3.Connection) -> None:
    """Backfill, który nie pobrał ani jednego dnia, nie może udawać sukcesu."""
    calendar = FakeCalendar([], failing_days=frozenset({MON, TUE, WED}))
    with pytest.raises(SourceUnavailable):
        run(conn, [calendar])


def test_empty_range_is_not_a_failure(conn: sqlite3.Connection) -> None:
    result = run(conn, [FakeCalendar([])], start=date(2026, 8, 15), end=date(2026, 8, 16))
    assert result.days_considered == 0  # type: ignore[attr-defined]
    assert result.complete is True  # type: ignore[attr-defined]


# ------------------------------------------------------------------ duplikaty daty


def test_backfill_marks_date_conflicts(conn: sqlite3.Connection) -> None:
    """Dwa źródła niezgodne co do daty dają dwa wiersze — post-pass je oznacza."""
    nasdaq = FakeCalendar([record("ACTU", MON, Timing.UNKNOWN)], name="nasdaq")
    finnhub = FakeCalendar([record("ACTU", TUE, Timing.UNKNOWN)], name="finnhub")
    result = run(conn, [nasdaq, finnhub])

    assert result.conflicts_marked == 2  # type: ignore[attr-defined]
    marked = conn.execute(
        "SELECT COUNT(*) AS n FROM earnings_events WHERE date_conflict = 1"
    ).fetchone()["n"]
    assert marked == 2

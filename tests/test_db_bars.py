"""Tabela świec — cache historii cen i podstawa cech reżimu rynkowego."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest

from emscan.db import bars_for, open_memory_db, record_iv30, upsert_bars
from emscan.sources.base import DailyBar

FETCHED_AT = datetime(2026, 8, 19, 21, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with open_memory_db() as connection:
        yield connection


def bar(day: date, *, close: float = 100.0, volume: int = 1_000_000) -> DailyBar:
    return DailyBar(day=day, open=99.0, high=101.0, low=98.0, close=close, volume=volume)


def test_bars_survive_the_round_trip(conn: sqlite3.Connection) -> None:
    written = upsert_bars(
        conn, "SPY", [bar(date(2026, 8, 18), close=767.45)], fetched_at=FETCHED_AT
    )
    assert written == 1
    restored = bars_for(conn, "SPY", date(2026, 8, 1), date(2026, 8, 31))
    assert restored == [bar(date(2026, 8, 18), close=767.45)]


def test_ticker_is_normalised(conn: sqlite3.Connection) -> None:
    upsert_bars(conn, " spy ", [bar(date(2026, 8, 18))], fetched_at=FETCHED_AT)
    assert len(bars_for(conn, "SPY", date(2026, 8, 18), date(2026, 8, 18))) == 1


def test_overlapping_windows_do_not_multiply_rows(conn: sqlite3.Connection) -> None:
    """Sedno cache'u: kolejne dni skanu pobierają nakładające się okna 45 sesji."""
    first = [bar(date(2026, 8, day)) for day in (17, 18)]
    second = [bar(date(2026, 8, day)) for day in (18, 19)]
    upsert_bars(conn, "AMAT", first, fetched_at=FETCHED_AT)
    upsert_bars(conn, "AMAT", second, fetched_at=FETCHED_AT)
    assert conn.execute("SELECT COUNT(*) AS n FROM daily_bars").fetchone()["n"] == 3


def test_newer_version_of_a_bar_wins(conn: sqlite3.Connection) -> None:
    """Dostawca koryguje ceny wstecz o splity (METHODOLOGY §6), więc świeższa wersja rządzi."""
    upsert_bars(conn, "LRCX", [bar(date(2026, 8, 18), close=830.0)], fetched_at=FETCHED_AT)
    upsert_bars(conn, "LRCX", [bar(date(2026, 8, 18), close=83.0)], fetched_at=FETCHED_AT)
    restored = bars_for(conn, "LRCX", date(2026, 8, 18), date(2026, 8, 18))
    assert restored[0].close == pytest.approx(83.0)


def test_range_is_inclusive_and_sorted(conn: sqlite3.Connection) -> None:
    upsert_bars(
        conn,
        "SPY",
        [bar(date(2026, 8, day)) for day in (17, 18, 19)],
        fetched_at=FETCHED_AT,
    )
    days = [b.day for b in bars_for(conn, "SPY", date(2026, 8, 17), date(2026, 8, 18))]
    assert days == [date(2026, 8, 17), date(2026, 8, 18)]


def test_empty_input_writes_nothing(conn: sqlite3.Connection) -> None:
    assert upsert_bars(conn, "SPY", [], fetched_at=FETCHED_AT) == 0


def test_missing_ticker_gives_no_bars(conn: sqlite3.Connection) -> None:
    assert bars_for(conn, "NOPE", date(2026, 1, 1), date(2026, 12, 31)) == []


# ------------------------------------------------------------------ iv30


def test_iv30_lands_on_the_measured_session(conn: sqlite3.Connection) -> None:
    upsert_bars(conn, "SPY", [bar(date(2026, 8, 18))], fetched_at=FETCHED_AT)
    assert record_iv30(conn, "SPY", date(2026, 8, 18), 0.1206) is True
    row = conn.execute("SELECT iv30 FROM daily_bars WHERE ticker = 'SPY'").fetchone()
    assert row["iv30"] == pytest.approx(0.1206)


def test_iv30_without_a_bar_is_refused(conn: sqlite3.Connection) -> None:
    """Brak świecy znaczy, że sesja się nie zamknęła. Nie tworzymy świecy bez OHLC."""
    assert record_iv30(conn, "SPY", date(2026, 8, 18), 0.1206) is False
    assert conn.execute("SELECT COUNT(*) AS n FROM daily_bars").fetchone()["n"] == 0


def test_upsert_does_not_clear_iv30(conn: sqlite3.Connection) -> None:
    """OHLC i iv30 pochodzą z różnych źródeł — odświeżenie świecy nie może gubić pomiaru."""
    upsert_bars(conn, "SPY", [bar(date(2026, 8, 18))], fetched_at=FETCHED_AT)
    record_iv30(conn, "SPY", date(2026, 8, 18), 0.1206)
    upsert_bars(conn, "SPY", [bar(date(2026, 8, 18), close=768.0)], fetched_at=FETCHED_AT)
    row = conn.execute("SELECT iv30, close FROM daily_bars WHERE ticker = 'SPY'").fetchone()
    assert row["iv30"] == pytest.approx(0.1206)
    assert row["close"] == pytest.approx(768.0)

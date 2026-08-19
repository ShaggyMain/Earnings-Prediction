"""Duplikaty daty publikacji i migracja schematu — METHODOLOGY §7."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from emscan.db import (
    conflicting_events,
    init_schema,
    mark_date_conflicts,
    open_db,
    open_memory_db,
    upsert_events,
)
from emscan.models import EarningsEvent, Timing, TimingConfidence

FETCHED_AT = datetime(2026, 8, 17, 19, 30, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with open_memory_db() as connection:
        yield connection


def event(ticker: str, event_date: date, *, timing: Timing = Timing.UNKNOWN) -> EarningsEvent:
    return EarningsEvent(
        ticker=ticker,
        event_date=event_date,
        timing=timing,
        timing_confidence=TimingConfidence.UNKNOWN,
        session_date=None,
        fetched_at=FETCHED_AT,
    )


def flags(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    return {
        (row["ticker"], row["event_date"]): row["date_conflict"]
        for row in conn.execute("SELECT ticker, event_date, date_conflict FROM earnings_events")
    }


def test_adjacent_days_mark_both_rows(conn: sqlite3.Connection) -> None:
    """Spółka nie raportuje dwa razy w odstępie doby — to jedna publikacja, dwie daty."""
    upsert_events(conn, [event("ACTU", date(2026, 8, 13)), event("ACTU", date(2026, 8, 14))])
    assert mark_date_conflicts(conn) == 2
    assert flags(conn) == {("ACTU", "2026-08-13"): 1, ("ACTU", "2026-08-14"): 1}


def test_the_record_stays_in_the_database(conn: sqlite3.Connection) -> None:
    """SPEC §1.4: flagujemy, nie usuwamy. Faza 2 wyklucza to jednym predykatem."""
    upsert_events(conn, [event("ACTU", date(2026, 8, 13)), event("ACTU", date(2026, 8, 14))])
    mark_date_conflicts(conn)
    kept = conn.execute("SELECT COUNT(*) AS n FROM earnings_events").fetchone()["n"]
    assert kept == 2


def test_normal_quarterly_spacing_is_not_a_conflict(conn: sqlite3.Connection) -> None:
    upsert_events(conn, [event("AMAT", date(2026, 5, 14)), event("AMAT", date(2026, 8, 13))])
    assert mark_date_conflicts(conn) == 0
    assert set(flags(conn).values()) == {0}


def test_three_days_apart_is_not_a_conflict(conn: sqlite3.Connection) -> None:
    """Próg to jeden dzień. Trzy dni to już przełożona publikacja, nie niezgodność źródeł."""
    upsert_events(conn, [event("FSI", date(2026, 8, 13)), event("FSI", date(2026, 8, 16))])
    assert mark_date_conflicts(conn) == 0


def test_different_tickers_on_adjacent_days_are_fine(conn: sqlite3.Connection) -> None:
    upsert_events(conn, [event("AMAT", date(2026, 8, 13)), event("NVDA", date(2026, 8, 14))])
    assert mark_date_conflicts(conn) == 0


def test_marking_is_idempotent(conn: sqlite3.Connection) -> None:
    """Drugi przebieg nie ma czego oznaczać — backfill woła to po każdym runie."""
    upsert_events(conn, [event("ACTU", date(2026, 8, 13)), event("ACTU", date(2026, 8, 14))])
    assert mark_date_conflicts(conn) == 2
    assert mark_date_conflicts(conn) == 0


def test_pair_split_across_two_runs_is_still_caught(conn: sqlite3.Connection) -> None:
    """Backfill wznawiany po przerwaniu zapisuje dni w osobnych runach — post-pass to łapie."""
    upsert_events(conn, [event("ACTU", date(2026, 8, 13))])
    assert mark_date_conflicts(conn) == 0
    upsert_events(conn, [event("ACTU", date(2026, 8, 14))])
    assert mark_date_conflicts(conn) == 2


def test_conflicting_events_lists_the_pairs(conn: sqlite3.Connection) -> None:
    upsert_events(
        conn,
        [
            event("ACTU", date(2026, 8, 13)),
            event("ACTU", date(2026, 8, 14)),
            event("AMAT", date(2026, 8, 13)),
        ],
    )
    mark_date_conflicts(conn)
    assert conflicting_events(conn) == [
        ("ACTU", date(2026, 8, 13)),
        ("ACTU", date(2026, 8, 14)),
    ]


def test_upsert_does_not_clear_the_flag(conn: sqlite3.Connection) -> None:
    """Kolejny skan nadpisuje timing i fetched_at, ale flagi nie rusza — jest wyliczana osobno."""
    upsert_events(conn, [event("ACTU", date(2026, 8, 13)), event("ACTU", date(2026, 8, 14))])
    mark_date_conflicts(conn)
    upsert_events(conn, [event("ACTU", date(2026, 8, 13), timing=Timing.AMC)])
    assert flags(conn)[("ACTU", "2026-08-13")] == 1


# ------------------------------------------------------------------ migracja


OLD_SCHEMA = """
CREATE TABLE earnings_events (
    id                 INTEGER PRIMARY KEY,
    ticker             TEXT    NOT NULL,
    company_name       TEXT,
    event_date         TEXT    NOT NULL,
    timing             TEXT    NOT NULL,
    session_date       TEXT,
    timing_confidence  TEXT    NOT NULL,
    eps_actual_present INTEGER NOT NULL DEFAULT 0,
    sources_json       TEXT    NOT NULL,
    fetched_at         TEXT    NOT NULL,
    UNIQUE(ticker, event_date)
);
"""


def test_existing_database_gets_the_new_column(tmp_path: Path) -> None:
    """Baza z wcześniejszego skanu nie dostałaby kolumny z `CREATE TABLE IF NOT EXISTS`."""
    db_path = tmp_path / "old.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(OLD_SCHEMA)
    legacy.commit()
    legacy.close()

    with open_db(db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(earnings_events)")}
        assert "date_conflict" in columns
        upsert_events(conn, [event("ACTU", date(2026, 8, 13)), event("ACTU", date(2026, 8, 14))])
        assert mark_date_conflicts(conn) == 2


def test_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "twice.db"
    with open_db(db_path) as conn:
        init_schema(conn)
        init_schema(conn)
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(earnings_events)")]
        assert columns.count("date_conflict") == 1

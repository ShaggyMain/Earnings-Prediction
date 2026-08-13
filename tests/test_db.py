"""Baza SQLite — schemat i operacje na zdarzeniach."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from sqlite3 import Connection

import pytest

from emscan.db import (
    event_id_for,
    events_by_event_date,
    events_by_session_date,
    init_schema,
    open_db,
    upsert_events,
)
from emscan.models import EarningsEvent, RawEarningsRecord, Timing, TimingConfidence

DAY_13 = date(2026, 8, 13)
DAY_14 = date(2026, 8, 14)
FETCHED_AT = datetime(2026, 8, 13, 19, 30, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Connection]:
    with open_db(tmp_path / "test.db") as connection:
        yield connection


def make_event(
    ticker: str,
    *,
    event_date: date = DAY_13,
    timing: Timing = Timing.AMC,
    confidence: TimingConfidence = TimingConfidence.HIGH,
    session_date: date | None = DAY_14,
    sources: dict[str, RawEarningsRecord] | None = None,
) -> EarningsEvent:
    return EarningsEvent(
        ticker=ticker,
        event_date=event_date,
        timing=timing,
        timing_confidence=confidence,
        session_date=session_date,
        company_name=f"{ticker} Inc.",
        sources=sources or {},
        fetched_at=FETCHED_AT,
    )


def test_schema_is_idempotent(tmp_path: Path) -> None:
    with open_db(tmp_path / "x.db") as conn:
        init_schema(conn)
        init_schema(conn)
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"earnings_events", "em_snapshots", "outcomes"} <= tables


def test_roundtrip_preserves_the_event(conn: Connection) -> None:
    upsert_events(conn, [make_event("AMAT")])
    (loaded,) = events_by_event_date(conn, DAY_13)
    assert loaded.ticker == "AMAT"
    assert loaded.timing == Timing.AMC
    assert loaded.timing_confidence == TimingConfidence.HIGH
    assert loaded.session_date == DAY_14
    assert loaded.fetched_at == FETCHED_AT


def test_roundtrip_preserves_raw_sources(conn: Connection) -> None:
    """`sources_json` musi przeżyć zapis — to on chroni odczyty przy konflikcie."""
    sources = {
        "nasdaq": RawEarningsRecord(
            source="nasdaq",
            ticker="FTLF",
            event_date=DAY_13,
            timing=Timing.AMC,
            eps_estimate=0.18,
        ),
        "finnhub": RawEarningsRecord(
            source="finnhub",
            ticker="FTLF",
            event_date=DAY_13,
            timing=Timing.BMO,
            eps_actual=0.2,
        ),
    }
    upsert_events(
        conn,
        [
            make_event(
                "FTLF",
                timing=Timing.UNKNOWN,
                confidence=TimingConfidence.LOW,
                session_date=None,
                sources=sources,
            )
        ],
    )
    (loaded,) = events_by_event_date(conn, DAY_13)
    assert loaded.sources["nasdaq"].timing == Timing.AMC
    assert loaded.sources["finnhub"].timing == Timing.BMO
    assert loaded.sources["finnhub"].eps_actual == pytest.approx(0.2)


def test_null_session_date_survives(conn: Connection) -> None:
    upsert_events(
        conn,
        [
            make_event(
                "NU",
                timing=Timing.UNKNOWN,
                confidence=TimingConfidence.UNKNOWN,
                session_date=None,
            )
        ],
    )
    (loaded,) = events_by_event_date(conn, DAY_13)
    assert loaded.session_date is None
    assert not loaded.is_scannable


def test_dmh_is_storable(conn: Connection) -> None:
    """DMH trafia do bazy z pełną informacją, choć EM dla niego nie liczymy."""
    upsert_events(
        conn,
        [make_event("XYZ", timing=Timing.DMH, confidence=TimingConfidence.HIGH, session_date=None)],
    )
    (loaded,) = events_by_event_date(conn, DAY_13)
    assert loaded.timing == Timing.DMH
    assert not loaded.is_scannable


def test_upsert_overwrites_changed_timing(conn: Connection) -> None:
    """Flaga BMO/AMC potrafi się zmienić przed publikacją — interesuje nas stan świeży."""
    upsert_events(conn, [make_event("AMAT", timing=Timing.AMC, session_date=DAY_14)])
    upsert_events(
        conn,
        [
            make_event(
                "AMAT",
                timing=Timing.BMO,
                confidence=TimingConfidence.MEDIUM,
                session_date=DAY_13,
            )
        ],
    )
    events = events_by_event_date(conn, DAY_13)
    assert len(events) == 1
    assert events[0].timing == Timing.BMO
    assert events[0].session_date == DAY_13


def test_same_ticker_on_different_dates_are_separate_rows(conn: Connection) -> None:
    upsert_events(
        conn,
        [
            make_event("ACTU", event_date=DAY_13, session_date=None),
            make_event("ACTU", event_date=DAY_14, session_date=None),
        ],
    )
    assert len(events_by_event_date(conn, DAY_13)) == 1
    assert len(events_by_event_date(conn, DAY_14)) == 1


def test_session_query_groups_amc_and_next_day_bmo(conn: Connection) -> None:
    """SPEC §1 „Kluczowa obserwacja o grupowaniu" — obie grupy w jednym zapytaniu."""
    upsert_events(
        conn,
        [
            make_event("AMAT", event_date=DAY_13, timing=Timing.AMC, session_date=DAY_14),
            make_event("CSPI", event_date=DAY_14, timing=Timing.BMO, session_date=DAY_14),
            make_event("BN", event_date=DAY_13, timing=Timing.BMO, session_date=DAY_13),
        ],
    )
    tickers = [e.ticker for e in events_by_session_date(conn, DAY_14)]
    assert tickers == ["AMAT", "CSPI"]


def test_event_id_lookup(conn: Connection) -> None:
    upsert_events(conn, [make_event("AMAT")])
    assert event_id_for(conn, "amat", DAY_13) is not None
    assert event_id_for(conn, "NIEMA", DAY_13) is None


def test_upsert_of_empty_iterable_is_a_noop(conn: Connection) -> None:
    assert upsert_events(conn, []) == 0

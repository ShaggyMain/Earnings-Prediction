"""Zapis i odczyt snapshotów EM — tabela `em_snapshots`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from emscan.db import (
    event_id_for,
    insert_snapshot,
    latest_snapshots_for_session,
    open_db,
    open_memory_db,
    upsert_events,
)
from emscan.models import (
    EarningsEvent,
    EmSnapshot,
    QualityFlag,
    Timing,
    TimingConfidence,
)

ET = ZoneInfo("America/New_York")

EVENT_DAY = date(2026, 8, 17)
SESSION = date(2026, 8, 18)
EXPIRY = date(2026, 8, 21)
FETCHED_AT = datetime(2026, 8, 17, 19, 30, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with open_db(tmp_path / "test.db") as connection:
        yield connection


def event(ticker: str, *, session_date: date | None = SESSION) -> EarningsEvent:
    return EarningsEvent(
        ticker=ticker,
        event_date=EVENT_DAY,
        timing=Timing.AMC,
        timing_confidence=TimingConfidence.HIGH,
        session_date=session_date,
        company_name=f"{ticker} Inc.",
        fetched_at=FETCHED_AT,
    )


def snapshot(event_id: int, *, minute: int = 30, em_pct: float | None = 0.08) -> EmSnapshot:
    return EmSnapshot(
        event_id=event_id,
        snapshot_at=datetime(2026, 8, 17, 15, minute, tzinfo=ET),
        spot=100.0,
        expiry=EXPIRY,
        dte=4,
        atm_strike=100.0,
        call_bid=4.9,
        call_ask=5.1,
        put_bid=4.8,
        put_ask=5.2,
        call_mid=5.0,
        put_mid=5.0,
        straddle=10.0,
        em_abs=8.5,
        em_pct=em_pct,
        em_abs_weighted=7.0,
        em_pct_weighted=0.07,
        em_pct_iv=0.052,
        iv_atm=0.5,
        oi_atm=500,
        volume_atm=1200,
        rel_spread=0.04,
        quality_flags=[QualityFlag.ZERO_BID, QualityFlag.DTE_GT_2],
    )


def stored(conn: sqlite3.Connection, ticker: str) -> int:
    upsert_events(conn, [event(ticker)])
    event_id = event_id_for(conn, ticker, EVENT_DAY)
    assert event_id is not None
    return event_id


def test_snapshot_survives_the_round_trip(conn: sqlite3.Connection) -> None:
    original = snapshot(stored(conn, "LIQD"))
    insert_snapshot(conn, original)

    (_, restored), *rest = latest_snapshots_for_session(conn, SESSION)
    assert not rest
    assert restored == original


def test_flags_keep_their_order(conn: sqlite3.Connection) -> None:
    """Kolejność flag jest częścią wartości — ten sam snapshot musi dać ten sam JSON."""
    insert_snapshot(conn, snapshot(stored(conn, "LIQD")))
    (_, restored), *_ = latest_snapshots_for_session(conn, SESSION)
    assert restored.quality_flags == [QualityFlag.ZERO_BID, QualityFlag.DTE_GT_2]


def test_missing_values_stay_null(conn: sqlite3.Connection) -> None:
    """Metody B i C bywają niepoliczalne — NULL nie może zamienić się w zero."""
    event_id = stored(conn, "SPARSE")
    insert_snapshot(
        conn,
        EmSnapshot(
            event_id=event_id,
            snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
            spot=6.0,
            expiry=EXPIRY,
            dte=4,
            atm_strike=6.0,
            straddle=0.4,
            em_abs=0.34,
            em_pct=0.056,
        ),
    )
    (_, restored), *_ = latest_snapshots_for_session(conn, SESSION)
    assert restored.em_pct_weighted is None
    assert restored.em_pct_iv is None
    assert restored.iv_atm is None
    assert restored.quality_flags == []


def test_two_scans_leave_two_rows_and_the_report_takes_the_newer(conn: sqlite3.Connection) -> None:
    """Snapshot jest pomiarem w chwili, nie stanem zdarzenia — dopisujemy, nie nadpisujemy."""
    event_id = stored(conn, "LIQD")
    insert_snapshot(conn, snapshot(event_id, minute=0, em_pct=0.07))
    insert_snapshot(conn, snapshot(event_id, minute=30, em_pct=0.09))

    rows = conn.execute("SELECT COUNT(*) AS n FROM em_snapshots").fetchone()["n"]
    (_, restored), *_ = latest_snapshots_for_session(conn, SESSION)
    assert rows == 2
    assert restored.em_pct == pytest.approx(0.09)


def test_event_without_a_snapshot_is_not_in_the_report(conn: sqlite3.Connection) -> None:
    """Odrzucony ticker zostaje w `earnings_events`, ale nie ma czym wypełnić raportu."""
    stored(conn, "NOOPT")
    assert latest_snapshots_for_session(conn, SESSION) == []


def test_other_sessions_are_not_mixed_in(conn: sqlite3.Connection) -> None:
    insert_snapshot(conn, snapshot(stored(conn, "LIQD")))
    upsert_events(conn, [event("OTHER", session_date=date(2026, 8, 19))])
    other_id = event_id_for(conn, "OTHER", EVENT_DAY)
    assert other_id is not None
    insert_snapshot(conn, snapshot(other_id))

    assert [event.ticker for event, _ in latest_snapshots_for_session(conn, SESSION)] == ["LIQD"]


def test_empty_session_gives_no_rows(conn: sqlite3.Connection) -> None:
    assert latest_snapshots_for_session(conn, SESSION) == []


def test_snapshot_needs_a_real_event(conn: sqlite3.Connection) -> None:
    """Klucz obcy pilnuje, żeby nie powstał snapshot bez zdarzenia."""
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(conn, snapshot(4242))


def test_memory_database_has_the_same_schema() -> None:
    """`--dry-run` przechodzi tę samą ścieżkę zapisu, tylko nic nie zostaje na dysku."""
    with open_memory_db() as memory:
        insert_snapshot(memory, snapshot(stored(memory, "LIQD")))
        assert len(latest_snapshots_for_session(memory, SESSION)) == 1

"""Zapis i odczyt rozliczeń — tabela `outcomes`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from emscan.db import (
    event_id_for,
    insert_outcome,
    open_memory_db,
    outcomes_for_session,
    upsert_events,
)
from emscan.models import (
    Direction,
    EarningsEvent,
    Outcome,
    Timing,
    TimingConfidence,
)

ET = ZoneInfo("America/New_York")

EVENT_DAY = date(2026, 8, 17)
SESSION = date(2026, 8, 18)
SETTLED_AT = datetime(2026, 8, 18, 17, 0, tzinfo=ET)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with open_memory_db() as connection:
        yield connection


def stored_event(conn: sqlite3.Connection, ticker: str, *, session_date: date = SESSION) -> int:
    upsert_events(
        conn,
        [
            EarningsEvent(
                ticker=ticker,
                event_date=EVENT_DAY,
                timing=Timing.AMC,
                timing_confidence=TimingConfidence.HIGH,
                session_date=session_date,
                fetched_at=datetime(2026, 8, 17, 19, 30, tzinfo=UTC),
            )
        ],
    )
    event_id = event_id_for(conn, ticker, EVENT_DAY)
    assert event_id is not None
    return event_id


def outcome(
    event_id: int,
    *,
    close_pct: float = 0.06,
    em_ratio: float | None = 0.71,
    vrp: float | None = 0.025,
    exceeded: bool | None = False,
) -> Outcome:
    return Outcome(
        event_id=event_id,
        baseline_close=100.0,
        next_open=104.0,
        next_close=100.0 * (1 + close_pct),
        gap_pct=0.04,
        close_pct=close_pct,
        intraday_pct=0.019,
        direction=Direction.UP if close_pct > 0 else Direction.DOWN,
        abs_move_pct=abs(close_pct),
        em_ratio=em_ratio,
        vrp=vrp,
        exceeded_em=exceeded,
        settled_at=SETTLED_AT,
    )


def test_outcome_survives_the_round_trip(conn: sqlite3.Connection) -> None:
    original = outcome(stored_event(conn, "AMCX"))
    insert_outcome(conn, original)
    assert outcomes_for_session(conn, SESSION) == {original.event_id: original}


def test_missing_comparisons_stay_null(conn: sqlite3.Connection) -> None:
    """Zdarzenie bez EM ma ruch, ale nie ma z czym go porównać."""
    event_id = stored_event(conn, "NOEM")
    insert_outcome(conn, outcome(event_id, em_ratio=None, vrp=None, exceeded=None))
    restored = outcomes_for_session(conn, SESSION)[event_id]
    assert (restored.em_ratio, restored.vrp, restored.exceeded_em) == (None, None, None)
    assert restored.close_pct == pytest.approx(0.06)


def test_boolean_survives_sqlite_integer(conn: sqlite3.Connection) -> None:
    """SQLite nie ma typu logicznego — 0/1 musi wrócić jako bool, nie jako liczba."""
    event_id = stored_event(conn, "BIG")
    insert_outcome(conn, outcome(event_id, close_pct=0.2, em_ratio=3.3, vrp=-0.14, exceeded=True))
    restored = outcomes_for_session(conn, SESSION)[event_id]
    assert restored.exceeded_em is True


def test_resettling_updates_instead_of_duplicating(conn: sqlite3.Connection) -> None:
    """Rozliczenie to stan zdarzenia, nie pomiar w chwili — inaczej niż snapshot."""
    event_id = stored_event(conn, "AMCX")
    insert_outcome(conn, outcome(event_id, close_pct=0.02))
    insert_outcome(conn, outcome(event_id, close_pct=0.09))

    rows = conn.execute("SELECT COUNT(*) AS n FROM outcomes").fetchone()["n"]
    assert rows == 1
    assert outcomes_for_session(conn, SESSION)[event_id].close_pct == pytest.approx(0.09)


def test_other_sessions_are_not_mixed_in(conn: sqlite3.Connection) -> None:
    insert_outcome(conn, outcome(stored_event(conn, "TODAY")))
    insert_outcome(conn, outcome(stored_event(conn, "TOMORROW", session_date=date(2026, 8, 19))))
    assert len(outcomes_for_session(conn, SESSION)) == 1


def test_outcome_needs_a_real_event(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_outcome(conn, outcome(4242))


def test_empty_session_gives_no_outcomes(conn: sqlite3.Connection) -> None:
    assert outcomes_for_session(conn, SESSION) == {}

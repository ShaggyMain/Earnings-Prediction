"""Rozliczenie — wzory ze SPEC §1.6, mapowanie na sesję i sytuacje brzegowe.

SPEC §Jakość kodu wymaga testów rozliczenia AMC i BMO oraz dnia świątecznego — to jest ten plik.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from emscan.db import (
    event_id_for,
    insert_snapshot,
    open_memory_db,
    outcomes_for_session,
    settlement_candidates,
    upsert_events,
)
from emscan.engine.outcomes import (
    MissingOutcome,
    compute_outcome,
    default_settle_scan_date,
    direction_of,
    last_closed_session,
    run_settle,
)
from emscan.models import (
    Direction,
    EarningsEvent,
    EmSnapshot,
    Timing,
    TimingConfidence,
)
from emscan.sources.base import DailyBar
from fakes import FakePrices

ET = ZoneInfo("America/New_York")

SCAN_DAY = date(2026, 8, 17)  # poniedziałek
SESSION = date(2026, 8, 18)
EXPIRY = date(2026, 8, 21)
SETTLED_AT = datetime(2026, 8, 18, 17, 0, tzinfo=ET)


def bar(day: date, *, open_: float, close: float, volume: int = 1_000_000) -> DailyBar:
    return DailyBar(
        day=day,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=volume,
    )


# ------------------------------------------------------------------ wzory


def test_outcome_follows_the_spec_formulas() -> None:
    outcome = compute_outcome(
        event_id=1,
        baseline_close=100.0,
        session_bar=bar(SESSION, open_=104.0, close=106.0),
        em_pct=0.085,
        settled_at=SETTLED_AT,
    )
    assert outcome.gap_pct == pytest.approx(0.04)
    assert outcome.close_pct == pytest.approx(0.06)
    assert outcome.intraday_pct == pytest.approx(106.0 / 104.0 - 1)
    assert outcome.abs_move_pct == pytest.approx(0.06)
    assert outcome.direction == Direction.UP
    assert outcome.em_ratio == pytest.approx(0.06 / 0.085)
    assert outcome.vrp == pytest.approx(0.085 - 0.06)
    assert outcome.exceeded_em is False


def test_both_measurements_are_kept_when_the_market_reverses() -> None:
    """SPEC §1.6: przy AMC rynek często odwraca ruch z otwarcia — sam gap myli."""
    outcome = compute_outcome(
        event_id=1,
        baseline_close=100.0,
        session_bar=bar(SESSION, open_=108.0, close=98.0),
        em_pct=0.05,
        settled_at=SETTLED_AT,
    )
    assert outcome.gap_pct == pytest.approx(0.08)
    assert outcome.close_pct == pytest.approx(-0.02)
    assert outcome.direction == Direction.DOWN
    assert outcome.abs_move_pct == pytest.approx(0.02)
    assert outcome.exceeded_em is False


def test_move_beyond_em_is_marked() -> None:
    """em_ratio > 1 znaczy, że rynek opcji nie doszacował ruchu."""
    outcome = compute_outcome(
        event_id=1,
        baseline_close=100.0,
        session_bar=bar(SESSION, open_=112.0, close=115.0),
        em_pct=0.06,
        settled_at=SETTLED_AT,
    )
    assert outcome.em_ratio == pytest.approx(0.15 / 0.06)
    assert outcome.vrp == pytest.approx(0.06 - 0.15)
    assert outcome.exceeded_em is True


@pytest.mark.parametrize(
    ("close_pct", "expected"),
    [(0.01, Direction.UP), (-0.01, Direction.DOWN), (0.0, Direction.FLAT)],
)
def test_direction(close_pct: float, expected: Direction) -> None:
    assert direction_of(close_pct) == expected


def test_zero_move_is_flat_not_down() -> None:
    """Nazwanie zera spadkiem byłoby zmyśleniem kierunku, którego nie było."""
    outcome = compute_outcome(
        event_id=1,
        baseline_close=100.0,
        session_bar=bar(SESSION, open_=100.0, close=100.0),
        em_pct=0.06,
        settled_at=SETTLED_AT,
    )
    assert outcome.direction == Direction.FLAT
    assert outcome.em_ratio == pytest.approx(0.0)


@pytest.mark.parametrize("em_pct", [None, 0.0])
def test_without_em_the_comparison_columns_stay_empty(em_pct: float | None) -> None:
    """Brak pomiaru EM nie może dać ani zera, ani nieskończoności."""
    outcome = compute_outcome(
        event_id=1,
        baseline_close=100.0,
        session_bar=bar(SESSION, open_=104.0, close=106.0),
        em_pct=em_pct,
        settled_at=SETTLED_AT,
    )
    assert outcome.close_pct == pytest.approx(0.06)
    assert (outcome.em_ratio, outcome.vrp, outcome.exceeded_em) == (None, None, None)


def test_nonpositive_baseline_is_an_error() -> None:
    with pytest.raises(ValueError, match="baseline_close"):
        compute_outcome(
            event_id=1,
            baseline_close=0.0,
            session_bar=bar(SESSION, open_=1.0, close=1.0),
            em_pct=0.06,
            settled_at=SETTLED_AT,
        )


# ------------------------------------------------------------------ przepływ settle


def event(ticker: str, *, event_date: date, timing: Timing, session_date: date) -> EarningsEvent:
    return EarningsEvent(
        ticker=ticker,
        event_date=event_date,
        timing=timing,
        timing_confidence=TimingConfidence.HIGH,
        session_date=session_date,
        fetched_at=datetime(2026, 8, 17, 19, 30, tzinfo=UTC),
    )


def seed(
    conn: sqlite3.Connection,
    events: list[EarningsEvent],
    *,
    em_pct: float | None = 0.06,
    with_snapshot: bool = True,
) -> None:
    upsert_events(conn, events)
    if not with_snapshot:
        return
    for item in events:
        event_id = event_id_for(conn, item.ticker, item.event_date)
        assert event_id is not None
        insert_snapshot(
            conn,
            EmSnapshot(
                event_id=event_id,
                snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
                spot=100.0,
                expiry=EXPIRY,
                dte=4,
                atm_strike=100.0,
                straddle=7.0,
                em_abs=6.0,
                em_pct=em_pct,
            ),
        )


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with open_memory_db() as connection:
        yield connection


def test_amc_and_next_day_bmo_share_one_baseline(conn: sqlite3.Connection) -> None:
    """Sedno grupowania ze SPEC §1: oba zdarzenia liczą się od tego samego zamknięcia."""
    seed(
        conn,
        [
            event("AMCX", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION),
            event("BMOX", event_date=SESSION, timing=Timing.BMO, session_date=SESSION),
        ],
    )
    history = [bar(SCAN_DAY, open_=99.0, close=100.0), bar(SESSION, open_=104.0, close=107.0)]
    result = run_settle(
        scan_date=SCAN_DAY,
        conn=conn,
        prices=FakePrices({"AMCX": history, "BMOX": history}),
        settled_at=SETTLED_AT,
    )

    assert (result.session_date, result.baseline_date) == (SESSION, SCAN_DAY)
    assert len(result.settled) == 2
    assert {row.outcome.baseline_close for row in result.settled if row.outcome} == {100.0}
    assert {round(row.outcome.close_pct, 4) for row in result.settled if row.outcome} == {0.07}


def test_settlement_after_a_holiday_uses_the_last_real_session(conn: sqlite3.Connection) -> None:
    """3 lipca 2026 to obchodzony Dzień Niepodległości: sesja 6.07, odniesienie 2.07."""
    thursday, monday = date(2026, 7, 2), date(2026, 7, 6)
    seed(conn, [event("HOL", event_date=thursday, timing=Timing.AMC, session_date=monday)])
    result = run_settle(
        scan_date=thursday,
        conn=conn,
        prices=FakePrices(
            {"HOL": [bar(thursday, open_=99.0, close=100.0), bar(monday, open_=90.0, close=92.0)]}
        ),
        settled_at=SETTLED_AT,
    )
    assert (result.session_date, result.baseline_date) == (monday, thursday)
    row = result.settled[0]
    assert row.outcome is not None
    assert row.outcome.close_pct == pytest.approx(-0.08)


def test_friday_scan_settles_on_monday(conn: sqlite3.Connection) -> None:
    friday, monday = date(2026, 8, 14), date(2026, 8, 17)
    seed(conn, [event("WKND", event_date=friday, timing=Timing.AMC, session_date=monday)])
    result = run_settle(
        scan_date=friday,
        conn=conn,
        prices=FakePrices(
            {"WKND": [bar(friday, open_=99.0, close=100.0), bar(monday, open_=101.0, close=103.0)]}
        ),
        settled_at=SETTLED_AT,
    )
    assert result.baseline_date == friday
    assert result.settled[0].outcome is not None


def test_outcome_lands_in_the_database(conn: sqlite3.Connection) -> None:
    seed(conn, [event("AMCX", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)])
    run_settle(
        scan_date=SCAN_DAY,
        conn=conn,
        prices=FakePrices(
            {
                "AMCX": [
                    bar(SCAN_DAY, open_=99.0, close=100.0),
                    bar(SESSION, open_=104.0, close=106.0),
                ]
            }
        ),
        settled_at=SETTLED_AT,
    )
    stored = outcomes_for_session(conn, SESSION)
    assert len(stored) == 1
    outcome = next(iter(stored.values()))
    assert outcome.close_pct == pytest.approx(0.06)
    assert outcome.direction == Direction.UP


def test_second_run_overwrites_the_settlement(conn: sqlite3.Connection) -> None:
    """Pierwszy przebieg mógł trafić przed zamknięciem sesji — drugi ma to poprawić."""
    seed(conn, [event("AMCX", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)])
    early = [bar(SCAN_DAY, open_=99.0, close=100.0), bar(SESSION, open_=104.0, close=104.5)]
    final = [bar(SCAN_DAY, open_=99.0, close=100.0), bar(SESSION, open_=104.0, close=109.0)]
    for history in (early, final):
        run_settle(
            scan_date=SCAN_DAY,
            conn=conn,
            prices=FakePrices({"AMCX": history}),
            settled_at=SETTLED_AT,
        )
    rows = conn.execute("SELECT COUNT(*) AS n FROM outcomes").fetchone()["n"]
    outcome = next(iter(outcomes_for_session(conn, SESSION).values()))
    assert rows == 1
    assert outcome.close_pct == pytest.approx(0.09)


# ------------------------------------------------------------------ sytuacje brzegowe


def test_missing_session_bar_leaves_no_row(conn: sqlite3.Connection) -> None:
    """SPEC §1.6: brak notowań to NO_DATA, nie zero. Brak wiersza JEST tym stanem."""
    seed(conn, [event("HALT", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)])
    result = run_settle(
        scan_date=SCAN_DAY,
        conn=conn,
        prices=FakePrices({"HALT": [bar(SCAN_DAY, open_=99.0, close=100.0)]}),
        settled_at=SETTLED_AT,
    )
    assert result.missing == {MissingOutcome.NO_SESSION_BAR: 1}
    assert outcomes_for_session(conn, SESSION) == {}


def test_missing_baseline_bar_is_its_own_reason(conn: sqlite3.Connection) -> None:
    seed(conn, [event("HALT", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)])
    result = run_settle(
        scan_date=SCAN_DAY,
        conn=conn,
        prices=FakePrices({"HALT": [bar(SESSION, open_=104.0, close=106.0)]}),
        settled_at=SETTLED_AT,
    )
    assert result.missing == {MissingOutcome.NO_BASELINE_BAR: 1}


def test_no_history_at_all_is_reported(conn: sqlite3.Connection) -> None:
    seed(conn, [event("GONE", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)])
    result = run_settle(scan_date=SCAN_DAY, conn=conn, prices=FakePrices({}), settled_at=SETTLED_AT)
    assert result.missing == {MissingOutcome.NO_PRICE_HISTORY: 1}


def test_one_broken_ticker_does_not_stop_the_settlement(conn: sqlite3.Connection) -> None:
    seed(
        conn,
        [
            event("OKAY", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION),
            event("BROKEN", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION),
        ],
    )
    history = [bar(SCAN_DAY, open_=99.0, close=100.0), bar(SESSION, open_=104.0, close=106.0)]
    result = run_settle(
        scan_date=SCAN_DAY,
        conn=conn,
        prices=FakePrices({"OKAY": history, "BROKEN": history}, failing=frozenset({"BROKEN"})),
        settled_at=SETTLED_AT,
    )
    assert [row.ticker for row in result.settled] == ["OKAY"]
    assert result.missing == {MissingOutcome.SOURCE_ERROR: 1}


def test_events_without_a_snapshot_are_not_settled(conn: sqlite3.Connection) -> None:
    """Ruchy całego uniwersum zbiera backfill; settle domyka pętlę EM -> realizacja."""
    seed(
        conn,
        [event("NOEM", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)],
        with_snapshot=False,
    )
    prices = FakePrices({"NOEM": [bar(SESSION, open_=104.0, close=106.0)]})
    result = run_settle(scan_date=SCAN_DAY, conn=conn, prices=prices, settled_at=SETTLED_AT)
    assert result.rows == ()
    assert prices.tickers_fetched == []


def test_exceeded_em_is_counted(conn: sqlite3.Connection) -> None:
    seed(
        conn,
        [
            event("BIG", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION),
            event("SMALL", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION),
        ],
        em_pct=0.06,
    )
    result = run_settle(
        scan_date=SCAN_DAY,
        conn=conn,
        prices=FakePrices(
            {
                "BIG": [
                    bar(SCAN_DAY, open_=99.0, close=100.0),
                    bar(SESSION, open_=110.0, close=112.0),
                ],
                "SMALL": [
                    bar(SCAN_DAY, open_=99.0, close=100.0),
                    bar(SESSION, open_=101.0, close=101.0),
                ],
            }
        ),
        settled_at=SETTLED_AT,
    )
    assert result.exceeded_em == 1


# ------------------------------------------------------------------ domyślna data rozliczenia


def et(year: int, month: int, day: int, hour: int = 17) -> datetime:
    return datetime(year, month, day, hour, 0, tzinfo=ET)


@pytest.mark.parametrize(
    ("moment", "session", "scan"),
    [
        # Wtorek 17:00 ET: sesja wtorkowa zamknęła się godzinę temu, skanował ją poniedziałek.
        (et(2026, 8, 18), date(2026, 8, 18), date(2026, 8, 17)),
        # Sobotni cron (SPEC §1.8 ma 2-6) rozlicza piątek, którego skan był w czwartek.
        (et(2026, 8, 22), date(2026, 8, 21), date(2026, 8, 20)),
        # Poniedziałek Labor Day: nie ma sesji, cofamy się do piątku i jego skanu z czwartku.
        (et(2026, 9, 7), date(2026, 9, 4), date(2026, 9, 3)),
        # Wtorek po Labor Day: sesja wtorkowa, ale poprzednia sesja to piątek.
        (et(2026, 9, 8), date(2026, 9, 8), date(2026, 9, 4)),
    ],
)
def test_default_settle_date_follows_the_exchange_calendar(
    moment: datetime, session: date, scan: date
) -> None:
    """Bez tego arytmetyka dat wjechałaby do YAML-a i pomyliła się na pierwszym święcie."""
    assert last_closed_session(moment) == session
    assert default_settle_scan_date(moment) == scan


def test_default_settle_date_is_not_today() -> None:
    """„Dziś" wskazywałoby sesję, która się jeszcze nie odbyła — cron chodzi dzień po skanie."""
    assert default_settle_scan_date(et(2026, 8, 18)) != date(2026, 8, 18)


def test_naive_moment_is_rejected() -> None:
    with pytest.raises(ValueError, match="strefą"):
        last_closed_session(datetime(2026, 8, 18, 17, 0))  # noqa: DTZ001


# ------------------------------------------------------------------ szerszy zakres rozliczenia


def test_candidates_default_to_events_with_em(conn: sqlite3.Connection) -> None:
    seed(conn, [event("WITH", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)])
    seed(
        conn,
        [event("WITHOUT", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)],
        with_snapshot=False,
    )
    default = settlement_candidates(conn, SESSION)
    assert [event.ticker for _, event, _ in default] == ["WITH"]


def test_candidates_can_include_events_without_em(conn: sqlite3.Connection) -> None:
    """Target fazy 2 i D3 liczą się z cen, więc zdarzenie bez EM to pełnoprawna obserwacja."""
    seed(conn, [event("WITH", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)])
    seed(
        conn,
        [event("WITHOUT", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)],
        with_snapshot=False,
    )
    widened = settlement_candidates(conn, SESSION, require_snapshot=False)
    assert sorted(event.ticker for _, event, _ in widened) == ["WITH", "WITHOUT"]
    assert [snapshot is None for _, event, snapshot in widened if event.ticker == "WITHOUT"] == [
        True
    ]


def test_settle_without_em_records_the_move_and_nulls_the_comparison(
    conn: sqlite3.Connection,
) -> None:
    """Ruch jest daną. Porównania z EM nie ma, więc em_ratio, vrp i exceeded_em są NULL."""
    seed(
        conn,
        [event("NOEM", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)],
        with_snapshot=False,
    )
    history = [bar(SCAN_DAY, open_=99.0, close=100.0), bar(SESSION, open_=104.0, close=106.0)]
    result = run_settle(
        scan_date=SCAN_DAY,
        conn=conn,
        prices=FakePrices({"NOEM": history}),
        settled_at=SETTLED_AT,
        require_snapshot=False,
    )

    assert len(result.settled) == 1
    assert result.settled_without_em == 1
    outcome = next(iter(outcomes_for_session(conn, SESSION).values()))
    assert outcome.close_pct == pytest.approx(0.06)
    assert outcome.intraday_pct == pytest.approx(106.0 / 104.0 - 1)
    assert (outcome.em_ratio, outcome.vrp, outcome.exceeded_em) == (None, None, None)


def test_widened_scope_settles_both_kinds(conn: sqlite3.Connection) -> None:
    seed(conn, [event("WITH", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)])
    seed(
        conn,
        [event("WITHOUT", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)],
        with_snapshot=False,
    )
    history = [bar(SCAN_DAY, open_=99.0, close=100.0), bar(SESSION, open_=104.0, close=106.0)]
    result = run_settle(
        scan_date=SCAN_DAY,
        conn=conn,
        prices=FakePrices({"WITH": history, "WITHOUT": history}),
        settled_at=SETTLED_AT,
        require_snapshot=False,
    )
    assert len(result.settled) == 2
    assert result.settled_without_em == 1
    assert len(outcomes_for_session(conn, SESSION)) == 2


def test_narrow_scope_asks_prices_only_about_measured_events(conn: sqlite3.Connection) -> None:
    """Domyślny zakres trzyma kaskadę: bez EM nie pytamy o ceny."""
    seed(
        conn,
        [event("NOEM", event_date=SCAN_DAY, timing=Timing.AMC, session_date=SESSION)],
        with_snapshot=False,
    )
    prices = FakePrices({"NOEM": [bar(SESSION, open_=104.0, close=106.0)]})
    result = run_settle(scan_date=SCAN_DAY, conn=conn, prices=prices, settled_at=SETTLED_AT)
    assert prices.tickers_fetched == []
    assert result.rows == ()

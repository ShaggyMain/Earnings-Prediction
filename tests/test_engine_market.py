"""Dane reżimu rynkowego — SPY, QQQ, IWM, VXX plus iv30.

Cechy „szerokiego rynku" ze SPEC §2.3 liczy faza 2; ten moduł zbiera surowce i to jest
testowane tutaj. Na atrapach, bez sieci.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest

from emscan.db import bars_for, open_memory_db
from emscan.engine.market import MARKET_INSTRUMENTS, MarketInstrument, run_market_update
from fakes import FakeOptions, FakePrices, bars

FETCHED_AT = datetime(2026, 8, 19, 21, 0, tzinfo=UTC)
START = date(2026, 8, 1)
END = date(2026, 8, 19)

HISTORY = {
    "SPY": bars(90_000_000, sessions=10, day=END),
    "QQQ": bars(40_000_000, sessions=10, day=END),
    "IWM": bars(30_000_000, sessions=10, day=END),
    "VXX": bars(5_000_000, sessions=10, day=END),
}


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with open_memory_db() as connection:
        yield connection


def run(
    conn: sqlite3.Connection,
    prices: FakePrices,
    *,
    options: FakeOptions | None = None,
    instruments: tuple[MarketInstrument, ...] = MARKET_INSTRUMENTS,
) -> object:
    return run_market_update(
        conn=conn,
        prices=prices,
        start=START,
        end=END,
        fetched_at=FETCHED_AT,
        options=options,
        instruments=instruments,
    )


def test_default_set_covers_market_tech_smallcaps_and_volatility() -> None:
    """Cztery role, żeby cecha reżimu nie była jednowymiarowa."""
    assert [i.ticker for i in MARKET_INSTRUMENTS] == ["SPY", "QQQ", "IWM", "VXX"]
    assert [i.ticker for i in MARKET_INSTRUMENTS if i.with_iv30] == ["SPY"]


def test_bars_of_every_instrument_land_in_the_database(conn: sqlite3.Connection) -> None:
    result = run(conn, FakePrices(HISTORY))
    assert result.instruments_fetched == ("SPY", "QQQ", "IWM", "VXX")  # type: ignore[attr-defined]
    assert result.bars_written == 40  # type: ignore[attr-defined]
    assert len(bars_for(conn, "SPY", START, END)) == 10
    assert result.complete is True  # type: ignore[attr-defined]


def test_market_bars_share_the_table_with_company_bars(conn: sqlite3.Connection) -> None:
    """Jeden szereg czasowy na ticker — faza 2 pyta o rynek i o spółkę tym samym zapytaniem."""
    run(conn, FakePrices(HISTORY))
    tables = {row["ticker"] for row in conn.execute("SELECT DISTINCT ticker FROM daily_bars")}
    assert tables == {"SPY", "QQQ", "IWM", "VXX"}


def test_iv30_is_recorded_for_the_last_session(conn: sqlite3.Connection) -> None:
    result = run(conn, FakePrices(HISTORY), options=FakeOptions({}, iv30={"SPY": 0.1206}))
    assert result.iv30_recorded == {"SPY": pytest.approx(0.1206)}  # type: ignore[attr-defined]
    row = conn.execute(
        "SELECT day, iv30 FROM daily_bars WHERE ticker = 'SPY' AND iv30 IS NOT NULL"
    ).fetchone()
    assert row["day"] == END.isoformat()


def test_without_an_options_source_there_is_no_iv30(conn: sqlite3.Connection) -> None:
    """Brak dostawcy to brak pomiaru, nie błąd i nie zero."""
    result = run(conn, FakePrices(HISTORY), options=None)
    assert result.iv30_recorded == {}  # type: ignore[attr-defined]
    assert result.complete is True  # type: ignore[attr-defined]


def test_provider_without_iv30_support_is_not_an_error(conn: sqlite3.Connection) -> None:
    """`OptionsChainSource.iv30` domyślnie zwraca None — dostawca bez tej danej po prostu milczy."""
    result = run(conn, FakePrices(HISTORY), options=FakeOptions({}))
    assert result.iv30_recorded == {}  # type: ignore[attr-defined]


def test_failing_iv30_does_not_break_the_run(conn: sqlite3.Connection) -> None:
    result = run(conn, FakePrices(HISTORY), options=FakeOptions({}, failing=frozenset({"SPY"})))
    assert result.iv30_recorded == {}  # type: ignore[attr-defined]
    assert len(bars_for(conn, "SPY", START, END)) == 10


def test_zero_bars_is_reported_as_a_failure(conn: sqlite3.Connection) -> None:
    """SPY przy `assetclass=stocks` zwraca pustą tabelę, nie błąd — to musi być widoczne."""
    result = run(conn, FakePrices({}))
    assert result.instruments_fetched == ()  # type: ignore[attr-defined]
    assert len(result.failures) == 4  # type: ignore[attr-defined]
    assert "klasę aktywów" in result.failures[0]  # type: ignore[attr-defined]
    assert result.complete is False  # type: ignore[attr-defined]


def test_one_broken_instrument_does_not_stop_the_rest(conn: sqlite3.Connection) -> None:
    prices = FakePrices(HISTORY, failing=frozenset({"QQQ"}))
    result = run(conn, prices)
    assert result.instruments_fetched == ("SPY", "IWM", "VXX")  # type: ignore[attr-defined]
    assert len(result.failures) == 1  # type: ignore[attr-defined]


def test_market_data_is_a_feature_not_a_precondition(conn: sqlite3.Connection) -> None:
    """Nawet gdy padnie wszystko, nie podnosimy wyjątku — skan bez cech reżimu wciąż działa."""
    result = run(conn, FakePrices({}, failing=frozenset({"SPY", "QQQ", "IWM", "VXX"})))
    assert result.bars_written == 0  # type: ignore[attr-defined]
    assert result.complete is False  # type: ignore[attr-defined]

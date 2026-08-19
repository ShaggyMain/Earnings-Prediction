"""Przepływ skanu — grupowanie sesji, kaskada filtrów, zachowanie przy awariach.

Wszystko na atrapach z `tests/fakes.py`, zero sieci (SPEC §B.3 pkt 6).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from emscan.db import latest_snapshots_for_session, open_memory_db
from emscan.engine.scan import ScanResult, run_scan, target_session
from emscan.engine.universe import RejectReason, UniverseFilters
from emscan.models import Timing
from emscan.sources.base import SourceUnavailable
from fakes import FakeCalendar, FakeOptions, FakePrices, bars, record, simple_chain

ET = ZoneInfo("America/New_York")

SCAN_DAY = date(2026, 8, 17)  # poniedziałek
SESSION = date(2026, 8, 18)
EXPIRY = date(2026, 8, 21)
STALE_EXPIRY = date(2026, 8, 14)
SNAPSHOT_AT = datetime(2026, 8, 17, 15, 30, tzinfo=ET)

LIQUID_VOLUME = 5_000_000
HISTORY = bars(900_000, day=SCAN_DAY)
THIN_HISTORY = bars(100_000, day=SCAN_DAY)


# ------------------------------------------------------------------ sesja rozliczeniowa


def test_session_is_the_first_one_after_the_scan() -> None:
    assert target_session(SCAN_DAY) == SESSION


def test_friday_scan_targets_monday() -> None:
    assert target_session(date(2026, 8, 14)) == date(2026, 8, 17)


def test_scan_before_a_holiday_skips_it() -> None:
    """3 lipca 2026 to obchodzony Dzień Niepodległości — sesji nie ma."""
    assert target_session(date(2026, 7, 2)) == date(2026, 7, 6)


# ------------------------------------------------------------------ pełny skan


@pytest.fixture
def world() -> Iterator[tuple[ScanResult, sqlite3.Connection, FakeOptions, FakePrices]]:
    """Jeden skan obejmujący wszystkie ścieżki kaskady naraz."""
    records = [
        record("LIQD", SCAN_DAY, Timing.AMC),
        record("CALM", SCAN_DAY, Timing.AMC),
        record("CHEAP", SCAN_DAY, Timing.AMC),
        record("THIN", SCAN_DAY, Timing.AMC),
        record("ILLIQ", SCAN_DAY, Timing.AMC),
        record("NOOPT", SCAN_DAY, Timing.AMC),
        record("PAST", SCAN_DAY, Timing.AMC),
        record("BROKEN", SCAN_DAY, Timing.AMC),
        record("NOBARS", SCAN_DAY, Timing.AMC),
        record("DMHX", SCAN_DAY, Timing.DMH),
        record("EARLY", SCAN_DAY, Timing.BMO),  # sesja 17.08 — rozliczone rano, nie skanujemy
        record("BMOX", SESSION, Timing.BMO),  # sesja 18.08 — ta sama grupa co AMC z 17.08
        record("LATER", SESSION, Timing.AMC),  # sesja 19.08 — inna grupa
    ]
    chains = {
        "LIQD": {EXPIRY: simple_chain("LIQD", EXPIRY, leg_price=5.0)},
        "BMOX": {EXPIRY: simple_chain("BMOX", EXPIRY, leg_price=8.0)},
        "CALM": {EXPIRY: simple_chain("CALM", EXPIRY, leg_price=1.0)},
        "CHEAP": {EXPIRY: simple_chain("CHEAP", EXPIRY, spot=4.0, leg_price=1.0)},
        "THIN": {EXPIRY: simple_chain("THIN", EXPIRY, leg_price=5.0)},
        "ILLIQ": {EXPIRY: simple_chain("ILLIQ", EXPIRY, leg_price=5.0)},
        "NOBARS": {EXPIRY: simple_chain("NOBARS", EXPIRY, leg_price=5.0)},
        "PAST": {STALE_EXPIRY: simple_chain("PAST", STALE_EXPIRY, leg_price=5.0)},
    }
    options = FakeOptions(
        chains,
        volumes={ticker: LIQUID_VOLUME for ticker in chains} | {"THIN": 50_000},
        not_covered=frozenset({"NOOPT"}),
        failing=frozenset({"BROKEN"}),
    )
    prices = FakePrices(
        {
            "LIQD": HISTORY,
            "BMOX": HISTORY,
            "CALM": HISTORY,
            "ILLIQ": THIN_HISTORY,
        },
        failing=frozenset({"NOBARS"}),
    )
    with open_memory_db() as conn:
        result = run_scan(
            scan_date=SCAN_DAY,
            conn=conn,
            calendars=[FakeCalendar(records)],
            options=options,
            prices=prices,
            filters=UniverseFilters(),
            snapshot_at=SNAPSHOT_AT,
        )
        yield result, conn, options, prices


def test_scan_covers_amc_of_the_day_and_bmo_of_the_session(world: Any) -> None:
    """SPEC §1 „Kluczowa obserwacja o grupowaniu" — dwie grupy, jedna sesja."""
    result, _, _, _ = world
    assert result.session_date == SESSION
    assert {row.ticker for row in result.rows if row.selected} == {"LIQD", "BMOX"}


def test_bmo_of_the_scan_day_is_not_scanned(world: Any) -> None:
    """BMO z dnia D rozliczyło się rano — o 15:30 nie ma już czego wyceniać."""
    result, _, _, _ = world
    assert "EARLY" not in {row.ticker for row in result.rows}


def test_amc_of_the_session_day_belongs_to_the_next_group(world: Any) -> None:
    result, _, _, _ = world
    assert "LATER" not in {row.ticker for row in result.rows}


def test_selected_are_sorted_by_em_descending(world: Any) -> None:
    result, _, _, _ = world
    assert [row.ticker for row in result.selected] == ["BMOX", "LIQD"]


def test_every_rejection_reason_is_recorded(world: Any) -> None:
    result, _, _, _ = world
    reasons = {row.ticker: row.rejected for row in result.rows}
    assert reasons["DMHX"] == RejectReason.NO_SESSION
    assert reasons["NOOPT"] == RejectReason.NO_OPTIONS
    assert reasons["PAST"] == RejectReason.NO_EXPIRY
    assert reasons["CHEAP"] == RejectReason.LOW_PRICE
    assert reasons["THIN"] == RejectReason.THIN_SESSION_VOLUME
    assert reasons["CALM"] == RejectReason.LOW_EM
    assert reasons["ILLIQ"] == RejectReason.THIN_VOLUME_20D
    assert reasons["BROKEN"] == RejectReason.SOURCE_ERROR
    assert reasons["NOBARS"] == RejectReason.SOURCE_ERROR


def test_rejection_carries_the_number_that_decided(world: Any) -> None:
    result, _, _, _ = world
    details = {row.ticker: row.detail for row in result.rows}
    assert details["CHEAP"] == "spot=4.00"
    assert details["THIN"] == "volume=50000"
    assert details["ILLIQ"] == "volume_20d=100000"
    assert details["CALM"] is not None and details["CALM"].startswith("em_pct=")


def test_a_broken_ticker_does_not_stop_the_scan(world: Any) -> None:
    """Awaria jednego dostawcy dla jednego tickera nie może wywrócić całego skanu."""
    result, _, _, _ = world
    assert len(result.selected) == 2
    assert result.rejections[RejectReason.SOURCE_ERROR] == 2


# ------------------------------------------------------------------ kaskada i zapisy


def test_price_history_is_fetched_only_for_survivors(world: Any) -> None:
    """Sedno kaskady ze SPEC §B.2: Nasdaq dostaje pytanie tylko o tych, co dotarli do etapu 3."""
    result, _, options, prices = world
    assert sorted(prices.tickers_fetched) == ["BMOX", "ILLIQ", "LIQD", "NOBARS"]
    assert result.price_lookups == 4
    # Etap 1 dotknął wszystkich kandydatów: 11 wierszy minus DMH, które nie ma sesji.
    assert len(options.tickers_fetched) == 10


def test_snapshot_is_written_even_when_em_is_below_the_threshold(world: Any) -> None:
    """EM 1,7% to poprawny pomiar. Faza 2 potrzebuje całego rozkładu (SPEC §2.1)."""
    _, conn, _, _ = world
    saved = {
        event.ticker: snapshot for event, snapshot in latest_snapshots_for_session(conn, SESSION)
    }
    assert "CALM" in saved
    assert saved["CALM"].em_pct == pytest.approx(0.85 * 2.0 / 100.0)


def test_snapshot_is_written_for_an_illiquid_name_too(world: Any) -> None:
    _, conn, _, _ = world
    saved = {event.ticker for event, _ in latest_snapshots_for_session(conn, SESSION)}
    assert {"LIQD", "BMOX", "CALM", "ILLIQ", "NOBARS"} == saved


def test_snapshot_count_matches_the_database(world: Any) -> None:
    result, conn, _, _ = world
    stored = conn.execute("SELECT COUNT(*) AS n FROM em_snapshots").fetchone()["n"]
    assert result.snapshots_written == stored == 5


def test_events_that_are_not_scanned_still_land_in_the_database(world: Any) -> None:
    """SPEC §1.4: rekordu nie usuwamy. DMH, BMO z rana i AMC z kolejnej grupy zostają."""
    _, conn, _, _ = world
    stored = {
        row["ticker"] for row in conn.execute("SELECT ticker FROM earnings_events").fetchall()
    }
    assert {"DMHX", "EARLY", "LATER", "NOOPT", "BROKEN"} <= stored
    assert len(stored) == 13


def test_events_seen_counts_both_days(world: Any) -> None:
    result, _, _, _ = world
    assert result.events_seen == 13
    assert len(result.rows) == 11  # 10 kandydatów tej sesji + DMH bez sesji


# ------------------------------------------------------------------ awarie kalendarza


def test_one_failing_calendar_degrades_but_does_not_stop_the_scan() -> None:
    records = [record("LIQD", SCAN_DAY, Timing.AMC)]
    options = FakeOptions(
        {"LIQD": {EXPIRY: simple_chain("LIQD", EXPIRY)}}, volumes={"LIQD": LIQUID_VOLUME}
    )
    with open_memory_db() as conn:
        result = run_scan(
            scan_date=SCAN_DAY,
            conn=conn,
            calendars=[
                FakeCalendar(records, name="ok"),
                FakeCalendar(records, name="broken", failing_days=frozenset({SCAN_DAY, SESSION})),
            ],
            options=options,
            prices=FakePrices({"LIQD": HISTORY}),
            filters=UniverseFilters(),
            snapshot_at=SNAPSHOT_AT,
        )
    assert len(result.calendar_failures) == 2
    assert [row.ticker for row in result.selected] == ["LIQD"]


def test_scan_without_any_calendar_data_raises() -> None:
    """Skan bez kalendarza nie ma czego liczyć — tu wyjątek jest właściwą reakcją."""
    with open_memory_db() as conn, pytest.raises(SourceUnavailable):
        run_scan(
            scan_date=SCAN_DAY,
            conn=conn,
            calendars=[
                FakeCalendar([], name="broken", failing_days=frozenset({SCAN_DAY, SESSION}))
            ],
            options=FakeOptions({}),
            prices=FakePrices({}),
            filters=UniverseFilters(),
            snapshot_at=SNAPSHOT_AT,
        )


def test_calendar_is_asked_about_both_days() -> None:
    calendar = FakeCalendar([])
    with open_memory_db() as conn, pytest.raises(SourceUnavailable):
        # Brak rekordów w obu dniach jest nierozróżnialny od braku danych — i tak ma być.
        run_scan(
            scan_date=SCAN_DAY,
            conn=conn,
            calendars=[calendar],
            options=FakeOptions({}),
            prices=FakePrices({}),
            filters=UniverseFilters(),
            snapshot_at=SNAPSHOT_AT,
        )
    assert calendar.days_fetched == [SCAN_DAY, SESSION]

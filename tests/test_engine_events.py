"""Scalanie kalendarzy, pewność timingu i mapowanie BMO/AMC -> session_date.

SPEC §Jakość kodu wymaga testu mapowania BMO/AMC na `session_date` — to jest ten plik.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from emscan.engine.events import (
    baseline_date_for,
    find_adjacent_date_conflicts,
    merge_records,
    resolve_timing,
    session_date_for,
)
from emscan.models import EarningsEvent, Timing, TimingConfidence
from emscan.sources.finnhub import FinnhubCalendarSource
from emscan.sources.nasdaq import NasdaqCalendarSource

DAY_13 = date(2026, 8, 13)  # czwartek
DAY_14 = date(2026, 8, 14)  # piątek
DAY_17 = date(2026, 8, 17)  # poniedziałek
FETCHED_AT = datetime(2026, 8, 13, 19, 30, tzinfo=UTC)


# ------------------------------------------------------------------ resolve_timing


def test_two_sources_agreeing_give_high_confidence() -> None:
    assert resolve_timing({"nasdaq": Timing.AMC, "finnhub": Timing.AMC}) == (
        Timing.AMC,
        TimingConfidence.HIGH,
    )


def test_single_source_gives_medium_confidence() -> None:
    assert resolve_timing({"finnhub": Timing.BMO}) == (Timing.BMO, TimingConfidence.MEDIUM)


def test_one_known_one_silent_gives_medium() -> None:
    assert resolve_timing({"nasdaq": Timing.UNKNOWN, "finnhub": Timing.BMO}) == (
        Timing.BMO,
        TimingConfidence.MEDIUM,
    )


def test_conflict_yields_unknown_not_a_winner() -> None:
    """SPEC §1.2 zakazuje zgadywania — przy sprzeczności nie wybieramy zwycięzcy."""
    assert resolve_timing({"nasdaq": Timing.AMC, "finnhub": Timing.BMO}) == (
        Timing.UNKNOWN,
        TimingConfidence.LOW,
    )


def test_no_source_knows_timing() -> None:
    assert resolve_timing({"nasdaq": Timing.UNKNOWN, "finnhub": Timing.UNKNOWN}) == (
        Timing.UNKNOWN,
        TimingConfidence.UNKNOWN,
    )


def test_empty_input() -> None:
    assert resolve_timing({}) == (Timing.UNKNOWN, TimingConfidence.UNKNOWN)


def test_dmh_agreeing_is_high_confidence() -> None:
    assert resolve_timing({"a": Timing.DMH, "b": Timing.DMH}) == (
        Timing.DMH,
        TimingConfidence.HIGH,
    )


def test_dmh_against_bmo_is_a_conflict() -> None:
    assert resolve_timing({"a": Timing.DMH, "b": Timing.BMO}) == (
        Timing.UNKNOWN,
        TimingConfidence.LOW,
    )


# ------------------------------------------------------------------ session_date


def test_bmo_is_consumed_the_same_day() -> None:
    assert session_date_for(DAY_13, Timing.BMO) == DAY_13


def test_amc_is_consumed_the_next_day() -> None:
    assert session_date_for(DAY_13, Timing.AMC) == DAY_14


def test_amc_on_friday_is_consumed_on_monday() -> None:
    """Sedno liczenia po kalendarzu giełdowym, a nie kalendarzowym."""
    assert session_date_for(DAY_14, Timing.AMC) == DAY_17


def test_amc_before_holiday_skips_it() -> None:
    """AMC 2.07.2026: 3 lipca to zastępczy Dzień Niepodległości, więc sesja dopiero 6.07."""
    assert session_date_for(date(2026, 7, 2), Timing.AMC) == date(2026, 7, 6)


def test_bmo_on_a_holiday_moves_to_next_session() -> None:
    """Źródło potrafi podać dzień bez sesji — bierzemy najbliższą sesję, nie zgadujemy."""
    assert session_date_for(date(2026, 7, 3), Timing.BMO) == date(2026, 7, 6)


@pytest.mark.parametrize("timing", [Timing.DMH, Timing.UNKNOWN])
def test_no_session_date_without_unambiguous_timing(timing: Timing) -> None:
    assert session_date_for(DAY_13, timing) is None


def test_baseline_is_the_session_before() -> None:
    assert baseline_date_for(DAY_14) == DAY_13  # AMC z 13.08
    assert baseline_date_for(DAY_13) == date(2026, 8, 12)  # BMO z 13.08


def test_baseline_skips_weekend() -> None:
    assert baseline_date_for(DAY_17) == DAY_14


# ------------------------------------------------------------------ merge_records


@pytest.fixture
def merged_13(nasdaq_13: Any, finnhub_13: Any) -> dict[str, EarningsEvent]:
    nasdaq = NasdaqCalendarSource(user_agent="pytest")
    finnhub = FinnhubCalendarSource(api_key="test-key")
    try:
        records = nasdaq.parse(nasdaq_13, DAY_13) + finnhub.parse(finnhub_13, DAY_13)
    finally:
        nasdaq.close()
        finnhub.close()
    return {e.ticker: e for e in merge_records(records, fetched_at=FETCHED_AT)}


def test_merge_combines_both_sources(merged_13: dict[str, EarningsEvent]) -> None:
    amat = merged_13["AMAT"]
    assert set(amat.sources) == {"nasdaq", "finnhub"}
    assert amat.timing == Timing.AMC
    assert amat.timing_confidence == TimingConfidence.HIGH
    assert amat.session_date == DAY_14


def test_merge_keeps_company_name_from_the_source_that_has_it(
    merged_13: dict[str, EarningsEvent],
) -> None:
    """Finnhub nie zwraca nazwy spółki, Nasdaq tak."""
    assert merged_13["AMAT"].company_name == "Applied Materials, Inc."
    assert merged_13["ALH"].company_name is None  # wyłącznie w Finnhub


def test_single_source_event_is_medium(merged_13: dict[str, EarningsEvent]) -> None:
    alh = merged_13["ALH"]
    assert set(alh.sources) == {"finnhub"}
    assert alh.timing_confidence == TimingConfidence.MEDIUM
    assert alh.session_date == DAY_13  # BMO


@pytest.mark.parametrize("ticker", ["FTLF", "REKR"])
def test_real_conflicts_are_marked_low_and_not_scannable(
    merged_13: dict[str, EarningsEvent], ticker: str
) -> None:
    """Realne rozbieżności z 13.08: Nasdaq mówi after-hours, Finnhub bmo."""
    event = merged_13[ticker]
    assert event.timing == Timing.UNKNOWN
    assert event.timing_confidence == TimingConfidence.LOW
    assert event.session_date is None
    assert not event.is_scannable
    # oba odczyty zostają w sources_json — nic nie ginie
    assert event.sources["nasdaq"].timing == Timing.AMC
    assert event.sources["finnhub"].timing == Timing.BMO


def test_conflict_records_eps_actual_as_information_only(
    merged_13: dict[str, EarningsEvent],
) -> None:
    """Wypełniony epsActual sugeruje, że Finnhub ma rację — ale nie rozstrzyga za nas."""
    ftlf = merged_13["FTLF"]
    assert ftlf.eps_actual_present
    assert ftlf.timing_confidence == TimingConfidence.LOW


def test_unknown_everywhere_stays_in_the_database(merged_13: dict[str, EarningsEvent]) -> None:
    """SPEC §1.4: rekordu o niskiej jakości nie usuwamy, tylko oznaczamy."""
    nu = merged_13["NU"]
    assert nu.timing == Timing.UNKNOWN
    assert nu.timing_confidence == TimingConfidence.UNKNOWN
    assert not nu.is_scannable


def test_scannable_events_have_a_session(merged_13: dict[str, EarningsEvent]) -> None:
    scannable = [e for e in merged_13.values() if e.is_scannable]
    assert {e.ticker for e in scannable} == {"AMAT", "ABEO", "ACOG", "ALH", "BN", "VNRX"}
    assert all(e.session_date is not None for e in scannable)


def test_grouping_puts_amc_and_next_day_bmo_in_one_session(
    merged_13: dict[str, EarningsEvent],
) -> None:
    """SPEC §1 „Kluczowa obserwacja o grupowaniu"."""
    assert merged_13["AMAT"].session_date == DAY_14  # AMC z 13.08
    assert merged_13["BN"].session_date == DAY_13  # BMO z 13.08


def test_friday_amc_rolls_over_the_weekend(nasdaq_14: Any) -> None:
    nasdaq = NasdaqCalendarSource(user_agent="pytest")
    try:
        events = {
            e.ticker: e
            for e in merge_records(nasdaq.parse(nasdaq_14, DAY_14), fetched_at=FETCHED_AT)
        }
    finally:
        nasdaq.close()
    assert events["CSAN"].timing == Timing.AMC
    assert events["CSAN"].session_date == DAY_17
    assert events["AYA"].timing == Timing.BMO
    assert events["AYA"].session_date == DAY_14


# ------------------------------------------------------------------ konflikt dat


def test_finds_same_ticker_on_adjacent_days(
    nasdaq_13: Any, finnhub_13: Any, finnhub_14: Any
) -> None:
    """Realny przypadek: Nasdaq umieścił ACTU i VNRX 13.08, Finnhub 14.08."""
    nasdaq = NasdaqCalendarSource(user_agent="pytest")
    finnhub = FinnhubCalendarSource(api_key="test-key")
    try:
        records = (
            nasdaq.parse(nasdaq_13, DAY_13)
            + finnhub.parse(finnhub_13, DAY_13)
            + finnhub.parse(finnhub_14, DAY_14)
        )
    finally:
        nasdaq.close()
        finnhub.close()

    events = merge_records(records, fetched_at=FETCHED_AT)
    conflicts = find_adjacent_date_conflicts(events)
    assert conflicts == [("ACTU", DAY_13, DAY_14), ("VNRX", DAY_13, DAY_14)]


def test_no_conflict_for_distant_dates() -> None:
    events = [
        EarningsEvent(
            ticker="XYZ",
            event_date=day,
            timing=Timing.UNKNOWN,
            timing_confidence=TimingConfidence.UNKNOWN,
            fetched_at=FETCHED_AT,
        )
        for day in (date(2026, 5, 1), date(2026, 8, 1))
    ]
    assert find_adjacent_date_conflicts(events) == []

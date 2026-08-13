"""Parsery kalendarzy — wyłącznie na nagranych fixtures, zero sieci."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from emscan.models import Timing
from emscan.sources.base import SourceDataError
from emscan.sources.finnhub import FinnhubCalendarSource
from emscan.sources.nasdaq import NasdaqCalendarSource, parse_int, parse_money

DAY_13 = date(2026, 8, 13)
DAY_14 = date(2026, 8, 14)


@pytest.fixture
def nasdaq() -> Iterator[NasdaqCalendarSource]:
    source = NasdaqCalendarSource(user_agent="pytest")
    yield source
    source.close()


@pytest.fixture
def finnhub() -> Iterator[FinnhubCalendarSource]:
    source = FinnhubCalendarSource(api_key="test-key")
    yield source
    source.close()


# ------------------------------------------------------------------ parse_money


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$3.38", 3.38),
        ("($0.03)", -0.03),
        ("$435,208,861,555", 435_208_861_555.0),
        ("$0.00", 0.0),
        ("", None),
        ("N/A", None),
        (None, None),
        ("--", None),
        ("nonsens", None),
    ],
)
def test_parse_money(raw: Any, expected: float | None) -> None:
    assert parse_money(raw) == expected


def test_parse_money_never_turns_missing_into_zero() -> None:
    """Zero to poprawny EPS — mylenie go z brakiem danych zafałszowałoby cechy fazy 2."""
    assert parse_money("N/A") is None
    assert parse_money("$0.00") == 0.0


def test_parse_int() -> None:
    assert parse_int("9") == 9
    assert parse_int("") is None


# ------------------------------------------------------------------ Nasdaq


def test_nasdaq_parses_all_rows(nasdaq: NasdaqCalendarSource, nasdaq_13: Any) -> None:
    records = nasdaq.parse(nasdaq_13, DAY_13)
    assert {r.ticker for r in records} == {
        "AMAT",
        "BN",
        "NU",
        "ABEO",
        "ACOG",
        "FTLF",
        "REKR",
        "ACTU",
        "VNRX",
    }
    assert all(r.source == "nasdaq" for r in records)
    assert all(r.event_date == DAY_13 for r in records)


def test_nasdaq_maps_timing(nasdaq: NasdaqCalendarSource, nasdaq_13: Any) -> None:
    by_ticker = {r.ticker: r for r in nasdaq.parse(nasdaq_13, DAY_13)}
    assert by_ticker["AMAT"].timing == Timing.AMC  # time-after-hours
    assert by_ticker["BN"].timing == Timing.BMO  # time-pre-market
    assert by_ticker["NU"].timing == Timing.UNKNOWN  # time-not-supplied


def test_nasdaq_extracts_numbers(nasdaq: NasdaqCalendarSource, nasdaq_13: Any) -> None:
    amat = next(r for r in nasdaq.parse(nasdaq_13, DAY_13) if r.ticker == "AMAT")
    assert amat.company_name == "Applied Materials, Inc."
    assert amat.eps_estimate == pytest.approx(3.38)
    assert amat.market_cap == pytest.approx(435_208_861_555.0)
    assert amat.num_estimates == 9


def test_nasdaq_never_reports_actual_eps(nasdaq: NasdaqCalendarSource, nasdaq_13: Any) -> None:
    """Endpoint kalendarza podaje wyłącznie prognozę — patrz docstring modułu."""
    records = nasdaq.parse(nasdaq_13, DAY_13)
    assert all(r.eps_actual is None for r in records)
    assert not any(r.eps_actual_present for r in records)


def test_nasdaq_handles_empty_day(nasdaq: NasdaqCalendarSource) -> None:
    """Dzień bez sesji: Nasdaq zwraca data=null. To poprawna odpowiedź, nie błąd."""
    assert nasdaq.parse({"data": None, "status": {"rCode": 200}}, DAY_13) == []


def test_nasdaq_rejects_broken_contract(nasdaq: NasdaqCalendarSource) -> None:
    with pytest.raises(SourceDataError):
        nasdaq.parse({"data": {"rows": "to nie jest lista"}}, DAY_13)
    with pytest.raises(SourceDataError):
        nasdaq.parse(["lista zamiast obiektu"], DAY_13)


def test_nasdaq_unknown_timing_value_falls_back_to_unknown(
    nasdaq: NasdaqCalendarSource,
) -> None:
    """Nowa wartość pola 'time' nie może wywalić skanu ani zostać zgadnięta."""
    payload = {"data": {"rows": [{"symbol": "XYZ", "time": "time-teleported"}]}}
    records = nasdaq.parse(payload, DAY_13)
    assert records[0].timing == Timing.UNKNOWN


def test_nasdaq_skips_rows_without_symbol(nasdaq: NasdaqCalendarSource) -> None:
    payload = {"data": {"rows": [{"symbol": "", "time": "time-pre-market"}]}}
    assert nasdaq.parse(payload, DAY_13) == []


# ------------------------------------------------------------------ Finnhub


def test_finnhub_parses_all_rows(finnhub: FinnhubCalendarSource, finnhub_13: Any) -> None:
    records = finnhub.parse(finnhub_13, DAY_13)
    assert {r.ticker for r in records} == {
        "ABEO",
        "ACOG",
        "ALH",
        "AMAT",
        "BN",
        "FTLF",
        "NU",
        "REKR",
    }
    assert all(r.source == "finnhub" for r in records)


def test_finnhub_maps_timing(finnhub: FinnhubCalendarSource, finnhub_13: Any) -> None:
    by_ticker = {r.ticker: r for r in finnhub.parse(finnhub_13, DAY_13)}
    assert by_ticker["AMAT"].timing == Timing.AMC
    assert by_ticker["ABEO"].timing == Timing.BMO
    assert by_ticker["NU"].timing == Timing.UNKNOWN  # hour == ""


def test_finnhub_maps_dmh(finnhub: FinnhubCalendarSource) -> None:
    """`dmh` nie wystąpił w danych z 13-14.08, ale dokumentacja go przewiduje."""
    payload = {"earningsCalendar": [{"symbol": "XYZ", "date": "2026-08-13", "hour": "dmh"}]}
    assert finnhub.parse(payload, DAY_13)[0].timing == Timing.DMH


def test_finnhub_reports_actual_eps(finnhub: FinnhubCalendarSource, finnhub_13: Any) -> None:
    by_ticker = {r.ticker: r for r in finnhub.parse(finnhub_13, DAY_13)}
    assert by_ticker["FTLF"].eps_actual == pytest.approx(0.2)
    assert by_ticker["FTLF"].eps_actual_present
    assert by_ticker["ACOG"].eps_actual is None
    assert not by_ticker["ACOG"].eps_actual_present


def test_finnhub_treats_zero_eps_as_present(
    finnhub: FinnhubCalendarSource, finnhub_13: Any
) -> None:
    """REKR ma epsActual == 0. Zero to opublikowany wynik, nie brak danych."""
    rekr = next(r for r in finnhub.parse(finnhub_13, DAY_13) if r.ticker == "REKR")
    assert rekr.eps_actual == 0.0
    assert rekr.eps_actual_present


def test_finnhub_trusts_record_date_over_query_date(
    finnhub: FinnhubCalendarSource, finnhub_14: Any
) -> None:
    """Data zdarzenia pochodzi z rekordu, żeby nie przypisać go do złej sesji."""
    records = finnhub.parse(finnhub_14, DAY_13)
    assert all(r.event_date == DAY_14 for r in records)


def test_finnhub_rejects_broken_contract(finnhub: FinnhubCalendarSource) -> None:
    with pytest.raises(SourceDataError):
        finnhub.parse({"cokolwiek": []}, DAY_13)
    with pytest.raises(SourceDataError):
        finnhub.parse({"earningsCalendar": "nie lista"}, DAY_13)


def test_finnhub_requires_api_key() -> None:
    with pytest.raises(ValueError, match="klucza API"):
        FinnhubCalendarSource(api_key="")


def test_finnhub_skips_row_with_broken_date(finnhub: FinnhubCalendarSource) -> None:
    payload = {"earningsCalendar": [{"symbol": "XYZ", "date": "13-08-2026", "hour": "bmo"}]}
    assert finnhub.parse(payload, DAY_13) == []

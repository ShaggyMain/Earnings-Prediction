"""Historia OHLC z Nasdaq — na nagranych fixtures, zero prawdziwej sieci."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from emscan.sources.base import SourceDataError
from emscan.sources.nasdaq_prices import ASSET_CLASS_ETF, NasdaqPriceSource


@pytest.fixture
def source() -> Iterator[NasdaqPriceSource]:
    client = NasdaqPriceSource(user_agent="pytest", min_interval=0, max_retries=1)
    yield client
    client.close()


def test_parses_all_rows(source: NasdaqPriceSource, nasdaq_history: Any) -> None:
    bars = source.parse(nasdaq_history, "AMAT")
    assert len(bars) == 32


def test_bars_are_sorted_ascending(source: NasdaqPriceSource, nasdaq_history: Any) -> None:
    """API oddaje od najnowszej sesji, a interfejs PriceSource wymaga rosnąco."""
    bars = source.parse(nasdaq_history, "AMAT")
    assert [b.day for b in bars] == sorted(b.day for b in bars)
    assert bars[0].day == date(2026, 7, 1)
    assert bars[-1].day == date(2026, 8, 14)


def test_strips_currency_and_thousand_separators(
    source: NasdaqPriceSource, nasdaq_history: Any
) -> None:
    last = source.parse(nasdaq_history, "AMAT")[-1]
    assert last.close == pytest.approx(507.18)
    assert last.open == pytest.approx(499.40)
    assert last.high == pytest.approx(523.00)
    assert last.low == pytest.approx(497.10)
    assert last.volume == 13_132_340


def test_handles_fractional_prices(source: NasdaqPriceSource, nasdaq_history: Any) -> None:
    """Nasdaq podaje ceny z trzema miejscami po przecinku, np. $668.405."""
    first = source.parse(nasdaq_history, "AMAT")[0]
    assert first.open == pytest.approx(668.405)


def test_null_data_means_no_quotes_not_an_error(source: NasdaqPriceSource) -> None:
    assert source.parse({"data": None}, "AMAT") == []


def test_rejects_broken_contract(source: NasdaqPriceSource) -> None:
    with pytest.raises(SourceDataError):
        source.parse({"data": {"tradesTable": {"rows": "nie lista"}}}, "AMAT")
    with pytest.raises(SourceDataError):
        source.parse(["lista zamiast obiektu"], "AMAT")


def test_incomplete_row_is_skipped_never_zeroed(source: NasdaqPriceSource) -> None:
    """SPEC §Czego NIE robić: brak notowań to brak świecy, nigdy świeca z zerami."""
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {
                        "date": "08/14/2026",
                        "close": "$507.18",
                        "volume": "13,132,340",
                        "open": "$499.40",
                        "high": "$523.00",
                        "low": "$497.10",
                    },
                    {
                        "date": "08/13/2026",
                        "close": "N/A",
                        "volume": "N/A",
                        "open": "N/A",
                        "high": "N/A",
                        "low": "N/A",
                    },
                ]
            }
        }
    }
    bars = source.parse(payload, "AMAT")
    assert len(bars) == 1
    assert bars[0].day == date(2026, 8, 14)


def test_row_with_broken_date_is_skipped(source: NasdaqPriceSource) -> None:
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {
                        "date": "2026-08-14",
                        "close": "$1",
                        "volume": "1",
                        "open": "$1",
                        "high": "$1",
                        "low": "$1",
                    }
                ]
            }
        }
    }
    assert source.parse(payload, "AMAT") == []


def test_zero_volume_session_is_kept(source: NasdaqPriceSource) -> None:
    """Zero to poprawny wolumen, nie brak danych — świeca musi przetrwać."""
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {
                        "date": "08/14/2026",
                        "close": "$5.00",
                        "volume": "0",
                        "open": "$5.00",
                        "high": "$5.00",
                        "low": "$5.00",
                    }
                ]
            }
        }
    }
    (bar,) = source.parse(payload, "AMAT")
    assert bar.volume == 0


# ------------------------------------------------------------------ okno zapytania i klasa aktywów

HISTORICAL_URL = "https://api.nasdaq.com/api/quote/AMAT/historical"
ETF_URL = "https://api.nasdaq.com/api/quote/SPY/historical"


@respx.mock
def test_request_window_always_ends_today(source: NasdaqPriceSource, nasdaq_history: Any) -> None:
    """Endpoint zwraca pustkę dla `todate` w odległej przeszłości — sprawdzone 2026-08-19.

    Dlatego pytamy o okno kończące się dziś, a przycinamy u siebie. Inaczej rozliczenie
    starszej sesji dostawałoby zero świec nieodróżnialne od braku notowań.
    """
    route = respx.get(HISTORICAL_URL).mock(return_value=httpx.Response(200, json=nasdaq_history))
    source.daily_bars("AMAT", date(2025, 6, 1), date(2025, 6, 30))

    sent = dict(route.calls[0].request.url.params)
    assert sent["fromdate"] == "2025-06-01"
    assert sent["todate"] > "2026-01-01"  # dzisiejsza data, nie 2025-06-30


@respx.mock
def test_result_is_trimmed_to_the_requested_end(
    source: NasdaqPriceSource, nasdaq_history: Any
) -> None:
    """Szersze okno w zapytaniu nie może przeciekać do wyniku."""
    respx.get(HISTORICAL_URL).mock(return_value=httpx.Response(200, json=nasdaq_history))
    bars = source.daily_bars("AMAT", date(2026, 7, 1), date(2026, 7, 15))
    assert [b.day for b in bars] == sorted(b.day for b in bars)
    assert max(b.day for b in bars) <= date(2026, 7, 15)
    assert min(b.day for b in bars) >= date(2026, 7, 1)


@respx.mock
def test_asset_class_reaches_the_request(nasdaq_history: Any) -> None:
    """SPY przy `assetclass=stocks` zwraca zero wierszy, nie błąd — stąd osobna instancja."""
    route = respx.get(ETF_URL).mock(return_value=httpx.Response(200, json=nasdaq_history))
    client = NasdaqPriceSource(
        user_agent="pytest", min_interval=0, max_retries=1, asset_class=ASSET_CLASS_ETF
    )
    try:
        client.daily_bars("SPY", date(2026, 7, 1), date(2026, 8, 19))
    finally:
        client.close()
    assert dict(route.calls[0].request.url.params)["assetclass"] == "etf"


def test_default_asset_class_is_stocks(source: NasdaqPriceSource) -> None:
    assert source.asset_class == "stocks"

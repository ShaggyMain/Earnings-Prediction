"""Łańcuch opcji z CBOE — na nagranych fixtures, zero prawdziwej sieci."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from emscan.sources.base import (
    OptionType,
    SourceDataError,
    SymbolNotCovered,
)
from emscan.sources.cboe import CboeOptionsSource, parse_occ_symbol

AMAT_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/AMAT.json"
ABEO_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/ABEO.json"
FTLF_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/FTLF.json"

AMAT_NEAR_EXPIRY = date(2026, 8, 14)
ABEO_NEAR_EXPIRY = date(2026, 8, 21)


@pytest.fixture
def source() -> Iterator[CboeOptionsSource]:
    client = CboeOptionsSource(user_agent="pytest", min_interval=0, max_retries=1)
    yield client
    client.close()


# ------------------------------------------------------------------ symbol OCC


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("AMAT260814C00507500", ("AMAT", date(2026, 8, 14), OptionType.CALL, 507.5)),
        ("ABEO260821P00006000", ("ABEO", date(2026, 8, 21), OptionType.PUT, 6.0)),
        ("REKR260918C00000500", ("REKR", date(2026, 9, 18), OptionType.CALL, 0.5)),
    ],
)
def test_parses_occ_symbol(symbol: str, expected: tuple[str, date, OptionType, float]) -> None:
    assert parse_occ_symbol(symbol) == expected


def test_parses_alphanumeric_root() -> None:
    """Root bywa alfanumeryczny — dlatego rozbieramy symbol od końca, nie regexem."""
    root, expiry, option_type, strike = parse_occ_symbol("BRKB260814C00450000")
    assert (root, expiry, option_type, strike) == (
        "BRKB",
        date(2026, 8, 14),
        OptionType.CALL,
        450.0,
    )


@pytest.mark.parametrize(
    "symbol",
    ["ZBYTKROTKI", "AMAT260814X00507500", "AMATABCDEFC00507500", "AMAT261332C00507500"],
)
def test_rejects_malformed_symbol(symbol: str) -> None:
    with pytest.raises(SourceDataError):
        parse_occ_symbol(symbol)


# ------------------------------------------------------------------ pobieranie


@respx.mock
def test_lists_expirations_sorted(source: CboeOptionsSource, cboe_amat: Any) -> None:
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    assert source.expirations("AMAT") == [AMAT_NEAR_EXPIRY, date(2026, 8, 21)]


@respx.mock
def test_one_request_serves_expirations_quote_and_chain(
    source: CboeOptionsSource, cboe_amat: Any
) -> None:
    """Sedno wyboru CBOE: łańcuch i spot z jednej odpowiedzi, bez dodatkowych zapytań."""
    route = respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    source.expirations("AMAT")
    source.quote("AMAT")
    source.chain("AMAT", AMAT_NEAR_EXPIRY)
    assert route.call_count == 1


@respx.mock
def test_ticker_is_case_insensitive(source: CboeOptionsSource, cboe_amat: Any) -> None:
    route = respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    source.expirations("amat")
    source.expirations("AMAT")
    assert route.call_count == 1


@respx.mock
def test_quote_carries_spot_and_session_ohlc(source: CboeOptionsSource, cboe_amat: Any) -> None:
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    quote = source.quote("AMAT")
    assert quote.spot == pytest.approx(506.7653)
    assert quote.close == pytest.approx(507.18)
    assert quote.prev_close == pytest.approx(507.18)
    assert quote.volume == 13_132_344


@respx.mock
def test_data_timestamp_is_new_york_time(source: CboeOptionsSource, cboe_amat: Any) -> None:
    """Znacznik CBOE nie ma strefy — bez niej flaga stale_quote liczyłaby się błędnie."""
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    stamp = source.data_timestamp("AMAT")
    assert stamp is not None
    assert stamp.isoformat() == "2026-08-15T03:44:36-04:00"


# ------------------------------------------------------------------ łańcuch


@respx.mock
def test_chain_is_sorted_and_typed(source: CboeOptionsSource, cboe_amat: Any) -> None:
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    chain = source.chain("AMAT", AMAT_NEAR_EXPIRY)

    assert chain.ticker == "AMAT"
    assert chain.expiry == AMAT_NEAR_EXPIRY
    assert chain.spot == pytest.approx(506.7653)
    assert chain.calls and chain.puts
    assert [q.strike for q in chain.calls] == sorted(q.strike for q in chain.calls)
    assert all(q.option_type == OptionType.CALL for q in chain.calls)
    assert all(q.option_type == OptionType.PUT for q in chain.puts)


@respx.mock
def test_chain_keeps_only_the_requested_expiry(source: CboeOptionsSource, cboe_amat: Any) -> None:
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    near = source.chain("AMAT", AMAT_NEAR_EXPIRY)
    far = source.chain("AMAT", date(2026, 8, 21))
    assert {q.strike for q in near.calls} != {q.strike for q in far.calls}


@respx.mock
def test_liquid_chain_has_usable_mids(source: CboeOptionsSource, cboe_amat: Any) -> None:
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    chain = source.chain("AMAT", AMAT_NEAR_EXPIRY)
    assert all(q.mid is not None for q in chain.calls)

    atm = min(chain.calls, key=lambda q: abs(q.strike - chain.spot))
    assert atm.strike == pytest.approx(507.5)
    assert atm.bid == pytest.approx(0.5)
    assert atm.ask == pytest.approx(1.22)
    assert atm.mid == pytest.approx(0.86)
    assert atm.open_interest == 55
    assert atm.implied_volatility == pytest.approx(0.6753)


@respx.mock
def test_illiquid_chain_yields_no_mid(source: CboeOptionsSource, cboe_abeo: Any) -> None:
    """ABEO ma bid = 0 na wielu kontraktach. Źródło zwraca mid=None, a zejście na
    lastPrice i flagę zero_bid zostawia silnikowi EM."""
    respx.get(ABEO_URL).mock(return_value=httpx.Response(200, json=cboe_abeo))
    chain = source.chain("ABEO", ABEO_NEAR_EXPIRY)

    zero_bid = [q for q in chain.calls + chain.puts if q.bid == 0]
    assert zero_bid, "fixture ABEO ma zawierać kontrakty z zerowym bid"
    assert all(q.mid is None for q in zero_bid)
    assert all(q.last is not None for q in zero_bid), "lastPrice musi przetrwać do silnika"


@respx.mock
def test_unknown_expiry_is_an_error(source: CboeOptionsSource, cboe_amat: Any) -> None:
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    with pytest.raises(SourceDataError, match="pełnego łańcucha"):
        source.chain("AMAT", date(2030, 1, 18))


# ------------------------------------------------------------------ brak pokrycia


@respx.mock
def test_403_means_no_listed_options(source: CboeOptionsSource) -> None:
    """FTLF odpowiedział 403 — to brak notowanych opcji, nie problem z uprawnieniami."""
    respx.get(FTLF_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(SymbolNotCovered):
        source.expirations("FTLF")
    assert not source.is_covered("FTLF")


@respx.mock
def test_is_covered_is_true_for_listed_symbol(source: CboeOptionsSource, cboe_amat: Any) -> None:
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    assert source.is_covered("AMAT")


@respx.mock
def test_missing_data_field_is_a_contract_error(source: CboeOptionsSource) -> None:
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json={"timestamp": "x"}))
    with pytest.raises(SourceDataError, match="'data'"):
        source.expirations("AMAT")


@respx.mock
def test_chain_without_spot_is_an_error(source: CboeOptionsSource, cboe_amat: Any) -> None:
    broken = {**cboe_amat, "data": {**cboe_amat["data"], "current_price": 0}}
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=broken))
    with pytest.raises(SourceDataError, match="ceny spot"):
        source.chain("AMAT", AMAT_NEAR_EXPIRY)


@respx.mock
def test_unparsable_contracts_are_skipped_not_fatal(
    source: CboeOptionsSource, cboe_amat: Any
) -> None:
    payload = {
        **cboe_amat,
        "data": {
            **cboe_amat["data"],
            "options": [{"option": "ŚMIEĆ"}, *cboe_amat["data"]["options"]],
        },
    }
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=payload))
    assert source.expirations("AMAT") == [AMAT_NEAR_EXPIRY, date(2026, 8, 21)]


# ------------------------------------------------------------------ kwotowanie akcji


@respx.mock
def test_chain_carries_the_underlying_quote(source: CboeOptionsSource, cboe_amat: Any) -> None:
    """Spread akcji przychodzi w tej samej odpowiedzi co łańcuch — nie wolno go wyrzucać.

    Koszt transakcji na instrumencie bazowym jest o dwa rzędy wielkości mniejszy niż spread
    opcyjny, więc jedno nie zastępuje drugiego (patrz docs/EVALUATION.md, hipoteza D3).
    """
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=cboe_amat))
    chain = source.chain("AMAT", AMAT_NEAR_EXPIRY)
    assert (chain.underlying_bid, chain.underlying_ask) == (506.8, 507.0)
    assert chain.underlying_rel_spread == pytest.approx(0.2 / 506.9, rel=1e-6)


@respx.mock
def test_underlying_spread_is_none_without_a_two_sided_quote(
    source: CboeOptionsSource, cboe_amat: Any
) -> None:
    """Brak kwotowania to brak danych, nie spread zerowy."""
    payload = {**cboe_amat, "data": {**cboe_amat["data"], "bid": 0, "ask": 0}}
    respx.get(AMAT_URL).mock(return_value=httpx.Response(200, json=payload))
    chain = source.chain("AMAT", AMAT_NEAR_EXPIRY)
    assert chain.underlying_rel_spread is None

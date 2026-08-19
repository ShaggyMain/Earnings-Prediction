"""Silnik expected move — trzy metody, wybór kontraktów i flagi jakości.

SPEC §Jakość kodu wymaga testu kalkulacji EM w trzech wariantach — to jest ten plik.
Mapowanie BMO/AMC na `session_date` testuje `test_engine_events.py`.

Dane pochodzą z dwóch celowo kontrastowych fixtures (PROBE-2026-08-17 §Materiał na
fixtures): **AMAT** nie ma ani jednego zerowego bid, więc przechodzi ścieżką czystą,
a **ABEO** ma 14 zerowych bid na 24 kontrakty i wymusza zejście na `lastPrice`.
Przypadki brzegowe, których nie ma w nagranych danych, budujemy z ręki.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from emscan.engine.expected_move import (
    STRADDLE_MULTIPLIER,
    LegPrice,
    NoAtmPrice,
    NoAtmStrike,
    NoUsableExpiry,
    common_strikes,
    compute_expected_move,
    leg_price,
    select_atm_strike,
    select_expiry,
)
from emscan.models import QualityFlag
from emscan.sources.base import OptionChain, OptionQuote, OptionType
from emscan.sources.cboe import CboeOptionsSource

ET = ZoneInfo("America/New_York")

AMAT_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/AMAT.json"
ABEO_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/ABEO.json"

AMAT_NEAR = date(2026, 8, 14)
AMAT_FAR = date(2026, 8, 21)
ABEO_NEAR = date(2026, 8, 21)
ABEO_FAR = date(2026, 9, 18)

AMAT_SPOT = 506.7653
ABEO_SPOT = 6.0577

# Znaczniki z fixtures: CBOE podaje czas nowojorski bez strefy.
AMAT_DATA_TS = datetime(2026, 8, 15, 3, 44, 36, tzinfo=ET)
ABEO_DATA_TS = datetime(2026, 8, 15, 23, 16, 45, tzinfo=ET)

EVENT_ID = 7


# ------------------------------------------------------------------ narzędzia


def _chains(url: str, payload: Any, ticker: str) -> Iterator[dict[date, OptionChain]]:
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, json=payload))
        source = CboeOptionsSource(user_agent="pytest", min_interval=0, max_retries=1)
        try:
            yield {expiry: source.chain(ticker, expiry) for expiry in source.expirations(ticker)}
        finally:
            source.close()


@pytest.fixture
def amat(cboe_amat: Any) -> Iterator[dict[date, OptionChain]]:
    yield from _chains(AMAT_URL, cboe_amat, "AMAT")


@pytest.fixture
def abeo(cboe_abeo: Any) -> Iterator[dict[date, OptionChain]]:
    yield from _chains(ABEO_URL, cboe_abeo, "ABEO")


def quote(
    strike: float,
    option_type: OptionType,
    *,
    bid: float | None = None,
    ask: float | None = None,
    last: float | None = None,
    oi: int | None = 500,
    volume: int | None = 100,
    iv: float | None = 0.5,
) -> OptionQuote:
    return OptionQuote(
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        last=last,
        open_interest=oi,
        volume=volume,
        implied_volatility=iv,
    )


def make_chain(
    *,
    spot: float,
    calls: tuple[OptionQuote, ...],
    puts: tuple[OptionQuote, ...],
    expiry: date = date(2026, 8, 21),
    ticker: str = "TEST",
) -> OptionChain:
    return OptionChain(
        ticker=ticker,
        expiry=expiry,
        spot=spot,
        calls=calls,
        puts=puts,
        fetched_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
    )


def ladder_chain(
    strikes: tuple[float, ...],
    *,
    spot: float,
    price: float = 1.0,
) -> OptionChain:
    """Symetryczna drabinka z tą samą ceną na każdym kontrakcie — do testów metody B."""
    return make_chain(
        spot=spot,
        calls=tuple(quote(k, OptionType.CALL, bid=price, ask=price) for k in strikes),
        puts=tuple(quote(k, OptionType.PUT, bid=price, ask=price) for k in strikes),
    )


# ------------------------------------------------------------------ wybór wygaśnięcia


def test_picks_earliest_expiry_at_or_after_the_session() -> None:
    expiries = [date(2026, 8, 14), date(2026, 8, 21), date(2026, 9, 18)]
    assert select_expiry(expiries, date(2026, 8, 17)) == date(2026, 8, 21)


def test_expiry_on_the_session_day_counts() -> None:
    """„>= session_date" — wygaśnięcie w dniu sesji rozliczeniowej jest tym właściwym."""
    expiries = [date(2026, 8, 14), date(2026, 8, 21)]
    assert select_expiry(expiries, date(2026, 8, 21)) == date(2026, 8, 21)


def test_unsorted_input_does_not_change_the_pick() -> None:
    expiries = [date(2026, 9, 18), date(2026, 8, 21), date(2026, 8, 14)]
    assert select_expiry(expiries, date(2026, 8, 14)) == date(2026, 8, 14)


def test_no_expiry_after_the_session_is_an_error() -> None:
    """Wszystkie opcje wygasły przed sesją — nie ma z czego liczyć EM, więc wyjątek."""
    with pytest.raises(NoUsableExpiry):
        select_expiry([date(2026, 8, 14)], date(2026, 8, 17))


def test_empty_expiry_list_is_an_error() -> None:
    with pytest.raises(NoUsableExpiry):
        select_expiry([], date(2026, 8, 17))


def test_picks_expiry_from_real_chain(amat: dict[date, OptionChain]) -> None:
    assert select_expiry(amat.keys(), date(2026, 8, 17)) == AMAT_FAR


# ------------------------------------------------------------------ wybór strike'u ATM


def test_atm_is_the_strike_nearest_to_spot(amat: dict[date, OptionChain]) -> None:
    """Spot 506,77 — 507,50 jest bliżej niż 505,00."""
    assert select_atm_strike(amat[AMAT_NEAR]) == 507.5


def test_atm_on_the_micro_cap(abeo: dict[date, OptionChain]) -> None:
    assert select_atm_strike(abeo[ABEO_NEAR]) == 6.0


def test_ladder_holds_only_strikes_quoted_on_both_sides() -> None:
    chain = make_chain(
        spot=100.0,
        calls=(quote(95, OptionType.CALL, bid=6, ask=7), quote(100, OptionType.CALL, bid=2, ask=3)),
        puts=(quote(100, OptionType.PUT, bid=2, ask=3), quote(105, OptionType.PUT, bid=6, ask=7)),
    )
    assert common_strikes(chain) == [100.0]


def test_atm_ignores_a_strike_without_a_pair() -> None:
    """Strike bez drugiej nogi jest bezużyteczny dla straddle'a, więc nie może być ATM."""
    chain = make_chain(
        spot=100.0,
        calls=(
            quote(100, OptionType.CALL, bid=2, ask=3),
            quote(101, OptionType.CALL, bid=1, ask=2),
        ),
        puts=(quote(100, OptionType.PUT, bid=2, ask=3),),
    )
    assert select_atm_strike(chain) == 100.0


def test_tie_picks_the_lower_strike() -> None:
    """Reguła jest arbitralna, ale musi być deterministyczna — dwa skany, ten sam wiersz."""
    chain = ladder_chain((100.0, 110.0), spot=105.0)
    assert select_atm_strike(chain) == 100.0


def test_no_common_strike_is_an_error() -> None:
    chain = make_chain(
        spot=100.0,
        calls=(quote(100, OptionType.CALL, bid=2, ask=3),),
        puts=(quote(105, OptionType.PUT, bid=2, ask=3),),
    )
    with pytest.raises(NoAtmStrike):
        select_atm_strike(chain)


# ------------------------------------------------------------------ cena nogi


def test_two_sided_quote_gives_mid_and_spread() -> None:
    assert leg_price(quote(100, OptionType.CALL, bid=1.0, ask=1.5)) == LegPrice(
        price=1.25, used_last=False, rel_spread=pytest.approx(0.4)
    )


def test_zero_bid_falls_back_to_last() -> None:
    """SPEC §1.5: bid == 0 → lastPrice. Spread względny jest wtedy niedefiniowany."""
    assert leg_price(quote(100, OptionType.CALL, bid=0.0, ask=0.6, last=0.15)) == LegPrice(
        price=0.15, used_last=True, rel_spread=None
    )


def test_zero_ask_falls_back_to_last() -> None:
    assert leg_price(quote(100, OptionType.CALL, bid=0.4, ask=0.0, last=0.5)) == LegPrice(
        price=0.5, used_last=True, rel_spread=None
    )


def test_missing_quote_falls_back_to_last() -> None:
    assert leg_price(quote(100, OptionType.CALL, last=0.5)) == LegPrice(
        price=0.5, used_last=True, rel_spread=None
    )


@pytest.mark.parametrize("last", [None, 0.0])
def test_no_quote_and_no_trade_gives_nothing(last: float | None) -> None:
    """`last` równe zero to brak danych, nie darmowa opcja."""
    assert leg_price(quote(100, OptionType.CALL, bid=0.0, ask=0.0, last=last)) is None


# ------------------------------------------------------------------ metoda A na czystych danych


@pytest.fixture
def amat_near_snapshot(amat: dict[date, OptionChain]) -> Any:
    return compute_expected_move(
        amat[AMAT_NEAR],
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 14, 15, 30, tzinfo=ET),
        data_timestamp=AMAT_DATA_TS,
    )


def test_method_a_is_straddle_times_multiplier(amat_near_snapshot: Any) -> None:
    """ATM 507,5: call mid (0,50+1,22)/2, put mid (0,69+2,00)/2."""
    call_mid, put_mid = 0.86, 1.345
    straddle = call_mid + put_mid

    assert amat_near_snapshot.call_mid == pytest.approx(call_mid)
    assert amat_near_snapshot.put_mid == pytest.approx(put_mid)
    assert amat_near_snapshot.straddle == pytest.approx(straddle)
    assert amat_near_snapshot.em_abs == pytest.approx(STRADDLE_MULTIPLIER * straddle)
    assert amat_near_snapshot.em_pct == pytest.approx(0.85 * straddle / AMAT_SPOT)


def test_snapshot_keeps_raw_quotes_next_to_the_computed_price(amat_near_snapshot: Any) -> None:
    assert (amat_near_snapshot.call_bid, amat_near_snapshot.call_ask) == (0.5, 1.22)
    assert (amat_near_snapshot.put_bid, amat_near_snapshot.put_ask) == (0.69, 2.0)


def test_clean_chain_has_no_zero_bid_flag(amat_near_snapshot: Any) -> None:
    """Cały sens fixture AMAT: 24 kontrakty, żadnego zerowego bid."""
    assert QualityFlag.ZERO_BID not in amat_near_snapshot.quality_flags


def test_method_b_weights_straddle_and_two_strangles(amat_near_snapshot: Any) -> None:
    """Skrzydła z drabinki 500-512,5: strangle_1 = 510C + 505P, strangle_2 = 512,5C + 502,5P."""
    straddle = 2.205
    strangle_1 = 0.15 + 0.22
    strangle_2 = 0.10 + 0.065
    expected = 0.60 * straddle + 0.30 * strangle_1 + 0.10 * strangle_2

    assert amat_near_snapshot.em_abs_weighted == pytest.approx(expected)
    assert amat_near_snapshot.em_pct_weighted == pytest.approx(expected / AMAT_SPOT)


def test_method_b_stays_below_method_a(amat_near_snapshot: Any) -> None:
    """Wagi metody B sumują się do 1 bez mnożnika 0,85 — patrz METHODOLOGY §4."""
    assert amat_near_snapshot.em_abs_weighted < amat_near_snapshot.em_abs


def test_method_c_uses_average_iv_of_both_atm_legs(amat: dict[date, OptionChain]) -> None:
    """ATM 505 na wygaśnięciu 21.08: IV 0,5194 (call) i 0,5238 (put), dte = 7."""
    snapshot = compute_expected_move(
        amat[AMAT_FAR],
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 14, 15, 30, tzinfo=ET),
    )
    iv_atm = (0.5194 + 0.5238) / 2

    assert snapshot.atm_strike == 505.0
    assert snapshot.dte == 7
    assert snapshot.iv_atm == pytest.approx(iv_atm)
    assert snapshot.em_pct_iv == pytest.approx(iv_atm * math.sqrt(7 / 365))


def test_three_methods_disagree_and_all_three_are_recorded(amat: dict[date, OptionChain]) -> None:
    """Porównanie metod jest częścią wartości projektu — nie wybieramy zwycięzcy."""
    snapshot = compute_expected_move(
        amat[AMAT_FAR],
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 14, 15, 30, tzinfo=ET),
    )
    assert snapshot.em_pct == pytest.approx(0.85 * 29.575 / AMAT_SPOT)
    assert snapshot.em_pct_iv is not None
    assert snapshot.em_pct != pytest.approx(snapshot.em_pct_iv)


# ------------------------------------------------------------------ ścieżka zero_bid


@pytest.fixture
def abeo_snapshot(abeo: dict[date, OptionChain]) -> Any:
    return compute_expected_move(
        abeo[ABEO_NEAR],
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
        data_timestamp=ABEO_DATA_TS,
    )


def test_zero_bid_chain_prices_from_last_and_raises_the_flag(abeo_snapshot: Any) -> None:
    """ATM 6,0: obie nogi mają bid = 0, więc mid pochodzi z lastPrice (0,15 i 0,25)."""
    assert abeo_snapshot.atm_strike == 6.0
    assert (abeo_snapshot.call_bid, abeo_snapshot.put_bid) == (0.0, 0.0)
    assert abeo_snapshot.call_mid == pytest.approx(0.15)
    assert abeo_snapshot.put_mid == pytest.approx(0.25)
    assert abeo_snapshot.straddle == pytest.approx(0.40)
    assert abeo_snapshot.em_pct == pytest.approx(0.85 * 0.40 / ABEO_SPOT)
    assert QualityFlag.ZERO_BID in abeo_snapshot.quality_flags


def test_one_sided_quote_leaves_relative_spread_unknown(abeo_snapshot: Any) -> None:
    """Zmyślony spread byłby gorszy od braku — o jakości mówi tu flaga zero_bid."""
    assert abeo_snapshot.rel_spread is None
    assert QualityFlag.WIDE_SPREAD not in abeo_snapshot.quality_flags


def test_unpriceable_wing_kills_method_b_but_not_method_a(abeo_snapshot: Any) -> None:
    """4P w fixture ABEO ma bid = 0 i last = 0, więc strangle_2 nie istnieje."""
    assert abeo_snapshot.em_pct is not None
    assert abeo_snapshot.em_abs_weighted is None
    assert abeo_snapshot.em_pct_weighted is None


def test_flag_does_not_remove_the_record(abeo_snapshot: Any) -> None:
    """SPEC §1.4: rekordu o niskiej jakości nigdy nie usuwamy — flagujemy."""
    assert abeo_snapshot.quality_flags
    assert abeo_snapshot.spot == pytest.approx(ABEO_SPOT)
    assert abeo_snapshot.event_id == EVENT_ID


def test_atm_without_any_price_is_an_error() -> None:
    """Obie nogi bez kwotowania i bez transakcji — wyjątek, nie EM równy zeru."""
    chain = make_chain(
        spot=6.0,
        calls=(quote(6, OptionType.CALL, bid=0.0, ask=0.5, last=0.0),),
        puts=(quote(6, OptionType.PUT, bid=0.0, ask=0.5, last=None),),
    )
    with pytest.raises(NoAtmPrice):
        compute_expected_move(
            chain,
            event_id=EVENT_ID,
            snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
        )


# ------------------------------------------------------------------ flagi jakości


def test_wide_spread_flag_takes_the_worse_leg(amat_near_snapshot: Any) -> None:
    """Call: 0,72/0,86 = 0,84; put: 1,31/1,345 = 0,97. Zapisujemy nogę gorszą."""
    assert amat_near_snapshot.rel_spread == pytest.approx(1.31 / 1.345)
    assert QualityFlag.WIDE_SPREAD in amat_near_snapshot.quality_flags


def test_narrow_spread_is_not_flagged() -> None:
    chain = ladder_chain((100.0,), spot=100.0)
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
    )
    assert snapshot.rel_spread == pytest.approx(0.0)
    assert QualityFlag.WIDE_SPREAD not in snapshot.quality_flags


def test_low_oi_uses_the_weaker_leg(amat_near_snapshot: Any) -> None:
    """OI 55 na callu i 121 na pucie — o dopuszczeniu decyduje noga słabsza."""
    assert amat_near_snapshot.oi_atm == 55
    assert QualityFlag.LOW_OI in amat_near_snapshot.quality_flags


def test_volume_sums_both_legs(amat_near_snapshot: Any) -> None:
    """Wolumen jest miarą aktywności na strike'u, nie warunkiem dopuszczenia."""
    assert amat_near_snapshot.volume_atm == 705 + 659


def test_oi_above_threshold_is_not_flagged(abeo_snapshot: Any) -> None:
    """Mikrospółka bywa flagowana za kwotowania, a nie za OI — 158 i 230 przechodzi."""
    assert abeo_snapshot.oi_atm == 158
    assert QualityFlag.LOW_OI not in abeo_snapshot.quality_flags


def test_unknown_oi_does_not_fake_a_low_oi_flag() -> None:
    """Brak OI to brak danych; zero uruchomiłoby flagę bez podstawy."""
    chain = make_chain(
        spot=100.0,
        calls=(quote(100, OptionType.CALL, bid=2, ask=3, oi=None),),
        puts=(quote(100, OptionType.PUT, bid=2, ask=3, oi=50),),
    )
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
    )
    assert snapshot.oi_atm is None
    assert QualityFlag.LOW_OI not in snapshot.quality_flags


def test_min_oi_threshold_comes_from_the_caller() -> None:
    chain = make_chain(
        spot=100.0,
        calls=(quote(100, OptionType.CALL, bid=2, ask=3, oi=80),),
        puts=(quote(100, OptionType.PUT, bid=2, ask=3, oi=80),),
    )
    kwargs: dict[str, Any] = {
        "event_id": EVENT_ID,
        "snapshot_at": datetime(2026, 8, 17, 15, 30, tzinfo=ET),
    }
    assert (
        QualityFlag.LOW_OI in compute_expected_move(chain, min_oi_atm=100, **kwargs).quality_flags
    )
    assert (
        QualityFlag.LOW_OI
        not in compute_expected_move(chain, min_oi_atm=50, **kwargs).quality_flags
    )


@pytest.mark.parametrize(
    ("snapshot_day", "expected_dte", "flagged"),
    [
        (date(2026, 8, 21), 0, False),
        (date(2026, 8, 19), 2, False),
        (date(2026, 8, 18), 3, True),
    ],
)
def test_dte_is_counted_from_the_snapshot_and_flagged_above_two(
    snapshot_day: date, expected_dte: int, flagged: bool
) -> None:
    """Mnożnik 0,85 zakłada wygaśnięcie tuż po wynikach — dalej EM jest zawyżony."""
    chain = ladder_chain((100.0,), spot=100.0)
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=datetime(
            snapshot_day.year, snapshot_day.month, snapshot_day.day, 15, 30, tzinfo=ET
        ),
    )
    assert snapshot.dte == expected_dte
    assert (QualityFlag.DTE_GT_2 in snapshot.quality_flags) is flagged


def test_stale_quote_is_flagged_when_data_is_older_than_allowed() -> None:
    chain = ladder_chain((100.0,), spot=100.0)
    snapshot_at = datetime(2026, 8, 17, 15, 30, tzinfo=ET)
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=snapshot_at,
        data_timestamp=snapshot_at - timedelta(minutes=31),
        stale_after=timedelta(minutes=30),
    )
    assert QualityFlag.STALE_QUOTE in snapshot.quality_flags


def test_quote_within_the_allowed_delay_is_not_stale() -> None:
    """Opóźnienie CBOE to typowo 15 minut — samo w sobie nie jest wadą."""
    chain = ladder_chain((100.0,), spot=100.0)
    snapshot_at = datetime(2026, 8, 17, 15, 30, tzinfo=ET)
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=snapshot_at,
        data_timestamp=snapshot_at - timedelta(minutes=15),
    )
    assert QualityFlag.STALE_QUOTE not in snapshot.quality_flags


def test_unknown_data_timestamp_is_not_stale() -> None:
    """Nie wiemy, jak stare są dane — to nie to samo co „dane są stare"."""
    chain = ladder_chain((100.0,), spot=100.0)
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
        data_timestamp=None,
    )
    assert QualityFlag.STALE_QUOTE not in snapshot.quality_flags


def test_fixture_recorded_after_the_session_is_stale(abeo_snapshot: Any) -> None:
    """Fixture ABEO ma znacznik z 15.08, skan udajemy 17.08 — dokładnie ten przypadek."""
    assert QualityFlag.STALE_QUOTE in abeo_snapshot.quality_flags


def test_flags_come_in_a_stable_order(abeo_snapshot: Any) -> None:
    """Kolejność flag to kolejność deklaracji enuma — inaczej ten sam snapshot dałby inny JSON."""
    assert abeo_snapshot.quality_flags == [
        QualityFlag.ZERO_BID,
        QualityFlag.STALE_QUOTE,
        QualityFlag.DTE_GT_2,
    ]


# ------------------------------------------------------------------ metody B i C bez danych


def test_method_b_needs_wings_on_both_sides_of_atm(amat: dict[date, OptionChain]) -> None:
    """ATM 505 jest drugie od dołu drabinki 21.08, więc strangle_2 nie ma dolnej nogi."""
    snapshot = compute_expected_move(
        amat[AMAT_FAR],
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 14, 15, 30, tzinfo=ET),
    )
    assert snapshot.em_abs_weighted is None
    assert snapshot.em_pct is not None


def test_method_b_does_not_substitute_a_neighbouring_strike() -> None:
    """Podstawienie sąsiada zmieniłoby definicję metody bez śladu w danych."""
    chain = make_chain(
        spot=100.0,
        calls=(
            quote(95, OptionType.CALL, bid=6, ask=7),
            quote(100, OptionType.CALL, bid=2, ask=3),
            quote(105, OptionType.CALL, bid=1, ask=2),
            quote(110, OptionType.CALL, bid=0.5, ask=1),
        ),
        puts=(
            quote(95, OptionType.PUT, bid=1, ask=2),
            quote(100, OptionType.PUT, bid=2, ask=3),
            quote(105, OptionType.PUT, bid=6, ask=7),
            quote(110, OptionType.PUT, bid=11, ask=12),
        ),
    )
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
    )
    # Drabinka 95-110, ATM 100 -> strangle_1 = 105C + 95P, ale strangle_2 wymaga strike'u
    # poniżej 95, którego nie ma.
    assert snapshot.em_abs_weighted is None


def test_symmetric_ladder_gives_method_b_both_strangles() -> None:
    chain = ladder_chain((90.0, 95.0, 100.0, 105.0, 110.0), spot=100.0, price=1.0)
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
    )
    # Każdy kontrakt wyceniony na 1.0, więc straddle i oba strangle wynoszą 2.0.
    assert snapshot.em_abs_weighted == pytest.approx(2.0)


def test_method_c_is_none_without_implied_volatility() -> None:
    """CBOE zwraca iv = 0 dla części kontraktów — zero znaczy „nie podano"."""
    chain = make_chain(
        spot=100.0,
        calls=(quote(100, OptionType.CALL, bid=2, ask=3, iv=0.0),),
        puts=(quote(100, OptionType.PUT, bid=2, ask=3, iv=None),),
    )
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
    )
    assert snapshot.iv_atm is None
    assert snapshot.em_pct_iv is None


def test_method_c_uses_the_only_leg_that_has_iv() -> None:
    chain = make_chain(
        spot=100.0,
        calls=(quote(100, OptionType.CALL, bid=2, ask=3, iv=0.6),),
        puts=(quote(100, OptionType.PUT, bid=2, ask=3, iv=0.0),),
    )
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
    )
    assert snapshot.iv_atm == pytest.approx(0.6)
    assert snapshot.em_pct_iv == pytest.approx(0.6 * math.sqrt(4 / 365))


def test_method_c_is_none_on_the_expiry_day(amat: dict[date, OptionChain]) -> None:
    """dte = 0 daje sqrt(0) = 0, czyli ciche zero. Metoda C wtedy nie istnieje."""
    snapshot = compute_expected_move(
        amat[AMAT_NEAR],
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 14, 15, 30, tzinfo=ET),
    )
    assert snapshot.dte == 0
    assert snapshot.iv_atm is not None
    assert snapshot.em_pct_iv is None


# ------------------------------------------------------------------ strefa czasowa


def test_naive_snapshot_timestamp_is_rejected() -> None:
    """SPEC §1.7: nigdy czas lokalny maszyny. Naiwny znacznik dałby dte z błędem godzin."""
    chain = ladder_chain((100.0,), spot=100.0)
    with pytest.raises(ValueError, match="strefę czasową"):
        compute_expected_move(
            chain,
            event_id=EVENT_ID,
            snapshot_at=datetime(2026, 8, 17, 15, 30),  # noqa: DTZ001
        )


def test_dte_is_counted_in_new_york_not_in_utc() -> None:
    """22:00 ET to już następny dzień w UTC — dte musi liczyć się po dacie nowojorskiej."""
    chain = ladder_chain((100.0,), spot=100.0)
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 20, 22, 0, tzinfo=ET),
    )
    assert snapshot.dte == 1


def test_snapshot_carries_the_underlying_quote(amat: dict[date, OptionChain]) -> None:
    """Snapshot musi zapisać koszt wejścia w akcje, nie tylko w opcje."""
    snapshot = compute_expected_move(
        amat[AMAT_NEAR],
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 14, 15, 30, tzinfo=ET),
    )
    assert (snapshot.underlying_bid, snapshot.underlying_ask) == (506.8, 507.0)


def test_missing_underlying_quote_stays_none() -> None:
    """Dostawca, który nie podaje kwotowania akcji, nie może dać zera w bazie."""
    chain = ladder_chain((100.0,), spot=100.0)
    snapshot = compute_expected_move(
        chain,
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
    )
    assert snapshot.underlying_bid is None
    assert snapshot.underlying_ask is None


def test_snapshot_carries_the_underlying_iv30(amat: dict[date, OptionChain]) -> None:
    """iv30 to reżim zmienności spółki, niezależny od wybranego wygaśnięcia."""
    snapshot = compute_expected_move(
        amat[AMAT_NEAR],
        event_id=EVENT_ID,
        snapshot_at=datetime(2026, 8, 14, 15, 30, tzinfo=ET),
    )
    assert snapshot.iv30 == pytest.approx(0.53017)
    # iv_atm dotyczy konkretnego kontraktu i przy wynikach jest wyższe.
    assert snapshot.iv_atm is not None and snapshot.iv_atm > snapshot.iv30

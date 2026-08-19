"""Silnik expected move — SPEC §1.5, docs/METHODOLOGY.md §2-4.

Wejściem jest `OptionChain` dla **jednego** wygaśnięcia, wyjściem `EmSnapshot`, czyli
wiersz tabeli `em_snapshots`. Silnik nie zna dostawcy: dostaje znormalizowany łańcuch,
więc podmiana CBOE na cokolwiek innego nie dotyka tego pliku.

Trzy rzeczy, które ten moduł rozstrzyga i których nie rozstrzyga źródło:

1. **Zejście na `lastPrice`.** `OptionQuote.mid` zwraca `None`, gdy którakolwiek noga
   kwotowania jest zerowa lub pusta — celowo, bo użycie ceny ostatniej transakcji musi
   iść w parze z flagą `zero_bid` (SPEC §1.5). Tę decyzję podejmuje silnik.
2. **Które kontrakty w ogóle wchodzą do rachunku.** Straddle wymaga obu nóg na tym
   samym strike'u, więc ATM wybieramy z drabinki strike'ów obecnych **po obu stronach**.
3. **Co jest błędem, a co flagą.** Brak ceny obu nóg ATM to `NoAtmPrice` — wyjątek,
   nie ciche zero (SPEC §Czego NIE robić). Natomiast szeroki spread, niskie OI, stara
   kwota czy odległe wygaśnięcie **nie** wykluczają rekordu: podnoszą flagę i wchodzą
   do bazy (SPEC §1.4).

Metody B i C mogą wyjść `None` przy zdrowym snapshocie — B, gdy drabinka nie ma
skrzydeł po obu stronach ATM, C, gdy dostawca nie podał IV albo `dte` wynosi zero.
`None` znaczy „nie da się policzyć", i tak trafia do bazy. Nigdy nie zastępujemy go zerem.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from emscan.log import get_logger
from emscan.models import EmSnapshot, QualityFlag
from emscan.sources.base import OptionChain, OptionQuote

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")

STRADDLE_MULTIPLIER = 0.85
"""Metoda A. Zakłada wygaśnięcie tuż po publikacji — patrz METHODOLOGY §4."""

WEIGHT_STRADDLE = 0.60
WEIGHT_STRANGLE_1 = 0.30
WEIGHT_STRANGLE_2 = 0.10
"""Metoda B, wagi wprost ze SPEC §1.5. Suma wag = 1, bez mnożnika 0.85."""

WIDE_SPREAD_THRESHOLD = 0.25
DTE_FLAG_THRESHOLD = 2
DEFAULT_MIN_OI_ATM = 100
"""Mirror filtru uniwersum ze SPEC §1.5. Skan podaje wartość z konfiguracji."""

DEFAULT_STALE_AFTER = timedelta(minutes=30)
"""Kwotowania CBOE są opóźnione typowo o 15 minut (PROBE-2026-08-17 §Ograniczenia).

Dwukrotność tego opóźnienia oznacza, że notowania przestały się odświeżać — na przykład
skan wystartował poza sesją.
"""

DAYS_PER_YEAR = 365.0


class ExpectedMoveError(Exception):
    """EM dla tego zdarzenia nie jest policzalny. Rekord nie powstaje, log zostaje."""


class NoUsableExpiry(ExpectedMoveError):
    """Brak wygaśnięcia w dniu sesji rozliczeniowej lub później."""


class NoAtmStrike(ExpectedMoveError):
    """Brak strike'u obecnego jednocześnie w callach i putach — nie ma z czego zrobić straddle'a."""


class NoAtmPrice(ExpectedMoveError):
    """Noga ATM nie ma ani kwotowania dwustronnego, ani ceny ostatniej transakcji."""


@dataclass(frozen=True)
class LegPrice:
    """Cena jednej nogi wraz z informacją, skąd się wzięła.

    Attributes:
        price: cena użyta w rachunku — mid albo `last`.
        used_last: True, gdy kwotowanie było jednostronne i zeszliśmy na `last`.
        rel_spread: `(ask - bid) / mid` albo None, gdy kwotowanie jednostronne.
    """

    price: float
    used_last: bool
    rel_spread: float | None


def select_expiry(expirations: Iterable[date], session_date: date) -> date:
    """Najwcześniejsze wygaśnięcie w dniu sesji rozliczeniowej lub później — SPEC §1.5.

    Raises:
        NoUsableExpiry: wszystkie wygaśnięcia są przed sesją rozliczeniową.
    """
    candidates = sorted(expiry for expiry in expirations if expiry >= session_date)
    if not candidates:
        raise NoUsableExpiry(
            f"brak wygaśnięcia >= {session_date.isoformat()} — opcje wygasły przed sesją"
        )
    return candidates[0]


def _by_strike(
    quotes: Iterable[OptionQuote], *, side: str, ticker: str
) -> dict[float, OptionQuote]:
    """Mapa strike -> kontrakt. Duplikat strike'u jest raportowany, wygrywa pierwszy."""
    indexed: dict[float, OptionQuote] = {}
    for quote in quotes:
        if quote.strike in indexed:
            log.warning(
                "zduplikowany strike w łańcuchu", ticker=ticker, side=side, strike=quote.strike
            )
            continue
        indexed[quote.strike] = quote
    return indexed


def common_strikes(chain: OptionChain) -> list[float]:
    """Strike'i obecne jednocześnie w callach i putach, rosnąco.

    Tylko na takiej drabince da się zbudować straddle i strangle — pojedyncza noga bez
    pary jest bezużyteczna dla wszystkich trzech metod.
    """
    calls = _by_strike(chain.calls, side="call", ticker=chain.ticker)
    puts = _by_strike(chain.puts, side="put", ticker=chain.ticker)
    return sorted(calls.keys() & puts.keys())


def _nearest_strike(ladder: list[float], spot: float) -> float:
    """Strike najbliższy spotowi; przy remisie **niższy**.

    Reguła remisu jest arbitralna, ale musi być deterministyczna — inaczej dwa skany
    tego samego łańcucha dałyby dwa różne wiersze.
    """
    return min(ladder, key=lambda strike: (abs(strike - spot), strike))


def _require_ladder(chain: OptionChain) -> list[float]:
    """Drabinka wspólnych strike'ów albo wyjątek.

    Raises:
        NoAtmStrike: żaden strike nie jest kwotowany po obu stronach.
    """
    ladder = common_strikes(chain)
    if not ladder:
        raise NoAtmStrike(
            f"{chain.ticker} {chain.expiry.isoformat()}: brak strike'u obecnego w callach i putach"
        )
    return ladder


def select_atm_strike(chain: OptionChain) -> float:
    """Strike najbliższy cenie spot — SPEC §1.5.

    Raises:
        NoAtmStrike: drabinka wspólnych strike'ów jest pusta.
    """
    return _nearest_strike(_require_ladder(chain), chain.spot)


def leg_price(quote: OptionQuote) -> LegPrice | None:
    """Cena nogi wg SPEC §1.5: mid, a przy kwotowaniu jednostronnym `last`.

    Zwraca None, gdy nie ma ani mid, ani dodatniej ceny ostatniej transakcji. `last`
    równe zero traktujemy jako brak danych, nie jako darmową opcję.
    """
    mid = quote.mid
    if mid is not None:
        bid, ask = quote.bid, quote.ask
        rel_spread = (ask - bid) / mid if bid is not None and ask is not None else None
        return LegPrice(price=mid, used_last=False, rel_spread=rel_spread)

    if quote.last is not None and quote.last > 0:
        return LegPrice(price=quote.last, used_last=True, rel_spread=None)

    return None


def _strangle_premium(
    ladder: list[float],
    calls: dict[float, OptionQuote],
    puts: dict[float, OptionQuote],
    *,
    atm_index: int,
    step: int,
) -> tuple[float, bool] | None:
    """Premia strangle'a `step` strike'ów od ATM: call wyżej, put niżej.

    Returns:
        (premia, czy_użyto_last) albo None, gdy któregoś skrzydła nie ma w drabince
        lub nie da się go wycenić. Nie podstawiamy sąsiedniego strike'u — to zmieniłoby
        definicję metody bez śladu w danych.
    """
    call_index, put_index = atm_index + step, atm_index - step
    if put_index < 0 or call_index >= len(ladder):
        return None

    call_leg = leg_price(calls[ladder[call_index]])
    put_leg = leg_price(puts[ladder[put_index]])
    if call_leg is None or put_leg is None:
        return None
    return call_leg.price + put_leg.price, call_leg.used_last or put_leg.used_last


def _atm_iv(call: OptionQuote, put: OptionQuote) -> float | None:
    """Średnia IV obu nóg ATM. Zero od dostawcy znaczy „nie podano", nie „zerowa zmienność".

    CBOE zwraca `iv: 0.0` dla części kontraktów (np. AMAT 500C w fixture) — użycie takiej
    wartości dałoby EM równy zero, czyli dokładnie ciche zero, którego SPEC zakazuje.
    """
    values = [
        iv for iv in (call.implied_volatility, put.implied_volatility) if iv is not None and iv > 0
    ]
    if not values:
        return None
    return sum(values) / len(values)


def _et_date(moment: datetime) -> date:
    """Data kalendarzowa momentu w czasie nowojorskim — SPEC §1.7, nigdy czas lokalny maszyny.

    Raises:
        ValueError: znacznik bez strefy. Naiwny `datetime` przy dte liczonym w ET daje
            cichy błąd kilku godzin, więc odrzucamy go wprost.
    """
    if moment.tzinfo is None:
        raise ValueError("snapshot_at musi mieć strefę czasową — patrz SPEC §1.7")
    return moment.astimezone(ET).date()


def _ordered_flags(flags: set[QualityFlag]) -> list[QualityFlag]:
    """Flagi w kolejności deklaracji enuma — żeby ten sam snapshot dawał ten sam JSON."""
    return [flag for flag in QualityFlag if flag in flags]


def compute_expected_move(
    chain: OptionChain,
    *,
    event_id: int,
    snapshot_at: datetime,
    data_timestamp: datetime | None = None,
    min_oi_atm: int = DEFAULT_MIN_OI_ATM,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> EmSnapshot:
    """Liczy trzy warianty EM z łańcucha jednego wygaśnięcia — SPEC §1.5.

    Args:
        chain: łańcuch dla wygaśnięcia wybranego wcześniej przez `select_expiry`.
        event_id: klucz zdarzenia z `earnings_events`. Snapshot jest wierszem tabeli,
            więc identyfikator podaje wołający (`scan`), a nie silnik.
        snapshot_at: moment pobrania danych, ze strefą. Od niego liczy się `dte`.
        data_timestamp: znacznik danych podany przez dostawcę (`CboeOptionsSource
            .data_timestamp`). None znaczy „nie wiadomo, jak stare" — to **nie** jest
            podstawa do flagi `stale_quote`.
        min_oi_atm: próg OI dla flagi `low_oi`.
        stale_after: dopuszczalny wiek kwotowania.

    Returns:
        `EmSnapshot` z wypełnionym wariantem A oraz — o ile dane pozwalają — B i C.

    Raises:
        NoAtmStrike: brak wspólnego strike'u dla calli i putów.
        NoAtmPrice: noga ATM bez mid i bez `last` — EM nie istnieje, rekord nie powstaje.
        ValueError: `snapshot_at` bez strefy czasowej.
    """
    ladder = _require_ladder(chain)
    calls = _by_strike(chain.calls, side="call", ticker=chain.ticker)
    puts = _by_strike(chain.puts, side="put", ticker=chain.ticker)
    atm_strike = _nearest_strike(ladder, chain.spot)
    atm_index = ladder.index(atm_strike)
    atm_call, atm_put = calls[atm_strike], puts[atm_strike]

    call_leg = leg_price(atm_call)
    put_leg = leg_price(atm_put)
    if call_leg is None or put_leg is None:
        missing = "call" if call_leg is None else "put"
        raise NoAtmPrice(
            f"{chain.ticker} {chain.expiry.isoformat()} @{atm_strike}: noga {missing} bez "
            "kwotowania i bez ceny ostatniej transakcji"
        )

    flags: set[QualityFlag] = set()

    # --- metoda A: 0.85 * straddle ---
    straddle = call_leg.price + put_leg.price
    em_abs = STRADDLE_MULTIPLIER * straddle
    em_pct = em_abs / chain.spot

    if call_leg.used_last or put_leg.used_last:
        flags.add(QualityFlag.ZERO_BID)
        log.warning(
            "kwotowanie jednostronne na ATM, mid z lastPrice",
            ticker=chain.ticker,
            expiry=chain.expiry.isoformat(),
            strike=atm_strike,
            call_from_last=call_leg.used_last,
            put_from_last=put_leg.used_last,
        )

    # Spread względny liczymy tylko z nóg kwotowanych dwustronnie; przy zejściu na `last`
    # jest niedefiniowany, a flaga zero_bid mówi o jakości więcej niż zmyślona liczba.
    spreads = [leg.rel_spread for leg in (call_leg, put_leg) if leg.rel_spread is not None]
    rel_spread = max(spreads) if spreads else None
    if rel_spread is not None and rel_spread > WIDE_SPREAD_THRESHOLD:
        flags.add(QualityFlag.WIDE_SPREAD)

    # --- metoda B: 60/30/10 ---
    em_abs_weighted: float | None = None
    em_pct_weighted: float | None = None
    strangle_1 = _strangle_premium(ladder, calls, puts, atm_index=atm_index, step=1)
    strangle_2 = _strangle_premium(ladder, calls, puts, atm_index=atm_index, step=2)
    if strangle_1 is not None and strangle_2 is not None:
        em_abs_weighted = (
            WEIGHT_STRADDLE * straddle
            + WEIGHT_STRANGLE_1 * strangle_1[0]
            + WEIGHT_STRANGLE_2 * strangle_2[0]
        )
        em_pct_weighted = em_abs_weighted / chain.spot
        if strangle_1[1] or strangle_2[1]:
            flags.add(QualityFlag.ZERO_BID)
    else:
        log.info(
            "drabinka nie ma skrzydeł po obu stronach ATM, metoda B pominięta",
            ticker=chain.ticker,
            expiry=chain.expiry.isoformat(),
            strikes=len(ladder),
            atm_index=atm_index,
        )

    # --- metoda C: spot * IV * sqrt(dte/365) ---
    dte = (chain.expiry - _et_date(snapshot_at)).days
    iv_atm = _atm_iv(atm_call, atm_put)
    em_pct_iv: float | None = None
    if iv_atm is not None and dte >= 1:
        em_pct_iv = iv_atm * math.sqrt(dte / DAYS_PER_YEAR)

    if dte > DTE_FLAG_THRESHOLD:
        flags.add(QualityFlag.DTE_GT_2)

    # --- pozostałe flagi jakości ---
    oi_atm = _weaker_leg(atm_call.open_interest, atm_put.open_interest)
    if oi_atm is not None and oi_atm < min_oi_atm:
        flags.add(QualityFlag.LOW_OI)

    if data_timestamp is not None and snapshot_at - data_timestamp > stale_after:
        flags.add(QualityFlag.STALE_QUOTE)
        log.warning(
            "kwotowanie starsze niż dopuszczalne",
            ticker=chain.ticker,
            data_timestamp=data_timestamp.isoformat(),
            snapshot_at=snapshot_at.isoformat(),
            age_minutes=round((snapshot_at - data_timestamp).total_seconds() / 60, 1),
        )

    return EmSnapshot(
        event_id=event_id,
        snapshot_at=snapshot_at,
        spot=chain.spot,
        expiry=chain.expiry,
        dte=dte,
        atm_strike=atm_strike,
        call_bid=atm_call.bid,
        call_ask=atm_call.ask,
        put_bid=atm_put.bid,
        put_ask=atm_put.ask,
        call_mid=call_leg.price,
        put_mid=put_leg.price,
        straddle=straddle,
        em_abs=em_abs,
        em_pct=em_pct,
        em_abs_weighted=em_abs_weighted,
        em_pct_weighted=em_pct_weighted,
        em_pct_iv=em_pct_iv,
        iv_atm=iv_atm,
        oi_atm=oi_atm,
        volume_atm=_leg_sum(atm_call.volume, atm_put.volume),
        rel_spread=rel_spread,
        quality_flags=_ordered_flags(flags),
    )


def _weaker_leg(call_value: int | None, put_value: int | None) -> int | None:
    """Mniejsza z dwóch wartości; None, gdy którejkolwiek nie znamy.

    Filtr płynności ma odrzucać straddle'a, którego **jedna** noga jest niehandlowalna,
    więc rządzi noga słabsza. Brak danych to brak danych — nie zastępujemy go zerem,
    bo zero uruchomiłoby flagę `low_oi` bez podstawy.
    """
    if call_value is None or put_value is None:
        return None
    return min(call_value, put_value)


def _leg_sum(call_value: int | None, put_value: int | None) -> int | None:
    """Suma obu nóg; None, gdy którejkolwiek nie znamy.

    Wolumen jest miarą aktywności na strike'u, nie warunkiem dopuszczenia — dlatego
    sumujemy, w odróżnieniu od OI.
    """
    if call_value is None or put_value is None:
        return None
    return call_value + put_value

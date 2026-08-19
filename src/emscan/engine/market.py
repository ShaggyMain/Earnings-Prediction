"""Dane reżimu rynkowego — cechy „szerokiego rynku" ze SPEC §2.3.

Pojedyncza spółka nie rusza się w próżni: ten sam raport przy VIX 15 i przy VIX 35 daje inną
reakcję. SPEC §2.3 wymienia VIX, zmianę VIX 5d i zwrot SPY 5d jako cechy fazy 2. Ten moduł
zbiera surowce, z których te cechy się liczy — sam ich nie liczy, bo to już faza 2.

## Co jest osiągalne, sprawdzone 2026-08-19

| Instrument | Jak | Wynik |
|---|---|---|
| SPY, QQQ, IWM, VXX | Nasdaq, `assetclass=etf` | działa; przy `stocks` zero wierszy, nie błąd |
| Indeks VIX | `assetclass=index` | **nie działa**, zero wierszy |
| Zmienność rynku | `iv30` z CBOE dla SPY | działa, lepszy zamiennik VIX niż VXX |

VXX zostaje w zestawie, ale z ostrzeżeniem: to ETN na kontrakty terminowe VIX, obciążony
kosztem rolowania, więc **nie jest poziomem VIX** i nie wolno go tak traktować. Do reżimu
zmienności służy `iv30` SPY; VXX jest materiałem pomocniczym.

Świece trafiają do tabeli `daily_bars`, tej samej co historia spółek — jeden szereg czasowy
na ticker, więc faza 2 pyta o rynek i o spółkę tym samym zapytaniem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from sqlite3 import Connection

from emscan.db import record_iv30, upsert_bars
from emscan.log import get_logger
from emscan.sources.base import OptionsChainSource, PriceSource, SourceError

log = get_logger(__name__)


@dataclass(frozen=True)
class MarketInstrument:
    """Instrument reprezentujący reżim rynkowy."""

    ticker: str
    role: str
    """Po co go trzymamy — trafia do logu, żeby dane nie były anonimowe."""
    with_iv30: bool = False
    """Czy pytać dostawcę opcji o zmienność implikowaną tego instrumentu."""


MARKET_INSTRUMENTS: tuple[MarketInstrument, ...] = (
    MarketInstrument("SPY", "szeroki rynek", with_iv30=True),
    MarketInstrument("QQQ", "spółki technologiczne"),
    MarketInstrument("IWM", "małe spółki"),
    MarketInstrument("VXX", "reżim zmienności (ETN na futures VIX, obciążony rolowaniem)"),
)


@dataclass(frozen=True)
class MarketResult:
    """Wynik jednego pobrania danych rynkowych."""

    start: date
    end: date
    bars_written: int = 0
    instruments_fetched: tuple[str, ...] = field(default_factory=tuple)
    iv30_recorded: dict[str, float] = field(default_factory=dict)
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return not self.failures


def run_market_update(
    *,
    conn: Connection,
    prices: PriceSource,
    start: date,
    end: date,
    fetched_at: datetime,
    options: OptionsChainSource | None = None,
    instruments: tuple[MarketInstrument, ...] = MARKET_INSTRUMENTS,
) -> MarketResult:
    """Pobiera świece instrumentów rynkowych i zapisuje je do `daily_bars`.

    Args:
        prices: źródło cen **skonfigurowane na klasę `etf`** — przy `stocks` te tickery
            zwracają pustą tabelę, co wygląda jak brak danych, a jest złym parametrem.
        options: dostawca opcji dla `iv30`. None pomija pomiar zmienności bez błędu.

    Awaria jednego instrumentu nie kończy pobrania. Wyjątku nie podnosimy nawet wtedy, gdy
    padną wszystkie: dane rynkowe są cechą pomocniczą, a nie warunkiem działania skanu.
    """
    failures: list[str] = []
    fetched: list[str] = []
    iv30: dict[str, float] = {}
    written = 0

    for instrument in instruments:
        try:
            bars = prices.daily_bars(instrument.ticker, start, end)
        except SourceError as exc:
            failures.append(f"{instrument.ticker}: {exc}")
            log.warning(
                "nie udało się pobrać instrumentu rynkowego",
                ticker=instrument.ticker,
                error=str(exc),
            )
            continue

        if not bars:
            failures.append(f"{instrument.ticker}: zero świec — sprawdź klasę aktywów")
            log.warning("instrument rynkowy bez świec", ticker=instrument.ticker)
            continue

        written += upsert_bars(conn, instrument.ticker, bars, fetched_at=fetched_at)
        fetched.append(instrument.ticker)
        log.info(
            "instrument rynkowy",
            ticker=instrument.ticker,
            role=instrument.role,
            bars=len(bars),
            last=bars[-1].day.isoformat(),
        )

        if instrument.with_iv30 and options is not None:
            measured = _measure_iv30(options, instrument.ticker)
            if measured is not None and record_iv30(
                conn, instrument.ticker, bars[-1].day, measured
            ):
                iv30[instrument.ticker] = measured

    return MarketResult(
        start=start,
        end=end,
        bars_written=written,
        instruments_fetched=tuple(fetched),
        iv30_recorded=iv30,
        failures=tuple(failures),
    )


def _measure_iv30(options: OptionsChainSource, ticker: str) -> float | None:
    """IV30 z dostawcy opcji, jeśli ten ją udostępnia.

    Interfejs `OptionsChainSource.iv30` zwraca domyślnie None, więc dostawca bez tej danej
    nie wymaga tu żadnego wyjątku — i nie ma powodu sprawdzać, którą implementację dostaliśmy.
    """
    try:
        return options.iv30(ticker)
    except SourceError as exc:
        log.warning("nie udało się zmierzyć iv30", ticker=ticker, error=str(exc))
        return None

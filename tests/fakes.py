"""Atrapy źródeł do testów przepływu — SPEC §B.3 pkt 6: zero sieci.

Atrapy siedzą za tymi samymi interfejsami co CBOE i Nasdaq, więc testują dokładnie ten
kontrakt, którego używa produkcja. Każda umie też **zawieść** na żądanie: awaria jednego
tickera jest normalnym stanem skanu, nie sytuacją wyjątkową, i musi być pokryta testem.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from emscan.models import RawEarningsRecord, Timing
from emscan.sources.base import (
    DailyBar,
    EarningsCalendarSource,
    OptionChain,
    OptionQuote,
    OptionsChainSource,
    OptionType,
    PriceSource,
    SourceUnavailable,
    SymbolNotCovered,
)

ET = ZoneInfo("America/New_York")


def record(
    ticker: str,
    event_date: date,
    timing: Timing,
    *,
    source: str = "fake-calendar",
) -> RawEarningsRecord:
    return RawEarningsRecord(source=source, ticker=ticker, event_date=event_date, timing=timing)


def simple_chain(
    ticker: str,
    expiry: date,
    *,
    spot: float = 100.0,
    leg_price: float = 5.0,
    oi: int = 500,
    strikes: tuple[float, ...] = (90.0, 95.0, 100.0, 105.0, 110.0),
    iv: float = 0.5,
) -> OptionChain:
    """Symetryczna drabinka z jedną ceną na kontrakt — łatwo policzyć oczekiwany EM.

    `straddle = 2 * leg_price`, więc EM metody A wynosi `0.85 * 2 * leg_price / spot`.
    """

    def quotes(option_type: OptionType) -> tuple[OptionQuote, ...]:
        return tuple(
            OptionQuote(
                strike=strike,
                option_type=option_type,
                bid=leg_price,
                ask=leg_price,
                last=leg_price,
                open_interest=oi,
                volume=100,
                implied_volatility=iv,
            )
            for strike in strikes
        )

    return OptionChain(
        ticker=ticker,
        expiry=expiry,
        spot=spot,
        calls=quotes(OptionType.CALL),
        puts=quotes(OptionType.PUT),
        fetched_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
    )


def bars(volume: int, *, sessions: int = 20, day: date = date(2026, 8, 17)) -> list[DailyBar]:
    """`sessions` świec o stałym wolumenie — do filtra płynności."""
    return [
        DailyBar(
            day=date.fromordinal(day.toordinal() - offset),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=volume,
        )
        for offset in reversed(range(sessions))
    ]


class FakeCalendar(EarningsCalendarSource):
    """Kalendarz z zadanych rekordów. `failing_days` udaje niedostępne źródło."""

    def __init__(
        self,
        records: Iterable[RawEarningsRecord],
        *,
        name: str = "fake-calendar",
        failing_days: frozenset[date] = frozenset(),
    ) -> None:
        self.name = name
        self._records = list(records)
        self._failing_days = failing_days
        self.days_fetched: list[date] = []

    def fetch_day(self, day: date) -> list[RawEarningsRecord]:
        self.days_fetched.append(day)
        if day in self._failing_days:
            raise SourceUnavailable(f"{self.name}: udawana awaria dla {day.isoformat()}")
        return [
            RawEarningsRecord(**{**rec.model_dump(), "source": self.name})
            for rec in self._records
            if rec.event_date == day
        ]


class FakeOptions(OptionsChainSource):
    """Łańcuchy opcji z pamięci, z możliwością udawania braku pokrycia i awarii."""

    name = "fake-options"

    def __init__(
        self,
        chains: dict[str, dict[date, OptionChain]],
        *,
        volumes: dict[str, int] | None = None,
        not_covered: frozenset[str] = frozenset(),
        failing: frozenset[str] = frozenset(),
        timestamps: dict[str, datetime] | None = None,
    ) -> None:
        self._chains = chains
        self._volumes = volumes or {}
        self._not_covered = not_covered
        self._failing = failing
        self._timestamps = timestamps or {}
        self.tickers_fetched: list[str] = []

    def _check(self, ticker: str) -> dict[date, OptionChain]:
        if ticker in self._not_covered:
            raise SymbolNotCovered(f"{self.name}: {ticker} bez notowanych opcji")
        if ticker in self._failing:
            raise SourceUnavailable(f"{self.name}: udawana awaria dla {ticker}")
        return self._chains.get(ticker, {})

    def expirations(self, ticker: str) -> list[date]:
        self.tickers_fetched.append(ticker)
        return sorted(self._check(ticker))

    def chain(self, ticker: str, expiry: date) -> OptionChain:
        return self._check(ticker)[expiry]

    def underlying_volume(self, ticker: str) -> int | None:
        return self._volumes.get(ticker)

    def data_timestamp(self, ticker: str) -> datetime | None:
        return self._timestamps.get(ticker)


class FakePrices(PriceSource):
    """Świece z pamięci. Liczy zapytania — to ona pokazuje, czy kaskada działa."""

    name = "fake-prices"

    def __init__(
        self,
        history: dict[str, list[DailyBar]],
        *,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self._history = history
        self._failing = failing
        self.tickers_fetched: list[str] = []

    def daily_bars(self, ticker: str, start: date, end: date) -> list[DailyBar]:
        self.tickers_fetched.append(ticker)
        if ticker in self._failing:
            raise SourceUnavailable(f"{self.name}: udawana awaria dla {ticker}")
        return [bar for bar in self._history.get(ticker, []) if start <= bar.day <= end]

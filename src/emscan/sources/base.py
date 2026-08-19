"""Interfejsy źródeł danych — SPEC §1.2.

Każde źródło stoi za interfejsem i jest wymienne przez config. Żaden dostawca nie
może wyciec do logiki biznesowej: silnik EM dostaje `OptionChain`, a nie odpowiedź
konkretnego API.

Zasada nadrzędna (SPEC §Czego NIE robić): źródło zawiodło → **wyjątek i log**.
Nigdy ciche zero, nigdy syntetyczny wypełniacz.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from emscan.models import RawEarningsRecord


class SourceError(Exception):
    """Błąd źródła danych. Nigdy nie tłumimy go na cichą wartość domyślną."""


class SourceUnavailable(SourceError):
    """Źródło nie odpowiedziało: sieć, timeout, 5xx, przekroczony limit zapytań."""


class SourceDataError(SourceError):
    """Źródło odpowiedziało, ale treść jest niezdatna do użycia lub zmienił się kontrakt."""


class SymbolNotCovered(SourceError):
    """Źródło nie prowadzi danych dla tego tickera — to informacja, nie awaria.

    Typowy przypadek: spółka bez notowanych opcji. CBOE zwraca wtedy HTTP 403,
    co wygląda jak błąd uprawnień, a oznacza po prostu brak instrumentu.
    Zdarzenie zostaje w bazie z flagą, ale nie wchodzi do skanu.
    """


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class OptionQuote(BaseModel):
    """Pojedynczy kontrakt opcyjny — znormalizowany, niezależny od dostawcy."""

    model_config = ConfigDict(frozen=True)

    strike: float
    option_type: OptionType
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: float | None = None

    @property
    def mid(self) -> float | None:
        """Środek spreadu. None, gdy którakolwiek noga kwotowania jest pusta lub zerowa.

        Zejście na `last` przy zerowym bid/ask jest decyzją **silnika EM**, nie źródła —
        bo to on musi przy okazji podnieść flagę `zero_bid` (SPEC §1.5).
        """
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2


class OptionChain(BaseModel):
    """Łańcuch opcji dla jednego wygaśnięcia."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    expiry: date
    spot: float
    calls: tuple[OptionQuote, ...]
    puts: tuple[OptionQuote, ...]
    fetched_at: datetime


class DailyBar(BaseModel):
    """Świeca dzienna OHLC."""

    model_config = ConfigDict(frozen=True)

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class EarningsCalendarSource(ABC):
    """Kalendarz wyników z flagą BMO/AMC."""

    name: str

    def close(self) -> None:
        """Zwalnia zasoby źródła (klient HTTP). Źródło bez zasobów nie musi tego nadpisywać."""
        return None

    @abstractmethod
    def fetch_day(self, day: date) -> list[RawEarningsRecord]:
        """Zdarzenia z publikacją w dniu `day`.

        Raises:
            SourceUnavailable: źródło nie odpowiedziało.
            SourceDataError: odpowiedź nie da się sparsować.
        """


class OptionsChainSource(ABC):
    """Łańcuch opcji. Implementacja Tradiera powstaje po weryfikacji kształtu API."""

    name: str

    def close(self) -> None:
        """Zwalnia zasoby źródła (klient HTTP). Źródło bez zasobów nie musi tego nadpisywać."""
        return None

    @abstractmethod
    def expirations(self, ticker: str) -> list[date]:
        """Dostępne daty wygaśnięcia, rosnąco."""

    @abstractmethod
    def chain(self, ticker: str, expiry: date) -> OptionChain:
        """Łańcuch dla konkretnego wygaśnięcia, wraz z ceną spot."""

    # Poniższe dwie metody są **opcjonalne**: nie każdy dostawca je udźwignie, a oba
    # pojęcia są od dostawcy niezależne, więc mają miejsce w interfejsie, nie w silniku.
    # Domyślne None znaczy „nie wiadomo" i nigdy nie jest zastępowane wartością udawaną.

    def data_timestamp(self, ticker: str) -> datetime | None:
        """Jak stare są kwotowania według dostawcy — podstawa flagi `stale_quote`.

        None **nie** podnosi flagi: „nie wiemy, jak stare są dane" to nie to samo co
        „dane są stare".
        """
        return None

    def underlying_volume(self, ticker: str) -> int | None:
        """Wolumen instrumentu bazowego w bieżącej sesji, jeśli dostawca go podaje.

        Tani filtr wstępny kaskady (SPEC §B.2). U CBOE przychodzi w tej samej odpowiedzi
        co łańcuch, więc nie kosztuje ani jednego dodatkowego zapytania. **Nie zastępuje**
        średniej 20-sesyjnej — służy tylko do odsiania spółek oczywiście niehandlowanych.
        """
        return None


class PriceSource(ABC):
    """Ceny OHLC. Implementacja Tradiera powstaje po weryfikacji kształtu API."""

    name: str

    def close(self) -> None:
        """Zwalnia zasoby źródła (klient HTTP). Źródło bez zasobów nie musi tego nadpisywać."""
        return None

    @abstractmethod
    def daily_bars(self, ticker: str, start: date, end: date) -> list[DailyBar]:
        """Świece dzienne w zakresie włącznie, rosnąco po dacie.

        Brak notowań w części zakresu (święto, zawieszenie) oznacza **mniej świec**,
        nigdy świecę wypełnioną zerami.
        """

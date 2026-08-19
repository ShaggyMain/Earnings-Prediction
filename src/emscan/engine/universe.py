"""Filtry uniwersum — SPEC §1.5, ułożone w kaskadę ze SPEC §B.2.

Kaskada nie jest tu ozdobą, tylko sposobem trzymania liczby zapytań pod kontrolą.
Kolejność jest dobrana tak, żeby każdy kolejny etap kosztował więcej od poprzedniego:

| Etap | Co odrzuca | Koszt |
|---|---|---|
| 0 | zdarzenia bez jednoznacznej sesji | 0 zapytań |
| 1 | brak opcji lub wygaśnięcia, tania spółka, wolumen sesji pod podłogą | 1 zapytanie (CBOE) |
| 2 | niskie OI, EM poniżej progu | 0 — łańcuch już jest |
| 3 | wolumen 20-sesyjny poniżej progu | 1 zapytanie (Nasdaq), tylko dla ocalałych |

Etap 1 filtruje po `spot` i wolumenie bieżącej sesji, bo CBOE podaje jedno i drugie
w tej samej odpowiedzi co łańcuch — nie kosztują ani jednego dodatkowego zapytania.
Jeden dzień to jednak nie średnia 20-sesyjna, więc wolumen sesji służy wyłącznie jako
**podłoga**: odrzuca spółki oczywiście niehandlowane, a nie te, które miały spokojny
dzień. Rozstrzygnięcie zapada w etapie 3, na prawdziwej średniej.

Zasada nadrzędna: filtr decyduje o tym, co wchodzi **do raportu**. Zdarzenie zostaje
w bazie zawsze, a policzony snapshot zapisujemy nawet wtedy, gdy EM nie sięgnął progu —
to poprawny pomiar, potrzebny w fazie 2 do rozkładu VRP (SPEC §1.4, §2.1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from emscan.config import Settings
from emscan.models import EmSnapshot
from emscan.sources.base import DailyBar


class RejectReason(StrEnum):
    """Dlaczego ticker nie wchodzi do raportu. Rekord w bazie zostaje niezależnie od tego."""

    NO_SESSION = "no_session"
    """Timing DMH lub UNKNOWN — nie ma jednoznacznej sesji rozliczeniowej."""
    NO_OPTIONS = "no_options"
    """Spółka nie ma notowanych opcji (`SymbolNotCovered`)."""
    NO_EXPIRY = "no_expiry"
    """Brak wygaśnięcia w dniu sesji rozliczeniowej lub później."""
    LOW_PRICE = "low_price"
    """`spot` poniżej progu."""
    THIN_SESSION_VOLUME = "thin_session_volume"
    """Wolumen bieżącej sesji poniżej podłogi filtra wstępnego."""
    THIN_VOLUME_20D = "thin_volume_20d"
    """Średni wolumen 20-sesyjny poniżej progu."""
    NO_PRICE_HISTORY = "no_price_history"
    """Źródło cen nie zwróciło ani jednej sesji — nie ma czego uśrednić."""
    NO_EM = "no_em"
    """EM niepoliczalny: brak wspólnego strike'u albo nogi ATM bez ceny."""
    LOW_OI = "low_oi"
    """`oi_atm` poniżej progu."""
    LOW_EM = "low_em"
    """`em_pct` poniżej progu — spółka jest płynna, tylko rynek nie wycenia ruchu."""
    SOURCE_ERROR = "source_error"
    """Źródło zawiodło dla tego tickera. Skan idzie dalej, powód zostaje w wyniku i logu."""


@dataclass(frozen=True)
class UniverseFilters:
    """Progi filtrów. Domyślne wartości ze SPEC §1.5, nadpisywalne z CLI i z `.env`."""

    min_price: float = 5.0
    min_volume_20d: int = 500_000
    min_oi_atm: int = 100
    min_em_pct: float = 0.06

    session_volume_floor: float = 0.2
    """Ułamek `min_volume_20d`, poniżej którego odrzucamy już na wolumenie jednej sesji.

    Próg jest celowo luźny. Twarde porównanie wolumenu jednego dnia z progiem 20-sesyjnym
    wyrzucałoby spółki, które miały spokojny dzień przed wynikami — a to akurat te,
    o które w tym projekcie chodzi.
    """

    volume_window: int = 20
    """Liczba sesji do średniej. SPEC §1.5 mówi „wolumen 20d"."""

    @classmethod
    def from_settings(cls, settings: Settings) -> UniverseFilters:
        """Progi z konfiguracji. Pozostałe pola zostają domyślne — nie są w `.env`."""
        return cls(
            min_price=settings.min_price,
            min_volume_20d=settings.min_volume_20d,
            min_oi_atm=settings.min_oi_atm,
            min_em_pct=settings.min_em_pct,
        )

    @property
    def session_volume_threshold(self) -> float:
        """Podłoga filtra wstępnego, w akcjach."""
        return self.min_volume_20d * self.session_volume_floor


def check_spot(spot: float, filters: UniverseFilters) -> RejectReason | None:
    """Filtr ceny. Zwraca powód odrzucenia albo None, gdy ticker przechodzi."""
    return RejectReason.LOW_PRICE if spot < filters.min_price else None


def prescreen_session_volume(volume: int | None, filters: UniverseFilters) -> RejectReason | None:
    """Filtr wstępny na wolumenie bieżącej sesji.

    Nieznany wolumen **nie** odrzuca: dostawca, który go nie podaje, nie jest dowodem
    na to, że spółka jest niehandlowana. Rozstrzyga wtedy dopiero etap 3.
    """
    if volume is None:
        return None
    return RejectReason.THIN_SESSION_VOLUME if volume < filters.session_volume_threshold else None


def average_volume(bars: Sequence[DailyBar], *, window: int) -> float | None:
    """Średni wolumen z ostatnich `window` sesji.

    Krótsza historia (świeże IPO) daje średnią z tego, co jest — odrzucenie spółki tylko
    za młody wiek notowania byłoby cichym zawężeniem uniwersum. Brak jakichkolwiek świec
    daje None, nie zero.
    """
    if not bars:
        return None
    recent = sorted(bars, key=lambda bar: bar.day)[-window:]
    return sum(bar.volume for bar in recent) / len(recent)


def check_volume_20d(
    bars: Sequence[DailyBar], filters: UniverseFilters
) -> tuple[float | None, RejectReason | None]:
    """Filtr płynności na prawdziwej średniej 20-sesyjnej.

    Returns:
        Para (średni wolumen, powód odrzucenia). Średnia wraca także przy odrzuceniu —
        raport i log mają pokazać liczbę, która o nim zdecydowała.
    """
    average = average_volume(bars, window=filters.volume_window)
    if average is None:
        return None, RejectReason.NO_PRICE_HISTORY
    if average < filters.min_volume_20d:
        return average, RejectReason.THIN_VOLUME_20D
    return average, None


def check_snapshot(snapshot: EmSnapshot, filters: UniverseFilters) -> RejectReason | None:
    """Filtry liczone z gotowego snapshotu: OI i wysokość EM.

    Nieznane `oi_atm` nie odrzuca — silnik zostawia tam None tylko wtedy, gdy dostawca
    milczy, a brak danych to nie to samo co niskie OI (patrz METHODOLOGY §3).
    """
    if snapshot.oi_atm is not None and snapshot.oi_atm < filters.min_oi_atm:
        return RejectReason.LOW_OI
    if snapshot.em_pct is None or snapshot.em_pct < filters.min_em_pct:
        return RejectReason.LOW_EM
    return None

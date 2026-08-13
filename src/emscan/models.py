"""Modele domenowe — SPEC §1.4.

Nazwy pól trzymają się słownika ze SPEC §1. Trzy rozszerzenia wobec schematu §1.4,
uzgodnione 2026-08-13 i opisane w docs/PLAN-faza-1.md:

1. `Timing.DMH` — Finnhub zwraca `hour: dmh` (publikacja w trakcie sesji). SPEC §1.2
   wymienia tę wartość, ale schemat §1.4 jej nie przewidywał. Rekord trafia do bazy
   z pełną informacją, natomiast EM dla niego nie liczymy — przy publikacji w trakcie
   sesji ani `session_date`, ani `baseline_close` nie są jednoznaczne.
2. `TimingConfidence` jako enum HIGH/MEDIUM/LOW/UNKNOWN — SPEC każe zapisywać
   `timing_confidence`, ale nie definiuje skali.
3. `eps_actual_present` — sygnał, że źródło zna już opublikowany EPS, czyli publikacja
   nastąpiła. Zapisywany wyłącznie jako informacja; **nie** rozstrzyga konfliktu timingu.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Timing(StrEnum):
    """Pora publikacji względem sesji USA."""

    BMO = "BMO"
    """Before market open — przed otwarciem."""
    AMC = "AMC"
    """After market close — po zamknięciu."""
    DMH = "DMH"
    """During market hours — w trakcie sesji. EM nieliczone, patrz docstring modułu."""
    UNKNOWN = "UNKNOWN"
    """Źródło nie podało pory. Nie zgadujemy — SPEC §1.2."""


class TimingConfidence(StrEnum):
    """Na ile ufamy ustalonej porze publikacji."""

    HIGH = "HIGH"
    """Co najmniej dwa źródła podały tę samą porę."""
    MEDIUM = "MEDIUM"
    """Dokładnie jedno źródło zna porę, pozostałe milczą."""
    LOW = "LOW"
    """Źródła podały sprzeczne pory — timing wynikowy jest UNKNOWN."""
    UNKNOWN = "UNKNOWN"
    """Żadne źródło nie podało pory."""


class Direction(StrEnum):
    """Kierunek ruchu w sesji rozliczeniowej."""

    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"


class QualityFlag(StrEnum):
    """Flagi jakości snapshotu — SPEC §1.4.

    Rekordu z flagą **nigdy nie usuwamy**, tylko oznaczamy.
    """

    ZERO_BID = "zero_bid"
    """bid lub ask równe zero — mid policzony z lastPrice."""
    WIDE_SPREAD = "wide_spread"
    """(ask - bid) / mid > 0.25."""
    STALE_QUOTE = "stale_quote"
    """Kwotowanie nieodświeżane — np. skan poza godzinami sesji."""
    LOW_OI = "low_oi"
    """Open interest na ATM poniżej progu z filtrów uniwersum."""
    DTE_GT_2 = "dte_gt_2"
    """Wygaśnięcie dalej niż 2 dni — mnożnik 0.85 zawyża EM, patrz METHODOLOGY §4."""


def _normalize_ticker(value: str) -> str:
    return value.strip().upper()


class RawEarningsRecord(BaseModel):
    """Rekord kalendarza z **jednego** źródła, przed scaleniem.

    Celowo bez `session_date` — ta wynika dopiero ze scalonego timingu.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    """Nazwa źródła, np. "nasdaq", "finnhub"."""
    ticker: str
    event_date: date
    timing: Timing
    company_name: str | None = None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    market_cap: float | None = None
    num_estimates: int | None = None

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, value: str) -> str:
        return _normalize_ticker(value)

    @property
    def eps_actual_present(self) -> bool:
        """Czy źródło zna już opublikowany EPS — czyli publikacja nastąpiła."""
        return self.eps_actual is not None


class EarningsEvent(BaseModel):
    """Zdarzenie po scaleniu wszystkich źródeł — tabela `earnings_events`."""

    ticker: str
    event_date: date
    timing: Timing
    timing_confidence: TimingConfidence
    session_date: date | None = None
    """Pierwsza sesja konsumująca wynik. None dla UNKNOWN i DMH."""
    company_name: str | None = None
    eps_actual_present: bool = False
    sources: dict[str, RawEarningsRecord] = Field(default_factory=dict)
    """Surowe rekordy per źródło — trafiają do kolumny `sources_json`."""
    fetched_at: datetime

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, value: str) -> str:
        return _normalize_ticker(value)

    @property
    def is_scannable(self) -> bool:
        """Czy da się dla tego zdarzenia policzyć EM.

        Wymaga jednoznacznej pory publikacji. DMH i UNKNOWN odpadają — rekord
        zostaje w bazie, ale nie wchodzi do skanu.
        """
        return self.timing in (Timing.BMO, Timing.AMC) and self.session_date is not None


class EmSnapshot(BaseModel):
    """Snapshot łańcucha opcji i policzony expected move — tabela `em_snapshots`.

    Wypełniany w kroku 4-5. Trzy warianty EM liczone równolegle, patrz METHODOLOGY §4.
    """

    event_id: int
    snapshot_at: datetime
    spot: float
    expiry: date
    dte: int
    atm_strike: float

    call_bid: float | None = None
    call_ask: float | None = None
    put_bid: float | None = None
    put_ask: float | None = None
    call_mid: float | None = None
    put_mid: float | None = None

    straddle: float | None = None
    em_abs: float | None = None
    em_pct: float | None = None
    """Metoda A: 0.85 * straddle."""
    em_abs_weighted: float | None = None
    em_pct_weighted: float | None = None
    """Metoda B: 0.60*straddle + 0.30*strangle_1 + 0.10*strangle_2."""
    em_pct_iv: float | None = None
    """Metoda C: spot * iv_atm * sqrt(dte/365)."""

    iv_atm: float | None = None
    oi_atm: int | None = None
    volume_atm: int | None = None
    rel_spread: float | None = None
    quality_flags: list[QualityFlag] = Field(default_factory=list)


class Outcome(BaseModel):
    """Rozliczenie zdarzenia — tabela `outcomes`. Wypełniane w kroku 6."""

    event_id: int
    baseline_close: float
    next_open: float
    next_close: float
    gap_pct: float
    close_pct: float
    intraday_pct: float
    direction: Direction
    abs_move_pct: float
    em_ratio: float | None = None
    """abs_move_pct / em_pct. None, gdy brak snapshotu EM."""
    vrp: float | None = None
    """em_pct - abs_move_pct. Dodatni = opcje były drogie."""
    exceeded_em: bool | None = None
    settled_at: datetime

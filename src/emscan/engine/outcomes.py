"""Rozliczenie zdarzenia — SPEC §1.6, docs/METHODOLOGY.md §5-6.

`scan` zapisuje, ile ruchu wycenił rynek. `settle` zapisuje, ile ruchu faktycznie było.
Dopiero para tych liczb — `em_ratio` i `vrp` — jest tym, po co ten projekt istnieje.

Rozliczamy zdarzenia, **które mają snapshot EM**. Ruch samych cen dla całego uniwersum
zbierze `backfill` (krok 8) i to on jest zbiorem treningowym fazy 2 (SPEC §2.1); `settle`
domyka pętlę EM -> realizacja, więc pytanie o ceny idzie tylko o te tickery, dla których
EM w ogóle policzyliśmy. Kaskada ze SPEC §B.2 obowiązuje i tutaj.

**Polityka korekt o splity — zweryfikowana 2026-08-18, nie założona.** Nasdaq zwraca ceny
**skorygowane retroaktywnie**: w szeregach LRCX, ORLY, IBKR, ANET i PANW, które w oknie
dwóch lat miały splity od 2:1 do 15:1, nie ma ani jednego skoku dzień-do-dnia powyżej 30%,
a poziom cen sprzed splitu jest dokładnie podzielony przez mnożnik. Konsekwencje dla
rozliczenia są dwie i obie są wpisane w ten moduł:

1. `baseline_close` i świeca sesji **muszą pochodzić z jednego zapytania**. Dwa zapytania
   rozdzielone splitem dałyby dwie różne skale i ruch zafałszowany o mnożnik splitu.
2. Wszystko, co porównujemy między `scan` a `settle`, jest **ułamkiem, nie ceną**. `em_pct`
   i `abs_move_pct` są niezmienne przy zmianie skali, więc `em_ratio` pozostaje poprawny
   nawet wtedy, gdy split wypadł między snapshotem a rozliczeniem.

Czego ta weryfikacja **nie** obejmuje: korekt o dywidendy. Nie dało się ich rozdzielić tą
metodą, bo dywidenda to ułamek procenta, a nie skok. Ponieważ obie ceny biorą się z jednego
zapytania, ewentualny artefakt dotyczy tylko dnia ex-dividend i jest rzędu 0,5% — zapisane
w METHODOLOGY §6 jako znane ograniczenie, nie zamiecione pod dywan.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from sqlite3 import Connection

from emscan.db import insert_outcome, latest_snapshots_for_session
from emscan.engine.events import baseline_date_for
from emscan.engine.scan import target_session
from emscan.log import get_logger
from emscan.models import Direction, EarningsEvent, Outcome
from emscan.sources.base import DailyBar, PriceSource, SourceError
from emscan.trading_calendar import ET, is_trading_day, previous_trading_day

log = get_logger(__name__)

PRICE_BUFFER_DAYS = 10
"""Zapas przed sesją bazową — pokrywa długi weekend i święto."""

SUSPICIOUS_MOVE = 0.5
"""Powyżej tego ruchu logujemy ostrzeżenie: to albo prawda, albo artefakt danych."""


class MissingOutcome(StrEnum):
    """Dlaczego zdarzenie nie zostało rozliczone. Wiersz wtedy **nie powstaje** — SPEC §1.6."""

    NO_PRICE_HISTORY = "no_price_history"
    """Źródło nie zwróciło żadnej świecy dla tego tickera."""
    NO_BASELINE_BAR = "no_baseline_bar"
    """Brak sesji odniesienia — zawieszenie notowań przed publikacją."""
    NO_SESSION_BAR = "no_session_bar"
    """Brak sesji rozliczeniowej: jeszcze się nie zamknęła albo notowania stały."""
    SOURCE_ERROR = "source_error"
    """Źródło cen zawiodło dla tego tickera. Rozliczenie idzie dalej."""


@dataclass(frozen=True)
class SettleRow:
    """Jedno zdarzenie po próbie rozliczenia."""

    event: EarningsEvent
    outcome: Outcome | None = None
    missing: MissingOutcome | None = None
    detail: str | None = None

    @property
    def ticker(self) -> str:
        return self.event.ticker

    @property
    def settled(self) -> bool:
        return self.outcome is not None


@dataclass(frozen=True)
class SettleResult:
    """Wynik jednego przebiegu `settle`."""

    scan_date: date
    session_date: date
    baseline_date: date
    settled_at: datetime
    rows: tuple[SettleRow, ...] = ()

    @property
    def settled(self) -> tuple[SettleRow, ...]:
        return tuple(row for row in self.rows if row.settled)

    @property
    def missing(self) -> dict[MissingOutcome, int]:
        """Liczba pominięć w rozbiciu na powody, malejąco."""
        counts: dict[MissingOutcome, int] = {}
        for row in self.rows:
            if row.missing is not None:
                counts[row.missing] = counts.get(row.missing, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], str(item[0]))))

    @property
    def exceeded_em(self) -> int:
        """Ile ruchów przebiło EM. Rynek zwykle przeszacowuje — patrz README §Ograniczenia."""
        return sum(1 for row in self.settled if row.outcome and row.outcome.exceeded_em)


def last_closed_session(moment: datetime) -> date:
    """Ostatnia sesja, która się już zamknęła w chwili `moment`.

    Dzień handlowy jest tą sesją — `settle` uruchamiany o 17:00 ET rozlicza sesję zamkniętą
    godzinę wcześniej. W weekend i w święto cofamy się do poprzedniej sesji.

    Raises:
        ValueError: moment bez strefy czasowej.
    """
    if moment.tzinfo is None:
        raise ValueError("last_closed_session wymaga znacznika ze strefą — patrz SPEC §1.7")
    day = moment.astimezone(ET).date()
    return day if is_trading_day(day) else previous_trading_day(day)


def default_settle_scan_date(moment: datetime) -> date:
    """Dzień skanu, którego rozliczenie ma sens o tej porze — domyślna wartość `settle --date`.

    Cron rozliczenia (SPEC §1.8: `0 21 * * 2-6`) chodzi **dzień po** skanie, więc „dziś" byłoby
    złą domyślną datą: wskazywałoby sesję, która jeszcze się nie odbyła. Właściwy dzień skanu to
    poprzednik ostatniej zamkniętej sesji, liczony po kalendarzu giełdowym.

    Dzięki temu arytmetyka dat nie wchodzi do YAML-a: sobotni cron sam trafia w skan z czwartku
    (sesja piątkowa), a poniedziałkowe święto nie przesuwa niczego o jeden dzień w bok.
    """
    return previous_trading_day(last_closed_session(moment))


def direction_of(close_pct: float) -> Direction:
    """Kierunek ruchu close-to-close.

    SPEC §1.6 podaje „UP jeśli close_pct > 0 else DOWN", ale enum ma też FLAT i to jest
    właściwsze dla ruchu dokładnie zerowego: nazwanie zera spadkiem byłoby zmyśleniem
    kierunku, którego nie było.
    """
    if close_pct > 0:
        return Direction.UP
    if close_pct < 0:
        return Direction.DOWN
    return Direction.FLAT


def compute_outcome(
    *,
    event_id: int,
    baseline_close: float,
    session_bar: DailyBar,
    em_pct: float | None,
    settled_at: datetime,
) -> Outcome:
    """Rozliczenie jednego zdarzenia — wzory ze SPEC §1.6.

    Args:
        baseline_close: zamknięcie ostatniej sesji **przed** publikacją.
        session_bar: świeca pierwszej sesji konsumującej wynik.
        em_pct: EM ze snapshotu. None albo zero oznacza brak pomiaru, więc `em_ratio`,
            `vrp` i `exceeded_em` zostają None — nie zerem i nie nieskończonością.

    Raises:
        ValueError: `baseline_close` niedodatnie. Dzielenie przez zero albo cenę ujemną
            dałoby liczbę bez znaczenia, a SPEC zabrania cichych wartości zastępczych.
    """
    if baseline_close <= 0:
        raise ValueError(f"baseline_close musi być dodatnie, jest {baseline_close}")

    gap_pct = session_bar.open / baseline_close - 1
    close_pct = session_bar.close / baseline_close - 1
    intraday_pct = session_bar.close / session_bar.open - 1 if session_bar.open > 0 else 0.0
    abs_move_pct = abs(close_pct)

    measured = em_pct is not None and em_pct > 0
    return Outcome(
        event_id=event_id,
        baseline_close=baseline_close,
        next_open=session_bar.open,
        next_close=session_bar.close,
        gap_pct=gap_pct,
        close_pct=close_pct,
        intraday_pct=intraday_pct,
        direction=direction_of(close_pct),
        abs_move_pct=abs_move_pct,
        em_ratio=abs_move_pct / em_pct if measured and em_pct else None,
        vrp=em_pct - abs_move_pct if measured and em_pct else None,
        exceeded_em=abs_move_pct > em_pct if measured and em_pct else None,
        settled_at=settled_at,
    )


def _bar_on(bars: Sequence[DailyBar], day: date) -> DailyBar | None:
    for bar in bars:
        if bar.day == day:
            return bar
    return None


def run_settle(
    *,
    scan_date: date,
    conn: Connection,
    prices: PriceSource,
    settled_at: datetime,
) -> SettleResult:
    """Rozlicza sesję następującą po `scan_date` — SPEC §1.6.

    Zdarzenie bez kompletnych cen **nie dostaje wiersza**: brak rozliczenia to stan
    `NO_DATA` ze SPEC §1.6, a tabela `outcomes` nie ma kolumny na powód, więc nieobecność
    wiersza jest tym powodem. Powód nazwany wprost trafia do wyniku i do logu.

    Ponowne uruchomienie nadpisuje rozliczenie (`UNIQUE(event_id)`): pierwszy przebieg mógł
    trafić w moment, w którym sesja jeszcze się nie zamknęła.
    """
    session_date = target_session(scan_date)
    baseline_date = baseline_date_for(session_date)
    pairs = latest_snapshots_for_session(conn, session_date)

    rows: list[SettleRow] = []
    for event, snapshot in pairs:
        ticker = event.ticker
        try:
            bars = prices.daily_bars(
                ticker, baseline_date - timedelta(days=PRICE_BUFFER_DAYS), session_date
            )
        except SourceError as exc:
            log.warning("źródło cen zawiodło", ticker=ticker, error=str(exc))
            rows.append(
                SettleRow(event=event, missing=MissingOutcome.SOURCE_ERROR, detail=str(exc))
            )
            continue

        if not bars:
            rows.append(SettleRow(event=event, missing=MissingOutcome.NO_PRICE_HISTORY))
            continue

        baseline_bar = _bar_on(bars, baseline_date)
        if baseline_bar is None:
            log.warning(
                "brak sesji odniesienia", ticker=ticker, baseline_date=baseline_date.isoformat()
            )
            rows.append(
                SettleRow(
                    event=event,
                    missing=MissingOutcome.NO_BASELINE_BAR,
                    detail=f"brak świecy {baseline_date.isoformat()}",
                )
            )
            continue

        session_bar = _bar_on(bars, session_date)
        if session_bar is None:
            rows.append(
                SettleRow(
                    event=event,
                    missing=MissingOutcome.NO_SESSION_BAR,
                    detail=f"brak świecy {session_date.isoformat()}",
                )
            )
            continue

        outcome = compute_outcome(
            event_id=snapshot.event_id,
            baseline_close=baseline_bar.close,
            session_bar=session_bar,
            em_pct=snapshot.em_pct,
            settled_at=settled_at,
        )
        insert_outcome(conn, outcome)

        if abs(outcome.close_pct) > SUSPICIOUS_MOVE:
            # Nasdaq koryguje ceny o splity (patrz docstring modułu), więc taki ruch jest
            # najpewniej prawdziwy — ale wart obejrzenia, zanim wejdzie do cech fazy 2.
            log.warning(
                "ruch powyżej progu podejrzliwości",
                ticker=ticker,
                close_pct=round(outcome.close_pct, 4),
                baseline_close=outcome.baseline_close,
                next_close=outcome.next_close,
            )

        rows.append(SettleRow(event=event, outcome=outcome))

    result = SettleResult(
        scan_date=scan_date,
        session_date=session_date,
        baseline_date=baseline_date,
        settled_at=settled_at,
        rows=tuple(rows),
    )
    log.info(
        "rozliczenie zakończone",
        scan_date=scan_date.isoformat(),
        session_date=session_date.isoformat(),
        baseline_date=baseline_date.isoformat(),
        candidates=len(result.rows),
        settled=len(result.settled),
        exceeded_em=result.exceeded_em,
        missing={str(reason): count for reason, count in result.missing.items()},
    )
    return result

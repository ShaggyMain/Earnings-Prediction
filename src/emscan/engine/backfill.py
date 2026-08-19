"""Backfill kalendarza wyników — SPEC §1.7, zbiór wyjściowy dla fazy 2 (SPEC §2.1).

Pobiera kalendarz dzień po dniu wstecz i zapisuje zdarzenia. **Nie** liczy EM: historyczne
ceny opcji są płatne, więc historyczny EM buduje się wyłącznie do przodu, przez `scan`.

## Co diagnostyka 2026-08-18 ustaliła o danych historycznych

Sprawdzone na siedmiu dniach rozrzuconych po dwóch latach, bo SPEC zakładał dostępność
tych danych, a nikt tego nie zweryfikował:

| Co | Wynik |
|---|---|
| Zasięg kalendarza | 2 lata wstecz działa: 296 rekordów dla 2024-08-14, 399 dla 2024-11-06 |
| Gęstość rok do roku | porównywalna (2024-11-06: 399, 2025-11-05: 417) |
| Kapitalizacja i prognoza EPS | obecne, użyteczne jako cechy fazy 2 |
| **`timing` (BMO/AMC)** | **pusty w 100%** — Nasdaq retroaktywnie kasuje flagę pory |
| `eps_actual` | nigdy nieobecny, także dla dat bieżących |

Ostatnie dwa wiersze są istotne: **bez pory publikacji nie da się jednoznacznie wskazać sesji
rozliczeniowej**, bo BMO z dnia D konsumuje sesja D, a AMC z dnia D sesja D+1. Dlatego ten
moduł zapisuje sam kalendarz, a rozliczeń historycznych **nie liczy** — wymagałoby to albo
pory publikacji z innego źródła (Finnhub, do sprawdzenia), albo przyjęcia okna dwusesyjnego.
Wybór między tymi wariantami zmienia definicję targetu fazy 2, więc nie jest decyzją
implementacyjną. Szczegóły w docs/PLAN-faza-1.md.

Czego ten moduł **nie robi celowo**: nie zgaduje pory publikacji z zachowania cen. Sesja
wybrana jako „ta z większym ruchem" zawyżyłaby rozkład targetu, bo target to właśnie ruch —
to jest wnioskowanie z tej samej zmiennej, którą model ma przewidywać.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from sqlite3 import Connection

from emscan.db import has_events_on, mark_date_conflicts, upsert_events
from emscan.engine.events import merge_records
from emscan.log import get_logger
from emscan.models import RawEarningsRecord
from emscan.sources.base import EarningsCalendarSource, SourceError, SourceUnavailable
from emscan.trading_calendar import is_trading_day

log = get_logger(__name__)

DEFAULT_MIN_INTERVAL = 0.3
"""Odstęp między zapytaniami zalecany dla backfillu, w sekundach.

Kilkaset zapytań pod rząd do darmowego, publicznego endpointu wymaga odstępu — inaczej
prosimy się o throttling i o to, żeby nas odcięto.
"""


@dataclass(frozen=True)
class BackfillResult:
    """Wynik jednego przebiegu backfillu."""

    start: date
    end: date
    days_considered: int = 0
    days_fetched: int = 0
    days_skipped: int = 0
    """Dni pominięte, bo były już w bazie (wznawianie)."""
    events_written: int = 0
    conflicts_marked: int = 0
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """Czy przebieg objął cały zakres bez ani jednej awarii dnia."""
        return not self.failures


def trading_days(start: date, end: date) -> list[date]:
    """Sesje giełdowe w zakresie włącznie, rosnąco.

    Backfill pyta tylko o dni sesyjne. Publikacja w weekend albo w święto jest u spółek
    amerykańskich praktycznie niespotykana, a różnica to 730 zapytań zamiast 500.
    """
    days: list[date] = []
    day = start
    while day <= end:
        if is_trading_day(day):
            days.append(day)
        day += timedelta(days=1)
    return days


def run_backfill(
    *,
    start: date,
    end: date,
    conn: Connection,
    calendars: Sequence[EarningsCalendarSource],
    fetched_at: datetime,
    resume: bool = True,
    on_day: Callable[[date, int, int], None] | None = None,
) -> BackfillResult:
    """Pobiera kalendarz dla całego zakresu i zapisuje zdarzenia.

    Args:
        resume: pomija dni, które są już w bazie. Backfill dwóch lat to kilkaset zapytań
            i kilka minut pracy, więc przerwany run musi dać się dokończyć bez powtarzania
            wszystkiego od początku.
        on_day: wołane po każdym dniu jako (dzień, numer, ile_wszystkich) — pod pasek postępu.

    Zdarzenia scalamy **w obrębie dnia**, tak jak robi to skan. Duplikaty rozjeżdżające się
    o jeden dzień oznacza dopiero `mark_date_conflicts` na końcu, bo para może leżeć w dwóch
    różnych dniach, a przy wznawianiu nawet w dwóch różnych runach.

    Raises:
        SourceUnavailable: ani jeden dzień nie został pobrany, a awarie wystąpiły. Pusty
            zakres bez awarii nie jest błędem — dwa dni świąteczne pod rząd też są zakresem.
    """
    days = trading_days(start, end)
    failures: list[str] = []
    fetched = skipped = written = 0

    for index, day in enumerate(days, start=1):
        if resume and has_events_on(conn, day):
            skipped += 1
            if on_day is not None:
                on_day(day, index, len(days))
            continue

        records: list[RawEarningsRecord] = []
        day_failed = False
        for source in calendars:
            try:
                records.extend(source.fetch_day(day))
            except SourceError as exc:
                day_failed = True
                failures.append(f"{source.name} {day.isoformat()}: {exc}")
                log.warning(
                    "źródło kalendarza zawiodło",
                    source=source.name,
                    day=day.isoformat(),
                    error=str(exc),
                )

        if records:
            events = merge_records(records, fetched_at=fetched_at)
            written += upsert_events(conn, events)
            fetched += 1
        elif not day_failed:
            # Dzień bez publikacji to poprawny wynik, nie awaria.
            fetched += 1

        if on_day is not None:
            on_day(day, index, len(days))

    if fetched == 0 and failures:
        raise SourceUnavailable(
            f"backfill {start.isoformat()}..{end.isoformat()}: nie pobrano ani jednego dnia "
            f"({len(failures)} awarii, pierwsza: {failures[0]})"
        )

    conflicts = mark_date_conflicts(conn)
    result = BackfillResult(
        start=start,
        end=end,
        days_considered=len(days),
        days_fetched=fetched,
        days_skipped=skipped,
        events_written=written,
        conflicts_marked=conflicts,
        failures=tuple(failures),
    )
    log.info(
        "backfill zakończony",
        start=start.isoformat(),
        end=end.isoformat(),
        days_considered=result.days_considered,
        days_fetched=result.days_fetched,
        days_skipped=result.days_skipped,
        events_written=result.events_written,
        conflicts_marked=result.conflicts_marked,
        failures=len(result.failures),
    )
    return result

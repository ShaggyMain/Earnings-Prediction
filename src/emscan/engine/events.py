"""Scalanie kalendarzy z wielu źródeł — SPEC §1.1, §1.2.

Odpowiada za trzy rzeczy: ustalenie pory publikacji na podstawie kilku źródeł wraz
z oceną pewności, wyliczenie `session_date` po kalendarzu giełdowym oraz wykrycie
przypadków, w których źródła nie zgadzają się co do samej daty publikacji.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from itertools import pairwise

from emscan.log import get_logger
from emscan.models import EarningsEvent, RawEarningsRecord, Timing, TimingConfidence
from emscan.trading_calendar import (
    next_trading_day,
    previous_trading_day,
    trading_day_on_or_after,
)

log = get_logger(__name__)


def resolve_timing(timings: dict[str, Timing]) -> tuple[Timing, TimingConfidence]:
    """Ustala porę publikacji z odczytów kilku źródeł.

    Args:
        timings: mapa nazwa źródła -> odczytany timing.

    Returns:
        Para (timing, pewność) wg reguł uzgodnionych 2026-08-13:

        - żadne źródło nie zna pory -> (UNKNOWN, UNKNOWN)
        - jedno źródło zna porę -> (ta pora, MEDIUM)
        - co najmniej dwa źródła zgodne -> (ta pora, HIGH)
        - źródła sprzeczne -> (UNKNOWN, LOW)

        Przy konflikcie celowo **nie** wybieramy zwycięzcy. SPEC §1.2 zakazuje
        zgadywania, a odczyty obu źródeł zostają w `sources_json`, więc nic nie ginie.
    """
    known = {source: timing for source, timing in timings.items() if timing != Timing.UNKNOWN}
    if not known:
        return Timing.UNKNOWN, TimingConfidence.UNKNOWN

    distinct = set(known.values())
    if len(distinct) > 1:
        return Timing.UNKNOWN, TimingConfidence.LOW

    resolved = distinct.pop()
    confidence = TimingConfidence.HIGH if len(known) >= 2 else TimingConfidence.MEDIUM
    return resolved, confidence


def session_date_for(event_date: date, timing: Timing) -> date | None:
    """Pierwsza sesja konsumująca wynik — SPEC §1.1.

    BMO z dnia D konsumuje sesja D, AMC z dnia D konsumuje sesja D+1, przy czym „D+1"
    liczy się po kalendarzu giełdowym: AMC z piątku rozlicza się w poniedziałek.

    Zwraca None dla DMH i UNKNOWN. Przy publikacji w trakcie sesji część ruchu dzieje
    się przed komunikatem, więc ani `session_date`, ani `baseline_close` nie są
    jednoznaczne i EM dla takiego zdarzenia nie ma sensu.
    """
    if timing == Timing.BMO:
        # Zwykle event_date jest sesją; gdy źródło poda weekend, bierzemy najbliższą sesję.
        return trading_day_on_or_after(event_date)
    if timing == Timing.AMC:
        return next_trading_day(event_date)
    return None


def baseline_date_for(session_date: date) -> date:
    """Sesja, której zamknięcie jest punktem odniesienia (`baseline_close`).

    Jedna reguła obsługuje oba przypadki: dla AMC z D sesją rozliczeniową jest D+1,
    a jej poprzedniczką D; dla BMO z D sesją jest D, a jej poprzedniczką D-1.
    """
    return previous_trading_day(session_date)


def merge_records(
    records: Iterable[RawEarningsRecord],
    *,
    fetched_at: datetime,
) -> list[EarningsEvent]:
    """Scala rekordy ze wszystkich źródeł w zdarzenia, po kluczu (ticker, event_date).

    Rekordy o niskiej jakości nie są odrzucane — zdarzenie z timingiem UNKNOWN trafia
    do bazy tak samo jak każde inne, tylko z odpowiednią pewnością (SPEC §1.4).
    """
    grouped: dict[tuple[str, date], dict[str, RawEarningsRecord]] = defaultdict(dict)
    for record in records:
        grouped[(record.ticker, record.event_date)][record.source] = record

    events: list[EarningsEvent] = []
    for (ticker, event_date), sources in sorted(grouped.items()):
        timing, confidence = resolve_timing({name: rec.timing for name, rec in sources.items()})
        session_date = session_date_for(event_date, timing)

        if confidence == TimingConfidence.LOW:
            log.warning(
                "źródła podają sprzeczną porę publikacji",
                ticker=ticker,
                event_date=event_date.isoformat(),
                readings={name: str(rec.timing) for name, rec in sources.items()},
                eps_actual_present=any(rec.eps_actual_present for rec in sources.values()),
            )

        events.append(
            EarningsEvent(
                ticker=ticker,
                event_date=event_date,
                timing=timing,
                timing_confidence=confidence,
                session_date=session_date,
                company_name=_first_company_name(sources.values()),
                eps_actual_present=any(rec.eps_actual_present for rec in sources.values()),
                sources=dict(sources),
                fetched_at=fetched_at,
            )
        )
    return events


def _first_company_name(records: Iterable[RawEarningsRecord]) -> str | None:
    """Pierwsza niepusta nazwa spółki. Finnhub jej nie podaje, Nasdaq tak."""
    for record in records:
        if record.company_name:
            return record.company_name
    return None


def find_adjacent_date_conflicts(
    events: Iterable[EarningsEvent],
    *,
    max_gap_days: int = 1,
) -> list[tuple[str, date, date]]:
    """Wykrywa ten sam ticker z publikacją w sąsiednich dniach — prawdopodobny duplikat.

    Źródła bywają niezgodne co do **daty**, nie tylko pory: 2026-08-13 Nasdaq umieścił
    ACTU, AIRE, FSI, SOWG i VNRX 13.08, a Finnhub 14.08. Po scaleniu po (ticker,
    event_date) powstają z tego dwa zdarzenia opisujące jedną publikację.

    Ta funkcja tylko **raportuje** takie pary. Polityka ich łączenia nie jest tu
    przesądzona, bo nie da się rozstrzygnąć danymi, która data jest prawdziwa —
    patrz docs/PLAN-faza-1.md, kwestia otwarta.

    Returns:
        Lista (ticker, wcześniejsza data, późniejsza data), posortowana.
    """
    by_ticker: dict[str, list[date]] = defaultdict(list)
    for event in events:
        by_ticker[event.ticker].append(event.event_date)

    conflicts: list[tuple[str, date, date]] = []
    for ticker, dates in by_ticker.items():
        ordered = sorted(dates)
        for earlier, later in pairwise(ordered):
            if 0 < (later - earlier).days <= max_gap_days:
                conflicts.append((ticker, earlier, later))
    return sorted(conflicts)

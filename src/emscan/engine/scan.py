"""Przepływ skanu — SPEC §1.1, §1.5, kaskada §B.2.

Skan w dniu **D** obejmuje dwie grupy naraz: AMC z dnia D i BMO z pierwszej sesji po D.
Obie mają tę samą sesję rozliczeniową i ten sam `baseline_close`, więc dla silnika są
jednym workiem (SPEC §1, „Kluczowa obserwacja o grupowaniu"). Dlatego kalendarz pobieramy
dla **dwóch dni**, a wybieramy z niego zdarzenia po `session_date`, nie po dacie publikacji:
BMO z samego dnia D rozliczyło się już rano i nie jest skanowalne.

Ten moduł jest orkiestracją, nie logiką: wybór wygaśnięcia i rachunek EM siedzą
w `expected_move`, progi w `universe`, a zapis w `db`. Tutaj zapada tylko kolejność
i decyzja, co zrobić z tickerem, który się wywrócił.

**Awaria jednego tickera nie kończy skanu.** Zdarzenie dostaje powód odrzucenia
(`SOURCE_ERROR`) plus wpis w logu i skan idzie dalej — dla zadania wsadowego to nie jest
ciche tłumienie błędu, bo powód wchodzi do wyniku i do podsumowania. Wyjątek podnosimy
dopiero wtedy, gdy **żadne** źródło kalendarza nie odpowiedziało: skan bez kalendarza nie
ma czego liczyć.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from sqlite3 import Connection

from emscan.db import event_id_for, insert_snapshot, upsert_bars, upsert_events
from emscan.engine.events import merge_records
from emscan.engine.expected_move import (
    ExpectedMoveError,
    NoUsableExpiry,
    compute_expected_move,
    select_expiry,
)
from emscan.engine.universe import (
    RejectReason,
    UniverseFilters,
    check_snapshot,
    check_spot,
    check_volume_20d,
    prescreen_session_volume,
)
from emscan.log import get_logger
from emscan.models import EarningsEvent, EmSnapshot, RawEarningsRecord
from emscan.sources.base import (
    EarningsCalendarSource,
    OptionsChainSource,
    PriceSource,
    SourceError,
    SourceUnavailable,
    SymbolNotCovered,
)
from emscan.trading_calendar import is_in_session, next_trading_day

log = get_logger(__name__)

PRICE_LOOKBACK_DAYS = 45
"""Ile dni kalendarzowych wstecz pytamy o świece, żeby mieć pewne 20 sesji.

20 sesji to około 28 dni kalendarzowych; zapas pokrywa święta i długie weekendy.
"""


@dataclass(frozen=True)
class ScanRow:
    """Jeden ticker rozpatrzony w skanie — razem z powodem, jeśli wypadł."""

    event: EarningsEvent
    snapshot: EmSnapshot | None = None
    volume_20d: float | None = None
    rejected: RejectReason | None = None
    detail: str | None = None
    """Szczegół powodu — treść wyjątku albo liczba, która zdecydowała."""

    @property
    def ticker(self) -> str:
        return self.event.ticker

    @property
    def selected(self) -> bool:
        """Czy ticker przeszedł wszystkie filtry i wchodzi do raportu."""
        return self.rejected is None


@dataclass(frozen=True)
class ScanResult:
    """Wynik jednego skanu. Zawiera **wszystkie** rozpatrzone tickery, nie tylko wybrane."""

    scan_date: date
    session_date: date
    snapshot_at: datetime
    rows: tuple[ScanRow, ...] = ()
    events_seen: int = 0
    """Ile zdarzeń wróciło z kalendarza dla obu dni — także tych z innej sesji."""
    snapshots_written: int = 0
    price_lookups: int = 0
    """Ile razy pytaliśmy Nasdaqa o historię cen. Miara skuteczności kaskady."""
    calendar_failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def selected(self) -> tuple[ScanRow, ...]:
        """Wybrane tickery, malejąco po EM."""
        return tuple(
            sorted(
                (row for row in self.rows if row.selected),
                key=lambda row: (row.snapshot.em_pct or 0.0) if row.snapshot else 0.0,
                reverse=True,
            )
        )

    @property
    def rejections(self) -> dict[RejectReason, int]:
        """Liczba odrzuceń w rozbiciu na powody, malejąco."""
        counts: dict[RejectReason, int] = {}
        for row in self.rows:
            if row.rejected is not None:
                counts[row.rejected] = counts.get(row.rejected, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], str(item[0]))))


def target_session(scan_date: date) -> date:
    """Sesja rozliczeniowa grupy skanowanej w dniu `scan_date`.

    Zawsze pierwsza sesja **po** dniu skanu: AMC z D i BMO z tej sesji konsumują się
    właśnie wtedy. Piątkowy skan celuje w poniedziałek, przedświąteczny — za święto.
    """
    return next_trading_day(scan_date)


def collect_events(
    calendars: Sequence[EarningsCalendarSource],
    *,
    scan_date: date,
    session_date: date,
    fetched_at: datetime,
) -> tuple[list[EarningsEvent], list[str]]:
    """Pobiera kalendarz obu dni ze wszystkich źródeł i scala go w zdarzenia.

    Returns:
        Para (zdarzenia, opisy awarii). Awaria jednego źródła obniża `timing_confidence`
        do MEDIUM, ale nie przerywa skanu — drugie źródło nadal wnosi porę publikacji.

    Raises:
        SourceUnavailable: nie odpowiedziało **żadne** źródło dla żadnego dnia.
    """
    records: list[RawEarningsRecord] = []
    failures: list[str] = []
    for source in calendars:
        for day in (scan_date, session_date):
            try:
                records.extend(source.fetch_day(day))
            except SourceError as exc:
                failures.append(f"{source.name} {day.isoformat()}: {exc}")
                log.warning(
                    "źródło kalendarza zawiodło",
                    source=source.name,
                    day=day.isoformat(),
                    error=str(exc),
                )

    if not records:
        raise SourceUnavailable(
            "żadne źródło kalendarza nie zwróciło danych dla "
            f"{scan_date.isoformat()} i {session_date.isoformat()}: {'; '.join(failures)}"
        )
    return merge_records(records, fetched_at=fetched_at), failures


def _scan_one(
    event: EarningsEvent,
    *,
    event_id: int,
    conn: Connection,
    options: OptionsChainSource,
    prices: PriceSource,
    filters: UniverseFilters,
    snapshot_at: datetime,
    scan_date: date,
) -> tuple[ScanRow, bool, bool]:
    """Kaskada dla jednego tickera.

    Returns:
        (wiersz, czy zapisano snapshot, czy pytaliśmy o historię cen).
    """
    ticker = event.ticker
    session_date = event.session_date
    assert session_date is not None  # gwarantuje wywołujący — patrz `run_scan`

    # --- etap 1: jedno zapytanie do dostawcy opcji ---
    try:
        expiries = options.expirations(ticker)
    except SymbolNotCovered:
        return ScanRow(event=event, rejected=RejectReason.NO_OPTIONS), False, False
    except SourceError as exc:
        log.warning("dostawca opcji zawiódł", ticker=ticker, error=str(exc))
        return (
            ScanRow(event=event, rejected=RejectReason.SOURCE_ERROR, detail=str(exc)),
            False,
            False,
        )

    try:
        expiry = select_expiry(expiries, session_date)
    except NoUsableExpiry as exc:
        return ScanRow(event=event, rejected=RejectReason.NO_EXPIRY, detail=str(exc)), False, False

    try:
        chain = options.chain(ticker, expiry)
        session_volume = options.underlying_volume(ticker)
        data_timestamp = options.data_timestamp(ticker)
    except SourceError as exc:
        log.warning("nie udało się pobrać łańcucha", ticker=ticker, error=str(exc))
        return (
            ScanRow(event=event, rejected=RejectReason.SOURCE_ERROR, detail=str(exc)),
            False,
            False,
        )

    reason = check_spot(chain.spot, filters)
    if reason is not None:
        return ScanRow(event=event, rejected=reason, detail=f"spot={chain.spot:.2f}"), False, False

    reason = prescreen_session_volume(session_volume, filters)
    if reason is not None:
        return (
            ScanRow(event=event, rejected=reason, detail=f"volume={session_volume}"),
            False,
            False,
        )

    # --- etap 2: rachunek EM, zero zapytań ---
    try:
        snapshot = compute_expected_move(
            chain,
            event_id=event_id,
            snapshot_at=snapshot_at,
            data_timestamp=data_timestamp,
            min_oi_atm=filters.min_oi_atm,
        )
    except ExpectedMoveError as exc:
        log.warning("EM niepoliczalny", ticker=ticker, error=str(exc))
        return ScanRow(event=event, rejected=RejectReason.NO_EM, detail=str(exc)), False, False

    # Snapshot zapisujemy zawsze, także gdy zaraz odpadnie na progu EM: to poprawny
    # pomiar, a faza 2 potrzebuje całego rozkładu, nie tylko ogona (SPEC §2.1).
    insert_snapshot(conn, snapshot)

    reason = check_snapshot(snapshot, filters)
    if reason is not None:
        detail = f"em_pct={snapshot.em_pct:.4f}" if snapshot.em_pct is not None else "em_pct=None"
        if reason == RejectReason.LOW_OI:
            detail = f"oi_atm={snapshot.oi_atm}"
        return ScanRow(event=event, snapshot=snapshot, rejected=reason, detail=detail), True, False

    # --- etap 3: prawdziwa średnia 20-sesyjna, jedno zapytanie na ocalałego ---
    try:
        bars = prices.daily_bars(ticker, scan_date - timedelta(days=PRICE_LOOKBACK_DAYS), scan_date)
    except SourceError as exc:
        log.warning("nie udało się pobrać historii cen", ticker=ticker, error=str(exc))
        return (
            ScanRow(
                event=event,
                snapshot=snapshot,
                rejected=RejectReason.SOURCE_ERROR,
                detail=str(exc),
            ),
            True,
            True,
        )

    # Świece i tak przeszły przez nasze ręce — zapisujemy je, żeby faza 2 nie musiała
    # odpytywać źródła o to samo tysiące razy (SPEC §2.3 potrzebuje realized vol i ADV).
    upsert_bars(conn, ticker, bars, fetched_at=snapshot_at)

    volume_20d, reason = check_volume_20d(bars, filters)
    detail = f"volume_20d={volume_20d:.0f}" if volume_20d is not None else "brak świec"
    return (
        ScanRow(
            event=event,
            snapshot=snapshot,
            volume_20d=volume_20d,
            rejected=reason,
            detail=detail if reason is not None else None,
        ),
        True,
        True,
    )


def run_scan(
    *,
    scan_date: date,
    conn: Connection,
    calendars: Sequence[EarningsCalendarSource],
    options: OptionsChainSource,
    prices: PriceSource,
    filters: UniverseFilters,
    snapshot_at: datetime,
) -> ScanResult:
    """Pełny skan dnia: kalendarz → filtry → EM → zapis.

    Zdarzenia trafiają do bazy **wszystkie**, także te niescanowalne (SPEC §1.4).
    Snapshot powstaje dla każdego policzonego EM. Filtry decydują tylko o tym, co
    wchodzi do raportu.

    Args:
        conn: połączenie z bazą. Dla `--dry-run` wystarczy `db.open_memory_db()` —
            przepływ jest wtedy identyczny, tylko nic nie zostaje na dysku.
        snapshot_at: moment skanu ze strefą; trafia do każdego snapshotu.

    Raises:
        SourceUnavailable: żadne źródło kalendarza nie odpowiedziało.
    """
    session_date = target_session(scan_date)
    if not is_in_session(snapshot_at):
        log.warning(
            "skan poza godzinami sesji — kwotowania będą nieodświeżane, "
            "spodziewaj się flag stale_quote i zero_bid",
            snapshot_at=snapshot_at.isoformat(),
        )

    events, failures = collect_events(
        calendars, scan_date=scan_date, session_date=session_date, fetched_at=snapshot_at
    )
    upsert_events(conn, events)

    rows: list[ScanRow] = []
    snapshots_written = 0
    price_lookups = 0

    for event in events:
        if event.session_date == session_date:
            event_id = event_id_for(conn, event.ticker, event.event_date)
            if event_id is None:  # pragma: no cover - naruszenie niezmiennika upsertu
                raise RuntimeError(f"zdarzenie {event.ticker} {event.event_date} nie ma id w bazie")
            row, written, looked_up = _scan_one(
                event,
                event_id=event_id,
                conn=conn,
                options=options,
                prices=prices,
                filters=filters,
                snapshot_at=snapshot_at,
                scan_date=scan_date,
            )
            rows.append(row)
            snapshots_written += int(written)
            price_lookups += int(looked_up)
        elif event.session_date is None and event.event_date in (scan_date, session_date):
            # DMH albo sprzeczne źródła: rekord jest w bazie, ale sesji dla niego nie ma.
            rows.append(ScanRow(event=event, rejected=RejectReason.NO_SESSION))

    result = ScanResult(
        scan_date=scan_date,
        session_date=session_date,
        snapshot_at=snapshot_at,
        rows=tuple(rows),
        events_seen=len(events),
        snapshots_written=snapshots_written,
        price_lookups=price_lookups,
        calendar_failures=tuple(failures),
    )
    log.info(
        "skan zakończony",
        scan_date=scan_date.isoformat(),
        session_date=session_date.isoformat(),
        events_seen=result.events_seen,
        considered=len(result.rows),
        selected=len(result.selected),
        snapshots_written=result.snapshots_written,
        price_lookups=result.price_lookups,
        rejections={str(reason): count for reason, count in result.rejections.items()},
    )
    return result

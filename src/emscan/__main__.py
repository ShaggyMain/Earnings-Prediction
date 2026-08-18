"""CLI — SPEC §1.7.

Zaimplementowane są `scan` i `report` (krok 5). `settle`, `stats` i `backfill` powstają
w krokach 6 i 8 — celowo nie ma dla nich atrap, bo komenda, która niczego nie robi, jest
gorsza od komendy, której nie ma.

**Znaczenie `--date` jest jedno we wszystkich komendach: to dzień skanu D.** Sesją, o którą
chodzi, jest pierwsza sesja po D — tam konsumują się zarówno AMC z dnia D, jak i BMO z tej
sesji (SPEC §1). Raport nazywa plik sesją, nie dniem skanu.

Wszystkie daty ISO, wszystkie momenty w strefie `America/New_York` (SPEC §1.7).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from emscan.config import Settings, get_settings
from emscan.db import latest_snapshots_for_session, open_db, open_memory_db
from emscan.engine.scan import ScanResult, run_scan, target_session
from emscan.engine.universe import UniverseFilters
from emscan.log import configure_logging, get_logger
from emscan.reporting.report import (
    ReportFormat,
    default_path,
    render,
    rows_from_snapshots,
    sort_rows,
)
from emscan.sources.base import EarningsCalendarSource, OptionsChainSource, PriceSource
from emscan.sources.cboe import CboeOptionsSource
from emscan.sources.finnhub import FinnhubCalendarSource
from emscan.sources.nasdaq import NasdaqCalendarSource
from emscan.sources.nasdaq_prices import NasdaqPriceSource

ET = ZoneInfo("America/New_York")

log = get_logger(__name__)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Earnings Expected Move Scanner — narzędzie badawcze, nie system tradingowy.",
)

DateOption = Annotated[
    str | None,
    typer.Option("--date", help="Dzień skanu w formacie ISO. Domyślnie dziś w strefie ET."),
]


def _parse_day(value: str | None) -> date:
    """Data ISO albo dzisiejsza data **nowojorska** — nigdy lokalna data maszyny."""
    if value is None:
        return datetime.now(tz=ET).date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"data musi być w formacie ISO (YYYY-MM-DD), jest: {value}"
        ) from exc


@contextmanager
def _live_sources(
    settings: Settings, *, raw_dir: Path
) -> Iterator[tuple[list[EarningsCalendarSource], OptionsChainSource, PriceSource]]:
    """Prawdziwe źródła, zamykane po wyjściu z bloku.

    Finnhub wchodzi tylko z kluczem. Bez niego skan nadal działa, ale porę publikacji zna
    jedno źródło, więc `timing_confidence` nie przekroczy MEDIUM (SPEC §1.2).
    """
    calendars: list[EarningsCalendarSource] = [
        NasdaqCalendarSource(
            user_agent=settings.user_agent,
            timeout=settings.http_timeout,
            max_retries=settings.http_max_retries,
            raw_dir=raw_dir,
        )
    ]
    if settings.finnhub_api_key:
        calendars.append(
            FinnhubCalendarSource(
                settings.finnhub_api_key,
                timeout=settings.http_timeout,
                max_retries=settings.http_max_retries,
                raw_dir=raw_dir,
            )
        )
    else:
        log.warning("brak klucza Finnhuba — pora publikacji tylko z Nasdaqa, pewność max MEDIUM")

    options = CboeOptionsSource(
        user_agent=settings.user_agent,
        timeout=settings.http_timeout,
        max_retries=settings.http_max_retries,
        raw_dir=raw_dir,
    )
    prices = NasdaqPriceSource(
        user_agent=settings.user_agent,
        timeout=settings.http_timeout,
        max_retries=settings.http_max_retries,
        raw_dir=raw_dir,
    )
    try:
        yield calendars, options, prices
    finally:
        # Adnotacja jest potrzebna: bez niej typ wspólny wypada na ABC, które close() nie zna.
        closables: tuple[EarningsCalendarSource | OptionsChainSource | PriceSource, ...] = (
            *calendars,
            options,
            prices,
        )
        for source in closables:
            source.close()


@contextmanager
def _database(settings: Settings, *, dry_run: bool) -> Iterator[sqlite3.Connection]:
    """Baza na dysku albo — przy `--dry-run` — w pamięci."""
    if dry_run:
        with open_memory_db() as conn:
            yield conn
        return
    with open_db(settings.resolved_db_path()) as conn:
        yield conn


def _echo_summary(result: ScanResult, *, top: int) -> None:
    """Zwięzłe podsumowanie na stdout. Pełna tabela idzie do raportu."""
    typer.echo(
        f"skan {result.scan_date.isoformat()} -> sesja {result.session_date.isoformat()} "
        f"({result.snapshot_at.strftime('%H:%M:%S %Z')})"
    )
    typer.echo(
        f"zdarzeń z kalendarza: {result.events_seen}, w tej sesji: {len(result.rows)}, "
        f"wybranych: {len(result.selected)}, snapshotów zapisanych: {result.snapshots_written}, "
        f"zapytań o historię cen: {result.price_lookups}"
    )
    for failure in result.calendar_failures:
        typer.echo(f"  uwaga: kalendarz zawiódł — {failure}")

    if result.rejections:
        powody = ", ".join(f"{reason}={count}" for reason, count in result.rejections.items())
        typer.echo(f"odrzucenia: {powody}")

    selected = result.selected[:top]
    if not selected:
        typer.echo("brak tickerów spełniających filtry")
        return

    typer.echo("")
    typer.echo(f"{'ticker':<8}{'timing':<8}{'spot':>9}{'expiry':>12}{'dte':>5}{'EM%':>8}  flagi")
    for row in selected:
        snapshot = row.snapshot
        if snapshot is None:  # pragma: no cover - wybrany wiersz zawsze ma snapshot
            continue
        em_pct = f"{snapshot.em_pct * 100:.2f}" if snapshot.em_pct is not None else "-"
        flags = ",".join(str(flag) for flag in snapshot.quality_flags)
        typer.echo(
            f"{row.ticker:<8}{row.event.timing!s:<8}{snapshot.spot:>9.2f}"
            f"{snapshot.expiry.isoformat():>12}{snapshot.dte:>5}{em_pct:>8}  {flags}"
        )


@app.callback()
def main(
    log_level: Annotated[
        str, typer.Option("--log-level", help="DEBUG/INFO/WARNING/ERROR")
    ] = "INFO",
    json_logs: Annotated[bool, typer.Option("--json-logs", help="Logi w JSON (dla CI)")] = False,
) -> None:
    """Konfiguruje logowanie przed każdą komendą."""
    configure_logging(log_level, json_output=json_logs)


@app.command()
def scan(
    day: DateOption = None,
    min_em: Annotated[
        float | None,
        typer.Option("--min-em", help="Minimalny EM w PROCENTACH, np. 6 (SPEC §1.7)"),
    ] = None,
    min_price: Annotated[float | None, typer.Option("--min-price")] = None,
    min_volume: Annotated[
        int | None, typer.Option("--min-volume", help="Średni wolumen 20d")
    ] = None,
    min_oi: Annotated[
        int | None,
        typer.Option("--min-oi", help="Minimalne OI na ATM. Progu 100 nie sięgają drogie spółki"),
    ] = None,
    top: Annotated[int, typer.Option("--top", help="Ile wierszy pokazać na stdout")] = 25,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Pełny przepływ, baza w pamięci — nic nie zostaje")
    ] = False,
) -> None:
    """Skan dnia: kalendarz obu grup, EM z łańcucha opcji, zapis snapshotów.

    Sensowne okno to 15:30 ET (SPEC §1.8). Uruchomiony poza sesją zwróci kwotowania
    z flagami `stale_quote` i `zero_bid` — to ograniczenie źródeł, nie błąd.
    """
    settings = get_settings()
    scan_date = _parse_day(day)

    filters = UniverseFilters.from_settings(settings)
    if min_em is not None:
        filters = replace(filters, min_em_pct=min_em / 100)
    if min_price is not None:
        filters = replace(filters, min_price=min_price)
    if min_volume is not None:
        filters = replace(filters, min_volume_20d=min_volume)
    if min_oi is not None:
        filters = replace(filters, min_oi_atm=min_oi)

    raw_dir = settings.resolved_raw_dir() / scan_date.isoformat()
    snapshot_at = datetime.now(tz=ET)

    with (
        _database(settings, dry_run=dry_run) as conn,
        _live_sources(settings, raw_dir=raw_dir) as (calendars, options, prices),
    ):
        result = run_scan(
            scan_date=scan_date,
            conn=conn,
            calendars=calendars,
            options=options,
            prices=prices,
            filters=filters,
            snapshot_at=snapshot_at,
        )
        _echo_summary(result, top=top)

    if dry_run:
        typer.echo("--dry-run: baza była w pamięci, nic nie zapisano")
    else:
        typer.echo(f"raport: python -m emscan report --date {scan_date.isoformat()}")


@app.command()
def report(
    day: DateOption = None,
    fmt: Annotated[ReportFormat, typer.Option("--format", help="md, csv albo html")] = (
        ReportFormat.MD
    ),
    out: Annotated[Path | None, typer.Option("--out", help="Ścieżka pliku wyjściowego")] = None,
    top: Annotated[int | None, typer.Option("--top", help="Tylko N najwyższych EM")] = None,
    min_em: Annotated[
        float | None,
        typer.Option("--min-em", help="Próg EM w PROCENTACH. 0 pokazuje wszystko, co w bazie"),
    ] = None,
) -> None:
    """Raport z zapisanych snapshotów. Nie rusza sieci — czyta wyłącznie bazę.

    Baza trzyma **każdy** policzony snapshot, także poniżej progu EM (SPEC §2.1), więc
    raport musi filtrować sam. Domyślnie stosuje próg z konfiguracji; `--min-em 0`
    pokazuje wszystko, co zmierzono.
    """
    settings = get_settings()
    scan_date = _parse_day(day)
    session_date = target_session(scan_date)
    threshold = settings.min_em_pct if min_em is None else min_em / 100

    with open_db(settings.resolved_db_path()) as conn:
        pairs = latest_snapshots_for_session(conn, session_date)

    rows = [
        row
        for row in rows_from_snapshots(pairs)
        if row.snapshot.em_pct is not None and row.snapshot.em_pct >= threshold
    ]
    if top is not None:
        rows = sort_rows(rows)[:top]

    notes: Sequence[str] = [
        f"Dzień skanu: {scan_date.isoformat()}",
        f"Próg EM: {threshold * 100:.2f}% (snapshotów w bazie dla tej sesji: {len(pairs)})",
    ]
    text = render(
        rows,
        session_date=session_date,
        generated_at=datetime.now(tz=ET),
        fmt=fmt,
        notes=notes,
    )

    path = out or default_path(settings.resolved_reports_dir(), session_date, fmt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    typer.echo(f"zdarzeń w raporcie: {len(rows)}")
    typer.echo(f"raport: {path}")


if __name__ == "__main__":
    app()

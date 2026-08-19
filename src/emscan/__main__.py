"""CLI — SPEC §1.7.

Zaimplementowane są `scan`, `settle`, `report`, `backfill` i `market`. `stats` powstaje wraz
z fazą 2 — celowo nie ma dla niej atrapy, bo komenda, która niczego nie robi, jest gorsza od
komendy, której nie ma.

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
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from emscan.config import Settings, get_settings
from emscan.db import (
    latest_snapshots_for_session,
    open_db,
    open_memory_db,
    outcomes_for_session,
)
from emscan.engine.backfill import (
    DEFAULT_MIN_INTERVAL,
    BackfillResult,
    run_backfill,
    trading_days,
)
from emscan.engine.market import MarketResult, run_market_update
from emscan.engine.outcomes import SettleResult, default_settle_scan_date, run_settle
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
from emscan.sources.nasdaq_prices import ASSET_CLASS_ETF, NasdaqPriceSource
from emscan.trading_calendar import is_in_scan_window

ET = ZoneInfo("America/New_York")

log = get_logger(__name__)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Earnings Expected Move Scanner — narzędzie badawcze, nie system tradingowy.",
)


class WindowPolicy(StrEnum):
    """Co zrobić, gdy skan wypada poza oknem sesji — asercja ze SPEC §1.8.

    Trzy zachowania, bo trzy różne konteksty. Cron potrzebuje `skip`: workflow ma dwa wpisy
    czasowe na DST i ten niewłaściwy musi zakończyć się zielono, a nie czerwono. Człowiek
    weryfikujący konfigurację potrzebuje `require`, czyli twardego błędu. Test i przebieg
    na próbę potrzebują `ignore`.
    """

    IGNORE = "ignore"
    SKIP = "skip"
    REQUIRE = "require"


DateOption = Annotated[
    str | None,
    typer.Option("--date", help="Dzień skanu w formacie ISO. Domyślnie dziś w strefie ET."),
]


def _now() -> datetime:
    """Teraz, w czasie nowojorskim. Wydzielone, żeby test mógł podstawić moment w oknie sesji."""
    return datetime.now(tz=ET)


def _parse_day(value: str | None) -> date:
    """Data ISO albo dzisiejsza data **nowojorska** — nigdy lokalna data maszyny."""
    if value is None:
        return _now().date()
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


def _echo_settlement(result: SettleResult) -> None:
    """Podsumowanie rozliczenia. Oba pomiary ruchu, bo gap i close-to-close mówią różne rzeczy."""
    typer.echo(
        f"rozliczenie sesji {result.session_date.isoformat()} "
        f"(odniesienie: zamknięcie {result.baseline_date.isoformat()})"
    )
    typer.echo(
        f"kandydatów: {len(result.rows)}, rozliczonych: {len(result.settled)} "
        f"(w tym bez EM: {result.settled_without_em}), ruch przebił EM: {result.exceeded_em}"
    )
    if result.missing:
        powody = ", ".join(f"{reason}={count}" for reason, count in result.missing.items())
        typer.echo(f"bez rozliczenia: {powody}")

    if not result.settled:
        return

    typer.echo("")
    typer.echo(f"{'ticker':<8}{'timing':<8}{'gap%':>8}{'close%':>9}{'kier.':>7}{'em_ratio':>10}")
    for row in sorted(
        result.settled, key=lambda r: r.outcome.abs_move_pct if r.outcome else 0.0, reverse=True
    ):
        outcome = row.outcome
        if outcome is None:  # pragma: no cover - filtr wyżej to wyklucza
            continue
        ratio = f"{outcome.em_ratio:.2f}" if outcome.em_ratio is not None else "-"
        typer.echo(
            f"{row.ticker:<8}{row.event.timing!s:<8}{outcome.gap_pct * 100:>8.2f}"
            f"{outcome.close_pct * 100:>9.2f}{outcome.direction!s:>7}{ratio:>10}"
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
    window: Annotated[
        WindowPolicy,
        typer.Option("--window", help="Poza oknem sesji: ignore, skip (cron) albo require"),
    ] = WindowPolicy.IGNORE,
) -> None:
    """Skan dnia: kalendarz obu grup, EM z łańcucha opcji, zapis snapshotów.

    Sensowne okno to 15:30 ET (SPEC §1.8). Uruchomiony poza sesją zwróci kwotowania
    z flagami `stale_quote` i `zero_bid` — to ograniczenie źródeł, nie błąd.
    """
    settings = get_settings()
    scan_date = _parse_day(day)
    snapshot_at = _now()

    if window != WindowPolicy.IGNORE and not is_in_scan_window(snapshot_at):
        message = (
            f"{snapshot_at.strftime('%Y-%m-%d %H:%M %Z')} jest poza oknem skanu "
            "(15:00-16:00 ET w dniu sesji)"
        )
        if window == WindowPolicy.REQUIRE:
            typer.echo(f"błąd: {message}", err=True)
            raise typer.Exit(1)
        typer.echo(f"pomijam skan: {message}")
        return

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
def settle(
    day: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="Dzień skanu w ISO. Domyślnie poprzednik ostatniej zamkniętej sesji.",
        ),
    ] = None,
    all_events: Annotated[
        bool,
        typer.Option(
            "--all-events",
            help="Rozlicz też zdarzenia bez EM — obserwacje dla fazy 2 i D3, więcej zapytań",
        ),
    ] = False,
) -> None:
    """Rozliczenie sesji następującej po dniu skanu — SPEC §1.6.

    Pyta o ceny tylko te tickery, dla których jest snapshot EM: `settle` domyka pętlę
    EM -> realizacja, a ruchy całego uniwersum zbierze `backfill`. Uruchomienie przed
    zamknięciem sesji nie psuje danych — kolejny przebieg nadpisze rozliczenie.

    Domyślna data **nie** jest dzisiejsza: rozliczenie chodzi dzień po skanie, więc „dziś"
    wskazywałoby sesję, która się jeszcze nie odbyła.

    `--all-events` rozszerza zakres na zdarzenia bez policzonego EM. Ich ruch jest pełnoprawną
    obserwacją dla targetu fazy 2 i dla hipotezy D3 (SPEC §2.1, §3.1), a zawężenie do zdarzeń
    z EM wyrzuca większość próbki — na sesji 2026-08-20 zostawiało 16 z 59.
    """
    settings = get_settings()
    scan_date = _parse_day(day) if day is not None else default_settle_scan_date(_now())
    raw_dir = settings.resolved_raw_dir() / scan_date.isoformat()

    prices = NasdaqPriceSource(
        user_agent=settings.user_agent,
        timeout=settings.http_timeout,
        max_retries=settings.http_max_retries,
        raw_dir=raw_dir,
    )
    try:
        with open_db(settings.resolved_db_path()) as conn:
            result = run_settle(
                scan_date=scan_date,
                conn=conn,
                prices=prices,
                settled_at=_now(),
                require_snapshot=not all_events,
            )
    finally:
        prices.close()

    _echo_settlement(result)
    typer.echo(f"raport: python -m emscan report --date {scan_date.isoformat()}")


def _echo_backfill(result: BackfillResult) -> None:
    typer.echo(
        f"backfill {result.start.isoformat()}..{result.end.isoformat()}: "
        f"sesji w zakresie {result.days_considered}, pobranych {result.days_fetched}, "
        f"pominiętych (już w bazie) {result.days_skipped}"
    )
    typer.echo(
        f"zdarzeń zapisanych: {result.events_written}, "
        f"oznaczonych duplikatów daty: {result.conflicts_marked}"
    )
    for failure in result.failures[:5]:
        typer.echo(f"  awaria: {failure}")
    if len(result.failures) > 5:
        typer.echo(f"  ... i {len(result.failures) - 5} dalszych awarii")
    if not result.complete:
        typer.echo("przebieg niepełny — uruchom ponownie, pobrane dni zostaną pominięte")


@app.command()
def backfill(
    date_from: Annotated[str, typer.Option("--from", help="Początek zakresu, ISO")],
    date_to: Annotated[
        str | None, typer.Option("--to", help="Koniec zakresu, ISO. Puste = dziś")
    ] = None,
    no_resume: Annotated[
        bool, typer.Option("--no-resume", help="Pobierz też dni, które są już w bazie")
    ] = False,
) -> None:
    """Historyczny kalendarz wyników — materiał wyjściowy dla fazy 2 (SPEC §2.1).

    **Zapisuje sam kalendarz, bez EM i bez rozliczeń.** Historyczne ceny opcji są płatne, więc
    EM buduje się wyłącznie do przodu przez `scan`. Rozliczeń historycznych nie liczymy, bo
    Nasdaq retroaktywnie kasuje porę publikacji (BMO/AMC), a bez niej sesja rozliczeniowa jest
    niejednoznaczna — patrz `engine/backfill.py` i docs/PLAN-faza-1.md.

    Zakres dwóch lat to kilkaset zapytań i kilka minut. Przebieg przerwany w połowie wolno
    uruchomić ponownie: dni już obecne w bazie są pomijane.
    """
    settings = get_settings()
    start = _parse_day(date_from)
    end = _parse_day(date_to) if date_to is not None else _now().date()
    if start > end:
        raise typer.BadParameter(f"--from ({start}) jest po --to ({end})")

    calendars: list[EarningsCalendarSource] = [
        NasdaqCalendarSource(
            user_agent=settings.user_agent,
            timeout=settings.http_timeout,
            max_retries=settings.http_max_retries,
            min_interval=DEFAULT_MIN_INTERVAL,
            # Bez cache surowych odpowiedzi: kilkaset plików JSON to setki megabajtów,
            # a wartość diagnostyczna jest tu znikoma.
            raw_dir=None,
        )
    ]
    if settings.finnhub_api_key:
        calendars.append(
            FinnhubCalendarSource(
                settings.finnhub_api_key,
                timeout=settings.http_timeout,
                max_retries=settings.http_max_retries,
                raw_dir=None,
            )
        )
    else:
        log.warning("brak klucza Finnhuba — backfill zbierze kalendarz z jednego źródła")

    total = len(trading_days(start, end))
    typer.echo(f"sesji do przetworzenia: {total}")

    try:
        with (
            open_db(settings.resolved_db_path()) as conn,
            typer.progressbar(length=total, label="backfill") as bar,
        ):
            result = run_backfill(
                start=start,
                end=end,
                conn=conn,
                calendars=calendars,
                fetched_at=_now(),
                resume=not no_resume,
                on_day=lambda _day, index, _total: bar.update(1) if index else None,
            )
    finally:
        for source in calendars:
            source.close()

    _echo_backfill(result)


MARKET_LOOKBACK_DAYS = 730
"""Domyślny zakres `market`: dwa lata. Endpoint zwraca cały zakres jednym zapytaniem, więc
dłuższe okno kosztuje tyle samo co krótkie, a daje historię pod realized vol."""


def _echo_market(result: MarketResult) -> None:
    typer.echo(
        f"dane rynkowe {result.start.isoformat()}..{result.end.isoformat()}: "
        f"instrumentów {len(result.instruments_fetched)} "
        f"({', '.join(result.instruments_fetched) or 'brak'}), świec zapisanych "
        f"{result.bars_written}"
    )
    for ticker, value in result.iv30_recorded.items():
        typer.echo(f"  iv30 {ticker}: {value * 100:.2f}%")
    for failure in result.failures:
        typer.echo(f"  awaria: {failure}")


@app.command()
def market(
    date_from: Annotated[
        str | None, typer.Option("--from", help="Początek zakresu, ISO. Puste = dwa lata wstecz")
    ] = None,
    date_to: Annotated[
        str | None, typer.Option("--to", help="Koniec zakresu, ISO. Puste = dziś")
    ] = None,
    no_iv: Annotated[
        bool, typer.Option("--no-iv", help="Pomiń pomiar iv30 (o jedno zapytanie mniej)")
    ] = False,
) -> None:
    """Świece instrumentów reżimu rynkowego: SPY, QQQ, IWM, VXX — plus iv30 dla SPY.

    Surowce pod cechy „szerokiego rynku" ze SPEC §2.3. Cechy liczy dopiero faza 2; tutaj
    zbieramy dane, bo ich brak jest blokadą, a nie decyzją modelową.

    Cztery zapytania o ceny (endpoint oddaje cały zakres naraz) plus jedno o zmienność.
    Indeks VIX nie jest dostępny u tego dostawcy w żadnej klasie aktywów — zamiennikiem
    jest `iv30` SPY, mierzone w momencie uruchomienia.
    """
    settings = get_settings()
    end = _parse_day(date_to) if date_to is not None else _now().date()
    start = (
        _parse_day(date_from)
        if date_from is not None
        else end - timedelta(days=MARKET_LOOKBACK_DAYS)
    )
    if start > end:
        raise typer.BadParameter(f"--from ({start}) jest po --to ({end})")

    prices = NasdaqPriceSource(
        user_agent=settings.user_agent,
        timeout=settings.http_timeout,
        max_retries=settings.http_max_retries,
        asset_class=ASSET_CLASS_ETF,
        raw_dir=None,
    )
    options = (
        None
        if no_iv
        else CboeOptionsSource(
            user_agent=settings.user_agent,
            timeout=settings.http_timeout,
            max_retries=settings.http_max_retries,
            raw_dir=None,
        )
    )
    try:
        with open_db(settings.resolved_db_path()) as conn:
            result = run_market_update(
                conn=conn,
                prices=prices,
                start=start,
                end=end,
                fetched_at=_now(),
                options=options,
            )
    finally:
        prices.close()
        if options is not None:
            options.close()

    _echo_market(result)


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
        outcomes = outcomes_for_session(conn, session_date)

    rows = [
        row
        for row in rows_from_snapshots(pairs, outcomes)
        if row.snapshot.em_pct is not None and row.snapshot.em_pct >= threshold
    ]
    if top is not None:
        rows = sort_rows(rows)[:top]

    notes: Sequence[str] = [
        f"Dzień skanu: {scan_date.isoformat()}",
        f"Próg EM: {threshold * 100:.2f}% (snapshotów w bazie dla tej sesji: {len(pairs)})",
        f"Rozliczonych zdarzeń: {len(outcomes)}",
    ]
    text = render(
        rows,
        session_date=session_date,
        generated_at=_now(),
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

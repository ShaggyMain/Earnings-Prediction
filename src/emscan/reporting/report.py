"""Raport skanu — SPEC §1.1, §1.7.

Kolumny są te ze SPEC §1.1, z trzema dodatkami, które wynikają wprost z innych jego
fragmentów: `dte` (bo mnożnik 0.85 zakłada bliskie wygaśnięcie — SPEC §1.5), osobne
kolumny dla trzech metod EM (bo porównanie metod jest częścią wartości projektu) oraz
`flagi` (bo SPEC §1.4 każe flagom towarzyszyć rekordowi — raport, który ukrywa `zero_bid`,
wprowadzałby w błąd).

Kolumny rozliczeniowe — ruch close-to-close, kierunek i `em_ratio` — są puste do czasu
`settle` (krok 6). Puste znaczy „jeszcze nie rozliczone", nigdy zero.

Markdown i HTML są dla człowieka: liczby sformatowane, brak danych jako „—". CSV jest dla
maszyny: wartości surowe, brak danych jako pusta komórka.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from emscan.models import EarningsEvent, EmSnapshot, Outcome

COLUMNS = (
    "ticker",
    "timing",
    "spot",
    "expiry",
    "dte",
    "straddle",
    "EM% A",
    "EM% B",
    "EM% C",
    "close-to-close %",
    "kierunek",
    "em_ratio",
    "flagi",
)

EMPTY = "—"

_FOOTER = (
    "EM to implikowany przez opcje ruch jednego odchylenia, nie prognoza. "
    "Metoda A = `0.85 * straddle`, B = `60/30/10`, C = `spot * IV * sqrt(dte/365)` "
    "— definicje i ograniczenia w "
    "`docs/METHODOLOGY.md` §4. Flagi jakości nie usuwają rekordu (SPEC §1.4). "
    "Kolumny rozliczeniowe wypełnia `settle`."
)


class ReportFormat(StrEnum):
    """Formaty ze SPEC §1.7."""

    MD = "md"
    CSV = "csv"
    HTML = "html"


@dataclass(frozen=True)
class ReportRow:
    """Zdarzenie razem ze snapshotem i — po rozliczeniu — z wynikiem."""

    event: EarningsEvent
    snapshot: EmSnapshot
    outcome: Outcome | None = None


def rows_from_snapshots(
    pairs: Iterable[tuple[EarningsEvent, EmSnapshot]],
    outcomes: dict[int, Outcome] | None = None,
) -> list[ReportRow]:
    """Buduje wiersze raportu z par zwróconych przez `db.latest_snapshots_for_session`.

    `outcomes` (mapa `event_id` -> rozliczenie) będzie podłączona w kroku 6; teraz zostaje
    pusta, więc kolumny rozliczeniowe są puste.
    """
    outcomes = outcomes or {}
    return [
        ReportRow(event=event, snapshot=snapshot, outcome=outcomes.get(snapshot.event_id))
        for event, snapshot in pairs
    ]


def sort_rows(rows: Iterable[ReportRow]) -> list[ReportRow]:
    """Malejąco po EM metody A; brak EM na koniec."""
    return sorted(rows, key=lambda row: row.snapshot.em_pct or 0.0, reverse=True)


def _pct(value: float | None) -> str:
    return EMPTY if value is None else f"{value * 100:.2f}%"


def _money(value: float | None) -> str:
    return EMPTY if value is None else f"{value:.2f}"


def _ratio(value: float | None) -> str:
    return EMPTY if value is None else f"{value:.2f}"


def _display_cells(row: ReportRow) -> tuple[str, ...]:
    snapshot, outcome = row.snapshot, row.outcome
    return (
        row.event.ticker,
        str(row.event.timing),
        _money(snapshot.spot),
        snapshot.expiry.isoformat(),
        str(snapshot.dte),
        _money(snapshot.straddle),
        _pct(snapshot.em_pct),
        _pct(snapshot.em_pct_weighted),
        _pct(snapshot.em_pct_iv),
        _pct(outcome.close_pct) if outcome else EMPTY,
        str(outcome.direction) if outcome else EMPTY,
        _ratio(outcome.em_ratio) if outcome else EMPTY,
        ", ".join(str(flag) for flag in snapshot.quality_flags) or EMPTY,
    )


def _raw_cells(row: ReportRow) -> tuple[str, ...]:
    """Wartości surowe dla CSV: bez znaku procenta, brak danych jako pusta komórka."""
    snapshot, outcome = row.snapshot, row.outcome

    def number(value: float | None) -> str:
        return "" if value is None else repr(value)

    return (
        row.event.ticker,
        str(row.event.timing),
        number(snapshot.spot),
        snapshot.expiry.isoformat(),
        str(snapshot.dte),
        number(snapshot.straddle),
        number(snapshot.em_pct),
        number(snapshot.em_pct_weighted),
        number(snapshot.em_pct_iv),
        number(outcome.close_pct if outcome else None),
        str(outcome.direction) if outcome else "",
        number(outcome.em_ratio if outcome else None),
        "|".join(str(flag) for flag in snapshot.quality_flags),
    )


def _markdown(
    rows: Sequence[ReportRow],
    *,
    session_date: date,
    generated_at: datetime,
    notes: Sequence[str],
) -> str:
    lines = [
        f"# Skan expected move — sesja {session_date.isoformat()}",
        "",
        f"- Wygenerowano: {generated_at.isoformat()}",
        f"- Zdarzeń w raporcie: {len(rows)}",
    ]
    lines.extend(f"- {note}" for note in notes)
    lines.extend(
        [
            "",
            "| " + " | ".join(COLUMNS) + " |",
            "|" + "|".join("---" for _ in COLUMNS) + "|",
        ]
    )
    if rows:
        lines.extend("| " + " | ".join(_display_cells(row)) + " |" for row in rows)
    else:
        lines.append("| " + " | ".join(EMPTY for _ in COLUMNS) + " |")
    lines.extend(["", f"> {_FOOTER}", ""])
    return "\n".join(lines)


def _csv(rows: Sequence[ReportRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(_raw_cells(row) for row in rows)
    return buffer.getvalue()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _html(
    rows: Sequence[ReportRow],
    *,
    session_date: date,
    generated_at: datetime,
    notes: Sequence[str],
) -> str:
    head = "".join(f"<th>{_escape(name)}</th>" for name in COLUMNS)
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(cell)}</td>" for cell in _display_cells(row)) + "</tr>"
        for row in rows
    )
    note_items = "".join(f"<li>{_escape(note)}</li>" for note in notes)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="pl"><head><meta charset="utf-8">'
        f"<title>Skan EM — {session_date.isoformat()}</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem}"
        "table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:.3rem .5rem;"
        "text-align:right}th:first-child,td:first-child,td:last-child{text-align:left}"
        "footer{margin-top:1.5rem;color:#555;font-size:.9rem}</style></head><body>"
        f"<h1>Skan expected move — sesja {session_date.isoformat()}</h1>"
        f"<ul><li>Wygenerowano: {generated_at.isoformat()}</li>"
        f"<li>Zdarzeń w raporcie: {len(rows)}</li>{note_items}</ul>"
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        f"<footer>{_escape(_FOOTER)}</footer></body></html>\n"
    )


def render(
    rows: Iterable[ReportRow],
    *,
    session_date: date,
    generated_at: datetime,
    fmt: ReportFormat = ReportFormat.MD,
    notes: Sequence[str] = (),
) -> str:
    """Raport w wybranym formacie. Wiersze są sortowane malejąco po EM metody A."""
    ordered = sort_rows(rows)
    if fmt == ReportFormat.CSV:
        return _csv(ordered)
    if fmt == ReportFormat.HTML:
        return _html(ordered, session_date=session_date, generated_at=generated_at, notes=notes)
    return _markdown(ordered, session_date=session_date, generated_at=generated_at, notes=notes)


def default_path(reports_dir: Path, session_date: date, fmt: ReportFormat) -> Path:
    """Ścieżka raportu: `reports/scan-<sesja>.<format>`.

    Nazwa jest kluczowana **sesją rozliczeniową**, nie dniem skanu: to sesja jest tym,
    czego raport dotyczy, i to po niej rozliczenie znajdzie swój plik.
    """
    return reports_dir / f"scan-{session_date.isoformat()}.{fmt.value}"

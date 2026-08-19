"""Raport skanu — kolumny, formatowanie i trzy formaty wyjścia."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from emscan.models import (
    Direction,
    EarningsEvent,
    EmSnapshot,
    Outcome,
    QualityFlag,
    Timing,
    TimingConfidence,
)
from emscan.reporting.report import (
    COLUMNS,
    EMPTY,
    ReportFormat,
    ReportRow,
    default_path,
    render,
    rows_from_snapshots,
)

ET = ZoneInfo("America/New_York")

EVENT_DAY = date(2026, 8, 17)
SESSION = date(2026, 8, 18)
EXPIRY = date(2026, 8, 21)
GENERATED_AT = datetime(2026, 8, 17, 15, 35, tzinfo=ET)


def event(ticker: str, timing: Timing = Timing.AMC) -> EarningsEvent:
    return EarningsEvent(
        ticker=ticker,
        event_date=EVENT_DAY,
        timing=timing,
        timing_confidence=TimingConfidence.HIGH,
        session_date=SESSION,
        fetched_at=datetime(2026, 8, 17, 19, 30, tzinfo=UTC),
    )


def snapshot(
    *,
    event_id: int = 1,
    em_pct: float | None = 0.085,
    em_pct_weighted: float | None = 0.07,
    em_pct_iv: float | None = None,
    flags: list[QualityFlag] | None = None,
) -> EmSnapshot:
    return EmSnapshot(
        event_id=event_id,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
        spot=100.0,
        expiry=EXPIRY,
        dte=4,
        atm_strike=100.0,
        straddle=10.0,
        em_abs=8.5,
        em_pct=em_pct,
        em_pct_weighted=em_pct_weighted,
        em_pct_iv=em_pct_iv,
        quality_flags=flags if flags is not None else [QualityFlag.DTE_GT_2],
    )


def outcome(event_id: int = 1) -> Outcome:
    return Outcome(
        event_id=event_id,
        baseline_close=100.0,
        next_open=104.0,
        next_close=106.0,
        gap_pct=0.04,
        close_pct=0.06,
        intraday_pct=0.019,
        direction=Direction.UP,
        abs_move_pct=0.06,
        em_ratio=0.71,
        vrp=0.025,
        exceeded_em=False,
        settled_at=datetime(2026, 8, 18, 20, 0, tzinfo=UTC),
    )


def md(rows: list[ReportRow], **kwargs: object) -> str:
    return render(rows, session_date=SESSION, generated_at=GENERATED_AT, **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ markdown


def test_markdown_has_every_column() -> None:
    text = md([ReportRow(event=event("LIQD"), snapshot=snapshot())])
    header = next(line for line in text.splitlines() if line.startswith("| ticker"))
    assert [cell.strip() for cell in header.strip("|").split("|")] == list(COLUMNS)


def test_markdown_formats_the_numbers() -> None:
    text = md([ReportRow(event=event("LIQD"), snapshot=snapshot())])
    row = next(line for line in text.splitlines() if line.startswith("| LIQD"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[:9] == [
        "LIQD",
        "AMC",
        "100.00",
        "2026-08-21",
        "4",
        "10.00",
        "8.50%",
        "7.00%",
        EMPTY,  # metoda C bez IV
    ]
    assert cells[12] == "dte_gt_2"


def test_settlement_columns_are_empty_before_settle() -> None:
    """Puste znaczy „jeszcze nie rozliczone" — nigdy zero."""
    text = md([ReportRow(event=event("LIQD"), snapshot=snapshot())])
    row = next(line for line in text.splitlines() if line.startswith("| LIQD"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[9:12] == [EMPTY, EMPTY, EMPTY]


def test_settlement_columns_fill_in_after_settle() -> None:
    rows = [ReportRow(event=event("LIQD"), snapshot=snapshot(), outcome=outcome())]
    row = next(line for line in md(rows).splitlines() if line.startswith("| LIQD"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[9:12] == ["6.00%", "UP", "0.71"]


def test_rows_are_sorted_by_em_descending() -> None:
    rows = [
        ReportRow(event=event("SMALL"), snapshot=snapshot(em_pct=0.07)),
        ReportRow(event=event("BIG"), snapshot=snapshot(em_pct=0.19)),
        ReportRow(event=event("NONE"), snapshot=snapshot(em_pct=None)),
    ]
    # Pierwszy wiersz zaczynający się od "| " to nagłówek tabeli, dane idą po nim.
    order = [line.split("|")[1].strip() for line in md(rows).splitlines() if line.startswith("| ")]
    assert order[1:] == ["BIG", "SMALL", "NONE"]


def test_empty_report_still_has_a_table() -> None:
    """Zero wybranych tickerów to wynik, nie awaria — raport ma to pokazać."""
    text = md([])
    assert "Zdarzeń w raporcie: 0" in text
    assert text.count(EMPTY) >= len(COLUMNS)


def test_notes_land_in_the_header() -> None:
    text = md([], notes=["Dzień skanu: 2026-08-17", "odrzucenia: low_em=3"])
    assert "- Dzień skanu: 2026-08-17" in text
    assert "- odrzucenia: low_em=3" in text


def test_footer_states_what_em_is_and_is_not() -> None:
    assert "nie prognoza" in md([])
    assert "METHODOLOGY" in md([])


# ------------------------------------------------------------------ csv


def test_csv_carries_raw_values() -> None:
    text = render(
        [ReportRow(event=event("LIQD"), snapshot=snapshot())],
        session_date=SESSION,
        generated_at=GENERATED_AT,
        fmt=ReportFormat.CSV,
    )
    header, row = list(csv.reader(io.StringIO(text)))
    assert header == list(COLUMNS)
    assert row[0] == "LIQD"
    assert float(row[6]) == pytest.approx(0.085)  # ułamek, nie procent


def test_csv_leaves_missing_values_empty() -> None:
    text = render(
        [ReportRow(event=event("LIQD"), snapshot=snapshot(em_pct_iv=None))],
        session_date=SESSION,
        generated_at=GENERATED_AT,
        fmt=ReportFormat.CSV,
    )
    _, row = list(csv.reader(io.StringIO(text)))
    assert row[8] == ""
    assert row[9:12] == ["", "", ""]


def test_csv_joins_flags_without_commas() -> None:
    """Przecinek w komórce CSV byłby separatorem — flagi łączymy pionową kreską."""
    text = render(
        [
            ReportRow(
                event=event("ABEO"),
                snapshot=snapshot(flags=[QualityFlag.ZERO_BID, QualityFlag.STALE_QUOTE]),
            )
        ],
        session_date=SESSION,
        generated_at=GENERATED_AT,
        fmt=ReportFormat.CSV,
    )
    _, row = list(csv.reader(io.StringIO(text)))
    assert row[12] == "zero_bid|stale_quote"


# ------------------------------------------------------------------ html


def test_html_is_self_contained_and_escaped() -> None:
    text = render(
        [ReportRow(event=event("LIQD"), snapshot=snapshot())],
        session_date=SESSION,
        generated_at=GENERATED_AT,
        fmt=ReportFormat.HTML,
    )
    assert text.startswith("<!DOCTYPE html>")
    assert "<td>LIQD</td>" in text
    assert "http" not in text  # żadnych zewnętrznych zasobów
    assert "&lt;" not in text or "<script" not in text


# ------------------------------------------------------------------ ścieżki i budowanie wierszy


def test_report_path_is_keyed_by_the_session() -> None:
    """To sesja jest tym, czego raport dotyczy — po niej rozliczenie znajdzie swój plik."""
    path = default_path(Path("reports"), SESSION, ReportFormat.MD)
    assert path == Path("reports/scan-2026-08-18.md")


@pytest.mark.parametrize("fmt", list(ReportFormat))
def test_every_format_has_its_own_extension(fmt: ReportFormat) -> None:
    assert default_path(Path("reports"), SESSION, fmt).suffix == f".{fmt.value}"


def test_rows_are_built_from_database_pairs() -> None:
    rows = rows_from_snapshots([(event("LIQD"), snapshot(event_id=11))])
    assert [row.event.ticker for row in rows] == ["LIQD"]
    assert rows[0].outcome is None


def test_outcomes_are_matched_by_event_id() -> None:
    """Podłączenie rozliczeń to krok 6 — mapa już tu wchodzi, żeby nie zmieniać kontraktu."""
    rows = rows_from_snapshots(
        [(event("LIQD"), snapshot(event_id=11))], outcomes={11: outcome(event_id=11)}
    )
    assert rows[0].outcome is not None
    assert rows[0].outcome.direction == Direction.UP

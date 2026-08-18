"""CLI — SPEC §1.7. Bez sieci: testujemy parsowanie, ścieżki i raport z bazy."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from emscan.__main__ import app
from emscan.config import get_settings
from emscan.db import event_id_for, insert_snapshot, open_db, upsert_events
from emscan.models import (
    EarningsEvent,
    EmSnapshot,
    QualityFlag,
    Timing,
    TimingConfidence,
)

ET = ZoneInfo("America/New_York")
runner = CliRunner()

SCAN_DAY = date(2026, 8, 17)
SESSION = date(2026, 8, 18)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Baza i katalog raportów w tmp — ustawienia są cache'owane, więc czyścimy cache."""
    monkeypatch.setenv("EMSCAN_DB_PATH", str(tmp_path / "emscan.db"))
    monkeypatch.setenv("EMSCAN_REPORTS_DIR", str(tmp_path / "reports"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def seed_snapshot(db_path: Path, *, ticker: str = "LIQD", em_pct: float = 0.085) -> None:
    with open_db(db_path) as conn:
        upsert_events(
            conn,
            [
                EarningsEvent(
                    ticker=ticker,
                    event_date=SCAN_DAY,
                    timing=Timing.AMC,
                    timing_confidence=TimingConfidence.HIGH,
                    session_date=SESSION,
                    fetched_at=datetime(2026, 8, 17, 19, 30, tzinfo=UTC),
                )
            ],
        )
        event_id = event_id_for(conn, ticker, SCAN_DAY)
        assert event_id is not None
        insert_snapshot(
            conn,
            EmSnapshot(
                event_id=event_id,
                snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
                spot=100.0,
                expiry=date(2026, 8, 21),
                dte=4,
                atm_strike=100.0,
                straddle=10.0,
                em_abs=8.5,
                em_pct=em_pct,
                quality_flags=[QualityFlag.DTE_GT_2],
            ),
        )


# ------------------------------------------------------------------ pomoc i walidacja


def test_help_lists_the_implemented_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "scan" in result.stdout
    assert "report" in result.stdout


def test_unimplemented_commands_are_absent_not_faked() -> None:
    """`settle` powstaje w kroku 6 — komenda, która nic nie robi, byłaby gorsza od jej braku."""
    assert runner.invoke(app, ["settle", "--date", "2026-08-17"]).exit_code != 0


def test_scan_help_lists_the_filter_overrides() -> None:
    result = runner.invoke(app, ["scan", "--help"])
    assert result.exit_code == 0
    for flag in ("--dry-run", "--min-em", "--min-price", "--min-volume", "--min-oi"):
        assert flag in result.stdout


def test_non_iso_date_is_rejected(workspace: Path) -> None:
    """SPEC §1.7: wszystkie daty ISO. Komunikat błędu idzie na stderr."""
    result = runner.invoke(app, ["report", "--date", "17.08.2026"])
    assert result.exit_code == 2
    assert "ISO" in result.stderr


# ------------------------------------------------------------------ raport


def test_report_writes_a_file_named_after_the_session(workspace: Path) -> None:
    seed_snapshot(workspace / "emscan.db")
    result = runner.invoke(app, ["report", "--date", SCAN_DAY.isoformat()])
    assert result.exit_code == 0

    path = workspace / "reports" / "scan-2026-08-18.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "LIQD" in text
    assert "8.50%" in text


def test_report_on_an_empty_database_is_not_an_error(workspace: Path) -> None:
    """Dzień bez skanowalnych zdarzeń to wynik, nie awaria."""
    result = runner.invoke(app, ["report", "--date", SCAN_DAY.isoformat()])
    assert result.exit_code == 0
    assert "zdarzeń w raporcie: 0" in result.stdout
    assert (workspace / "reports" / "scan-2026-08-18.md").exists()


def test_report_honours_format_and_output_path(workspace: Path) -> None:
    seed_snapshot(workspace / "emscan.db")
    out = workspace / "custom.csv"
    result = runner.invoke(
        app, ["report", "--date", SCAN_DAY.isoformat(), "--format", "csv", "--out", str(out)]
    )
    assert result.exit_code == 0
    assert out.read_text(encoding="utf-8").startswith("ticker,timing,spot")


def test_report_top_limits_the_rows(workspace: Path) -> None:
    seed_snapshot(workspace / "emscan.db")
    result = runner.invoke(app, ["report", "--date", SCAN_DAY.isoformat(), "--top", "0"])
    assert result.exit_code == 0
    assert "zdarzeń w raporcie: 0" in result.stdout


def test_report_applies_the_em_threshold(workspace: Path) -> None:
    """Baza trzyma każdy pomiar, raport pokazuje tylko to, co przeszło próg (SPEC §1.5)."""
    seed_snapshot(workspace / "emscan.db", ticker="CALM", em_pct=0.02)
    result = runner.invoke(app, ["report", "--date", SCAN_DAY.isoformat()])
    assert result.exit_code == 0
    assert "zdarzeń w raporcie: 0" in result.stdout

    text = (workspace / "reports" / "scan-2026-08-18.md").read_text(encoding="utf-8")
    assert "snapshotów w bazie dla tej sesji: 1" in text
    assert "CALM" not in text


def test_report_min_em_zero_shows_everything_measured(workspace: Path) -> None:
    seed_snapshot(workspace / "emscan.db", ticker="CALM", em_pct=0.02)
    result = runner.invoke(app, ["report", "--date", SCAN_DAY.isoformat(), "--min-em", "0"])
    assert result.exit_code == 0
    assert "zdarzeń w raporcie: 1" in result.stdout
    assert "CALM" in (workspace / "reports" / "scan-2026-08-18.md").read_text(encoding="utf-8")

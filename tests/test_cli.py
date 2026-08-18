"""CLI — SPEC §1.7. Bez sieci: testujemy parsowanie, ścieżki i raport z bazy."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from emscan.__main__ import app
from emscan.config import get_settings
from emscan.db import (
    event_id_for,
    insert_outcome,
    insert_snapshot,
    open_db,
    upsert_events,
)
from emscan.models import (
    Direction,
    EarningsEvent,
    EmSnapshot,
    Outcome,
    QualityFlag,
    Timing,
    TimingConfidence,
)

ET = ZoneInfo("America/New_York")
runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """Tekst bez kolorów i bez łamania linii.

    Rich koloruje pomoc i komunikaty błędów, a przy okazji **rozbija nazwy opcji**: `--dry-run`
    trafia do wyjścia jako `-` + `-dry` + `-run` ze znacznikami ANSI pomiędzy. Kolor włącza się
    na GitHub Actions, a lokalnie nie — asercja na surowym wydruku przechodziła u mnie i padała
    na CI. Dlatego: albo tekst przez ten filtr, albo (lepiej) asercja na zachowaniu.
    """
    return " ".join(_ANSI.sub("", text).split())


SCAN_DAY = date(2026, 8, 17)
SESSION = date(2026, 8, 18)


@pytest.fixture(autouse=True)
def force_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wymusza kolorowanie wyjścia w każdym teście CLI.

    GitHub Actions włącza kolor, a lokalny terminal w testach nie — i właśnie na tej różnicy
    przeszły u mnie dwa testy, które padły na CI. Wymuszenie tu sprawia, że warunek z CI jest
    warunkiem domyślnym: nowa asercja na renderowanym tekście pęknie od razu, u autora.
    """
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")


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
    """Nazwy komend nie mają dywizów, więc rich ich nie rozbija — tu tekst jest bezpieczny."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("scan", "settle", "report"):
        assert command in plain(result.stdout)


def test_unimplemented_commands_are_absent_not_faked() -> None:
    """`stats` i `backfill` powstają w kroku 8 — atrapa byłaby gorsza od braku komendy."""
    assert runner.invoke(app, ["stats"]).exit_code != 0
    assert runner.invoke(app, ["backfill", "--from", "2024-01-01"]).exit_code != 0


def test_scan_accepts_every_filter_override(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wszystkie progi nadpisywalne z CLI. Nieznana flaga dałaby kod 2, więc to je weryfikuje.

    Skan pomijamy oknem sesji, żeby test nie tknął sieci — sprawdzamy parsowanie argumentów,
    nie przepływ.
    """
    monkeypatch.setattr("emscan.__main__._now", lambda: OUTSIDE_WINDOW)
    result = runner.invoke(
        app,
        [
            "scan",
            "--window",
            "skip",
            "--min-em",
            "6",
            "--min-price",
            "5",
            "--min-volume",
            "500000",
            "--min-oi",
            "100",
            "--top",
            "5",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "pomijam skan" in result.stdout


def test_scan_rejects_an_unknown_flag() -> None:
    """Kontrola dla testu wyżej: gdyby flagi nie istniały, kod wyjścia byłby właśnie taki."""
    assert runner.invoke(app, ["scan", "--min-nonsense", "1"]).exit_code == 2


def test_non_iso_date_is_rejected(workspace: Path) -> None:
    """SPEC §1.7: wszystkie daty ISO. Komunikat błędu idzie na stderr."""
    result = runner.invoke(app, ["report", "--date", "17.08.2026"])
    assert result.exit_code == 2
    assert "ISO" in plain(result.stderr)


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


def seed_outcome(db_path: Path, *, ticker: str = "LIQD") -> None:
    """Dokłada rozliczenie do istniejącego snapshotu."""
    with open_db(db_path) as conn:
        event_id = event_id_for(conn, ticker, SCAN_DAY)
        assert event_id is not None
        insert_outcome(
            conn,
            Outcome(
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
                settled_at=datetime(2026, 8, 18, 17, 0, tzinfo=ET),
            ),
        )


# ------------------------------------------------------------------ settle


def test_settle_accepts_an_explicit_date(workspace: Path) -> None:
    """Bez snapshotów w bazie nie ma o co pytać źródła cen, więc test nie rusza sieci."""
    result = runner.invoke(app, ["settle", "--date", SCAN_DAY.isoformat()])
    assert result.exit_code == 0
    assert "rozliczonych: 0" in result.stdout


def test_settle_without_snapshots_does_nothing_and_says_so(workspace: Path) -> None:
    """Bez snapshotów nie ma o co pytać źródła cen — więc ta komenda nie rusza sieci."""
    result = runner.invoke(app, ["settle", "--date", SCAN_DAY.isoformat()])
    assert result.exit_code == 0
    assert "rozliczonych: 0" in result.stdout


def test_report_fills_the_settlement_columns(workspace: Path) -> None:
    seed_snapshot(workspace / "emscan.db")
    seed_outcome(workspace / "emscan.db")
    result = runner.invoke(app, ["report", "--date", SCAN_DAY.isoformat()])
    assert result.exit_code == 0

    text = (workspace / "reports" / "scan-2026-08-18.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| LIQD"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[9:12] == ["6.00%", "UP", "0.71"]
    assert "Rozliczonych zdarzeń: 1" in text


# ------------------------------------------------------------------ okno sesji (SPEC §1.8)

OUTSIDE_WINDOW = datetime(2026, 8, 18, 7, 17, tzinfo=ET)  # rano, sesja jeszcze nie ruszyła
HOLIDAY_WINDOW = datetime(2026, 7, 3, 15, 30, tzinfo=ET)  # obchodzony Dzień Niepodległości


def test_window_skip_exits_green_outside_the_window(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cron ma dwa wpisy czasowe na DST — ten niewłaściwy musi kończyć się zielono."""
    monkeypatch.setattr("emscan.__main__._now", lambda: OUTSIDE_WINDOW)
    result = runner.invoke(app, ["scan", "--date", SCAN_DAY.isoformat(), "--window", "skip"])
    assert result.exit_code == 0
    assert "pomijam skan" in result.stdout


def test_window_require_fails_outside_the_window(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("emscan.__main__._now", lambda: OUTSIDE_WINDOW)
    result = runner.invoke(app, ["scan", "--date", SCAN_DAY.isoformat(), "--window", "require"])
    assert result.exit_code == 1
    assert "poza oknem skanu" in result.stderr


def test_holiday_is_outside_the_window_even_at_the_right_hour(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cron nie zna kalendarza NYSE i odpali się w święto — asercja to wyłapuje."""
    monkeypatch.setattr("emscan.__main__._now", lambda: HOLIDAY_WINDOW)
    result = runner.invoke(app, ["scan", "--window", "skip"])
    assert result.exit_code == 0
    assert "pomijam skan" in result.stdout


def test_window_gate_does_not_touch_the_database(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pominięcie następuje przed otwarciem bazy i przed pierwszym zapytaniem do źródeł."""
    monkeypatch.setattr("emscan.__main__._now", lambda: OUTSIDE_WINDOW)
    runner.invoke(app, ["scan", "--window", "skip"])
    assert not (workspace / "emscan.db").exists()

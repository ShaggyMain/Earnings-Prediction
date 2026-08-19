"""Workflow'y GitHub Actions — niezmienniki, których złamanie kosztuje dane.

Nie testujemy tu YAML-a linijka po linijce. Testujemy trzy rzeczy, które muszą być prawdziwe,
a przy edycji pliku łatwo je stracić:

1. **Wspólna grupa `concurrency`.** Skan i rozliczenie commitują tę samą bazę SQLite. Binarki
   nie da się scalić, więc równoległy przebieg kończy się konfliktem i utratą danych.
2. **Para wpisów cron na DST.** Cron w UTC nie przesuwa się z czasem letnim (SPEC §1.8), więc
   każdy workflow ma dwa wpisy godzinę po sobie.
3. **Polityka okna w skanie.** Bez `--window` niewłaściwy przebieg z pary DST zapisałby
   snapshoty z kwotowań po zamknięciu sesji.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
SCAN = WORKFLOWS / "scan.yml"
SETTLE = WORKFLOWS / "settle.yml"


@pytest.mark.parametrize("path", [SCAN, SETTLE])
def test_workflow_exists(path: Path) -> None:
    assert path.is_file()


@pytest.mark.parametrize("path", [SCAN, SETTLE])
def test_both_workflows_share_one_concurrency_group(path: Path) -> None:
    """Ta sama grupa w obu plikach — inaczej dwa przebiegi commitują bazę równolegle."""
    assert "group: emscan-database" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("path", "hours", "minute"), [(SCAN, (19, 20), "0"), (SETTLE, (21, 22), "0")]
)
def test_dst_pair_of_crons(path: Path, hours: tuple[int, int], minute: str) -> None:
    """Dwa wpisy godzinę po sobie: jeden trafia w porę letnią, drugi w zimową.

    Skan celuje w 15:00 ET, nie 15:30, bo okno kończy się 15:45 i zapas na opóźnienie
    harmonogramu ma wynosić 45 minut, nie 15 — patrz `trading_calendar.SCAN_WINDOW_END`.
    """
    crons = re.findall(r'cron: "(\d+) (\d+) ([^"]+)"', path.read_text(encoding="utf-8"))
    assert len(crons) == 2
    assert tuple(int(hour) for _, hour, _ in crons) == hours
    assert {value for value, _, _ in crons} == {minute}
    assert crons[0][0] == crons[1][0]  # ta sama minuta
    assert crons[0][2] == crons[1][2]  # ten sam zakres dni


def test_scan_uses_the_window_policy() -> None:
    assert "--window" in SCAN.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [SCAN, SETTLE])
def test_commit_only_when_the_database_changed(path: Path) -> None:
    """Pominięcie poza oknem i sesja bez zdarzeń to poprawne wyniki, nie powód do commita."""
    text = path.read_text(encoding="utf-8")
    assert "git diff --quiet -- data/emscan.db" in text
    assert "steps.changed.outputs.changed == 'true'" in text


@pytest.mark.parametrize("path", [SCAN, SETTLE])
def test_workflows_can_be_run_by_hand(path: Path) -> None:
    """SPEC §1.8 wymaga `workflow_dispatch` — bez tego nie da się nadrobić pominiętego dnia."""
    assert "workflow_dispatch:" in path.read_text(encoding="utf-8")


def test_only_the_calendar_key_comes_from_secrets() -> None:
    """CBOE i Nasdaq nie wymagają klucza (PROBE-2026-08-17), więc w workflow ma być jeden."""
    text = SCAN.read_text(encoding="utf-8")
    assert text.count("secrets.") == 1
    assert "secrets.EMSCAN_FINNHUB_API_KEY" in text

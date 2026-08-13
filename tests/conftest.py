"""Wspólne narzędzia testowe.

Wszystkie testy działają **bez sieci** (SPEC §B.3 pkt 6) — dane pochodzą z fixtures
nagranych 2026-08-13 przez `scripts/probe_sources.py` i przyciętych skryptem
`scripts/make_fixtures.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Wczytuje nagraną odpowiedź API."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def nasdaq_13() -> Any:
    return load_fixture("nasdaq_earnings_2026-08-13.json")


@pytest.fixture
def nasdaq_14() -> Any:
    return load_fixture("nasdaq_earnings_2026-08-14.json")


@pytest.fixture
def finnhub_13() -> Any:
    return load_fixture("finnhub_earnings_2026-08-13.json")


@pytest.fixture
def finnhub_14() -> Any:
    return load_fixture("finnhub_earnings_2026-08-14.json")

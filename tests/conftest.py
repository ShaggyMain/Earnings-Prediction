"""Wspólne narzędzia testowe.

Wszystkie testy działają **bez sieci** (SPEC §B.3 pkt 6) — dane pochodzą z fixtures
nagranych 2026-08-13 przez `scripts/probe_sources.py` i przyciętych skryptem
`scripts/make_fixtures.py`.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Blokuje prawdziwe połączenia w każdym teście — SPEC §B.3 pkt 6.

    `respx` przechwytuje żądania w warstwie transportu httpx, więc testy na nagranych fixtures
    działają jak dotąd. Ten bezpiecznik łapie to, czego respx nie widzi: komendę CLI, która
    naprawdę idzie do sieci, bo test nie spodziewał się, że ona już istnieje. Dokładnie tak
    `runner.invoke(app, ["backfill", ...])` przeszedł 422 dni z żywego API, zamiast zwrócić
    kod 2 za nieznaną komendę.
    """
    real_connect = socket.socket.connect

    def guard(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if host in _LOCAL_HOSTS:
            real_connect(self, address)
            return
        raise RuntimeError(
            f"test próbował połączyć się z {host!r} — sieć w testach jest zabroniona "
            "(SPEC §B.3 pkt 6). Użyj respx albo atrapy z tests/fakes.py."
        )

    monkeypatch.setattr(socket.socket, "connect", guard)
    yield


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


@pytest.fixture
def cboe_amat() -> Any:
    """Spółka płynna: wszystkie 24 kontrakty mają niezerowe bid i ask."""
    return load_fixture("cboe_options_AMAT.json")


@pytest.fixture
def cboe_abeo() -> Any:
    """Mikrospółka: 14 z 24 kontraktów ma bid = 0 — materiał na flagę zero_bid."""
    return load_fixture("cboe_options_ABEO.json")


@pytest.fixture
def nasdaq_history() -> Any:
    return load_fixture("nasdaq_historical_AMAT.json")

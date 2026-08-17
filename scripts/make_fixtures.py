#!/usr/bin/env python3
"""Buduje fixtures testowe z surowych odpowiedzi w `data/raw/`.

Testy muszą działać bez sieci (SPEC §B.3 pkt 6), więc potrzebują nagranych odpowiedzi.
Pełne pliki mają po 75 KB, co jest nieczytelne w code review — ten skrypt przycina je
do kilkunastu spółek dobranych tak, żeby pokrywały wszystkie przypadki brzegowe.

Nie odpytuje sieci. Wymaga wcześniejszego uruchomienia `scripts/probe_sources.py`.

    python scripts/make_fixtures.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# Dobór celowy — każda spółka pokrywa inny przypadek. Nie zmieniaj bez powodu,
# bo testy w tests/test_engine_events.py opierają się na tych konkretnych tickerach.
KEEP_13 = [
    "AMAT",  # AMC w obu źródłach -> timing HIGH
    "BN",  # BMO w Nasdaq
    "NU",  # time-not-supplied w Nasdaq
    "FTLF",  # KONFLIKT: Nasdaq after-hours vs Finnhub bmo, epsActual wypełnione
    "REKR",  # KONFLIKT: jw.
    "ACTU",  # konflikt DATY: Nasdaq 13.08, Finnhub 14.08
    "VNRX",  # konflikt DATY: Nasdaq 13.08 AMC, Finnhub 14.08
    "ABEO",  # bmo w Finnhub, epsActual wypełnione
    "ACOG",  # amc w Finnhub, epsActual puste
    "ALH",  # wyłącznie w Finnhub — brak weryfikacji krzyżowej
]
# 14.08.2026 to piątek — AMC z tego dnia rozlicza się dopiero w poniedziałek 17.08,
# co czyni te rekordy testem przeskoku przez weekend.
KEEP_14 = [
    "ACTU",  # konflikt DATY względem Nasdaq 13.08
    "VNRX",  # konflikt DATY względem Nasdaq 13.08
    "CSAN",  # AMC w piątek -> sesja poniedziałkowa
    "AYA",  # BMO w piątek -> sesja tego samego dnia
    "BXBL",  # time-not-supplied
    "CSPI",  # obecny w obu źródłach
]


def trim_nasdaq(payload: dict[str, Any], keep: list[str]) -> dict[str, Any]:
    rows = payload.get("data", {}).get("rows") or []
    kept = [r for r in rows if str(r.get("symbol", "")).upper() in keep]
    return {
        "data": {"asOf": payload.get("data", {}).get("asOf"), "headers": {}, "rows": kept},
        "message": None,
        "status": {"rCode": 200},
    }


def trim_finnhub(payload: dict[str, Any], keep: list[str]) -> dict[str, Any]:
    rows = payload.get("earningsCalendar") or []
    kept = [r for r in rows if str(r.get("symbol", "")).upper() in keep]
    return {"earningsCalendar": kept}


# Pełna odpowiedź CBOE dla AMAT ma 1,6 MB i 3574 kontrakty — nieczytelne w review.
# Zostawiamy dwa najbliższe wygaśnięcia i po kilka strike'ów wokół ATM, czyli dokładnie
# to, czego dotyka silnik EM.
CBOE_EXPIRIES = 2
CBOE_STRIKES_AROUND_ATM = 6


def trim_cboe(payload: dict[str, Any], _keep: list[str]) -> dict[str, Any]:
    data = payload.get("data") or {}
    contracts = data.get("options") or []
    spot = float(data.get("current_price") or 0)

    def suffix(symbol: str) -> tuple[str, str, float] | None:
        if len(symbol) <= 15:
            return None
        tail = symbol[-15:]
        if tail[6] not in "CP" or not tail[:6].isdigit() or not tail[7:].isdigit():
            return None
        return tail[:6], tail[6], int(tail[7:]) / 1000

    parsed = [(s, c) for c in contracts if (s := suffix(str(c.get("option", "")))) is not None]
    expiries = sorted({p[0][0] for p in parsed})[:CBOE_EXPIRIES]

    kept: list[dict[str, Any]] = []
    for expiry in expiries:
        for side in ("C", "P"):
            legs = [(p, c) for p, c in parsed if p[0] == expiry and p[1] == side]
            legs.sort(key=lambda item: abs(item[0][2] - spot))
            kept.extend(contract for _, contract in legs[:CBOE_STRIKES_AROUND_ATM])

    trimmed = {k: v for k, v in data.items() if k != "options"}
    trimmed["options"] = kept
    return {
        "timestamp": payload.get("timestamp"),
        "symbol": payload.get("symbol"),
        "data": trimmed,
    }


def trim_nasdaq_historical(payload: dict[str, Any], _keep: list[str]) -> dict[str, Any]:
    """Historia jest już krótka (jeden miesiąc) — przepisujemy bez zmian."""
    return payload


def main() -> int:
    argparse.ArgumentParser(description="Przycina surowe odpowiedzi do fixtures").parse_args()

    FIXTURES.mkdir(parents=True, exist_ok=True)
    # (podkatalog data/raw, nazwa pliku, funkcja przycinająca, lista tickerów)
    jobs = [
        ("2026-08-13", "nasdaq_earnings_2026-08-13.json", trim_nasdaq, KEEP_13),
        ("2026-08-13", "nasdaq_earnings_2026-08-14.json", trim_nasdaq, KEEP_14),
        ("2026-08-13", "finnhub_earnings_2026-08-13.json", trim_finnhub, KEEP_13),
        ("2026-08-13", "finnhub_earnings_2026-08-14.json", trim_finnhub, KEEP_14),
        ("2026-08-17", "cboe_options_AMAT.json", trim_cboe, []),
        ("2026-08-17", "cboe_options_ABEO.json", trim_cboe, []),
        ("2026-08-17", "nasdaq_historical_AMAT.json", trim_nasdaq_historical, []),
    ]

    missing = 0
    for subdir, filename, trim, keep in jobs:
        source_file = REPO_ROOT / "data" / "raw" / subdir / filename
        if not source_file.exists():
            print(f"pominięto (brak): {subdir}/{filename}")
            missing += 1
            continue
        trimmed = trim(json.loads(source_file.read_text(encoding="utf-8")), keep)
        target = FIXTURES / filename
        target.write_text(
            json.dumps(trimmed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        data = trimmed.get("data") or {}
        count = len(
            trimmed.get("earningsCalendar")
            or data.get("rows")
            or data.get("options")
            or (data.get("tradesTable") or {}).get("rows")
            or []
        )
        size_kb = target.stat().st_size / 1024
        print(f"{target.relative_to(REPO_ROOT)}: {count} rekordów, {size_kb:.0f} KB")

    if missing:
        print(f"\nUWAGA: {missing} plików źródłowych nie znaleziono w data/raw/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

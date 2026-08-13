#!/usr/bin/env python3
"""Buduje fixtures testowe z surowych odpowiedzi w `data/raw/`.

Testy muszą działać bez sieci (SPEC §B.3 pkt 6), więc potrzebują nagranych odpowiedzi.
Pełne pliki mają po 75 KB, co jest nieczytelne w code review — ten skrypt przycina je
do kilkunastu spółek dobranych tak, żeby pokrywały wszystkie przypadki brzegowe.

Nie odpytuje sieci. Wymaga wcześniejszego uruchomienia `scripts/probe_sources.py`.

    python scripts/make_fixtures.py --raw-date 2026-08-13
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Przycina surowe odpowiedzi do fixtures")
    parser.add_argument("--raw-date", default="2026-08-13", help="katalog w data/raw/")
    args = parser.parse_args()

    raw_dir = REPO_ROOT / "data" / "raw" / args.raw_date
    if not raw_dir.exists():
        print(f"BŁĄD: brak {raw_dir}. Uruchom najpierw scripts/probe_sources.py")
        return 1

    FIXTURES.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("nasdaq_earnings_2026-08-13.json", trim_nasdaq, KEEP_13),
        ("nasdaq_earnings_2026-08-14.json", trim_nasdaq, KEEP_14),
        ("finnhub_earnings_2026-08-13.json", trim_finnhub, KEEP_13),
        ("finnhub_earnings_2026-08-14.json", trim_finnhub, KEEP_14),
    ]

    for filename, trim, keep in jobs:
        source_file = raw_dir / filename
        if not source_file.exists():
            print(f"pominięto (brak): {filename}")
            continue
        trimmed = trim(json.loads(source_file.read_text(encoding="utf-8")), keep)
        target = FIXTURES / filename
        target.write_text(
            json.dumps(trimmed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        count = len(trimmed.get("earningsCalendar", trimmed.get("data", {}).get("rows", [])))
        print(f"{target.relative_to(REPO_ROOT)}: {count} rekordów")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

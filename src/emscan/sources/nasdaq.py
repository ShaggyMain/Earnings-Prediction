"""Kalendarz wyników z Nasdaq — SPEC §1.2, źródło nr 1.

Kształt odpowiedzi zweryfikowany na żywym API 2026-08-13 (docs/PROBE-2026-08-13.md),
nie odtworzony z dokumentacji:

    {"data": {"asOf": ..., "headers": {...},
              "rows": [{"symbol": "AMAT", "name": "Applied Materials, Inc.",
                        "time": "time-after-hours", "marketCap": "$435,208,861,555",
                        "epsForecast": "$3.38", "noOfEsts": "9",
                        "lastYearRptDt": "8/14/2025", "lastYearEPS": "$2.48",
                        "fiscalQuarterEnding": "Jul/2026"}]},
     "message": null, "status": {"rCode": 200}}

Dwie rzeczy warte zapamiętania:

1. Pole `time` miało wartość `time-not-supplied` dla **54%** spółek. Nasdaq sam
   w sobie nie wystarcza do ustalenia BMO/AMC — stąd weryfikacja krzyżowa.
2. Endpoint nie zwraca opublikowanego EPS, tylko prognozę. `eps_actual` z tego
   źródła jest zawsze None.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from emscan.log import get_logger
from emscan.models import RawEarningsRecord, Timing
from emscan.sources.base import EarningsCalendarSource, SourceDataError
from emscan.sources.http import HttpFetcher

log = get_logger(__name__)

API_URL = "https://api.nasdaq.com/api/calendar/earnings"

# Pole "time" -> Timing. Wartości spoza tej mapy trafiają do UNKNOWN i są logowane,
# bo oznaczają zmianę kontraktu API.
TIMING_MAP = {
    "time-after-hours": Timing.AMC,
    "time-pre-market": Timing.BMO,
    "time-not-supplied": Timing.UNKNOWN,
}


def parse_money(value: Any) -> float | None:
    """Liczba z formatu Nasdaq: "$3.38", "($0.03)", "$435,208,861,555", "", "N/A".

    Nawiasy oznaczają wartość ujemną. Nierozpoznany format daje None, **nigdy zero** —
    zero to poprawna wartość EPS i pomylenie jej z brakiem danych zafałszowałoby cechy.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"N/A", "NA", "--", "-"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def parse_int(value: Any) -> int | None:
    """Liczba całkowita z pola tekstowego Nasdaq."""
    number = parse_money(value)
    return int(number) if number is not None else None


class NasdaqCalendarSource(EarningsCalendarSource):
    """Kalendarz wyników Nasdaq. Wymaga nagłówka User-Agent przeglądarki."""

    name = "nasdaq"

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 20.0,
        max_retries: int = 3,
        min_interval: float = 0.0,
        raw_dir: Path | None = None,
    ) -> None:
        self._fetcher = HttpFetcher(
            self.name,
            timeout=timeout,
            max_retries=max_retries,
            # Skan pyta o dwa dni, więc domyślnie bez odstępu. Backfill pyta o kilkaset
            # i podaje wartość dodatnią — patrz engine/backfill.py.
            min_interval=min_interval,
            raw_dir=raw_dir,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    def close(self) -> None:
        self._fetcher.close()

    def fetch_day(self, day: date) -> list[RawEarningsRecord]:
        payload = self._fetcher.get_json(
            API_URL,
            params={"date": day.isoformat()},
            raw_name=f"nasdaq_earnings_{day.isoformat()}.json",
        )
        records = self.parse(payload, day)
        log.info("fetch", source=self.name, day=day.isoformat(), records=len(records))
        return records

    def parse(self, payload: Any, day: date) -> list[RawEarningsRecord]:
        """Zamienia odpowiedź API na rekordy. Wydzielone, żeby dało się testować z fixtures."""
        if not isinstance(payload, dict):
            raise SourceDataError(f"{self.name}: oczekiwano obiektu JSON, jest {type(payload)}")

        data = payload.get("data")
        if data is None:
            # Nasdaq zwraca data=null dla dni bez sesji — to poprawna, pusta odpowiedź.
            log.info("brak sesji albo brak zdarzeń", source=self.name, day=day.isoformat())
            return []
        if not isinstance(data, dict):
            raise SourceDataError(f"{self.name}: pole 'data' ma nieoczekiwany typ {type(data)}")

        rows = data.get("rows") or []
        if not isinstance(rows, list):
            raise SourceDataError(f"{self.name}: pole 'rows' nie jest listą")

        records: list[RawEarningsRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                log.warning("wiersz bez symbolu — pomijam", source=self.name, day=day.isoformat())
                continue

            raw_time = str(row.get("time") or "")
            timing = TIMING_MAP.get(raw_time)
            if timing is None:
                log.warning(
                    "nieznana wartość pola 'time' — zmiana kontraktu API?",
                    source=self.name,
                    ticker=symbol,
                    value=raw_time,
                )
                timing = Timing.UNKNOWN

            records.append(
                RawEarningsRecord(
                    source=self.name,
                    ticker=symbol,
                    event_date=day,
                    timing=timing,
                    company_name=str(row.get("name") or "").strip() or None,
                    eps_estimate=parse_money(row.get("epsForecast")),
                    eps_actual=None,  # endpoint kalendarza nie podaje opublikowanego EPS
                    market_cap=parse_money(row.get("marketCap")),
                    num_estimates=parse_int(row.get("noOfEsts")),
                )
            )
        return records

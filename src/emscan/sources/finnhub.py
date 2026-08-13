"""Kalendarz wyników z Finnhub — SPEC §1.2, źródło nr 2.

Kształt odpowiedzi zweryfikowany na żywym API 2026-08-13 (docs/PROBE-2026-08-13.md):

    {"earningsCalendar": [
        {"symbol": "ABEO", "date": "2026-08-13", "hour": "bmo", "quarter": 2,
         "year": 2026, "epsEstimate": -0.2346, "epsActual": -0.35,
         "revenueEstimate": 12483922, "revenueActual": 11380000}]}

Uwagi z obserwacji:

1. Pole `hour` miało realnie tylko wartości `bmo`, `amc` i pusty string. Wartość `dmh`
   z dokumentacji jest obsłużona, ale w danych z 13-14.08 nie wystąpiła ani razu.
2. `epsActual` bywa wypełnione już rano — dla FTLF i REKR potwierdzało wersję Finnhuba
   (`bmo`) przeciwko Nasdaqowi (`after-hours`). Zapisujemy to jako informację,
   ale **nie** rozstrzygamy nią konfliktu timingu (uzgodnienie z 2026-08-13).
3. Zbiór spółek pokrywa się z Nasdaqiem tylko częściowo — 140 wspólnych przy 199
   wyłącznie w Nasdaq i 66 wyłącznie w Finnhub. Łączenie źródeł istotnie poszerza
   kalendarz, ale dla większości spółek weryfikacja krzyżowa nie będzie możliwa.

Limit darmowego planu to 60 zapytań na minutę, stąd domyślny odstęp między zapytaniami.
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

API_URL = "https://finnhub.io/api/v1/calendar/earnings"

TIMING_MAP = {
    "bmo": Timing.BMO,
    "amc": Timing.AMC,
    "dmh": Timing.DMH,
    "": Timing.UNKNOWN,
}


class FinnhubCalendarSource(EarningsCalendarSource):
    """Kalendarz wyników Finnhub. Wymaga darmowego klucza API."""

    name = "finnhub"

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        min_interval: float = 1.0,
        raw_dir: Path | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("FinnhubCalendarSource wymaga niepustego klucza API")
        self._api_key = api_key
        self._fetcher = HttpFetcher(
            self.name,
            timeout=timeout,
            max_retries=max_retries,
            min_interval=min_interval,
            raw_dir=raw_dir,
        )

    def close(self) -> None:
        self._fetcher.close()

    def fetch_day(self, day: date) -> list[RawEarningsRecord]:
        payload = self._fetcher.get_json(
            API_URL,
            # klucz idzie w parametrach, ale nazwa pliku cache go nie zawiera
            params={"from": day.isoformat(), "to": day.isoformat(), "token": self._api_key},
            raw_name=f"finnhub_earnings_{day.isoformat()}.json",
        )
        records = self.parse(payload, day)
        log.info("fetch", source=self.name, day=day.isoformat(), records=len(records))
        return records

    def parse(self, payload: Any, day: date) -> list[RawEarningsRecord]:
        """Zamienia odpowiedź API na rekordy. Wydzielone, żeby dało się testować z fixtures."""
        if not isinstance(payload, dict):
            raise SourceDataError(f"{self.name}: oczekiwano obiektu JSON, jest {type(payload)}")
        if "earningsCalendar" not in payload:
            raise SourceDataError(f"{self.name}: brak pola 'earningsCalendar'")

        rows = payload.get("earningsCalendar") or []
        if not isinstance(rows, list):
            raise SourceDataError(f"{self.name}: pole 'earningsCalendar' nie jest listą")

        records: list[RawEarningsRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                log.warning("wiersz bez symbolu — pomijam", source=self.name, day=day.isoformat())
                continue

            # Finnhub potrafi zwrócić zdarzenie z sąsiedniego dnia — ufamy jego dacie,
            # a nie dacie zapytania, żeby nie przypisać zdarzenia do złej sesji.
            raw_date = str(row.get("date") or "").strip()
            try:
                event_date = date.fromisoformat(raw_date) if raw_date else day
            except ValueError:
                log.warning(
                    "niepoprawna data zdarzenia — pomijam",
                    source=self.name,
                    ticker=symbol,
                    value=raw_date,
                )
                continue

            raw_hour = str(row.get("hour") or "").strip().lower()
            timing = TIMING_MAP.get(raw_hour)
            if timing is None:
                log.warning(
                    "nieznana wartość pola 'hour' — zmiana kontraktu API?",
                    source=self.name,
                    ticker=symbol,
                    value=raw_hour,
                )
                timing = Timing.UNKNOWN

            records.append(
                RawEarningsRecord(
                    source=self.name,
                    ticker=symbol,
                    event_date=event_date,
                    timing=timing,
                    company_name=None,  # ten endpoint nie zwraca nazwy spółki
                    eps_estimate=_as_float(row.get("epsEstimate")),
                    eps_actual=_as_float(row.get("epsActual")),
                )
            )
        return records


def _as_float(value: Any) -> float | None:
    """Finnhub zwraca liczby jako JSON number albo null. Zero jest wartością, nie brakiem."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

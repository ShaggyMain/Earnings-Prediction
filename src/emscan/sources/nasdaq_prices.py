"""Historia cen OHLC z Nasdaq — źródło rozliczeń i backfillu.

Kształt odpowiedzi zweryfikowany na żywym API 2026-08-17:

    GET https://api.nasdaq.com/api/quote/AMAT/historical
        ?assetclass=stocks&fromdate=2024-08-01&todate=2026-08-14&limit=9999

    {"data": {"symbol": "AMAT", "totalRecords": 511,
              "tradesTable": {"rows": [
                  {"date": "08/14/2026", "close": "$507.18", "volume": "13,132,340",
                   "open": "$499.40", "high": "$523.00", "low": "$497.10"}]}}}

Uwagi z obserwacji:

1. **Zasięg to około 2 lata** — zapytanie od 2024-08-01 zwróciło 511 sesji, czyli
   dokładnie tyle, ile potrzebuje backfill uzgodniony na 2 lata. Głębsza historia
   z tego endpointu nie przychodzi.
2. Daty są w formacie **MM/DD/YYYY**, a liczby w tym samym zapisie co w kalendarzu
   (`$507.18`, `13,132,340`), więc korzystamy z `parse_money` z modułu kalendarza.
3. Wiersze przychodzą **od najnowszego**, a interfejs `PriceSource` wymaga rosnąco —
   odwracamy kolejność.

**Polityka korekt o splity jest na razie NIEZWERYFIKOWANA.** Nie wiadomo, czy Nasdaq
zwraca ceny surowe, czy skorygowane. Split między `baseline_close` a sesją rozliczeniową
zafałszowałby ruch o rząd wielkości, więc przed krokiem 6 trzeba to sprawdzić na
konkretnym historycznym splicie — patrz docs/METHODOLOGY.md §6 i docs/PLAN-faza-1.md.
Do tego czasu nie zakładamy żadnego wariantu.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from emscan.log import get_logger
from emscan.sources.base import DailyBar, PriceSource, SourceDataError
from emscan.sources.http import HttpFetcher
from emscan.sources.nasdaq import parse_money

log = get_logger(__name__)

API_URL = "https://api.nasdaq.com/api/quote/{ticker}/historical"


class NasdaqPriceSource(PriceSource):
    """Świece dzienne z endpointu historycznego Nasdaq."""

    name = "nasdaq"

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        min_interval: float = 0.2,
        raw_dir: Path | None = None,
    ) -> None:
        self._fetcher = HttpFetcher(
            self.name,
            timeout=timeout,
            max_retries=max_retries,
            min_interval=min_interval,
            not_covered_statuses=frozenset({404}),
            raw_dir=raw_dir,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    def close(self) -> None:
        self._fetcher.close()

    def daily_bars(self, ticker: str, start: date, end: date) -> list[DailyBar]:
        symbol = ticker.strip().upper()
        payload = self._fetcher.get_json(
            API_URL.format(ticker=symbol),
            params={
                "assetclass": "stocks",
                "fromdate": start.isoformat(),
                "todate": end.isoformat(),
                "limit": 9999,
            },
            raw_name=f"nasdaq_historical_{symbol}_{start.isoformat()}_{end.isoformat()}.json",
        )
        bars = self.parse(payload, symbol)
        log.info(
            "fetch",
            source=self.name,
            ticker=symbol,
            bars=len(bars),
            start=start.isoformat(),
            end=end.isoformat(),
        )
        return bars

    def parse(self, payload: Any, ticker: str) -> list[DailyBar]:
        """Zamienia odpowiedź API na świece. Wydzielone, żeby dało się testować z fixtures."""
        if not isinstance(payload, dict):
            raise SourceDataError(f"{self.name}: oczekiwano obiektu JSON, jest {type(payload)}")

        data = payload.get("data")
        if data is None:
            # Nasdaq zwraca data=null dla symbolu bez notowań w zadanym zakresie.
            log.info("brak notowań w zakresie", source=self.name, ticker=ticker)
            return []
        if not isinstance(data, dict):
            raise SourceDataError(f"{self.name}: pole 'data' ma nieoczekiwany typ {type(data)}")

        table = data.get("tradesTable") or {}
        rows = table.get("rows") or []
        if not isinstance(rows, list):
            raise SourceDataError(f"{self.name}: pole 'rows' nie jest listą")

        bars: list[DailyBar] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bar = _row_to_bar(row, ticker=ticker, source=self.name)
            if bar is not None:
                bars.append(bar)

        # API oddaje od najnowszej sesji, interfejs wymaga rosnąco.
        return sorted(bars, key=lambda b: b.day)


def _row_to_bar(row: dict[str, Any], *, ticker: str, source: str) -> DailyBar | None:
    """Jeden wiersz na świecę. Niekompletny wiersz jest pomijany, nie zerowany."""
    raw_date = str(row.get("date") or "").strip()
    try:
        # Sesja to data kalendarzowa — budujemy `date` wprost z MM/DD/YYYY, zamiast
        # przechodzić przez naiwny `datetime`.
        month, day_of_month, year = raw_date.split("/")
        day = date(int(year), int(month), int(day_of_month))
    except ValueError:
        log.warning(
            "niepoprawna data sesji — pomijam", source=source, ticker=ticker, value=raw_date
        )
        return None

    open_ = parse_money(row.get("open"))
    high = parse_money(row.get("high"))
    low = parse_money(row.get("low"))
    close = parse_money(row.get("close"))
    volume = parse_money(row.get("volume"))
    if open_ is None or high is None or low is None or close is None or volume is None:
        # Brak notowań (zawieszenie, halt) — SPEC zabrania cichego zera.
        log.warning(
            "niekompletna świeca — pomijam",
            source=source,
            ticker=ticker,
            day=day.isoformat(),
            row=row,
        )
        return None

    return DailyBar(day=day, open=open_, high=high, low=low, close=close, volume=int(volume))

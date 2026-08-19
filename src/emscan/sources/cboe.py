"""Łańcuch opcji z CBOE — główne źródło EM.

Kształt odpowiedzi zweryfikowany na żywym API 2026-08-17, nie odtworzony z dokumentacji:

    GET https://cdn.cboe.com/api/global/delayed_quotes/options/AMAT.json

    {"timestamp": "2026-08-15 03:44:36", "symbol": "AMAT",
     "data": {"symbol": "AMAT", "current_price": 506.7653, "bid": 506.8, "ask": 507.0,
              "open": 499.87, "high": 523.0, "low": 497.1, "close": 507.18,
              "prev_day_close": 507.18, "volume": 13132344, "iv30": ...,
              "options": [{"option": "AMAT260814C00235000", "bid": 267.85, "ask": 275.05,
                           "iv": 0.0, "open_interest": 1.0, "volume": 0.0,
                           "delta": 1.0, "gamma": 0.0, "theta": -0.0002, ...}]}}

Dlaczego to źródło, a nie yfinance ani Tradier:

1. **Jedno zapytanie zwraca wszystko** — cenę spot i pełny łańcuch dla wszystkich
   wygaśnięć naraz (dla AMAT: 3574 kontrakty, 18 wygaśnięć). Nie ma osobnego
   odpytywania o listę expiry ani o kwotowanie akcji.
2. **Bez klucza i bez rejestracji**, działa przez proxy — w odróżnieniu od yfinance,
   który za proxy nie działa (patrz docs/PROBE-2026-08-13.md §2).
3. **Pokrycie sięga mikrospółek** — sprawdzone na 10 tickerach, 9 zwróciło łańcuch,
   w tym REKR przy cenie 0,72 USD i VNRX przy 0,54 USD.

Dwie rzeczy, o których trzeba pamiętać:

- **HTTP 403 oznacza brak notowanych opcji**, a nie problem z uprawnieniami. Tak
  odpowiedział FTLF. Podnosimy wtedy `SymbolNotCovered`, nie błąd.
- **Kwotowania są opóźnione** (endpoint nazywa się `delayed_quotes`), typowo o 15 minut.
  Dla skanu 30 minut przed zamknięciem to akceptowalne, ale `snapshot_at` zapisuje
  moment pobrania, a `timestamp` z odpowiedzi mówi, jak stare są dane — porównanie
  obu podnosi flagę `stale_quote`.

Symbol kontraktu jest w formacie OCC: `AMAT260814C00235000` = root + YYMMDD + C/P +
strike w tysięcznych. Root bywa alfanumeryczny, więc rozbieramy symbol od końca
(stała długość 15 znaków), a nie wyrażeniem regularnym od początku.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from emscan.log import get_logger
from emscan.sources.base import (
    OptionChain,
    OptionQuote,
    OptionsChainSource,
    OptionType,
    SourceDataError,
    SymbolNotCovered,
)
from emscan.sources.http import HttpFetcher

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")
API_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker}.json"

# Sufiks OCC: YYMMDD (6) + C/P (1) + strike w tysięcznych (8).
_OCC_SUFFIX_LEN = 15
_OCC_STRIKE_DIVISOR = 1000.0


class CboeQuote:
    """Kwotowanie instrumentu bazowego, wyciągnięte z tej samej odpowiedzi co łańcuch."""

    __slots__ = ("ask", "bid", "close", "high", "low", "open", "prev_close", "spot", "volume")

    def __init__(self, payload: dict[str, Any]) -> None:
        self.spot = _as_float(payload.get("current_price"))
        self.bid = _as_float(payload.get("bid"))
        self.ask = _as_float(payload.get("ask"))
        self.open = _as_float(payload.get("open"))
        self.high = _as_float(payload.get("high"))
        self.low = _as_float(payload.get("low"))
        self.close = _as_float(payload.get("close"))
        self.prev_close = _as_float(payload.get("prev_day_close"))
        self.volume = _as_int(payload.get("volume"))


def parse_occ_symbol(symbol: str) -> tuple[str, date, OptionType, float]:
    """Rozbiera symbol OCC na (root, wygaśnięcie, typ, strike).

    Raises:
        SourceDataError: symbol nie ma formatu OCC.
    """
    text = symbol.strip().upper()
    if len(text) <= _OCC_SUFFIX_LEN:
        raise SourceDataError(f"cboe: symbol '{symbol}' jest za krótki jak na format OCC")

    root, suffix = text[:-_OCC_SUFFIX_LEN], text[-_OCC_SUFFIX_LEN:]
    yymmdd, cp, strike_raw = suffix[:6], suffix[6], suffix[7:]
    if cp not in ("C", "P") or not yymmdd.isdigit() or not strike_raw.isdigit():
        raise SourceDataError(f"cboe: symbol '{symbol}' nie pasuje do formatu OCC")

    try:
        # Wygaśnięcie to data kalendarzowa, nie moment w czasie — budujemy `date` wprost,
        # zamiast przechodzić przez naiwny `datetime`.
        expiry = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError as exc:
        raise SourceDataError(f"cboe: '{symbol}' ma niepoprawną datę wygaśnięcia") from exc

    option_type = OptionType.CALL if cp == "C" else OptionType.PUT
    return root, expiry, option_type, int(strike_raw) / _OCC_STRIKE_DIVISOR


class CboeOptionsSource(OptionsChainSource):
    """Łańcuch opcji z publicznego endpointu CBOE.

    Jedna odpowiedź obsługuje wszystkie wygaśnięcia danego tickera, więc trzymamy ją
    w pamięci na czas życia obiektu. Skan przechodzi po tickerach pojedynczo, więc
    cache zwykle zawiera jeden wpis i nie rośnie w nieskończoność.
    """

    name = "cboe"

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
            not_covered_statuses=frozenset({403, 404}),
            raw_dir=raw_dir,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        self._cache: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self._fetcher.close()

    def clear_cache(self) -> None:
        """Czyści cache odpowiedzi — używane między dniami skanu."""
        self._cache.clear()

    def _payload(self, ticker: str) -> dict[str, Any]:
        symbol = ticker.strip().upper()
        if symbol in self._cache:
            return self._cache[symbol]

        payload = self._fetcher.get_json(
            API_URL.format(ticker=symbol),
            raw_name=f"cboe_options_{symbol}.json",
        )
        if not isinstance(payload, dict) or "data" not in payload:
            raise SourceDataError(f"{self.name}: odpowiedź dla {symbol} nie ma pola 'data'")

        self._cache[symbol] = payload
        contracts = (payload.get("data") or {}).get("options") or []
        log.info("fetch", source=self.name, ticker=symbol, contracts=len(contracts))
        return payload

    def quote(self, ticker: str) -> CboeQuote:
        """Kwotowanie instrumentu bazowego — bez dodatkowego zapytania."""
        data = self._payload(ticker).get("data") or {}
        return CboeQuote(data)

    def underlying_volume(self, ticker: str) -> int | None:
        """Wolumen akcji w bieżącej sesji — bez dodatkowego zapytania."""
        return self.quote(ticker).volume

    def data_timestamp(self, ticker: str) -> datetime | None:
        """Znacznik czasu danych podany przez CBOE — podstawa flagi `stale_quote`.

        Odpowiedź niesie czas bez strefy, w czasie nowojorskim.
        """
        raw = self._payload(ticker).get("timestamp")
        if not raw:
            return None
        try:
            return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
        except ValueError:
            log.warning("nieoczekiwany format timestamp", source=self.name, value=raw)
            return None

    def expirations(self, ticker: str) -> list[date]:
        """Dostępne wygaśnięcia, rosnąco.

        Raises:
            SymbolNotCovered: spółka nie ma notowanych opcji.
        """
        contracts = (self._payload(ticker).get("data") or {}).get("options") or []
        expiries = set()
        for contract in contracts:
            symbol = str(contract.get("option") or "")
            if not symbol:
                continue
            try:
                _, expiry, _, _ = parse_occ_symbol(symbol)
            except SourceDataError:
                log.warning("pomijam kontrakt o nieznanym symbolu", source=self.name, symbol=symbol)
                continue
            expiries.add(expiry)
        return sorted(expiries)

    def chain(self, ticker: str, expiry: date) -> OptionChain:
        """Łańcuch dla jednego wygaśnięcia, wraz z ceną spot.

        Raises:
            SymbolNotCovered: spółka nie ma notowanych opcji.
            SourceDataError: brak ceny spot albo brak kontraktów na to wygaśnięcie.
        """
        symbol = ticker.strip().upper()
        data = self._payload(symbol).get("data") or {}
        quote = CboeQuote(data)
        if quote.spot is None or quote.spot <= 0:
            raise SourceDataError(f"{self.name}: brak ceny spot dla {symbol}")

        calls: list[OptionQuote] = []
        puts: list[OptionQuote] = []
        for contract in data.get("options") or []:
            occ = str(contract.get("option") or "")
            if not occ:
                continue
            try:
                _, contract_expiry, option_type, strike = parse_occ_symbol(occ)
            except SourceDataError:
                continue
            if contract_expiry != expiry:
                continue

            leg = OptionQuote(
                strike=strike,
                option_type=option_type,
                bid=_as_float(contract.get("bid")),
                ask=_as_float(contract.get("ask")),
                last=_as_float(contract.get("last_trade_price")),
                volume=_as_int(contract.get("volume")),
                open_interest=_as_int(contract.get("open_interest")),
                implied_volatility=_as_float(contract.get("iv")),
            )
            (calls if option_type == OptionType.CALL else puts).append(leg)

        if not calls or not puts:
            raise SourceDataError(
                f"{self.name}: {symbol} nie ma pełnego łańcucha na {expiry.isoformat()} "
                f"(calls={len(calls)}, puts={len(puts)})"
            )

        return OptionChain(
            ticker=symbol,
            expiry=expiry,
            spot=quote.spot,
            calls=tuple(sorted(calls, key=lambda q: q.strike)),
            puts=tuple(sorted(puts, key=lambda q: q.strike)),
            fetched_at=datetime.now(tz=ET),
            underlying_bid=quote.bid,
            underlying_ask=quote.ask,
        )

    def is_covered(self, ticker: str) -> bool:
        """Czy spółka ma u CBOE notowane opcje. Nie podnosi wyjątku."""
        try:
            self._payload(ticker)
        except SymbolNotCovered:
            return False
        return True


def _as_float(value: Any) -> float | None:
    """Zero jest wartością, nie brakiem — nie zamieniamy go na None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    """CBOE podaje wolumen i OI jako liczby zmiennoprzecinkowe (np. 55.0)."""
    number = _as_float(value)
    return int(number) if number is not None else None

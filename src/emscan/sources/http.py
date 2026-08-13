"""Wspólny klient HTTP dla źródeł — timeout, retry z backoffem, rate limit, cache surowych
odpowiedzi (SPEC §Jakość kodu).

Cache trafia do `data/raw/YYYY-MM-DD/` i służy dwóm rzeczom: odtworzeniu tego, co
naprawdę zwróciło API danego dnia, oraz materiałowi na fixtures do testów bez sieci.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from emscan.log import get_logger
from emscan.sources.base import SourceDataError, SourceUnavailable

log = get_logger(__name__)

# Kody, przy których ponawianie ma sens — przeciążenie lub limit zapytań.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpFetcher:
    """Klient HTTP jednego źródła.

    Args:
        source_name: nazwa źródła, trafia do logów i nazw plików cache.
        timeout: limit czasu pojedynczego zapytania.
        max_retries: liczba prób łącznie z pierwszą.
        min_interval: minimalny odstęp między zapytaniami, w sekundach. Finnhub na
            darmowym planie dopuszcza 60 zapytań na minutę, więc 1.0 trzyma nas
            bezpiecznie poniżej limitu.
        retry_backoff: mnożnik oczekiwania między próbami. Testy podają 0, żeby nie
            spać naprawdę.
        raw_dir: katalog na surowe odpowiedzi; None wyłącza cache.
        headers: nagłówki dokładane do każdego zapytania.
    """

    def __init__(
        self,
        source_name: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        min_interval: float = 0.0,
        retry_backoff: float = 1.0,
        raw_dir: Path | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.source_name = source_name
        self.min_interval = min_interval
        self.raw_dir = raw_dir
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=headers or {},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpFetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _save_raw(self, raw_name: str, text: str) -> None:
        """Zapisuje surową odpowiedź. Klucze API nigdy nie trafiają do nazwy pliku."""
        if self.raw_dir is None:
            return
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        (self.raw_dir / raw_name).write_text(text, encoding="utf-8")

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        raw_name: str | None = None,
    ) -> Any:
        """Pobiera i parsuje JSON.

        Raises:
            SourceUnavailable: sieć, timeout albo status nie do naprawienia ponowieniem.
            SourceDataError: odpowiedź nie jest poprawnym JSON-em.
        """
        text = self._get_text(url, params=params, headers=headers)
        if raw_name:
            self._save_raw(raw_name, text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceDataError(
                f"{self.source_name}: odpowiedź nie jest JSON-em ({exc})"
            ) from exc

    def _get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        attempt = retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=self._retry_backoff, min=0, max=16),
            retry=retry_if_exception_type(SourceUnavailable),
            reraise=True,
        )(self._get_text_once)
        result: str = attempt(url, params=params, headers=headers)
        return result

    def _get_text_once(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        self._throttle()
        try:
            response = self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{self.source_name}: {type(exc).__name__}: {exc}") from exc
        finally:
            self._last_request_at = time.monotonic()

        if response.status_code in _RETRYABLE_STATUS:
            raise SourceUnavailable(f"{self.source_name}: HTTP {response.status_code} — ponawialny")
        if response.status_code >= 400:
            # 401/403 to zwykle brak lub wygaśnięcie klucza; ponawianie nic nie da.
            raise SourceDataError(f"{self.source_name}: HTTP {response.status_code}")
        return response.text

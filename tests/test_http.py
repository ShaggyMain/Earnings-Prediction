"""Klient HTTP źródeł — retry, rate limit, cache. Wszystko na respx, zero prawdziwej sieci."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from emscan.sources.base import SourceDataError, SourceUnavailable
from emscan.sources.http import HttpFetcher

URL = "https://example.test/api"


@pytest.fixture
def fetcher(tmp_path: Path) -> Iterator[HttpFetcher]:
    client = HttpFetcher(
        "test",
        max_retries=3,
        retry_backoff=0,  # bez realnego czekania
        raw_dir=tmp_path / "raw",
    )
    yield client
    client.close()


@respx.mock
def test_returns_parsed_json(fetcher: HttpFetcher) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert fetcher.get_json(URL) == {"ok": True}


@respx.mock
def test_saves_raw_response(fetcher: HttpFetcher, tmp_path: Path) -> None:
    """Cache surowych odpowiedzi to materiał na przyszłe fixtures — SPEC §Jakość kodu."""
    respx.get(URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    fetcher.get_json(URL, raw_name="odpowiedz.json")
    saved = tmp_path / "raw" / "odpowiedz.json"
    assert saved.exists()
    assert '"a"' in saved.read_text(encoding="utf-8")


@respx.mock
def test_api_key_never_reaches_the_cache_filename(fetcher: HttpFetcher, tmp_path: Path) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    fetcher.get_json(URL, params={"token": "sekret123"}, raw_name="bez_klucza.json")
    assert [p.name for p in (tmp_path / "raw").iterdir()] == ["bez_klucza.json"]


@respx.mock
def test_retries_on_rate_limit_then_succeeds(fetcher: HttpFetcher) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert fetcher.get_json(URL) == {"ok": True}
    assert route.call_count == 3


@respx.mock
def test_gives_up_after_max_retries(fetcher: HttpFetcher) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(SourceUnavailable):
        fetcher.get_json(URL)
    assert route.call_count == 3


@respx.mock
def test_does_not_retry_unauthorized(fetcher: HttpFetcher) -> None:
    """401/403 to brak lub wygaśnięcie klucza — ponawianie tylko marnuje limit."""
    route = respx.get(URL).mock(return_value=httpx.Response(401))
    with pytest.raises(SourceDataError):
        fetcher.get_json(URL)
    assert route.call_count == 1


@respx.mock
def test_transport_error_is_retried_and_wrapped(fetcher: HttpFetcher) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError("reset"), httpx.Response(200, json={"ok": 1})]
    )
    assert fetcher.get_json(URL) == {"ok": 1}
    assert route.call_count == 2


@respx.mock
def test_timeout_becomes_source_unavailable(fetcher: HttpFetcher) -> None:
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("za wolno"))
    with pytest.raises(SourceUnavailable):
        fetcher.get_json(URL)


@respx.mock
def test_malformed_json_is_a_data_error(fetcher: HttpFetcher) -> None:
    """Źródło odpowiedziało śmieciem — wyjątek, nigdy ciche zero (SPEC §Czego NIE robić)."""
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>błąd</html>"))
    with pytest.raises(SourceDataError):
        fetcher.get_json(URL)


@respx.mock
def test_rate_limit_spaces_requests(tmp_path: Path) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    with HttpFetcher("test", min_interval=0.05, retry_backoff=0) as client:
        import time

        started = time.monotonic()
        for _ in range(3):
            client.get_json(URL)
        assert time.monotonic() - started >= 0.10

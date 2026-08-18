"""Filtry uniwersum — SPEC §1.5."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from emscan.config import Settings
from emscan.engine.universe import (
    RejectReason,
    UniverseFilters,
    average_volume,
    check_snapshot,
    check_spot,
    check_volume_20d,
    prescreen_session_volume,
)
from emscan.models import EmSnapshot
from emscan.sources.base import DailyBar

ET = ZoneInfo("America/New_York")
FILTERS = UniverseFilters()


def snapshot(*, em_pct: float | None = 0.10, oi_atm: int | None = 500) -> EmSnapshot:
    return EmSnapshot(
        event_id=1,
        snapshot_at=datetime(2026, 8, 17, 15, 30, tzinfo=ET),
        spot=100.0,
        expiry=date(2026, 8, 21),
        dte=4,
        atm_strike=100.0,
        em_pct=em_pct,
        oi_atm=oi_atm,
    )


def bar(day: date, volume: int) -> DailyBar:
    return DailyBar(day=day, open=1.0, high=1.0, low=1.0, close=1.0, volume=volume)


# ------------------------------------------------------------------ progi z konfiguracji


def test_filters_come_from_settings() -> None:
    settings = Settings(
        min_price=7.5, min_volume_20d=250_000, min_oi_atm=50, min_em_pct=0.04, finnhub_api_key=None
    )
    filters = UniverseFilters.from_settings(settings)
    assert (filters.min_price, filters.min_volume_20d) == (7.5, 250_000)
    assert (filters.min_oi_atm, filters.min_em_pct) == (50, 0.04)


def test_session_volume_threshold_is_a_fraction_of_the_real_one() -> None:
    """Podłoga filtra wstępnego jest luźna celowo — spokojny dzień to nie brak płynności."""
    assert FILTERS.session_volume_threshold == pytest.approx(100_000)


# ------------------------------------------------------------------ cena


@pytest.mark.parametrize(
    ("spot", "expected"),
    [(4.99, RejectReason.LOW_PRICE), (5.0, None), (500.0, None)],
)
def test_price_filter(spot: float, expected: RejectReason | None) -> None:
    assert check_spot(spot, FILTERS) == expected


# ------------------------------------------------------------------ wolumen


def test_unknown_session_volume_does_not_reject() -> None:
    """Dostawca, który nie podaje wolumenu, nie jest dowodem na brak płynności."""
    assert prescreen_session_volume(None, FILTERS) is None


@pytest.mark.parametrize(
    ("volume", "expected"),
    [(99_999, RejectReason.THIN_SESSION_VOLUME), (100_000, None), (5_000_000, None)],
)
def test_session_volume_prescreen(volume: int, expected: RejectReason | None) -> None:
    assert prescreen_session_volume(volume, FILTERS) == expected


def test_average_takes_only_the_last_sessions() -> None:
    """Okno to 20 sesji — starsze świece nie mogą rozwodnić średniej."""
    old = [bar(date(2026, 7, 1), 10_000_000)]
    recent = [bar(date(2026, 8, day), 1_000_000) for day in range(1, 21)]
    assert average_volume(old + recent, window=20) == pytest.approx(1_000_000)


def test_average_uses_what_exists_for_a_young_listing() -> None:
    """Świeże IPO ma mniej niż 20 sesji — odrzucenie za wiek notowania byłoby cichym filtrem."""
    assert average_volume([bar(date(2026, 8, 17), 800_000)], window=20) == pytest.approx(800_000)


def test_average_of_nothing_is_unknown_not_zero() -> None:
    assert average_volume([], window=20) is None


def test_volume_filter_returns_the_number_that_decided() -> None:
    average, reason = check_volume_20d([bar(date(2026, 8, 17), 100_000)], FILTERS)
    assert reason == RejectReason.THIN_VOLUME_20D
    assert average == pytest.approx(100_000)


def test_volume_filter_passes_liquid_names() -> None:
    average, reason = check_volume_20d([bar(date(2026, 8, 17), 900_000)], FILTERS)
    assert (average, reason) == (pytest.approx(900_000), None)


def test_no_price_history_is_its_own_reason() -> None:
    """Brak świec to inny problem niż niska płynność i musi się inaczej nazywać."""
    assert check_volume_20d([], FILTERS) == (None, RejectReason.NO_PRICE_HISTORY)


# ------------------------------------------------------------------ snapshot


def test_low_oi_is_rejected_before_em_is_even_considered() -> None:
    assert check_snapshot(snapshot(oi_atm=99, em_pct=0.5), FILTERS) == RejectReason.LOW_OI


def test_unknown_oi_does_not_reject() -> None:
    """Silnik zostawia None tylko wtedy, gdy dostawca milczy — patrz METHODOLOGY §3."""
    assert check_snapshot(snapshot(oi_atm=None), FILTERS) is None


@pytest.mark.parametrize(
    ("em_pct", "expected"),
    [(0.0599, RejectReason.LOW_EM), (0.06, None), (0.25, None), (None, RejectReason.LOW_EM)],
)
def test_em_threshold(em_pct: float | None, expected: RejectReason | None) -> None:
    assert check_snapshot(snapshot(em_pct=em_pct), FILTERS) == expected


def test_thresholds_are_per_run() -> None:
    """CLI nadpisuje progi na jeden skan — filtry są niemutowalne, więc powstaje nowy zestaw."""
    loose = UniverseFilters(min_em_pct=0.01)
    assert check_snapshot(snapshot(em_pct=0.02), FILTERS) == RejectReason.LOW_EM
    assert check_snapshot(snapshot(em_pct=0.02), loose) is None

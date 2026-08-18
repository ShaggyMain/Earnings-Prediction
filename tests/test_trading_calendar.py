"""Kalendarz sesji NYSE. SPEC §Jakość kodu wymaga testu dnia świątecznego."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from emscan.trading_calendar import (
    easter_sunday,
    is_in_scan_window,
    is_in_session,
    is_trading_day,
    next_trading_day,
    nyse_holidays,
    previous_trading_day,
    trading_day_on_or_after,
)


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
    ],
)
def test_easter_sunday(year: int, expected: date) -> None:
    assert easter_sunday(year) == expected


def test_weekend_is_not_a_session() -> None:
    assert not is_trading_day(date(2026, 8, 15))  # sobota
    assert not is_trading_day(date(2026, 8, 16))  # niedziela
    assert is_trading_day(date(2026, 8, 14))  # piątek


@pytest.mark.parametrize(
    ("holiday", "opis"),
    [
        (date(2026, 1, 1), "Nowy Rok (czwartek)"),
        (date(2026, 1, 19), "Martin Luther King Jr. Day"),
        (date(2026, 2, 16), "Washington's Birthday"),
        (date(2026, 4, 3), "Wielki Piątek"),
        (date(2026, 5, 25), "Memorial Day"),
        (date(2026, 6, 19), "Juneteenth"),
        (date(2026, 7, 3), "4 lipca wypada w sobotę, obchodzony w piątek"),
        (date(2026, 9, 7), "Labor Day"),
        (date(2026, 11, 26), "Thanksgiving"),
        (date(2026, 12, 25), "Boże Narodzenie"),
    ],
)
def test_nyse_holidays_2026(holiday: date, opis: str) -> None:
    assert not is_trading_day(holiday), opis


def test_new_year_on_saturday_is_not_observed_on_friday() -> None:
    """Wyjątek NYSE: Nowy Rok w sobotę nie przesuwa się na piątek 31 grudnia."""
    assert date(2022, 1, 1).weekday() == 5
    assert is_trading_day(date(2021, 12, 31))


def test_new_year_on_sunday_moves_to_monday() -> None:
    assert date(2023, 1, 1).weekday() == 6
    assert not is_trading_day(date(2023, 1, 2))


def test_juneteenth_only_from_2022() -> None:
    assert is_trading_day(date(2021, 6, 18))  # piątek, jeszcze bez święta
    assert not is_trading_day(date(2022, 6, 20))  # 19.06.2022 to niedziela -> poniedziałek


def test_special_closure_is_honoured() -> None:
    """Dzień żałoby narodowej po śmierci Jimmy'ego Cartera."""
    assert date(2025, 1, 9).weekday() == 3
    assert not is_trading_day(date(2025, 1, 9))


def test_next_trading_day_skips_weekend() -> None:
    assert next_trading_day(date(2026, 8, 14)) == date(2026, 8, 17)


def test_next_trading_day_skips_holiday_and_weekend() -> None:
    """Czwartek przed Thanksgiving: kolejna sesja to piątek, bo święto jest w czwartek."""
    assert next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)


def test_next_trading_day_is_exclusive() -> None:
    assert next_trading_day(date(2026, 8, 13)) == date(2026, 8, 14)


def test_previous_trading_day_skips_weekend() -> None:
    assert previous_trading_day(date(2026, 8, 17)) == date(2026, 8, 14)


def test_trading_day_on_or_after_keeps_a_session() -> None:
    assert trading_day_on_or_after(date(2026, 8, 13)) == date(2026, 8, 13)


def test_trading_day_on_or_after_moves_off_weekend() -> None:
    assert trading_day_on_or_after(date(2026, 8, 15)) == date(2026, 8, 17)


def test_holidays_are_computed_for_any_year() -> None:
    """Reguły, nie zaszyta lista — backfill sięga lat, których nikt nie wpisywał ręcznie."""
    assert date(2019, 12, 25) in nyse_holidays(2019)
    assert date(2031, 12, 25) in nyse_holidays(2031)


# ------------------------------------------------------------------ godziny sesji


ET = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")


def et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (et(2026, 8, 18, 9, 30), True),
        (et(2026, 8, 18, 12, 0), True),
        (et(2026, 8, 18, 16, 0), True),
        (et(2026, 8, 18, 9, 29), False),
        (et(2026, 8, 18, 16, 1), False),
        (et(2026, 8, 15, 12, 0), False),  # sobota
        (et(2026, 7, 3, 12, 0), False),  # obchodzony Dzień Niepodległości
    ],
)
def test_is_in_session(moment: datetime, expected: bool) -> None:
    assert is_in_session(moment) is expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (et(2026, 8, 18, 15, 30), True),
        (et(2026, 8, 18, 15, 0), True),
        (et(2026, 8, 18, 16, 0), True),
        (et(2026, 8, 18, 14, 59), False),
        (et(2026, 8, 18, 16, 1), False),
        (et(2026, 8, 15, 15, 30), False),  # sobota
        (et(2026, 7, 3, 15, 30), False),  # święto
    ],
)
def test_is_in_scan_window(moment: datetime, expected: bool) -> None:
    assert is_in_scan_window(moment) is expected


def test_summer_cron_hits_the_window_and_winter_cron_misses_it() -> None:
    """To jest cała asercja ze SPEC §1.8: cron w UTC nie przesuwa się z DST.

    Wpis `30 19 * * 1-5` to 15:30 ET w sierpniu i 14:30 ET w styczniu. Drugi wpis, godzinę
    później, jest tym właściwym w czasie zimowym — a o tym, który z nich liczy się dzisiaj,
    rozstrzyga `is_in_scan_window`, nie kalendarz w cronie.
    """
    summer = datetime(2026, 8, 18, 19, 30, tzinfo=UTC_TZ)
    winter_early = datetime(2027, 1, 19, 19, 30, tzinfo=UTC_TZ)
    winter_late = datetime(2027, 1, 19, 20, 30, tzinfo=UTC_TZ)

    assert is_in_scan_window(summer) is True
    assert is_in_scan_window(winter_early) is False
    assert is_in_scan_window(winter_late) is True
    # Ten sam wpis cron dałby w sierpniu godzinę po zamknięciu sesji.
    assert is_in_scan_window(datetime(2026, 8, 18, 20, 30, tzinfo=UTC_TZ)) is False


@pytest.mark.parametrize("check", [is_in_session, is_in_scan_window])
def test_naive_timestamp_is_rejected(check: object) -> None:
    """SPEC §1.7: naiwny znacznik porównany z godzinami ET myli się o kilka godzin."""
    with pytest.raises(ValueError, match="strefą"):
        check(datetime(2026, 8, 18, 15, 30))  # type: ignore[operator]  # noqa: DTZ001

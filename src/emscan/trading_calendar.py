"""Kalendarz sesji NYSE — czysta logika dat, bez wiedzy o wynikach spółek.

`session_date` ze SPEC §1.1 liczy się po kalendarzu **giełdowym**, nie kalendarzowym:
AMC z piątku konsumuje się w poniedziałek, a nie w sobotę. Ten moduł dostarcza tej
wiedzy i nie zależy od niczego poza stdlib, żeby dało się go testować bez sieci.

Święta wyznaczane regułami, więc działa dla dowolnego roku — także w backfillu.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

_SATURDAY = 5
_SUNDAY = 6

ET = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

SCAN_WINDOW_START = time(15, 0)
SCAN_WINDOW_END = time(16, 0)
"""Okno, w którym skan ma sens: ostatnia godzina sesji (SPEC §1.8 celuje w 15:30 ET).

Godzina zapasu, a nie kwadrans, bo cron w GitHub Actions bywa opóźniony o kilkanaście minut.
Opóźnienie przekraczające okno oznacza pominięcie skanu — kwotowania po zamknięciu byłyby
nieodświeżone, a fałszywy snapshot jest gorszy od braku snapshotu.
"""
"""Zwykłe godziny sesji NYSE w czasie nowojorskim.

Skrócone sesje (wigilia, dzień po Święcie Dziękczynienia — zamknięcie 13:00) nie są tu
uwzględnione: dla skanu, który interesuje okno 15:30, oznacza to jedynie ostrzeżenie
zamiast twardej blokady. Krok 7 zamienia to na asercję i wtedy trzeba je dodać.
"""

# Sesje zamknięte poza zwykłym kalendarzem świąt — żałoby narodowe, klęski żywiołowe.
# Lista jest prowadzona ręcznie i NIE jest kompletna wstecz. Przy rozszerzaniu
# backfillu poza zakres poniżej trzeba ją zweryfikować z oficjalnym kalendarzem NYSE.
SPECIAL_CLOSURES: frozenset[date] = frozenset(
    {
        date(2025, 1, 9),  # dzień żałoby narodowej po śmierci Jimmy'ego Cartera
    }
)

# Od którego roku NYSE obchodzi Juneteenth.
_JUNETEENTH_FROM = 2022


def easter_sunday(year: int) -> date:
    """Niedziela Wielkanocna wg algorytmu gregoriańskiego (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lo = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lo) // 451
    month, day = divmod(h + lo - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """N-ty `weekday` (0=poniedziałek) danego miesiąca, np. 3. poniedziałek stycznia."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Ostatni `weekday` danego miesiąca, np. ostatni poniedziałek maja."""
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(holiday: date) -> date | None:
    """Przesunięcie święta wypadającego w weekend.

    Sobota → piątek przed, niedziela → poniedziałek po. Zwraca None, gdy święta
    nie obchodzi się wcale.
    """
    if holiday.weekday() == _SATURDAY:
        return holiday - timedelta(days=1)
    if holiday.weekday() == _SUNDAY:
        return holiday + timedelta(days=1)
    return holiday


@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> frozenset[date]:
    """Dni, w których NYSE jest zamknięta przez cały dzień, w danym roku.

    Skrócone sesje (Wigilia, dzień po Święcie Dziękczynienia) NIE są tu ujęte —
    to wciąż pełnoprawne sesje z otwarciem i zamknięciem, a operujemy na danych
    dziennych, więc skrócenie nie zmienia `open` ani `close`.
    """
    days: set[date] = set()

    # Nowy Rok: w sobotę NYSE nie odrabia go w piątek 31 grudnia — wyjątek od reguły.
    new_year = date(year, 1, 1)
    if new_year.weekday() != _SATURDAY and (observed := _observed(new_year)) is not None:
        days.add(observed)

    days.add(_nth_weekday(year, 1, 0, 3))  # Martin Luther King Jr. Day
    days.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    days.add(easter_sunday(year) - timedelta(days=2))  # Wielki Piątek
    days.add(_last_weekday(year, 5, 0))  # Memorial Day

    if year >= _JUNETEENTH_FROM and (juneteenth := _observed(date(year, 6, 19))) is not None:
        days.add(juneteenth)
    if (independence := _observed(date(year, 7, 4))) is not None:
        days.add(independence)

    days.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving

    if (christmas := _observed(date(year, 12, 25))) is not None:
        days.add(christmas)

    days.update(d for d in SPECIAL_CLOSURES if d.year == year)
    return frozenset(days)


def is_trading_day(day: date) -> bool:
    """Czy `day` jest sesją giełdową NYSE."""
    return day.weekday() < _SATURDAY and day not in nyse_holidays(day.year)


def next_trading_day(day: date) -> date:
    """Pierwsza sesja **po** `day` (samo `day` nie jest brane pod uwagę)."""
    candidate = day + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def previous_trading_day(day: date) -> date:
    """Ostatnia sesja **przed** `day` (samo `day` nie jest brane pod uwagę)."""
    candidate = day - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def trading_day_on_or_after(day: date) -> date:
    """`day`, jeśli jest sesją; w przeciwnym razie najbliższa sesja po nim."""
    return day if is_trading_day(day) else next_trading_day(day)


def is_in_session(moment: datetime) -> bool:
    """Czy dany moment wypada w godzinach zwykłej sesji NYSE.

    Skan poza sesją nie jest błędem kodu, ale kwotowania są wtedy nieodświeżane i często
    jednostronne (SPEC §Ograniczenia) — dlatego pytanie ma sens dla ostrzeżenia w logu.

    Raises:
        ValueError: moment bez strefy czasowej. Naiwny znacznik porównany z godzinami
            nowojorskimi daje cichy błąd kilku godzin — SPEC §1.7.
    """
    if moment.tzinfo is None:
        raise ValueError("is_in_session wymaga znacznika ze strefą — patrz SPEC §1.7")
    local = moment.astimezone(ET)
    if not is_trading_day(local.date()):
        return False
    return MARKET_OPEN <= local.time() <= MARKET_CLOSE


def is_in_scan_window(moment: datetime) -> bool:
    """Czy moment wypada w oknie skanu — asercja wymagana przez SPEC §1.8.

    Cron w UTC **nie przesuwa się z DST**: ten sam wpis `30 19 * * 1-5` to 15:30 ET w czasie
    letnim i 14:30 ET w zimowym. Dlatego workflow ma dwa wpisy cron, po jednym na każdą porę
    roku, a o tym, który z nich jest tym właściwym, rozstrzyga ta funkcja. Ten drugi kończy
    się pominięciem, nie błędem.

    Święto wypada z okna automatycznie: cron odpali się i tak, bo nie zna kalendarza NYSE.

    Raises:
        ValueError: moment bez strefy czasowej.
    """
    if moment.tzinfo is None:
        raise ValueError("is_in_scan_window wymaga znacznika ze strefą — patrz SPEC §1.7")
    local = moment.astimezone(ET)
    if not is_trading_day(local.date()):
        return False
    return SCAN_WINDOW_START <= local.time() <= SCAN_WINDOW_END

"""Baza SQLite — schemat ze SPEC §1.4 i operacje na nim.

Świadomie na gołym `sqlite3` ze stdlib, bez ORM: schemat jest mały, zapytania są
proste, a SPEC podaje DDL wprost. Daty i znaczniki czasu zapisujemy jako tekst ISO,
żeby baza była czytelna bez narzędzi.

Tabele `llm_cache` i `llm_costs` (SPEC §B.3, §B.5) powstaną dopiero w fazie 4 —
nie tworzymy schematu na zapas.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from emscan.log import get_logger
from emscan.models import (
    Direction,
    EarningsEvent,
    EmSnapshot,
    Outcome,
    QualityFlag,
    RawEarningsRecord,
    Timing,
    TimingConfidence,
)
from emscan.sources.base import DailyBar

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS earnings_events (
    id                 INTEGER PRIMARY KEY,
    ticker             TEXT    NOT NULL,
    company_name       TEXT,
    event_date         TEXT    NOT NULL,           -- ISO
    timing             TEXT    NOT NULL,           -- BMO|AMC|DMH|UNKNOWN
    session_date       TEXT,                       -- ISO; NULL dla DMH i UNKNOWN
    timing_confidence  TEXT    NOT NULL,           -- HIGH|MEDIUM|LOW|UNKNOWN
    eps_actual_present INTEGER NOT NULL DEFAULT 0,
    sources_json       TEXT    NOT NULL,
    fetched_at         TEXT    NOT NULL,
    date_conflict      INTEGER NOT NULL DEFAULT 0,   -- patrz METHODOLOGY §7
    UNIQUE(ticker, event_date)
);


CREATE TABLE IF NOT EXISTS em_snapshots (
    id               INTEGER PRIMARY KEY,
    event_id         INTEGER NOT NULL REFERENCES earnings_events(id) ON DELETE CASCADE,
    snapshot_at      TEXT    NOT NULL,
    spot             REAL    NOT NULL,
    expiry           TEXT    NOT NULL,
    dte              INTEGER NOT NULL,
    atm_strike       REAL    NOT NULL,
    call_bid         REAL,
    call_ask         REAL,
    put_bid          REAL,
    put_ask          REAL,
    call_mid         REAL,
    put_mid          REAL,
    underlying_bid   REAL,                         -- kwotowanie AKCJI, patrz METHODOLOGY §3
    underlying_ask   REAL,
    iv30             REAL,                         -- IV 30d instrumentu bazowego, ułamek
    straddle         REAL,
    em_abs           REAL,
    em_pct           REAL,                         -- metoda A: 0.85 * straddle
    em_abs_weighted  REAL,
    em_pct_weighted  REAL,                         -- metoda B: 60/30/10
    em_pct_iv        REAL,                         -- metoda C: spot * IV * sqrt(dte/365)
    iv_atm           REAL,
    oi_atm           INTEGER,
    volume_atm       INTEGER,
    rel_spread       REAL,
    quality_flags    TEXT    NOT NULL DEFAULT '[]'
);


-- Świece dzienne. Nie ma jej w SPEC §1.4, dodana świadomie: `settle` i `scan` pobierają
-- historię cen tak czy tak, a bez jej zapisania faza 2 musiałaby odpytać źródło tysiące razy
-- o dane, które już przez nas przeszły. Klucz (ticker, day) sprawia, że nakładające się okna
-- kolejnych dni nie mnożą wierszy.
CREATE TABLE IF NOT EXISTS daily_bars (
    id          INTEGER PRIMARY KEY,
    ticker      TEXT    NOT NULL,
    day         TEXT    NOT NULL,                  -- ISO
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      INTEGER NOT NULL,
    iv30        REAL,                              -- tylko dla świec, przy których ją zmierzono
    fetched_at  TEXT    NOT NULL,
    UNIQUE(ticker, day)
);

CREATE TABLE IF NOT EXISTS outcomes (
    id             INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL REFERENCES earnings_events(id) ON DELETE CASCADE,
    baseline_close REAL    NOT NULL,
    next_open      REAL    NOT NULL,
    next_close     REAL    NOT NULL,
    gap_pct        REAL    NOT NULL,
    close_pct      REAL    NOT NULL,
    intraday_pct   REAL    NOT NULL,
    direction      TEXT    NOT NULL,               -- UP|DOWN|FLAT
    abs_move_pct   REAL    NOT NULL,
    em_ratio       REAL,
    vrp            REAL,
    exceeded_em    INTEGER,
    settled_at     TEXT    NOT NULL,
    UNIQUE(event_id)
);
"""


INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_session ON earnings_events(session_date);
CREATE INDEX IF NOT EXISTS idx_events_event_date ON earnings_events(event_date);
CREATE INDEX IF NOT EXISTS idx_events_conflict ON earnings_events(date_conflict);
CREATE INDEX IF NOT EXISTS idx_snapshots_event ON em_snapshots(event_id);
CREATE INDEX IF NOT EXISTS idx_bars_ticker_day ON daily_bars(ticker, day);
"""
"""Indeksy osobno od tabel, bo powstają **po** migracji kolumn.

Indeks na `date_conflict` odwołuje się do kolumny, której baza z wcześniejszego skanu jeszcze
nie ma — wykonany przed `ALTER TABLE` wywracał całe otwarcie bazy.
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Otwiera połączenie z włączonymi kluczami obcymi i dostępem po nazwie kolumny."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Tworzy tabele, dokłada brakujące kolumny, potem indeksy. Idempotentne.

    Kolejność jest istotna: indeks na kolumnie dodanej migracją nie może powstać przed nią.
    """
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.executescript(INDEXES)


MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("earnings_events", "date_conflict", "INTEGER NOT NULL DEFAULT 0"),
    ("em_snapshots", "underlying_bid", "REAL"),
    ("em_snapshots", "underlying_ask", "REAL"),
    ("em_snapshots", "iv30", "REAL"),
)
"""Kolumny dodane po tym, jak schemat trafił do użytku: (tabela, kolumna, definicja).

Lista rośnie wraz ze schematem. `CREATE TABLE IF NOT EXISTS` nie zmienia istniejącej tabeli,
więc bez tego baza z wcześniejszego skanu nie dostałaby nowych kolumn, a zapytania na nie
liczące wywracałyby się. W SQLite dodanie kolumny z wartością domyślną nie przepisuje tabeli,
więc migracja jest tania i może być odpalana przy każdym otwarciu bazy.
"""


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Dokłada kolumny z `MIGRATIONS`, których w bazie jeszcze nie ma. Idempotentne."""
    for table, column, definition in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # tabeli nie ma — powstanie ze SCHEMA z kolumną już w środku
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            log.info("migracja schematu", table=table, added=column)


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Połączenie z gotowym schematem, zamykane po wyjściu z bloku."""
    conn = connect(db_path)
    try:
        init_schema(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def open_memory_db() -> Iterator[sqlite3.Connection]:
    """Baza w pamięci — `scan --dry-run` i testy.

    Dzięki temu „na próbę" znaczy dokładnie ten sam przepływ, z prawdziwymi kluczami
    obcymi i prawdziwym `event_id`, tylko bez śladu na dysku. Alternatywa — udawany
    identyfikator zdarzenia — wymagałaby fałszowania danych w silniku.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        init_schema(conn)
        yield conn
    finally:
        conn.close()


def _serialize_sources(sources: dict[str, RawEarningsRecord]) -> str:
    return json.dumps(
        {name: json.loads(rec.model_dump_json()) for name, rec in sources.items()},
        ensure_ascii=False,
        sort_keys=True,
    )


def _deserialize_sources(payload: str) -> dict[str, RawEarningsRecord]:
    raw: dict[str, Any] = json.loads(payload)
    return {name: RawEarningsRecord.model_validate(rec) for name, rec in raw.items()}


def upsert_events(conn: sqlite3.Connection, events: Iterable[EarningsEvent]) -> int:
    """Zapisuje zdarzenia, nadpisując istniejące po (ticker, event_date).

    Nadpisanie jest zamierzone: flaga BMO/AMC potrafi się zmienić na kilka dni przed
    publikacją (SPEC §Ograniczenia), a interesuje nas stan najświeższy. Poprzednia
    wersja rekordu nie jest archiwizowana — gdyby historia zmian timingu okazała się
    potrzebna do analizy, wymaga to osobnej tabeli audytowej.
    """
    rows = [
        (
            event.ticker,
            event.company_name,
            event.event_date.isoformat(),
            str(event.timing),
            event.session_date.isoformat() if event.session_date else None,
            str(event.timing_confidence),
            int(event.eps_actual_present),
            _serialize_sources(event.sources),
            event.fetched_at.isoformat(),
        )
        for event in events
    ]
    if not rows:
        return 0
    cur = conn.executemany(
        """
        INSERT INTO earnings_events
            (ticker, company_name, event_date, timing, session_date,
             timing_confidence, eps_actual_present, sources_json, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, event_date) DO UPDATE SET
            company_name       = excluded.company_name,
            timing             = excluded.timing,
            session_date       = excluded.session_date,
            timing_confidence  = excluded.timing_confidence,
            eps_actual_present = excluded.eps_actual_present,
            sources_json       = excluded.sources_json,
            fetched_at         = excluded.fetched_at
        """,
        rows,
    )
    return cur.rowcount


def _row_to_event(row: sqlite3.Row) -> EarningsEvent:
    return EarningsEvent(
        ticker=row["ticker"],
        company_name=row["company_name"],
        event_date=date.fromisoformat(row["event_date"]),
        timing=Timing(row["timing"]),
        timing_confidence=TimingConfidence(row["timing_confidence"]),
        session_date=date.fromisoformat(row["session_date"]) if row["session_date"] else None,
        eps_actual_present=bool(row["eps_actual_present"]),
        sources=_deserialize_sources(row["sources_json"]),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
    )


def events_by_event_date(conn: sqlite3.Connection, event_date: date) -> list[EarningsEvent]:
    """Zdarzenia opublikowane danego dnia."""
    cur = conn.execute(
        "SELECT * FROM earnings_events WHERE event_date = ? ORDER BY ticker",
        (event_date.isoformat(),),
    )
    return [_row_to_event(row) for row in cur.fetchall()]


def events_by_session_date(conn: sqlite3.Connection, session_date: date) -> list[EarningsEvent]:
    """Zdarzenia konsumowane w danej sesji — AMC z D-1 oraz BMO z D razem.

    To jest grupowanie ze SPEC §1 „Kluczowa obserwacja o grupowaniu".
    """
    cur = conn.execute(
        "SELECT * FROM earnings_events WHERE session_date = ? ORDER BY ticker",
        (session_date.isoformat(),),
    )
    return [_row_to_event(row) for row in cur.fetchall()]


def has_events_on(conn: sqlite3.Connection, event_date: date) -> bool:
    """Czy w bazie jest choć jedno zdarzenie z tego dnia — podstawa wznawiania backfillu.

    Dzień faktycznie pusty (święto, brak publikacji) zostanie przy wznowieniu pobrany
    ponownie. To kilka zapytań więcej i świadomie wybrana cena za brak osobnej tabeli
    „dni już przetworzonych".
    """
    cur = conn.execute(
        "SELECT 1 FROM earnings_events WHERE event_date = ? LIMIT 1", (event_date.isoformat(),)
    )
    return cur.fetchone() is not None


def event_id_for(conn: sqlite3.Connection, ticker: str, event_date: date) -> int | None:
    """Klucz główny zdarzenia albo None, gdy go nie ma."""
    cur = conn.execute(
        "SELECT id FROM earnings_events WHERE ticker = ? AND event_date = ?",
        (ticker.strip().upper(), event_date.isoformat()),
    )
    row = cur.fetchone()
    return int(row["id"]) if row else None


# ------------------------------------------------------------------ em_snapshots


def insert_snapshot(conn: sqlite3.Connection, snapshot: EmSnapshot) -> int:
    """Dopisuje snapshot EM i zwraca jego identyfikator.

    Tabela celowo nie ma `UNIQUE(event_id)`: snapshot jest **pomiarem w chwili**, a nie
    stanem zdarzenia. Dwa skany tego samego dnia dają dwa wiersze i to jest wartość —
    widać, jak EM zmieniał się w miarę zbliżania się do publikacji. Raport bierze
    najświeższy wiersz na zdarzenie.
    """
    cur = conn.execute(
        """
        INSERT INTO em_snapshots
            (event_id, snapshot_at, spot, expiry, dte, atm_strike,
             call_bid, call_ask, put_bid, put_ask, call_mid, put_mid,
             underlying_bid, underlying_ask, iv30,
             straddle, em_abs, em_pct, em_abs_weighted, em_pct_weighted, em_pct_iv,
             iv_atm, oi_atm, volume_atm, rel_spread, quality_flags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.event_id,
            snapshot.snapshot_at.isoformat(),
            snapshot.spot,
            snapshot.expiry.isoformat(),
            snapshot.dte,
            snapshot.atm_strike,
            snapshot.call_bid,
            snapshot.call_ask,
            snapshot.put_bid,
            snapshot.put_ask,
            snapshot.call_mid,
            snapshot.put_mid,
            snapshot.underlying_bid,
            snapshot.underlying_ask,
            snapshot.iv30,
            snapshot.straddle,
            snapshot.em_abs,
            snapshot.em_pct,
            snapshot.em_abs_weighted,
            snapshot.em_pct_weighted,
            snapshot.em_pct_iv,
            snapshot.iv_atm,
            snapshot.oi_atm,
            snapshot.volume_atm,
            snapshot.rel_spread,
            json.dumps([str(flag) for flag in snapshot.quality_flags]),
        ),
    )
    return int(cur.lastrowid or 0)


def _row_to_snapshot(row: sqlite3.Row) -> EmSnapshot:
    return EmSnapshot(
        event_id=int(row["event_id"]),
        snapshot_at=datetime.fromisoformat(row["snapshot_at"]),
        spot=row["spot"],
        expiry=date.fromisoformat(row["expiry"]),
        dte=int(row["dte"]),
        atm_strike=row["atm_strike"],
        call_bid=row["call_bid"],
        call_ask=row["call_ask"],
        put_bid=row["put_bid"],
        put_ask=row["put_ask"],
        call_mid=row["call_mid"],
        put_mid=row["put_mid"],
        underlying_bid=row["underlying_bid"],
        underlying_ask=row["underlying_ask"],
        iv30=row["iv30"],
        straddle=row["straddle"],
        em_abs=row["em_abs"],
        em_pct=row["em_pct"],
        em_abs_weighted=row["em_abs_weighted"],
        em_pct_weighted=row["em_pct_weighted"],
        em_pct_iv=row["em_pct_iv"],
        iv_atm=row["iv_atm"],
        oi_atm=row["oi_atm"],
        volume_atm=row["volume_atm"],
        rel_spread=row["rel_spread"],
        quality_flags=[QualityFlag(flag) for flag in json.loads(row["quality_flags"])],
    )


def _events_by_id_for_session(
    conn: sqlite3.Connection, session_date: date
) -> dict[int, EarningsEvent]:
    """Zdarzenia danej sesji, po kluczu głównym. To grupowanie ze SPEC §1: AMC z D-1 i BMO z D."""
    return {
        int(row["id"]): _row_to_event(row)
        for row in conn.execute(
            "SELECT * FROM earnings_events WHERE session_date = ? ORDER BY ticker",
            (session_date.isoformat(),),
        )
    }


def _latest_snapshots_by_event(
    conn: sqlite3.Connection, event_ids: Iterable[int]
) -> dict[int, EmSnapshot]:
    """Najświeższy snapshot na zdarzenie. Jedno zapytanie, bez N+1."""
    ids = tuple(event_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    latest: dict[int, EmSnapshot] = {}
    for row in conn.execute(
        # Sortowanie rosnąco po snapshot_at sprawia, że ostatni wpis do słownika jest
        # najświeższy. Liczba znaków zapytania pochodzi z długości listy, nie z danych.
        f"SELECT * FROM em_snapshots WHERE event_id IN ({placeholders}) ORDER BY snapshot_at",
        ids,
    ):
        latest[int(row["event_id"])] = _row_to_snapshot(row)
    return latest


def latest_snapshots_for_session(
    conn: sqlite3.Connection, session_date: date
) -> list[tuple[EarningsEvent, EmSnapshot]]:
    """Zdarzenia danej sesji, które **mają** snapshot EM — tak czyta je raport.

    Zdarzenie bez snapshotu (odrzucone przez filtry albo z niepoliczalnym EM) nie ma tu pary;
    w tabeli `earnings_events` zostaje nietknięte.
    """
    events = _events_by_id_for_session(conn, session_date)
    latest = _latest_snapshots_by_event(conn, events)
    return [(event, latest[event_id]) for event_id, event in events.items() if event_id in latest]


def settlement_candidates(
    conn: sqlite3.Connection, session_date: date, *, require_snapshot: bool = True
) -> list[tuple[int, EarningsEvent, EmSnapshot | None]]:
    """Zdarzenia do rozliczenia, wraz z identyfikatorem i snapshotem, jeśli istnieje.

    `require_snapshot=True` (domyślnie) zwraca tylko zdarzenia z policzonym EM — to domyka
    pętlę EM -> realizacja. `False` zwraca **wszystkie** zdarzenia z wyliczoną sesją, także te
    bez EM: ich ruch jest pełnoprawną obserwacją dla targetu fazy 2 i dla hipotezy D3, które
    liczą się z samych cen (SPEC §2.1, §3.1). Rozliczenia bez EM mają `em_ratio`, `vrp`
    i `exceeded_em` równe NULL.
    """
    events = _events_by_id_for_session(conn, session_date)
    latest = _latest_snapshots_by_event(conn, events)
    candidates: list[tuple[int, EarningsEvent, EmSnapshot | None]] = []
    for event_id, event in events.items():
        snapshot = latest.get(event_id)
        if snapshot is None and require_snapshot:
            continue
        candidates.append((event_id, event, snapshot))
    return candidates


# ------------------------------------------------------------------ outcomes


def insert_outcome(conn: sqlite3.Connection, outcome: Outcome) -> int:
    """Zapisuje rozliczenie, nadpisując istniejące dla tego zdarzenia.

    `UNIQUE(event_id)` jest tu zamierzone, w odróżnieniu od snapshotów: rozliczenie to
    **stan** zdarzenia, nie pomiar w chwili. Ponowny `settle` ma poprawić wynik, jeśli
    pierwszy przebieg trafił w moment przed zamknięciem sesji.
    """
    cur = conn.execute(
        """
        INSERT INTO outcomes
            (event_id, baseline_close, next_open, next_close, gap_pct, close_pct,
             intraday_pct, direction, abs_move_pct, em_ratio, vrp, exceeded_em, settled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            baseline_close = excluded.baseline_close,
            next_open      = excluded.next_open,
            next_close     = excluded.next_close,
            gap_pct        = excluded.gap_pct,
            close_pct      = excluded.close_pct,
            intraday_pct   = excluded.intraday_pct,
            direction      = excluded.direction,
            abs_move_pct   = excluded.abs_move_pct,
            em_ratio       = excluded.em_ratio,
            vrp            = excluded.vrp,
            exceeded_em    = excluded.exceeded_em,
            settled_at     = excluded.settled_at
        """,
        (
            outcome.event_id,
            outcome.baseline_close,
            outcome.next_open,
            outcome.next_close,
            outcome.gap_pct,
            outcome.close_pct,
            outcome.intraday_pct,
            str(outcome.direction),
            outcome.abs_move_pct,
            outcome.em_ratio,
            outcome.vrp,
            None if outcome.exceeded_em is None else int(outcome.exceeded_em),
            outcome.settled_at.isoformat(),
        ),
    )
    return int(cur.lastrowid or 0)


def _row_to_outcome(row: sqlite3.Row) -> Outcome:
    return Outcome(
        event_id=int(row["event_id"]),
        baseline_close=row["baseline_close"],
        next_open=row["next_open"],
        next_close=row["next_close"],
        gap_pct=row["gap_pct"],
        close_pct=row["close_pct"],
        intraday_pct=row["intraday_pct"],
        direction=Direction(row["direction"]),
        abs_move_pct=row["abs_move_pct"],
        em_ratio=row["em_ratio"],
        vrp=row["vrp"],
        exceeded_em=None if row["exceeded_em"] is None else bool(row["exceeded_em"]),
        settled_at=datetime.fromisoformat(row["settled_at"]),
    )


def outcomes_for_session(conn: sqlite3.Connection, session_date: date) -> dict[int, Outcome]:
    """Rozliczenia zdarzeń danej sesji, po `event_id` — w tej formie bierze je raport."""
    cur = conn.execute(
        """
        SELECT o.* FROM outcomes o
        JOIN earnings_events e ON e.id = o.event_id
        WHERE e.session_date = ?
        """,
        (session_date.isoformat(),),
    )
    return {int(row["event_id"]): _row_to_outcome(row) for row in cur.fetchall()}


# ------------------------------------------------------------------ duplikaty daty publikacji


def mark_date_conflicts(conn: sqlite3.Connection, *, max_gap_days: int = 1) -> int:
    """Oznacza pary „ten sam ticker w sąsiednich dniach" — METHODOLOGY §7.

    Spółka nie raportuje dwa razy w odstępie doby, więc taka para to prawie na pewno **jedna**
    publikacja opisana przez dwa źródła, które nie zgadzają się co do daty. Nie scalamy jej
    i nie wybieramy zwycięzcy: żadna dostępna przesłanka nie rozstrzyga, która data jest
    prawdziwa, a rozstrzyganie po cenach byłoby wnioskowaniem z tej samej zmiennej, którą
    faza 2 ma przewidywać.

    Oznaczamy **oba** wiersze pary. Faza 2 wyklucza je jednym predykatem, a rekord zostaje
    w bazie — SPEC §1.4 zabrania usuwania rekordów o niskiej jakości.

    Przebieg jest post-passem po całej tabeli, nie decyzją przy zapisie: backfill przetwarza
    dni po kolei i wznawia się po przerwaniu, więc para może powstać z dwóch różnych runów.

    Returns:
        Liczba wierszy oznaczonych **w tym przebiegu** (flaga nie jest zerowana ponownie).
    """
    cur = conn.execute(
        """
        UPDATE earnings_events SET date_conflict = 1
        WHERE date_conflict = 0
          AND id IN (
            SELECT a.id FROM earnings_events a
            JOIN earnings_events b
              ON b.ticker = a.ticker
             AND b.id != a.id
             AND ABS(JULIANDAY(b.event_date) - JULIANDAY(a.event_date)) <= ?
          )
        """,
        (max_gap_days,),
    )
    marked = cur.rowcount
    if marked:
        log.warning("oznaczono duplikaty daty publikacji", rows=marked)
    return marked


def conflicting_events(conn: sqlite3.Connection) -> list[tuple[str, date]]:
    """Oznaczone pary, po tickerze i dacie — do raportowania i do wykluczeń w fazie 2."""
    cur = conn.execute(
        "SELECT ticker, event_date FROM earnings_events WHERE date_conflict = 1 "
        "ORDER BY ticker, event_date"
    )
    return [(row["ticker"], date.fromisoformat(row["event_date"])) for row in cur.fetchall()]


# ------------------------------------------------------------------ daily_bars


def upsert_bars(
    conn: sqlite3.Connection,
    ticker: str,
    bars: Iterable[DailyBar],
    *,
    fetched_at: datetime,
) -> int:
    """Zapisuje świece, nadpisując istniejące po (ticker, day).

    Nadpisanie jest zamierzone: dostawca koryguje ceny wstecz (splity — patrz METHODOLOGY §6),
    więc świeższa wersja świecy jest tą właściwą. `iv30` nie jest tu ruszane, bo pochodzi
    z innego źródła niż OHLC — ustawia je `record_iv30`.
    """
    symbol = ticker.strip().upper()
    rows = [
        (
            symbol,
            bar.day.isoformat(),
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            fetched_at.isoformat(),
        )
        for bar in bars
    ]
    if not rows:
        return 0
    cur = conn.executemany(
        """
        INSERT INTO daily_bars (ticker, day, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, day) DO UPDATE SET
            open = excluded.open, high = excluded.high, low = excluded.low,
            close = excluded.close, volume = excluded.volume, fetched_at = excluded.fetched_at
        """,
        rows,
    )
    return cur.rowcount


def record_iv30(conn: sqlite3.Connection, ticker: str, day: date, iv30: float) -> bool:
    """Dopisuje zmierzoną IV30 do świecy dnia, w którym ją zmierzono.

    Wartość jest pomiarem z momentu pobrania, nie wielkością dzienną, więc trafia wyłącznie
    do świecy tego dnia. Brak świecy oznacza, że sesja jeszcze się nie zamknęła — zwracamy
    False, zamiast tworzyć świecę bez OHLC.
    """
    cur = conn.execute(
        "UPDATE daily_bars SET iv30 = ? WHERE ticker = ? AND day = ?",
        (iv30, ticker.strip().upper(), day.isoformat()),
    )
    return cur.rowcount > 0


def bars_for(conn: sqlite3.Connection, ticker: str, start: date, end: date) -> list[DailyBar]:
    """Świece z bazy w zakresie włącznie, rosnąco po dacie."""
    cur = conn.execute(
        "SELECT * FROM daily_bars WHERE ticker = ? AND day BETWEEN ? AND ? ORDER BY day",
        (ticker.strip().upper(), start.isoformat(), end.isoformat()),
    )
    return [
        DailyBar(
            day=date.fromisoformat(row["day"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=int(row["volume"]),
        )
        for row in cur.fetchall()
    ]

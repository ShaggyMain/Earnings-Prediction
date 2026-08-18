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
    UNIQUE(ticker, event_date)
);

CREATE INDEX IF NOT EXISTS idx_events_session ON earnings_events(session_date);
CREATE INDEX IF NOT EXISTS idx_events_event_date ON earnings_events(event_date);

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

CREATE INDEX IF NOT EXISTS idx_snapshots_event ON em_snapshots(event_id);

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


def connect(db_path: Path) -> sqlite3.Connection:
    """Otwiera połączenie z włączonymi kluczami obcymi i dostępem po nazwie kolumny."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Tworzy tabele, jeśli ich nie ma. Idempotentne."""
    conn.executescript(SCHEMA)


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
             straddle, em_abs, em_pct, em_abs_weighted, em_pct_weighted, em_pct_iv,
             iv_atm, oi_atm, volume_atm, rel_spread, quality_flags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def latest_snapshots_for_session(
    conn: sqlite3.Connection, session_date: date
) -> list[tuple[EarningsEvent, EmSnapshot]]:
    """Najświeższy snapshot na zdarzenie, dla wszystkich zdarzeń danej sesji.

    To jest grupowanie ze SPEC §1 — AMC z D-1 i BMO z D razem. Zdarzenia bez snapshotu
    (odrzucone przez filtry albo z niepoliczalnym EM) po prostu nie mają tu pary; w tabeli
    `earnings_events` zostają nietknięte.
    """
    events: dict[int, EarningsEvent] = {}
    for row in conn.execute(
        "SELECT * FROM earnings_events WHERE session_date = ? ORDER BY ticker",
        (session_date.isoformat(),),
    ):
        events[int(row["id"])] = _row_to_event(row)
    if not events:
        return []

    placeholders = ",".join("?" * len(events))
    latest: dict[int, EmSnapshot] = {}
    for row in conn.execute(
        # Sortowanie rosnąco po snapshot_at sprawia, że ostatni wpis do słownika jest
        # najświeższy. Liczba znaków zapytania pochodzi z długości listy, nie z danych.
        f"SELECT * FROM em_snapshots WHERE event_id IN ({placeholders}) ORDER BY snapshot_at",
        tuple(events),
    ):
        latest[int(row["event_id"])] = _row_to_snapshot(row)

    return [(event, latest[event_id]) for event_id, event in events.items() if event_id in latest]


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

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

from emscan.models import EarningsEvent, RawEarningsRecord, Timing, TimingConfidence

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

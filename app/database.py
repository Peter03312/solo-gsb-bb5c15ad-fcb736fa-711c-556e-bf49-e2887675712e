"""SQLite connection management and schema initialisation."""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS corridors (
    number    INTEGER PRIMARY KEY,
    source    INTEGER NOT NULL,
    target    INTEGER NOT NULL,
    minutes   INTEGER NOT NULL CHECK (minutes >= 1),
    capacity  INTEGER NOT NULL CHECK (capacity >= 1)
);

CREATE TABLE IF NOT EXISTS closures (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    corridor  INTEGER NOT NULL REFERENCES corridors(number),
    start     INTEGER NOT NULL CHECK (start >= 0),
    end       INTEGER NOT NULL CHECK (end > start)
);

CREATE TABLE IF NOT EXISTS exits (
    node      INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS groups (
    number              INTEGER PRIMARY KEY,
    people              INTEGER NOT NULL CHECK (people >= 1),
    source              INTEGER NOT NULL,
    earliest_departure  INTEGER NOT NULL CHECK (earliest_departure >= 0),
    priority            INTEGER NOT NULL,
    deadline            INTEGER NOT NULL CHECK (deadline >= earliest_departure)
);

CREATE TABLE IF NOT EXISTS plans (
    group_number  INTEGER PRIMARY KEY REFERENCES groups(number),
    departure     INTEGER,
    arrival       INTEGER,
    exit_node     INTEGER,
    route         TEXT,
    feasible      INTEGER NOT NULL DEFAULT 0,
    reason        TEXT
);

CREATE TABLE IF NOT EXISTS clock (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    minute  INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO clock (id, minute) VALUES (1, 0);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    db_path = db_path or os.environ.get("EVAC_DB_PATH", "/data/evacuation.db")
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)

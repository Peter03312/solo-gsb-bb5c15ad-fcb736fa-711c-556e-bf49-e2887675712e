"""Demo data loaded on first start when the database is empty."""
from __future__ import annotations

import sqlite3

# Positions:
#   1          <- group sources live around the left side
#  / \
# 2   3
# |   |
# 4   5(exit)
#  \ /
#   6(exit)
SEED_CORRIDORS = [
    # number, source, target, travel minutes, people/minute
    (1, 1, 2, 2, 20),
    (2, 1, 3, 1, 30),
    (3, 3, 5, 3, 25),
    (4, 2, 4, 2, 20),
    (5, 4, 5, 1, 40),
    (6, 4, 6, 2, 20),
    (7, 5, 6, 1, 40),
    (8, 2, 6, 4, 15),
]

# corridor, [start, end)
SEED_CLOSURES = [
    (3, 10, 15),
]

SEED_EXITS = [5, 6]

# number, people, source, earliest departure, priority, deadline
SEED_GROUPS = [
    (1, 25, 1, 0, 10, 30),
    (2, 20, 4, 0, 5, 20),
    (3, 15, 3, 2, 5, 20),
]


def seed_if_empty(conn: sqlite3.Connection) -> bool:
    import os

    if os.environ.get("EVAC_SEED", "1") == "0":
        return False
    count = conn.execute("SELECT COUNT(*) FROM corridors").fetchone()[0]
    if count:
        return False
    conn.executemany(
        "INSERT INTO corridors (number, source, target, minutes, capacity) "
        "VALUES (?, ?, ?, ?, ?)",
        SEED_CORRIDORS,
    )
    conn.executemany(
        "INSERT INTO closures (corridor, start, end) VALUES (?, ?, ?)",
        SEED_CLOSURES,
    )
    conn.executemany("INSERT INTO exits (node) VALUES (?)", [(n,) for n in SEED_EXITS])
    conn.executemany(
        "INSERT INTO groups (number, people, source, earliest_departure, "
        "priority, deadline) VALUES (?, ?, ?, ?, ?, ?)",
        SEED_GROUPS,
    )
    return True

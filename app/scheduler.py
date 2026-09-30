"""Evacuation scheduling core.

Each group is scheduled by a time-layered graph search over discrete minutes:
being at node ``u`` at minute ``m`` means the group may enter an outgoing
corridor whose whole occupancy window ``[m, m + travel)`` is free (capacity and
closures), and it may wait at its source for one minute.  Only simple paths are
kept.  Layers are processed in increasing minute order, so the first layer that
reaches any exit is the earliest arrival; ties resolve by exit number then the
corridor-number sequence, exactly as required.
"""
from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field

GROUP_ORDER = "-priority, earliest, number"


class ConflictError(Exception):
    """A change touches the remaining route of a departed group."""


class NotFoundError(Exception):
    """A referenced corridor/group does not exist."""


@dataclass(frozen=True)
class Corridor:
    number: int
    source: int
    target: int
    minutes: int
    capacity: int


@dataclass(frozen=True)
class Group:
    number: int
    people: int
    source: int
    earliest_departure: int
    priority: int
    deadline: int


@dataclass
class Plan:
    feasible: bool
    departure: int | None = None
    arrival: int | None = None
    exit_node: int | None = None
    route: tuple[int, ...] = ()
    reason: str | None = None

    def to_row(self) -> dict:
        return {
            "departure": self.departure,
            "arrival": self.arrival,
            "exit_node": self.exit_node,
            "route": json.dumps(list(self.route)),
            "feasible": 1 if self.feasible else 0,
            "reason": self.reason,
        }


@dataclass
class State:
    now: int
    corridors: dict[int, Corridor]
    closures: dict[int, list[tuple[int, int]]]
    exits: set[int]
    groups: dict[int, Group]
    adj: dict[int, list[Corridor]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# State loading
# ---------------------------------------------------------------------------

def load_state(conn: sqlite3.Connection) -> State:
    now = conn.execute("SELECT minute FROM clock WHERE id = 1").fetchone()[0]

    corridors: dict[int, Corridor] = {}
    for r in conn.execute("SELECT * FROM corridors"):
        corridors[r["number"]] = Corridor(
            r["number"], r["source"], r["target"], r["minutes"], r["capacity"]
        )

    closures: dict[int, list[tuple[int, int]]] = {}
    for r in conn.execute("SELECT corridor, start, end FROM closures ORDER BY start"):
        closures.setdefault(r["corridor"], []).append((r["start"], r["end"]))

    exits = {r[0] for r in conn.execute("SELECT node FROM exits")}

    groups: dict[int, Group] = {}
    for r in conn.execute("SELECT * FROM groups"):
        groups[r["number"]] = Group(
            r["number"],
            r["people"],
            r["source"],
            r["earliest_departure"],
            r["priority"],
            r["deadline"],
        )

    adj: dict[int, list[Corridor]] = {}
    for c in corridors.values():
        adj.setdefault(c.source, []).append(c)
    for edges in adj.values():
        edges.sort(key=lambda c: c.number)

    return State(now, corridors, closures, exits, groups, adj)


def _plan_from_row(row: sqlite3.Row, corridors: dict[int, Corridor]) -> Plan:
    route = tuple(json.loads(row["route"])) if row["route"] else ()
    return Plan(
        feasible=bool(row["feasible"]),
        departure=row["departure"],
        arrival=row["arrival"],
        exit_node=row["exit_node"],
        route=route,
        reason=row["reason"],
    )


def load_plans(conn: sqlite3.Connection, corridors: dict[int, Corridor]) -> dict[int, Plan]:
    plans: dict[int, Plan] = {}
    for r in conn.execute("SELECT * FROM plans"):
        plans[r["group_number"]] = _plan_from_row(r, corridors)
    return plans


# ---------------------------------------------------------------------------
# Occupancy helpers
# ---------------------------------------------------------------------------

def _leg_intervals(plan: Plan, state: State):
    """Yield (corridor_number, entry, exit) for every leg of a feasible plan."""
    entry = plan.departure
    for number in plan.route:
        c = state.corridors[number]
        yield number, entry, entry + c.minutes
        entry += c.minutes


def build_used(conn: sqlite3.Connection, state: State) -> dict[tuple[int, int], int]:
    """Occupancy fixed by groups that have departed at the current minute."""
    used: dict[tuple[int, int], int] = {}
    for r in conn.execute("SELECT * FROM plans WHERE feasible = 1"):
        plan = _plan_from_row(r, state.corridors)
        if plan.departure is None or plan.departure >= state.now:
            continue  # not departed yet: it will be rescheduled
        group = state.groups[r["group_number"]]
        for number, entry, exit_time in _leg_intervals(plan, state):
            for minute in range(entry, exit_time):
                used[(number, minute)] = used.get((number, minute), 0) + group.people
    return used


def add_plan_occupancy(used: dict[tuple[int, int], int], plan: Plan, group: Group,
                       state: State) -> None:
    for number, entry, exit_time in _leg_intervals(plan, state):
        for minute in range(entry, exit_time):
            used[(number, minute)] = used.get((number, minute), 0) + group.people


def closure_touches_departed(corridor_number: int, start: int, end: int,
                             state: State, plans: dict[int, Plan]) -> int | None:
    """Return the group number whose departed remaining route is touched, else None."""
    for number, plan in plans.items():
        if not plan.feasible or plan.departure is None or plan.departure >= state.now:
            continue
        group = state.groups[number]
        for cnum, entry, exit_time in _leg_intervals(plan, state):
            if cnum != corridor_number or exit_time <= state.now:
                continue  # leg fully in the past: not part of the remaining route
            remaining_start = max(entry, state.now)
            if remaining_start < end and start < exit_time:
                return group.number
    return None


# ---------------------------------------------------------------------------
# Single-group search
# ---------------------------------------------------------------------------

def _reachable(state: State, source: int, people: int) -> bool:
    """Static reachability of some exit ignoring time (used for failure reason)."""
    seen = {source}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        if node in state.exits:
            return True
        for c in state.adj.get(node, ()):
            if c.capacity < people or c.target in seen:
                continue
            seen.add(c.target)
            queue.append(c.target)
    return False


def _edge_free(corridor: Corridor, entry: int, people: int, state: State,
               used: dict[tuple[int, int], int]) -> bool:
    exit_time = entry + corridor.minutes
    for minute in range(entry, exit_time):
        if used.get((corridor.number, minute), 0) + people > corridor.capacity:
            return False
    for cstart, cend in state.closures.get(corridor.number, ()):
        if cstart < exit_time and entry < cend:
            return False
    return True


def find_plan(group: Group, state: State, used: dict[tuple[int, int], int]) -> Plan:
    """Earliest-arrival plan for one group against fixed occupancy in ``used``."""
    earliest = max(state.now, group.earliest_departure)
    if earliest > group.deadline:
        return Plan(False, reason=(
            f"earliest possible departure minute {earliest} is later than "
            f"deadline minute {group.deadline}"
        ))

    if not _reachable(state, group.source, group.people):
        return Plan(False, reason=(
            "no directed route from source to any exit using corridors with "
            f"capacity >= {group.people}"
        ))

    # Time-layered label-setting search.
    # layers[minute][node] holds a Pareto frontier of labels; a label is
    # (corridor sequence, departure minute or None, visited node set).
    # Label A dominates B when A has the smaller-or-equal corridor sequence and
    # visits a subset of B's nodes (it can take every extension B can take).
    layers: dict[int, dict[int, list]] = {
        earliest: {group.source: [((), None, frozenset({group.source}))]}
    }

    def add_label(labels, seq, departure, visited):
        for s2, _, v2 in labels:
            if s2 <= seq and v2 <= visited:
                return  # existing label dominates
        labels[:] = [l for l in labels if not (seq <= l[0] and visited <= l[2])]
        labels.append((seq, departure, visited))

    for minute in range(earliest, group.deadline + 1):
        layer = layers.get(minute)
        if not layer:
            continue

        # First minute with any exit label is the earliest arrival; ties are
        # resolved by exit node, then corridor-number sequence.
        reached = [
            (node, label)
            for node, labels in layer.items() if node in state.exits
            for label in labels
        ]
        if reached:
            node, (seq, departure, _) = min(reached, key=lambda x: (x[0], x[1][0]))
            return Plan(
                feasible=True,
                departure=departure if departure is not None else minute,
                arrival=minute,
                exit_node=node,
                route=seq,
            )

        for node, labels in list(layer.items()):
            for seq, departure, visited in labels:
                # Waiting is only meaningful at the source before departure.
                if node == group.source and not seq and minute < group.deadline:
                    bucket = layers.setdefault(minute + 1, {})
                    add_label(bucket.setdefault(group.source, []),
                              seq, departure, visited)

                if node in state.exits:
                    continue

                for corridor in state.adj.get(node, ()):
                    if corridor.capacity < group.people or corridor.target in visited:
                        continue
                    exit_time = minute + corridor.minutes
                    if exit_time > group.deadline:
                        continue
                    if not _edge_free(corridor, minute, group.people, state, used):
                        continue
                    bucket = layers.setdefault(exit_time, {})
                    add_label(
                        bucket.setdefault(corridor.target, []),
                        seq + (corridor.number,),
                        departure if departure is not None else minute,
                        visited | {corridor.target},
                    )

    return Plan(False, reason=(
        f"no feasible route arriving no later than deadline minute "
        f"{group.deadline} (capacity or closures block every departure)"
    ))


# ---------------------------------------------------------------------------
# Atomic (re)scheduling
# ---------------------------------------------------------------------------

def _ordered_undeparted(state: State, plans: dict[int, Plan]) -> list[Group]:
    groups = [
        g for g in state.groups.values()
        if g.number not in plans
        or not plans[g.number].feasible
        or plans[g.number].departure is None
        or plans[g.number].departure >= state.now
    ]
    groups.sort(key=lambda g: (-g.priority, g.earliest_departure, g.number))
    return groups


def reschedule(conn: sqlite3.Connection) -> State:
    """Recompute plans of every not-yet-departed group in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = load_state(conn)
        old_plans = load_plans(conn, state.corridors)

        # Plans belonging to departed groups are immutable; keep their occupancy.
        kept: dict[int, Plan] = {}
        for number, plan in old_plans.items():
            if (plan.feasible and plan.departure is not None
                    and plan.departure < state.now):
                kept[number] = plan
        used = build_used(conn, state)

        new_plans = dict(kept)
        for group in _ordered_undeparted(state, old_plans):
            if group.deadline < state.now:
                new_plans[group.number] = Plan(False, reason=(
                    f"deadline minute {group.deadline} has passed "
                    f"(current minute {state.now})"
                ))
            else:
                plan = find_plan(group, state, used)
                if plan.feasible:
                    add_plan_occupancy(used, plan, group, state)
                new_plans[group.number] = plan

        conn.execute("DELETE FROM plans")
        for number, plan in new_plans.items():
            row = plan.to_row()
            conn.execute(
                "INSERT INTO plans (group_number, departure, arrival, exit_node, "
                "route, feasible, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (number, row["departure"], row["arrival"], row["exit_node"],
                 row["route"], row["feasible"], row["reason"]),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return state


# ---------------------------------------------------------------------------
# Mutating operations (each commits or rejects atomically together with reschedule)
# ---------------------------------------------------------------------------

def add_closure(conn: sqlite3.Connection, corridor: int, start: int, end: int) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = load_state(conn)
        if corridor not in state.corridors:
            raise NotFoundError(f"corridor {corridor} does not exist")
        plans = load_plans(conn, state.corridors)
        hit = closure_touches_departed(corridor, start, end, state, plans)
        if hit is not None:
            raise ConflictError(
                f"closure [{start}, {end}) of corridor {corridor} touches the "
                f"remaining route of departed group {hit}; whole change rejected"
            )
        cur = conn.execute(
            "INSERT INTO closures (corridor, start, end) VALUES (?, ?, ?)",
            (corridor, start, end),
        )
        closure_id = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    reschedule(conn)
    return closure_id


def add_exit(conn: sqlite3.Connection, node: int) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT OR IGNORE INTO exits (node) VALUES (?)", (node,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    reschedule(conn)


def add_group(conn: sqlite3.Connection, number: int, people: int, source: int,
              earliest: int, priority: int, deadline: int) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM groups WHERE number = ?", (number,)).fetchone():
            raise ConflictError(f"group {number} already exists")
        conn.execute(
            "INSERT INTO groups (number, people, source, earliest_departure, "
            "priority, deadline) VALUES (?, ?, ?, ?, ?, ?)",
            (number, people, source, earliest, priority, deadline),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    reschedule(conn)


def advance_clock(conn: sqlite3.Connection, minutes: int) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = conn.execute("SELECT minute FROM clock WHERE id = 1").fetchone()[0]
        now += minutes
        conn.execute("UPDATE clock SET minute = ? WHERE id = 1", (now,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    reschedule(conn)
    return now

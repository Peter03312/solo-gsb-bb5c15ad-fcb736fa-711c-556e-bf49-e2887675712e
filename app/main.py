"""FastAPI application: venue evacuation channel scheduling service."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import scheduler
from .database import connect, init_db
from .models import AdvanceIn, ClosureIn, CorridorIn, ExitIn, GroupIn
from .seed import seed_if_empty

DB_LOCK = threading.RLock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.conn = connect()
    init_db(app.state.conn)
    with DB_LOCK:
        app.state.seeded = seed_if_empty(app.state.conn)
        scheduler.reschedule(app.state.conn)
    yield
    app.state.conn.close()


app = FastAPI(
    title="场馆疏散通道编排服务",
    version="1.0.0",
    description=(
        "按优先级逐组安排最早到达的疏散路线。封闭时段为左闭右开区间；"
        "当前分钟推进后已出发组占用固定；触及已出发组剩余路线的封闭整次拒绝。"
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@app.exception_handler(scheduler.ConflictError)
async def _conflict_handler(request, exc):
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(scheduler.NotFoundError)
async def _not_found_handler(request, exc):
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _network_payload(state: scheduler.State) -> dict:
    return {
        "current_minute": state.now,
        "corridors": [
            {
                "corridor": c.number,
                "source": c.source,
                "target": c.target,
                "travel_minutes": c.minutes,
                "capacity_per_minute": c.capacity,
                "closures": [
                    {"start": s, "end": e} for s, e in state.closures.get(c.number, [])
                ],
            }
            for c in sorted(state.corridors.values(), key=lambda x: x.number)
        ],
        "exits": sorted(state.exits),
        "groups": [
            {
                "group": g.number,
                "people": g.people,
                "source": g.source,
                "earliest_departure": g.earliest_departure,
                "priority": g.priority,
                "deadline": g.deadline,
            }
            for g in sorted(
                state.groups.values(),
                key=lambda x: (-x.priority, x.earliest_departure, x.number),
            )
        ],
    }


def _plan_payload(state: scheduler.State, number: int, plan: scheduler.Plan,
                  order: int) -> dict:
    group = state.groups[number]
    departed = bool(plan.feasible and plan.departure is not None
                    and plan.departure < state.now)
    payload = {
        "group": number,
        "schedule_order": order,
        "priority": group.priority,
        "people": group.people,
        "status": "SCHEDULED" if plan.feasible else "UNSCHEDULABLE",
        "departed": departed,
        "departure_minute": plan.departure,
        "arrival_minute": plan.arrival,
        "exit": plan.exit_node,
        "route_corridors": list(plan.route),
        "reason": plan.reason,
    }
    if plan.feasible:
        legs = []
        entry = plan.departure
        for cnum in plan.route:
            c = state.corridors[cnum]
            legs.append({
                "corridor": cnum,
                "from": c.source,
                "to": c.target,
                "enter_minute": entry,
                "leave_minute": entry + c.minutes,
                "occupies_minutes": list(range(entry, entry + c.minutes)),
            })
            entry += c.minutes
        payload["legs"] = legs
    return payload


def _all_plans_payload(conn, state: scheduler.State) -> dict:
    plans = scheduler.load_plans(conn, state.corridors)
    order_numbers = [g.number for g in _order_groups(state)]
    # departed groups first (already fixed), in departure order, then pending
    departed = sorted(
        (n for n, p in plans.items()
         if p.feasible and p.departure is not None and p.departure < state.now),
        key=lambda n: (plans[n].departure, n),
    )
    pending = [n for n in order_numbers if n not in departed]
    sequence = departed + pending
    return {
        "current_minute": state.now,
        "plans": [
            _plan_payload(state, n, plans[n], order_numbers.index(n))
            for n in sequence
        ],
    }


def _order_groups(state: scheduler.State):
    groups = list(state.groups.values())
    groups.sort(key=lambda g: (-g.priority, g.earliest_departure, g.number))
    return groups


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


@app.get("/api/state", tags=["query"])
def get_state():
    with DB_LOCK:
        conn = app.state.conn
        state = scheduler.load_state(conn)
        payload = _network_payload(state)
        payload.update(_all_plans_payload(conn, state))
        return payload


@app.get("/api/plans", tags=["query"])
def get_plans():
    with DB_LOCK:
        state = scheduler.load_state(app.state.conn)
        return _all_plans_payload(app.state.conn, state)


@app.get("/api/groups/{number}/plan", tags=["query"])
def get_group_plan(number: int):
    with DB_LOCK:
        conn = app.state.conn
        state = scheduler.load_state(conn)
        if number not in state.groups:
            raise HTTPException(404, f"group {number} does not exist")
        row = conn.execute(
            "SELECT * FROM plans WHERE group_number = ?", (number,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"no plan for group {number}")
        order_numbers = [g.number for g in _order_groups(state)]
        order = order_numbers.index(number)
        return _plan_payload(state, number, scheduler._plan_from_row(row, state.corridors), order)  # noqa: SLF001


@app.post("/api/corridors", status_code=201, tags=["network"])
def create_corridors(items: list[CorridorIn] | CorridorIn):
    """Add corridor(s). Accepts one object or a list; the batch is atomic."""
    many = items if isinstance(items, list) else [items]
    with DB_LOCK:
        conn = app.state.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = {r[0] for r in conn.execute("SELECT number FROM corridors")}
            seen: set[int] = set()
            for item in many:
                if item.number in existing or item.number in seen:
                    raise scheduler.ConflictError(
                        f"corridor {item.number} already exists"
                    )
                seen.add(item.number)
                conn.execute(
                    "INSERT INTO corridors (number, source, target, minutes, capacity)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (item.number, item.source, item.target, item.minutes, item.capacity),
                )
                for win in item.closures:
                    conn.execute(
                        "INSERT INTO closures (corridor, start, end) VALUES (?, ?, ?)",
                        (item.number, win.start, win.end),
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        scheduler.reschedule(conn)
        state = scheduler.load_state(conn)
        return JSONResponse(status_code=201, content=_network_payload(state))


@app.post("/api/closures", status_code=201, tags=["network"])
def create_closure(item: ClosureIn):
    """Add a [start, end) closure; rejected (409) if it touches a departed route."""
    with DB_LOCK:
        closure_id = scheduler.add_closure(
            app.state.conn, item.corridor, item.start, item.end
        )
        state = scheduler.load_state(app.state.conn)
        return JSONResponse(
            status_code=201,
            content={"id": closure_id, **_all_plans_payload(app.state.conn, state)},
        )


@app.post("/api/exits", status_code=201, tags=["network"])
def create_exit(item: ExitIn):
    with DB_LOCK:
        scheduler.add_exit(app.state.conn, item.node)
        state = scheduler.load_state(app.state.conn)
        return JSONResponse(status_code=201, content=_network_payload(state))


@app.post("/api/groups", status_code=201, tags=["network"])
def create_group(item: GroupIn):
    """Register a new group; all not-yet-departed groups are rescheduled."""
    with DB_LOCK:
        scheduler.add_group(
            app.state.conn,
            item.number,
            item.people,
            item.source,
            item.earliest_departure,
            item.priority,
            item.deadline,
        )
        state = scheduler.load_state(app.state.conn)
        return JSONResponse(
            status_code=201,
            content={
                **_network_payload(state),
                **_all_plans_payload(app.state.conn, state),
            },
        )


@app.post("/api/clock/advance", tags=["control"])
def advance(item: AdvanceIn):
    """Advance the current minute by N. Departed routes become immutable."""
    with DB_LOCK:
        now = scheduler.advance_clock(app.state.conn, item.minutes)
        state = scheduler.load_state(app.state.conn)
        return {"current_minute": now, **_all_plans_payload(app.state.conn, state)}

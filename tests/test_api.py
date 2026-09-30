"""End-to-end tests for scheduling rules and HTTP API behaviour."""
from concurrent.futures import ThreadPoolExecutor

from conftest import load_seed


def _plan_by_group(payload):
    return {p["group"]: p for p in payload["plans"]}


def _usage(data):
    """Aggregate (corridor, minute) -> people across all scheduled groups."""
    usage = {}
    for p in data["plans"]:
        for leg in p.get("legs", []):
            for minute in leg["occupies_minutes"]:
                key = (leg["corridor"], minute)
                usage[key] = usage.get(key, 0) + p["people"]
    return usage


# ---------------------------------------------------------------------------
# Seed scenario
# ---------------------------------------------------------------------------

def test_seed_plans_follow_priority_and_tie_break(client):
    load_seed(client)
    data = client.get("/api/plans").json()
    plans = _plan_by_group(data)

    # G1 (priority 10, 25 people): c1/c4 carry only 20/min so it must use
    # c2,c3 to exit 5; departure 0, arrival 4.
    g1 = plans[1]
    assert g1["status"] == "SCHEDULED"
    assert (g1["departure_minute"], g1["arrival_minute"], g1["exit"]) == (0, 4, 5)
    assert g1["route_corridors"] == [2, 3]

    # G2 (source 4): arrives exit 5 at minute 1 via c5 (exit number 5 < 6).
    g2 = plans[2]
    assert (g2["departure_minute"], g2["arrival_minute"], g2["exit"]) == (0, 1, 5)
    assert g2["route_corridors"] == [5]

    # G3 (earliest 2): c3 occupied by G1 until 4, so departs 4, arrives 7.
    g3 = plans[3]
    assert g3["status"] == "SCHEDULED"
    assert (g3["departure_minute"], g3["arrival_minute"], g3["exit"]) == (4, 7, 5)
    assert g3["route_corridors"] == [3]


def test_capacity_and_closure_invariants_hold(client):
    load_seed(client)
    data = client.get("/api/state").json()
    caps = {c["corridor"]: c["capacity_per_minute"] for c in data["corridors"]}
    closures = {c["corridor"]: c["closures"] for c in data["corridors"]}

    for (corridor, minute), people in _usage(data).items():
        assert people <= caps[corridor]
        for win in closures[corridor]:
            assert not (win["start"] <= minute < win["end"])


def test_closure_touching_departed_remaining_route_is_rejected(client):
    load_seed(client)
    # Advance to minute 1: G1 (dep 0) is now departed and occupies c3 on
    # minutes 1,2,3; closure [3,6) overlaps minute 3 -> reject.
    client.post("/api/clock/advance", json={"minutes": 1})
    resp = client.post("/api/closures", json={"corridor": 3, "start": 3, "end": 6})
    assert resp.status_code == 409
    assert "departed group 1" in resp.json()["detail"]

    # [4,7) starts exactly when G1 leaves c3 (half-open boundary): it does not
    # touch G1. G3 has not departed (dep >= now), so it gets rescheduled:
    # c3 minutes 4..6 closed, minutes 7..9 free (seeded closure starts at 10),
    # hence G3 enters c3 at minute 7 and arrives at 10.
    resp = client.post("/api/closures", json={"corridor": 3, "start": 4, "end": 7})
    assert resp.status_code == 201
    plans = _plan_by_group(resp.json())
    assert plans[1]["route_corridors"] == [2, 3]  # departed plan untouched
    assert plans[3]["status"] == "SCHEDULED"
    assert plans[3]["route_corridors"] == [3]
    assert plans[3]["departure_minute"] == 7
    assert plans[3]["arrival_minute"] == 10


def test_half_open_boundary_closure_is_accepted(client):
    load_seed(client)
    # Advance to minute 1: G1 departed at 0 and is inside c3, which it occupies
    # on minutes [1,4) (travel 3). G2's c5 occupancy [0,1) is already past.
    client.post("/api/clock/advance", json={"minutes": 1})

    # A closure fully in the past, covering even a minute G2 actually used,
    # does not touch any remaining route.
    resp = client.post("/api/closures", json={"corridor": 5, "start": 0, "end": 1})
    assert resp.status_code == 201

    # A closure ending exactly at the entry minute of G1's remaining leg
    # (half-open boundary, c3 is first occupied on minute 1) does not touch it.
    resp = client.post("/api/closures", json={"corridor": 3, "start": 0, "end": 1})
    assert resp.status_code == 201

    # A closure starting exactly when G1 leaves c3 (minute 4) is also safe.
    resp = client.post("/api/closures", json={"corridor": 3, "start": 4, "end": 9})
    assert resp.status_code == 201

    # Any overlap with minutes 1..3 touches the remaining route -> rejected.
    resp = client.post("/api/closures", json={"corridor": 3, "start": 3, "end": 6})
    assert resp.status_code == 409
    assert "departed group 1" in resp.json()["detail"]


def test_advance_clock_fixes_departed_groups(client):
    load_seed(client)
    client.post("/api/clock/advance", json={"minutes": 5})
    # now == 5: every seed group has departure < 5, so all are departed/fixed.
    data = client.get("/api/plans").json()
    assert all(p["departed"] for p in data["plans"])

    # G1 finished c3 at minute 4, but G3 is inside c3 on minutes 4..7 and must
    # reject a closure that overlaps those minutes.
    resp = client.post("/api/closures", json={"corridor": 3, "start": 5, "end": 8})
    assert resp.status_code == 409
    assert "departed group 3" in resp.json()["detail"]

    # A closure fully in the past (including yesterday's completed minute 4)
    # never touches a remaining route and is always accepted.
    resp = client.post("/api/closures", json={"corridor": 3, "start": 0, "end": 1})
    assert resp.status_code == 201
    resp = client.post("/api/closures", json={"corridor": 3, "start": 4, "end": 5})
    assert resp.status_code == 201


def test_new_closure_reschedules_only_undeparted(client):
    load_seed(client)
    # At minute 0 all groups are still reschedulable. Close c3 minutes 7..12
    # (which does not affect the G1 occupancy on minutes 1..3, but changes G3).
    resp = client.post("/api/closures", json={"corridor": 3, "start": 7, "end": 12})
    assert resp.status_code == 201
    plans = _plan_by_group(resp.json())
    # G1's earliest-arrival plan is unchanged (c3 used at minutes 1..3, free):
    assert plans[1]["route_corridors"] == [2, 3]
    # G3 still uses c3 at minutes 4..6, arrival 7 — before the new closure.
    assert plans[3]["arrival_minute"] == 7


# ---------------------------------------------------------------------------
# Custom scenarios
# ---------------------------------------------------------------------------

def _new_network(client, corridors, exits):
    payload = [
        {"corridor": n, "source": s, "target": t,
         "travel_minutes": m, "capacity": c}
        for n, s, t, m, c in corridors
    ]
    assert client.post("/api/corridors", json=payload).status_code == 201
    for node in exits:
        assert client.post("/api/exits", json={"node": node}).status_code == 201


def _add_group(client, number, people, source, earliest, priority, deadline):
    return client.post("/api/groups", json={
        "group": number, "people": people, "source": source,
        "earliest": earliest, "priority": priority, "deadline": deadline})


def test_priority_group_preempts_capacity(client):
    # Two parallel 2-minute lanes, each 5/min.
    _new_network(client, [(1, 0, 9, 2, 5), (2, 0, 9, 2, 5)], [9])
    _add_group(client, 1, 5, 0, 0, 1, 20)
    assert _plan_by_group(client.get("/api/plans").json())[1]["route_corridors"] == [1]

    # New higher-priority group claims lane c1 at minute 0 (the preferred
    # corridor number); G1 keeps its earliest arrival minute 2 by taking c2.
    _add_group(client, 2, 5, 0, 0, 9, 20)
    plans = _plan_by_group(client.get("/api/plans").json())
    assert plans[2]["route_corridors"] == [1]
    assert plans[2]["departure_minute"] == 0
    assert plans[2]["arrival_minute"] == 2
    assert plans[1]["route_corridors"] == [2]
    assert plans[1]["departure_minute"] == 0
    assert plans[1]["arrival_minute"] == 2


def test_unschedulable_when_group_bigger_than_capacity(client):
    _new_network(client, [(1, 0, 9, 1, 3)], [9])
    _add_group(client, 1, 5, 0, 0, 1, 5)
    g1 = _plan_by_group(client.get("/api/plans").json())[1]
    assert g1["status"] == "UNSCHEDULABLE"
    assert "no directed route" in g1["reason"]


def test_deadline_failure_reason(client):
    _new_network(client, [(1, 0, 9, 5, 50)], [9])
    _add_group(client, 1, 1, 0, 0, 1, 3)
    g1 = _plan_by_group(client.get("/api/plans").json())[1]
    assert g1["status"] == "UNSCHEDULABLE"
    assert "deadline" in g1["reason"]


def test_tie_break_exit_number_then_corridor_sequence(client):
    # Same arrival minute to exits 5 and 6 -> exit 5 wins regardless of cnum.
    _new_network(client, [(8, 0, 5, 1, 10), (1, 0, 6, 1, 10)], [6, 5])
    _add_group(client, 1, 1, 0, 0, 1, 10)
    g1 = _plan_by_group(client.get("/api/plans").json())[1]
    assert (g1["exit"], g1["route_corridors"]) == (5, [8])


def test_same_priority_orders_by_earliest_then_group_number(client):
    _new_network(client, [(1, 0, 9, 1, 1)], [9])
    _add_group(client, 2, 1, 0, 5, 1, 20)
    _add_group(client, 1, 1, 0, 0, 1, 20)
    data = client.get("/api/plans").json()
    # Earlier earliest-departure is scheduled first even though group 2 existed.
    assert [p["group"] for p in data["plans"]] == [1, 2]
    plans = _plan_by_group(data)
    assert plans[1]["departure_minute"] == 0
    assert plans[2]["departure_minute"] == 5  # its earliest-departure constraint


def test_same_priority_same_earliest_orders_by_group_number(client):
    _new_network(client, [(1, 0, 9, 1, 1)], [9])
    _add_group(client, 7, 1, 0, 0, 1, 20)
    _add_group(client, 3, 1, 0, 0, 1, 20)
    data = client.get("/api/plans").json()
    assert [p["group"] for p in data["plans"]] == [3, 7]
    plans = _plan_by_group(data)
    assert plans[3]["departure_minute"] == 0
    assert plans[7]["departure_minute"] == 1


def test_indivisible_group_never_shares_beyond_capacity(client):
    # One corridor of 5/min; a group of 5 must occupy each minute alone.
    _new_network(client, [(1, 0, 9, 1, 5)], [9])
    _add_group(client, 1, 5, 0, 0, 1, 5)
    _add_group(client, 2, 4, 0, 0, 1, 5)
    plans = _plan_by_group(client.get("/api/plans").json())
    assert plans[1]["departure_minute"] == 0
    assert plans[2]["departure_minute"] == 1  # never mixed to 9 on one minute


def test_indivisible_group_is_marked_unschedulable_if_too_big(client):
    _new_network(client, [(1, 0, 9, 1, 5), (2, 0, 9, 1, 5)], [9])
    _add_group(client, 1, 6, 0, 0, 1, 5)
    g1 = _plan_by_group(client.get("/api/plans").json())[1]
    assert g1["status"] == "UNSCHEDULABLE"  # not split 5+1 across the lanes


def test_batch_corridor_creation_is_atomic(client):
    resp = client.post("/api/corridors", json=[
        {"corridor": 70, "source": 1, "target": 2, "travel_minutes": 1, "capacity": 5},
        {"corridor": 70, "source": 3, "target": 4, "travel_minutes": 1, "capacity": 5},
    ])
    assert resp.status_code == 409
    state = client.get("/api/state").json()
    assert 70 not in {c["corridor"] for c in state["corridors"]}


def test_validation_errors(client):
    r = _add_group(client, 9, 1, 0, 10, 1, 3)  # deadline < earliest
    assert r.status_code == 422
    r = client.post("/api/closures", json={"corridor": 99, "start": 5, "end": 2})
    assert r.status_code == 422  # end <= start
    r = client.post("/api/closures", json={"corridor": 99, "start": 0, "end": 5})
    assert r.status_code == 404


def test_concurrent_new_groups_never_overuse_capacity(client):
    _new_network(client, [(1, 0, 9, 1, 1)], [9])

    def add(n):
        return _add_group(client, n, 1, 0, 0, 1, 200).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(add, range(1, 17)))
    assert all(s == 201 for s in statuses)

    data = client.get("/api/state").json()
    for (corridor, minute), people in _usage(data).items():
        assert people <= 1
    assert all(p["status"] == "SCHEDULED" for p in data["plans"])


def test_concurrent_closures_are_serialised_without_corruption(client):
    load_seed(client)
    # Advance to minute 1 so G1 is departed and its c3 occupancy is protected.
    client.post("/api/clock/advance", json={"minutes": 1})

    def close(_):
        return client.post("/api/closures", json={
            "corridor": 3, "start": 3, "end": 6}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(close, range(8)))
    # Exactly one request can insert the closure (then it always "touches");
    # every request either commits its own insert or is rejected as a conflict.
    assert set(statuses) <= {201, 409}
    assert 409 in statuses
    plans = _plan_by_group(client.get("/api/plans").json())
    # Departed G1 plan never changes no matter which request committed.
    assert plans[1]["route_corridors"] == [2, 3]
    assert plans[1]["departure_minute"] == 0

"""Pytest fixtures: each test gets an isolated empty SQLite file."""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("EVAC_DB_PATH", str(db))
    monkeypatch.setenv("EVAC_SEED", "0")

    import app.database
    import app.main
    import app.scheduler
    import app.seed
    importlib.reload(app.database)
    importlib.reload(app.scheduler)
    importlib.reload(app.seed)
    importlib.reload(app.main)

    with TestClient(app.main.app) as c:
        yield c


def load_seed(client):
    """Load the demo network + groups used by the seed-based tests."""
    corridors = [
        {"corridor": 1, "source": 1, "target": 2, "travel_minutes": 2, "capacity": 20},
        {"corridor": 2, "source": 1, "target": 3, "travel_minutes": 1, "capacity": 30},
        {"corridor": 3, "source": 3, "target": 5, "travel_minutes": 3, "capacity": 25},
        {"corridor": 4, "source": 2, "target": 4, "travel_minutes": 2, "capacity": 20},
        {"corridor": 5, "source": 4, "target": 5, "travel_minutes": 1, "capacity": 40},
        {"corridor": 6, "source": 4, "target": 6, "travel_minutes": 2, "capacity": 20},
        {"corridor": 7, "source": 5, "target": 6, "travel_minutes": 1, "capacity": 40},
        {"corridor": 8, "source": 2, "target": 6, "travel_minutes": 4, "capacity": 15},
    ]
    assert client.post("/api/corridors", json=corridors).status_code == 201
    for node in (5, 6):
        assert client.post("/api/exits", json={"node": node}).status_code == 201
    assert client.post("/api/closures", json={
        "corridor": 3, "start": 10, "end": 15}).status_code == 201
    groups = [
        {"group": 1, "people": 25, "source": 1, "earliest": 0,
         "priority": 10, "deadline": 30},
        {"group": 2, "people": 20, "source": 4, "earliest": 0,
         "priority": 5, "deadline": 20},
        {"group": 3, "people": 15, "source": 3, "earliest": 2,
         "priority": 5, "deadline": 20},
    ]
    for g in groups:
        assert client.post("/api/groups", json=g).status_code == 201

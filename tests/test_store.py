import pytest

from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db")


def add(store, event_id="E1", now=100.0, destinations=("crm", "slack")):
    return store.add_event(event_id, "orders", {"id": event_id}, {d: {"to": d} for d in destinations}, now)


def test_an_event_is_stored_with_one_pending_delivery_per_destination(store):
    assert add(store)
    e = store.event("E1")
    assert [(d["destination"], d["status"], d["attempts"]) for d in e["deliveries"]] == [("crm", "pending", 0), ("slack", "pending", 0)]


def test_the_same_event_id_is_stored_only_once(store):
    assert add(store) and not add(store)
    assert len(store.events()) == 1 and len(store.event("E1")["deliveries"]) == 2


def test_due_returns_only_pending_deliveries_whose_time_has_come(store):
    add(store, now=100)
    assert len(store.due(99)) == 0 and len(store.due(100)) == 2
    first = store.due(100)[0]
    store.mark_retry(first["id"], 1, 200, "boom", 503, 100)
    assert [d["destination"] for d in store.due(150)] == ["slack"]
    store.mark_sent(store.due(150)[0]["id"], 1, 200, 150)
    assert store.due(150) == [] and len(store.due(200)) == 1


def test_replay_only_works_on_a_dead_delivery_and_resets_attempts(store):
    add(store)
    d = store.due(100)[0]
    assert not store.replay(d["id"], 300)  # pending
    store.mark_dead(d["id"], 5, "gave up", 503, 200)
    assert d["id"] not in [x["id"] for x in store.due(100)]
    assert store.replay(d["id"], 300)
    again = store.delivery(d["id"])
    assert (again["status"], again["attempts"], again["next_attempt_at"]) == ("pending", 0, 300)
    store.mark_sent(d["id"], 1, 200, 301)
    assert not store.replay(d["id"], 400)  # sent


def test_events_are_newest_first_and_limited(store):
    for i, t in enumerate([100, 300, 200]):
        add(store, f"E{i}", now=t)
    assert [e["id"] for e in store.events()] == ["E1", "E2", "E0"]
    assert len(store.events(limit=2)) == 2


def test_a_failed_insert_leaves_nothing_behind(store):
    store.add_event("E1", "orders", {}, {"crm": {}}, 1)
    with pytest.raises(Exception):
        store.add_event("E2", "orders", {}, {"crm": {}, "bad": object()}, 1)  # payload cannot be stored
    assert store.event("E2") is None

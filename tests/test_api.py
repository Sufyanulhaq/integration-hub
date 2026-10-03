import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app, order_body
from tests.conftest import SECRET, START, signed


@pytest.fixture
def client(app):
    return TestClient(app)


def post(client, clock, payload, **kw):
    body, headers = signed(payload, clock, **kw)
    return client.post("/webhooks/orders", content=body, headers=headers)


def statuses(client, event_id):
    return {d["destination"]: d["status"] for d in client.get(f"/api/events/{event_id}").json()["deliveries"]}


def test_a_signed_order_is_accepted_and_queued_for_all_four_systems(client, clock):
    r = post(client, clock, order_body("ORD-1"))
    assert r.status_code == 202 and r.json() == {"status": "accepted", "event_id": "ORD-1"}
    assert statuses(client, "ORD-1") == {"crm": "pending", "accounting": "pending", "slack": "pending", "sheets": "pending"}


def test_nothing_is_sent_until_the_queue_runs_and_then_each_system_gets_its_own_shape(client, clock, world):
    post(client, clock, order_body("ORD-1"))
    assert all(not rows for rows in world.received.values())
    assert client.post("/api/run").json() == {"attempted": 4}
    assert statuses(client, "ORD-1") == {"crm": "sent", "accounting": "sent", "slack": "sent", "sheets": "sent"}
    assert world.received["crm"][0]["email"] == "priya.nair@example.com"
    assert world.received["accounting"][0]["lines"][0]["code"] == "DESK-120"
    assert world.received["slack"][0]["text"].startswith("New order ORD-1")


def test_the_same_order_sent_twice_is_delivered_once(client, clock, world):
    assert post(client, clock, order_body("ORD-1")).status_code == 202
    again = post(client, clock, order_body("ORD-1"))
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    client.post("/api/run")
    assert len(world.received["crm"]) == 1 and len(client.get("/api/events").json()) == 1


def test_a_forged_signature_is_refused_and_nothing_is_stored(client, clock):
    r = post(client, clock, order_body("ORD-1"), secret="not-the-secret")
    assert r.status_code == 401 and "does not match" in r.json()["error"]
    assert client.get("/api/events").json() == []


def test_missing_headers_and_stale_requests_are_refused(client, clock):
    body = json.dumps(order_body("ORD-1")).encode()
    assert client.post("/webhooks/orders", content=body).status_code == 401
    assert post(client, clock, order_body("ORD-1"), timestamp=START - 3600).status_code == 401


def test_a_captured_request_cannot_be_replayed_later(client, clock):
    body, headers = signed(order_body("ORD-1"), clock)
    clock.advance(301)
    r = client.post("/webhooks/orders", content=body, headers=headers)
    assert r.status_code == 401 and "too old" in r.json()["error"]


def test_a_tampered_body_is_refused(client, clock):
    body, headers = signed(order_body("ORD-1"), clock)
    r = client.post("/webhooks/orders", content=body.replace(b"349.0", b"1.0"), headers=headers)
    assert r.status_code == 401


def test_signed_garbage_is_a_400(client, clock):
    assert post(client, clock, b"not json at all").status_code == 400


def test_a_bad_field_is_a_422_that_names_the_field(client, clock):
    bad = order_body("ORD-1")
    bad["customer"]["email"] = "nope"
    r = post(client, clock, bad)
    assert r.status_code == 422 and r.json()["error"] == "invalid order"
    assert any(p.startswith("customer.email") for p in r.json()["problems"])
    assert client.get("/api/events").json() == []


def test_a_total_that_does_not_add_up_is_a_422_with_the_right_figure(client, clock):
    bad = order_body("ORD-1")
    bad["total"] = 5.0
    r = post(client, clock, bad)
    assert r.status_code == 422 and "items, which add up to 362.0" in " ".join(r.json()["problems"])
    assert client.get("/api/events").json() == []


def test_an_oversized_body_is_a_413(client, clock):
    assert post(client, clock, b"x" * (256 * 1024 + 1)).status_code == 413


def test_a_flaky_system_is_retried_with_backoff_while_the_others_finish(client, clock, world):
    world.fail_next("crm", 2)
    post(client, clock, order_body("ORD-1"))
    client.post("/api/run")
    assert statuses(client, "ORD-1") == {"crm": "pending", "accounting": "sent", "slack": "sent", "sheets": "sent"}
    clock.advance(2); client.post("/api/run")
    assert statuses(client, "ORD-1")["crm"] == "pending"
    clock.advance(4); client.post("/api/run")
    crm = client.get("/api/events/ORD-1").json()["deliveries"][0]
    assert (crm["status"], crm["attempts"]) == ("sent", 3) and len(world.received["crm"]) == 1


def test_a_rejected_delivery_stops_and_can_be_replayed_after_the_cause_is_fixed(client, clock, world):
    post(client, clock, order_body("ORD-1", "XYZ"))
    client.post("/api/run")
    accounting = next(d for d in client.get("/api/events/ORD-1").json()["deliveries"] if d["destination"] == "accounting")
    assert accounting["status"] == "dead" and "unsupported currency XYZ" in accounting["last_error"]
    assert statuses(client, "ORD-1")["slack"] == "sent"  # one system failing does not hold up the rest
    assert client.post(f"/api/deliveries/{accounting['id']}/replay").status_code == 200
    client.post("/api/run")
    assert statuses(client, "ORD-1")["accounting"] == "dead"  # still unsupported, so it is rejected again


def test_only_dead_deliveries_can_be_replayed(client, clock):
    post(client, clock, order_body("ORD-1"))
    delivery_id = client.get("/api/events/ORD-1").json()["deliveries"][0]["id"]
    assert client.post(f"/api/deliveries/{delivery_id}/replay").status_code == 409
    assert client.post("/api/deliveries/9999/replay").status_code == 409


def test_unknown_event_is_a_404(client):
    assert client.get("/api/events/nope").status_code == 404


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True, "demo_mode": True, "destinations": ["accounting", "crm", "sheets", "slack"], "max_attempts": 5}


@pytest.mark.parametrize("scenario, http_status", [("normal", 202), ("flaky_crm", 202), ("bad_currency", 202), ("forged", 401)])
def test_the_demo_scenarios(client, scenario, http_status):
    assert client.post("/api/demo/send", json={"scenario": scenario}).json()["http_status"] == http_status


def test_the_duplicate_demo_scenario_shows_both_answers(client):
    r = client.post("/api/demo/send", json={"scenario": "duplicate"}).json()
    assert (r["first"]["http_status"], r["second"]["http_status"], r["second"]["status"]) == (202, 200, "duplicate")


def test_unknown_demo_scenario_is_a_422(client):
    assert client.post("/api/demo/send", json={"scenario": "explode"}).status_code == 422


def test_demo_endpoints_are_off_when_demo_mode_is_off(tmp_path, clock):
    import httpx
    settings = Settings(webhook_secret=SECRET, db_path=str(tmp_path / "x.db"), demo_mode=False, destination_urls={"crm": "http://crm.test/c"})
    c = TestClient(create_app(settings, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))), clock=clock, start_worker=False))
    assert c.post("/api/demo/send", json={"scenario": "normal"}).status_code == 404
    assert c.get("/api/health").json()["destinations"] == ["crm"]
    assert post(c, clock, order_body("ORD-1")).status_code == 202
    assert list(statuses(c, "ORD-1")) == ["crm"]  # only the configured systems get the order


def test_the_background_worker_delivers_without_anyone_asking(tmp_path, world):
    settings = Settings(webhook_secret=SECRET, db_path=str(tmp_path / "w.db"), base_delay=0.05)
    with TestClient(create_app(settings, world=world, start_worker=True)) as c:
        body, headers = signed(order_body("ORD-1"), time.time)
        assert c.post("/webhooks/orders", content=body, headers=headers).status_code == 202
        deadline = time.time() + 5
        while time.time() < deadline and set(statuses(c, "ORD-1").values()) != {"sent"}:
            time.sleep(0.1)
        assert set(statuses(c, "ORD-1").values()) == {"sent"}


def test_the_page_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Integration Hub" in page.text

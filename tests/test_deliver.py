import json
import threading
import time

import httpx
import pytest

from app.config import Settings
from app.deliver import MAX_WAIT, Deliverer
from app.store import Store
from tests.conftest import START, Clock


def setup(tmp_path, handler, **cfg):
    clock = Clock()
    store = Store(tmp_path / "d.db")
    store.add_event("E1", "orders", {"id": "E1"}, {"crm": {"hello": "world"}}, clock())
    settings = Settings(base_delay=2.0, max_attempts=5, **cfg)
    deliverer = Deliverer(store, {"crm": "http://crm.test/contacts"}, httpx.Client(transport=httpx.MockTransport(handler)), settings, clock)
    return store, deliverer, clock


def the_delivery(store):
    return store.event("E1")["deliveries"][0]


def test_a_2xx_is_final(tmp_path):
    store, deliverer, _ = setup(tmp_path, lambda r: httpx.Response(201))
    assert deliverer.process_due() == 1 and deliverer.process_due() == 0
    d = the_delivery(store)
    assert (d["status"], d["attempts"], d["last_status_code"], d["last_error"]) == ("sent", 1, 201, None)


def test_every_request_carries_an_idempotency_key_and_the_mapped_payload(tmp_path):
    seen = []
    store, deliverer, _ = setup(tmp_path, lambda r: seen.append(r) or httpx.Response(200))
    deliverer.process_due()
    assert seen[0].headers["Idempotency-Key"] == "E1:crm" and seen[0].url == "http://crm.test/contacts"
    assert json.loads(seen[0].content) == {"hello": "world"}


def test_retries_double_the_wait_each_time_and_succeed_when_the_system_recovers(tmp_path):
    answers = iter([503, 503, 503, 200])
    store, deliverer, clock = setup(tmp_path, lambda r: httpx.Response(next(answers)))
    waits = []
    for _ in range(4):
        deliverer.process_due()
        d = the_delivery(store)
        if d["status"] == "sent":
            break
        waits.append(d["next_attempt_at"] - clock())
        assert deliverer.process_due() == 0  # not due yet, so nothing is tried early
        clock.advance(waits[-1])
    assert waits == [2.0, 4.0, 8.0]
    d = the_delivery(store)
    assert (d["status"], d["attempts"]) == ("sent", 4)


def test_it_gives_up_after_the_last_attempt_and_says_so(tmp_path):
    calls = []
    store, deliverer, clock = setup(tmp_path, lambda r: calls.append(1) or httpx.Response(500))
    for _ in range(10):
        deliverer.process_due()
        clock.advance(100)
    d = the_delivery(store)
    assert (d["status"], d["attempts"], len(calls)) == ("dead", 5, 5)
    assert "gave up after 5 attempts" in d["last_error"] and "500" in d["last_error"]


def test_a_rejection_is_dead_at_once_with_the_reason(tmp_path):
    store, deliverer, clock = setup(tmp_path, lambda r: httpx.Response(422, json={"error": "unsupported currency XYZ"}))
    deliverer.process_due()
    d = the_delivery(store)
    assert (d["status"], d["attempts"], d["last_status_code"]) == ("dead", 1, 422)
    assert d["last_error"].startswith("rejected with 422") and "unsupported currency XYZ" in d["last_error"]
    clock.advance(1000)
    assert deliverer.process_due() == 0


@pytest.mark.parametrize("code", [400, 401, 404, 409, 410])
def test_other_client_errors_are_never_retried(tmp_path, code):
    store, deliverer, _ = setup(tmp_path, lambda r: httpx.Response(code))
    deliverer.process_due()
    assert the_delivery(store)["status"] == "dead"


@pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504])
def test_these_failures_are_retried(tmp_path, code):
    store, deliverer, _ = setup(tmp_path, lambda r: httpx.Response(code))
    deliverer.process_due()
    d = the_delivery(store)
    assert d["status"] == "pending" and d["attempts"] == 1


def test_a_redirect_is_not_followed_or_retried(tmp_path):
    store, deliverer, _ = setup(tmp_path, lambda r: httpx.Response(302, headers={"Location": "http://elsewhere"}))
    deliverer.process_due()
    assert the_delivery(store)["status"] == "dead"


def test_retry_after_is_respected_when_longer_than_the_backoff(tmp_path):
    store, deliverer, clock = setup(tmp_path, lambda r: httpx.Response(429, headers={"Retry-After": "30"}))
    deliverer.process_due()
    assert the_delivery(store)["next_attempt_at"] == START + 30


def test_retry_after_never_shortens_the_backoff_and_is_capped(tmp_path):
    store, deliverer, _ = setup(tmp_path, lambda r: httpx.Response(429, headers={"Retry-After": "1"}))
    deliverer.process_due()
    assert the_delivery(store)["next_attempt_at"] == START + 2
    store2, deliverer2, _ = setup(tmp_path / "b", lambda r: httpx.Response(429, headers={"Retry-After": "999999"}))
    deliverer2.process_due()
    assert the_delivery(store2)["next_attempt_at"] == START + MAX_WAIT


def test_network_errors_are_retried(tmp_path):
    def handler(request):
        raise httpx.ConnectError("refused")
    store, deliverer, _ = setup(tmp_path, handler)
    deliverer.process_due()
    d = the_delivery(store)
    assert d["status"] == "pending" and d["last_error"] == "network error: ConnectError"


def test_a_destination_that_is_not_configured_is_dead_with_a_clear_message(tmp_path):
    store, deliverer, _ = setup(tmp_path, lambda r: httpx.Response(200))
    deliverer.destinations.clear()
    deliverer.process_due()
    assert the_delivery(store)["status"] == "dead" and "no destination called crm" in the_delivery(store)["last_error"]


def test_a_replayed_delivery_gets_a_fresh_set_of_attempts(tmp_path):
    answers = {"code": 500}
    store, deliverer, clock = setup(tmp_path, lambda r: httpx.Response(answers["code"]))
    for _ in range(6):
        deliverer.process_due(); clock.advance(100)
    assert the_delivery(store)["status"] == "dead"
    answers["code"] = 200
    assert store.replay(the_delivery(store)["id"], clock())
    deliverer.process_due()
    d = the_delivery(store)
    assert (d["status"], d["attempts"]) == ("sent", 1)


def test_two_callers_at_once_never_send_the_same_delivery_twice(tmp_path):
    calls = []

    def slow(request):
        calls.append(1)
        time.sleep(0.05)
        return httpx.Response(200)

    store, deliverer, _ = setup(tmp_path, slow)
    threads = [threading.Thread(target=deliverer.process_due) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1 and the_delivery(store)["status"] == "sent"

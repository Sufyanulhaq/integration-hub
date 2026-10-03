import json

import pytest

from app.config import Settings
from app.events import OrderEvent
from app.main import create_app, order_body
from app.mock_destinations import MockWorld
from app.signing import sign

SECRET = "test-secret"
START = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = START) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings(tmp_path):
    return Settings(webhook_secret=SECRET, db_path=str(tmp_path / "hub.db"), base_delay=2.0, max_attempts=5)


@pytest.fixture
def world():
    return MockWorld()


@pytest.fixture
def app(settings, world, clock):
    return create_app(settings, world=world, clock=clock, start_worker=False)


def signed(payload, clock, secret=SECRET, timestamp=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    ts = str(int(clock() if timestamp is None else timestamp))
    return body, {"X-Timestamp": ts, "X-Signature": sign(secret, ts, body), "Content-Type": "application/json"}


@pytest.fixture
def order():
    return OrderEvent.model_validate(order_body("ORD-1"))

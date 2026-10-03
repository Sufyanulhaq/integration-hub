"""The web API. Start it with: uvicorn app.main:create_app --factory"""
from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .config import Settings
from .deliver import Deliverer
from .events import OrderEvent
from .mapping import MAPPERS
from .mock_destinations import URLS, MockWorld
from .signing import SignatureError, sign, verify
from .store import Store

STATIC = Path(__file__).resolve().parent.parent / "static"


class Scenario(BaseModel):
    scenario: str


def order_body(order_id: str, currency: str = "GBP") -> dict:
    items = [{"sku": "DESK-120", "name": "Standing desk 120cm", "qty": 1, "unit_price": 349.0},
             {"sku": "CBL-02", "name": "Cable kit", "qty": 2, "unit_price": 6.5}]
    return {"id": order_id, "type": "order.created", "customer": {"name": "Priya Nair", "email": "priya.nair@example.com"},
            "items": items, "currency": currency, "total": 362.0}


def create_app(settings: Settings | None = None, client: httpx.Client | None = None, world: MockWorld | None = None,
               clock: Callable[[], float] = time.time, start_worker: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    if settings.destination_urls:
        destinations, http = settings.destination_urls, client or httpx.Client(timeout=10)
    else:
        world = world or MockWorld()
        destinations, http = URLS, client or world.client()
    deliverer = Deliverer(store, destinations, http, settings, clock)
    counter = {"n": 0}
    stop = threading.Event()

    def worker() -> None:
        while not stop.wait(0.5):
            deliverer.process_due()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        thread = threading.Thread(target=worker, daemon=True) if start_worker else None
        if thread:
            thread.start()
        yield
        stop.set()

    app = FastAPI(title="Integration Hub", lifespan=lifespan)
    app.state.store, app.state.deliverer, app.state.world = store, deliverer, world

    def accept(body: bytes, timestamp: str | None, signature: str | None) -> tuple[int, dict]:
        """The whole webhook decision, kept separate from the HTTP layer so the demo can use it too."""
        if len(body) > settings.max_body:
            return 413, {"error": "body too large"}
        try:
            verify(settings.webhook_secret, timestamp, signature, body, clock(), settings.tolerance)
        except SignatureError as exc:
            return 401, {"error": str(exc)}
        try:
            event = OrderEvent.model_validate(json.loads(body))
        except json.JSONDecodeError:
            return 400, {"error": "body is not valid JSON"}
        except ValidationError as exc:
            return 422, {"error": "invalid order", "problems": [f"{'.'.join(map(str, e['loc'])) or 'order'}: {e['msg']}" for e in exc.errors()]}
        deliveries = {name: mapper(event) for name, mapper in MAPPERS.items() if name in destinations}
        if not store.add_event(event.id, "orders", event.model_dump(), deliveries, clock()):
            return 200, {"status": "duplicate", "event_id": event.id}
        return 202, {"status": "accepted", "event_id": event.id}

    @app.post("/webhooks/orders")
    async def orders(request: Request):
        status, body = accept(await request.body(), request.headers.get("X-Timestamp"), request.headers.get("X-Signature"))
        return JSONResponse(body, status_code=status)

    def view(event: dict) -> dict:
        return {"id": event["id"], "received_at": event["received_at"], "customer": event["payload"]["customer"]["name"],
                "total": event["payload"]["total"], "currency": event["payload"]["currency"],
                "payload": event["payload"], "deliveries": [
                    {k: d[k] for k in ("id", "destination", "status", "attempts", "last_error", "last_status_code", "next_attempt_at", "payload")}
                    for d in event["deliveries"]]}

    @app.get("/api/health")
    def health():
        return {"ok": True, "demo_mode": settings.demo_mode, "destinations": sorted(destinations), "max_attempts": settings.max_attempts}

    @app.get("/api/events")
    def events():
        return [view(e) for e in store.events()]

    @app.get("/api/events/{event_id}")
    def one(event_id: str):
        event = store.event(event_id)
        if not event:
            raise HTTPException(404, "no such event")
        return view(event)

    @app.post("/api/deliveries/{delivery_id}/replay")
    def replay(delivery_id: int):
        if not store.replay(delivery_id, clock()):
            raise HTTPException(409, "Only a dead delivery can be replayed.")
        return {"replayed": delivery_id}

    @app.post("/api/run")
    def run():
        return {"attempted": deliverer.process_due()}

    def need_demo() -> None:
        if not settings.demo_mode:
            raise HTTPException(404, "demo mode is off")

    @app.post("/api/demo/send")
    def demo_send(body: Scenario):
        """Signs and sends a test order, so the page can show each case without a second program."""
        need_demo()
        counter["n"] += 1
        order_id = f"ORD-{1000 + counter['n']}"

        def send(payload: dict, signature_secret: str | None = None) -> dict:
            raw = json.dumps(payload).encode()
            ts = str(int(clock()))
            status, answer = accept(raw, ts, sign(signature_secret or settings.webhook_secret, ts, raw))
            return {"http_status": status, **answer}

        match body.scenario:
            case "normal":
                return send(order_body(order_id))
            case "flaky_crm":
                world.fail_next("crm", 2)
                return send(order_body(order_id))
            case "bad_currency":
                return send(order_body(order_id, "XYZ"))
            case "duplicate":
                first = send(order_body(order_id))
                return {"first": first, "second": send(order_body(order_id))}
            case "forged":
                return send(order_body(order_id), signature_secret="not-the-real-secret")
            case _:
                raise HTTPException(422, "unknown scenario")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app

"""Stand ins for the four systems, so the demo and the tests need no accounts. They behave like real
APIs in the ways that matter here: the CRM can be made to fail, the accounting system rejects a
currency it does not support, and the CRM checks the idempotency key."""
from __future__ import annotations

import json

import httpx

URLS = {
    "crm": "http://crm.mock/contacts",
    "accounting": "http://accounting.mock/invoices",
    "slack": "http://slack.mock/hook",
    "sheets": "http://sheets.mock/rows",
}
SUPPORTED_CURRENCIES = {"GBP", "USD", "EUR"}


class MockWorld:
    def __init__(self) -> None:
        self.received: dict[str, list[dict]] = {name: [] for name in URLS}
        self._failures: dict[str, int] = {}
        self._seen: set[str] = set()

    def fail_next(self, name: str, times: int) -> None:
        self._failures[name] = times

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.host.split(".")[0]
        if name not in URLS:
            return httpx.Response(404, json={"error": "unknown host"})
        if self._failures.get(name, 0) > 0:
            self._failures[name] -= 1
            return httpx.Response(503, json={"error": "service unavailable"})
        body = json.loads(request.content)
        if name == "accounting" and body.get("currency") not in SUPPORTED_CURRENCIES:
            return httpx.Response(422, json={"error": f"unsupported currency {body.get('currency')}"})
        key = request.headers.get("Idempotency-Key", "")
        if name == "crm":
            if key in self._seen:
                return httpx.Response(200, json={"duplicate": True})
            self._seen.add(key)
        self.received[name].append(body)
        return httpx.Response(201, json={"ok": True})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

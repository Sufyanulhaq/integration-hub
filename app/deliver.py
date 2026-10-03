"""Sends stored deliveries to their destinations. The rules that matter:

- A success (2xx) is final.
- A failure that may pass (network error, 408, 429, 5xx) is retried with a doubling wait, and a
  Retry-After header from the other side is respected. After the last attempt it becomes dead.
- A failure that will never pass (any other 4xx) goes straight to dead, with the reason, because
  retrying a rejected request only repeats the rejection.
- Every request carries an Idempotency-Key, so a retry after a lost reply cannot create a duplicate.
- A dead delivery waits for a person, who can replay it."""
from __future__ import annotations

import threading
import time
from typing import Callable

import httpx

from .config import Settings
from .store import Store

RETRYABLE = {408, 429}
MAX_WAIT = 3600.0


class Deliverer:
    def __init__(self, store: Store, destinations: dict[str, str], client: httpx.Client, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.destinations = destinations
        self.client = client
        self.settings = settings
        self.clock = clock
        self._lock = threading.Lock()  # one pass at a time, so two callers cannot send the same delivery twice

    def process_due(self) -> int:
        """Try every delivery that is due. Returns how many attempts were made."""
        with self._lock:
            tried = 0
            for delivery in self.store.due(self.clock()):
                self._attempt(delivery)
                tried += 1
            return tried

    def _attempt(self, d: dict) -> None:
        attempts = d["attempts"] + 1
        url = self.destinations.get(d["destination"])
        now = self.clock()
        if url is None:
            self.store.mark_dead(d["id"], attempts, f"no destination called {d['destination']} is configured", None, now)
            return
        try:
            response = self.client.post(url, json=d["payload"], headers={"Idempotency-Key": f"{d['event_id']}:{d['destination']}"})
        except httpx.TransportError as exc:
            self._failed(d, attempts, f"network error: {type(exc).__name__}", None, None)
            return
        code = response.status_code
        if 200 <= code < 300:
            self.store.mark_sent(d["id"], attempts, code, now)
        elif code in RETRYABLE or code >= 500:
            self._failed(d, attempts, f"destination answered {code}", code, response.headers.get("Retry-After"))
        else:
            detail = response.text.strip().replace("\n", " ")[:200]
            self.store.mark_dead(d["id"], attempts, f"rejected with {code}: {detail}" if detail else f"rejected with {code}", code, now)

    def _failed(self, d: dict, attempts: int, error: str, code: int | None, retry_after: str | None) -> None:
        now = self.clock()
        if attempts >= self.settings.max_attempts:
            self.store.mark_dead(d["id"], attempts, f"{error}, gave up after {attempts} attempts", code, now)
            return
        wait = self.settings.base_delay * 2 ** (attempts - 1)
        if retry_after and retry_after.isdigit():
            wait = max(wait, float(retry_after))
        self.store.mark_retry(d["id"], attempts, now + min(wait, MAX_WAIT), error, code, now)

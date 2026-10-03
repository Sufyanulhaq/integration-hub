"""Settings read from environment variables. Everything has a default so the demo starts with none."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    webhook_secret: str = "demo-secret-change-me"
    db_path: str = "data/hub.db"
    max_attempts: int = 5
    base_delay: float = 2.0  # seconds before the first retry, doubling each time
    tolerance: int = 300  # how old a signed request may be, in seconds
    max_body: int = 256 * 1024
    demo_mode: bool = True  # built in mock systems and the demo buttons
    destination_urls: dict | None = None  # real systems: {"crm": "https://...", ...}

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        urls = os.environ.get("DESTINATION_URLS")
        return cls(
            webhook_secret=os.environ.get("WEBHOOK_SECRET", d.webhook_secret),
            db_path=os.environ.get("HUB_DB", d.db_path),
            max_attempts=int(os.environ.get("MAX_ATTEMPTS", d.max_attempts)),
            base_delay=float(os.environ.get("BASE_DELAY", d.base_delay)),
            tolerance=int(os.environ.get("SIGNATURE_TOLERANCE", d.tolerance)),
            demo_mode=os.environ.get("DEMO_MODE", "1") != "0" and not urls,
            destination_urls=json.loads(urls) if urls else None,
        )

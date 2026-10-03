"""Checks that a webhook really came from the sender who knows the shared secret. The signature
covers the timestamp as well as the body, and old timestamps are refused, so a captured request
cannot be replayed later."""
from __future__ import annotations

import hashlib
import hmac


class SignatureError(Exception):
    """The request is not trustworthy. The message says why, and is safe to return."""


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: str | None, signature: str | None, body: bytes, now: float, tolerance: int = 300) -> None:
    if not timestamp or not signature:
        raise SignatureError("missing X-Timestamp or X-Signature header")
    try:
        sent_at = int(timestamp)
    except ValueError:
        raise SignatureError("X-Timestamp is not a number") from None
    if abs(now - sent_at) > tolerance:
        raise SignatureError("timestamp is too old or too far in the future")
    expected = sign(secret, sent_at, body)
    if not hmac.compare_digest(expected, signature.strip()):  # constant time, so timing leaks nothing
        raise SignatureError("signature does not match")

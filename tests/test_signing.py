import pytest

from app.signing import SignatureError, sign, verify

BODY = b'{"id":"ORD-1"}'
NOW = 1_800_000_000


def good(ts=NOW, secret="s"):
    return str(ts), sign(secret, ts, BODY)


def test_a_correct_signature_passes():
    ts, sig = good()
    verify("s", ts, sig, BODY, NOW)


def test_the_signature_is_stable_and_prefixed():
    assert sign("s", 1, b"x") == sign("s", "1", b"x") and sign("s", 1, b"x").startswith("sha256=")


@pytest.mark.parametrize("mutate, message", [
    (lambda ts, sig: (ts, sig.replace("sha256=", "sha256=0")), "does not match"),
    (lambda ts, sig: (ts, ""), "missing"),
    (lambda ts, sig: ("", sig), "missing"),
    (lambda ts, sig: (None, None), "missing"),
    (lambda ts, sig: ("yesterday", sig), "not a number"),
])
def test_bad_headers_are_refused(mutate, message):
    ts, sig = mutate(*good())
    with pytest.raises(SignatureError, match=message):
        verify("s", ts, sig, BODY, NOW)


def test_a_changed_body_is_refused():
    ts, sig = good()
    with pytest.raises(SignatureError, match="does not match"):
        verify("s", ts, sig, BODY + b" ", NOW)


def test_the_wrong_secret_is_refused():
    ts, sig = good(secret="other")
    with pytest.raises(SignatureError, match="does not match"):
        verify("s", ts, sig, BODY, NOW)


def test_the_timestamp_is_part_of_what_is_signed():
    _, sig = good(ts=NOW)
    with pytest.raises(SignatureError, match="does not match"):
        verify("s", str(NOW + 1), sig, BODY, NOW)


@pytest.mark.parametrize("offset, ok", [(0, True), (299, True), (300, True), (301, False), (-301, False), (3600, False)])
def test_old_and_future_requests_are_refused(offset, ok):
    ts, sig = good(ts=NOW - offset)
    if ok:
        verify("s", ts, sig, BODY, NOW)
    else:
        with pytest.raises(SignatureError, match="too old"):
            verify("s", ts, sig, BODY, NOW)

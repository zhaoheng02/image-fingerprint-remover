"""Small signed-cookie sessions for OAuth-backed deployments."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


class InvalidSession(Exception):
    pass


def create_session_token(payload: dict[str, Any], secret: str, ttl_seconds: int = 30 * 24 * 60 * 60) -> str:
    if not secret:
        raise InvalidSession("Session secret is not configured.")
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_seconds
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encoded = _b64(raw)
    signature = _sign(encoded, secret)
    return f"{encoded}.{signature}"


def read_session_token(token: str, secret: str) -> dict[str, Any]:
    if not token or not secret or "." not in token:
        raise InvalidSession("Invalid session token.")
    encoded, signature = token.rsplit(".", 1)
    expected = _sign(encoded, secret)
    if not hmac.compare_digest(signature, expected):
        raise InvalidSession("Invalid session signature.")
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidSession("Invalid session payload.") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise InvalidSession("Session expired.")
    return payload


def _sign(value: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), value.encode("ascii"), hashlib.sha256).digest()
    return _b64(digest)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))

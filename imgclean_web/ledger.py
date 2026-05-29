"""Credit ledger implementations for local development and Supabase."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .identity import User


@dataclass(frozen=True)
class Account:
    user_id: str
    email: str
    credits: int


class InsufficientCredits(Exception):
    pass


class LocalLedger:
    def __init__(self, path: Path, initial_credits: int):
        self.path = path
        self.initial_credits = initial_credits
        self._lock = threading.Lock()

    def get_account(self, user: User) -> Account:
        with self._lock:
            data = self._read()
            account = data["accounts"].setdefault(
                user.user_id,
                {"email": user.email, "credits": self.initial_credits},
            )
            if user.email and not account.get("email"):
                account["email"] = user.email
            self._write(data)
            return Account(user.user_id, account.get("email", ""), int(account["credits"]))

    def consume_credit(self, user: User, job_id: str, metadata: dict[str, Any]) -> int:
        with self._lock:
            data = self._read()
            account = data["accounts"].setdefault(
                user.user_id,
                {"email": user.email, "credits": self.initial_credits},
            )
            if int(account["credits"]) <= 0:
                raise InsufficientCredits()
            account["credits"] = int(account["credits"]) - 1
            data["usage_events"].append({
                "user_id": user.user_id,
                "job_id": job_id,
                "event_type": "clean_image",
                "credits_delta": -1,
                "metadata": metadata,
            })
            self._write(data)
            return int(account["credits"])

    def grant_credits(
        self,
        user_id: str,
        credits: int,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            data = self._read()
            account = data["accounts"].setdefault(
                user_id,
                {"email": "", "credits": self.initial_credits},
            )
            account["credits"] = int(account["credits"]) + credits
            data["usage_events"].append({
                "user_id": user_id,
                "event_type": reason,
                "credits_delta": credits,
                "metadata": metadata or {},
            })
            self._write(data)
            return int(account["credits"])

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"accounts": {}, "usage_events": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def record_webhook_event(self, provider: str, event_id: str, event_name: str, raw_payload: dict[str, Any]) -> bool:
        with self._lock:
            data = self._read()
            events = data.setdefault("payment_webhook_events", {})
            key = f"{provider}:{event_id}"
            if key in events:
                return False
            events[key] = {
                "provider": provider,
                "event_id": event_id,
                "event_name": event_name,
                "raw_payload": raw_payload,
            }
            self._write(data)
            return True

    def upsert_order(
        self,
        provider: str,
        provider_order_id: str,
        user_id: str,
        credits: int,
        amount_cents: int | None,
        currency: str | None,
        status: str,
        raw_payload: dict[str, Any],
    ) -> None:
        with self._lock:
            data = self._read()
            orders = data.setdefault("orders", {})
            orders[f"{provider}:{provider_order_id}"] = {
                "provider": provider,
                "provider_order_id": provider_order_id,
                "user_id": user_id,
                "credits": credits,
                "amount_cents": amount_cents,
                "currency": currency,
                "status": status,
                "raw_payload": raw_payload,
            }
            self._write(data)


class SupabaseLedger:
    def __init__(self, supabase_url: str, service_key: str, initial_credits: int):
        self.supabase_url = supabase_url.rstrip("/")
        self.service_key = service_key
        self.initial_credits = initial_credits

    async def get_account(self, user: User) -> Account:
        headers = self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(
                f"{self.supabase_url}/rest/v1/profiles",
                params={"user_id": f"eq.{user.user_id}", "select": "user_id,email,credits"},
                headers=headers,
            )
            res.raise_for_status()
            rows = res.json()
            if not rows:
                payload = {
                    "user_id": user.user_id,
                    "email": user.email,
                    "credits": self.initial_credits,
                }
                upsert = await client.post(f"{self.supabase_url}/rest/v1/profiles", json=payload, headers=headers)
                upsert.raise_for_status()
                return Account(user.user_id, user.email, self.initial_credits)
        row = rows[0]
        return Account(row["user_id"], row.get("email") or "", int(row["credits"]))

    async def consume_credit(self, user: User, job_id: str, metadata: dict[str, Any]) -> int:
        await self.get_account(user)
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{self.supabase_url}/rest/v1/rpc/consume_credit",
                json={"p_user_id": user.user_id, "p_job_id": job_id, "p_metadata": metadata},
                headers=self._headers(),
            )
        if res.status_code == 402:
            raise InsufficientCredits()
        res.raise_for_status()
        return int(res.json())

    async def grant_credits(
        self,
        user_id: str,
        credits: int,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{self.supabase_url}/rest/v1/rpc/grant_credits",
                json={
                    "p_user_id": user_id,
                    "p_delta": credits,
                    "p_reason": reason,
                    "p_metadata": metadata or {},
                },
                headers=self._headers(),
            )
        res.raise_for_status()
        return int(res.json())

    async def record_webhook_event(
        self,
        provider: str,
        event_id: str,
        event_name: str,
        raw_payload: dict[str, Any],
    ) -> bool:
        payload = {
            "provider": provider,
            "event_id": event_id,
            "event_name": event_name,
            "raw_payload": raw_payload,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(f"{self.supabase_url}/rest/v1/payment_webhook_events", json=payload, headers=self._headers())
        if res.status_code == 409:
            return False
        res.raise_for_status()
        return True

    async def upsert_order(
        self,
        provider: str,
        provider_order_id: str,
        user_id: str,
        credits: int,
        amount_cents: int | None,
        currency: str | None,
        status: str,
        raw_payload: dict[str, Any],
    ) -> None:
        payload = {
            "provider": provider,
            "provider_order_id": provider_order_id,
            "user_id": user_id,
            "credits": credits,
            "amount_cents": amount_cents,
            "currency": currency,
            "status": status,
            "raw_payload": raw_payload,
        }
        headers = self._headers()
        headers["prefer"] = "resolution=merge-duplicates"
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{self.supabase_url}/rest/v1/orders",
                params={"on_conflict": "provider,provider_order_id"},
                json=payload,
                headers=headers,
            )
        res.raise_for_status()

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "authorization": f"Bearer {self.service_key}",
            "content-type": "application/json",
            "prefer": "return=representation",
        }

"""Authentication helpers for the SaaS API."""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request


@dataclass(frozen=True)
class User:
    user_id: str
    email: str = ""


class Authenticator:
    def __init__(self, mode: str, supabase_url: str = "", supabase_key: str = ""):
        self.mode = mode
        self.supabase_url = supabase_url.rstrip("/")
        self.supabase_key = supabase_key

    async def current_user(self, request: Request) -> User | None:
        if self.mode == "none":
            return None
        if self.mode == "dev":
            user_id = request.headers.get("x-user-id")
            if not user_id:
                raise HTTPException(status_code=401, detail="Missing x-user-id.")
            return User(user_id=user_id, email=request.headers.get("x-user-email", ""))
        if self.mode == "supabase":
            return await self._supabase_user(request)
        raise HTTPException(status_code=500, detail=f"Unsupported auth mode: {self.mode}")

    async def _supabase_user(self, request: Request) -> User:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token.")
        if not self.supabase_url or not self.supabase_key:
            raise HTTPException(status_code=500, detail="Supabase auth is not configured.")

        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.get(
                f"{self.supabase_url}/auth/v1/user",
                headers={
                    "apikey": self.supabase_key,
                    "authorization": auth,
                },
            )
        if res.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid Supabase session.")
        data = res.json()
        return User(user_id=data["id"], email=data.get("email", ""))

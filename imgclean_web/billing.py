"""Payment checkout helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .identity import User


@dataclass(frozen=True)
class CreditPack:
    pack_id: str
    name: str
    credits: int
    amount_cents: int
    currency: str = "usd"
    stripe_price_id: str = ""


@dataclass(frozen=True)
class CheckoutSession:
    provider_order_id: str
    checkout_url: str
    credits: int
    amount_cents: int
    currency: str
    raw_payload: dict[str, Any]


DEFAULT_PACKS = {
    "starter": CreditPack("starter", "Starter pack - 25 image cleanups", 25, 900),
    "growth": CreditPack("growth", "Growth pack - 100 image cleanups", 100, 2900),
}


class BillingNotConfigured(Exception):
    pass


class UnknownCreditPack(Exception):
    pass


class StripeBilling:
    def __init__(
        self,
        secret_key: str,
        app_base_url: str,
        starter_price_id: str = "",
        growth_price_id: str = "",
        api_base_url: str = "https://api.stripe.com",
    ):
        self.secret_key = secret_key
        self.app_base_url = app_base_url.rstrip("/")
        self.api_base_url = api_base_url.rstrip("/")
        self.packs = {
            "starter": _replace_price_id(DEFAULT_PACKS["starter"], starter_price_id),
            "growth": _replace_price_id(DEFAULT_PACKS["growth"], growth_price_id),
        }

    async def create_checkout_session(self, pack_id: str, user: User) -> CheckoutSession:
        if not self.secret_key:
            raise BillingNotConfigured("Stripe is not configured.")
        pack = self.packs.get(pack_id)
        if pack is None:
            raise UnknownCreditPack(pack_id)

        metadata = {
            "user_id": user.user_id,
            "credits": str(pack.credits),
            "pack": pack.pack_id,
        }
        data: dict[str, str] = {
            "mode": "payment",
            "success_url": f"{self.app_base_url}/dashboard?checkout=success",
            "cancel_url": f"{self.app_base_url}/pricing?checkout=cancelled",
            "client_reference_id": user.user_id,
            "metadata[user_id]": metadata["user_id"],
            "metadata[credits]": metadata["credits"],
            "metadata[pack]": metadata["pack"],
            "payment_intent_data[metadata][user_id]": metadata["user_id"],
            "payment_intent_data[metadata][credits]": metadata["credits"],
            "payment_intent_data[metadata][pack]": metadata["pack"],
            "line_items[0][quantity]": "1",
        }
        if user.email:
            data["customer_email"] = user.email
        if pack.stripe_price_id:
            data["line_items[0][price]"] = pack.stripe_price_id
        else:
            data.update({
                "line_items[0][price_data][currency]": pack.currency,
                "line_items[0][price_data][unit_amount]": str(pack.amount_cents),
                "line_items[0][price_data][product_data][name]": pack.name,
            })

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.api_base_url}/v1/checkout/sessions",
                data=data,
                headers={
                    "Authorization": f"Bearer {self.secret_key}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        response.raise_for_status()
        payload = response.json()
        checkout_url = payload.get("url")
        provider_order_id = payload.get("id")
        if not checkout_url or not provider_order_id:
            raise BillingNotConfigured("Stripe did not return a checkout URL.")
        return CheckoutSession(
            provider_order_id=provider_order_id,
            checkout_url=checkout_url,
            credits=pack.credits,
            amount_cents=pack.amount_cents,
            currency=pack.currency.upper(),
            raw_payload=payload,
        )


def _replace_price_id(pack: CreditPack, price_id: str) -> CreditPack:
    return CreditPack(
        pack_id=pack.pack_id,
        name=pack.name,
        credits=pack.credits,
        amount_cents=pack.amount_cents,
        currency=pack.currency,
        stripe_price_id=price_id,
    )

import hashlib
import hmac
import io
import json
import time
import struct
import zlib

from fastapi.testclient import TestClient
from PIL import Image

from imgclean_web.billing import CheckoutSession
from imgclean_web.app import create_app
from imgclean_web.storage import SupabaseStorage


def _png_chunk(ctype: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + ctype
        + body
        + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)
    )


def _make_dirty_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(40, 120, 220)).save(buf, format="PNG")
    base = buf.getvalue()
    insert_at = base.rfind(b"IEND") - 4
    marker = _png_chunk(
        b"tEXt",
        b"parameters\x00prompt, seed: 123, Model hash: abcdef, Sampler: Euler",
    )
    return base[:insert_at] + marker + base[insert_at:]


def _client(tmp_path, monkeypatch, credits="2"):
    app = _app(tmp_path, monkeypatch, credits)
    return TestClient(app)


def _app(tmp_path, monkeypatch, credits="2"):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "dev")
    monkeypatch.setenv("IMGCLEAN_LEDGER_BACKEND", "local")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "local")
    monkeypatch.setenv("IMGCLEAN_INITIAL_CREDITS", credits)
    monkeypatch.setenv("LEMONSQUEEZY_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "stripe-whsec")
    monkeypatch.setenv("APP_BASE_URL", "https://frontend-green-one-33.vercel.app")
    return create_app()


def test_authenticated_clean_consumes_credit_and_returns_download(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, credits="2")

    me_before = client.get("/api/me", headers={"x-user-id": "user-1"})
    assert me_before.status_code == 200
    assert me_before.json()["credits"] == 2

    response = client.post(
        "/api/clean",
        headers={"x-user-id": "user-1"},
        data={"mode": "safe"},
        files=[("files", ("dirty.png", _make_dirty_png(), "image/png"))],
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["ok"] is True
    assert result["download_url"].startswith("/download/")
    assert result["credits_remaining"] == 1

    me_after = client.get("/api/me", headers={"x-user-id": "user-1"})
    assert me_after.status_code == 200
    assert me_after.json()["credits"] == 1

    download = client.get(result["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "image/png"


def test_clean_rejects_when_user_has_no_credits(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, credits="0")

    response = client.post(
        "/api/clean",
        headers={"x-user-id": "user-2"},
        data={"mode": "safe"},
        files=[("files", ("dirty.png", _make_dirty_png(), "image/png"))],
    )

    assert response.status_code == 402
    assert response.json()["detail"] == "Insufficient credits."


def test_lemonsqueezy_webhook_grants_credits(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, credits="0")
    body = json.dumps(
        {
            "meta": {
                "event_name": "order_created",
                "custom_data": {"user_id": "user-3", "credits": 25},
            },
            "data": {
                "id": "order_123",
                "attributes": {
                    "status": "paid",
                    "total": 900,
                    "currency": "USD",
                },
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    response = client.post(
        "/api/webhooks/lemonsqueezy",
        content=body,
        headers={"x-signature": signature, "content-type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["credited"] == 25
    me = client.get("/api/me", headers={"x-user-id": "user-3"})
    assert me.json()["credits"] == 25

    duplicate = client.post(
        "/api/webhooks/lemonsqueezy",
        content=body,
        headers={"x-signature": signature, "content-type": "application/json"},
    )

    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert client.get("/api/me", headers={"x-user-id": "user-3"}).json()["credits"] == 25


def test_stripe_checkout_creates_pending_order(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, credits="0")

    class FakeBilling:
        async def create_checkout_session(self, pack_id, user):
            assert pack_id == "starter"
            assert user.user_id == "user-4"
            return CheckoutSession(
                provider_order_id="cs_test_123",
                checkout_url="https://checkout.stripe.com/c/pay/cs_test_123",
                credits=25,
                amount_cents=900,
                currency="USD",
                raw_payload={"id": "cs_test_123"},
            )

    app.state.billing = FakeBilling()
    client = TestClient(app)

    response = client.post(
        "/api/billing/checkout",
        json={"pack": "starter"},
        headers={"x-user-id": "user-4", "x-user-email": "buyer@example.com"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "stripe"
    assert payload["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_123"
    ledger = json.loads((tmp_path / "web-data" / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["orders"]["stripe:cs_test_123"]["status"] == "pending"
    assert ledger["orders"]["stripe:cs_test_123"]["credits"] == 25


def test_stripe_webhook_grants_credits_once(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, credits="0")
    body = json.dumps(
        {
            "id": "evt_checkout_completed",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_paid_123",
                    "payment_status": "paid",
                    "amount_total": 900,
                    "currency": "usd",
                    "payment_intent": "pi_123",
                    "metadata": {"user_id": "user-5", "credits": "25", "pack": "starter"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = int(time.time())
    digest = hmac.new(b"stripe-whsec", f"{timestamp}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    signature = f"t={timestamp},v1={digest}"

    response = client.post(
        "/api/webhooks/stripe",
        content=body,
        headers={"stripe-signature": signature, "content-type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["credited"] == 25
    assert client.get("/api/me", headers={"x-user-id": "user-5"}).json()["credits"] == 25

    duplicate = client.post(
        "/api/webhooks/stripe",
        content=body,
        headers={"stripe-signature": signature, "content-type": "application/json"},
    )

    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert client.get("/api/me", headers={"x-user-id": "user-5"}).json()["credits"] == 25


def test_cors_allows_vercel_frontend(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.options(
        "/api/me",
        headers={
            "origin": "https://frontend-green-one-33.vercel.app",
            "access-control-request-method": "GET",
            "access-control-request-headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://frontend-green-one-33.vercel.app"


def test_supabase_storage_uploads_and_returns_signed_url(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if "/object/sign/" in url:
                return FakeResponse({"signedURL": "/object/sign/imgclean-files/cleaned/job/output.png?token=abc"})
            return FakeResponse({"Key": "cleaned/job/output.png"})

    monkeypatch.setattr("imgclean_web.storage.httpx.Client", FakeClient)

    storage = SupabaseStorage("https://project.supabase.co", "service-key", "imgclean-files", 120)
    stored = storage.put_cleaned("job", "output.png", b"cleaned", ".png")

    assert stored.local_path is None
    assert stored.download_url == "https://project.supabase.co/storage/v1/object/sign/imgclean-files/cleaned/job/output.png?token=abc"
    assert calls[0][0] == "https://project.supabase.co/storage/v1/object/imgclean-files/cleaned/job/output.png"
    assert calls[0][1]["content"] == b"cleaned"
    assert calls[0][1]["headers"]["content-type"] == "image/png"
    assert calls[1][0] == "https://project.supabase.co/storage/v1/object/sign/imgclean-files/cleaned/job/output.png"
    assert calls[1][1]["json"] == {"expiresIn": 120}

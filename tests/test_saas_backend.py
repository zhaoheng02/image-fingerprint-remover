import hashlib
import hmac
import io
import json
import time
import struct
import zlib
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from imgclean_web.billing import CheckoutSession
from imgclean_web.app import create_app
from imgclean_web.session import create_session_token
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


def _make_watermarked_png() -> bytes:
    image = Image.new("RGB", (220, 120), color=(35, 75, 118))
    draw = ImageDraw.Draw(image)
    draw.rectangle((116, 82, 210, 108), fill=(14, 31, 50))
    draw.text((124, 88), "WATERMARK", fill=(245, 248, 255))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


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


def test_watermark_mode_auto_detects_text_without_mask_or_box(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, credits="2")

    response = client.post(
        "/api/clean",
        headers={"x-user-id": "user-watermark"},
        data={"mode": "watermark"},
        files=[("files", ("watermarked.png", _make_watermarked_png(), "image/png"))],
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["ok"] is True
    assert result["download_filename"].endswith(".png")


def test_watermark_mode_accepts_box_and_returns_download(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, credits="2")

    response = client.post(
        "/api/clean",
        headers={"x-user-id": "user-watermark"},
        data={"mode": "watermark", "watermark_box": "0,0,2,2"},
        files=[("files", ("dirty.png", _make_dirty_png(), "image/png"))],
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["ok"] is True
    assert result["download_filename"].endswith(".png")
    download = client.get(result["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "image/png"


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


def test_cors_allows_airtap_secret_header(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.options(
        "/api/airtap/posts/render",
        headers={
            "origin": "https://frontend-green-one-33.vercel.app",
            "access-control-request-method": "POST",
            "access-control-request-headers": "x-airtap-secret,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://frontend-green-one-33.vercel.app"
    assert "x-airtap-secret" in response.headers["access-control-allow-headers"]


def test_download_proxy_rejects_untrusted_hosts(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/api/download", params={"url": "https://example.com/file.png"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported download URL."


def test_download_proxy_returns_attachment(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    client = _client(tmp_path, monkeypatch)

    class FakeResponse:
        status_code = 200
        content = b"image-bytes"
        headers = {"content-type": "image/png"}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            assert url == "https://project.supabase.co/storage/v1/object/sign/imgclean-files/a.png?token=abc"
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)

    response = client.get(
        "/api/download",
        params={
            "url": "https://project.supabase.co/storage/v1/object/sign/imgclean-files/a.png?token=abc",
            "filename": "a.cleaned.png",
        },
    )

    assert response.status_code == 200
    assert response.content == b"image-bytes"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == 'attachment; filename="a.cleaned.png"'


def test_wechat_auth_status_reports_configuration(tmp_path, monkeypatch):
    for key in ("WECHAT_APP_ID", "WECHAT_APP_SECRET", "IMGCLEAN_SESSION_SECRET", "WECHAT_REDIRECT_URI"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IMGCLEAN_API_BASE_URL", "https://imgclean-api.vercel.app")
    client = _client(tmp_path, monkeypatch)

    response = client.get("/api/auth/wechat/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "wechat"
    assert payload["configured"] is False
    assert payload["missing"] == ["WECHAT_APP_ID", "WECHAT_APP_SECRET", "IMGCLEAN_SESSION_SECRET"]
    assert payload["callback_url"] == "https://imgclean-api.vercel.app/api/auth/wechat/callback"
    assert payload["callback_domain"] == "imgclean-api.vercel.app"


def test_wechat_login_redirect_uses_api_callback_from_request(tmp_path, monkeypatch):
    for key in ("WECHAT_REDIRECT_URI", "IMGCLEAN_API_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WECHAT_APP_ID", "wx-test-app")
    monkeypatch.setenv("WECHAT_APP_SECRET", "wechat-secret")
    monkeypatch.setenv("IMGCLEAN_SESSION_SECRET", "session-secret")
    client = _client(tmp_path, monkeypatch)

    response = client.get(
        "/api/auth/wechat/login",
        params={"return_to": "https://frontend-green-one-33.vercel.app/dashboard"},
        headers={"host": "api.example.com", "x-forwarded-proto": "https"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlparse(location)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "open.weixin.qq.com"
    assert parsed.path == "/connect/qrconnect"
    assert params["appid"] == ["wx-test-app"]
    assert params["redirect_uri"] == ["https://api.example.com/api/auth/wechat/callback"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["snsapi_login"]
    assert "state" in params
    assert parsed.fragment == "wechat_redirect"


def test_wechat_miniprogram_status_reports_configuration(tmp_path, monkeypatch):
    for key in ("WECHAT_MINIPROGRAM_APP_ID", "WECHAT_MINIPROGRAM_APP_SECRET", "IMGCLEAN_SESSION_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    client = TestClient(create_app())

    response = client.get("/api/auth/wechat-miniprogram/status")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "wechat_miniprogram",
        "configured": False,
        "missing": ["WECHAT_MINIPROGRAM_APP_ID", "WECHAT_MINIPROGRAM_APP_SECRET", "IMGCLEAN_SESSION_SECRET"],
    }


def test_wechat_miniprogram_login_returns_bearer_session(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "wechat_miniprogram")
    monkeypatch.setenv("IMGCLEAN_LEDGER_BACKEND", "local")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "local")
    monkeypatch.setenv("IMGCLEAN_CREDITS_ENABLED", "false")
    monkeypatch.setenv("IMGCLEAN_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("WECHAT_MINIPROGRAM_APP_ID", "wx-mini-app")
    monkeypatch.setenv("WECHAT_MINIPROGRAM_APP_SECRET", "mini-secret")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"openid": "openid-123", "unionid": "union-456", "session_key": "do-not-return"}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params):
            assert url == "https://api.weixin.qq.com/sns/jscode2session"
            assert params == {
                "appid": "wx-mini-app",
                "secret": "mini-secret",
                "js_code": "wx-login-code",
                "grant_type": "authorization_code",
            }
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)
    client = TestClient(create_app())

    response = client.post("/api/auth/wechat-miniprogram/login", json={"code": "wx-login-code"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "wechat_miniprogram"
    assert payload["token_type"] == "Bearer"
    assert payload["user_id"] == "wechat_mp:openid-123"
    assert payload["openid"] == "openid-123"
    assert "session_key" not in payload

    me = client.get("/api/me", headers={"authorization": f"Bearer {payload['token']}"})
    assert me.status_code == 200
    assert me.json()["authenticated"] is True
    assert me.json()["user_id"] == "wechat_mp:openid-123"
    assert me.json()["credits"] is None


def test_wechat_miniprogram_clean_skips_credits_when_billing_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "wechat_miniprogram")
    monkeypatch.setenv("IMGCLEAN_LEDGER_BACKEND", "local")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "local")
    monkeypatch.setenv("IMGCLEAN_INITIAL_CREDITS", "0")
    monkeypatch.setenv("IMGCLEAN_CREDITS_ENABLED", "false")
    monkeypatch.setenv("IMGCLEAN_SESSION_SECRET", "session-secret")
    token = create_session_token(
        {"sub": "wechat_mp:openid-123", "provider": "wechat_miniprogram"},
        "session-secret",
    )
    client = TestClient(create_app())

    response = client.post(
        "/api/clean",
        headers={"authorization": f"Bearer {token}"},
        data={"mode": "safe"},
        files=[("files", ("dirty.png", _make_dirty_png(), "image/png"))],
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["ok"] is True
    assert result["credits_remaining"] is None


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

"""FastAPI web app for browser-based image cleaning."""
from __future__ import annotations

import os
import re
import time
import uuid
import hmac
import hashlib
import inspect as pyinspect
from urllib.parse import quote, urlencode, urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from imgclean.clean import VALID_MODES, clean_bytes
from imgclean.detect import detect_format, inspect as inspect_file
from imgclean.findings import InspectReport
from .billing import BillingNotConfigured, StripeBilling, UnknownCreditPack
from .identity import Authenticator, User
from .ledger import InsufficientCredits, LocalLedger, SupabaseLedger
from .session import create_session_token, read_session_token
from .storage import LocalStorage, R2Storage, SupabaseStorage
from .watermark import WatermarkRequest, parse_watermark_box, remove_watermark_bytes


SUPPORTED_FORMATS = {"png", "jpeg"}
MIME_TYPES = {"png": "image/png", "jpeg": "image/jpeg"}
DEFAULT_MAX_UPLOAD_MB = 25
DEFAULT_TTL_SECONDS = 6 * 60 * 60
WEB_MODES = set(VALID_MODES) | {"watermark"}
WECHAT_AUTH_URL = "https://open.weixin.qq.com/connect/qrconnect"
WECHAT_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
WECHAT_USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"


@dataclass(frozen=True)
class WebSettings:
    data_dir: Path
    max_upload_bytes: int
    ttl_seconds: int
    auth_mode: str
    ledger_backend: str
    storage_backend: str
    initial_credits: int
    supabase_url: str
    supabase_secret_key: str
    supabase_storage_bucket: str
    r2_endpoint_url: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    lemonsqueezy_webhook_secret: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_starter: str
    stripe_price_growth: str
    stripe_api_base_url: str
    app_base_url: str
    cors_origins: tuple[str, ...]
    cors_origin_regex: str
    session_secret: str
    wechat_app_id: str
    wechat_app_secret: str
    wechat_redirect_uri: str

    @classmethod
    def from_env(cls) -> "WebSettings":
        data_dir = Path(os.environ.get("IMGCLEAN_WEB_DATA_DIR", ".web_data"))
        max_mb = int(os.environ.get("IMGCLEAN_MAX_UPLOAD_MB", str(DEFAULT_MAX_UPLOAD_MB)))
        ttl = int(os.environ.get("IMGCLEAN_JOB_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))
        cors_origins = tuple(
            origin.strip()
            for origin in os.environ.get(
                "IMGCLEAN_CORS_ORIGINS",
                "http://localhost:3000,http://127.0.0.1:3000,https://frontend-green-one-33.vercel.app",
            ).split(",")
            if origin.strip()
        )
        return cls(
            data_dir=data_dir,
            max_upload_bytes=max_mb * 1024 * 1024,
            ttl_seconds=ttl,
            auth_mode=os.environ.get("IMGCLEAN_AUTH_MODE", "none"),
            ledger_backend=os.environ.get("IMGCLEAN_LEDGER_BACKEND", "local"),
            storage_backend=os.environ.get("IMGCLEAN_STORAGE_BACKEND", "local"),
            initial_credits=int(os.environ.get("IMGCLEAN_INITIAL_CREDITS", "0")),
            supabase_url=os.environ.get("SUPABASE_URL", ""),
            supabase_secret_key=os.environ.get("SUPABASE_SECRET_KEY", ""),
            supabase_storage_bucket=os.environ.get("SUPABASE_STORAGE_BUCKET", "imgclean-files"),
            r2_endpoint_url=os.environ.get("R2_ENDPOINT_URL", ""),
            r2_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
            r2_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
            r2_bucket=os.environ.get("R2_BUCKET", ""),
            lemonsqueezy_webhook_secret=os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", ""),
            stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", ""),
            stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
            stripe_price_starter=os.environ.get("STRIPE_PRICE_STARTER", ""),
            stripe_price_growth=os.environ.get("STRIPE_PRICE_GROWTH", ""),
            stripe_api_base_url=os.environ.get("STRIPE_API_BASE_URL", "https://api.stripe.com"),
            app_base_url=os.environ.get(
                "APP_BASE_URL",
                os.environ.get("PUBLIC_APP_URL", os.environ.get("FRONTEND_URL", "http://127.0.0.1:3000")),
            ),
            cors_origins=cors_origins,
            cors_origin_regex=os.environ.get("IMGCLEAN_CORS_ORIGIN_REGEX", r"https://.*\.vercel\.app"),
            session_secret=os.environ.get("IMGCLEAN_SESSION_SECRET", ""),
            wechat_app_id=os.environ.get("WECHAT_APP_ID", ""),
            wechat_app_secret=os.environ.get("WECHAT_APP_SECRET", ""),
            wechat_redirect_uri=os.environ.get("WECHAT_REDIRECT_URI", ""),
        )


def create_app() -> FastAPI:
    settings = WebSettings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="Image Fingerprint Remover")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_origin_regex=settings.cors_origin_regex or None,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["authorization", "content-type"],
        allow_credentials=True,
    )
    app.state.settings = settings
    app.state.auth = Authenticator(
        settings.auth_mode,
        settings.supabase_url,
        settings.supabase_secret_key,
        settings.session_secret,
    )
    app.state.ledger = _build_ledger(settings)
    app.state.storage = _build_storage(settings)
    app.state.billing = _build_billing(settings)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _render_index(settings)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/me")
    async def me(request: Request) -> dict[str, Any]:
        user = await app.state.auth.current_user(request)
        if user is None:
            return {"authenticated": False, "credits": None}
        account = await _maybe_await(app.state.ledger.get_account(user))
        return {
            "authenticated": True,
            "user_id": account.user_id,
            "email": account.email,
            "name": user.name,
            "credits": account.credits,
        }

    @app.get("/api/auth/wechat/status")
    def wechat_status() -> dict[str, Any]:
        missing = []
        if not settings.wechat_app_id:
            missing.append("WECHAT_APP_ID")
        if not settings.wechat_app_secret:
            missing.append("WECHAT_APP_SECRET")
        if not settings.session_secret:
            missing.append("IMGCLEAN_SESSION_SECRET")
        return {
            "provider": "wechat",
            "configured": not missing,
            "missing": missing,
        }

    @app.post("/api/clean")
    async def clean_endpoint(
        request: Request,
        mode: str = Form("safe"),
        watermark_box: str = Form(""),
        files: list[UploadFile] = File(...),
        watermark_mask: UploadFile | None = File(None),
    ) -> dict[str, Any]:
        if mode not in WEB_MODES:
            raise HTTPException(status_code=400, detail=f"Unsupported mode: {mode}")
        if not files:
            raise HTTPException(status_code=400, detail="Upload at least one file.")

        user = await app.state.auth.current_user(request)
        mask_bytes = await watermark_mask.read() if watermark_mask is not None else None
        if watermark_mask is not None:
            await watermark_mask.close()
        try:
            watermark_request = WatermarkRequest(mask_bytes=mask_bytes, box=parse_watermark_box(watermark_box))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        app.state.storage.purge_expired() if hasattr(app.state.storage, "purge_expired") else None
        results: list[dict[str, Any]] = []
        for upload in files:
            try:
                results.append(
                    await _process_upload(upload, mode, settings, app.state.storage, app.state.ledger, user, watermark_request)
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except InsufficientCredits:
                raise HTTPException(status_code=402, detail="Insufficient credits.")
            except HTTPException as exc:
                results.append({
                    "ok": False,
                    "filename": upload.filename or "image",
                    "error": str(exc.detail),
                })
            finally:
                await upload.close()
        return {"mode": mode, "results": results}

    @app.get("/api/auth/wechat/login")
    def wechat_login(return_to: str = "") -> RedirectResponse:
        if not settings.wechat_app_id or not settings.wechat_app_secret:
            raise HTTPException(status_code=503, detail="WeChat OAuth is not configured.")
        if not settings.session_secret:
            raise HTTPException(status_code=503, detail="Session secret is not configured.")
        redirect_uri = settings.wechat_redirect_uri or f"{settings.app_base_url.rstrip('/')}/api/auth/wechat/callback"
        state = create_session_token(
            {
                "kind": "wechat_oauth_state",
                "return_to": _safe_return_to(return_to, settings.app_base_url),
            },
            settings.session_secret,
            ttl_seconds=10 * 60,
        )
        query = urlencode(
            {
                "appid": settings.wechat_app_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "snsapi_login",
                "state": state,
            },
            quote_via=quote,
        )
        return RedirectResponse(f"{WECHAT_AUTH_URL}?{query}#wechat_redirect", status_code=302)

    @app.get("/api/auth/wechat/callback")
    async def wechat_callback(code: str, state: str) -> RedirectResponse:
        if not settings.wechat_app_id or not settings.wechat_app_secret or not settings.session_secret:
            raise HTTPException(status_code=503, detail="WeChat OAuth is not configured.")
        try:
            state_payload = read_session_token(state, settings.session_secret)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid OAuth state.")
        if state_payload.get("kind") != "wechat_oauth_state":
            raise HTTPException(status_code=401, detail="Invalid OAuth state.")

        async with httpx.AsyncClient(timeout=30) as client:
            token_response = await client.get(
                WECHAT_TOKEN_URL,
                params={
                    "appid": settings.wechat_app_id,
                    "secret": settings.wechat_app_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
            token_response.raise_for_status()
            token_payload = token_response.json()
            if "errcode" in token_payload:
                raise HTTPException(status_code=401, detail=token_payload.get("errmsg", "WeChat login failed."))
            user_response = await client.get(
                WECHAT_USERINFO_URL,
                params={
                    "access_token": token_payload["access_token"],
                    "openid": token_payload["openid"],
                    "lang": "zh_CN",
                },
            )
            user_response.raise_for_status()
            profile = user_response.json()
            if "errcode" in profile:
                raise HTTPException(status_code=401, detail=profile.get("errmsg", "WeChat profile failed."))

        union_or_open_id = profile.get("unionid") or token_payload["openid"]
        session = create_session_token(
            {
                "sub": f"wechat:{union_or_open_id}",
                "provider": "wechat",
                "name": profile.get("nickname", ""),
                "email": "",
            },
            settings.session_secret,
        )
        response = RedirectResponse(_safe_return_to(str(state_payload.get("return_to") or ""), settings.app_base_url), status_code=302)
        response.set_cookie(
            "imgclean_session",
            session,
            httponly=True,
            secure=settings.app_base_url.startswith("https://"),
            samesite="none" if settings.app_base_url.startswith("https://") else "lax",
            max_age=30 * 24 * 60 * 60,
        )
        return response

    @app.post("/api/auth/logout")
    def logout() -> Response:
        response = Response(content='{"ok":true}', media_type="application/json")
        response.delete_cookie("imgclean_session", samesite="none" if settings.app_base_url.startswith("https://") else "lax")
        return response

    @app.post("/api/billing/checkout")
    async def create_checkout(request: Request) -> dict[str, Any]:
        user = await app.state.auth.current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Login required.")
        payload = await request.json()
        pack_id = str(payload.get("pack") or "starter")
        try:
            session = await app.state.billing.create_checkout_session(pack_id, user)
        except UnknownCreditPack:
            raise HTTPException(status_code=400, detail="Unknown credit pack.")
        except BillingNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        await _maybe_await(
            app.state.ledger.upsert_order(
                "stripe",
                session.provider_order_id,
                user.user_id,
                session.credits,
                session.amount_cents,
                session.currency,
                "pending",
                session.raw_payload,
            )
        )
        return {
            "provider": "stripe",
            "checkout_url": session.checkout_url,
            "credits": session.credits,
            "order_id": session.provider_order_id,
        }

    @app.post("/api/webhooks/stripe")
    async def stripe_webhook(request: Request) -> dict[str, Any]:
        body = await request.body()
        if settings.stripe_webhook_secret:
            _verify_stripe_signature(body, request.headers.get("stripe-signature", ""), settings.stripe_webhook_secret)
        payload = await request.json()
        event_name = payload.get("type", "")
        event_id = payload.get("id") or _payload_fingerprint(body)
        is_new = await _maybe_await(app.state.ledger.record_webhook_event("stripe", event_id, event_name, payload))
        if not is_new:
            return {"ok": True, "duplicate": True}
        if event_name != "checkout.session.completed":
            return {"ok": True, "ignored": True}

        checkout = payload.get("data", {}).get("object") or {}
        if checkout.get("payment_status") not in {"paid", "no_payment_required"}:
            return {"ok": True, "ignored": True}
        metadata = checkout.get("metadata") or {}
        user_id = metadata.get("user_id") or checkout.get("client_reference_id")
        credits = int(metadata.get("credits") or 0)
        if not user_id or credits <= 0:
            raise HTTPException(status_code=400, detail="Missing user_id or credits in Stripe metadata.")

        provider_order_id = checkout.get("id") or event_id
        amount_cents = checkout.get("amount_total")
        currency = (checkout.get("currency") or "usd").upper()
        await _maybe_await(
            app.state.ledger.upsert_order(
                "stripe",
                provider_order_id,
                user_id,
                credits,
                amount_cents,
                currency,
                "paid",
                checkout,
            )
        )
        remaining = await _maybe_await(
            app.state.ledger.grant_credits(
                user_id,
                credits,
                "stripe_checkout",
                {
                    "event_id": event_id,
                    "checkout_session_id": provider_order_id,
                    "payment_intent": checkout.get("payment_intent"),
                    "amount_total": amount_cents,
                    "currency": currency,
                },
            )
        )
        return {"ok": True, "credited": credits, "credits": remaining}

    @app.post("/api/webhooks/lemonsqueezy")
    async def lemonsqueezy_webhook(request: Request) -> dict[str, Any]:
        body = await request.body()
        if settings.lemonsqueezy_webhook_secret:
            signature = request.headers.get("x-signature", "")
            expected = hmac.new(
                settings.lemonsqueezy_webhook_secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise HTTPException(status_code=401, detail="Invalid webhook signature.")

        payload = await request.json()
        event_name = payload.get("meta", {}).get("event_name", "")
        event_id = _lemonsqueezy_event_id(payload, body)
        is_new = await _maybe_await(app.state.ledger.record_webhook_event("lemonsqueezy", event_id, event_name, payload))
        if not is_new:
            return {"ok": True, "duplicate": True}
        if event_name not in {"order_created", "subscription_payment_success"}:
            return {"ok": True, "ignored": True}

        custom = payload.get("meta", {}).get("custom_data") or {}
        user_id = custom.get("user_id")
        credits = int(custom.get("credits") or 0)
        if not user_id or credits <= 0:
            raise HTTPException(status_code=400, detail="Missing user_id or credits in webhook custom_data.")
        data = payload.get("data", {})
        attributes = data.get("attributes", {})
        await _maybe_await(
            app.state.ledger.upsert_order(
                "lemonsqueezy",
                str(data.get("id") or event_id),
                user_id,
                credits,
                attributes.get("total"),
                (attributes.get("currency") or "").upper() or None,
                attributes.get("status") or event_name,
                data,
            )
        )
        remaining = await _maybe_await(
            app.state.ledger.grant_credits(
                user_id,
                credits,
                "lemonsqueezy",
                {
                    "event_name": event_name,
                    "order_id": payload.get("data", {}).get("id"),
                    "status": payload.get("data", {}).get("attributes", {}).get("status"),
                },
            )
        )
        return {"ok": True, "credited": credits, "credits": remaining}

    @app.get("/download/{job_id}")
    def download(job_id: str) -> FileResponse:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(status_code=404, detail="File not found.")
        meta = _job_meta_path(settings, job_id)
        if not meta.exists():
            raise HTTPException(status_code=404, detail="File not found.")

        lines = meta.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2:
            raise HTTPException(status_code=404, detail="File not found.")
        filename, output_name = lines[0], lines[1]
        output_path = meta.parent / output_name
        if not output_path.exists():
            raise HTTPException(status_code=404, detail="File not found.")

        fmt = detect_format(output_path.read_bytes())
        return FileResponse(
            output_path,
            media_type=MIME_TYPES.get(fmt, "application/octet-stream"),
            filename=filename,
        )

    @app.get("/api/download")
    async def download_proxy(url: str, filename: str = "cleaned-image") -> Response:
        parsed = urlparse(url)
        allowed_hosts = {urlparse(settings.supabase_url).netloc} if settings.supabase_url else set()
        if parsed.scheme not in {"http", "https"} or parsed.netloc not in allowed_hosts:
            raise HTTPException(status_code=400, detail="Unsupported download URL.")
        async with httpx.AsyncClient(timeout=60) as client:
            remote = await client.get(url)
        if remote.status_code >= 400:
            raise HTTPException(status_code=remote.status_code, detail="Download failed.")
        safe_name = _safe_filename(filename)
        return Response(
            content=remote.content,
            media_type=remote.headers.get("content-type", "application/octet-stream"),
            headers={"content-disposition": f'attachment; filename="{safe_name}"'},
        )

    return app


async def _process_upload(
    upload: UploadFile,
    mode: str,
    settings: WebSettings,
    storage: Any,
    ledger: Any,
    user: User | None,
    watermark_request: WatermarkRequest | None = None,
) -> dict[str, Any]:
    original_name = _safe_filename(upload.filename or "image")
    data = await upload.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        max_mb = settings.max_upload_bytes // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"{original_name} exceeds {max_mb} MB.")

    fmt = detect_format(data)
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=415, detail=f"{original_name} is not a supported PNG/JPEG image.")

    job_id = uuid.uuid4().hex
    job_dir = settings.data_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    input_path = job_dir / f"input{_suffix_for_format(fmt)}"
    input_path.write_bytes(data)
    if user is not None:
        storage.put_original(job_id, original_name, data, _suffix_for_format(fmt))
    input_report = inspect_file(input_path)

    if mode == "watermark":
        cleaned, fmt_out = remove_watermark_bytes(data, fmt, watermark_request or WatermarkRequest())
    else:
        cleaned, fmt_out = clean_bytes(data, mode=mode)
    output_suffix = _suffix_for_format(fmt_out)
    cleaned_name = f"{Path(original_name).stem}.cleaned{output_suffix}"
    stored = storage.put_cleaned(job_id, cleaned_name, cleaned, output_suffix)
    output_path = stored.local_path
    if output_path is None:
        output_path = job_dir / f"output{output_suffix}"
        output_path.write_bytes(cleaned)
    output_report = inspect_file(output_path)

    credits_remaining = None
    if user is not None:
        credits_remaining = await _maybe_await(
            ledger.consume_credit(
                user,
                job_id,
                {
                    "filename": original_name,
                    "mode": mode,
                    "input_findings": len(input_report.findings),
                    "output_findings": len(output_report.findings),
                },
            )
        )
    else:
        # Keep only the cleaned file for anonymous/local download; the original upload is not retained.
        input_path.unlink(missing_ok=True)

    return {
        "ok": True,
        "filename": original_name,
        "download_filename": cleaned_name,
        "download_url": stored.download_url,
        "credits_remaining": credits_remaining,
        "input": _public_report(input_report, original_name),
        "output": _public_report(output_report, cleaned_name),
        "input_size": len(data),
        "output_size": len(cleaned),
    }


def _public_report(report: InspectReport, display_path: str) -> dict[str, Any]:
    data = report.to_dict()
    data["path"] = display_path
    return data


def _safe_filename(name: str) -> str:
    stem = Path(name).name.strip() or "image"
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" .")
    return cleaned or "image"


def _suffix_for_format(fmt: str) -> str:
    return ".jpg" if fmt == "jpeg" else ".png"


def _job_meta_path(settings: WebSettings, job_id: str) -> Path:
    return settings.data_dir / job_id / "job.txt"


def _build_ledger(settings: WebSettings) -> Any:
    if settings.ledger_backend == "supabase":
        return SupabaseLedger(settings.supabase_url, settings.supabase_secret_key, settings.initial_credits)
    return LocalLedger(settings.data_dir / "ledger.json", settings.initial_credits)


def _build_storage(settings: WebSettings) -> Any:
    if settings.storage_backend == "supabase":
        return SupabaseStorage(
            settings.supabase_url,
            settings.supabase_secret_key,
            settings.supabase_storage_bucket,
            settings.ttl_seconds,
        )
    if settings.storage_backend == "r2":
        return R2Storage(
            settings.r2_endpoint_url,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_bucket,
            settings.ttl_seconds,
        )
    return LocalStorage(settings.data_dir, settings.ttl_seconds)


def _build_billing(settings: WebSettings) -> StripeBilling:
    return StripeBilling(
        settings.stripe_secret_key,
        settings.app_base_url,
        settings.stripe_price_starter,
        settings.stripe_price_growth,
        settings.stripe_api_base_url,
    )


def _safe_return_to(return_to: str, app_base_url: str) -> str:
    fallback = f"{app_base_url.rstrip('/')}/dashboard"
    if not return_to:
        return fallback
    parsed = urlparse(return_to)
    base = urlparse(app_base_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc == base.netloc:
        return return_to
    if return_to.startswith("/"):
        return f"{app_base_url.rstrip('/')}{return_to}"
    return fallback


async def _maybe_await(value: Any) -> Any:
    if pyinspect.isawaitable(value):
        return await value
    return value


def _verify_stripe_signature(body: bytes, signature_header: str, secret: str, tolerance_seconds: int = 300) -> None:
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts.setdefault(key, []).append(value)
    timestamps = parts.get("t") or []
    signatures = parts.get("v1") or []
    if not timestamps or not signatures:
        raise HTTPException(status_code=401, detail="Invalid Stripe signature.")
    try:
        timestamp = int(timestamps[0])
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Stripe signature.")
    if abs(time.time() - timestamp) > tolerance_seconds:
        raise HTTPException(status_code=401, detail="Expired Stripe signature.")
    expected = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, signature) for signature in signatures):
        raise HTTPException(status_code=401, detail="Invalid Stripe signature.")


def _payload_fingerprint(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _lemonsqueezy_event_id(payload: dict[str, Any], body: bytes) -> str:
    meta = payload.get("meta") or {}
    event_name = meta.get("event_name") or "unknown"
    explicit = meta.get("event_id") or meta.get("webhook_id")
    data_id = payload.get("data", {}).get("id")
    return str(explicit or f"{event_name}:{data_id or _payload_fingerprint(body)}")


def _render_index(settings: WebSettings) -> str:
    max_mb = settings.max_upload_bytes // (1024 * 1024)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Image Fingerprint Remover</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #171b21;
      --muted: #667085;
      --line: #d8dee8;
      --panel: #f7f9fc;
      --teal: #047a7a;
      --blue: #1d5fd1;
      --amber: #b76b10;
      --danger: #b42318;
      --ok: #147a3f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: #ffffff;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }}
    .wrap {{
      width: min(1120px, calc(100vw - 32px));
      margin: 0 auto;
    }}
    .topbar {{
      min-height: 72px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}
    h1 {{
      margin: 0;
      font-size: 22px;
      line-height: 1.2;
      letter-spacing: 0;
    }}
    .status-pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px 12px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    main {{
      display: grid;
      grid-template-columns: minmax(0, 420px) minmax(0, 1fr);
      gap: 28px;
      padding: 28px 0 40px;
    }}
    section {{
      min-width: 0;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 18px;
    }}
    label, legend {{
      display: block;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .dropzone {{
      display: grid;
      place-items: center;
      min-height: 190px;
      border: 2px dashed #a9b5c8;
      border-radius: 8px;
      background: #ffffff;
      text-align: center;
      padding: 22px;
      cursor: pointer;
    }}
    .dropzone strong {{
      display: block;
      font-size: 18px;
      margin-bottom: 8px;
    }}
    .dropzone span {{
      color: var(--muted);
      font-size: 14px;
    }}
    #files {{
      position: absolute;
      inline-size: 1px;
      block-size: 1px;
      opacity: 0;
    }}
    .pick-button {{
      width: auto;
      min-width: 160px;
      margin: 14px auto 0;
      padding: 10px 14px;
      font-size: 14px;
    }}
    fieldset {{
      border: 0;
      padding: 0;
      margin: 18px 0 0;
    }}
    .modes {{
      display: grid;
      gap: 8px;
    }}
    .mode {{
      display: flex;
      align-items: center;
      gap: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      padding: 12px;
      cursor: pointer;
    }}
    .mode input {{
      inline-size: 18px;
      block-size: 18px;
      accent-color: var(--teal);
    }}
    .mode span {{
      color: var(--muted);
      font-size: 13px;
      margin-left: auto;
    }}
    .watermark-options {{
      display: none;
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #ffffff;
    }}
    .watermark-options.active {{
      display: block;
    }}
    .watermark-options label {{
      margin-top: 10px;
      font-size: 13px;
    }}
    .watermark-options input[type="text"],
    .watermark-options input[type="file"] {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfcfe;
    }}
    button {{
      width: 100%;
      margin-top: 18px;
      border: 0;
      border-radius: 8px;
      background: var(--blue);
      color: #ffffff;
      font-weight: 800;
      font-size: 16px;
      padding: 13px 16px;
      cursor: pointer;
    }}
    button:disabled {{
      cursor: wait;
      background: #94a3b8;
    }}
    .fineprint {{
      margin: 14px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }}
    .results-head {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 12px;
      margin-bottom: 14px;
    }}
    h2 {{
      margin: 0;
      font-size: 18px;
      letter-spacing: 0;
    }}
    .empty {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 28px;
      color: var(--muted);
      background: #fbfcfe;
    }}
    .preview-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 12px;
    }}
    .preview-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: #ffffff;
    }}
    .preview-card img {{
      display: block;
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: contain;
      background: #eef2f7;
    }}
    .preview-meta {{
      padding: 10px;
      border-top: 1px solid var(--line);
    }}
    .preview-meta strong {{
      display: block;
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .preview-meta span {{
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
    }}
    .result {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 12px;
      background: #ffffff;
    }}
    .result-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
      font-weight: 800;
      overflow-wrap: anywhere;
    }}
    .badge {{
      flex: 0 0 auto;
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 800;
      background: #e8f5ee;
      color: var(--ok);
    }}
    .badge.warn {{
      background: #fff3df;
      color: var(--amber);
    }}
    .badge.err {{
      background: #ffe8e5;
      color: var(--danger);
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfcfe;
    }}
    .metric b {{
      display: block;
      font-size: 18px;
      margin-bottom: 3px;
    }}
    .metric span {{
      color: var(--muted);
      font-size: 12px;
    }}
    .findings {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 0 0 12px;
      padding: 0;
      list-style: none;
    }}
    .findings li {{
      border: 1px solid #f1c27d;
      border-radius: 999px;
      padding: 5px 8px;
      color: #7a4308;
      background: #fff8ec;
      font-size: 12px;
    }}
    a.download {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      border-radius: 8px;
      padding: 8px 12px;
      background: var(--teal);
      color: #ffffff;
      text-decoration: none;
      font-weight: 800;
    }}
    @media (max-width: 820px) {{
      .topbar {{
        align-items: flex-start;
        flex-direction: column;
        padding: 18px 0;
      }}
      main {{
        grid-template-columns: 1fr;
        gap: 18px;
      }}
      .metrics {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <h1>Image Fingerprint Remover</h1>
      <div class="status-pill">PNG / JPEG · {max_mb} MB</div>
    </div>
  </header>
  <main class="wrap">
    <section>
      <form id="clean-form" class="panel">
        <label for="files">图片</label>
        <label class="dropzone" id="dropzone" for="files">
          <span>
            <strong id="file-title">选择或拖入图片</strong>
            <span id="file-subtitle">可一次处理多张 PNG / JPEG</span>
            <button class="pick-button" type="button" id="pick-files">选择本地图片</button>
          </span>
        </label>
        <input id="files" name="files" type="file" accept="image/png,image/jpeg" multiple required>
        <fieldset>
          <legend>模式</legend>
          <div class="modes">
            <label class="mode"><input type="radio" name="mode" value="safe" checked>安全<span>元数据</span></label>
            <label class="mode"><input type="radio" name="mode" value="paranoid">深度<span>重编码</span></label>
            <label class="mode"><input type="radio" name="mode" value="nuclear">核弹<span>扰动像素</span></label>
            <label class="mode"><input type="radio" name="mode" value="watermark">去水印<span>遮罩 / 区域</span></label>
          </div>
          <div class="watermark-options" id="watermark-options">
            <p class="fineprint">默认会自动检测常见文字水印；效果不准时可填写像素区域 x,y,w,h，或上传黑白遮罩图，白色区域为水印。</p>
            <label for="watermark-box">水印区域</label>
            <input id="watermark-box" name="watermark_box" type="text" placeholder="例如：120,80,300,90">
            <label for="watermark-mask">水印遮罩图</label>
            <input id="watermark-mask" name="watermark_mask" type="file" accept="image/png,image/jpeg">
          </div>
        </fieldset>
        <button id="submit" type="submit">开始处理</button>
        <p class="fineprint">上传文件仅用于本次处理，原图不会保留；下载文件会生成短期有效的清理结果。</p>
      </form>
    </section>
    <section>
      <div class="results-head">
        <h2>结果</h2>
        <span id="summary" class="fineprint"></span>
      </div>
      <div id="results" class="empty">等待上传</div>
    </section>
  </main>
  <script>
    const form = document.getElementById('clean-form');
    const fileInput = document.getElementById('files');
    const fileTitle = document.getElementById('file-title');
    const fileSubtitle = document.getElementById('file-subtitle');
    const results = document.getElementById('results');
    const submit = document.getElementById('submit');
    const summary = document.getElementById('summary');
    const dropzone = document.getElementById('dropzone');
    const pickFiles = document.getElementById('pick-files');
    const watermarkOptions = document.getElementById('watermark-options');
    let previewUrls = [];

    function updateFileLabel() {{
      const count = fileInput.files.length;
      fileTitle.textContent = count ? `${{count}} 张图片已选择` : '选择或拖入图片';
      fileSubtitle.textContent = count ? [...fileInput.files].map(f => f.name).join(' · ') : '可一次处理多张 PNG / JPEG';
      renderSelectedPreviews();
    }}

    function selectedMode() {{
      return form.querySelector('input[name="mode"]:checked').value;
    }}

    function updateModeOptions() {{
      watermarkOptions.classList.toggle('active', selectedMode() === 'watermark');
    }}

    function fmtBytes(value) {{
      if (value < 1024) return `${{value}} B`;
      if (value < 1024 * 1024) return `${{(value / 1024).toFixed(1)}} KB`;
      return `${{(value / 1024 / 1024).toFixed(1)}} MB`;
    }}

    function escapeHtml(value) {{
      return String(value).replace(/[&<>"']/g, char => ({{
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }}[char]));
    }}

    function cleanupPreviewUrls() {{
      previewUrls.forEach(url => URL.revokeObjectURL(url));
      previewUrls = [];
    }}

    function renderSelectedPreviews() {{
      cleanupPreviewUrls();
      const files = [...fileInput.files];
      if (!files.length) {{
        results.className = 'empty';
        results.textContent = '等待选择图片';
        summary.textContent = '';
        return;
      }}
      const previews = files.map(file => {{
        const url = URL.createObjectURL(file);
        previewUrls.push(url);
        return `<article class="preview-card">
          <img src="${{url}}" alt="${{escapeHtml(file.name)}} 预览">
          <div class="preview-meta">
            <strong>${{escapeHtml(file.name)}}</strong>
            <span>${{fmtBytes(file.size)}} · 待处理</span>
          </div>
        </article>`;
      }}).join('');
      results.className = '';
      results.innerHTML = `<div class="preview-grid">${{previews}}</div>`;
      summary.textContent = `${{files.length}} 张待处理`;
    }}

    function findingList(report) {{
      const entries = Object.entries(report.by_category || {{}});
      if (!entries.length) return '<ul class="findings"><li>无中高风险项</li></ul>';
      return `<ul class="findings">${{entries.map(([k, v]) => `<li>${{k}} × ${{v}}</li>`).join('')}}</ul>`;
    }}

    function proxyDownloadHref(item) {{
      if (!item.download_url) return '#';
      const filename = item.download_filename || 'cleaned-image';
      if (item.download_url.startsWith('http')) {{
        return `/api/download?url=${{encodeURIComponent(item.download_url)}}&filename=${{encodeURIComponent(filename)}}`;
      }}
      return item.download_url;
    }}

    function renderResult(item) {{
      if (!item.ok) {{
        return `<article class="result">
          <div class="result-title">${{item.filename}} <span class="badge err">失败</span></div>
          <p class="fineprint">${{item.error}}</p>
        </article>`;
      }}
      const cleanBadge = item.output.is_clean ? '<span class="badge">已清理</span>' : '<span class="badge warn">需复查</span>';
      return `<article class="result">
        <div class="result-title">${{item.filename}} ${{cleanBadge}}</div>
        <div class="metrics">
          <div class="metric"><b>${{item.input.finding_count}}</b><span>清理前</span></div>
          <div class="metric"><b>${{item.output.finding_count}}</b><span>清理后</span></div>
          <div class="metric"><b>${{fmtBytes(item.output_size)}}</b><span>输出大小</span></div>
        </div>
        ${{findingList(item.input)}}
        <a class="download" href="${{proxyDownloadHref(item)}}" download="${{item.download_filename || 'cleaned-image'}}">下载 cleaned 图片</a>
      </article>`;
    }}

    fileInput.addEventListener('change', updateFileLabel);
    pickFiles.addEventListener('click', event => {{
      event.preventDefault();
      fileInput.click();
    }});
    form.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', updateModeOptions));
    dropzone.addEventListener('dragover', event => {{
      event.preventDefault();
      dropzone.style.borderColor = '#1d5fd1';
    }});
    dropzone.addEventListener('dragleave', () => {{
      dropzone.style.borderColor = '#a9b5c8';
    }});
    dropzone.addEventListener('drop', event => {{
      event.preventDefault();
      dropzone.style.borderColor = '#a9b5c8';
      if (event.dataTransfer.files.length) {{
        fileInput.files = event.dataTransfer.files;
        updateFileLabel();
      }}
    }});
    updateModeOptions();
    form.addEventListener('submit', async event => {{
      event.preventDefault();
      if (!fileInput.files.length) {{
        results.className = 'empty';
        results.textContent = '请先选择本地图片。';
        return;
      }}
      submit.disabled = true;
      submit.textContent = '处理中...';
      summary.textContent = '处理中...';
      const body = new FormData(form);
      try {{
        const response = await fetch('/api/clean', {{ method: 'POST', body }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || '处理失败');
        cleanupPreviewUrls();
        results.className = '';
        results.innerHTML = payload.results.map(renderResult).join('');
        const okCount = payload.results.filter(x => x.ok).length;
        summary.textContent = `${{okCount}} / ${{payload.results.length}} 完成`;
      }} catch (err) {{
        results.className = 'empty';
        results.textContent = err.message;
      }} finally {{
        submit.disabled = false;
        submit.textContent = '开始处理';
      }}
    }});
  </script>
</body>
</html>"""

"""FastAPI web app for browser-based image cleaning."""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
import hmac
import base64
import hashlib
import html
import inspect as pyinspect
import io
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from PIL import Image, UnidentifiedImageError

from imgclean.clean import VALID_MODES, clean_bytes
from imgclean.detect import detect_format, inspect as inspect_file
from imgclean.findings import InspectReport
from .airtap_relay import (
    LocalAirtapRelayStore,
    SupabaseAirtapRelayStore,
    _is_direct_video_url,
    _youtube_urls,
    _youtube_video_id,
    render_wechat_pushplus,
    render_xiaohongshu_note,
)
from .billing import BillingNotConfigured, StripeBilling, UnknownCreditPack
from .identity import Authenticator, User
from .ledger import InsufficientCredits, LocalLedger, SupabaseLedger
from .session import InvalidSession, create_session_token, read_session_token
from .storage import LocalStorage, R2Storage, SupabaseStorage
from .watermark import WatermarkRequest, parse_watermark_box, remove_watermark_bytes


SUPPORTED_FORMATS = {"png", "jpeg"}
MIME_TYPES = {"png": "image/png", "jpeg": "image/jpeg"}
DEFAULT_MAX_UPLOAD_MB = 25
DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_AIRTAP_SIGNED_URL_TTL_SECONDS = 3 * 24 * 60 * 60
WEB_MODES = set(VALID_MODES) | {"watermark"}
WECHAT_AUTH_URL = "https://open.weixin.qq.com/connect/qrconnect"
WECHAT_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
WECHAT_USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"
WECHAT_MINIPROGRAM_SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
AIRTAP_MAX_AVATAR_BYTES = 2 * 1024 * 1024
AIRTAP_AVATAR_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
AIRTAP_WECHAT_INLINE_SOURCE_MAX_BYTES = 6 * 1024 * 1024
AIRTAP_WECHAT_INLINE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
AIRTAP_WECHAT_VIDEO_CONTENT_TYPES = {"video/mp4", "video/webm", "video/quicktime", "video/x-m4v"}
AIRTAP_WECHAT_HOSTED_AVATAR_OUTPUT_MAX_BYTES = 48 * 1024
AIRTAP_WECHAT_HOSTED_AVATAR_MAX_EDGES = (180, 128, 96)
AIRTAP_WECHAT_HOSTED_IMAGE_OUTPUT_MAX_BYTES = 700 * 1024
AIRTAP_WECHAT_HOSTED_IMAGE_MAX_EDGES = (1280, 960, 720, 480)
DEFAULT_AIRTAP_WECHAT_VIDEO_MAX_MB = 25
PUSHPLUS_UPLOAD_TOKEN_ENDPOINT = "https://www.pushplus.plus/api/open/userImage/uploadToken"
AIRTAP_API_BASE_URL = "https://airtap.ai/cortex/api"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebSettings:
    data_dir: Path
    max_upload_bytes: int
    ttl_seconds: int
    auth_mode: str
    ledger_backend: str
    storage_backend: str
    initial_credits: int
    credits_enabled: bool
    supabase_url: str
    supabase_secret_key: str
    supabase_storage_bucket: str
    airtap_storage_bucket: str
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
    api_base_url: str
    app_base_url: str
    cors_origins: tuple[str, ...]
    cors_origin_regex: str
    session_secret: str
    wechat_app_id: str
    wechat_app_secret: str
    wechat_redirect_uri: str
    wechat_miniprogram_app_id: str
    wechat_miniprogram_app_secret: str
    airtap_relay_secret: str
    airtap_signed_url_ttl_seconds: int
    airtap_ai_enabled: bool
    openai_api_key: str
    openai_base_url: str
    airtap_ai_model: str
    airtap_api_base_url: str
    airtap_personal_access_token: str
    airtap_xhs_model_id: str
    airtap_receiver_id: str
    airtap_video_download_enabled: bool
    airtap_video_max_bytes: int
    cron_secret: str
    pushplus_token: str
    pushplus_access_key: str
    pushplus_topic: str
    pushplus_endpoint: str
    pushplus_upload_token_endpoint: str

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
            credits_enabled=_env_bool(os.environ.get("IMGCLEAN_CREDITS_ENABLED", "true")),
            supabase_url=os.environ.get("SUPABASE_URL", ""),
            supabase_secret_key=os.environ.get("SUPABASE_SECRET_KEY", ""),
            supabase_storage_bucket=os.environ.get("SUPABASE_STORAGE_BUCKET", "imgclean-files"),
            airtap_storage_bucket=os.environ.get("AIRTAP_STORAGE_BUCKET", "imgclean-airtap"),
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
            api_base_url=os.environ.get("IMGCLEAN_API_BASE_URL", os.environ.get("API_BASE_URL", "")),
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
            wechat_miniprogram_app_id=os.environ.get("WECHAT_MINIPROGRAM_APP_ID", ""),
            wechat_miniprogram_app_secret=os.environ.get("WECHAT_MINIPROGRAM_APP_SECRET", ""),
            airtap_relay_secret=os.environ.get("AIRTAP_RELAY_SECRET", ""),
            airtap_signed_url_ttl_seconds=int(
                os.environ.get("AIRTAP_SIGNED_URL_TTL_SECONDS", str(DEFAULT_AIRTAP_SIGNED_URL_TTL_SECONDS))
            ),
            airtap_ai_enabled=_env_bool(os.environ.get("AIRTAP_AI_ENABLED", "false")),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com"),
            airtap_ai_model=os.environ.get("AIRTAP_AI_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")),
            airtap_api_base_url=os.environ.get("AIRTAP_BASE_URL", AIRTAP_API_BASE_URL),
            airtap_personal_access_token=os.environ.get("AIRTAP_PERSONAL_ACCESS_TOKEN", ""),
            airtap_xhs_model_id=os.environ.get("AIRTAP_XHS_MODEL_ID", "airtap-1.0"),
            airtap_receiver_id=os.environ.get("AIRTAP_RECEIVER_ID", "cloud"),
            airtap_video_download_enabled=_env_bool(os.environ.get("AIRTAP_WECHAT_VIDEO_DOWNLOAD_ENABLED", "true")),
            airtap_video_max_bytes=int(
                os.environ.get("AIRTAP_WECHAT_VIDEO_MAX_MB", str(DEFAULT_AIRTAP_WECHAT_VIDEO_MAX_MB))
            )
            * 1024
            * 1024,
            cron_secret=os.environ.get("CRON_SECRET", ""),
            pushplus_token=os.environ.get("PUSHPLUS_TOKEN", ""),
            pushplus_access_key=os.environ.get("PUSHPLUS_ACCESS_KEY", ""),
            pushplus_topic=os.environ.get("PUSHPLUS_TOPIC", ""),
            pushplus_endpoint=os.environ.get("PUSHPLUS_ENDPOINT", "https://www.pushplus.plus/send"),
            pushplus_upload_token_endpoint=os.environ.get(
                "PUSHPLUS_UPLOAD_TOKEN_ENDPOINT", PUSHPLUS_UPLOAD_TOKEN_ENDPOINT
            ),
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
        allow_headers=["authorization", "content-type", "x-airtap-secret"],
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
    app.state.airtap_relay = _build_airtap_relay_store(settings)

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
        if not settings.credits_enabled:
            return {
                "authenticated": True,
                "user_id": user.user_id,
                "email": user.email,
                "name": user.name,
                "credits": None,
            }
        account = await _maybe_await(app.state.ledger.get_account(user))
        return {
            "authenticated": True,
            "user_id": account.user_id,
            "email": account.email,
            "name": user.name,
            "credits": account.credits,
        }

    @app.get("/api/auth/wechat-miniprogram/status")
    def wechat_miniprogram_status() -> dict[str, Any]:
        missing = []
        if not settings.wechat_miniprogram_app_id:
            missing.append("WECHAT_MINIPROGRAM_APP_ID")
        if not settings.wechat_miniprogram_app_secret:
            missing.append("WECHAT_MINIPROGRAM_APP_SECRET")
        if not settings.session_secret:
            missing.append("IMGCLEAN_SESSION_SECRET")
        return {
            "provider": "wechat_miniprogram",
            "configured": not missing,
            "missing": missing,
        }

    @app.post("/api/auth/wechat-miniprogram/login")
    async def wechat_miniprogram_login(request: Request) -> dict[str, Any]:
        if not settings.wechat_miniprogram_app_id or not settings.wechat_miniprogram_app_secret:
            raise HTTPException(status_code=503, detail="WeChat Mini Program login is not configured.")
        if not settings.session_secret:
            raise HTTPException(status_code=503, detail="Session secret is not configured.")
        payload = await request.json()
        code = str(payload.get("code") or "").strip()
        if not code:
            raise HTTPException(status_code=400, detail="Missing wx.login code.")

        async with httpx.AsyncClient(timeout=30) as client:
            session_response = await client.get(
                WECHAT_MINIPROGRAM_SESSION_URL,
                params={
                    "appid": settings.wechat_miniprogram_app_id,
                    "secret": settings.wechat_miniprogram_app_secret,
                    "js_code": code,
                    "grant_type": "authorization_code",
                },
            )
            session_response.raise_for_status()
            session_payload = session_response.json()
        if "errcode" in session_payload:
            raise HTTPException(status_code=401, detail=session_payload.get("errmsg", "WeChat Mini Program login failed."))
        openid = str(session_payload.get("openid") or "")
        if not openid:
            raise HTTPException(status_code=401, detail="WeChat Mini Program login did not return openid.")

        user_id = f"wechat_mp:{openid}"
        token = create_session_token(
            {
                "sub": user_id,
                "provider": "wechat_miniprogram",
                "openid": openid,
                "unionid": session_payload.get("unionid", ""),
            },
            settings.session_secret,
        )
        return {
            "provider": "wechat_miniprogram",
            "token_type": "Bearer",
            "token": token,
            "expires_in": 30 * 24 * 60 * 60,
            "user_id": user_id,
            "openid": openid,
        }

    @app.get("/api/auth/wechat/status")
    def wechat_status(request: Request) -> dict[str, Any]:
        missing = []
        if not settings.wechat_app_id:
            missing.append("WECHAT_APP_ID")
        if not settings.wechat_app_secret:
            missing.append("WECHAT_APP_SECRET")
        if not settings.session_secret:
            missing.append("IMGCLEAN_SESSION_SECRET")
        callback_url = _wechat_redirect_uri(settings, request)
        return {
            "provider": "wechat",
            "configured": not missing,
            "missing": missing,
            "callback_url": callback_url,
            "callback_domain": urlparse(callback_url).netloc,
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

    @app.post("/api/airtap/profiles/upsert")
    async def airtap_profiles_upsert(request: Request) -> dict[str, Any]:
        _require_airtap_relay_secret(request, settings)
        payload = await request.json()
        profiles = payload.get("profiles")
        if not isinstance(profiles, list):
            raise HTTPException(status_code=400, detail="profiles must be a list.")
        try:
            hydrated_profiles = await _download_airtap_profile_avatars(profiles)
            stored_profiles = app.state.airtap_relay.upsert_profiles(hydrated_profiles)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "profiles": stored_profiles}

    @app.get("/api/airtap/profiles/lookup")
    def airtap_profiles_lookup(request: Request, name: str) -> dict[str, Any]:
        _require_airtap_relay_secret(request, settings)
        profile = app.state.airtap_relay.lookup_profile(name)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        return {"ok": True, "profile": profile}

    @app.get("/api/airtap/debug/summary")
    def airtap_debug_summary(request: Request) -> dict[str, Any]:
        _require_airtap_relay_secret(request, settings)
        return {"ok": True, "summary": app.state.airtap_relay.summary()}

    @app.get("/api/airtap/debug/recent")
    def airtap_debug_recent(request: Request, scope: str = "x-hourly-wechat", hours: int = 1, limit: int = 20) -> dict[str, Any]:
        _require_airtap_relay_secret(request, settings)
        safe_hours = min(max(hours, 1), 24)
        safe_limit = min(max(limit, 1), 100)
        shapes = app.state.airtap_relay.recent_post_shapes(
            scope,
            since_seconds=safe_hours * 60 * 60,
            limit=safe_limit,
        )
        return {"ok": True, "scope": scope, "hours": safe_hours, "count": len(shapes), "posts": shapes}

    @app.get("/api/airtap/avatars/{filename}")
    def airtap_avatar(filename: str) -> FileResponse:
        path = app.state.airtap_relay.avatar_path(filename)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="Avatar not found.")
        suffix = path.suffix.lower()
        media_type = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/webp" if suffix == ".webp" else "image/png"
        return FileResponse(path, media_type=media_type)

    @app.get("/api/airtap/media/{filename}")
    def airtap_media(filename: str) -> FileResponse:
        path = app.state.airtap_relay.media_path(filename)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="Media not found.")
        suffix = path.suffix.lower()
        media_type = (
            "image/jpeg"
            if suffix in {".jpg", ".jpeg"}
            else "image/webp"
            if suffix == ".webp"
            else "video/mp4"
            if suffix == ".mp4"
            else "video/webm"
            if suffix == ".webm"
            else "video/quicktime"
            if suffix == ".mov"
            else "video/x-m4v"
            if suffix == ".m4v"
            else "image/png"
        )
        return FileResponse(path, media_type=media_type)

    @app.post("/api/airtap/posts/render")
    async def airtap_posts_render(request: Request) -> dict[str, Any]:
        _require_airtap_relay_secret(request, settings)
        payload = await request.json()
        rendered, channels = await _render_airtap_payload(app, payload, settings, record_seen=False)
        return {
            "ok": True,
            "new_count": len(rendered.new_posts),
            "duplicate_count": rendered.duplicate_count,
            "posts": rendered.new_posts,
            "channels": channels,
        }

    @app.post("/api/airtap/posts/publish")
    async def airtap_posts_publish(request: Request) -> dict[str, Any]:
        _require_airtap_relay_secret(request, settings)
        payload = await request.json()
        rendered, channels = await _render_airtap_payload(app, payload, settings, record_seen=True)
        pushes = await _publish_airtap_channels(rendered.new_posts, channels, settings)
        return {
            "ok": True,
            "new_count": len(rendered.new_posts),
            "duplicate_count": rendered.duplicate_count,
            "posts": rendered.new_posts,
            "channels": channels,
            "pushes": pushes,
        }

    @app.get("/api/airtap/xhs/dispatch")
    async def airtap_xhs_dispatch(request: Request, dry_run: bool = False, hours: int = 8) -> dict[str, Any]:
        _require_airtap_or_cron_secret(request, settings)
        safe_hours = min(max(hours, 1), 24)
        dispatch_key = _xhs_dispatch_key(safe_hours)
        existing_dispatch = None if dry_run else app.state.airtap_relay.dispatch_status("xiaohongshu", dispatch_key)
        posts = app.state.airtap_relay.recent_posts(
            "x-hourly-wechat",
            since_seconds=safe_hours * 60 * 60,
        )
        channels = _render_airtap_channels({"channels": ["xiaohongshu"]}, posts)
        ai_channels = await _maybe_compose_airtap_channels_with_ai(posts, {"channels": ["xiaohongshu"]}, settings)
        if ai_channels:
            channels = {**channels, **ai_channels}
        if existing_dispatch:
            return {
                "ok": True,
                "dispatch_key": dispatch_key,
                "post_count": len(posts),
                "channels": channels,
                "airtap": {"ok": False, "reason": "already_dispatched", "dispatch": existing_dispatch},
            }
        if dry_run:
            return {
                "ok": True,
                "dispatch_key": dispatch_key,
                "post_count": len(posts),
                "channels": channels,
                "airtap": {"ok": False, "reason": "dry_run"},
            }
        if not posts:
            app.state.airtap_relay.record_dispatch(
                "xiaohongshu",
                dispatch_key,
                {"post_count": 0, "reason": "no_posts"},
            )
            return {
                "ok": True,
                "dispatch_key": dispatch_key,
                "post_count": 0,
                "channels": channels,
                "airtap": {"ok": False, "reason": "no_posts"},
            }
        confirm_token = _create_xhs_confirmation_token(settings, dispatch_key)
        confirm_url = _xhs_confirmation_url(settings, request, confirm_token)
        dispatch_record = app.state.airtap_relay.record_dispatch(
            "xiaohongshu",
            dispatch_key,
            {
                "status": "pending_confirmation",
                "post_count": len(posts),
                "xiaohongshu": channels["xiaohongshu"],
                "confirm_url": confirm_url,
            },
        )
        approval_channel = _render_xhs_approval_push(channels["xiaohongshu"], confirm_url, post_count=len(posts))
        approval_push = await _send_pushplus(approval_channel, settings)
        return {
            "ok": True,
            "dispatch_key": dispatch_key,
            "post_count": len(posts),
            "channels": channels,
            "approval": {
                "status": dispatch_record.get("status"),
                "confirm_url": confirm_url,
            },
            "approval_push": approval_push,
            "airtap": {"ok": False, "reason": "awaiting_confirmation"},
        }

    @app.get("/api/airtap/xhs/confirm", response_class=HTMLResponse)
    async def airtap_xhs_confirm(token: str) -> HTMLResponse:
        payload = _read_xhs_confirmation_token(settings, token)
        dispatch_key = str(payload.get("dispatch_key") or "")
        dispatch = app.state.airtap_relay.dispatch_status("xiaohongshu", dispatch_key)
        if not dispatch:
            raise HTTPException(status_code=404, detail="Xiaohongshu approval was not found.")
        if dispatch.get("taskId"):
            return HTMLResponse(_xhs_confirmation_page("已提交过", "这条小红书发布任务之前已经创建过了。"))
        channel = dispatch.get("xiaohongshu")
        if not isinstance(channel, dict):
            raise HTTPException(status_code=409, detail="Xiaohongshu approval payload is incomplete.")
        post_count = int(dispatch.get("post_count") or 0)
        airtap_task = await _create_airtap_xhs_task(settings, channel, post_count=post_count)
        if not airtap_task.get("ok"):
            return HTMLResponse(
                _xhs_confirmation_page("发布任务创建失败", str(airtap_task.get("message") or airtap_task.get("reason") or "")),
                status_code=502,
            )
        app.state.airtap_relay.record_dispatch(
            "xiaohongshu",
            dispatch_key,
            {
                **dispatch,
                "status": "task_created",
                "taskId": airtap_task.get("taskId", ""),
                "taskState": airtap_task.get("taskState", ""),
                "confirmed_at": int(time.time()),
            },
        )
        return HTMLResponse(
            _xhs_confirmation_page(
                "已创建小红书发布任务",
                f"Airtap 任务已提交，任务 ID：{str(airtap_task.get('taskId') or '')}",
            )
        )

    @app.get("/api/auth/wechat/login")
    def wechat_login(request: Request, return_to: str = "") -> RedirectResponse:
        if not settings.wechat_app_id or not settings.wechat_app_secret:
            raise HTTPException(status_code=503, detail="WeChat OAuth is not configured.")
        if not settings.session_secret:
            raise HTTPException(status_code=503, detail="Session secret is not configured.")
        redirect_uri = _wechat_redirect_uri(settings, request)
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
    if user is not None and settings.credits_enabled:
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
    if user is None:
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


def _build_airtap_relay_store(settings: WebSettings) -> Any:
    if settings.storage_backend == "supabase" and settings.supabase_url and settings.supabase_secret_key:
        return SupabaseAirtapRelayStore(
            settings.data_dir,
            settings.supabase_url,
            settings.supabase_secret_key,
            settings.airtap_storage_bucket,
            settings.airtap_signed_url_ttl_seconds,
            settings.api_base_url,
        )
    return LocalAirtapRelayStore(settings.data_dir, settings.api_base_url)


async def _render_airtap_payload(
    app: FastAPI,
    payload: dict[str, Any],
    settings: WebSettings,
    *,
    record_seen: bool,
) -> tuple[Any, dict[str, dict[str, Any]]]:
    rendered = app.state.airtap_relay.render_posts(payload, record_seen=record_seen)
    if "wechat" in _airtap_requested_channels(payload):
        await _prepare_airtap_wechat_media(app, rendered.new_posts, settings)
    channels = _render_airtap_channels(payload, rendered.new_posts)
    ai_channels = await _maybe_compose_airtap_channels_with_ai(rendered.new_posts, payload, settings)
    if ai_channels:
        channels = {**channels, **ai_channels}
    return rendered, channels


def _render_airtap_channels(payload: dict[str, Any], posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    channels = _airtap_requested_channels(payload)
    rendered = {}
    if "wechat" in channels:
        rendered["wechat"] = render_wechat_pushplus(posts)
    if "xiaohongshu" in channels:
        rendered["xiaohongshu"] = render_xiaohongshu_note(posts)
    return rendered


def _airtap_requested_channels(payload: dict[str, Any]) -> list[str]:
    requested_channels = payload.get("channels") or ["wechat", "xiaohongshu"]
    channels = [str(channel) for channel in requested_channels if str(channel) in {"wechat", "xiaohongshu"}]
    return channels or ["wechat", "xiaohongshu"]


def _env_bool(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _require_airtap_relay_secret(request: Request, settings: WebSettings) -> None:
    if not settings.airtap_relay_secret:
        raise HTTPException(status_code=503, detail="Airtap relay secret is not configured.")
    supplied = request.headers.get("x-airtap-secret", "")
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        supplied = auth_header.split(" ", 1)[1].strip()
    if not hmac.compare_digest(supplied, settings.airtap_relay_secret):
        raise HTTPException(status_code=401, detail="Invalid Airtap relay secret.")


def _require_airtap_or_cron_secret(request: Request, settings: WebSettings) -> None:
    supplied = request.headers.get("x-airtap-secret", "")
    auth_header = request.headers.get("authorization", "")
    bearer = ""
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header.split(" ", 1)[1].strip()
        supplied = bearer
    if settings.airtap_relay_secret and hmac.compare_digest(supplied, settings.airtap_relay_secret):
        return
    if settings.cron_secret and hmac.compare_digest(bearer, settings.cron_secret):
        return
    raise HTTPException(status_code=401, detail="Invalid Airtap or cron secret.")


def _xhs_dispatch_key(hours: int) -> str:
    window_seconds = hours * 60 * 60
    asia_shanghai_offset = 8 * 60 * 60
    window = int((time.time() + asia_shanghai_offset) // window_seconds)
    return f"xhs-{hours}h-{window}"


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


def _wechat_redirect_uri(settings: WebSettings, request: Request) -> str:
    if settings.wechat_redirect_uri:
        return settings.wechat_redirect_uri
    if settings.api_base_url:
        return f"{settings.api_base_url.rstrip('/')}/api/auth/wechat/callback"
    return f"{_external_base_url(request)}/api/auth/wechat/callback"


def _external_base_url(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    proto = (forwarded_proto.split(",", 1)[0].strip() if forwarded_proto else request.url.scheme) or "https"
    forwarded_host = request.headers.get("x-forwarded-host", "")
    host = (forwarded_host.split(",", 1)[0].strip() if forwarded_host else request.headers.get("host", ""))
    host = host or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


async def _maybe_await(value: Any) -> Any:
    if pyinspect.isawaitable(value):
        return await value
    return value


async def _download_airtap_profile_avatars(profiles: list[Any]) -> list[dict[str, Any]]:
    hydrated: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for raw_profile in profiles:
            if not isinstance(raw_profile, dict):
                hydrated.append({})
                continue
            profile = dict(raw_profile)
            avatar_url = str(profile.get("avatar_url") or "").strip()
            if profile.get("avatar_base64") or not avatar_url:
                hydrated.append(profile)
                continue
            parsed = urlparse(avatar_url)
            if parsed.scheme not in {"http", "https"}:
                hydrated.append(profile)
                continue
            try:
                response = await client.get(avatar_url)
                response.raise_for_status()
            except (httpx.HTTPError, RuntimeError):
                hydrated.append(profile)
                continue
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in AIRTAP_AVATAR_CONTENT_TYPES or len(response.content) > AIRTAP_MAX_AVATAR_BYTES:
                hydrated.append(profile)
                continue
            profile["avatar_base64"] = base64.b64encode(response.content).decode("ascii")
            profile["avatar_content_type"] = content_type
            hydrated.append(profile)
    return hydrated


async def _prepare_airtap_wechat_media(app: FastAPI, posts: list[dict[str, Any]], settings: WebSettings) -> None:
    urls: list[str] = []
    entries = _airtap_post_entries(posts)
    has_video_sources = False
    for post in entries:
        avatar_url = str(post.get("avatar_url") or "").strip()
        if avatar_url:
            urls.append(avatar_url)
        urls.extend(str(url) for url in post.get("image_urls") or [] if url)
        urls.extend(_airtap_youtube_thumbnail_urls(post))
        if _airtap_video_source_urls(post):
            has_video_sources = True
    if not urls and not has_video_sources:
        return
    image_cache: dict[str, bytes] = {}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        upload_config = await _get_pushplus_upload_config(client, settings)
        for url in dict.fromkeys(urls):
            image_cache[url] = await _download_wechat_image_bytes(client, url)

        for post in entries:
            avatar_url = str(post.get("avatar_url") or "").strip()
            avatar_source = image_cache.get(avatar_url, b"")
            if avatar_source:
                try:
                    avatar_bytes = _image_bytes_to_jpeg_bytes(
                        avatar_source,
                        max_edges=AIRTAP_WECHAT_HOSTED_AVATAR_MAX_EDGES,
                        output_max_bytes=AIRTAP_WECHAT_HOSTED_AVATAR_OUTPUT_MAX_BYTES,
                    )
                except (OSError, UnidentifiedImageError, ValueError) as exc:
                    logger.warning("Airtap WeChat avatar encode failed: url=%s error=%s", avatar_url, exc)
                    avatar_bytes = b""
            else:
                avatar_bytes = b""
            if avatar_bytes:
                avatar_display_url = await _publish_wechat_image(
                    app,
                    client,
                    settings,
                    upload_config,
                    avatar_bytes,
                    prefix="wechat-avatar",
                )
                if avatar_display_url:
                    post["avatar_display_url"] = avatar_display_url
                else:
                    post["avatar_data_uri"] = _jpeg_bytes_to_data_uri(avatar_bytes)

            media_items = []
            for url in post.get("image_urls") or []:
                source = image_cache.get(str(url), b"")
                if not source:
                    continue
                try:
                    image_bytes = _image_bytes_to_jpeg_bytes(
                        source,
                        max_edges=AIRTAP_WECHAT_HOSTED_IMAGE_MAX_EDGES,
                        output_max_bytes=AIRTAP_WECHAT_HOSTED_IMAGE_OUTPUT_MAX_BYTES,
                    )
                except (OSError, UnidentifiedImageError, ValueError) as exc:
                    logger.warning("Airtap WeChat media encode failed: url=%s error=%s", url, exc)
                    continue
                display_url = await _publish_wechat_image(
                    app,
                    client,
                    settings,
                    upload_config,
                    image_bytes,
                    prefix="wechat-image",
                )
                if display_url:
                    media_items.append({"url": str(url), "display_url": display_url})
            if media_items:
                post["wechat_media_items"] = media_items

            await _prepare_airtap_youtube_thumbnails(app, client, settings, upload_config, post, image_cache)

            video_items = []
            for url in _airtap_video_source_urls(post):
                video = await _download_wechat_video(client, url, settings)
                if not video:
                    continue
                display_url = await _publish_wechat_video(
                    app,
                    video["content"],
                    video["content_type"],
                    prefix="wechat-video",
                )
                if display_url:
                    video_items.append(
                        {
                            "url": url,
                            "display_url": display_url,
                            "content_type": video["content_type"],
                            "source": video.get("source", ""),
                        }
                    )
            if video_items:
                post["wechat_video_items"] = video_items


def _airtap_video_source_urls(post: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for value in post.get("video_urls") or []:
        url = str(value or "").strip()
        if url:
            urls.append(url)
    for card in list(post.get("youtube_cards") or []) + list(post.get("link_cards") or []):
        if not isinstance(card, dict):
            continue
        provider = str(card.get("provider") or card.get("site") or "").lower()
        url = str(card.get("url") or card.get("href") or "").strip()
        if url and ("youtube" in provider or _youtube_video_id(url)):
            urls.append(url)
    text = str(post.get("text") or "")
    urls.extend(_youtube_urls(text))
    return list(dict.fromkeys(urls))


def _airtap_youtube_thumbnail_urls(post: dict[str, Any]) -> list[str]:
    urls = []
    for card in list(post.get("youtube_cards") or []) + list(post.get("link_cards") or []):
        if not isinstance(card, dict):
            continue
        provider = str(card.get("provider") or card.get("site") or "").lower()
        url = str(card.get("url") or card.get("href") or "").strip()
        if "youtube" not in provider and not _youtube_video_id(url):
            continue
        thumbnail = str(card.get("thumbnail_url") or card.get("image_url") or "").strip()
        if thumbnail:
            urls.append(thumbnail)
    for url in _youtube_urls(str(post.get("text") or "")):
        video_id = _youtube_video_id(url)
        if video_id:
            urls.append(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
    return list(dict.fromkeys(urls))


async def _prepare_airtap_youtube_thumbnails(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings: WebSettings,
    upload_config: dict[str, str],
    post: dict[str, Any],
    image_cache: dict[str, bytes],
) -> None:
    for card in list(post.get("youtube_cards") or []) + list(post.get("link_cards") or []):
        if not isinstance(card, dict):
            continue
        provider = str(card.get("provider") or card.get("site") or "").lower()
        url = str(card.get("url") or card.get("href") or "").strip()
        if "youtube" not in provider and not _youtube_video_id(url):
            continue
        thumbnail_url = str(card.get("thumbnail_url") or card.get("image_url") or "").strip()
        if not thumbnail_url:
            video_id = _youtube_video_id(url)
            thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""
        source = image_cache.get(thumbnail_url, b"")
        if not source:
            continue
        try:
            image_bytes = _image_bytes_to_jpeg_bytes(
                source,
                max_edges=AIRTAP_WECHAT_HOSTED_IMAGE_MAX_EDGES,
                output_max_bytes=AIRTAP_WECHAT_HOSTED_IMAGE_OUTPUT_MAX_BYTES,
            )
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            logger.warning("Airtap WeChat YouTube thumbnail encode failed: url=%s error=%s", thumbnail_url, exc)
            continue
        display_url = await _publish_wechat_image(
            app,
            client,
            settings,
            upload_config,
            image_bytes,
            prefix="wechat-youtube",
        )
        if display_url:
            card["display_url"] = display_url


def _airtap_post_entries(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        entries.append(post)
        quote = post.get("quote")
        if isinstance(quote, dict):
            entries.append(quote)
    return entries


async def _download_wechat_image_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return b""
    try:
        response = await client.get(url)
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Airtap WeChat image download failed: url=%s error=%s", url, exc)
        return b""
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in AIRTAP_WECHAT_INLINE_CONTENT_TYPES:
        return b""
    if len(response.content) > AIRTAP_WECHAT_INLINE_SOURCE_MAX_BYTES:
        logger.warning("Airtap WeChat image too large to process: url=%s bytes=%s", url, len(response.content))
        return b""
    return response.content


async def _download_wechat_video(
    client: httpx.AsyncClient,
    url: str,
    settings: WebSettings,
) -> dict[str, Any] | None:
    if not settings.airtap_video_download_enabled:
        return None
    if _youtube_video_id(url):
        return await _download_youtube_video(url, settings)
    return await _download_direct_wechat_video(client, url, settings)


async def _download_direct_wechat_video(
    client: httpx.AsyncClient,
    url: str,
    settings: WebSettings,
) -> dict[str, Any] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not _is_direct_video_url(url):
        return None
    try:
        response = await client.get(url)
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Airtap WeChat video download failed: url=%s error=%s", url, exc)
        return None
    if len(response.content) > settings.airtap_video_max_bytes:
        logger.warning("Airtap WeChat video too large to store: url=%s bytes=%s", url, len(response.content))
        return None
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in AIRTAP_WECHAT_VIDEO_CONTENT_TYPES:
        content_type = _video_content_type_from_url(url)
    if content_type not in AIRTAP_WECHAT_VIDEO_CONTENT_TYPES:
        return None
    return {"content": response.content, "content_type": content_type, "source": "direct"}


async def _download_youtube_video(url: str, settings: WebSettings) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(_download_youtube_video_sync, url, settings.airtap_video_max_bytes)
    except Exception as exc:
        logger.warning("Airtap WeChat YouTube download failed: url=%s error=%s", url, exc)
        return None


def _download_youtube_video_sync(url: str, max_bytes: int) -> dict[str, Any] | None:
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        logger.warning("yt-dlp is not installed; YouTube video will remain a link card.")
        return None
    max_mb = max(1, max_bytes // (1024 * 1024))
    with tempfile.TemporaryDirectory(prefix="airtap-youtube-") as tmpdir:
        output_template = str(Path(tmpdir) / "video.%(ext)s")
        options = {
            "format": f"best[ext=mp4][filesize<{max_mb}M]/best[filesize<{max_mb}M]/best[ext=mp4]/best",
            "noplaylist": True,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
            "retries": 1,
            "max_filesize": max_bytes,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
        files = sorted(Path(tmpdir).glob("video.*"))
        if not files:
            return None
        video_path = files[0]
        content = video_path.read_bytes()
        if len(content) > max_bytes:
            return None
        content_type = _video_content_type_from_url(video_path.name)
        if content_type not in AIRTAP_WECHAT_VIDEO_CONTENT_TYPES:
            content_type = "video/mp4"
        return {"content": content, "content_type": content_type, "source": "youtube"}


def _video_content_type_from_url(url: str) -> str:
    path = urlparse(str(url or "")).path.lower()
    if path.endswith(".mp4"):
        return "video/mp4"
    if path.endswith(".webm"):
        return "video/webm"
    if path.endswith(".mov"):
        return "video/quicktime"
    if path.endswith(".m4v"):
        return "video/x-m4v"
    return ""


async def _get_pushplus_upload_config(client: httpx.AsyncClient, settings: WebSettings) -> dict[str, str]:
    if not settings.pushplus_access_key:
        return {}
    try:
        response = await client.get(
            settings.pushplus_upload_token_endpoint,
            headers={"access-key": settings.pushplus_access_key},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("PushPlus image upload token request failed: %s", exc)
        return {}
    if payload.get("code") not in {200, "200"}:
        logger.warning("PushPlus image upload token rejected: code=%s msg=%s", payload.get("code"), payload.get("msg"))
        return {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    upload_url = str(data.get("uploadUrl") or data.get("upload_url") or "")
    token = str(data.get("token") or data.get("uploadToken") or data.get("upload_token") or "")
    if not upload_url or not token:
        return {}
    return {"upload_url": upload_url, "token": token}


async def _publish_wechat_image(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings: WebSettings,
    upload_config: dict[str, str],
    content: bytes,
    *,
    prefix: str,
) -> str:
    pushplus_url = await _upload_pushplus_image(client, upload_config, content, prefix=prefix)
    if pushplus_url:
        return pushplus_url
    try:
        stored = app.state.airtap_relay.store_media(content, "image/jpeg", prefix=prefix)
    except Exception as exc:
        logger.warning("Airtap WeChat media storage failed: %s", exc)
        return ""
    return str(stored.get("media_url") or "")


async def _publish_wechat_video(
    app: FastAPI,
    content: bytes,
    content_type: str,
    *,
    prefix: str,
) -> str:
    try:
        stored = app.state.airtap_relay.store_media(content, content_type, prefix=prefix)
    except Exception as exc:
        logger.warning("Airtap WeChat video storage failed: %s", exc)
        return ""
    return str(stored.get("media_url") or "")


async def _upload_pushplus_image(
    client: httpx.AsyncClient,
    upload_config: dict[str, str],
    content: bytes,
    *,
    prefix: str,
) -> str:
    upload_url = upload_config.get("upload_url", "")
    token = upload_config.get("token", "")
    if not upload_url or not token:
        return ""
    filename = f"{prefix}-{hashlib.sha256(content).hexdigest()[:16]}.jpg"
    try:
        response = await client.post(
            upload_url,
            data={"token": token},
            files={"file": (filename, content, "image/jpeg")},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("PushPlus image upload failed: %s", exc)
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    image_url = str(payload.get("url") or data.get("url") or data.get("thumbnail") or "")
    if image_url.startswith("//"):
        return f"https:{image_url}"
    return image_url


def _image_bytes_to_jpeg_bytes(
    content: bytes,
    *,
    max_edges: tuple[int, ...],
    output_max_bytes: int,
) -> bytes:
    source = Image.open(io.BytesIO(content))
    output = io.BytesIO()
    for edge in max_edges:
        image = source.copy()
        image.thumbnail((edge, edge))
        if image.mode in {"RGBA", "LA"}:
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        for quality in (76, 66, 56, 46):
            output.seek(0)
            output.truncate(0)
            image.save(output, format="JPEG", quality=quality, optimize=True)
            if output.tell() <= output_max_bytes:
                return output.getvalue()
    raise ValueError("encoded image is too large")


def _jpeg_bytes_to_data_uri(content: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(content).decode("ascii")


async def _maybe_compose_airtap_channels_with_ai(
    posts: list[dict[str, Any]],
    payload: dict[str, Any],
    settings: WebSettings,
) -> dict[str, dict[str, Any]]:
    if not settings.airtap_ai_enabled or not settings.openai_api_key or not posts:
        return {}
    requested_channels = payload.get("channels") or ["wechat", "xiaohongshu"]
    channels = [str(channel) for channel in requested_channels if str(channel) == "xiaohongshu"]
    if not channels:
        return {}
    prompt = _airtap_ai_prompt(posts, channels)
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{settings.openai_base_url.rstrip('/')}/v1/responses",
                headers={
                    "authorization": f"Bearer {settings.openai_api_key}",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.airtap_ai_model,
                    "input": prompt,
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "airtap_channel_content",
                            "schema": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "wechat": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "target": {"type": "string"},
                                            "template": {"type": "string"},
                                            "title": {"type": "string"},
                                            "content": {"type": "string"},
                                        },
                                        "required": ["target", "template", "title", "content"],
                                    },
                                    "xiaohongshu": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "format": {"type": "string"},
                                            "title": {"type": "string"},
                                            "body": {"type": "string"},
                                            "hashtags": {"type": "array", "items": {"type": "string"}},
                                        },
                                        "required": ["format", "title", "body", "hashtags"],
                                    },
                                },
                            },
                        }
                    },
                },
            )
            response.raise_for_status()
    except Exception as exc:
        logger.warning("Airtap AI channel composition failed: %s", exc)
        return {}
    try:
        content = _extract_responses_text(response.json())
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Airtap AI channel composition returned invalid JSON: %s", exc)
        return {}
    return _normalize_ai_channels(parsed, channels)


def _airtap_ai_prompt(posts: list[dict[str, Any]], channels: list[str]) -> str:
    return (
        "你是社交媒体内容编辑。Airtap 已经抓取原始帖子，你负责把内容处理成可直接发布或推送的成品。\n"
        "要求：\n"
        "1. 保留作者、发布时间、原文重点、引用关系、图片和视频链接，不要遗漏。\n"
        "2. 非中文内容要翻译成自然中文；重要英文原句可保留在括号内。\n"
        "3. 小红书不是 X 原帖搬运：先把帖子聚类，只选最适合发布的一个主题写成笔记；主题太散时选择最强线索，不要硬做合集。\n"
        "4. 小红书正文不要逐条按作者罗列，不要写成“作者A提到/作者B提到”的清单；要写成有观点、有依据、有风险提示的人话笔记。\n"
        "5. 带平台水印的视频只能作为素材线索，不要要求 Airtap 直接上传或搬运；正文里给出原创信息图、关键帧重绘或文字化处理建议。\n"
        "6. 不要使用“我先说结论”“为什么值得看”“这8小时我帮你”这类模板话术；如果需要分析，直接写具体判断和依据。\n"
        "7. 只输出 JSON，不要 Markdown。\n"
        f"需要生成的渠道：{', '.join(channels)}。\n"
        "帖子 JSON：\n"
        f"{json.dumps(posts, ensure_ascii=False)}"
    )


def _extract_responses_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return text
    return ""


def _normalize_ai_channels(payload: dict[str, Any], channels: list[str]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    if "wechat" in channels and isinstance(payload.get("wechat"), dict):
        wechat = payload["wechat"]
        normalized["wechat"] = {
            "target": str(wechat.get("target") or "pushplus"),
            "template": str(wechat.get("template") or "html"),
            "title": str(wechat.get("title") or "X 每小时摘要"),
            "content": str(wechat.get("content") or ""),
        }
    if "xiaohongshu" in channels and isinstance(payload.get("xiaohongshu"), dict):
        xhs = payload["xiaohongshu"]
        hashtags = xhs.get("hashtags")
        normalized["xiaohongshu"] = {
            "format": str(xhs.get("format") or "note"),
            "title": str(xhs.get("title") or "8小时市场观察"),
            "body": str(xhs.get("body") or ""),
            "hashtags": [str(tag) for tag in hashtags] if isinstance(hashtags, list) else [],
        }
    return {key: value for key, value in normalized.items() if value}


async def _publish_airtap_channels(
    new_posts: list[dict[str, Any]],
    channels: dict[str, dict[str, Any]],
    settings: WebSettings,
) -> dict[str, dict[str, Any]]:
    pushes: dict[str, dict[str, Any]] = {}
    if "wechat" not in channels:
        return pushes
    if not new_posts:
        pushes["wechat"] = {"ok": False, "provider": "pushplus", "reason": "no_new_posts"}
        return pushes
    if not settings.pushplus_token:
        pushes["wechat"] = {"ok": False, "provider": "pushplus", "reason": "pushplus_token_not_configured"}
        return pushes
    pushes["wechat"] = await _send_pushplus(channels["wechat"], settings)
    return pushes


def _create_xhs_confirmation_token(settings: WebSettings, dispatch_key: str) -> str:
    secret = _xhs_confirmation_secret(settings)
    return create_session_token(
        {"kind": "xhs_publish_confirmation", "dispatch_key": dispatch_key},
        secret,
        ttl_seconds=30 * 60 * 60,
    )


def _read_xhs_confirmation_token(settings: WebSettings, token: str) -> dict[str, Any]:
    secret = _xhs_confirmation_secret(settings)
    try:
        payload = read_session_token(token, secret)
    except InvalidSession as exc:
        raise HTTPException(status_code=401, detail="Invalid Xiaohongshu confirmation token.") from exc
    if payload.get("kind") != "xhs_publish_confirmation":
        raise HTTPException(status_code=401, detail="Invalid Xiaohongshu confirmation token.")
    return payload


def _xhs_confirmation_secret(settings: WebSettings) -> str:
    secret = settings.session_secret or settings.airtap_relay_secret or settings.cron_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Xiaohongshu confirmation secret is not configured.")
    return secret


def _xhs_confirmation_url(settings: WebSettings, request: Request, token: str) -> str:
    base_url = settings.api_base_url.rstrip("/") if settings.api_base_url else _external_base_url(request)
    return f"{base_url}/api/airtap/xhs/confirm?token={quote(token)}"


def _render_xhs_approval_push(channel: dict[str, Any], confirm_url: str, *, post_count: int) -> dict[str, Any]:
    title = str(channel.get("title") or "小红书发布确认")
    body = str(channel.get("body") or "")
    hashtags = [str(tag).lstrip("#") for tag in channel.get("hashtags") or [] if str(tag).strip()]
    hashtag_text = " ".join(f"#{html.escape(tag)}" for tag in hashtags)
    content = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'background:#f8fafc;padding:12px;box-sizing:border-box;">'
        '<div style="padding:14px 16px;border-radius:10px;background:#111827;color:#fff;margin-bottom:12px;">'
        '<div style="font-size:13px;color:#cbd5e1;">小红书发布前确认</div>'
        f'<div style="font-size:20px;font-weight:800;line-height:1.35;margin-top:4px;">{html.escape(title)}</div>'
        f'<div style="font-size:12px;color:#94a3b8;margin-top:4px;">这批内容来自最近 {post_count} 条已存 X 线索；确认后才会调用 Airtap 发布。</div>'
        "</div>"
        '<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:13px 14px;">'
        '<div style="font-size:13px;font-weight:800;color:#0f766e;margin-bottom:6px;">笔记正文预览</div>'
        f'<div style="white-space:pre-wrap;line-height:1.7;color:#111827;font-size:15px;word-break:break-word;overflow-wrap:anywhere;">{html.escape(body)}</div>'
        f'<div style="font-size:13px;color:#475569;margin-top:10px;line-height:1.5;">{hashtag_text}</div>'
        "</div>"
        f'<a href="{html.escape(confirm_url)}" style="display:block;margin-top:12px;padding:13px 16px;'
        'text-align:center;background:#2563eb;color:#fff;text-decoration:none;border-radius:9px;'
        'font-size:16px;font-weight:800;">确认发布到小红书</a>'
        '<div style="font-size:12px;color:#64748b;line-height:1.5;margin-top:8px;">'
        "点确认后，后端会创建 Airtap 小红书发布任务；如果小红书登录失效，Airtap 会停在登录/验证页并回报。"
        "</div></div>"
    )
    return {
        "target": "pushplus",
        "template": "html",
        "title": f"小红书发布确认：{title}",
        "content": content,
    }


def _xhs_confirmation_page(title: str, message: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title></head>"
        '<body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#f8fafc;'
        'color:#111827;padding:24px;">'
        '<div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;'
        'padding:20px;">'
        f'<h1 style="font-size:22px;line-height:1.35;margin:0 0 10px;">{html.escape(title)}</h1>'
        f'<p style="font-size:15px;line-height:1.7;color:#475569;margin:0;">{html.escape(message)}</p>'
        "</div></body></html>"
    )


async def _create_airtap_xhs_task(settings: WebSettings, channel: dict[str, Any], *, post_count: int) -> dict[str, Any]:
    if not settings.airtap_personal_access_token:
        return {"ok": False, "reason": "airtap_token_not_configured"}
    title = str(channel.get("title") or "")
    body = str(channel.get("body") or "")
    hashtags = [str(tag) for tag in channel.get("hashtags") or [] if str(tag).strip()]
    message = _airtap_xhs_publish_message(title, body, hashtags, post_count=post_count)
    payload: dict[str, Any] = {
        "receiverId": settings.airtap_receiver_id,
        "modelId": settings.airtap_xhs_model_id,
        "userMessage": {"type": "user", "parts": [{"type": "text", "text": message}]},
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{settings.airtap_api_base_url.rstrip('/')}/task/v1/taskCreate",
                headers={
                    "Authorization": f"Bearer {settings.airtap_personal_access_token}",
                    "Content-Type": "application/json",
                    "x-airtap-pilot-client-type": "pilot-agent",
                    "x-airtap-pilot-client-name": "imgclean-server",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("Airtap Xiaohongshu task creation failed: %s", exc)
        return {"ok": False, "reason": "airtap_task_create_failed", "message": str(exc)}
    return {
        "ok": True,
        "taskId": data.get("taskId", ""),
        "taskState": data.get("taskState", ""),
    }


def _airtap_xhs_publish_message(title: str, body: str, hashtags: list[str], *, post_count: int) -> str:
    hashtag_text = " ".join(f"#{tag.lstrip('#')}" for tag in hashtags)
    return (
        "Publish the backend-prepared Xiaohongshu note.\n\n"
        "Do not open X. Do not scrape X. Do not rewrite the copy. "
        "The server already collected the hourly posts, deduped them, and generated this 8-hour summary.\n\n"
        "Use the Xiaohongshu mobile app only. Open the installed Xiaohongshu app on the Airtap phone and publish from inside the app. "
        "Do not use the web publisher, xiaohongshu.com, a browser, a desktop uploader, or third-party web publishing tooling for the final post.\n\n"
        "Create a Xiaohongshu note in the mobile app with exactly this content:\n\n"
        f"Title:\n{title}\n\n"
        f"Body:\n{body}\n\n"
        f"Hashtags:\n{hashtag_text}\n\n"
        f"Source batch: {post_count} stored hourly posts from the backend.\n"
        "If the Xiaohongshu mobile app is missing, logged out, asks for verification, or cannot publish, stop and report the blocker. "
        "Do not switch to web publishing as a fallback and do not wait for user login."
    )


async def _send_pushplus(channel: dict[str, Any], settings: WebSettings) -> dict[str, Any]:
    payload = {
        "token": settings.pushplus_token,
        "title": str(channel.get("title") or "X 每小时摘要"),
        "content": str(channel.get("content") or ""),
        "template": str(channel.get("template") or "html"),
    }
    if settings.pushplus_topic:
        payload["topic"] = settings.pushplus_topic
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                settings.pushplus_endpoint,
                headers={"content-type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:
        return {"ok": False, "provider": "pushplus", "reason": "pushplus_request_failed", "message": str(exc)}
    code = body.get("code")
    return {
        "ok": code in {200, "200"},
        "provider": "pushplus",
        "code": code,
        "message": body.get("msg") or body.get("message") or "",
        "provider_message_id": body.get("data"),
    }


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

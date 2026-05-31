"""Airtap relay storage and channel renderers."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


DEFAULT_STATE = {"profiles": {}, "aliases": {}, "seen_posts": {}}
logger = logging.getLogger(__name__)
AVATAR_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class RenderedChannels:
    new_posts: list[dict[str, Any]]
    duplicate_count: int
    channels: dict[str, dict[str, Any]]


class LocalAirtapRelayStore:
    def __init__(self, data_dir: Path, api_base_url: str = ""):
        self.root = data_dir / "airtap"
        self.avatar_dir = self.root / "avatars"
        self.state_path = self.root / "state.json"
        self.api_base_url = api_base_url.rstrip("/")
        self._lock = threading.Lock()

    def upsert_profiles(self, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            state = self._read()
            stored_profiles = []
            for profile in profiles:
                stored = self._upsert_profile(state, profile)
                stored_profiles.append(self.public_profile(stored))
            self._write(state)
            return stored_profiles

    def lookup_profile(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._read()
            profile = self._lookup_profile(state, name)
            if profile is None:
                return None
            return self.public_profile(profile)

    def render_posts(self, payload: dict[str, Any], *, record_seen: bool = True) -> RenderedChannels:
        scope = str(payload.get("scope") or "default").strip() or "default"
        requested_channels = payload.get("channels") or ["wechat", "xiaohongshu"]
        channels = [str(channel) for channel in requested_channels if str(channel) in {"wechat", "xiaohongshu"}]
        if not channels:
            channels = ["wechat", "xiaohongshu"]

        with self._lock:
            state = self._read()
            seen_for_scope = state.setdefault("seen_posts", {}).setdefault(scope, {})
            new_posts = []
            duplicate_count = 0
            for raw_post in payload.get("posts") or []:
                if not isinstance(raw_post, dict):
                    continue
                post = self._enrich_post(state, raw_post)
                key = _post_key(post)
                if key in seen_for_scope:
                    duplicate_count += 1
                    continue
                if record_seen:
                    seen_for_scope[key] = {
                        "first_seen_at": int(time.time()),
                        "author_name": post.get("author_name", ""),
                        "published_at": post.get("published_at", ""),
                        "url": post.get("url", ""),
                    }
                new_posts.append(post)
            if record_seen:
                self._write(state)

        rendered = {}
        if "wechat" in channels:
            rendered["wechat"] = render_wechat_pushplus(new_posts)
        if "xiaohongshu" in channels:
            rendered["xiaohongshu"] = render_xiaohongshu_note(new_posts)
        return RenderedChannels(new_posts, duplicate_count, rendered)

    def public_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        avatar_url = profile.get("avatar_url") or ""
        if avatar_url.startswith("/api/") and self.api_base_url:
            avatar_url = f"{self.api_base_url}{avatar_url}"
        return {
            "display_name": profile.get("display_name", ""),
            "handle": profile.get("handle", ""),
            "avatar_url": avatar_url,
            "aliases": profile.get("aliases", []),
            "updated_at": profile.get("updated_at"),
        }

    def avatar_path(self, filename: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
        return self.avatar_dir / safe_name

    def _upsert_profile(self, state: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        display_name = str(profile.get("display_name") or "").strip()
        handle = str(profile.get("handle") or "").strip().lstrip("@")
        aliases = _aliases(display_name, handle)
        if not aliases:
            raise ValueError("Profile requires display_name or handle.")

        profile_id = _normalize_alias(handle or display_name)
        existing = state.setdefault("profiles", {}).get(profile_id, {})
        avatar_url = existing.get("avatar_url", "")
        avatar_path = existing.get("avatar_path", "")
        avatar_bytes = _decode_avatar(profile)
        if avatar_bytes:
            content_type = _avatar_content_type(profile)
            stored_avatar = self._store_avatar(profile_id, avatar_bytes, content_type)
            avatar_url = stored_avatar["avatar_url"]
            avatar_path = stored_avatar["avatar_path"]
        elif profile.get("avatar_url"):
            avatar_url = str(profile.get("avatar_url"))
            avatar_path = ""

        stored = {
            "id": profile_id,
            "display_name": display_name or existing.get("display_name", ""),
            "handle": handle or existing.get("handle", ""),
            "avatar_url": avatar_url,
            "avatar_path": avatar_path,
            "aliases": aliases,
            "updated_at": int(time.time()),
        }
        state["profiles"][profile_id] = stored
        for alias in aliases:
            state.setdefault("aliases", {})[_normalize_alias(alias)] = profile_id
        return stored

    def _lookup_profile(self, state: dict[str, Any], name: str) -> dict[str, Any] | None:
        profile_id = state.get("aliases", {}).get(_normalize_alias(name))
        if not profile_id:
            return None
        return state.get("profiles", {}).get(profile_id)

    def _enrich_post(self, state: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(post)
        profile = self._lookup_profile(state, str(post.get("author_handle") or post.get("author_name") or ""))
        if profile:
            public_profile = self.public_profile(profile)
            enriched["author_name"] = enriched.get("author_name") or public_profile["display_name"]
            enriched["author_handle"] = enriched.get("author_handle") or public_profile["handle"]
            enriched["avatar_url"] = public_profile["avatar_url"]
        return enriched

    def _read(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return json.loads(json.dumps(DEFAULT_STATE))
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        for key, value in DEFAULT_STATE.items():
            data.setdefault(key, json.loads(json.dumps(value)))
        return data

    def _write(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _store_avatar(self, profile_id: str, content: bytes, content_type: str) -> dict[str, str]:
        suffix = AVATAR_CONTENT_TYPES[content_type]
        filename = f"{profile_id}-{_hash_bytes(content)[:12]}{suffix}"
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        self.avatar_path(filename).write_bytes(content)
        return {"avatar_path": filename, "avatar_url": f"/api/airtap/avatars/{filename}"}


class SupabaseAirtapRelayStore(LocalAirtapRelayStore):
    def __init__(
        self,
        data_dir: Path,
        supabase_url: str,
        service_key: str,
        bucket: str,
        ttl_seconds: int,
        api_base_url: str = "",
    ):
        super().__init__(data_dir, api_base_url)
        self.supabase_url = supabase_url.rstrip("/")
        self.service_key = service_key
        self.bucket = bucket
        self.ttl_seconds = ttl_seconds
        self.state_object_path = "airtap/state.json"

    def public_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        public = super().public_profile(profile)
        avatar_path = profile.get("avatar_path")
        if avatar_path:
            public["avatar_url"] = self._signed_url(str(avatar_path))
        return public

    def _read(self) -> dict[str, Any]:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                f"{self.supabase_url}/storage/v1/object/authenticated/{self.bucket}/{self.state_object_path}",
                headers=self._headers(),
            )
        if _is_missing_storage_object(response):
            return json.loads(json.dumps(DEFAULT_STATE))
        if response.status_code >= 400:
            logger.warning(
                "Supabase Airtap state read failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()
        data = json.loads(response.content.decode("utf-8"))
        for key, value in DEFAULT_STATE.items():
            data.setdefault(key, json.loads(json.dumps(value)))
        return data

    def _write(self, state: dict[str, Any]) -> None:
        body = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        self._upload_object(self.state_object_path, body, "application/json")

    def _store_avatar(self, profile_id: str, content: bytes, content_type: str) -> dict[str, str]:
        suffix = AVATAR_CONTENT_TYPES[content_type]
        path = f"airtap/avatars/{profile_id}-{_hash_bytes(content)[:12]}{suffix}"
        self._upload_object(path, content, content_type)
        return {"avatar_path": path, "avatar_url": self._signed_url(path)}

    def _upload_object(self, path: str, content: bytes, content_type: str) -> None:
        with httpx.Client(timeout=60) as client:
            response = self._post_object(client, path, content, content_type)
            if response.status_code in {400, 404} and self._ensure_bucket(client):
                response = self._post_object(client, path, content, content_type)
        if response.status_code >= 400:
            logger.warning(
                "Supabase Airtap object upload failed: path=%s status=%s body=%s",
                path,
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()

    def _post_object(self, client: httpx.Client, path: str, content: bytes, content_type: str) -> httpx.Response:
        return client.post(
            f"{self.supabase_url}/storage/v1/object/{self.bucket}/{path}",
            content=content,
            headers={
                **self._headers(),
                "content-type": content_type,
                "x-upsert": "true",
            },
        )

    def _ensure_bucket(self, client: httpx.Client) -> bool:
        response = client.post(
            f"{self.supabase_url}/storage/v1/bucket",
            json={"id": self.bucket, "name": self.bucket, "public": False},
            headers={**self._headers(), "content-type": "application/json"},
        )
        if response.status_code in {200, 201}:
            return True
        body = response.text.lower()
        if response.status_code in {400, 409} and ("already exists" in body or "duplicate" in body):
            return True
        logger.warning(
            "Supabase Airtap bucket ensure failed: bucket=%s status=%s body=%s",
            self.bucket,
            response.status_code,
            response.text[:500],
        )
        return False

    def _signed_url(self, path: str) -> str:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.supabase_url}/storage/v1/object/sign/{self.bucket}/{path}",
                json={"expiresIn": self.ttl_seconds},
                headers=self._headers(),
            )
        response.raise_for_status()
        signed_url = response.json()["signedURL"]
        if signed_url.startswith("http"):
            return signed_url
        return f"{self.supabase_url}/storage/v1{signed_url}"

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "authorization": f"Bearer {self.service_key}",
        }


def render_wechat_pushplus(posts: list[dict[str, Any]]) -> dict[str, Any]:
    title = f"X 每小时更新：{len(posts)} 条新内容" if posts else "X 每小时更新：暂无新内容"
    if not posts:
        content = (
            '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
            'padding:16px;border:1px solid #e5e7eb;border-radius:10px;background:#f8fafc;color:#475569;">'
            "本小时暂无新内容。"
            "</div>"
        )
    else:
        cards = [_wechat_post_row(post) for post in posts]
        content = (
            '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
            'background:#f8fafc;padding:10px;max-width:100%;box-sizing:border-box;">'
            '<div style="padding:14px 16px;margin-bottom:10px;border-radius:10px;'
            'background:#111827;color:#fff;">'
            '<div style="font-size:13px;color:#cbd5e1;">Airtap 自动抓取 · Codex 内容整理</div>'
            f'<div style="font-size:20px;font-weight:800;line-height:1.35;margin-top:4px;">{html.escape(title)}</div>'
            '<div style="font-size:12px;color:#94a3b8;margin-top:4px;">正文、引用和图片已直接整理在微信内；原文链接仅作备用。</div>'
            "</div>"
            + "".join(cards)
            + "</div>"
        )
    return {
        "target": "pushplus",
        "template": "html",
        "title": title,
        "content": content,
    }


def render_xiaohongshu_note(posts: list[dict[str, Any]]) -> dict[str, Any]:
    title = f"X 科技/美股快讯：{len(posts)} 条值得看" if posts else "X 科技/美股快讯：暂无新内容"
    if not posts:
        body = "本小时暂无新内容。"
    else:
        sections = []
        for index, post in enumerate(posts, start=1):
            sections.append(
                "\n".join(
                    item
                    for item in [
                        f"{index}. {_plain_author(post)}",
                        f"发布时间：{post.get('published_at', '')}" if post.get("published_at") else "",
                        "核心内容：",
                        str(post.get("text") or "").strip(),
                        _plain_quote(post),
                        _plain_media("图片素材", post.get("image_urls") or []),
                        _plain_media("视频素材", post.get("video_urls") or []),
                        f"原文链接：{post.get('url', '')}" if post.get("url") else "",
                    ]
                    if item
                )
            )
        body = (
            "这组内容来自 X 上几个科技/美股账号的最新动态，我做了去重和整理，方便快速扫重点。\n\n"
            + "\n\n---\n\n".join(sections)
            + "\n\n适合配图发布：优先使用原帖图片/视频素材，正文保留作者和原文链接，方便回溯。"
        )
    return {
        "format": "note",
        "title": title,
        "body": body,
        "hashtags": ["X资讯", "科技股", "美股", "AI", "投资观察"],
    }


def _wechat_post_row(post: dict[str, Any]) -> str:
    avatar = html.escape(str(post.get("avatar_url") or ""))
    avatar_cell = (
        f'<img src="{avatar}" alt="" style="display:block;width:42px;height:42px;border-radius:50%;object-fit:cover;border:1px solid #e5e7eb;">'
        if avatar
        else '<div style="width:42px;height:42px;border-radius:50%;background:#dbeafe;"></div>'
    )
    media = _wechat_media(post)
    quote = _wechat_quote(post)
    author_name, author_handle = _wechat_author_parts(post)
    author = html.escape(author_name)
    handle = html.escape(f"@{author_handle}") if author_handle else ""
    handle_line = (
        f'<div style="font-size:12px;color:#64748b;line-height:1.35;word-break:break-all;overflow-wrap:anywhere;">{handle}</div>'
        if handle
        else ""
    )
    time_text = html.escape(str(post.get("published_at") or ""))
    text = html.escape(str(post.get("text") or ""))
    url = html.escape(str(post.get("url") or ""))
    source = (
        '<div style="margin-top:12px;padding:8px 10px;background:#f8fafc;border-radius:8px;'
        'font-size:12px;line-height:1.5;color:#64748b;word-break:break-all;overflow-wrap:anywhere;">'
        f"原文链接（备用）：{url}</div>"
        if url
        else ""
    )
    return (
        '<div style="margin:10px 0;padding:14px;background:#fff;border:1px solid #e5e7eb;'
        'border-radius:10px;max-width:100%;box-sizing:border-box;overflow:hidden;">'
        '<div style="min-height:44px;margin-bottom:10px;">'
        f'<div style="float:left;margin-right:10px;">{avatar_cell}</div>'
        f'<div style="font-weight:800;color:#0f172a;font-size:16px;line-height:1.35;word-break:break-word;overflow-wrap:anywhere;">{author}</div>'
        f"{handle_line}"
        f'<div style="font-size:12px;color:#64748b;margin-top:2px;line-height:1.35;">{time_text}</div>'
        '<div style="clear:both;"></div>'
        "</div>"
        f'<div style="white-space:pre-wrap;line-height:1.7;color:#111827;font-size:15px;word-break:break-word;overflow-wrap:anywhere;">{text}</div>'
        f"{quote}{media}{source}"
        "</div>"
    )


def _wechat_quote(post: dict[str, Any]) -> str:
    quote = post.get("quote") or {}
    if not isinstance(quote, dict) or not quote:
        return ""
    author = html.escape(str(quote.get("author_name") or quote.get("author_handle") or "Quoted post"))
    text = html.escape(str(quote.get("text") or ""))
    return (
        '<div style="margin-top:12px;padding:10px 12px;border-left:4px solid #38bdf8;'
        'background:#f0f9ff;color:#334155;border-radius:8px;word-break:break-word;overflow-wrap:anywhere;">'
        f'<div style="font-size:12px;font-weight:700;">引用：{author}</div>'
        f'<div style="white-space:pre-wrap;line-height:1.6;margin-top:4px;">{text}</div>'
        "</div>"
    )


def _wechat_media(post: dict[str, Any]) -> str:
    lines = []
    for url in post.get("image_urls") or []:
        safe_url = html.escape(str(url))
        lines.append(
            '<div style="margin-top:12px;">'
            f'<img src="{safe_url}" alt="图片" style="display:block;width:100%;height:auto;max-width:100%;'
            'border-radius:8px;border:1px solid #e5e7eb;background:#f8fafc;">'
            "</div>"
        )
    for label, urls in (("视频素材", post.get("video_urls") or []),):
        for url in urls:
            safe_url = html.escape(str(url))
            lines.append(
                '<div style="margin-top:6px;padding:8px 10px;background:#f8fafc;border-radius:8px;'
                'border:1px solid #e2e8f0;word-break:break-all;overflow-wrap:anywhere;">'
                f'<span style="font-weight:700;color:#0f172a;">{html.escape(label)}</span>：'
                f'<span style="color:#475569;">{safe_url}</span>'
                "</div>"
            )
    if not lines:
        return ""
    return '<div style="margin-top:12px;">' + "".join(lines) + "</div>"


def _plain_author(post: dict[str, Any]) -> str:
    author = str(post.get("author_name") or post.get("author_handle") or "Unknown").strip()
    handle = str(post.get("author_handle") or "").strip().lstrip("@")
    if handle and handle.lower() not in author.lower():
        return f"{author} (@{handle})"
    return author


def _wechat_author_parts(post: dict[str, Any]) -> tuple[str, str]:
    author = str(post.get("author_name") or post.get("author_handle") or "Unknown").strip()
    handle = str(post.get("author_handle") or "").strip().lstrip("@")
    if handle and handle.lower() in author.lower():
        return author, ""
    return author, handle


def _plain_quote(post: dict[str, Any]) -> str:
    quote = post.get("quote") or {}
    if not isinstance(quote, dict) or not quote:
        return ""
    author = quote.get("author_name") or quote.get("author_handle") or "quoted post"
    return f"引用：{author} - {quote.get('text', '')}"


def _plain_media(label: str, urls: list[Any]) -> str:
    if not urls:
        return ""
    return "\n".join(f"{label}：{url}" for url in urls)


def _decode_avatar(profile: dict[str, Any]) -> bytes:
    avatar_base64 = str(profile.get("avatar_base64") or "").strip()
    if not avatar_base64:
        return b""
    if "," in avatar_base64 and avatar_base64.split(",", 1)[0].startswith("data:"):
        avatar_base64 = avatar_base64.split(",", 1)[1]
    return base64.b64decode(avatar_base64, validate=True)


def _avatar_content_type(profile: dict[str, Any]) -> str:
    content_type = str(profile.get("avatar_content_type") or "image/png").lower()
    if content_type not in AVATAR_CONTENT_TYPES:
        raise ValueError("Unsupported avatar content type.")
    return content_type


def _aliases(display_name: str, handle: str) -> list[str]:
    values = []
    for value in (display_name, handle):
        normalized = value.strip().lstrip("@")
        if normalized and normalized not in values:
            values.append(normalized)
    return values


def _normalize_alias(value: str) -> str:
    normalized = str(value or "").strip().lower().lstrip("@")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_missing_storage_object(response: httpx.Response) -> bool:
    if response.status_code == 404:
        return True
    if response.status_code != 400:
        return False
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    values = " ".join(str(value).lower() for value in payload.values())
    return (
        "object not found" in values
        or "bucket not found" in values
        or "resource was not found" in values
        or "not_found" in values
    )


def _post_key(post: dict[str, Any]) -> str:
    for field in ("id", "url"):
        value = str(post.get(field) or "").strip()
        if value:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
    fallback = "|".join(
        str(post.get(field) or "")
        for field in ("author_handle", "author_name", "published_at", "text")
    )
    return hashlib.sha256(fallback.encode("utf-8")).hexdigest()

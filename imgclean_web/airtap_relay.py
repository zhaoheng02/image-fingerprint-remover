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


DEFAULT_STATE = {"profiles": {}, "aliases": {}, "seen_posts": {}, "dispatches": {}}
logger = logging.getLogger(__name__)
AVATAR_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
KNOWN_X_PROFILES = {
    "xiaomustock": "川沐｜Trumoo 🐮",
    "hanking66": "美股仙人",
    "aleabitoreddit": "Serenity",
    "iamramenpanda": "RamenPanda",
    "artofspecuycky": "Art of Speculation",
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
        self.media_dir = self.root / "media"
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

    def summary(self) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            profiles = []
            for profile in state.get("profiles", {}).values():
                public = self.public_profile(profile)
                profiles.append(
                    {
                        "display_name": public.get("display_name", ""),
                        "handle": public.get("handle", ""),
                        "avatar_url_present": bool(public.get("avatar_url")),
                        "aliases": public.get("aliases", []),
                        "updated_at": public.get("updated_at"),
                    }
                )
            seen_scopes = {}
            for scope, posts in (state.get("seen_posts") or {}).items():
                if not isinstance(posts, dict):
                    continue
                recent_posts = sorted(
                    posts.values(),
                    key=lambda item: item.get("first_seen_at", 0) if isinstance(item, dict) else 0,
                    reverse=True,
                )[:10]
                seen_scopes[str(scope)] = {
                    "count": len(posts),
                    "recent": [
                        {
                            "author_name": str(post.get("author_name") or ""),
                            "published_at": str(post.get("published_at") or ""),
                            "url_present": bool(post.get("url")),
                            "first_seen_at": post.get("first_seen_at"),
                        }
                        for post in recent_posts
                        if isinstance(post, dict)
                    ],
                }
            return {
                "profile_count": len(profiles),
                "alias_count": len(state.get("aliases") or {}),
                "profiles": sorted(profiles, key=lambda item: (item["handle"], item["display_name"])),
                "seen_scopes": seen_scopes,
            }

    def recent_posts(self, scope: str, *, since_seconds: int, limit: int = 80) -> list[dict[str, Any]]:
        cutoff = int(time.time()) - since_seconds
        with self._lock:
            state = self._read()
            posts = state.get("seen_posts", {}).get(scope, {})
            if not isinstance(posts, dict):
                return []
            recent_items = []
            for item in posts.values():
                if not isinstance(item, dict):
                    continue
                first_seen_at = int(item.get("first_seen_at") or 0)
                if first_seen_at < cutoff:
                    continue
                post = item.get("post")
                if not isinstance(post, dict):
                    post = {
                        "author_name": item.get("author_name", ""),
                        "published_at": item.get("published_at", ""),
                        "url": item.get("url", ""),
                    }
                if not _has_dispatch_content(post):
                    continue
                recent_items.append((first_seen_at, post))
            recent_items.sort(key=lambda item: item[0])
            return [dict(post) for _, post in recent_items[-limit:]]

    def dispatch_status(self, channel: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._read()
            dispatches = state.get("dispatches", {}).get(channel, {})
            dispatch = dispatches.get(key) if isinstance(dispatches, dict) else None
            return dict(dispatch) if isinstance(dispatch, dict) else None

    def record_dispatch(self, channel: str, key: str, details: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            dispatches = state.setdefault("dispatches", {}).setdefault(channel, {})
            dispatch = {"dispatched_at": int(time.time()), **details}
            dispatches[key] = dispatch
            self._write(state)
            return dict(dispatch)

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
                        "author_handle": post.get("author_handle", ""),
                        "published_at": post.get("published_at", ""),
                        "url": post.get("url", ""),
                        "post": _stored_post(post),
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

    def media_path(self, filename: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
        return self.media_dir / safe_name

    def store_media(self, content: bytes, content_type: str, *, prefix: str = "wechat") -> dict[str, str]:
        return self._store_media(content, content_type, prefix=prefix)

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
        inferred_handle = _infer_post_handle(post)
        if inferred_handle and _is_missing_author(enriched.get("author_handle")):
            enriched["author_handle"] = inferred_handle
        profile = self._lookup_profile(
            state,
            str(enriched.get("author_handle") or enriched.get("author_name") or inferred_handle or ""),
        )
        if profile:
            public_profile = self.public_profile(profile)
            if _is_missing_author(enriched.get("author_name")):
                enriched["author_name"] = public_profile["display_name"]
            if _is_missing_author(enriched.get("author_handle")):
                enriched["author_handle"] = public_profile["handle"]
            enriched["avatar_url"] = public_profile["avatar_url"]
        elif inferred_handle:
            normalized_handle = _normalize_alias(inferred_handle)
            if _is_missing_author(enriched.get("author_name")) and normalized_handle in KNOWN_X_PROFILES:
                enriched["author_name"] = KNOWN_X_PROFILES[normalized_handle]
            if _is_missing_author(enriched.get("author_handle")):
                enriched["author_handle"] = inferred_handle
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

    def _store_media(self, content: bytes, content_type: str, *, prefix: str = "wechat") -> dict[str, str]:
        suffix = AVATAR_CONTENT_TYPES.get(content_type, ".jpg")
        safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", prefix).strip("._-") or "wechat"
        filename = f"{safe_prefix}-{_hash_bytes(content)[:16]}{suffix}"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.media_path(filename).write_bytes(content)
        media_url = f"/api/airtap/media/{filename}"
        if self.api_base_url:
            media_url = f"{self.api_base_url}{media_url}"
        return {"media_path": filename, "media_url": media_url}


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

    def _store_media(self, content: bytes, content_type: str, *, prefix: str = "wechat") -> dict[str, str]:
        suffix = AVATAR_CONTENT_TYPES.get(content_type, ".jpg")
        safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", prefix).strip("._-") or "wechat"
        path = f"airtap/media/{safe_prefix}-{_hash_bytes(content)[:16]}{suffix}"
        self._upload_object(path, content, content_type)
        return {"media_path": path, "media_url": self._signed_url(path)}

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
    title = f"X 每小时摘要：{len(posts)} 条线索" if posts else "X 每小时摘要：暂无新内容"
    if not posts:
        content = (
            '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
            'padding:16px;border:1px solid #e5e7eb;border-radius:10px;background:#f8fafc;color:#475569;">'
            "本小时暂无新内容。"
            "</div>"
        )
    else:
        lead = _wechat_digest_lead(posts)
        cards = [_wechat_post_row(post) for post in posts]
        content = (
            '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
            'background:#f8fafc;padding:10px;max-width:100%;box-sizing:border-box;">'
            '<div style="padding:14px 16px;margin-bottom:10px;border-radius:10px;'
            'background:#111827;color:#fff;">'
            '<div style="font-size:13px;color:#cbd5e1;">1 小时信息整理</div>'
            f'<div style="font-size:20px;font-weight:800;line-height:1.35;margin-top:4px;">{html.escape(title)}</div>'
            '<div style="font-size:12px;color:#94a3b8;margin-top:4px;">正文、引用和图片尽量直接展示；回溯信息只留在后台记录里。</div>'
            "</div>"
            f"{lead}"
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
    title = f"8小时市场观察：{len(posts)}条线索" if posts else "8小时市场观察：暂无新内容"
    if not posts:
        body = "这 8 小时暂无新内容。"
    else:
        sections = []
        for index, post in enumerate(posts, start=1):
            sections.append(
                "\n".join(
                    item
                    for item in [
                        f"{index}. {_plain_author(post)}提到：{_plain_text(post)}",
                        f"我会这么看：{_editorial_take(post)}",
                        _plain_quote(post),
                        "有图/视频素材，可以在发小红书时配上。" if (post.get("image_urls") or post.get("video_urls")) else "",
                    ]
                    if item
                )
            )
        body = (
            "这 8 小时我帮你把几个 X 账号的信息压了一遍。不是逐条搬运，主要看这些线索背后可能说明什么。\n\n"
            + "\n\n---\n\n".join(sections)
            + "\n\n整体看下来，先当作信息雷达，不急着下结论；真正要交易还是回到财报、流动性和自己的仓位。"
        )
    return {
        "format": "note",
        "title": title,
        "body": body,
        "hashtags": ["X资讯", "科技股", "美股", "AI", "投资观察"],
    }


def _wechat_post_row(post: dict[str, Any]) -> str:
    avatar = html.escape(str(post.get("avatar_display_url") or post.get("avatar_data_uri") or ""))
    author_name, author_handle = _wechat_author_parts(post)
    avatar_cell = (
        f'<img src="{avatar}" alt="" style="display:block;width:42px;height:42px;border-radius:50%;object-fit:cover;border:1px solid #e5e7eb;">'
        if avatar
        else _wechat_text_avatar(author_name or author_handle)
    )
    media = _wechat_media(post)
    quote = _wechat_quote(post)
    author = html.escape(author_name)
    handle = html.escape(f"@{author_handle}") if author_handle else ""
    handle_line = (
        f'<div style="font-size:12px;color:#64748b;line-height:1.35;word-break:break-all;overflow-wrap:anywhere;">{handle}</div>'
        if handle
        else ""
    )
    time_text = html.escape(str(post.get("published_at") or ""))
    text = html.escape(str(post.get("text") or ""))
    take = html.escape(_editorial_take(post))
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
        '<div style="margin-top:8px;padding:10px 12px;background:#f8fafc;border-radius:8px;'
        'border:1px solid #e2e8f0;">'
        '<div style="font-size:12px;font-weight:800;color:#0f766e;margin-bottom:4px;">这条在说什么</div>'
        f'<div style="white-space:pre-wrap;line-height:1.7;color:#111827;font-size:15px;word-break:break-word;overflow-wrap:anywhere;">{text}</div>'
        "</div>"
        '<div style="margin-top:8px;padding:10px 12px;background:#fff7ed;border-radius:8px;'
        'border:1px solid #fed7aa;">'
        '<div style="font-size:12px;font-weight:800;color:#9a3412;margin-bottom:4px;">为什么值得看</div>'
        f'<div style="line-height:1.65;color:#431407;font-size:14px;word-break:break-word;overflow-wrap:anywhere;">{take}</div>'
        "</div>"
        f"{quote}{media}"
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
    remaining_image_urls = [str(value) for value in post.get("image_urls") or [] if value]

    for item in post.get("wechat_media_items") or []:
        if not isinstance(item, dict):
            continue
        display_url = str(item.get("display_url") or "")
        original_url = str(item.get("url") or "")
        if not display_url:
            continue
        safe_url = html.escape(display_url)
        lines.append(
            '<div style="margin-top:12px;">'
            f'<img src="{safe_url}" alt="图片" style="display:block;width:100%;height:auto;max-width:100%;'
            'border-radius:8px;border:1px solid #e5e7eb;background:#f8fafc;">'
            "</div>"
        )
        if original_url in remaining_image_urls:
            remaining_image_urls.remove(original_url)

    image_data_uris = [str(value) for value in post.get("image_data_uris") or [] if value]
    for data_uri in image_data_uris:
        safe_url = html.escape(data_uri)
        lines.append(
            '<div style="margin-top:12px;">'
            f'<img src="{safe_url}" alt="图片" style="display:block;width:100%;height:auto;max-width:100%;'
            'border-radius:8px;border:1px solid #e5e7eb;background:#f8fafc;">'
            "</div>"
        )
    if image_data_uris:
        remaining_image_urls = remaining_image_urls[len(image_data_uris):]

    for url in remaining_image_urls:
        lines.append(
            '<div style="margin-top:6px;padding:8px 10px;background:#f8fafc;border-radius:8px;'
            'border:1px solid #e2e8f0;word-break:break-all;overflow-wrap:anywhere;">'
            '<span style="font-weight:700;color:#0f172a;">图片素材</span>：'
            '<span style="color:#475569;">已记录，未能稳定内嵌时不在微信里展示外链。</span>'
            "</div>"
        )
    for label, urls in (("视频素材", post.get("video_urls") or []),):
        for _url in urls:
            lines.append(
                '<div style="margin-top:6px;padding:8px 10px;background:#f8fafc;border-radius:8px;'
                'border:1px solid #e2e8f0;word-break:break-all;overflow-wrap:anywhere;">'
                f'<span style="font-weight:700;color:#0f172a;">{html.escape(label)}</span>：'
                '<span style="color:#475569;">已记录，发布小红书时优先使用本地下载素材。</span>'
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


def _wechat_digest_lead(posts: list[dict[str, Any]]) -> str:
    author_names = []
    for post in posts:
        author = _wechat_author_parts(post)[0]
        if author and author not in author_names:
            author_names.append(author)
    author_text = "、".join(author_names[:3])
    if len(author_names) > 3:
        author_text += f" 等 {len(author_names)} 个账号"
    elif not author_text:
        author_text = "几个账号"

    snippets = [_short_text(_plain_text(post), 42) for post in posts[:3] if _plain_text(post)]
    if snippets:
        detail = "；".join(snippets)
    else:
        detail = "这批内容没有太长正文，先按作者和素材保留。"
    lead_text = (
        f"这 1 小时抓到 {len(posts)} 条新线索，主要来自 {author_text}。"
        "我先按信息雷达处理：把正文放出来，再补一层为什么值得看。"
    )
    return (
        '<div style="margin:10px 0;padding:12px 14px;background:#ecfeff;border:1px solid #a5f3fc;'
        'border-radius:10px;color:#164e63;box-sizing:border-box;">'
        '<div style="font-size:13px;font-weight:800;margin-bottom:5px;">我先说结论</div>'
        f'<div style="font-size:14px;line-height:1.65;word-break:break-word;overflow-wrap:anywhere;">{html.escape(lead_text)}</div>'
        f'<div style="font-size:13px;line-height:1.6;margin-top:6px;color:#155e75;word-break:break-word;overflow-wrap:anywhere;">{html.escape(detail)}</div>'
        "</div>"
    )


def _wechat_text_avatar(name: str) -> str:
    initials = html.escape(_initials(name))
    return (
        '<div style="width:42px;height:42px;border-radius:50%;background:#dbeafe;'
        'border:1px solid #bfdbfe;color:#1d4ed8;font-weight:800;font-size:13px;'
        'line-height:42px;text-align:center;letter-spacing:0;">'
        f"{initials}</div>"
    )


def _initials(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", " ", str(value or "")).strip()
    if not cleaned:
        return "X"
    ascii_letters = re.sub(r"[^A-Za-z]", "", cleaned)
    uppercase_letters = re.findall(r"[A-Z]", ascii_letters)
    if len(uppercase_letters) >= 2:
        return (uppercase_letters[0] + uppercase_letters[-1]).upper()
    words = [word for word in cleaned.split() if word]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if ascii_letters:
        return ascii_letters[:2].upper()
    return cleaned[:2]


def _plain_text(post: dict[str, Any]) -> str:
    text = str(post.get("text") or "").strip()
    return re.sub(r"\s+", " ", text)


def _short_text(text: str, limit: int = 80) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "..."


def _editorial_take(post: dict[str, Any]) -> str:
    text = f"{post.get('text') or ''} {json.dumps(post.get('quote') or {}, ensure_ascii=False)}".lower()
    if any(keyword in text for keyword in ("财报", "earnings", "guidance", "revenue")):
        return "这类信息适合先放进财报日历里看，重点不是标题本身，而是预期差和盘后的反应。"
    if any(keyword in text for keyword in ("券商", "报税", "broker", "tax")):
        return "这提醒的是交易基础设施问题。很多看似简单的交易动作，背后其实连着清算、合规和税务。"
    if any(keyword in text for keyword in ("nvda", "gpu", "ai", "算力", "数据中心", "datacenter", "data center")):
        return "这条不只是在说单个公司，更像是在看算力链条里哪些环节正在被重新定价。"
    if any(keyword in text for keyword in ("sec", "fed", "rate", "inflation", "利率", "通胀")):
        return "这类宏观/监管线索不用急着下结论，先看它会不会影响资金风险偏好和仓位变化。"
    if post.get("image_urls") or post.get("video_urls"):
        return "这条带素材，适合回看图表或视频里的原始信息，先看证据再看结论。"
    return "我会先把它当作一个观察点，等后续有没有更多账号、数据或市场反应来验证。"


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


def _stored_post(post: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "author_name",
        "author_handle",
        "published_at",
        "text",
        "url",
        "quote",
        "image_urls",
        "video_urls",
        "avatar_url",
    }
    return {key: value for key, value in post.items() if key in allowed}


def _has_dispatch_content(post: dict[str, Any]) -> bool:
    if str(post.get("text") or "").strip():
        return True
    quote = post.get("quote")
    if isinstance(quote, dict) and str(quote.get("text") or "").strip():
        return True
    return bool(post.get("image_urls") or post.get("video_urls"))


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


def _is_missing_author(value: Any) -> bool:
    normalized = str(value or "").strip()
    return not normalized or normalized.lower() in {"unknown", "none", "null", "-"}


def _infer_post_handle(post: dict[str, Any]) -> str:
    for field in ("author_handle", "source_handle", "handle", "username", "screen_name"):
        value = str(post.get(field) or "").strip().lstrip("@")
        if value and not _is_missing_author(value):
            return value
    for field in ("url", "original_url", "source_url", "canonical_url"):
        handle = _handle_from_x_url(str(post.get(field) or ""))
        if handle:
            return handle
    return ""


def _handle_from_x_url(url: str) -> str:
    match = re.search(r"https?://(?:www\.)?(?:x|twitter)\.com/([^/?#]+)/status/", str(url), re.IGNORECASE)
    if not match:
        return ""
    handle = match.group(1).strip().lstrip("@")
    if handle.lower() in {"i", "home", "search", "explore"}:
        return ""
    return handle


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

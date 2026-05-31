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
from urllib.parse import urlparse

import httpx


DEFAULT_STATE = {"profiles": {}, "aliases": {}, "seen_posts": {}, "dispatches": {}}
logger = logging.getLogger(__name__)
AVATAR_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MEDIA_CONTENT_TYPES = {
    **AVATAR_CONTENT_TYPES,
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-m4v": ".m4v",
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

    def recent_post_shapes(self, scope: str, *, since_seconds: int, limit: int = 20) -> list[dict[str, Any]]:
        posts = self.recent_posts(scope, since_seconds=since_seconds, limit=limit)
        shapes = []
        for post in posts:
            quote = post.get("quote") if isinstance(post.get("quote"), dict) else {}
            shapes.append(
                {
                    "id": str(post.get("id") or ""),
                    "author_name": str(post.get("author_name") or ""),
                    "author_handle": str(post.get("author_handle") or ""),
                    "published_at": str(post.get("published_at") or ""),
                    "url_present": bool(post.get("url")),
                    "text_present": bool(str(post.get("text") or "").strip()),
                    "avatar_present": bool(post.get("avatar_url") or post.get("avatar_display_url")),
                    "image_count": len([value for value in post.get("image_urls") or [] if value]),
                    "video_count": len([value for value in post.get("video_urls") or [] if value]),
                    "link_card_count": len([value for value in post.get("link_cards") or [] if value]),
                    "has_quote": bool(quote),
                    "quote_text_present": bool(str(quote.get("text") or "").strip()) if quote else False,
                    "quote_image_count": len([value for value in quote.get("image_urls") or [] if value]) if quote else 0,
                    "quote_video_count": len([value for value in quote.get("video_urls") or [] if value]) if quote else 0,
                    "media_error_present": bool(post.get("media_error") or (quote.get("media_error") if quote else "")),
                    "quote_error_present": bool(post.get("quote_error") or (quote.get("quote_error") if quote else "")),
                }
            )
        return shapes

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
                if _is_demo_post(post):
                    continue
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
                post = self._enrich_post(state, _normalize_post_schema(raw_post))
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
        quote = enriched.get("quote")
        if isinstance(quote, dict):
            enriched["quote"] = self._enrich_quote(state, quote)
        return enriched

    def _enrich_quote(self, state: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
        enriched = _normalize_post_schema(quote)
        stored_post = _find_stored_post(state, enriched)
        if stored_post:
            for field in (
                "id",
                "author_name",
                "author_handle",
                "published_at",
                "text",
                "url",
                "image_urls",
                "video_urls",
                "link_cards",
                "youtube_cards",
            ):
                if _is_missing_post_value(enriched.get(field)) and not _is_missing_post_value(stored_post.get(field)):
                    enriched[field] = stored_post[field]

        inferred_handle = _infer_post_handle(enriched)
        if inferred_handle and _is_missing_author(enriched.get("author_handle")):
            enriched["author_handle"] = inferred_handle
        profile = self._lookup_profile(
            state,
            str(enriched.get("author_handle") or enriched.get("author_name") or inferred_handle or ""),
        )
        if profile:
            public_profile = self.public_profile(profile)
            enriched["author_name"] = public_profile["display_name"]
            enriched["author_handle"] = public_profile["handle"]
            enriched["avatar_url"] = public_profile["avatar_url"]
        elif inferred_handle and _is_missing_author(enriched.get("author_name")):
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
        suffix = MEDIA_CONTENT_TYPES.get(content_type, ".jpg")
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
        suffix = MEDIA_CONTENT_TYPES.get(content_type, ".jpg")
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
            '<div style="font-size:12px;color:#94a3b8;margin-top:4px;">正文、引用、图片和视频卡片尽量直接展示；回溯信息只留在后台记录里。</div>'
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
    if not posts:
        return {
            "format": "note",
            "title": "这 8 小时暂无适合发的选题",
            "body": "这 8 小时没有收集到足够稳定的素材，先不硬发。",
            "hashtags": ["美股", "投资观察"],
        }

    topic = _xhs_select_topic(posts)
    body = _xhs_note_body(posts, topic)
    return {
        "format": "note",
        "title": str(topic["title"]),
        "body": body,
        "hashtags": list(topic["hashtags"]),
        "source_count": len(posts),
        "topic": str(topic["key"]),
        "media_policy": _xhs_media_policy(posts),
    }


XHS_TOPICS: tuple[dict[str, Any], ...] = (
    {
        "key": "dram",
        "title": "存储股这轮，市场可能在换估值口径",
        "hashtags": ["美股", "AI基建", "存储芯片", "投资观察"],
        "keywords": ("dram", "hbm", "存储", "海力士", "美光", "micron", "三星", "sk hynix", "周期股", "成长股"),
    },
    {
        "key": "ai_infra",
        "title": "AI算力链又有新信号",
        "hashtags": ["AI", "美股", "科技股", "投资观察"],
        "keywords": ("nvda", "nvidia", "gpu", "ai", "算力", "数据中心", "datacenter", "data center", "光模块", "mrvl", "avgo"),
    },
    {
        "key": "broker_tax",
        "title": "交易这件事，绕不开基础设施",
        "hashtags": ["美股", "交易记录", "投资观察", "税务"],
        "keywords": ("券商", "报税", "税务", "broker", "tax", "robinhood", "清算", "合规"),
    },
    {
        "key": "xiaomi_auto",
        "title": "小米汽车的传播效率，确实值得单独看",
        "hashtags": ["小米汽车", "新能源汽车", "商业观察", "产品设计"],
        "keywords": ("xiaomi", "小米", "yu7", "汽车", "法拉利", "ferrari", "tesla", "特斯拉"),
    },
    {
        "key": "earnings",
        "title": "财报周先看预期差，不急着下判断",
        "hashtags": ["美股财报", "科技股", "投资观察"],
        "keywords": ("财报", "earnings", "guidance", "revenue", "eps", "业绩"),
    },
    {
        "key": "market",
        "title": "这批市场线索，我会先这样看",
        "hashtags": ["美股", "投资观察", "市场记录"],
        "keywords": (),
    },
)


def _xhs_select_topic(posts: list[dict[str, Any]]) -> dict[str, Any]:
    scores: dict[str, int] = {str(topic["key"]): 0 for topic in XHS_TOPICS}
    first_match_index: dict[str, int] = {}
    for index, post in enumerate(posts):
        text = _xhs_search_text(post)
        for topic in XHS_TOPICS:
            key = str(topic["key"])
            score = sum(1 for keyword in topic["keywords"] if str(keyword).lower() in text)
            if score:
                scores[key] += score
                first_match_index.setdefault(key, index)
    ranked = sorted(
        XHS_TOPICS,
        key=lambda topic: (
            scores.get(str(topic["key"]), 0),
            -first_match_index.get(str(topic["key"]), len(posts)),
        ),
        reverse=True,
    )
    winner = ranked[0]
    if scores.get(str(winner["key"]), 0) <= 0:
        return XHS_TOPICS[-1]
    return winner


def _xhs_search_text(post: dict[str, Any]) -> str:
    parts = [str(post.get("text") or ""), str(post.get("author_name") or ""), str(post.get("author_handle") or "")]
    quote = post.get("quote")
    if isinstance(quote, dict):
        parts.extend([str(quote.get("text") or ""), str(quote.get("author_name") or ""), str(quote.get("author_handle") or "")])
    return " ".join(parts).lower()


def _xhs_note_body(posts: list[dict[str, Any]], topic: dict[str, Any]) -> str:
    key = str(topic["key"])
    sections = [
        _xhs_opening(posts, key),
        f"AI分析：\n{_xhs_analysis(posts, key)}",
        _xhs_media_policy(posts),
        _xhs_image_plan(posts, key),
        _xhs_close(key),
    ]
    return "\n\n".join(section for section in sections if section)


def _xhs_opening(posts: list[dict[str, Any]], key: str) -> str:
    if key == "dram":
        quote_text = _xhs_first_quote_text(posts)
        quote_line = f"引用里还有一句更直接：{_short_text(quote_text, 62)}" if quote_text else ""
        return "\n".join(
            item
            for item in [
                "今天这批素材里，最值得单独拎出来的是存储股。",
                "有帖子提到，市场开始把存储从周期股往成长股 PE 上挪。这个变化比单日涨跌更关键。",
                quote_line,
            ]
            if item
        )
    if key == "ai_infra":
        signal = _xhs_first_post_text(posts)
        return (
            "这批素材指向一个老问题：AI 算力链里，到底还有哪些环节没有被充分定价。\n"
            f"原始线索里有一句可以留着看：{signal}"
        )
    if key == "broker_tax":
        signal = _xhs_first_post_text(posts)
        return (
            "这条线索不是在聊哪家券商好用，而是在提醒一件更底层的事。\n"
            f"原帖里的核心句子是：{signal}"
        )
    if key == "xiaomi_auto":
        signal = _xhs_first_post_text(posts)
        return (
            "小米汽车这类视频不适合直接搬，但它背后的传播效率值得记一笔。\n"
            f"素材里的原话是：{signal}"
        )
    if key == "earnings":
        signal = _xhs_first_post_text(posts)
        return f"财报相关的线索先别急着当结论看，我会把它放进预期差清单里。\n原始线索：{signal}"
    signal = _xhs_first_post_text(posts)
    return f"这批内容暂时还没有形成特别集中的主题，我会先记成一条市场观察。\n原始线索：{signal}"


def _xhs_analysis(posts: list[dict[str, Any]], key: str) -> str:
    if key == "dram":
        return (
            "如果资金真的把 DRAM/HBM 放进 AI 基建框架，估值弹性会比传统周期股大。"
            "但这条线不能只看口号，后面要盯美光、海力士、三星的盈利上修，以及现货价格有没有继续配合。"
        )
    if key == "ai_infra":
        return (
            "AI 这条链现在的问题不是有没有需求，而是哪一环开始从“卖铲子”变成瓶颈。"
            "我会优先看订单、交付和毛利率，单纯一句看多不够。"
        )
    if key == "broker_tax":
        return (
            "很多人看交易只看买卖按钮，但券商、清算、税表和账户合规才是底座。"
            "一旦这层处理不好，所谓低门槛交易反而可能变成后面的麻烦。"
        )
    if key == "xiaomi_auto":
        return (
            "小米车的话题扩散很快，说明它已经不只是车评内容，而是产品设计、品牌声量和用户审美在一起发酵。"
            "真正要看的是这种关注能不能转成订单和复购，而不是单条视频有多热闹。"
        )
    if key == "earnings":
        return "财报线索最怕只看标题。真正有用的是市场原来预期什么、公司实际交了什么、盘后资金怎么投票。"
    if any(post.get("image_urls") or post.get("video_urls") for post in posts):
        return "这批素材里有图片或视频，我会先看证据，再决定是不是值得扩展成一篇完整笔记。"
    return "现在的信息密度还不够，先当观察点，等后续有没有更多账号或市场反应来验证。"


def _xhs_media_policy(posts: list[dict[str, Any]]) -> str:
    has_video = any(post.get("video_urls") for post in posts)
    has_image = any(post.get("image_urls") for post in posts)
    lines = []
    if has_video:
        lines.append("视频只作为素材线索，不要直接搬带平台水印的视频。发布时改成原创信息图、关键帧重绘，或者只写成文字观察。")
    if has_image:
        lines.append("原图也不要简单拼贴，适合重画成信息卡：保留逻辑，不保留平台外观。")
    return "\n".join(lines)


def _xhs_image_plan(posts: list[dict[str, Any]], key: str) -> str:
    if key == "dram":
        cards = [
            "封面：存储股是不是在换估值？",
            "图2：周期股视角和 AI 基建视角的差别",
            "图3：接下来盯海力士、美光、三星的哪些指标",
            "图4：风险，别把叙事当业绩",
        ]
    elif key == "ai_infra":
        cards = [
            "封面：AI 算力链还有哪里没定价？",
            "图2：需求、交付、毛利率三条线",
            "图3：把原始帖子里的信号改成观察清单",
        ]
    elif key == "broker_tax":
        cards = [
            "封面：交易不是只有买卖按钮",
            "图2：券商、清算、税表、合规的关系",
            "图3：普通投资者容易忽略的坑",
        ]
    elif key == "xiaomi_auto":
        cards = [
            "封面：小米车为什么这么容易被讨论？",
            "图2：设计、价格、传播效率拆开看",
            "图3：热度最后要回到订单验证",
        ]
    else:
        cards = ["封面：这批市场线索先记下来", "图2：原始信号整理", "图3：后续要验证什么"]
    return "配图建议：\n" + "\n".join(f"- {card}" for card in cards)


def _xhs_close(key: str) -> str:
    if key in {"dram", "ai_infra", "earnings"}:
        return "不构成投资建议，我只是把这条线先放进观察清单。后面如果有财报或价格数据跟上，再单独拆。"
    if key == "xiaomi_auto":
        return "这类内容我会少看热闹，多看它能不能变成真实订单。"
    return "先记下来，不急着下结论。"


def _xhs_first_post_text(posts: list[dict[str, Any]]) -> str:
    for post in posts:
        text = _plain_text(post)
        if text:
            return _short_text(text, 90)
    return "这批素材正文不多，先按主题留档。"


def _xhs_first_quote_text(posts: list[dict[str, Any]]) -> str:
    for post in posts:
        quote = post.get("quote")
        if not isinstance(quote, dict):
            continue
        text = _plain_text(quote)
        if text:
            return text
    return ""


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
        '<div style="font-size:12px;font-weight:800;color:#9a3412;margin-bottom:4px;">AI分析</div>'
        f'<div style="line-height:1.65;color:#431407;font-size:14px;word-break:break-word;overflow-wrap:anywhere;">{take}</div>'
        "</div>"
        f"{quote}{media}"
        "</div>"
    )


def _wechat_quote(post: dict[str, Any]) -> str:
    quote = post.get("quote") or {}
    if not isinstance(quote, dict) or not quote:
        return ""
    avatar = html.escape(str(quote.get("avatar_display_url") or quote.get("avatar_data_uri") or ""))
    author_name, author_handle = _wechat_author_parts(quote)
    author = html.escape(author_name or (f"@{author_handle}" if author_handle else "引用内容"))
    handle = html.escape(f"@{author_handle}") if author_handle and author_handle.lower() not in author_name.lower() else ""
    text = html.escape(str(quote.get("text") or ""))
    media = _wechat_media(quote)
    avatar_cell = (
        f'<img src="{avatar}" alt="" style="display:block;width:34px;height:34px;border-radius:50%;object-fit:cover;border:1px solid #bae6fd;">'
        if avatar
        else _wechat_text_avatar(author_name or author_handle)
    )
    text_block = (
        f'<div style="white-space:pre-wrap;line-height:1.6;margin-top:6px;font-size:14px;">{text}</div>'
        if text
        else '<div style="line-height:1.6;margin-top:6px;font-size:14px;color:#64748b;">引用内容这次没有抓全；如果历史库里已有原帖，后端会自动补齐。</div>'
    )
    handle_line = (
        f'<div style="font-size:12px;color:#64748b;line-height:1.35;word-break:break-all;overflow-wrap:anywhere;">{handle}</div>'
        if handle
        else ""
    )
    return (
        '<div style="margin-top:12px;padding:10px 12px;border-left:4px solid #38bdf8;'
        'background:#f0f9ff;color:#334155;border-radius:8px;word-break:break-word;overflow-wrap:anywhere;">'
        '<div style="min-height:36px;">'
        f'<div style="float:left;margin-right:9px;">{avatar_cell}</div>'
        f'<div style="font-size:13px;font-weight:800;line-height:1.35;">引用：{author}</div>'
        f"{handle_line}"
        '<div style="clear:both;"></div>'
        "</div>"
        f"{text_block}{media}"
        "</div>"
    )


def _wechat_media(post: dict[str, Any]) -> str:
    lines = []
    remaining_image_urls = [str(value) for value in post.get("image_urls") or [] if value]
    remaining_video_urls = [str(value) for value in post.get("video_urls") or [] if value]

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
    for item in post.get("wechat_video_items") or []:
        if not isinstance(item, dict):
            continue
        display_url = str(item.get("display_url") or "")
        original_url = str(item.get("url") or "")
        if not display_url:
            continue
        safe_url = html.escape(display_url)
        source_url = html.escape(original_url)
        source_link = (
            f'<a href="{source_url}" style="display:inline-block;margin-top:8px;color:#2563eb;text-decoration:none;'
            'font-size:13px;font-weight:700;">打开原视频</a>'
            if original_url
            else ""
        )
        lines.append(
            '<div style="margin-top:12px;border:1px solid #e2e8f0;border-radius:9px;background:#fff;overflow:hidden;">'
            '<div style="padding:9px 10px;">'
            '<div style="font-size:12px;font-weight:800;color:#7c3aed;margin-bottom:6px;">视频预览</div>'
            f'<video src="{safe_url}" controls preload="metadata" playsinline '
            'style="display:block;width:100%;max-width:100%;height:auto;border-radius:8px;background:#0f172a;"></video>'
            f"{source_link}"
            "</div></div>"
        )
        if original_url in remaining_video_urls:
            remaining_video_urls.remove(original_url)

    for url in remaining_video_urls:
        if _youtube_video_id(url):
            continue
        safe_url = html.escape(url)
        if _is_direct_video_url(url):
            lines.append(
                '<div style="margin-top:12px;border:1px solid #e2e8f0;border-radius:9px;background:#fff;overflow:hidden;">'
                '<div style="padding:9px 10px;">'
                '<div style="font-size:12px;font-weight:800;color:#7c3aed;margin-bottom:6px;">视频预览</div>'
                f'<video src="{safe_url}" controls preload="metadata" playsinline '
                'style="display:block;width:100%;max-width:100%;height:auto;border-radius:8px;background:#0f172a;"></video>'
                f'<a href="{safe_url}" style="display:inline-block;margin-top:8px;color:#2563eb;text-decoration:none;'
                'font-size:13px;font-weight:700;">打不开时点这里</a>'
                "</div></div>"
            )
        else:
            lines.append(
                '<div style="margin-top:12px;border:1px solid #e2e8f0;border-radius:9px;background:#fff;padding:9px 10px;'
                'word-break:break-word;overflow-wrap:anywhere;">'
                '<div style="font-size:12px;font-weight:800;color:#7c3aed;margin-bottom:4px;">视频素材</div>'
                '<div style="font-size:14px;color:#475569;line-height:1.55;">这条带视频，微信内无法稳定内嵌播放。'
                f'<a href="{safe_url}" style="color:#2563eb;text-decoration:none;font-weight:700;">点击打开视频</a></div>'
                "</div>"
            )
    for card in _youtube_cards(post):
        url = html.escape(str(card.get("url") or ""))
        title = html.escape(str(card.get("title") or "YouTube 视频"))
        thumbnail = html.escape(str(card.get("display_url") or card.get("thumbnail_url") or ""))
        thumb = (
            f'<img src="{thumbnail}" alt="YouTube 视频预览" style="display:block;width:100%;height:auto;max-width:100%;'
            'border-radius:8px 8px 0 0;background:#111827;">'
            if thumbnail
            else ""
        )
        lines.append(
            '<div style="margin-top:12px;border:1px solid #e2e8f0;border-radius:9px;background:#fff;overflow:hidden;">'
            f'<a href="{url}" style="color:#0f172a;text-decoration:none;display:block;">'
            f"{thumb}"
            '<div style="padding:9px 10px;">'
            '<div style="font-size:12px;font-weight:800;color:#dc2626;margin-bottom:3px;">YouTube 视频</div>'
            f'<div style="font-size:14px;line-height:1.45;font-weight:700;word-break:break-word;overflow-wrap:anywhere;">{title}</div>'
            '<div style="font-size:12px;color:#64748b;margin-top:4px;word-break:break-all;overflow-wrap:anywhere;">点击打开视频</div>'
            "</div></a></div>"
        )
    if not lines:
        return ""
    return '<div style="margin-top:12px;">' + "".join(lines) + "</div>"


def _youtube_cards(post: dict[str, Any]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_card in list(post.get("youtube_cards") or []) + list(post.get("link_cards") or []):
        if not isinstance(raw_card, dict):
            continue
        url = str(raw_card.get("url") or raw_card.get("href") or "").strip()
        video_id = _youtube_video_id(url)
        provider = str(raw_card.get("provider") or raw_card.get("site") or "").lower()
        if not video_id and "youtube" not in provider:
            continue
        key = video_id or url
        if not key or key in seen:
            continue
        seen.add(key)
        thumbnail = str(raw_card.get("display_url") or raw_card.get("thumbnail_url") or raw_card.get("image_url") or "")
        if not thumbnail and video_id:
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        cards.append(
            {
                "url": url or (f"https://www.youtube.com/watch?v={video_id}" if video_id else ""),
                "title": str(raw_card.get("title") or "YouTube 视频"),
                "thumbnail_url": thumbnail,
                "display_url": str(raw_card.get("display_url") or ""),
            }
        )

    text_sources = [str(post.get("text") or ""), *[str(url) for url in post.get("video_urls") or []]]
    for text in text_sources:
        for url in _youtube_urls(text):
            video_id = _youtube_video_id(url)
            key = video_id or url
            if not key or key in seen:
                continue
            seen.add(key)
            cards.append(
                {
                    "url": url,
                    "title": "YouTube 视频",
                    "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else "",
                    "display_url": "",
                }
            )
    return cards


def _youtube_urls(text: str) -> list[str]:
    pattern = re.compile(
        r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s<>\"']*v=|shorts/|embed/)|youtu\.be/)[^\s<>\"']+",
        re.IGNORECASE,
    )
    return [match.group(0).rstrip(").,，。；;") for match in pattern.finditer(str(text or ""))]


def _youtube_video_id(url: str) -> str:
    value = str(url or "")
    patterns = (
        r"[?&]v=([A-Za-z0-9_-]{6,})",
        r"youtu\.be/([A-Za-z0-9_-]{6,})",
        r"youtube\.com/(?:shorts|embed)/([A-Za-z0-9_-]{6,})",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _is_direct_video_url(url: str) -> bool:
    path = urlparse(str(url or "")).path.lower()
    return path.endswith((".mp4", ".mov", ".m4v", ".webm"))


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
        "我按信息雷达处理：先保留正文和引用，再补一层 AI 分析。"
    )
    return (
        '<div style="margin:10px 0;padding:12px 14px;background:#ecfeff;border:1px solid #a5f3fc;'
        'border-radius:10px;color:#164e63;box-sizing:border-box;">'
        '<div style="font-size:13px;font-weight:800;margin-bottom:5px;">摘要</div>'
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


def _normalize_post_schema(post: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(post)
    _copy_first_present(normalized, "id", ("tweet_id", "status_id", "post_id", "tweetId", "statusId"))
    _copy_first_present(normalized, "url", ("canonical_url", "source_url", "original_url", "permalink", "href"))
    _copy_first_present(normalized, "author_name", ("display_name", "name", "user_name", "username_display"))
    _copy_first_present(
        normalized,
        "author_handle",
        ("handle", "username", "screen_name", "source_handle", "author_username", "user_handle"),
    )
    _copy_first_present(normalized, "published_at", ("time", "timestamp", "created_at", "createdAt", "published"))
    _copy_first_present(normalized, "text", ("full_text", "content", "body", "tweet_text", "tweetText"))

    normalized["author_handle"] = str(normalized.get("author_handle") or "").strip().lstrip("@")
    if _is_missing_post_value(normalized.get("url")):
        inferred_url = _x_status_url_from_parts(
            str(normalized.get("author_handle") or ""),
            str(normalized.get("id") or ""),
        )
        if inferred_url:
            normalized["url"] = inferred_url

    normalized["image_urls"] = _merged_urls(
        normalized.get("image_urls"),
        _urls_from_fields(normalized, ("images", "image", "photo_urls", "photos", "picture_urls", "image_url")),
        _classified_media_urls(normalized, kind="image"),
    )
    normalized["video_urls"] = _merged_urls(
        normalized.get("video_urls"),
        _urls_from_fields(normalized, ("videos", "video", "video_url", "playback_url", "videoUrl")),
        _classified_media_urls(normalized, kind="video"),
    )
    normalized["link_cards"] = _normalized_cards(
        normalized.get("link_cards") or normalized.get("links") or normalized.get("cards") or []
    )
    normalized["youtube_cards"] = _normalized_cards(normalized.get("youtube_cards") or normalized.get("youtube") or [])

    quote = _normalize_quote_schema(normalized)
    if quote:
        normalized["quote"] = quote
    return normalized


def _normalize_quote_schema(post: dict[str, Any]) -> dict[str, Any]:
    quote_value = post.get("quote")
    for field in ("quoted_post", "quoted_tweet", "quoted_status", "quote_tweet", "quoted"):
        if not quote_value and isinstance(post.get(field), (dict, str)):
            quote_value = post.get(field)

    quote: dict[str, Any] = {}
    if isinstance(quote_value, dict):
        quote = dict(quote_value)
    elif isinstance(quote_value, str) and quote_value.strip():
        quote = {"text": quote_value.strip()}

    _copy_first_present(quote, "id", ("tweet_id", "status_id", "post_id", "tweetId", "statusId"))
    _copy_first_present(quote, "url", ("canonical_url", "source_url", "original_url", "permalink", "href"))
    _copy_first_present(quote, "author_name", ("display_name", "name", "user_name", "username_display"))
    _copy_first_present(
        quote,
        "author_handle",
        ("handle", "username", "screen_name", "source_handle", "author_username", "user_handle"),
    )
    _copy_first_present(quote, "published_at", ("time", "timestamp", "created_at", "createdAt", "published"))
    _copy_first_present(quote, "text", ("full_text", "content", "body", "tweet_text", "tweetText"))

    flat_mapping = {
        "id": ("quote_id", "quoted_id", "quoted_post_id", "quoted_tweet_id"),
        "url": ("quote_url", "quoted_url", "quoted_post_url", "quoted_tweet_url"),
        "author_name": ("quote_author_name", "quoted_author_name", "quote_display_name", "quoted_display_name"),
        "author_handle": (
            "quote_author_handle",
            "quoted_author_handle",
            "quote_handle",
            "quoted_handle",
            "quote_username",
            "quoted_username",
        ),
        "published_at": ("quote_published_at", "quoted_published_at", "quote_time", "quoted_time"),
        "text": ("quote_text", "quoted_text", "quote_full_text", "quoted_full_text"),
    }
    for target, aliases in flat_mapping.items():
        value = _first_present_value(post, aliases)
        if not _is_missing_post_value(value) and _is_missing_post_value(quote.get(target)):
            quote[target] = value

    quote["author_handle"] = str(quote.get("author_handle") or "").strip().lstrip("@")
    if _is_missing_post_value(quote.get("url")):
        inferred_url = _x_status_url_from_parts(str(quote.get("author_handle") or ""), str(quote.get("id") or ""))
        if inferred_url:
            quote["url"] = inferred_url

    quote["image_urls"] = _merged_urls(
        quote.get("image_urls"),
        _urls_from_fields(quote, ("images", "image", "photo_urls", "photos", "picture_urls", "image_url")),
        _urls_from_fields(post, ("quote_image_urls", "quoted_image_urls", "quote_images", "quoted_images")),
        _classified_media_urls(quote, kind="image"),
    )
    quote["video_urls"] = _merged_urls(
        quote.get("video_urls"),
        _urls_from_fields(quote, ("videos", "video", "video_url", "playback_url", "videoUrl")),
        _urls_from_fields(post, ("quote_video_urls", "quoted_video_urls", "quote_videos", "quoted_videos")),
        _classified_media_urls(quote, kind="video"),
    )
    quote["link_cards"] = _normalized_cards(
        quote.get("link_cards") or quote.get("links") or post.get("quote_link_cards") or post.get("quoted_link_cards") or []
    )
    quote["youtube_cards"] = _normalized_cards(
        quote.get("youtube_cards") or quote.get("youtube") or post.get("quote_youtube_cards") or []
    )

    return quote if any(not _is_missing_post_value(quote.get(field)) for field in ("id", "url", "text", "author_handle", "author_name")) else {}


def _copy_first_present(target: dict[str, Any], field: str, aliases: tuple[str, ...]) -> None:
    if not _is_missing_post_value(target.get(field)):
        return
    value = _first_present_value(target, aliases)
    if not _is_missing_post_value(value):
        target[field] = value


def _first_present_value(record: dict[str, Any], fields: tuple[str, ...]) -> Any:
    for field in fields:
        value = record.get(field)
        if not _is_missing_post_value(value):
            return value
    return None


def _merged_urls(*values: Any) -> list[str]:
    urls: list[str] = []
    for value in values:
        urls.extend(_urls_from_value(value))
    return list(dict.fromkeys(urls))


def _urls_from_fields(record: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    urls: list[str] = []
    for field in fields:
        urls.extend(_urls_from_value(record.get(field)))
    return list(dict.fromkeys(urls))


def _urls_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [match.group(0).rstrip(").,，。]】") for match in re.finditer(r"https?://[^\s\"'<>]+", value)]
    if isinstance(value, dict):
        urls: list[str] = []
        for key in (
            "url",
            "href",
            "src",
            "media_url",
            "media_url_https",
            "expanded_url",
            "image_url",
            "thumbnail_url",
            "video_url",
            "playback_url",
        ):
            urls.extend(_urls_from_value(value.get(key)))
        for key in ("variants", "sources", "items"):
            urls.extend(_urls_from_value(value.get(key)))
        return list(dict.fromkeys(urls))
    if isinstance(value, (list, tuple, set)):
        urls: list[str] = []
        for item in value:
            urls.extend(_urls_from_value(item))
        return list(dict.fromkeys(urls))
    return []


def _classified_media_urls(record: dict[str, Any], *, kind: str) -> list[str]:
    urls = _urls_from_fields(record, ("media", "media_urls", "attachments"))
    if kind == "video":
        return [url for url in urls if _looks_like_video_url(url)]
    return [url for url in urls if not _looks_like_video_url(url)]


def _normalized_cards(value: Any) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    raw_items = value if isinstance(value, list) else [value] if value else []
    for item in raw_items:
        if isinstance(item, dict):
            card = {
                "provider": str(item.get("provider") or item.get("site") or item.get("source") or ""),
                "title": str(item.get("title") or item.get("text") or ""),
                "url": str(item.get("url") or item.get("href") or ""),
                "thumbnail_url": str(item.get("thumbnail_url") or item.get("image_url") or item.get("thumbnail") or ""),
            }
            if card["url"] or card["thumbnail_url"] or card["title"]:
                cards.append(card)
        elif isinstance(item, str):
            for url in _urls_from_value(item):
                cards.append({"provider": "", "title": "", "url": url, "thumbnail_url": ""})
    return cards


def _x_status_url_from_parts(handle: str, status_id: str) -> str:
    clean_handle = str(handle or "").strip().lstrip("@")
    clean_id = str(status_id or "").strip()
    if not clean_handle or not re.fullmatch(r"\d{6,}", clean_id):
        return ""
    return f"https://x.com/{clean_handle}/status/{clean_id}"


def _looks_like_video_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    path = parsed.path.lower()
    return path.endswith((".mp4", ".webm", ".mov", ".m4v", ".m3u8")) or "video" in parsed.netloc.lower()


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
        "video_cards",
        "link_cards",
        "youtube_cards",
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


def _find_stored_post(state: dict[str, Any], needle: dict[str, Any]) -> dict[str, Any]:
    needle_ids = _post_identity_values(needle)
    if not needle_ids:
        return {}
    for posts in (state.get("seen_posts") or {}).values():
        if not isinstance(posts, dict):
            continue
        for item in posts.values():
            if not isinstance(item, dict):
                continue
            post = item.get("post") if isinstance(item.get("post"), dict) else item
            if not isinstance(post, dict):
                continue
            if needle_ids & _post_identity_values(post):
                return dict(post)
    return {}


def _post_identity_values(post: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for field in ("id", "url", "original_url", "source_url", "canonical_url", "quote_url"):
        raw = str(post.get(field) or "").strip()
        if not raw:
            continue
        values.add(raw)
        normalized_url = _normalized_status_url(raw)
        if normalized_url:
            values.add(normalized_url)
        status_id = _status_id_from_x_url(raw)
        if status_id:
            values.add(status_id)
    return values


def _normalized_status_url(url: str) -> str:
    parsed = urlparse(str(url))
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
    }:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[1].lower() != "status":
        return ""
    return f"https://x.com/{parts[0]}/status/{parts[2]}"


def _status_id_from_x_url(url: str) -> str:
    normalized = _normalized_status_url(url)
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1]


def _is_missing_post_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return not value
    return False


def _is_demo_post(post: dict[str, Any]) -> bool:
    values = [
        str(post.get("id") or ""),
        str(post.get("url") or ""),
        str(post.get("author_handle") or ""),
        str(post.get("published_at") or ""),
    ]
    haystack = " ".join(values).lower()
    demo_markers = (
        "demo-",
        "codex_smoke",
        "xhs-smoke",
        "smoke run",
        "样式验证",
    )
    return any(marker in haystack for marker in demo_markers)


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

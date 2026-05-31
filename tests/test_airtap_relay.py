import base64
import io
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from PIL import Image

from imgclean_web.app import create_app


def _client(tmp_path, monkeypatch, secret="relay-secret"):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "local")
    if secret:
        monkeypatch.setenv("AIRTAP_RELAY_SECRET", secret)
    else:
        monkeypatch.delenv("AIRTAP_RELAY_SECRET", raising=False)
    return TestClient(create_app())


def _avatar_base64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(30, 100, 180)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _large_image_bytes() -> bytes:
    image = Image.effect_noise((900, 600), 80).convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_airtap_profiles_upsert_stores_avatar_and_supports_lookup(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/airtap/profiles/upsert",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "profiles": [
                {
                    "display_name": "xiao mu",
                    "handle": "xiaomustock",
                    "avatar_base64": _avatar_base64(),
                    "avatar_content_type": "image/png",
                }
            ]
        },
    )

    assert response.status_code == 200
    profile = response.json()["profiles"][0]
    assert profile["display_name"] == "xiao mu"
    assert profile["handle"] == "xiaomustock"
    assert profile["avatar_url"].startswith("/api/airtap/avatars/")
    assert profile["aliases"] == ["xiao mu", "xiaomustock"]

    lookup = client.get(
        "/api/airtap/profiles/lookup",
        headers={"authorization": "Bearer relay-secret"},
        params={"name": "@xiaomustock"},
    )

    assert lookup.status_code == 200
    assert lookup.json()["profile"]["display_name"] == "xiao mu"
    avatar = client.get(lookup.json()["profile"]["avatar_url"])
    assert avatar.status_code == 200
    assert avatar.headers["content-type"] == "image/png"


def test_airtap_profiles_upsert_downloads_avatar_url(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    image_bytes = base64.b64decode(_avatar_base64())

    class FakeResponse:
        status_code = 200
        content = image_bytes
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            assert url == "https://airtap.ai/content/live/android-files/xiaomu.png"
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)

    response = client.post(
        "/api/airtap/profiles/upsert",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "profiles": [
                {
                    "display_name": "xiao mu",
                    "handle": "xiaomustock",
                    "avatar_url": "https://airtap.ai/content/live/android-files/xiaomu.png",
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["profiles"][0]["avatar_url"].startswith("/api/airtap/avatars/")


def test_airtap_posts_render_enriches_profiles_without_marking_seen(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    image_bytes = base64.b64decode(_avatar_base64())

    class FakeResponse:
        content = image_bytes
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, timeout, follow_redirects=False):
            self.timeout = timeout
            self.follow_redirects = follow_redirects

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            assert url == "https://airtap.ai/content/live/android-files/chart.png"
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)
    client.post(
        "/api/airtap/profiles/upsert",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "profiles": [
                {
                    "display_name": "xiao mu",
                    "handle": "xiaomustock",
                    "avatar_base64": _avatar_base64(),
                    "avatar_content_type": "image/png",
                }
            ]
        },
    )

    payload = {
        "scope": "x-hourly-watch",
        "channels": ["wechat", "xiaohongshu"],
        "posts": [
            {
                "id": "tweet-1",
                "author_name": "xiao mu",
                "author_handle": "xiaomustock",
                "published_at": "4分钟前",
                "text": "NVDA keeps shipping <fast>.",
                "url": "https://x.com/xiaomustock/status/1",
                "quote": {
                    "author_name": "Someone",
                    "text": "Data center demand is still strong.",
                },
                "image_urls": ["https://airtap.ai/content/live/android-files/chart.png"],
                "video_urls": ["https://airtap.ai/content/live/android-files/clip.mp4"],
            }
        ],
    }

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json=payload,
    )

    assert response.status_code == 200
    rendered = response.json()
    assert rendered["new_count"] == 1
    assert rendered["duplicate_count"] == 0
    assert rendered["channels"]["wechat"]["target"] == "pushplus"
    assert rendered["channels"]["wechat"]["template"] == "html"
    assert "xiao mu" in rendered["channels"]["wechat"]["content"]
    assert "NVDA keeps shipping &lt;fast&gt;." in rendered["channels"]["wechat"]["content"]
    assert "<table" not in rendered["channels"]["wechat"]["content"]
    assert "1 小时信息整理" in rendered["channels"]["wechat"]["content"]
    assert "摘要" in rendered["channels"]["wechat"]["content"]
    assert "AI分析" in rendered["channels"]["wechat"]["content"]
    assert "我先说结论" not in rendered["channels"]["wechat"]["content"]
    assert "为什么值得看" not in rendered["channels"]["wechat"]["content"]
    assert "原文链接" not in rendered["channels"]["wechat"]["content"]
    assert "https://x.com/" not in rendered["channels"]["wechat"]["content"]
    assert '<img src="/api/airtap/media/' in rendered["channels"]["wechat"]["content"]
    assert '<img src="https://airtap.ai/content/live/android-files/chart.png"' not in rendered["channels"]["wechat"]["content"]
    assert "视频预览" in rendered["channels"]["wechat"]["content"]
    assert "<video" in rendered["channels"]["wechat"]["content"]
    assert "https://airtap.ai/content/live/android-files/clip.mp4" in rendered["channels"]["wechat"]["content"]
    assert "/api/airtap/avatars/" not in rendered["channels"]["wechat"]["content"]
    assert rendered["channels"]["xiaohongshu"]["format"] == "note"
    assert rendered["channels"]["xiaohongshu"]["title"] == "8小时市场观察：1条线索"
    assert "AI分析：" in rendered["channels"]["xiaohongshu"]["body"]
    assert "我会这么看：" not in rendered["channels"]["xiaohongshu"]["body"]
    assert "这 8 小时我帮你" not in rendered["channels"]["xiaohongshu"]["body"]
    assert "整体看下来" not in rendered["channels"]["xiaohongshu"]["body"]
    assert "NVDA keeps shipping <fast>." in rendered["channels"]["xiaohongshu"]["body"]
    assert "https://airtap.ai/content/live/android-files/chart.png" not in rendered["channels"]["xiaohongshu"]["body"]

    second_preview = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json=payload,
    )

    assert second_preview.status_code == 200
    assert second_preview.json()["new_count"] == 1
    assert second_preview.json()["duplicate_count"] == 0


def test_wechat_digest_uses_text_avatar_and_hides_source_links(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "wechat-digest",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "tweet-no-avatar-1",
                    "author_name": "IamRamenPanda",
                    "published_at": "3h ago",
                    "text": "炒股需要券商的根本原因是券商要报税",
                    "url": "https://x.com/IamRamenPanda/status/2060993144800625145",
                },
                {
                    "id": "tweet-no-avatar-2",
                    "author_name": "ArtofSpecuycky",
                    "published_at": "2h ago",
                    "text": "下周重点关注这几个财报",
                    "url": "https://x.com/ArtofSpecuycky/status/2061018073185091633",
                },
            ],
        },
    )

    assert response.status_code == 200
    content = response.json()["channels"]["wechat"]["content"]
    assert "X 每小时摘要：2 条线索" in content
    assert "摘要" in content
    assert "AI分析" in content
    assert "我先说结论" not in content
    assert "为什么值得看" not in content
    assert "Codex" not in content
    assert "Airtap" not in content
    assert "IP" in content
    assert "AS" in content
    assert "原文链接" not in content
    assert "https://x.com/" not in content
    assert "炒股需要券商的根本原因是券商要报税" in content
    assert "下周重点关注这几个财报" in content


def test_wechat_digest_enriches_quote_from_stored_history_and_profile(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    profile = client.post(
        "/api/airtap/profiles/upsert",
        headers={"x-airtap-secret": "relay-secret"},
        json={"profiles": [{"display_name": "川沐｜Trumoo", "handle": "xiaomustock"}]},
    )
    history = client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-hourly-wechat",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "2060000000000000000",
                    "author_handle": "xiaomustock",
                    "published_at": "2026年4月27日",
                    "text": "海力士韩国开盘爆拉5%，等市场把存储股从周期股定为成长股。",
                    "url": "https://x.com/xiaomustock/status/2060000000000000000",
                }
            ],
        },
    )

    assert profile.status_code == 200
    assert history.status_code == 200

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "quote-history-preview",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "2061082495332794506",
                    "author_handle": "xiaomustock",
                    "published_at": "2026-05-31",
                    "text": "HODL，$DRAM 坚持。",
                    "url": "https://x.com/xiaomustock/status/2061082495332794506",
                    "quote": {
                        "url": "https://x.com/xiaomustock/status/2060000000000000000",
                        "author_handle": "xiaomustock",
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    content = response.json()["channels"]["wechat"]["content"]
    assert "引用" in content
    assert "川沐｜Trumoo" in content
    assert "@xiaomustock" in content
    assert "海力士韩国开盘爆拉5%" in content


def test_wechat_digest_renders_youtube_preview_card(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "youtube-card-preview",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "tweet-youtube-1",
                    "author_handle": "xiaomustock",
                    "text": "这条引用了一个 YouTube 访谈 https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "url": "https://x.com/xiaomustock/status/2061",
                }
            ],
        },
    )

    assert response.status_code == 200
    content = response.json()["channels"]["wechat"]["content"]
    assert "YouTube 视频" in content
    assert "i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg" in content
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in content


def test_airtap_posts_infer_author_from_source_handle(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post(
        "/api/airtap/profiles/upsert",
        headers={"x-airtap-secret": "relay-secret"},
        json={"profiles": [{"display_name": "川沐｜Trumoo 🐮", "handle": "xiaomustock"}]},
    )

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "wechat-infer-source",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "tweet-infer-source-1",
                    "source_handle": "xiaomustock",
                    "text": "AI PC 的小思考。",
                }
            ],
        },
    )

    assert response.status_code == 200
    post = response.json()["posts"][0]
    assert post["author_name"] == "川沐｜Trumoo 🐮"
    assert post["author_handle"] == "xiaomustock"
    content = response.json()["channels"]["wechat"]["content"]
    assert "Unknown" not in content
    assert "川沐｜Trumoo" in content


def test_airtap_posts_infer_author_from_x_url(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "wechat-infer-url",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "tweet-infer-url-1",
                    "url": "https://x.com/IamRamenPanda/status/2060993144800625145",
                    "text": "炒股需要券商的根本原因是券商要报税。",
                }
            ],
        },
    )

    assert response.status_code == 200
    post = response.json()["posts"][0]
    assert post["author_name"] == "RamenPanda"
    assert post["author_handle"] == "IamRamenPanda"
    assert "Unknown" not in response.json()["channels"]["wechat"]["content"]


def test_airtap_posts_render_hosts_avatar_and_media_separately(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    image_bytes = _large_image_bytes()
    media_url = "https://airtap.ai/content/live/android-files/shared-large.png"

    class FakeResponse:
        content = image_bytes
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, timeout, follow_redirects=False):
            self.timeout = timeout
            self.follow_redirects = follow_redirects

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            assert url == media_url
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-hourly-inline-shared",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "tweet-inline-shared-1",
                    "author_name": "xiao mu",
                    "avatar_url": media_url,
                    "text": "头像和正文图片都需要直接内嵌。",
                    "image_urls": [media_url],
                }
            ],
        },
    )

    assert response.status_code == 200
    content = response.json()["channels"]["wechat"]["content"]
    assert content.count('src="/api/airtap/media/') >= 2
    assert '<img src="https://' not in content


def test_airtap_posts_render_uses_pushplus_image_service_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "local")
    monkeypatch.setenv("AIRTAP_RELAY_SECRET", "relay-secret")
    monkeypatch.setenv("PUSHPLUS_ACCESS_KEY", "pushplus-access-key")
    image_bytes = _large_image_bytes()
    media_url = "https://airtap.ai/content/live/android-files/chart.png"
    calls = []

    class FakeImageResponse:
        content = image_bytes
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class FakeTokenResponse:
        headers = {"content-type": "application/json"}
        content = b""

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "data": {"uploadUrl": "https://upload.pushplus.test", "token": "upload-token"}}

    class FakeUploadResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"url": "https://pic.pushplus.plus/airtap/chart.jpg"}

    class FakeAsyncClient:
        def __init__(self, timeout, follow_redirects=False):
            self.timeout = timeout
            self.follow_redirects = follow_redirects

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            calls.append(("GET", url, headers))
            if url == "https://www.pushplus.plus/api/open/userImage/uploadToken":
                assert headers == {"access-key": "pushplus-access-key"}
                return FakeTokenResponse()
            assert url == media_url
            return FakeImageResponse()

        async def post(self, url, data=None, files=None, **kwargs):
            calls.append(("POST", url, data, bool(files), kwargs))
            assert url == "https://upload.pushplus.test"
            assert data == {"token": "upload-token"}
            assert files
            return FakeUploadResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)
    client = TestClient(create_app())

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-pushplus-image-service",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "tweet-pushplus-image-1",
                    "author_name": "xiao mu",
                    "text": "正文图应该走 PushPlus 图床。",
                    "image_urls": [media_url],
                }
            ],
        },
    )

    assert response.status_code == 200
    content = response.json()["channels"]["wechat"]["content"]
    assert 'src="https://pic.pushplus.plus/airtap/chart.jpg"' in content
    assert 'src="data:image/' not in content
    assert "/api/airtap/media/" not in content
    assert "图片素材" not in content
    assert [call[0] for call in calls] == ["GET", "GET", "POST"]


def test_airtap_posts_render_does_not_prevent_later_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "local")
    monkeypatch.setenv("AIRTAP_RELAY_SECRET", "relay-secret")
    monkeypatch.setenv("PUSHPLUS_TOKEN", "push-token")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "请求成功", "data": "push-message-id"}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            calls.append((url, json))
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)
    client = TestClient(create_app())
    payload = {
        "scope": "x-preview-then-publish",
        "channels": ["wechat"],
        "posts": [{"id": "tweet-preview-1", "author_name": "xiao mu", "text": "preview first"}],
    }

    preview = client.post("/api/airtap/posts/render", headers={"x-airtap-secret": "relay-secret"}, json=payload)
    published = client.post("/api/airtap/posts/publish", headers={"x-airtap-secret": "relay-secret"}, json=payload)

    assert preview.status_code == 200
    assert preview.json()["new_count"] == 1
    assert published.status_code == 200
    assert published.json()["new_count"] == 1
    assert published.json()["pushes"]["wechat"]["ok"] is True
    assert len(calls) == 1


def test_airtap_xhs_dispatch_uses_stored_hourly_posts_without_rescrape(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    blank = client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-hourly-wechat",
            "channels": ["xiaohongshu"],
            "posts": [{"id": "tweet-hourly-blank-1"}],
        },
    )
    publish = client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-hourly-wechat",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "tweet-hourly-stored-1",
                    "author_handle": "IamRamenPanda",
                    "text": "炒股需要券商的根本原因是券商要报税。",
                    "url": "https://x.com/IamRamenPanda/status/2060993144800625145",
                }
            ],
        },
    )

    assert blank.status_code == 200
    assert publish.status_code == 200

    dispatch = client.get(
        "/api/airtap/xhs/dispatch",
        headers={"x-airtap-secret": "relay-secret"},
        params={"dry_run": "1"},
    )

    assert dispatch.status_code == 200
    payload = dispatch.json()
    assert payload["ok"] is True
    assert payload["post_count"] == 1
    assert payload["channels"]["xiaohongshu"]["title"] == "8小时市场观察：1条线索"
    assert "炒股需要券商" in payload["channels"]["xiaohongshu"]["body"]
    assert payload["airtap"]["ok"] is False
    assert payload["airtap"]["reason"] == "dry_run"


def test_airtap_xhs_dispatch_skips_demo_hourly_posts(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    publish = client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-hourly-wechat",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "demo-1",
                    "author_handle": "codex_smoke",
                    "published_at": "样式验证",
                    "text": "这是一条样式验证内容，不应该进入定时小红书汇总。",
                    "url": "https://x.com/codex_smoke/status/demo-1",
                },
                {
                    "id": "tweet-hourly-real-1",
                    "author_handle": "IamRamenPanda",
                    "text": "炒股需要券商的根本原因是券商要报税。",
                    "url": "https://x.com/IamRamenPanda/status/2060993144800625145",
                },
            ],
        },
    )

    assert publish.status_code == 200

    dispatch = client.get(
        "/api/airtap/xhs/dispatch",
        headers={"x-airtap-secret": "relay-secret"},
        params={"dry_run": "1"},
    )

    assert dispatch.status_code == 200
    payload = dispatch.json()
    assert payload["post_count"] == 1
    assert "炒股需要券商" in payload["channels"]["xiaohongshu"]["body"]
    assert "样式验证" not in payload["channels"]["xiaohongshu"]["body"]


def test_airtap_xhs_dispatch_pushes_note_preview_for_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("PUSHPLUS_TOKEN", "push-token")
    monkeypatch.setenv("IMGCLEAN_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("AIRTAP_PERSONAL_ACCESS_TOKEN", "airtap-token")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "执行成功", "data": "push-message-id"}

    class FakeAsyncClient:
        def __init__(self, timeout, **kwargs):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None, **kwargs):
            calls.append((url, headers or {}, json or {}))
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)
    client = _client(tmp_path, monkeypatch)
    client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-hourly-wechat",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "tweet-hourly-task-1",
                    "author_handle": "ArtofSpecuycky",
                    "text": "下周重点关注这几个财报。",
                    "url": "https://x.com/ArtofSpecuycky/status/2061018073185091633",
                }
            ],
        },
    )
    calls.clear()

    dispatch = client.get("/api/airtap/xhs/dispatch", headers={"x-airtap-secret": "relay-secret"})
    duplicate = client.get("/api/airtap/xhs/dispatch", headers={"x-airtap-secret": "relay-secret"})

    assert dispatch.status_code == 200
    payload = dispatch.json()
    assert payload["airtap"]["reason"] == "awaiting_confirmation"
    assert payload["approval_push"]["ok"] is True
    assert payload["approval"]["confirm_url"].startswith("http")
    assert duplicate.status_code == 200
    assert duplicate.json()["airtap"]["reason"] == "already_dispatched"
    assert len(calls) == 1
    url, _headers, push_payload = calls[0]
    assert url == "https://www.pushplus.plus/send"
    assert "确认发布到小红书" in push_payload["content"]
    assert payload["approval"]["confirm_url"] in push_payload["content"]
    assert "下周重点关注这几个财报" in push_payload["content"]


def test_airtap_xhs_confirmation_endpoint_creates_publish_task(tmp_path, monkeypatch):
    monkeypatch.setenv("PUSHPLUS_TOKEN", "push-token")
    monkeypatch.setenv("IMGCLEAN_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("AIRTAP_PERSONAL_ACCESS_TOKEN", "airtap-token")
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, timeout, **kwargs):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None, **kwargs):
            calls.append((url, headers or {}, json or {}))
            if url == "https://www.pushplus.plus/send":
                return FakeResponse({"code": 200, "msg": "执行成功", "data": "push-message-id"})
            return FakeResponse({"taskId": "task-xhs-confirmed", "taskState": "QUEUED"})

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)
    client = _client(tmp_path, monkeypatch)
    client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-hourly-wechat",
            "channels": ["wechat"],
            "posts": [
                {
                    "id": "tweet-hourly-task-confirm-1",
                    "author_handle": "ArtofSpecuycky",
                    "text": "下周重点关注这几个财报。",
                    "url": "https://x.com/ArtofSpecuycky/status/2061018073185091633",
                }
            ],
        },
    )
    calls.clear()
    dispatch = client.get("/api/airtap/xhs/dispatch", headers={"x-airtap-secret": "relay-secret"})
    confirm_url = dispatch.json()["approval"]["confirm_url"]
    token = parse_qs(urlparse(confirm_url).query)["token"][0]

    confirmation = client.get(f"/api/airtap/xhs/confirm?token={token}")

    assert confirmation.status_code == 200
    assert "已创建小红书发布任务" in confirmation.text
    assert any(call[0] == "https://airtap.ai/cortex/api/task/v1/taskCreate" for call in calls)
    airtap_call = [call for call in calls if call[0] == "https://airtap.ai/cortex/api/task/v1/taskCreate"][0]
    headers = airtap_call[1]
    message = airtap_call[2]["userMessage"]["parts"][0]["text"]
    assert headers["Authorization"] == "Bearer airtap-token"
    assert "Do not open X" in message
    assert "下周重点关注这几个财报" in message
    assert "relay-secret" not in message


def test_airtap_debug_summary_reports_profiles_and_seen_posts(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "local")
    monkeypatch.setenv("AIRTAP_RELAY_SECRET", "relay-secret")
    monkeypatch.setenv("PUSHPLUS_TOKEN", "push-token")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "请求成功"}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)
    client = TestClient(create_app())
    client.post(
        "/api/airtap/profiles/upsert",
        headers={"x-airtap-secret": "relay-secret"},
        json={"profiles": [{"display_name": "Art", "handle": "ArtofSpecuycky"}]},
    )
    client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-hourly-watch",
            "channels": ["wechat"],
            "posts": [{"id": "tweet-summary-1", "author_handle": "ArtofSpecuycky", "text": "summary"}],
        },
    )

    response = client.get("/api/airtap/debug/summary", headers={"x-airtap-secret": "relay-secret"})

    assert response.status_code == 200
    summary = response.json()["summary"]
    assert summary["profile_count"] == 1
    assert summary["alias_count"] == 2
    assert summary["profiles"][0]["handle"] == "ArtofSpecuycky"
    assert summary["seen_scopes"]["x-hourly-watch"]["count"] == 1
    assert summary["seen_scopes"]["x-hourly-watch"]["recent"][0]["author_name"] == "Art"


def test_airtap_posts_render_uses_ai_composer_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "local")
    monkeypatch.setenv("AIRTAP_RELAY_SECRET", "relay-secret")
    monkeypatch.setenv("AIRTAP_AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.xairouter.com")
    monkeypatch.setenv("AIRTAP_AI_MODEL", "gpt-5.5")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    '{"wechat":{"target":"pushplus","template":"html","title":"AI标题",'
                                    '"content":"<table><tr><td>AI微信内容</td></tr></table>"},'
                                    '"xiaohongshu":{"format":"note","title":"AI小红书标题","body":"AI小红书正文",'
                                    '"hashtags":["AI","美股"]}}'
                                ),
                            }
                        ]
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            calls.append((url, headers, json))
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)
    client = TestClient(create_app())

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-ai-compose",
            "channels": ["wechat", "xiaohongshu"],
            "posts": [
                {
                    "id": "tweet-ai-1",
                    "author_name": "xiao mu",
                    "published_at": "4分钟前",
                    "text": "NVDA keeps shipping.",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["channels"]["wechat"]["title"] == "X 每小时摘要：1 条线索"
    assert "<table" not in payload["channels"]["wechat"]["content"]
    assert payload["channels"]["xiaohongshu"]["title"] == "AI小红书标题"
    assert payload["channels"]["xiaohongshu"]["body"] == "AI小红书正文"
    assert calls[0][0] == "https://api.xairouter.com/v1/responses"
    assert calls[0][1]["authorization"] == "Bearer openai-key"
    assert calls[0][2]["model"] == "gpt-5.5"
    assert "需要生成的渠道：xiaohongshu" in calls[0][2]["input"]
    assert "NVDA keeps shipping." in calls[0][2]["input"]


def test_airtap_posts_render_skips_ai_composer_for_wechat_only(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "local")
    monkeypatch.setenv("AIRTAP_RELAY_SECRET", "relay-secret")
    monkeypatch.setenv("AIRTAP_AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    calls = []

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            calls.append((url, headers, json))
            raise AssertionError("AI composer should not run for WeChat-only payloads")

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)
    client = TestClient(create_app())

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-ai-wechat-only",
            "channels": ["wechat"],
            "posts": [{"id": "tweet-ai-2", "author_name": "xiao mu", "text": "wechat only"}],
        },
    )

    assert response.status_code == 200
    assert "wechat only" in response.json()["channels"]["wechat"]["content"]
    assert calls == []


def test_airtap_posts_publish_pushes_wechat_content_with_pushplus(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "local")
    monkeypatch.setenv("AIRTAP_RELAY_SECRET", "relay-secret")
    monkeypatch.setenv("PUSHPLUS_TOKEN", "push-token")
    monkeypatch.setenv("PUSHPLUS_TOPIC", "x-hourly")
    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "请求成功", "data": "push-message-id"}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            calls.append((url, headers, json))
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.app.httpx.AsyncClient", FakeAsyncClient)
    client = TestClient(create_app())

    response = client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-pushplus",
            "channels": ["wechat", "xiaohongshu"],
            "posts": [
                {
                    "id": "tweet-push-1",
                    "author_name": "xiao mu",
                    "published_at": "4分钟前",
                    "text": "NVDA keeps shipping.",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["new_count"] == 1
    assert payload["pushes"]["wechat"]["ok"] is True
    assert payload["pushes"]["wechat"]["provider"] == "pushplus"
    assert payload["pushes"]["wechat"]["provider_message_id"] == "push-message-id"
    assert calls[0][0] == "https://www.pushplus.plus/send"
    assert calls[0][1]["content-type"] == "application/json"
    assert calls[0][2]["token"] == "push-token"
    assert calls[0][2]["template"] == "html"
    assert calls[0][2]["topic"] == "x-hourly"
    assert "NVDA keeps shipping." in calls[0][2]["content"]

    duplicate = client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-pushplus",
            "channels": ["wechat"],
            "posts": [{"id": "tweet-push-1", "author_name": "xiao mu", "text": "NVDA keeps shipping."}],
        },
    )

    assert duplicate.status_code == 200
    assert duplicate.json()["new_count"] == 0
    assert duplicate.json()["pushes"]["wechat"]["ok"] is False
    assert duplicate.json()["pushes"]["wechat"]["reason"] == "no_new_posts"
    assert len(calls) == 1


def test_airtap_posts_publish_reports_missing_pushplus_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "scope": "x-missing-token",
            "channels": ["wechat"],
            "posts": [{"id": "tweet-1", "author_name": "xiao mu", "text": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["pushes"]["wechat"] == {
        "ok": False,
        "provider": "pushplus",
        "reason": "pushplus_token_not_configured",
    }


def test_airtap_relay_requires_configured_secret(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, secret="")

    missing_config = client.post("/api/airtap/posts/render", json={"posts": []})

    assert missing_config.status_code == 503
    assert missing_config.json()["detail"] == "Airtap relay secret is not configured."

    client = _client(tmp_path, monkeypatch)
    wrong_secret = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "wrong"},
        json={"posts": []},
    )

    assert wrong_secret.status_code == 401
    assert wrong_secret.json()["detail"] == "Invalid Airtap relay secret."


def test_airtap_profiles_use_supabase_storage_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "service-key")
    monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", "imgclean-files")
    monkeypatch.setenv("AIRTAP_STORAGE_BUCKET", "imgclean-airtap")
    monkeypatch.setenv("AIRTAP_RELAY_SECRET", "relay-secret")
    calls = []

    class FakeResponse:
        def __init__(self, payload=None, status_code=200, content=b""):
            self.payload = payload or {}
            self.status_code = status_code
            self.content = content

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"unexpected HTTP {self.status_code}")

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers):
            calls.append(("GET", url, headers, None))
            return FakeResponse(status_code=404)

        def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs.get("headers"), kwargs.get("content")))
            if "/object/sign/" in url:
                return FakeResponse({"signedURL": "/object/sign/imgclean-airtap/airtap/avatars/a.png?token=abc"})
            return FakeResponse({"Key": "ok"})

    monkeypatch.setattr("imgclean_web.airtap_relay.httpx.Client", FakeClient)
    client = TestClient(create_app())

    response = client.post(
        "/api/airtap/profiles/upsert",
        headers={"x-airtap-secret": "relay-secret"},
        json={
            "profiles": [
                {
                    "display_name": "xiao mu",
                    "handle": "xiaomustock",
                    "avatar_base64": _avatar_base64(),
                    "avatar_content_type": "image/png",
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["profiles"][0]["avatar_url"].startswith("https://project.supabase.co/storage/v1/object/sign/")
    assert any(call[0] == "POST" and "/object/imgclean-airtap/airtap/avatars/" in call[1] for call in calls)
    assert any(call[0] == "POST" and call[1].endswith("/object/imgclean-airtap/airtap/state.json") for call in calls)


def test_airtap_supabase_missing_state_accepts_storage_400(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "service-key")
    monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", "imgclean-files")
    monkeypatch.setenv("AIRTAP_STORAGE_BUCKET", "imgclean-airtap")
    monkeypatch.setenv("AIRTAP_RELAY_SECRET", "relay-secret")

    class FakeResponse:
        status_code = 400
        content = b""

        def json(self):
            return {"statusCode": "404", "error": "not_found", "message": "Object not found"}

        def raise_for_status(self):
            raise AssertionError("missing state should initialize an empty relay store")

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers):
            assert url == "https://project.supabase.co/storage/v1/object/authenticated/imgclean-airtap/airtap/state.json"
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.airtap_relay.httpx.Client", FakeClient)
    client = TestClient(create_app())

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={"scope": "first-run", "posts": []},
    )

    assert response.status_code == 200
    assert response.json()["new_count"] == 0


def test_airtap_supabase_missing_bucket_accepts_storage_400(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "service-key")
    monkeypatch.setenv("AIRTAP_STORAGE_BUCKET", "imgclean-airtap")
    monkeypatch.setenv("AIRTAP_RELAY_SECRET", "relay-secret")

    class FakeResponse:
        status_code = 400
        content = b""

        def json(self):
            return {"statusCode": "404", "error": "Bucket not found", "message": "Bucket not found"}

        def raise_for_status(self):
            raise AssertionError("missing bucket should initialize an empty relay store")

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers):
            return FakeResponse()

    monkeypatch.setattr("imgclean_web.airtap_relay.httpx.Client", FakeClient)
    client = TestClient(create_app())

    response = client.post(
        "/api/airtap/posts/render",
        headers={"x-airtap-secret": "relay-secret"},
        json={"scope": "first-run", "posts": []},
    )

    assert response.status_code == 200
    assert response.json()["new_count"] == 0


def test_airtap_supabase_upload_creates_missing_bucket_and_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv("IMGCLEAN_AUTH_MODE", "none")
    monkeypatch.setenv("IMGCLEAN_STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "service-key")
    monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", "imgclean-files")
    monkeypatch.setenv("AIRTAP_STORAGE_BUCKET", "imgclean-airtap")
    monkeypatch.setenv("AIRTAP_RELAY_SECRET", "relay-secret")
    calls = []

    class FakeResponse:
        def __init__(self, status_code=200, content=b"", text=""):
            self.status_code = status_code
            self.content = content
            self.text = text

        def json(self):
            return {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"unexpected HTTP {self.status_code}")

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers):
            calls.append(("GET", url))
            return FakeResponse(status_code=404)

        def post(self, url, **kwargs):
            calls.append(("POST", url))
            if url == "https://project.supabase.co/storage/v1/bucket":
                return FakeResponse(status_code=200, text='{"name":"imgclean-airtap"}')
            object_uploads = [
                call for call in calls
                if call == ("POST", "https://project.supabase.co/storage/v1/object/imgclean-airtap/airtap/state.json")
            ]
            if len(object_uploads) == 1:
                return FakeResponse(status_code=400, text='{"message":"Bucket not found"}')
            return FakeResponse(status_code=200, text='{"Key":"airtap/state.json"}')

    monkeypatch.setattr("imgclean_web.airtap_relay.httpx.Client", FakeClient)
    client = TestClient(create_app())

    response = client.post(
        "/api/airtap/posts/publish",
        headers={"x-airtap-secret": "relay-secret"},
        json={"scope": "first-run", "channels": ["wechat"], "posts": []},
    )

    assert response.status_code == 200
    assert calls == [
        ("GET", "https://project.supabase.co/storage/v1/object/authenticated/imgclean-airtap/airtap/state.json"),
        ("POST", "https://project.supabase.co/storage/v1/object/imgclean-airtap/airtap/state.json"),
        ("POST", "https://project.supabase.co/storage/v1/bucket"),
        ("POST", "https://project.supabase.co/storage/v1/object/imgclean-airtap/airtap/state.json"),
    ]

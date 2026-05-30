import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MINIAPP = ROOT / "miniapp"


def test_miniapp_scaffold_has_required_wechat_files():
    for relative in [
        "project.config.json",
        "app.json",
        "app.js",
        "app.wxss",
        "sitemap.json",
        "config.js",
        "pages/index/index.json",
        "pages/index/index.js",
        "pages/index/index.wxml",
        "pages/index/index.wxss",
    ]:
        assert (MINIAPP / relative).exists(), relative


def test_miniapp_config_points_to_hosted_api_and_declares_page():
    project = json.loads((MINIAPP / "project.config.json").read_text(encoding="utf-8"))
    app = json.loads((MINIAPP / "app.json").read_text(encoding="utf-8"))
    config = (MINIAPP / "config.js").read_text(encoding="utf-8")
    index_js = (MINIAPP / "pages/index/index.js").read_text(encoding="utf-8")

    assert project["compileType"] == "miniprogram"
    assert app["pages"] == ["pages/index/index"]
    assert 'apiBaseUrl: "https://imgclean-api.vercel.app"' in config
    assert "/api/auth/wechat-miniprogram/login" in index_js
    assert "/api/clean" in index_js

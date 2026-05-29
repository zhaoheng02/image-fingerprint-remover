import io
import struct
import zlib

from fastapi.testclient import TestClient
from PIL import Image

from imgclean_web.app import create_app


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


def test_clean_endpoint_returns_reports_and_download(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    client = TestClient(create_app())

    response = client.post(
        "/api/clean",
        data={"mode": "safe"},
        files=[("files", ("dirty.png", _make_dirty_png(), "image/png"))],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "safe"
    assert len(payload["results"]) == 1

    result = payload["results"][0]
    assert result["input"]["finding_count"] > 0
    assert result["output"]["is_clean"] is True
    assert result["download_url"].startswith("/download/")

    download = client.get(result["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "image/png"
    assert "attachment" in download.headers["content-disposition"]


def test_index_renders_selected_image_previews(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGCLEAN_WEB_DATA_DIR", str(tmp_path / "web-data"))
    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "renderSelectedPreviews" in html
    assert "URL.createObjectURL" in html
    assert "preview-grid" in html

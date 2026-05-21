"""End-to-end tests covering the AI-fingerprint detection + cleaning matrix.

Strategy: build small synthetic PNG/JPEG fixtures that carry each kind of
identifier, then assert
  (1) inspect() catches it with the expected category + severity, and
  (2) clean(..., mode='safe') removes every non-INFO finding while leaving
      the decoded pixel data bit-identical to the original.

We do not depend on real ChatGPT/Midjourney files for the test matrix; the
real ChatGPT sample is exercised separately in samples/.
"""
import hashlib
import io
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

from imgclean.clean import clean_bytes
from imgclean.detect import inspect
from imgclean.detect.png import PNG_HEADER


def _png_chunk(ctype: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body)) + ctype + body
        + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)
    )


def _make_png(extra_chunks_before_iend: list[tuple[bytes, bytes]] = (),
              trailing: bytes = b"") -> bytes:
    """Build a minimal valid 2x2 PNG with optional extra chunks + trailing bytes."""
    im = Image.new("RGB", (2, 2), color=(123, 222, 17))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    base = buf.getvalue()
    # Split base PNG before the IEND chunk so we can insert extras.
    iend_off = base.rfind(b"IEND")
    # Chunks start at length(4) before the type, so back off 4 bytes.
    insert_at = iend_off - 4
    out = base[:insert_at]
    for ctype, body in extra_chunks_before_iend:
        out += _png_chunk(ctype, body)
    out += base[insert_at:]
    out += trailing
    return out


def _pixel_sha(data: bytes) -> str:
    im = Image.open(io.BytesIO(data))
    im.load()
    return hashlib.sha256(im.tobytes()).hexdigest()


def _inspect_bytes(data: bytes, tmp_path: Path, name="t.png"):
    p = tmp_path / name
    p.write_bytes(data)
    return inspect(p)


# ---------- PNG: Stable Diffusion A1111 ----------

A1111_VALUE = (
    "masterpiece, best quality, 1girl\n"
    "Negative prompt: lowres, bad anatomy\n"
    "Steps: 28, Sampler: DPM++ 2M Karras, CFG scale: 7.0, Seed: 1234567890, "
    "Size: 512x768, Model hash: 6ce0161689, Model: v1-5-pruned-emaonly"
)


def test_detect_a1111_parameters(tmp_path):
    data = _make_png([(b"tEXt", b"parameters\x00" + A1111_VALUE.encode("latin1"))])
    r = _inspect_bytes(data, tmp_path)
    cats = {f.category.value for f in r.findings}
    assert "ai_generation_prompt" in cats, r.to_dict()
    assert any("Stable Diffusion" in f.name or "A1111" in f.name for f in r.findings)


def test_clean_safe_removes_a1111_and_preserves_pixels(tmp_path):
    data = _make_png([(b"tEXt", b"parameters\x00" + A1111_VALUE.encode("latin1"))])
    before_pixels = _pixel_sha(data)
    cleaned, _ = clean_bytes(data, mode="safe")
    after = _inspect_bytes(cleaned, tmp_path, name="cleaned.png")
    assert after.is_clean, after.to_dict()
    assert _pixel_sha(cleaned) == before_pixels


# ---------- PNG: ComfyUI workflow ----------

COMFYUI_JSON = b'{"prompt":{"1":{"class_type":"KSampler","inputs":{"seed":7}}}}'


def test_detect_comfyui_workflow(tmp_path):
    data = _make_png([
        (b"tEXt", b"prompt\x00" + COMFYUI_JSON),
        (b"tEXt", b"workflow\x00" + b'{"nodes":[]}'),
    ])
    r = _inspect_bytes(data, tmp_path)
    names = [f.name for f in r.findings]
    assert any("ComfyUI" in n for n in names), r.to_dict()


# ---------- PNG: zTXt with compressed AI marker ----------

def test_detect_ztxt_compressed(tmp_path):
    payload = b"openai-dalle3-image-generation-marker"
    compressed = b"\x00" + zlib.compress(payload)  # comp method + zlib data
    data = _make_png([(b"zTXt", b"Description\x00" + compressed)])
    r = _inspect_bytes(data, tmp_path)
    cats = {f.category.value for f in r.findings}
    assert "ai_generation_prompt" in cats or "png_text" in cats


# ---------- PNG: C2PA / JUMBF ----------

def test_detect_c2pa_cabx(tmp_path):
    jumbf = (
        b"\x00\x00\x00\x20jumbc2pa" + b"\x00" * 8
        + b"\x00\x00\x00\x10c2ma" + b"\x00" * 8
        + b"urn:c2pa:test-uuid-123"
    )
    data = _make_png([(b"caBX", b"\x00\x00\x00\x20" + jumbf)])
    r = _inspect_bytes(data, tmp_path)
    cats = {f.category.value for f in r.findings}
    assert "c2pa_manifest" in cats, r.to_dict()


# ---------- PNG: trailing bytes ----------

def test_detect_trailing_bytes(tmp_path):
    data = _make_png([], trailing=b"SECRET_PAYLOAD_HERE")
    r = _inspect_bytes(data, tmp_path)
    cats = {f.category.value for f in r.findings}
    assert "trailing_bytes" in cats


def test_clean_removes_trailing_bytes(tmp_path):
    data = _make_png([], trailing=b"SECRET_PAYLOAD_HERE")
    cleaned, _ = clean_bytes(data, mode="safe")
    r = _inspect_bytes(cleaned, tmp_path, "cleaned.png")
    cats = {f.category.value for f in r.findings}
    assert "trailing_bytes" not in cats


# ---------- PNG: unknown chunk ----------

def test_detect_unknown_chunk(tmp_path):
    data = _make_png([(b"vNdR", b"vendor-private-payload-with-id-99")])
    r = _inspect_bytes(data, tmp_path)
    cats = {f.category.value for f in r.findings}
    assert "png_chunk_unknown" in cats


# ---------- JPEG: EXIF + GPS + serial ----------

def _make_jpeg_with_exif() -> bytes:
    """Build a small JPEG and inject an EXIF APP1 block with Make/Model/GPS/Serial."""
    im = Image.new("RGB", (8, 8), color=(50, 60, 70))
    buf = io.BytesIO()
    # Use Pillow's exif support to attach simple tags.
    exif = Image.Exif()
    exif[0x010F] = "TestMake"
    exif[0x0110] = "TestModel"
    exif[0xA431] = "SN-12345"  # SerialNumber
    exif[0x8825] = {  # GPS IFD pointer with minimal GPS tag
        0x0001: "N",
        0x0002: (51, 30, 0.0),
        0x0003: "E",
        0x0004: (0, 7, 0.0),
    }
    im.save(buf, format="JPEG", quality=85, exif=exif.tobytes())
    return buf.getvalue()


def test_detect_jpeg_exif_gps_serial(tmp_path):
    data = _make_jpeg_with_exif()
    r = _inspect_bytes(data, tmp_path, "t.jpg")
    cats = {f.category.value for f in r.findings}
    assert "exif" in cats, r.to_dict()
    # GPS detection: depends on our minimal IFD parser seeing the GPS pointer.
    # Accept either explicit gps_location finding or GPS-present flag inside the EXIF finding.
    has_gps = "gps_location" in cats or any(
        f.extra.get("gps") for f in r.findings if f.category.value == "exif"
    )
    assert has_gps


def test_clean_safe_removes_jpeg_exif(tmp_path):
    data = _make_jpeg_with_exif()
    cleaned, fmt = clean_bytes(data, mode="safe")
    assert fmt == "jpeg"
    r = _inspect_bytes(cleaned, tmp_path, "cleaned.jpg")
    assert r.is_clean, r.to_dict()


# ---------- JPEG: XMP with DigitalSourceType=trainedAlgorithmicMedia ----------

def _make_jpeg_with_xmp(xmp_xml: str) -> bytes:
    im = Image.new("RGB", (4, 4), color=(10, 20, 30))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    jpeg = buf.getvalue()
    xmp_header = b"http://ns.adobe.com/xap/1.0/\x00"
    xmp_body = xmp_header + xmp_xml.encode("utf-8")
    # APP1 marker
    seg = b"\xff\xe1" + struct.pack(">H", len(xmp_body) + 2) + xmp_body
    # Insert after SOI (\xff\xd8)
    return jpeg[:2] + seg + jpeg[2:]


def test_detect_xmp_digital_source_type(tmp_path):
    xmp = """<?xpacket?><rdf:RDF xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/">
    <Iptc4xmpExt:DigitalSourceType>http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia</Iptc4xmpExt:DigitalSourceType>
    </rdf:RDF>"""
    data = _make_jpeg_with_xmp(xmp)
    r = _inspect_bytes(data, tmp_path, "t.jpg")
    flagged = [f for f in r.findings if "AI" in f.name or "trainedAlgorithmic" in (f.detail or "")]
    assert flagged, r.to_dict()


# ---------- regression: a totally clean PNG must be reported clean ----------

def test_clean_png_reports_clean(tmp_path):
    data = _make_png([])
    r = _inspect_bytes(data, tmp_path)
    assert r.is_clean, r.to_dict()

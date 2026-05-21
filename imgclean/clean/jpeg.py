"""JPEG cleaner — strip APPn/COM segments, optionally re-encode pixels."""
import io
import struct

from PIL import Image
import numpy as np

from ..detect.jpeg import iter_segments, SOI, EOI


# Keep only APP0 (JFIF basic) of the APPn range; drop APP1..APP15 and COM.
_KEEP_APP = {0xE0}


def _strip_segments(data: bytes) -> bytes:
    """Rewrite JPEG keeping all SOFn / DQT / DHT / SOS segments and the
    entropy-coded scan, but dropping every APP1..APP15 and COM segment."""
    out = io.BytesIO()
    out.write(SOI)
    sos_offset = None
    for off, marker, body in iter_segments(data):
        if marker == 0xDA:  # SOS — write the SOS header, then copy entropy data
            sos_offset = off
            out.write(bytes([0xFF, marker]))
            out.write(struct.pack(">H", len(body) + 2))
            out.write(body)
            break
        if marker == 0xFE:  # COM
            continue
        if 0xE1 <= marker <= 0xEF:  # APP1..APP15
            continue
        if 0xE0 <= marker <= 0xEF and marker not in _KEEP_APP:
            continue
        out.write(bytes([0xFF, marker]))
        if body:
            out.write(struct.pack(">H", len(body) + 2))
            out.write(body)

    if sos_offset is None:
        # No SOS found — return original to avoid producing a broken file.
        return data

    # Copy the entropy-coded scan up to the final EOI.
    # Find the SOS body end in the original, then copy through to last EOI.
    scan_start = None
    n = len(data)
    # Re-walk to locate the byte right after the SOS body in the original.
    for off, marker, body in iter_segments(data):
        if marker == 0xDA:
            scan_start = off + 2 + 2 + len(body)  # 0xFF DA + length(2) + body
            break
    if scan_start is None:
        return data

    eoi = data.rfind(EOI)
    if eoi == -1:
        return data
    out.write(data[scan_start:eoi])
    out.write(EOI)
    return out.getvalue()


def _reencode_pixels(data: bytes, quality: int, noise_sigma: float) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode != "RGB":
        im = im.convert("RGB")
    if noise_sigma > 0:
        arr = np.asarray(im, dtype=np.int16)
        rng = np.random.default_rng()
        arr = arr + rng.normal(0.0, noise_sigma, arr.shape).round().astype(np.int16)
        arr = np.clip(arr, 0, 255).astype(np.uint8)
        im = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, subsampling=2, optimize=True)
    return _strip_segments(buf.getvalue())


def _nuclear_pixels(data: bytes) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    new_w = max(1, int(round(w * 0.997)))
    new_h = max(1, int(round(h * 0.997)))
    im = im.resize((new_w, new_h), Image.LANCZOS)
    if new_w > 4 and new_h > 4:
        im = im.crop((2, 2, new_w - 2, new_h - 2))
    arr = np.asarray(im, dtype=np.int16)
    rng = np.random.default_rng()
    arr = arr + rng.normal(0.0, 0.7, arr.shape).round().astype(np.int16)
    arr = arr + rng.integers(-1, 2, size=(arr.shape[-1],)).astype(np.int16)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    final = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    final.save(buf, format="JPEG", quality=88, subsampling=2, optimize=True)
    return _strip_segments(buf.getvalue())


def clean(data: bytes, mode: str) -> bytes:
    if mode == "safe":
        return _strip_segments(data)
    if mode == "paranoid":
        return _reencode_pixels(data, quality=92, noise_sigma=0.5)
    if mode == "nuclear":
        return _nuclear_pixels(data)
    raise ValueError(mode)

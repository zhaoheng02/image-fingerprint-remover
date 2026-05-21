"""PNG cleaner — strip identifying chunks, optionally re-encode pixels."""
import io
import struct
import zlib

from PIL import Image
import numpy as np

from ..detect.png import PNG_HEADER, iter_chunks
from ..fingerprints import PNG_CRITICAL_CHUNKS, PNG_SAFE_ANCILLARY


def _crc(chunk_type: bytes, body: bytes) -> bytes:
    return struct.pack(">I", zlib.crc32(chunk_type + body) & 0xFFFFFFFF)


def _encode_chunk(chunk_type: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + chunk_type + body + _crc(chunk_type, body)


def _strip_chunks(data: bytes) -> bytes:
    """Safe mode: rewrite PNG keeping only IHDR/PLTE/IDAT/IEND and a small
    whitelist of rendering-relevant ancillary chunks.  Pixel data (IDAT) is
    copied byte-for-byte.  Trailing bytes after IEND are dropped."""
    out = io.BytesIO()
    out.write(PNG_HEADER)
    saw_iend = False
    for _off, ctype, body in iter_chunks(data):
        if ctype in PNG_CRITICAL_CHUNKS or ctype in PNG_SAFE_ANCILLARY:
            out.write(_encode_chunk(ctype, body))
            if ctype == b"IEND":
                saw_iend = True
        # else: drop (tEXt, zTXt, iTXt, eXIf, iCCP, tIME, pHYs, caBX, unknown)
    if not saw_iend:
        out.write(_encode_chunk(b"IEND", b""))
    return out.getvalue()


def _reencode_pixels(data: bytes, noise_sigma: float = 0.0) -> bytes:
    """Decode → optional noise → re-encode as a fresh PNG with a stock sRGB
    color space.  Used by paranoid/nuclear modes."""
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode not in ("RGB", "RGBA", "L", "LA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    arr = np.asarray(im, dtype=np.int16)
    if noise_sigma > 0:
        rng = np.random.default_rng()
        noise = rng.normal(0.0, noise_sigma, arr.shape)
        arr = np.clip(arr + noise.round().astype(np.int16), 0, 255)
    arr = arr.astype(np.uint8)
    out_im = Image.fromarray(arr, mode=im.mode)
    buf = io.BytesIO()
    out_im.save(buf, format="PNG", optimize=True)  # Pillow writes only IHDR/IDAT/IEND
    return _strip_chunks(buf.getvalue())  # belt-and-braces


def _nuclear_pixels(data: bytes) -> bytes:
    """Aggressive: re-encode pixels through a JPEG round-trip, then a slight
    resize + 2px crop + color shift.  Designed to disrupt frequency-domain
    watermarks at the cost of visible-but-mild quality loss."""
    im = Image.open(io.BytesIO(data))
    im.load()
    # alpha-channel images can't go through JPEG; keep them in PNG-only path
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if not has_alpha:
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=92, subsampling=2, optimize=True)
        im = Image.open(buf)
        im.load()

    w, h = im.size
    new_w = max(1, int(round(w * 0.997)))
    new_h = max(1, int(round(h * 0.997)))
    im = im.resize((new_w, new_h), Image.LANCZOS)
    # crop 2px each side
    if new_w > 4 and new_h > 4:
        im = im.crop((2, 2, new_w - 2, new_h - 2))

    arr = np.asarray(im, dtype=np.int16)
    rng = np.random.default_rng()
    # subtle gaussian noise + tiny per-channel bias to perturb DCT coefficients
    arr = arr + rng.normal(0.0, 0.7, arr.shape).round().astype(np.int16)
    if arr.shape[-1] >= 3:
        bias = rng.integers(-1, 2, size=arr.shape[-1]).astype(np.int16)
        arr = arr + bias
    arr = np.clip(arr, 0, 255).astype(np.uint8)

    final = Image.fromarray(arr, mode=im.mode)
    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    return _strip_chunks(buf.getvalue())


def clean(data: bytes, mode: str) -> bytes:
    if mode == "safe":
        return _strip_chunks(data)
    if mode == "paranoid":
        return _reencode_pixels(data, noise_sigma=0.5)
    if mode == "nuclear":
        return _nuclear_pixels(data)
    raise ValueError(mode)

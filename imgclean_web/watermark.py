"""Mask-based visible watermark suppression."""
from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from imgclean.clean import clean_bytes


@dataclass(frozen=True)
class WatermarkRequest:
    mask_bytes: bytes | None = None
    box: tuple[int, int, int, int] | None = None


def remove_watermark_bytes(data: bytes, fmt: str, request: WatermarkRequest) -> tuple[bytes, str]:
    image = Image.open(io.BytesIO(data))
    image.load()
    rgb = image.convert("RGB")
    mask = _build_mask(rgb.size, request)
    if not mask.getbbox():
        raise ValueError("watermark mode requires a mask image or box x,y,w,h.")

    current = np.asarray(rgb, dtype=np.uint8)
    original = current.copy()
    mask_image = mask.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(1.2))
    mask_arr = np.asarray(mask_image, dtype=np.float32) / 255.0
    hard_mask = mask_arr > 0.03

    for radius in (2, 3, 5, 8, 13, 21):
        blurred = np.asarray(Image.fromarray(current).filter(ImageFilter.GaussianBlur(radius)), dtype=np.uint8)
        current = np.where(hard_mask[..., None], blurred, original)

    alpha = np.clip(mask_arr[..., None], 0.0, 1.0)
    blended = (original.astype(np.float32) * (1.0 - alpha) + current.astype(np.float32) * alpha).round()
    output = Image.fromarray(np.clip(blended, 0, 255).astype(np.uint8), mode="RGB")

    buffer = io.BytesIO()
    if fmt == "jpeg":
        output.save(buffer, format="JPEG", quality=92, subsampling=2, optimize=True)
    else:
        output.save(buffer, format="PNG", optimize=True)
    return clean_bytes(buffer.getvalue(), mode="safe")


def parse_watermark_box(value: str) -> tuple[int, int, int, int] | None:
    if not value.strip():
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("watermark_box must be x,y,w,h.")
    x, y, width, height = [int(float(part)) for part in parts]
    if width <= 0 or height <= 0:
        raise ValueError("watermark_box width and height must be positive.")
    return x, y, width, height


def _build_mask(size: tuple[int, int], request: WatermarkRequest) -> Image.Image:
    mask = Image.new("L", size, 0)
    if request.mask_bytes:
        mask_image = Image.open(io.BytesIO(request.mask_bytes)).convert("L").resize(size, Image.Resampling.LANCZOS)
        mask = Image.eval(mask_image, lambda pixel: 255 if pixel > 64 else 0)
    if request.box:
        x, y, width, height = request.box
        max_x, max_y = size
        left = max(0, min(max_x, x))
        top = max(0, min(max_y, y))
        right = max(0, min(max_x, x + width))
        bottom = max(0, min(max_y, y + height))
        if right > left and bottom > top:
            ImageDraw.Draw(mask).rectangle((left, top, right, bottom), fill=255)
    return mask

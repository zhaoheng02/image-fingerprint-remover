"""Visible watermark suppression with manual or automatic masks."""
from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from imgclean.clean import clean_bytes

try:  # OpenCV gives noticeably better fills, but keep the app usable without it.
    import cv2
except ImportError:  # pragma: no cover - exercised by environments without cv2
    cv2 = None


@dataclass(frozen=True)
class WatermarkRequest:
    mask_bytes: bytes | None = None
    box: tuple[int, int, int, int] | None = None
    auto: bool = True


def remove_watermark_bytes(data: bytes, fmt: str, request: WatermarkRequest) -> tuple[bytes, str]:
    image = Image.open(io.BytesIO(data))
    image.load()
    rgb = image.convert("RGB")
    mask = _build_mask(rgb, request)
    if not mask.getbbox():
        raise ValueError("Could not detect a likely watermark. Provide watermark_box x,y,w,h or a mask image.")

    original = np.asarray(rgb, dtype=np.uint8)
    mask_image = mask.filter(ImageFilter.MaxFilter(9))
    output = _inpaint(original, np.asarray(mask_image, dtype=np.uint8))
    mask_image = mask_image.filter(ImageFilter.GaussianBlur(1.2))
    mask_arr = np.asarray(mask_image, dtype=np.float32) / 255.0

    alpha = np.clip(mask_arr[..., None], 0.0, 1.0)
    blended = (original.astype(np.float32) * (1.0 - alpha) + output.astype(np.float32) * alpha).round()
    output_image = Image.fromarray(np.clip(blended, 0, 255).astype(np.uint8), mode="RGB")

    buffer = io.BytesIO()
    if fmt == "jpeg":
        output_image.save(buffer, format="JPEG", quality=92, subsampling=2, optimize=True)
    else:
        output_image.save(buffer, format="PNG", optimize=True)
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


def _build_mask(rgb: Image.Image, request: WatermarkRequest) -> Image.Image:
    mask = Image.new("L", rgb.size, 0)
    if request.mask_bytes:
        mask_image = Image.open(io.BytesIO(request.mask_bytes)).convert("L").resize(rgb.size, Image.Resampling.LANCZOS)
        mask = Image.eval(mask_image, lambda pixel: 255 if pixel > 64 else 0)
    if request.box:
        x, y, width, height = request.box
        max_x, max_y = rgb.size
        left = max(0, min(max_x, x))
        top = max(0, min(max_y, y))
        right = max(0, min(max_x, x + width))
        bottom = max(0, min(max_y, y + height))
        if right > left and bottom > top:
            ImageDraw.Draw(mask).rectangle((left, top, right, bottom), fill=255)
    if request.auto and not mask.getbbox():
        mask = _auto_detect_text_mask(rgb)
    return mask


def _auto_detect_text_mask(rgb: Image.Image) -> Image.Image:
    arr = np.asarray(rgb, dtype=np.uint8)
    gray = np.asarray(rgb.convert("L"), dtype=np.float32)
    local = np.asarray(rgb.convert("L").filter(ImageFilter.GaussianBlur(4.0)), dtype=np.float32)
    contrast = np.abs(gray - local)

    color_range = arr.max(axis=2).astype(np.int16) - arr.min(axis=2).astype(np.int16)
    bright_text = gray > max(170.0, float(np.percentile(gray, 86)))
    dark_text = gray < min(85.0, float(np.percentile(gray, 14)))
    high_contrast = contrast > max(14.0, float(np.percentile(contrast, 86)))
    low_saturation = color_range < 54
    candidate = (bright_text | dark_text) & high_contrast & low_saturation

    height, width = gray.shape
    y = np.arange(height)[:, None]
    x = np.arange(width)[None, :]
    common_watermark_zone = (
        (y >= int(height * 0.56))
        | (y <= int(height * 0.20))
        | (x <= int(width * 0.22))
        | (x >= int(width * 0.78))
    )
    zone_candidate = candidate & common_watermark_zone
    if zone_candidate.sum() >= max(8, int(width * height * 0.00025)):
        candidate = zone_candidate

    mask = Image.fromarray((candidate.astype(np.uint8) * 255), mode="L")
    mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(3))
    mask = _drop_tiny_mask(mask, min_pixels=max(8, int(width * height * 0.00018)))
    return mask.filter(ImageFilter.MaxFilter(7))


def _drop_tiny_mask(mask: Image.Image, min_pixels: int) -> Image.Image:
    arr = np.asarray(mask, dtype=np.uint8) > 0
    if int(arr.sum()) < min_pixels:
        return Image.new("L", mask.size, 0)
    return Image.fromarray((arr.astype(np.uint8) * 255), mode="L")


def _inpaint(original: np.ndarray, mask: np.ndarray) -> np.ndarray:
    hard_mask = mask > 8
    if cv2 is not None and hard_mask.any():
        return cv2.inpaint(original, hard_mask.astype(np.uint8) * 255, 3, cv2.INPAINT_TELEA)

    current = original.copy()
    for radius in (2, 3, 5, 8, 13, 21):
        blurred = np.asarray(Image.fromarray(current).filter(ImageFilter.GaussianBlur(radius)), dtype=np.uint8)
        current = np.where(hard_mask[..., None], blurred, original)
    return current

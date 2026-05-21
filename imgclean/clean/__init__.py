from pathlib import Path

from ..detect import detect_format
from . import png as png_clean
from . import jpeg as jpeg_clean


VALID_MODES = ("safe", "paranoid", "nuclear")


def clean_bytes(data: bytes, mode: str = "safe") -> tuple[bytes, str]:
    """Return (cleaned_bytes, format_out).  format_out may differ from input
    only when nuclear mode transcodes through JPEG."""
    if mode not in VALID_MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {VALID_MODES}")
    fmt = detect_format(data)
    if fmt == "png":
        return png_clean.clean(data, mode), "png"
    if fmt == "jpeg":
        return jpeg_clean.clean(data, mode), "jpeg"
    raise ValueError(f"unsupported format {fmt!r} for cleaning in this build")


def clean_file(in_path: str | Path, out_path: str | Path, mode: str = "safe") -> None:
    data = Path(in_path).read_bytes()
    cleaned, _ = clean_bytes(data, mode=mode)
    Path(out_path).write_bytes(cleaned)

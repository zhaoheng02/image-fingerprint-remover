from pathlib import Path

from ..findings import InspectReport
from . import png as png_detect
from . import jpeg as jpeg_detect


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def detect_format(data: bytes) -> str:
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(JPEG_MAGIC):
        return "jpeg"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (
        b"heic", b"heix", b"mif1", b"msf1", b"avif", b"avis"
    ):
        return "heic_or_avif"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    return "unknown"


def inspect(path: str | Path) -> InspectReport:
    p = Path(path)
    data = p.read_bytes()
    fmt = detect_format(data)
    report = InspectReport(path=str(p), format=fmt, file_size=len(data))
    if fmt == "png":
        png_detect.inspect(data, report)
    elif fmt == "jpeg":
        jpeg_detect.inspect(data, report)
    else:
        report.notes.append(
            f"format '{fmt}' detected — only PNG and JPEG are fully supported in this build."
        )
    return report

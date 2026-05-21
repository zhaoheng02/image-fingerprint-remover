from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MED = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Category(str, Enum):
    EXIF = "exif"
    XMP = "xmp"
    IPTC = "iptc"
    ICC = "icc"
    THUMBNAIL = "thumbnail"
    PNG_TEXT = "png_text"
    PNG_CHUNK = "png_chunk_unknown"
    JPEG_APP = "jpeg_app_segment"
    JPEG_COMMENT = "jpeg_comment"
    JPEG_QT = "jpeg_quantization_table"
    C2PA = "c2pa_manifest"
    AI_PROMPT = "ai_generation_prompt"
    AI_TAG = "ai_generation_tag"
    GPS = "gps_location"
    SERIAL = "device_serial"
    TRAILING = "trailing_bytes"
    FS_META = "filesystem_metadata"
    OTHER = "other"


@dataclass
class Finding:
    category: Category
    severity: Severity
    location: str  # e.g. "PNG chunk caBX @offset=33"
    name: str  # short identifier shown to user
    detail: str  # one-line description
    size_bytes: int = 0
    value_preview: str = ""  # truncated content sample
    source: str = ""  # which detector raised it
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["category"] = self.category.value
        d["severity"] = self.severity.value
        return d


@dataclass
class InspectReport:
    path: str
    format: str
    file_size: int
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        # File is "clean" if no MED/HIGH/CRITICAL findings remain.  INFO and LOW
        # are residual fingerprints that safe mode intentionally cannot remove
        # (e.g. JPEG quantization tables, standard ICC profile) — the user must
        # opt into paranoid/nuclear to neutralize those.
        return not any(
            f.severity in (Severity.MED, Severity.HIGH, Severity.CRITICAL)
            for f in self.findings
        )

    def by_category(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.category.value] = out.get(f.category.value, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "format": self.format,
            "file_size": self.file_size,
            "is_clean": self.is_clean,
            "finding_count": len(self.findings),
            "by_category": self.by_category(),
            "notes": self.notes,
            "findings": [f.to_dict() for f in self.findings],
        }

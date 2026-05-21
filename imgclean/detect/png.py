"""PNG chunk-level inspector."""
import struct
import zlib
from typing import Iterator

from ..findings import Category, Finding, InspectReport, Severity
from ..fingerprints import (
    C2PA_LABELS,
    JUMBF_MAGIC,
    PNG_AI_TEXT_KEYS,
    PNG_AI_VALUE_PATTERNS,
    PNG_CRITICAL_CHUNKS,
    PNG_SAFE_ANCILLARY,
    XMP_AI_PATTERNS,
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def iter_chunks(data: bytes) -> Iterator[tuple[int, bytes, bytes]]:
    """Yield (offset, type, data) for each PNG chunk. Tolerates trailing bytes."""
    if not data.startswith(PNG_HEADER):
        return
    off = len(PNG_HEADER)
    n = len(data)
    while off + 8 <= n:
        (length,) = struct.unpack(">I", data[off : off + 4])
        ctype = data[off + 4 : off + 8]
        body = data[off + 8 : off + 8 + length]
        yield off, ctype, body
        off += 12 + length  # length + type + body + crc
        if ctype == b"IEND":
            break


def _decode_text(body: bytes, chunk_type: bytes) -> tuple[str, str]:
    """Decode a tEXt/zTXt/iTXt chunk body into (key, value)."""
    try:
        if chunk_type == b"tEXt":
            k, _, v = body.partition(b"\x00")
            return k.decode("latin1", "replace"), v.decode("latin1", "replace")
        if chunk_type == b"zTXt":
            k, _, rest = body.partition(b"\x00")
            if not rest:
                return k.decode("latin1", "replace"), ""
            # rest[0] is compression method (0 = zlib)
            comp = rest[1:]
            try:
                v = zlib.decompress(comp).decode("utf-8", "replace")
            except zlib.error:
                v = f"<zlib decode error, {len(comp)} bytes>"
            return k.decode("latin1", "replace"), v
        if chunk_type == b"iTXt":
            # keyword\0 comp_flag(1) comp_method(1) lang\0 trans\0 text
            try:
                k, rest = body.split(b"\x00", 1)
                comp_flag = rest[0]
                # rest[1] = comp method
                lang, rest2 = rest[2:].split(b"\x00", 1)
                trans, text = rest2.split(b"\x00", 1)
                if comp_flag:
                    try:
                        text = zlib.decompress(text)
                    except zlib.error:
                        pass
                return k.decode("utf-8", "replace"), text.decode("utf-8", "replace")
            except (ValueError, IndexError):
                return "<malformed iTXt>", body[:200].decode("utf-8", "replace")
    except Exception as exc:  # pragma: no cover
        return f"<error: {exc}>", ""
    return "<unknown>", ""


def _match_ai_keys(key: str, value: str) -> tuple[str | None, str | None]:
    """Return (label, why) if the text chunk looks like an AI fingerprint."""
    kl = key.lower().strip()
    if kl in PNG_AI_TEXT_KEYS:
        label, why = PNG_AI_TEXT_KEYS[kl]
        return label, why
    for pat, why in PNG_AI_VALUE_PATTERNS:
        if pat.search(value):
            return "AI generation marker", why
    for pat, why in XMP_AI_PATTERNS:
        if pat.search(value):
            return "AI generation marker (XMP)", why
    return None, None


def inspect(data: bytes, report: InspectReport) -> None:
    if not data.startswith(PNG_HEADER):
        report.notes.append("PNG magic missing — not a PNG.")
        return

    last_end = len(PNG_HEADER)
    saw_iend = False
    for off, ctype, body in iter_chunks(data):
        last_end = off + 12 + len(body)
        ctype_s = ctype.decode("latin1", "replace")
        if ctype in PNG_CRITICAL_CHUNKS or ctype in PNG_SAFE_ANCILLARY:
            if ctype == b"IEND":
                saw_iend = True
            continue

        # Text chunks: decode and AI-match.
        if ctype in (b"tEXt", b"zTXt", b"iTXt"):
            key, value = _decode_text(body, ctype)
            label, why = _match_ai_keys(key, value)
            severity = Severity.HIGH if label else Severity.MED
            category = Category.AI_PROMPT if label else Category.PNG_TEXT
            preview = value[:240].replace("\n", " ")
            detail = (
                f"PNG {ctype_s} key={key!r} ({len(value)} chars)"
                + (f" — {why}" if why else "")
            )
            report.findings.append(
                Finding(
                    category=category,
                    severity=severity,
                    location=f"PNG {ctype_s} @offset={off}",
                    name=label or f"PNG text chunk: {key}",
                    detail=detail,
                    size_bytes=len(body),
                    value_preview=preview,
                    source="png.text",
                    extra={"key": key, "length": len(value)},
                )
            )
            continue

        # eXIf chunk = embedded EXIF
        if ctype == b"eXIf":
            report.findings.append(
                Finding(
                    category=Category.EXIF,
                    severity=Severity.HIGH,
                    location=f"PNG eXIf @offset={off}",
                    name="Embedded EXIF block in PNG",
                    detail="PNG carries a full EXIF block (may include camera Make/Model, GPS, serial).",
                    size_bytes=len(body),
                    source="png.exif",
                )
            )
            continue

        # iCCP — embedded ICC profile (can carry custom IDs).
        if ctype == b"iCCP":
            name, _, _ = body.partition(b"\x00")
            report.findings.append(
                Finding(
                    category=Category.ICC,
                    severity=Severity.LOW,
                    location=f"PNG iCCP @offset={off}",
                    name=f"ICC profile: {name.decode('latin1','replace')}",
                    detail="Embedded ICC color profile — may carry custom strings or UUIDs.",
                    size_bytes=len(body),
                    source="png.iccp",
                )
            )
            continue

        # tIME — last modification timestamp
        if ctype == b"tIME":
            if len(body) == 7:
                y, mo, d, h, mi, s = struct.unpack(">HBBBBB", body[:7])
                report.findings.append(
                    Finding(
                        category=Category.OTHER,
                        severity=Severity.LOW,
                        location=f"PNG tIME @offset={off}",
                        name="PNG last-modification timestamp",
                        detail=f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d} UTC",
                        size_bytes=len(body),
                        source="png.time",
                    )
                )
            continue

        # pHYs — pixel density (can fingerprint editor)
        if ctype == b"pHYs":
            report.findings.append(
                Finding(
                    category=Category.OTHER,
                    severity=Severity.INFO,
                    location=f"PNG pHYs @offset={off}",
                    name="Pixel-density chunk",
                    detail="Editor-set DPI/aspect — mild fingerprint.",
                    size_bytes=len(body),
                    source="png.phys",
                )
            )
            continue

        # caBX — Adobe-registered C2PA JUMBF container.
        # Also handle any unknown chunk whose body contains the JUMBF magic.
        if ctype == b"caBX" or (JUMBF_MAGIC in body[:64] and any(lbl in body[:200] for lbl in C2PA_LABELS)):
            report.findings.append(
                Finding(
                    category=Category.C2PA,
                    severity=Severity.CRITICAL,
                    location=f"PNG {ctype_s} @offset={off}",
                    name="C2PA / Content Credentials manifest",
                    detail=(
                        "JUMBF box containing C2PA manifest — encodes signing entity (camera, OpenAI, Adobe, Google), "
                        "assertions, edit history, and a content-binding hash."
                    ),
                    size_bytes=len(body),
                    value_preview=body[:200].decode("latin1", "replace"),
                    source="png.c2pa",
                    extra={"chunk": ctype_s},
                )
            )
            continue

        # Unknown ancillary chunk.
        report.findings.append(
            Finding(
                category=Category.PNG_CHUNK,
                severity=Severity.MED,
                location=f"PNG {ctype_s} @offset={off}",
                name=f"Non-standard PNG chunk: {ctype_s}",
                detail="Unknown ancillary chunk — may carry vendor identifiers.",
                size_bytes=len(body),
                value_preview=body[:200].decode("latin1", "replace"),
                source="png.unknown",
            )
        )

    if saw_iend and last_end < len(data):
        trailing = len(data) - last_end
        report.findings.append(
            Finding(
                category=Category.TRAILING,
                severity=Severity.HIGH,
                location=f"PNG trailing bytes @offset={last_end}",
                name="Trailing bytes after IEND",
                detail=f"{trailing} extra bytes after PNG end-of-file marker — possible appended payload.",
                size_bytes=trailing,
                value_preview=data[last_end : last_end + 200].decode("latin1", "replace"),
                source="png.trailing",
            )
        )

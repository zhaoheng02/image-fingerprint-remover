"""JPEG marker-level inspector."""
import hashlib
import struct
from typing import Iterator

from ..findings import Category, Finding, InspectReport, Severity
from ..fingerprints import (
    ADOBE_APP14,
    C2PA_LABELS,
    EXIF_LEAK_TAGS,
    GPS_TAGS_PREFIX,
    JUMBF_MAGIC,
    PHOTOSHOP_8BIM,
    PHOTOSHOP_HEADER,
    XMP_AI_PATTERNS,
)

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
SOS = 0xDA


def iter_segments(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    """Yield (offset, marker_code, segment_body) for each JPEG marker segment.

    Stops at SOS (start of scan) since the scan payload is the entropy-coded
    image data and is not a metadata segment.
    """
    if not data.startswith(SOI):
        return
    off = 2
    n = len(data)
    while off < n:
        if data[off] != 0xFF:
            return
        # Skip fill bytes 0xFF 0xFF...
        while off < n and data[off] == 0xFF:
            off += 1
        if off >= n:
            return
        marker = data[off]
        off += 1
        # Standalone markers (no length).
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0x01, 0xD8, 0xD9):
            yield off - 2, marker, b""
            if marker == 0xD9:  # EOI
                return
            continue
        if off + 2 > n:
            return
        (length,) = struct.unpack(">H", data[off : off + 2])
        body = data[off + 2 : off + length]
        yield off - 2, marker, body
        off += length
        if marker == SOS:
            # Skip the entropy-coded segment until we find the next marker
            # (or EOI).  We do not need to inspect it here.
            while off < n - 1:
                if data[off] == 0xFF and data[off + 1] != 0x00 and data[off + 1] not in (
                    0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7
                ):
                    break
                off += 1


def _check_xmp(value: str) -> tuple[str | None, str | None]:
    for pat, why in XMP_AI_PATTERNS:
        if pat.search(value):
            return "AI generation marker (XMP)", why
    return None, None


def _parse_exif(body: bytes) -> tuple[list[tuple[str, str]], bool, bool]:
    """Return (notable tag list, has_gps, has_thumbnail). Best-effort minimal parser."""
    # body starts with "Exif\0\0" then TIFF header
    if not body.startswith(b"Exif\x00\x00"):
        return [], False, False
    tiff = body[6:]
    if len(tiff) < 8:
        return [], False, False
    endian = tiff[:2]
    if endian == b"II":
        fmt = "<"
    elif endian == b"MM":
        fmt = ">"
    else:
        return [], False, False
    magic = struct.unpack(fmt + "H", tiff[2:4])[0]
    if magic != 0x002A:
        return [], False, False
    ifd0_off = struct.unpack(fmt + "I", tiff[4:8])[0]

    # Tag id -> name (subset)
    TAG_NAMES = {
        0x010F: "Make", 0x0110: "Model", 0x0131: "Software",
        0x013B: "Artist", 0x8298: "Copyright", 0x9286: "UserComment",
        0xA420: "ImageUniqueID", 0xC62F: "BodySerialNumber",
        0xA431: "SerialNumber", 0xA432: "LensSerialNumber",
        0x9003: "DateTimeOriginal", 0x9004: "DateTimeDigitized",
        0x010E: "ImageDescription", 0x013C: "HostComputer",
        0x8825: "GPSIFD", 0x8769: "ExifIFD",
    }

    tags: list[tuple[str, str]] = []
    has_gps = False
    has_thumb = False

    def read_ifd(ifd_off: int, depth: int = 0) -> int:
        nonlocal has_gps, has_thumb
        if ifd_off + 2 > len(tiff) or depth > 3:
            return 0
        (count,) = struct.unpack(fmt + "H", tiff[ifd_off : ifd_off + 2])
        entries_off = ifd_off + 2
        for i in range(count):
            e = entries_off + i * 12
            if e + 12 > len(tiff):
                return 0
            (tag, typ, n, val) = struct.unpack(fmt + "HHII", tiff[e : e + 12])
            name = TAG_NAMES.get(tag)
            if tag == 0x8825:
                has_gps = True
            if name and name not in ("GPSIFD", "ExifIFD"):
                # Try ascii read
                tv = ""
                if typ == 2:  # ASCII
                    if n <= 4:
                        tv = struct.pack(fmt + "I", val).rstrip(b"\x00").decode("utf-8", "replace")
                    elif val + n <= len(tiff):
                        tv = tiff[val : val + n].rstrip(b"\x00").decode("utf-8", "replace")
                tags.append((name, tv))
            if tag == 0x8769 and val < len(tiff):
                read_ifd(val, depth + 1)
        # next IFD pointer for IFD1 (thumbnail)
        next_off_pos = entries_off + count * 12
        if next_off_pos + 4 <= len(tiff):
            (next_off,) = struct.unpack(fmt + "I", tiff[next_off_pos : next_off_pos + 4])
            if depth == 0 and next_off:
                has_thumb = True
                read_ifd(next_off, depth + 1)
            return next_off
        return 0

    try:
        read_ifd(ifd0_off)
    except Exception:
        pass
    return tags, has_gps, has_thumb


def inspect(data: bytes, report: InspectReport) -> None:
    if not data.startswith(SOI):
        report.notes.append("JPEG SOI marker missing.")
        return

    last_seg_end = 0
    seen_sos = False
    for off, marker, body in iter_segments(data):
        last_seg_end = off + 2 + (len(body) + 2 if body else 0)
        if marker == SOS:
            seen_sos = True
            break

        if marker == 0xFE:  # COM
            txt = body.decode("utf-8", "replace")
            report.findings.append(
                Finding(
                    category=Category.JPEG_COMMENT,
                    severity=Severity.MED,
                    location=f"JPEG COM @offset={off}",
                    name="JPEG free-form comment",
                    detail=f"{len(body)} bytes",
                    size_bytes=len(body),
                    value_preview=txt[:240],
                    source="jpeg.com",
                )
            )
            continue

        if marker == 0xDB:  # DQT
            h = hashlib.sha1(body).hexdigest()[:12]
            report.findings.append(
                Finding(
                    category=Category.JPEG_QT,
                    severity=Severity.LOW,
                    location=f"JPEG DQT @offset={off}",
                    name=f"Quantization table (hash {h})",
                    detail="Camera / encoder fingerprint — survives EXIF strip.",
                    size_bytes=len(body),
                    source="jpeg.dqt",
                    extra={"sha1_12": h},
                )
            )
            continue

        # APP segments
        if 0xE0 <= marker <= 0xEF:
            seg_name = f"APP{marker - 0xE0}"

            # APP1 = EXIF or XMP
            if marker == 0xE1:
                if body.startswith(b"Exif\x00\x00"):
                    tags, has_gps, has_thumb = _parse_exif(body)
                    leaked = [(k, v) for k, v in tags if k in EXIF_LEAK_TAGS]
                    report.findings.append(
                        Finding(
                            category=Category.EXIF,
                            severity=Severity.HIGH if leaked or has_gps else Severity.MED,
                            location=f"JPEG {seg_name} EXIF @offset={off}",
                            name="EXIF metadata block",
                            detail=(
                                f"{len(tags)} known tags"
                                + (f"; GPS present" if has_gps else "")
                                + (f"; embedded thumbnail" if has_thumb else "")
                            ),
                            size_bytes=len(body),
                            value_preview="; ".join(f"{k}={v[:60]}" for k, v in leaked[:6]),
                            source="jpeg.exif",
                            extra={"tags": dict(tags), "gps": has_gps, "thumbnail": has_thumb},
                        )
                    )
                    if has_gps:
                        report.findings.append(
                            Finding(
                                category=Category.GPS,
                                severity=Severity.CRITICAL,
                                location=f"JPEG {seg_name} EXIF GPS IFD @offset={off}",
                                name="GPS location embedded",
                                detail="EXIF GPS IFD present — reveals capture location.",
                                source="jpeg.gps",
                            )
                        )
                    if has_thumb:
                        report.findings.append(
                            Finding(
                                category=Category.THUMBNAIL,
                                severity=Severity.MED,
                                location=f"JPEG {seg_name} EXIF IFD1 @offset={off}",
                                name="Embedded thumbnail",
                                detail="EXIF IFD1 thumbnail — may show original pre-edit pixels.",
                                source="jpeg.thumb",
                            )
                        )
                    for k, v in tags:
                        if k.startswith(GPS_TAGS_PREFIX):
                            continue
                        if k in ("Make", "Model") and v:
                            pass  # already captured above
                        if "Serial" in k and v:
                            report.findings.append(
                                Finding(
                                    category=Category.SERIAL,
                                    severity=Severity.CRITICAL,
                                    location=f"JPEG EXIF tag {k}",
                                    name=f"Device serial: {k}",
                                    detail=v,
                                    source="jpeg.serial",
                                )
                            )
                    continue
                if body.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                    xmp = body[29:].decode("utf-8", "replace")
                    label, why = _check_xmp(xmp)
                    sev = Severity.HIGH if label else Severity.MED
                    cat = Category.AI_TAG if label else Category.XMP
                    report.findings.append(
                        Finding(
                            category=cat,
                            severity=sev,
                            location=f"JPEG {seg_name} XMP @offset={off}",
                            name=label or "XMP packet",
                            detail=why or f"{len(xmp)} chars of XMP metadata",
                            size_bytes=len(body),
                            value_preview=xmp[:240],
                            source="jpeg.xmp",
                        )
                    )
                    continue
                if body.startswith(b"http://ns.adobe.com/xmp/extension/\x00"):
                    report.findings.append(
                        Finding(
                            category=Category.XMP,
                            severity=Severity.MED,
                            location=f"JPEG {seg_name} XMP-extended @offset={off}",
                            name="XMP extension packet",
                            detail=f"{len(body)} bytes (continuation of XMP)",
                            size_bytes=len(body),
                            source="jpeg.xmp_ext",
                        )
                    )
                    continue

            # APP2 = ICC, FPXR, MPF
            if marker == 0xE2:
                if body.startswith(b"ICC_PROFILE\x00"):
                    report.findings.append(
                        Finding(
                            category=Category.ICC,
                            severity=Severity.LOW,
                            location=f"JPEG {seg_name} ICC @offset={off}",
                            name="ICC color profile segment",
                            detail="Embedded ICC profile — may carry custom strings/UUIDs.",
                            size_bytes=len(body),
                            source="jpeg.icc",
                        )
                    )
                    continue
                if body.startswith(b"MPF\x00"):
                    report.findings.append(
                        Finding(
                            category=Category.THUMBNAIL,
                            severity=Severity.MED,
                            location=f"JPEG {seg_name} MPF @offset={off}",
                            name="Multi-picture (MPF) — embedded extra images",
                            detail="Often contains an additional preview/original frame.",
                            size_bytes=len(body),
                            source="jpeg.mpf",
                        )
                    )
                    continue

            # APP11 = JUMBF / C2PA
            if marker == 0xEB and JUMBF_MAGIC in body[:64]:
                report.findings.append(
                    Finding(
                        category=Category.C2PA,
                        severity=Severity.CRITICAL,
                        location=f"JPEG {seg_name} @offset={off}",
                        name="C2PA / Content Credentials manifest (JUMBF)",
                        detail="Encodes signing entity and edit history.",
                        size_bytes=len(body),
                        value_preview=body[:200].decode("latin1", "replace"),
                        source="jpeg.c2pa",
                    )
                )
                continue

            # APP13 = Photoshop IRB / IPTC
            if marker == 0xED and body.startswith(PHOTOSHOP_HEADER):
                report.findings.append(
                    Finding(
                        category=Category.IPTC,
                        severity=Severity.HIGH,
                        location=f"JPEG {seg_name} Photoshop IRB @offset={off}",
                        name="Photoshop 8BIM / IPTC block",
                        detail="Photoshop resource block — may include original filename, paths, URL, IPTC.",
                        size_bytes=len(body),
                        source="jpeg.psd",
                    )
                )
                continue

            # APP14 = Adobe
            if marker == 0xEE and body.startswith(ADOBE_APP14):
                report.findings.append(
                    Finding(
                        category=Category.JPEG_APP,
                        severity=Severity.LOW,
                        location=f"JPEG {seg_name} Adobe @offset={off}",
                        name="Adobe APP14 marker (DCT transform)",
                        detail="Identifies file as having passed through an Adobe encoder.",
                        size_bytes=len(body),
                        source="jpeg.adobe",
                    )
                )
                continue

            # APP0 with JFIF magic is the standard JPEG header — not a fingerprint.
            if marker == 0xE0 and body.startswith(b"JFIF\x00"):
                continue
            # APP0 with JFXX (thumbnail extension) — flag as MED.
            if marker == 0xE0 and body.startswith(b"JFXX\x00"):
                report.findings.append(
                    Finding(
                        category=Category.THUMBNAIL,
                        severity=Severity.MED,
                        location=f"JPEG APP0 JFXX @offset={off}",
                        name="JFIF extension thumbnail",
                        detail="JFXX block carries an embedded thumbnail.",
                        size_bytes=len(body),
                        source="jpeg.jfxx",
                    )
                )
                continue

            # Generic APPn we don't otherwise recognize.
            preview = body[:60].decode("latin1", "replace")
            report.findings.append(
                Finding(
                    category=Category.JPEG_APP,
                    severity=Severity.MED,
                    location=f"JPEG {seg_name} @offset={off}",
                    name=f"Unknown {seg_name} segment",
                    detail=f"{len(body)} bytes of vendor-specific data.",
                    size_bytes=len(body),
                    value_preview=preview,
                    source="jpeg.appn",
                )
            )

    # Trailing bytes after EOI
    eoi_idx = data.rfind(EOI)
    if eoi_idx != -1 and eoi_idx + 2 < len(data):
        trailing = len(data) - eoi_idx - 2
        report.findings.append(
            Finding(
                category=Category.TRAILING,
                severity=Severity.HIGH,
                location=f"JPEG trailing bytes @offset={eoi_idx + 2}",
                name="Trailing bytes after EOI",
                detail=f"{trailing} extra bytes after JPEG end-of-image marker.",
                size_bytes=trailing,
                value_preview=data[eoi_idx + 2 : eoi_idx + 2 + 200].decode("latin1", "replace"),
                source="jpeg.trailing",
            )
        )

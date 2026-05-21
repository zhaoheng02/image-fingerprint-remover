"""Generate a synthetic demo PNG carrying a fake C2PA-like JUMBF chunk,
plus an SD-A1111 'parameters' tEXt chunk and trailing bytes, so the README
screenshot has something interesting to display without using copyrighted
sample images.

Run:
    .venv/bin/python scripts/make_demo_image.py samples/demo_image.png
"""
import io
import struct
import sys
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _chunk(ctype: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body)) + ctype + body
        + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)
    )


def main(out: str = "samples/demo_image.png") -> int:
    # Render a gradient + caption image
    w, h = 1280, 720
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            r = int(40 + 180 * (x / w))
            g = int(60 + 120 * (y / h))
            b = int(180 - 90 * (x / w))
            px[x, y] = (r, g, b)
    draw = ImageDraw.Draw(im)
    title = "image-fingerprint-remover"
    sub = "synthetic demo  ·  this PNG carries a fake C2PA + SD prompt block"
    try:
        font_big = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 56)
        font_small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
    except OSError:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()
    draw.text((60, 280), title, fill=(255, 255, 255), font=font_big)
    draw.text((60, 360), sub, fill=(230, 230, 230), font=font_small)

    # Encode to PNG and inject extra chunks
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    base = buf.getvalue()
    iend_off = base.rfind(b"IEND") - 4  # back up to length field

    extras = b""
    # Fake C2PA JUMBF box inside a caBX chunk
    jumbf = (
        b"\x00\x00\x00\x20jumbc2pa\x00\x11\x00\x10\xaa\xaa\x00\x008\xee\x71\x03"
        b"\x00\x00\x00\x40jumbc2ma\x00\x11\x00\x10\xaa\xaa\x00\x008\xee\x71\x03"
        b"urn:c2pa:demo-fake-0000-1111-2222-aaaaaaaaaaaa"
        b"\x00\x00\x00\x18jumbc2as\x00\x11\x00\x10\xaa\xaa\x00\x008\xee\x71\x03"
        b"c2pa.assertions\x00"
    )
    extras += _chunk(b"caBX", b"\x00\x00\x00\x20" + jumbf)

    # Fake Stable Diffusion A1111 parameters block
    a1111 = (
        b"parameters\x00"
        b"a cat astronaut in space, masterpiece, best quality\n"
        b"Negative prompt: lowres, bad anatomy\n"
        b"Steps: 28, Sampler: DPM++ 2M Karras, CFG scale: 7.0, "
        b"Seed: 1234567890, Size: 1280x720, "
        b"Model hash: 6ce0161689, Model: v1-5-pruned-emaonly"
    )
    extras += _chunk(b"tEXt", a1111)

    # Software field that looks like it came from a generator
    extras += _chunk(b"tEXt", b"Software\x00DemoGen 2.0 (synthetic)")

    out_data = base[:iend_off] + extras + base[iend_off:]
    # Trailing bytes after IEND
    out_data += b"appended-payload-for-demo"

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out_data)
    print(f"wrote {out_path}  ({out_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "samples/demo_image.png"))

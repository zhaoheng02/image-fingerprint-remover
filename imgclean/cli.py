"""imgclean command-line entrypoint."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from . import __version__
from .clean import VALID_MODES, clean_bytes
from .detect import inspect
from .report import format_report, format_summary


def _print_report(report, as_json: bool, use_color: bool) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(format_report(report, use_color=use_color))


def _cmd_inspect(args: argparse.Namespace) -> int:
    report = inspect(args.path)
    _print_report(report, args.json, use_color=not args.no_color and sys.stdout.isatty())
    return 0 if report.is_clean else 1


def _cmd_clean(args: argparse.Namespace) -> int:
    in_path = Path(args.path)
    if not in_path.exists():
        print(f"error: {in_path} does not exist", file=sys.stderr)
        return 2

    data = in_path.read_bytes()
    if args.in_place:
        out_path = in_path
    elif args.output:
        out_path = Path(args.output)
    else:
        stem = in_path.stem
        out_path = in_path.with_name(f"{stem}.cleaned{in_path.suffix}")

    try:
        cleaned, fmt_out = clean_bytes(data, mode=args.mode)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_path.write_bytes(cleaned)

    # Normalize filesystem timestamps for paranoid/nuclear modes.
    if args.mode in ("paranoid", "nuclear"):
        try:
            ts = 946684800.0  # 2000-01-01 UTC, intentionally not "now"
            os.utime(out_path, (ts, ts))
        except OSError:
            pass

    pre = inspect(str(in_path))
    post = inspect(str(out_path))
    summary = {
        "mode": args.mode,
        "input": str(in_path),
        "output": str(out_path),
        "input_size": len(data),
        "output_size": len(cleaned),
        "input_findings": len(pre.findings),
        "output_findings": len(post.findings),
        "input_critical": sum(1 for f in pre.findings if f.severity.value == "critical"),
        "output_critical": sum(1 for f in post.findings if f.severity.value == "critical"),
        "post_clean": post.is_clean,
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        delta = len(data) - len(cleaned)
        sign = "-" if delta >= 0 else "+"
        print(f"=== imgclean clean ({args.mode}) ===")
        print(f"  input:  {summary['input']} ({summary['input_size']:,}B, {summary['input_findings']} findings)")
        print(f"  output: {summary['output']} ({summary['output_size']:,}B, {summary['output_findings']} findings)")
        print(f"  size delta: {sign}{abs(delta):,}B")
        print(f"  post-clean: {'YES' if summary['post_clean'] else 'NO — re-inspect for residual findings'}")
        if not summary["post_clean"]:
            print("")
            print(format_report(post, use_color=not args.no_color and sys.stdout.isatty()))

    return 0 if summary["post_clean"] else 1


def _cmd_batch(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    exts = {".png", ".jpg", ".jpeg", ".PNG", ".JPG", ".JPEG"}
    walk = root.rglob("*") if args.recursive else root.iterdir()
    files = [p for p in walk if p.is_file() and p.suffix in exts]

    results = []
    for p in files:
        try:
            data = p.read_bytes()
            cleaned, _ = clean_bytes(data, mode=args.mode) if not args.dry_run else (data, "")
            out = p if args.in_place and not args.dry_run else (
                p.with_name(f"{p.stem}.cleaned{p.suffix}")
            )
            if not args.dry_run:
                out.write_bytes(cleaned)
            pre = inspect(str(p))
            post = inspect(str(out)) if not args.dry_run else pre
            results.append({
                "file": str(p),
                "input_findings": len(pre.findings),
                "output_findings": len(post.findings),
                "size_in": len(data),
                "size_out": len(cleaned),
                "clean": post.is_clean,
            })
        except Exception as exc:
            results.append({"file": str(p), "error": str(exc)})

    if args.json:
        print(json.dumps({"mode": args.mode, "dry_run": args.dry_run, "results": results}, indent=2))
    else:
        for r in results:
            if "error" in r:
                print(f"  ERR  {r['file']}: {r['error']}")
            else:
                status = "CLEAN" if r["clean"] else "DIRTY"
                print(f"  {status:5s}  {r['file']}  {r['input_findings']}→{r['output_findings']} findings")
        print(f"\n{len(results)} file(s) processed.")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    a = inspect(args.a)
    b = inspect(args.b)

    def keyset(r):
        return {(f.category.value, f.location, f.name) for f in r.findings}

    removed = keyset(a) - keyset(b)
    added = keyset(b) - keyset(a)
    common = keyset(a) & keyset(b)

    # Pixel-level comparison via SHA256 of decoded pixels.
    def pixel_hash(path: str) -> str | None:
        try:
            from PIL import Image
            im = Image.open(path)
            im.load()
            return hashlib.sha256(im.tobytes()).hexdigest()
        except Exception:
            return None

    pa, pb = pixel_hash(args.a), pixel_hash(args.b)
    pixels_identical = (pa is not None and pa == pb)

    if args.json:
        print(json.dumps({
            "a": args.a, "b": args.b,
            "removed": [list(x) for x in removed],
            "added": [list(x) for x in added],
            "common": [list(x) for x in common],
            "pixels_identical": pixels_identical,
            "a_pixel_sha256": pa, "b_pixel_sha256": pb,
        }, indent=2, ensure_ascii=False))
    else:
        print(f"=== imgclean diff: {args.a}  vs  {args.b} ===")
        print(f"  pixels_identical: {pixels_identical}")
        if pa: print(f"    a sha256: {pa}")
        if pb: print(f"    b sha256: {pb}")
        print(f"  findings: a={len(a.findings)}  b={len(b.findings)}  common={len(common)}")
        if removed:
            print("  removed (a → b):")
            for r in sorted(removed):
                print(f"    - [{r[0]}] {r[1]}: {r[2]}")
        if added:
            print("  added   (a → b):")
            for r in sorted(added):
                print(f"    + [{r[0]}] {r[1]}: {r[2]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="imgclean",
        description="Detect and strip identifying fingerprints from images "
                    "(AI watermarks, EXIF, C2PA, GPS, vendor markers).",
    )
    p.add_argument("--version", action="version", version=f"imgclean {__version__}")
    p.add_argument("--no-color", action="store_true", help="disable ANSI color in output")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("inspect", help="report all identifying metadata in a file")
    s.add_argument("path")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_inspect)

    s = sub.add_parser("clean", help="produce a cleaned copy of the file")
    s.add_argument("path")
    s.add_argument("-m", "--mode", choices=VALID_MODES, default="safe",
                   help="safe = strip metadata only (pixels untouched); "
                        "paranoid = + re-encode pixels + light noise; "
                        "nuclear = + transcode + resize + color shift to disrupt robust watermarks.")
    s.add_argument("-o", "--output", help="output path (default: <name>.cleaned.<ext>)")
    s.add_argument("--in-place", action="store_true", help="overwrite the input file")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_clean)

    s = sub.add_parser("batch", help="clean every supported image in a directory")
    s.add_argument("dir")
    s.add_argument("-m", "--mode", choices=VALID_MODES, default="safe")
    s.add_argument("--recursive", action="store_true")
    s.add_argument("--in-place", action="store_true")
    s.add_argument("--dry-run", action="store_true",
                   help="report what would change without writing files.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_batch)

    s = sub.add_parser("diff", help="show what changed between two images")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_diff)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

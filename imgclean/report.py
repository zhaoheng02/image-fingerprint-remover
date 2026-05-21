"""Human-readable formatter for InspectReport."""
from .findings import InspectReport, Severity

# Plain ANSI color helpers (no external dep).
_SEV_COLORS = {
    Severity.INFO: "\033[2m",       # dim
    Severity.LOW: "\033[36m",       # cyan
    Severity.MED: "\033[33m",       # yellow
    Severity.HIGH: "\033[31m",      # red
    Severity.CRITICAL: "\033[1;31m",  # bold red
}
_RESET = "\033[0m"


def _color(text: str, sev: Severity, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{_SEV_COLORS.get(sev, '')}{text}{_RESET}"


def format_report(report: InspectReport, use_color: bool = True) -> str:
    lines: list[str] = []
    header = f"=== imgclean inspect: {report.path} ==="
    lines.append(header)
    lines.append(f"format: {report.format}   file_size: {report.file_size:,} bytes")
    if report.is_clean and not report.findings:
        lines.append(_color("CLEAN — no identifying metadata detected.", Severity.INFO, use_color))
    elif report.is_clean:
        lines.append(_color("CLEAN — only informational findings.", Severity.LOW, use_color))
    else:
        total = len(report.findings)
        crit = sum(1 for f in report.findings if f.severity == Severity.CRITICAL)
        high = sum(1 for f in report.findings if f.severity == Severity.HIGH)
        lines.append(
            _color(
                f"NOT CLEAN — {total} finding(s); {crit} critical, {high} high.",
                Severity.HIGH if crit + high else Severity.MED,
                use_color,
            )
        )

    if report.notes:
        lines.append("notes:")
        for n in report.notes:
            lines.append(f"  - {n}")

    if report.findings:
        lines.append("")
        lines.append("findings:")
        for f in report.findings:
            tag = f"[{f.severity.value.upper():>8}]"
            lines.append(_color(f"  {tag} {f.name}", f.severity, use_color))
            lines.append(f"           location: {f.location}")
            lines.append(f"           category: {f.category.value}   size: {f.size_bytes:,}B")
            lines.append(f"           {f.detail}")
            if f.value_preview:
                preview = f.value_preview.strip().replace("\n", " ")
                if len(preview) > 200:
                    preview = preview[:200] + "…"
                lines.append(f"           preview: {preview}")
            lines.append("")
    return "\n".join(lines)


def format_summary(report: InspectReport) -> str:
    status = "CLEAN" if report.is_clean else "DIRTY"
    return f"{status:5s}  {report.path}  ({len(report.findings)} findings, {report.file_size:,}B)"

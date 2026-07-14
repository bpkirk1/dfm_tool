"""Server-side PDF export for a built report dict."""
from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .. import config
from ..commentary import build_commentary


def render_report_pdf(
    report: dict[str, Any], show_manual: bool = True, show_strip: bool = True
) -> bytes:
    # show_manual/show_strip are presentation-only per-run toggles. They never
    # change the underlying report data — only what this PDF renders.
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story: list[Any] = []

    summary = report.get("summary", {})
    story.append(Paragraph("DFM Readiness Report", styles["Title"]))
    story.append(Spacer(1, 10))
    story.append(
        Paragraph(
            f"<b>{report.get('part_name', '')}</b> &middot; {report.get('family', '')}",
            styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            f"Ruleset {report.get('ruleset_version', '')} &middot; "
            f"Score: <b>{summary.get('readiness_score', 'n/a')}%</b>",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 14))

    rows = [["Rule ID", "Parameter", "Measured", "Limit", "Verdict", "Severity", "Source"]]
    for row in summary.get("results", []):
        if not show_manual and row.get("verdict") == "manual":
            continue  # presentation-only: hide manual rows for this run
        measured = row.get("measured")
        rows.append(
            [
                row.get("rule_id", ""),
                row.get("parameter", ""),
                "manual" if measured is None else str(measured),
                str(row.get("limit_detail", "")),
                row.get("verdict", ""),
                row.get("severity", ""),
                (row.get("source") or "")[:45],
            ]
        )

    table = Table(rows, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(table)

    corrections = report.get("corrections", [])
    if corrections:
        story.append(Spacer(1, 14))
        story.append(Paragraph("<b>Recommended corrections</b>", styles["Heading3"]))
        crows = [["Parameter", "Current", "Target", "Direction", "Recommendation"]]
        for c in corrections:
            target = c.get("target_value")
            crows.append(
                [
                    c.get("parameter", ""),
                    "—" if c.get("current_value") is None else str(c.get("current_value")),
                    "review" if target is None else str(target),
                    c.get("direction", ""),
                    (c.get("recommendation") or "")[:90],
                ]
            )
        ctable = Table(crows, repeatRows=1, colWidths=[95, 55, 55, 55, 240])
        ctable.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0369a1")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(ctable)

    # Commentary narrative (deterministic; respects the same per-run toggles).
    sections = build_commentary(report, show_manual=show_manual, show_strip=show_strip)
    if sections:
        story.append(Spacer(1, 14))
        story.append(Paragraph("<b>Commentary</b>", styles["Heading3"]))
        for sec in sections:
            story.append(Paragraph(sec.title, styles["Heading4"]))
            for para in sec.paragraphs:
                story.append(Paragraph(para, styles["Normal"]))
            if sec.id in ("manual", "next_steps", "proposed", "strip"):
                for it in sec.items:
                    text = it.get("text")
                    if text:
                        story.append(Paragraph(f"&bull; {text}", styles["Normal"]))
            story.append(Spacer(1, 6))

    manual = summary.get("manual_check_parameters", [])
    if manual and show_manual:
        story.append(Spacer(1, 12))
        story.append(Paragraph("<b>Requires manual check</b>", styles["Heading3"]))
        for param in manual:
            story.append(Paragraph(f"&bull; {param}", styles["Normal"]))

    proposed = summary.get("proposed", [])
    if proposed:
        story.append(Spacer(1, 12))
        story.append(
            Paragraph("<b>Proposed criteria &mdash; not enforced</b>", styles["Heading3"])
        )
        story.append(
            Paragraph(
                "Mined from reference DFMs and awaiting sign-off; these did not affect "
                "the score or any verdict above.",
                styles["Normal"],
            )
        )
        for p in proposed:
            units = f" {p.get('units')}" if p.get("units") else ""
            story.append(
                Paragraph(
                    f"&bull; <b>{p.get('rule_id', '')}</b> &mdash; {p.get('parameter', '')} "
                    f"({p.get('operator', '')} {p.get('limit', '')}{units})",
                    styles["Normal"],
                )
            )

    story.append(Spacer(1, 16))
    story.append(Paragraph("<b>About &amp; version control</b>", styles["Heading3"]))
    story.append(
        Paragraph(
            f"Application: {config.APP_NAME} v{config.APP_VERSION} &middot; "
            f"Ruleset version: {report.get('ruleset_version', 'n/a')} &middot; "
            f"Criteria schema: {report.get('schema_version', 'n/a')} &middot; "
            f"Generated (UTC): {report.get('generated_at', 'n/a')}",
            styles["Normal"],
        )
    )

    doc.build(story)
    return buffer.getvalue()

from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "reports" / "phase7k_strategy_technical_report.md"
OUTPUT = ROOT / "reports" / "phase7k_strategy_technical_report.pdf"
FONT_REGULAR = Path(r"C:\Windows\Fonts\NotoSerifSC-VF.ttf")
FONT_BOLD = Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf")


def _register_fonts() -> tuple[str, str]:
    regular = "NotoSerifSC"
    bold = "NotoSansSC"
    pdfmetrics.registerFont(TTFont(regular, str(FONT_REGULAR)))
    pdfmetrics.registerFont(TTFont(bold, str(FONT_BOLD)))
    return regular, bold


def _clean_inline(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"<font name='Courier'>\1</font>", text)
    text = text.replace("&", "&amp;").replace("<font name='Courier'>", "<font name='Courier'>")
    text = text.replace("</font>", "</font>").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("&lt;font name='Courier'&gt;", "<font name='Courier'>")
    text = text.replace("&lt;/font&gt;", "</font>")
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    return text


def _table_from_lines(lines: list[str], styles: dict[str, ParagraphStyle]) -> Table:
    rows: list[list[Paragraph]] = []
    for raw in lines:
        parts = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} for cell in parts):
            continue
        rows.append([Paragraph(_clean_inline(cell), styles["TableCell"]) for cell in parts])
    col_count = max(len(r) for r in rows)
    for row in rows:
        while len(row) < col_count:
            row.append(Paragraph("", styles["TableCell"]))
    usable_width = A4[0] - 4 * cm
    col_widths = [usable_width / col_count] * col_count
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "NotoSerifSC"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAEAEA")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAAAAA")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _parse_markdown(text: str, styles: dict[str, ParagraphStyle]) -> list:
    story: list = []
    lines = text.splitlines()
    i = 0
    in_code = False
    code_lines: list[str] = []
    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("```"):
            if in_code:
                story.append(Preformatted("\n".join(code_lines), styles["Code"]))
                story.append(Spacer(1, 0.18 * cm))
                code_lines = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_lines.append(line)
            i += 1
            continue

        if not line:
            story.append(Spacer(1, 0.12 * cm))
            i += 1
            continue

        if line.startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].rstrip())
                i += 1
            story.append(_table_from_lines(table_lines, styles))
            story.append(Spacer(1, 0.25 * cm))
            continue

        if line.startswith("# "):
            story.append(Paragraph(_clean_inline(line[2:]), styles["Title"]))
            story.append(Spacer(1, 0.15 * cm))
        elif line.startswith("## "):
            if line.startswith("## 附录"):
                story.append(PageBreak())
            story.append(Paragraph(_clean_inline(line[3:]), styles["Heading1"]))
        elif line.startswith("### "):
            story.append(Paragraph(_clean_inline(line[4:]), styles["Heading2"]))
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(Paragraph("• " + _clean_inline(lines[i][2:]), styles["Body"]))
                i += 1
            story.extend(items)
            story.append(Spacer(1, 0.08 * cm))
            continue
        elif re.match(r"^\d+\. ", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\. ", lines[i]):
                items.append(Paragraph(_clean_inline(lines[i]), styles["Body"]))
                i += 1
            story.extend(items)
            story.append(Spacer(1, 0.08 * cm))
            continue
        elif line.startswith("\\[") or line.startswith("\\]") or line.startswith("\\") or line.startswith("&") or line.startswith("=") or line.startswith("+") or line.startswith("{") or line.startswith("}"):
            formula = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith("#") and not lines[i].startswith("|"):
                formula.append(lines[i].rstrip())
                if lines[i].strip().endswith("\\]"):
                    i += 1
                    break
                i += 1
            cleaned = "\n".join(formula).replace("\\[", "").replace("\\]", "").strip()
            story.append(Preformatted(cleaned, styles["Formula"]))
            story.append(Spacer(1, 0.08 * cm))
            continue
        else:
            story.append(Paragraph(_clean_inline(line), styles["Body"]))
        i += 1

    img_root = ROOT / "artifacts" / "phase7k_backtest" / "multi_cap_open_open_20160101_20260520_rerun"
    equity = img_root / "equity_curve_compare.png"
    drawdown = img_root / "drawdown_curve_compare.png"
    if equity.exists() and drawdown.exists():
        story.append(PageBreak())
        story.append(Paragraph("附录 B 回测曲线", styles["Heading1"]))
        for title, image_path in (("净值曲线对比", equity), ("回撤曲线对比", drawdown)):
            story.append(KeepTogether([Paragraph(title, styles["Heading2"]), Image(str(image_path), width=16 * cm, height=9 * cm)]))
            story.append(Spacer(1, 0.3 * cm))
    return story


def _page(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("NotoSerifSC", 8)
    canvas.drawCentredString(A4[0] / 2, 0.9 * cm, f"{doc.page}")
    canvas.restoreState()


def main() -> int:
    regular, bold = _register_fonts()
    base = getSampleStyleSheet()
    styles = {
        "Title": ParagraphStyle(
            "Title",
            parent=base["Title"],
            fontName=bold,
            fontSize=18,
            leading=24,
            alignment=TA_CENTER,
            spaceAfter=8,
        ),
        "Heading1": ParagraphStyle(
            "Heading1",
            parent=base["Heading1"],
            fontName=bold,
            fontSize=13,
            leading=18,
            spaceBefore=10,
            spaceAfter=6,
        ),
        "Heading2": ParagraphStyle(
            "Heading2",
            parent=base["Heading2"],
            fontName=bold,
            fontSize=11,
            leading=15,
            spaceBefore=7,
            spaceAfter=4,
        ),
        "Body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName=regular,
            fontSize=9.2,
            leading=14.2,
            firstLineIndent=0,
            alignment=TA_LEFT,
        ),
        "TableCell": ParagraphStyle(
            "TableCell",
            parent=base["BodyText"],
            fontName=regular,
            fontSize=7.2,
            leading=9.5,
        ),
        "Code": ParagraphStyle(
            "Code",
            parent=base["Code"],
            fontName="Courier",
            fontSize=7.4,
            leading=9,
            leftIndent=0.3 * cm,
        ),
        "Formula": ParagraphStyle(
            "Formula",
            parent=base["Code"],
            fontName="Courier",
            fontSize=7.5,
            leading=9.5,
            leftIndent=0.45 * cm,
        ),
    }
    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=1.8 * cm,
        bottomMargin=1.6 * cm,
    )
    story = _parse_markdown(SOURCE.read_text(encoding="utf-8"), styles)
    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

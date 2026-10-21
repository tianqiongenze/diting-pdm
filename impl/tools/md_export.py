"""把项目内全部 Markdown 批量导出为 Word(.docx) 与 PDF。

设计要点：
- 不依赖 pandoc / LibreOffice（环境不可控），纯 Python 生成
- 中文：docx 用「微软雅黑 / 等线」并设置 eastAsia；PDF 用 reportlab 内置 CID 字体 STSong-Light（免字体文件）
- 支持的 Markdown 子集：标题、段落、无序/有序列表、表格、代码块、引用、分割线、粗体、行内代码
- 输出：export/docx/**、export/pdf/**，保持相对路径，文件名带序号便于排序

用法：
    python tools/md_export.py            # 导出全部
    python tools/md_export.py --only docs # 仅导出 docs/ 下
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # impl/
PROJECT = ROOT.parent                                # Diting-PdM/
OUT = PROJECT / "export"

CJK_FONT_DOCX = "微软雅黑"
CJK_FONT_PDF = "STSong-Light"

SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "export", "node_modules"}


# ---------------------------------------------------------------------------
# Markdown 解析（块级）
# ---------------------------------------------------------------------------
def parse_blocks(text: str) -> list[tuple[str, object]]:
    lines = text.splitlines()
    blocks: list[tuple[str, object]] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(buf)))
            continue

        if re.match(r"^\|.*\|\s*$", stripped) and i + 1 < n and re.match(r"^\|[\s:\-|]+\|\s*$", lines[i + 1].strip()):
            rows = []
            header = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2
            while i < n and re.match(r"^\|.*\|\s*$", lines[i].strip()):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            blocks.append(("table", (header, rows)))
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            blocks.append(("h", (len(m.group(1)), m.group(2).strip())))
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            blocks.append(("hr", None))
            i += 1
            continue

        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(("quote", " ".join(buf)))
            continue

        m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
        if m:
            items = []
            while i < n:
                mm = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[i])
                if not mm:
                    break
                indent = len(mm.group(1))
                ordered = mm.group(2).endswith(".")
                items.append((indent, ordered, mm.group(3).strip()))
                i += 1
            blocks.append(("list", items))
            continue

        if not stripped:
            i += 1
            continue

        buf = []
        while i < n and lines[i].strip() and not lines[i].strip().startswith(("#", "```", "|", ">")):
            if re.match(r"^(\s*)([-*+]|\d+\.)\s+", lines[i]):
                break
            if re.match(r"^(-{3,}|\*{3,})$", lines[i].strip()):
                break
            buf.append(lines[i].strip())
            i += 1
        if buf:
            blocks.append(("p", " ".join(buf)))
        else:
            i += 1
    return blocks


def strip_inline(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text


def inline_runs(text: str):
    """把 **bold** 与 `code` 拆成 (片段, 是否粗体, 是否代码)。"""
    pattern = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            yield (text[pos : m.start()], False, False)
        token = m.group(0)
        if token.startswith("**"):
            yield (token[2:-2], True, False)
        else:
            yield (token[1:-1], False, True)
        pos = m.end()
    if pos < len(text):
        yield (text[pos:], False, False)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def export_docx(md_path: Path, out_path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    doc = Document()

    normal = doc.styles["Normal"]
    normal.font.name = CJK_FONT_DOCX
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT_DOCX)

    for lvl in range(1, 5):
        st = doc.styles[f"Heading {lvl}"]
        st.font.name = CJK_FONT_DOCX
        st.element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT_DOCX)
        st.font.color.rgb = RGBColor(0x1A, 0x1D, 0x21)

    blocks = parse_blocks(md_path.read_text(encoding="utf-8"))

    for kind, payload in blocks:
        if kind == "h":
            level, text = payload  # type: ignore[misc]
            doc.add_heading(strip_inline(text), level=min(level, 4))
        elif kind == "p":
            p = doc.add_paragraph()
            for frag, bold, code in inline_runs(payload):  # type: ignore[arg-type]
                run = p.add_run(frag)
                run.bold = bold
                if code:
                    run.font.name = "Consolas"
                    run.font.size = Pt(9.5)
                    run.font.color.rgb = RGBColor(0x0B, 0x4F, 0x75)
        elif kind == "list":
            for indent, ordered, text in payload:  # type: ignore[misc]
                style = "List Number" if ordered else "List Bullet"
                p = doc.add_paragraph(style=style)
                p.paragraph_format.left_indent = Pt(18 + indent * 6)
                for frag, bold, code in inline_runs(text):
                    run = p.add_run(frag)
                    run.bold = bold
                    if code:
                        run.font.name = "Consolas"
                        run.font.size = Pt(9.5)
        elif kind == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(18)
            run = p.add_run(strip_inline(payload))  # type: ignore[arg-type]
            run.italic = True
            run.font.color.rgb = RGBColor(0x4A, 0x51, 0x58)
        elif kind == "code":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(12)
            run = p.add_run(payload)  # type: ignore[arg-type]
            run.font.name = "Consolas"
            run.font.size = Pt(8.5)
            run.font.color.rgb = RGBColor(0x24, 0x29, 0x2F)
        elif kind == "table":
            header, rows = payload  # type: ignore[misc]
            cols = max(len(header), max((len(r) for r in rows), default=0))
            table = doc.add_table(rows=1, cols=cols)
            table.style = "Table Grid"
            for idx in range(cols):
                cell = table.rows[0].cells[idx]
                cell.text = ""
                run = cell.paragraphs[0].add_run(strip_inline(header[idx]) if idx < len(header) else "")
                run.bold = True
                run.font.size = Pt(9)
            for row in rows:
                cells = table.add_row().cells
                for idx in range(cols):
                    cells[idx].text = ""
                    run = cells[idx].paragraphs[0].add_run(strip_inline(row[idx]) if idx < len(row) else "")
                    run.font.size = Pt(9)
        elif kind == "hr":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run("— — —")
            run.font.color.rgb = RGBColor(0xB0, 0xB6, 0xBD)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def export_pdf(md_path: Path, out_path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (
        HRFlowable,
        PageBreak,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    try:
        pdfmetrics.registerFont(UnicodeCIDFont(CJK_FONT_PDF))
    except Exception:  # noqa: BLE001
        pass

    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "body", parent=base["BodyText"], fontName=CJK_FONT_PDF, fontSize=9.5, leading=15, alignment=TA_LEFT, spaceAfter=4
    )
    h_styles = {
        1: ParagraphStyle("h1", parent=body, fontSize=18, leading=24, spaceBefore=10, spaceAfter=8, textColor=colors.HexColor("#0F5C8C")),
        2: ParagraphStyle("h2", parent=body, fontSize=14, leading=20, spaceBefore=12, spaceAfter=6, textColor=colors.HexColor("#1A1D21")),
        3: ParagraphStyle("h3", parent=body, fontSize=11.5, leading=17, spaceBefore=9, spaceAfter=4, textColor=colors.HexColor("#1A1D21")),
        4: ParagraphStyle("h4", parent=body, fontSize=10.5, leading=15, spaceBefore=7, spaceAfter=3, textColor=colors.HexColor("#4A5158")),
    }
    code_style = ParagraphStyle(
        "code", parent=body, fontName="Courier", fontSize=7.2, leading=9.2, textColor=colors.HexColor("#24292F")
    )
    quote_style = ParagraphStyle(
        "quote", parent=body, fontSize=9.5, leading=15, leftIndent=12, textColor=colors.HexColor("#4A5158")
    )

    def esc(t: str) -> str:
        t = html.escape(t, quote=False)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"`([^`]+)`", r'<font face="Courier" color="#0B4F75">\1</font>', t)
        t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
        return t

    story = []
    for kind, payload in parse_blocks(md_path.read_text(encoding="utf-8")):
        if kind == "h":
            level, text = payload  # type: ignore[misc]
            story.append(Paragraph(esc(text), h_styles[min(level, 4)]))
        elif kind == "p":
            story.append(Paragraph(esc(payload), body))  # type: ignore[arg-type]
        elif kind == "list":
            for indent, ordered, text in payload:  # type: ignore[misc]
                bullet = "•" if not ordered else "▪"
                style = ParagraphStyle(
                    f"li{indent}", parent=body, leftIndent=12 + indent * 8, bulletIndent=4 + indent * 8, spaceAfter=2
                )
                story.append(Paragraph(f"{bullet} {esc(text)}", style))
        elif kind == "quote":
            story.append(Paragraph("▏ " + esc(payload), quote_style))  # type: ignore[arg-type]
        elif kind == "code":
            story.append(Preformatted(str(payload), code_style))
            story.append(Spacer(1, 4))
        elif kind == "table":
            header, rows = payload  # type: ignore[misc]
            cols = max(len(header), max((len(r) for r in rows), default=0))
            data = [[Paragraph(f"<b>{esc(c)}</b>", body) for c in header] + [""] * (cols - len(header))]
            for row in rows:
                data.append([Paragraph(esc(c), body) for c in row] + [""] * (cols - len(row)))
            avail = A4[0] - 36 * mm
            col_w = [avail / cols] * cols
            t = Table(data, colWidths=col_w, repeatRows=1)
            t.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D8DDE3")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F5F7")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 3),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                        ("TOPPADDING", (0, 0), (-1, -1), 2),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ]
                )
            )
            story.append(t)
            story.append(Spacer(1, 6))
        elif kind == "hr":
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#D8DDE3")))
            story.append(Spacer(1, 4))

    if not story:
        story.append(Paragraph("(empty)", body))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=md_path.stem,
        author="Diting PdM",
    )

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont(CJK_FONT_PDF, 7.5)
        canvas.setFillColor(colors.HexColor("#8B939C"))
        canvas.drawString(18 * mm, 9 * mm, "谛听 Diting · 设备预测性维护智能体平台")
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"{doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)


# ---------------------------------------------------------------------------
def collect(only: str | None) -> list[Path]:
    files = []
    for p in sorted(PROJECT.rglob("*.md")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if only and only not in str(p.relative_to(PROJECT)).replace("\\", "/"):
            continue
        files.append(p)
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    files = collect(args.only)
    if not files:
        print("no markdown found")
        return 1

    ok = fail = 0
    for idx, md in enumerate(files, 1):
        rel = md.relative_to(PROJECT)
        stem = f"{idx:02d}_{md.stem}"
        for fmt, fn in (("docx", export_docx), ("pdf", export_pdf)):
            target = OUT / fmt / rel.parent / f"{stem}.{fmt}"
            try:
                fn(md, target)
                ok += 1
                print(f"[{fmt}] {rel} -> {target.relative_to(PROJECT)}")
            except Exception as exc:  # noqa: BLE001
                fail += 1
                print(f"[{fmt}] FAILED {rel}: {exc}", file=sys.stderr)

    print(f"\n完成：{ok} 成功 / {fail} 失败 / 共 {len(files)} 个源文件")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

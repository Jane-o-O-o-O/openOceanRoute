"""Render the maintained manual and design sources to inspectable Chinese PDFs."""
from pathlib import Path
import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image, KeepTogether
from reportlab.graphics.shapes import Drawing, Rect, String, Line

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output/pdf"
OUTPUT.mkdir(parents=True, exist_ok=True)
FONT_PATH = Path("/System/Library/Fonts/STHeiti Light.ttc")
pdfmetrics.registerFont(TTFont("OceanCJK", str(FONT_PATH), subfontIndex=0))
pdfmetrics.registerFontFamily("OceanCJK", normal="OceanCJK", bold="OceanCJK", italic="OceanCJK", boldItalic="OceanCJK")
INK, TEAL, MUTED = (colors.HexColor(v) for v in ("#183447", "#087D86", "#607782"))
WIDTH = A4[0] - 90
STYLES = {
    "title": ParagraphStyle("title", fontName="OceanCJK", fontSize=28, leading=40, textColor=INK, spaceAfter=16, wordWrap="CJK"),
    "h2": ParagraphStyle("h2", fontName="OceanCJK", fontSize=15, leading=24, textColor=TEAL, spaceBefore=14, spaceAfter=10, wordWrap="CJK", keepWithNext=True),
    "h3": ParagraphStyle("h3", fontName="OceanCJK", fontSize=11.5, leading=20, textColor=TEAL, spaceBefore=10, spaceAfter=8, wordWrap="CJK", keepWithNext=True),
    "body": ParagraphStyle("body", fontName="OceanCJK", fontSize=10, leading=17, textColor=INK, spaceAfter=9, wordWrap="CJK"),
    "small": ParagraphStyle("small", fontName="OceanCJK", fontSize=8.5, leading=14, textColor=MUTED, spaceAfter=8, wordWrap="CJK"),
    "code": ParagraphStyle("code", fontName="OceanCJK", fontSize=8.3, leading=13, textColor=INK, leftIndent=8, rightIndent=8, backColor=colors.HexColor("#EDF4F6"), borderPadding=8, spaceAfter=12, wordWrap="CJK"),
    "cell": ParagraphStyle("cell", fontName="OceanCJK", fontSize=8.5, leading=12, textColor=INK, wordWrap="CJK"),
    "head": ParagraphStyle("head", fontName="OceanCJK", fontSize=8.5, leading=13, textColor=colors.white, wordWrap="CJK"),
}


def inline(text):
    text = text.replace("—", "-").replace("–", "-").replace("−", "-").replace("‑", "-")
    text = escape(text)
    text = re.sub(r"`([^`]+)`", r'<font color="#087D86">\1</font>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r'<b>\1</b>', text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: m[1] if m[1] == m[2] else f"{m[1]} ({m[2]})", text)
    return text


def table(lines, design=False):
    rows = [[v.strip() for v in line.strip().strip("|").split("|")] for line in lines]
    rows = [r for r in rows if not all(re.fullmatch(r":?-+:?", c) for c in r)]
    count = len(rows[0])
    widths = [WIDTH * v for v in ([.28, .43, .29] if count == 3 and design else [.2, .5, .3] if count == 3 else [.21, .13, .48, .18] if count == 4 else [1 / count] * count)]
    flowable = Table([[Paragraph(inline(v), STYLES["head" if index == 0 else "cell"]) for v in row] for index, row in enumerate(rows)], colWidths=widths, repeatRows=1)
    flowable.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#EEF5F6"), colors.HexColor("#F8FAFB")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 1), (-1, -1), .3, colors.HexColor("#D3E1E6")),
    ]))
    return flowable


def architecture():
    drawing = Drawing(WIDTH, 190)
    boxes = [(0, 125, WIDTH, 52, "React / Leaflet / Three.js", "编辑、联动、三维与文件操作"),
             (0, 57, WIDTH, 51, "FastAPI + 工程 / GIS / 力学内核", "计算、验证、来源与数值诊断"),
             (0, 0, WIDTH * .48, 40, "SQLite / JSON 任务", "关系修订、检查点与恢复"),
             (WIDTH * .52, 0, WIDTH * .48, 40, "开放格式", "CSV / JSON / GIS / 图形")]
    for x, y, width, height, label, detail in boxes:
        drawing.add(Rect(x, y, width, height, fillColor=colors.HexColor("#EDF5F6"), strokeColor=TEAL, strokeWidth=.7, rx=5, ry=5))
        drawing.add(String(x + width / 2, y + height - 18, label, fontName="OceanCJK", fontSize=10, textAnchor="middle", fillColor=INK))
        drawing.add(String(x + width / 2, y + 10, detail, fontName="OceanCJK", fontSize=8, textAnchor="middle", fillColor=MUTED))
    for x, start, end in [(WIDTH / 2, 125, 108), (WIDTH * .24, 57, 40), (WIDTH * .76, 57, 40)]:
        drawing.add(Line(x, start, x, end, strokeColor=TEAL, strokeWidth=1))
    return drawing


def relations():
    drawing = Drawing(WIDTH, 235)
    boxes = {"ws": (WIDTH*.34, 188, WIDTH*.32, 36, "Workspace", "完整工程与修订"),
             "shared": (0, 112, WIDTH*.28, 40, "共享资源", "缆材库 / GIS"),
             "path": (WIDTH*.36, 112, WIDTH*.28, 40, "Cable Paths", "独立路线投影"),
             "assembly": (WIDTH*.72, 112, WIDTH*.28, 40, "Assemblies", "唯一实物库存"),
             "project": (WIDTH*.18, 25, WIDTH*.28, 40, "Project schema 1", "路线 / 物性 / 费用"),
             "association": (WIDTH*.56, 25, WIDTH*.36, 40, "Associations", "投放 / 互斥备选")}
    for key, (x, y, width, height, label, detail) in boxes.items():
        drawing.add(Rect(x, y, width, height, fillColor=colors.HexColor("#EDF5F6"), strokeColor=TEAL, strokeWidth=.7, rx=4, ry=4))
        drawing.add(String(x+width/2, y+height-15, label, fontName="OceanCJK", fontSize=9, textAnchor="middle", fillColor=INK))
        drawing.add(String(x+width/2, y+9, detail, fontName="OceanCJK", fontSize=8, textAnchor="middle", fillColor=MUTED))
    for parent, child in [("ws", "shared"), ("ws", "path"), ("ws", "assembly"), ("path", "project"), ("path", "association"), ("assembly", "association")]:
        x, y, w, h, *_ = boxes[parent]; a, b, c, d, *_ = boxes[child]
        drawing.add(Line(x+w/2, y, a+c/2, b+d, strokeColor=TEAL, strokeWidth=.8))
    return drawing


def append_paragraph_group(story, paragraphs, source_line=""):
    headings = []
    while story and isinstance(story[-1], Paragraph) and story[-1].style.name in {"h2", "h3"}:
        headings.insert(0, story.pop())
    group = KeepTogether(headings + paragraphs)
    group.source_line = source_line
    story.append(group)


def build(source_name, target_name, title, design=False):
    text = (ROOT / "docs" / source_name).read_text()
    story = [Spacer(1, 28), Paragraph("OCEANROUTE", STYLES["small"]), Paragraph(title, STYLES["title"]),
             Paragraph("海缆规划与敷设研究工作空间 / 0.2", STYLES["h2"]),
             Paragraph("2026-10-04 · 独立实现 · 可运行开发版", STYLES["body"]),
             Spacer(1, 12), Paragraph("本文对应当前程序行为。规划功能和研究模型的验证范围分别说明；没有原厂或海试对照时，不声称等效工程精度。", STYLES["body"])]
    if design:
        story.extend([Spacer(1, 10), architecture()])
    else:
        screenshot = ROOT / "web/artifacts/production-workspace.png"
        if not screenshot.exists():
            screenshot = ROOT / "web/artifacts/planning-verified.png"
        if screenshot.exists():
            image = Image(str(screenshot))
            image.drawHeight = WIDTH * image.imageHeight / image.imageWidth
            image.drawWidth = WIDTH
            story.extend([Spacer(1, 12), image, Spacer(1, 8), Paragraph("工作空间示例。数据为合成演示，界面随版本更新。", STYLES["small"])])
    story.append(PageBreak())
    lines, index = text.splitlines(), 0
    final_section_start = None
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("# ") or line.startswith("版本"):
            continue
        if line.startswith("```"):
            language = line[3:].strip()
            block = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index]); index += 1
            index += 1
            if language == "mermaid" and design:
                story.extend([relations(), Spacer(1, 8)])
            else:
                story.append(Paragraph("<br/>".join(inline(v).replace(" ", "&#160;") for v in block), STYLES["code"]))
        elif line.startswith("|"):
            block = [line]
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index]); index += 1
            story.extend([table(block, design), Spacer(1, 9)])
        elif line.startswith("## "):
            if design and line[3:].startswith("9."):
                final_section_start = len(story)
            if not design and re.match(r"4\.", line[3:]):
                story.append(PageBreak())
            story.append(Paragraph(inline(line[3:]), STYLES["h2"]))
        elif line.startswith("### "):
            story.append(Paragraph(inline(line[4:]), STYLES["h3"]))
        elif line.startswith("- "):
            block = [Paragraph(inline(line), STYLES["body"])]
            while index < len(lines) and lines[index].strip().startswith("- "):
                block.append(Paragraph(inline(lines[index].strip()), STYLES["body"]))
                index += 1
            if story and getattr(story[-1], "source_line", "").endswith((":", "：")):
                block.insert(0, story.pop())
            append_paragraph_group(story, block)
        else:
            append_paragraph_group(story, [Paragraph(inline(line), STYLES["body"])], line)
    if final_section_start is not None:
        story[final_section_start:] = [KeepTogether(story[final_section_start:])]
    def page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(TEAL); canvas.rect(45, A4[1]-32, 25, 3, fill=1, stroke=0)
        canvas.setFillColor(MUTED); canvas.setFont("OceanCJK", 8)
        canvas.drawString(80, A4[1]-33, "OceanRoute / " + title)
        canvas.setStrokeColor(colors.HexColor("#D3E1E6")); canvas.line(45, 36, A4[0]-45, 36)
        canvas.drawString(45, 23, "0.2 · 2026-10-04 · 独立实现，研究模型待工程校核")
        canvas.drawRightString(A4[0]-45, 23, str(doc.page))
        canvas.restoreState()
    target = OUTPUT / target_name
    document = SimpleDocTemplate(str(target), pagesize=A4, leftMargin=45, rightMargin=45, topMargin=51, bottomMargin=51,
                                 title=title, author="OceanRoute", subject="独立实现用户与设计文档")
    document.build(story, onFirstPage=page, onLaterPages=page)
    print(target)


if __name__ == "__main__":
    build("USER_MANUAL.md", "OceanRoute_用户手册.pdf", "用户手册")
    build("DESIGN.md", "OceanRoute_设计文档.pdf", "软件设计文档", design=True)

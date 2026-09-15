"""Check actual 0.9 PDF text, bounds and maintained-source coverage.

This is an automatic structural check, not glyph/layout/visual acceptance.
Every final page still requires a separate, actual image review. Running this
module writes a NEW development_0.9_* report unless --verify-only is supplied;
existing reports, PDFs, Markdown and historic release evidence are never edited.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import unicodedata

import pdfplumber
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "resources/validation/development_0.9_pdf_structure.json"
BUILDER = ROOT / "resources/build_product_documents.py"
MAX_PDF_BYTES = 32 * 1024**2
MAX_SOURCE_BYTES = 2 * 1024**2
MAX_PAGES = 200
DASHES = str.maketrans({"—": "-", "–": "-", "−": "-", "‑": "-"})

REQUIRED = {
    "manual": {
        "section": "7.1 路线侧坡与KP坡度规则",
        "terms": ["真实侧坡采样", "KP 坡度规则", "右舷上坡为正", "RULE_RANGE_EMPTY",
                  "应用侧坡记录", "应用坡度规则列表", "最大探点绝对坡", "null"],
        "formulas": [],
    },
    "design": {
        "section": "6.1 路线侧坡、KP规则与原子应用",
        "terms": ["route-side-slopes-v1", "kp-slope-rules-v1", "side_slopes", "slope_rules",
                  "terrain_library_signature", "route_signature", "max_sampled_abs_slope_deg",
                  "source_boundary", "heading_policy", "end_kp_m", "sampled_pass",
                  "continuous_bed_verified", "App.applySideSlopesCandidate",
                  "/api/terrain/side-slopes/example", "/api/terrain/side-slopes",
                  "/api/tools/slope-rules", "analysisProject", "analysisCurrent"],
        "formulas": [
            "port = atan((D(-w)-D(0))/w)",
            "starboard = atan((D(0)-D(w))/w)",
            "full = atan((D(-w)-D(w))/(2w))",
            "adjacent_i = atan((D(u_i)-D(u_(i+1)))/(u_(i+1)-u_i))",
            "maximum = max(abs(adjacent_i) over actual adjacent valid probes)",
        ],
    },
}


def normalized(value: str) -> str:
    """Ignore layout whitespace and the builder's four dash substitutions.

    No letters, CJK glyphs, ASCII field characters, symbols or digits are
    discarded. A snake_case key split by a real PDF line wrap still matches.
    The builder explicitly renders U+2080 as a supported subscript ASCII zero;
    that presentation change is matched as zero, never as a missing character.
    """
    printable = value.translate(DASHES).replace("₀", "0")
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", printable))


def inline_text(value: str) -> str:
    """Expected printable Markdown inline text, without importing the builder."""
    value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                   lambda m: m[1] if m[1] == m[2] else f"{m[1]} ({m[2]})", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    return re.sub(r"\*\*(.+?)\*\*", r"\1", value).translate(DASHES)


def descriptor(path: Path) -> dict:
    payload = path.read_bytes()
    try:
        label = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        label = str(path.resolve())
    return {"path": label, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def source_units(source: str) -> tuple[list[dict], list[dict]]:
    """Enumerate each printed prose/heading/table cell/formula or code unit.

    JSON formatting is canonicalized because the PDF builder pretty-prints
    parsed JSON. Mermaid is explicitly reported as a diagram substitution;
    its code is not falsely required to appear as printed source code.
    """
    units, substituted = [], []
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        line_number = index + 1
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("# ") or line.startswith("版本"):
            continue
        if line.startswith("```"):
            language = line[3:].strip()
            block = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            if index == len(lines):
                raise ValueError(f"Unclosed source code fence at line {line_number}")
            index += 1
            if language == "mermaid":
                substituted.append({"source_line": line_number, "kind": "mermaid_diagram",
                                    "literal_code_coverage_checked": False})
            elif language == "json":
                value = json.loads("\n".join(block))
                printable = json.dumps(value, ensure_ascii=False, allow_nan=False)
                units.append({"source_line": line_number, "kind": "json", "text": printable})
            else:
                units.extend({"source_line": line_number + 1 + offset, "kind": "code_or_formula",
                              "text": inline_text(row)} for offset, row in enumerate(block) if row.strip())
        elif line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                units.extend({"source_line": line_number, "kind": "table_cell", "column": i + 1,
                              "text": inline_text(cell)} for i, cell in enumerate(cells) if cell)
        elif line.startswith("## ") or line.startswith("### "):
            units.append({"source_line": line_number, "kind": "heading",
                          "text": inline_text(re.sub(r"^#{2,3}\s+", "", line))})
        else:
            units.append({"source_line": line_number, "kind": "prose_or_list", "text": inline_text(line)})
    return units, substituted


def body_flow_text(reader_page, title: str, date: str, number: int) -> tuple[str, bool]:
    """Use independent PDF draw order to keep table-cell prose together.

    pdfplumber's geometric reading order may interleave wrapped table columns.
    The initial visitor-matrix filter was empirically wrong for ReportLab's
    fragmented Td text: pypdf's visitor reported increasingly negative tm[5]
    while pdfplumber and the actual image showed correctly placed characters.
    Keep the complete independent draw-order text, and remove ONLY the exact
    known three-line decoration prefix. Their actual physical positions are
    separately checked with pdfplumber; no coordinate heuristic drops prose.
    """
    raw = reader_page.extract_text() or ""
    lines = raw.splitlines()
    first = [index for index, line in enumerate(lines) if line.strip()][:3]
    expected = ["OceanRoute / " + title,
                f"0.9 · {date} · 独立实现，研究模型待工程校核", str(number)]
    verified = (len(first) == 3 and all(normalized(lines[index]) == normalized(value)
                                      for index, value in zip(first, expected)))
    if verified:
        return "\n".join(lines[first[-1] + 1:]), True
    # A changed/unexpected PDF decoration cannot silently permit a passed gate.
    # Preserve all text for diagnostics and fail the page's explicit prefix check.
    return raw, False


def missing_unit(unit: dict) -> dict:
    text = unit["text"]
    return {**{key: value for key, value in unit.items() if key != "text"},
            "text_excerpt": text[:240], "printable_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "normalized_length": len(normalized(text))}


def inspect_document(kind: str, pdf_path: Path, source_path: Path, date: str) -> dict:
    pdf_info = descriptor(pdf_path)
    source_info = descriptor(source_path)
    if not 0 < pdf_info["bytes"] <= MAX_PDF_BYTES:
        raise ValueError(f"{kind}: PDF must be nonempty and at most {MAX_PDF_BYTES} bytes")
    if not 0 < source_info["bytes"] <= MAX_SOURCE_BYTES:
        raise ValueError(f"{kind}: Markdown exceeds the bounded source size")
    source = source_path.read_text(encoding="utf-8")
    units, substituted = source_units(source)
    required = REQUIRED[kind]
    source_normalized = normalized(inline_text(source))
    required_source_missing = [value for value in [required["section"], *required["terms"], *required["formulas"]]
                               if normalized(value) not in source_normalized]
    source_version_ok = bool(re.search(r"^版本\s+0\.9(?:\D|$)", source, re.M))
    reader = PdfReader(pdf_path)
    if reader.is_encrypted:
        raise ValueError(f"{kind}: encrypted PDF cannot be accepted")
    count = len(reader.pages)
    if not 1 <= count <= MAX_PAGES:
        raise ValueError(f"{kind}: actual PDF page count must be within 1..{MAX_PAGES}")
    title = "用户手册" if kind == "manual" else "软件设计文档"
    expected_header = normalized("OceanRoute / " + title)
    expected_footer = normalized(f"0.9 · {date} · 独立实现，研究模型待工程校核")
    outliers, bad_characters, pages, flow_pages = [], [], [], []
    with pdfplumber.open(pdf_path) as pdf:
        if len(pdf.pages) != count:
            raise ValueError(f"{kind}: independent PDF parsers disagree on page count")
        for number, page in enumerate(pdf.pages, 1):
            height, width = float(page.height), float(page.width)
            if not all(math.isfinite(v) and v > 100 for v in (height, width)):
                raise ValueError(f"{kind}: non-finite or invalid page dimensions")
            chars = [char for char in page.chars if char.get("text", "").strip()]
            page_outliers = []
            for char in chars:
                coords = [float(char[key]) for key in ("x0", "x1", "top", "bottom")]
                if (not all(math.isfinite(value) for value in coords) or coords[0] < 40
                        or coords[1] > width - 40 or coords[2] < 16 or coords[3] > height - 15):
                    row = {"page": number, "text": char["text"],
                           **dict(zip(("x0", "x1", "top", "bottom"), coords))}
                    outliers.append(row)
                    page_outliers.append(row)
            text = page.extract_text() or ""
            header = page.crop((0, 0, width, 45)).extract_text() or ""
            footer = page.crop((0, height - 40, width, height)).extract_text() or ""
            page_number = page.crop((width - 75, height - 40, width, height)).extract_text() or ""
            body = page.crop((0, 45, width, height - 40)).extract_text() or ""
            flow, prefix_verified = body_flow_text(reader.pages[number - 1], title, date, number)
            flow_pages.append(flow)
            invalid = sorted(set(re.findall(r"\(cid:\d+\)|[\ufffd\x00\ue000-\uf8ff]", text + flow)))
            if invalid:
                bad_characters.append({"page": number, "tokens": invalid})
            row = {"page": number, "width_pt": width, "height_pt": height,
                   "non_whitespace_characters": len(chars),
                   "body_text_characters": len(normalized(body)),
                   "body_draw_order_text_characters": len(normalized(flow)),
                   "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                   "body_draw_order_sha256": hashlib.sha256(flow.encode()).hexdigest(),
                   "draw_order_decoration_prefix_verified": prefix_verified,
                   "header_present": expected_header in normalized(header),
                   "footer_version_date_present": expected_footer in normalized(footer),
                   "page_number_present": normalized(page_number) == str(number),
                   "character_boundary_outliers": len(page_outliers),
                   "unsupported_extraction_tokens": invalid,
                   "short_body_requires_visual_review": len(normalized(body)) < 80}
            row["status"] = "passed" if (row["body_text_characters"] > 0 and len(normalized(flow)) > 0
                            and row["header_present"] and row["footer_version_date_present"]
                            and row["page_number_present"] and prefix_verified and not invalid and not page_outliers) else "failed"
            pages.append(row)
    # Page 1 is the generated cover, not the maintained Markdown body. Joining
    # remaining draw-order text permits paragraphs/code that span actual pages.
    body_text = normalized("\n".join(flow_pages[1:]))
    missing = [missing_unit(unit) for unit in units if normalized(unit["text"]) not in body_text]
    required_pdf_missing = [value for value in [required["section"], *required["terms"], *required["formulas"]]
                            if normalized(value) not in body_text]
    expected_counter = Counter("".join(normalized(unit["text"]) for unit in units))
    actual_counter = Counter(body_text)
    # Repeated table headers, diagrams and generated labels may add printable
    # characters; they do not excuse a missing source unit checked above.
    character_shortfalls = [{"character": char, "source_occurrences": amount,
                             "pdf_occurrences": actual_counter[char]}
                            for char, amount in sorted(expected_counter.items()) if actual_counter[char] < amount]
    cover_version_ok = "海缆规划与敷设研究工作空间/0.9" in normalized(flow_pages[0])
    status = "passed" if (source_version_ok and cover_version_ok and not missing
                           and not required_source_missing and not required_pdf_missing
                           and not character_shortfalls and all(row["status"] == "passed" for row in pages)) else "failed"
    return {"kind": kind, "status": status, "pdf": pdf_info, "source": source_info,
            "pages": count, "page_checks": pages,
            "source_version_0_9_present": source_version_ok, "cover_version_0_9_present": cover_version_ok,
            "character_boundary_outliers": outliers, "unsupported_extraction_tokens": bad_characters,
            "source_coverage": {"checked_printable_units": len(units),
                                "unit_kinds": dict(Counter(unit["kind"] for unit in units)),
                                "missing_units": missing, "printable_character_shortfalls": character_shortfalls,
                                "diagram_substitutions_not_literal_code": substituted},
            "new_0_9_content": {"required_section": required["section"],
                               "required_fields_and_terms": required["terms"],
                               "required_formulas": required["formulas"],
                               "missing_from_source": required_source_missing,
                               "missing_from_pdf": required_pdf_missing}}


def report_target(path: Path) -> Path:
    path = path if path.is_absolute() else ROOT / path
    if (path.parent.resolve() != (ROOT / "resources/validation").resolve()
            or not re.fullmatch(r"development_0\.9_[A-Za-z0-9_.-]+\.json", path.name)
            or path.is_symlink()):
        raise ValueError("Report must be a non-symlink resources/validation/development_0.9_*.json")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual-pdf", type=Path, default=ROOT / "output/pdf/OceanRoute_用户手册_0.9.pdf")
    parser.add_argument("--design-pdf", type=Path, default=ROOT / "output/pdf/OceanRoute_设计文档_0.9.pdf")
    parser.add_argument("--date", default="2026-10-04", help="Exact generated footer date")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--verify-only", action="store_true", help="Check PDFs and print JSON without writing any file")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        parser.error("--date must use YYYY-MM-DD")
    target = report_target(args.report)
    if not args.verify_only and target.exists():
        raise FileExistsError(f"Refusing to overwrite existing QA evidence: {target}")
    started = datetime.now(timezone.utc)
    documents = [inspect_document("manual", args.manual_pdf, ROOT / "docs/USER_MANUAL.md", args.date),
                 inspect_document("design", args.design_pdf, ROOT / "docs/DESIGN.md", args.date)]
    report = {
        "version": "0.9.0", "status": "passed" if all(row["status"] == "passed" for row in documents) else "failed",
        "recorded_at_utc": started.isoformat(), "builder": descriptor(BUILDER),
        "validator": descriptor(Path(__file__)), "documents": documents,
        "output_written": not args.verify_only,
        "scope": "Actual dynamic page counts; nonblank body, exact header/footer/page numbers; 40pt horizontal, 16pt top and 15pt bottom character bounds; printable maintained-source units, new side-slope/KP fields and formulas.",
        "normalization": "Unicode NFC, layout whitespace, Markdown inline presentation, builder dash substitutions and explicit U+2080-to-subscript-ASCII-0 presentation. JSON parsed formatting is canonicalized. No NUL/replacement glyph stripping, ASCII/CJK/digit dropping or field-prefix-only matching.",
        "visual_review_required": True, "actual_pages_visually_reviewed_by_this_script": [],
        "limitations": ["Extraction cannot prove glyph shape, absence of overlapping text or table rules, screenshot correctness, contrast, orphan headings, excessive whitespace or pleasing pagination.",
                        "Mermaid source is replaced by an explicit generated diagram; literal Mermaid code coverage is excluded and reported.",
                        "Every final page requires actual rendered-image review. Automatic passed is not visual acceptance or engineering validation."],
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if not args.verify_only:
        # Exclusive creation also protects against a competing writer after the
        # preflight exists check. No force/overwrite option is provided.
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
    print(encoded, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())

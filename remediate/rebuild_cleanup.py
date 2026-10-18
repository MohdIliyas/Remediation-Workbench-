"""Clean up a Word file produced by pdf2docx before remediation.

pdf2docx reproduces the look of a PDF, but leaves problems that hurt screen
reader users:
  - missing spaces between words (common in LaTeX / pdfTeX PDFs, which
    position each word instead of typing a space)
  - page headers/footers ("Page 3 of 10") repeated inside the body
  - borderless "tables" that are really boxes, columns or code listings
  - lines of different sizes merged into one paragraph (a section title and
    the first question label, for example)
"""
from __future__ import annotations

import copy
import re
from collections import Counter

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

W_P, W_R, W_T, W_TBL = qn("w:p"), qn("w:r"), qn("w:t"), qn("w:tbl")
W_BR, W_TAB = qn("w:br"), qn("w:tab")


# ---------------------------------------------------------------- spaces

def _pdf_text(pdf_path: str) -> str:
    import pymupdf
    with pymupdf.open(pdf_path) as d:
        return "\n".join(page.get_text("text") for page in d)


def _space_map(text: str):
    """Text without whitespace, plus for each of its chars whether whitespace came right before it."""
    chars, before = [], []
    pending = False
    for ch in text:
        if ch.isspace():
            pending = True
            continue
        chars.append(ch)
        before.append(pending)
        pending = False
    return "".join(chars), before


def restore_spaces(doc, pdf_path: str) -> tuple[int, int]:
    """Insert the word spaces MuPDF sees in the PDF but pdf2docx dropped.

    Returns (paragraphs fixed, paragraphs that could not be matched)."""
    flat, space_before = _space_map(_pdf_text(pdf_path))
    pointer, fixed, unmatched = 0, 0, 0

    for p in doc.element.body.iter(W_P):
        t_nodes = [t for t in p.iter(W_T)]
        para_chars = "".join(ch for t in t_nodes for ch in (t.text or "") if not ch.isspace())
        if len(para_chars) < 2:
            continue
        pos = flat.find(para_chars, pointer, pointer + 200000)
        if pos < 0:
            pos = flat.find(para_chars)
        if pos < 0:
            unmatched += 1
            continue
        pointer = pos + len(para_chars)

        idx, changed = pos, False
        prev_char = ""
        # walk every text node in order; a tab or line break between nodes counts as a space
        for el in p.iter():
            if el.tag in (W_TAB, W_BR):
                prev_char = " "
                continue
            if el.tag != W_T or not el.text:
                continue
            out = []
            for ch in el.text:
                if ch.isspace():
                    out.append(ch)
                    prev_char = ch
                    continue
                if idx != pos and space_before[idx] and prev_char and not prev_char.isspace():
                    out.append(" ")
                    changed = True
                out.append(ch)
                prev_char = ch
                idx += 1
            new = "".join(out)
            if new != el.text:
                el.text = new
                el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        if changed:
            fixed += 1
    return fixed, unmatched


# ---------------------------------------------------------------- running headers / footers

PAGE_RE = re.compile(r"^\s*(page\s*)?\d+\s*(of|/)\s*\d+\s*$", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text)).strip().lower()


def remove_running_text(doc) -> int:
    """Drop page-number lines and short lines repeated on many pages."""
    body = doc.element.body
    paras = [p for p in body.iter(W_P)]
    texts = {p: "".join(t.text or "" for t in p.iter(W_T)).strip() for p in paras}
    counts = Counter(_norm(t) for t in texts.values() if 0 < len(t) < 120)
    pages = max(1, sum(1 for t in texts.values() if PAGE_RE.match(t)))
    removed = 0
    for p, text in texts.items():
        if not text:
            continue
        n = _norm(text)
        if PAGE_RE.match(text) or (pages >= 3 and len(text) < 120 and counts[n] >= max(3, pages - 1)):
            parent = p.getparent()
            parent.remove(p)
            removed += 1
            # a table cell must keep at least one paragraph
            if parent.tag == qn("w:tc") and parent.find(W_P) is None:
                parent.append(OxmlElement("w:p"))
    return removed


# ---------------------------------------------------------------- layout tables

def _has_visible_border(tbl) -> bool:
    for b in tbl.iter():
        tag = b.tag.split("}")[1] if "}" in b.tag else ""
        if tag in ("top", "bottom", "left", "right", "insideH", "insideV", "start", "end") \
                and b.getparent() is not None and b.getparent().tag in (qn("w:tcBorders"), qn("w:tblBorders")):
            if b.get(qn("w:val")) not in (None, "nil", "none") and b.get(qn("w:sz"), "1") != "0":
                return True
    return False


def _cell_fill(tc):
    shd = tc.find(f"{qn('w:tcPr')}/{qn('w:shd')}")
    fill = shd.get(qn("w:fill")) if shd is not None else None
    return None if fill in (None, "auto", "FFFFFF", "ffffff") else fill


def _shade(p, fill):
    if not fill:
        return
    pPr = p.find(qn("w:pPr"))
    if pPr is None:
        pPr = OxmlElement("w:pPr")
        p.insert(0, pPr)
    if pPr.find(qn("w:shd")) is None:
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill)
        # shd goes before spacing/ind/jc in pPr
        anchor = next((c for c in pPr if c.tag in (qn("w:tabs"), qn("w:spacing"), qn("w:ind"), qn("w:jc"),
                                                    qn("w:rPr"))), None)
        if anchor is not None:
            anchor.addprevious(shd)
        else:
            pPr.append(shd)


def _has_text(el) -> bool:
    return any((t.text or "").strip() for t in el.iter(W_T)) or el.find(f".//{qn('w:drawing')}") is not None


def unwrap_layout_tables(doc) -> int:
    """Replace borderless or single-row tables with plain paragraphs.

    One-cell rows keep their paragraphs; rows with several cells become one
    paragraph per row, cells separated by a tab (keeps code line numbers and
    answer choices on one line)."""
    body = doc.element.body
    count = 0
    for tbl in list(body.iter(W_TBL)):
        if tbl.getparent() is None:
            continue
        rows = tbl.findall(qn("w:tr"))
        if len(rows) > 1 and _has_visible_border(tbl):
            continue  # looks like a real data table
        new_paras = []
        for tr in rows:
            cells = [tc for tc in tr.findall(qn("w:tc")) if _has_text(tc)]
            if not cells:
                continue
            if len(cells) == 1:
                fill = _cell_fill(cells[0])
                for p in cells[0].findall(W_P):
                    if _has_text(p):
                        p = copy.deepcopy(p)
                        _shade(p, fill)
                        new_paras.append(p)
                continue
            merged = None
            for i, tc in enumerate(cells):
                for j, p in enumerate(q for q in tc.findall(W_P) if _has_text(q)):
                    if merged is None:
                        merged = copy.deepcopy(p)
                        for r in list(merged):
                            if r.tag != qn("w:pPr"):
                                merged.remove(r)
                    else:
                        sep = OxmlElement("w:r")
                        sep.append(OxmlElement("w:tab") if j == 0 else OxmlElement("w:t"))
                        if j:
                            sep[0].text = " "
                            sep[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                        merged.append(sep)
                    for child in p:
                        if child.tag != qn("w:pPr"):
                            merged.append(copy.deepcopy(child))
            if merged is not None:
                new_paras.append(merged)
        for p in new_paras:
            tbl.addprevious(p)
        tbl.getparent().remove(tbl)
        count += 1
    return count


# ---------------------------------------------------------------- merged lines

def _run_size(r) -> float | None:
    sz = r.find(f"{qn('w:rPr')}/{qn('w:sz')}")
    try:
        return int(sz.get(qn("w:val"))) / 2 if sz is not None else None
    except (TypeError, ValueError):
        return None


def _run_bold(r) -> bool:
    b = r.find(f"{qn('w:rPr')}/{qn('w:b')}")
    return b is not None and b.get(qn("w:val")) not in ("0", "false")


def split_mixed_lines(doc) -> int:
    """Split a paragraph at line breaks where the text size or weight changes."""
    splits = 0
    for p in list(doc.element.body.iter(W_P)):
        if p.find(f".//{qn('w:br')}") is None:
            continue
        # segments of runs; a run holding a plain line break ends its segment
        segments, current = [], []
        for child in list(p):
            if child.tag != W_R:
                continue
            current.append(child)
            br = child.find(W_BR)
            if br is not None and br.get(qn("w:type")) in (None, "textWrapping"):
                segments.append(current)
                current = []
        segments.append(current)
        segments = [seg for seg in segments if any(_has_text(r) for r in seg)]
        if len(segments) < 2:
            continue

        def style_of(seg):
            runs = [r for r in seg if _has_text(r)]
            return (max((_run_size(r) or 0) for r in runs), all(_run_bold(r) for r in runs))

        styles = [style_of(s) for s in segments]
        if all(abs(st[0] - styles[0][0]) < 1 and st[1] == styles[0][1] for st in styles):
            continue  # same look throughout: an ordinary wrapped paragraph
        # group consecutive segments with the same look
        groups = [[segments[0]]]
        for prev, seg, st, pst in zip(segments, segments[1:], styles[1:], styles):
            if abs(st[0] - pst[0]) < 1 and st[1] == pst[1]:
                groups[-1].append(seg)
            else:
                groups.append([seg])
        pPr = p.find(qn("w:pPr"))
        anchor = p
        for g_i, group in enumerate(groups):
            np_ = OxmlElement("w:p")
            if pPr is not None:
                np_.append(copy.deepcopy(pPr))
            for s_i, seg in enumerate(group):
                last_seg = s_i == len(group) - 1
                for r in seg:
                    if last_seg:  # the break that ended this group is now a paragraph end
                        for br in r.findall(W_BR):
                            if br.get(qn("w:type")) in (None, "textWrapping"):
                                r.remove(br)
                    np_.append(r)
            anchor.addnext(np_)
            anchor = np_
        p.getparent().remove(p)
        splits += len(groups) - 1
    return splits


# ---------------------------------------------------------------- entry point

def clean_converted_docx(docx_path: str, pdf_path: str) -> dict:
    doc = Document(docx_path)
    stats = {}
    stats["tables_unwrapped"] = unwrap_layout_tables(doc)
    stats["running_removed"] = remove_running_text(doc)
    stats["spaces_fixed"], stats["unmatched"] = restore_spaces(doc, pdf_path)
    stats["lines_split"] = split_mixed_lines(doc)
    doc.save(docx_path)
    return stats

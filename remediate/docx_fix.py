"""Word (.docx) remediation.

Fixes automatically:
  - document title and language
  - "fake" headings (bold/large Normal text) -> real Heading styles
  - skipped heading levels (H1 -> H3 becomes H1 -> H2)
  - typed bullet lists -> real list style (when the document has one)
  - table header rows marked to repeat / be announced as headers

Flags for a person:
  - images with missing, filename-like, or auto-generated alt text
    (drafts alt text first if a local vision model is configured)
  - vague or raw-URL link text, broken links
  - merged cells, possible layout tables, runs of empty paragraphs
"""
from __future__ import annotations

import os
import re
from collections import Counter

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from .preserve import set_style_keep_look
from .common import (FileReport, Options, check_urls, is_vague_link_text,
                     looks_like_raw_url)

BODY_STYLE_PREFIXES = ("normal", "body", "default", "plain", "no spacing")
BULLET_RE = re.compile(r"^\s*([•·▪■◦●○\-\*–]|o(?=\s))\s+")
NUMBER_RE = re.compile(r"^\s*(\d{1,2}|[a-z])[\.\)]\s+", re.I)
IMAGE_NAME_RE = re.compile(r"(\.(png|jpe?g|gif|bmp|emf|wmf|tiff?|svg)$)|^(picture|image|graphic)\s*\d*$", re.I)
AUTO_ALT_RE = re.compile(r"^(a picture containing|a close up of|a screenshot of|description automatically generated)", re.I)
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


# ---------------------------------------------------------------- helpers

def _style_name(p) -> str:
    try:
        return (p.style.name or "").strip()
    except Exception:
        return ""


def _heading_level(p) -> int | None:
    m = re.match(r"heading\s*(\d)", _style_name(p).lower())
    return int(m.group(1)) if m else None


def _default_size(doc) -> float:
    """Font size from docDefaults (w:sz is in half-points); Word's fallback is 10pt."""
    sz = doc.styles.element.find(f"{qn('w:docDefaults')}/{qn('w:rPrDefault')}/{qn('w:rPr')}/{qn('w:sz')}")
    try:
        return int(sz.get(qn("w:val"))) / 2 if sz is not None else 10.0
    except (TypeError, ValueError):
        return 10.0


def _run_size(run, para, default: float) -> float:
    if run.font.size:
        return run.font.size.pt
    st = para.style
    while st is not None:
        if st.font is not None and st.font.size:
            return st.font.size.pt
        st = st.base_style
    return default


def _run_bold(run, para) -> bool:
    if run.bold is not None:
        return bool(run.bold)
    st = para.style
    while st is not None:
        if st.font is not None and st.font.bold is not None:
            return bool(st.font.bold)
        st = st.base_style
    return False


def _heading_style(doc, level: int):
    name = f"Heading {level}"
    try:
        return doc.styles[name]
    except KeyError:
        st = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        st.base_style = doc.styles["Normal"]
        st.next_paragraph_style = doc.styles["Normal"]
        st.font.bold = True
        st.font.size = Pt({1: 16, 2: 14, 3: 12}.get(level, 12))
        pPr = st.element.get_or_add_pPr()
        ol = OxmlElement("w:outlineLvl")
        ol.set(qn("w:val"), str(level - 1))
        pPr.append(ol)
        return st


def _insert_in_order(parent, child, order: list[str]):
    """Insert child into parent respecting OOXML element order."""
    tag = child.tag
    idx = order.index(tag)
    later = set(order[idx + 1:])
    for i, existing in enumerate(parent):
        if existing.tag in later:
            parent.insert(i, child)
            return
    parent.append(child)


TRPR_ORDER = [qn(t) for t in (
    "w:cnfStyle", "w:divId", "w:gridBefore", "w:gridAfter", "w:wBefore", "w:wAfter",
    "w:cantSplit", "w:trHeight", "w:tblHeader", "w:tblCellSpacing", "w:jc",
    "w:hidden", "w:ins", "w:del", "w:trPrChange")]


def _snippet(text: str, n: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------- fixes

def fix_language(doc, rep: FileReport, lang: str):
    styles = doc.styles.element
    dd = styles.find(qn("w:docDefaults"))
    if dd is None:
        dd = OxmlElement("w:docDefaults")
        styles.insert(0, dd)
    rpd = dd.find(qn("w:rPrDefault"))
    if rpd is None:
        rpd = OxmlElement("w:rPrDefault")
        dd.insert(0, rpd)
    rpr = rpd.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        rpd.append(rpr)
    el = rpr.find(qn("w:lang"))
    if el is None or not el.get(qn("w:val")):
        if el is None:
            el = OxmlElement("w:lang")
            rpr.append(el)
        el.set(qn("w:val"), lang)
        rep.fix(f"Set document language to {lang}")


def fix_fake_headings(doc, rep: FileReport):
    paras = doc.paragraphs
    default = _default_size(doc)
    # body text size = most common size by character count
    sizes = Counter()
    for p in paras:
        for r in p.runs:
            s = _run_size(r, p, default)
            if s and r.text.strip():
                sizes[s] += len(r.text)
    body = sizes.most_common(1)[0][0] if sizes else default

    candidates = []
    for i, p in enumerate(paras):
        text = p.text.strip()
        if not text or _heading_level(p) or _style_name(p).lower() == "title":
            continue
        if not _style_name(p).lower().startswith(BODY_STYLE_PREFIXES):
            continue
        if p._p.pPr is not None and p._p.pPr.numPr is not None:
            continue
        if len(text) > 120 or len(text.split()) > 15 or text.endswith((".", ",", ";")):
            continue
        if BULLET_RE.match(text) or NUMBER_RE.match(text) or re.fullmatch(r"[\d\W]+", text):
            continue
        runs = [r for r in p.runs if r.text.strip()]
        if not runs:
            continue
        all_bold = all(_run_bold(r, p) for r in runs)
        size = max(_run_size(r, p, default) for r in runs)
        if (all_bold and size >= body) or size >= body + 2:
            # skip if the next paragraph is also bold-short text (likely a bold list)
            candidates.append((i, p, size))

    if not candidates:
        return

    has_h1 = any(_heading_level(p) == 1 or _style_name(p).lower() == "title" for p in paras)
    ranks = sorted({round(s) for _, _, s in candidates}, reverse=True)
    start = 2 if has_h1 else 1
    count = 0
    for i, p, size in candidates:
        level = min(start + ranks.index(round(size)), 3)
        set_style_keep_look(doc, p, _heading_style(doc, level))
        count += 1
        rep.fix(f"Converted visual heading to Heading {level}", _snippet(p.text))
    if count:
        rep.flag(f"{count} paragraph(s) were converted to headings based on formatting. "
                 "Confirm each is really a heading and the levels make sense.")


def fix_heading_order(doc, rep: FileReport):
    prev = 0
    for p in doc.paragraphs:
        lvl = _heading_level(p)
        if not lvl:
            continue
        if lvl > prev + 1:
            new = prev + 1
            set_style_keep_look(doc, p, _heading_style(doc, new))
            rep.fix(f"Heading level {lvl} changed to {new} (no skipped levels)", _snippet(p.text))
            lvl = new
        prev = lvl
    if prev == 0:
        # no headings at all: the first short line becomes Heading 1, looking exactly as before
        first = next((p for p in doc.paragraphs if p.text.strip() and len(p.text.strip()) <= 150), None)
        if first is not None:
            set_style_keep_look(doc, first, _heading_style(doc, 1))
            rep.fix("Document had no headings: made its first line Heading 1 (it looks the same)",
                    _snippet(first.text))
            rep.flag("Only the first line was made a heading. If the document has sections, mark their "
                     "titles as headings too.")
        else:
            rep.flag("Document has no headings. Add headings if it has sections.")


def fix_typed_lists(doc, rep: FileReport, convert: bool = False):
    styles = {s.name for s in doc.styles if s.type == WD_STYLE_TYPE.PARAGRAPH}
    bullet_style = "List Bullet" if convert and "List Bullet" in styles else None
    fixed = flagged = 0
    for p in doc.paragraphs:
        if p._p.pPr is not None and p._p.pPr.numPr is not None:
            continue
        text = p.text
        m = BULLET_RE.match(text)
        if m and bullet_style:
            # strip the typed bullet from the first run(s)
            remove = len(m.group(0))
            for r in p.runs:
                if remove <= 0:
                    break
                take = min(remove, len(r.text))
                r.text = r.text[take:]
                remove -= take
            p.style = doc.styles[bullet_style]
            fixed += 1
        elif m or NUMBER_RE.match(text):
            flagged += 1  # left untouched so the layout doesn't change
    if fixed:
        rep.fix(f"Converted {fixed} typed bullet(s) to a real bulleted list")
    if flagged:
        rep.flag(f"{flagged} list item(s) are typed by hand (e.g. '1.' or '-'). "
                 "Apply Word's list formatting so screen readers announce them as a list.")


def fix_tables(doc, rep: FileReport):
    for n, table in enumerate(doc.tables, 1):
        where = f"Table {n}"
        rows, cols = len(table.rows), len(table.columns)
        tbl = table._tbl
        if rows <= 1 or cols <= 1:
            rep.flag("May be a layout table (only one row or column). If it only positions content, "
                     "remove the table; if it holds data, add a header row.", where)
            continue
        first = table.rows[0]
        if not any(c.text.strip() for c in first.cells):
            rep.flag("First row is empty; add column headers.", where)
            continue
        trPr = first._tr.get_or_add_trPr()
        if trPr.find(qn("w:tblHeader")) is None:
            _insert_in_order(trPr, OxmlElement("w:tblHeader"), TRPR_ORDER)
            rep.fix("Marked first row as the header row", where)
            rep.flag("Confirm the first row holds column headers.", where)
        if tbl.findall(".//" + qn("w:gridSpan")) or tbl.findall(".//" + qn("w:vMerge")):
            rep.flag("Has merged cells. Simplify the table or check it with a screen reader.", where)
        empties = [c for c in first.cells if not c.text.strip()]
        if empties:
            rep.flag(f"{len(empties)} header cell(s) are empty.", where)


W_P_TAG = qn("w:p")


def _para_text(p_el) -> str:
    return "".join(t.text or "" for t in p_el.iter(qn("w:t"))).strip()


def image_context(docpr) -> str:
    """Caption and nearby paragraphs around an image, for the alt-text model."""
    p = docpr
    while p is not None and p.tag != W_P_TAG:
        p = p.getparent()
    if p is None:
        return ""
    before, after = [], []
    sib = p.getprevious()
    while sib is not None and len(before) < 2:
        if sib.tag == W_P_TAG and _para_text(sib):
            before.insert(0, _para_text(sib))
        sib = sib.getprevious()
    sib = p.getnext()
    while sib is not None and len(after) < 2:
        if sib.tag == W_P_TAG and _para_text(sib):
            after.append(_para_text(sib))
        sib = sib.getnext()
    own = _para_text(p)
    return " ".join(x for x in (*before, own, *after) if x)[:1500]


def image_bytes_for(doc, docpr) -> bytes | None:
    holder = docpr.getparent()
    blip = holder.find(f".//{{{A_NS}}}blip") if holder is not None else None
    rid = blip.get(f"{{{R_NS}}}embed") if blip is not None else None
    if rid and rid in doc.part.related_parts:
        return doc.part.related_parts[rid].blob
    return None


def is_decorative(docpr) -> bool:
    return any("decorative" in (el.tag or "").lower() for el in docpr.iter())


def check_images(doc, rep: FileReport, opts: Options, collector=None, out_name: str = ""):
    """Flag images needing alt text; with a local model, draft it and queue it for review."""
    from .alt_apply import _set_decorative
    drafted = 0
    behind = 0
    for docpr in doc.element.body.iter(f"{{{WP_NS}}}docPr"):
        anchor = docpr.getparent()
        # a picture placed behind the text is a background: screen readers should read the text
        if anchor is not None and anchor.tag == f"{{{WP_NS}}}anchor" and anchor.get("behindDoc") == "1" \
                and not is_decorative(docpr):
            docpr.set("descr", "")
            _set_decorative(docpr, True)
            behind += 1
    if behind:
        rep.fix(f"Marked {behind} picture(s) placed behind the text as decorative backgrounds")
    for n, docpr in enumerate(doc.element.body.iter(f"{{{WP_NS}}}docPr"), 1):
        if is_decorative(docpr):
            continue
        descr = (docpr.get("descr") or "").strip()
        if descr and not IMAGE_NAME_RE.search(descr) and not AUTO_ALT_RE.match(descr):
            continue
        reason = ("alt text is Word's automatic description" if AUTO_ALT_RE.match(descr)
                  else "alt text is a file name" if descr else "no alt text")
        where = f"Image {n}"
        if collector is None:
            rep.flag(f"Needs alt text ({reason}).", where)
            continue
        item = collector.add(image_bytes_for(doc, docpr),
                             {"kind": "docx", "file": out_name, "index": n},
                             image_context(docpr), where, existing=descr)
        if item["draft"] and not item["decorative_suggested"]:
            docpr.set("descr", item["draft"])  # better than nothing until a person reviews it
            drafted += 1
    if collector is not None and collector.items:
        ai = " (drafts written in, marked unreviewed)" if drafted else ""
        rep.flag(f"{len(collector.items)} image(s) need alt text{ai}. Use the alt-text review page.")
    if drafted:
        rep.fix(f"Drafted alt text for {drafted} image(s) with the local model")


def check_links(doc, rep: FileReport, opts: Options):
    urls = []
    for hl in doc.element.body.iter(qn("w:hyperlink")):
        text = "".join(t.text or "" for t in hl.iter(qn("w:t"))).strip()
        rid = hl.get(qn("r:id"))
        url = doc.part.rels[rid].target_ref if rid and rid in doc.part.rels else None
        if not url:
            continue
        urls.append(url)
        if is_vague_link_text(text):
            rep.flag(f"Link text \u201c{text}\u201d doesn't say where it goes. Rewrite it to describe the destination.", url)
        elif looks_like_raw_url(text) and len(text) > 40:
            rep.flag("Link text is a long raw URL. Replace it with a descriptive name.", _snippet(text))
    if opts.check_links and urls:
        for url, problem in check_urls(urls).items():
            rep.flag(f"Broken link: {problem}.", url)


def check_empty_paragraphs(doc, rep: FileReport):
    run_len = worst = 0
    for p in doc.paragraphs:
        if not p.text.strip() and not p._p.findall(".//" + qn("w:drawing")):
            run_len += 1
            worst = max(worst, run_len)
        else:
            run_len = 0
    if worst >= 3:
        rep.flag(f"Found {worst} empty paragraphs in a row used for spacing. "
                 "Screen readers announce each as 'blank'; use paragraph spacing instead.")


def fix_title(doc, rep: FileReport, fallback: str):
    cp = doc.core_properties
    if cp.title and cp.title.strip():
        return
    title = None
    for p in doc.paragraphs:
        if (_heading_level(p) == 1 or _style_name(p).lower() == "title") and p.text.strip():
            title = p.text.strip()
            break
    if not title:
        title = re.sub(r"[_\-]+", " ", fallback).strip().title()
        rep.flag("Title was taken from the file name. Check it reads well.", title)
    cp.title = " ".join(title.split())[:200]
    rep.fix("Set document title", cp.title)


# ---------------------------------------------------------------- entry point

def remediate_docx(src: str, dst: str, rep: FileReport, opts: Options, title_hint: str | None = None,
                   collector=None):
    doc = Document(src)
    fallback = title_hint or os.path.splitext(os.path.basename(src))[0]
    fix_language(doc, rep, opts.lang)
    fix_fake_headings(doc, rep)
    fix_heading_order(doc, rep)
    fix_title(doc, rep, fallback)
    fix_typed_lists(doc, rep, convert=opts.convert_lists)
    fix_tables(doc, rep)
    check_images(doc, rep, opts, collector, os.path.basename(dst))
    check_links(doc, rep, opts)
    check_empty_paragraphs(doc, rep)
    if opts.fix_contrast:
        from .contrast import fix_docx_contrast
        c = fix_docx_contrast(doc)
        if c["fixed"]:
            rep.fix(f"Adjusted {c['fixed']} low-contrast colour(s) in text and styles to meet WCAG contrast",
                    "; ".join(c["examples"]))
        if c["left"]:
            rep.flag(f"{c['left']} text run(s) still have low contrast and couldn't be adjusted safely.")
    from .logic_check import docx_logic
    try:
        docx_logic(doc, rep)
    except Exception as e:
        rep.errors.append(f"Logic check didn't finish: {e}")
    doc.save(dst)
    return dst

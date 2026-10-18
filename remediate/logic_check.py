"""Final logic check, run on every remediated file.

Fixes what is safe to fix automatically and lists the rest, so every output is logical as a whole:
  PDF:   headings start at H1 and never skip a level; layout "tables" (one row or one column) are
         retagged as text; data tables without header cells get their first row as headers;
         figures without meaningful alt text; reading order that jumps around the page;
         form fields without labels.
  Word:  bullet and number colours follow the text; layout tables.
  PowerPoint: bullet colours follow the text; video and audio need captions / transcripts.
"""
from __future__ import annotations

import re

import pikepdf
from pikepdf import Name

from .common import FileReport

BAD_ALT = re.compile(r"^\s*(image|picture|photo|graphic|figure|img)?\s*\d*\s*$|\.(png|jpe?g|gif|bmp|tiff?|emf|wmf)\s*$",
                     re.I)


# ---------------------------------------------------------------- PDF

def _walk(node, visit, seen=None):
    seen = set() if seen is None else seen
    if isinstance(node, pikepdf.Array):
        for k in node:
            _walk(k, visit, seen)
        return
    if not isinstance(node, pikepdf.Dictionary):
        return
    if node.objgen != (0, 0):
        if node.objgen in seen:
            return
        seen.add(node.objgen)
    if "/S" in node:
        visit(node)
    _walk(node.get("/K"), visit, seen)


def _kids(el):
    k = el.get("/K")
    k = k if isinstance(k, pikepdf.Array) else [k]
    return [x for x in k if isinstance(x, pikepdf.Dictionary) and "/S" in x]


def _role(root):
    rolemap = root.get("/RoleMap")

    def resolve(name):
        seen, cur = set(), str(name)
        while rolemap is not None and cur in rolemap and cur not in seen:
            seen.add(cur)
            cur = str(rolemap[cur])
        return cur
    return resolve


def _mcid_text(path: str) -> dict:
    """{(page index, MCID): {"text", "size", "bold", "coloured"}} read from the page content."""
    from pdfminer.converter import PDFPageAggregator
    from pdfminer.layout import LTChar
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage

    class Dev(PDFPageAggregator):
        def __init__(self, r):
            super().__init__(r, laparams=None)
            self.stack, self.chars = [], []

        def begin_tag(self, tag, props=None):
            m = None
            try:
                if isinstance(props, dict) and "MCID" in props:
                    m = int(props["MCID"])
            except Exception:
                m = None
            self.stack.append(m)

        def end_tag(self):
            if self.stack:
                self.stack.pop()

        def render_char(self, *a, **k):
            adv = super().render_char(*a, **k)
            item = self.cur_item._objs[-1] if self.cur_item._objs else None
            mcid = next((m for m in reversed(self.stack) if m is not None), None)
            if isinstance(item, LTChar) and mcid is not None:
                self.chars.append((mcid, item))
            return adv

    def colour_of(c):
        try:
            col = c.graphicstate.ncolor
            if isinstance(col, (list, tuple)) and len(col) == 3:
                return max(col) - min(col) > 0.15  # a real hue, not black or grey
            if isinstance(col, (list, tuple)) and len(col) == 4:
                return max(col[:3]) - min(col[:3]) > 0.15
        except Exception:
            pass
        return False

    out = {}
    r = PDFResourceManager()
    dev = Dev(r)
    interp = PDFPageInterpreter(r, dev)
    with open(path, "rb") as fh:
        for pno, page in enumerate(PDFPage.get_pages(fh)):
            dev.stack, dev.chars = [], []
            interp.process_page(page)
            by = {}
            for m, c in dev.chars:
                by.setdefault(m, []).append(c)
            for m, cs in by.items():
                vis = [c for c in cs if c.get_text().strip()]
                if not vis:
                    continue
                names = [c.fontname.lower() for c in vis]
                out[(pno, m)] = {
                    "text": "".join(c.get_text() for c in cs).strip(),
                    "size": round(max(c.size for c in vis), 1),
                    "bold": sum(any(k in n for k in ("bold", "black", "heavy", "semibold")) for n in names) > len(vis) / 2,
                    "coloured": sum(colour_of(c) for c in vis) > len(vis) / 2,
                }
    return out


def _infer_headings(pdf, path: str, elems: list, role, rep: FileReport) -> int:
    """A tagged PDF with no heading tags at all (every line a paragraph): find the lines that look like
    headings from their text styling and retag them. Nothing on the page changes."""
    from collections import Counter
    page_of = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
    info = _mcid_text(path)
    paras = []
    for el in elems:
        if role(el.S) != "/P":
            continue
        pg = el.get("/Pg")
        kids = el.get("/K")
        kids = kids if isinstance(kids, pikepdf.Array) else [kids]
        parts = []
        for k in kids:
            if isinstance(k, pikepdf.Dictionary) and "/MCID" in k:
                kp = k.get("/Pg", pg)
                if kp is not None and kp.objgen in page_of:
                    parts.append(info.get((page_of[kp.objgen], int(k.MCID))))
            elif k is not None and not isinstance(k, pikepdf.Dictionary) and pg is not None and pg.objgen in page_of:
                try:
                    parts.append(info.get((page_of[pg.objgen], int(k))))
                except (TypeError, ValueError):
                    pass
        parts = [x for x in parts if x]
        if not parts:
            continue
        text = " ".join(x["text"] for x in parts).strip()
        paras.append({"el": el, "text": text, "page": page_of.get(pg.objgen, 0) if pg is not None else 0,
                      "size": max(x["size"] for x in parts),
                      "bold": all(x["bold"] for x in parts), "coloured": all(x["coloured"] for x in parts)})
    if not paras:
        return 0
    weights = Counter()
    for p in paras:
        weights[p["size"]] += len(p["text"])
    body = weights.most_common(1)[0][0]
    heads = []
    for p in paras:
        t = p["text"]
        words = len(t.split())
        if not t or not t[0].isalnum() or words > 12 or len(t) > 90 or t.endswith((".", ",", ";", ":")) or ":" in t:
            continue
        if p["size"] >= body * 1.15 or (p["bold"] and p["coloured"]) or (p["bold"] and words <= 8):
            heads.append(p)
    if not heads:
        return 0
    # levels: the first heading on page 1, when it opens the document and its style is used only
    # once, is the title (H1); the remaining styles rank by size (coloured above plain bold of the same
    # size) from H2 down to H4
    key = lambda p: (p["size"], p["coloured"])
    title = None
    first = heads[0]
    if first["page"] == 0 and sum(1 for p in heads if key(p) == key(first)) == 1:
        title = first
    keys = sorted({key(p) for p in heads if p is not title}, key=lambda k: (k[0], k[1]), reverse=True)
    level_of = {k: min(i + 2, 4) for i, k in enumerate(keys)}
    for p in heads:
        p["el"].S = Name.H1 if p is title else Name(f"/H{level_of[key(p)]}")
    rep.fix(f"Tagged {len(heads)} heading(s) the PDF showed visually (larger, bold or coloured lines) "
            "but had tagged as plain paragraphs")
    rep.flag("Headings were found from the text styling of an already-tagged PDF. Check the heading list "
             "(Acrobat: Tags panel or the Navigation pane) and the levels.")
    return len(heads)


def pdf_logic(path: str, rep: FileReport):
    try:
        pdf = pikepdf.open(path, allow_overwriting_input=True)
    except Exception:
        return
    changed = False
    with pdf:
        root = pdf.Root.get("/StructTreeRoot")
        if root is not None:
            role = _role(root)
            elems = []
            _walk(root.get("/K"), elems.append)

            # headings: the team's procedure. If the outline doesn't start with H1, add an H1 at the
            # start of the tag tree (no visible change; it carries the document title for screen
            # readers), keep the existing headings, then make sure no later heading skips a level.
            if not any(re.fullmatch(r"/H[1-6]?", role(el.S)) for el in elems):
                try:
                    if _infer_headings(pdf, path, elems, role, rep):
                        changed = True
                except Exception as e:
                    rep.errors.append(f"Couldn't look for headings: {e}")
            headings = [el for el in elems if re.fullmatch(r"/H([1-6])", role(el.S))]
            prev, fixed_h, added_h1 = 0, 0, False
            # no headings at all (for example a handwritten scan the OCR can't read): the team's
            # procedure is an H1 at the start of the tag structure; it carries the document title
            if not headings or role(headings[0].S) != "/H1":
                title = str(pdf.docinfo.get("/Title", "") or "").strip()
                try:
                    if not title and "/Metadata" in pdf.Root:
                        with pdf.open_metadata() as meta:
                            title = str(meta.get("dc:title", "") or "").strip()
                except Exception:
                    pass
                kids = root.get("/K")
                holder = root
                first = kids[0] if isinstance(kids, pikepdf.Array) and len(kids) else kids
                if isinstance(first, pikepdf.Dictionary) and role(first.get("/S", "")) in ("/Document", "/Part", "/Sect", "/Div"):
                    holder = first
                h1 = pdf.make_indirect(pikepdf.Dictionary(Type=Name.StructElem, S=Name.H1, P=holder,
                                                          ActualText=pikepdf.String(title or "Document")))
                hk = holder.get("/K")
                if isinstance(hk, pikepdf.Array):
                    hk.insert(0, h1)
                elif hk is None:
                    holder.K = pikepdf.Array([h1])
                else:
                    holder.K = pikepdf.Array([h1, hk])
                added_h1, prev = True, 1
            for el in headings:
                level = int(role(el.S)[2])
                new = 1 if prev == 0 else min(level, prev + 1)
                if new != level:
                    el.S = Name(f"/H{new}")
                    fixed_h += 1
                prev = new
            if added_h1:
                changed = True
                rep.fix("Added a Heading 1 at the start of the tag structure (the document "
                        + ("had no headings" if not headings else "didn't start with H1")
                        + "); the page looks the same")
            if fixed_h:
                changed = True
                rep.fix(f"Made the heading outline logical: no heading skips a level ({fixed_h} heading(s) adjusted)")

            # tables
            layout, headed = 0, 0
            for el in elems:
                if role(el.S) != "/Table":
                    continue
                rows = []

                def collect(e):
                    for k in _kids(e):
                        rk = role(k.S)
                        if rk == "/TR":
                            rows.append(k)
                        elif rk in ("/THead", "/TBody", "/TFoot"):
                            collect(k)
                collect(el)
                cells = [[c for c in _kids(r) if role(c.S) in ("/TD", "/TH")] for r in rows]
                widest = max((len(c) for c in cells), default=0)
                flat = [c for row in cells for c in row]

                def holds_blocks(c):
                    found = []
                    _walk(c.get("/K"), lambda e: found.append(role(e.S)))
                    return any(re.fullmatch(r"/H[1-6]|/L|/Figure|/Table|/Form", r) for r in found)

                def is_empty(c):
                    k = c.get("/K")
                    return k is None or (isinstance(k, pikepdf.Array) and len(k) == 0)
                layout_like = (len(rows) <= 1 or widest <= 1
                               or (flat and sum(holds_blocks(c) for c in flat) >= max(1, len(flat) // 3))
                               or (len(flat) >= 4 and sum(is_empty(c) for c in flat) > len(flat) / 2))
                if layout_like:
                    # used for layout, not data: read it as ordinary text
                    el.S = Name.Div
                    for r in rows:
                        r.S = Name.Div
                    for row in cells:
                        for c in row:
                            # a cell that holds paragraphs, lists or figures becomes a group (Div);
                            # a cell with just text becomes a paragraph
                            c.S = Name.Div if _kids(c) else Name.P
                            if "/A" in c:
                                del c["/A"]
                    for k in _kids(el):
                        if role(k.S) in ("/THead", "/TBody", "/TFoot"):
                            k.S = Name.Div
                    layout += 1
                    continue
                if not any(role(c.S) == "/TH" for row in cells for c in row) and cells and cells[0]:
                    for c in cells[0]:
                        c.S = Name.TH
                        c.A = pikepdf.Dictionary(O=Name.Table, Scope=Name.Column)
                    headed += 1
            if layout:
                changed = True
                rep.fix(f"Removed table tags from {layout} layout table(s) that hold no real data "
                        "(one row or column, headings or lists inside cells, or mostly empty cells); "
                        "their content is now tagged as paragraphs and groups")
            if headed:
                changed = True
                rep.fix(f"Marked the first row as header cells in {headed} table(s) that had none")
                rep.flag(f"{headed} table(s) got their first row as headers automatically. Check that the first "
                         "row really holds the column headings.")

            # figures: meaningful alt text or nothing (artifact)
            weak = 0
            for el in elems:
                if role(el.S) == "/Figure":
                    alt = str(el.get("/Alt", "") or el.get("/ActualText", "") or "")
                    if not alt.strip() or BAD_ALT.search(alt.strip()):
                        weak += 1
            if weak:
                rep.flag(f"{weak} figure(s) still have no meaningful alt text (empty, \"image\", or a file "
                         "name). Use the alt-text review page or mark them decorative.")

        # form fields need labels
        acro = pdf.Root.get("/AcroForm")
        if acro is not None:
            fields, unlabeled = 0, 0

            def walk_fields(arr):
                nonlocal fields, unlabeled
                for f in arr or []:
                    if "/Kids" in f and "/FT" not in f:
                        walk_fields(f.Kids)
                        continue
                    fields += 1
                    if not str(f.get("/TU", "")).strip():
                        unlabeled += 1
            walk_fields(acro.get("/Fields"))
            if unlabeled:
                rep.flag(f"{unlabeled} of {fields} form field(s) have no label (tooltip) for screen readers. "
                         "Add one in Acrobat (Prepare Form > field Properties > Tooltip).")
        if changed:
            pdf.save(path)
    try:
        _reading_order(path, rep)
    except Exception:
        pass


def _reading_order(path: str, rep: FileReport):
    """Pages whose text is drawn (and so tagged / read) out of visual order."""
    import pymupdf
    odd = []
    with pymupdf.open(path) as doc:
        for pno, page in enumerate(doc, 1):
            raw = [b for b in page.get_text("blocks", sort=False) if b[4].strip() and b[6] == 0]
            if len(raw) < 6:
                continue
            # measure as the page is shown (scans are often stored sideways and displayed upright)
            m = page.rotation_matrix
            blocks = []
            for b in raw:
                r = pymupdf.Rect(b[:4]) * m
                blocks.append((r.x0, r.y0, r.x1, r.y1))
            jumps = 0
            for a, b in zip(blocks, blocks[1:]):
                up = a[1] - b[1]
                new_column = b[0] > a[2] - 5  # moved right: a new column starting at the top is normal
                if up > 40 and not new_column:
                    jumps += 1
            if jumps >= 3 and jumps > len(blocks) * 0.15:
                odd.append(pno)
    if odd:
        pages = ", ".join(map(str, odd[:15])) + ("…" if len(odd) > 15 else "")
        rep.flag(f"Reading order may jump around on page(s) {pages}: the text isn't stored top to bottom. "
                 "Check those pages with a screen reader or Acrobat's Reading Order tool.")


# ---------------------------------------------------------------- Word

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_logic(doc, rep: FileReport):
    # bullets and numbers in an odd colour: let them follow the text colour
    recoloured = 0
    try:
        numbering = doc.part.numbering_part.element
    except Exception:
        numbering = None
    if numbering is not None:
        for lvl in numbering.iter(f"{W}lvl"):
            rpr = lvl.find(f"{W}rPr")
            if rpr is None:
                continue
            col = rpr.find(f"{W}color")
            if col is not None and (col.get(f"{W}val") or "auto").lower() not in ("auto", "000000"):
                rpr.remove(col)
                recoloured += 1
    pictures = 0
    if numbering is not None:
        for lvl in numbering.iter(f"{W}lvl"):
            pic = lvl.find(f"{W}lvlPicBulletId")
            if pic is not None:
                lvl.remove(pic)
                txt = lvl.find(f"{W}lvlText")
                if txt is not None:
                    txt.set(f"{W}val", "\u2022")
                pictures += 1
    if recoloured:
        rep.fix(f"Bullets and numbers now follow the text colour ({recoloured} list level(s) had their own colour)")
    if pictures:
        rep.fix(f"Replaced {pictures} picture bullet(s) with a standard bullet")


# ---------------------------------------------------------------- PowerPoint

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _plain_bullets(root) -> tuple[int, int]:
    """Bullet colours removed (bullets follow the text colour) and picture bullets replaced."""
    from lxml import etree as ET
    recoloured = pictures = 0
    for el in list(root.iter(f"{A}buClr")):
        el.getparent().remove(el)
        recoloured += 1
    for blip in list(root.iter(f"{A}buBlip")):
        parent = blip.getparent()
        font = ET.Element(f"{A}buFont", typeface="Arial")
        char = ET.Element(f"{A}buChar", char="\u2022")
        parent.replace(blip, char)
        char.addprevious(font)
        pictures += 1
    return recoloured, pictures


def pptx_logic(prs, rep: FileReport):
    recoloured = pictures = 0
    media = []
    for n, slide in enumerate(prs.slides, 1):
        r, p = _plain_bullets(slide.shapes._spTree)
        recoloured += r
        pictures += p
        xml = slide.shapes._spTree.xml
        if "videoFile" in xml or "audioFile" in xml or "p14:media" in xml:
            media.append(n)
    # bullets inherited from the theme: slide masters, their layouts, and the deck defaults
    themed = 0
    for master in prs.slide_masters:
        r, p = _plain_bullets(master._element)
        themed += r + p
        for layout in master.slide_layouts:
            r, p = _plain_bullets(layout._element)
            themed += r + p
    try:
        r, p = _plain_bullets(prs.part._element)
        themed += r + p
    except Exception:
        pass
    if recoloured or pictures:
        rep.fix(f"Bullets now follow the text colour ({recoloured} bullet colour(s) removed"
                + (f", {pictures} picture bullet(s) replaced with a standard bullet" if pictures else "") + ")")
    if themed:
        rep.fix(f"Fixed {themed} bullet style(s) inherited from the theme (slide master and layouts): standard "
                "bullets in the text colour")
    if media:
        rep.flag(f"Video or audio on slide(s) {', '.join(map(str, media))}: videos need captions and audio "
                 "needs a transcript. The tool can't create these; add them before posting.")

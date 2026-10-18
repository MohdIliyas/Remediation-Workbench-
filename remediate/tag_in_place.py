"""Tag an untagged digital PDF in place.

Nothing is redrawn, so the pages look exactly as before. The page content is
interpreted (pdfminer) to learn what each text object says and where it sits;
then the original drawing instructions are wrapped in marked content and a tag
tree is built:
  - text objects  -> H1-H3 / P / L-LI-LBody, grouped into paragraphs, in content order
  - images        -> Figure (then queued for alt text like any tagged PDF)
  - shapes, lines, backgrounds, running heads/feet, page numbers -> Artifact
Where the PDF has no real space characters (common with LaTeX), each element gets
an ActualText with spaces restored, so screen readers don't run words together.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator, String

PATH_START = {"m", "re"}
PATH_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
LIST_RE = re.compile(r"^\s*(?:[•·▪■◦●○‣\-–*]|\(?\d{1,2}[.)]|\(?[a-zA-Z][.)]|\(?[ivxIVX]{1,4}[.)])\s+\S")
SYMBOL_ONLY = re.compile(r"^\s*[•·▪■◦●○‣\-–*]\s*$")
PRESENTATION_HINTS = ("powerpoint", "keynote", "impress", "google slides", "presentation")


# ---------------------------------------------------------------- reading the content

def _font_weights(path: str) -> dict:
    """{font name: weight} from the font descriptors. Some producers (Chrome) name every font
    "Regular" and only record the real weight (700 = bold) in the descriptor."""
    weights = {}
    try:
        with pikepdf.open(path) as pdf:
            seen = set()
            for page in pdf.pages:
                res = page.obj.get("/Resources")
                fonts = res.get("/Font") if res is not None else None
                for _, f in (fonts.items() if fonts is not None else []):
                    if f.objgen in seen:
                        continue
                    seen.add(f.objgen)
                    cands = [f] + list(f.get("/DescendantFonts", []) or [])
                    for c in cands:
                        fd = c.get("/FontDescriptor")
                        if fd is None:
                            continue
                        w = fd.get("/FontWeight")
                        if w is None:
                            continue
                        for nm in (fd.get("/FontName"), c.get("/BaseFont"), f.get("/BaseFont")):
                            if nm is not None:
                                weights[str(nm).lstrip("/")] = float(w)
    except Exception:
        pass
    return weights


def _read_pages(path: str):
    """Per page: text objects (in content order) and page-level images, via pdfminer."""
    from pdfminer.converter import PDFPageAggregator
    from pdfminer.layout import LTChar
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage

    class Recorder(PDFPageAggregator):
        def __init__(self, rsrc):
            super().__init__(rsrc, laparams=None)
            self.reset()

        def reset(self):
            self.bt, self.do, self.depth, self.show = -1, -1, 0, -1
            self.chars, self.images = [], []

        def begin_figure(self, name, bbox, matrix):
            self.depth += 1
            super().begin_figure(name, bbox, matrix)

        def end_figure(self, name):
            super().end_figure(name)
            self.depth -= 1

        def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
            adv = super().render_char(matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate)
            item = self.cur_item._objs[-1] if self.cur_item._objs else None
            if isinstance(item, LTChar):
                self.chars.append((self.show if self.depth == 0 else None, item))
            return adv

    class Interp(PDFPageInterpreter):
        def _shown(self):
            if self.device.depth == 0:
                self.device.show += 1

        # pdfminer runs Tj, ' and " through do_TJ, so counting there counts every text-showing
        # operator exactly once (counting in do_Tj too would count Tj twice)
        def do_TJ(self, seq):
            self._shown()
            super().do_TJ(seq)

        def do_Do(self, xobjid):
            if self.device.depth == 0:
                self.device.do += 1
                a, b, c, d, e, f = self.ctm
                xs = [e, a + e, c + e, a + c + e]
                ys = [f, b + f, d + f, b + d + f]
                self.device.images.append((self.device.do, (min(xs), min(ys), max(xs), max(ys))))
            super().do_Do(xobjid)

    weights = _font_weights(path)
    rsrc = PDFResourceManager()
    dev = Recorder(rsrc)
    interp = Interp(rsrc, dev)
    pages = []
    with open(path, "rb") as fh:
        for page in PDFPage.get_pages(fh):
            dev.reset()
            interp.process_page(page)
            x0, y0, x1, y1 = page.mediabox
            # group text-show operators into visual lines (LaTeX packs many lines in one BT block,
            # PowerPoint one line per block; lines work for both)
            per_op = defaultdict(list)
            for k, ch in dev.chars:
                if k is not None:
                    w = weights.get(ch.fontname)
                    if w is not None:
                        ch.weight_hint = w
                    per_op[k].append(ch)
            lines, cur, last = {}, None, None
            for k in sorted(per_op):
                chars = [c for c in per_op[k] if c.get_text().strip()] or per_op[k]
                y = statistics.median(c.y0 for c in chars)
                size = max(c.size for c in chars)
                x0 = min(c.x0 for c in chars)
                if last is not None and abs(y - last[0]) < max(size, last[1]) * 0.5 and x0 >= last[2] - size:
                    lines[cur]["ops"].append(k)
                    lines[cur]["chars"].extend(per_op[k])
                else:
                    cur = len(lines)
                    lines[cur] = {"ops": [k], "chars": list(per_op[k])}
                last = (y, size, max(c.x1 for c in chars))
            pages.append({"lines": lines, "images": list(dev.images),
                          "W": x1 - x0, "H": y1 - y0, "y0": y0})
    return pages


def _block_info(chars, H):
    """Text (with spaces restored from gaps), box (top-down), size and weight of one text object."""
    chars = [c for c in chars if c.get_text() != ""]
    if not chars:
        return None
    out, prev, inserted = [], None, 0
    for c in chars:
        t = c.get_text()
        if prev is not None:
            same_line = abs(c.y0 - prev.y0) < max(c.size, 1) * 0.5
            gap = c.x0 - prev.x1
            if not same_line:
                if out and not out[-1].isspace():
                    out.append(" ")
            elif gap > max(c.size, 1) * 0.18 and out and not out[-1].isspace() and not t.isspace():
                out.append(" ")
                inserted += 1
        out.append(t)
        prev = c
    text = re.sub(r"\s+", " ", "".join(out)).strip()
    raw = re.sub(r"\s+", " ", "".join(c.get_text() for c in chars)).strip()
    sizes = [round(c.size, 1) for c in chars if c.get_text().strip()]
    names = [c.fontname.lower() + (" bold" if getattr(c, "weight_hint", 0) >= 600 else "")
             for c in chars if c.get_text().strip()]
    def lightness(c):
        try:
            col = c.graphicstate.ncolor
            if isinstance(col, (int, float)):
                return float(col)
            if col and len(col) == 3:
                return 0.2126 * col[0] + 0.7152 * col[1] + 0.0722 * col[2]
            if col and len(col) == 4:  # CMYK: light when there is little ink
                return 1 - min(1.0, sum(col) / 2)
        except Exception:
            pass
        return 0.0
    visible = [c for c in chars if c.get_text().strip()]

    def hued(c):
        try:
            col = c.graphicstate.ncolor
            if isinstance(col, (list, tuple)) and len(col) in (3, 4):
                vals = col[:3] if len(col) == 3 else [1 - min(1.0, col[i] + col[3]) for i in range(3)]
                return max(vals) - min(vals) > 0.15
        except Exception:
            pass
        return False
    coloured = bool(visible) and sum(1 for c in visible if hued(c)) > len(visible) / 2
    tilted = bool(visible) and sum(1 for c in visible if not getattr(c, "upright", True)) > len(visible) / 2
    pale = bool(visible) and sum(lightness(c) for c in visible) / len(visible) > 0.75
    return {
        "tilted": tilted, "pale": pale, "coloured": coloured,
        "text": text, "raw": raw, "spaces_added": inserted,
        "x0": min(c.x0 for c in chars), "x1": max(c.x1 for c in chars),
        "top": H - max(c.y1 for c in chars), "bottom": H - min(c.y0 for c in chars),
        "size": max(sizes) if sizes else 0,
        "bold": bool(names) and sum(any(k in n for k in ("bold", "black", "heavy", "semibold")) for n in names) > len(names) / 2,
    }


def _fkey(t: str) -> str:
    return re.sub(r"[^a-z]+", "", t.lower())


# ---------------------------------------------------------------- deciding structure

def _plan(pages, presentation: bool):
    """Assign every text object a role: furniture, or a member of a structure element."""
    infos = []  # (page_no, bt, info)
    for pno, pg in enumerate(pages):
        for ln, line in sorted(pg["lines"].items()):
            info = _block_info(line["chars"], pg["H"])
            if info:
                infos.append((pno, ln, info))

    # body text size: the size that carries real text on the most pages (a dense reference
    # section in small print must not win just by sheer character count), then by characters
    weights, per_page = Counter(), defaultdict(Counter)
    for pno, _, i in infos:
        weights[i["size"]] += len(i["text"])
        per_page[i["size"]][pno] += len(i["text"])
    if weights:
        body = max(weights, key=lambda sz: (sum(1 for n in per_page[sz].values() if n >= 150), weights[sz]))
    else:
        body = 11

    # running heads / feet: the content area can be inset (slides printed with margins), so
    # "edge" means the top or bottom band of the text actually on the page
    n_pages = len(pages)
    band = {}
    for pno in range(n_pages):
        tops = [i["top"] for p, _, i in infos if p == pno]
        bots = [i["bottom"] for p, _, i in infos if p == pno]
        if tops:
            lo, hi = min(tops), max(bots)
            band[pno] = (lo, hi, max(hi - lo, 1))

    def at_edge(pno, i):
        if pno not in band:
            return False
        lo, hi, h = band[pno]
        return i["top"] <= lo + 0.12 * h or i["bottom"] >= hi - 0.12 * h

    edge_text, edge_spot = defaultdict(set), defaultdict(set)
    for pno, bt, i in infos:
        if at_edge(pno, i) and len(i["text"]) < 120:
            edge_text[_fkey(i["text"])].add(pno)
            H = pages[pno]["H"]
            edge_spot[(round(i["size"]), round(i["top"] / H * 50))].add(pno)
    need = max(3, n_pages // 3)
    furniture_keys = {k for k, ps in edge_text.items() if n_pages >= 3 and len(ps) >= need and k}
    furniture_spots = {k for k, ps in edge_spot.items() if n_pages >= 3 and len(ps) >= need}

    # watermarks: tilted or pale, oversized text repeated across pages ("DRAFT", "SAMPLE", a logo word)
    mark_pages = defaultdict(set)
    for pno, _, i in infos:
        if i.get("tilted") or (i.get("pale") and i["size"] >= body * 1.8):
            mark_pages[_fkey(i["text"])].add(pno)
    watermark_keys = {k for k, ps in mark_pages.items() if k and (len(ps) >= 2 or n_pages == 1)}

    def is_furniture(pno, i):
        if _fkey(i["text"]) in watermark_keys and (i.get("tilted") or i.get("pale")):
            return True
        if not at_edge(pno, i):
            return False
        if _fkey(i["text"]) in furniture_keys:
            return True
        small = i["size"] <= body * 0.85
        spot = (round(i["size"]), round(i["top"] / pages[pno]["H"] * 50)) in furniture_spots
        return small and spot

    elements = defaultdict(list)  # page -> list of elements [{tag, bts, text, size, ...}]
    roles = {}  # (pno, bt) -> "artifact" | element id
    for pno, pg in enumerate(pages):
        H = pg["H"]
        page_infos = [(bt, i) for p, bt, i in infos if p == pno]
        cur = None
        pending_symbol = []
        for bt, i in page_infos:
            near_edge = at_edge(pno, i)
            if is_furniture(pno, i) or \
                    (near_edge and re.fullmatch(r"[\d\s.\-–|/]+|page\s*\d+(\s*of\s*\d+)?", i["text"], re.I)):
                roles[(pno, bt)] = "artifact"
                continue
            if SYMBOL_ONLY.match(i["text"]):
                pending_symbol.append(bt)  # a bullet drawn on its own: it belongs to the next item
                continue
            t = i["text"].strip()
            plain_start = bool(t) and t[0].isalnum()
            close = (cur is not None and abs(i["size"] - cur["size"]) <= 1.0
                     and 0 <= i["top"] - cur["bottom"] <= i["size"] * 1.2
                     and i["x0"] >= cur["x0"] - i["size"] * 3)
            size_heading = (i["size"] >= body * 1.2 or (i["bold"] and i["size"] >= body * 1.05 and len(t) < 80)
                            or (i.get("coloured") and i["size"] >= body * 1.1 and len(t) < 80))
            # a short bold line on its own at body size ("Question 3"), but not a bold line that
            # simply continues the line above it
            bold_heading = (i["bold"] and i["size"] >= body * 0.95 and len(t) < 60 and len(t.split()) <= 10
                            and ":" not in t
                            and not (close and cur is not None and not cur["heading"]))
            is_heading = (len(t.split()) <= 15 and not t.endswith((".", ",", ";")) and plain_start
                          and not LIST_RE.match(t) and (size_heading or bold_heading))
            is_item = bool(LIST_RE.match(i["text"])) or bool(pending_symbol)
            joinable = close and ((not is_heading and not is_item and not cur["heading"])
                                  or (is_heading and cur["heading"]))  # a title on two lines
            if close and not is_heading and not is_item and cur is not None and cur["item"]:
                joinable = True  # second line of a list item
            if joinable:
                cur["bts"].append(bt)
                cur["texts"].append(i["text"])
                cur["raws"].append(i["raw"])
                cur["bottom"] = max(cur["bottom"], i["bottom"])
                cur["spaces_added"] += i["spaces_added"]
            else:
                cur = {"bts": pending_symbol + [bt], "texts": [i["text"]], "raws": [i["raw"]],
                       "coloured": bool(i.get("coloured")), "size": i["size"], "x0": i["x0"], "top": i["top"], "bottom": i["bottom"],
                       "heading": is_heading, "item": is_item and not is_heading,
                       "spaces_added": i["spaces_added"]}
                elements[pno].append(cur)
            pending_symbol = []
        for bt in pending_symbol:
            roles[(pno, bt)] = "artifact"

    # heading levels. Slide decks: first slide's title H1, each slide's title H2, other
    # headings on a slide H3. Documents: by size across the document (largest = H1).
    head_sizes = sorted({round(e["size"]) for es in elements.values() for e in es if e["heading"]}, reverse=True)
    # documents: the cover/title size(s) are H1; the rest fall into at most three tiers (H2-H4),
    # sizes within ~15% of each other sharing a tier, so designed documents with many slightly
    # different heading sizes still get a short, sensible outline
    pages_of = defaultdict(set)
    for pno, es in elements.items():
        for e in es:
            if e["heading"]:
                pages_of[round(e["size"], 1)].add(pno)
    title_sizes = []
    exact_sizes = sorted(pages_of, reverse=True)
    for sz in exact_sizes:  # largest first: sizes used only on the first page, above everything else
        if pages_of[sz] == {min(elements) if elements else 0}:
            title_sizes.append(sz)
        else:
            break
    if len(title_sizes) == len(exact_sizes):
        title_sizes = title_sizes[:1]  # a one-page document: only the biggest is the title
    rest = [sz for sz in exact_sizes if sz not in title_sizes]
    # the opening line of page 1, when it is a heading, is the document title (H1)
    first_elem = None
    if not presentation and elements and not title_sizes:
        p0 = elements[min(elements)]
        if p0 and p0[0]["heading"]:
            first_elem = p0[0]
    doc_levels, tier, top = {}, 0, None
    base = 2 if (title_sizes or first_elem is not None) else 1
    for sz in rest:
        if top is not None and sz < top * 0.85:
            tier += 1
            top = sz
        elif top is None:
            top = sz
        doc_levels[sz] = min(base + tier, base + 2)
    # within one size band, coloured headings rank above plain ones (green section titles over
    # bold black subheadings)
    has_colour = {}
    for es in elements.values():
        for e in es:
            if e["heading"]:
                has_colour.setdefault(doc_levels.get(round(e["size"], 1), base), set()).add(bool(e.get("coloured")))
    for sz in title_sizes:
        doc_levels[sz] = 1
    for pno in sorted(elements):
        heads = [e for e in elements[pno] if e["heading"]]
        slide_title = max(heads, key=lambda e: (e["size"], -e["top"])) if heads else None
        for e in elements[pno]:
            if not e["heading"]:
                e["tag"] = "/LI" if e["item"] else "/P"
                continue
            if presentation:
                level = (1 if pno == 0 else 2) if e is slide_title else 3
            elif e is first_elem:
                level = 1
            else:
                level = doc_levels.get(round(e["size"], 1), 4)
                if not e.get("coloured") and has_colour.get(level) == {True, False}:
                    level = min(level + 1, base + 2)
            e["tag"] = f"/H{level}"
    # a logical outline never skips a level: walk the headings in reading order and pull any
    # heading down to at most one level below the previous one (the first heading becomes H1)
    prev = 0
    for pno in sorted(elements):
        for e in elements[pno]:
            if not e.get("tag", "").startswith("/H"):
                continue
            level = int(e["tag"][2])
            if level > prev + 1:
                level = prev + 1
                e["tag"] = f"/H{level}"
            prev = level
    return elements, roles, body


# ---------------------------------------------------------------- writing the tags

TEXT_SHOW = {"Tj", "TJ", "'", '"'}


def _wrap_page(pdf, page, plan_page, artifacts, image_slots, lines):
    """Rewrite one page's content: wrap text (per line, even inside a text object), images
    and shapes in marked content. Nothing is drawn differently."""
    ops = list(pikepdf.parse_content_stream(page))
    line_of_op = {k: ln for ln, line in lines.items() for k in line["ops"]}
    elem_of_line = {}
    for idx, e in enumerate(plan_page):
        for ln in e["bts"]:
            elem_of_line[ln] = idx
    out, mcid_of_elem, mcid = [], defaultdict(list), 0
    figures, seq = [], []
    show_i, do_i = -1, -1
    in_text, in_path = False, False
    open_target = None  # what the currently open marked-content sequence belongs to

    def close():
        nonlocal open_target
        if open_target is not None:
            out.append(([], Operator("EMC")))
            open_target = None

    def open_for(target):
        nonlocal open_target, mcid
        if target == open_target:
            return
        close()
        if target == "A":
            out.append(([Name.Artifact], Operator("BMC")))
        else:
            tag = plan_page[target]["tag"]
            tag = "/LBody" if tag == "/LI" else tag
            out.append(([Name(tag), Dictionary(MCID=mcid)], Operator("BDC")))
            if target not in mcid_of_elem:
                seq.append(("e", target))
            mcid_of_elem[target].append(mcid)
            mcid += 1
        open_target = target

    for operands, op in ops:
        name = str(op)
        if name == "BT":
            in_text = True
            out.append((operands, op))
            continue
        if name == "ET":
            close()
            in_text = False
            out.append((operands, op))
            continue
        if in_text:
            if name in TEXT_SHOW:
                show_i += 1
                ln = line_of_op.get(show_i)
                if ln is None:
                    target = open_target if open_target is not None else "A"
                elif ln in artifacts or ln not in elem_of_line:
                    target = "A"
                else:
                    target = elem_of_line[ln]
                open_for(target)
            out.append((operands, op))
            continue
        if name == "Do":
            do_i += 1
            if do_i in image_slots:
                out.append(([Name.Figure, Dictionary(MCID=mcid)], Operator("BDC")))
                figures.append((mcid, image_slots[do_i]))
                seq.append(("f", mcid))
                mcid += 1
            else:
                out.append(([Name.Artifact], Operator("BMC")))
            out.append((operands, op))
            out.append(([], Operator("EMC")))
            continue
        if name in ("sh", "INLINE IMAGE") and not in_path:
            out.append(([Name.Artifact], Operator("BMC")))
            out.append((operands, op))
            out.append(([], Operator("EMC")))
            continue
        if name in PATH_START and not in_path:
            in_path = True
            out.append(([Name.Artifact], Operator("BMC")))
        out.append((operands, op))
        if in_path and (name in PATH_PAINT or name == "n"):
            out.append(([], Operator("EMC")))
            in_path = False
    if in_path:
        out.append(([], Operator("EMC")))
    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
    return mcid_of_elem, figures, seq


def tag_in_place(src: str, out_pdf: str, lang: str, title: str) -> dict:
    with pikepdf.open(src) as probe:
        meta = " ".join(str(probe.docinfo.get(k, "")) for k in ("/Title", "/Creator", "/Producer")).lower()
        landscape = sum(1 for p in probe.pages if float(p.mediabox[2]) > float(p.mediabox[3]))
        presentation = any(h in meta for h in PRESENTATION_HINTS) or landscape > len(probe.pages) / 2
    pages = _read_pages(src)
    elements, roles, body = _plan(pages, presentation)

    with pikepdf.open(src) as pdf:
        if len(pdf.pages) != len(pages):
            raise RuntimeError("page count mismatch while reading the PDF")
        root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
        doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
        kids, nums = Array(), Array()
        counts = Counter()
        first_heading = None
        for pno, page in enumerate(pdf.pages):
            pg = pages[pno]
            res = page.obj.get("/Resources")
            xobjs = res.get("/XObject") if res is not None else None
            image_names = {str(k) for k, v in (xobjs.items() if xobjs is not None else [])
                           if v.get("/Subtype") == "/Image"}
            # page-level Do operators that draw images (forms stay artifacts)
            ops = list(pikepdf.parse_content_stream(page))
            slots, do_i = {}, -1
            boxes = dict(pg["images"])
            for operands, op in ops:
                if str(op) == "Do":
                    do_i += 1
                    if operands and str(operands[0]) in image_names:
                        bx = boxes.get(do_i)
                        area = (bx[2] - bx[0]) * (bx[3] - bx[1]) if bx else 0
                        page_area = pg["W"] * pg["H"]
                        has_text = bool(pg["lines"])
                        # a picture filling most of the page with text over it is a background
                        if bx and area > 400 and not (has_text and area > 0.75 * page_area):
                            slots[do_i] = bx
                        elif bx and has_text and area > 0.75 * page_area:
                            counts["backgrounds"] += 1
            artifacts = {bt for (p, bt), r in roles.items() if p == pno and r == "artifact"}
            plan_page = elements.get(pno, [])
            mcid_of_elem, figures, seq = _wrap_page(pdf, page, plan_page, artifacts, slots, pg["lines"])

            parents = Array()
            slot_elems = {}
            current_list = None
            for kind, ref in seq:
                if kind == "f":
                    el = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Figure, P=doc,
                                                      Pg=page.obj, K=Array([ref])))
                    kids.append(el)
                    slot_elems[ref] = el
                    counts["figures"] += 1
                    current_list = None
                    continue
                e = plan_page[ref]
                mcids = mcid_of_elem[ref]
                text = " ".join(e["texts"])
                if e["tag"] == "/LI":
                    if current_list is None:
                        current_list = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.L, P=doc,
                                                                    K=Array()))
                        kids.append(current_list)
                        counts["lists"] += 1
                    li = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.LI, P=current_list, K=Array()))
                    body_el = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.LBody, P=li,
                                                           Pg=page.obj, K=Array(mcids)))
                    li.K.append(body_el)
                    current_list.K.append(li)
                    target = body_el
                    counts["items"] += 1
                else:
                    current_list = None
                    target = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name(e["tag"]), P=doc,
                                                          Pg=page.obj, K=Array(mcids)))
                    kids.append(target)
                    counts["headings" if e["tag"].startswith("/H") else "paragraphs"] += 1
                    if e["tag"].startswith("/H") and first_heading is None:
                        first_heading = text
                if e["spaces_added"]:
                    target.ActualText = String(text)
                    counts["spaces_restored"] += 1
                for m in mcids:
                    slot_elems[m] = target
            max_m = max(slot_elems) if slot_elems else -1
            for m in range(max_m + 1):
                parents.append(slot_elems.get(m, pikepdf.Object.parse(b"null")))
            page.obj.StructParents = pno
            page.obj.Tabs = Name.S
            nums.append(pno)
            nums.append(pdf.make_indirect(parents))
        doc.K = kids
        root.K = Array([doc])
        root.ParentTree = pdf.make_indirect(Dictionary(Nums=nums))
        root.ParentTreeNextKey = len(pdf.pages)
        pdf.Root.StructTreeRoot = root
        pdf.Root.MarkInfo = Dictionary(Marked=True)
        pdf.Root.Lang = String(lang)
        pdf.save(out_pdf)
    counts["artifact_text"] = sum(1 for r in roles.values() if r == "artifact")
    return {"counts": dict(counts), "presentation": presentation, "first_heading": first_heading}

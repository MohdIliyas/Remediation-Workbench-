"""Tagged scan: the original page images, unchanged, plus an invisible tagged text layer.

Visually identical to the scan. Underneath, every OCR word sits over its printed
word in an invisible embedded font (Tesseract's glyphless font), grouped into
H1-H6 and P structure elements in reading order. The page image and any old
hidden OCR text are marked as artifacts, as are running heads, footers and page
numbers, so assistive technology reads only the structured text.
"""
from __future__ import annotations

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator, String

from .structure import clean_leaders

FONT_KEY = Name("/FRemOCR")
TEXT_SHOW = {"Tj", "TJ", "'", '"'}


def _strip_invisible_text(ops):
    """Drop text objects drawn invisibly (render mode 3): an old OCR layer."""
    out, buf, in_text, tr, invisible = [], [], False, 0, False
    for operands, op in ops:
        name = str(op)
        if name == "Tr" and operands:
            try:
                tr = int(operands[0])
            except (TypeError, ValueError):
                pass
        if name == "BT":
            in_text, buf, invisible = True, [(operands, op)], False
            continue
        if in_text:
            buf.append((operands, op))
            if name in TEXT_SHOW and tr == 3:
                invisible = True
            if name == "ET":
                in_text = False
                if not invisible:
                    out.extend(buf)
                buf = []
            continue
        out.append((operands, op))
    return out


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-be")


def _to_user(X, Y, rot, W, H):
    """A point on the page as displayed (rotated `rot` clockwise) -> unrotated PDF user space."""
    if rot == 90:
        return W - Y, X
    if rot == 180:
        return W - X, H - Y
    if rot == 270:
        return Y, H - X
    return X, Y


class PageGeom:
    def __init__(self, mediabox, rot):
        self.x0, self.y0 = mediabox[0], mediabox[1]
        self.W, self.H = mediabox[2] - mediabox[0], mediabox[3] - mediabox[1]
        self.rot = rot % 360
        self.disp_h = self.W if self.rot in (90, 270) else self.H

    def matrix(self, X, Y):
        """Text matrix that writes left-to-right on the displayed page starting at (X, Y)."""
        ox, oy = _to_user(X, Y, self.rot, self.W, self.H)
        ax, ay = _to_user(X + 1, Y, self.rot, self.W, self.H)
        bx, by = _to_user(X, Y + 1, self.rot, self.W, self.H)
        return [ax - ox, ay - oy, bx - ox, by - oy, round(ox + self.x0, 2), round(oy + self.y0, 2)]


def _word_ops(w, geom, scale):
    """Invisible text for one word, stretched to cover the printed word."""
    word = clean_leaders(w["text"]) if any(c in w["text"] for c in ".…") else w["text"]
    if not word:
        return []  # a dot leader on its own: nothing worth reading aloud
    text = word + " "
    size = max(w["h"] * scale, 1.0)
    width = max(w["w"] * scale, 0.5)
    tz = width / (len(text) * 0.5 * size) * 100  # glyphless font: every glyph 0.5 em wide
    X = w["left"] * scale
    Y = geom.disp_h - (w["top"] + w["h"]) * scale
    return [
        ([], Operator("BT")),
        ([3], Operator("Tr")),
        ([FONT_KEY, round(size, 2)], Operator("Tf")),
        ([round(tz, 2)], Operator("Tz")),
        (geom.matrix(X, Y), Operator("Tm")),
        ([String(_utf16(text))], Operator("Tj")),
        ([], Operator("ET")),
    ]


def build_tagged_scan(src_pdf: str, out_pdf: str, scan: dict, lang: str, dpi: int = 300,
                      straighten: bool = False) -> dict:
    words = scan["words"]
    items = [it for it in scan["items"] if it.get("words")]
    from .structure import normalize_heading_levels
    normalize_heading_levels(items)
    with pikepdf.open(scan["font_pdf"]) as fsrc:
        font_src = fsrc.pages[0].Resources.Font
        font_obj_src = font_src[list(font_src.keys())[0]]
        with pikepdf.open(src_pdf) as pdf:
            turns = {pi["page"]: pi["turn"] for pi in scan.get("page_info", [])}
            straightened = 0
            font = pdf.copy_foreign(font_obj_src)
            scale = 72.0 / dpi

            # group items by page (an item never spans pages: OCR paragraphs are page-local)
            by_page: dict[int, list] = {}
            for idx, it in enumerate(items):
                pno = words[it["words"][0]]["page"]
                by_page.setdefault(pno, []).append(idx)
            used = {w for it in items for w in it["words"]}

            root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
            doc_elem = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
            kids = Array()
            parent_nums = Array()
            elem_for_item: dict[int, pikepdf.Object] = {}

            for pno, page in enumerate(pdf.pages, 1):
                mb = [float(v) for v in page.mediabox]
                orig = int(page.obj.get("/Rotate", 0)) % 360
                turn = turns.get(pno, 0)
                geom = PageGeom(mb, orig + turn)
                if turn:
                    straightened += 1          # read the right way up for OCR...
                    if straighten:
                        page.obj.Rotate = geom.rot  # ...and only change how it is shown if asked
                old = _strip_invisible_text(pikepdf.parse_content_stream(page))
                ops = [([Name.Artifact], Operator("BMC")), ([], Operator("q"))] + old + \
                      [([], Operator("Q")), ([], Operator("EMC"))]

                mcid = 0
                parents = Array()
                for idx in by_page.get(pno, []):
                    it = items[idx]
                    tag = f"/H{min(max(it['level'], 1), 6)}" if it["kind"] == "heading" else "/P"
                    elem = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name(tag), P=doc_elem,
                                                        Pg=page.obj, K=Array([mcid])))
                    elem_for_item[idx] = elem
                    ops.append(([Name(tag), Dictionary(MCID=mcid)], Operator("BDC")))
                    for wid in it["words"]:
                        ops.extend(_word_ops(words[wid], geom, scale))
                    ops.append(([], Operator("EMC")))
                    parents.append(elem)
                    mcid += 1
                # leftover words on this page (running heads, footers, page numbers)
                stray = [w for w in words.values() if w["page"] == pno and w["id"] not in used]
                if stray:
                    ops.append(([Name.Artifact, Dictionary(Type=Name.Pagination)], Operator("BDC")))
                    for w in stray:
                        ops.extend(_word_ops(w, geom, scale))
                    ops.append(([], Operator("EMC")))

                page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(ops))
                res = page.obj.get("/Resources")
                if res is None:
                    page.obj.Resources = Dictionary()
                    res = page.obj.Resources
                if "/Font" not in res:
                    res.Font = Dictionary()
                res.Font[FONT_KEY] = font
                page.obj.StructParents = pno - 1
                page.obj.Tabs = Name.S
                parent_nums.append(pno - 1)
                parent_nums.append(pdf.make_indirect(parents))

            for idx in range(len(items)):
                if idx in elem_for_item:
                    kids.append(elem_for_item[idx])
            doc_elem.K = kids
            root.K = Array([doc_elem])
            root.ParentTree = pdf.make_indirect(Dictionary(Nums=parent_nums))
            root.ParentTreeNextKey = len(pdf.pages)
            pdf.Root.StructTreeRoot = root
            pdf.Root.MarkInfo = Dictionary(Marked=True)
            pdf.Root.Lang = String(lang)
            pdf.Root.ViewerPreferences = Dictionary(DisplayDocTitle=True)
            title = scan["title"]
            with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
                meta["dc:title"] = title
            pdf.docinfo["/Title"] = title
            pdf.save(out_pdf)
    return {"elements": len(elem_for_item), "artifact_words": len(words) - len(used),
            "straightened": straightened}

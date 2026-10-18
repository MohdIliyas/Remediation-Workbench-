"""Figures in tagged PDFs: find them, crop their pictures, write alt text back.

Figures are numbered by a depth-first walk of the structure tree. The same walk
is used when collecting and when applying, so "figure 7" always means the same
tag in the same file.
"""
from __future__ import annotations

import pikepdf


def _figure_types(root) -> set:
    """/Figure plus every custom tag the file's RoleMap resolves to /Figure (e.g. /Image, /Graphic)."""
    rolemap = root.get("/RoleMap")
    names = {"/Figure"}
    if rolemap is None:
        return names
    for key in rolemap.keys():
        seen, cur = set(), str(key)
        while cur not in seen and cur in rolemap:
            seen.add(cur)
            cur = str(rolemap[cur])
        if cur == "/Figure":
            names.add(str(key))
    return names


def iter_figures(pdf: pikepdf.Pdf):
    """Yield (index, struct element) for every figure (incl. role-mapped ones), in reading order."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return
    fig_types = _figure_types(root)
    seen = set()
    index = 0

    def walk(node):
        nonlocal index
        if isinstance(node, pikepdf.Array):
            for child in node:
                yield from walk(child)
            return
        if not isinstance(node, pikepdf.Dictionary):
            return
        key = node.objgen
        if key != (0, 0):
            if key in seen:
                return
            seen.add(key)
        if str(node.get("/S")) in fig_types:
            index += 1
            yield index, node
        kids = node.get("/K")
        if kids is not None and node.get("/Type") != "/MCR":
            yield from walk(kids)

    yield from walk(root.get("/K"))


def _page_of(elem, page_index: dict):
    pg = elem.get("/Pg")
    if pg is not None and pg.objgen in page_index:
        return page_index[pg.objgen]
    kids = elem.get("/K")
    for k in (kids if isinstance(kids, pikepdf.Array) else [kids]):
        if isinstance(k, pikepdf.Dictionary):
            pg = k.get("/Pg")
            if pg is not None and pg.objgen in page_index:
                return page_index[pg.objgen]
    return None


def _page_mcids(elem) -> list[int]:
    """MCIDs drawn directly in the page content (not inside form XObjects)."""
    kids = elem.get("/K")
    kids = kids if isinstance(kids, pikepdf.Array) else [kids]
    out = []
    for k in kids:
        if isinstance(k, pikepdf.Dictionary):
            if "/MCID" in k and "/Stm" not in k:
                out.append(int(k.MCID))
        elif k is not None:
            try:
                out.append(int(k))
            except (TypeError, ValueError):
                pass
    return out


def _stm_mcids(elem) -> dict:
    """{form XObject objgen: set of MCIDs} for content drawn inside form XObjects."""
    kids = elem.get("/K")
    kids = kids if isinstance(kids, pikepdf.Array) else [kids]
    out: dict = {}
    for k in kids:
        if isinstance(k, pikepdf.Dictionary) and "/MCID" in k and "/Stm" in k:
            out.setdefault(k.Stm.objgen, set()).add(int(k.MCID))
    return out


def _filter_ops(ops, wanted: set, keep_do: set):
    """Keep every state/path op, but only paint inside the wanted marked-content sequences.
    Do operators naming a form in keep_do are always kept (the form is filtered separately)."""
    stack, kept = [], []
    for operands, op in ops:
        name = str(op)
        if name in ("BDC", "BMC"):
            mine = False
            if name == "BDC" and len(operands) == 2 and isinstance(operands[1], pikepdf.Dictionary):
                m = operands[1].get("/MCID")
                mine = m is not None and int(m) in wanted
            stack.append(mine or (bool(stack) and stack[-1]))
        elif name == "EMC":
            if stack:
                stack.pop()
        elif name == "Do" and operands and str(operands[0]) in keep_do:
            pass
        elif name in PAINT_OPS and not (stack and stack[-1]):
            if name not in ("Tj", "TJ", "'", '"', "Do", "INLINE IMAGE", "sh"):
                kept.append(([], pikepdf.Operator("n")))  # end the path without painting it
            continue
        kept.append((operands, op))
    return kept


PAINT_OPS = {"Tj", "TJ", "'", '"', "Do", "INLINE IMAGE", "S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "sh"}


def drawn_bounds(path: str, page_no: int, mcids: list[int], stm: dict | None = None):
    """Where the figure's own content actually lands on the page (PyMuPDF page coordinates).

    Renders a copy of the page that paints only the figure's marked content (on the page and
    inside form XObjects), then measures the non-white, non-transparent pixels."""
    import io
    import pymupdf
    from PIL import Image
    stm = stm or {}
    if not mcids and not stm:
        return None
    with pikepdf.open(path) as src:
        src_page = src.pages[page_no]
        form_names = {}
        res = src_page.obj.get("/Resources")
        xobjs = res.get("/XObject") if res is not None else None
        for name, xo in (xobjs.items() if xobjs is not None else []):
            if xo.objgen in stm:
                form_names[name] = xo.objgen
        tmp = pikepdf.new()
        tmp.pages.append(src_page)
        page = tmp.pages[0]
        try:
            ops = list(pikepdf.parse_content_stream(page))
        except Exception:
            return None
        page.obj.Contents = tmp.make_stream(pikepdf.unparse_content_stream(
            _filter_ops(ops, set(mcids), set(form_names))))
        if form_names:
            txo = page.obj.Resources.XObject
            for name, og in form_names.items():
                form = txo[name]
                try:
                    fops = list(pikepdf.parse_content_stream(form))
                    form.write(pikepdf.unparse_content_stream(_filter_ops(fops, stm[og], set())))
                except Exception:
                    pass
        buf = io.BytesIO()
        tmp.save(buf)
    with pymupdf.open(stream=buf.getvalue(), filetype="pdf") as d:
        pg = d[0]
        zoom = 0.75  # 54 dpi is plenty to measure
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True)
        im = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        r, g, b, a = im.split()
        # drawn = not transparent AND not (near) white: white backing boxes don't count
        lum = Image.merge("RGB", (r, g, b)).convert("L")
        mask = Image.eval(a, lambda v: 255 if v > 16 else 0)
        ink = Image.eval(lum, lambda v: 255 if v < 240 else 0)
        from PIL import ImageChops
        drawn = ImageChops.multiply(mask, ink)
        box = _main_cluster(drawn) or drawn.getbbox()
        if not box:
            return None
        return pymupdf.Rect(box[0] / zoom, box[1] / zoom, box[2] / zoom, box[3] / zoom) & pg.rect


def _main_cluster(mask_img):
    """Bounding box of the main drawn area, ignoring small detached marks (stamps, specks)
    that a PDF assembler sometimes leaves inside a figure's content."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    m = (np.array(mask_img) > 0).astype("uint8")
    if not m.any():
        return None
    joined = cv2.dilate(m, np.ones((9, 9), "uint8"))  # merge strokes, letters and nearby parts
    n, labels, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    if n <= 2:
        return None  # one cluster: the plain bounding box is right
    areas = stats[1:, cv2.CC_STAT_AREA]
    biggest = areas.max()
    keep = [i + 1 for i, a in enumerate(areas) if a >= 0.2 * biggest]
    xs0 = [stats[i, cv2.CC_STAT_LEFT] for i in keep]
    ys0 = [stats[i, cv2.CC_STAT_TOP] for i in keep]
    xs1 = [stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH] for i in keep]
    ys1 = [stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] for i in keep]
    pad = 4  # undo most of the dilation
    return (max(min(xs0) + pad, 0), max(min(ys0) + pad, 0), max(xs1) - pad, max(ys1) - pad)


def _bbox_of(elem):
    attrs = elem.get("/A")
    for a in (attrs if isinstance(attrs, pikepdf.Array) else [attrs]):
        if isinstance(a, pikepdf.Dictionary) and "/BBox" in a:
            try:
                return [float(v) for v in a["/BBox"]]
            except (TypeError, ValueError):
                pass
    return None


def figures_needing_alt(path: str) -> list[dict]:
    """[{index, page, bbox}] for figures with no /Alt (bbox in PDF points, may be None)."""
    out = []
    with pikepdf.open(path) as pdf:
        page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
        for idx, fig in iter_figures(pdf):
            if str(fig.get("/Alt", "")).strip() or str(fig.get("/ActualText", "")).strip():
                continue
            out.append({"index": idx, "page": _page_of(fig, page_index), "bbox": _bbox_of(fig),
                        "mcids": _page_mcids(fig), "stm": _stm_mcids(fig)})
        sizes = {i: [float(v) for v in p.mediabox] for i, p in enumerate(pdf.pages)}
    for f in out:
        bb, pno = f["bbox"], f["page"]
        if bb and pno is not None:
            mb = sizes[pno]
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            # a box well outside the page is in some other coordinate space: don't trust it
            if bb[0] < mb[0] - 20 or bb[1] < mb[1] - 20 or bb[2] > mb[2] + 20 or bb[3] > mb[3] + 20:
                f["bbox"] = None
            f["thin"] = min(abs(w), abs(h)) < 3 or abs(w * h) < 150  # rules, borders, specks
        else:
            f["thin"] = False
    return out


def _context_preview(page, rect):
    """The figure with some of the page around it, and an outline marking the figure itself."""
    import io
    import pymupdf
    from PIL import Image, ImageDraw
    # enough surroundings to see what a piece belongs to (a thin strip gets room above and below)
    pad = max(48, 0.25 * max(rect.width, rect.height))
    pad_x, pad_y = pad, pad
    ctx = pymupdf.Rect(rect.x0 - pad_x, rect.y0 - pad_y, rect.x1 + pad_x, rect.y1 + pad_y) & page.rect
    zoom = 1.6
    pix = page.get_pixmap(clip=ctx, matrix=pymupdf.Matrix(zoom, zoom))
    im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    dr = ImageDraw.Draw(im)
    box = [(rect.x0 - ctx.x0) * zoom - 3, (rect.y0 - ctx.y0) * zoom - 3,
           (rect.x1 - ctx.x0) * zoom + 3, (rect.y1 - ctx.y0) * zoom + 3]
    for k in range(3):
        dr.rectangle([box[0] - k, box[1] - k, box[2] + k, box[3] + k], outline=(214, 90, 0))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def crop_figures(path: str, figs: list[dict]) -> list[dict]:
    """Add the exact figure image (for the AI), a context preview (for people) and nearby text."""
    import pymupdf

    with pymupdf.open(path) as doc:
        by_page: dict[int, list[dict]] = {}
        for f in figs:
            by_page.setdefault(f["page"], []).append(f)
        for pno, page_figs in by_page.items():
            if pno is None or pno >= len(doc):
                for f in page_figs:
                    f.update(image=None, preview=None, context="",
                             where=f"Figure {f['index']} (page unknown)")
                continue
            page = doc[pno]
            H = page.mediabox.height
            images = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()
                      if pymupdf.Rect(i["bbox"]).width > 24 and pymupdf.Rect(i["bbox"]).height > 24]
            unknown = [f for f in page_figs if not f["bbox"] and not f.get("mcids") and not f.get("stm")]
            pair = len(unknown) == len(images)
            for f in page_figs:
                rect, exact = None, False
                try:
                    rect = drawn_bounds(path, pno, f.get("mcids") or [], f.get("stm"))
                except Exception:
                    rect = None
                if rect is not None and not rect.is_empty:
                    exact = True
                    if rect.width < 3 or rect.height < 3 or rect.width * rect.height < 150:
                        f["thin"] = True
                elif f["bbox"]:
                    x0, y0, x1, y1 = f["bbox"]
                    rect, exact = pymupdf.Rect(x0, H - y1, x1, H - y0), True
                elif pair and f in unknown:
                    rect, exact = images[unknown.index(f)], True
                if rect is None or (rect & page.rect).is_empty:
                    rect, exact = page.rect, False
                rect = rect & page.rect
                f["rect"] = rect if exact else None
                f["image"] = page.get_pixmap(clip=rect, dpi=150 if exact else 72).tobytes("png")
                f["preview"] = _context_preview(page, rect) if exact and rect != page.rect else f["image"]
                f["context"] = _nearby_text(page, rect if exact else None)
                f["where"] = (f"Page {pno + 1}, figure {f['index']}" if exact else
                              f"Page {pno + 1}, figure {f['index']} (whole page shown; exact spot unknown)")
        # small pieces overlapping a much larger figure on the same page: likely part of that picture
        for pno, page_figs in by_page.items():
            rects = [(f, f.get("rect")) for f in page_figs if f.get("rect") is not None]
            for f, r in rects:
                area = r.width * r.height
                for g, q in rects:
                    if g is f:
                        continue
                    big = q.width * q.height
                    overlap = (r & q)
                    if big > 4 * area and not overlap.is_empty and overlap.width * overlap.height > 0.5 * area:
                        f["piece"] = True
                        break
    return figs


def _nearby_text(page, rect) -> str:
    blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
    if rect is None:
        return " ".join(b[4] for b in blocks)[:1200]
    near = [b for b in blocks if b[3] >= rect.y0 - 140 and b[1] <= rect.y1 + 140]
    near.sort(key=lambda b: (b[1], b[0]))
    return " ".join(b[4] for b in near)[:1200]


def set_figure_alt(path: str, decisions: dict[int, str]):
    """decisions: {figure index: alt text}. Writes /Alt on those figure tags."""
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for idx, fig in iter_figures(pdf):
            if idx in decisions:
                fig.Alt = pikepdf.String(decisions[idx])
        pdf.save(path)


def _parent_tree_array(root, key):
    """The ParentTree array for a page's StructParents key (walks Nums/Kids)."""
    def find(node):
        nums = node.get("/Nums")
        if nums is not None:
            for i in range(0, len(nums) - 1, 2):
                if int(nums[i]) == key:
                    return nums[i + 1]
        for kid in node.get("/Kids", []) or []:
            got = find(kid)
            if got is not None:
                return got
        return None
    pt = root.get("/ParentTree")
    return find(pt) if pt is not None else None


def _artifact_mcids(owner, mcids: set, pdf, is_page: bool) -> int:
    ops = list(pikepdf.parse_content_stream(owner))
    changed = 0
    for i, (operands, op) in enumerate(ops):
        if str(op) == "BDC" and len(operands) == 2 and isinstance(operands[1], pikepdf.Dictionary):
            mcid = operands[1].get("/MCID")
            if mcid is not None and int(mcid) in mcids:
                ops[i] = ([pikepdf.Name.Artifact], pikepdf.Operator("BMC"))
                changed += 1
    if changed:
        data = pikepdf.unparse_content_stream(ops)
        if is_page:
            owner.obj.Contents = pdf.make_stream(data)
        else:
            owner.write(data)
    return changed


def make_figures_artifacts(path: str, indices: set) -> int:
    """Decorative figures: turn their content into artifacts and drop them from the tag tree."""
    done = 0
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        root = pdf.Root.StructTreeRoot
        page_by_obj = {p.obj.objgen: p for p in pdf.pages}
        targets = [(i, f) for i, f in iter_figures(pdf) if i in indices]
        for _, fig in targets:
            kids = fig.get("/K")
            kids = kids if isinstance(kids, pikepdf.Array) else [kids]
            page_mcids, stm_mcids = {}, {}
            for k in kids:
                if isinstance(k, (int, pikepdf.Object)) and not isinstance(k, pikepdf.Dictionary):
                    try:
                        mcid = int(k)
                    except (TypeError, ValueError):
                        continue
                    pg = fig.get("/Pg")
                    if pg is not None:
                        page_mcids.setdefault(pg.objgen, set()).add(mcid)
                elif isinstance(k, pikepdf.Dictionary) and "/MCID" in k:
                    pg = k.get("/Pg") or fig.get("/Pg")
                    if "/Stm" in k:
                        stm_mcids.setdefault(k.Stm.objgen, (k.Stm, set()))[1].add(int(k.MCID))
                    elif pg is not None:
                        page_mcids.setdefault(pg.objgen, set()).add(int(k.MCID))
            for og, mcids in page_mcids.items():
                page = page_by_obj.get(og)
                if page is None:
                    continue
                _artifact_mcids(page, mcids, pdf, True)
                sp = page.obj.get("/StructParents")
                arr = _parent_tree_array(root, int(sp)) if sp is not None else None
                if arr is not None:
                    for m in mcids:
                        if m < len(arr):
                            arr[m] = pikepdf.Object.parse(b"null")
            for _, (stm, mcids) in stm_mcids.items():
                _artifact_mcids(stm, mcids, pdf, False)
            parent = fig.get("/P")
            if parent is not None:
                pk = parent.get("/K")
                if isinstance(pk, pikepdf.Array):
                    for j in range(len(pk) - 1, -1, -1):
                        if isinstance(pk[j], pikepdf.Dictionary) and pk[j].objgen == fig.objgen:
                            del pk[j]
                elif isinstance(pk, pikepdf.Dictionary) and pk.objgen == fig.objgen:
                    del parent["/K"]
            done += 1
        pdf.save(path)
    return done

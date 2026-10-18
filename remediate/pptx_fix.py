"""PowerPoint (.pptx) remediation.

Fixes automatically (no visual change):
  - presentation title and text language
  - low-contrast text colours set directly on text (measured on the rendered slides)
Drafts / queues for review:
  - alt text for pictures (same review page as Word and PDF)
Flags for a person:
  - slides without a title, duplicate slide titles
  - charts, SmartArt and other graphics without alt text
  - vague link text, broken links, merged table cells
  - reading order on busy slides
"""
from __future__ import annotations

import os
import re
import tempfile
from collections import Counter

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.oxml.ns import qn

from .common import FileReport, Options, check_urls, is_vague_link_text, looks_like_raw_url

AUTO_ALT_RE = re.compile(r"^(a picture containing|a close up of|a screenshot of|description automatically generated)", re.I)
IMAGE_NAME_RE = re.compile(r"(\.(png|jpe?g|gif|bmp|emf|wmf|tiff?|svg)$)|^(picture|image|graphic)\s*\d*$", re.I)
TITLE_TYPES = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE, PP_PLACEHOLDER.VERTICAL_TITLE}

IMPRESS_PDF_FILTER = (
    'pdf:impress_pdf_Export:{'
    '"UseTaggedPDF":{"type":"boolean","value":"true"},'
    '"PDFUACompliance":{"type":"boolean","value":"true"},'
    '"ExportNotes":{"type":"boolean","value":"false"}'
    '}'
)


def _iter_shapes(shapes):
    for sh in shapes:
        yield sh
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_shapes(sh.shapes)


def slide_title(slide) -> str:
    for sh in slide.placeholders:
        try:
            if sh.placeholder_format.type in TITLE_TYPES and sh.has_text_frame:
                return sh.text_frame.text.strip()
        except ValueError:
            continue
    return ""


def has_title_placeholder(slide) -> bool:
    for sh in slide.placeholders:
        try:
            if sh.placeholder_format.type in TITLE_TYPES:
                return True
        except ValueError:
            continue
    return False


def _cnvpr(shape):
    el = shape._element
    for tag in ("p:nvPicPr", "p:nvSpPr", "p:nvGraphicFramePr", "p:nvGrpSpPr", "p:nvCxnSpPr"):
        nv = el.find(qn(tag))
        if nv is not None:
            return nv.find(qn("p:cNvPr"))
    return None


def _is_decorative(cnvpr) -> bool:
    return any("decorative" in (e.tag or "").lower() for e in cnvpr.iter())


def _slide_text(slide, limit=1200) -> str:
    bits = [slide_title(slide)]
    for sh in _iter_shapes(slide.shapes):
        if sh.has_text_frame and sh.text_frame.text.strip():
            bits.append(sh.text_frame.text.strip())
    if slide.has_notes_slide:
        bits.append(slide.notes_slide.notes_text_frame.text.strip())
    return " ".join(b for b in bits if b)[:limit]


# ---------------------------------------------------------------- fixes

def fix_title(prs, rep: FileReport, fallback: str):
    cp = prs.core_properties
    if cp.title and cp.title.strip():
        return
    title = ""
    for slide in prs.slides:
        title = slide_title(slide)
        if title:
            break
    if not title:
        title = re.sub(r"[_\-]+", " ", fallback).strip().title()
        rep.flag("Presentation title was taken from the file name. Check it reads well.", title)
    cp.title = " ".join(title.split())[:200]
    rep.fix("Set presentation title", cp.title)


def fix_language(prs, rep: FileReport, lang: str):
    count = 0
    for slide in prs.slides:
        for r in slide._element.iter(qn("a:r")):
            if r.find(qn("a:rPr")) is None:
                r.insert(0, etree.SubElement(r, qn("a:rPr")))  # runs with no formatting of their own
        for el in slide._element.iter(qn("a:rPr"), qn("a:endParaRPr")):
            if not el.get("lang"):
                el.set("lang", lang)
                count += 1
    if count:
        rep.fix(f"Set text language to {lang} on {count} text run(s)")


def _first_text(slide) -> str:
    for sh in slide.shapes:
        if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip():
            line = sh.text_frame.text.strip().splitlines()[0].strip()
            return line[:80]
    return ""


def _hidden_title(slide, prs, text: str):
    """Give a slide a title screen readers and checkers find, placed just off the slide so the slide
    looks exactly the same when presented."""
    from lxml import etree
    from pptx.oxml.ns import qn
    W, H = int(prs.slide_width * 0.8), int(prs.slide_height * 0.12)
    off_x = -W - 91440   # just left of the slide
    for sh in slide.shapes:   # an empty title placeholder: fill it and move it off the slide
        if sh.is_placeholder and sh.placeholder_format.type is not None \
                and "TITLE" in str(sh.placeholder_format.type) and not sh.text_frame.text.strip():
            sh.text_frame.text = text
            sh.left, sh.top, sh.width, sh.height = off_x, 0, W, H
            return
    tree = slide.shapes._spTree
    new_id = max([int(e.get("id")) for e in tree.iter(qn("p:cNvPr")) if e.get("id", "").isdigit()] + [1]) + 1
    sp = etree.fromstring(
        '<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<p:nvSpPr><p:cNvPr id="{new_id}" name="Title (off slide)"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{off_x}" y="0"/><a:ext cx="{W}" cy="{H}"/></a:xfrm></p:spPr>'
        '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US"/><a:t></a:t></a:r></a:p></p:txBody></p:sp>')
    sp.find(".//" + qn("a:t")).text = text
    first = tree.find(qn("p:sp"))   # first in the tree, so it is read first
    anchor = tree.find(qn("p:grpSpPr"))
    (anchor if anchor is not None else tree[1]).addnext(sp)


def check_titles(prs, rep: FileReport):
    titles, added = [], []
    for n, slide in enumerate(prs.slides, 1):
        t = slide_title(slide)
        if not t:
            text = _first_text(slide) or f"Slide {n}"
            try:
                _hidden_title(slide, prs, text)
                added.append(n)
                t = text
            except Exception:
                rep.flag("Slide has no title. Add one with the slide's title placeholder "
                         "(Home > Layout), or a hidden title, so screen reader users can navigate.",
                         f"Slide {n}")
        titles.append(t)
    if added:
        rep.fix(f"Gave {len(added)} slide(s) without a title a title just off the slide (screen readers and "
                "checkers use it; the slide looks the same): slide(s) " + ", ".join(map(str, added[:20])))
    lowered = [t.lower() for t in titles]
    dups = [t for t, c in Counter(lowered).items() if t and c > 1]
    for t in dups:
        slides = [str(i) for i, x in enumerate(lowered, 1) if x == t]
        shown = titles[lowered.index(t)]
        rep.flag(f"Slides {', '.join(slides)} share the title \u201c{shown[:60]}\u201d. Make each title unique "
                 "(for example add \u201c(continued)\u201d or a part number).")


def fix_tables(prs, rep: FileReport):
    for n, slide in enumerate(prs.slides, 1):
        for sh in _iter_shapes(slide.shapes):
            if not sh.has_table:
                continue
            tbl = sh.table
            tblPr = tbl._tbl.tblPr
            if len(tbl.rows) > 1 and tblPr.get("firstRow") not in ("1", "true"):
                tblPr.set("firstRow", "1")   # the header row screen readers and checkers need
                rep.fix("Turned on the table's header row (its first row may now be styled as a header)",
                        f"Slide {n}")
            merged = any(c.is_merge_origin or c.is_spanned for r in tbl.rows for c in r.cells)
            if merged:
                rep.flag("Table has merged cells. Simplify it or check it with a screen reader.", f"Slide {n}")


def _backgrounds_behind_text(slide, slide_w, slide_h) -> set:
    """Pictures that fill most of the slide with text placed on top: they are backgrounds."""
    shapes = list(slide.shapes)
    out = set()
    for i, sh in enumerate(shapes):
        if sh.shape_type != MSO_SHAPE_TYPE.PICTURE or sh.width is None or sh.height is None:
            continue
        if sh.width * sh.height < 0.75 * slide_w * slide_h:
            continue
        above = shapes[i + 1:]
        if any(getattr(t, "has_text_frame", False) and t.text_frame.text.strip() for t in above):
            out.add(sh.shape_id)
    return out


def check_images(prs, rep: FileReport, opts: Options, collector, out_name: str):
    from .alt_apply import _set_decorative
    drafted = queued = 0
    backgrounds = 0
    for n, slide in enumerate(prs.slides, 1):
        context = None
        bg_ids = _backgrounds_behind_text(slide, prs.slide_width, prs.slide_height)
        for sh in list(slide.shapes):
            if sh.shape_id in bg_ids:
                cnv = _cnvpr(sh)
                if cnv is not None and not _is_decorative(cnv):
                    cnv.set("descr", "")
                    _set_decorative(cnv, True)
                    backgrounds += 1
        for sh in _iter_shapes(slide.shapes):
            cnv = _cnvpr(sh)
            if cnv is None or _is_decorative(cnv):
                continue
            descr = (cnv.get("descr") or "").strip()
            needs = not descr or IMAGE_NAME_RE.search(descr) or AUTO_ALT_RE.match(descr)
            if not needs:
                continue
            where = f"Slide {n}, {sh.name}"
            if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                if collector is None:
                    rep.flag("Picture needs alt text.", where)
                    continue
                if context is None:
                    context = _slide_text(slide)
                try:
                    blob = sh.image.blob
                except Exception:
                    blob = None
                item = collector.add(blob, {"kind": "pptx", "file": out_name, "slide": n,
                                            "shape_id": sh.shape_id}, context, where, existing=descr)
                queued += 1
                if item["draft"] and not item["decorative_suggested"]:
                    cnv.set("descr", item["draft"])
                    drafted += 1
            elif sh.shape_type in (MSO_SHAPE_TYPE.CHART, MSO_SHAPE_TYPE.DIAGRAM) or getattr(sh, "has_chart", False):
                if True:
                    rep.flag("Chart or diagram needs alt text (right-click > View Alt Text). "
                             "Charts usually also need their key numbers in the slide text or notes.", where)
    if queued:
        ai = " (drafts written in, marked unreviewed)" if drafted else ""
        rep.flag(f"{queued} picture(s) need alt text{ai}. Use the alt-text review page.")
    if drafted:
        rep.fix(f"Drafted alt text for {drafted} picture(s) with the local model")
    if backgrounds:
        rep.fix(f"Marked {backgrounds} full-slide background picture(s) behind text as decorative, so screen "
                "readers read the text instead")


def describe_media_and_templates(prs, rep: FileReport):
    """Two kinds of picture the image step leaves alone, which checkers (Ally) still count as images:
    a video's or audio clip's preview picture, and pictures in the slide master and layouts (template
    graphics repeated on every slide). Videos get a description; undescribed template pictures are
    marked decorative. Nothing already described is touched."""
    from .alt_apply import _set_decorative
    videos = 0
    for n, slide in enumerate(prs.slides, 1):
        for sh in _iter_shapes(slide.shapes):
            el = sh._element
            if not el.tag.endswith("}pic"):
                continue
            xml = etree.tostring(el).decode("utf8", "ignore")
            if not ("videoFile" in xml or "audioFile" in xml or "p14:media" in xml):
                continue
            cnv = _cnvpr(sh)
            if cnv is None or (cnv.get("descr") or "").strip() or _is_decorative(cnv):
                continue
            kind = "audio clip" if "audioFile" in xml and "videoFile" not in xml else "video"
            rels = [r.target_ref for r in slide.part.rels.values() if r.is_external]
            site = "YouTube " if any("youtu" in t for t in rels) else ("Vimeo " if any("vimeo" in t for t in rels) else "")
            title = slide_title(slide) or f"slide {n}"
            cnv.set("descr", f"Embedded {site}{kind}: {title}"[:250])
            videos += 1
    if videos:
        rep.fix(f"Described {videos} embedded video/audio preview picture(s) (they count as images in checkers)")
    template = 0
    for master in prs.slide_masters:
        for part in [master] + list(master.slide_layouts):
            for sh in _iter_shapes(part.shapes):
                if not sh._element.tag.endswith("}pic"):
                    continue
                cnv = _cnvpr(sh)
                if cnv is None or (cnv.get("descr") or "").strip() or _is_decorative(cnv):
                    continue
                _set_decorative(cnv, True)
                template += 1
    if template:
        rep.fix(f"Marked {template} template picture(s) in the slide master/layouts as decorative (repeated design "
                "graphics on every slide)")


def check_links(prs, rep: FileReport, opts: Options):
    urls = []
    for n, slide in enumerate(prs.slides, 1):
        for sh in _iter_shapes(slide.shapes):
            if not sh.has_text_frame:
                continue
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    url = r.hyperlink.address if r.hyperlink is not None else None
                    if not url:
                        continue
                    urls.append(url)
                    text = r.text.strip()
                    if is_vague_link_text(text):
                        rep.flag(f"Link text \u201c{text}\u201d doesn't say where it goes.", f"Slide {n}: {url}")
                    elif looks_like_raw_url(text) and len(text) > 40:
                        rep.flag("Link text is a long raw URL. Replace it with a descriptive name.", f"Slide {n}")
    if opts.check_links and urls:
        for url, problem in check_urls(urls).items():
            rep.flag(f"Broken link: {problem}.", url)


def check_reading_order(prs, rep: FileReport):
    for n, slide in enumerate(prs.slides, 1):
        shapes = [sh for sh in slide.shapes if (sh.has_text_frame and sh.text_frame.text.strip())
                  or sh.shape_type == MSO_SHAPE_TYPE.PICTURE]
        if len(shapes) < 4:
            continue
        first = shapes[0]
        is_title = getattr(first, "is_placeholder", False) and first.placeholder_format.type in TITLE_TYPES
        if not is_title and slide_title(slide):
            rep.flag("Busy slide where the title isn't read first. Check the reading order "
                     "(Home > Arrange > Selection Pane, or Review > Check Accessibility).", f"Slide {n}")


# ---------------------------------------------------------------- contrast (measured on the rendered deck)

EMU_PER_PT = 12700


def _worst_background(page_img, rect_px, fg, light_text):
    """The hardest background colour behind a text box: the brightest part for light text,
    the darkest for dark text (text pixels themselves are ignored)."""
    from .contrast import luminance
    x0, y0, x1, y1 = [int(v) for v in rect_px]
    crop = page_img.crop((max(x0, 0), max(y0, 0), max(x1, 1), max(y1, 1))).convert("RGB")
    crop.thumbnail((200, 200))
    px = [c for c in crop.getdata() if sum(abs(a - b) for a, b in zip(c, fg)) > 90]
    if not px:
        return None
    px.sort(key=luminance)
    return px[int(len(px) * (0.97 if light_text else 0.03))]


def _backing_opacity(fg, bg, need, light_text):
    """Smallest see-through backing (black for light text, white for dark) that makes text pass."""
    from .contrast import ratio
    base = (0, 0, 0) if light_text else (255, 255, 255)
    for step in range(5, 19):  # 25% .. 90% opaque
        a = step * 0.05
        mixed = tuple(round(a * o + (1 - a) * b) for o, b in zip(base, bg))
        if ratio(fg, mixed) >= need + 0.1:
            return a, base
    return 0.9, base


def _set_backing(shape, colour, opacity) -> bool:
    """Give a shape a translucent solid fill. Only for shapes that have no fill of their own."""
    from lxml import etree as ET
    spPr = shape._element.find(qn("p:spPr"))
    if spPr is None:
        return False
    for tag in ("a:solidFill", "a:gradFill", "a:blipFill", "a:pattFill", "a:grpFill"):
        if spPr.find(qn(tag)) is not None:
            return False  # already has its own background: leave the design alone
    fill = ET.Element(qn("a:solidFill"))
    clr = ET.SubElement(fill, qn("a:srgbClr"), val="%02X%02X%02X" % colour)
    ET.SubElement(clr, qn("a:alpha"), val=str(int(round(opacity * 100000))))
    nofill = spPr.find(qn("a:noFill"))
    if nofill is not None:
        spPr.replace(nofill, fill)
    else:
        anchor = spPr.find(qn("a:prstGeom"))
        if anchor is None:
            anchor = spPr.find(qn("a:custGeom"))
        if anchor is None:
            anchor = spPr.find(qn("a:xfrm"))
        if anchor is not None:
            anchor.addnext(fill)
        else:
            spPr.insert(0, fill)
    return True


def _gradient_stops(grad):
    out = []
    for gs in grad.iter(qn("a:gs")):
        c = gs.find(qn("a:srgbClr"))
        if c is not None and c.get("val"):
            v = c.get("val")
            out.append(tuple(int(v[i:i + 2], 16) for i in (0, 2, 4)))
    return out


def _solid_for(stops, fgs, need):
    """The gradient's own colour that passes against every failing text colour, else black or white."""
    from .contrast import ratio
    best = max(stops, key=lambda c: min(ratio(f, c) for f in fgs), default=None)
    if best is not None and min(ratio(f, best) for f in fgs) >= need + 0.1:
        return best
    for c in ((0, 0, 0), (255, 255, 255)):
        if min(ratio(f, c) for f in fgs) >= need + 0.1:
            return c
    return None


def _make_solid(parent, grad, colour):
    from lxml import etree as ET
    fill = ET.Element(qn("a:solidFill"))
    ET.SubElement(fill, qn("a:srgbClr"), val="%02X%02X%02X" % colour)
    parent.replace(grad, fill)


def replace_gradients(prs, pdf_path: str, spans: list, rep: FileReport) -> int:
    """Text that fails over a gradient (its own text box, or the slide background): the gradient is
    replaced by a solid colour that passes, taken from the gradient itself when one of its colours works."""
    import pymupdf
    failing = [s for s in spans if s["ratio"] < s["need"] and "cx" in s]
    if not failing:
        return 0
    changed = 0
    slide_w_pt = prs.slide_width / EMU_PER_PT
    with pymupdf.open(pdf_path) as doc:
        widths = [pg.rect.width for pg in doc]
    for n, slide in enumerate(prs.slides, 1):
        here = [s for s in failing if s["page"] == n]
        if not here or n > len(widths):
            continue
        scale = widths[n - 1] / slide_w_pt if slide_w_pt else 1.0
        done_shapes = set()
        for sh in slide.shapes:
            if not getattr(sh, "has_text_frame", False) or sh.left is None:
                continue
            r = [sh.left / EMU_PER_PT * scale, sh.top / EMU_PER_PT * scale,
                 (sh.left + sh.width) / EMU_PER_PT * scale, (sh.top + sh.height) / EMU_PER_PT * scale]
            inside = [s for s in here if r[0] <= s["cx"] <= r[2] and r[1] <= s["cy"] <= r[3]]
            spPr = sh._element.find(qn("p:spPr"))
            grad = spPr.find(qn("a:gradFill")) if spPr is not None else None
            if not inside or grad is None:
                continue
            colour = _solid_for(_gradient_stops(grad), [s["fg"] for s in inside], max(s["need"] for s in inside))
            if colour is not None:
                _make_solid(spPr, grad, colour)
                done_shapes.add(sh.shape_id)
                changed += 1
                rep.fix("Replaced a gradient behind failing text with a solid colour that meets WCAG contrast",
                        f"Slide {n}, {sh.name}")
        # the slide background itself
        bg = slide._element.find(qn("p:cSld")).find(qn("p:bg"))
        bgpr = bg.find(qn("p:bgPr")) if bg is not None else None
        grad = bgpr.find(qn("a:gradFill")) if bgpr is not None else None
        if grad is not None:
            colour = _solid_for(_gradient_stops(grad), [s["fg"] for s in here], max(s["need"] for s in here))
            if colour is not None:
                _make_solid(bgpr, grad, colour)
                changed += 1
                rep.fix("Replaced the slide's gradient background with a solid colour so its text meets WCAG "
                        "contrast", f"Slide {n}")
    return changed


def add_backings(prs, pdf_path: str, spans: list, rep: FileReport) -> int:
    """Text that fails on a busy picture: add the lightest translucent backing behind its box."""
    import pymupdf
    from PIL import Image
    from .contrast import luminance
    trouble = [s for s in spans if s["ratio"] < s["need"]]
    if not trouble:
        return 0
    slide_w_pt = prs.slide_width / EMU_PER_PT
    zoom = 1.0
    added = 0
    with pymupdf.open(pdf_path) as doc:
        by_slide: dict[int, list] = {}
        for sp in trouble:
            by_slide.setdefault(sp["page"], []).append(sp)
        for n, slide in enumerate(prs.slides, 1):
            if n not in by_slide or n > len(doc):
                continue
            page = doc[n - 1]
            scale = page.rect.width / slide_w_pt if slide_w_pt else 1.0  # PDF points per slide point
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            boxes = []
            for sh in slide.shapes:
                if not getattr(sh, "has_text_frame", False) or not sh.text_frame.text.strip():
                    continue
                if sh.left is None or sh.width is None:
                    continue
                r = [sh.left / EMU_PER_PT * scale, sh.top / EMU_PER_PT * scale,
                     (sh.left + sh.width) / EMU_PER_PT * scale, (sh.top + sh.height) / EMU_PER_PT * scale]
                boxes.append((sh, r))
            hits: dict[int, list] = {}
            for sp in by_slide[n]:
                # find the text box holding this text (its centre lies inside the box)
                cx = sp.get("cx")
                cy = sp.get("cy")
                if cx is None:
                    continue
                inside = [(sh, r) for sh, r in boxes if r[0] <= cx <= r[2] and r[1] <= cy <= r[3]]
                if inside:
                    sh, r = min(inside, key=lambda t: (t[1][2] - t[1][0]) * (t[1][3] - t[1][1]))
                    hits.setdefault(id(sh), [sh, r, []])[2].append(sp)
            for sh, r, sps in hits.values():
                fg = max(sps, key=lambda x: x["need"])["fg"]
                need = max(x["need"] for x in sps)
                light = luminance(fg) > 0.4
                bg = _worst_background(img, [v * zoom for v in r], fg, light)
                if bg is None:
                    continue
                opacity, colour = _backing_opacity(fg, bg, need, light)
                if _set_backing(sh, colour, opacity):
                    added += 1
                    rep.fix(f"Added a {round(opacity * 100)}% {'dark' if light else 'light'} see-through backing "
                            "behind text on a picture so it meets WCAG contrast", f"Slide {n}, {sh.name}")
    return added


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def recolor_in_place(prs, spans: list, rep: FileReport) -> int:
    """Text that fails on a plain background, recoloured where it sits: the failing piece is matched to
    its text box or table cell on that slide (by position and words), and only those runs get the team's
    standard colour closest to their own that passes on the measured background. Covers theme colours
    and colours inherited from the master, which a colour-wide change can't reach, and black text that
    fails on one dark slide but passes on white ones. Returns the number of runs recoloured."""
    from pptx.dml.color import RGBColor
    from .contrast import STANDARD_COLORS, ratio
    emu = 12700.0
    slides = list(prs.slides)
    changed = 0
    from pptx.enum.dml import MSO_FILL

    def solid(obj):
        try:
            return obj.fill.type == MSO_FILL.SOLID
        except Exception:
            return False

    for sp in spans:
        if not (1 <= sp["page"] <= len(slides)):
            continue
        key = _norm(sp["text"])[:14]
        if not key:
            continue
        short = len(key) < 3   # "$4", "#7": match only a cell holding exactly that, in that column
        slide = slides[sp["page"] - 1]
        frames = []
        for sh in _iter_shapes(slide.shapes):
            try:
                x0, y0 = sh.left / emu, sh.top / emu
                x1, y1 = x0 + sh.width / emu, y0 + sh.height / emu
            except (TypeError, AttributeError):
                continue
            is_table = getattr(sh, "has_table", False)
            bottom = 10 ** 6 if is_table else y1 + 6   # table rows grow taller than the saved frame
            if not (x0 - 6 <= sp["cx"] <= x1 + 6 and y0 - 6 <= sp["cy"] <= bottom):
                continue
            if getattr(sh, "has_text_frame", False) and not short and key in _norm(sh.text_frame.text):
                frames.append((sh.text_frame, solid(sh)))
            elif is_table:
                cols = [c.width / emu for c in sh.table.columns]
                edges = [x0]
                for w in cols:
                    edges.append(edges[-1] + w)
                col = next((k for k in range(len(cols)) if edges[k] - 3 <= sp["cx"] <= edges[k + 1] + 3), None)
                # the same words can sit in several cells: take the one in this column at the measured height
                cands, ytop = [], y0
                for row in sh.table.rows:
                    rh = (row.height or 0) / emu
                    for k, cell in enumerate(row.cells):
                        ct = _norm(cell.text_frame.text)
                        hit = key in ct and (k == col if col is not None else not short)
                        if hit:
                            try:   # table-style shading counts as a fill for cells
                                filled = cell.fill.type in (MSO_FILL.SOLID, MSO_FILL.BACKGROUND)
                            except Exception:
                                filled = False
                            cands.append((abs(ytop + rh / 2 - sp["cy"]), cell.text_frame, filled))
                    ytop += rh
                if cands:
                    best = min(cands, key=lambda c: c[0])
                    frames.append((best[1], best[2]))
        bg, fg = tuple(sp["bg"]), tuple(sp["fg"])
        from .contrast import pick_standard
        pick = pick_standard(fg, bg, sp["need"])
        if sp.get("busy"):
            # an uneven background (picture, gradient): recolour when the text sits in a filled cell or
            # shape, or when the new colour passes with a wide margin; otherwise leave it to the backings
            wide = pick is not None and ratio(pick, bg) >= sp["need"] * 1.35
            frames = [f for f in frames if f[1] or wide]
        if not frames:
            changed += _recolor_smartart(slide, sp, key, emu)
            continue
        if pick is None:
            continue
        for tf, _ in frames[:1]:   # the innermost match is enough: one text box or cell
            for para in tf.paragraphs:
                if not short and key[:6] not in _norm(para.text) and key not in _norm(tf.text)[:len(key)]:
                    continue
                if short and key not in _norm(para.text):
                    continue
                for run in para.runs:
                    if run.text.strip():
                        run.font.color.rgb = RGBColor(*pick)
                        changed += 1
    return changed


def _recolor_smartart(slide, sp: dict, key: str, emu: float) -> int:
    """SmartArt keeps its text in the diagram's data part and its cached drawing, not on the slide.
    Recolour the runs holding the failing text in both, with the standard colour that passes there."""
    from lxml import etree as _et
    from .contrast import STANDARD_COLORS, ratio
    if len(key) < 3:
        return 0
    bg, fg = tuple(sp["bg"]), tuple(sp["fg"])
    from .contrast import pick_standard
    best = pick_standard(fg, bg, sp["need"])
    if best is None or (sp.get("busy") and ratio(best, bg) < sp["need"] * 1.35):
        return 0   # an uneven background: only with a wide margin
    pick = "%02X%02X%02X" % best
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    changed = 0
    for sh in slide.shapes:
        el = sh._element
        rel = el.find(".//{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds")
        if rel is None:
            continue
        try:
            x0, y0 = sh.left / emu, sh.top / emu
            if not (x0 - 6 <= sp["cx"] <= x0 + sh.width / emu + 6 and y0 - 6 <= sp["cy"] <= y0 + sh.height / emu + 6):
                continue
        except (TypeError, AttributeError):
            continue
        parts = []
        dm = rel.get("{%s}dm" % R)
        if dm and dm in slide.part.rels:
            parts.append(slide.part.rels[dm].target_part)
        for r in slide.part.rels.values():   # the cached drawing PowerPoint (and the checker) shows
            if r.reltype.endswith("/diagramDrawing"):
                parts.append(r.target_part)
        for part in parts:
            try:
                root = _et.fromstring(part.blob)
            except Exception:
                continue
            hit = 0
            for p_el in root.iter("{%s}p" % A):
                text = "".join(t.text or "" for t in p_el.iter("{%s}t" % A))
                if key[:6] not in _norm(text):
                    continue
                for r_el in p_el.iter("{%s}r" % A):
                    rpr = r_el.find("{%s}rPr" % A)
                    if rpr is None:
                        rpr = _et.Element("{%s}rPr" % A)
                        r_el.insert(0, rpr)
                    for old in rpr.findall("{%s}solidFill" % A):
                        rpr.remove(old)
                    fill = _et.Element("{%s}solidFill" % A)
                    _et.SubElement(fill, "{%s}srgbClr" % A).set("val", pick)
                    rpr.insert(0, fill)   # a colour fill comes first among the run's properties
                    hit += 1
            if hit:
                part._blob = _et.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                changed += hit
    return changed


def fix_contrast(prs, path_for_render: str, rep: FileReport, backings: bool = True):
    from .common import soffice_convert
    from .contrast import _plan, hexrgb, pdf_text_contrast, summarize_failures

    work = tempfile.mkdtemp(prefix="pptc_")
    try:
        pdf = soffice_convert(path_for_render, work, "pdf")
        spans = pdf_text_contrast(pdf)
    except Exception as e:
        rep.errors.append(f"Contrast check could not run: {e}")
        return
    plan, busy = _plan(spans)
    changed = 0
    bboxes_known = all("cx" in sp for sp in spans)
    if plan:
        for slide in prs.slides:
            for clr in slide._element.iter(qn("a:srgbClr")):
                parent = clr.getparent()
                if parent is None or parent.tag != qn("a:solidFill"):
                    continue
                holder = parent.getparent()
                if holder is None or holder.tag not in (qn("a:rPr"), qn("a:defRPr"), qn("a:endParaRPr")):
                    continue  # only text colours, never shape fills
                rgb = tuple(int(clr.get("val", "000000")[i:i + 2], 16) for i in (0, 2, 4))
                for old, new in plan.items():
                    if all(abs(a - b) <= 3 for a, b in zip(rgb, old)):
                        clr.set("val", hexrgb(new))
                        changed += 1
                        break
    if changed:
        rep.fix(f"Adjusted {changed} low-contrast text colour(s) to meet WCAG contrast",
                ", ".join(f"#{hexrgb(a)} \u2192 #{hexrgb(b)}" for a, b in list(plan.items())[:5]))
    failing = [s for s in spans if s["ratio"] < s["need"]]
    fixed_colors = set(plan) if changed else set()
    left = [s for s in failing if s["fg"] not in fixed_colors]
    if left and bboxes_known:
        # text on a plain background: recolour it where it sits (before any backing boxes, which are
        # for text on pictures and gradients)
        try:
            n = recolor_in_place(prs, left, rep)
        except Exception as e:
            n = 0
            rep.errors.append(f"Couldn't recolour text in place: {e}")
        if n:
            rep.fix(f"Recoloured {n} piece(s) of text that failed on their slide's background (including theme and "
                    "inherited colours) with the closest standard colour that passes there; the same colour on "
                    "other slides is unchanged")
            import os as _os
            tmp_pptx = _os.path.join(work, "recolor.pptx")
            prs.save(tmp_pptx)
            try:
                pdf = soffice_convert(tmp_pptx, work, "pdf")
                again = pdf_text_contrast(pdf)
                left = [s for s in again if s["ratio"] < s["need"]]
            except Exception:
                pass
    if backings and left and bboxes_known:
        try:
            solid = replace_gradients(prs, pdf, left, rep)
        except Exception as e:
            solid = 0
            rep.errors.append(f"Couldn't replace gradients: {e}")
        try:
            added = add_backings(prs, pdf, left, rep)
        except Exception as e:
            added = 0
            rep.errors.append(f"Couldn't add text backings: {e}")
        if added or solid:
            # measure again with the backings in place
            import os as _os
            tmp_pptx = _os.path.join(work, "check.pptx")
            prs.save(tmp_pptx)
            try:
                again = pdf_text_contrast(soffice_convert(tmp_pptx, work, "pdf"))
                left = [s for s in again if s["ratio"] < s["need"]]
            except Exception:
                pass
    if left:
        rep.flag(f"{len(left)} piece(s) of text still have low contrast (theme colours, text on pictures, "
                 "or backgrounds). Check them on these slides.",
                 "; ".join(x.replace("Page", "Slide") for x in summarize_failures(left, 4)))


# ---------------------------------------------------------------- entry point

def remediate_pptx(src: str, dst: str, rep: FileReport, opts: Options, collector=None):
    prs = Presentation(src)
    fallback = os.path.splitext(os.path.basename(src))[0]
    fix_title(prs, rep, fallback)
    fix_language(prs, rep, opts.lang)
    check_titles(prs, rep)
    fix_tables(prs, rep)
    check_images(prs, rep, opts, collector, os.path.basename(dst))
    try:
        describe_media_and_templates(prs, rep)
    except Exception as e:
        rep.flag(f"Couldn't describe video previews or template pictures: {e}")
    check_links(prs, rep, opts)
    check_reading_order(prs, rep)
    if opts.fix_contrast:
        fix_contrast(prs, src, rep, backings=opts.text_backings)
    from .logic_check import pptx_logic
    try:
        pptx_logic(prs, rep)
    except Exception as e:
        rep.errors.append(f"Logic check didn't finish: {e}")
    prs.save(dst)
    return dst

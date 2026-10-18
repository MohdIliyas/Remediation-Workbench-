"""Change a paragraph's style (for meaning) without changing how it looks.

Before switching style, we work out the paragraph's effective formatting from
document defaults + its old style chain. After switching, anything the new style
would change (alignment, indents, spacing, font, size, color, bold...) is pinned
back as direct formatting. Screen readers get the new structure; the page looks
exactly as it did.
"""
from __future__ import annotations

import copy

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

# Paragraph properties that affect layout, in schema order.
PPR_ORDER = [qn(t) for t in (
    "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr",
    "w:widowControl", "w:numPr", "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs",
    "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap", "w:overflowPunct",
    "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi", "w:adjustRightInd",
    "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
    "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
    "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr",
    "w:sectPr", "w:pPrChange")]
PPR_KEEP = {qn(t) for t in ("w:spacing", "w:ind", "w:jc", "w:contextualSpacing",
                            "w:pBdr", "w:shd", "w:pageBreakBefore")}

# Run properties that affect appearance, in schema order.
RPR_ORDER = [qn(t) for t in (
    "w:rStyle", "w:rFonts", "w:b", "w:bCs", "w:i", "w:iCs", "w:caps", "w:smallCaps",
    "w:strike", "w:dstrike", "w:outline", "w:shadow", "w:emboss", "w:imprint",
    "w:noProof", "w:snapToGrid", "w:vanish", "w:webHidden", "w:color", "w:spacing",
    "w:w", "w:kern", "w:position", "w:sz", "w:szCs", "w:highlight", "w:u", "w:effect",
    "w:bdr", "w:shd", "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang",
    "w:eastAsianLayout", "w:specVanish", "w:oMath")]
RPR_KEEP = {qn(t) for t in (
    "w:rFonts", "w:b", "w:bCs", "w:i", "w:iCs", "w:caps", "w:smallCaps", "w:strike",
    "w:color", "w:spacing", "w:kern", "w:sz", "w:szCs", "w:u", "w:shd", "w:highlight",
    "w:vertAlign")}


def _neutral(tag: str, kind: str):
    """Value meaning 'not set' for a property the old look never had."""
    el = OxmlElement(tag.replace("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}", "w:"))
    local = tag.split("}")[1]
    if local in ("b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "contextualSpacing",
                 "pageBreakBefore"):
        el.set(qn("w:val"), "0")
    elif local == "color":
        el.set(qn("w:val"), "auto")
    elif local == "u":
        el.set(qn("w:val"), "none")
    elif local == "jc":
        el.set(qn("w:val"), "left")
    elif local == "spacing" and kind == "p":
        el.set(qn("w:before"), "0")
        el.set(qn("w:after"), "0")
        el.set(qn("w:line"), "240")
        el.set(qn("w:lineRule"), "auto")
    elif local == "spacing":
        el.set(qn("w:val"), "0")
    elif local == "ind":
        el.set(qn("w:left"), "0")
        el.set(qn("w:firstLine"), "0")
    elif local == "kern":
        el.set(qn("w:val"), "0")
    elif local in ("shd",):
        el.set(qn("w:val"), "clear")
        el.set(qn("w:fill"), "auto")
    elif local == "highlight":
        el.set(qn("w:val"), "none")
    elif local == "vertAlign":
        el.set(qn("w:val"), "baseline")
    else:
        return None  # pBdr, rFonts, sz etc. are handled by the defaults snapshot
    return el


def _style_chain(style):
    chain = []
    while style is not None:
        chain.append(style)
        style = style.base_style
    return list(reversed(chain))  # root first


FONT_SLOTS = ("ascii", "hAnsi", "eastAsia", "cs")


def _overlay(base, top):
    """Attribute-level inheritance; for fonts, a slot set at a higher level replaces
    both the named font and the theme font from lower levels."""
    merged = copy.deepcopy(base)
    if top.tag == qn("w:rFonts"):
        for slot in FONT_SLOTS:
            if top.get(qn(f"w:{slot}")) is not None or top.get(qn(f"w:{slot}Theme")) is not None:
                for a in (slot, f"{slot}Theme"):
                    merged.attrib.pop(qn(f"w:{a}"), None)
    for k, v in top.attrib.items():
        merged.set(k, v)
    return merged


MERGED_TAGS = (qn("w:spacing"), qn("w:ind"), qn("w:rFonts"))


def _merge(target: dict, props_el, keep: set):
    if props_el is None:
        return
    for child in props_el:
        if child.tag not in keep:
            continue
        if child.tag in target and child.tag in MERGED_TAGS:
            target[child.tag] = _overlay(target[child.tag], child)
        else:
            target[child.tag] = copy.deepcopy(child)


def _effective(doc, style, kind: str) -> dict:
    keep, tag = (PPR_KEEP, "w:pPr") if kind == "p" else (RPR_KEEP, "w:rPr")
    out: dict = {}
    dd = doc.styles.element.find(qn("w:docDefaults"))
    if dd is not None:
        holder = dd.find(qn("w:pPrDefault") if kind == "p" else qn("w:rPrDefault"))
        if holder is not None:
            _merge(out, holder.find(qn(tag)), keep)
    for st in _style_chain(style):
        _merge(out, st.element.find(qn(tag)), keep)
    return out


def _insert(parent, child, order):
    idx = order.index(child.tag) if child.tag in order else len(order)
    for i, existing in enumerate(parent):
        if existing.tag in order and order.index(existing.tag) > idx:
            parent.insert(i, child)
            return
    parent.append(child)


def _complete(el, kind: str):
    """Fill attributes the old look left at Word's defaults, so the new style can't supply them."""
    local = el.tag.split("}")[1]
    if kind == "p" and local == "spacing":
        for k, v in (("before", "0"), ("after", "0"), ("line", "240"), ("lineRule", "auto")):
            if el.get(qn(f"w:{k}")) is None:
                el.set(qn(f"w:{k}"), v)
    elif kind == "p" and local == "ind":
        for k in ("left", "right"):
            if el.get(qn(f"w:{k}")) is None and el.get(qn("w:start" if k == "left" else "w:end")) is None:
                el.set(qn(f"w:{k}"), "0")
        if el.get(qn("w:hanging")) is None and el.get(qn("w:firstLine")) is None:
            el.set(qn("w:firstLine"), "0")
    return el


def _pin(container, old: dict, new: dict, order, keep, kind: str):
    present = {c.tag: c for c in container}
    for t in keep:
        if t in present:
            # direct formatting wins, but a partial one (e.g. only "space before")
            # would let the new style fill the gaps, so fill them from the old look
            if t in MERGED_TAGS and t in old:
                full = _complete(_overlay(old[t], present[t]), kind)
                container.replace(present[t], full)
            continue
        if t in old:
            if t not in new or old[t].attrib != new[t].attrib:
                _insert(container, _complete(copy.deepcopy(old[t]), kind), order)
        elif t in new:
            neutral = _neutral(t, kind)
            if neutral is not None:
                _insert(container, neutral, order)


def set_style_keep_look(doc, paragraph, new_style):
    old_style = paragraph.style
    old_p, new_p = _effective(doc, old_style, "p"), _effective(doc, new_style, "p")
    old_r, new_r = _effective(doc, old_style, "r"), _effective(doc, new_style, "r")

    paragraph.style = new_style

    pPr = paragraph._p.get_or_add_pPr()
    _pin(pPr, old_p, new_p, PPR_ORDER, PPR_KEEP, "p")

    for r in paragraph._p.iter(qn("w:r")):
        rPr = r.find(qn("w:rPr"))
        if rPr is None:
            rPr = OxmlElement("w:rPr")
            r.insert(0, rPr)
        _pin(rPr, old_r, new_r, RPR_ORDER, RPR_KEEP, "r")

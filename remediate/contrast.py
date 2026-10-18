"""Text colour contrast (WCAG 2.1 SC 1.4.3): check and fix.

Ratios: 4.5:1 for normal text, 3:1 for large text (18pt+, or 14pt+ bold).
Fixes keep the hue and change lightness only as far as needed, so a pale blue
becomes a readable blue rather than black.
"""
from __future__ import annotations

import colorsys
import statistics
from collections import defaultdict

# ---------------------------------------------------------------- colour maths


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb) -> float:
    r, g, b = (x / 255 for x in rgb)
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ratio(a, b) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def required(size_pt: float, bold: bool) -> float:
    return 3.0 if size_pt >= 18 or (bold and size_pt >= 14) else 4.5


import threading as _threading

STANDARD_COLORS = {            # the team's replacement colours (all pass 4.5:1 on white, white passes on dark)
    "black": (0, 0, 0),
    "dark blue": (11, 61, 145),
    "dark red": (154, 28, 38),
    "dark green": (3, 100, 81),
    "white": (255, 255, 255),
}
_mode = _threading.local()


def use_standard_colors(on: bool):
    """Per worker thread: pick contrast replacements from STANDARD_COLORS first."""
    _mode.standard = bool(on)


def _standard_choice(fg, bgs, need: float):
    h, l, s = colorsys.rgb_to_hls(*(x / 255 for x in fg))
    hue = h * 360

    def hue_gap(target):
        d = abs(hue - target) % 360
        return min(d, 360 - d)
    if s < 0.2:                       # grey text: black or white
        order = ["black", "white"]
    else:
        order = sorted(["dark blue", "dark red", "dark green"],
                       key=lambda n: hue_gap({"dark blue": 220, "dark red": 355, "dark green": 165}[n]))
        order += ["black", "white"]
    for name in order:
        rgb = STANDARD_COLORS[name]
        if all(ratio(rgb, b) >= need + 0.05 for b in bgs):
            return rgb
    return None


def fix_color(fg, bgs, need: float):
    """Replacement colour that reaches `need` against every background in bgs: one of the standard
    colours when that mode is on (and one passes), otherwise the closest shade of the same hue."""
    if getattr(_mode, "standard", False):
        pick = _standard_choice(fg, bgs, need)
        if pick is not None:
            return pick
    h, l, s = colorsys.rgb_to_hls(*(x / 255 for x in fg))
    light_bg = statistics.mean(luminance(b) for b in bgs) > 0.18
    steps = [i / 100 for i in range(101)]
    candidates = [l - d for d in steps] if light_bg else [l + d for d in steps]
    for cand in candidates:
        cand = min(max(cand, 0.0), 1.0)
        rgb = tuple(round(x * 255) for x in colorsys.hls_to_rgb(h, cand, s))
        if all(ratio(rgb, b) >= need + 0.05 for b in bgs):
            return rgb
    return None


def hexrgb(rgb) -> str:
    return "%02X%02X%02X" % tuple(rgb)


def parse_hex(h: str):
    h = (h or "").strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


# ---------------------------------------------------------------- PDF: measure

def _ring_background(pix, rect, scale):
    """Median colour of a thin ring just outside the text box, and how uniform it is."""
    x0, y0, x1, y1 = [int(v * scale) for v in (rect[0], rect[1], rect[2], rect[3])]
    pad = 2
    W, H, n = pix.width, pix.height, pix.n
    samples = pix.samples
    pts = []
    for x in range(max(x0 - pad, 0), min(x1 + pad, W - 1), 2):
        for y in (max(y0 - pad, 0), min(y1 + pad, H - 1)):
            pts.append((x, y))
    for y in range(max(y0, 0), min(y1, H - 1), 2):
        for x in (max(x0 - pad, 0), min(x1 + pad, W - 1)):
            pts.append((x, y))
    cols = []
    for x, y in pts:
        i = (y * W + x) * n
        cols.append(tuple(samples[i:i + 3]))
    if not cols:
        return None, 99.0
    med = tuple(int(statistics.median(c[k] for c in cols)) for k in range(3))
    lums = sorted(luminance(c) for c in cols)
    lo, hi = lums[len(lums) // 10], lums[(len(lums) * 9) // 10]
    spread = (hi + 0.05) / (lo + 0.05)  # contrast between light and dark parts of the background
    return med, spread


def pdf_text_contrast(path: str, max_pages: int = 400) -> list[dict]:
    """Every visible text span with its colour, background, ratio and requirement."""
    import pymupdf

    out = []
    scale = 1.5
    with pymupdf.open(path) as doc:
        for pno, page in enumerate(doc):
            if pno >= max_pages:
                break
            invisible = {tuple(round(v) for v in t["bbox"]) for t in page.get_texttrace()
                         if t.get("type") == 3 or t.get("opacity", 1) == 0}
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    dx, dy = line.get("dir", (1, 0))
                    tilted = abs(dy) > 0.2 or dx < 0
                    for sp in line["spans"]:
                        if tilted and sp.get("size", 0) >= 30:
                            continue  # a watermark (large rotated text) is decorative: never recoloured
                        text = sp["text"].strip()
                        if len(text) < 2:
                            continue
                        if tuple(round(v) for v in sp["bbox"]) in invisible:
                            continue
                        c = sp["color"]
                        fg = ((c >> 16) & 255, (c >> 8) & 255, c & 255)
                        bg, spread = _ring_background(pix, sp["bbox"], scale)
                        if bg is None:
                            continue
                        bold = bool(sp["flags"] & 16) or "bold" in sp["font"].lower()
                        need = required(sp["size"], bold)
                        out.append({"page": pno + 1, "text": text[:60], "fg": fg, "bg": bg,
                                    "ratio": ratio(fg, bg), "need": need,
                                    "busy": spread > 1.5, "size": sp["size"],
                                    "cx": (sp["bbox"][0] + sp["bbox"][2]) / 2,
                                    "cy": (sp["bbox"][1] + sp["bbox"][3]) / 2})
    return out


# ---------------------------------------------------------------- PDF: fix

def _plan(spans: list[dict]):
    """Per text colour: replacement colour if it can be fixed safely, else None."""
    by_color = defaultdict(list)
    for s in spans:
        by_color[s["fg"]].append(s)
    plan, unfixable = {}, []
    for fg, group in by_color.items():
        failing = [s for s in group if s["ratio"] < s["need"]]
        if not failing:
            continue
        if any(s["busy"] for s in failing):
            unfixable += [s for s in failing if s["busy"]]
            failing = [s for s in failing if not s["busy"]]
            if not failing:
                continue
        bgs = list({s["bg"] for s in group if not s["busy"]})
        need = max(s["need"] for s in failing)
        new = fix_color(fg, bgs, need)
        if new is None:
            unfixable += failing
        else:
            plan[fg] = new
    return plan, unfixable


def pick_standard(fg, bg, need):
    """The team standard colour for text that fails on bg: neutral text (black, white, greys) becomes black
    or white, whichever passes better; coloured text keeps its family (blue -> dark blue, red/orange ->
    dark red, green/teal -> dark green) when that passes, else black or white. None if nothing passes."""
    import colorsys
    ok = {n: c for n, c in STANDARD_COLORS.items() if ratio(c, bg) >= need}
    if not ok:
        return None
    r, g, b = (v / 255 for v in fg)
    h, l, sat = colorsys.rgb_to_hls(r, g, b)
    if sat > 0.25 and 0.08 < l < 0.92:
        deg = h * 360
        family = "dark red" if deg < 50 or deg >= 300 else ("dark green" if deg < 190 else "dark blue")
        if family in ok:
            return ok[family]
    bw = [c for n, c in ok.items() if n in ("black", "white")]
    if bw:
        return max(bw, key=lambda c: ratio(c, bg))
    return max(ok.values(), key=lambda c: ratio(c, bg))


_CMYK_CACHE: dict = {}


def _cmyk_rgb(c, m, y, k):
    """CMYK -> RGB exactly as MuPDF measures text colours (its colour management), so a colour the
    checker reports can be found in the page again. Falls back to the simple formula."""
    key = tuple(round(float(v), 4) for v in (c, m, y, k))
    hit = _CMYK_CACHE.get(key)
    if hit is None:
        try:
            import pymupdf
            pix = pymupdf.Pixmap(pymupdf.csCMYK, pymupdf.IRect(0, 0, 1, 1), False)
            pix.set_pixel(0, 0, tuple(int(round(min(max(v, 0), 1) * 255)) for v in key))
            hit = tuple(pymupdf.Pixmap(pymupdf.csRGB, pix).pixel(0, 0)[:3])
        except Exception:
            hit = tuple(round(255 * (1 - min(1, x + key[3]))) for x in key[:3])
        _CMYK_CACHE[key] = hit
    return hit


def _close(a, b, tol=3):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


TEXT_SHOW = {"Tj", "TJ", "'", '"'}
FILL_PAINT = {"f", "F", "f*", "B", "B*", "b", "b*", "sh"}


def _cs_kinds(owner) -> dict:
    """Map a page's/form's named colour spaces to their channel model (ICCBased N=3 acts as RGB...)."""
    kinds = {"/DeviceRGB": "/DeviceRGB", "/DeviceGray": "/DeviceGray", "/DeviceCMYK": "/DeviceCMYK",
             "/RGB": "/DeviceRGB", "/G": "/DeviceGray", "/CMYK": "/DeviceCMYK"}
    try:
        obj = owner.obj if hasattr(owner, "obj") else owner
        res = obj.get("/Resources")
        spaces = res.get("/ColorSpace") if res is not None else None
        for key, val in (spaces.items() if spaces is not None else []):
            base = val[0] if hasattr(val, "__len__") and not isinstance(val, (str, bytes)) and len(val) else val
            base = str(base)
            if base == "/ICCBased":
                n = int(val[1].get("/N", 3))
                kinds[str(key)] = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(n, "")
            elif base in ("/DeviceRGB", "/CalRGB"):
                kinds[str(key)] = "/DeviceRGB"
            elif base in ("/DeviceGray", "/CalGray"):
                kinds[str(key)] = "/DeviceGray"
            elif base == "/DeviceCMYK":
                kinds[str(key)] = "/DeviceCMYK"
    except Exception:
        pass
    return kinds


def _op_rgb(name, vals, cs):
    if name == "rg" or (name in ("sc", "scn") and cs == "/DeviceRGB" and len(vals) == 3):
        return tuple(round(v * 255) for v in vals)
    if name == "g" or (name in ("sc", "scn") and cs == "/DeviceGray" and len(vals) == 1):
        return (round(vals[0] * 255),) * 3
    if name == "k" or (name in ("sc", "scn") and cs == "/DeviceCMYK" and len(vals) == 4):
        c, m, y, k = vals
        return _cmyk_rgb(c, m, y, k)
    return None


def _rewrite_stream(pdf, stream_owner, plan, counter):
    """Recolour fill-colour operators that are used only for text (not shapes)."""
    import pikepdf

    try:
        ops = list(pikepdf.parse_content_stream(stream_owner))
    except Exception:
        return
    kinds = _cs_kinds(stream_owner)
    # pass 1: which colour-setting ops feed text, and which feed shapes
    cs = "/DeviceGray"
    current = None
    uses: dict[int, set] = {}
    for i, (operands, op) in enumerate(ops):
        name = str(op)
        if name == "cs" and operands:
            cs = kinds.get(str(operands[0]), str(operands[0]))
        if name in ("rg", "g", "k", "sc", "scn"):
            current = i
            uses[i] = set()
        elif current is not None and name in TEXT_SHOW:
            uses[current].add("text")
        elif current is not None and name in FILL_PAINT:
            uses[current].add("shape")
        elif name == "Do" and current is not None:
            uses[current].add("shape")
    # pass 2: rewrite
    cs = "/DeviceGray"
    cs_raw = "/DeviceGray"
    changed, new_ops = False, []
    cur = None  # the active fill-colour op: (index, operands, op, cs_raw, cs)

    def shared_target(c):
        """New colour for text whose colour op also paints shapes, or None."""
        if c is None or uses.get(c[0], set()) != {"text", "shape"}:
            return None
        try:
            vals = [float(v) for v in c[1]]
        except (TypeError, ValueError):
            return None
        rgb = _op_rgb(str(c[2]), vals, c[4])
        if rgb is None:
            return None
        for old, new in plan.items():
            if _close(rgb, old):
                return new
        return None

    for i, (operands, op) in enumerate(ops):
        name = str(op)
        if name in TEXT_SHOW:
            new = shared_target(cur)
            if new is not None:
                # the colour also paints shapes: set the passing colour for this text only, then put back
                # the original colour so the shapes keep theirs
                vals = [pikepdf.Object.parse(f"{v / 255:.4f}".encode()) for v in new]
                same_space = str(cur[2]) in ("sc", "scn") and cur[4] == "/DeviceRGB"
                new_ops.append((vals, pikepdf.Operator(str(cur[2]) if same_space else "rg")))
                new_ops.append((operands, op))
                if not same_space and str(cur[2]) in ("sc", "scn"):
                    new_ops.append(([pikepdf.Name(cur[3])], pikepdf.Operator("cs")))
                new_ops.append((cur[1], cur[2]))
                changed = True
                counter[0] += 1
                continue
        if name == "cs" and operands:
            cs_raw = str(operands[0])
            cs = kinds.get(cs_raw, cs_raw)
        if i in uses and uses[i] == {"text"}:
            try:
                vals = [float(v) for v in operands]
            except (TypeError, ValueError):
                vals = None
            rgb = _op_rgb(name, vals, cs) if vals is not None else None
            if rgb is not None:
                for old, new in plan.items():
                    if _close(rgb, old):
                        if name in ("sc", "scn") and cs == "/DeviceRGB":
                            # stay in the document's own colour space (e.g. ICC-based RGB)
                            operands = [pikepdf.Object.parse(f"{v / 255:.4f}".encode()) for v in new]
                        else:
                            operands = [pikepdf.Object.parse(f"{v / 255:.4f}".encode()) for v in new]
                            op = pikepdf.Operator("rg")
                        changed = True
                        counter[0] += 1
                        break
        if name in ("rg", "g", "k", "sc", "scn"):
            cur = (i, operands, op, cs_raw, cs)
        new_ops.append((operands, op))
    if changed:
        data = pikepdf.unparse_content_stream(new_ops)
        if isinstance(stream_owner, pikepdf.Page):
            stream_owner.obj.Contents = pdf.make_stream(data)
        else:
            stream_owner.write(data)


def fix_pdf_contrast(path: str) -> dict:
    """Rewrite failing text colours in place. Returns a summary for the report."""
    import pikepdf

    spans = pdf_text_contrast(path)
    plan, unfixable = _plan(spans)
    counter = [0]
    if plan:
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            for page in pdf.pages:
                _rewrite_stream(pdf, page, plan, counter)
                res = page.obj.get("/Resources")
                xobjs = res.get("/XObject") if res is not None else None
                for _, xo in (xobjs.items() if xobjs is not None else []):
                    if xo.get("/Subtype") == "/Form":
                        _rewrite_stream(pdf, xo, plan, counter)
            pdf.save(path)
    after = pdf_text_contrast(path) if plan else spans
    still = [s for s in after if s["ratio"] < s["need"]]
    return {"colors_fixed": len(plan) if counter[0] else 0, "ops": counter[0],
            "plan": {hexrgb(k): hexrgb(v) for k, v in plan.items()},
            "remaining": still, "busy": unfixable}


def fix_local_text_contrast(path: str) -> int:
    """Per-piece contrast fix for any PDF: text pieces that still fail after the colour-by-colour fix (a
    colour that passes elsewhere, invisible text over a picture of words) get black or white where they
    sit. Visible text is changed only on an even background; other uses of the colour are untouched."""
    return fix_invisible_text_contrast(path, visible=True)


def fix_invisible_text_contrast(path: str, visible: bool = False) -> int:
    """Checkers measure even invisible text (render mode 3, such as a text layer laid over a picture of
    the words). Where such text fails, set a fill colour that passes against the background measured at
    that spot (white on dark, black on light), then put the previous colour back. Invisible, so the page
    looks exactly the same. Returns the number of text pieces changed."""
    import pikepdf
    import pymupdf
    failing = [x for x in pdf_text_contrast(path) if x["ratio"] < x["need"]]
    if not failing:
        return 0
    by_page = {}
    for x in failing:
        by_page.setdefault(x["page"], []).append(x)
    white, black = (255, 255, 255), (0, 0, 0)
    changed = 0
    with pymupdf.open(path) as d:
        heights = {i + 1: d[i].rect.height for i in range(len(d))}
        tops = {i + 1: d[i].mediabox.y1 if d[i].mediabox.y0 == 0 else d[i].mediabox.y1 for i in range(len(d))}
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for pno, spans in by_page.items():
            page = pdf.pages[pno - 1]
            if int(page.obj.get("/Rotate", 0) or 0) % 360:
                continue
            try:
                ops = pikepdf.parse_content_stream(page)
            except Exception:
                continue
            mb = [float(v) for v in page.mediabox]

            def kinds_of(res):
              cs_kinds = {"/DeviceRGB": "/DeviceRGB", "/DeviceCMYK": "/DeviceCMYK", "/DeviceGray": "/DeviceGray"}
              try:
                for nm, obj in ((res.get("/ColorSpace") if res is not None else None) or {}).items():
                    if isinstance(obj, pikepdf.Array) and len(obj) and str(obj[0]) == "/ICCBased":
                        n = int(obj[1].get("/N", 0))
                        cs_kinds[str(nm)] = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(n, "other")
                    elif isinstance(obj, pikepdf.Name):
                        cs_kinds[str(nm)] = str(obj)
              except Exception:
                pass
              return cs_kinds

            def run(ops, ctm, res, depth):
              nonlocal changed
              cs_kinds = kinds_of(res)
              out, ctm_stack = [], []
              tm, tlm, tr, lead, fs = [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 0, 0], 0, 0.0, 12.0
              cur = None
              cs_op = None
              touched = False

              def mul(a, b):
                  return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
                          a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
                          a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]

              for operands, op in ops:
                  name = str(op)
                  try:
                      vals = [float(v) for v in operands] if name in ("cm", "Tm", "Td", "TD", "TL", "Tr") else []
                  except (TypeError, ValueError):
                      vals = []
                  if name == "Do" and operands and depth < 4 and res is not None:
                      xo = (res.get("/XObject") or {}).get(str(operands[0]))
                      if xo is not None and str(xo.get("/Subtype")) == "/Form" and xo.objgen not in seen_forms:
                          seen_forms.add(xo.objgen)
                          fm = [float(v) for v in xo.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                          try:
                              f_ops = pikepdf.parse_content_stream(xo)
                          except Exception:
                              f_ops = None
                          if f_ops is not None:
                              f_out, f_touched = run(f_ops, mul(fm, ctm), xo.get("/Resources") or res, depth + 1)
                              if f_touched:
                                  xo.write(pikepdf.unparse_content_stream(f_out))
                  if name == "q":   # save/restore covers the colour too, not only the position
                      ctm_stack.append((list(ctm), cur, cs_op))
                  elif name == "Q" and ctm_stack:
                      ctm, cur, cs_op = ctm_stack.pop()
                  elif name == "cm" and len(vals) == 6:
                      ctm = mul(vals, ctm)
                  elif name == "BT":
                      tm, tlm = [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 0, 0]
                  elif name == "Tm" and len(vals) == 6:
                      tm, tlm = list(vals), list(vals)
                  elif name in ("Td", "TD") and len(vals) == 2:
                      if name == "TD":
                          lead = -vals[1]
                      tlm = mul([1, 0, 0, 1, vals[0], vals[1]], tlm)
                      tm = list(tlm)
                  elif name == "T*":
                      tlm = mul([1, 0, 0, 1, 0, -lead], tlm)
                      tm = list(tlm)
                  elif name == "TL" and vals:
                      lead = vals[0]
                  elif name == "Tr" and vals:
                      tr = int(vals[0])
                  elif name == "Tf" and len(operands) == 2:
                      try:
                          fs = float(operands[1])
                      except (TypeError, ValueError):
                          pass
                  elif name == "cs":
                      cs_op = (operands, op)
                  elif name in ("rg", "g", "k", "sc", "scn"):
                      cur = (operands, op)
                  if name in TEXT_SHOW and (tr in (3, 7) or visible):
                      m = mul(tm, ctm)
                      x, y = m[4] - mb[0], mb[3] - m[5]          # MuPDF page coordinates (top-left origin)
                      size = max(abs(fs) * (m[2] ** 2 + m[3] ** 2) ** 0.5, 4)
                      # the direction the text runs (horizontal, vertical or slanted), in page coordinates
                      dl = (m[0] ** 2 + m[1] ** 2) ** 0.5 or 1.0
                      ux, uy = m[0] / dl, -m[1] / dl
                      # the baseline sits below the letters' middle: shift the start point up by ~0.3 em
                      bx, by = x + uy * size * 0.3, y - ux * size * 0.3
                      # a measured piece covers a whole line; a text op can start anywhere along it
                      try:
                          if name == "TJ":
                              nchar = sum(len(bytes(x)) for x in operands[0] if isinstance(x, pikepdf.String))
                          else:
                              nchar = len(bytes(operands[-1])) if operands else 0
                      except Exception:
                          nchar = 0
                      op_len = nchar * size * 0.55
                      near = []
                      for sp in spans:
                          half = max(len(sp.get("text") or "") * sp.get("size", size) * 0.32, size)
                          vx, vy = sp["cx"] - bx, sp["cy"] - by
                          along = vx * ux + vy * uy
                          across = abs(vx * uy - vy * ux)
                          # this op draws its whole string; the measured piece may be any part of it
                          if across < size * 1.2 and -half - size <= along <= op_len + half + size:
                              near.append(sp)
                      fg_now = None
                      if near and tr not in (3, 7):
                          # visible text on an uneven background is recoloured only with a wide margin (below)
                          # and only when the text's colour right now is the one measured as failing there
                          cname = str(cur[1]) if cur is not None else ""
                          try:
                              cvals = [float(v) for v in cur[0]] if cname in ("rg", "g", "k", "sc", "scn") else None
                          except (TypeError, ValueError):
                              cvals = None      # a pattern or a named spot colour
                          kind = None
                          if cname in ("sc", "scn"):
                              kind = cs_kinds.get(str(cs_op[0][0]) if cs_op is not None and cs_op[0] else "", "other")
                          if cname in ("rg", "g", "k") and cvals is not None:
                              fg_now = _op_rgb(cname, cvals, None)
                          elif kind in ("/DeviceRGB", "/DeviceCMYK", "/DeviceGray") and cvals is not None:
                              fg_now = _op_rgb(cname, cvals, kind)
                          else:
                              fg_now = None
                          if fg_now is not None:
                              near = [q for q in near if _close(tuple(q["fg"]), tuple(fg_now), tol=6)]
                          elif cname in ("sc", "scn"):
                              # a spot/named colour: trust the measurement, but only for a piece right on this line
                              near = [q for q in near if abs((q["cx"] - bx) * uy - (q["cy"] - by) * ux) < size * 0.6]
                              fg_now = tuple(near[0]["fg"]) if near else None
                          else:
                              near = []
                      if near:
                          sp = min(near, key=lambda q: abs(q["cy"] - y) + abs(q["cx"] - x) / 40)
                          bg = tuple(sp["bg"])
                          if fg_now is None:
                              pick = max((white, black), key=lambda c: ratio(c, bg))
                          else:   # the team's standard colour for this text and background
                              pick = pick_standard(fg_now, bg, sp["need"]) or black
                          margin = 1.35 if (sp.get("busy") and tr not in (3, 7)) else 1.0
                          if ratio(pick, bg) >= sp["need"] * margin:
                              v = [pikepdf.Object.parse(f"{c / 255:.4f}".encode()) for c in pick]
                              out.append((v, pikepdf.Operator("rg")))
                              out.append((operands, op))
                              if cur is not None:
                                  if str(cur[1]) in ("sc", "scn") and cs_op is not None:
                                      out.append(cs_op)          # rg switched the colour space: put it back
                                  out.append(cur)
                              changed += 1
                              touched = True
                              continue
                  out.append((operands, op))
              return out, touched

            seen_forms = set()
            page_out, page_touched = run(ops, [1, 0, 0, 1, 0, 0], page.obj.get("/Resources"), 0)
            if page_touched:
                page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(page_out))
        pdf.save(path)
    return changed


def summarize_failures(spans: list[dict], limit: int = 5) -> list[str]:
    seen, out = set(), []
    for s in spans:
        key = (s["page"], s["fg"], s["bg"])
        if key in seen:
            continue
        seen.add(key)
        out.append(f"Page {s['page']}: \u201c{s['text'][:40]}\u201d is #{hexrgb(s['fg'])} on "
                   f"#{hexrgb(s['bg'])} ({s['ratio']:.1f}:1, needs {s['need']:.1f}:1)")
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- Word

def fix_docx_contrast(doc) -> dict:  # noqa: C901
    """Darken run colours that fail against white, paragraph shading, or cell shading."""
    from docx.oxml.ns import qn

    W = qn
    fixed, examples, failing_left = 0, [], 0
    body = doc.element.body

    def shading_of(el):
        shd = el.find(f"{W('w:pPr')}/{W('w:shd')}") if el.tag == W("w:p") else None
        if shd is not None:
            rgb = parse_hex(shd.get(W("w:fill")))
            if rgb:
                return rgb
        anc = el.getparent()
        while anc is not None:
            if anc.tag == W("w:tc"):
                cshd = anc.find(f"{W('w:tcPr')}/{W('w:shd')}")
                rgb = parse_hex(cshd.get(W("w:fill"))) if cshd is not None else None
                if rgb:
                    return rgb
            anc = anc.getparent()
        return (255, 255, 255)

    for p in body.iter(W("w:p")):
        bg_para = shading_of(p)
        for r in p.iter(W("w:r")):
            rpr = r.find(W("w:rPr"))
            if rpr is None:
                continue
            col = rpr.find(W("w:color"))
            fg = parse_hex(col.get(W("w:val"))) if col is not None else None
            if not fg:
                continue
            text = "".join(t.text or "" for t in r.iter(W("w:t"))).strip()
            if len(text) < 1:
                continue
            bg = bg_para
            rshd = rpr.find(W("w:shd"))
            if rshd is not None and parse_hex(rshd.get(W("w:fill"))):
                bg = parse_hex(rshd.get(W("w:fill")))
            sz = rpr.find(W("w:sz"))
            size = int(sz.get(W("w:val"))) / 2 if sz is not None and sz.get(W("w:val"), "").isdigit() else 11
            b = rpr.find(W("w:b"))
            bold = b is not None and b.get(W("w:val")) not in ("0", "false")
            need = required(size, bold)
            if ratio(fg, bg) >= need:
                continue
            new = fix_color(fg, [bg], need)
            if new is None:
                failing_left += 1
                continue
            col.set(W("w:val"), hexrgb(new))
            for attr in ("w:themeColor", "w:themeShade", "w:themeTint"):
                col.attrib.pop(W(attr), None)
            fixed += 1
            if len(examples) < 4:
                examples.append(f"\u201c{text[:30]}\u201d #{hexrgb(fg)} \u2192 #{hexrgb(new)}")
    # style colours (headings, hyperlinks, titles) checked against white
    from docx.enum.style import WD_STYLE_TYPE
    for st in doc.styles:
        if st.type not in (WD_STYLE_TYPE.PARAGRAPH, WD_STYLE_TYPE.CHARACTER):
            continue
        rpr = st.element.find(W("w:rPr"))
        col = rpr.find(W("w:color")) if rpr is not None else None
        fg = parse_hex(col.get(W("w:val"))) if col is not None else None
        if not fg:
            continue
        size = st.font.size.pt if st.font is not None and st.font.size else 11
        bold = bool(st.font.bold) if st.font is not None else False
        need = required(size, bold)
        if ratio(fg, (255, 255, 255)) >= need:
            continue
        new = fix_color(fg, [(255, 255, 255)], max(need, 4.5))  # styles may be used at small sizes
        if new is None:
            continue
        col.set(W("w:val"), hexrgb(new))
        for attr in ("w:themeColor", "w:themeShade", "w:themeTint"):
            col.attrib.pop(W(attr), None)
        fixed += 1
        if len(examples) < 4:
            examples.append(f"style \u201c{st.name}\u201d #{hexrgb(fg)} \u2192 #{hexrgb(new)}")
    return {"fixed": fixed, "examples": examples, "left": failing_left}


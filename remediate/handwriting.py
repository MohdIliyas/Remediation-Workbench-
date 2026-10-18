"""Handwriting transcription (optional, for handwritten math and notes).

1. find_regions: split each page into the boxes it is drawn in (problem cells of a test
   sheet, the typed header above them). Pages without boxes are cut into bands at blank rows.
2. transcribe: each region goes to the local vision model (Ollama), which writes Markdown
   with the math in LaTeX. Nothing leaves this computer.
3. A person reviews every region next to its image (the review page shows a rendered preview).
4. build_document: approved regions become a Word document with real Word equations
   (pandoc turns LaTeX into Word math), plus a tagged PDF.
AI transcription of handwriting makes mistakes; nothing is used until a person approves it.
"""
from __future__ import annotations

import base64
import io
import os
import re

import requests

PROMPT = (
    "This image is one part of a page from a university course: a single problem box, or a "
    "header. It may mix printed text and handwriting. Transcribe exactly what is written, in "
    "reading order, as Markdown. Write every mathematical expression in LaTeX between $ signs "
    "(for example $2y^2(3y-2)(y-4)$). Keep the problem number at the start. Put each step of "
    "worked math on its own line, and include every final answer (there may be more than one). "
    "Boxes drawn around answers are not part of the math: do not turn them into symbols. "
    "Ignore grading marks such as check marks. If a symbol is unclear, write [?] instead of "
    "guessing. Do not add numbers, bullets or list markers that are not written on the page. Write a "
    "circled letter as the letter in parentheses, for example (A). Put a title or section heading on its "
    "own line, exactly as written. Keep normal capitalisation as written. If the box contains a drawing "
    "(a diagram, graph, free-body diagram, sketch or table drawn by hand), put it on its own line where it "
    "appears, as [Diagram: what kind of drawing it is, every labelled part and what it is, how the parts "
    "are connected or move, and the values shown]. Output only the transcription."
)


# ---------------------------------------------------------------- regions

def _ink_rows(gray, threshold=170):
    import numpy as np
    return (np.asarray(gray) < threshold).sum(axis=1)


def _bands(ink, horiz, h, w, dpi):
    """No boxes: cut the page into bands of about a quarter page, each cut placed in the
    emptiest row nearby (ruled lines of notebook paper don't count as writing)."""
    import numpy as np
    writing = ink.astype("int32") - horiz.astype("int32")
    rows = np.clip(writing, 0, 1).sum(axis=1)
    inked = np.where(rows > 3)[0]
    if not len(inked):
        return []
    top, bottom = int(inked[0]), int(inked[-1])
    target = max(h // 4, int(dpi * 1.5))
    cuts, y = [top], top
    while bottom - y > target * 1.3:
        lo, hi = y + int(target * 0.7), min(y + int(target * 1.3), bottom)
        cut = lo + int(np.argmin(rows[lo:hi]))
        cuts.append(cut)
        y = cut
    cuts.append(bottom)
    return [(0, max(a - 8, 0), w, min(b + 8, h)) for a, b in zip(cuts, cuts[1:]) if b - a > dpi * 0.15]


def page_needs_transcription(page) -> bool:
    """Scanned or image-heavy pages may hold handwriting; pages of real typed text don't."""
    text = page.get_text("text").strip()
    area = page.rect.width * page.rect.height
    covered = 0.0
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        covered += max(0, x1 - x0) * max(0, y1 - y0)
    return len(text) < 80 or covered > 0.5 * area


def _sideways(png_gray) -> bool:
    """Lines of writing make horizontal stripes of ink. If the stripes run up and down instead,
    the page is lying on its side (works for handwriting, where Tesseract can't tell)."""
    import cv2
    import numpy as np
    g = np.asarray(png_gray)
    ink = (g < 170).astype("uint8")
    h, w = ink.shape
    # leave out ruled lines and box borders: only writing should count
    lines = cv2.bitwise_or(
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 6, 20), 1))),
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 6, 20)))))
    writing = np.clip(ink.astype("int16") - lines, 0, 1)
    if writing.sum() < 500:
        return False
    # smear along each direction: writing joins into stripes along its own lines
    along_rows = cv2.dilate(writing.astype("uint8"), np.ones((1, 9), "uint8"))
    along_cols = cv2.dilate(writing.astype("uint8"), np.ones((9, 1), "uint8"))

    def stripes(profile):
        p = profile.astype("float")
        return p.std() / (p.mean() + 1e-6)
    horizontal = stripes(along_rows.sum(axis=1))
    vertical = stripes(along_cols.sum(axis=0))
    return vertical > horizontal * 1.15


def ai_pick_upright(img_a, img_b, url: str, model: str):
    """Ask the local vision model which of two images is upright. Returns 0 (A), 1 (B) or None."""
    if not (url and model):
        return None
    pics = []
    for im in (img_a, img_b):
        im = im.convert("L")
        im.thumbnail((700, 700))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        pics.append(base64.b64encode(buf.getvalue()).decode())
    try:
        r = requests.post(url.rstrip("/") + "/api/generate", timeout=180, json={
            "model": model, "stream": False, "images": pics,
            "prompt": ("These two images show the same page turned two different ways. In which one is "
                       "the writing upright and readable from left to right? Answer with one letter: "
                       "A for the first image, B for the second."),
            "options": {"temperature": 0, "num_predict": 5},
        })
        r.raise_for_status()
        ans = (r.json().get("response") or "").strip().upper()
    except (requests.RequestException, ValueError):
        return None
    if ans.startswith("A"):
        return 0
    if ans.startswith("B"):
        return 1
    return None


def upright_turn(png_gray) -> int:
    """Degrees to turn a page clockwise so it is upright: Tesseract when it is confident,
    otherwise the stripe test for sideways handwriting."""
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        png_gray.save(f.name)
        try:
            out = subprocess.run(["tesseract", f.name, "-", "--psm", "0"], capture_output=True,
                                 text=True, timeout=60).stdout
        except Exception:
            return 0
        finally:
            os.unlink(f.name)
    rot = re.search(r"Rotate:\s*(\d+)", out)
    conf = re.search(r"Orientation confidence:\s*([\d.]+)", out)
    if rot and conf and float(conf.group(1)) >= 8:
        return int(rot.group(1)) % 360
    if _sideways(png_gray):
        guess = int(rot.group(1)) % 360 if rot else 0
        return guess if guess in (90, 270) else 90
    return 0


def find_regions(page, dpi: int = 200, turn: int | None = None, url: str = "", model: str = "") -> list[dict]:
    """Regions of one page, in reading order: {"box": (x0, y0, x1, y1) in pixels, "png": bytes}."""
    import cv2
    import numpy as np
    from PIL import Image

    pix = page.get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if turn is None:
        small = img.convert("L")
        small.thumbnail((1400, 1400))
        turn = upright_turn(small)
        if turn in (90, 270):
            # sideways is certain, the direction isn't: let the vision model choose
            choice = ai_pick_upright(small.rotate(-90, expand=True), small.rotate(-270, expand=True), url, model)
            if choice is not None:
                turn = (90, 270)[choice]
    turn = int(turn) % 360
    if turn:
        img = img.rotate(-turn, expand=True)  # PIL turns counter-clockwise
    gray = np.asarray(img.convert("L"))
    h, w = gray.shape
    ink = (gray < 170).astype("uint8")
    dark = (gray < 110)  # real writing, not scan bleed-through

    # long horizontal and vertical rules = the grid of a worksheet (sides may be open)
    horiz = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 4, 40), 1)))
    vert = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 8, 40))))

    def runs(idx):
        out = []
        for v in idx:
            if out and v - out[-1][1] <= 4:
                out[-1][1] = v
            else:
                out.append([v, v])
        return [(a + b) // 2 for a, b in out]

    # rules: horizontal lines at least half the page wide (text underlines are shorter)
    h_rules = []
    for y in runs(np.where(horiz.sum(axis=1) > 0.5 * w)[0]):
        xs = np.where(horiz[max(y - 3, 0):y + 4].any(axis=0))[0]
        h_rules.append((y, xs.min(), xs.max()))
    v_rules = []
    for x in runs(np.where(vert.sum(axis=0) > 0.15 * h)[0]):
        ys = np.where(vert[:, max(x - 3, 0):x + 4].any(axis=1))[0]
        v_rules.append((x, ys.min(), ys.max()))

    # lined notebook paper: many evenly spaced rules -> they are the paper, not boxes
    if len(h_rules) >= 6:
        gaps = np.diff([r[0] for r in h_rules])
        if np.median(gaps) < 0.06 * h:
            h_rules = []

    regions = []
    if len(h_rules) >= 2:
        top, bottom = h_rules[0][0], h_rules[-1][0]
        if top > 0.05 * h and dark[:top].sum() > 600:
            regions.append((0, 0, w, top))  # the typed header above the grid
        for (y0, _, _), (y1, _, _) in zip(h_rules, h_rules[1:]):
            mid = (y0 + y1) // 2
            # vertical dividers that actually run through this row
            xs = [x for x, a, b in v_rules if a <= mid <= b and 0.03 * w < x < 0.97 * w]
            edges = [0] + xs + [w]
            for x0, x1 in zip(edges, edges[1:]):
                cell = (x0 + 4, y0 + 4, x1 - 4, y1 - 4)
                if cell[2] - cell[0] < 0.1 * w or cell[3] - cell[1] < 0.03 * h:
                    continue
                if ink[cell[1]:cell[3], cell[0]:cell[2]].sum() < 80:
                    continue  # empty box
                regions.append(cell)
        if h - bottom > 0.05 * h and dark[bottom:].sum() > 600:
            regions.append((0, bottom, w, h))
    else:
        regions = _bands(ink, horiz, h, w, dpi)

    out = []
    for box in regions:
        crop = img.crop(box)
        crop.thumbnail((1400, 1400))
        buf = io.BytesIO()
        crop.save(buf, "PNG")
        out.append({"box": [int(v) for v in box], "png": buf.getvalue(), "turn": turn})
    return out


# ---------------------------------------------------------------- AI

def clean_transcript(text: str) -> str:
    """Tidy model output: drop code fences, unify display math to $$ ... $$."""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    t = t.replace("\\[", "$$").replace("\\]", "$$")
    t = t.replace("\\begin{align*}", "\\begin{aligned}").replace("\\end{align*}", "\\end{aligned}")
    t = t.replace("&\\hline", "").replace("\\hline", "")
    return t.strip()


def transcribe(png: bytes, url: str, model: str, timeout: int = 600) -> str | None:
    if not (url and model):
        return None
    try:
        r = requests.post(url.rstrip("/") + "/api/generate", timeout=timeout, json={
            "model": model, "prompt": PROMPT, "stream": False,
            "images": [base64.b64encode(png).decode()],
            "options": {"temperature": 0.1, "num_predict": 1200},
        })
        r.raise_for_status()
        return clean_transcript(r.json().get("response") or "")
    except (requests.RequestException, ValueError):
        return None


# ---------------------------------------------------------------- preview

def _preview_lines(text: str) -> list[str]:
    """Turn a transcript into simple lines that matplotlib's math renderer understands."""
    t = clean_transcript(text)
    t = re.sub(r"\\begin\{[a-z*]+\}|\\end\{[a-z*]+\}", "", t)  # drop environment markers first
    t = re.sub(r"\$\$(.+?)\$\$", lambda m: "\n".join(
        "$" + ln.replace("&", "").strip() + "$" for ln in re.split(r"\\\\", m.group(1))
        if ln.replace("&", "").strip()), t, flags=re.S)
    lines = []
    for ln in t.splitlines():
        ln = ln.strip()
        if not ln or ln in ("$", "$$"):
            continue
        ln = re.sub(r"^[#>*\-]+\s*", "", ln)
        lines.append(ln)
    return lines[:40]


def render_preview(text: str) -> bytes:
    """PNG of the transcript with the math drawn, so a reviewer can compare it with the handwriting."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    lines = _preview_lines(text) or ["(empty)"]
    fig = plt.figure(figsize=(6.5, 0.42 * len(lines) + 0.3), dpi=110)
    for i, ln in enumerate(lines):
        y = 1 - (i + 0.8) / (len(lines) + 0.3)
        try:
            fig.text(0.02, y, ln, fontsize=13, va="center")
            fig.canvas.draw()
        except Exception:
            fig.texts[-1].remove()
            fig.text(0.02, y, "\u26a0 can't draw: " + ln.replace("$", ""), fontsize=11, va="center",
                     color="#9a1c26")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------- document

_SUB = str.maketrans("0123456789+-=()aeoxijklmnprstuv", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓᵢⱼₖₗₘₙₚᵣₛₜᵤᵥ")
_SUP = str.maketrans("0123456789+-=()in", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ")


def _plain_math(expr: str) -> str | None:
    """Simple math (a symbol, a vector, a subscript or power) as ordinary characters; None if it is a
    real equation that must stay an equation."""
    e = expr.strip()
    if not e or len(e) > 40 or re.search(r"\\(frac|sum|int|sqrt|begin|left|right|lim|prod|cdot|times)", e) or "=" in e:
        return None
    e = re.sub(r"\\(vec|overrightarrow)\{([^{}])\}", lambda m: m.group(2) + "\u20d7", e)
    e = re.sub(r"\\vec\s*([A-Za-z])", lambda m: m.group(1) + "\u20d7", e)
    e = re.sub(r"\\hat\{([^{}])\}", lambda m: m.group(1) + "\u0302", e)
    e = re.sub(r"\\dot\{([^{}])\}", lambda m: m.group(1) + "\u0307", e)
    greek = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "theta": "θ", "omega": "ω", "Omega": "Ω",
             "mu": "μ", "pi": "π", "sigma": "σ", "phi": "φ", "lambda": "λ", "rho": "ρ", "tau": "τ", "Delta": "Δ"}
    e = re.sub(r"\\([A-Za-z]+)", lambda m: greek.get(m.group(1), "\x00"), e)
    if "\x00" in e:
        return None

    def sub(m):
        body = m.group(1) or m.group(2)
        out = body.translate(_SUB)
        return out if all(c not in "abcdefghijklmnopqrstuvwxyz0123456789+-=()" or c in "aeoxijklmnprstuv0123456789+-=()"
                          for c in body) and out != body or body.isdigit() else None
    for pat, table in ((r"_\{([^{}]+)\}|_(\w)", _SUB), (r"\^\{([^{}]+)\}|\^(\w)", _SUP)):
        def rep(m, table=table):
            body = m.group(1) or m.group(2)
            t = body.translate(table)
            if any(ch == c for ch, c in zip(t, body) if ch.isalpha()):
                raise ValueError
            return t
        try:
            e = re.sub(pat, rep, e)
        except ValueError:
            return None
    if re.search(r"[\\{}^_]", e):
        return None
    return e


def math_alts(regions: list) -> list[str]:
    """Spoken descriptions of the equations that stay equations, in reading order (for the PDF)."""
    out = []
    for r in regions:
        if r.get("status") != "approved":
            continue
        t = clean_transcript(r.get("final") or "")
        for m in re.finditer(r"\$\$(.+?)\$\$|\$([^$]+)\$", t, flags=re.S):
            expr = m.group(1) or m.group(2)
            if m.group(2) is not None and _plain_math(expr) is not None:
                continue
            if m.group(1) is not None:
                body = re.sub(r"\\begin\{[a-z*]+\}|\\end\{[a-z*]+\}", "", expr)
                for step in re.split(r"\\\\", body):
                    if step.replace("&", "").strip():
                        out.append(speakable("$" + step.replace("&", "") + "$"))
            else:
                out.append(speakable("$" + expr + "$"))
    return [a for a in out if a]


def _section_heading(line: str) -> bool:
    """A section title such as "1.2 MULTIPLE REFERENCE FRAMES, AND VECTORS" or "EXAMPLE:"."""
    t = line.strip().rstrip(":").strip()
    if not t or len(t) > 80 or "=" in t or "$" in t or t.startswith("!["):
        return False
    words = t.split()
    numbered = bool(re.match(r"^\d+(\.\d+)+\s+[A-Za-z]", t))
    caps = sum(1 for c in t if c.isupper()) >= 0.7 * max(1, sum(1 for c in t if c.isalpha()))
    return (numbered and len(words) <= 10) or (caps and 1 <= len(words) <= 6 and line.strip().endswith(":"))


def _title_case(line: str) -> str:
    t = line.strip().rstrip(":").strip()
    if sum(1 for c in t if c.isupper()) >= 0.7 * max(1, sum(1 for c in t if c.isalpha())):
        small = {"and", "or", "of", "the", "a", "an", "in", "on", "to", "for"}
        t = " ".join(w.lower() if i and w.lower() in small else w.capitalize() for i, w in enumerate(t.split()))
    return t


def _markdown_steps(text: str, images: list | None = None) -> list[str]:
    """One paragraph per line of work; a multi-line math block becomes one equation per step
    (screen readers read each step on its own), and problem numbers stay plain text."""
    t = clean_transcript(text)
    t = re.sub(r"\\begin\{[a-z*]+\}|\\end\{[a-z*]+\}", "", t)

    def block(m):
        steps = [ln.replace("&", "").strip() for ln in re.split(r"\\\\", m.group(1))]
        return "\n".join("$$" + st + "$$" for st in steps if st)
    t = re.sub(r"\$\$(.+?)\$\$", block, t, flags=re.S)
    out = []
    images = list(images or [])
    t = re.sub(r"(?<!\$)\$([^$\n]+)\$(?!\$)",
               lambda m: _plain_math(m.group(1)) if _plain_math(m.group(1)) is not None else m.group(0), t)
    for ln in t.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = DIAGRAM_RE.match(ln)
        if m:
            desc = m.group(1).strip().replace("[", "(").replace("]", ")")
            if images:
                path = images.pop(0)
                out += [f"![{desc}]({path})", ""]   # pandoc keeps the alt text on the picture
            else:
                out += [f"Diagram: {desc}", ""]
            continue
        ln = re.sub(r"^(\d+)\.(\s|$)", r"\1\\.\2", ln)  # "3." stays text, not a list item
        if ln.startswith("$$") and ln.endswith("$$") and ln.count("$$") == 2:
            out += [ln, ""]                    # a display equation on its own
        else:
            out += [ln, ""]
    return out


def build_document(regions: list[dict], title: str, out_docx: str, lang: str = "en-US") -> str:
    """Approved regions -> Word document with Word equations (via pandoc)."""
    import pypandoc
    from docx import Document

    approved = [r for r in regions if r.get("status") == "approved"]
    # the document's own first heading (e.g. "1.2 Multiple Reference Frames, and Vectors") makes the best
    # title; the file name is the fallback
    first_lines = [speakable(r.get("final") or "").splitlines()[:1] for r in approved]
    first = next((ln[0] for ln in first_lines if ln and _section_heading(ln[0])), "")
    if first:
        title = _title_case(first)
    parts = [f"# {title}", ""]
    for r in approved:
        steps = _markdown_steps(r.get("final") or "", r.get("diagram_paths"))
        for k, ln in enumerate(steps):
            plain = ln.replace("\\.", ".").strip()
            if plain and _section_heading(plain) and _title_case(plain) != title:
                steps[k] = "## " + _title_case(plain)
            elif plain and _title_case(plain) == title and k == 0:
                steps[k] = ""   # the title line is already the document heading
        parts += steps
    markdown = "\n".join(parts)
    pypandoc.convert_text(markdown, "docx", format="markdown+tex_math_dollars", outputfile=out_docx,
                          extra_args=[f"--metadata=lang:{lang}"])
    doc = Document(out_docx)
    doc.core_properties.title = title
    # every picture's descriptions = its alt text (pandoc puts the file path in the inner one)
    WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
    PIC = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"
    for docpr in doc.element.body.iter(f"{WP}docPr"):
        alt = docpr.get("descr", "")
        drawing = docpr.getparent()
        for c in drawing.iter(f"{PIC}cNvPr"):
            c.set("descr", alt)
            c.set("name", "Diagram")
    doc.save(out_docx)
    return out_docx


# ---------------------------------------------------------------- accessible scan from the reviewed text

def speakable(text: str) -> str:
    """Reviewed Markdown + LaTeX -> plain text a screen reader can say."""
    t = clean_transcript(text)
    t = re.sub(r"\\begin\{[a-z*]+\}|\\end\{[a-z*]+\}", "", t)
    t = t.replace("$$", "\n").replace("$", "")
    for _ in range(3):  # nested fractions
        t = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", t)
    t = re.sub(r"\\sqrt\[([^\]]*)\]\{([^{}]*)\}", r"\1th root of (\2)", t)
    t = re.sub(r"\\sqrt\{([^{}]*)\}", r"square root of (\1)", t)
    t = re.sub(r"\\(hat|vec|bar|dot)\{([^{}]*)\}", lambda m: f"{m.group(2)}-{m.group(1)}", t)
    words = {"cdot": "·", "times": "×", "div": "÷", "pm": "±", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
             "ne": "≠", "neq": "≠", "infty": "infinity", "pi": "pi", "theta": "theta", "alpha": "alpha",
             "beta": "beta", "Delta": "delta", "delta": "delta", "mu": "mu", "sigma": "sigma", "rightarrow": "→",
             "to": "→", "approx": "≈", "left": "", "right": "", "quad": " ", "qquad": " "}
    t = re.sub(r"\\([A-Za-z]+)", lambda m: words.get(m.group(1), m.group(1)), t)
    t = t.replace("\\\\", "\n").replace("&", "").replace("{", "").replace("}", "")
    t = re.sub(r"^[#>*\-]+\s*", "", t, flags=re.M)
    t = t.replace("[?]", "(unclear)")
    t = re.sub(r"\[Diagram:\s*(.+?)\]", r"Diagram: \1", t)
    return "\n".join(re.sub(r"\s+", " ", ln).strip() for ln in t.splitlines() if ln.strip())


def _looks_like_heading(line: str) -> bool:
    words = line.split()
    return (0 < len(words) <= 8 and line[:1].isalpha() and "=" not in line
            and not line.endswith((".", ",")) and sum(c.isdigit() for c in line) <= 2)


def rebuild_scan_from_transcript(src_pdf: str, out_pdf: str, regions: list, lang: str, title: str,
                                 work: str) -> dict:
    """Rebuild the same-look tagged scan, with the reviewed transcription as its text layer instead of
    OCR. Returns counts. Every page that holds content must have approved (or skipped) boxes."""
    import subprocess
    import pymupdf
    from PIL import Image
    from .ocr_rebuild import DPI
    from .tagged_scan import build_tagged_scan

    approved = [r for r in regions if r.get("status") == "approved" and (r.get("final") or "").strip()]
    if not approved:
        raise ValueError("no approved boxes")
    k = DPI / 200.0  # boxes were found at 200 dpi; the scan builder works at the OCR resolution
    words, items, wid = {}, [], 0
    first_title = None
    for r in sorted(approved, key=lambda r: (r["page"], r.get("n", 0))):
        x0, y0, x1, y1 = [v * k for v in r.get("box", [0, 0, 200 * 8.5, 200 * 11])]
        lines = speakable(r["final"]).splitlines()
        if not lines:
            continue
        lh = max(min((y1 - y0) / max(len(lines), 1), 60 * k), 12 * k)
        for li, line in enumerate(lines):
            ids = []
            toks = line.split()
            total = sum(len(t) + 1 for t in toks) or 1
            x, ytop = x0 + 6 * k, y0 + li * lh
            for tok in toks:
                w = max((x1 - x0 - 12 * k) * (len(tok) + 1) / total, 4)
                words[wid] = {"id": wid, "text": tok, "left": int(x), "top": int(ytop), "w": int(w * 0.92),
                              "h": int(lh * 0.8), "page": r["page"], "conf": 100.0}
                ids.append(wid)
                wid += 1
                x += w
            if first_title is None:
                first_title = line
                items.append({"kind": "heading", "level": 1, "words": ids})
            elif li == 0 and _looks_like_heading(line):
                items.append({"kind": "heading", "level": 2, "words": ids})
            else:
                items.append({"kind": "para", "words": ids})
    # the invisible glyphless font, made by Tesseract from a blank page
    os.makedirs(work, exist_ok=True)
    blank = os.path.join(work, "blank.png")
    Image.new("L", (300, 100), 255).save(blank)
    base = os.path.join(work, "glyphless")
    subprocess.run(["tesseract", blank, base, "-l", "eng", "--dpi", str(DPI), "-c", "textonly_pdf=1", "pdf"],
                   capture_output=True, timeout=120)
    font_pdf = base + ".pdf"
    turns = {}
    for r in approved:
        turns.setdefault(r["page"], int(r.get("turn", 0)) % 360)
    with pymupdf.open(src_pdf) as d:
        n_pages = len(d)
    scan = {"words": words, "items": items, "font_pdf": font_pdf, "title": title or first_title or "Document",
            "page_info": [{"page": p, "turn": turns.get(p, 0)} for p in range(1, n_pages + 1)]}
    build_tagged_scan(src_pdf, out_pdf, scan, lang)
    return {"lines": len(items), "headings": sum(1 for i in items if i["kind"] == "heading")}


# ---------------------------------------------------------------- diagrams

DIAGRAM_RE = re.compile(r"\[Diagram:\s*(.+?)\]\s*$", re.M)


def locate_diagrams(png: bytes, url: str, model: str) -> list:
    """Ask the vision model where the drawings are: [(x0, y0, x1, y1) in image pixels]."""
    if not (url and model):
        return []
    try:
        r = requests.post(url.rstrip("/") + "/api/generate", timeout=300, json={
            "model": model, "stream": False, "format": "json",
            "images": [base64.b64encode(png).decode()],
            "prompt": ("Find every hand-drawn diagram, graph or sketch in this image (not plain writing or "
                       "equations). Reply as JSON: {\"diagrams\": [{\"bbox_2d\": [x1, y1, x2, y2]}]} in "
                       "pixel coordinates of this image. Reply {\"diagrams\": []} if there is none."),
            "options": {"temperature": 0, "num_predict": 300},
        })
        r.raise_for_status()
        import json
        data = json.loads(r.json().get("response") or "{}")
        out = []
        for d in data.get("diagrams", []) or []:
            b = d.get("bbox_2d") or d.get("bbox") or []
            if len(b) == 4 and all(isinstance(v, (int, float)) for v in b) and b[2] > b[0] and b[3] > b[1]:
                out.append(tuple(int(v) for v in b))
        return out[:6]
    except Exception:
        return []


def crop_diagrams(png: bytes, transcript: str, url: str, model: str) -> list:
    """For a box whose transcription mentions diagrams: PNG crops of each drawing (the whole box
    when the model can't say where), paired in order with the descriptions."""
    from PIL import Image
    descs = DIAGRAM_RE.findall(transcript or "")
    if not descs:
        return []
    im = Image.open(io.BytesIO(png)).convert("RGB")
    W, H = im.size
    boxes = locate_diagrams(png, url, model)
    out = []
    for i, desc in enumerate(descs):
        b = boxes[i] if i < len(boxes) else None
        if b:
            pad = max(24, int(0.08 * max(b[2] - b[0], b[3] - b[1])))
            b = (max(b[0] - pad, 0), max(b[1] - pad, 0), min(b[2] + pad, W), min(b[3] + pad, H))
            crop = im.crop(b)
        else:
            crop = im
        buf = io.BytesIO()
        crop.save(buf, "PNG")
        out.append({"png": buf.getvalue(), "desc": desc.strip(), "whole_box": not b})
    return out


def describe_pdf_equations(pdf_path: str, alts: list[str]) -> int:
    """LibreOffice exports each Word equation as a figure without a description. Give those figures the
    equation's spoken form (in reading order), or a generic description when the counts don't line up."""
    import pikepdf
    n = 0
    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        root = pdf.Root.get("/StructTreeRoot")
        if root is None:
            return 0
        figs = []

        def walk(node):
            if isinstance(node, pikepdf.Array):
                for k in node:
                    walk(k)
            elif isinstance(node, pikepdf.Dictionary) and "/S" in node:
                if str(node.S) in ("/Figure", "/Formula") and not str(node.get("/Alt", "")).strip():
                    figs.append(node)
                walk(node.get("/K"))
        walk(root.get("/K"))
        for i, f in enumerate(figs):
            text = alts[i] if len(alts) == len(figs) else (alts[i] if i < len(alts) else "")
            f.Alt = pikepdf.String("Equation: " + text if text else "Math expression")
            n += 1
        if n:
            pdf.save(pdf_path)
    return n


# ---------------------------------------------------------------- drawings the AI didn't mention

def find_drawings(png: bytes) -> list:
    """Hand-drawn figures in a handwriting box: ink blobs much taller than a line of writing (after
    removing ruled lines and underlines), with nearby pieces merged. Returns (x0, y0, x1, y1) boxes."""
    import cv2
    import numpy as np
    from PIL import Image
    im = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    h, w = im.shape
    ink = (im < 160).astype("uint8")
    rules = cv2.bitwise_or(
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 5, 40), 1))),
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 2, 40)))))
    ink = np.clip(ink.astype("int16") - rules, 0, 1).astype("uint8")
    blob = cv2.dilate(ink, np.ones((9, 9), "uint8"))
    n, _, st, _ = cv2.connectedComponentsWithStats(blob, 8)
    heights = [st[i, 3] for i in range(1, n) if st[i, 4] > 60]
    if not heights:
        return []
    med = float(np.median(heights))
    boxes = [[int(st[i, 0]), int(st[i, 1]), int(st[i, 0] + st[i, 2]), int(st[i, 1] + st[i, 3])]
             for i in range(1, n)
             if st[i, 3] > 3.2 * med and st[i, 2] > 0.07 * w and st[i, 3] > 0.18 * h and st[i, 4] > 0.01 * w * h]
    gap = 3 * med   # pieces of one drawing sit close together: merge them
    merged = True
    while merged and len(boxes) > 1:
        merged = False
        for a in range(len(boxes)):
            for b in range(a + 1, len(boxes)):
                A, B = boxes[a], boxes[b]
                if A[0] - gap <= B[2] and B[0] - gap <= A[2] and A[1] - gap <= B[3] and B[1] - gap <= A[3]:
                    boxes[a] = [min(A[0], B[0]), min(A[1], B[1]), max(A[2], B[2]), max(A[3], B[3])]
                    boxes.pop(b)
                    merged = True
                    break
            if merged:
                break
    return [tuple(b) for b in boxes]


def add_missing_drawings(png: bytes, transcript: str, url: str, model: str) -> tuple:
    """A box whose transcription has no [Diagram: ...] line but that holds a drawing: crop each drawing,
    have the local AI describe it (the alt-text drafter), and add a [Diagram: ...] line for it.
    Returns (transcript, [{"png": bytes, "desc": str}]). A transcription that already names its
    diagrams is returned unchanged (the existing diagram handling covers it)."""
    from PIL import Image
    if not transcript or DIAGRAM_RE.search(transcript):
        return transcript, []
    boxes = find_drawings(png)
    if not boxes:
        return transcript, []
    from .alt_text import draft_alt_text
    im = Image.open(io.BytesIO(png)).convert("RGB")
    W, H = im.size
    found, lines = [], []
    for x0, y0, x1, y1 in boxes:
        pad = max(20, int(0.06 * max(x1 - x0, y1 - y0)))
        crop = im.crop((max(x0 - pad, 0), max(y0 - pad, 0), min(x1 + pad, W), min(y1 + pad, H)))
        buf = io.BytesIO()
        crop.save(buf, "PNG")
        draft = draft_alt_text(buf.getvalue(), transcript[:1200], url, model) if url and model else None
        desc = (draft or {}).get("alt") or "Hand-drawn diagram (describe it)"
        desc = desc.replace("[", "(").replace("]", ")")
        found.append({"png": buf.getvalue(), "desc": desc, "whole_box": False})
        lines.append(f"[Diagram: {desc}]")
    # put the diagram(s) after the box's first line (usually its heading), else at the top
    rows = transcript.splitlines()
    at = 1 if rows and len(rows) > 1 else 0
    return "\n".join(rows[:at] + lines + rows[at:]), found

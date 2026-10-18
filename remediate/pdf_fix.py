"""PDF remediation.

Route chosen per file:
  1. Scanned pages      -> OCR with OCRmyPDF (only pages that have no text)
  2. Already tagged     -> keep the existing tags, fix title/language/viewer settings
  3. Untagged           -> rebuild: PDF -> Word (pdf2docx) -> Word fixes ->
                           tagged PDF/UA export (LibreOffice). The editable .docx
                           is kept so a person can adjust it and re-export.
Every output PDF is then checked (tags, title, language, text on every page,
figures without alt text, links) and with veraPDF if it is installed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pikepdf

from .common import FileReport, Options, check_urls, docx_to_tagged_pdf
from .docx_fix import remediate_docx
from .ocr_rebuild import scanned_pdf_to_docx
from .rebuild_cleanup import clean_converted_docx
from .pdf_figures import crop_figures, figures_needing_alt, set_figure_alt

VERAPDF = os.environ.get("VERAPDF", shutil.which("verapdf") or "")
MIN_TEXT_CHARS = 25  # a page with fewer real characters is treated as scanned


# ---------------------------------------------------------------- analysis

def is_placeholder_title(title: str, path: str = "") -> bool:
    """Titles that are really file names or tool defaults count as missing."""
    t = title.strip().lower()
    if not t:
        return True
    stem = os.path.splitext(os.path.basename(path))[0].lower() if path else ""
    return (t == stem or re.search(r"\.(pdf|docx?|pptx?)$", t) is not None
            or ("_" in t and " " not in t) or t in {"untitled", "microsoft word", "document", "title"})

def page_texts(path: str) -> list[str]:
    out = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True,
                         text=True, timeout=600).stdout
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    return pages


def analyze(path: str) -> dict:
    with pikepdf.open(path) as pdf:
        root = pdf.Root
        mark = root.get("/MarkInfo")
        tagged = "/StructTreeRoot" in root and bool(mark and mark.get("/Marked", False))
        info = {
            "pages": len(pdf.pages),
            "tagged": tagged,
            "title": str(pdf.docinfo.get("/Title", "")).strip() if pdf.docinfo else "",
            "lang": str(root.get("/Lang", "")).strip(),
            "has_forms": "/AcroForm" in root,
            "latex": any(k in str(pdf.docinfo.get(key, "")) for key in ("/Producer", "/Creator")
                         for k in ("TeX", "LaTeX")) if pdf.docinfo else False,
        }
    if is_placeholder_title(info["title"], path):
        info["title"] = ""
    info["has_code"] = has_monospace_text(path)
    texts = page_texts(path)
    scanned = [i + 1 for i, t in enumerate(texts)
               if len(re.sub(r"\s", "", t)) < MIN_TEXT_CHARS]
    info["scanned_pages"] = scanned
    # scans that already carry an invisible OCR layer (HeinOnline, JSTOR, library scanners...)
    info["ocr_layer_pages"] = [p for p in ocr_layer_pages(path) if p not in scanned]
    image_pages = len(scanned) + len(info["ocr_layer_pages"])
    info["mostly_scanned"] = image_pages >= max(1, info["pages"] // 2)
    # pages that are really scans with a few typed notes added (e.g. red annotations on handwritten notes)
    info["scan_like_pages"] = []
    try:
        import pymupdf
        with pymupdf.open(path) as d:
            for i, pg in enumerate(d):
                if i + 1 in scanned:
                    continue
                area = pg.rect.width * pg.rect.height
                big = max((abs(pymupdf.Rect(im["bbox"])) for im in pg.get_image_info()), default=0)
                if big < 0.85 * area or len(re.sub(r"\s", "", texts[i])) >= 400 or pg.rect.height <= pg.rect.width:
                    continue   # not a full-page portrait image with only a few typed words
                # a scanned sheet of paper: mostly white, little colour (not a photo or a slide graphic)
                pix = pg.get_pixmap(dpi=20)
                px = pix.samples
                n = pix.n
                vals = [px[k:k + 3] for k in range(0, len(px) - 2, n)]
                light = sum(1 for v in vals if min(v) > 200) / max(len(vals), 1)
                if light >= 0.6:
                    info["scan_like_pages"].append(i + 1)
    except Exception:
        pass
    return info


def ocr_layer_pages(path: str) -> list[int]:
    """Pages that are one big picture with only invisible text on top."""
    try:
        import pymupdf
    except ImportError:
        return []
    found = []
    with pymupdf.open(path) as d:
        for i, page in enumerate(d):
            area = page.rect.get_area() or 1
            big = any(pymupdf.Rect(im["bbox"]).get_area() >= 0.6 * area for im in page.get_image_info())
            if not big:
                continue
            visible = invisible = 0
            for span in page.get_texttrace():
                n = len(span.get("chars", ()))
                if span.get("type") == 3 or span.get("opacity", 1) == 0:
                    invisible += n
                else:
                    visible += n
            if invisible > visible * 4 or (visible < MIN_TEXT_CHARS and invisible == 0):
                found.append(i + 1)
    return found


MONO_RE = re.compile(r"mono|courier|consol|inconsolata|cmtt|sfmono|menlo|typewriter|tt\d|code", re.I)


def has_monospace_text(path: str) -> bool:
    try:
        import pymupdf
        with pymupdf.open(path) as d:
            return any(MONO_RE.search(f[3] or "") for page in d for f in page.get_fonts())
    except Exception:
        return False


def figures_without_alt(pdf: pikepdf.Pdf) -> int:
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    count = 0
    stack = [root.get("/K")]
    seen = set()
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if isinstance(node, pikepdf.Array):
            stack.extend(list(node))
            continue
        if not isinstance(node, pikepdf.Dictionary):
            continue
        key = node.objgen
        if key != (0, 0):
            if key in seen:
                continue
            seen.add(key)
        if node.get("/S") == "/Figure" and not str(node.get("/Alt", "")).strip() \
                and not str(node.get("/ActualText", "")).strip():
            count += 1
        stack.append(node.get("/K"))
    return count


def link_urls(pdf: pikepdf.Pdf) -> list[str]:
    urls = []
    for page in pdf.pages:
        for annot in page.get("/Annots", []) or []:
            a = annot.get("/A")
            if a is not None and a.get("/S") == "/URI":
                urls.append(str(a.get("/URI")))
    return urls


# ---------------------------------------------------------------- fixes

def fix_metadata(path: str, title: str, lang: str, rep: FileReport | None = None,
                 force_title: bool = False):
    """Title (Info + XMP), /Lang, DisplayDocTitle. Leaves structure untouched."""
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        changed = []
        cur_title = str(pdf.docinfo.get("/Title", "")).strip() if pdf.docinfo else ""
        if is_placeholder_title(cur_title, path):
            cur_title = ""
        if title and (not cur_title or (force_title and cur_title != title)):
            changed.append(f"title \u201c{title}\u201d")
            cur_title = title
        if cur_title:
            # write XMP first: pikepdf syncs XMP -> Info on close, then set Info explicitly
            with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
                meta["dc:title"] = cur_title
            pdf.docinfo["/Title"] = cur_title
        if not str(pdf.Root.get("/Lang", "")).strip():
            pdf.Root.Lang = pikepdf.String(lang)
            changed.append(f"language {lang}")
        vp = pdf.Root.get("/ViewerPreferences")
        if vp is None:
            pdf.Root.ViewerPreferences = pikepdf.Dictionary()
            vp = pdf.Root.ViewerPreferences
        if not vp.get("/DisplayDocTitle", False):
            vp.DisplayDocTitle = True
            changed.append("show title in window bar")
        pdf.save(path)
    if rep and changed:
        rep.fix("Set " + ", ".join(changed))


OCR_STAMP = ("ocrmypdf", "tesseract", "fpdf2")
WORKBENCH = "Remediation Workbench"


def clear_ocr_stamp(path: str) -> bool:
    """OCR tools write their own name as the PDF's Creator. Checkers such as Ally read that label and
    score the whole file as "scanned" (0%), even when every page has real text. Replace the label;
    the text itself is untouched."""
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        stamped = any(w in str(pdf.docinfo.get(k, "")).lower() for k in ("/Creator", "/Producer") for w in OCR_STAMP)
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
            for key in ("xmp:CreatorTool", "pdf:Producer"):
                if any(w in str(meta.get(key, "")).lower() for w in OCR_STAMP):
                    meta[key] = WORKBENCH
                    stamped = True
        if not stamped:
            return False
        for k in ("/Creator", "/Producer"):
            if any(w in str(pdf.docinfo.get(k, "")).lower() for w in OCR_STAMP):
                pdf.docinfo[k] = WORKBENCH
        pdf.save(path)
    return True


def looks_handwritten(page_info: list) -> bool:
    """Handwriting, from what OCR reports: typed scans read at ~85-95% confidence with few unsure words;
    handwriting, even neat block printing, reads at 50-70% with 12% or more unsure words."""
    if not page_info:
        return False
    conf = sum(pi.get("conf", 0) for pi in page_info) / len(page_info)
    words = sum(pi.get("words", 0) for pi in page_info)
    low = sum(pi.get("low_count", 0) for pi in page_info)
    share = low / words if words else 0
    return conf < 75 or (share >= 0.12 and conf < 85)


def run_ocr(src: str, dst: str, opts: Options, rep: FileReport):
    cmd = ["ocrmypdf", "--skip-text", "--rotate-pages", "--deskew",
           "-l", opts.ocr_lang, "--output-type", "pdf", "--optimize", "1", src, dst]
    from .common import run_cmd
    r = run_cmd(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode not in (0, 10):  # 10 = PDF/A conversion warning, output still fine
        # deskew can fail on odd pages; retry plain
        cmd = ["ocrmypdf", "--skip-text", "-l", opts.ocr_lang, "--output-type", "pdf", src, dst]
        r = run_cmd(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode not in (0, 10):
            raise RuntimeError("OCR failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1])


def rebuild_via_word(src: str, out_docx: str, out_pdf: str, title: str,
                     opts: Options, rep: FileReport, collector=None):
    from pdf2docx import Converter  # imported lazily; it is slow to import

    tmp_docx = out_docx + ".raw.docx"
    cv = Converter(src)
    try:
        cv.convert(tmp_docx, multi_processing=False)
    finally:
        cv.close()
    stats = clean_converted_docx(tmp_docx, src)
    if stats["tables_unwrapped"]:
        rep.fix(f"Removed {stats['tables_unwrapped']} layout table(s) left by the conversion")
    if stats["running_removed"]:
        rep.fix(f"Removed {stats['running_removed']} repeated page header/footer line(s)")
    if stats["spaces_fixed"]:
        rep.fix(f"Restored missing word spaces in {stats['spaces_fixed']} paragraph(s)")
    if stats["unmatched"] > 5:
        rep.flag(f"{stats['unmatched']} paragraph(s) couldn't be checked for missing spaces. "
                 "Read through the _editable.docx for run-together words.")
    remediate_docx(tmp_docx, out_docx, rep, opts, title_hint=title, collector=collector)
    os.remove(tmp_docx)
    if collector is not None:
        for item in collector.items:
            item["target"]["pdf"] = os.path.basename(out_pdf)
    docx_to_tagged_pdf(out_docx, out_pdf)
    rep.fix("Rebuilt as a tagged PDF (PDF/UA export) from a cleaned-up Word version")
    rep.flag("Layout was rebuilt. Compare the new PDF with the original page by page "
             "(columns, tables, equations, and footnotes are the usual trouble spots). "
             "Fix problems in the _editable.docx and export again.")


# ---------------------------------------------------------------- checks

def verapdf_check(path: str, rep: FileReport, label: str):
    if not VERAPDF:
        return
    try:
        r = subprocess.run([VERAPDF, "--flavour", "ua1", "--format", "json", path],
                           capture_output=True, text=True, timeout=600)
        data = json.loads(r.stdout)
        jobs = data.get("report", {}).get("jobs", [])
        vr = jobs[0].get("validationResult", {}) if jobs else {}
        if isinstance(vr, list):
            vr = vr[0] if vr else {}
        if vr.get("compliant"):
            rep.fix(f"{label} passes veraPDF PDF/UA-1 check")
            return
        rules = vr.get("details", {}).get("ruleSummaries", [])
        for rule in rules[:8]:
            rep.flag(f"veraPDF: {rule.get('description', 'rule failed')} "
                     f"({rule.get('failedChecks', '?')} occurrence(s))", label)
        if len(rules) > 8:
            rep.flag(f"veraPDF reported {len(rules) - 8} more rule failure(s).", label)
    except Exception as e:  # validation is best effort
        rep.errors.append(f"veraPDF could not run: {e}")


def contrast_checks(path: str, rep: FileReport, opts: Options, label: str):
    from .contrast import fix_pdf_contrast, pdf_text_contrast, summarize_failures
    try:
        if opts.fix_contrast:
            r = fix_pdf_contrast(path)
            if r["ops"]:
                changes = ", ".join(f"#{a} \u2192 #{b}" for a, b in list(r["plan"].items())[:5])
                rep.fix(f"Adjusted {len(r['plan'])} low-contrast text colour(s) in {r['ops']} place(s)",
                        changes)
            remaining = r["remaining"]
            if remaining or r.get("busy"):   # text on uneven backgrounds too
                from .contrast import fix_invisible_text_contrast
                n = fix_invisible_text_contrast(path, visible=True)
                if n:
                    rep.fix(f"Recoloured {n} piece(s) of text that failed only where they sit (for example black text "
                            "on a dark band or picture, or an invisible text layer) with the closest standard colour "
                            "that passes there; the same colour elsewhere is unchanged")
                    remaining = [x for x in pdf_text_contrast(path) if x["ratio"] < x["need"]]
            if remaining:   # invisible text layers over pictures: recolour them (no visible change)
                from .contrast import fix_invisible_text_contrast
                n = fix_invisible_text_contrast(path)
                if n:
                    rep.fix(f"Gave {n} piece(s) of invisible text (a text layer over a picture of the words) a "
                            "colour that passes contrast; the page looks the same")
                    remaining = [x for x in pdf_text_contrast(path) if x["ratio"] < x["need"]]
        else:
            remaining = [x for x in pdf_text_contrast(path) if x["ratio"] < x["need"]]
    except Exception as e:
        rep.errors.append(f"Contrast check could not run: {e}")
        return
    busy = [x for x in remaining if x["busy"]]
    plain = [x for x in remaining if not x["busy"]]
    if busy:
        rep.flag(f"{len(busy)} piece(s) of text sit on an uneven background (a photo, gradient, or the "
                 "edge of a shaded box), so no single colour fixes them. Check these by eye.", "; ".join(summarize_failures(busy, 3)))
    if plain:
        rep.flag(f"{len(plain)} piece(s) of text still fail contrast.", "; ".join(summarize_failures(plain, 3)))


def final_checks(path: str, rep: FileReport, opts: Options, label: str, text_check: bool = True):
    try:
        if clear_ocr_stamp(path):
            rep.fix("Removed the OCR program's name from the PDF's Creator field (checkers such as Ally read it "
                    "and score the file as scanned)")
    except Exception:
        pass
    from .logic_check import pdf_logic
    try:
        pdf_logic(path, rep)  # headings, tables, alt text, reading order, form labels
    except Exception as e:
        rep.errors.append(f"Logic check didn't finish: {e}")
    info = analyze(path)
    if not info["tagged"]:
        rep.flag("Output is not tagged. Tag it in Acrobat (Accessibility > Autotag) and check reading order.", label)
    if info["scanned_pages"] and text_check:
        pages = ", ".join(map(str, info["scanned_pages"][:15]))
        rep.flag(f"No readable text found on page(s) {pages}. They may be images, blank, "
                 "or OCR could not read them.", label)
    with pikepdf.open(path) as pdf:
        n = figures_without_alt(pdf)
        urls = link_urls(pdf)
    if n and not any(r["msg"].endswith("Use the alt-text review page.") for r in rep.review):
        rep.flag(f"{n} figure(s) have no alt text.", label)
    if opts.check_links and urls:
        for url, problem in check_urls(urls).items():
            rep.flag(f"Broken link: {problem}.", url)
    contrast_checks(path, rep, opts, label)
    if info["has_forms"]:
        rep.flag("Has form fields. Check each field has a tooltip/label and a logical tab order.", label)
    verapdf_check(path, rep, label)


# ---------------------------------------------------------------- entry point

def tagged_figure_alt(path: str, rep: FileReport, collector):
    """Queue figures without alt text in an already-tagged PDF; write in model drafts."""
    if collector is None:
        return
    figs = figures_needing_alt(path)
    if not figs:
        return
    # crop and draft page by page, so alt text is written while the file is being remediated;
    # a page whose figures can't be measured within 60 seconds is skipped instead of freezing the file
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout
    by_page = {}
    for f in figs:
        by_page.setdefault(f.get("page"), []).append(f)
    drafts, done_figs, skipped = {}, [], []
    guard = ThreadPoolExecutor(max_workers=1)
    from .common import check_stop
    for pno, page_figs in by_page.items():
      check_stop()
      fut = guard.submit(crop_figures, path, page_figs)
      try:
          cropped = fut.result(timeout=60)
      except _Timeout:
          skipped.append((pno or 0) + 1)
          guard.shutdown(wait=False)
          guard = ThreadPoolExecutor(max_workers=1)
          continue
      except Exception:
          skipped.append((pno or 0) + 1)
          continue
      for f in cropped:
        done_figs.append(f)
        item = collector.add(f.get("image"), {"kind": "pdf-figure", "file": os.path.basename(path),
                                              "index": f["index"]}, f.get("context", ""),
                             f["where"] + (" (thin line or speck)" if f.get("thin") else
                                           " (part of a larger picture)" if f.get("piece") else ""),
                             decorative_hint=f.get("thin", False) or f.get("piece", False),
                             preview_bytes=f.get("preview"))
        if item["draft"] and not item["decorative_suggested"]:
            drafts[f["index"]] = item["draft"]
    guard.shutdown(wait=False)
    if skipped:
        rep.flag(f"Figures on page(s) {', '.join(map(str, skipped[:15]))} couldn't be measured in time and were "
                 "skipped. Add their alt text in Acrobat.")
    figs = done_figs
    if drafts:
        set_figure_alt(path, drafts)
        rep.fix(f"Drafted alt text for {len(drafts)} figure(s) with the local model")
    rep.flag(f"{len(figs)} figure(s) need alt text"
             + (" (drafts written in, marked unreviewed)" if drafts else "")
             + ". Use the alt-text review page.")


def remediate_pdf(src: str, outdir: str, rep: FileReport, opts: Options, collector=None):
    stem = os.path.splitext(os.path.basename(src))[0]
    work = tempfile.mkdtemp(prefix="pdf_")
    try:
        try:
            info = analyze(src)
        except pikepdf.PasswordError:
            raise RuntimeError("PDF is password-protected. Remove the password and upload it again.")

        raw_title = re.sub(r"^microsoft\s+(powerpoint|word|excel)\s*-\s*", "", info["title"], flags=re.I)
        raw_title = re.sub(r"\.(pptx?|docx?|xlsx?|pdf)$", "", raw_title, flags=re.I)
        raw_title = re.sub(r"\s{2,}", " ", raw_title.replace("_", " ")).strip()
        title = raw_title or re.sub(r"[_\-]+", " ", stem).strip().title()
        if raw_title and raw_title != info["title"]:
            info["title"] = ""  # force the cleaned title in
        current = src

        if info.get("scan_like_pages") and not info["mostly_scanned"]:
            pages = ", ".join(map(str, info["scan_like_pages"][:15]))
            rep.handwritten = True
            rep.flag(f"Page(s) {pages} are scanned images (likely handwriting) with a few typed notes on top. "
                     "Checkers only see the typed notes; screen readers can't read the scanned writing. "
                     "Use Transcribe handwriting, review the boxes, then Build typed version.")
        if info["scanned_pages"] and not info["mostly_scanned"]:
            ocr_out = os.path.join(work, "ocr.pdf")
            pages = ", ".join(map(str, info["scanned_pages"][:15]))
            try:
                run_ocr(current, ocr_out, opts, rep)
            except RuntimeError as e:
                # e.g. the PDF has damaged internal data that viewers tolerate but OCRmyPDF
                # rejects. Don't fail the whole file over a few text-less pages.
                rep.flag(f"Page(s) {pages} have no text, and OCR couldn't run on this file "
                         f"({str(e).replace('OCR failed: ', '')}). If those pages are decorative "
                         "(covers, patterns) nothing is needed; if they show text as a picture, "
                         "that text isn't readable by screen readers yet.")
            else:
                current = ocr_out
                rep.fix(f"Added OCR text to {len(info['scanned_pages'])} scanned page(s)")
                rep.flag("OCR text can contain errors (math, handwriting, small print, "
                         "poor scans). Spot-check it, e.g. by selecting and copying text.")

        if info["tagged"] and not info["scanned_pages"]:
            rep.route = "Already tagged: kept tags, fixed document settings"
            out = os.path.join(outdir, f"{stem}_accessible.pdf")
            shutil.copy(current, out)
            fix_metadata(out, title, opts.lang, rep)
            rep.flag("Existing tags were kept as-is. Check reading order and tag types "
                     "(Acrobat Reading Order tool or PAC).")
            rep.outputs.append(os.path.basename(out))
            tagged_figure_alt(out, rep, collector)
            final_checks(out, rep, opts, os.path.basename(out))
            return

        if info["mostly_scanned"]:
            rep.route = "Scanned: tagged scan (original look)"
            from .ocr_rebuild import analyze_scan, write_text_docx
            from .tagged_scan import build_tagged_scan
            name_title = title
            scan = analyze_scan(src, opts.ocr_lang, title, use_heading_title=not info["title"],
                                font_dir=work, cancel_file=opts.cancel_file)
            title = scan["title"]
            _avg = (sum(pi["conf"] for pi in scan.get("page_info", [])) / max(len(scan.get("page_info", [])), 1))
            if looks_handwritten(scan.get("page_info", [])) and not info["title"] and name_title:
                title = name_title          # handwriting: OCR guesses make a poor title
                scan["title"] = name_title
            if info["ocr_layer_pages"]:
                rep.fix(f"Recognised {len(info['ocr_layer_pages'])} page(s) as scans with a hidden "
                        "OCR layer; replaced it with a fresh, structured one")
            out_pdf = os.path.join(outdir, f"{stem}_accessible.pdf")
            try:
                if not scan["font_pdf"]:
                    raise RuntimeError("OCR font unavailable")
                r = build_tagged_scan(src, out_pdf, scan, opts.lang, straighten=opts.straighten_pages)
                rep.outputs.append(os.path.basename(out_pdf))
                rep.fix(f"Tagged the scan without changing how it looks: {scan['headings']} heading(s), "
                        f"{r['elements'] - scan['headings']} paragraph(s), reading order, title and language")
                if r["artifact_words"]:
                    rep.fix("Marked running heads, footers and page numbers as artifacts so screen "
                            "readers skip them")
                if r.get("straightened"):
                    if opts.straighten_pages:
                        rep.fix(f"Read {r['straightened']} sideways page(s) the right way up and turned "
                                "them upright for viewing")
                    else:
                        rep.fix(f"Read {r['straightened']} sideways page(s) the right way up for OCR; "
                                "the pages still look exactly as they did")
            except Exception as e:
                rep.errors.append(f"Tagged scan could not be built ({e}); use the text version.")
            txt_docx = os.path.join(outdir, f"{stem}_text-version.docx")
            txt_pdf = os.path.join(outdir, f"{stem}_text-version.pdf")
            want_text = opts.text_version or not os.path.exists(out_pdf)  # fallback if tagging failed
            try:
                if not want_text:
                    raise StopIteration
                counts = write_text_docx(scan, txt_docx)
                docx_to_tagged_pdf(txt_docx, txt_pdf)
                fix_metadata(txt_pdf, title, opts.lang, force_title=True)
                rep.outputs += [os.path.basename(txt_pdf), os.path.basename(txt_docx)]
                rep.fix(f"Also built a reflowable text version ({counts['headings']} heading(s)) for "
                        "students who prefer resizable text or e-readers")
            except StopIteration:
                pass
            except Exception as e:
                rep.errors.append(f"Text version could not be built: {e}")
            weak = [pi for pi in scan.get("page_info", []) if pi["words"] >= 5 and pi["conf"] < 75]
            empty = [pi["page"] for pi in scan.get("page_info", []) if pi["words"] < 5]
            if weak:
                rep.flag("OCR may be unreliable on page(s) " + ", ".join(
                    f"{pi['page']} ({pi['conf']:.0f}% confidence)" for pi in weak[:12])
                    + ". Check the text there, or rescan those pages at higher quality.")
            if empty:
                rep.flag("No readable text found on page(s) " + ", ".join(map(str, empty[:15]))
                         + " (blank pages, photos, or unreadable scans).")
            proof = [pi for pi in scan.get("page_info", []) if pi.get("low_count")]
            if proof:
                rep.flag("Proofread these OCR words against the original (the OCR was unsure of them): "
                         + "; ".join(f"page {pi['page']}: " + ", ".join(pi["low_words"][:5])
                                     + (f" (+{pi['low_count'] - 5} more)" if pi["low_count"] > 5 else "")
                                     for pi in proof[:10])
                         + ("; and more pages" if len(proof) > 10 else "") + ".")
            missed = [pi["page"] for pi in scan.get("page_info", []) if pi.get("uncovered")]
            if missed:
                rep.flag("Some printed areas have no OCR text on page(s) " + ", ".join(map(str, missed[:15]))
                         + ". Check them: they may be missed text, a table, a chart or a picture that needs "
                           "alt text, so that all meaningful content is captured.")
            avg = (sum(pi["conf"] for pi in scan.get("page_info", [])) / max(len(scan.get("page_info", [])), 1))
            if looks_handwritten(scan.get("page_info", [])):
                rep.handwritten = True
                rep.flag("This scan looks handwritten: OCR can't read handwriting reliably, so its text layer "
                         "is mostly guesswork. Use Transcribe handwriting (it starts on its own in Automatic "
                         "mode when the AI is on), review the boxes, then Build typed version: that also "
                         "rebuilds this PDF's text from your reviewed transcription, with diagrams described.")
            rep.flag(f"OCR confidence averages {avg:.0f}%. OCR text can contain errors (math, small print, "
                     "poor scans); spot-check it by selecting and copying a few passages.")
            rep.flag("Figures, diagrams and equations in the scan have no alt text yet, and footnotes "
                     "are read where they fall on each page.")
            main = out_pdf if os.path.exists(out_pdf) else (txt_pdf if os.path.exists(txt_pdf) else None)
            if main:
                final_checks(main, rep, opts, os.path.basename(main))
            return

        if not opts.rebuild_untagged:
            from .tag_in_place import tag_in_place
            out = os.path.join(outdir, f"{stem}_accessible.pdf")
            try:
                r = tag_in_place(current, out, opts.lang, title)
                c = r["counts"]
                rep.route = "Untagged: tagged in place (layout unchanged)"
                rep.fix(f"Tagged the PDF without changing how it looks: {c.get('headings', 0)} heading(s), "
                        f"{c.get('paragraphs', 0)} paragraph(s), {c.get('lists', 0)} list(s), "
                        f"{c.get('figures', 0)} figure(s), reading order, language")
                if c.get("artifact_text"):
                    rep.fix(f"Marked {c['artifact_text']} running head/footer/page-number item(s) as artifacts")
                if c.get("backgrounds"):
                    rep.fix(f"Marked {c['backgrounds']} full-page background picture(s) behind text as artifacts")
                if c.get("spaces_restored"):
                    rep.fix(f"Gave {c['spaces_restored']} passage(s) readable text with word spaces "
                            "(the PDF had none) for screen readers")
                if r["presentation"]:
                    rep.flag("Slide deck: each slide title was tagged as a heading. Check slides without "
                             "a clear title, and the reading order on busy slides.")
                rep.flag("Tags were added automatically from the layout. Check headings, lists and reading "
                         "order on a few pages (Acrobat Reading Order tool or PAC).")
                fix_metadata(out, title, opts.lang, rep, force_title=not info["title"])
                rep.outputs.append(os.path.basename(out))
                tagged_figure_alt(out, rep, collector)
                final_checks(out, rep, opts, os.path.basename(out))
                return
            except Exception as e:
                rep.errors.append(f"Tagging in place failed ({e}); rebuilt through Word instead.")
                if os.path.exists(out):
                    os.remove(out)
        rep.route = "Untagged: rebuilt through Word as tagged PDF"
        if info["latex"]:
            rep.flag("This PDF was made with LaTeX. The most reliable fix is from the instructor's "
                     ".tex source: recompile with a current TeX distribution and tagging turned on "
                     "(\\DocumentMetadata with tagging=on), which keeps the layout exactly.")
        if info["has_code"]:
            rep.flag("Contains code listings. Rebuilt code often loses its background, line-number "
                     "alignment and colors (check contrast). Compare every code block with the original, "
                     "or tag the original PDF in Acrobat instead of using the rebuild.")
        out_docx = os.path.join(outdir, f"{stem}_editable.docx")
        out_pdf = os.path.join(outdir, f"{stem}_accessible.pdf")
        try:
            rebuild_via_word(current, out_docx, out_pdf, title, opts, rep, collector)
            rep.outputs += [os.path.basename(out_pdf), os.path.basename(out_docx)]
            fix_metadata(out_pdf, title, opts.lang, rep)
            final_checks(out_pdf, rep, opts, os.path.basename(out_pdf))
        except Exception as e:
            # fall back to a settings-only fix so the team still gets something usable
            rep.errors.append(f"Rebuild failed ({e}); delivered original with settings fixed.")
            for f in (out_docx, out_pdf):
                if os.path.exists(f):
                    os.remove(f)
            out = os.path.join(outdir, f"{stem}_settings-fixed.pdf")
            shutil.copy(current, out)
            fix_metadata(out, title, opts.lang, rep)
            rep.outputs.append(os.path.basename(out))
            rep.route = "Untagged: rebuild failed, settings fixed only"
            final_checks(out, rep, opts, os.path.basename(out))
    finally:
        shutil.rmtree(work, ignore_errors=True)

"""Dispatch one uploaded file to the right remediation route."""
from __future__ import annotations

import os
import shutil
import tempfile
import traceback

from .common import Cancelled, FileReport, Options, docx_to_tagged_pdf, soffice_convert, stop_requested
from .docx_fix import remediate_docx
from .pdf_fix import final_checks, fix_metadata, remediate_pdf
from .alt_text import AltCollector
import re

SUPPORTED = {".pdf", ".docx", ".doc", ".pptx", ".ppt"}


def pptx_to_tagged_pdf(pptx_path: str, out_pdf: str) -> str:
    from .pptx_fix import IMPRESS_PDF_FILTER
    work = tempfile.mkdtemp(prefix="pexp_")
    try:
        produced = soffice_convert(pptx_path, work, IMPRESS_PDF_FILTER)
        shutil.move(produced, out_pdf)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out_pdf


def transcribe_handwriting(src: str, rep: FileReport, opts: Options, file_key: str, progress=None):
    """Optional: each box of each page -> local AI transcription, queued for a person to review."""
    import pymupdf
    from .handwriting import crop_diagrams, find_regions, page_needs_transcription, transcribe
    boxes = []
    with pymupdf.open(src) as doc:
        for pno, page in enumerate(doc, 1):
            if stop_requested(opts):
                raise Cancelled()
            if not page_needs_transcription(page):
                continue  # typed page: normal remediation already covers it
            for r in find_regions(page, url=opts.ollama_url, model=opts.ollama_model):
                boxes.append((pno, r))
    for n, (pno, r) in enumerate(boxes, 1):
        if stop_requested(opts):
            raise Cancelled()
        if progress is not None:
            try:
                rep.progress = f"Transcribing handwriting: box {n} of {len(boxes)}"
                progress(0, 0)
            except Exception:
                pass
        name = f"{file_key}-hw-{n}.png"
        with open(os.path.join(opts.alt_dir, name), "wb") as f:
            f.write(r["png"])
        draft = transcribe(r["png"], opts.ollama_url, opts.ollama_model)
        diagrams = []
        extra = []
        if draft:   # drawings the AI didn't mention get their own [Diagram: ...] line and crop
            from .handwriting import add_missing_drawings
            try:
                draft, extra = add_missing_drawings(r["png"], draft, opts.ollama_url, opts.ollama_model)
            except Exception:
                extra = []
        for k, d in enumerate(crop_diagrams(r["png"], draft or "", opts.ollama_url, opts.ollama_model) if not extra else extra, 1):
            dname = f"{file_key}-hw-{n}-d{k}.png"
            with open(os.path.join(opts.alt_dir, dname), "wb") as f:
                f.write(d["png"])
            diagrams.append({"image": dname, "whole_box": d["whole_box"]})
        rep.hw.append({"id": f"{file_key}-hw-{n}", "page": pno, "n": n, "image": name,
                       "draft": draft or "", "final": "", "status": "pending", "box": r["box"],
                       "failed": draft is None, "turn": r.get("turn", 0), "diagrams": diagrams})
    if boxes:
        rep.flag(f"Handwriting was transcribed by AI in {len(boxes)} box(es). AI misreads handwriting and "
                 "math: check every box against the original on the handwriting review page, then build "
                 "the typed version.")


def process_file(src: str, outdir: str, opts: Options, rep: FileReport,
                 also_pdf: bool = False, progress=None) -> FileReport:
    if getattr(opts, "auto", False):
        from .common import automatic_options
        opts, many = automatic_options(opts, src)
        if many:
            rep.flag("Large file with many pictures: AI alt-text drafts were left for later so the batch finishes "
                     "quickly. Start them with \"Draft with AI\" on the alt-text review page.")
    ext = os.path.splitext(src)[1].lower()
    stem = os.path.splitext(os.path.basename(src))[0]
    rep.status = "processing"
    file_key = re.sub(r"[^A-Za-z0-9_-]+", "_", stem)[:60] or "file"
    ai_url = opts.ollama_url if opts.ai_drafts else ""
    collector = AltCollector(opts.alt_dir or None, file_key, ai_url, opts.ollama_model) \
        if opts.alt_dir else None
    if collector is not None and progress is not None:
        collector.on_progress = progress
    from .common import watch_stop
    watch_stop(getattr(opts, "cancel_file", ""))
    if collector is not None:
        collector.stop_check = lambda: stop_requested(opts)
    from .contrast import use_standard_colors
    use_standard_colors(getattr(opts, "standard_colors", True))
    try:
        if stop_requested(opts):
            raise Cancelled()
        if ext == ".pdf":
            rep.kind = "PDF"
            remediate_pdf(src, outdir, rep, opts, collector)
            if getattr(rep, "handwritten", False):
                # a reflowed "text version" of handwriting is built from OCR guesses: leave it out
                for name in [o for o in rep.outputs if "_text-version" in o]:
                    try:
                        os.remove(os.path.join(outdir, name))
                    except OSError:
                        pass
                    rep.outputs.remove(name)
                rep.fixed = [f for f in rep.fixed if "text version" not in f.get("msg", "").lower()]
            auto_hw = getattr(opts, "auto", False) and getattr(rep, "handwritten", False)
            if (opts.handwriting or auto_hw) and opts.ollama_url and opts.alt_dir:
                transcribe_handwriting(src, rep, opts, file_key, progress)

        elif ext in (".docx", ".doc"):
            rep.kind = "Word"
            work = tempfile.mkdtemp(prefix="doc_")
            try:
                source = src
                if ext == ".doc":
                    source = soffice_convert(src, work, "docx")
                    rep.fix("Converted legacy .doc to .docx")
                out = os.path.join(outdir, f"{stem}_accessible.docx")
                remediate_docx(source, out, rep, opts, collector=collector)
                rep.outputs.append(os.path.basename(out))
                rep.route = "Word: fixed in place"
                if also_pdf:
                    pdf = os.path.join(outdir, f"{stem}_accessible.pdf")
                    docx_to_tagged_pdf(out, pdf)
                    fix_metadata(pdf, "", opts.lang)
                    rep.outputs.append(os.path.basename(pdf))
                    rep.fix("Exported a tagged PDF copy")
                    for item in (collector.items if collector else []):
                        item["target"]["pdf"] = os.path.basename(pdf)
                    final_checks(pdf, rep, Options(**{**opts.__dict__, "check_links": False}),
                                 os.path.basename(pdf))
            finally:
                shutil.rmtree(work, ignore_errors=True)
        elif ext in (".pptx", ".ppt"):
            rep.kind = "PowerPoint"
            from .pptx_fix import remediate_pptx
            work = tempfile.mkdtemp(prefix="ppt_")
            try:
                source = src
                if ext == ".ppt":
                    source = soffice_convert(src, work, "pptx")
                    rep.fix("Converted legacy .ppt to .pptx")
                out = os.path.join(outdir, f"{stem}_accessible.pptx")
                remediate_pptx(source, out, rep, opts, collector=collector)
                rep.outputs.append(os.path.basename(out))
                rep.route = "PowerPoint: fixed in place"
                if also_pdf:
                    pdf = os.path.join(outdir, f"{stem}_accessible.pdf")
                    pptx_to_tagged_pdf(out, pdf)
                    fix_metadata(pdf, "", opts.lang)
                    rep.outputs.append(os.path.basename(pdf))
                    rep.fix("Exported a tagged PDF copy")
                    for item in (collector.items if collector else []):
                        item["target"]["pdf"] = os.path.basename(pdf)
                    final_checks(pdf, rep, Options(**{**opts.__dict__, "check_links": False, "fix_contrast": False}),
                                 os.path.basename(pdf), text_check=False)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        else:
            raise RuntimeError(f"{ext or 'This file type'} isn't supported yet.")
        rep.status = "done"
    except Cancelled:
        rep.status = "cancelled"
        rep.errors.append("Stopped before this file finished. Upload it again to remediate it.")
        for name in list(rep.outputs):  # never hand out a half-finished file
            try:
                os.remove(os.path.join(outdir, name))
            except OSError:
                pass
        rep.outputs = []
    except Exception as e:
        rep.status = "error"
        rep.errors.append(str(e) or type(e).__name__)
        print(f"[error] {os.path.basename(src)}\n{traceback.format_exc()}", flush=True)
    finally:
        if collector is not None:
            rep.alt = collector.items
    return rep

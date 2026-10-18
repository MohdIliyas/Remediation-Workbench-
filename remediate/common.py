"""Shared pieces: the per-file report, processing options, and the link checker."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

import requests


@dataclass
class Options:
    lang: str = "en-US"            # document language written into files
    ocr_lang: str = "eng"          # Tesseract language code(s), e.g. "eng+spa"
    check_links: bool = True       # test every URL (the files themselves never leave this machine)
    convert_lists: bool = False    # turn typed bullets into real lists (changes indentation)
    fix_contrast: bool = True      # darken/lighten text colours that fail WCAG contrast
    text_version: bool = False     # for scans, also build a reflowed text-only version
    straighten_pages: bool = False # for scans, change how sideways pages are shown (off = keep as is)
    rebuild_untagged: bool = False # untagged PDFs: rebuild through Word instead of tagging in place
    ollama_url: str = ""           # optional local vision model for alt-text drafts
    ollama_model: str = ""
    alt_dir: str = ""              # where image previews for the review page are saved
    cancel_file: str = ""          # if this file exists, the batch was stopped
    ai_drafts: bool = True         # write AI alt-text drafts during remediation (off: draft later)
    text_backings: bool = True     # PowerPoint: see-through backing behind text that fails on pictures
    handwriting: bool = False      # PDFs: transcribe handwriting with the local AI for review
    standard_colors: bool = True   # contrast fixes use black / dark blue / dark red / dark green / white
    auto: bool = False             # automatic: the tool picks every fix each file type needs

    @classmethod
    def from_env(cls, **overrides) -> "Options":
        o = cls(
            lang=os.environ.get("DOC_LANG", "en-US"),
            ocr_lang=os.environ.get("OCR_LANG", "eng"),
            check_links=os.environ.get("CHECK_LINKS", "1") != "0",
            convert_lists=os.environ.get("CONVERT_LISTS", "0") == "1",
            fix_contrast=os.environ.get("FIX_CONTRAST", "1") != "0",
            text_version=os.environ.get("TEXT_VERSION", "0") == "1",
            straighten_pages=os.environ.get("STRAIGHTEN_PAGES", "0") == "1",
            rebuild_untagged=os.environ.get("REBUILD_UNTAGGED", "0") == "1",
            ai_drafts=os.environ.get("AI_DRAFTS", "1") != "0",
            text_backings=os.environ.get("TEXT_BACKINGS", "1") != "0",
            standard_colors=os.environ.get("STANDARD_COLORS", "1") != "0",
            ollama_url=os.environ.get("OLLAMA_URL", ""),
            ollama_model=os.environ.get("OLLAMA_MODEL", ""),
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(o, k, v)
        return o


def format_duration(seconds) -> str:
    """'45 s', '3 min 07 s', '1 h 02 min'."""
    try:
        sec = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if sec < 60:
        return f"{sec} s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


AUTO_IMAGE_LIMIT = 60  # above this many pictures, AI drafts are left for "Draft with AI" on the review page


def count_images(path: str) -> int:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            import pymupdf
            with pymupdf.open(path) as d:
                return sum(len(p.get_images()) for p in d)
        if ext in (".docx", ".pptx"):
            import zipfile
            with zipfile.ZipFile(path) as z:
                return sum(1 for n in z.namelist() if "/media/" in n)
    except Exception:
        return 0
    return 0


def automatic_options(opts, path: str):
    """Everything a file of this type needs, so nobody has to know which boxes to tick."""
    import dataclasses
    ext = os.path.splitext(path)[1].lower()
    many = False   # no large-file deferral: drafting happens during processing for every file
    return dataclasses.replace(
        opts,
        fix_contrast=True,
        standard_colors=True,
        check_links=True,
        ai_drafts=True,      # drafts are written while the file is processed, every image (as in 9.29)
        text_backings=ext in (".pptx", ".ppt"),
        convert_lists=ext in (".docx", ".doc"),
        text_version=ext == ".pdf",       # only used for scans: the reflowed copy scores highest in Ally
        rebuild_untagged=False,           # tag in place: the layout stays exactly the same
        straighten_pages=False,
        handwriting=False,                # started from the results page when wanted (slow, needs review)
    ), many


class Cancelled(BaseException):
    """Raised at a safe point when someone pressed Stop remediating."""


def stop_requested(opts) -> bool:
    path = getattr(opts, "cancel_file", "")
    return bool(path) and os.path.exists(path)


import threading as _threading
import signal as _signal
_stop = _threading.local()


def watch_stop(cancel_file: str):
    """Remember, for this worker thread, the file that appears when someone presses Stop."""
    _stop.path = cancel_file or ""


def check_stop():
    """Raise Cancelled if this worker's batch was stopped (cheap: one file check)."""
    path = getattr(_stop, "path", "")
    if path and os.path.exists(path):
        raise Cancelled()


def run_cmd(cmd, timeout=600, check=False, capture_output=True, text=False, **kw):
    """Like subprocess.run, but a running program is ended within about a second of Stop."""
    path = getattr(_stop, "path", "")
    if not path:
        return subprocess.run(cmd, timeout=timeout, check=check, capture_output=capture_output, text=text, **kw)
    import time as _time
    pipe = subprocess.PIPE if capture_output else None
    proc = subprocess.Popen(cmd, stdout=pipe, stderr=pipe, text=text, start_new_session=True, **kw)
    start = _time.time()
    while True:
        try:
            out, err = proc.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            stopped = os.path.exists(path)
            if stopped or _time.time() - start > timeout:
                try:
                    os.killpg(proc.pid, _signal.SIGKILL)   # the program and anything it started
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.communicate()
                if stopped:
                    raise Cancelled()
                raise subprocess.TimeoutExpired(cmd, timeout)
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


@dataclass
class FileReport:
    filename: str
    kind: str = ""
    status: str = "queued"          # queued | processing | done | error
    route: str = ""                 # short description of what path the file took
    fixed: list = field(default_factory=list)    # things the tool fixed
    review: list = field(default_factory=list)   # things a person must check
    errors: list = field(default_factory=list)
    outputs: list = field(default_factory=list)  # output file names
    alt: list = field(default_factory=list)      # images queued for alt-text review
    progress: str = ""                           # shown while the file is being processed
    started_at: str = ""                         # when work on this file began
    hw: list = field(default_factory=list)       # handwriting boxes awaiting review
    handwritten: bool = False                    # a scan whose OCR says it is mostly handwriting
    seconds: float = 0.0                         # how long remediation took

    def fix(self, msg: str, where: str = ""):
        self.fixed.append({"msg": msg, "where": where})

    def flag(self, msg: str, where: str = ""):
        self.review.append({"msg": msg, "where": where})

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------- links

VAGUE_LINK_TEXT = {
    "click here", "click", "here", "link", "this link", "read more", "more",
    "learn more", "go here", "this", "see here", "website", "click this link",
}


def is_vague_link_text(text: str) -> bool:
    t = text.strip().lower().strip(".:!>» ")
    return t in VAGUE_LINK_TEXT


def looks_like_raw_url(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith(("http://", "https://", "www."))


def check_urls(urls: list[str], timeout: float = 8.0) -> dict[str, str]:
    """Return {url: problem} for URLs that look broken. Only the URL is requested."""
    headers = {"User-Agent": "Mozilla/5.0 (accessibility link check)"}

    def one(url):
        if not url.lower().startswith(("http://", "https://")):
            return url, None  # mailto:, internal anchors, etc.
        try:
            r = requests.head(url, allow_redirects=True, timeout=timeout, headers=headers)
            if r.status_code in (403, 405, 429) or r.status_code >= 500:
                r = requests.get(url, allow_redirects=True, timeout=timeout,
                                 headers=headers, stream=True)
                r.close()
            if r.status_code in (401, 403):
                return url, (f"returned HTTP {r.status_code} (the site may just block automated "
                             "checks; open it in a browser to confirm)")
            if r.status_code >= 400:
                return url, f"returned HTTP {r.status_code}"
            return url, None
        except requests.RequestException as e:
            return url, f"could not be reached ({type(e).__name__})"

    unique = sorted(set(urls))
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = ex.map(one, unique)
    return {u: p for u, p in results if p}


# ---------------------------------------------------------------- LibreOffice

SOFFICE = os.environ.get("SOFFICE", shutil.which("soffice") or shutil.which("libreoffice") or "soffice")

PDF_UA_FILTER = (
    'pdf:writer_pdf_Export:{'
    '"UseTaggedPDF":{"type":"boolean","value":"true"},'
    '"PDFUACompliance":{"type":"boolean","value":"true"},'
    '"ExportBookmarks":{"type":"boolean","value":"true"},'
    '"ExportFormFields":{"type":"boolean","value":"false"}'
    '}'
)


def soffice_convert(src: str, outdir: str, target: str, timeout: int = 600) -> str:
    """Run LibreOffice headless with its own profile so parallel jobs don't collide."""
    profile = tempfile.mkdtemp(prefix="lo_profile_")
    try:
        cmd = [
            SOFFICE, "--headless", "--norestore",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", target, "--outdir", outdir, src,
        ]
        run_cmd(cmd, check=True, capture_output=True, timeout=timeout)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    ext = target.split(":")[0]
    out = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + "." + ext)
    if not os.path.exists(out):
        raise RuntimeError(f"LibreOffice did not produce {os.path.basename(out)}")
    return out


def docx_to_tagged_pdf(docx_path: str, out_pdf: str) -> str:
    workdir = tempfile.mkdtemp(prefix="export_")
    try:
        produced = soffice_convert(docx_path, workdir, PDF_UA_FILTER)
        shutil.move(produced, out_pdf)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return out_pdf


def new_id() -> str:
    return uuid.uuid4().hex[:12]

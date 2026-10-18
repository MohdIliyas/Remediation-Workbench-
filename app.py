"""Remediation Workbench: a small local web app for batch accessibility remediation.

Run:  python app.py        (then open http://<this-machine>:8080)
All processing happens on this machine. See README.md for settings.
"""
from __future__ import annotations

import csv
import io
import json
import re
import os
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import wraps

from flask import (Flask, Response, send_file, abort, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from remediate import __version__


def _use_eastern_time():
    """Show and record times in Eastern Time (EST/EDT) instead of the container's UTC.
    APP_TZ can name another zone. Uses the system zone files, or Python's tzdata package."""
    zone = os.environ.get("APP_TZ", "America/New_York")
    path = os.path.join("/usr/share/zoneinfo", *zone.split("/"))
    if not os.path.exists(path):
        try:
            import tzdata
            path = os.path.join(os.path.dirname(tzdata.__file__), "zoneinfo", *zone.split("/"))
        except ImportError:
            path = ""
    if path and os.path.exists(path):
        os.environ["TZ"] = path
        time.tzset()


_use_eastern_time()
from remediate.alt_apply import apply_decisions
from remediate.alt_text import draft_alt_text, ollama_status
from remediate.common import FileReport, Options, format_duration, new_id
from remediate.pipeline import SUPPORTED, process_file

DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", "data"))
WORKERS = int(os.environ.get("WORKERS", "2"))
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "7"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1000"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


app.jinja_env.filters["duration"] = format_duration


@app.context_processor
def inject_version():
    return {"app_version": __version__}
executor = ThreadPoolExecutor(max_workers=WORKERS)
locks: dict[str, threading.Lock] = {}
locks_guard = threading.Lock()
active: set = set()            # (job_id, file index) handed to a worker in this run of the app
active_guard = threading.Lock()


# ---------------------------------------------------------------- auth (optional)

def require_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if APP_PASSWORD:
            auth = request.authorization
            if not auth or auth.password != APP_PASSWORD:
                return Response("Sign in required.", 401,
                                {"WWW-Authenticate": 'Basic realm="Remediation Workbench"'})
        return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- job storage

def job_dir(job_id: str) -> str:
    safe = secure_filename(job_id)
    if not safe or safe != job_id:
        abort(404)
    return os.path.join(DATA_DIR, "jobs", safe)


def job_lock(job_id: str) -> threading.Lock:
    with locks_guard:
        return locks.setdefault(job_id, threading.Lock())


def load_job(job_id: str) -> dict:
    path = os.path.join(job_dir(job_id), "job.json")
    if not os.path.exists(path):
        abort(404)
    with open(path) as f:
        return json.load(f)


def save_job(job: dict):
    path = os.path.join(job_dir(job["id"]), "job.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(job, f, separators=(",", ":"))   # compact: big batches save much faster
    os.replace(tmp, path)


def update_file(job_id: str, index: int, rep: FileReport):
    with job_lock(job_id):
        job = load_job(job_id)
        job["files"][index] = rep.to_dict()
        if all(f["status"] in ("done", "error", "cancelled") for f in job["files"]):
            job["finished"] = datetime.now().isoformat(timespec="seconds")
            save_job(job)
            write_reports(job)
            try:
                log_history(job)
            except Exception:
                pass
            _auto_draft_if_needed(job)
        else:
            save_job(job)


DRAFT_PARALLEL = max(1, int(os.environ.get("DRAFT_PARALLEL", "1")))  # one draft at a time, as in 9.29
DRAFT_TIMEOUT = max(60, int(os.environ.get("DRAFT_TIMEOUT", "180")))  # seconds per draft, as in 9.29

ALT_MSG = re.compile(r"Needs alt text \(|image\(s\) need alt text|figure\(s\) still have no meaningful alt text|"
                     r"figure\(s\) have no alt text|figure\(s\) need alt text|Large file with many pictures|"
                     r"^Picture needs alt text|picture\(s\) need alt text|image\(s\) still need review on the alt-text")


def refresh_alt_review(job: dict):
    """Keep each file's checklist in step with its images: the alt-text lines written at processing
    time are replaced by one live line, or removed once every image is approved or decorative."""
    for fr in job["files"]:
        alt = fr.get("alt", [])
        if not alt:
            continue
        keep = [r for r in fr.get("review", []) if not ALT_MSG.search(r.get("msg", ""))]
        pending = [a for a in alt if a.get("status") == "pending"]
        if pending:
            ready = sum(1 for a in pending if a.get("draft"))
            msg = f"{len(pending)} image(s) still need review on the alt-text review page"
            if job.get("drafting", {}).get("running"):
                msg += " (AI drafts are being written now)"
            elif ready:
                msg += f" ({ready} with AI drafts ready to approve)"
            keep.append({"msg": msg + ".", "where": ""})
        fr["review"] = keep


def update_history_counts(job: dict):
    """Rewrite this batch's history lines with the files' current review counts."""
    if not os.path.exists(HISTORY_PATH):
        return
    with history_guard:
        with open(HISTORY_PATH) as f:
            lines = f.read().splitlines()
        out = []
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                out.append(ln)
                continue
            if r.get("batch") == job["id"] and r.get("index", -1) < len(job["files"]):
                fr = job["files"][r["index"]]
                r["review"] = len(fr.get("review", []))
                r["fixed"] = len(fr.get("fixed", []))
                ln = json.dumps(r)
            out.append(ln)
        with open(HISTORY_PATH, "w") as f:
            f.write("\n".join(out) + "\n")


def _alt_changed(job: dict):
    """After drafts or reviews: refresh checklists, reports and history."""
    refresh_alt_review(job)
    save_job(job)
    if job.get("finished"):
        write_reports(job)
        try:
            update_history_counts(job)
            archive_history(job, force=True)
        except Exception:
            pass


def _auto_draft_if_needed(job: dict):
    """Images left without a draft (big files defer drafting so the batch finishes fast): start
    writing their drafts in the background right away, so nobody has to press Draft with AI.
    Called with the batch lock held; the drafting thread waits for it."""
    try:
        undrafted = [it for fr in job["files"] for it in fr.get("alt", [])
                     if it["status"] == "pending" and not it.get("draft") and not it.get("tiny")
                     and it.get("ai_image")]
        if not undrafted or job.get("drafting", {}).get("running"):
            return
        d = Options.from_env()
        if not ollama_status(d.ollama_url, d.ollama_model).get("on"):
            return
        job["drafting"] = {"running": True, "done": 0, "total": len(undrafted), "auto": True}
        refresh_alt_review(job)
        save_job(job)
        threading.Thread(target=_draft_later, args=(job["id"],), daemon=True).start()
    except Exception:
        pass


HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
history_guard = threading.Lock()


def batch_seconds(job: dict) -> float:
    try:
        a = datetime.fromisoformat(job["created"])
        b = datetime.fromisoformat(job["finished"])
        return max((b - a).total_seconds(), 0)
    except (KeyError, TypeError, ValueError):
        return 0.0


def _logged_batches() -> set:
    ids = set()
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            for line in f:
                try:
                    ids.add(json.loads(line)["batch"])
                except (ValueError, KeyError):
                    continue
    return ids


HISTORY_DIR = os.path.join(DATA_DIR, "history")
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "365"))  # 0 = keep forever


def archive_history(job: dict, force: bool = False):
    """Copy a finished batch's originals, remediated files and report into the history folder,
    so history keeps the actual files after the batch is deleted or cleaned up."""
    if not job.get("finished"):
        return
    src = job_dir(job["id"])
    dst = os.path.join(HISTORY_DIR, job["id"])
    if os.path.isdir(dst) and not force:
        return
    os.makedirs(os.path.join(dst, "in"), exist_ok=True)
    os.makedirs(os.path.join(dst, "out"), exist_ok=True)
    for fr in job["files"]:
        if fr.get("status") == "deleted":
            continue
        pairs = [(os.path.join(src, "in", fr["filename"]), os.path.join(dst, "in", fr["filename"]))]
        pairs += [(os.path.join(src, "out", o), os.path.join(dst, "out", o)) for o in fr.get("outputs", [])]
        for a, b in pairs:
            if os.path.isfile(a):
                shutil.copy2(a, b)
    for rpt in ("remediation-report.html", "remediation-report.csv"):
        a = os.path.join(src, "out", rpt)
        if os.path.isfile(a):
            shutil.copy2(a, os.path.join(dst, rpt))
    with open(os.path.join(dst, "batch.json"), "w") as f:
        json.dump(job, f)


def cleanup_history():
    if HISTORY_DAYS <= 0 or not os.path.isdir(HISTORY_DIR):
        return
    cutoff = time.time() - HISTORY_DAYS * 86400
    for name in os.listdir(HISTORY_DIR):
        path = os.path.join(HISTORY_DIR, name)
        if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)


def log_history(job: dict):
    """Add a finished batch's files to the history log (once). The log survives deletion."""
    if not job.get("finished"):
        return
    try:
        archive_history(job)
    except Exception:
        pass
    with history_guard:
        if job["id"] in _logged_batches():
            return
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, "a") as f:
            for i, fr in enumerate(job["files"]):
                if fr.get("status") == "deleted":
                    continue
                f.write(json.dumps({
                    "original": fr.get("filename", ""),
                    "batch": job["id"], "label": job.get("label", ""), "created": job.get("created", ""),
                    "finished": job.get("finished", ""), "index": i, "file": fr.get("filename", ""),
                    "kind": fr.get("kind", ""), "status": fr.get("status", ""), "route": fr.get("route", ""),
                    "seconds": fr.get("seconds", 0), "fixed": len(fr.get("fixed", [])),
                    "review": len(fr.get("review", [])), "images": len(fr.get("alt", [])),
                    "outputs": fr.get("outputs", []),
                    "errors": [str(e)[:300] for e in fr.get("errors", [])[:3]],
                }) + "\n")


def run_one(job_id: str, index: int, src: str, outdir: str, opts: Options, also_pdf: bool):
    rep = FileReport(os.path.basename(src))
    if opts.cancel_file and os.path.exists(opts.cancel_file):
        rep.status = "cancelled"
        rep.errors.append("Stopped before it started. Upload it again to remediate it.")
        with active_guard:
            active.discard((job_id, index))
        update_file(job_id, index, rep)
        return
    try:
        rep.status = "processing"
        update_file(job_id, index, rep)
        last = [0.0]

        def progress(seen, drafted):
            drafting = drafted > 0 or ((opts.ai_drafts or getattr(opts, "auto", False)) and opts.ollama_url)
            rep.progress = (f"Drafting alt text: {seen} image(s) so far, {drafted} AI draft(s) written"
                            if drafting else f"Collecting images: {seen} so far")
            if time.time() - last[0] > 2:  # don't rewrite the batch file more than every 2 seconds
                last[0] = time.time()
                update_file(job_id, index, rep)

        started = time.time()
        rep.started_at = datetime.now().isoformat(timespec="seconds")
        try:
            process_file(src, outdir, opts, rep, also_pdf=also_pdf, progress=progress)
        finally:
            rep.seconds = round(time.time() - started, 1)
        rep.progress = ""
    except Exception as e:  # never leave a file stuck on "Working"
        rep.status = "error"
        rep.errors.append(str(e))
    finally:
        with active_guard:
            active.discard((job_id, index))
        try:
            update_file(job_id, index, rep)
        except Exception:
            import traceback
            traceback.print_exc()


def recover_interrupted_jobs():
    """Batches still marked as running when the app starts were cut off by a restart."""
    root = os.path.join(DATA_DIR, "jobs")
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        try:
            job = load_job(name)
        except Exception:
            continue
        if job.get("drafting", {}).get("running"):
            job["drafting"]["running"] = False
            save_job(job)
        if job.get("finished"):
            continue
        changed = False
        for fr in job["files"]:
            if fr["status"] in ("queued", "processing"):
                fr["status"] = "cancelled"
                fr.setdefault("errors", []).append(
                    "Interrupted when the tool restarted. Upload it again to remediate it.")
                changed = True
        if changed or all(f["status"] in ("done", "error", "cancelled", "deleted") for f in job["files"]):
            job["finished"] = datetime.now().isoformat(timespec="seconds")
            save_job(job)
            try:
                write_reports(job)
            except Exception:
                pass


def cleanup_old_jobs():
    root = os.path.join(DATA_DIR, "jobs")
    cutoff = time.time() - RETENTION_DAYS * 86400
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------- reports

def write_reports(job: dict):
    out = os.path.join(job_dir(job["id"]), "out")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "remediation-report.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["File", "Type", "Status", "Route", "Time taken", "Kind", "Detail", "Where"])
        for fr in job["files"]:
            if fr["status"] == "deleted":
                continue
            base = [fr["filename"], fr["kind"], fr["status"], fr["route"], format_duration(fr.get("seconds", 0))]
            for item in fr["fixed"]:
                w.writerow(base + ["Fixed", item["msg"], item["where"]])
            for item in fr["review"]:
                w.writerow(base + ["Needs review", item["msg"], item["where"]])
            for e in fr["errors"]:
                w.writerow(base + ["Error", e, ""])
    with app.app_context():  # called from worker threads
        html = render_template("report.html", job=job, batch_seconds=batch_seconds(job))
    with open(os.path.join(out, "remediation-report.html"), "w") as f:
        f.write(html)


# ---------------------------------------------------------------- routes

@app.get("/")
@require_auth
def index():
    root = os.path.join(DATA_DIR, "jobs")
    recent = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root), key=lambda n: os.path.getmtime(os.path.join(root, n)),
                           reverse=True)[:10]:
            try:
                recent.append(load_job(name))
            except Exception:
                pass
    d = Options.from_env()
    return render_template("index.html", recent=recent, supported=sorted(SUPPORTED),
                           max_mb=MAX_UPLOAD_MB, defaults=d,
                           ai=ollama_status(d.ollama_url, d.ollama_model))


@app.post("/jobs")
@require_auth
def create_job():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return redirect(url_for("index", error="nofiles"))

    job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + new_id()[:6]
    base = job_dir(job_id)
    indir, outdir = os.path.join(base, "in"), os.path.join(base, "out")
    os.makedirs(indir)
    os.makedirs(outdir)

    opts = Options.from_env(
        alt_dir=os.path.join(base, "alt"),
        cancel_file=os.path.join(base, "CANCEL"),
        lang=request.form.get("lang") or None,
        ocr_lang=request.form.get("ocr_lang") or None,
        check_links=request.form.get("check_links") == "on",
        convert_lists=request.form.get("convert_lists") == "on",
        ai_drafts=request.form.get("ai_drafts") == "on",
        text_backings=request.form.get("text_backings") == "on",
        handwriting=request.form.get("handwriting") == "on",
        standard_colors=request.form.get("standard_colors") == "on",
        fix_contrast=request.form.get("fix_contrast") == "on",
        text_version=request.form.get("text_version") == "on",
        straighten_pages=request.form.get("straighten_pages") == "on",
        rebuild_untagged=request.form.get("rebuild_untagged") == "on",
    )
    also_pdf = request.form.get("also_pdf") == "on"
    if request.form.get("mode", "auto") != "custom":
        opts.auto = True     # each file gets what its type needs (decided per file while processing)
        also_pdf = False

    saved, skipped, used = [], [], set()
    for f in files:
        name = secure_filename(f.filename) or "file"
        stem, ext = os.path.splitext(name)
        if ext.lower() not in SUPPORTED:
            skipped.append(f.filename)
            continue
        n, candidate = 1, name
        while candidate.lower() in used:
            n += 1
            candidate = f"{stem}-{n}{ext}"
        used.add(candidate.lower())
        f.save(os.path.join(indir, candidate))
        saved.append(candidate)

    job = {
        "id": job_id,
        "label": (request.form.get("label") or "").strip()[:120],
        "created": datetime.now().isoformat(timespec="seconds"),
        "finished": None,
        "options": {**{k: v for k, v in opts.__dict__.items() if k not in ("alt_dir", "cancel_file")},
                    "also_pdf": also_pdf},
        "skipped": skipped,
        "files": [FileReport(n).to_dict() for n in saved],
    }
    if not saved:
        job["finished"] = job["created"]
    save_job(job)
    for i, name in enumerate(saved):
        with active_guard:
            active.add((job_id, i))
        executor.submit(run_one, job_id, i, os.path.join(indir, name), outdir, opts, also_pdf)
    return redirect(url_for("show_job", job_id=job_id))


@app.get("/history")
@require_auth
def history():
    rows = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    removed = set()
    if os.path.exists(HISTORY_PATH + ".removed"):
        with open(HISTORY_PATH + ".removed") as f:
            removed = {ln.strip() for ln in f if ln.strip()}
    archived_batches = {}
    for r in rows:
        b = r["batch"]
        if b not in archived_batches:
            meta = os.path.join(HISTORY_DIR, b, "batch.json")
            archived_batches[b] = None
            if os.path.isfile(meta):
                try:
                    with open(meta) as f:
                        archived_batches[b] = json.load(f)
                except ValueError:
                    pass
        arch = os.path.join(HISTORY_DIR, b)
        key = f"{b}:{r.get('index', 0)}"
        r["key"] = key
        r["batch_exists"] = os.path.isdir(os.path.join(DATA_DIR, "jobs", b))
        r["removed"] = key in removed
        # the latest outputs (e.g. a typed version built later) come from the archived batch record
        job = archived_batches[b]
        outs = r.get("outputs", [])
        if job and r.get("index", 0) < len(job["files"]):
            outs = job["files"][r["index"]].get("outputs", outs)
        r["files"] = [] if r["removed"] else [o for o in outs if os.path.isfile(os.path.join(arch, "out", o))]
        orig = r.get("original") or r.get("file")
        r["original_ok"] = (not r["removed"]) and os.path.isfile(os.path.join(arch, "in", orig))
        r["original"] = orig
        r["report_ok"] = os.path.isfile(os.path.join(arch, "remediation-report.html"))
    rows.sort(key=lambda r: (r.get("finished") or r.get("created") or ""), reverse=True)
    # summary: every file ever remediated (history), plus files still being worked on right now
    in_progress = 0
    jobs_root = os.path.join(DATA_DIR, "jobs")
    for name in (os.listdir(jobs_root) if os.path.isdir(jobs_root) else []):
        try:
            jb = load_job(name)
        except Exception:
            continue
        if not jb.get("finished"):
            in_progress += sum(1 for f in jb["files"] if f.get("status") in ("queued", "processing"))
    summary = {
        "total": len(rows) + in_progress,
        "remediated": sum(1 for r in rows if r.get("status") == "done" and not r.get("review")),
        "review": sum(1 for r in rows if r.get("status") == "done" and r.get("review")),
        "failed": sum(1 for r in rows if r.get("status") == "error"),
        "stopped": sum(1 for r in rows if r.get("status") == "cancelled"),
        "in_progress": in_progress,
    }
    return render_template("history.html", rows=rows, keep_days=HISTORY_DAYS, summary=summary)


@app.get("/history/<batch>/<kind>/<path:name>")
@require_auth
def history_file(batch, kind, name):
    if kind not in ("in", "out", "report"):
        abort(404)
    base = os.path.realpath(os.path.join(HISTORY_DIR, batch))
    path = os.path.realpath(os.path.join(base, name) if kind == "report" else os.path.join(base, kind, name))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        abort(404)
    as_attachment = not name.endswith(".html")
    return send_file(path, as_attachment=as_attachment, download_name=os.path.basename(path))


@app.post("/api/history/<batch>/<int:index>/delete")
@require_auth
def history_delete(batch, index):
    """Delete the archived copies of one file (the history line itself stays as a record)."""
    base = os.path.join(HISTORY_DIR, batch)
    meta = os.path.join(base, "batch.json")
    names = []
    if os.path.isfile(meta):
        with open(meta) as f:
            job = json.load(f)
        if 0 <= index < len(job["files"]):
            fr = job["files"][index]
            names = [os.path.join(base, "in", fr["filename"])] + \
                    [os.path.join(base, "out", o) for o in fr.get("outputs", [])]
    for path in names:
        if os.path.realpath(path).startswith(os.path.realpath(base) + os.sep) and os.path.isfile(path):
            os.remove(path)
    with history_guard:
        with open(HISTORY_PATH + ".removed", "a") as f:
            f.write(f"{batch}:{index}\n")
    return jsonify({"ok": True})


@app.get("/version")
def version():
    return jsonify({"version": __version__})


@app.get("/jobs/<job_id>")
@require_auth
def show_job(job_id):
    return render_template("job.html", job=load_job(job_id))


@app.get("/api/jobs/<job_id>")
@require_auth
def job_json(job_id):
    return jsonify(load_job(job_id))


@app.post("/api/jobs/<job_id>/cancel")
@require_auth
def cancel_job(job_id):
    """Stop remediating: files not started are skipped; files in progress stop at a safe point."""
    base = job_dir(job_id)
    with job_lock(job_id):
        job = load_job(job_id)
        if job.get("finished"):
            return jsonify({"ok": True, "already_finished": True})
        with open(os.path.join(base, "CANCEL"), "w") as f:
            f.write(datetime.now().isoformat(timespec="seconds"))
        job["cancel_requested"] = True
        with active_guard:
            running = {i for (jid, i) in active if jid == job_id}
        for i, fr in enumerate(job["files"]):
            # nothing is working on it (e.g. the tool was restarted): mark it stopped now
            if fr["status"] in ("queued", "processing") and i not in running:
                fr["status"] = "cancelled"
                fr.setdefault("errors", []).append("Stopped. Upload it again to remediate it.")
        if all(f["status"] in ("done", "error", "cancelled", "deleted") for f in job["files"]):
            job["finished"] = datetime.now().isoformat(timespec="seconds")
            save_job(job)
            write_reports(job)
        else:
            save_job(job)
    return jsonify({"ok": True})


FINAL_STATES = ("done", "error", "cancelled")


@app.post("/api/jobs/<job_id>/files/<int:index>/delete")
@require_auth
def delete_file(job_id, index):
    """Remove one file from a batch: its upload, its remediated outputs and its previews."""
    base = job_dir(job_id)
    with job_lock(job_id):
        job = load_job(job_id)
        if index < 0 or index >= len(job["files"]):
            abort(404)
        fr = job["files"][index]
        if fr["status"] not in FINAL_STATES:
            return jsonify({"ok": False, "error": "This file is still being processed."}), 409
        targets = [os.path.join(base, "in", fr["filename"])]
        targets += [os.path.join(base, "out", o) for o in fr.get("outputs", [])]
        targets += [os.path.join(base, "alt", a["preview"]) for a in fr.get("alt", []) if a.get("preview")]
        targets += [os.path.join(base, "alt", a["image"]) for a in fr.get("hw", []) if a.get("image")]
        for t in targets:
            t = os.path.abspath(t)
            if t.startswith(os.path.abspath(base) + os.sep) and os.path.isfile(t):
                os.remove(t)
        job["files"][index] = {"filename": fr["filename"], "kind": fr.get("kind", ""), "status": "deleted",
                               "route": "", "fixed": [], "review": [], "errors": [], "outputs": [], "alt": []}
        save_job(job)
        if job.get("finished"):
            write_reports(job)
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/delete")
@require_auth
def delete_job(job_id):
    """Remove a whole batch (uploads, outputs, previews, report) from this computer."""
    base = job_dir(job_id)
    with job_lock(job_id):
        job = load_job(job_id)
        if any(f["status"] not in FINAL_STATES + ("deleted",) for f in job["files"]):
            return jsonify({"ok": False, "error": "Stop the batch or let it finish first."}), 409
        shutil.rmtree(base, ignore_errors=True)
    with locks_guard:
        locks.pop(job_id, None)
    return jsonify({"ok": True, "redirect": url_for("index")})


@app.get("/jobs/<job_id>/files/<path:name>")
@require_auth
def download_file(job_id, name):
    return send_from_directory(os.path.join(job_dir(job_id), "out"), name, as_attachment=True)


@app.get("/jobs/<job_id>/report")
@require_auth
def view_report(job_id):
    job = load_job(job_id)
    return render_template("report.html", job=job, web_view=True, batch_seconds=batch_seconds(job))


@app.get("/jobs/<job_id>/download")
@require_auth
def download_all(job_id):
    job = load_job(job_id)
    out = os.path.join(job_dir(job_id), "out")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(out)):
            z.write(os.path.join(out, name), name)
    buf.seek(0)
    fname = f"remediated-{job['label'] or job_id}.zip".replace(" ", "-")
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{secure_filename(fname)}"'})


# ---------------------------------------------------------------- alt-text review

@app.get("/jobs/<job_id>/alt")
@require_auth
def alt_review(job_id):
    d = Options.from_env()
    return render_template("alt.html", job=load_job(job_id), ai=ollama_status(d.ollama_url, d.ollama_model))


@app.get("/jobs/<job_id>/alt-img/<path:name>")
@require_auth
def alt_image(job_id, name):
    return send_from_directory(os.path.join(job_dir(job_id), "alt"), name)


def _refresh_history(job_id: str):
    try:
        archive_history(load_job(job_id), force=True)
    except Exception:
        pass


@app.after_request
def _after_alt_save(resp):
    if request.method == "POST" and request.path.endswith("/alt") and request.path.startswith("/api/jobs/") \
            and resp.status_code == 200:
        _refresh_history(request.path.split("/")[3])
    return resp


@app.post("/api/jobs/<job_id>/alt")
@require_auth
def alt_save(job_id):
    payload = request.get_json(silent=True) or {}
    decisions = payload.get("decisions") or []
    if not isinstance(decisions, list) or not decisions:
        return jsonify({"ok": False, "error": "Nothing to save."}), 400
    outdir = os.path.join(job_dir(job_id), "out")
    with job_lock(job_id):
        job = load_job(job_id)
        notes = []
        ids = {d.get("id") for d in decisions}
        try:
            for fr in job["files"]:
                mine = [d for d in decisions if any(it["id"] == d.get("id") for it in fr.get("alt", []))]
                if mine:
                    notes += apply_decisions(outdir, fr["alt"], mine, job["options"].get("lang", "en-US"))
        except Exception as e:
            return jsonify({"ok": False, "error": f"Couldn't update the files: {e}"}), 500
        _alt_changed(job)
    items = [it for fr in job["files"] for it in fr.get("alt", []) if it["id"] in ids]
    return jsonify({"ok": True, "notes": notes,
                    "items": [{"id": it["id"], "status": it["status"], "final": it["final"],
                               "note": it.get("note", "")} for it in items]})


def _draft_later(job_id: str):
    """Write AI drafts for images that don't have one yet (after the batch has finished)."""
    d = Options.from_env()
    base = job_dir(job_id)
    with job_lock(job_id):
        job = load_job(job_id)
        todo = [(fi, it["id"]) for fi, fr in enumerate(job["files"]) for it in fr.get("alt", [])
                if it["status"] == "pending" and not it.get("draft") and not it.get("tiny")
                and it.get("ai_image")]
    # several images at once: Ollama works on parallel requests, so drafting finishes much sooner.
    # Only real drafts count; failed requests are retried one at a time with a longer wait, and after
    # the first failure the rest of the batch goes one image at a time (a busy or CPU-only computer).
    from concurrent.futures import ThreadPoolExecutor
    counter = {"ok": 0, "failed": [], "stopped": False, "serial": False, "retrying": False}
    count_lock = threading.Lock()
    serial_lock = threading.Lock()

    def publish(job):
        running = job.get("drafting", {}).get("running", False)  # Stop drafting clears this
        if not running:
            counter["stopped"] = True
        from remediate.alt_text import LAST_ERROR
        job["drafting"] = {"running": running, "done": counter["ok"], "total": len(todo),
                           "failed": len(counter["failed"]), "retrying": counter["retrying"],
                           "reason": LAST_ERROR.get("reason", "") if counter["failed"] else ""}

    def one(task, timeout=DRAFT_TIMEOUT):
        fi, item_id = task
        if counter["stopped"]:
            return
        with job_lock(job_id):
            job = load_job(job_id)
            if not job.get("drafting", {}).get("running"):
                counter["stopped"] = True
                return
            item = next((it for it in job["files"][fi]["alt"] if it["id"] == item_id), None)
        if item is None or item.get("draft") or item["status"] != "pending":
            return
        try:
            with open(os.path.join(base, "alt", item["ai_image"]), "rb") as f:
                png = f.read()
        except OSError:
            return
        if counter["serial"]:
            with serial_lock:
                draft = draft_alt_text(png, item.get("context", ""), d.ollama_url, d.ollama_model, timeout=timeout)
        else:
            draft = draft_alt_text(png, item.get("context", ""), d.ollama_url, d.ollama_model, timeout=timeout)
        with count_lock:
            if draft and draft.get("alt") is not None:
                counter["ok"] += 1
                if task in counter["failed"]:
                    counter["failed"].remove(task)
            else:
                if task not in counter["failed"]:
                    counter["failed"].append(task)
                counter["serial"] = True   # back off: one at a time from now on
        with job_lock(job_id):
            job = load_job(job_id)
            if draft:
                for it in job["files"][fi]["alt"]:
                    if it["id"] == item_id and it["status"] == "pending" and not it.get("draft"):
                        it["draft"] = draft.get("alt", "")
                        it["ai"] = True
                        it["decorative_suggested"] = it.get("decorative_suggested") or bool(draft.get("decorative"))
                        it["complex"] = bool(draft.get("complex"))
                        it["long"] = draft.get("long", "")
                        it["notes_ai"] = [n for n, on in (
                            ("Has words, numbers or handwriting that should also be real text", draft.get("text_in_image")),
                            ("Relies on colour alone: make sure the meaning is also in words", draft.get("color_only"))) if on]
            publish(job)
            save_job(job)

    with ThreadPoolExecutor(max_workers=DRAFT_PARALLEL) as pool:
        list(pool.map(one, todo))
    # second chance for the ones that failed: one at a time, with a much longer wait
    if counter["failed"] and not counter["stopped"]:
        counter["retrying"] = True
        for task in list(counter["failed"]):
            if counter["stopped"]:
                break
            one(task, timeout=DRAFT_TIMEOUT * 3)
    if counter["stopped"]:
        with job_lock(job_id):
            job = load_job(job_id)
            _alt_changed(job)
        return
    done = counter["ok"]
    # don't wait for approval: write the new drafts into the files now (still marked unreviewed,
    # so they can be changed on the review page at any time)
    with job_lock(job_id):
        job = load_job(job_id)
        lang = job.get("options", {}).get("lang", "en-US")
        outdir = os.path.join(base, "out")
        for fr in job["files"]:
            fresh = [it for it in fr.get("alt", []) if it["status"] == "pending" and it.get("draft")
                     and it.get("ai") and not it.get("decorative_suggested") and not it.get("written")]
            if not fresh:
                continue
            try:
                apply_decisions(outdir, fr["alt"], [{"id": it["id"], "status": "approved", "text": it["draft"]}
                                                    for it in fresh], lang)
            except Exception:
                continue
            for it in fresh:  # written into the file, but nobody has reviewed it yet
                it["status"] = "pending"
                it["final"] = ""
                it["written"] = True
        from remediate.alt_text import LAST_ERROR
        job["drafting"] = {"running": False, "done": done, "total": len(todo), "failed": len(counter["failed"]),
                           "reason": LAST_ERROR.get("reason", "") if counter["failed"] else ""}
        for fr in job["files"]:
            n = sum(1 for it in fr.get("alt", []) if it.get("written"))
            if n:
                fr["fixed"] = [x for x in fr.get("fixed", []) if not x.get("msg", "").startswith("Wrote AI alt-text drafts")]
                fr["fixed"].append({"msg": f"Wrote AI alt-text drafts for {n} image(s) into the file (marked "
                                           "unreviewed until you approve them)", "where": ""})
        _alt_changed(job)


@app.post("/api/jobs/<job_id>/alt/draft")
@require_auth
def alt_draft_start(job_id):
    d = Options.from_env()
    if not ollama_status(d.ollama_url, d.ollama_model).get("on"):
        return jsonify({"ok": False, "error": "AI drafts aren't available: Ollama isn't running."}), 409
    with job_lock(job_id):
        job = load_job(job_id)
        if not job.get("finished"):
            return jsonify({"ok": False, "error": "Wait for the batch to finish first."}), 409
        if job.get("drafting", {}).get("running"):
            return jsonify({"ok": True, "already": True})
        job["drafting"] = {"running": True, "done": 0, "total": 0}
        save_job(job)
    threading.Thread(target=_draft_later, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/alt/draft/stop")
@require_auth
def alt_draft_stop(job_id):
    with job_lock(job_id):
        job = load_job(job_id)
        if job.get("drafting", {}).get("running"):
            job["drafting"]["running"] = False
            save_job(job)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- handwriting review

@app.get("/jobs/<job_id>/handwriting")
@require_auth
def hw_review(job_id):
    job = load_job(job_id)
    files = [(i, f) for i, f in enumerate(job["files"]) if f.get("hw")]
    return render_template("handwriting.html", job=job, files=files)


@app.post("/api/jobs/<job_id>/handwriting/save")
@require_auth
def hw_save(job_id):
    d = request.get_json(silent=True) or {}
    with job_lock(job_id):
        job = load_job(job_id)
        for fr in job["files"]:
            for it in fr.get("hw", []):
                if it["id"] == d.get("id") and d.get("status") in ("pending", "approved", "skipped"):
                    it["status"] = d["status"]
                    it["final"] = d.get("text", "")
                    save_job(job)
                    return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Box not found."}), 404


def _typed_report_data(job: dict) -> list:
    """Read-only summary of each handwritten file's review and its typed text (nothing is changed)."""
    from remediate.handwriting import DIAGRAM_RE, speakable
    rows = []
    for fr in job["files"]:
        boxes = fr.get("hw", [])
        if not boxes:
            continue
        approved = [b for b in boxes if b.get("status") == "approved"]
        text_by_page = {}
        diagrams = []
        for b in approved:
            final = b.get("final") or ""
            diagrams += [d.strip() for d in DIAGRAM_RE.findall(final)]
            text_by_page.setdefault(b.get("page", 1), []).append(speakable(final))
        outs = fr.get("outputs", [])
        rows.append({
            "file": fr["filename"],
            "boxes": len(boxes),
            "approved": len(approved),
            "skipped": sum(1 for b in boxes if b.get("status") == "skipped"),
            "pending": sum(1 for b in boxes if b.get("status") == "pending"),
            "edited": sum(1 for b in approved if (b.get("final") or "").strip() != (b.get("draft") or "").strip()),
            "unclear": sum((b.get("final") or "").count("[?]") for b in approved),
            "diagrams": diagrams,
            "equations": sum(len(re.findall(r"\$\$.+?\$\$|\$[^$]+\$", b.get("final") or "", flags=re.S)) for b in approved),
            "typed_docx": next((o for o in outs if o.endswith("_typed.docx")), ""),
            "typed_pdf": next((o for o in outs if o.endswith("_typed.pdf")), ""),
            "pages": [(p, "\n\n".join(t for t in texts if t)) for p, texts in sorted(text_by_page.items())],
        })
    return rows


@app.get("/jobs/<job_id>/handwriting/report")
@require_auth
def hw_report(job_id):
    job = load_job(job_id)
    rows = _typed_report_data(job)
    html = render_template("hw_report.html", job=job, rows=rows, web=request.args.get("download") is None,
                           made=datetime.now().strftime("%Y-%m-%d %H:%M"))
    if request.args.get("download") is not None:
        return Response(html, mimetype="text/html", headers={
            "Content-Disposition": f'attachment; filename="typed-version-report-{job_id}.html"'})
    return html


@app.get("/jobs/<job_id>/handwriting/report.csv")
@require_auth
def hw_report_csv(job_id):
    job = load_job(job_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["File", "Boxes", "Approved", "Skipped", "Not reviewed", "Edited by a person", "Unclear [?] marks",
                "Diagrams", "Diagram descriptions", "Equations", "Typed Word file", "Typed PDF", "Typed text"])
    for r in _typed_report_data(job):
        w.writerow([r["file"], r["boxes"], r["approved"], r["skipped"], r["pending"], r["edited"], r["unclear"],
                    len(r["diagrams"]), " | ".join(r["diagrams"]), r["equations"], r["typed_docx"] or "not built",
                    r["typed_pdf"] or "not built",
                    "\n\n".join(f"Page {p}:\n{t}" for p, t in r["pages"])])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="typed-version-report-{job_id}.csv"'})


def _typed_report_data(job: dict) -> list:
    """Everything the typed-version report shows, read from the saved batch (nothing is changed)."""
    from remediate.handwriting import DIAGRAM_RE, _section_heading, _title_case, math_alts, speakable
    rows = []
    for i, fr in enumerate(job["files"]):
        boxes = fr.get("hw", [])
        if not boxes:
            continue
        approved = [b for b in boxes if b.get("status") == "approved"]
        pages = {}
        for b in approved:
            text = b.get("final") or ""
            pages.setdefault(b.get("page", 1), []).append(speakable(text))
        headings = []
        for b in approved:
            for ln in speakable(b.get("final") or "").splitlines():
                if _section_heading(ln) and _title_case(ln) not in headings:
                    headings.append(_title_case(ln))
        drawings = [d.strip() for b in approved for d in DIAGRAM_RE.findall(b.get("final") or "")]
        typed = [o for o in fr.get("outputs", []) if "_typed." in o]
        rows.append({
            "index": i, "file": fr["filename"],
            "boxes": len(boxes), "approved": len(approved),
            "skipped": sum(1 for b in boxes if b.get("status") == "skipped"),
            "pending": sum(1 for b in boxes if b.get("status") == "pending"),
            "typed": typed, "built": any(o.endswith(".pdf") for o in typed),
            "headings": headings, "drawings": drawings, "equations": math_alts(boxes),
            "pages": [{"page": p, "text": "\n".join(t for t in pages[p] if t)} for p in sorted(pages)],
        })
    return rows


@app.get("/jobs/<job_id>/typed-report")
@require_auth
def typed_report(job_id):
    job = load_job(job_id)
    return render_template("typed_report.html", job=job, rows=_typed_report_data(job),
                           made=datetime.now().strftime("%Y-%m-%d %H:%M"))


@app.get("/jobs/<job_id>/typed-report.csv")
@require_auth
def typed_report_csv(job_id):
    job = load_job(job_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["File", "Boxes", "Approved", "Skipped", "Not reviewed", "Typed version built", "Typed files",
                "Headings", "Drawings (alt text)", "Equations (spoken)", "Typed text"])
    for r in _typed_report_data(job):
        w.writerow([r["file"], r["boxes"], r["approved"], r["skipped"], r["pending"], "yes" if r["built"] else "no",
                    "; ".join(r["typed"]), " | ".join(r["headings"]), " | ".join(r["drawings"]),
                    " | ".join(r["equations"]),
                    "\n\n".join(f"Page {p['page']}:\n{p['text']}" for p in r["pages"])])
    name = f"typed-version-report-{job_id}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.post("/api/jobs/<job_id>/handwriting/save_many")
@require_auth
def hw_save_many(job_id):
    """Approve all: many boxes in one request (one read and one write of the batch record)."""
    items = (request.get_json(silent=True) or {}).get("items", [])
    wanted = {it.get("id"): it for it in items if it.get("status") in ("pending", "approved", "skipped")}
    saved = 0
    with job_lock(job_id):
        job = load_job(job_id)
        for fr in job["files"]:
            for box in fr.get("hw", []):
                it = wanted.get(box["id"])
                if it is not None:
                    box["status"] = it["status"]
                    box["final"] = it.get("text", "")
                    saved += 1
        save_job(job)
    return jsonify({"ok": True, "saved": saved})


_preview_cache: dict = {}


@app.post("/api/jobs/<job_id>/handwriting/preview")
@require_auth
def hw_preview(job_id):
    from remediate.handwriting import render_preview
    text = (request.get_json(silent=True) or {}).get("text", "")[:6000]
    png = _preview_cache.get(text)
    if png is None:   # the same text draws the same picture: draw it once
        png = render_preview(text)
        if len(_preview_cache) > 600:
            _preview_cache.clear()
        _preview_cache[text] = png
    return Response(png, mimetype="image/png")


def _hw_reread(job_id: str, index: int, page_no: int):
    """Turn one page 180 degrees and transcribe its boxes again (background)."""
    import pymupdf
    from remediate.handwriting import crop_diagrams, find_regions, transcribe
    d = Options.from_env()
    base = job_dir(job_id)
    try:
        with job_lock(job_id):
            job = load_job(job_id)
            fr = job["files"][index]
            old = [it for it in fr["hw"] if it["page"] == page_no]
            turn = ((old[0].get("turn", 0) if old else 0) + 180) % 360
        src = os.path.join(base, "in", fr["filename"])
        with pymupdf.open(src) as doc:
            regions = find_regions(doc[page_no - 1], turn=turn)
        key = os.path.splitext(fr["filename"])[0]
        new = []
        for k, r in enumerate(regions, 1):
            name = f"{key}-hw-p{page_no}-t{turn}-{k}.png"
            with open(os.path.join(base, "alt", name), "wb") as f:
                f.write(r["png"])
            draft = transcribe(r["png"], d.ollama_url, d.ollama_model)
            diagrams = []
            extra = []
            if draft:
                from remediate.handwriting import add_missing_drawings
                try:
                    draft, extra = add_missing_drawings(r["png"], draft, d.ollama_url, d.ollama_model)
                except Exception:
                    extra = []
            for j, dg in enumerate(crop_diagrams(r["png"], draft or "", d.ollama_url, d.ollama_model) if not extra else extra, 1):
                dname = f"{key}-hw-p{page_no}-t{turn}-{k}-d{j}.png"
                with open(os.path.join(base, "alt", dname), "wb") as f:
                    f.write(dg["png"])
                diagrams.append({"image": dname, "whole_box": dg["whole_box"]})
            new.append({"id": f"{key}-hw-p{page_no}-t{turn}-{k}", "page": page_no, "n": k, "image": name,
                        "draft": draft or "", "final": "", "status": "pending", "failed": draft is None,
                        "turn": turn, "box": r["box"], "diagrams": diagrams})
        with job_lock(job_id):
            job = load_job(job_id)
            fr = job["files"][index]
            keep = [it for it in fr["hw"] if it["page"] != page_no]
            fr["hw"] = sorted(keep + new, key=lambda it: (it["page"], it["n"]))
            job.setdefault("hw_busy", {}).pop(f"{index}:{page_no}", None)
            save_job(job)
    except Exception:
        with job_lock(job_id):
            job = load_job(job_id)
            job.setdefault("hw_busy", {}).pop(f"{index}:{page_no}", None)
            save_job(job)


@app.post("/api/jobs/<job_id>/files/<int:index>/handwriting/turn")
@require_auth
def hw_turn(job_id, index):
    page_no = int((request.get_json(silent=True) or {}).get("page", 0))
    with job_lock(job_id):
        job = load_job(job_id)
        busy = job.setdefault("hw_busy", {})
        busy[f"{index}:{page_no}"] = True
        save_job(job)
    threading.Thread(target=_hw_reread, args=(job_id, index, page_no), daemon=True).start()
    return jsonify({"ok": True})


def _hw_start(job_id: str, index: int):
    """Transcribe handwriting for one file after the batch has finished (background)."""
    from remediate.pipeline import transcribe_handwriting
    d = Options.from_env()
    base = job_dir(job_id)
    with job_lock(job_id):
        job = load_job(job_id)
        fr = job["files"][index]
    rep = FileReport(fr["filename"])
    opts = Options.from_env(alt_dir=os.path.join(base, "alt"), cancel_file="")
    key = os.path.splitext(fr["filename"])[0]
    try:
        transcribe_handwriting(os.path.join(base, "in", fr["filename"]), rep, opts, key, None)
    except Exception as e:
        rep.errors.append(f"Handwriting transcription stopped: {e}")
    with job_lock(job_id):
        job = load_job(job_id)
        fr = job["files"][index]
        fr["hw"] = rep.hw
        fr["review"] += [r for r in rep.review if r not in fr["review"]]
        fr["errors"] += rep.errors
        job.setdefault("hw_busy", {}).pop(f"{index}:all", None)
        save_job(job)


@app.post("/api/jobs/<job_id>/files/<int:index>/handwriting/start")
@require_auth
def hw_start(job_id, index):
    d = Options.from_env()
    if not ollama_status(d.ollama_url, d.ollama_model).get("on"):
        return jsonify({"ok": False, "error": "AI isn't available: Ollama isn't running."}), 409
    with job_lock(job_id):
        job = load_job(job_id)
        if not job.get("finished"):
            return jsonify({"ok": False, "error": "Wait for the batch to finish first."}), 409
        fr = job["files"][index]
        if not fr["filename"].lower().endswith(".pdf"):
            return jsonify({"ok": False, "error": "Handwriting transcription works on PDFs."}), 409
        job.setdefault("hw_busy", {})[f"{index}:all"] = True
        save_job(job)
    threading.Thread(target=_hw_start, args=(job_id, index), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/files/<int:index>/handwriting/build")
@require_auth
def hw_build(job_id, index):
    from remediate.common import docx_to_tagged_pdf
    from remediate.handwriting import build_document
    base = job_dir(job_id)
    with job_lock(job_id):
        job = load_job(job_id)
        fr = job["files"][index]
        boxes = fr.get("hw", [])
        if any(b["status"] == "pending" for b in boxes):
            return jsonify({"ok": False, "error": "Approve or skip every box first."}), 409
        if not any(b["status"] == "approved" for b in boxes):
            return jsonify({"ok": False, "error": "No approved boxes to build from."}), 409
        stem = os.path.splitext(fr["filename"])[0]
        lang = job.get("options", {}).get("lang", "en-US")
    docx_name, pdf_name = f"{stem}_typed.docx", f"{stem}_typed.pdf"
    out_docx = os.path.join(base, "out", docx_name)
    # boxes with a drawing the AI never mentioned: crop it, describe it and add its [Diagram: ...] line,
    # so the typed version shows the picture (saved, so the review page shows it too)
    from remediate.handwriting import DIAGRAM_RE, add_missing_drawings
    dopts = Options.from_env()
    added_any = False
    for b in boxes:
        if b.get("status") != "approved" or b.get("diagrams") or DIAGRAM_RE.search(b.get("final") or ""):
            continue
        img = os.path.join(base, "alt", b.get("image", ""))
        if not os.path.isfile(img):
            continue
        try:
            with open(img, "rb") as fh:
                new_text, extra = add_missing_drawings(fh.read(), b.get("final") or "", dopts.ollama_url, dopts.ollama_model)
        except Exception:
            extra = []
        if extra:
            b["final"] = new_text
            b["diagrams"] = []
            for k, dg in enumerate(extra, 1):
                name = f"{b['id']}-draw{k}.png"
                with open(os.path.join(base, "alt", name), "wb") as fh:
                    fh.write(dg["png"])
                b["diagrams"].append({"image": name, "whole_box": False})
            added_any = True
    if added_any:
        with job_lock(job_id):
            job = load_job(job_id)
            by_id = {b["id"]: b for b in boxes}
            for box in job["files"][index].get("hw", []):
                if box["id"] in by_id and by_id[box["id"]].get("diagrams") and not box.get("diagrams"):
                    box["final"] = by_id[box["id"]]["final"]
                    box["diagrams"] = by_id[box["id"]]["diagrams"]
            save_job(job)
    for b in boxes:  # diagram crops go into the Word file as pictures with their descriptions as alt text
        b["diagram_paths"] = [os.path.join(base, "alt", d["image"]) for d in b.get("diagrams", [])
                              if os.path.exists(os.path.join(base, "alt", d["image"]))]
    try:
        build_document(boxes, f"{stem.replace('_', ' ')} (typed version)", out_docx, lang)
        docx_to_tagged_pdf(out_docx, os.path.join(base, "out", pdf_name))
        from remediate.handwriting import describe_pdf_equations, math_alts
        describe_pdf_equations(os.path.join(base, "out", pdf_name), math_alts(boxes))
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't build the typed version: {e}"}), 500
    # a handwritten scan: rebuild its accessible PDF with the reviewed text in place of the OCR guesses
    rebuilt = None
    try:
        from remediate.handwriting import rebuild_scan_from_transcript
        import pymupdf
        src = os.path.join(base, "in", fr["filename"])
        with pymupdf.open(src) as d:
            n_pages = len(d)
        covered = {b["page"] for b in boxes}
        if fr.get("route", "").startswith("Scanned") and covered >= set(range(1, n_pages + 1)):
            rebuilt = f"{stem}_accessible.pdf"
            rebuild_scan_from_transcript(src, os.path.join(base, "out", rebuilt), boxes, lang,
                                         stem.replace("_", " "), os.path.join(base, "alt", f"{stem}-rebuild"))
            from remediate.common import FileReport as _FR
            from remediate.logic_check import pdf_logic
            pdf_logic(os.path.join(base, "out", rebuilt), _FR(rebuilt))
    except Exception as e:
        rebuilt = None
        app.logger.warning("scan rebuild from transcript failed: %s", e)
    with job_lock(job_id):
        job = load_job(job_id)
        fr = job["files"][index]
        for name in (docx_name, pdf_name):
            if name not in fr["outputs"] and os.path.exists(os.path.join(base, "out", name)):
                fr["outputs"].append(name)
        msg = "Built a typed version from the reviewed handwriting (Word with real equations, and a tagged PDF)"
        if not any(x.get("msg") == msg for x in fr["fixed"]):
            fr["fixed"].append({"msg": msg, "where": ""})
        if rebuilt:
            msg2 = ("Rebuilt the accessible PDF's text from the reviewed transcription (same look; screen "
                    "readers now read the checked text, with diagrams described)")
            if not any(x.get("msg") == msg2 for x in fr["fixed"]):
                fr["fixed"].append({"msg": msg2, "where": ""})
            if rebuilt not in fr["outputs"]:
                fr["outputs"].append(rebuilt)
        save_job(job)
        if job.get("finished"):
            write_reports(job)
            try:
                archive_history(job, force=True)
            except Exception:
                pass
    return jsonify({"ok": True, "files": [docx_name, pdf_name] + ([rebuilt] if rebuilt else [])})


@app.errorhandler(413)
def too_large(_):
    return render_template("index.html", recent=[], supported=sorted(SUPPORTED), max_mb=MAX_UPLOAD_MB,
                           defaults=Options.from_env(), ai={"on": False, "reason": ""},
                           error=f"That upload is over {MAX_UPLOAD_MB} MB. Split it into smaller batches."), 413


if __name__ == "__main__":
    os.makedirs(os.path.join(DATA_DIR, "jobs"), exist_ok=True)
    cleanup_old_jobs()
    recover_interrupted_jobs()
    cleanup_history()
    for _name in os.listdir(os.path.join(DATA_DIR, "jobs")):
        try:
            log_history(load_job(_name))
        except Exception:
            pass
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    print(f"Remediation Workbench v{__version__} on http://{host}:{port}  (data in {DATA_DIR})")
    try:
        from waitress import serve  # production server if installed
        serve(app, host=host, port=port, threads=16)
    except ImportError:
        app.run(host=host, port=port, threaded=True)

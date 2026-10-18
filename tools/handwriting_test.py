"""Handwriting test: how well does the local AI model read handwritten pages?

Run inside the tool's container (nothing leaves this computer):
    docker compose exec workbench python tools/handwriting_test.py /data/YOUR_FILE.pdf

Each page is cut into a top and a bottom half (so the writing is large enough to read),
sent to Ollama, and the transcription is saved as Markdown with the math in LaTeX:
    data/handwriting_test/YOUR_FILE.md      (C:\\tools\\remediator\\data\\handwriting_test on Windows)
This is only a test. It does not change any files or batches.
"""
import base64
import io
import os
import sys
import time

import pymupdf
import requests
from PIL import Image

URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b")

PROMPT = (
    "This is part of a page from a university math course. It may mix printed text and handwriting. "
    "Transcribe everything you can read, in reading order, as Markdown. "
    "Write every mathematical expression in LaTeX between $ signs (for example $2y^2(3y-2)(y-4)$). "
    "Keep problem numbers. Write each step of worked math on its own line. "
    "Ignore grading marks such as check marks. "
    "If you cannot read a symbol, write [?] instead of guessing. Output only the transcription."
)


def ask(png: bytes) -> str:
    r = requests.post(f"{URL}/api/generate", timeout=600, json={
        "model": MODEL, "prompt": PROMPT, "stream": False,
        "images": [base64.b64encode(png).decode()],
        "options": {"temperature": 0.1, "num_predict": 1500},
    })
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def halves(page):
    pix = page.get_pixmap(dpi=200)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    w, h = im.size
    overlap = int(h * 0.06)
    for box in ((0, 0, w, h // 2 + overlap), (0, h // 2 - overlap, w, h)):
        part = im.crop(box)
        part.thumbnail((1600, 1600))
        buf = io.BytesIO()
        part.save(buf, "PNG")
        yield buf.getvalue()


def main(path: str):
    name = os.path.splitext(os.path.basename(path))[0]
    out_dir = os.path.join(os.environ.get("DATA_DIR", "/data"), "handwriting_test")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, name + ".md")
    lines = [f"# Handwriting test: {os.path.basename(path)}", f"Model: {MODEL}", ""]
    start = time.time()
    with pymupdf.open(path) as doc:
        for n, page in enumerate(doc, 1):
            for k, png in enumerate(halves(page)):
                part = "top" if k == 0 else "bottom"
                print(f"Reading page {n} ({part} half)...", flush=True)
                t = time.time()
                try:
                    text = ask(png)
                except Exception as e:  # keep going: a test should report, not stop
                    text = f"(could not read this part: {e})"
                lines += [f"## Page {n}, {part} half  ({time.time() - t:.0f} s)", "", text, ""]
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
    print(f"Done in {time.time() - start:.0f} s. Saved: {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python tools/handwriting_test.py /data/YOUR_FILE.pdf")
    main(sys.argv[1])

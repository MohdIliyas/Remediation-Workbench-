"""Alt-text drafting with a vision model running locally in Ollama.

Nothing leaves the machine: OLLAMA_URL points at Ollama on this computer (or an
approved campus server). Every draft is a suggestion that a person reviews on
the alt-text review page before it counts.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading

import requests

PROMPT = """You write alt text for images in university course materials, for students who use screen readers.

Text from the document near the image (may be partial or unrelated):
\"\"\"{context}\"\"\"

Return JSON with exactly these keys:
- "alt": alt text of at most two short lines (under 200 characters). Describe the purpose and the information the image conveys, not only its appearance. Be concise but complete. If it contains important words or numbers, include them. For a chart or graph, give the type and the main trend or key values. For a logo, give the organization's name followed by "logo". Do not start with "Image of", "Picture of" or "Graphic of". Do not repeat a caption that is already in the nearby text word for word.
- "decorative": true only if the image adds no information (borders, dividers, background art, stock decoration).
- "complex": true if a short alt text cannot carry the information (detailed charts, diagrams, maps, infographics, equations, tables shown as pictures), meaning it also needs a longer description nearby.
- "long": only when "complex" is true: a longer description of at most 50 words (about three lines) with the data or relationships the short alt text leaves out; otherwise "".
- "text_in_image": true if the image contains words, numbers, handwriting or a signature that should exist as real text.
- "color_only": true if the image relies on colour alone to convey meaning (for example a chart whose lines differ only by colour).
"""

_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def image_hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def to_png(data: bytes, max_side: int = 1024) -> bytes | None:
    """Normalise any image Pillow can read to a modest PNG (faster for the model)."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGBA" if "A" in im.mode else "RGB")
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()
    except Exception:
        return None


def image_size(data: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
        return Image.open(io.BytesIO(data)).size
    except Exception:
        return (0, 0)


def ollama_status(url: str, model: str, timeout: float = 3.0) -> dict:
    """Is the configured model reachable? Used by the upload page."""
    if not (url and model):
        return {"on": False, "reason": "not set up"}
    try:
        r = requests.get(url.rstrip("/") + "/api/tags", timeout=timeout)
        r.raise_for_status()
        names = {m.get("name", "") for m in r.json().get("models", [])}
        wanted = model if ":" in model else model + ":latest"
        if wanted in names or model in names:
            return {"on": True, "model": model}
        return {"on": False, "reason": f"model {model} isn't downloaded (run: ollama pull {model})"}
    except (requests.RequestException, ValueError):
        return {"on": False, "reason": "Ollama isn't running or can't be reached"}


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip().strip('"')
    text = re.sub(r"^(an? )?(image|picture|photo|graphic|illustration) (of|showing) ", "", text, flags=re.I)
    return text[:1].upper() + text[1:] if text else text


LAST_ERROR = {"reason": ""}  # why the most recent draft failed, shown to the user


def draft_alt_text(png: bytes, context: str, url: str, model: str, timeout: int = 180) -> dict | None:
    """Return {"alt", "decorative", "complex"} or None if no model is available."""
    if not (url and model and png):
        return None
    key = image_hash(png) + "|" + (context or "")[-200:]
    with _cache_lock:
        if key in _cache:
            return dict(_cache[key])
    def ask(image):
        r = requests.post(
            url.rstrip("/") + "/api/generate",
            json={
                "model": model,
                "prompt": PROMPT.format(context=(context or "(none)")[:1500]),
                "images": [base64.b64encode(image).decode()],
                "format": "json",
                "stream": False,
                # a short JSON answer is all we need; capping it keeps each draft quick (as in 9.29)
                "options": {"temperature": 0.2, "num_predict": 200},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        body = r.json()
        raw = (body.get("response") or "").strip()
        if not raw or body.get("done") is False:
            # Ollama couldn't process it (often an image too big for the graphics memory)
            LAST_ERROR["reason"] = "the AI returned an empty answer"
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            # a reply cut off mid-way: keep the alt text if it is there
            m = re.search(r'"alt"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
            if not m:
                raise
            data = {"alt": m.group(1).encode().decode("unicode_escape", errors="ignore"),
                    "decorative": '"decorative": true' in raw, "complex": '"complex": true' in raw}
        res = {
            "alt": _clean(str(data.get("alt", "")))[:400],
            "decorative": bool(data.get("decorative", False)),
            "complex": bool(data.get("complex", False)),
            "long": _clean(str(data.get("long", "") or ""))[:400] if data.get("complex") else "",
            "text_in_image": bool(data.get("text_in_image", False)),
            "color_only": bool(data.get("color_only", False)),
        }
        if not res["alt"] and not res["decorative"]:
            LAST_ERROR["reason"] = "the AI returned an empty answer"
            return None   # never save a blank draft
        return res

    try:
        result = ask(png)
        if result is None:
            smaller = to_png(png, max_side=512)   # second try with a smaller copy
            if smaller:
                result = ask(smaller)
    except requests.Timeout:
        LAST_ERROR["reason"] = "the AI didn't answer in time"
        return None
    except requests.RequestException as e:
        LAST_ERROR["reason"] = f"the AI returned an error ({str(e)[:120]})"
        return None
    except ValueError:
        LAST_ERROR["reason"] = "the AI's reply couldn't be read"
        return None
    if result is None:
        return None
    with _cache_lock:
        _cache[key] = result
    return dict(result)


AI_SIDE = 768  # the model gets a smaller copy than people see: faster, still plenty for alt text


class AltCollector:
    """Saves image previews, gets drafts, and builds the review list for one file."""

    def __init__(self, alt_dir: str | None, file_key: str, url: str, model: str):
        self.alt_dir, self.file_key = alt_dir, file_key
        self.url, self.model = url, model
        self.items: list[dict] = []
        self.stop_check = None  # set by the pipeline; checked before each AI draft
        self.on_progress = None  # set by the app: called with (images seen, drafts written)
        self._seen_pictures: dict[str, dict] = {}  # identical picture -> its draft
        self._drafted = 0
        if alt_dir:
            os.makedirs(alt_dir, exist_ok=True)

    @property
    def ai_on(self) -> bool:
        return bool(self.url and self.model)

    def add(self, image_bytes: bytes | None, target: dict, context: str, where: str,
            existing: str = "", decorative_hint: bool = False,
            preview_bytes: bytes | None = None) -> dict:
        if self.stop_check is not None and self.stop_check():
            from .common import Cancelled
            raise Cancelled()
        n = len(self.items) + 1
        png = to_png(image_bytes, max_side=AI_SIDE) if image_bytes else None
        shown = to_png(preview_bytes, max_side=1400) if preview_bytes else to_png(image_bytes) if image_bytes else None
        preview = ai_copy = None
        if shown and self.alt_dir:
            preview = f"{self.file_key}-{n}.png"
            with open(os.path.join(self.alt_dir, preview), "wb") as f:
                f.write(shown)
        if png and self.alt_dir:
            ai_copy = f"{self.file_key}-{n}.ai.png"  # kept so drafts can be written later
            with open(os.path.join(self.alt_dir, ai_copy), "wb") as f:
                f.write(png)
        w, h = image_size(image_bytes) if image_bytes else (0, 0)
        tiny = (0 < w < 24) or (0 < h < 24) or decorative_hint
        draft = None
        if png and not tiny and self.ai_on:
            same = image_hash(png)
            if same in self._seen_pictures:
                draft = dict(self._seen_pictures[same])  # identical picture: reuse its draft
            else:
                draft = draft_alt_text(png, context, self.url, self.model)
                if draft is not None:
                    self._seen_pictures[same] = draft
                    self._drafted += 1
        item = {
            "id": f"{self.file_key}-{n}",
            "where": where,
            "preview": preview,
            "context": re.sub(r"\s+", " ", context or "")[:600],
            "existing": existing,
            "draft": (draft or {}).get("alt", ""),
            "decorative_suggested": bool(draft and draft.get("decorative")) or tiny,
            "complex": bool(draft and draft.get("complex")),
            "long": (draft or {}).get("long", ""),
            "notes_ai": [n for n, on in (("Has words, numbers or handwriting that should also be real text",
                                          (draft or {}).get("text_in_image")),
                                         ("Relies on colour alone: make sure the meaning is also in words",
                                          (draft or {}).get("color_only"))) if on],
            "ai": draft is not None,
            "status": "pending",      # pending | approved | decorative
            "final": "",
            "target": target,
            "ai_image": ai_copy,
            "tiny": tiny,
        }
        self.items.append(item)
        if self.on_progress is not None:
            try:
                self.on_progress(len(self.items), self._drafted)
            except Exception:
                pass
        return item

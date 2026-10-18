"""Find headings in plain paragraphs (OCR text, converted PDFs) without relying on font size.

1. Table of contents: if the document lists its own sections ("I. Before The
   Struggle ..... 371"), those entries are the headings, and their numbering
   gives the level. Matching entries in the body become headings, including
   run-in headings at the start of a paragraph ("1. The Academic Reformers. The...").
2. Fallback: short standalone outline-numbered lines ("II. THE ISSUES") and
   short ALL-CAPS lines ("INTRODUCTION") become headings.
3. Page furniture (running heads, footers, page numbers) is removed.

Input/output: a list of paragraph dicts {"text", "lines", "page"}; output items
are {"kind": "heading"|"para", "text", "level"}.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter

LEADER = re.compile(r"(?:\.\s?){3,}|…|_{3,}")
TOC_LINE = re.compile(r"^(?P<body>.+?)\s*(?:(?:(?:\.\s?){2,}|…|\s{3,})[\s.]*(?P<page>\d{1,4})|(?:\.\s?){4,})\s*$")
ENDS_WITH_PAGE_REF = re.compile(r"(?:\.\s?){2,}\s*\d{1,4}\s*$")
NUM = re.compile(r"^\s*(?P<num>[IVXLC]+\.|[A-Z]\.|\d{1,2}\.|[a-z]\.|\(\d{1,2}\)|\([a-z]\))\s+")
LEVEL_OF = [
    (re.compile(r"^[IVXLC]+\.$"), 2),
    (re.compile(r"^[A-Z]\.$"), 3),
    (re.compile(r"^\d{1,2}\.$"), 4),
    (re.compile(r"^[a-z]\.$"), 5),
    (re.compile(r"^\(\d{1,2}\)$"), 6),
    (re.compile(r"^\([a-z]\)$"), 6),
]


LEADER_JUNK = re.compile(r"(?:\.\s?){3,}[.,:;cseoanr0-9]*|[.,:;]*(?:[cseo]{6,})[.,:;]*|…+")


def clean_leaders(text: str) -> str:
    """Remove dot leaders (and OCR's garbled versions of them, like 'ccssesse') from text."""
    cleaned = LEADER_JUNK.sub(" ", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


ROMAN_SEQ = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]


def level_for(num: str | None, last_roman: str | None = None) -> int:
    if not num:
        return 2
    core = num.rstrip(".")
    if re.fullmatch(r"[IVXLC]+", core):
        if len(core) > 1 or core == "I":
            return 2
        # a single V / X counts as roman only right after IV / IX; C, D, L are letters
        if last_roman and core in ROMAN_SEQ and ROMAN_SEQ.index(core) == ROMAN_SEQ.index(last_roman) + 1:
            return 2
        return 3
    for rx, lvl in LEVEL_OF:
        if rx.match(num):
            return lvl
    return 3


# ---------------------------------------------------------------- page furniture

def _fkey(t: str) -> str:
    """OCR-tolerant key: letters only, digits ignored ("2001] HISTORY ... 361" == "2001) HISTORY ... 363")."""
    return re.sub(r"[^a-z]+", "", t.lower())


PAGE_ONLY = re.compile(r"^[\[\]()\d\s.\-–]+$|^(page\s*)?\d+(\s*(of|/)\s*\d+)?$", re.I)


def strip_furniture(paras: list[dict], pages: int) -> list[dict]:
    """Drop running heads/feet and page numbers, line by line."""
    # a running head/foot repeats across pages, so count pages, not occurrences
    per_page: dict = {}
    for p in paras:
        keys = per_page.setdefault(p.get("page", 0), set())
        for line in p.get("lines") or [p["text"]]:
            k = _fkey(line)
            if len(line) < 100 and len(k) >= 6:
                keys.add(k)
    counts = Counter(k for keys in per_page.values() for k in keys)
    threshold = max(3, pages // 3)
    furniture = {k for k, c in counts.items() if c >= threshold} if pages >= 3 else set()
    out = []
    for p in paras:
        lines = p.get("lines") or [p["text"]]
        lwords = p.get("line_words") or [[] for _ in lines]
        kept, kept_words = [], []
        for line, lw in zip(lines, lwords):
            k = _fkey(line)
            if PAGE_ONLY.match(line.strip()) or k in furniture:
                continue
            # furniture glued to the start of a line ("...1954 371 MR. IRETON: Yes.")
            for f in furniture:
                if len(f) >= 12 and k.startswith(f) and len(k) > len(f):
                    letters = 0
                    for i, ch in enumerate(line):
                        if ch.isalpha():
                            letters += 1
                            if letters == len(f):
                                trimmed = re.sub(r"^[\s\d\]\)\[(.,-]+", "", line[i + 1:])
                                drop = len(line.split()) - len(trimmed.split())
                                line, lw = trimmed, lw[max(drop, 0):]
                                break
                    break
            kept.append(line)
            kept_words.append(lw)
        if not kept:
            continue
        q = dict(p)
        q["lines"] = kept
        q["line_words"] = kept_words
        q["text"] = " ".join(kept).strip()
        out.append(q)
    return out


# ---------------------------------------------------------------- table of contents

def parse_toc_lines(lines: list[str]) -> list[dict]:
    """TOC entries from plain text lines (e.g. a PDF's own text layer)."""
    fake = [{"text": l, "lines": [l]} for l in lines[:400]]
    entries, _ = parse_toc(fake, limit=400)
    return entries


def parse_toc(paras: list[dict], limit: int = 80) -> tuple[list[dict], set[int]]:
    """Return TOC entries [{num, title, level, norm}] and the indexes of TOC paragraphs."""
    entries, toc_paras = [], set()
    pending = ""
    last_roman = None
    for i, p in enumerate(paras[:limit]):  # a TOC sits near the start
        lines = p.get("lines") or [p["text"]]
        hits = 0
        for line in lines:
            line = line.strip()
            m = TOC_LINE.match(line)
            if not m:
                # entry wrapped onto the next line ("b. Trade and Judicial Regulation of")
                pending = line if (NUM.match(line) and len(line) < 90) else ""
                continue
            body = (pending + " " + m.group("body")).strip() if pending else m.group("body")
            pending = ""
            body = LEADER.sub(" ", body).strip(" .")
            nm = NUM.match(body)
            num = nm.group("num") if nm else None
            title = body[nm.end():].strip() if nm else body
            if len(title) < 3 or len(title) > 160:
                continue
            lvl = level_for(num, last_roman)
            if lvl == 2 and num:
                last_roman = num.rstrip(".")
            entries.append({"num": num, "title": title, "level": lvl,
                            "norm": norm(((num or "") + " " + title))})
            hits += 1
        if hits >= 2 or (hits and len(lines) <= 2 and LEADER.search(p["text"])):
            toc_paras.add(i)
    if len(entries) < 4:
        return [], set()
    # no number at the top level ("Introduction", "Conclusion") sits with the roman numerals
    return entries, toc_paras


def _similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.88


def _split_at_norm_prefix(text: str, n_norm_chars: int) -> int:
    """Index in text where the first n normalised characters end."""
    count = 0
    for i, ch in enumerate(text):
        if ch.isalnum():
            count += 1
            if count >= n_norm_chars:
                return i + 1
    return len(text)


GARBLED_LEADER = re.compile(r"(?:\.\s?){3,}|[cseo.,:;]{8,}")


def looks_like_toc(text: str, entries: list[dict]) -> bool:
    nt = norm(text)
    hits = sum(1 for e in entries if len(e["norm"]) >= 8 and norm(e["title"]) in nt)
    return hits >= 2 or (hits >= 1 and bool(GARBLED_LEADER.search(text)))


def _is_toc_line(line: str) -> bool:
    return bool(TOC_LINE.match(line.strip()) or ENDS_WITH_PAGE_REF.search(line) or GARBLED_LEADER.search(line))


def match_toc(paras: list[dict], entries: list[dict], toc_paras: set[int]) -> list[dict]:
    items = []
    remaining = list(entries)
    toc_like = {i for i, p in enumerate(paras[:60])
                if i in toc_paras or looks_like_toc(p["text"], entries)}
    last_toc = max(toc_like) if toc_like else -1
    for i, p in enumerate(paras):
        text = p["text"].strip()
        lines = p.get("lines") or [text]
        lwords = p.get("line_words") or [[] for _ in lines]
        flat = lambda ws: [w for l in ws for w in l]  # noqa: E731
        if i in toc_like:
            # keep the TOC as text, but whatever follows its last entry (e.g. "INTRODUCTION
            # My article studies...") is body text and may start with a heading
            cut = max((k for k, l in enumerate(lines) if _is_toc_line(l)), default=len(lines) - 1)
            items.append({"kind": "para", "text": " ".join(lines[:cut + 1]).strip(),
                          "words": flat(lwords[:cut + 1])})
            lines, lwords = lines[cut + 1:], lwords[cut + 1:]
            if not lines:
                continue
            text = " ".join(lines).strip()
        elif i < last_toc:
            items.append({"kind": "para", "text": text, "words": flat(lwords)})  # inside the TOC pages
            continue
        matched = False
        # (a) the first one or two lines are a heading on their own
        for n in (1, 2):
            head = " ".join(lines[:n]).strip()
            if ENDS_WITH_PAGE_REF.search(head) or GARBLED_LEADER.search(head):
                break
            nh = norm(head)
            for e in remaining:
                if _similar(nh, e["norm"]) or _similar(nh, norm(e["title"])):
                    items.append({"kind": "heading", "text": head, "level": e["level"],
                                  "words": flat(lwords[:n])})
                    rest = " ".join(lines[n:]).strip()
                    if rest:
                        items.append({"kind": "para", "text": rest, "words": flat(lwords[n:])})
                    remaining.remove(e)
                    matched = True
                    break
            if matched:
                break
        # (b) run-in heading: "1. The Academic Reformers. The vision of..."
        if not matched:
            nt = norm(text)
            for e in remaining:
                if e["level"] >= 3 and len(e["norm"]) >= 6 and nt.startswith(e["norm"] + " "):
                    cut = _split_at_norm_prefix(text, len(e["norm"].replace(" ", "")))
                    head = text[:cut].strip()
                    rest = text[cut:].lstrip(" .:—-")
                    allw = flat(lwords)
                    k = len(head.split())
                    items.append({"kind": "heading", "text": head, "level": e["level"], "words": allw[:k]})
                    if rest:
                        items.append({"kind": "para", "text": rest, "words": allw[k:]})
                    remaining.remove(e)
                    matched = True
                    break
        if not matched:
            items.append({"kind": "para", "text": text, "words": flat(lwords)})
    return items


# ---------------------------------------------------------------- fallback

def outline_fallback(items: list[dict]) -> list[dict]:
    """Headings from outline numbering or ALL CAPS when there is no usable TOC."""
    out = []
    for it in items:
        if it["kind"] != "para":
            out.append(it)
            continue
        t = it["text"].strip()
        words = t.split()
        short = 1 <= len(words) <= 12 and len(t) <= 90 and not t.endswith((".", ",", ";", ":"))
        letters = [c for c in t if c.isalpha()]
        caps = len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.85
        nm = NUM.match(t)
        if (short and it.get("big")) or (short and nm and re.match(r"^[IVXLC]+\.$", nm.group("num"))) \
                or (short and caps and not LEADER.search(t)):
            out.append({**it, "kind": "heading", "text": t, "level": 2})
        else:
            out.append(it)
    return out


def build_structure(paras: list[dict], pages: int, layer_lines: list[str] | None = None
                    ) -> tuple[list[dict], dict]:
    paras = strip_furniture(paras, pages)
    entries, toc_paras = parse_toc(paras)
    if layer_lines:
        clean = parse_toc_lines(layer_lines)
        if len(clean) > len(entries):
            entries, toc_paras = clean, set()
    if entries:
        items = match_toc(paras, entries, toc_paras)
        used = "toc"
    else:
        items = [{"kind": "para", "text": p["text"].strip(), "big": p.get("big", False),
                  "words": [w for l in (p.get("line_words") or []) for w in l]} for p in paras]
        used = "outline"
    items = outline_fallback(items)
    return items, {"source": used, "toc_entries": len(entries),
                   "headings": sum(1 for i in items if i["kind"] == "heading")}


def normalize_heading_levels(items: list[dict]) -> list[dict]:
    """A logical outline: the first heading is H1 and no heading skips a level
    (H1 -> H3 becomes H1 -> H2). Changes the items in place and returns them."""
    prev = 0
    for it in items:
        if it.get("kind") != "heading":
            continue
        level = max(int(it.get("level", 2)), 1)
        if prev == 0:
            level = 1
        elif level > prev + 1:
            level = prev + 1
        it["level"] = level
        prev = level
    return items

# Remediation Workbench

A local web app for accessibility teams in public universities to remediate course files to meet federal law (ADA Title II, Section 504/508, WCAG 2.1 AA).

**Everything runs locally. No files are sent to an outside service.**

Version is shown at top of app and at http://localhost:8080/version

## What it does

Upload a batch of PDFs and Word files → returns remediated files + a review checklist.

**Word (.docx, .doc):**
- Auto-fixes: Document title/language, fake headings → real H1-H3, skipped heading levels, first table row → header row, legacy .doc → .docx
- Flags for human review: Images without alt text, vague links ("click here"), merged cells, layout tables
- Layout preserved. Font/size/color stay the same, only structure changes.

**PowerPoint (.pptx, .ppt):**
- Auto-fixes: Title/language, low-contrast text, legacy .ppt → .pptx
- Flags: No slide title, images without alt text, charts without alt text, vague links
- Optional: Tagged PDF export. Adds light backing behind text on photos if contrast fails.

**PDF - 3 routes:**
1. **Already tagged:** Keeps tags, fixes title/language/window title
2. **Untagged (born-digital):** Tags in-place - headings, paragraphs, lists, figures. Pages look identical (pixel-verified). Option to rebuild through Word.
3. **Scanned (including HeinOnline/JSTOR OCR layers):** `_accessible.pdf` looks exactly like original - image marked as background + invisible tagged text layer on top. Detects sideways/upside-down pages. Includes OCR confidence check. Optional reflowable text version.

Every output PDF is checked for tags, title, language, figures without alt, broken links. Runs veraPDF PDF/UA-1 check if installed.

Generates `remediation-report.html` and `remediation-report.csv` for each batch.

## Color Contrast

With "Fix low-contrast text" on (default):
Checks WCAG 2.1 - 4.5:1 normal text, 3:1 large (18pt+ or 14pt+ bold)
- Word/rebuilt PDFs: Failing colors darkened/lightened just enough to pass
- Tagged PDFs: Colors rewritten inside PDF, tags untouched
- Text on photos/gradients: Flagged for review
- Scans: Accessible text version is black on white

## What it can't do (requires human review)

- Alt text accuracy / decorative vs informative images
- Meaning conveyed by color alone, text inside images
- Complex multi-column reading order
- Math/equations (needs MathML or alt text)
- Always compare rebuilt PDF to original

## Setup

### Option A: Docker (Recommended - Windows/Mac/Linux)

1. Install Docker Desktop
2. Edit `docker-compose.yml` → set `APP_PASSWORD`
3. `docker compose up -d --build` (first build takes a few minutes)
4. Open `http://localhost:8080` — teammates use `http://<your-machine-name-or-IP>:8080`

### Option B: Run Directly (Linux / macOS)

Install dependencies:
- **Ubuntu/Debian:** `sudo apt install tesseract-ocr ghostscript qpdf poppler-utils libreoffice-writer`
- **macOS:** `brew install tesseract ghostscript qpdf poppler` + `brew install --cask libreoffice`

Run:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
HOST=0.0.0.0 APP_PASSWORD=change-me python app.py
```

Optional: Install veraPDF for PDF/UA validation.

## How to Use

1. Open app at `http://localhost:8080`, enter password
2. Upload batch of PDFs/DOCs/PPTs
3. Choose options (fix contrast, tagged PDF export, etc.)
4. Click Start remediation
5. Download remediated files + `remediation-report.html` for checklist
6. Complete human review items (alt text, reading order)

## License

Copyright (c) 2026 Mohammed Iliyas Sanjamala. All rights reserved.

Remediation Workbench is **free for personal and internal organizational use**, but it is **not open source**.
You may install it, use it and change its configuration settings (passwords, ports, AI model), but you may not
modify the source code, sell it, host it as a service or include it in a paid product without a written
commercial license. See [LICENSE](LICENSE) for the full terms.

When you use it, please credit: "Uses Remediation Workbench by Mohammed Iliyas Sanjamala".

Third-party components remain under their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

For a commercial license, contact mohdiliyas83@outlook.com.

## Acknowledgments

Inspired by Arizona State University's open-source accessibility tool.

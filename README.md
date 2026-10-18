# Remediation Workbench (version 10.0)

The version number is shown at the top of every page of the app, and at http://localhost:8080/version.

A small web app for accessibility teams. Upload a batch of PDFs and Word files, and it returns remediated files plus a checklist of what still needs a person. Everything runs on the machine hosting it. No file is sent to an outside service.

## What it does

**Word (.docx, .doc)**

| Fixed automatically | Flagged for review |
|---|---|
| Document title and language | Images with no alt text, a file name as alt text, or Word's auto-generated text |
| Bold or large "fake" headings become real Heading 1–3 | Vague link text ("click here"), long raw URLs, broken links |
| Skipped heading levels (H1 → H3 becomes H1 → H2) | Merged cells, empty header cells, possible layout tables |
| Typed bullets (•, -) become a real list *(optional, off by default)* | Hand-typed lists |
| First table row marked as a header row | Runs of empty paragraphs used for spacing |
| Legacy .doc converted to .docx | |

**Layout is preserved.** When a paragraph becomes a heading, its original font, size, color, alignment, indentation and spacing are kept, so the document looks exactly the same; only its structure changes. The one option that changes layout (turning typed bullets into real lists) is off unless you tick it.

Optionally exports a tagged PDF (PDF/UA mode) of each Word file.

**PowerPoint (.pptx, .ppt)**

| Fixed automatically | Flagged for review |
|---|---|
| Presentation title and text language | Slides with no title or an empty title; duplicate slide titles |
| Low-contrast text colours (measured on the rendered slides) | Pictures without alt text (drafted and queued on the review page) |
| Legacy .ppt converted to .pptx | Charts and diagrams without alt text; tables without a header row; merged cells |
| | Vague link text, broken links; busy slides where the title isn't read first |

Layout is untouched (verified slide by slide), with one optional exception: text that still fails contrast over a photo gets the lightest see-through backing that makes it pass ("PowerPoint: add see-through backing behind text on photos", on by default). The table header-row switch is left to a person because in PowerPoint it also restyles the row. Optionally exports a tagged PDF of each deck.

**PDF** — each file takes one of three routes, shown in the results:

1. **Already tagged:** tags are kept as-is; title, language and "show title in window bar" are fixed.
2. **Untagged (born-digital):** tagged in place by default. Nothing is redrawn, so pages look exactly as before (verified pixel by pixel): text becomes headings, paragraphs and lists in reading order, pictures become figures for alt text, and shapes, running heads, footers and page numbers become artifacts. Slide decks get the first slide's title as H1 and each slide title as H2. Where a PDF has no real space characters (LaTeX), screen readers get text with spaces restored. Ticking "Rebuild untagged PDFs through Word" uses the older route instead: rebuilt through Word. PDF → Word, the Word fixes above, then a tagged PDF export. You get `_accessible.pdf` plus `_editable.docx` so you can correct anything and export again.
3. **Scanned** (including scans with a hidden OCR layer, such as HeinOnline or JSTOR downloads): the main output, `_accessible.pdf`, **looks exactly like the original**. The page images are untouched and marked as background, with an invisible, tagged text layer on top: headings (found from the document's own table of contents, outline numbering or larger type), paragraphs in reading order, title and language. Running heads, footers and page numbers are marked as artifacts so screen readers skip them. Pages scanned sideways or upside down (common with photocopied textbook spreads) are detected and read the right way up; the pages keep their original orientation unless "show sideways pages upright" is ticked. Each page's OCR confidence is checked, and weak pages are listed for review. A reflowable `_text-version.pdf/.docx` can be produced on request for students who prefer resizable text.

Every output PDF is checked for tags, title, language, pages without text, figures without alt text, form fields and broken links. If veraPDF is installed, it also runs a PDF/UA-1 check.

Each batch comes with `remediation-report.html` and `remediation-report.csv` (one row per fix or review item), which is useful for tracking progress across courses.

## Colour contrast

With **Fix low-contrast text** ticked (the default), every text colour is checked against WCAG 2.1 (4.5:1 for normal text, 3:1 for large text: 18pt+, or 14pt+ bold):

- **Word files and rebuilt PDFs:** failing text colours and style colours (headings, links) are darkened or lightened just enough to pass, keeping the hue.
- **Already-tagged PDFs:** the failing colours are rewritten inside the PDF. Tags and layout are untouched, and shapes that share a colour with text are left alone.
- **Text on photos, gradients or the edge of shaded boxes** has no single colour that works, so it is flagged with the page and the text.
- **Scans:** the accessible text version is dark text on white, so it passes.

Contrast of text *inside images* (for example a screenshot or a chart's labels) can't be measured this way. Check those by eye, or describe them fully in the alt text.

## What it can't do

No tool can make every file compliant on its own. Plan for a human pass on each file, focused on the review checklist. The tool cannot judge:

- whether alt text is accurate, or whether an image is decorative
- meaning carried by colour alone, and text inside images
- reading order in complex multi-column layouts
- math and equations (these need MathML or alt text)
- whether a rebuilt PDF matches the original's layout — always compare them

Scanned textbook text-versions leave out figures, so add those back with alt text where students need them.

## Setup

### Option A: Docker (recommended, works on Windows, Mac or Linux)

1. Install Docker Desktop (Windows/Mac) or Docker Engine (Linux).
2. In this folder, edit `docker-compose.yml` and set `APP_PASSWORD`.
3. Run `docker compose up -d --build`. The first build takes several minutes.
4. Open `http://localhost:8080`. Teammates use `http://<this-machine's-name-or-IP>:8080`.

### Option B: Run directly (Linux or macOS)

Install the system tools first.

- **Ubuntu/Debian:** `sudo apt install tesseract-ocr ghostscript qpdf poppler-utils libreoffice-writer`
- **macOS (Homebrew):** `brew install tesseract ghostscript qpdf poppler` and `brew install --cask libreoffice`

Then:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
HOST=0.0.0.0 APP_PASSWORD=change-me python app.py
```

On Windows without Docker, use WSL (Ubuntu) and follow the Linux steps.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8080` | Use `HOST=0.0.0.0` so teammates can reach it |
| `APP_PASSWORD` | empty (off) | Shared password for the team (browser sign-in prompt) |
| `WORKERS` | `2` | Files processed at the same time |
| `RETENTION_DAYS` | `7` | Batches (uploads and outputs) are deleted after this, at startup |
| `MAX_UPLOAD_MB` | `1000` | Largest batch upload |
| `DOC_LANG` | `en-US` | Default document language |
| `OCR_LANG` | `eng` | Default Tesseract language(s), e.g. `eng+spa` (install matching language packs) |
| `CHECK_LINKS` | `1` | Default for the link check box |
| `CONVERT_LISTS` | `0` | Default for turning typed bullets into real lists (changes indentation) |
| `VERAPDF` | auto-detect | Path to the veraPDF command-line tool, if installed |
| `SOFFICE` | auto-detect | Path to LibreOffice, if it's not on the PATH |
| `OLLAMA_URL` / `OLLAMA_MODEL` | empty | Optional local alt-text drafts (below) |

### Optional: veraPDF for PDF/UA checks

Install the free veraPDF command-line tool (verapdf.org), then set `VERAPDF=/path/to/verapdf`. Failures show up in each file's review list.

### Alt text (with optional local AI drafts)

Every image that needs alt text, in Word files, rebuilt PDFs and already-tagged PDFs, is listed on the **alt-text review page** (button on each batch's results page). Each card shows the image, the nearby text from the document, and a draft. Choose **Approve** (after editing) or **Decorative**, then **Save decisions**. The tool writes your text into the files and regenerates the PDFs.

To get AI drafts, install [Ollama](https://ollama.com) on the same computer (Windows installer available), then in PowerShell run `ollama pull qwen2.5vl:7b`. The two `OLLAMA_` lines in `docker-compose.yml` point the tool at it. The upload page says whether drafts are on. The model runs on this computer's GPU, so no image leaves the machine. Drafts are written into the files straight away (better than nothing) but stay marked "Not reviewed" until someone approves them.

Notes:
- If the upload page says Ollama can't be reached, set the Windows environment variable `OLLAMA_HOST` to `0.0.0.0`, restart Ollama, and refresh.
- Already-tagged PDFs: figures are found even when the file uses its own tag names (such as /Image or /Graphic mapped to Figure). Each figure's position is measured from what it actually draws, and its review card shows the surrounding page with the figure outlined, so pictures that the PDF split into several pieces still make sense. Thin lines, specks, and small pieces of a larger picture are pre-marked as likely decorative. Choosing "Decorative" converts the figure to an artifact inside the PDF, so screen readers skip it; no Acrobat step needed.
- Images flagged "Needs a longer description too" (detailed charts, diagrams, equations) need a paragraph or data table in the document as well as short alt text.
- Scanned PDFs: the text version leaves figures out, so they don't appear on the review page.

## Before sharing with the team

- Ask your IT team to confirm where this should run (a staff workstation or a small campus VM) and whether a shared password is enough, or whether it should sit behind campus sign-in.
- Uploaded files are stored in `data/jobs/` until the retention period ends. Keep that folder on university-managed storage.
- The link checker visits the web addresses found in documents. Turn it off in the form if that's a concern.

## Project layout

```
app.py                     web app (upload, progress, downloads, reports)
remediate/pipeline.py      picks the route for each file
remediate/docx_fix.py      Word fixes and checks
remediate/pdf_fix.py       PDF routes, metadata fixes, checks, veraPDF
remediate/ocr_rebuild.py   scanned PDF → structured Word via Tesseract layout
remediate/alt_text.py      optional Ollama alt-text drafts
remediate/common.py        report format, options, link checker, LibreOffice export
templates/, static/        pages and styles
```

## Ideas for next steps

- PowerPoint (.pptx): slide titles, reading order, alt text, same approach as Word
- Blackboard Ultra documents: export, check, and re-import
- A "batch compare" view that shows the original and rebuilt PDF side by side


## Handwriting test (tools/handwriting_test.py)

A one-off test of how well the local AI model reads handwritten pages (for example math solutions).
It changes nothing in the tool. Copy the PDF into `C:\tools\remediator\data`, then run:

    docker compose exec workbench python tools/handwriting_test.py /data/FILE.pdf

The transcription (Markdown, math in LaTeX) is saved to `C:\tools\remediator\data\handwriting_test\FILE.md`.


## What's new in 9.17

- Handwriting (optional, PDFs): "Handwriting: transcribe with AI" reads each box or section with the local model;
  a review page shows each one next to its transcription with the math drawn; after review, "Build typed version"
  makes a Word file with real equations plus a tagged PDF. Works on worksheets with boxes, lined paper, plain
  pages and sideways scans; typed pages are skipped.
- History keeps its own copies of originals, remediated files and reports (HISTORY_DAYS, default 365; 0 = forever),
  even after a batch is deleted or cleaned up. "Delete files" removes the copies; the record stays.
- Logic check on every output: headings start at H1 and never skip a level (scans included); layout tables in tagged
  PDFs become text; data tables without headers get a header row; figures without meaningful alt text, unlabeled
  form fields and pages whose reading order jumps around are listed; odd bullet colours follow the text; video and
  audio are flagged for captions and transcripts.
- Background pictures behind text (Word, PowerPoint, PDF) are marked decorative so screen readers read the text.
- Contrast fixes use the standard colours (black, dark blue, dark red, dark green, white) unless unticked.
- AI drafts from "Draft with AI" are written straight into the files (still marked unreviewed).
- Alt-text drafts follow the team guidance: at most two short lines, a long description (50 words or fewer)
  for complex images, and flags for text in images and colour-only meaning.


## What's new in 9.18

- Automatic mode (the default): the tool applies everything each file type needs; no options to choose.
  Word: headings, title, language, contrast (standard colours), real bullet lists, tables, alt-text drafts,
  links. PowerPoint: the same plus a see-through backing for text on photos. PDF: tagged in place, contrast,
  alt-text drafts, links. Scans: OCR, tagged scan and a reflowed text version. Files with more than 60 pictures
  leave AI drafts for "Draft with AI" on the review page. "Choose options myself" shows the individual options.
- Scanned PDFs get a "Transcribe handwriting" button on the results page.


## What's new in 9.22 (team remediation procedures)

- OCR: a list of low-confidence words to proofread per page, and pages with printed areas the OCR didn't read
  (missed text, tables, pictures). Watermarks in digital PDFs (large, rotated or pale text repeated across pages)
  become artifacts and are never recoloured.
- Headings: when a PDF's tags don't start with H1, an H1 is added at the start of the tag structure (carrying the
  document title, no visible change); existing headings are kept and no level is skipped afterwards.
- Non-table tables: tables with headings, lists or figures in their cells, or mostly empty cells, lose their table
  tags; cells become paragraphs or groups.
- Bullets: theme bullets (slide master and layouts) and picture bullets become standard bullets in the text colour
  (PowerPoint and Word).
- Gradients: when text fails over a gradient text box or a gradient slide background, the gradient is replaced
  with a solid colour that passes (a colour from the gradient when one works).


## What's new in 9.23

- Already-tagged PDFs with no heading tags at all (every line tagged as a paragraph, as some syllabus and
  export tools produce) get their headings found from the text styling: the title line on page 1 becomes H1,
  the main section style H2, smaller bold subheadings H3. Nothing on the page changes.


## What's new in 9.24

- Fixed: when tagging untagged PDFs in place, text written with the Tj operator (Chrome / Skia "Save as PDF",
  many web exports) was counted twice, so tags after the first heading pointed at the wrong text. Each text
  operator is now counted once.
- Bold is read from the font's real weight (FontWeight in the font descriptor) when the font name says "Regular".
- Coloured headings (for example green section titles only slightly larger than the body) are recognised; the
  opening title line becomes H1, coloured sections H2, bold subheadings H3; labels with a colon are not headings.


## What's new in 9.25 (handwritten documents)

- Handwritten scans are detected (average OCR confidence under 65%) and flagged at the top of the results.
  In Automatic mode, with the AI on, handwriting transcription starts by itself. The reflowed text version
  (built from OCR guesses) is left out for handwriting.
- Diagrams: the AI writes each drawing as a [Diagram: description] line and locates it; the review page shows
  the cropped drawing; the typed Word version gets the picture with the description as its alt text.
- Build typed version also rebuilds the scan's accessible PDF: same look, but its invisible text layer is the
  reviewed transcription (math made speakable, diagrams described, first line as H1, short labels as H2).
- Any PDF that still has no headings gets an H1 carrying its title at the start of the tag structure.

## What's new in 9.29

- Alt-text drafts no longer wait for "Draft with AI": when a batch finishes, any images still without a draft
  (large files defer them so the batch finishes quickly) are drafted automatically in the background and written
  into the files. The review page shows the progress; all that's left is to review and Approve all.
- Approve all explains itself: "everything is already reviewed", "drafts are still being written", or "the
  remaining images have no text". When every image is reviewed, the page says so and hides the bulk buttons.

## What's new in 9.30

- The results page shows alt-text drafting live ("Writing AI alt-text drafts: 37 of 149 images…" with a bar).
- Checklists stay current: alt-text lines written at processing time are replaced by one live line
  ("N image(s) still need review… (N with AI drafts ready to approve)") and disappear once every image is approved
  or decorative. A "Wrote AI alt-text drafts for N image(s)" line is added to Fixed. Reports and History counts update.

## What's new in 9.31

- AI alt-text drafting is part of remediating: a batch with deferred drafts stays on step 2 ("Writing AI alt-text
  drafts…") and "Review alt text" appears only when every draft is written, so the review page opens ready.
- Drafts are written 3 at a time (DRAFT_PARALLEL, default 3). For Ollama to work on them in parallel, set the
  Windows environment variable OLLAMA_NUM_PARALLEL=3 and restart Ollama.
- The home page shows "Drafting alt text… N of M" for a batch that is still drafting.

## What's new in 9.32

- Fixed: complex images' AI replies (with their long description) could exceed the reply limit and arrive cut off,
  so the draft was discarded. The limit is higher and a cut-off reply's alt text is still used.
- Drafting counts only real drafts; failed requests are retried one at a time with a longer wait, and after a
  failure the batch continues one image at a time. The reason for any failure is shown.
- Defaults: 2 drafts at a time (DRAFT_PARALLEL), 300 seconds per draft (DRAFT_TIMEOUT).

## What's new in 9.33

- Alt-text drafting restored to exactly how version 9.29 produced drafts: one image at a time, the same reply
  length and the same 180-second wait. Everything added since 9.29 stays: drafts finish before review opens, live
  progress, live checklists and History counts, and (only when a draft fails) salvaging, retrying and a reason.

## What's new in 9.34

- Automatic mode drafts alt text for every image while the file is processed, however many pictures it has (no
  large-file deferral); the progress line counts the drafts as they are written. Drafting after processing only
  retries images whose draft failed.
- The stylesheet and logo URLs carry the version number, so browsers load the new look after every update
  (no more half-styled pages after updating).


## Version 10.0 (consolidated release)

Everything from 9.34 (all features, the 9.29-style alt-text drafting) plus:
- Alt text: PDF figures are cropped and drafted page by page while the file is remediated; a page whose figures
  can't be measured in 60 seconds is skipped (never freezes the file); an empty or unfinished AI answer is never
  saved as a blank draft and is retried once with a smaller image; the progress line always shows drafting.
- PDF contrast: text whose colour is shared with shapes (e.g. grey page numbers next to a grey rule) is recoloured
  on its own; the shapes keep their colour.
- PowerPoint: slides without a title get a title placed just off the slide (screen readers and checkers use it,
  the slide looks the same); tables get their header row turned on.
- Word: a document with no headings gets its first line as Heading 1, looking the same.

## Version 10.1

Fix: when OCR runs on a PDF (even only on image-only slides), OCRmyPDF/Tesseract wrote their names into the PDF's
Creator fields, and Ally then scored the whole file as "scanned" (0%). Every PDF output now has those labels
replaced with "Remediation Workbench"; the text and tags are untouched.

## Version 10.2 (handwritten notes)

- A digital PDF whose pages are really scanned paper (a full-page, mostly white portrait image) with a few typed
  notes on top is now recognised as handwriting: it is flagged (checkers only score the typed notes; screen readers
  can't read the writing), transcription starts in Automatic mode, and "Transcribe handwriting" appears for it.
  Its route and output are unchanged.
- Handwritten scans take their title from the file name instead of from OCR guesses.

## Version 10.3

Handwriting detection also catches neat block printing: a scan counts as handwritten when its average OCR confidence
is under 75%, or when 12% or more of its words are low-confidence (typed scans read at ~85-95% with ~5% unsure words).
Handwritten scans get no OCR "text version" (it would be full of misreads); transcription starts in Automatic mode.

## Version 10.4 (typed versions of handwritten notes)

- The AI is told not to add numbering that isn't on the page, to write circled letters as (A), to keep headings on
  their own line, and to describe diagrams fully (what kind of drawing, every labelled part, how parts connect).
- Diagram crops keep a wider margin so edges and labels aren't cut off.
- Typed version: the title and Heading 1 come from the notes' own first heading (not the file name); section titles
  such as "1.2 ..." and "EXAMPLE:" become headings; no "Page N" headings.

## Version 10.5 (typed versions score high in Ally)

- Simple inline math in typed versions (a symbol, vector, subscript or power: q, q₁, V⃗, p⃗, n̂, x²) is written as
  ordinary characters, not as equation objects (which LibreOffice exported as images without descriptions).
- Every equation that remains gets alt text in the typed PDF with its spoken form ("Equation: (dv)/(dt) = ...").

## Version 10.6

- Start remediation can be clicked only once: it shows "Uploading… N%" with a progress bar and opens the batch when
  the upload finishes (the old code disabled the first button in the form, a file's Remove button, so big uploads
  could be submitted twice).
- Stop works on large files: OCRmyPDF, page rendering, per-page OCR and LibreOffice are ended within about a second
  of Stop, and PDF figure pages and OCR pages check for Stop; a stop can no longer be swallowed by an error handler.

## Version 10.7

- Review handwriting: "Approve all" approves every box that has text (one request) and skips empty boxes, after a
  confirmation; "Build all typed versions" builds every file whose boxes are all reviewed, one after another, with
  progress (each build is the same as the file's own button).
- Big batches stay responsive: math previews are drawn only as boxes scroll into view (and cached), the batch record
  is saved compactly, and the server has more request threads.

## Version 10.8

Drawings in handwritten notes reach the typed version even when the AI's transcription doesn't mention them: a
drawing detector (ink much taller than a line of writing, ruled lines and underlines removed, nearby pieces merged)
finds hand-drawn figures in boxes without a [Diagram: ...] line, crops them, has the local AI describe them and adds a
[Diagram: ...] line (reviewable). This runs at transcription, at Turn page and re-read, and at Build typed version
(so batches processed earlier get their drawings without re-uploading). Boxes the AI already described are unchanged.

## Version 10.9

New "Typed version report" for handwritten files (button on the results page and the Review handwriting page): per
file, boxes approved / skipped / not reviewed, whether the typed version is built (with links), headings, drawings with
their alt text, equations as read aloud, and the full typed text page by page. Print or save as PDF, or download as CSV.
It only reads the saved batch; nothing else changes.

## Version 10.10

PowerPoint: an embedded video's or audio clip's preview picture gets a description ("Embedded YouTube video:
<slide title>"), and pictures in the slide master/layouts without a description are marked decorative (template
graphics). Checkers such as Ally count both as images; nothing already described is changed.

## Version 10.11

- PDF contrast: CMYK text colours are converted the way they are measured (MuPDF's colour management), so the
  planned colour fixes apply (before, CMYK colours never matched: "0 changes"). Text that fails only where it sits
  (e.g. black text on a dark band or picture, while the same black passes on white pages) is recoloured in place
  with the closest team standard colour that passes there, only when its colour is the one measured as failing;
  invisible text layers get white or black. Tested on the TNTP Opportunity Myth report: 725 -> 98 failing pieces.
- Times are shown and recorded in Eastern Time (EST/EDT) instead of the container's UTC (APP_TZ can change it).

## Version 10.12 (PowerPoint contrast)

- PowerPoint text that fails contrast on a plain background is recoloured where it sits: each failing piece is matched
  to its text box or table cell (short cell text like "$4" by exact text and column) and only those runs change,
  so theme colours, colours inherited from the master, and black text that fails on one dark slide but passes on
  white slides are all fixed. Text in filled cells/shapes is fixed even when the background reads as uneven.
  SmartArt text is recoloured in the diagram's data and drawing. Gradient replacement and see-through backings
  still handle text on pictures and gradients. Virtual Class Meeting 4: 56 -> 3 failing; Meeting 1: 47 -> 18.
- Recolouring (PDF and PowerPoint) uses the team's standard colours: neutral text becomes black or white,
  coloured text keeps its family (dark blue, dark red, dark green).

## Version 10.13

PDF in-place contrast recolouring now finds text anywhere along a measured line (not only near its middle), follows
rotated and vertical text (e.g. running headers up the page edge), and understands colours set through named ICC
colour spaces (spot colours are matched by position only). TNTP Opportunity Myth report: 725 -> 46 failing pieces.

## Version 10.14 (contrast across PDF and PowerPoint)

- PDF in-place recolouring follows text into form XObjects (nested drawing blocks), tracks the fill colour through
  q/Q save-restore blocks as viewers do, matches pieces anywhere within a long text instruction, and also runs for
  text on uneven backgrounds, where it recolours only when the new colour passes with a 35% margin.
- PowerPoint: the table cell at the measured height is chosen when the same words appear in several cells; short
  labels ("#1") are found inside longer cells; uneven backgrounds and SmartArt use the same 35% margin rule; see-through
  backings remain the fallback for text on pictures.
- Results: TNTP report 725 -> 0, Virtual Class Meeting 1 47 -> 0, Meeting 4 56 -> 0, quiz 10 -> 2.

## License

The project's own code is released under the MIT License (see LICENSE). It relies on other open-source software
under its own licenses, including AGPL-licensed PyMuPDF and Ghostscript; see THIRD_PARTY_NOTICES.md.

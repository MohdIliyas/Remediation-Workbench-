# Third-party software

The MIT license in LICENSE covers this project's own code. The tool installs and uses other open-source
software, each under its own license. Notable ones:

| Component | License | Note |
| --- | --- | --- |
| PyMuPDF (pymupdf), pdf2docx (uses PyMuPDF) | AGPL-3.0 (or commercial licence from Artifex) | If you distribute the tool or run it as a network service for others, AGPL terms apply to the combined program |
| Ghostscript (in the Docker image, used by OCRmyPDF) | AGPL-3.0 | Same as above |
| OCRmyPDF | MPL-2.0 | |
| Tesseract OCR | Apache-2.0 | |
| pikepdf | MPL-2.0 | |
| LibreOffice | MPL-2.0 | |
| pandoc (pypandoc_binary) | GPL-2.0-or-later | Bundled pandoc binary |
| Flask, Waitress, python-docx, python-pptx, Pillow, requests, matplotlib, OpenCV | BSD / MIT / ZPL / Apache-style | |
| Qwen2.5-VL 7B (run separately in Ollama, not included) | Apache-2.0 | Model weights are downloaded by the user |

Check each project's current license before redistributing.

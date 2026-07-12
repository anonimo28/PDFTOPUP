# PDFTOPUP — PDF ↔ EPUB Converter

A web app that converts scanned PDFs to text-based EPUBs (via OCR) and EPUBs to PDFs.

## Features

- **PDF → EPUB** — Detects text layers; OCRs scanned pages with Tesseract. Groups pages into chapters. 13 OCR languages supported.
- **EPUB → PDF** — Extracts text from EPUB chapters and formats them into a PDF with chapter titles and word-wrapped body text.
- Dark-themed drag-and-drop web UI with progress tracking.

## Requirements

- Python 3.10+
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) (`brew install tesseract`)
- [Poppler](https://poppler.freedesktop.org/) (`brew install poppler`)

## Quick Start

```bash
git clone https://github.com/anonimo28/PDFTOPUP.git
cd PDFTOPUP
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./start.sh
```

Open http://127.0.0.1:8080 in your browser.

## Stack

Flask, PyMuPDF, Tesseract OCR, pdf2image, EbookLib, Pillow.

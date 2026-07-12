#!/usr/bin/env python3
"""PDF → EPUB converter: OCRs scanned PDFs and packages text into an EPUB."""

import os
import re
import time
import threading
import tempfile
import traceback
import uuid
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template_string

import fitz  # PyMuPDF
import pytesseract
from pdf2image import convert_from_path
from ebooklib import epub

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

# ── System dependency checks ────────────────────────────────────────────────

def check_dependencies():
    missing = []
    try:
        import subprocess
        subprocess.run(["tesseract", "--version"], capture_output=True, check=True)
    except Exception:
        missing.append("tesseract-ocr (install: brew install tesseract)")
    try:
        subprocess.run(["pdftoppm", "-v"], capture_output=True, check=True)
    except Exception:
        missing.append("poppler (install: brew install poppler)")
    if missing:
        print("ERROR: Missing system dependencies:")
        for m in missing:
            print(f"  - {m}")
        print("Install them and restart the app.")
        exit(1)

check_dependencies()

# ── Templates ───────────────────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDF → EPUB Converter</title>
<style>
  :root { --bg:#0f1117; --card:#1a1d27; --accent:#7c5cff; --accent2:#00d4aa;
          --text:#e4e6eb; --muted:#888; --err:#ff5757; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,
        BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; min-height:100vh;
        display:flex; align-items:center; justify-content:center; padding:2rem; }
  .container { max-width:560px; width:100%; }
  h1 { font-size:1.6rem; margin-bottom:.4rem; }
  .subtitle { color:var(--muted); margin-bottom:2rem; font-size:.9rem; }
  .upload-box { background:var(--card); border-radius:16px; padding:2.5rem 2rem;
    text-align:center; border:2px dashed #333; transition:border-color .2s; }
  .upload-box.dragover { border-color:var(--accent); background:#1e1b2e; }
  input[type=file] { display:none; }
  .btn { background:var(--accent); color:#fff; border:none; padding:.8rem 2rem;
    border-radius:10px; font-size:1rem; cursor:pointer; font-weight:600;
    transition:transform .1s, opacity .2s; }
  .btn:hover { transform:translateY(-1px); }
  .btn:disabled { opacity:.4; cursor:default; transform:none; }
  .btn.secondary { background:transparent; border:1px solid #444; margin-top:1rem; }
  .or { color:var(--muted); margin:1.2rem 0; font-size:.85rem; }
  .meta-fields { margin-top:1.5rem; text-align:left; display:none; }
  .meta-fields.active { display:block; }
  .meta-fields label { display:block; font-size:.8rem; color:var(--muted);
    margin:.8rem 0 .3rem; }
  .meta-fields input, .meta-fields select {
    width:100%; background:#11131a; border:1px solid #333; border-radius:8px;
    padding:.6rem .8rem; color:var(--text); font-size:.9rem; }
  .meta-fields input:focus { outline:none; border-color:var(--accent); }
  .progress { margin-top:1.5rem; display:none; }
  .progress.active { display:block; }
  .bar { background:#11131a; border-radius:8px; height:8px; overflow:hidden; }
  .bar-fill { background:linear-gradient(90deg,var(--accent),var(--accent2));
    height:100%; width:0%; transition:width .3s; border-radius:8px; }
  .status { color:var(--muted); font-size:.85rem; margin-top:.6rem; text-align:center; }
  .error { color:var(--err); }
  .success { color:var(--accent2); }
  .muted { color:var(--muted); font-size:.8em; }
  .lang-row { display:flex; gap:1rem; }
  .lang-row > div { flex:1; }
  .filename { color:var(--accent2); font-weight:600; font-size:.85rem;
    margin-top:.5rem; }
</style>
</head>
<body>
<div class="container">
  <h1>📕 → 📖 PDF to EPUB</h1>
  <p class="subtitle">Upload a scanned PDF, get a text-based EPUB book back.</p>

  <div class="upload-box" id="dropZone">
    <input type="file" id="fileInput" accept="application/pdf">
    <p>Drag & drop a PDF here</p>
    <p class="or">— or —</p>
    <button class="btn" onclick="document.getElementById('fileInput').click()">
      Choose PDF
    </button>
    <div class="filename" id="fileName"></div>
  </div>

  <div class="meta-fields" id="metaFields">
    <label>Title</label>
    <input type="text" id="bookTitle" placeholder="Auto-detect from PDF">
    <label>Author</label>
    <input type="text" id="bookAuthor" placeholder="Unknown">
    <div class="lang-row">
      <div>
        <label>OCR Language</label>
        <select id="ocrLang">
          <option value="eng">English</option>
          <option value="spa">Spanish</option>
          <option value="fra">French</option>
          <option value="deu">German</option>
          <option value="ita">Italian</option>
          <option value="por">Portuguese</option>
          <option value="chi_sim">Chinese (Simplified)</option>
          <option value="chi_tra">Chinese (Traditional)</option>
          <option value="jpn">Japanese</option>
          <option value="kor">Korean</option>
          <option value="rus">Russian</option>
          <option value="ara">Arabic</option>
          <option value="hin">Hindi</option>
        </select>
      </div>
      <div>
        <label>DPI (higher = better quality, slower)</label>
        <select id="dpi">
          <option value="200">200 (fast)</option>
          <option value="300" selected>300 (balanced)</option>
          <option value="400">400 (high quality)</option>
        </select>
      </div>
    </div>
    <button class="btn" id="convertBtn" style="width:100%;margin-top:1.5rem;"
      onclick="convert()" disabled>Convert to EPUB</button>
  </div>

  <div class="progress" id="progress">
    <div class="bar"><div class="bar-fill" id="barFill"></div></div>
    <div class="status" id="statusText"></div>
  </div>
</div>

<script>
let file = null;
const dz = document.getElementById('dropZone');
const fi = document.getElementById('fileInput');

fi.addEventListener('change', e => { if(e.target.files[0]) selectFile(e.target.files[0]); });
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('dragover');
  if(e.dataTransfer.files[0]) selectFile(e.dataTransfer.files[0]);
});

function selectFile(f) {
  file = f;
  document.getElementById('fileName').textContent = '✓ ' + f.name;
  document.getElementById('metaFields').classList.add('active');
  // Auto-fill title from filename
  const base = f.name.replace(/\\.pdf$/i, '').replace(/[_-]+/g,' ').trim();
  document.getElementById('bookTitle').placeholder = base;
  document.getElementById('convertBtn').disabled = false;
}

let pollTimer = null;
async function convert() {
  if(!file) return;
  const fd = new FormData();
  fd.append('pdf', file);
  fd.append('title', document.getElementById('bookTitle').value || '');
  fd.append('author', document.getElementById('bookAuthor').value || '');
  fd.append('lang', document.getElementById('ocrLang').value);
  fd.append('dpi', document.getElementById('dpi').value);

  document.getElementById('progress').classList.add('active');
  document.getElementById('convertBtn').disabled = true;
  document.getElementById('statusText').textContent = 'Uploading...';
  document.getElementById('barFill').style.width = '5%';

  try {
    const res = await fetch('/convert', { method:'POST', body:fd });
    const data = await res.json();
    if(data.error) {
      document.getElementById('statusText').innerHTML =
        '<span class="error">Error: ' + data.error + '</span>';
      document.getElementById('convertBtn').disabled = false;
      return;
    }
    pollTimer = setInterval(() => poll(data.job), 1000);
  } catch(err) {
    document.getElementById('statusText').innerHTML =
      '<span class="error">Network error: ' + err + '</span>';
    document.getElementById('convertBtn').disabled = false;
  }
}

async function poll(job) {
  try {
    const r = await fetch('/status/' + job);
    const s = await r.json();
    document.getElementById('barFill').style.width = s.pct + '%';
    document.getElementById('statusText').innerHTML = s.status +
      ' <span class="muted">(' + s.pct + '%)</span>';
    if(s.pct >= 100 && s.done) {
      clearInterval(pollTimer);
      document.getElementById('statusText').innerHTML =
        '<span class="success">✓ Done! Download starting...</span>';
      // Trigger download without navigating away
      const a = document.createElement('a');
      a.href = '/download/' + job;
      a.download = '';
      document.body.appendChild(a);
      a.click();
      a.remove();
      document.getElementById('convertBtn').disabled = false;
      setTimeout(() => {
        document.getElementById('progress').classList.remove('active');
      }, 3000);
    }
    if(s.error) {
      clearInterval(pollTimer);
      document.getElementById('statusText').innerHTML =
        '<span class="error">Error: ' + s.error + '</span>';
      document.getElementById('convertBtn').disabled = false;
    }
  } catch(e) { /* ignore transient poll failures */ }
}
</script>
</body>
</html>"""

# ── In-memory job store ─────────────────────────────────────────────────────

JOBS = {}


def update_job(job_id, **kw):
    JOBS.setdefault(job_id, {}).update(kw)


def convert_pdf_to_epub(job_id, pdf_path, title, author, lang, dpi):
    """Worker: OCR each page, build an EPUB."""
    try:
        update_job(job_id, status="Opening PDF...", pct=5)

        # First, try PyMuPDF to detect if PDF already has a text layer
        doc = fitz.open(pdf_path)
        total = doc.page_count
        update_job(job_id, status=f"Analyzing {total} pages...", pct=10)

        # Extract metadata
        if not title:
            meta = doc.metadata
            title = meta.get("title") or Path(pdf_path).stem.replace("_", " ").title()
        if not author:
            meta = doc.metadata
            author = meta.get("author") or "Unknown"

        # Check if pages have extractable text (digitized PDF) or need OCR
        pages_text = []
        needs_ocr = False
        for i in range(total):
            text = doc.load_page(i).get_text("text").strip()
            pages_text.append(text)
            if i < 5 and len(text) < 50:  # sample first 5 pages
                needs_ocr = True

        # If any of the sampled pages are empty, scan all to decide
        if needs_ocr:
            needs_ocr = any(len(t) < 20 for t in pages_text)

        doc.close()

        if needs_ocr:
            update_job(job_id, status="Rendering pages to images...", pct=15)
            images = convert_from_path(
                pdf_path,
                dpi=int(dpi),
                fmt="png",
            )
            update_job(job_id, status="Running OCR (this may take a while)...", pct=25)

            for i, img in enumerate(images):
                text = pytesseract.image_to_string(img, lang=lang)
                pages_text[i] = text.strip()
                pct = 25 + int((i + 1) / total * 65)
                update_job(
                    job_id,
                    status=f"OCR page {i + 1} of {total}...",
                    pct=min(pct, 90),
                )
        else:
            update_job(job_id, status="Extracting text layer...", pct=90)

        # ── Build EPUB ────────────────────────────────────────────────────
        update_job(job_id, status="Building EPUB...", pct=92)

        book = epub.EpubBook()
        book.set_identifier(f"pdf2epub-{job_id}")
        book.set_title(title)
        book.set_language(lang.split("_")[0])
        book.add_author(author)

        cover_html = f"<html><body><h1>{title}</h1><h3>by {author}</h3></body></html>"
        cover = epub.EpubHtml(title="Cover", file_name="cover.xhtml",
                              content=cover_html)
        book.add_item(cover)

        spine = [cover]
        chapters = []

        # One chapter per 10 pages (group to avoid tiny chapters)
        pages_per_chapter = 10
        for start in range(0, total, pages_per_chapter):
            chunk = pages_text[start:start + pages_per_chapter]
            end_page = min(start + len(chunk), total)
            ch_title = f"Pages {start + 1}–{end_page}"

            body_parts = []
            for idx, pt in enumerate(chunk):
                page_num = start + idx + 1
                pt = re.sub(r"\n{3,}", "\n\n", pt.strip())
                if pt:
                    body_parts.append(f'<div class="page" id="p{page_num}"><p>{pt}</p></div>')

            ch_html = (
                "<html><head><style>"
                ".page { margin-bottom:1.5em; } "
                "</style></head><body>"
                + "\n".join(body_parts)
                + "</body></html>"
            )

            ch_file = f"chap_{start // pages_per_chapter + 1:04d}.xhtml"
            ch = epub.EpubHtml(title=ch_title, file_name=ch_file, content=ch_html)
            book.add_item(ch)
            chapters.append(ch)
            spine.append(ch)

        book.spine = spine

        # Table of contents linking to chapter items
        book.toc = chapters

        # Add navigation files
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # Write EPUB to temp file
        out_path = os.path.join(tempfile.gettempdir(), f"{job_id}.epub")
        epub.write_epub(out_path, book, {})

        # Clean up the uploaded PDF
        try:
            os.unlink(pdf_path)
        except Exception:
            pass

        update_job(job_id, status="Done!", pct=100, done=True, out_path=out_path,
                   out_name=f"{title}.epub")

    except Exception as e:
        traceback.print_exc()
        update_job(job_id, error=str(e), status=f"Error: {e}", done=True)


# ── Routes ───────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/convert", methods=["POST"])
def convert():
    f = request.files.get("pdf")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    job_id = uuid.uuid4().hex[:12]

    pdf_path = os.path.join(tempfile.gettempdir(), f"{job_id}.pdf")
    f.save(pdf_path)

    JOBS[job_id] = {"pct": 0, "status": "Queued...", "done": False}

    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()
    lang = request.form.get("lang", "eng")
    dpi = request.form.get("dpi", "300")

    t = threading.Thread(
        target=convert_pdf_to_epub,
        args=(job_id, pdf_path, title, author, lang, dpi),
        daemon=True,
    )
    t.start()

    return jsonify({"job": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("out_path"):
        return "File not ready", 404
    return send_file(job["out_path"], as_attachment=True,
                     download_name=job.get("out_name", "book.epub"))


# ── Background job cleanup ─────────────────────────────────────────────────

def cleanup_old_jobs():
    while True:
        time.sleep(300)
        now = time.time()
        stale = [jid for jid, j in list(JOBS.items())
                 if j.get("done") and now - j.get("ts", 0) > 3600]
        for jid in stale:
            job = JOBS.pop(jid, {})
            out = job.get("out_path")
            if out and os.path.exists(out):
                try:
                    os.unlink(out)
                except Exception:
                    pass


cleanup_thread = threading.Thread(target=cleanup_old_jobs, daemon=True)
cleanup_thread.start()

# ── Add timestamp on job completion ────────────────────────────────────────

original_update = update_job


def update_job_with_ts(job_id, **kw):
    if kw.get("done"):
        kw["ts"] = time.time()
    original_update(job_id, **kw)


update_job = update_job_with_ts

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=True)

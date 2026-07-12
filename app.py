#!/usr/bin/env python3
"""PDF ↔ EPUB converter: OCR scanned PDFs to EPUB, or EPUB to PDF."""

import os
import re
import time
import threading
import tempfile
import traceback
import uuid
from pathlib import Path
from html.parser import HTMLParser

from flask import Flask, request, jsonify, send_file, render_template_string

import fitz  # PyMuPDF
import pytesseract
from pdf2image import convert_from_path
from ebooklib import epub
from cleaner import clean_ocr_text, extract_structure

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

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

# ── HTML → plain text ──────────────────────────────────────────────────────

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._text = []
        self._skip = False
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skip = True
        if tag in ('p', 'br', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'tr'):
            self._text.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self._skip = False
        if tag in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'tr'):
            self._text.append('\n')
    def handle_data(self, data):
        if not self._skip:
            self._text.append(data)
    def get_text(self):
        return re.sub(r'\n{3,}', '\n\n', ''.join(self._text)).strip()


def strip_html(html):
    ext = TextExtractor()
    ext.feed(html)
    return ext.get_text()


# ── Word wrapping helpers for PDF generation ───────────────────────────────

def word_wrap(text, max_chars):
    words = text.split()
    lines = []
    cur = []
    for w in words:
        cur.append(w)
        if len(' '.join(cur)) > max_chars:
            cur.pop()
            lines.append(' '.join(cur))
            cur = [w]
    if cur:
        lines.append(' '.join(cur))
    return lines


def wrap_paragraphs(text, max_chars, paragraphs=None):
    if paragraphs is None:
        paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
    lines = []
    for para in paragraphs:
        wrapped = word_wrap(para, max_chars)
        lines.extend(wrapped)
        lines.append('')
    return lines


# ── Templates ───────────────────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDF ↔ EPUB Converter</title>
<style>
  :root { --bg:#0f1117; --card:#1a1d27; --accent:#7c5cff; --accent2:#00d4aa;
          --text:#e4e6eb; --muted:#888; --err:#ff5757; --toggle-bg:#11131a; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,
        BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; min-height:100vh;
        display:flex; align-items:center; justify-content:center; padding:2rem; }
  .container { max-width:560px; width:100%; }
  h1 { font-size:1.6rem; margin-bottom:.4rem; }
  .subtitle { color:var(--muted); margin-bottom:1.5rem; font-size:.9rem; }
  .mode-toggle { display:flex; border-radius:10px; overflow:hidden;
    border:1px solid #333; margin-bottom:1.5rem; }
  .mode-btn { flex:1; padding:.7rem; border:none; background:var(--toggle-bg);
    color:var(--muted); font-size:.9rem; font-weight:600; cursor:pointer;
    transition:all .2s; }
  .mode-btn.active { background:var(--accent); color:#fff; }
  .mode-btn:not(.active):hover { background:#1e1b2e; }
  .upload-box { background:var(--card); border-radius:16px; padding:2.5rem 2rem;
    text-align:center; border:2px dashed #333; transition:border-color .2s; }
  .upload-box.dragover { border-color:var(--accent); background:#1e1b2e; }
  input[type=file] { display:none; }
  .btn { background:var(--accent); color:#fff; border:none; padding:.8rem 2rem;
    border-radius:10px; font-size:1rem; cursor:pointer; font-weight:600;
    transition:transform .1s, opacity .2s; }
  .btn:hover { transform:translateY(-1px); }
  .btn:disabled { opacity:.4; cursor:default; transform:none; }
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
  <h1>📄 ↔ 📖 PDF & EPUB</h1>
  <p class="subtitle" id="subtitle">Convert scanned PDFs to text EPUBs, or EPUBs to PDFs.</p>

  <div class="mode-toggle">
    <button class="mode-btn active" data-mode="pdf2epub" onclick="setMode('pdf2epub')">PDF → EPUB</button>
    <button class="mode-btn" data-mode="epub2pdf" onclick="setMode('epub2pdf')">EPUB → PDF</button>
  </div>

  <div class="upload-box" id="dropZone">
    <input type="file" id="fileInput" accept=".pdf,application/pdf">
    <p id="dropText">Drag & drop a PDF here</p>
    <p class="or">— or —</p>
    <button class="btn" onclick="document.getElementById('fileInput').click()">
      Choose File
    </button>
    <div class="filename" id="fileName"></div>
  </div>

  <div class="meta-fields" id="metaFields">
    <label>Title</label>
    <input type="text" id="bookTitle" placeholder="Auto-detect">
    <label>Author</label>
    <input type="text" id="bookAuthor" placeholder="Unknown">
    <div class="lang-row" id="ocrSection">
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
        <label>DPI (higher = better, slower)</label>
        <select id="dpi">
          <option value="200">200 (fast)</option>
          <option value="300" selected>300 (balanced)</option>
          <option value="400">400 (high quality)</option>
        </select>
      </div>
    </div>
    <label style="display:flex;align-items:center;gap:.5rem;margin-top:1rem;
                  font-size:.85rem;color:var(--text);cursor:pointer;">
      <input type="checkbox" id="cleanOcr" checked
             style="width:16px;height:16px;accent-color:var(--accent);">
      Clean up OCR artifacts (fix broken lines, remove page numbers, headers)
    </label>
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
let mode = 'pdf2epub';
const dz = document.getElementById('dropZone');
const fi = document.getElementById('fileInput');

function setMode(m) {
  mode = m;
  document.querySelectorAll('.mode-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === m);
  });
  const toEpub = m === 'pdf2epub';
  document.getElementById('dropText').textContent = toEpub
    ? 'Drag & drop a PDF here' : 'Drag & drop an EPUB here';
  fi.accept = toEpub ? '.pdf,application/pdf' : '.epub,application/epub+zip';
  document.getElementById('subtitle').textContent = toEpub
    ? 'Convert scanned PDFs to text EPUBs.' : 'Convert EPUBs to PDFs.';
  document.getElementById('ocrSection').style.display = toEpub ? 'flex' : 'none';
  document.getElementById('convertBtn').textContent = toEpub
    ? 'Convert to EPUB' : 'Convert to PDF';
  // Reset on mode switch
  file = null;
  document.getElementById('fileName').textContent = '';
  document.getElementById('metaFields').classList.remove('active');
  document.getElementById('convertBtn').disabled = true;
  fi.value = '';
}

fi.addEventListener('change', e => { if(e.target.files[0]) selectFile(e.target.files[0]); });
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('dragover');
  if(e.dataTransfer.files[0]) selectFile(e.dataTransfer.files[0]);
});

function selectFile(f) {
  file = f;
  const ext = f.name.split('.').pop().toLowerCase();
  if ((mode === 'pdf2epub' && ext !== 'pdf') || (mode === 'epub2pdf' && ext !== 'epub')) {
    document.getElementById('statusText').innerHTML =
      '<span class="error">Please select a ' + (mode==='pdf2epub'?'PDF':'EPUB') + ' file.</span>';
    document.getElementById('fileName').textContent = '';
    document.getElementById('convertBtn').disabled = true;
    return;
  }
  document.getElementById('fileName').textContent = '\u2713 ' + f.name;
  document.getElementById('metaFields').classList.add('active');
  const base = f.name.replace(/\\.[^/.]+$/, '').replace(/[_-]+/g,' ').trim();
  document.getElementById('bookTitle').placeholder = base;
  document.getElementById('convertBtn').disabled = false;
}

let pollTimer = null;
async function convert() {
  if(!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('mode', mode);
  fd.append('title', document.getElementById('bookTitle').value || '');
  fd.append('author', document.getElementById('bookAuthor').value || '');
  fd.append('lang', document.getElementById('ocrLang').value);
  fd.append('dpi', document.getElementById('dpi').value);
  fd.append('clean_ocr', document.getElementById('cleanOcr').checked ? '1' : '0');

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
        '<span class="success">\u2713 Done! Download starting...</span>';
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


# ── PDF → EPUB ──────────────────────────────────────────────────────────────

def convert_pdf_to_epub(job_id, pdf_path, title, author, lang, dpi, clean_ocr=False):
    try:
        update_job(job_id, status="Opening PDF...", pct=5)

        doc = fitz.open(pdf_path)
        total = doc.page_count
        update_job(job_id, status=f"Analyzing {total} pages...", pct=10)

        if not title:
            meta = doc.metadata
            title = meta.get("title") or Path(pdf_path).stem.replace("_", " ").title()
        if not author:
            meta = doc.metadata
            author = meta.get("author") or "Unknown"

        pages_text = []
        needs_ocr = False
        for i in range(total):
            text = doc.load_page(i).get_text("text").strip()
            pages_text.append(text)
            if i < 5 and len(text) < 50:
                needs_ocr = True

        if needs_ocr:
            needs_ocr = any(len(t) < 20 for t in pages_text)

        doc.close()

        if needs_ocr:
            update_job(job_id, status="Rendering pages to images...", pct=15)
            images = convert_from_path(pdf_path, dpi=int(dpi), fmt="png")
            update_job(job_id, status="Running OCR (this may take a while)...", pct=25)

            for i, img in enumerate(images):
                text = pytesseract.image_to_string(img, lang=lang)
                pages_text[i] = text.strip()
                pct = 25 + int((i + 1) / total * 65)
                update_job(job_id, status=f"OCR page {i + 1} of {total}...",
                           pct=min(pct, 90))
        else:
            update_job(job_id, status="Extracting text layer...", pct=90)

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

        if clean_ocr:
            # ── Run the OCR cleaner & use detected structure ──
            update_job(job_id, status="Cleaning OCR text...", pct=92)
            full_text = "\n".join(pages_text)
            full_text = clean_ocr_text(full_text)
            structure = extract_structure(full_text)

            total_ch = len(structure) or 1
            for idx, sec in enumerate(structure):
                heading = sec["heading"]
                body = sec["body"]
                paras = body.split("\n\n")
                body_html = "".join(
                    f"<p>{p.strip()}</p>" for p in paras if p.strip()
                )
                ch_html = (
                    "<html><head><style>p{text-indent:1.2em;"
                    "line-height:1.6;margin:.4em 0}"
                    "h2{text-align:center}</style></head><body>"
                    f"<h2>{heading}</h2>{body_html}</body></html>"
                )
                ch_file = f"chap_{idx + 1:04d}.xhtml"
                ch = epub.EpubHtml(title=heading, file_name=ch_file,
                                   content=ch_html)
                book.add_item(ch)
                chapters.append(ch)
                spine.append(ch)

                pct = 93 + int((idx + 1) / total_ch * 6)
                update_job(job_id, status=f"Building chapter {idx + 1} of {total_ch}...",
                           pct=min(pct, 99))
        else:
            # ── Original: group every N pages ──
            pages_per_chapter = 10
            total_grps = max((total + pages_per_chapter - 1) // pages_per_chapter, 1)
            for idx, start in enumerate(range(0, total, pages_per_chapter)):
                chunk = pages_text[start:start + pages_per_chapter]
                end_page = min(start + len(chunk), total)
                ch_title = f"Pages {start + 1}–{end_page}"

                body_parts = []
                for pt in chunk:
                    pt = re.sub(r"\n{3,}", "\n\n", pt.strip())
                    if pt:
                        body_parts.append(f"<p>{pt}</p>")

                ch_html = (
                    "<html><head><style>p{margin-bottom:1em}</style>"
                    "</head><body>"
                    + "\n".join(body_parts)
                    + "</body></html>"
                )

                ch_file = f"chap_{idx + 1:04d}.xhtml"
                ch = epub.EpubHtml(title=ch_title, file_name=ch_file,
                                   content=ch_html)
                book.add_item(ch)
                chapters.append(ch)
                spine.append(ch)

                pct = 93 + int((idx + 1) / total_grps * 6)
                update_job(job_id, status=f"Building chapter {idx + 1} of {total_grps}...",
                           pct=min(pct, 99))

        book.spine = spine
        book.toc = chapters

        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        out_path = os.path.join(tempfile.gettempdir(), f"{job_id}.epub")
        epub.write_epub(out_path, book, {})

        try:
            os.unlink(pdf_path)
        except Exception:
            pass

        update_job(job_id, status="Done!", pct=100, done=True, out_path=out_path,
                   out_name=f"{title}.epub")

    except Exception as e:
        traceback.print_exc()
        update_job(job_id, error=str(e), status=f"Error: {e}", done=True)


# ── EPUB → PDF ──────────────────────────────────────────────────────────────

def convert_epub_to_pdf(job_id, epub_path):
    try:
        update_job(job_id, status="Opening EPUB...", pct=5)

        book = epub.read_epub(epub_path)

        title_meta = book.get_metadata("DC", "title")
        title = title_meta[0][0] if title_meta else Path(epub_path).stem
        author_meta = book.get_metadata("DC", "creator")
        author = author_meta[0][0] if author_meta else "Unknown"

        # Collect chapters in spine order, skip nav/toc
        chapters = []
        for item in book.get_items():
            if isinstance(item, epub.EpubHtml) and item.file_name not in (
                "nav.xhtml", "toc.ncx"
            ):
                chapters.append(item)

        total = len(chapters)
        if total == 0:
            raise ValueError("No readable content found in EPUB")

        update_job(job_id, status=f"Processing {total} chapters...", pct=10)

        # PDF layout
        margin = 72
        font_size = 11
        line_h = 15
        page_w, page_h = 612, 792
        text_w = page_w - 2 * margin
        max_chars = int(text_w / (font_size * 0.55))

        doc = fitz.open()

        for i, ch in enumerate(chapters):
            html = ch.get_content().decode("utf-8", errors="replace")
            text = strip_html(html)
            lines = wrap_paragraphs(text, max_chars)

            pct = 10 + int((i + 1) / total * 82)
            update_job(job_id, status=f"Chapter {i + 1} of {total}...",
                       pct=min(pct, 92))

            page = doc.new_page()
            y = margin + 10

            ch_title = ch.title or f"Chapter {i + 1}"
            page.insert_text(
                fitz.Point(margin, y), ch_title,
                fontname="Helvetica-Bold", fontsize=14,
            )
            y += 24

            for line in lines:
                if y + line_h > page_h - margin:
                    page = doc.new_page()
                    y = margin
                page.insert_text(
                    fitz.Point(margin, y), line,
                    fontname="Helvetica", fontsize=font_size,
                )
                y += line_h

        update_job(job_id, status="Saving PDF...", pct=95)

        out_path = os.path.join(tempfile.gettempdir(), f"{job_id}.pdf")
        doc.save(out_path)
        doc.close()

        try:
            os.unlink(epub_path)
        except Exception:
            pass

        update_job(job_id, status="Done!", pct=100, done=True, out_path=out_path,
                   out_name=f"{title}.pdf")

    except Exception as e:
        traceback.print_exc()
        update_job(job_id, error=str(e), status=f"Error: {e}", done=True)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/convert", methods=["POST"])
def convert():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    job_id = uuid.uuid4().hex[:12]
    mode = request.form.get("mode", "pdf2epub")
    ext = "pdf" if mode == "epub2pdf" else "epub"
    in_path = os.path.join(tempfile.gettempdir(), f"{job_id}.{mode.split('2')[0]}")
    f.save(in_path)

    JOBS[job_id] = {"pct": 0, "status": "Queued...", "done": False}

    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()
    lang = request.form.get("lang", "eng")
    dpi = request.form.get("dpi", "300")
    clean_ocr = request.form.get("clean_ocr", "0") == "1"

    if mode == "epub2pdf":
        target = convert_epub_to_pdf
        args = (job_id, in_path)
    else:
        target = convert_pdf_to_epub
        args = (job_id, in_path, title, author, lang, dpi, clean_ocr)

    t = threading.Thread(target=target, args=args, daemon=True)
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

"""
app.py — Superfirma
====================
Flask web application for FPE exam submission management.

Routes:
  GET/POST /            — Student submission form
  GET      /success     — Confirmation after successful submission
  GET/POST /admin/login — Professor login
  GET      /admin/logout
  GET      /admin       — Dashboard (protected)
  GET      /admin/download/<id>  — Individual docx download
  GET      /admin/download-zip   — All submissions as .zip
  GET/POST /admin/debug-docx     — OOXML table inspector (dev tool)
"""

import io
import os
import uuid
import zipfile
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, send_file, jsonify, flash, abort
)
from werkzeug.utils import secure_filename

from image_processor import process_firma, process_nombre
from doc_builder import insert_images_into_docx

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'superfirma-dev-secret-2026-change-in-prod')

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
PROCESSED_DIR = os.path.join(BASE_DIR, 'processed')
TEMP_DIR      = os.path.join(BASE_DIR, 'temp')
UPLOADS_DIR   = os.path.join(BASE_DIR, 'uploads')
DB_PATH       = os.path.join(BASE_DIR, 'superfirma.db')

ALLOWED_DOC = {'docx'}
ALLOWED_IMG = {'jpg', 'jpeg', 'png', 'webp', 'bmp', 'heic', 'heif'}

ADMIN_USER = os.environ.get('ADMIN_USER', 'profesor')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'profesor123')

MAX_CONTENT_MB  = 50
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_MB * 1024 * 1024


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS entregas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                alumno      TEXT    NOT NULL,
                filename    TEXT    NOT NULL,
                filepath    TEXT    NOT NULL,
                fecha       TEXT    NOT NULL
            )
        ''')
        conn.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def allowed(filename: str, allowed_set: set) -> bool:
    return (
        '.' in filename and
        filename.rsplit('.', 1)[1].lower() in allowed_set
    )


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


# ── Routes: Alumno ────────────────────────────────────────────────────────────

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        errors = []

        alumno_name = request.form.get('alumno', '').strip()
        examen_file = request.files.get('examen')
        firma_file  = request.files.get('firma')
        nombre_file = request.files.get('nombre')

        # Validation
        if not alumno_name:
            errors.append('Introduce tu nombre completo.')
        if not examen_file or not allowed(examen_file.filename, ALLOWED_DOC):
            errors.append('Sube un archivo de examen válido (.docx).')
        if not firma_file or not allowed(firma_file.filename, ALLOWED_IMG):
            errors.append('Sube una foto de tu firma válida (jpg/png/webp).')
        if not nombre_file or not allowed(nombre_file.filename, ALLOWED_IMG):
            errors.append('Sube una foto de tu nombre y apellidos válida (jpg/png/webp).')

        if errors:
            return render_template('index.html', errors=errors,
                                   alumno_name=alumno_name)

        try:
            docx_bytes   = examen_file.read()
            firma_bytes  = firma_file.read()
            nombre_bytes = nombre_file.read()

            app.logger.info(f"[PIPELINE] firma input:  {len(firma_bytes)} bytes")
            app.logger.info(f"[PIPELINE] nombre input: {len(nombre_bytes)} bytes")

            # Image processing pipeline
            firma_png  = process_firma(firma_bytes)
            nombre_png = process_nombre(nombre_bytes)

            app.logger.info(f"[PIPELINE] firma_png:  {len(firma_png)} bytes")
            app.logger.info(f"[PIPELINE] nombre_png: {len(nombre_png)} bytes")

            # Verify the processed images actually contain ink
            import numpy as np
            from PIL import Image as _PIL
            _f = _PIL.open(io.BytesIO(firma_png)).convert('RGBA')
            _n = _PIL.open(io.BytesIO(nombre_png)).convert('RGBA')
            _f_ink = int(np.sum(np.array(_f.split()[3]) > 20))
            _n_ink = int(np.sum(np.array(_n.split()[3]) > 20))
            app.logger.info(f"[PIPELINE] firma  ink px: {_f_ink}  size: {_f.size}")
            app.logger.info(f"[PIPELINE] nombre ink px: {_n_ink}  size: {_n.size}")

            if _n_ink < 50:
                app.logger.warning("[PIPELINE] nombre_png tiene muy poca tinta — pipeline produjo imagen casi vacia")

            # Document assembly
            result_bytes = insert_images_into_docx(docx_bytes, firma_png, nombre_png)

            # Persist
            uid          = uuid.uuid4().hex[:10]
            safe_exam    = secure_filename(examen_file.filename).rsplit('.', 1)[0]
            out_filename = f'{safe_exam}_FIRMADO_{uid}.docx'
            out_path     = os.path.join(PROCESSED_DIR, out_filename)

            with open(out_path, 'wb') as fh:
                fh.write(result_bytes)

            with get_db() as conn:
                conn.execute(
                    'INSERT INTO entregas (alumno, filename, filepath, fecha) '
                    'VALUES (?, ?, ?, ?)',
                    (alumno_name, out_filename, out_path,
                     datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                )
                conn.commit()

            session['last_filename'] = out_filename
            return redirect(url_for('success'))

        except ValueError as e:
            return render_template('index.html', errors=[str(e)],
                                   alumno_name=alumno_name)
        except Exception as e:
            app.logger.exception('Processing error')
            return render_template('index.html',
                                   errors=[f'Error interno al procesar: {e}'],
                                   alumno_name=alumno_name)

    return render_template('index.html')


@app.route('/success')
def success():
    filename = session.pop('last_filename', None)
    if not filename:
        return redirect(url_for('index'))
    return render_template('success.html', filename=filename)


# ── Routes: Admin ─────────────────────────────────────────────────────────────

@app.route('/preview', methods=['GET', 'POST'])
def preview():
    """
    Debug endpoint: upload an image, choose pipeline (firma/nombre),
    get back the processed PNG composited on white for visual inspection.
    Accessible without login — remove in production.
    """
    if request.method == 'GET':
        return '''
        <h2 style="font-family:sans-serif">Preview pipeline</h2>
        <form method="post" enctype="multipart/form-data" style="font-family:sans-serif">
          <p>Imagen: <input type="file" name="img" accept="image/*" required></p>
          <p>Pipeline:
            <select name="pipeline">
              <option value="firma">process_firma</option>
              <option value="nombre">process_nombre</option>
            </select>
          </p>
          <button type="submit">Procesar</button>
        </form>
        '''
    f = request.files.get('img')
    if not f:
        return 'No image', 400

    data     = f.read()
    pipeline = request.form.get('pipeline', 'nombre')

    if pipeline == 'firma':
        result_png = process_firma(data)
    else:
        result_png = process_nombre(data)

    import numpy as np
    from PIL import Image as _PIL
    result_img = _PIL.open(io.BytesIO(result_png)).convert('RGBA')
    ink_px = int(np.sum(np.array(result_img.split()[3]) > 20))

    bg = _PIL.new('RGB', result_img.size, (255, 255, 255))
    bg.paste(result_img, mask=result_img.split()[3])

    out = io.BytesIO()
    bg.save(out, format='PNG')
    out.seek(0)

    app.logger.info(f"[PREVIEW] pipeline={pipeline} size={result_img.size} ink_px={ink_px}")
    return send_file(out, mimetype='image/png',
                     headers={'X-Ink-Pixels': str(ink_px),
                              'X-Image-Size': str(result_img.size)})


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin'):
        return redirect(url_for('admin_dashboard'))
    error = None
    if request.method == 'POST':
        if (request.form.get('usuario') == ADMIN_USER and
                request.form.get('password') == ADMIN_PASS):
            session['admin'] = True
            return redirect(url_for('admin_dashboard'))
        error = 'Credenciales incorrectas.'
    return render_template('login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/admin')
@login_required
def admin_dashboard():
    with get_db() as conn:
        entregas = conn.execute(
            'SELECT * FROM entregas ORDER BY fecha DESC'
        ).fetchall()
    return render_template('admin.html', entregas=entregas)


@app.route('/admin/download/<int:entrega_id>')
@login_required
def admin_download(entrega_id):
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM entregas WHERE id=?', (entrega_id,)
        ).fetchone()
    if not row:
        abort(404)
    if not os.path.exists(row['filepath']):
        abort(410)   # Gone — file was deleted
    return send_file(row['filepath'], as_attachment=True,
                     download_name=row['filename'])


@app.route('/admin/download-zip')
@login_required
def admin_download_zip():
    with get_db() as conn:
        entregas = conn.execute('SELECT * FROM entregas').fetchall()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for row in entregas:
            if os.path.exists(row['filepath']):
                zf.write(row['filepath'], row['filename'])
    buf.seek(0)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return send_file(buf, as_attachment=True,
                     download_name=f'entregas_{ts}.zip',
                     mimetype='application/zip')


# ── Dev tool: OOXML table inspector ──────────────────────────────────────────

@app.route('/admin/debug-docx', methods=['GET', 'POST'])
@login_required
def debug_docx():
    if request.method == 'GET':
        return render_template('debug_docx.html')

    f = request.files.get('docx')
    if not f or not allowed(f.filename, ALLOWED_DOC):
        return jsonify({'error': 'Sube un .docx válido'}), 400

    from docx import Document
    doc    = Document(io.BytesIO(f.read()))
    result = []
    for t_idx, tbl in enumerate(doc.tables):
        rows = []
        for r_idx, row in enumerate(tbl.rows):
            seen, cells = set(), []
            for c_idx, cell in enumerate(row.cells):
                key    = id(cell._tc)
                merged = key in seen
                seen.add(key)
                cells.append({
                    'col': c_idx,
                    'merged': merged,
                    'text': cell.text[:120].replace('\n', ' | ')
                })
            rows.append({'row': r_idx, 'cells': cells})
        result.append({'table': t_idx, 'rows': rows})
    return jsonify(result)


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(413)
def too_large(e):
    return render_template('index.html',
                           errors=[f'Archivo demasiado grande. Máximo {MAX_CONTENT_MB} MB.']), 413


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


# ── Bootstrap ─────────────────────────────────────────────────────────────────
# Runs on every startup (gunicorn import + direct python app.py)

os.makedirs(PROCESSED_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
init_db()


@app.context_processor
def inject_now():
    return {'now': datetime.now()}


if __name__ == '__main__':
    app.run(debug=False, port=5001)

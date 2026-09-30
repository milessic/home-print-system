#!/usr/bin/env python3
"""
main.py - Home printing system backend.

Run with:
    python3 main.py

Environment variables (also read from a .env file next to this script;
real environment variables take precedence):
    PRINTER_NAME   CUPS queue name to print to (required, see SETUP.md)
    HOST           Bind address (default: 0.0.0.0)
    PORT           Bind port (default: 5000)
    HOME_URL       Target of the "Home" button in the top bar (optional;
                   the button is hidden when unset)
    STYLES_URL     Origin of the milessic-themes server, e.g.
                   http://mbs.local:9312 (optional; the page falls back to
                   unstyled-but-working when unset or unreachable)

Requires a printer already added to CUPS (see SETUP.md) and the following
pip packages: flask, pypdf, Pillow, pycups.
"""

import base64
import html
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, send_file, abort, Response

from users import verify_password

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str) -> None:
    """Minimal .env reader (KEY=VALUE lines, # comments, optional quotes).
    Variables already set in the real environment are left untouched."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv(os.path.join(BASE_DIR, ".env"))

UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
PROCESSED_FOLDER = os.path.join(BASE_DIR, "uploads", "processed")
META_FILE = os.path.join(BASE_DIR, "uploads_meta.json")

PRINTER_NAME = os.environ.get("PRINTER_NAME")
if not PRINTER_NAME:
    raise SystemExit(
        "PRINTER_NAME environment variable must be set to your CUPS queue "
        "name (see SETUP.md)."
    )

HOME_URL = os.environ.get("HOME_URL", "").strip()

# milessic-themes (see the manifesto on the themes server). Version is pinned
# so browsers can cache the bundle; bump it deliberately after checking the
# gallery. The page starts on STYLES_THEME; users switch themes in the UI
# (themes.js stores their choice in the browser's localStorage).
STYLES_URL = os.environ.get("STYLES_URL", "").strip().rstrip("/")
STYLES_THEME = "system"
STYLES_VERSION = "1.0.0"

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".txt"}
MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 64 MB

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


# ---------------------------------------------------------------------------
# Small JSON-backed metadata store for uploaded files
# ---------------------------------------------------------------------------

def _load_meta() -> dict:
    if not os.path.exists(META_FILE):
        return {}
    try:
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_meta(meta: dict) -> None:
    tmp = META_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, META_FILE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Auth: credentials are sent with every request (HTTP Basic Auth), no tokens.
# The browser-side UI stores base64(username:password) in localStorage and
# attaches it as an Authorization header (or ?auth= query param for the
# <img>/<iframe> preview requests, which can't set custom headers).
# ---------------------------------------------------------------------------

def _get_request_credentials():
    auth = request.authorization
    if auth and auth.username is not None:
        return auth.username, auth.password

    token = request.args.get("auth")
    if token:
        try:
            decoded = base64.b64decode(token).decode("utf-8")
            username, _, password = decoded.partition(":")
            return username, password
        except Exception:
            return None, None

    return None, None


def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        username, password = _get_request_credentials()
        if not username or not verify_password(username, password):
            return jsonify({"error": "Unauthorized"}), 401
        request.auth_username = username
        return f(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Filesystem error handling
#
# Running as a systemd service (see SETUP.md) means main.py runs as whatever
# `User=` the unit specifies, not the user who checked out/copied the code.
# If that user doesn't own uploads/, uploads/processed/ or uploads_meta.json,
# every file write below raises PermissionError. Catch it here so we log a
# diagnostic pointing at SETUP.md instead of leaking a stack trace, and so
# the client always gets a clean JSON 500 to key off of.
# ---------------------------------------------------------------------------

def _handle_fs_error(exc: OSError, context: str):
    app.logger.error(
        "%s failed due to a filesystem error: %s. If this is a permission "
        "error, check that the service user owns %r, %r and %r - see the "
        "'Permission denied' section in SETUP.md.",
        context, exc, UPLOAD_FOLDER, PROCESSED_FOLDER, META_FILE,
    )
    return jsonify({"error": "Internal server error"}), 500


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        return exc
    app.logger.exception("Unhandled exception")
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Routes: app shell + auth check
# ---------------------------------------------------------------------------

def _theme_head_tags() -> str:
    """Theme bundle + the themes.js helper (theme picker, restores the
    user's stored theme). The helper is loaded without defer on purpose so
    it re-applies the stored theme as early as possible."""
    if not STYLES_URL:
        return ""
    css = f"{STYLES_URL}/css/bundle/{STYLES_THEME}.css?v={STYLES_VERSION}"
    js = f"{STYLES_URL}/js/themes.js?v={STYLES_VERSION}"
    return (
        f'<link rel="stylesheet" data-milessic-theme href="{html.escape(css)}">\n'
        f'<script src="{html.escape(js)}"></script>'
    )


@app.route("/")
def index():
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        page = f.read()
    page = page.replace("{{THEME_HEAD}}", _theme_head_tags())
    page = page.replace("{{HOME_URL}}", html.escape(HOME_URL))
    return Response(page, mimetype="text/html")


@app.route("/api/login", methods=["POST"])
@requires_auth
def login():
    return jsonify({"status": "ok", "username": request.auth_username})


# ---------------------------------------------------------------------------
# Routes: upload + preview
# ---------------------------------------------------------------------------

def _safe_ext(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


def _record_path(record: dict) -> str:
    """Where a record's file currently lives: uploads/ until it's been
    printed once, then uploads/processed/ (see print_file, which moves it
    there after a successful print)."""
    folder = PROCESSED_FOLDER if record.get("processed") else UPLOAD_FOLDER
    return os.path.join(folder, record["stored_name"])


@app.route("/api/upload", methods=["POST"])
@requires_auth
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    ext = _safe_ext(file.filename)
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type '{ext}'"}), 400

    file_id = uuid.uuid4().hex
    stored_name = f"{file_id}{ext}"
    path = os.path.join(UPLOAD_FOLDER, stored_name)
    try:
        file.save(path)
    except OSError as exc:
        return _handle_fs_error(exc, "Saving uploaded file")

    meta = _load_meta()
    meta[file_id] = {
        "original_name": file.filename,
        "stored_name": stored_name,
        "ext": ext,
        "owner": request.auth_username,
        "hide": False,
        "processed": False,
        "created_at": _now_iso(),
        "processed_at": None,
    }
    try:
        _save_meta(meta)
    except OSError as exc:
        return _handle_fs_error(exc, "Saving upload metadata")

    return jsonify({"id": file_id, "filename": file.filename, "type": ext.lstrip(".")})


@app.route("/api/file/<file_id>")
@requires_auth
def get_file(file_id):
    meta = _load_meta()
    record = meta.get(file_id)
    if not record:
        abort(404)
    path = _record_path(record)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, download_name=record["original_name"])


@app.route("/api/files")
@requires_auth
def list_files():
    meta = _load_meta()
    mine = [
        {
            "id": fid,
            "filename": rec["original_name"],
            "original_name": rec["original_name"],
            "stored_name": rec["stored_name"],
            "type": rec["ext"].lstrip("."),
            "processed": rec.get("processed", False),
            "created_at": rec.get("created_at"),
            "processed_at": rec.get("processed_at"),
        }
        for fid, rec in meta.items()
        if rec.get("owner") == request.auth_username and not rec.get("hide", False)
    ]
    return jsonify(mine)


@app.route("/api/file/<file_id>/hide", methods=["POST"])
@requires_auth
def hide_file(file_id):
    meta = _load_meta()
    record = meta.get(file_id)
    if not record or record.get("owner") != request.auth_username:
        abort(404)
    record["hide"] = True
    try:
        _save_meta(meta)
    except OSError as exc:
        return _handle_fs_error(exc, "Hiding file")
    return jsonify({"status": "ok"})


@app.route("/api/file/<file_id>", methods=["DELETE"])
@requires_auth
def delete_file(file_id):
    meta = _load_meta()
    record = meta.get(file_id)
    if not record or record.get("owner") != request.auth_username:
        abort(404)
    path = _record_path(record)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return _handle_fs_error(exc, "Deleting file")

    del meta[file_id]
    try:
        _save_meta(meta)
    except OSError as exc:
        return _handle_fs_error(exc, "Saving metadata after delete")
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Rotation helpers
# ---------------------------------------------------------------------------

def _rotate_pdf(src_path: str, dst_path: str, rotation: int) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(src_path)
    writer = PdfWriter()
    for page in reader.pages:
        if rotation:
            page.rotate(rotation)
        writer.add_page(page)
    with open(dst_path, "wb") as f:
        writer.write(f)


def _rotate_image(src_path: str, dst_path: str, rotation: int) -> None:
    from PIL import Image

    with Image.open(src_path) as img:
        if rotation:
            # PIL rotates counter-clockwise for positive angles; our UI
            # rotation values are clockwise, so negate.
            img = img.rotate(-rotation, expand=True)
        # JPEG has no alpha channel - saving an RGBA image (which rotate()
        # can produce) as .jpg silently corrupts/blanks the output on some
        # Pillow/libjpeg builds, so flatten to RGB first for jpg targets.
        if dst_path.lower().endswith((".jpg", ".jpeg")) and img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        img.save(dst_path)


def _cleanup_temp_file(path: str) -> None:
    """Best-effort removal of a disposable rotated-copy file produced by
    _prepare_print_file. Never fails the request over this - it's just
    tidying up uploads/processed/."""
    try:
        os.remove(path)
    except OSError as exc:
        app.logger.warning("Could not remove temporary print file %s: %s", path, exc)


def _prepare_print_file(record: dict, rotation: int) -> str:
    """Return path to the file that should actually be sent to the printer,
    applying rotation for PDFs and images. Other types are printed as-is.
    The returned path may be a throwaway rotated copy in PROCESSED_FOLDER
    (caller is responsible for cleaning that up) - it is never the
    canonical copy that print_file() moves into PROCESSED_FOLDER on
    success."""
    src_path = _record_path(record)
    rotation = rotation % 360

    if rotation == 0 or record["ext"] not in (".pdf", ".png", ".jpg", ".jpeg"):
        return src_path

    out_name = f"{uuid.uuid4().hex}{record['ext']}"
    out_path = os.path.join(PROCESSED_FOLDER, out_name)

    if record["ext"] == ".pdf":
        _rotate_pdf(src_path, out_path, rotation)
    else:
        _rotate_image(src_path, out_path, rotation)

    return out_path


# ---------------------------------------------------------------------------
# Routes: print
# ---------------------------------------------------------------------------

PAPER_SIZES = {"A4", "Letter", "Legal"}


@app.route("/api/print", methods=["POST"])
@requires_auth
def print_file():
    data = request.get_json(silent=True) or {}
    file_id = data.get("id")
    rotation = int(data.get("rotation", 0)) % 360
    paper = data.get("paper", "A4")
    copies = max(1, min(50, int(data.get("copies", 1))))

    if paper not in PAPER_SIZES:
        paper = "A4"

    meta = _load_meta()
    record = meta.get(file_id)
    if not record:
        return jsonify({"error": "Unknown file id"}), 404

    src_path = _record_path(record)  # where the canonical file lives before this print

    try:
        print_path = _prepare_print_file(record, rotation)
    except OSError as exc:  # e.g. permission denied writing to PROCESSED_FOLDER
        return _handle_fs_error(exc, "Preparing file for print")
    except Exception as exc:  # rotation/library failure
        app.logger.exception("Failed to prepare file for print")
        return jsonify({"error": f"Failed to prepare file: {exc}"}), 500

    # A rotated print_path is a disposable temp copy (see _prepare_print_file) -
    # print_path == src_path only when no rotation was applied.
    is_temp_copy = print_path != src_path

    cmd = [
        "lp",
        "-d", PRINTER_NAME,
        "-n", str(copies),
        "-o", f"media={paper}",
        # NOTE: intentionally NOT passing "-o fit-to-page" here. That option
        # only applies to the legacy pstops filter chain; on driverless /
        # IPP-Everywhere queues (the common way to add a WiFi printer to
        # CUPS today) it is unsupported and has been observed to make the
        # filter chain emit a blank page instead of erroring. The modern,
        # widely-supported equivalent is print-scaling.
        "-o", "print-scaling=auto",
        print_path,
    ]

    app.logger.info("Print command: %s", " ".join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        if is_temp_copy:
            _cleanup_temp_file(print_path)
        return jsonify({"error": "'lp' command not found. Is CUPS installed?"}), 500
    except subprocess.TimeoutExpired:
        if is_temp_copy:
            _cleanup_temp_file(print_path)
        return jsonify({"error": "Timed out sending job to printer"}), 504

    if is_temp_copy:
        _cleanup_temp_file(print_path)

    app.logger.info("lp exit=%s stdout=%r stderr=%r", result.returncode, result.stdout, result.stderr)

    if result.returncode != 0:
        return jsonify({
            "error": "Print command failed",
            "detail": (result.stderr or result.stdout).strip(),
        }), 502

    # Move the file into uploads/processed/ on its first successful print.
    # Bug fix: this move never used to happen - "processed" was tracked
    # only as a metadata flag, so files just sat in uploads/ forever
    # regardless of print status.
    if not record.get("processed"):
        new_path = os.path.join(PROCESSED_FOLDER, record["stored_name"])
        try:
            os.replace(src_path, new_path)
        except OSError as exc:
            # The job was already submitted to CUPS at this point - the print
            # itself succeeded, only the archival move failed (e.g. permission
            # denied on uploads/processed/). Report it as a failure anyway so
            # it's visible and can be retried/investigated, since otherwise
            # the file would be silently stuck in uploads/ while marked
            # "not printed" was already true from the client's perspective.
            return _handle_fs_error(exc, "Moving printed file to processed folder")

    record["processed"] = True
    record["processed_at"] = _now_iso()
    meta[file_id] = record
    try:
        _save_meta(meta)
    except OSError as exc:
        # The job was already submitted to CUPS successfully at this point,
        # so don't fail the request over a metadata write - just log it.
        app.logger.error("Failed to save metadata after print (job already submitted): %s", exc)

    job_id = result.stdout.strip()
    return jsonify({"status": "submitted", "job": job_id})


# ---------------------------------------------------------------------------
# Routes: printer status (green / yellow / red)
# ---------------------------------------------------------------------------

# Reasons that are informational only and should not downgrade status.
_BENIGN_REASONS = {"none"}


def _normalize_reasons(raw) -> list:
    """printer-state-reasons should be a list of strings, but be defensive:
    pycups/IPP can hand back a single comma-joined string depending on
    version, and iterating a bare string char-by-char would silently
    corrupt the message instead of failing loudly."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(r).strip() for r in raw if str(r).strip() and str(r).strip().lower() not in _BENIGN_REASONS]


def _printer_status_via_pycups() -> dict:
    import cups  # may raise ImportError, or OSError if libcups.so is missing

    conn = cups.Connection()
    printers = conn.getPrinters()

    printer = printers.get(PRINTER_NAME)
    if not printer:
        return {"color": "red", "message": f"Printer '{PRINTER_NAME}' not found in CUPS"}

    state = printer.get("printer-state")  # 3=idle, 4=processing, 5=stopped
    reasons = _normalize_reasons(printer.get("printer-state-reasons"))
    is_accepting = printer.get("printer-is-accepting-jobs", True)

    if state == 5 or not is_accepting:
        return {
            "color": "red",
            "message": "Printer stopped: " + (", ".join(reasons) if reasons else "unknown reason"),
        }

    if reasons:
        return {"color": "yellow", "message": ", ".join(reasons)}

    if state in (3, 4):
        return {"color": "green", "message": "Ready" if state == 3 else "Printing"}

    return {"color": "yellow", "message": f"Unknown state ({state})"}


def _printer_status_via_lpstat() -> dict:
    """Fallback that only needs the 'lpstat' CLI (part of cups-client),
    used when the pycups python binding isn't installed/working. This
    keeps the status indicator functional even if pycups failed to build."""
    try:
        result = subprocess.run(
            ["lpstat", "-p", PRINTER_NAME, "-l"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return {"color": "red", "message": "'lpstat' not found - is CUPS installed?"}
    except subprocess.TimeoutExpired:
        return {"color": "red", "message": "Timed out talking to CUPS"}

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return {"color": "red", "message": detail or f"Printer '{PRINTER_NAME}' not found"}

    output = result.stdout
    lines = output.splitlines()
    first_line = lines[0].strip() if lines else ""

    reasons = []
    for line in lines:
        line = line.strip()
        if line.lower().startswith("alerts:"):
            reasons = _normalize_reasons(line.split(":", 1)[1])

    if "disabled" in first_line.lower():
        return {"color": "red", "message": first_line or "Printer disabled/stopped"}

    if reasons:
        return {"color": "yellow", "message": ", ".join(reasons)}

    if "printing" in first_line.lower():
        return {"color": "green", "message": "Printing"}
    if "idle" in first_line.lower():
        return {"color": "green", "message": "Ready"}

    return {"color": "yellow", "message": first_line or "Unknown status"}


def _printer_status() -> dict:
    try:
        return _printer_status_via_pycups()
    except Exception as exc:
        app.logger.info("pycups unavailable/failed (%s), falling back to lpstat", exc)
        return _printer_status_via_lpstat()


@app.route("/api/printer-status")
@requires_auth
def printer_status():
    return jsonify(_printer_status())


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=False)

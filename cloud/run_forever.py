"""
Fly.io entrypoint: runs the capture script in a restart loop and
serves /data over HTTP with a small web UI — including a "Capture"
button that gracefully stops the active session, finalizes that
file, and immediately starts a new one, handing back a zip of the
just-finished session directly.

Built for a LEGACY FREE-ALLOWANCE Fly.io org: 1 shared-cpu-1x VM,
a 3GB persistent volume, with automatic disk-budget cleanup.
IMPORTANT: cleanup deletes the OLDEST files first once the budget
is exceeded, regardless of whether they've been downloaded — pull
data down regularly.
"""
import html
import http.server
import io
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import zipfile
from urllib.parse import urlparse, parse_qs

DATA_DIR = os.environ.get("CAPTURE_OUTPUT_DIR", "/data")
PORT = int(os.environ.get("PORT", "8080"))
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
RESTART_AFTER_HOURS = float(os.environ.get("RESTART_AFTER_HOURS", "6"))
MAX_DATA_BYTES = int(os.environ.get("MAX_DATA_BYTES", str(2 * 1024**3)))
CLEANUP_CHECK_INTERVAL = 300

os.makedirs(DATA_DIR, exist_ok=True)

# Shared state between the HTTP handler thread(s) and the supervisor
# thread that owns the actual capture subprocess.
proc_lock = threading.Lock()
current_proc = None  # the live subprocess.Popen, or None between restarts


def dir_size_bytes(path):
    total = 0
    for f in os.listdir(path):
        fp = os.path.join(path, f)
        if os.path.isfile(fp):
            total += os.path.getsize(fp)
    return total


def enforce_disk_budget():
    while True:
        try:
            size = dir_size_bytes(DATA_DIR)
            if size > MAX_DATA_BYTES:
                files = [os.path.join(DATA_DIR, f) for f in os.listdir(DATA_DIR)
                         if os.path.isfile(os.path.join(DATA_DIR, f))]
                files.sort(key=os.path.getmtime)
                while size > MAX_DATA_BYTES and files:
                    victim = files.pop(0)
                    freed = os.path.getsize(victim)
                    os.remove(victim)
                    size -= freed
                    print(f"[cleanup] removed {victim} ({freed/1e6:.1f}MB)", flush=True)
        except Exception as e:
            print(f"[cleanup] error: {e}", flush=True)
        time.sleep(CLEANUP_CHECK_INTERVAL)


def zip_bytes_for(fname):
    target = os.path.join(DATA_DIR, fname)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(target, arcname=fname)
    return buf.getvalue()


def current_active_file():
    """The file with the most recent mtime is whichever session is
    actively being written to right now."""
    files = [f for f in os.listdir(DATA_DIR)
             if os.path.isfile(os.path.join(DATA_DIR, f))]
    if not files:
        return None
    return max(files, key=lambda f: os.path.getmtime(os.path.join(DATA_DIR, f)))


class AuthedHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DATA_DIR, **kw)

    def _authorized(self):
        if not ACCESS_TOKEN:
            return True
        supplied = self.headers.get("Authorization", "")
        query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        return supplied == f"Bearer {ACCESS_TOKEN}" or query_token == ACCESS_TOKEN

    def do_GET(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Unauthorized\n")
            return

        parsed = urlparse(self.path)
        token_qs = parse_qs(parsed.query).get("token", [""])[0]

        if parsed.path in ("/", ""):
            self._handle_index(token_qs)
        elif parsed.path == "/status":
            self._handle_status()
        elif parsed.path == "/latest":
            self._handle_latest()
        elif parsed.path == "/capture-now":
            self._handle_capture_now()
        elif parsed.path == "/clear-old":
            self._handle_clear_old()
        elif parsed.path.startswith("/zip/"):
            self._handle_zip(parsed.path[len("/zip/"):])
        else:
            super().do_GET()

    def _handle_index(self, token):
        active = current_active_file()
        files = sorted(
            [f for f in os.listdir(DATA_DIR) if os.path.isfile(os.path.join(DATA_DIR, f))],
            key=lambda f: os.path.getmtime(os.path.join(DATA_DIR, f)),
            reverse=True,
        )
        rows = []
        for f in files:
            fp = os.path.join(DATA_DIR, f)
            size_mb = os.path.getsize(fp) / 1e6
            mtime = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(os.path.getmtime(fp)))
            is_active = (f == active)
            status_label = (
                '<span style="color:#e08;font-weight:bold">● ACTIVE</span>' if is_active
                else '<span style="color:#0a6">✓ captured</span>'
            )
            action = (
                f'<a href="/capture-now?token={html.escape(token)}" '
                f'style="background:#e05;color:#fff;padding:4px 10px;'
                f'border-radius:4px;text-decoration:none">Capture now</a>'
                if is_active else
                f'<a href="/zip/{html.escape(f)}?token={html.escape(token)}" '
                f'style="background:#06a;color:#fff;padding:4px 10px;'
                f'border-radius:4px;text-decoration:none">Download zip</a>'
            )
            rows.append(
                f"<tr><td>{html.escape(f)}</td><td>{size_mb:.1f} MB</td>"
                f"<td>{mtime}</td><td>{status_label}</td><td>{action}</td></tr>"
            )

        body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Polymarket Lifecycle Capture</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 20px; background:#111; color:#eee; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ padding: 8px 12px; border-bottom: 1px solid #333; text-align: left; }}
h2 {{ font-weight: 600; }}
</style></head>
<body>
<h2>Polymarket Lifecycle Capture</h2>
<p>The ACTIVE file is being written to right now. Pressing "Capture now" gracefully
stops that session, finalizes the file, starts a fresh one immediately, and
downloads a zip of what was just captured.</p>
<p><a href="/clear-old?token={html.escape(token)}"
   onclick="return confirm('Delete every finalized file? The active session will be kept.')"
   style="background:#a30;color:#fff;padding:6px 14px;border-radius:4px;text-decoration:none">
   🗑 Clear old files (keeps active)</a></p>
<table>
<tr><th>File</th><th>Size</th><th>Last write</th><th>Status</th><th></th></tr>
{"".join(rows) if rows else "<tr><td colspan=5>No files yet</td></tr>"}
</table>
</body></html>"""
        body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_status(self):
        files = []
        for f in sorted(os.listdir(DATA_DIR)):
            fp = os.path.join(DATA_DIR, f)
            if os.path.isfile(fp):
                files.append({
                    "name": f,
                    "size_mb": round(os.path.getsize(fp) / 1e6, 2),
                    "modified": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(fp))),
                })
        total = dir_size_bytes(DATA_DIR)
        body = json.dumps({
            "total_size_mb": round(total / 1e6, 2),
            "cleanup_budget_mb": round(MAX_DATA_BYTES / 1e6, 2),
            "pct_of_budget_used": round(100 * total / MAX_DATA_BYTES, 1),
            "files": files,
        }, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_latest(self):
        latest = current_active_file()
        if not latest:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"no files yet\n")
            return
        fp = os.path.join(DATA_DIR, latest)
        body = json.dumps({
            "latest_file": latest,
            "size_mb": round(os.path.getsize(fp) / 1e6, 2),
            "last_modified": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(fp))),
            "download_raw": f"/{latest}?token=...",
            "download_zip": f"/zip/{latest}?token=...",
        }, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_clear_old(self):
        # Deletes every finalized file, leaving the currently-active
        # session untouched so capture keeps running uninterrupted —
        # the server-side equivalent of clear_captured.sh.
        active = current_active_file()
        deleted = []
        for f in os.listdir(DATA_DIR):
            fp = os.path.join(DATA_DIR, f)
            if os.path.isfile(fp) and f != active:
                os.remove(fp)
                deleted.append(f)
        body = json.dumps({
            "deleted": deleted,
            "deleted_count": len(deleted),
            "kept_active_file": active,
        }, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        print(f"[fileserver] cleared {len(deleted)} finalized file(s), "
              f"kept active: {active}", flush=True)

    def _handle_capture_now(self):
        # Identify the active file BEFORE stopping anything, since
        # once the process exits the supervisor may already start a
        # new one and current_active_file() would then point at the
        # wrong (brand new, empty) file.
        target_file = current_active_file()

        with proc_lock:
            proc = current_proc
            if proc is not None and proc.poll() is None:
                print("[capture-now] stopping active session for "
                      f"manual capture: {target_file}", flush=True)
                proc.terminate()  # SIGTERM — the script handles this
                                  # gracefully (writes CAPTURE COMPLETE,
                                  # flushes/closes the file) before exiting.
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    print("[capture-now] graceful stop timed out, "
                          "forcing kill", flush=True)
                    proc.kill()
                    proc.wait(timeout=5)

        if not target_file:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"nothing was actively capturing\n")
            return

        # Give the supervisor loop a brief moment to notice the
        # process exited and spin up the next session, so by the
        # time we respond, a fresh file already exists (matches the
        # "automatically create another one" requirement).
        time.sleep(2)

        data = zip_bytes_for(target_file)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                          f'attachment; filename="{target_file}.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        print(f"[capture-now] delivered zip of {target_file}, "
              f"new session should now be starting", flush=True)

    def _handle_zip(self, fname):
        target = os.path.join(DATA_DIR, fname)
        if not os.path.isfile(target):
            self.send_response(404)
            self.end_headers()
            return
        data = zip_bytes_for(fname)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                          f'attachment; filename="{fname}.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        if not self._authorized():
            self.send_response(401)
            self.end_headers()
            return
        fname = os.path.basename(urlparse(self.path).path)
        target = os.path.join(DATA_DIR, fname)
        if os.path.isfile(target):
            os.remove(target)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"deleted {fname}\n".encode())
            print(f"[fileserver] deleted {fname} by request", flush=True)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[fileserver] {self.address_string()} - {fmt % args}", flush=True)


def run_file_server():
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), AuthedHandler) as httpd:
        print(f"[fileserver] serving {DATA_DIR} on :{PORT} "
              f"(disk budget: {MAX_DATA_BYTES/1e9:.1f}GB)", flush=True)
        httpd.serve_forever()


def run_capture_forever():
    global current_proc
    while True:
        print(f"[supervisor] starting capture (restart every "
              f"{RESTART_AFTER_HOURS}h, or on-demand via Capture button)",
              flush=True)
        proc = subprocess.Popen(
            [sys.executable, "capture/capture_market_lifecycle.py",
             "--hours", str(RESTART_AFTER_HOURS),
             "--output-dir", DATA_DIR],
        )
        with proc_lock:
            current_proc = proc
        returncode = proc.wait()
        with proc_lock:
            current_proc = None
        print(f"[supervisor] capture process exited with code "
              f"{returncode} — restarting in 2s", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    threading.Thread(target=run_file_server, daemon=True).start()
    threading.Thread(target=enforce_disk_budget, daemon=True).start()
    run_capture_forever()
      

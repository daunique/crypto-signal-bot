"""
Fly.io entrypoint: runs the capture script in a restart loop and
serves the /data directory over HTTP so it can be pulled down
manually from a phone via curl/wget.

Built for a LEGACY FREE-ALLOWANCE Fly.io org: 1 shared-cpu-1x VM,
a 3GB persistent volume. Includes automatic disk-budget cleanup so
the volume never silently fills — IMPORTANT: cleanup deletes the
OLDEST files first once the budget is exceeded, regardless of
whether they've been downloaded yet. Pull data down regularly.
"""
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

# Legacy free allowance = 3GB volume. Leave real headroom below that.
MAX_DATA_BYTES = int(os.environ.get("MAX_DATA_BYTES", str(2 * 1024**3)))  # 2GB
CLEANUP_CHECK_INTERVAL = 300  # seconds

os.makedirs(DATA_DIR, exist_ok=True)


def dir_size_bytes(path):
    total = 0
    for f in os.listdir(path):
        fp = os.path.join(path, f)
        if os.path.isfile(fp):
            total += os.path.getsize(fp)
    return total


def enforce_disk_budget():
    """Delete the OLDEST capture files first once total size exceeds
    MAX_DATA_BYTES. WARNING: this does not check whether a file has
    been downloaded — pull data down regularly to avoid losing
    anything to automatic cleanup."""
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
                    print(f"[cleanup] removed {victim} ({freed/1e6:.1f}MB) "
                          f"to stay under free-tier disk budget", flush=True)
        except Exception as e:
            print(f"[cleanup] error: {e}", flush=True)
        time.sleep(CLEANUP_CHECK_INTERVAL)


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

        if parsed.path == "/status":
            self._handle_status()
            return

        if parsed.path.startswith("/zip/"):
            self._handle_zip(parsed.path[len("/zip/"):])
            return

        super().do_GET()

    def _handle_status(self):
        # Lets you check disk usage / how close to the cleanup
        # threshold you are, WITHOUT downloading anything — so you
        # know whether it's urgent to pull data down right now.
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

    def _handle_zip(self, fname):
        # Compresses a single file on-the-fly and streams it back as
        # a .zip — JSONL compresses very well (~90% smaller in past
        # sessions), so this meaningfully reduces mobile data usage.
        target = os.path.join(DATA_DIR, fname)
        if not os.path.isfile(target):
            self.send_response(404)
            self.end_headers()
            return
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(target, arcname=fname)
        data = buf.getvalue()
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
    while True:
        print(f"[supervisor] starting capture (restart every "
              f"{RESTART_AFTER_HOURS}h)", flush=True)
        proc = subprocess.run(
            [sys.executable, "capture/capture_market_lifecycle.py",
             "--hours", str(RESTART_AFTER_HOURS),
             "--output-dir", DATA_DIR],
        )
        print(f"[supervisor] capture process exited with code "
              f"{proc.returncode} — restarting in 5s", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=run_file_server, daemon=True).start()
    threading.Thread(target=enforce_disk_budget, daemon=True).start()
    run_capture_forever()
    

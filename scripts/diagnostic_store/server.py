"""Bounded, upload-only screenshot store. Run behind the supplied nginx configuration."""

import argparse
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zipfile


MAX_UPLOAD = 5 * 1024 * 1024
MAX_IMAGES = 128


def validate_bundle(data):
    """Never extract uploads; accept only bounded, uncompressed PNG/JPEG entries."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if not 1 <= len(entries) <= MAX_IMAGES:
            raise ValueError("invalid image count")
        names = set()
        total = 0
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or ":" in entry.filename or entry.is_dir() or entry.filename in names
                    or entry.flag_bits & 1 or entry.compress_type != zipfile.ZIP_STORED
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("invalid image entry")
            names.add(entry.filename)
            total += entry.file_size
            if total > MAX_UPLOAD or entry.file_size <= 0:
                raise ValueError("image budget exceeded")
            image = archive.read(entry)  # Checks CRC; stored size is already bounded.
            suffix = path.suffix.lower()
            if suffix == ".png":
                valid = image.startswith(b"\x89PNG\r\n\x1a\n")
            elif suffix in (".jpg", ".jpeg"):
                valid = image.startswith(b"\xff\xd8\xff")
            else:
                valid = False
            if not valid:
                raise ValueError("only screenshot images are accepted")


class Store:
    def __init__(self, config):
        self.root = Path(config["directory"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = int(config["retention_days"]) * 86400
        self.cap = int(config["max_bytes"])
        self.reserve = int(config["min_free_bytes"])
        self.daily_cap = int(config["daily_max_bytes"])
        self.max_files = int(config["max_files"])
        if min(self.ttl, self.cap, self.reserve, self.daily_cap, self.max_files) <= 0:
            raise ValueError("storage limits must be positive")
        self.lock = threading.Lock()

    def inventory(self, now):
        files = []
        for path in self.root.glob("*.zip"):
            info = path.stat()
            if now - info.st_mtime >= self.ttl:
                path.unlink()
            else:
                files.append(info)
        return files

    def clean(self):
        with self.lock:
            self.inventory(time.time())
            # An interrupted atomic write must not consume capacity indefinitely.
            for path in self.root.glob("*.part"):
                path.unlink()

    def save(self, data):
        with self.lock:
            now = time.time()
            files = self.inventory(now)
            size = len(data)
            today = int(now // 86400)
            daily = sum(f.st_size for f in files if int(f.st_mtime // 86400) == today)
            if (len(files) >= self.max_files or sum(f.st_size for f in files) + size > self.cap
                    or daily + size > self.daily_cap
                    or shutil.disk_usage(self.root).free - size < self.reserve):
                return None
            identifier = secrets.token_hex(32)
            temporary = self.root / (identifier + ".part")
            destination = self.root / (identifier + ".zip")
            try:
                with temporary.open("xb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                    os.fchmod(output.fileno(), 0o640)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return {"id": identifier, "bytes": size,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "expires_at": int(now + self.ttl)}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        self.store = store
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "DiagnosticStore"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def reply(self, status, payload):
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200 if self.path == "/healthz" else 404,
                   {"status": "ok"} if self.path == "/healthz" else {"error": "not found"})

    def do_POST(self):
        if self.path != "/upload":
            self.reply(404, {"error": "not found"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                or self.headers.get("Transfer-Encoding")):
            self.reply(411, {"error": "content length required"})
            return
        size = int(lengths[0])
        if not 1 <= size <= MAX_UPLOAD:
            self.reply(413, {"error": "upload exceeds 5 MiB"})
            return
        if self.headers.get("Content-Type") != "application/zip":
            self.reply(415, {"error": "ZIP required"})
            return
        try:
            data = self.rfile.read(size)
            if len(data) != size:
                raise ValueError("incomplete upload")
            validate_bundle(data)
        except (ValueError, zipfile.BadZipFile, RuntimeError, EOFError, OSError):
            self.reply(400, {"error": "invalid screenshot bundle"})
            return
        try:
            receipt = self.server.store.save(data)
        except OSError:
            logging.exception("Could not persist screenshot bundle")
            self.reply(503, {"error": "storage unavailable"})
            return
        if receipt is None:
            self.reply(507, {"error": "storage budget reached"})
        else:
            self.reply(201, receipt)

    def log_message(self, format, *args):
        logging.info(format, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    store = Store(config)
    store.clean()

    def housekeeping():
        while True:
            time.sleep(600)
            try:
                store.clean()
            except OSError:
                logging.exception("Screenshot retention cleanup failed")

    threading.Thread(target=housekeeping, daemon=True).start()
    Server(("127.0.0.1", int(config.get("port", 9340))), store).serve_forever()


if __name__ == "__main__":
    main()

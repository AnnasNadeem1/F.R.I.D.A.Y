"""The phone server: serves the mobile web app and a small API, on 127.0.0.1 only.

Reach it from your phone through Tailscale (`tailscale serve`), which provides
the HTTPS that phones require for microphone access and keeps it private to
your own devices. On top of that, every /api call needs the pairing token
that the desktop app shows as a QR code.

API (all but /api/hello need "Authorization: Bearer <token>"):
  POST /api/message  {"text": ...}        -> server-sent events (see below)
  POST /api/voice    raw audio (webm/wav) -> server-sent events
  GET  /api/audio/<id>                    -> audio/wav for one spoken sentence
  POST /api/push/subscribe  <subscription JSON>
  POST /api/push/test
Events: transcript, delta (reply text), audio ({"url"}), activity, error, done.
"""
import hmac
import io
import json
import logging
import mimetypes
import secrets
import threading
import time
import uuid
import wave
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from ..config import settings
from . import push

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
TOKEN_FILE = settings.data_dir / "phone_token.txt"
MAX_UPLOAD = 15 * 1024 * 1024  # bytes of audio (~10 min of opus); plenty for one request
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/sw.js": "sw.js",
          "/manifest.webmanifest": "manifest.webmanifest"}
ICONS = {"/icon-192.png": "phone-icon-192.png", "/icon-512.png": "phone-icon-512.png"}


def token() -> str:
    """The pairing token (created once). Anyone holding it can talk to the assistant."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def reset_token() -> str:
    """Unpair every phone: a new token, and their push subscriptions are dropped."""
    TOKEN_FILE.unlink(missing_ok=True)
    push.clear()
    return token()


def wav_bytes(audio: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.astype(np.int16).tobytes())
    return buf.getvalue()


class _Nothing(Exception):
    """Nothing to answer (empty message, or speech that couldn't be made out)."""


class _AudioCache:
    """Recently synthesised sentences, fetched once by the phone. Small and short-lived."""

    def __init__(self, size=60, ttl=300):
        self.items: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self.size, self.ttl = size, ttl
        self.lock = threading.Lock()

    def put(self, data: bytes) -> str:
        key = uuid.uuid4().hex
        with self.lock:
            self.items[key] = (time.time(), data)
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return key

    def get(self, key: str) -> bytes | None:
        with self.lock:
            item = self.items.get(key)
        if not item or time.time() - item[0] > self.ttl:
            return None
        return item[1]


class PhoneServer:
    def __init__(self, engine, port: int):
        self.engine = engine
        self.port = port
        self.audio = _AudioCache()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            # ---- helpers

            def _authorised(self) -> bool:
                header = self.headers.get("Authorization", "")
                given = header[7:] if header.startswith("Bearer ") else ""
                return bool(given) and hmac.compare_digest(given, token())

            def _send(self, code: int, body: bytes, kind: str, extra: dict | None = None):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj):
                self._send(code, json.dumps(obj).encode(), "application/json")

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_UPLOAD:
                    raise ValueError("upload too large")
                return self.rfile.read(n)

            # ---- routes

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in STATIC:
                    f = WEB_DIR / STATIC[path]
                    kind = "application/manifest+json" if f.suffix == ".webmanifest" else \
                        (mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                    extra = {"Service-Worker-Allowed": "/"} if path == "/sw.js" else None
                    return self._send(200, f.read_bytes(), kind + ("; charset=utf-8" if kind.startswith(("text", "application/j")) else ""), extra)
                if path in ICONS:
                    f = settings.data_dir / ICONS[path]
                    return self._send(200, f.read_bytes(), "image/png") if f.exists() else self._send(404, b"", "text/plain")
                if path == "/api/hello":
                    return self._json(200, server.hello(self._authorised()))
                if not self._authorised():
                    return self._json(401, {"error": "not paired"})
                if path.startswith("/api/audio/"):
                    data = server.audio.get(path.rsplit("/", 1)[-1])
                    return self._send(200, data, "audio/wav") if data else self._json(404, {"error": "expired"})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if not self._authorised():
                    return self._json(401, {"error": "not paired"})
                try:
                    body = self._body()
                except ValueError as e:
                    return self._json(413, {"error": str(e)})
                if path == "/api/push/subscribe":
                    push.subscribe(json.loads(body or b"{}"))
                    return self._json(200, {"ok": True})
                if path == "/api/push/test":
                    push.notify(settings.name, "Notifications are working.", tag="test")
                    return self._json(200, {"ok": True})
                if path in ("/api/message", "/api/voice"):
                    return server.converse(self, path, body)
                self._json(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="phone-server").start()
        log.info("phone server on 127.0.0.1:%s", port)

    # ---------------------------------------------------------------- API logic

    def hello(self, authorised: bool) -> dict:
        info = {"name": settings.name, "paired": authorised}
        if authorised:
            info.update(push_key=push.public_key(), ready=self.engine.ready,
                        wake_phrase=getattr(self.engine, "wake_phrase", settings.name))
        return info

    def converse(self, handler: BaseHTTPRequestHandler, path: str, body: bytes):
        """Stream one conversational turn to the phone as server-sent events."""
        if not self.engine.ready:
            return handler._json(503, {"error": f"{settings.name} is still starting up"})
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Accel-Buffering", "no")
        handler.send_header("Connection", "close")
        handler.send_header("Transfer-Encoding", "chunked")  # each event is delivered as soon as it's written
        handler.end_headers()
        write_lock = threading.Lock()
        handler.close_connection = True

        def event(name: str, data):
            msg = f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
            with write_lock:
                try:
                    handler.wfile.write(f"{len(msg):x}\r\n".encode() + msg + b"\r\n")
                    handler.wfile.flush()
                except OSError:
                    pass  # phone went away; finish the turn anyway so history stays consistent

        try:
            if path == "/api/voice":
                text = self.engine.transcribe_upload(body)
                if not text:
                    raise _Nothing("Didn't catch that.")
                event("transcript", {"text": text})
            else:
                text = (json.loads(body or b"{}").get("text") or "").strip()
                if not text:
                    raise _Nothing("")

            # Sentences are synthesised in order on one worker, so audio arrives in sequence
            # while the text keeps streaming.
            jobs: list[threading.Thread] = []

            def on_sentence(sentence: str):
                prev = jobs[-1] if jobs else None

                def synth():
                    try:
                        audio, rate = self.engine.synth_for_phone(sentence)
                    except Exception as e:
                        log.warning("phone synth failed: %s", e)
                        return
                    if prev:
                        prev.join()
                    if audio is not None and audio.size:
                        event("audio", {"url": f"/api/audio/{self.audio.put(wav_bytes(audio, rate))}",
                                        "text": sentence})

                t = threading.Thread(target=synth, daemon=True)
                jobs.append(t)
                t.start()

            self.engine.phone_turn(text, on_delta=lambda c: event("delta", {"text": c}),
                                   on_sentence=on_sentence, on_activity=lambda a: event("activity", {"text": a}))
            for t in jobs:
                t.join(timeout=60)
        except _Nothing as e:
            if str(e):
                event("error", {"message": str(e)})
        except Exception as e:
            log.exception("phone turn failed")
            event("error", {"message": str(e)[:200]})
        event("done", {})
        with write_lock:
            try:
                handler.wfile.write(b"0\r\n\r\n")  # end of the chunked stream
            except OSError:
                pass

    def stop(self):
        self.httpd.shutdown()

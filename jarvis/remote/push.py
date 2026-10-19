"""Web push notifications to paired phones (reminders, Claude alerts).

Uses the standard Web Push protocol: the phone's browser gives us a
subscription (an endpoint on Google's push service plus encryption keys), and
we send end-to-end encrypted messages to it, signed with our own VAPID key.
Free, and the push service can't read the contents."""
import base64
import json
import logging
import threading

from cryptography.hazmat.primitives import serialization

from ..config import settings

log = logging.getLogger(__name__)

KEY_FILE = settings.data_dir / "push_vapid_private.pem"
SUBS_FILE = settings.data_dir / "push_subscriptions.json"
_lock = threading.Lock()


def _vapid():
    from py_vapid import Vapid

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if not KEY_FILE.exists():
        v = Vapid()
        v.generate_keys()
        v.save_key(str(KEY_FILE))
    return Vapid.from_file(str(KEY_FILE))


def public_key() -> str:
    """The key the browser needs to subscribe (base64url, uncompressed EC point)."""
    raw = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _load() -> list[dict]:
    try:
        return json.loads(SUBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(subs: list[dict]):
    SUBS_FILE.write_text(json.dumps(subs, indent=1), encoding="utf-8")


def subscribe(sub: dict):
    with _lock:
        subs = [s for s in _load() if s.get("endpoint") != sub.get("endpoint")]
        subs.append(sub)
        _save(subs)


def clear():
    with _lock:
        _save([])


def count() -> int:
    return len(_load())


def notify(title: str, body: str, tag: str = "friday"):
    """Send to every subscribed phone, in the background. Dead subscriptions are dropped."""
    def run():
        from pywebpush import WebPushException, webpush

        with _lock:
            subs = _load()
        if not subs:
            return
        key = str(KEY_FILE)
        payload = json.dumps({"title": title, "body": body, "tag": tag})
        alive = []
        for sub in subs:
            try:
                webpush(sub, payload, vapid_private_key=key, vapid_claims={"sub": "mailto:friday@localhost"},
                        ttl=3600, timeout=10)
                alive.append(sub)
            except WebPushException as e:
                status = getattr(e.response, "status_code", None)
                if status not in (404, 410):  # 404/410 = the phone unsubscribed; anything else, keep it
                    alive.append(sub)
                log.warning("push failed (%s): %s", status, str(e)[:120])
            except Exception as e:
                alive.append(sub)
                log.warning("push failed: %s", e)
        if len(alive) != len(subs):
            with _lock:
                _save(alive)

    threading.Thread(target=run, daemon=True, name="push").start()

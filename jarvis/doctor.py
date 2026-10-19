"""Self-check: verifies each part of the assistant with a real but tiny request
and explains anything broken in plain words.

    python -m jarvis --doctor        (or Settings -> Diagnostics in the app)
"""
import json
import time
from dataclasses import dataclass

from .config import ROOT, settings

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _run(name: str, fn) -> Check:
    try:
        status, detail = fn()
    except Exception as e:
        status, detail = FAIL, f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
    return Check(name, status, detail)


def _gemini():
    if not settings.gemini_api_key:
        return WARN, "no GEMINI_API_KEY in .env (Groq will be used)"
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    t = time.perf_counter()
    client.models.generate_content(model=settings.gemini_model, contents="Reply with: ok")
    return OK, f"{settings.gemini_model} answered in {time.perf_counter() - t:.1f}s"


def _groq():
    if not settings.groq_api_key:
        return WARN, "no GROQ_API_KEY in .env (no backup brain, no accurate speech recognition)"
    from groq import Groq

    client = Groq(api_key=settings.groq_api_key, timeout=15)
    ids = {m.id for m in client.models.list().data}
    missing = [m for m in (settings.groq_model, "whisper-large-v3-turbo") if m not in ids]
    if missing:
        return WARN, f"key works, but these models aren't available: {', '.join(missing)}"
    return OK, f"key works; {settings.groq_model} and whisper-large-v3-turbo available"


def _voice():
    from .engine import make_speaker

    speaker = make_speaker()
    desc = speaker.describe()
    engine = speaker.engine
    if hasattr(engine, "fallback"):  # ElevenLabs: try a 3-character line (costs ~3 credits)
        engine.synth("Hi.")
        if engine.last_error:
            return WARN, f"ElevenLabs failed ({engine.last_error[:100]}); the backup voice will be used"
    if settings.tts_engine not in desc.lower() and not (settings.tts_engine == "piper" and "piper" in desc.lower()):
        return WARN, f"asked for {settings.tts_engine}, but using {desc}"
    return OK, desc


def _whisper():
    from .speech.stt import Transcriber

    t = Transcriber(settings.whisper_model)
    return OK, f"{t.model_name} on {t.device.upper()} (live text)"


def _wake():
    import numpy as np

    from .speech.wake import WakeWord

    w = WakeWord(settings.models_dir, settings.wake_threshold, settings.name, settings.wake_model)
    w.score(np.zeros(1280, np.int16))
    note = "" if settings.name.lower() in w.phrase.lower() else f" (no '{settings.name}' model added yet)"
    return (OK if not note else WARN), f'"{w.phrase}", threshold {settings.wake_threshold}{note}'


def _audio():
    import numpy as np
    import sounddevice as sd

    from .speech.mic import Microphone

    mic_name = sd.query_devices(kind="input")["name"]
    out_name = sd.query_devices(kind="output")["name"]
    try:
        mic = Microphone()
    except Exception as e:
        return FAIL, f"couldn't open the microphone '{mic_name}': {e}"
    try:
        audio = mic.read(16000 // 2)
    finally:
        mic.close()
    level = float(np.sqrt(np.mean(audio ** 2)))
    note = " (silent: muted, or the wrong input device?)" if level < 1e-5 else ""
    status = WARN if note or "headset" in mic_name.lower() else OK
    if "headset" in mic_name.lower():
        note += " (a Bluetooth headset mic forces call-quality audio; prefer the laptop's mic)"
    return status, f"mic '{mic_name}', speaker '{out_name}'{note}"


def _google():
    from .google_tools import TOKEN, _credentials, _service

    if not TOKEN.exists():
        return WARN, "not connected (python -m jarvis --google-login)"
    _credentials()
    # Events only: Jarvis has calendar.events access, not the calendar list.
    _service("calendar", "v3").events().list(calendarId="primary", maxResults=1).execute()
    return OK, "calendar connected"


def _classroom():
    from .classroom import TOKEN, classroom_courses

    if not TOKEN.exists():
        return WARN, "not connected (Settings -> CONNECT GOOGLE CLASSROOM)"
    r = classroom_courses()
    if "error" in r:
        return FAIL, r["error"]
    return OK, f"{len(r['courses'])} active classes"


def _email():
    from .email_tools import _backend

    b = _backend()
    if b.name == "Yahoo Mail":
        b._imap().logout()
        return OK, f"Yahoo Mail login works ({settings.yahoo_email})"
    from .google_tools import _service

    _service("gmail", "v1").users().getProfile(userId="me").execute()
    return OK, "Gmail connected"


def _web():
    from .web_tools import get_weather, web_search

    r = web_search("open-meteo weather api", 1)
    w = get_weather("London", 1)
    if not r["results"]:
        return WARN, "search returned nothing"
    return OK, f"search and weather work (London now {w['now']['temperature_c']}°C)"


def _claude():
    from .claude_hooks_setup import SETTINGS

    hooks = json.loads(SETTINGS.read_text(encoding="utf-8")).get("hooks", {}) if SETTINGS.exists() else {}
    ours = [e for e, entries in hooks.items() for x in entries
            if any("notify_jarvis.py" in h.get("command", "") for h in x.get("hooks", []))]
    if not ours:
        return WARN, "hooks not installed (python -m jarvis --install-claude-hooks)"
    return OK, f"hooks installed for {', '.join(sorted(ours))}"


def _startup():
    from . import winstartup

    return OK, ("starts with Windows" if winstartup.is_startup_enabled() else "doesn't start with Windows (optional)")


def _files():
    missing = [p for p in ("pronunciations.txt", "vocabulary.txt", ".env") if not (ROOT / p).exists()]
    return (WARN if missing else OK), (f"missing: {', '.join(missing)}" if missing else "config files present")


CHECKS = [
    ("Config files", _files), ("Brain: Gemini", _gemini), ("Brain backup + hearing: Groq", _groq),
    ("Voice", _voice), ("Speech recognition", _whisper), ("Wake word", _wake), ("Microphone & speaker", _audio),
    ("Web search & weather", _web), ("Google Calendar", _google), ("Google Classroom", _classroom), ("Email", _email),
    ("Claude Code hooks", _claude), ("Windows startup", _startup),
]


def run_all(progress=None) -> list[Check]:
    results = []
    for name, fn in CHECKS:
        if progress:
            progress(name)
        results.append(_run(name, fn))
    return results


def main():
    import sys

    try:  # ✓/✗ need UTF-8; old Windows code pages (e.g. when piped) get plain marks instead
        "✓✗".encode(sys.stdout.encoding or "ascii")
        ok_mark, fail_mark = "✓", "✗"
    except (UnicodeEncodeError, LookupError):
        ok_mark, fail_mark = "OK", "XX"
    marks = {OK: f"\x1b[32m{ok_mark}\x1b[0m", WARN: "\x1b[33m!\x1b[0m", FAIL: f"\x1b[31m{fail_mark}\x1b[0m"}
    print(f"{settings.name} diagnostics\n")
    for c in run_all(progress=lambda n: print(f"  checking {n}...", end="\r", flush=True)):
        print(f"  {marks[c.status]} {c.name:30} {c.detail}" + " " * 10)

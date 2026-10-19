"""Settings loaded from .env (secrets) with sensible defaults."""
import logging
import os
import warnings
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Hugging Face (Whisper downloads) warns about Windows symlinks and missing
# tokens; both are harmless here, so keep them out of the console.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# The xet transfer backend stalled on this connection; plain HTTP is reliable.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
warnings.filterwarnings("ignore", message=".*unauthenticated requests.*")
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    name: str = _env("ASSISTANT_NAME") or _env("JARVIS_NAME", "Jarvis")
    wake_model: str = _env("WAKE_MODEL")  # .onnx in models/openwakeword; empty = pick by name, else hey_jarvis
    llm_provider: str = _env("LLM_PROVIDER", "gemini").lower()
    gemini_api_key: str = _env("GEMINI_API_KEY")
    gemini_model: str = _env("GEMINI_MODEL", "gemini-flash-lite-latest")  # higher free-tier rate limit than full Flash
    groq_api_key: str = _env("GROQ_API_KEY")
    groq_model: str = _env("GROQ_MODEL", "openai/gpt-oss-120b")
    # groq = final transcript by Groq's Whisper large-v3-turbo (much better with accents;
    # sends your voice clips to Groq). local = everything stays on this PC.
    stt_engine: str = _env("STT_ENGINE", "groq").lower()
    whisper_model: str = _env("WHISPER_MODEL", "auto")  # auto = small.en on GPU, base.en on CPU
    tts_engine: str = _env("TTS_ENGINE", "piper").lower()  # elevenlabs | kokoro | piper | sapi
    kokoro_voice: str = _env("KOKORO_VOICE", "bm_george")  # bm_george | bm_lewis | bm_daniel | bm_fable
    elevenlabs_api_key: str = _env("ELEVENLABS_API_KEY")
    elevenlabs_voice_id: str = _env("ELEVENLABS_VOICE_ID")
    elevenlabs_model: str = _env("ELEVENLABS_MODEL", "eleven_flash_v2_5")  # fastest, cheapest in credits
    piper_voice: str = _env("PIPER_VOICE", "en_GB-alan-medium")
    tts_speed: float = float(_env("TTS_SPEED", "1.1"))  # higher = slower, clearer speech
    email_provider: str = _env("EMAIL_PROVIDER", "auto").lower()  # auto = Yahoo if configured, else Gmail
    yahoo_email: str = _env("YAHOO_EMAIL")
    yahoo_app_password: str = _env("YAHOO_APP_PASSWORD").replace(" ", "")  # Yahoo shows it in groups of 4
    speak_typed: bool = _env("SPEAK_TYPED", "1") == "1"  # in the app, also speak replies to typed messages
    # Phone app (jarvis.remote): served on 127.0.0.1 and reached through Tailscale.
    phone_access: bool = _env("PHONE_ACCESS", "1") == "1"
    phone_port: int = int(_env("PHONE_PORT", "8770"))
    classroom_reminders: bool = _env("CLASSROOM_REMINDERS", "1") == "1"
    classroom_remind_hours: str = _env("CLASSROOM_REMIND_HOURS", "24,2")  # hours before the due time
    claude_bridge_port: int = int(_env("CLAUDE_BRIDGE_PORT", "8765"))
    claude_announce_min_seconds: float = float(_env("CLAUDE_ANNOUNCE_MIN_SECONDS", "30"))  # skip quick replies
    # Ignore the wake word mid-sentence ("see you on Friday"); you address the assistant after a pause.
    # Say the wake word while the assistant is talking to cut it off. Best with headphones:
    # through laptop speakers its own voice saying its name could stop it.
    barge_in: bool = _env("BARGE_IN", "1") == "1"
    wake_require_pause: bool = _env("WAKE_REQUIRE_PAUSE", "1") == "1"
    wake_threshold: float = float(_env("WAKE_THRESHOLD", "0.5"))  # lower = more sensitive, more false wakes
    followup_seconds: float = float(_env("FOLLOWUP_SECONDS", "6"))  # listen this long for a follow-up, no wake word
    data_dir: Path = ROOT / "data"
    models_dir: Path = ROOT / "models"


settings = Settings()


def update_env(values: dict[str, str]):
    """Set keys in .env, replacing existing lines and appending new ones.
    Other lines (API keys, comments) are left exactly as they are."""
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    lines += [f"{k}={v}" for k, v in remaining.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_vocabulary() -> list[str]:
    """Words from vocabulary.txt that speech recognition should expect."""
    path = ROOT / "vocabulary.txt"
    if not path.exists():
        return []
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        words += [w.strip() for w in line.split(",") if w.strip()]
    return words

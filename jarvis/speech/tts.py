"""Text-to-speech that starts talking on the first sentence while the LLM is
still generating the rest. Synthesis and playback run in separate threads so
sentence N+1 is synthesised while sentence N plays."""
import logging
import queue
import re
import threading
import time
from pathlib import Path
from typing import Iterable

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

# Split only at real sentence ends. Cutting at commas, colons or semicolons makes separate clips, each with
# its own falling "end of sentence" tone and pause, which sounds like odd stops mid-sentence.
_SENTENCE_END = re.compile(r"(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)(?<!\bvs\.)(?<!\be\.g\.)(?<!\bi\.e\.)"
                           r"(?<=[.!?])\s+|\n+")
_MARKDOWN = re.compile(r"[*_#`>\[\]]")
# Pause inserted after a clip, by the punctuation that ended it (seconds). Each clip's own leading and
# trailing silence is trimmed first, so these are the pauses you actually hear.
PAUSE_AFTER = {".": 0.26, "!": 0.30, "?": 0.30, ",": 0.12, ";": 0.18, ":": 0.18}
DEFAULT_PAUSE = 0.20
FIRST_SPLIT_CHARS = 110  # an opening sentence longer than this is split at a comma, to start speaking sooner


def trim_silence(audio: np.ndarray, rate: int, margin_ms: int = 25) -> np.ndarray:
    """Cut silence from both ends of a clip (voices pad each clip with 100-200 ms)."""
    if audio.size == 0:
        return audio
    loud = np.nonzero(np.abs(audio.astype(np.int32)) > max(int(np.abs(audio).max()) * 0.02, 60))[0]
    if loud.size == 0:
        return audio
    margin = int(rate * margin_ms / 1000)
    return audio[max(0, loud[0] - margin): min(len(audio), loud[-1] + margin)]


IPA_RULES: list[tuple[re.Pattern, str]] = []  # (respelling, IPA) pairs; filled from pronunciations.txt

INTERNAL_PAUSE_CAP = 0.16  # longest silence kept inside a clip (seconds); set from KOKORO_PAUSE_CAP, 0 = off


def shorten_pauses(audio: np.ndarray, rate: int, cap: float) -> np.ndarray:
    """Shorten silences inside a clip to `cap` seconds. Kokoro's network generates 200-450 ms of
    silence at every comma, which sounds like hesitation; a natural comma pause is about 150 ms.
    The cut is taken from the middle of the silence, so the words themselves aren't touched."""
    if cap <= 0 or audio.size == 0:
        return audio
    frame = max(1, int(rate * 0.01))  # 10 ms
    n = len(audio) // frame
    if n < 8:
        return audio
    rms = np.sqrt((audio[: n * frame].astype(np.float32).reshape(n, frame) ** 2).mean(axis=1))
    quiet = rms < max(float(rms.max()) * 0.03, 1.0)
    keep = np.ones(len(audio), bool)
    cap_frames = int(cap * 100)
    i = 0
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            run = j - i
            if run > cap_frames + 3 and i > 0 and j < n:  # leave the very start and end alone
                extra = run - cap_frames
                cut_from = (i + (run - extra) // 2) * frame
                keep[cut_from: cut_from + extra * frame] = False
            i = j
        else:
            i += 1
    return audio[keep]


def finish_clip(audio: np.ndarray, rate: int, text: str) -> np.ndarray:
    """Trim a synthesised clip and append the pause its closing punctuation calls for."""
    ending = text.rstrip(" \"')]\u201d\u2019")[-1:]
    pause = np.zeros(int(rate * PAUSE_AFTER.get(ending, DEFAULT_PAUSE)), np.int16)
    return np.concatenate([trim_silence(audio, rate).astype(np.int16), pause])


def clean(text: str) -> str:
    return _MARKDOWN.sub("", text).strip()


class PiperEngine:
    def __init__(self, voice: str, models_dir: Path, length_scale: float = 1.1):
        from piper import PiperVoice
        from piper.config import SynthesisConfig

        path = models_dir / f"{voice}.onnx"
        if not path.exists():
            raise FileNotFoundError(
                f"Piper voice not found at {path}. Download it with:\n"
                f"  .venv\\Scripts\\python -m piper.download_voices {voice} --data-dir models"
            )
        self.voice = PiperVoice.load(str(path))
        self.sample_rate = self.voice.config.sample_rate
        # length_scale > 1 speaks slower; default voices run a little fast and slur.
        self.config = SynthesisConfig(length_scale=length_scale)
        self.synth("ready")  # first call is ~3 s slower; pay it at startup, not on the first reply

    def synth(self, text: str) -> np.ndarray:
        chunks = list(self.voice.synthesize(text, syn_config=self.config))
        return np.concatenate([c.audio_int16_array for c in chunks]) if chunks else np.zeros(0, np.int16)


class KokoroEngine:
    """Kokoro (82M-parameter neural TTS): far more natural than Piper, runs on the CPU at
    roughly 1.8x real time. Voices: bm_george, bm_lewis, bm_daniel, bm_fable (British male)."""

    def __init__(self, voice: str, models_dir: Path, length_scale: float = 1.0, variant: str = "int8"):
        from kokoro_onnx import Kokoro

        d = models_dir / "kokoro"
        # int8 is the compressed model (smaller, faster); "fp32" is full quality, ~0.3 s slower per sentence.
        model, voices = d / ("kokoro-v1.0.onnx" if variant == "fp32" else "kokoro-v1.0.int8.onnx"), d / "voices-v1.0.bin"
        if not model.exists() or not voices.exists():
            raise FileNotFoundError(f"Kokoro model files not found in {d}. See README.md, 'Voice'.")
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.enable_cpu_mem_arena = False  # don't hold on to a growing memory pool between sentences
        opts.intra_op_num_threads = 6
        self.kokoro = Kokoro.from_session(
            ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"]), str(voices))
        self.voice = voice  # name, or a blend such as "af_bella:3,af_heart:1" (weights are relative)
        self.style = self._style(voice)
        self.speed = 1.0 / length_scale  # our setting: higher = slower; Kokoro's: higher = faster
        self.lang = "en-gb" if voice.startswith("b") else "en-us"
        self.sample_rate = 24000
        self.synth("ready")  # warm-up, so the first reply isn't slower

    def _style(self, voice: str):
        """A voice name, or a weighted mix of several ("af_bella:3,af_heart:1" is 75% / 25%)."""
        if "," not in voice and ":" not in voice:
            return voice
        parts = []
        for item in voice.split(","):
            name, _, weight = item.strip().partition(":")
            parts.append((self.kokoro.get_voice_style(name.strip()), float(weight or 1)))
        return sum(style * w for style, w in parts) / sum(w for _, w in parts)

    @property
    def label(self) -> str:
        return " + ".join(item.split(":")[0].strip()[3:].title() for item in self.voice.split(","))

    def _phonemes(self, text: str) -> str | None:
        """IPA for the whole text when it contains a name with an exact pronunciation
        (pronunciations.txt "Annas = an-nass | ɑnəs"); None to let Kokoro work it out itself."""
        hits = [(m.start(), m.end(), ipa) for pattern, ipa in IPA_RULES for m in pattern.finditer(text)]
        if not hits:
            return None
        out, pos = [], 0
        for start, end, ipa in sorted(hits):
            if start < pos:
                continue
            if text[pos:start].strip():
                out.append(self.kokoro.tokenizer.phonemize(text[pos:start], self.lang))
            out.append(ipa)
            pos = end
        if text[pos:].strip():
            out.append(self.kokoro.tokenizer.phonemize(text[pos:], self.lang))
        return re.sub(r"\s+([.,!?;:])", r"\1", " ".join(p.strip() for p in out))  # no space before punctuation

    def synth(self, text: str) -> np.ndarray:
        phonemes = self._phonemes(text)
        audio, _ = self.kokoro.create(phonemes or text, voice=self.style, speed=self.speed, lang=self.lang,
                                      is_phonemes=phonemes is not None)
        audio = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        return shorten_pauses(audio, self.sample_rate, INTERNAL_PAUSE_CAP)


class ElevenLabsEngine:
    """ElevenLabs cloud voices. The free plan has a small monthly character quota, so
    every failure (quota used up, offline, voice not allowed) falls back to the local
    voice for that sentence, and ElevenLabs is rested for a while before retrying."""

    URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

    def __init__(self, api_key: str, voice_id: str, model: str, fallback):
        import requests

        if not api_key or not voice_id:
            raise ValueError("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must both be set in .env")
        self.session = requests.Session()
        self.session.headers["xi-api-key"] = api_key
        self.voice_id, self.model = voice_id, model
        self.fallback = fallback
        self.sample_rate = 24000  # we ask for raw 24 kHz PCM, the same rate Kokoro uses
        self._rest_until = 0.0
        self.last_error = ""
        self._prev_text, self._prev_time = "", 0.0  # last sentence spoken, for natural flow
        # Open the HTTPS connection now (free; the response itself doesn't matter), so the
        # first reply doesn't pay the ~4 s connection setup.
        try:
            self.session.get("https://api.elevenlabs.io/v1/models", timeout=8)
        except requests.RequestException:
            pass

    def synth(self, text: str) -> np.ndarray:
        import requests

        if time.monotonic() >= self._rest_until:
            try:
                body = {"text": text, "model_id": self.model}
                # Sentences are sent one at a time; telling ElevenLabs what came just before keeps
                # the intonation flowing instead of each sentence starting "fresh". (v3 doesn't accept it.)
                if self._prev_text and time.monotonic() - self._prev_time < 20 and "v3" not in self.model:
                    body["previous_text"] = self._prev_text[-500:]
                r = self.session.post(self.URL.format(voice_id=self.voice_id), params={"output_format": "pcm_24000"},
                                      json=body, timeout=8)  # a slow reply is worse than the local voice
                if r.status_code == 200 and r.content:
                    self._prev_text, self._prev_time = text, time.monotonic()
                    return np.frombuffer(r.content[: len(r.content) // 2 * 2], dtype=np.int16)
                self.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                if "quota_exceeded" in r.text:
                    rest = 6 * 3600  # out of credits: won't change until the monthly reset, so stop asking
                elif r.status_code in (401, 402, 403, 429):
                    rest = 900
                else:
                    rest = 60
                self._rest_until = time.monotonic() + rest
            except requests.RequestException as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self._rest_until = time.monotonic() + 60
            log.warning("ElevenLabs failed (%s); using the local voice for now", self.last_error)
            preload = getattr(self.fallback, "preload", None)
            if preload:
                preload()  # start loading the backup voice now, not when it's needed mid-sentence
        return self._fallback(text)

    def _fallback(self, text: str) -> np.ndarray:
        if not hasattr(self.fallback, "synth"):
            return np.zeros(0, np.int16)
        audio = self.fallback.synth(text)
        if self.fallback.sample_rate == self.sample_rate or not audio.size:
            return audio
        # Piper is 22.05 kHz; resample so it can share the open 24 kHz output stream.
        n = int(len(audio) * self.sample_rate / self.fallback.sample_rate)
        return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.int16)


class LazyEngine:
    """Stands in for a local voice engine and builds it on first use (thread-safe)."""

    def __init__(self, factory, label: str, sample_rate: int):
        self._factory, self.label, self.sample_rate = factory, label, sample_rate
        self._engine = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    def _load(self):
        with self._lock:
            if self._engine is None:
                log.info("loading backup voice %s", self.label)
                self._engine = self._factory()
                self.sample_rate = getattr(self._engine, "sample_rate", self.sample_rate)

    def preload(self):
        """Load in the background (harmless if already loaded or loading)."""
        if self._engine is None:
            threading.Thread(target=self._load, daemon=True, name="backup-voice").start()

    def synth(self, text: str) -> np.ndarray:
        self._load()
        if not hasattr(self._engine, "synth"):  # fell back to the Windows voice: can't render audio
            return np.zeros(0, np.int16)
        return self._engine.synth(text)


class Pronunciations:
    """Respellings from pronunciations.txt ("Annas = Uh-nuss"), applied just before
    speaking. Re-read whenever the file changes, so edits work without a restart."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime = None
        self._rules: list[tuple[re.Pattern, str]] = []

    def _load(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._rules, self._mtime = [], None
            return
        if mtime == self._mtime:
            return
        rules, ipa_rules = [], []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            word, sounds = (s.strip() for s in line.split("=", 1))
            sounds, _, ipa = (s.strip() for s in sounds.partition("|"))  # optional exact IPA, used by Kokoro only
            if word and sounds:
                rules.append((re.compile(rf"\b{re.escape(word)}\b", re.I), sounds))
                if ipa:
                    ipa_rules.append((re.compile(rf"\b{re.escape(sounds)}\b", re.I), ipa))
        IPA_RULES[:] = ipa_rules
        # Longest first, so "Annas Khan" wins over "Annas".
        self._rules = sorted(rules, key=lambda r: -len(r[0].pattern))
        self._mtime = mtime

    def apply(self, text: str) -> str:
        self._load()
        for pattern, sounds in self._rules:
            text = pattern.sub(sounds, text)
        return text


class SapiEngine:
    """Windows built-in voices. Robotic, but zero setup; used as a fallback."""

    def __init__(self):
        self._local = threading.local()

    def _engine(self):
        # pyttsx3/COM objects must be created on the thread that uses them.
        if not hasattr(self._local, "engine"):
            import pyttsx3
            self._local.engine = pyttsx3.init()
            self._local.engine.setProperty("rate", 185)
        return self._local.engine

    def speak_direct(self, text: str):
        e = self._engine()
        e.say(text)
        e.runAndWait()


class Speaker:
    def __init__(self, engine_name: str, voice: str, models_dir: Path, length_scale: float = 1.1,
                 kokoro_voice: str = "bm_george", pronunciations: Path | None = None, elevenlabs: dict | None = None,
                 kokoro_model: str = "int8"):
        """engine_name: elevenlabs | kokoro | piper | sapi. Local voices fall back kokoro -> piper ->
        Windows voice. ElevenLabs wraps the best local voice, used whenever ElevenLabs fails."""
        self.engine = None
        self.pronunciations = Pronunciations(pronunciations) if pronunciations else None
        if engine_name == "elevenlabs":
            # The backup voice is only loaded the first time ElevenLabs actually fails, so the
            # ~350 MB Kokoro model isn't held in memory on days ElevenLabs works fine.
            local = LazyEngine(lambda: Speaker._local_engine("kokoro", voice, models_dir, length_scale, kokoro_voice,
                                                             kokoro_model),
                               label="Kokoro " + " + ".join(v.split(":")[0].strip()[3:].title() for v in kokoro_voice.split(",")), sample_rate=24000)
            try:
                self.engine = ElevenLabsEngine(fallback=local, **(elevenlabs or {}))
            except Exception as e:
                log.warning("ElevenLabs unavailable (%s); using the local voice.", e)
                self.engine = local
        else:
            self.engine = Speaker._local_engine(engine_name, voice, models_dir, length_scale, kokoro_voice, kokoro_model)
        self._start_threads()

    @staticmethod
    def _local_engine(engine_name, voice, models_dir, length_scale, kokoro_voice, kokoro_model="int8"):
        engine = None
        if engine_name == "kokoro":
            try:
                engine = KokoroEngine(kokoro_voice, models_dir, length_scale, kokoro_model)
            except Exception as e:
                log.warning("Kokoro unavailable (%s); falling back to Piper.", e)
                engine_name = "piper"
        if engine_name == "piper":
            try:
                engine = PiperEngine(voice, models_dir, length_scale)
            except Exception as e:
                log.warning("Piper unavailable (%s); falling back to Windows SAPI voice.", e)
        return engine or SapiEngine()

    def describe(self) -> str:
        """Which voice is really in use, e.g. "ElevenLabs (backup: Kokoro bm_george)"."""
        def name(e):
            if isinstance(e, LazyEngine):
                return e.label + ("" if e.loaded else ", loads if needed")
            if isinstance(e, KokoroEngine):
                return f"Kokoro {e.label}"
            return {"PiperEngine": "Piper Alan", "SapiEngine": "Windows voice"}.get(type(e).__name__, type(e).__name__)
        if isinstance(self.engine, ElevenLabsEngine):
            return f"ElevenLabs (backup: {name(self.engine.fallback)})"
        return name(self.engine)

    def _start_threads(self):
        self._text_q: queue.Queue[str | None] = queue.Queue()
        self._audio_q: queue.Queue = queue.Queue(maxsize=2)
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0
        self._lock = threading.Lock()
        self._cancel = threading.Event()  # set by interrupt(): drop everything until resume()
        self.on_level = None  # optional callback(0..1): how loud the voice is right now, for the UI
        threading.Thread(target=self._synth_loop, daemon=True).start()
        if hasattr(self.engine, "synth"):
            threading.Thread(target=self._play_loop, daemon=True).start()

    def _done_one(self):
        with self._lock:
            self._pending -= 1
            if self._pending == 0:
                self._idle.set()

    def _synth_loop(self):
        while True:
            text = self._text_q.get()
            try:
                if not self._cancel.is_set():
                    if hasattr(self.engine, "synth"):
                        audio = finish_clip(self.engine.synth(text), self.engine.sample_rate, text)
                        if not self._cancel.is_set():
                            self._audio_q.put(audio)
                            continue  # play loop marks it done
                    else:
                        self.engine.speak_direct(text)
            except Exception:
                log.exception("TTS failed")
            self._done_one()

    def _open_stream(self):
        stream = sd.OutputStream(samplerate=self.engine.sample_rate, channels=1, dtype="int16", latency="high")
        stream.start()
        return stream

    def _level(self, value: float):
        if self.on_level:
            try:
                self.on_level(value)
            except Exception:
                pass

    def _play_loop(self):
        # One stream kept open for the whole session. Opening a new stream per
        # sentence makes Bluetooth earbuds swallow the start of each sentence.
        rate = self.engine.sample_rate
        step = int(rate * 0.05)  # write in 50 ms slices so an interruption stops almost at once
        stream = None
        while True:
            audio = self._audio_q.get()
            try:
                if stream is None or not stream.active:
                    stream = self._open_stream()
                if audio.size and not self._cancel.is_set():
                    for i in range(0, len(audio), step):
                        if self._cancel.is_set():
                            stream.abort()  # discard what's already buffered too
                            stream = None
                            break
                        piece = audio[i:i + step]
                        self._level(min(1.0, float(np.sqrt(np.mean((piece / 32768.0) ** 2))) * 6))
                        stream.write(piece)
            except Exception:
                log.exception("Playback failed; reopening audio output")
                stream = None  # e.g. earbuds disconnected; reopen on the default device next time
            self._level(0.0)
            self._done_one()

    def say(self, sentence: str):
        if self._cancel.is_set():
            return  # interrupted: the rest of this reply isn't wanted
        sentence = clean(sentence)
        if self.pronunciations:
            sentence = self.pronunciations.apply(sentence)
        if not sentence:
            return
        with self._lock:
            self._pending += 1
            self._idle.clear()
        self._text_q.put(sentence)

    def interrupt(self):
        """Stop talking now: drop queued sentences and cut the current one short.
        Further say() calls are ignored until resume()."""
        self._cancel.set()
        for q in (self._text_q, self._audio_q):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
                self._done_one()

    def resume(self):
        self._cancel.clear()

    def chime(self, rising=True, wait=True):
        """Short two-note tone: rising = "I'm listening", falling = "going to sleep".
        wait=False returns immediately so listening can start while it plays."""
        rate = getattr(self.engine, "sample_rate", 22050)
        notes = (660, 880) if rising else (880, 587)
        tone = []
        for f in notes:
            t = np.arange(int(rate * 0.09)) / rate
            env = np.minimum(1, np.minimum(t, t[::-1]) / 0.01)  # 10 ms fade in/out avoids clicks
            tone.append(np.sin(2 * np.pi * f * t) * env * 0.25)
        audio = (np.concatenate(tone) * 32767).astype(np.int16)
        if not hasattr(self.engine, "synth"):
            sd.play(audio, rate)
            if wait:
                sd.wait()
            return
        with self._lock:
            self._pending += 1
            self._idle.clear()
        self._audio_q.put(audio)
        if wait:
            self.wait()

    def stream(self, chunks: Iterable[str], echo=False, on_chunk=None) -> str:
        """Speak a stream of text deltas sentence by sentence. Returns the full text.
        on_chunk(text) sees each delta as it arrives (for displaying it)."""
        buf, full, first = "", [], True
        for chunk in chunks:
            full.append(chunk)
            if on_chunk:
                on_chunk(chunk)
            if echo:
                print(chunk, end="", flush=True)
            buf += chunk
            parts = _SENTENCE_END.split(buf)
            for sentence in parts[:-1]:
                self._say_sentence(sentence, first)
                first = False
            buf = parts[-1]
        self._say_sentence(buf, first)
        if echo:
            print()
        return "".join(full)

    def _say_sentence(self, sentence: str, first: bool):
        """A long opening sentence is split at its first comma: the first words are
        synthesised (and heard) sooner, which matters with slower voices like Kokoro."""
        if first and len(sentence) > FIRST_SPLIT_CHARS:
            cut = sentence.find(", ", 30)
            if 0 < cut < len(sentence) - 30:
                self.say(sentence[:cut + 1])
                self.say(sentence[cut + 2:])
                return
        self.say(sentence)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until everything queued has been spoken. True if idle (False on timeout)."""
        return self._idle.wait(timeout)

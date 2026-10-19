"""Speech recognition with energy-based end-of-speech detection.

Live text: while you speak, the audio so far is re-transcribed locally with
faster-whisper about twice a second, so words appear as you talk.

Final text: when you stop, the whole clip goes to Groq's hosted Whisper
large-v3-turbo if a Groq key is set (far better with accents), falling back
to the local model. Without Groq, the last live result is reused when it
already covered all your speech, which skips a final pass."""
import difflib
import io
import logging
import re
import threading
import time
import wave
from typing import Callable

import numpy as np

from .mic import SAMPLE_RATE, Microphone

log = logging.getLogger(__name__)

BLOCK = 480  # 30 ms
PARTIAL_EVERY = 0.5  # seconds between live transcription updates
MIN_PARTIAL_AUDIO = 1.0  # seconds; Whisper hallucinates on tiny scraps of audio
# Phrases Whisper invents from silence or noise (it was trained on subtitled video).
_HALLUCINATIONS = {"thanks for watching", "thank you for watching", "subscribe", "you", "bye", "thank you"}


def _is_hallucination(text: str) -> bool:
    return text.lower().strip(" .!?,") in _HALLUCINATIONS


def _cuda_available() -> bool:
    """Whisper on GPU needs NVIDIA's cuBLAS DLL; skip the attempt when it's absent."""
    import ctypes

    try:
        ctypes.WinDLL("cublas64_12.dll")
        return True
    except (OSError, AttributeError):
        return False


def _whisper(name: str, **kwargs):
    """Load from the local cache first. Otherwise Hugging Face is contacted on
    every start to check for updates, which hangs when the connection stalls."""
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(name, local_files_only=True, **kwargs)
    except Exception:
        log.info("Downloading Whisper %s...", name)
        return WhisperModel(name, **kwargs)


class GroqWhisper:
    """Hosted Whisper large-v3-turbo. Sends the recorded clip to Groq."""

    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo"):
        from groq import Groq

        self.client = Groq(api_key=api_key, timeout=8.0)
        self.model = model

    def transcribe(self, audio: np.ndarray, prompt: str | None) -> str:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        r = self.client.audio.transcriptions.create(
            file=("speech.wav", buf.getvalue()), model=self.model, language="en", prompt=prompt or None,
            temperature=0.0,
        )
        return (r.text or "").strip()


class Transcriber:
    """model_name "auto" picks small.en on the GPU when CUDA libraries are
    installed, otherwise base.en on the CPU. That local model drives the live
    text; `remote` (GroqWhisper), when given, produces the final transcript."""

    def __init__(self, model_name: str = "auto", vocabulary: list[str] | None = None, remote: GroqWhisper | None = None):
        self.model, self.device, self.model_name = self._load(model_name)
        self.remote = remote
        # Whisper treats the prompt as preceding text, which biases it toward
        # these spellings (place names, people, jargon it would otherwise mangle).
        self.prompt = f"Words you may hear: {', '.join(vocabulary)}." if vocabulary else None
        self.vocabulary = [v for v in (vocabulary or []) if " " not in v]

    @staticmethod
    def _load(model_name: str):
        # Two workers let the final pass run while a stale live pass finishes.
        if _cuda_available():
            try:
                name = "small.en" if model_name == "auto" else model_name
                m = _whisper(name, device="cuda", compute_type="float16", num_workers=2)
                # transcribe() is lazy; consume it so CUDA problems fail here, not mid-conversation.
                list(m.transcribe(np.zeros(SAMPLE_RATE, np.float32), language="en")[0])
                return m, "cuda", name
            except Exception as e:
                log.info("GPU speech recognition failed (%s); using CPU", str(e).splitlines()[0])
        name = "base.en" if model_name == "auto" else model_name
        return _whisper(name, device="cpu", compute_type="int8", cpu_threads=8, num_workers=2), "cpu", name

    def transcribe(self, audio: np.ndarray, live=False) -> str:
        segments, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=not live,  # VAD trims silence on the final pass; too slow to bother with live
            without_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt=self.prompt,
        )
        return self._snap_to_vocabulary(" ".join(s.text.strip() for s in segments).strip())

    def transcribe_final(self, audio: np.ndarray) -> str:
        if self.remote:
            try:
                text = self.remote.transcribe(audio, self.prompt)
                return "" if _is_hallucination(text) else self._snap_to_vocabulary(text)
            except Exception as e:
                log.warning("Groq transcription failed (%s); using local model", str(e).splitlines()[0])
        return self.transcribe(audio)

    def _snap_to_vocabulary(self, text: str) -> str:
        """Replace near-misses of vocabulary words ("Rawpindi" -> "Rawalpindi").
        Only words of 5+ letters with high similarity, so ordinary words are left alone."""
        if not self.vocabulary or not text:
            return text

        def fix(m: re.Match) -> str:
            word = m.group(0)
            if len(word) < 5:
                return word
            best = difflib.get_close_matches(word.lower(), [v.lower() for v in self.vocabulary], n=1, cutoff=0.8)
            # Leave inflected forms alone ("Pakistani" is not a misheard "Pakistan").
            if not best or word.lower().startswith(best[0]):
                return word
            return next(v for v in self.vocabulary if v.lower() == best[0])

        return re.sub(r"[A-Za-z]+", fix, text)

    def listen(
        self,
        mic: Microphone,
        on_partial: Callable[[str], None] | None = None,
        max_wait=6.0,
        on_level: Callable[[float], None] | None = None,
        max_len=30.0,
        end_silence=0.8,
    ) -> str | None:
        """Read from the shared mic until the speaker pauses and return the
        transcript, or None if nobody spoke within max_wait seconds. Audio
        already buffered in the mic (e.g. words right after the wake word) counts."""
        frames: list[np.ndarray] = []
        latest = {"text": "", "covered": 0}  # last live transcript and how many samples it saw
        inflight = {"covered": 0}  # samples seen by the live pass currently running
        finished = threading.Event()  # set once we return, so late live passes stay quiet
        idle = threading.Event()
        idle.set()

        def live_pass(audio: np.ndarray):
            try:
                text = self.transcribe(audio, live=True)
                if text and not _is_hallucination(text):
                    latest.update(text=text, covered=len(audio))
                    if on_partial and not finished.is_set():
                        on_partial(text)
            except Exception:
                log.exception("live transcription failed")
            finally:
                idle.set()

        # Speech threshold from the room's background level (measured continuously by the mic).
        threshold = min(max(mic.noise_floor() * 3.0, 0.008), 0.03)
        started, silent_for, speech_end, last_partial = False, 0.0, 0, 0.0
        heard_for = 0.0  # seconds of audio read while waiting for speech to start
        while True:
            block = mic.read(BLOCK)
            level = float(np.sqrt(np.mean(block ** 2)))
            if on_level:
                on_level(min(1.0, level * 8))
            loud = level > threshold
            if not started:
                frames = (frames + [block])[-10:]  # 300 ms pre-roll so the first word isn't clipped
                heard_for += BLOCK / SAMPLE_RATE
                if loud:
                    started = True
                    speech_end = len(frames) * BLOCK
                elif heard_for > max_wait:
                    return None
                continue
            frames.append(block)
            total = len(frames) * BLOCK
            if loud:
                speech_end = total
                silent_for = 0.0
            else:
                silent_for += BLOCK / SAMPLE_RATE
            if silent_for >= end_silence or total / SAMPLE_RATE >= max_len:
                break
            # Kick off a live pass if the previous one has finished.
            now = time.monotonic()
            if (on_partial and idle.is_set() and now - last_partial >= PARTIAL_EVERY
                    and total / SAMPLE_RATE >= MIN_PARTIAL_AUDIO):
                idle.clear()
                last_partial = now
                inflight["covered"] = total
                threading.Thread(target=live_pass, args=(np.concatenate(frames),), daemon=True).start()

        try:
            audio = np.concatenate(frames)
            if self.remote:
                return self.transcribe_final(audio) or None
            # A live pass already running over all the speech will finish sooner than a new pass.
            if not idle.is_set() and inflight["covered"] >= speech_end:
                idle.wait()
            if latest["text"] and latest["covered"] >= speech_end:
                return latest["text"]
            return self.transcribe(audio) or None
        finally:
            finished.set()

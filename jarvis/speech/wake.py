"""Always-on wake word detection with openWakeWord ("hey jarvis"). Runs a
tiny model on 80 ms audio frames; light enough to leave on all day."""
import collections
import logging
import re
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

FRAME = 1280  # 80 ms, the frame size openWakeWord expects


def find_model(d: Path, name: str) -> Path:
    """A wake word model for the assistant's name (e.g. hey_friday*.onnx) if one has been
    added to models/openwakeword, otherwise the built-in "hey jarvis"."""
    support = {"melspectrogram.onnx", "embedding_model.onnx", "silero_vad.onnx"}
    for p in sorted(d.glob("*.onnx")):
        if p.name not in support and name.lower() in p.stem.lower():
            return p
    return d / "hey_jarvis_v0.1.onnx"


class WakeWord:
    def __init__(self, models_dir: Path, threshold: float = 0.5, name: str = "Jarvis", model_file: str = "",
                 require_pause: bool = True):
        from openwakeword.model import Model

        d = models_dir / "openwakeword"
        path = d / model_file if model_file else find_model(d, name)
        # "hey_friday_v0.1.onnx" -> "Hey Friday": the phrase to show the user.
        self.phrase = " ".join(w for w in re.sub(r"_v?\d.*$", "", path.stem).split("_")).title()
        if not path.exists():
            raise FileNotFoundError(
                f"Wake word model not found at {path}. Download it with:\n"
                "  .venv\\Scripts\\python -c \"from openwakeword import utils; "
                "utils.download_models(['hey_jarvis'], target_directory='models/openwakeword')\""
            )
        self.model = Model(
            wakeword_models=[str(path)],
            inference_framework="onnx",
            melspec_model_path=str(d / "melspectrogram.onnx"),
            embedding_model_path=str(d / "embedding_model.onnx"),
        )
        self.threshold = threshold
        self.require_pause = require_pause  # ignore the name when it's mid-sentence

    def score(self, frame: np.ndarray) -> float:
        """Wake word confidence (0-1) for one 80 ms int16 frame."""
        return max(self.model.predict(frame).values())

    def wait(self, mic, on_level=None, stop=None) -> float | None:
        """Block until the wake word is heard; returns its confidence, or None if `stop`
        (a threading.Event) gets set first. Reads from the shared mic, so whatever you
        say next stays buffered for the listener. on_level(0..1) gets the mic loudness."""
        self.model.reset()  # forget audio from before we started listening
        levels: collections.deque[float] = collections.deque(maxlen=24)  # ~2 s of 80 ms frame loudness
        while True:
            if stop is not None and stop.is_set():
                return None
            audio = mic.read(FRAME)
            levels.append(float(np.sqrt(np.mean(audio ** 2))))
            if on_level:
                on_level(min(1.0, levels[-1] * 8))
            s = self.score((np.clip(audio, -1, 1) * 32767).astype(np.int16))
            if s < self.threshold:
                continue
            if self.require_pause and self._mid_sentence(list(levels), mic.noise_floor()):
                log.info("wake word score %.2f ignored: you were already talking (mid-sentence)", s)
                self.model.reset()
                continue
            log.info("wake word score %.2f", s)
            return s

    @staticmethod
    def _mid_sentence(levels: list[float], noise: float) -> bool:
        """True if there was steady speech just before the wake word ("...see you on Friday").
        Detection fires ~0.5 s into the word, so 0.8-1.4 s before detection is the gap
        before it: silent when you address the assistant, busy mid-sentence. A short
        "hey"/"okay" right before the name is under the 0.4 s that counts as talking."""
        if len(levels) < 18:
            return False
        recent = levels[::-1]  # recent[0] = the detection frame
        word_peak = max(recent[:8])
        loud = max(noise * 3, word_peak * 0.25, 0.01)
        return sum(level > loud for level in recent[10:18]) >= 5

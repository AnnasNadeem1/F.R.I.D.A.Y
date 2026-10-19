"""One microphone stream that stays open for the whole session.

Wake word detection and speech recognition both read from it in turn, so no
audio is lost between them: "Hey Jarvis, what's the time" arrives intact.
(Closing and reopening a stream on Windows takes long enough to drop the
words right after the wake word.)"""
import collections
import queue

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
_CALLBACK_BLOCK = 480  # 30 ms
_MAX_BUFFER_SECONDS = 15  # older audio is dropped if nobody is reading


class MicClosed(Exception):
    """Raised by read() once the microphone has been switched off."""


class Microphone:
    def __init__(self):
        self._closed = False
        self._q: queue.Queue[np.ndarray | None] = queue.Queue()
        self._buf = np.zeros(0, np.float32)
        self._levels = collections.deque(maxlen=200)  # ~6 s of 30 ms block levels
        self._max_blocks = int(_MAX_BUFFER_SECONDS * SAMPLE_RATE / _CALLBACK_BLOCK)
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=_CALLBACK_BLOCK, callback=self._callback
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        if self._closed:
            return
        self._q.put(indata[:, 0].copy())
        while self._q.qsize() > self._max_blocks:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def read(self, n: int) -> np.ndarray:
        """Next n samples (float32, 16 kHz mono), in order, blocking until available."""
        while len(self._buf) < n:
            if self._closed:
                raise MicClosed()
            try:
                block = self._q.get(timeout=3)
            except queue.Empty:
                raise RuntimeError("microphone stopped sending audio (unplugged or in use?)")
            if block is None:  # close() woke us up
                raise MicClosed()
            self._levels.append(float(np.sqrt(np.mean(block ** 2))))
            self._buf = np.concatenate([self._buf, block])
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def flush(self):
        """Discard buffered audio, e.g. what was recorded while Jarvis was talking."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._buf = np.zeros(0, np.float32)
        if self._closed:
            self._q.put(None)  # keep the wake-up for a reader racing with close()

    def noise_floor(self) -> float:
        """Typical background level: a low percentile of recent block levels,
        so it reflects the room rather than your voice."""
        return float(np.percentile(self._levels, 20)) if self._levels else 0.003

    def close(self):
        """Release the microphone (Windows' mic-in-use indicator turns off) and
        wake any reader, which then gets MicClosed."""
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._q.put(None)

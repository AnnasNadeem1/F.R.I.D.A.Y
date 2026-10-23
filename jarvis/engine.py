"""The assistant's run loop, independent of how it's displayed.

Engine owns the voice pipeline (wake word -> listen -> reply -> follow-ups),
typed messages, and background announcements, and reports everything to a
Listener. The terminal (__main__) and the desktop app (gui) are listeners.

States: loading, off (microphone released), sleeping (waiting for the wake
word), listening, thinking, speaking, error.
"""
import io
import logging
import random
import re
import threading

from .assistant import Assistant
from .config import load_vocabulary, settings
from .llm import available_providers

log = logging.getLogger(__name__)

# Saying one of these ends the conversation and goes back to waiting for the wake word.
SLEEP_PHRASES = ("that's all", "that is all", "goodbye", "bye", "go to sleep", "never mind", "nevermind", "stop listening")
# The request can start with the tail of the wake phrase; covers both names and common mishearings.
_WAKE_PREFIX = re.compile(
    rf"^\W*(hey|hi|ok|okay)?\W*({re.escape(settings.name.lower())}|jarvis|jervis|travis|javis|vis|friday|fridays)\b\W*",
    re.I)


def strip_wake_word(text: str) -> str:
    """The request can arrive with the tail of "Hey Jarvis" attached; drop it."""
    rest = _WAKE_PREFIX.sub("", text, count=1).strip()
    return rest[:1].upper() + rest[1:] if rest else ""


def is_sleep_phrase(text: str) -> bool:
    """Short sign-offs like "that's all, thanks" end the conversation. Only short
    utterances count, so "never mind the weather, what's on today?" still gets answered."""
    t = re.sub(r"[^\w' ]", " ", text.lower())
    words = t.split()
    if len(words) > 5:
        return False
    name = settings.name.lower()
    if " ".join(words) in ("thanks", "thank you", f"thanks {name}", f"thank you {name}"):
        return True
    return any(re.search(rf"\b{re.escape(p)}\b", t) for p in SLEEP_PHRASES)


def make_speaker():
    from .config import ROOT
    from .speech import tts
    from .speech.tts import Speaker

    tts.INTERNAL_PAUSE_CAP = settings.kokoro_pause_cap
    return Speaker(settings.tts_engine, settings.piper_voice, settings.models_dir, settings.tts_speed,
                   kokoro_voice=settings.kokoro_voice, pronunciations=ROOT / "pronunciations.txt",
                   kokoro_model=settings.kokoro_model,
                   elevenlabs={"api_key": settings.elevenlabs_api_key, "voice_id": settings.elevenlabs_voice_id,
                               "model": settings.elevenlabs_model})


class Listener:
    """Receives engine events. Calls come from background threads."""

    def state(self, state: str): ...
    def status(self, text: str): ...                 # loading progress, device info
    def user_partial(self, text: str): ...           # live words while you speak
    def user(self, text: str, source: str): ...      # final words; source = "voice" | "typed"
    def assistant_start(self): ...
    def assistant_delta(self, chunk: str): ...
    def assistant_end(self): ...
    def notice(self, kind: str, text: str): ...      # kind = "reminder" | "claude"
    def error(self, text: str): ...
    def level(self, value: float): ...               # 0..1 loudness of whoever is talking (for the UI)
    def activity(self, text: str): ...               # what the assistant is doing, e.g. "Searching the web"


# What the window shows while a tool runs, and which tools are slow enough to say "one sec" first.
TOOL_ACTIVITY = {
    "web_search": "Searching the web", "news_search": "Checking the news", "read_webpage": "Reading a page",
    "get_weather": "Checking the weather", "calendar_list_events": "Checking your calendar",
    "calendar_create_event": "Updating your calendar", "email_list_unread": "Checking your email",
    "email_read": "Reading an email", "email_create_draft": "Drafting an email", "email_send_draft": "Sending",
    "set_reminder": "Setting a reminder", "list_reminders": "Checking reminders", "remember": "Remembering that",
    "claude_code_status": "Checking on Claude", "classroom_assignments": "Checking Google Classroom",
    "classroom_courses": "Checking Google Classroom", "daily_briefing": "Putting your briefing together", "claude_task_prepare": "Preparing a Claude task",
    "claude_task_start": "Starting Claude", "claude_task_status": "Checking on Claude's task",
    "claude_task_cancel": "Stopping Claude", "focus_start": "Starting your focus session", "focus_stop": "Ending your session",
    "focus_status": "Checking your study time", "open_app": "Opening an app", "open_website": "Opening a site",
    "media_control": "Adjusting the sound", "lock_screen": "Locking the screen", "look_at_screen": "Looking at your screen",
    "clipboard": "Using the clipboard", "system_status": "Checking the PC",
    "plan_my_day": "Planning your day", "save_plan": "Saving the plan", "evening_checkin": "Reviewing your day",
    "todo_add": "Adding to your list", "todo_list": "Checking your list", "todo_done": "Ticking that off",
    "todo_remove": "Updating your list", "note_add": "Saving a note", "note_search": "Looking through your notes",
}
SLOW_TOOLS = {"web_search", "news_search", "read_webpage", "get_weather", "calendar_list_events",
              "email_list_unread", "email_read", "classroom_assignments", "classroom_courses",
              "daily_briefing", "look_at_screen"}
FILLERS = ("On it.", "One sec.", "Let me check.", "Checking now.", "Give me a second.")


class _Interrupted(Exception):
    pass


def _decode_audio(data: bytes):
    """Any common recording (webm/opus from the phone, wav, mp3...) -> 16 kHz mono float32.
    Uses PyAV directly; faster-whisper's own helper doesn't match the installed PyAV version."""
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    parts = []
    with av.open(io.BytesIO(data), mode="r") as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush what the resampler still holds
            parts.append(out.to_ndarray().reshape(-1))
    if not parts:
        return np.zeros(0, np.float32)
    return np.concatenate(parts).astype(np.float32) / 32768.0


class Engine:
    def __init__(self, listener: Listener, speak_typed: bool = True, listening: bool = True):
        self.l = listener
        self.speak_typed = speak_typed
        self.state = "loading"
        self._voice_state = "loading"  # what the voice loop is doing, to return to after a typed reply
        self._listening = threading.Event()
        if listening:
            self._listening.set()
        self._stopping = threading.Event()
        self._turn = threading.Lock()  # one reply at a time (voice and typed share the conversation)
        self._ready = threading.Event()
        self.mic = None
        self.wake_phrase = f"Hey {settings.name}"
        self._turn_spoken = False  # has anything been said aloud in the current reply?
        self._speaking_turn = False
        self._phone_sink = None  # set during a phone turn: (say, on_activity, spoken)
        self.phone_server = None

    # ------------------------------------------------------------ control

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="engine").start()

    def stop(self):
        self._stopping.set()
        self._listening.set()  # unblock the loop so it can exit
        self._release_mic()

    @property
    def listening(self) -> bool:
        return self._listening.is_set()

    def set_listening(self, on: bool):
        """Off releases the microphone entirely; reminders and Claude announcements still play."""
        if on:
            self._listening.set()
        else:
            self._listening.clear()
            self._release_mic()
            if self._ready.is_set():
                self._set_state("off")

    def submit_text(self, text: str):
        """A typed message. Answered in the same conversation as voice."""
        text = text.strip()
        if text:
            threading.Thread(target=self._typed, args=(text,), daemon=True).start()

    # ------------------------------------------------------------ internals

    def _set_state(self, s: str, voice: bool = True):
        if voice:
            self._voice_state = s
        self.state = s
        self.l.state(s)

    def _release_mic(self):
        mic, self.mic = self.mic, None
        if mic:
            mic.close()

    def _load(self):
        from .speech.stt import GroqWhisper, Transcriber
        from .speech.wake import WakeWord

        self.l.status("Connecting to the language model...")
        self.assistant = Assistant(available_providers())
        self.assistant.on_tool = self._on_tool
        self.l.status("Loading voice...")
        self.speaker = make_speaker()
        self.speaker.on_level = self.l.level
        self.l.status("Loading speech recognition...")
        remote = GroqWhisper(settings.groq_api_key) if settings.stt_engine == "groq" and settings.groq_api_key else None
        self.stt = Transcriber(settings.whisper_model, load_vocabulary(), remote)
        self.l.status("Loading wake word...")
        self.wake = WakeWord(settings.models_dir, settings.wake_threshold, settings.name, settings.wake_model,
                             settings.wake_require_pause)
        self.wake_phrase = self.wake.phrase
        final = "Groq large-v3-turbo" if remote else f"{self.stt.model_name} (local)"
        voice = self.speaker.describe()
        log.info("Voice: %s", voice)
        self.l.status(f"Wake word: \"{self.wake_phrase}\"  ·  Voice: {voice}  ·  Hearing: {final}")

    def _start_background(self):
        from .claude_bridge import Bridge
        from .reminders import ReminderScheduler, _toast

        def announce(kind: str, text: str):
            self.l.notice(kind, text)
            self.speaker.chime(rising=True, wait=False)
            self.speaker.say(text)
            if settings.phone_access:
                from .remote import push
                push.notify({"reminder": "Reminder", "focus": "Focus"}.get(kind, "Claude Code"), text, tag=kind)

        def reminder(text: str, late: bool):
            announce("reminder", f"While I was offline, you had a reminder: {text}." if late else f"Reminder: {text}.")

        def claude(text: str):
            _toast("Claude Code", text)
            announce("claude", text)

        ReminderScheduler(reminder)
        from .focus import MANAGER as focus_manager
        focus_manager.announce = announce
        focus_manager.start_thread()
        from . import claude_tasks
        claude_tasks.MANAGER.on_done = claude
        claude_tasks.MANAGER.on_activity = self.l.activity
        from .classroom import ClassroomWatcher
        ClassroomWatcher()
        from .briefing import BriefingScheduler
        BriefingScheduler(self.run_briefing)
        from .planner import CHECKIN_PROMPT, PLAN_PROMPT, DailyScheduler
        DailyScheduler("plan", lambda: settings.plan_time, lambda: self.run_scheduled(PLAN_PROMPT, "Plan for today"), 12)
        DailyScheduler("checkin", lambda: settings.checkin_time,
                       lambda: self.run_scheduled(CHECKIN_PROMPT, "Evening check-in"), 23)
        Bridge(claude, settings.claude_bridge_port, settings.claude_announce_min_seconds)
        if settings.phone_access:
            from .remote.server import PhoneServer
            try:
                self.phone_server = PhoneServer(self, settings.phone_port)
            except OSError as e:
                log.warning("phone server couldn't start on port %s: %s", settings.phone_port, e)

    def run_briefing(self) -> bool:
        """Speak the daily briefing now (scheduled, or the BRIEF ME NOW button)."""
        from .briefing import PROMPT

        return self.run_scheduled(PROMPT, "Daily briefing")

    def run_scheduled(self, prompt: str, title: str) -> bool:
        """Start a spoken exchange by itself (briefing, day plan, evening check-in). Waits its turn
        if a conversation is in progress. Also pushed to the phone as a notification."""
        if not self._ready.is_set():
            return False
        self.l.notice("briefing", title)
        self.speaker.chime(rising=True, wait=False)
        self._reply(prompt, speak=True)
        self._set_state("off" if not self._listening.is_set() else self._voice_state, voice=False)
        history = self.assistant.history
        if settings.phone_access and history and history[-1]["role"] == "assistant" and history[-1]["content"]:
            from .remote import push
            push.notify(title, history[-1]["content"][:240], tag="briefing")
        return True

    # ------------------------------------------------------------ phone access (jarvis.remote)

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def transcribe_upload(self, data: bytes) -> str | None:
        """Speech recorded on the phone (webm/opus, wav...) -> text, via the same recogniser."""
        audio = _decode_audio(data)
        if audio.size < 16000 * 0.3:
            return None
        text = self.stt.transcribe_final(audio)
        return strip_wake_word(text) or text or None

    def synth_for_phone(self, sentence: str):
        """Speech audio for the phone (not played here): (int16 array, sample rate)."""
        from .speech.tts import clean

        engine = self.speaker.engine
        if not hasattr(engine, "synth"):
            return None, 0
        text = clean(sentence)
        if self.speaker.pronunciations:
            text = self.speaker.pronunciations.apply(text)
        if not text:
            return None, 0
        from .speech.tts import finish_clip

        return finish_clip(engine.synth(text), engine.sample_rate, text), engine.sample_rate

    def phone_turn(self, text: str, on_delta, on_sentence, on_activity):
        """One conversational turn from the phone: same memory and history as voice and typing,
        shown in the desktop chat too, but spoken on the phone rather than out loud here."""
        from .speech.tts import FIRST_SPLIT_CHARS, _SENTENCE_END

        with self._turn:
            self.l.user(text, "phone")
            self.l.assistant_start()
            spoken = {"any": False}

            def say(sentence: str):
                sentence = sentence.strip()
                if sentence:
                    spoken["any"] = True
                    on_sentence(sentence)

            self._phone_sink = (say, on_activity, spoken)
            buf, first = "", True
            try:
                for chunk in self.assistant.respond(text):
                    on_delta(chunk)
                    self.l.assistant_delta(chunk)
                    buf += chunk
                    parts = _SENTENCE_END.split(buf)
                    for sentence in parts[:-1]:
                        cut = sentence.find(", ", 15) if first and len(sentence) > FIRST_SPLIT_CHARS else -1
                        if 0 < cut < len(sentence) - 15:  # start talking sooner, as on the desktop
                            say(sentence[:cut + 1])
                            sentence = sentence[cut + 2:]
                        say(sentence)
                        first = False
                    buf = parts[-1]
                say(buf)
            finally:
                self._phone_sink = None
                self.l.activity("")
                self.l.assistant_end()

    def _run(self):
        self._set_state("loading")
        try:
            self._load()
        except Exception as e:
            log.exception("startup failed")
            self.l.error(f"Startup failed: {e}")
            self._set_state("error")
            return
        self._start_background()
        self._ready.set()
        self.speaker.say(f"{settings.name} online.")
        self.speaker.wait()
        self._voice_loop()

    def _voice_loop(self):
        from .speech.mic import MicClosed, Microphone

        while not self._stopping.is_set():
            if not self._listening.is_set():
                if self.state not in ("thinking", "speaking"):
                    self._set_state("off")
                self._listening.wait()
                continue
            if self.mic is None:
                try:
                    self.mic = Microphone()
                except Exception as e:
                    self.l.error(f"Couldn't open the microphone: {e}")
                    self.set_listening(False)
                    continue
            mic = self.mic
            try:
                self._set_state("sleeping")
                mic.flush()  # ignore anything heard while the assistant was talking
                self.wake.wait(mic, on_level=self.l.level)
                self.speaker.chime(rising=True, wait=False)
                # No flush here: words right after the wake word are already buffered in the mic.
                text = self._hear(mic, 5.0, after_wake=True)
                while text:
                    if is_sleep_phrase(text):
                        self.speaker.say("Very good.")
                        self.speaker.wait()
                        break
                    if self._reply(text, speak=True, mic=mic):
                        # The wake word was said while she was talking: give them the full
                        # follow-up window to say what they wanted, no need to say the name again.
                        self.speaker.chime(rising=True, wait=False)
                        text = self._hear(mic, settings.followup_seconds, after_wake=True)
                        continue
                    mic.flush()  # conversation mode: a follow-up needs no wake word
                    text = self._hear(mic, settings.followup_seconds)
                self.speaker.chime(rising=False)
            except MicClosed:
                continue
            except Exception as e:
                log.exception("voice loop error")
                self.l.error(f"Voice error: {e}")
                self._release_mic()

    def _hear(self, mic, max_wait: float, after_wake=False) -> str | None:
        from .speech.mic import MicClosed

        self._set_state("listening")
        text = None
        # After the wake word, the first thing heard can be just the end of the name itself
        # (still buffered in the mic), e.g. when you say "Friday" and pause. That isn't a
        # request, so keep listening for the follow-up window instead of going back to sleep.
        for attempt in range(2 if after_wake else 1):
            try:
                text = self.stt.listen(mic, on_partial=self.l.user_partial,
                                       max_wait=max_wait if attempt == 0 else settings.followup_seconds,
                                       on_level=self.l.level)
            except MicClosed:
                raise
            except Exception as e:
                self.l.error(f"Speech recognition failed: {e}")
                return None
            finally:
                self.l.level(0.0)
            if text and after_wake:
                text = strip_wake_word(text) or None
                if text is None:
                    self.l.user_partial("")
                    continue  # only the name was heard; wait for the actual request
            break
        self.l.user_partial("")
        if text:
            self.l.user(text, "voice")
        return text

    def _on_tool(self, name: str):
        activity = TOOL_ACTIVITY.get(name, "Working on it")
        self.l.activity(activity)
        phone = self._phone_sink
        if phone:  # a phone turn: the filler and activity go to the phone, not the laptop speakers
            say, on_activity, spoken = phone
            on_activity(activity)
            if not spoken["any"] and name in SLOW_TOOLS:
                say(random.choice(FILLERS))
            return
        # Slow lookups would leave a long silence; say a quick filler first (once per reply).
        if self._speaking_turn and not self._turn_spoken and name in SLOW_TOOLS:
            self._turn_spoken = True
            self.speaker.say(random.choice(FILLERS))

    def _watch_for_interrupt(self, mic, done: threading.Event, interrupted: threading.Event):
        """While the assistant talks, listen for its name; if heard, cut the speech short."""
        from .speech.mic import MicClosed

        try:
            if self.wake.wait(mic, stop=done) is not None:
                interrupted.set()
                self.speaker.interrupt()
        except MicClosed:
            pass
        except Exception:
            log.exception("interrupt watcher failed")

    def _reply(self, text: str, speak: bool, mic=None) -> bool:
        """Answer one message. With a mic, saying the wake word stops the reply;
        returns True if that happened."""
        with self._turn:
            self._set_state("thinking", voice=mic is not None)
            self.l.assistant_start()
            self._turn_spoken, self._speaking_turn = False, speak
            done, interrupted = threading.Event(), threading.Event()
            watcher = None
            if speak and mic is not None and settings.barge_in:
                watcher = threading.Thread(target=self._watch_for_interrupt, args=(mic, done, interrupted),
                                           daemon=True, name="interrupt-watcher")
                watcher.start()

            def on_chunk(chunk: str):
                if interrupted.is_set():
                    raise _Interrupted()
                if self.state != "speaking":
                    self._set_state("speaking", voice=mic is not None)
                self._turn_spoken = True
                self.l.assistant_delta(chunk)

            reply = self.assistant.respond(text)
            try:
                if speak:
                    self.speaker.stream(reply, on_chunk=on_chunk)
                    while not self.speaker.wait(0.05):  # finish speaking, unless interrupted
                        if interrupted.is_set():
                            break
                else:
                    for chunk in reply:
                        on_chunk(chunk)
            except _Interrupted:
                pass
            except Exception as e:
                log.exception("reply failed")
                self.l.error(str(e))
                if speak:
                    self.speaker.say("Sorry, something went wrong reaching my brain.")
                    self.speaker.wait()
            finally:
                reply.close()  # if cut short, the assistant records the reply as interrupted
                done.set()
                if watcher:
                    watcher.join(timeout=1)
                if interrupted.is_set():
                    self.assistant.mark_interrupted()
                    self.speaker.interrupt()
                    self.speaker.wait()
                    self.speaker.resume()
                    self.l.notice("interrupted", "Stopped talking")
                self.l.level(0.0)
                self.l.activity("")
                self.l.assistant_end()
            return interrupted.is_set()

    def _typed(self, text: str):
        if not self._ready.is_set():
            self.l.error("Still starting up, give me a moment.")
            return
        self.l.user(text, "typed")
        self._reply(text, speak=self.speak_typed)
        # Back to whatever the voice loop is doing (typed replies don't change it).
        self._set_state("off" if not self._listening.is_set() else self._voice_state, voice=False)

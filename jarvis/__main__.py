"""Command-line entry point. (The desktop app is jarvis.pyw / jarvis.gui.)

    python -m jarvis            voice in the terminal: say "Hey Jarvis", then talk
    python -m jarvis --text     typed chat (add --speak to hear replies)
"""
import argparse
import logging
import shutil
import sys

from .config import settings
from .engine import Engine, Listener


class ConsoleListener(Listener):
    """Shows the engine's events in the terminal."""

    def __init__(self):
        self._live = False

    def _clear_live(self):
        if self._live:
            print("\r\x1b[2K", end="", flush=True)
            self._live = False

    def state(self, state):
        if state == "sleeping":
            self._clear_live()
            print("\x1b[2m(sleeping - say the wake word)\x1b[0m", flush=True)
        elif state == "off":
            print("\x1b[2m(microphone off)\x1b[0m", flush=True)

    def status(self, text):
        print(text, flush=True)

    def user_partial(self, text):
        if not text:
            self._clear_live()
            return
        # One line only: erasing a line clears one row, so wrapped text would leave copies.
        width = shutil.get_terminal_size((100, 20)).columns - 1
        line = f"you> {text}"
        line = line if len(line) <= width else "you> ..." + text[-(width - 8):]
        print(f"\r\x1b[2K{line}", end="", flush=True)
        self._live = True

    def user(self, text, source):
        self._clear_live()
        if source == "voice":
            print(f"you> {text}", flush=True)

    def assistant_start(self):
        print(f"{settings.name.lower()}> ", end="", flush=True)

    def assistant_delta(self, chunk):
        print(chunk, end="", flush=True)

    def assistant_end(self):
        print(flush=True)

    def notice(self, kind, text):
        print(f"\n\x1b[1m[{kind}]\x1b[0m {text}", flush=True)

    def error(self, text):
        print(f"\n[error] {text}", flush=True)


def voice_mode():
    import threading

    engine = Engine(ConsoleListener())
    engine.start()
    print(f'Starting {settings.name}... say "Hey {settings.name}" once it\'s ready. Ctrl+C to quit.\n')
    threading.Event().wait()  # the engine runs in the background until Ctrl+C


def text_mode(speak: bool):
    """Typed chat. With --speak, replies are spoken and reminders/Claude announcements play."""
    from .assistant import Assistant
    from .llm import available_providers

    assistant = Assistant(available_providers())
    speaker = None
    if speak:
        from .engine import make_speaker
        speaker = make_speaker()
    print(f"{settings.name} ready (text mode). Ctrl+C to quit.\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user:
            continue
        print(f"{settings.name.lower()}> ", end="", flush=True)
        try:
            if speaker:
                speaker.stream(assistant.respond(user), echo=True)
                print()
                speaker.wait()
            else:
                for chunk in assistant.respond(user):
                    print(chunk, end="", flush=True)
                print()
        except Exception as e:
            print(f"\n[error] {e}")


def main():
    ap = argparse.ArgumentParser(prog="jarvis")
    ap.add_argument("--text", action="store_true", help="typed chat instead of voice")
    ap.add_argument("--speak", action="store_true", help="in text mode, also speak replies")
    ap.add_argument("--google-login", action="store_true", help="connect Google Calendar and Gmail (one-time)")
    ap.add_argument("--classroom-login", action="store_true", help="connect Google Classroom (one-time)")
    ap.add_argument("--install-claude-hooks", action="store_true", help="make Claude Code sessions report to Jarvis")
    ap.add_argument("--uninstall-claude-hooks", action="store_true", help="remove Jarvis's Claude Code hooks")
    ap.add_argument("--doctor", action="store_true", help="check every part of the assistant and explain problems")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="[%(levelname)s] %(message)s")

    if args.doctor:
        from .doctor import main as doctor
        doctor()
        return
    if args.classroom_login:
        from .classroom import login as classroom_login
        classroom_login()
        return
    if args.google_login:
        from .google_tools import login
        login()
        return
    if args.install_claude_hooks or args.uninstall_claude_hooks:
        from . import claude_hooks_setup
        claude_hooks_setup.install() if args.install_claude_hooks else claude_hooks_setup.uninstall()
        return
    try:
        text_mode(args.speak) if args.text else voice_mode()
    except (KeyboardInterrupt, EOFError):
        print("\nShutting down.")
    except Exception as e:
        sys.exit(f"Startup failed: {e}")


if __name__ == "__main__":
    main()

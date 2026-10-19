# Jarvis: Module Overview

Jarvis is a Python voice assistant for Windows. You say "Hey Jarvis", it transcribes your speech, sends it to an LLM that can call tools, and speaks the answer. For setup and usage see [README.md](README.md). This file describes what each module does.

## Flow

```
mic -> wake word -> speech-to-text -> Assistant -> LLM provider (Gemini / Groq)
                                          |             |
                                          |        tool calls
                                          v             v
                                     text-to-speech   tools: memory, reminders,
                                                      calendar, email, Claude status
```

## Core (`jarvis/`)

| File | Purpose |
|---|---|
| `__main__.py` | Entry point. Modes: wake word (default), push-to-talk (`--ptt`), typed chat (`--text`). Also handles `--google-login` and `--install-claude-hooks` / `--uninstall-claude-hooks`. Strips "Hey Jarvis" from utterances, ends the conversation on sign-offs like "that's all", and starts the background reminder and Claude Code listeners. |
| `assistant.py` | Conversation loop. Builds the system prompt (persona, current time, remembered facts), streams the reply, runs tool calls (up to 5 rounds per turn) and keeps the last 40 messages. Falls back to the next LLM provider only if the first fails before producing output. A failed turn is rolled back. |
| `config.py` | Loads settings from `.env` into a single `settings` object (API keys, models, voice, email provider, bridge port, wake threshold). Also loads `vocabulary.txt`. |
| `tools.py` | Tool registry. The `@tool` decorator registers a function for the LLM; `run_tool` executes it and returns errors as data. `CONTEXT` holds the user's latest words so tools can verify real consent. Defines `get_current_datetime`. |
| `store.py` | SQLite storage (`data/jarvis.db`) with `facts` and `reminders` tables. Uses a short-lived connection per call under a lock, so it is safe across threads. |
| `memory.py` | Long-term memory. Tools `remember` and `forget`. Up to 100 facts are injected into every system prompt. |
| `reminders.py` | Tools `set_reminder`, `list_reminders`, `cancel_reminder`. A background thread polls every 5 seconds, then speaks the reminder and shows a Windows toast. Reminders missed while Jarvis was off are announced at startup. |
| `email_tools.py` | Tools to list unread mail, read, draft and send. Backends: Yahoo (IMAP/SMTP with an app password) or Gmail. Reading never marks mail as read. Sending is guarded in code: the draft must come from an earlier turn, and the user's latest words must be a clear yes. |
| `google_tools.py` | Google OAuth login, calendar tools (`calendar_list_events`, `calendar_create_event`) and the Gmail backend used by `email_tools`. |
| `claude_bridge.py` | Local HTTP server on 127.0.0.1 (default port 8765) that receives Claude Code hook events. Announces when a long task finishes or needs permission, and provides the `claude_code_status` tool. Writes a heartbeat file so the hook knows whether Jarvis is running. |
| `claude_hooks_setup.py` | Adds or removes Jarvis's hooks in `~/.claude/settings.json`, backing it up first. Touches only entries that run `notify_jarvis.py`. |

## LLM providers (`jarvis/llm/`)

| File | Purpose |
|---|---|
| `base.py` | Provider-neutral types (`ToolCall`, `Done`, `Provider` protocol) and the plain-dict history format. |
| `gemini.py` | Gemini provider: streams text and tool calls. |
| `groq_provider.py` | Groq provider: same interface, used as the fallback. |
| `__init__.py` | Builds providers and orders them by `LLM_PROVIDER`. |

## Speech (`jarvis/speech/`)

| File | Purpose |
|---|---|
| `mic.py` | One microphone stream kept open for the whole session, shared by wake-word detection and speech recognition so no audio is lost between them. |
| `wake.py` | openWakeWord "hey jarvis" detection on 80 ms audio frames. |
| `stt.py` | Speech-to-text. Live text from local faster-whisper; final transcript from Groq Whisper (if a key is set) or the local model. Energy-based end-of-speech detection, hallucination filtering and snapping to the vocabulary file. |
| `tts.py` | Text-to-speech with Piper (Windows SAPI fallback). Starts speaking on the first sentence while the LLM is still generating. Also plays the listening and sleeping chimes. |

## Other

| Path | Purpose |
|---|---|
| `hooks/notify_jarvis.py` | The Claude Code hook script. Forwards prompt-submit, stop and notification events to the bridge. Standard library only, exits at once if Jarvis isn't running. |
| `models/` | Piper voice and openWakeWord models. |
| `data/` | SQLite database (memory and reminders). |
| `vocabulary.txt` | Names and jargon that speech recognition should expect. |
| `.env`, `.env.example` | Secrets and optional settings. |
| `credentials.json`, `token.json` | Google OAuth files. Keep private. |

## Notes

- With `STT_ENGINE=groq` (the default), voice clips are sent to Groq for the final transcript. Set `STT_ENGINE=local` to keep everything on the PC.
- `.env`, `credentials.json` and `token.json` grant access to accounts. The folder is not a git repo yet, so confirm `.gitignore` covers them before running `git init`.

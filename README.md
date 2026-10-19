# F.R.I.D.A.Y. (the "jarvis" project)

A voice assistant that runs on your PC. Say "Friday", talk naturally, and she answers out loud. She searches the web, checks the weather, remembers things about you, sets reminders, handles your Google Calendar and your email (Yahoo Mail or Gmail), and tells you when your Claude Code sessions finish. The code and folder are still called `jarvis`. The assistant's name is a setting (`ASSISTANT_NAME`).

- **Brain:** Gemini (free tier), with Groq as automatic fallback
- **Ears:** openWakeWord for the wake word. Whisper turns speech into text: live words locally, the final transcript on Groq.
- **Voice:** your ElevenLabs voice, with Kokoro or Piper as a local backup
- **Web:** DuckDuckGo search and news, page reading, and Open-Meteo weather. All free, no keys.
- **Memory and reminders:** SQLite at `data/jarvis.db`

## Run it

**Desktop app (recommended):** double-click `jarvis.pyw`, or run:
```
.venv\Scripts\pythonw.exe jarvis.pyw
```
A HUD-style window opens and her icon appears among your hidden tray icons. The animated core's colour shows her state: amber means standby, teal means listening, violet means thinking, bright amber means speaking, and grey means the mic is off. The ring of bars moves with whoever is talking.

- **Tray icon:** left-click to show or hide the window. Right-click for *Listening on/off*, *Start with Windows*, and *Quit*.
- **Closing the window** hides it to the tray, and she keeps running. Quit from the tray menu.
- **MIC LIVE / MIC OFF** releases the microphone completely. Reminders and Claude announcements still play.
- **Pages:** Chat (with a box for typing), Reminders, Memory (add or forget facts), Claude (session status), and Settings.
- **Settings → Diagnostics** checks every part (keys, models, mic, web, Google, email, hooks) and explains anything broken. `python -m jarvis --doctor` does the same in a terminal.
- **Settings → Add to Start Menu** makes it launchable and pinnable like any app.
- Logs go to `data/jarvis.log`.

**Terminal versions:**
```
.venv\Scripts\python -m jarvis            # voice, in the terminal
.venv\Scripts\python -m jarvis --text     # typed chat (add --speak to hear replies)
.venv\Scripts\python -m jarvis --doctor   # self-check
```
Add `-v` to the terminal versions to see what she's doing (tool calls, model fallbacks). Only run one copy at a time, either the app or the terminal, because they share the microphone and the Claude bridge.

### Talking to her

- **"Friday, what's the time?"** Say the request in the same breath, or pause for the rising chime. Say just "Friday": the current wake word model was trained on the bare name.
- **"Friday" mid-sentence is ignored** ("see you on Friday"). She only wakes when you say her name after a pause.
- **Interrupt her** by saying "Friday" while she's talking. She stops and listens. This works best with headphones.
- **Follow-ups need no wake word.** After each answer she listens for a few seconds.
- **"That's all", "thanks" or "goodbye"** ends the conversation. Silence does too. A falling chime means she's on standby again.
- **Web:** "What's the latest on the Pakistan cricket team?" "Who founded Groq?" "What does that article say?" She searches, reads a page if she needs to, and names her source.
- **Weather:** "Will it rain in Islamabad tomorrow?"
- **Memory:** "Remember my sister's name is Ayesha." "Forget that." "What do you know about me?"
- **Reminders:** "Remind me in 20 minutes to stretch." "What reminders do I have?" "Cancel the stretch reminder." When a reminder is due she speaks it and shows a Windows notification. Reminders missed while she was off are announced at the next start.
- **Calendar:** "What's on tomorrow?" "Add dentist on Thursday at 3." Needs Google connected, see below.
- **Email:** "Any important emails?" "Read the one from Ali." "Reply saying I'll be there." She reads the draft back and only sends after you say a clear "yes". This rule is enforced in code, not just requested of the AI.

## Settings

Most of these can be changed in the app (Settings → Save and restart). Secrets live in `.env`, and `.env.example` lists every key:

| Setting | Default | What it does |
|---|---|---|
| `GEMINI_API_KEY`, `GROQ_API_KEY` | | API keys. At least one is required, and both is best. |
| `ASSISTANT_NAME` | `Jarvis` | Her name: window, tray, personality. Set to `Friday`. |
| `WAKE_MODEL` | (auto) | Wake word file in `models/openwakeword`. By default a file with her name in it, otherwise "hey jarvis". |
| `WAKE_THRESHOLD` | `0.5` | Lower wakes more easily, with more false wakes |
| `WAKE_REQUIRE_PAUSE` | `1` | Ignore her name mid-sentence |
| `BARGE_IN` | `1` | Saying her name while she talks interrupts her |
| `FOLLOWUP_SECONDS` | `6` | How long she waits for a follow-up |
| `TTS_ENGINE` | `piper` | `elevenlabs`, `kokoro`, `piper` or `sapi` |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | | Your ElevenLabs voice |
| `ELEVENLABS_MODEL` | `eleven_flash_v2_5` | `eleven_multilingual_v2` sounds richest but is slower and costs twice the credits |
| `KOKORO_VOICE` | `bm_george` | The local voice, and the backup when ElevenLabs is unavailable |
| `TTS_SPEED` | `1.1` | Pace of the local voices (higher is slower) |
| `STT_ENGINE` | `groq` | `groq` gives an accurate final transcript but sends voice clips to Groq. `local` keeps everything on this PC. |
| `GEMINI_MODEL` | `gemini-flash-lite-latest` | Full Flash is smarter but only allows ~5 requests/min free |
| `LLM_PROVIDER` | `gemini` | Which brain to try first |
| `BRIEFING_TIME`, `BRIEFING_LOCATION` | (off), `Islamabad` | Automatic daily briefing time (24-hour HH:MM) and weather city |
| `CLAUDE_TASKS`, `CLAUDE_PROJECTS`, `CLAUDE_TASK_MODEL`, `CLAUDE_TASK_MINUTES` | `1`, jarvis + fedcard, `sonnet`, `10` | Voice-launched Claude tasks |

**Misheard names?** Add them to `vocabulary.txt`, which covers places, people and jargon.

**Audio with Bluetooth earbuds:** keep Windows' input on the laptop **Microphone Array**, not "Headset (your earbuds)". Using the earbud mic forces Bluetooth into call mode, which makes everything sound worse.

## Daily briefing

Say "Friday, good morning" (or "brief me", "what does my day look like?") and she gives you a short rundown: weather, today's calendar, assignments (overdue and due soon), recent unread email and today's reminders. It also works by typing and from the phone. For an automatic one, set a time under **Settings → Daily briefing** (or `BRIEFING_TIME=07:30`). If the PC was off at that time, you get it once when she starts, up until noon. **BRIEF ME NOW** in Settings tests it.

## Claude Code tasks by voice

"Friday, ask Claude to find out why the tests fail in fedcard." She prepares the task, **reads the plan back** (the project, what Claude will do, and whether it may change files), and starts it only after a clear spoken yes. She tells you when it's done, and you can ask "how's Claude getting on?" or say "cancel the Claude task".

- **Read-only by default:** Claude can read and search files and check git history. Anything else is refused. Say you want files changed ("...and fix it") and she asks again, naming that, before it may create and edit files and run tests. Even then it **cannot delete files** or run `git push`, `reset`, `clean` or `checkout`.
- **Allowed projects only:** `CLAUDE_PROJECTS=jarvis=C:\jarvis;fedcard=C:\fedcard`.
- **One task at a time,** with a time limit (`CLAUDE_TASK_MINUTES`, default 10) and a cancel.
- **Your Pro login only:** any `ANTHROPIC_API_KEY` is removed from its environment, so it can't bill an API account.
- **Fresh session each time:** Claude doesn't remember your other conversations, so describe the job fully.
- **It uses your Claude plan's usage**, the same pool as your own sessions. Coding tasks use far more than chat. Pick a lighter model in Settings (`CLAUDE_TASK_MODEL=haiku`), or switch the feature off (`CLAUDE_TASKS=0`).
- **One-time sign-in:** the command-line Claude Code needs its own login: run `claude auth login` once. Diagnostics shows whether it's signed in.

## Backups

The project is a git repository. `.env`, the Google files, `models/` and `data/` are ignored, so the code can be pushed to a private GitHub repo without leaking secrets: `git remote add origin <url>` then `git push -u origin main`.

## Connect Yahoo Mail

Yahoo has no public mail API, so Jarvis uses standard IMAP (reading) and SMTP (sending) with an **app password**: a separate password just for Jarvis, which you can revoke at any time without affecting your normal login.

1. Sign in at [login.yahoo.com](https://login.yahoo.com), then open **Account Info → Account Security**.
2. Choose **Generate app password** (or "Manage app passwords"), name it "Jarvis", and copy the password Yahoo shows.
3. Add both lines to `.env` (spaces in the password are fine):
   ```
   YAHOO_EMAIL=you@yahoo.com
   YAHOO_APP_PASSWORD=abcd efgh ijkl mnop
   ```
4. Restart Jarvis and ask "Do I have any unread emails?"

Reading never marks your mail as read. Drafts stay inside Jarvis until you say yes, so nothing goes to your Yahoo Drafts folder, and an unsent draft is gone once Jarvis restarts. If both Yahoo and Gmail are configured, Yahoo is used. You can override this with `EMAIL_PROVIDER=gmail`.

## Connect Google Classroom

Friday can see your classes, what's assigned, when it's due and whether you've turned it in. She **reminds you 24 hours and 2 hours before** each unfinished assignment is due: spoken, on Windows, and pushed to your phone. Those reminders disappear once you hand the work in. Ask "What's due this week?", "Do I have any homework for physics?" or "Did I submit the lab report?". Her access is read-only: she can't change or submit anything.

1. In the [Google Cloud console](https://console.cloud.google.com), using the same project as Calendar, go to **APIs & Services → Library** and enable the **Google Classroom API**.
2. If your Classroom is on a **different account** (e.g. your school account), add that account under **OAuth consent screen → Audience → Test users**.
3. In Friday's window, go to **Settings → CONNECT GOOGLE CLASSROOM** (or run `python -m jarvis --classroom-login`) and sign in with the account your Classroom is on.

This login is stored separately from Calendar's, in `token_classroom.json`, which is git-ignored. Change the reminder timing under Settings → Google Classroom (`CLASSROOM_REMIND_HOURS`, default `24,2`). Some schools block outside apps from their Google accounts. If Google says access is blocked by your school, only the school's IT admin can allow it.

## Connect Google Calendar and Gmail (one-time)

Gmail only works if your Google account has a Gmail inbox. An account registered with another address, such as Yahoo, gets Calendar but no Gmail. Use Yahoo Mail above for that case.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project, for example "Jarvis".
2. Under **APIs & Services → Library**, enable **Google Calendar API** and **Gmail API**.
3. Set up the **OAuth consent screen** (Google Auth Platform): choose **External**, give it any app name, and use your own email. Under **Audience / Test users**, add your Gmail address.
4. Under **Credentials → Create credentials → OAuth client ID**, choose application type **Desktop app**, then download the JSON file.
5. Save the file as `C:\jarvis\credentials.json`.
6. Run `.venv\Scripts\python -m jarvis --google-login` and approve access in the browser. Google will warn that the app isn't verified. That's expected, because it's your own app, so click **Continue**.

This creates `token.json`. Both files are git-ignored. Don't share them, because they grant access to your mail and calendar.

While the app is in Google's "Testing" mode, the login expires about every 7 days. When Jarvis says Google isn't connected, run `--google-login` again.

## Voice

Pick a voice in **Settings → Voice** and press **Preview** to hear it before switching.
- **Kokoro** (George, Lewis, Daniel, Fable) sounds natural, but starts speaking about 1 second after a reply begins. Its model files live in `models/kokoro/` and come from the [kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0): `kokoro-v1.0.int8.onnx` and `voices-v1.0.bin`.
- **Piper (Alan)** is more robotic but nearly instant.

**Mispronounced names:** add them to `pronunciations.txt` as `word = how it sounds`, for example `Annas = Uh-nuss`. Test them with **Settings → Say it**. Changes apply straight away, with no restart.

## Claude Code notifications

Jarvis can tell you, out loud, when a Claude Code session finishes a task or needs your permission, and answer "what's Claude doing?". It's free, because it only reports on sessions you're already running.

```
.venv\Scripts\python -m jarvis --install-claude-hooks     # once
.venv\Scripts\python -m jarvis --uninstall-claude-hooks   # to remove
```

This adds three small hooks (prompt sent, Claude finished, Claude needs you) to `~/.claude/settings.json`, after backing it up as `settings.json.bak-jarvis`. Each hook forwards the event to Jarvis on `127.0.0.1`, which only your PC can reach. When Jarvis isn't running, the hook exits immediately (about 0.1 s) and Claude carries on as normal.

- **Finished:** "Claude finished in fedcard. It says: Fixed the login bug…". Replies that took under 30 seconds are skipped (`CLAUDE_ANNOUNCE_MIN_SECONDS`).
- **Needs you:** "Claude needs your permission in jarvis."
- **Ask:** "Hey Jarvis, what's Claude doing?"

Each announcement is also shown as a Windows notification.

## Project layout

```
jarvis.pyw         desktop app launcher (relaunches itself in .venv)
jarvis/
  gui.py           HUD window, tray icon, settings, diagnostics dialog
  engine.py        the voice loop: wake word, listening, replies, interrupting
  assistant.py     personality, system prompt, tool-calling loop
  llm/             Gemini and Groq providers with fallback
  tools.py         tool registry
  web_tools.py     web search, news, page reading, weather
  memory.py        long-term facts
  reminders.py     reminders and the background scheduler
  google_tools.py  Google Calendar, plus the Gmail backend
  classroom.py     Google Classroom assignments and due-date reminders
  briefing.py      the daily briefing tool and its schedule
  claude_tasks.py  voice-launched Claude Code tasks (confirmation, modes, limits)
  email_tools.py   email tools (Yahoo or Gmail) and the send safeguard
  claude_bridge.py Claude Code event listener and status tool
  remote/          phone app: server, web app, Tailscale, push notifications
  doctor.py        self-check (--doctor / Settings -> Diagnostics)
  winstartup.py    start with Windows, Start Menu shortcut
  store.py         SQLite
  speech/          microphone, wake word, speech-to-text, text-to-speech
hooks/
  notify_jarvis.py the Claude Code hook (forwards events to the assistant)
models/            voice, speech and wake word models (downloaded)
data/              memory and reminders database, logs, icon
pronunciations.txt how to say names ("Annas = Uh-nuss")
vocabulary.txt     words speech recognition should expect
```

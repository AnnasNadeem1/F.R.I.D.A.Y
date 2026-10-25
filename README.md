# F.R.I.D.A.Y. (the `jarvis` project)

A voice assistant and study coach that runs on a Windows PC and costs nothing to run. Say "Friday", talk naturally, and she answers out loud. She searches the web, remembers things about you, sets reminders, reads your Google Calendar, Classroom deadlines and email, runs focus sessions and tells you off for opening YouTube, controls your PC, tells you what your Claude Code sessions are doing, and works from your phone too.

The code and folder are called `jarvis`. The assistant's name is a setting (`ASSISTANT_NAME`).

## What she does

| Area | Details |
|---|---|
| **Voice** | Wake word ("Friday"), natural conversation, live transcription as you speak, interrupt her by saying her name. ElevenLabs voice with a local Kokoro backup. |
| **Brain** | Gemini (free tier) with Groq as automatic fallback. If the first model is slow, the next starts and the first answer wins. |
| **Memory** | Remembers facts about you, keeps a to-do list and notes. All in a local SQLite database. |
| **Reminders** | Spoken and Windows notification. Missed ones are announced at the next start. |
| **Calendar, email** | Google Calendar, plus Yahoo Mail or Gmail. Email is only sent after she reads the draft back and you say a clear yes, enforced in code. |
| **School** | Google Classroom: what's due, what's overdue, automatic reminders 24 h and 2 h before each unfinished assignment. |
| **Study coach** | Focus sessions with a log, daily totals and streaks, distraction watch, a morning day plan and an evening check-in, and a persona that pushes back when you want to skip. |
| **PC control** | Open apps and sites, media keys and volume, lock the screen, describe your screen, clipboard, battery and memory. |
| **Claude Code** | Tells you when a session finishes or needs you, and can start read-only or edit tasks on your Claude plan, after your spoken yes. |
| **Web** | DuckDuckGo search and news, page reading, Open-Meteo weather. No keys needed. |
| **Phone** | A private web app over Tailscale: talk to her, get push notifications. |
| **Quiet mode** | Quiet hours and a QUIET button: she stays silent and notifies you instead. |

## Quick start

Requirements: Windows 10 or 11, Python 3.13, a microphone.

```
git clone https://github.com/AnnasNadeem1/F.R.I.D.A.Y
cd F.R.I.D.A.Y
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`. A free [Gemini](https://aistudio.google.com/apikey) key or [Groq](https://console.groq.com/keys) key is the minimum; both is best. Then download the models (they're large and git-ignored):

```
# wake word (put a custom "Friday" model in the same folder to wake on that name)
.venv\Scripts\python -c "from openwakeword import utils; utils.download_models(['hey_jarvis'], target_directory='models/openwakeword')"
# offline voice
.venv\Scripts\python -m piper.download_voices en_GB-alan-medium --data-dir models
```

For the Kokoro voice, put `kokoro-v1.0.int8.onnx` and `voices-v1.0.bin` (optionally `kokoro-v1.0.onnx` for full quality) from the [kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0) into `models/kokoro/`.

Run it:

```
.venv\Scripts\pythonw.exe jarvis.pyw        # desktop app (or just double-click jarvis.pyw)
.venv\Scripts\python -m jarvis --text       # typed chat in the terminal (add --speak to hear replies)
.venv\Scripts\python -m jarvis --doctor     # checks every part and explains problems
```

Only run one copy at a time, because they share the microphone.

## The desktop app

A HUD-style window plus a tray icon. The core's colour shows her state: amber is standby, teal listening, violet thinking, bright amber speaking, grey mic off. Closing the window hides it to the tray; quit from the tray menu.

- **Pages:** Chat, Study, Reminders, Memory, Claude, Phone, Settings.
- **Study** shows today's minutes against your goal, the last 7 days, your streak, how often you were caught drifting, a 14-day chart, recent sessions and your to-do list. You can start and stop focus sessions there.
- **MIC LIVE / MIC OFF** releases the microphone completely. **QUIET** silences her for two hours.
- **Settings** covers voice, wake word, Classroom, briefing, Claude tasks and the study options below. Saving restarts the app. **Diagnostics** and **Add to Start Menu** are at the bottom.
- Logs go to `data/jarvis.log`.

## Talking to her

- **Wake:** "Friday, what's the time?" in one breath, or say just "Friday" and wait for the chime. "See you on Friday" mid-sentence is ignored.
- **Follow-ups need no wake word** for a few seconds. "Thanks" or "that's all" ends the conversation.
- **Interrupt** by saying "Friday" while she talks (best with headphones).
- **Memory:** "Remember my sister's name is Ayesha." "Forget that." "What do you know about me?"
- **Reminders:** "Remind me in 20 minutes to stretch." "What reminders do I have?"
- **Web:** "What's the latest on the Pakistan cricket team?" "Will it rain in Islamabad tomorrow?"
- **To-do and notes:** "Add buy a charger to my to-do list." "What's on my list?" "I finished the charger." "Note that the lab is on Thursday."
- **School:** "What's due this week?" "Did I submit the lab report?"
- **Briefing:** "Good morning" gives weather, today's calendar, assignments, recent email and reminders.

## Study coach

- **Focus sessions:** "I'm studying deep learning for 45 minutes." A timer runs, the time is logged, and she announces when it ends. Say "I'm allowed YouTube" for lectures.
- **Distraction watch:** during a session (and in `STUDY_HOURS`, if set) she checks which window is in front and nags, a little firmer each time, when it matches `distractions.txt` (YouTube, games and so on). It reads the program name and window title on your PC only: titles are never stored or sent anywhere, only a count of how often you drifted.
- **Daily plan and check-in:** "Plan my day" builds an ordered plan from your calendar, deadlines, to-dos and yesterday's results, and saves it. In the evening she compares it with what happened and asks what to move to tomorrow. Set `PLAN_TIME` and `CHECKIN_TIME` to have her start these herself.
- **Accountability:** when you say you want to skip studying, she tells you to do it and keeps pressing, easing off only for real reasons like illness.

## PC control

"Open Chrome", "go to github.com", "pause", "next song", "turn the volume down", "mute", "set the volume to 40", "lock my PC", "what's on my screen?", "copy that to the clipboard", "how's my battery?".

Apps are found through the Start Menu. Looking at the screen takes one screenshot of your main display and sends it to Gemini, and only when you ask. Locking, screen and clipboard actions only run if your own message asks for them, which stops a web page or an email from triggering them.

## Quiet hours and quiet mode

`QUIET_HOURS=23:00-07:00` (or the QUIET button, or "go quiet for an hour") makes her speak only when spoken to. Reminders, Claude alerts and focus messages appear as notifications and chat notes, and the scheduled briefing, plan and check-in wait. A focus session you start yourself still gets its nags during quiet hours. Quiet mode silences everything until it ends.

## Settings

Most of these can be changed in the app. Secrets live in `.env`, and `.env.example` lists every key.

| Setting | Default | What it does |
|---|---|---|
| `GEMINI_API_KEY`, `GROQ_API_KEY` | | At least one is required |
| `ASSISTANT_NAME` | `Jarvis` | Her name (window, personality) |
| `WAKE_MODEL`, `WAKE_THRESHOLD` | auto, `0.5` | Wake word file and sensitivity |
| `WAKE_REQUIRE_PAUSE`, `BARGE_IN`, `FOLLOWUP_SECONDS` | `1`, `1`, `6` | Mid-sentence filter, interrupting, follow-up window |
| `TTS_ENGINE` | `piper` | `elevenlabs`, `kokoro`, `piper` or `sapi` |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` | | Your ElevenLabs voice. Flash costs half the credits of Multilingual v2. |
| `KOKORO_VOICE` | `bm_george` | Local voice and ElevenLabs backup. A mix works: `af_bella:1,af_nicole:1` |
| `KOKORO_PAUSE_CAP`, `KOKORO_MODEL` | `0.16`, `int8` | Comma pause length, and `int8` or `fp32` quality |
| `STT_ENGINE` | `groq` | `groq` is accurate but sends voice clips to Groq; `local` stays on the PC |
| `LLM_PROVIDER`, `GEMINI_MODEL` | `gemini`, `gemini-flash-lite-latest` | Which brain first |
| `BRIEFING_TIME`, `BRIEFING_LOCATION` | off, `Islamabad` | Automatic briefing time and weather city |
| `PLAN_TIME`, `CHECKIN_TIME` | off | Automatic day plan and evening check-in (HH:MM) |
| `STUDY_GOAL_MINUTES`, `STUDY_STREAK_MINUTES` | `120`, `25` | Daily focus goal, minutes that keep a streak |
| `DISTRACTION_WATCH`, `STUDY_HOURS` | `1`, off | Distraction nags, and extra hours to watch |
| `QUIET_HOURS` | off | Daily range when she stays silent, e.g. `23:00-07:00` |
| `CLAUDE_TASKS`, `CLAUDE_PROJECTS`, `CLAUDE_TASK_MODEL`, `CLAUDE_TASK_MINUTES` | `1`, see below, `sonnet`, `10` | Voice-launched Claude tasks |

**Files you can edit** (changes apply immediately): `pronunciations.txt` (`Annas = an-nass | ˈʌnəs`, where the part after `|` is optional exact IPA for the Kokoro voice), `vocabulary.txt` (words speech recognition should expect), `distractions.txt` (what counts as a distraction).

**Bluetooth earbuds:** keep Windows' input on the laptop microphone array, not the earbud mic. Using the earbud mic forces call mode and makes everything sound worse.

## Connecting accounts

### Yahoo Mail

Yahoo has no mail API, so she uses IMAP and SMTP with an **app password**: a separate password you can revoke at any time.

1. Sign in at [login.yahoo.com](https://login.yahoo.com), open **Account Security** and choose **Generate app password**.
2. Add to `.env`:
   ```
   YAHOO_EMAIL=you@yahoo.com
   YAHOO_APP_PASSWORD=abcd efgh ijkl mnop
   ```
3. Restart and ask "Do I have any unread emails?"

Reading never marks mail as read. Drafts live inside Friday until you say yes, so nothing goes to your Drafts folder.

### Google Calendar (and Gmail)

1. In the [Google Cloud console](https://console.cloud.google.com) create a project and enable the **Google Calendar API** (and **Gmail API** if your account has a Gmail inbox).
2. Set up the OAuth consent screen (External, add your own address as a test user).
3. Create an **OAuth client ID** of type **Desktop app**, download the JSON and save it as `credentials.json` in the project folder.
4. Run `.venv\Scripts\python -m jarvis --google-login` and approve in the browser. Google warns the app is unverified; that's expected for your own app.

This creates `token.json`. While the app is in Testing mode the login expires about every 7 days; run the login again when she says Google isn't connected.

### Google Classroom

Read-only: she can't change or submit anything.

1. Enable the **Google Classroom API** in the same project, and add your school account as a test user if it's a different account.
2. In **Settings → CONNECT GOOGLE CLASSROOM** (or `python -m jarvis --classroom-login`) sign in with the account your Classroom is on.

Reminder timing is `CLASSROOM_REMIND_HOURS` (default `24,2`). Some schools block outside apps; only the school's IT admin can allow it.

### Phone (tested on Android with Chrome)

A private web app, reachable only over your own **Tailscale** network.

1. Install Tailscale on the PC and the phone, signed in to the same account.
2. In the Tailscale admin console's DNS page, turn on MagicDNS and **Enable HTTPS**.
3. In the app: **Phone → SET UP PHONE ACCESS**.
4. Scan the QR code with the phone (it carries a private pairing key, so only scan it on your own phone) and open the link in Chrome.
5. Chrome menu → **Add to Home screen**, then **ENABLE NOTIFICATIONS** inside the app.

**UNPAIR ALL PHONES** makes a new key.

### Claude Code

```
.venv\Scripts\python -m jarvis --install-claude-hooks      # once
.venv\Scripts\python -m jarvis --uninstall-claude-hooks    # to remove
```

This adds three small hooks to `~/.claude/settings.json` (backed up first). Each forwards an event to Friday on `127.0.0.1`; if she isn't running, the hook exits instantly. She announces finished tasks and permission requests, and answers "what's Claude doing?".

**Voice-launched tasks:** "Friday, ask Claude to find out why the tests fail in fedcard." She reads the plan back (project, what Claude will do, and whether it may change files) and starts only after a clear yes.
- Read-only by default. Changing files needs a second, explicit yes, and even then it cannot delete files or run `git push`, `reset`, `clean` or `checkout`.
- Only projects listed in `CLAUDE_PROJECTS` (`name=folder;name=folder`), one task at a time, stopped after `CLAUDE_TASK_MINUTES`.
- Uses your Claude plan's login (run `claude auth login` once). Any `ANTHROPIC_API_KEY` is removed from its environment so it can't bill an API account.
- It uses your plan's usage, so pick a lighter model (`CLAUDE_TASK_MODEL=haiku`) or turn it off (`CLAUDE_TASKS=0`).

## Privacy and safety

- **Stays on your PC:** the database (`data/jarvis.db`), the distraction watch, tokens and `.env`. Git ignores `.env`, the Google files, `models/` and `data/`.
- **Sent to services:** your messages and tool results go to Gemini or Groq. With `STT_ENGINE=groq`, voice clips go to Groq for transcription. ElevenLabs gets the text she speaks. Screen descriptions go to Gemini, only when you ask.
- **Guards in code, not just in the prompt:** email needs a read-back and a clear yes. Lock, screen and clipboard actions need your own words. Email, calendar and web content is treated as data, never as instructions. Claude tasks are confirmed, limited to allowed projects, and can't delete.
- **No cost surprises:** everything runs on free tiers. Tools are only offered to the model when the conversation needs them, which keeps requests small enough for the free limits.

## Troubleshooting

- **Run the self-check:** Settings → Diagnostics, or `python -m jarvis --doctor`.
- **Slow replies:** the free tiers rate-limit. She falls back between Gemini and Groq automatically, but heavy use can still slow her down for a few minutes.
- **ElevenLabs out of credits:** she switches to the Kokoro backup on her own and retries ElevenLabs after a rest. Credits reset monthly.
- **A name sounds wrong:** add it to `pronunciations.txt` and test with Settings → Say it.
- **Nothing happens on double-click:** run `.venv\Scripts\python jarvis.pyw` in a terminal to see the error, and check `data/jarvis.log`.

## Project layout

```
jarvis.pyw         desktop app launcher (relaunches itself in .venv)
jarvis/
  gui.py           HUD window, tray icon, Study page, settings, diagnostics
  engine.py        the voice loop: wake word, listening, replies, interrupting
  assistant.py     persona, system prompt, tool-calling loop, tool routing
  llm/             Gemini and Groq providers with hedged fallback
  tools.py         tool registry
  speech/          microphone, wake word, speech-to-text, text-to-speech
  focus.py         focus sessions, study log, streaks, distraction nags
  watch.py         foreground-window check for the distraction watch
  planner.py       daily plan, evening check-in, daily scheduler
  quiet.py         quiet hours and quiet mode
  tasks.py         to-do list and notes
  pc_tools.py      apps, sites, media, lock, screen, clipboard, system status
  web_tools.py     web search, news, page reading, weather
  memory.py        long-term facts
  reminders.py     reminders and the background scheduler
  google_tools.py  Google Calendar and the Gmail backend
  email_tools.py   email tools (Yahoo or Gmail) and the send safeguard
  classroom.py     Google Classroom assignments and due-date reminders
  briefing.py      the daily briefing
  claude_tasks.py  voice-launched Claude Code tasks
  claude_bridge.py Claude Code event listener
  remote/          phone app: server, web app, Tailscale, push
  doctor.py        self-check
  store.py         SQLite
hooks/             the Claude Code hook script
pronunciations.txt, vocabulary.txt, distractions.txt
```

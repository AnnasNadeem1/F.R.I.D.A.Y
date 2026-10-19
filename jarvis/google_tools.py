"""Google Calendar tools, plus the Gmail backend used by email_tools.

Setup (once): put your OAuth client file at credentials.json in the project
root, then run `python -m jarvis --google-login` to approve access in the
browser. The resulting token.json is reused afterwards. Both files are
git-ignored; never share them.
"""
import base64
import logging
from datetime import datetime, timedelta
from email.message import EmailMessage

from .config import ROOT
from .tools import tool

log = logging.getLogger(__name__)

CREDENTIALS = ROOT / "credentials.json"
TOKEN = ROOT / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",  # create drafts and send them
]
NOT_SET_UP = (
    "Google isn't connected yet. Tell the user to put credentials.json in the C:\\jarvis folder "
    "and run: python -m jarvis --google-login"
)

_services: dict = {}


class GoogleNotSetUp(Exception):
    pass


def _credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN.exists():
        raise GoogleNotSetUp(NOT_SET_UP)
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json())
        else:
            raise GoogleNotSetUp(NOT_SET_UP)
    return creds


def login():
    """Interactive one-time consent in the browser (run from the command line)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CREDENTIALS.exists():
        raise SystemExit(f"Missing {CREDENTIALS}. See README.md, 'Connect Google Calendar and Gmail'.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    TOKEN.write_text(creds.to_json())
    print(f"Google connected. Token saved to {TOKEN.name}.")


def _service(name: str, version: str):
    if name not in _services:
        from googleapiclient.discovery import build

        _services[name] = build(name, version, credentials=_credentials(), cache_discovery=False)
    return _services[name]


def _google(fn):
    """Turn setup problems into a message the model can relay."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GoogleNotSetUp as e:
            return {"error": str(e)}
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _local(dt_str: str) -> datetime:
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.astimezone()


# ---------------------------------------------------------------- Calendar

@tool(
    "List calendar events. 'start_date' is a local date YYYY-MM-DD (default today); 'days' is how many days to cover.",
    {
        "type": "object",
        "properties": {"start_date": {"type": "string"}, "days": {"type": "integer"}},
    },
)
@_google
def calendar_list_events(start_date: str = "", days: int = 1):
    start = datetime.fromisoformat(start_date).astimezone() if start_date else datetime.now().astimezone()
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=max(1, min(days, 31)))
    items = _service("calendar", "v3").events().list(
        calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=50,
    ).execute().get("items", [])
    events = []
    for e in items:
        s = e["start"].get("dateTime") or e["start"].get("date")
        events.append({
            "title": e.get("summary", "(no title)"),
            "start": s,
            "all_day": "date" in e["start"],
            "location": e.get("location", ""),
        })
    return {"events": events}


@tool(
    "Add an event to the user's Google Calendar. 'start' is local ISO 8601 date-time "
    "(e.g. 2026-10-20T15:00:00). Confirm the details back to the user after adding.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "start": {"type": "string"},
            "duration_minutes": {"type": "integer"},
            "location": {"type": "string"},
        },
        "required": ["title", "start"],
    },
)
@_google
def calendar_create_event(title: str, start: str, duration_minutes: int = 60, location: str = ""):
    s = _local(start)
    e = s + timedelta(minutes=duration_minutes or 60)
    body = {"summary": title, "start": {"dateTime": s.isoformat()}, "end": {"dateTime": e.isoformat()}}
    if location:
        body["location"] = location
    ev = _service("calendar", "v3").events().insert(calendarId="primary", body=body).execute()
    return {"status": "created", "title": title, "start": s.isoformat(timespec="minutes"), "id": ev.get("id")}


# ---------------------------------------------------------------- Gmail backend
# Used by email_tools when the Google account has a Gmail inbox. (Google accounts
# registered with another address, e.g. Yahoo, have Calendar but no Gmail.)

def _header(msg: dict, name: str) -> str:
    return next((h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower() == name.lower()), "")


def _body_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _body_text(part)
        if text:
            return text
    return ""


class GmailBackend:
    name = "Gmail"

    def list_unread(self, limit: int) -> list[dict]:
        gm = _service("gmail", "v1").users()
        ids = gm.messages().list(userId="me", q="is:unread in:inbox category:primary", maxResults=limit).execute()
        out = []
        for m in ids.get("messages", []):
            msg = gm.messages().get(userId="me", id=m["id"], format="metadata",
                                    metadataHeaders=["From", "Subject", "Date"]).execute()
            out.append({"id": m["id"], "from": _header(msg, "From"), "subject": _header(msg, "Subject"),
                        "preview": msg.get("snippet", "")})
        return out

    def read(self, id: str) -> dict:
        msg = _service("gmail", "v1").users().messages().get(userId="me", id=id, format="full").execute()
        return {"from": _header(msg, "From"), "subject": _header(msg, "Subject"), "date": _header(msg, "Date"),
                "body": _body_text(msg["payload"])}

    def send(self, msg: EmailMessage):
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        _service("gmail", "v1").users().messages().send(userId="me", body={"raw": raw}).execute()


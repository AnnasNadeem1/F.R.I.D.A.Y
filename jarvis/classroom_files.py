"""Lecture slides and handouts from Google Classroom, saved into folders on this PC.

Friday lists a class's materials through the Classroom API (read-only), then opens each file's
download link in your normal browser, where you're already signed in to the school account. The browser
does the downloading; she then moves the new file from your Downloads folder into
<CLASSROOM_FOLDER>\\<class name>\\. No Google Drive permission is needed.

Safety: downloading starts only after your spoken yes; files go only into CLASSROOM_FOLDER; nothing is
overwritten; executables and scripts are never kept; she never opens a downloaded file."""
import ctypes
import logging
import re
import shutil
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable

from .classroom import ClassroomNotSetUp, _guard, _pages, _thread_api
from .config import settings
from .email_tools import user_confirmed
from .store import db
from .tools import CONTEXT, tool

log = logging.getLogger(__name__)

MATERIALS_SCOPE = "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly"
NEEDS_RECONNECT = ("Listing lecture materials needs one more Classroom permission. Tell the user to open Settings "
                   "and press CONNECT GOOGLE CLASSROOM once more, and approve it.")
MAX_FILES = 20
WAIT_SECONDS = 60       # for one file to start and finish downloading
MAX_WAIT_SECONDS = 300  # even for a big one
ALLOWED = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".md", ".rtf", ".odt", ".odp",
           ".ods", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ipynb", ".py", ".java", ".cpp", ".c", ".h",
           ".sql", ".json", ".zip", ".mp3", ".mp4", ".mkv", ".mov", ".tex", ".epub"}
PARTIAL = (".crdownload", ".tmp", ".part", ".opdownload", ".download")
# Never kept even if a teacher attached them: anything that can run.
BLOCKED = {".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".jar", ".scr", ".lnk", ".com",
           ".dll", ".hta", ".reg", ".wsf", ".pif", ".cpl", ".msc"}

# Set by the engine: ON_DONE(text) announces the result, ON_ACTIVITY(text) shows progress in the window.
ON_DONE: Callable[[str], None] | None = None
ON_ACTIVITY: Callable[[str], None] | None = None

_pending: dict | None = None        # a plan waiting for the user's yes: {"turn", "key", "files", "folder_for"}
_batch: dict | None = None          # the running download: {"total", "done", "current", "folder"}
_lock = threading.Lock()


# ---------------------------------------------------------------- paths

def downloads_dir() -> Path:
    """The user's real Downloads folder (it can be moved), falling back to ~/Downloads."""
    try:
        class GUID(ctypes.Structure):
            _fields_ = [("data", ctypes.c_ubyte * 16)]

        guid = GUID.from_buffer_copy(uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            if path.is_dir():
                return path
    except Exception:
        log.debug("known-folder lookup failed", exc_info=True)
    return Path.home() / "Downloads"


def root_dir() -> Path:
    return Path(settings.classroom_folder) if settings.classroom_folder else Path.home() / "Documents" / "University"


def safe_name(name: str, limit: int = 80) -> str:
    """A string that's safe as one Windows file or folder name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = (name[:limit].rstrip(" .") or "untitled")
    if re.fullmatch(r"(con|prn|aux|nul|com\d|lpt\d)", name, re.I):  # reserved device names on Windows
        name = "_" + name
    return name


def short_name(course_name: str) -> str:
    """"FALL 2026 Deep Learning BSCS 7 C" -> "Deep Learning": drop the semester, program and section codes
    and anything in brackets, which is rarely part of what you'd call the class."""
    name = re.sub(r"\([^)]*\)", " ", course_name or "")
    name = re.sub(r"\b(fall|spring|summer|winter)\s*'?\d{2,4}\b", " ", name, flags=re.I)
    name = re.sub(r"\b[A-Z]{2,4}[- ]?(\d{1,2}|[IVX]{1,4})\b(\s+[A-Z](,\s*[A-Z])*)?\s*$", " ", name.strip())
    name = re.sub(r"\b[A-Z](,\s*[A-Z])+\s*$", " ", name.strip())
    cleaned = safe_name(re.sub(r"\s+", " ", name).strip(" -,"), 60)
    return cleaned if cleaned != "untitled" else safe_name(course_name, 60)


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 100):
        alt = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not alt.exists():
            return alt
    return path.with_name(f"{path.stem} ({uuid.uuid4().hex[:6]}){path.suffix}")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def matches(filename: str, expected_title: str) -> bool:
    """Does a file that appeared in Downloads look like the one we asked for? Browsers add " (1)" and
    Google adds its own extension, so compare names without punctuation or extension."""
    want = _norm(Path(expected_title).stem if Path(expected_title).suffix.lower() in ALLOWED | BLOCKED
                 else expected_title)
    got = _norm(Path(filename).stem)
    return len(want) >= 3 and len(got) >= 3 and (want in got or got in want)


# ---------------------------------------------------------------- links

def download_url(link: str, file_id: str) -> tuple[str, str]:
    """(URL that makes the browser download this file, extension the saved file will have or "").
    Google Docs, Slides and Sheets can't be downloaded as-is, so they're exported."""
    account = f"&authuser={settings.classroom_account}" if settings.classroom_account else ""
    m = re.search(r"docs\.google\.com/(document|presentation|spreadsheets)/d/([\w-]+)", link or "")
    if m:
        kind, doc_id = m.groups()
        if kind == "document":
            return f"https://docs.google.com/document/d/{doc_id}/export?format=pdf{account}", ".pdf"
        if kind == "presentation":
            return f"https://docs.google.com/presentation/d/{doc_id}/export/pdf?{account.lstrip('&')}", ".pdf"
        return f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=xlsx{account}", ".xlsx"
    return f"https://drive.google.com/uc?export=download&id={file_id}{account}", ""


# ---------------------------------------------------------------- listing

def _has_scope() -> bool:
    import json

    from .classroom import TOKEN

    try:
        return MATERIALS_SCOPE in json.loads(TOKEN.read_text(encoding="utf-8")).get("scopes", [])
    except (OSError, ValueError):
        return False


def _courses(api, name: str) -> list[dict]:
    courses = _pages(lambda tok: api.courses().list(studentId="me", courseStates=["ACTIVE"], pageToken=tok,
                                                    pageSize=50), "courses")
    if name.strip():
        courses = [c for c in courses if name.strip().lower() in c.get("name", "").lower()]
    return courses


def _parse(m: dict) -> dict | None:
    if "driveFile" in m:
        d = m["driveFile"].get("driveFile", {})
        return {"kind": "file", "id": d.get("id", ""), "title": d.get("title", "untitled"), "link": d.get("alternateLink", "")}
    if "youtubeVideo" in m:
        v = m["youtubeVideo"]
        return {"kind": "video", "id": v.get("id", ""), "title": v.get("title", "video"), "link": v.get("alternateLink", "")}
    if "link" in m:
        v = m["link"]
        return {"kind": "link", "id": v.get("url", ""), "title": v.get("title") or v.get("url", ""), "link": v.get("url", "")}
    if "form" in m:
        v = m["form"]
        return {"kind": "form", "id": v.get("formUrl", ""), "title": v.get("title", "form"), "link": v.get("formUrl", "")}
    return None


def _collect(course: dict) -> list[dict]:
    """Every attachment in one class: lecture materials plus the files attached to assignments."""
    from googleapiclient.errors import HttpError

    api, cid, found, seen = _thread_api(), course["id"], [], set()
    sources = (
        ("material", lambda tok: api.courses().courseWorkMaterials().list(courseId=cid, pageToken=tok, pageSize=100),
         "courseWorkMaterial"),
        ("assignment", lambda tok: api.courses().courseWork().list(courseId=cid, pageToken=tok, pageSize=100),
         "courseWork"),
    )
    for origin, call, key in sources:
        try:
            entries = _pages(call, key)
        except HttpError as e:
            if origin == "material" and e.resp.status == 403 and "scope" in str(e).lower():
                raise ClassroomNotSetUp(NEEDS_RECONNECT)
            log.warning("classroom files: %s of %s unreadable: %s", origin, course.get("name"), e)
            continue
        for entry in entries:
            for m in entry.get("materials", []):
                item = _parse(m)
                if not item or not item["id"] or (item["kind"], item["id"]) in seen:
                    continue
                seen.add((item["kind"], item["id"]))
                found.append({**item, "course_id": cid, "course": course.get("name", ""), "origin": origin,
                              "source": entry.get("title", ""), "posted": entry.get("creationTime", "")})
    return found


def _downloaded_ids() -> set[str]:
    with db() as c:
        return {r["file_id"] for r in c.execute("SELECT file_id FROM classroom_downloads")}


def _posted(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%a %d %b")
    except ValueError:
        return ""


def _gather(course: str, new_only: bool) -> tuple[list[dict], list[dict], list[dict]]:
    """(downloadable files, videos, links) for the matching classes."""
    if not _has_scope():
        raise ClassroomNotSetUp(NEEDS_RECONNECT)
    api = _thread_api()
    courses = _courses(api, course)
    if not courses:
        raise ClassroomNotSetUp(f"No active class matches {course!r}." if course else "No active classes found.")
    got = _downloaded_ids()
    files, videos, links = [], [], []
    for c in courses:
        for item in _collect(c):
            if item["kind"] == "file":
                if not (new_only and item["id"] in got):
                    files.append(item)
            elif item["kind"] == "video":
                videos.append(item)
            elif item["kind"] == "link":
                links.append(item)
    files.sort(key=lambda i: i["posted"], reverse=True)
    return files, videos, links


@tool(
    "List the lecture slides, handouts and other files attached in the user's Google Classroom classes (read-only; "
    "nothing is downloaded). 'course' is part of a class name (empty = all classes). By default only files not "
    "already downloaded. Use for 'what lectures are new in Deep Learning', 'what slides do I have'.",
    {"type": "object", "properties": {"course": {"type": "string"}, "new_only": {"type": "boolean"}}},
)
@_guard
def classroom_materials(course: str = "", new_only: bool = True):
    files, videos, links = _gather(course, new_only)
    return {"files": [{"title": f["title"], "class": f["course"], "from": f["source"], "posted": _posted(f["posted"])}
                      for f in files[:40]],
            "file_count": len(files),
            "videos": [{"title": v["title"], "class": v["course"]} for v in videos[:10]],
            "links": [{"title": v["title"], "class": v["course"]} for v in links[:10]]}


# ---------------------------------------------------------------- downloading

def _folder_for(course_id: str, course_name: str, folder: str) -> Path:
    """The class's folder under the root. A name given once is remembered for next time."""
    with db() as c:
        if folder.strip():
            c.execute("INSERT OR REPLACE INTO classroom_folders (course_id, folder) VALUES (?, ?)",
                      (course_id, safe_name(folder, 60)))
        row = c.execute("SELECT folder FROM classroom_folders WHERE course_id = ?", (course_id,)).fetchone()
    return root_dir() / (row["folder"] if row else short_name(course_name))


def _list_dir(path: Path) -> dict[str, int]:
    try:
        return {p.name: p.stat().st_size for p in path.iterdir() if p.is_file()}
    except OSError:
        return {}


def _fetch_one(item: dict, dest_dir: Path, downloads: Path) -> tuple[Path | None, str]:
    """Open the download link, wait for the browser to finish, move the file into place."""
    url, export_ext = download_url(item["link"], item["id"])
    expected = item["title"] if export_ext == "" else item["title"] + export_ext
    if Path(expected).suffix.lower() in BLOCKED:
        return None, "it's a program or script, which I don't download"
    before = set(_list_dir(downloads))
    webbrowser.open(url)
    start = time.time()
    deadline = start + WAIT_SECONDS
    sizes: dict[str, int] = {}
    while time.time() < min(deadline, start + MAX_WAIT_SECONDS):
        time.sleep(1.5)
        now = _list_dir(downloads)
        for name, size in now.items():
            if name in before or not matches(name, item["title"]):
                continue
            if name.lower().endswith(PARTIAL):  # still downloading: keep waiting while it grows
                if size != sizes.get(name):
                    deadline = max(deadline, time.time() + 20)
                sizes[name] = size
                continue
            if size == 0 or sizes.get(name) != size:  # not finished until its size stops changing
                sizes[name] = size
                continue
            ext = Path(name).suffix.lower()
            source = downloads / name
            if ext in BLOCKED or ext not in ALLOWED:
                return None, f"files of type {ext or 'unknown'} aren't downloaded (left in Downloads)"
            target = _unique(dest_dir / (safe_name(Path(name).stem, 100) + ext))
            if not _inside(root_dir(), target):
                return None, "that folder isn't allowed"
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            return target, ""
    return None, ("the browser didn't finish a download. Is it signed in to your school account"
                  + (f" ({settings.classroom_account})" if settings.classroom_account else "") + "?")


def _run_batch(files: list[dict], folders: dict[str, Path]):
    global _batch
    downloads = downloads_dir()
    ok, failed = [], []
    try:
        for i, item in enumerate(files, 1):
            _batch.update(done=i - 1, current=item["title"])
            if ON_ACTIVITY:
                ON_ACTIVITY(f"Downloading {item['title']} ({i} of {len(files)})")
            try:
                path, why = _fetch_one(item, folders[item["course_id"]], downloads)
            except Exception as e:
                log.exception("classroom download failed")
                path, why = None, f"{type(e).__name__}"
            if path:
                ok.append(path)
                with db() as c:
                    c.execute("INSERT OR REPLACE INTO classroom_downloads (file_id, course_id, title, path) VALUES (?,?,?,?)",
                              (item["id"], item["course_id"], item["title"], str(path)))
            else:
                failed.append((item["title"], why))
    finally:
        where = ", ".join(sorted({p.parent.name for p in ok})) or "nowhere"
        text = f"Downloaded {len(ok)} of {len(files)} files into {where}."
        if failed:
            text += f" Couldn't get {failed[0][0]}: {failed[0][1]}" + (f", and {len(failed) - 1} more." if len(failed) > 1 else ".")
        log.info("classroom batch: %s", text)
        _batch = None
        if ON_ACTIVITY:
            ON_ACTIVITY("")
        if ON_DONE:
            ON_DONE(text)


@tool(
    "Download new lecture files from Google Classroom into folders on the PC. Two steps, like email: the first call "
    "returns a plan, which you read back (how many files, which class, which folder) and ask 'Shall I download "
    "them?'; only after the user says yes, call it again with the same arguments. 'course' is part of a class "
    "name (empty = all classes). 'folder' optionally names the class folder, e.g. 'Deep Learning' (remembered). "
    "Opens the user's browser to fetch each file, one by one, then files it away.",
    {"type": "object", "properties": {"course": {"type": "string"}, "folder": {"type": "string"}}},
)
@_guard
def classroom_download(course: str = "", folder: str = ""):
    global _pending, _batch
    with _lock:
        if _batch:
            return {"busy": True, "progress": f"{_batch['done']} of {_batch['total']} done, now {_batch['current']}"}
        key = (course.strip().lower(), folder.strip().lower())
        turn = CONTEXT.get("turn", 0)
        if _pending and _pending["key"] == key and _pending["turn"] < turn and user_confirmed(CONTEXT.get("last_user_message", "")):
            files, folders = _pending["files"], _pending["folders"]
            _pending = None
            _batch = {"total": len(files), "done": 0, "current": files[0]["title"]}
            threading.Thread(target=_run_batch, args=(files, folders), daemon=True, name="classroom-download").start()
            return {"started": len(files), "note": "Tell the user it's started and that you'll say when it's done."}
    files, _, _ = _gather(course, new_only=True)
    if not files:
        return {"nothing_new": True, "note": "Every file in that class has already been downloaded."}
    files = files[:MAX_FILES]
    folders = {}
    for f in files:
        folders[f["course_id"]] = _folder_for(f["course_id"], f["course"], folder if len({x["course_id"] for x in files}) == 1 else "")
    with _lock:
        _pending = {"key": key, "turn": turn, "files": files, "folders": folders}
    return {"needs_confirmation": True,
            "plan": {"files": len(files), "capped_at": MAX_FILES if len(files) == MAX_FILES else None,
                     "titles": [f["title"] for f in files[:6]],
                     "folders": sorted({str(p) for p in folders.values()})},
            "note": "Read this back briefly and ask 'Shall I download them?'. Do not call again until they say yes."}

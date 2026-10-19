"""Talks to the local Tailscale client: is it installed and signed in, what is this
PC's private HTTPS address, and the one-time `tailscale serve` that forwards that
address to the phone server on 127.0.0.1."""
import json
import shutil
import subprocess
import threading
from pathlib import Path

CANDIDATES = [Path(r"C:\Program Files\Tailscale\tailscale.exe"), Path(r"C:\Program Files (x86)\Tailscale\tailscale.exe")]


def exe() -> str | None:
    for p in CANDIDATES:
        if p.exists():
            return str(p)
    return shutil.which("tailscale")


def _run(*args, timeout=15) -> subprocess.CompletedProcess:
    return subprocess.run([exe(), *args], capture_output=True, text=True, timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def status() -> dict:
    """{"installed", "running", "dns_name" (e.g. laptop.tail1234.ts.net), "problem"}."""
    if not exe():
        return {"installed": False, "running": False, "dns_name": "",
                "problem": "Tailscale isn't installed on this PC."}
    try:
        r = _run("status", "--json")
        data = json.loads(r.stdout or "{}")
    except Exception as e:
        return {"installed": True, "running": False, "dns_name": "", "problem": f"Couldn't ask Tailscale: {e}"}
    backend = data.get("BackendState", "")
    if backend != "Running":
        return {"installed": True, "running": False, "dns_name": "",
                "problem": "Tailscale is installed but not signed in / connected." if backend else
                "Tailscale isn't running."}
    dns = (data.get("Self") or {}).get("DNSName", "").rstrip(".")
    return {"installed": True, "running": True, "dns_name": dns,
            "problem": "" if dns else "MagicDNS is off: turn it on in the Tailscale admin console (DNS page)."}


def serve(port: int) -> tuple[bool, str]:
    """Forward https://<this PC>.ts.net to the phone server. Persists across reboots.
    Only devices on your own tailnet can reach it (this is 'serve', not 'funnel')."""
    import re
    import time

    # On a new tailnet, `serve` prints an approval link and then waits until someone opens it,
    # so read its output as it comes instead of blocking.
    p = subprocess.Popen([exe(), "serve", "--bg", f"http://127.0.0.1:{port}"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.extend(p.stdout), daemon=True)
    reader.start()
    deadline = time.time() + 30
    while time.time() < deadline and p.poll() is None:
        link = re.search(r"https://login\.tailscale\.com/\S+", "".join(lines))
        if link:
            p.kill()
            return False, (f"Tailscale needs you to allow 'Serve' on your network (one time). Open this link, approve "
                           f"it, then press SET UP PHONE ACCESS again:\n\n{link.group(0)}")
        time.sleep(0.2)
    if p.poll() is None:
        p.kill()
        return False, "Tailscale didn't answer in time.\n\n" + "".join(lines)[-400:]
    reader.join(timeout=1)

    class _R:  # same shape as subprocess.run's result, for the checks below
        returncode, stdout, stderr = p.returncode, "".join(lines), ""
    r = _R()
    out = r.stdout.strip()
    if r.returncode != 0:
        if "HTTPS" in out or "https" in out:
            return False, ("Tailscale needs HTTPS certificates enabled: open login.tailscale.com/admin/dns and "
                           "click 'Enable HTTPS', then try again.\n\n" + out[-400:])
        return False, out[-600:]
    return True, out[-600:]


def serving(port: int) -> bool:
    try:
        r = _run("serve", "status", "--json")
        return f"127.0.0.1:{port}" in r.stdout
    except Exception:
        return False

"""A temporary public https:// link to the local engine, for phones and for sharing a live demo.

    python3 run.py --share

Phones only allow the camera, compass and GPS on https pages, so this is what makes the
cardholder page's "Show me where" view work on a phone against the real engine.

Two free providers, no account needed:
  * Cloudflare quick tunnels, if `cloudflared` is installed (brew install cloudflared)
  * localhost.run over plain `ssh`, which every Mac, Linux and Windows 10+ machine already has

The link is random, lives only while this process runs, and gives anyone who has it the same
access as you at localhost (simulated data only). Stop sharing with Ctrl+C.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

PROVIDERS = {
    "cloudflared": (re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com"),
                    lambda port: ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]),
    "ssh": (re.compile(r"https://[a-z0-9-]+\.(?:lhr\.life|localhost\.run)"),
            lambda port: ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
                          "-o", "ExitOnForwardFailure=yes", "-R", f"80:localhost:{port}", "nokey@localhost.run"]),
}


def available() -> list[str]:
    return [name for name in PROVIDERS if shutil.which(name)]


def find_url(text: str, provider: str) -> str | None:
    m = PROVIDERS[provider][0].search(text)
    return m.group(0) if m else None


def start(port: int, timeout: float = 30.0, popen=subprocess.Popen) -> tuple[subprocess.Popen, str, str]:
    """Open a tunnel to localhost:port. Returns (process, https url, provider). Raises RuntimeError on failure."""
    names = available()
    if not names:
        raise RuntimeError("Sharing needs `cloudflared` or `ssh`, and neither was found. "
                           "Install cloudflared (macOS: brew install cloudflared) and try again.")
    errors = []
    for name in names:
        proc = popen(PROVIDERS[name][1](port), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, text=True, bufsize=1)
        found: list[str] = []
        seen: list[str] = []

        def read():
            for line in proc.stdout:          # keep draining so the tunnel never blocks on a full pipe
                if not found:
                    seen.append(line)
                    url = find_url(line, name)
                    if url:
                        found.append(url)

        threading.Thread(target=read, daemon=True).start()
        deadline = time.monotonic() + timeout
        while not found and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if found:
            return proc, found[0], name
        proc.terminate()
        errors.append(f"{name}: {''.join(seen[-3:]).strip() or 'no link within ' + str(int(timeout)) + 's'}")
    raise RuntimeError("Couldn't open a share link. " + " | ".join(errors))

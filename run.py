#!/usr/bin/env python3
"""Start Swipestop with one command and open the dashboard.

    python run.py                # dashboard at http://localhost:8000
    python run.py --port 8080 --no-browser
    GEMINI_API_KEY=... python run.py   # also understand free-form questions

Needs only Python 3.11+. No packages to install.
"""
import argparse
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def wait_for(port: int, proc, seconds: float = 120) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow other devices, e.g. your phone for AR")
    ap.add_argument("--no-browser", action="store_true")
    args, extra = ap.parse_known_args()
    if sys.version_info < (3, 11):
        sys.exit(f"Swipestop needs Python 3.11 or newer; this is {sys.version.split()[0]}.")
    cmd = [sys.executable, str(ROOT / "local" / "demo_server.py"), "--port", str(args.port), "--host", args.host, *extra]
    proc = subprocess.Popen(cmd)
    try:
        if wait_for(args.port, proc):
            url = f"http://localhost:{args.port}"
            print(f"\n  Swipestop is running:\n    dashboard  {url}\n    AR globe   {url}/ar\n    API        {url}/api/stats\n")
            if not args.no_browser:
                webbrowser.open(url)
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()

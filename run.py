#!/usr/bin/env python3
"""Start Swipestop with one command and open the dashboard.

    python3 run.py               # http://localhost:8000   (Windows: python run.py)
    python3 run.py --share       # plus a temporary https:// link for your phone or anyone you send it to
    python3 run.py --port 8080 --no-browser
    GEMINI_API_KEY=... python3 run.py   # also understand free-form questions

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
    ap.add_argument("--share", action="store_true",
                    help="also open a temporary public https:// link (camera AR on phones, live demos)")
    args, extra = ap.parse_known_args()
    if sys.version_info < (3, 11):
        sys.exit(f"Swipestop needs Python 3.11 or newer; this is {sys.version.split()[0]}.")
    tunnel_proc, public = None, None
    if args.share:
        sys.path.insert(0, str(ROOT / "local"))
        import tunnel
        print("opening a share link...")
        try:
            tunnel_proc, public, provider = tunnel.start(args.port)
        except RuntimeError as e:
            print(f"  {e}\n  Continuing without a share link.")
        if public and "--public-url" not in extra:
            extra += ["--public-url", public]       # phone push alerts link to the shared page
    cmd = [sys.executable, str(ROOT / "local" / "demo_server.py"), "--port", str(args.port), "--host", args.host, *extra]
    proc = subprocess.Popen(cmd)
    try:
        if wait_for(args.port, proc):
            url = f"http://localhost:{args.port}"
            print(f"\n  Swipestop is running:\n    product page  {url}\n    ops metrics   {url}/console\n"
                  f"    AR globe      {url}/ar\n    API           {url}/api/stats\n")
            if public:
                print(f"  Share link (works on any phone, camera AR included):\n\n      {public}\n\n"
                      "  Anyone with this link can use the demo until you press Ctrl+C.\n")
            if not args.no_browser:
                webbrowser.open(url)
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()


if __name__ == "__main__":
    main()

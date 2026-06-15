#!/usr/bin/env python3
"""One-shot launcher: start the poe2market web dashboard AND the PoE2 macro.

Running this gives you both tools from a single command/double-click:

  * the FastAPI dashboard (sale history, net worth, deals) on http://127.0.0.1:8000,
    which also auto-syncs your sale history in the background, and
  * the always-on-top macro overlay (Shift+4 toggle, Shift+5 quit).

The web server runs as a child process; the macro overlay runs here on the main
thread (its tk window + Win32 keyboard hook need the main thread). Quitting the
macro overlay (Shift+5) shuts the web server down too.

    python poe2_launcher.py            # default port 8000
    python poe2_launcher.py --port 8123
    python poe2_launcher.py --no-macro # dashboard only
    python poe2_launcher.py --no-web   # macro only
"""
import argparse
import atexit
import runpy
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch poe2market dashboard + PoE2 macro.")
    parser.add_argument("--port", type=int, default=8000, help="Web dashboard port.")
    parser.add_argument("--host", default="127.0.0.1", help="Web dashboard host.")
    parser.add_argument("--no-web", action="store_true", help="Skip the web dashboard.")
    parser.add_argument("--no-macro", action="store_true", help="Skip the macro overlay.")
    parser.add_argument("--no-open", action="store_true", help="Don't open the browser.")
    args = parser.parse_args()

    server: subprocess.Popen | None = None

    def shutdown() -> None:
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()

    atexit.register(shutdown)

    if not args.no_web:
        url = f"http://{args.host}:{args.port}"
        print(f"[launcher] starting dashboard at {url}", flush=True)
        server = subprocess.Popen(
            [sys.executable, "-m", "poe2market.cli", "serve",
             "--host", args.host, "--port", str(args.port), "--no-open"],
            cwd=str(HERE),
        )
        if not args.no_open:
            def _open() -> None:
                time.sleep(2.0)
                webbrowser.open(url)
            threading.Thread(target=_open, daemon=True).start()

    if args.no_macro:
        # No overlay to host the main thread — just wait on the server.
        print("[launcher] macro disabled; press Ctrl+C to stop the dashboard.", flush=True)
        try:
            if server:
                server.wait()
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()
        return

    print("[launcher] starting macro overlay (Shift+4 toggle · Shift+5 quit)", flush=True)
    try:
        # Runs the macro's tk mainloop on this (main) thread; returns when the
        # user quits the overlay.
        runpy.run_path(str(HERE / "poe2_macro.py"), run_name="__main__")
    finally:
        shutdown()
        print("[launcher] stopped.", flush=True)


if __name__ == "__main__":
    main()

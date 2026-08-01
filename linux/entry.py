"""Linux Preview launcher and browser-process owner."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from linux.build_id import BASE_BUILD, BASE_RELEASE, PREVIEW_LABEL, PREVIEW_VERSION, identity
    from linux.runtime import prepare_runtime, xdg_paths
else:
    from .build_id import BASE_BUILD, BASE_RELEASE, PREVIEW_LABEL, PREVIEW_VERSION, identity
    from .runtime import prepare_runtime, xdg_paths


def _parse() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--linux-print-paths", action="store_true")
    parser.add_argument("--linux-print-identity", action="store_true")
    parser.add_argument("--browser-mode", choices=("auto", "app", "system", "none"),
                        default=os.environ.get("U64DECK_BROWSER_MODE", "auto"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--help", action="store_true")
    return parser.parse_known_args()


def _print_paths() -> None:
    paths = xdg_paths()
    print(f"config={paths['config'] / 'config.json'}")
    print(f"data={paths['data']}")
    print(f"state={paths['state']}")
    print(f"cache={paths['cache']}")


def _browser_candidates() -> list[str]:
    explicit = os.environ.get("U64DECK_BROWSER", "").strip()
    if explicit:
        return [explicit]
    return ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
            "microsoft-edge", "microsoft-edge-stable"]


def _find_browser() -> str | None:
    import shutil
    for candidate in _browser_candidates():
        if os.path.sep in candidate:
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def _wait_ready(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/app_config", timeout=0.8) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def _launch_browser(url: str, mode: str, paths: dict[str, Path], holder: dict) -> None:
    if not _wait_ready(url):
        print(f"  warning: UI did not become ready; open {url}", flush=True)
        return
    if mode == "none":
        return
    browser = _find_browser()
    if mode == "system" or (mode == "auto" and browser is None):
        try:
            webbrowser.open(url)
        except Exception as exc:
            print(f"  warning: could not open system browser: {exc}", flush=True)
        return
    if browser is None:
        print(f"  no Chromium/Chrome/Edge app browser found; open {url}", flush=True)
        return
    profile = paths["cache"] / "browser-profile"
    browser_path = Path(browser)
    snap_chromium = (
        "chromium" in browser_path.name.lower()
        and (os.environ.get("SNAP") or str(browser_path).startswith("/snap/")
             or str(browser_path).startswith("/var/lib/snapd/snap/"))
    )
    if snap_chromium:
        profile = Path.home() / "snap/chromium/common/u64deck-profile"
    profile.mkdir(parents=True, exist_ok=True)
    command = [browser, f"--app={url}", f"--user-data-dir={profile}",
               "--no-first-run", "--no-default-browser-check"]
    stop_event = holder.get("stop")
    if stop_event is not None and stop_event.is_set():
        return
    try:
        proc = subprocess.Popen(command, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        holder["process"] = proc
        holder["pgid"] = os.getpgid(proc.pid)
        print(f"  Linux app window: {Path(browser).name} (PID {proc.pid})", flush=True)
    except Exception as exc:
        print(f"  warning: could not open app browser: {exc}; open {url}", flush=True)


def _close_owned_browser(holder: dict) -> None:
    proc = holder.get("process")
    pgid = holder.get("pgid")
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(int(pgid), signal.SIGTERM)
        proc.wait(timeout=3.0)
    except Exception:
        try:
            os.killpg(int(pgid), signal.SIGKILL)
        except Exception:
            pass


def main() -> int:
    args, core_args = _parse()
    source_root = Path(__file__).resolve().parents[1]
    if args.help:
        print("Linux options: --linux-print-paths, --linux-print-identity, "
              "--browser-mode auto|app|system|none, --no-browser")
        print("Remaining options are passed to the RC45 server.")
        return 0
    if args.linux_print_paths:
        _print_paths()
        return 0
    if args.linux_print_identity:
        print(identity(source_root))
        print(f"Base lineage: {BASE_RELEASE} · build {BASE_BUILD}")
        return 0

    runtime, paths, build = prepare_runtime(source_root)
    os.environ["U64DECK_CONFIG_DIR"] = str(paths["config"])
    os.environ["U64DECK_DATA_DIR"] = str(paths["data"])
    pid_file = paths["state"] / "u64deck.pid"

    sys.path.insert(0, str(runtime))
    server = importlib.import_module("server")
    pid_file.write_text(str(os.getpid()) + "\n", encoding="ascii")
    port = int(server.CFG.get("http_port", 8064))
    for index, value in enumerate(core_args):
        if value == "--port" and index + 1 < len(core_args):
            try:
                port = int(core_args[index + 1])
            except ValueError:
                pass
    scheme = "https" if (server.CFG.get("tls_certfile") and server.CFG.get("tls_keyfile")) else "http"
    url = f"{scheme}://localhost:{port}"
    mode = "none" if args.no_browser else args.browser_mode
    holder: dict = {"stop": threading.Event()}
    browser_thread = threading.Thread(target=_launch_browser,
                                      args=(url, mode, paths, holder),
                                      daemon=True, name="linux-browser-launch")
    browser_thread.start()
    old_argv = sys.argv[:]
    sys.argv = [str(runtime / "server.py"), *core_args, "--no-browser"]
    print(f"  Linux Preview data: {paths['data']}")
    print(f"  Linux Preview config: {paths['config'] / 'config.json'}")
    print(f"  Base lineage: {BASE_RELEASE} · build {BASE_BUILD}")
    result: dict[str, object] = {"status": 0, "error": None}
    stop_requested = threading.Event()

    def request_stop(signum, frame):
        stop_requested.set()
        active = getattr(server, "_UVICORN_SERVER", None)
        if active is not None:
            active.should_exit = True

    def run_server():
        try:
            server.main()
        except BaseException as exc:  # returned to the main thread below
            result["error"] = exc
            result["status"] = 130 if isinstance(exc, KeyboardInterrupt) else 1

    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, request_stop)

    server_thread = threading.Thread(target=run_server, name="u64deck-server")
    server_thread.start()
    try:
        while server_thread.is_alive():
            server_thread.join(0.2)
            if stop_requested.is_set():
                active = getattr(server, "_UVICORN_SERVER", None)
                if active is not None:
                    active.should_exit = True
        error = result.get("error")
        if error and not isinstance(error, KeyboardInterrupt):
            raise error
        return int(result.get("status") or 0)
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
        sys.argv = old_argv
        holder["stop"].set()
        browser_thread.join(timeout=2.0)
        _close_owned_browser(holder)
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

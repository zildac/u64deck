"""
u64deck — lightweight web control deck for the Ultimate 64.

Run:  python server.py --u64 192.168.x.x
Then open http://localhost:8064
"""

import argparse
import asyncio
import copy
import io
import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import discovery
from d64 import DiskImage, ascii_to_petscii
from ultimate import (AudioReceiver, CommandSocket, DeviceFS, UltimateError,
                      UltimateREST, VideoReceiver)
from device_coordinator import DeviceOperationCoordinator, OperationCancelled
from index_store import IndexStore
from index_migration import STABLE_INDEX_NAME, prepare_stable_index
from local_indexer import (list_local_volumes, normalise_ultimate_root,
                           resolve_source, scan_local_tree, volume_identity)
from sid_indexer import SID_HEADER_BYTES, scan_local_sid_tree
from release import VERSION, RELEASE_LABEL, build_id
from state_io import (read_json as _read_json, warn_throttled as _warn_throttled,
                      write_json_atomic as _write_json_atomic)
from user_items import UserItemsStore

import sys

# When frozen by PyInstaller, bundled read-only assets (static/) live in the
# unpack dir (_MEIPASS); persistent files (config.json) live next to the exe.
FROZEN = getattr(sys, "frozen", False)
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
ROOT = Path(sys.executable).parent if FROZEN else Path(__file__).parent


MIB = 1024 * 1024
MAX_MOUNT_UPLOAD = 300 * MIB       # includes large DNP-style images
MAX_IMAGE_UPLOAD = 4 * MIB         # D64/D71/D81 inspection only
MAX_RUN_UPLOAD = 32 * MIB          # PRG/CRT/SID/MOD/T64
MAX_LIBRARY_UPLOAD = 300 * MIB
MAX_SWAP_FILES = 64
MAX_SWAP_TOTAL = 96 * MIB
MAX_SID_UPLOAD = 2 * MIB
MAX_SID_TOTAL = 64 * MIB
MAX_ASSEMBLY64_DOWNLOAD = 64 * MIB


async def _read_upload(file: UploadFile, limit: int) -> tuple[str, bytes]:
    """Read at most limit+1 bytes and return a safe basename."""
    raw_name = (file.filename or "upload.bin").replace("\\", "/")
    name = raw_name.rsplit("/", 1)[-1] or "upload.bin"
    declared = getattr(file, "size", None)
    if declared is not None and declared > limit:
        raise HTTPException(413, f"{name} exceeds the {limit // MIB} MiB upload limit")
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"{name} exceeds the {limit // MIB} MiB upload limit")
    return name, data


def _attachment_headers(name: str) -> dict[str, str]:
    """Safe RFC 5987 Content-Disposition for PETSCII and device filenames."""
    clean = "".join(c for c in str(name) if c >= " " and c not in '\r\n"') or "download.bin"
    ascii_name = clean.encode("ascii", "replace").decode("ascii")
    return {"Content-Disposition":
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(clean)}'}


BUILD = build_id(ASSETS, Path(__file__).parent)

DEFAULT_CONFIG = {
    "u64_host": "",
    "password": "",
    "http_port": 8064,
    # Interface the u64deck web UI listens on. "127.0.0.1" = this machine
    # only (most secure); "0.0.0.0" = reachable from the LAN (needed for
    # phone/tablet use — anyone on the network can then control the C64).
    "http_host": "0.0.0.0",
    # Optional TLS for the browser<->u64deck hop (the device hop is plaintext
    # by firmware design and can't be fixed here). Point these at a cert/key
    # pair to serve https:// instead of http://.
    "tls_certfile": "",
    "tls_keyfile": "",
    "video_port": 11000,
    "audio_port": 11001,
    # "unicast": device streams straight to this PC.
    # "multicast": device streams to a group; u64deck joins it, so other
    # receivers (e.g. VLC via prkl_ultimate's Data Streams page) can watch
    # the same stream at the same time. Groups match prkl's defaults.
    "stream_transport": "unicast",
    # Local interface IP to use as the stream destination / multicast join
    # interface. Empty = auto (routing-table pick). Set this when virtual
    # adapters (Hyper-V, VPN, WSL) confuse the auto pick.
    "local_ip": "",
    "multicast_video": "239.0.1.64",
    "multicast_audio": "239.0.1.65",
    # Boot timing for Mount+Run / Mount+LOAD and post-reboot F7 automation:
    # seconds to wait after reset before typing. Stock BASIC boots in ~2.5s;
    # cartridges with boot menus (Retro Replay, Action Replay...) may need longer.
    "boot_wait": 2.8,
    # Optional key sent after u64deck-initiated resets/reboots to answer a
    # cartridge boot menu, and BEFORE Mount+Run / Mount+LOAD types its command.
    # E.g. "F7" installs Fastload from a Retro
    # Replay boot menu deterministically instead of letting the LOAD characters
    # hit the menu. Tokens: F1..F8, RETURN, SPACE, or empty for none.
    "boot_prekey": "",
    # Preferred safety mode for existing disk images. Unlinked keeps drive
    # writes temporary; a newly-created blank image is mounted read/write by
    # the create workflow because writing to it is the reason it was created.
    "default_mount_mode": "unlinked",
    # v2 changed the shipped existing-image default from read/write to
    # unlinked. The marker lets an upgraded config migrate once without
    # repeatedly overriding later user choices.
    "mount_mode_policy_version": 2,
    # Local desktop launcher used by start.bat/server.py. Edge app mode opens
    # u64deck in its own Chromium window without changing the Windows default
    # browser. "system" uses the configured default browser; "none" leaves
    # the URL for the user to open manually.
    "browser_startup": "edge_app",
    # Freezer cartridges (Retro Replay, Action Replay...) can break DMA
    # run: the firmware's post-load cartridge restore hard-resets into the
    # cart menu, killing the program you just launched. When true, u64deck
    # blanks the Cartridge config item for the DMA run and writes the
    # original value back afterwards (config changes only apply at the next
    # reset, so the running program is untouched; flash is never written).
    "cart_safe_run": True,
    # HVSC Songlengths.md5 file (optional): enables accurate auto-advance in
    # the jukebox. Download with HVSC; keyed by full-file md5.
    "songlengths_path": "",
    # HVSC root on device storage (e.g. /Usb0/HVSC or /Usb0/C64Music).
    # Leave empty: u64deck auto-detects it on first jukebox use, saves it
    # here, and wires up Songlengths.md5 automatically.
    "hvsc_path": "",
    # Auto-advance fallback when a tune's length is unknown (seconds; 0=off)
    "sid_default_secs": 180,
    # Last-used local SID metadata source. This is only a convenience value;
    # the scanner remains read-only and stores Ultimate-style paths in SQLite.
    "sid_local_source": "",
    "sid_index_root": "",
    "ftp_user": "anonymous",
    "ftp_password": "",
    "assembly64": {
        "base": "http://hackerswithstyle.se/leet",
        "client_id": "Ultimate",
    },
}


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = ROOT / "config.json"
    user = None
    if path.exists():
        user = _read_json(path, {})
        if isinstance(user, dict):
            # merge assembly64 sub-dict so new keys keep their defaults
            a64 = dict(DEFAULT_CONFIG["assembly64"])
            if isinstance(user.get("assembly64"), dict):
                a64.update(user["assembly64"])
            cfg.update(user)
            cfg["assembly64"] = a64
        else:
            print("  warning: config.json must contain a JSON object; using defaults")
    # migration: "u64deck" was the old shipped default Client-Id; the service
    # expects the reference client's id ("Ultimate"), so upgrade it silently
    if cfg["assembly64"].get("client_id") == "u64deck":
        cfg["assembly64"]["client_id"] = "Ultimate"
    # Unlinked is the safe default for images opened from storage, search,
    # favourites or local files. Configs without the policy marker are
    # migrated once; subsequent user choices are preserved.
    if isinstance(user, dict) and int(user.get("mount_mode_policy_version", 0) or 0) < 2:
        cfg["default_mount_mode"] = "unlinked"
        cfg["mount_mode_policy_version"] = 2
        cfg["_config_migration_pending"] = True
    return cfg


def save_config():
    """Persist current settings next to the server/exe so they survive restarts."""
    try:
        _write_json_atomic(ROOT / "config.json", CFG, indent=2)
    except OSError as e:
        # During the one-time config migration this can run before the
        # diagnostics deque is initialised. Never turn a harmless persistence
        # failure into an import-time crash.
        if "DIAG_EVENTS" in globals():
            _warn_event("save-config", f"could not save config.json: {e}")
        else:
            print(f"warning: could not save config.json: {e}")


CFG = load_config()
if CFG.pop("_config_migration_pending", False):
    save_config()
USER_ITEMS = UserItemsStore(ROOT / "user_items.json")
DIAG_EVENTS = deque(maxlen=200)
MOUNT_STATE = {"a": {}, "b": {}}
VALID_MOUNT_MODES = {"readonly", "readwrite", "unlinked"}

VALID_BROWSER_STARTUP = {"edge_app", "system", "none"}

def _normalise_browser_startup(value: object) -> str:
    aliases = {
        "edge": "edge_app", "edge-app": "edge_app", "app": "edge_app",
        "default": "system", "browser": "system", "off": "none",
        "disabled": "none",
    }
    mode = aliases.get(str(value or "").strip().lower(), str(value or "").strip().lower())
    return mode if mode in VALID_BROWSER_STARTUP else "edge_app"

def _edge_candidates(env: dict[str, str] | None = None) -> list[Path]:
    env = env or os.environ
    candidates: list[Path] = []
    for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = env.get(key)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return candidates

def _find_edge_executable(env: dict[str, str] | None = None) -> Path | None:
    for candidate in _edge_candidates(env):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None

def _edge_profile_dir(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    base = env.get("LOCALAPPDATA")
    return (Path(base) / "u64deck" / "EdgeProfile") if base else (ROOT / ".edge-profile")

def _wait_for_ui(url: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + max(0.5, timeout)
    verify = not url.lower().startswith("https://")
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.8, verify=verify) as client:
                if client.get(url).status_code < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.15)
    return False

def _launch_local_browser(url: str, mode: str | None = None) -> dict:
    """Open the local UI according to the persisted launcher preference."""
    selected = _normalise_browser_startup(mode or CFG.get("browser_startup"))
    if selected == "none":
        return {"mode": selected, "opened": False, "reason": "disabled"}
    if selected == "edge_app":
        edge = _find_edge_executable()
        if edge is not None:
            profile = _edge_profile_dir()
            try:
                profile.mkdir(parents=True, exist_ok=True)
                subprocess.Popen([
                    str(edge), f"--app={url}", f"--user-data-dir={profile}",
                    "--no-first-run", "--no-default-browser-check",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print("  browser: Microsoft Edge app window")
                return {"mode": selected, "opened": True, "browser": "edge",
                        "executable": str(edge), "profile": str(profile)}
            except OSError as exc:
                _warn_event("edge-launch", f"could not launch Edge app mode: {exc}")
        print("  browser: Edge not found; using the Windows default browser")
    import webbrowser
    opened = bool(webbrowser.open(url))
    return {"mode": "system", "opened": opened, "browser": "system"}

def _schedule_browser_launch(url: str, mode: str | None = None) -> threading.Thread:
    def worker():
        if _wait_for_ui(url):
            _launch_local_browser(url, mode)
        else:
            _warn_event("browser-launch", "web UI did not become ready before browser launch timeout")
    thread = threading.Thread(target=worker, daemon=True, name="browser-launch")
    thread.start()
    return thread

CFG["browser_startup"] = _normalise_browser_startup(CFG.get("browser_startup"))

def _diag_event(level: str, message: str, **extra):
    item = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "level": str(level), "message": str(message)[:1000]}
    if extra:
        item["extra"] = {str(k): str(v)[:500] for k, v in extra.items()}
    DIAG_EVENTS.append(item)

def _mount_mode(mode: str | None) -> str:
    value = str(mode or CFG.get("default_mount_mode", "readwrite")).lower()
    if value not in VALID_MOUNT_MODES:
        raise HTTPException(400, "mode must be readonly, readwrite or unlinked")
    return value

def _warn_event(key: str, message: str) -> None:
    _diag_event("warning", message)
    _warn_throttled(key, message)


@asynccontextmanager
async def _lifespan(_app):
    yield
    _clean_shutdown()          # cancel jukebox timer, stop stream receivers


app = FastAPI(title="u64deck", version=VERSION, lifespan=_lifespan)


@app.middleware("http")
async def _browser_security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response

DEVICE_OP = DeviceOperationCoordinator()

rest: UltimateREST = None
cmd: CommandSocket = None
devfs: DeviceFS = None
video: VideoReceiver = None
audio: AudioReceiver = None

# In-memory cache of parsed disk images, keyed by token
IMAGE_CACHE: OrderedDict[str, dict] = OrderedDict()
IMAGE_CACHE_MAX = 8


def init_backends():
    global rest, cmd, devfs, video, audio
    rest = UltimateREST(CFG["u64_host"], CFG.get("password", ""), coordinator=DEVICE_OP)
    cmd = CommandSocket(CFG["u64_host"], coordinator=DEVICE_OP)
    devfs = DeviceFS(CFG["u64_host"], CFG.get("ftp_user", "anonymous"),
                     CFG.get("ftp_password", ""), coordinator=DEVICE_OP)
    video = VideoReceiver(CFG["video_port"]); video.start()
    audio = AudioReceiver(CFG["audio_port"]); audio.start()
    if CFG.get("stream_transport") == "multicast":
        video.set_multicast(CFG["multicast_video"], CFG.get("local_ip", ""))
        audio.set_multicast(CFG["multicast_audio"], CFG.get("local_ip", ""))


def err(e: Exception, code: int = 502):
    _diag_event("error", str(e), status=code)
    raise HTTPException(status_code=code, detail=str(e))


def cache_image(name: str, data: bytes) -> dict:
    img = DiskImage(data, name_hint=name)
    token = uuid.uuid4().hex[:12]
    if len(IMAGE_CACHE) >= IMAGE_CACHE_MAX:
        IMAGE_CACHE.popitem(last=False)
    IMAGE_CACHE[token] = {"name": name, "data": data, "img": img, "ts": time.time()}
    out = img.listing()
    out.update({"token": token, "image_name": name, "size": len(data)})
    return out


def get_cached(token: str):
    entry = IMAGE_CACHE.get(token)
    if not entry:
        raise HTTPException(410, "image no longer cached — re-open it")
    IMAGE_CACHE.move_to_end(token)
    entry["ts"] = time.time()
    return entry


def _local_ip() -> str:
    """The local IP the device should stream to: explicit override or auto."""
    return CFG.get("local_ip") or rest.local_ip_towards_device()


@app.get("/api/interfaces")
def interfaces():
    out = []
    try:
        import psutil
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith("127."):
                    out.append({"name": name, "ip": a.address})
    except ImportError:
        # psutil missing: fall back to stdlib (IPs without adapter names)
        try:
            seen = set()
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127.") and ip not in seen:
                    seen.add(ip)
                    out.append({"name": "adapter", "ip": ip})
        except OSError:
            pass
    if not out:
        # last resort: the primary-route IP (same trick discovery uses)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            out.append({"name": "primary", "ip": s.getsockname()[0]})
            s.close()
        except OSError:
            pass
    try:
        auto = rest.local_ip_towards_device()
    except Exception:
        auto = ""
    return {"interfaces": out, "auto": auto,
            "selected": CFG.get("local_ip", "")}


@app.post("/api/interfaces")
def interfaces_set(payload: dict = Body(...)):
    ip = (payload.get("ip") or "").strip()      # "" = back to auto
    if ip:
        try:
            socket.inet_aton(ip)
        except OSError:
            raise HTTPException(400, "not a valid IPv4 address")
    CFG["local_ip"] = ip
    save_config()
    # Re-point any running stream at the new interface; refresh mcast joins
    for name in ("video", "audio"):
        if STREAM_STATE.get(name):
            _stream_ctl(name, True)
        elif CFG.get("stream_transport") == "multicast":
            recv = video if name == "video" else audio
            group = CFG["multicast_video"] if name == "video" else CFG["multicast_audio"]
            recv.set_multicast(group, CFG.get("local_ip", ""))
    return interfaces()


# --- discovery / connection ---------------------------------------------

@app.get("/api/discover")
async def api_discover(subnet: str = Query(""), port: int = Query(80)):
    """Sweep local /24 subnet(s) for Ultimate devices (same method as
    Ultimate64 Manager: TCP:80 scan + /v1/info verification)."""
    extra = [subnet] if subnet else None
    try:
        return await discovery.discover(extra, port)
    except Exception as e:
        err(e, 500)


@app.post("/api/connect")
def api_connect(payload: dict = Body(...)):
    """Switch the active device at runtime (used after discovery)."""
    host = (payload.get("host") or "").strip()
    if not host:
        raise HTTPException(400, "host required")
    if INDEXJOB.get("running"):
        raise HTTPException(409, "stop the storage index before switching devices")
    global rest, cmd, devfs
    password = payload.get("password", CFG.get("password", ""))
    try:
        new_rest = UltimateREST(host, password, coordinator=DEVICE_OP)
        new_cmd = CommandSocket(host, coordinator=DEVICE_OP)
        new_devfs = DeviceFS(host, CFG.get("ftp_user", "anonymous"),
                             CFG.get("ftp_password", ""), coordinator=DEVICE_OP)
    except Exception as e:
        return {"connected": False, "host": host, "error": str(e)}
    # Verify before replacing a known-good connection. A typo should not
    # strand the UI on a dead host or overwrite the saved configuration.
    try:
        device_info = new_rest.info()
    except Exception as e:
        new_rest.close()
        new_cmd.close()
        return {"connected": False, "host": host, "error": str(e)}
    old_rest, old_cmd = rest, cmd
    rest, cmd, devfs = new_rest, new_cmd, new_devfs
    CFG["u64_host"] = host
    CFG["password"] = password
    save_config()
    try:
        if old_cmd:
            old_cmd.close()
        if old_rest:
            old_rest.close()
    except Exception:
        pass
    return {"connected": True, "host": host, "info": device_info}


# --- basic / machine ----------------------------------------------------

def _clean_shutdown():
    _juke_cancel_timer()
    global _INDEX_STORE, _INDEX_STORE_PATH, _INDEX_THREAD
    if INDEXJOB.get("running"):
        INDEXJOB["stop"] = True
        DEVICE_OP.set_background_paused(False)
        DEVICE_OP.wake()
        if _INDEX_THREAD and _INDEX_THREAD.is_alive():
            _INDEX_THREAD.join(timeout=2.0)
    if _INDEX_STORE is not None:
        try:
            _INDEX_STORE.close()
        except Exception:
            pass
        _INDEX_STORE = None
        _INDEX_STORE_PATH = ""
    for resource in (cmd, rest, video, audio):
        try:
            if resource:
                resource.close() if hasattr(resource, "close") else resource.stop()
        except Exception:
            pass
    for receiver in (video, audio):
        try:
            if receiver and receiver.is_alive():
                receiver.join(timeout=1.5)
        except Exception:
            pass


@app.get("/api/app_config")
def app_config():
    safe = {k: v for k, v in CFG.items() if "password" not in k}
    safe["version"] = VERSION
    safe["release_label"] = RELEASE_LABEL
    safe["build"] = BUILD
    return safe


@app.get("/api/local_settings")
def local_settings_get():
    edge = _find_edge_executable()
    return {
        "browser_startup": _normalise_browser_startup(CFG.get("browser_startup")),
        "edge_available": edge is not None,
        "edge_path": str(edge) if edge is not None else "",
        "edge_profile": str(_edge_profile_dir()),
    }


@app.post("/api/local_settings")
def local_settings_set(payload: dict = Body(...)):
    mode = _normalise_browser_startup(payload.get("browser_startup"))
    raw = str(payload.get("browser_startup", "")).strip().lower()
    aliases = {"edge", "edge-app", "app", "default", "browser", "off", "disabled"}
    if raw and raw not in VALID_BROWSER_STARTUP and raw not in aliases:
        raise HTTPException(400, "browser_startup must be edge_app, system or none")
    CFG["browser_startup"] = mode
    save_config()
    result = local_settings_get()
    result["saved"] = True
    return result


def _boot_options_payload() -> dict:
    prekey = str(CFG.get("boot_prekey", "")).strip().upper()
    return {
        "auto_fastload": prekey == "F7",
        "boot_prekey": prekey,
        "boot_wait": float(CFG.get("boot_wait", 2.8)),
    }


@app.get("/api/user_items")
def user_items_list():
    return USER_ITEMS.snapshot()


@app.post("/api/user_items/favorite")
def user_items_favorite(payload: dict = Body(...)):
    try:
        item = USER_ITEMS.favourite(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"favorite": item}


@app.delete("/api/user_items/favorite")
def user_items_unfavorite(item_id: str = Query(...)):
    return {"removed": USER_ITEMS.unfavourite(item_id)}


@app.post("/api/user_items/recent")
def user_items_recent(payload: dict = Body(...)):
    try:
        item = USER_ITEMS.recent(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"recent": item}


@app.delete("/api/user_items/recent")
def user_items_clear_recents():
    return {"cleared": USER_ITEMS.clear_recents()}


@app.get("/api/boot_options")
def boot_options_get():
    """Return u64deck's local post-reset boot-menu automation settings."""
    return _boot_options_payload()


@app.post("/api/boot_options")
def boot_options_set(payload: dict = Body(...)):
    """Enable/disable Retro Replay Fastload selection after u64deck resets."""
    enabled = payload.get("auto_fastload")
    if not isinstance(enabled, bool):
        raise HTTPException(400, "auto_fastload must be true or false")
    current = str(CFG.get("boot_prekey", "")).strip().upper()
    if enabled:
        CFG["boot_prekey"] = "F7"
    elif current == "F7":
        CFG["boot_prekey"] = ""
    save_config()
    return _boot_options_payload()


@app.get("/api/info")
def info():
    if not CFG.get("u64_host"):
        raise HTTPException(503, "No device configured — use Select Ultimate…")
    try:
        # Status polling sits behind user actions but ahead of background work.
        with DEVICE_OP.operation("status", "checking Ultimate status"):
            return rest.info()
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.put("/api/machine/{action}")
def machine(action: str):
    allowed = {"reset", "reboot", "pause", "resume", "poweroff", "menu_button"}
    if action not in allowed:
        raise HTTPException(400, "unknown action")
    try:
        # Keep the reset/reboot and optional post-boot key as one interactive
        # operation so indexing cannot resume during the cartridge-menu wait.
        with DEVICE_OP.operation("interactive", f"machine {action}"):
            result = rest.put(f"/v1/machine:{action}")
            pressed = None
            if action == "reset":
                pressed = _send_boot_prekey()
            elif action == "reboot":
                # A full Ultimate reboot drops the persistent command socket.
                if cmd:
                    cmd.close()
                delay = max(2.5, float(CFG.get("boot_wait", 2.8)))
                pressed = _send_boot_prekey(delay=delay, retry_window=8.0)
            if pressed and isinstance(result, dict):
                result = dict(result)
                result["u64deck_boot_prekey"] = pressed
            return result
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


# --- configuration ------------------------------------------------------

@app.get("/api/configs")
def configs():
    try:
        return rest.get_json("/v1/configs")
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.get("/api/configs/{category}")
def config_category(category: str, detail: bool = False):
    try:
        if detail:
            return rest.get_json(f"/v1/configs/{category}/*")
        return rest.get_json(f"/v1/configs/{category}")
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.put("/api/configs/{category}/{item}")
def config_set(category: str, item: str, value: str = Query(...)):
    try:
        return rest.put(f"/v1/configs/{category}/{item}", value=value)
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.post("/api/configs")
def config_bulk(payload: dict = Body(...)):
    try:
        return rest.post_json("/v1/configs", payload)
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.put("/api/configs_action/{action}")
def config_action(action: str):
    if action not in {"save_to_flash", "load_from_flash", "reset_to_default"}:
        raise HTTPException(400, "unknown action")
    try:
        return rest.put(f"/v1/configs:{action}")
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


# --- drives -------------------------------------------------------------

def _drive_key(drive: str) -> str:
    value = str(drive or "a").lower()
    if value not in {"a", "b"}:
        raise HTTPException(400, "drive must be a or b")
    return value

def _remember_mount(drive: str, mode: str, *, path: str = "", name: str = "") -> None:
    drive = _drive_key(drive)
    MOUNT_STATE[drive] = {"mode": _mount_mode(mode), "path": path, "name": name,
                          "mounted_at": time.time()}

@app.get("/api/mount/options")
def mount_options():
    return {"default_mode": _mount_mode(CFG.get("default_mount_mode")),
            "modes": sorted(VALID_MOUNT_MODES)}

@app.post("/api/mount/options")
def mount_options_set(payload: dict = Body(...)):
    mode = _mount_mode(payload.get("default_mode"))
    CFG["default_mount_mode"] = mode
    CFG["mount_mode_policy_version"] = 2
    save_config()
    return mount_options()

@app.get("/api/drives")
def drives():
    try:
        out = rest.get_json("/v1/drives")
        decision = _reconcile_swap_from_drives(out)
        for row in out.get("drives", []) if isinstance(out, dict) else []:
            for key in ("a", "b"):
                if isinstance(row.get(key), dict):
                    row[key]["u64deck_mount"] = dict(MOUNT_STATE.get(key) or {})
        if isinstance(out, dict):
            out["swap_reconstructed"] = bool(decision and decision.get("source") == "reconstructed")
            out["swap_decision"] = dict(decision or SWAP.get("decision") or {})
        return out
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.put("/api/drives/{drive}/{action}")
def drive_action(drive: str, action: str):
    drive = _drive_key(drive)
    if action not in {"remove", "reset", "on", "off"}:
        raise HTTPException(400, "unknown action")
    try:
        out = rest.put(f"/v1/drives/{drive}:{action}")
        if action in {"remove", "off"}:
            MOUNT_STATE[drive] = {}
            if SWAP.get("drive") == drive and SWAP.get("source") in {"auto", "reconstructed"}:
                SWAP.update({"items": [], "index": -1, "source": "none", "decision": {}})
        return out
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.post("/api/mount/upload")
async def mount_upload(drive: str = Form("a"), mode: str = Form("readwrite"),
                       file: UploadFile = File(...)):
    drive, mode = _drive_key(drive), _mount_mode(mode)
    name, data = await _read_upload(file, MAX_MOUNT_UPLOAD)
    try:
        out = await run_in_threadpool(rest.mount_attachment, drive, name, data,
                                      mode=mode)
        _remember_mount(drive, mode, name=name)
        return out
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.put("/api/mount/device")
def mount_device(drive: str = "a", mode: str = "readwrite", image: str = Query(...)):
    drive, mode = _drive_key(drive), _mount_mode(mode)
    _swap_build_from_device(image, drive, mode)
    try:
        out = rest.mount_path(drive, image, mode=mode)
        _remember_mount(drive, mode, path=image, name=image.rsplit("/", 1)[-1])
        return _swap_response(out)
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


# --- device file system (FTP) ------------------------------------------

def _cache_live_directory(path: str, entries: list[dict]) -> None:
    """Refresh one browsed folder and invalidate stale completion markers."""
    try:
        store = _index_store()
        previous = store.get_directory(path)
        if previous is None:
            if store.complete_cover(path) is not None:
                store.invalidate_path(path)
        elif previous != entries:
            store.invalidate_path(path)
        store.put_directory(path, entries)
    except Exception as cache_error:
        _warn_event("cache-live-directory", f"could not update storage index: {cache_error}")


@app.get("/api/fs")
def fs_list(path: str = "/"):
    try:
        entries = devfs.list_dir(path)
        _cache_live_directory(path, entries)
        return {"path": path, "entries": entries}
    except Exception as e:
        err(e)


@app.get("/api/fs/download")
def fs_download(path: str = Query(...)):
    try:
        data = devfs.fetch(path)
    except Exception as e:
        err(e)
    name = path.rsplit("/", 1)[-1]
    return Response(content=data, media_type="application/octet-stream",
                    headers=_attachment_headers(name))


def _sibling_copy_path(path: str, tag: str = "copy") -> str:
    folder, _, name = str(path).rpartition("/")
    folder = folder or "/"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}-{stamp}"
    copied = f"{stem}{suffix}{('.' + ext) if ext else ''}"
    return (folder.rstrip("/") + "/" + copied) if folder != "/" else "/" + copied

def _copy_device_file(source: str, destination: str | None = None, tag: str = "copy") -> dict:
    source = str(source or "")
    if not source.startswith("/") or source.endswith("/"):
        raise HTTPException(400, "source must be a device file path")
    destination = str(destination or _sibling_copy_path(source, tag))
    if not destination.startswith("/") or destination.endswith("/"):
        raise HTTPException(400, "destination must be a device file path")
    if destination == source:
        raise HTTPException(400, "destination must differ from source")
    with DEVICE_OP.operation("interactive", f"copying {source}"):
        data = devfs.fetch(source, max_size=MAX_MOUNT_UPLOAD)
        devfs.upload(destination, data)
    parent = destination.rsplit("/", 1)[0] or "/"
    try:
        _index_store().invalidate_path(parent)
    except Exception:
        pass
    _diag_event("info", "device file copied", source=source, destination=destination, bytes=len(data))
    return {"source": source, "destination": destination, "bytes": len(data)}

@app.post("/api/fs/duplicate")
def fs_duplicate(payload: dict = Body(...)):
    try:
        return _copy_device_file(payload.get("path"), payload.get("destination"), "copy")
    except HTTPException:
        raise
    except Exception as e:
        err(e)

@app.post("/api/mount/backup_then_rw")
def mount_backup_then_rw(payload: dict = Body(...)):
    source = str(payload.get("path") or "")
    drive = _drive_key(payload.get("drive", "a"))
    try:
        backup = _copy_device_file(source, None, "backup")
        out = rest.mount_path(drive, source, mode="readwrite")
        _remember_mount(drive, "readwrite", path=source, name=source.rsplit("/", 1)[-1])
        _swap_build_from_device(source, drive, "readwrite")
        return _swap_response({"backup": backup, "mount": out, "mode": "readwrite", "drive": drive})
    except HTTPException:
        raise
    except (UltimateError, httpx.HTTPError, OSError) as e:
        err(e)


# --- disk image inspection / per-file run ------------------------------

@app.post("/api/image/open/upload")
async def image_open_upload(file: UploadFile = File(...)):
    name, data = await _read_upload(file, MAX_IMAGE_UPLOAD)
    try:
        return await run_in_threadpool(cache_image, name, data)
    except ValueError as e:
        err(e, 400)


@app.get("/api/image/open/device")
def image_open_device(path: str = Query(...)):
    try:
        data = devfs.fetch(path)
        out = cache_image(path.rsplit("/", 1)[-1], data)
        out["device_path"] = path
        return out
    except ValueError as e:
        err(e, 400)
    except Exception as e:
        err(e)


@app.post("/api/image/{token}/run")
def image_run(token: str, index: int = Query(...), mode: str = Query("dma")):
    """mode=dma  -> extract PRG and DMA run it (no mount)
       mode=load -> DMA-load only (no RUN)"""
    entry = get_cached(token)
    img: DiskImage = entry["img"]
    try:
        f = img.find(index=index)
    except KeyError as e:
        err(e, 404)
    if f.file_type != "PRG":
        raise HTTPException(400, f"{f.name} is {f.file_type}, not PRG")
    try:
        data = img.extract(f)
    except ValueError as e:
        err(e, 400)
    if len(data) < 3:
        raise HTTPException(400, "file is empty")
    try:
        if mode == "load":
            return rest.post_file("/v1/runners:load_prg", f.name + ".prg", data)
        return _run_cart_safe(lambda: rest.run_prg(f.name + ".prg", data))
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.get("/api/image/{token}/file")
def image_extract(token: str, index: int = Query(...)):
    entry = get_cached(token)
    img: DiskImage = entry["img"]
    try:
        f = img.find(index=index)
        data = img.extract(f)
    except (KeyError, ValueError) as e:
        err(e, 400)
    ext = ".prg" if f.file_type == "PRG" else ".bin"
    return Response(content=data, media_type="application/octet-stream",
                    headers=_attachment_headers(f.name + ext))


@app.post("/api/image/{token}/mount_load")
def image_mount_load(token: str, index: int = Query(...), drive: str = Query("a"),
                     mode: str = Query("readwrite"), device_path: str = Query(None)):
    """Mount the image, reset, then type LOAD"NAME",8,1 + RUN via the
    keyboard buffer — for multi-load software that needs the disk present."""
    drive, mode = _drive_key(drive), _mount_mode(mode)
    entry = get_cached(token)
    img: DiskImage = entry["img"]
    try:
        f = img.find(index=index)
    except KeyError as e:
        err(e, 404)
    try:
        with DEVICE_OP.operation("interactive", "mounting and loading disk file"):
            if device_path:
                rest.mount_path(drive, device_path, mode=mode)
            else:
                rest.mount_attachment(drive, entry["name"], entry["data"], mode=mode)
            _remember_mount(drive, mode, path=device_path or "", name=entry["name"])
            if device_path:
                _swap_build_from_device(device_path, drive, mode)
            rest.put("/v1/machine:reset")
            _boot_settle()
            bus_id = _bus_id_for(drive)
            line = b'LOAD"' + f.raw_name + b'"' + f',{bus_id},1\r'.encode()
            cmd.type_petscii(line)
            time.sleep(0.4)
            cmd.type_petscii(b"RUN\r")
            return _swap_response({"errors": [], "typed": f'LOAD"{f.name}",{bus_id},1 + RUN'}) if device_path else {"errors": [], "typed": f'LOAD"{f.name}",{bus_id},1 + RUN'}
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


_PREKEYS = {"F1": 133, "F2": 137, "F3": 134, "F4": 138, "F5": 135,
            "F6": 139, "F7": 136, "F8": 140, "RETURN": 13, "SPACE": 32}


def _configured_boot_prekey() -> str:
    prekey = str(CFG.get("boot_prekey", "")).strip().upper()
    return prekey if prekey in _PREKEYS else ""


def _send_boot_prekey(delay: float = 1.0, retry_window: float = 0.0) -> str | None:
    """Press the configured cartridge-menu key after a tool-initiated reset.

    Returns the token that was sent, or None when automation is disabled. A
    reboot can briefly take port 64 offline, so callers may request retries.
    """
    prekey = _configured_boot_prekey()
    if not prekey:
        return None
    time.sleep(max(0.0, delay))
    deadline = time.monotonic() + max(0.0, retry_window)
    while True:
        try:
            cmd.type_petscii(bytes([_PREKEYS[prekey]]))
            return prekey
        except UltimateError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.4)


def _boot_settle():
    """Wait out the reset, answering a cartridge boot menu if configured."""
    wait = float(CFG.get("boot_wait", 2.8))
    prekey = _configured_boot_prekey()
    if prekey:
        _send_boot_prekey(min(1.0, wait))
        time.sleep(max(0.0, wait - 1.0) + 0.6)   # menu handoff margin
    else:
        time.sleep(wait)


def _bus_id_for(drive: str) -> int:
    try:
        for d in rest.get_json("/v1/drives").get("drives", []):
            if drive in d and d[drive].get("enabled"):
                return d[drive].get("bus_id", 8)
    except Exception:
        pass
    return 8


def _mount_and_boot(drive: str, mode: str, *, device_path: str = None,
                    name: str = None, data: bytes = None):
    """Mount an image and autostart it: reset, LOAD"*",{bus},1 + RUN."""
    drive, mode = _drive_key(drive), _mount_mode(mode)
    with DEVICE_OP.operation("interactive", "mounting and booting disk"):
        if device_path:
            rest.mount_path(drive, device_path, mode=mode)
        else:
            rest.mount_attachment(drive, name, data, mode=mode)
        _remember_mount(drive, mode, path=device_path or "", name=name or (device_path or "").rsplit("/", 1)[-1])
        rest.put("/v1/machine:reset")
        _boot_settle()
        bus_id = _bus_id_for(drive)
        cmd.type_petscii(f'LOAD"*",{bus_id},1\r'.encode())
        time.sleep(0.4)
        cmd.type_petscii(b"RUN\r")
        return {"errors": [], "typed": f'LOAD"*",{bus_id},1 + RUN'}


@app.put("/api/mount/run/device")
def mount_run_device(drive: str = Query("a"), mode: str = Query("readwrite"),
                     image: str = Query(...)):
    """One click: mount a disk image from Ultimate storage and boot it."""
    try:
        drive, mode = _drive_key(drive), _mount_mode(mode)
        _swap_build_from_device(image, drive, mode)
        return _swap_response(_mount_and_boot(drive, mode, device_path=image))
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.post("/api/mount/run/upload")
async def mount_run_upload(drive: str = Form("a"), mode: str = Form("readwrite"),
                           file: UploadFile = File(...)):
    """One click: mount an uploaded disk image and boot it."""
    name, data = await _read_upload(file, MAX_MOUNT_UPLOAD)
    try:
        return await run_in_threadpool(_mount_and_boot, drive, mode,
                                       name=name, data=data)
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


# --- runners ------------------------------------------------------------

_CART_CAT = "C64 and Cartridge Settings"
_CART_ITEM = "Cartridge"


def _cart_configured() -> str:
    """Currently configured cartridge (CRT path), or '' for none."""
    try:
        j = rest.get_json(f"/v1/configs/{_CART_CAT}")
        return str((j.get(_CART_CAT) or {}).get(_CART_ITEM) or "")
    except Exception:
        return ""


def _run_cart_safe(fn):
    """Run a DMA action with any freezer cartridge parked."""
    with DEVICE_OP.operation("interactive", "running software"):
        cart = _cart_configured() if CFG.get("cart_safe_run", True) else ""
        if cart:
            try:
                rest.put(f"/v1/configs/{_CART_CAT}/{_CART_ITEM}", value="")
            except Exception:
                cart = ""
        try:
            return fn()
        finally:
            if cart:
                try:
                    rest.put(f"/v1/configs/{_CART_CAT}/{_CART_ITEM}", value=cart)
                except Exception:
                    pass


def _t64_first_prg(data: bytes):
    """Extract the first PRG from a T64 tape archive -> (name, prg_bytes).

    T64: 64-byte header (signature, version, max/used entries, tape name),
    then 32-byte directory entries: c64s type, 1541 type, start addr,
    end addr, reserved, data offset, reserved, 16-char name. Many T64s in
    the wild carry the famous broken end-address (0xC3C6 bug), so the
    length is clamped to what's actually in the file."""
    if len(data) < 96 or not data[:3] in (b"C64", b"T64"):
        raise ValueError("not a T64 file")
    used = int.from_bytes(data[0x24:0x26], "little") or 1
    for i in range(min(used, 64)):
        off = 64 + i * 32
        entry = data[off:off + 32]
        if len(entry) < 32 or entry[0] == 0:
            continue
        start = int.from_bytes(entry[2:4], "little")
        end = int.from_bytes(entry[4:6], "little")
        doff = int.from_bytes(entry[8:12], "little")
        name = entry[16:32].decode("latin-1", "replace").rstrip(" \x00") or "PROGRAM"
        length = max(0, end - start)
        avail = len(data) - doff
        if length <= 0 or length > avail:      # 0xC3C6-style broken end addr
            length = avail
        if doff >= len(data) or length <= 0:
            continue
        prg = start.to_bytes(2, "little") + data[doff:doff + length]
        return name, prg
    raise ValueError("no program entries in T64")


def _runner_for(name: str) -> str:
    low = name.lower()
    if low.endswith(".crt"):
        return "run_crt"
    if low.endswith(".sid"):
        return "sidplay"
    if low.endswith(".mod"):
        return "modplay"
    return "run_prg"


@app.put("/api/run/device")
def run_device(path: str = Query(...)):
    try:
        if path.lower().endswith(".t64"):
            name, prg = _t64_first_prg(devfs.fetch(path))
            return _run_cart_safe(
                lambda: rest.run_prg(name + ".prg", prg))
        runner = _runner_for(path)
        if runner == "run_crt":
            return rest.put(f"/v1/runners:{runner}", file=path)
        return _run_cart_safe(lambda: rest.put(f"/v1/runners:{runner}", file=path))
    except ValueError as e:
        err(e, 400)
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.post("/api/run/upload")
async def run_upload(file: UploadFile = File(...)):
    name, data = await _read_upload(file, MAX_RUN_UPLOAD)
    try:
        if name.lower().endswith(".t64"):
            name, prg = _t64_first_prg(data)
            return await run_in_threadpool(
                _run_cart_safe, lambda: rest.run_prg(name + ".prg", prg))
        runner = _runner_for(name)
        if runner == "run_crt":
            return await run_in_threadpool(
                rest.post_file, f"/v1/runners:{runner}", name, data)
        return await run_in_threadpool(
            _run_cart_safe,
            lambda: rest.post_file(f"/v1/runners:{runner}", name, data))
    except ValueError as e:
        err(e, 400)
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


# --- keyboard -----------------------------------------------------------

@app.post("/api/keys")
def keys(payload: dict = Body(...)):
    """{"text": "..."} for ASCII text, or {"petscii": [13, 65, ...]} raw codes."""
    data = b""
    if "text" in payload:
        data += ascii_to_petscii(str(payload["text"]))
    if "petscii" in payload:
        data += bytes(b & 0xFF for b in payload["petscii"])
    if not data:
        raise HTTPException(400, "nothing to type")
    if len(data) > 512:
        raise HTTPException(400, "too much text at once")
    try:
        cmd.type_petscii(data)
        return {"errors": [], "sent": len(data)}
    except UltimateError as e:
        err(e)


# --- streams ------------------------------------------------------------

STREAM_STATE = {"video": False, "audio": False}
STREAM_LAST = {}    # per-stream record of the last start/stop attempt


def _stream_ctl(name: str, on: bool):
    stream_id = {"video": 0, "audio": 1}[name]
    port = CFG["video_port"] if name == "video" else CFG["audio_port"]
    recv = video if name == "video" else audio
    if CFG.get("stream_transport") == "multicast":
        group = CFG["multicast_video"] if name == "video" else CFG["multicast_audio"]
        dest = f"{group}:{port}"
        recv.set_multicast(group, CFG.get("local_ip", ""))
    else:
        dest = f"{_local_ip()}:{port}"
        recv.set_multicast(None)
    rec = {"action": "start" if on else "stop", "dest": dest,
           "transport": CFG.get("stream_transport", "unicast"),
           "via": "rest", "rest_error": None, "socket_error": None,
           "ts": time.strftime("%H:%M:%S")}
    STREAM_LAST[name] = rec
    try:
        if on:
            rest.stream_start(name, dest)
        else:
            rest.stream_stop(name)
        rec["ok"] = True
    except (UltimateError, httpx.HTTPError) as e:
        rec["rest_error"] = str(e)
        rec["via"] = "socket"
        # older firmware: fall back to the TCP command socket
        try:
            if on:
                cmd.stream_on(stream_id, dest)
            else:
                cmd.stream_off(stream_id)
            rec["ok"] = True
        except Exception as e2:
            rec["socket_error"] = str(e2)
            rec["ok"] = False
            raise
    STREAM_STATE[name] = on


@app.get("/api/stream/transport")
def stream_transport_get():
    return {"transport": CFG.get("stream_transport", "unicast"),
            "multicast_video": CFG["multicast_video"],
            "multicast_audio": CFG["multicast_audio"]}


@app.post("/api/stream/transport")
def stream_transport_set(payload: dict = Body(...)):
    mode = payload.get("transport")
    if mode not in ("unicast", "multicast"):
        raise HTTPException(400, "transport must be unicast or multicast")
    CFG["stream_transport"] = mode
    for k in ("multicast_video", "multicast_audio"):
        if payload.get(k):
            CFG[k] = payload[k]
    save_config()
    # If a stream is running, restart it so the device redirects output —
    # if not, just (un)join the group so a stream someone else started
    # (e.g. via prkl's web page) becomes visible immediately.
    for name in ("video", "audio"):
        if STREAM_STATE.get(name):
            _stream_ctl(name, True)
        else:
            recv = video if name == "video" else audio
            group = (CFG["multicast_video"] if name == "video" else CFG["multicast_audio"])
            recv.set_multicast(group if mode == "multicast" else None,
                               CFG.get("local_ip", ""))
    return stream_transport_get()


@app.put("/api/stream/{name}/{state}")
def stream_ctl(name: str, state: str):
    if name not in ("video", "audio") or state not in ("start", "stop"):
        raise HTTPException(400, "bad stream request")
    try:
        _stream_ctl(name, state == "start")
        return {"errors": [], "state": STREAM_STATE}
    except Exception as e:
        err(e)


@app.get("/api/stream/status")
def stream_status():
    return {
        "state": STREAM_STATE,
        "transport": CFG.get("stream_transport", "unicast"),
        "local_ip": CFG.get("local_ip") or "(auto)",
        "video": {"frames": video.frame_no, "packets": video.packets,
                  "mcast_group": video._mcast_group},
        "audio": {"packets": audio.packets, "last_pkt_len": audio.last_pkt_len,
                  "mcast_group": audio._mcast_group},
        "last_command": STREAM_LAST,
        # kept for backward compat
        "frames": video.frame_no, "packets": video.packets,
    }


@app.websocket("/ws/video")
async def ws_video(ws: WebSocket, buffer: int = 1):
    """Video frames over WS.

    ?buffer=1  (default) latest-frame-wins: lowest latency, drops stale
               frames if the client/network can't keep up.
    ?buffer=N  (2..128)  buffered full-rate: keeps every frame up to N deep
               before dropping the oldest — for fast LAN/Wi-Fi 6 where you
               want all 50 fps even through brief hiccups.
    """
    await ws.accept()
    loop = asyncio.get_running_loop()
    depth = max(1, min(int(buffer or 1), 128))
    queue: asyncio.Queue = asyncio.Queue(maxsize=depth)

    def on_frame(frame: bytes):
        def push():
            if queue.full():
                try:
                    queue.get_nowait()      # drop oldest, keep newest
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(frame)
        loop.call_soon_threadsafe(push)

    video.subscribe(on_frame)
    try:
        await ws.send_bytes(video.latest)
        while True:
            frame = await queue.get()
            await ws.send_bytes(frame)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass                                   # client gone or server stopping
    finally:
        video.unsubscribe(on_frame)


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    # Keep latency bounded: eight ~32 ms chunks is enough to absorb a brief
    # browser hiccup without allowing a full second of stale audio to queue.
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)

    def on_chunk(chunk: bytes):
        def push():
            if queue.full():
                try:
                    queue.get_nowait()       # discard oldest, keep audio current
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(chunk)
        loop.call_soon_threadsafe(push)

    audio.subscribe(on_chunk)
    try:
        while True:
            chunk = await queue.get()
            await ws.send_bytes(chunk)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass                                   # client gone or server stopping
    finally:
        audio.unsubscribe(on_chunk)


# --- Assembly64 ----------------------------------------------------------
# Protocol reverse-engineered from the Ultimate firmware's own client
# (software/network/assembly.cc): AQL query syntax is (field:"value")
# joined by " & "; the service expects User-Agent "Assembly Query" and a
# Client-Id header. Endpoints: /leet/search/aql/{off}/{lim}?query=,
# /leet/search/aql/presets, /leet/search/entries/{id}/{cat},
# /leet/search/bin/{id}/{cat}/{itemIdOrIdx}.

def _asm_headers():
    return {"User-Agent": "Assembly Query",
            "Client-Id": CFG["assembly64"].get("client_id", "u64deck"),
            "Accept-Encoding": "identity"}


def _asm_base():
    return CFG["assembly64"]["base"].rstrip("/")


def _asm_urlencode(s: str) -> str:
    """Byte-for-byte replica of the firmware's url_encode()."""
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        if ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9") or c in "_-.*":
            out.append(c)
        else:
            out.append("%%%02x" % b)
    return "".join(out)


def _build_aql(fields: list) -> str:
    """Firmware-exact AQL: text fields quoted, dropdown (preset) fields not,
    'rating' gets a >= prefix unless the value already starts with '>'.
    fields: [{"name": str, "value": str, "dropdown": bool}] in form order."""
    parts = []
    for f in fields:
        name = str(f.get("name", "")).strip()
        val = str(f.get("value", "")).strip()
        if not name or not val:
            continue
        dropdown = bool(f.get("dropdown"))
        v = val
        if name.lower() == "rating" and not v.startswith(">"):
            v = ">=" + v
        parts.append(f'({name}:{v})' if dropdown else f'({name}:"{v}")')
    return " & ".join(parts)


async def _asm_do_search(query: str, offset: int, limit: int):
    url = f"{_asm_base()}/search/aql/{offset}/{limit}?query=" + _asm_urlencode(query)
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.get(url, headers=_asm_headers())
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:300]
            err(Exception(f"Assembly64 answered {e.response.status_code} for query "
                          f"'{query}'" + (f" — {body}" if body.strip() else "")))
        except httpx.HTTPError as e:
            err(e)
    try:
        results = r.json()
    except ValueError:
        raise HTTPException(502, "Assembly64 sent non-JSON: " + r.text[:200])
    return {"query": query, "results": results}


@app.get("/api/asm64/search")
async def asm64_search_get(name: str = "", group: str = "", handle: str = "",
                           event: str = "", offset: int = 0, limit: int = 100):
    """Simple text-field search (all quoted)."""
    fields = [{"name": k, "value": v} for k, v in
              (("name", name), ("group", group), ("handle", handle), ("event", event))]
    query = _build_aql(fields)
    if not query:
        raise HTTPException(400, "give at least one search field")
    return await _asm_do_search(query, offset, limit)


@app.post("/api/asm64/search")
async def asm64_search_post(payload: dict = Body(...)):
    """Full form search: ordered fields with dropdown flags (see _build_aql)."""
    query = _build_aql(payload.get("fields") or [])
    if not query:
        raise HTTPException(400, "give at least one search field")
    return await _asm_do_search(query, int(payload.get("offset", 0)),
                                int(payload.get("limit", 100)))


@app.get("/api/asm64/presets")
async def asm64_presets():
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(_asm_base() + "/search/aql/presets", headers=_asm_headers())
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            err(e)


@app.get("/api/asm64/entries")
async def asm64_entries(id: str = Query(...), category: int = Query(...)):
    url = f"{_asm_base()}/search/entries/{id}/{category}"
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.get(url, headers=_asm_headers())
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            err(e)


async def _asm_fetch_binary(id: str, category: int, item: str) -> bytes:
    url = f"{_asm_base()}/search/bin/{id}/{category}/{item}"
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        async with c.stream("GET", url, headers=_asm_headers()) as r:
            r.raise_for_status()
            try:
                declared = int(r.headers.get("content-length") or 0)
            except ValueError:
                declared = 0
            if declared > MAX_ASSEMBLY64_DOWNLOAD:
                raise HTTPException(413, "Assembly64 file exceeds the 64 MiB limit")
            chunks, total = [], 0
            async for chunk in r.aiter_bytes(MIB):
                total += len(chunk)
                if total > MAX_ASSEMBLY64_DOWNLOAD:
                    raise HTTPException(413, "Assembly64 file exceeds the 64 MiB limit")
                chunks.append(chunk)
            return b"".join(chunks)


def _asm_deploy_bytes(filename: str, action: str, data: bytes):
    """Synchronous device half of Assembly64 deploy (run in worker thread)."""
    low = filename.lower()
    if action == "inspect" and low.endswith((".d64", ".d71", ".d81")):
        return cache_image(filename, data)
    if action in ("mount_a", "mount_b"):
        drive, mode = action[-1], _mount_mode(None)
        out = rest.mount_attachment(drive, filename, data, mode=mode)
        _remember_mount(drive, mode, name=filename)
        return out
    if action == "mount_run":
        if not low.endswith((".d64", ".d71", ".d81", ".g64")):
            raise ValueError("Mount & Run is only available for disk images")
        return _mount_and_boot("a", _mount_mode(None), name=filename, data=data)
    if low.endswith((".d64", ".d71", ".d81", ".g64")):
        mode = _mount_mode(None)
        out = rest.mount_attachment("a", filename, data, mode=mode)
        _remember_mount("a", mode, name=filename)
        rest.put("/v1/machine:reset")
        return out
    if low.endswith(".crt"):
        return rest.post_file("/v1/runners:run_crt", filename, data)
    if low.endswith(".sid"):
        return _run_cart_safe(
            lambda: rest.post_file("/v1/runners:sidplay", filename, data))
    if low.endswith(".mod"):
        return _run_cart_safe(
            lambda: rest.post_file("/v1/runners:modplay", filename, data))
    return _run_cart_safe(lambda: rest.run_prg(filename, data))


@app.post("/api/asm64/deploy")
async def asm64_deploy(payload: dict = Body(...)):
    """Download a file from Assembly64 and act on it.

    payload: {id, category, item (contentEntry id or index), filename,
              action: run|mount_run|mount_a|mount_b|inspect}
    """
    for k in ("id", "category", "item", "filename", "action"):
        if k not in payload:
            raise HTTPException(400, f"missing {k}")
    try:
        data = await _asm_fetch_binary(str(payload["id"]), int(payload["category"]),
                                       str(payload["item"]))
    except httpx.HTTPError as e:
        err(e)
    filename = str(payload["filename"]) or "download.prg"
    action = payload["action"]
    if action not in {"run", "mount_run", "mount_a", "mount_b", "inspect"}:
        raise HTTPException(400, "unknown Assembly64 action")
    try:
        return await run_in_threadpool(_asm_deploy_bytes, filename, action, data)
    except ValueError as e:
        err(e, 400)
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


@app.get("/api/asm64/get")
async def asm64_get(path: str = Query(...)):
    """Raw passthrough kept for debugging."""
    if not path.startswith("/"):
        raise HTTPException(400, "path must start with /")
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(_asm_base() + path, headers=_asm_headers())
        except httpx.HTTPError as e:
            err(e)
    ctype = r.headers.get("content-type", "application/octet-stream")
    if "json" in ctype:
        return JSONResponse(status_code=r.status_code, content=r.json())
    return Response(content=r.content, status_code=r.status_code, media_type=ctype)


# --- SQLite directory/image index and recursive search --------------------

_LEGACY_IMG_CACHE_FILE = ROOT / ".imagecache.json"
_LEGACY_DIR_CACHE_FILE = ROOT / ".dircache.json"
_LEGACY_INDEX_META_FILE = ROOT / ".indexmeta.json"
_LEGACY_IMPORT_MARKER = ROOT / ".legacy-cache-imported"
_INDEX_STORE = None
_INDEX_STORE_PATH = ""
_INDEX_STORE_LOCK = threading.RLock()
_INDEX_THREAD = None
_INDEX_MIGRATION_ATTEMPTED = False
_INDEX_MIGRATION_STATUS = {
    "status": "pending",
    "path": STABLE_INDEX_NAME,
    "migrated_sources": 0,
    "backup_dir": "",
}


def _index_db_path(host: str = "") -> Path:
    """Return the stable installation-local index path.

    The host argument remains accepted for compatibility with older callers,
    but DHCP address changes must never select a different database.
    """
    return ROOT / STABLE_INDEX_NAME


def _legacy_index_db_path(host: str) -> Path:
    """Old per-IP filename, used only as a safe fallback after migration failure."""
    import hashlib as _hashlib
    host = host or "unconfigured"
    safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in host)[:40]
    suffix = _hashlib.sha1(host.encode("utf-8", "replace")).hexdigest()[:8]
    return ROOT / f".u64deck-index-{safe}-{suffix}.sqlite3"


def _prepare_stable_index() -> dict:
    global _INDEX_MIGRATION_ATTEMPTED, _INDEX_MIGRATION_STATUS
    if not _INDEX_MIGRATION_ATTEMPTED:
        _INDEX_MIGRATION_STATUS = prepare_stable_index(ROOT, log=print)
        _INDEX_MIGRATION_ATTEMPTED = True
        if _INDEX_MIGRATION_STATUS.get("status") == "failed":
            _warn_event(
                "index-migration",
                "stable index migration failed; legacy database retained: " +
                str(_INDEX_MIGRATION_STATUS.get("error", "unknown error")),
            )
    return dict(_INDEX_MIGRATION_STATUS)


def _index_store() -> IndexStore:
    """Open the stable SQLite index and import legacy JSON caches once."""
    global _INDEX_STORE, _INDEX_STORE_PATH
    host = str(CFG.get("u64_host") or "")
    with _INDEX_STORE_LOCK:
        migration = _prepare_stable_index()
        stable = _index_db_path()
        if migration.get("status") == "failed" and not stable.is_file():
            # A failed merge must never prevent the application starting or
            # discard access to the currently selected legacy index.
            desired = _legacy_index_db_path(host)
        else:
            desired = stable
        desired_key = str(desired.resolve())
        if _INDEX_STORE is None or _INDEX_STORE_PATH != desired_key:
            if _INDEX_STORE is not None:
                try:
                    _INDEX_STORE.close()
                except Exception:
                    pass
            _INDEX_STORE = IndexStore(desired)
            _INDEX_STORE_PATH = desired_key

            legacy_exists = any(p.is_file() for p in (
                _LEGACY_DIR_CACHE_FILE,
                _LEGACY_IMG_CACHE_FILE,
                _LEGACY_INDEX_META_FILE,
            ))
            try:
                marker_host = _LEGACY_IMPORT_MARKER.read_text(encoding="utf-8").strip()
            except OSError:
                marker_host = ""
            if host and legacy_exists and (not marker_host or marker_host == host):
                imported = _INDEX_STORE.import_legacy(
                    _LEGACY_DIR_CACHE_FILE,
                    _LEGACY_IMG_CACHE_FILE,
                    _LEGACY_INDEX_META_FILE,
                )
                try:
                    _LEGACY_IMPORT_MARKER.write_text(host, encoding="utf-8")
                except OSError:
                    pass
                if imported.get("imported"):
                    print(
                        "  imported legacy caches into SQLite: "
                        f"{imported['directories']} directories, "
                        f"{imported['images']} disk images"
                    )
        return _INDEX_STORE


INDEXJOB = {
    "running": False,
    "mode": "ultimate",
    "root": "",
    "source": "",
    "dirs": 0,
    "files": 0,
    "images": 0,
    "images_cached": 0,
    "bytes_read": 0,
    "scan_errors": 0,
    "error_samples": [],
    "current": "",
    "started": 0.0,
    "stop": False,
    "error": "",
    "paused": False,
    "manual_paused": False,
    "pause_reason": "",
    "pending_dirs": 0,
    "elapsed": 0.0,
    "dirs_per_sec": 0.0,
    "images_per_sec": 0.0,
    "files_per_sec": 0.0,
    "eta_secs": None,
    "eta_source": "",
    "previous_total": 0,
    "verification": False,
    "dirs_new": 0,
    "dirs_changed": 0,
    "dirs_unchanged": 0,
    "images_new": 0,
    "images_changed": 0,
}
_INDEX_JOB_LOCK = threading.RLock()
_INDEX_PAUSE_COND = threading.Condition(_INDEX_JOB_LOCK)


def _index_update_progress(*, pending_dirs: int | None = None) -> None:
    with _INDEX_JOB_LOCK:
        if pending_dirs is not None:
            INDEXJOB["pending_dirs"] = max(0, int(pending_dirs))
        if not INDEXJOB["started"]:
            return
        elapsed = max(0.001, time.time() - float(INDEXJOB["started"]))
        INDEXJOB["elapsed"] = round(elapsed, 1)
        INDEXJOB["dirs_per_sec"] = round(INDEXJOB["dirs"] / elapsed, 2)
        image_total = INDEXJOB["images"] + INDEXJOB["images_cached"]
        INDEXJOB["images_per_sec"] = round(image_total / elapsed, 2)
        INDEXJOB["files_per_sec"] = round(int(INDEXJOB.get("files", 0)) / elapsed, 2)

        work_done = INDEXJOB["dirs"] + image_total
        work_rate = work_done / elapsed if work_done else 0.0
        previous_total = int(INDEXJOB.get("previous_total") or 0)
        if previous_total > work_done and work_rate > 0:
            INDEXJOB["eta_secs"] = round((previous_total - work_done) / work_rate)
            INDEXJOB["eta_source"] = "previous index"
        elif INDEXJOB["dirs"] >= 20 and INDEXJOB["pending_dirs"] and INDEXJOB["dirs_per_sec"] > 0:
            # First-run estimate based on the currently discovered queue. It
            # may move as deeper folders are discovered, so the UI labels it ~.
            INDEXJOB["eta_secs"] = round(
                INDEXJOB["pending_dirs"] / INDEXJOB["dirs_per_sec"]
            )
            INDEXJOB["eta_source"] = "discovered queue"
        else:
            INDEXJOB["eta_secs"] = None
            INDEXJOB["eta_source"] = ""


def _index_wait_state(reason: str) -> None:
    with _INDEX_JOB_LOCK:
        INDEXJOB["paused"] = bool(reason)
        INDEXJOB["pause_reason"] = reason
        INDEXJOB["manual_paused"] = DEVICE_OP.background_paused()


def _background_device_call(reason: str, fn):
    """Run one low-priority device request, yielding to UI/status traffic."""
    try:
        with DEVICE_OP.operation(
            "background",
            reason,
            wait_callback=_index_wait_state,
            cancel_check=lambda: bool(INDEXJOB["stop"]),
        ):
            _index_wait_state("")
            return fn()
    except OperationCancelled:
        return None


def _image_cache_key(entry: dict, full_path: str) -> tuple[str, int, str]:
    return full_path, int(entry.get("size", 0) or 0), str(entry.get("mtime", "") or "")


def _directory_equivalent(previous, current, *, allow_timezone_shift: bool = False) -> bool:
    """Compare local/FTP listings while tolerating FAT timestamp quirks."""
    def keyed(entries):
        return {
            str(e.get("name", "")): (
                bool(e.get("dir")),
                int(e.get("size", 0) or 0),
                str(e.get("mtime", "") or ""),
            )
            for e in entries if e.get("name")
        }
    left, right = keyed(previous), keyed(current)
    if left.keys() != right.keys():
        return False
    for name, (is_dir, size, mtime) in left.items():
        other_dir, other_size, other_mtime = right[name]
        if is_dir != other_dir or size != other_size:
            return False
        if not IndexStore.mtime_equivalent(mtime, other_mtime, allow_timezone_shift=allow_timezone_shift):
            return False
    return True


def _index_worker(root: str):
    store = _index_store()
    t0 = time.monotonic()
    img_exts = (".d64", ".d71", ".d81")
    pend = deque([root])
    try:
        verification = bool(INDEXJOB.get("verification"))
        while pend and not INDEXJOB["stop"]:
            _index_update_progress(pending_dirs=len(pend))
            folder = pend.popleft()
            with _INDEX_JOB_LOCK:
                INDEXJOB["current"] = folder
            entries = _background_device_call(
                f"indexing {folder}", lambda: devfs.list_dir(folder)
            )
            if entries is None:
                if INDEXJOB["stop"]:
                    break
                continue
            previous_entries = store.get_directory(folder)
            if previous_entries is None:
                with _INDEX_JOB_LOCK:
                    INDEXJOB["dirs_new"] += 1
            elif _directory_equivalent(previous_entries, entries, allow_timezone_shift=verification):
                with _INDEX_JOB_LOCK:
                    INDEXJOB["dirs_unchanged"] += 1
            else:
                with _INDEX_JOB_LOCK:
                    INDEXJOB["dirs_changed"] += 1
            store.put_directory(folder, entries)
            with _INDEX_JOB_LOCK:
                INDEXJOB["dirs"] += 1
            base = "" if folder == "/" else folder
            for e in entries:
                if INDEXJOB["stop"]:
                    break
                full = base + "/" + e["name"]
                if e.get("dir"):
                    pend.append(full)
                    continue
                with _INDEX_JOB_LOCK:
                    INDEXJOB["files"] += 1
                if not e["name"].lower().endswith(img_exts):
                    continue
                path, size, mtime = _image_cache_key(e, full)
                cached = store.get_image_compatible(path, size, mtime, allow_timezone_shift=verification)
                if cached is not None:
                    with _INDEX_JOB_LOCK:
                        INDEXJOB["images_cached"] += 1
                    continue
                existed = store.has_image_path(path)
                data = _background_device_call(
                    f"reading disk image {full}", lambda p=full: devfs.fetch(p)
                )
                if data is None:
                    if INDEXJOB["stop"]:
                        break
                    continue
                with _INDEX_JOB_LOCK:
                    INDEXJOB["bytes_read"] += len(data)
                parse_error = ""
                try:
                    img = DiskImage(data, name_hint=full)
                    image_entries = [
                        {"name": f.name, "file_type": f.file_type,
                         "blocks": f.blocks}
                        for f in img.entries
                    ]
                    parse_ok = True
                except Exception as exc:
                    image_entries = []
                    parse_ok = False
                    parse_error = str(exc)
                    with _INDEX_JOB_LOCK:
                        INDEXJOB["scan_errors"] += 1
                        if len(INDEXJOB["error_samples"]) < 12:
                            INDEXJOB["error_samples"].append(f"{full}: {exc}")
                store.put_image(path, size, mtime, image_entries,
                                parse_ok=parse_ok, parse_error=parse_error)
                with _INDEX_JOB_LOCK:
                    INDEXJOB["images"] += 1
                    if existed:
                        INDEXJOB["images_changed"] += 1
                    else:
                        INDEXJOB["images_new"] += 1
                _index_update_progress(pending_dirs=len(pend))

        if not INDEXJOB["stop"]:
            meta = {
                "completed": time.strftime("%Y-%m-%d %H:%M"),
                "completed_at": time.time(),
                "dirs": INDEXJOB["dirs"],
                "images": INDEXJOB["images"] + INDEXJOB["images_cached"],
                "secs": round(time.monotonic() - t0, 1),
            }
            store.set_index_root(root, meta)
    except Exception as e:
        with _INDEX_JOB_LOCK:
            INDEXJOB["error"] = str(e)
    finally:
        _index_update_progress(pending_dirs=0)
        with _INDEX_JOB_LOCK:
            INDEXJOB["running"] = False
            INDEXJOB["paused"] = False
            INDEXJOB["manual_paused"] = False
            INDEXJOB["pause_reason"] = ""
            INDEXJOB["current"] = ""
        DEVICE_OP.set_background_paused(False)


def _local_pause_wait() -> bool:
    """Block a local scan while paused; return False when stopping."""
    with _INDEX_PAUSE_COND:
        while INDEXJOB["manual_paused"] and not INDEXJOB["stop"]:
            INDEXJOB["paused"] = True
            INDEXJOB["pause_reason"] = "paused by user"
            _INDEX_PAUSE_COND.wait(timeout=0.5)
        if INDEXJOB["pause_reason"] == "paused by user":
            INDEXJOB["paused"] = False
            INDEXJOB["pause_reason"] = ""
        return not bool(INDEXJOB["stop"])


def _local_index_progress(snapshot: dict) -> None:
    with _INDEX_JOB_LOCK:
        INDEXJOB["dirs"] = int(snapshot.get("dirs", 0))
        INDEXJOB["files"] = int(snapshot.get("files", 0))
        INDEXJOB["images"] = int(snapshot.get("images", 0))
        INDEXJOB["images_cached"] = int(snapshot.get("images_cached", 0))
        INDEXJOB["bytes_read"] = int(snapshot.get("bytes_read", 0))
        INDEXJOB["scan_errors"] = int(snapshot.get("errors", 0))
        INDEXJOB["error_samples"] = list(snapshot.get("error_samples", []))
        INDEXJOB["current"] = str(snapshot.get("current", INDEXJOB["current"]))
    _index_update_progress(pending_dirs=int(snapshot.get("pending_dirs", 0)))


def _local_index_worker(source_text: str, root: str):
    store = _index_store()
    source = None
    scan_id = ""
    t0 = time.monotonic()
    try:
        source = resolve_source(source_text)
        scan_id = store.begin_local_scan(root, str(source))
        print(f"  local USB index: {source} -> {root}")

        def is_cached(path: str, size: int, mtime: str) -> bool:
            return store.get_image(path, size, mtime) is not None

        def commit(directories, images, cached_paths):
            store.put_local_batch(scan_id, directories, images, cached_paths)

        summary = scan_local_tree(
            source,
            root,
            image_is_cached=is_cached,
            commit_batch=commit,
            stop_check=lambda: bool(INDEXJOB["stop"]),
            pause_wait=_local_pause_wait,
            progress=_local_index_progress,
        )
        summary["secs"] = round(time.monotonic() - t0, 1)
        _local_index_progress(summary)
        if not INDEXJOB["stop"]:
            completed = store.finish_local_scan(
                scan_id,
                root,
                str(source),
                summary,
                volume_id=volume_identity(source),
            )
            print(
                "  local USB index complete: "
                f"{completed['dirs']} directories, {completed['files']} files, "
                f"{completed['images']} disk images in {completed['secs']:.1f}s"
            )
        else:
            store.abort_local_scan(scan_id)
    except Exception as exc:
        if scan_id:
            try:
                store.abort_local_scan(scan_id)
            except Exception:
                pass
        with _INDEX_JOB_LOCK:
            INDEXJOB["error"] = str(exc)
    finally:
        _index_update_progress(pending_dirs=0)
        with _INDEX_PAUSE_COND:
            INDEXJOB["running"] = False
            INDEXJOB["paused"] = False
            INDEXJOB["manual_paused"] = False
            INDEXJOB["pause_reason"] = ""
            INDEXJOB["current"] = ""
            _INDEX_PAUSE_COND.notify_all()


@app.get("/api/local/volumes")
def local_volumes():
    """Return local drive/mount roots visible to the u64deck process."""
    return {"volumes": list_local_volumes(), "platform": os.name}


@app.post("/api/fs/index/local")
def fs_index_local_start(payload: dict = Body(...)):
    """Build the current device's SQLite index from a locally attached USB."""
    if INDEXJOB["running"]:
        raise HTTPException(409, "an index run is already in progress")
    if SID_INDEX_JOB["running"]:
        raise HTTPException(409, "wait for the SID metadata refresh to finish first")
    if not str(CFG.get("u64_host") or "").strip():
        raise HTTPException(409, "select the target Ultimate before building its local USB index")
    try:
        source = resolve_source(payload.get("source", ""))
        root = normalise_ultimate_root(payload.get("root", "/USB0"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    store = _index_store()
    counts = store.scope_counts(root)
    previous_total = int(counts["directories"]) + int(counts["images"])
    with _INDEX_PAUSE_COND:
        INDEXJOB.update({
            "running": True,
            "mode": "local",
            "root": root,
            "source": str(source),
            "dirs": 0,
            "files": 0,
            "images": 0,
            "images_cached": 0,
            "bytes_read": 0,
            "scan_errors": 0,
            "error_samples": [],
            "current": root,
            "stop": False,
            "started": time.time(),
            "error": "",
            "paused": False,
            "manual_paused": False,
            "pause_reason": "",
            "pending_dirs": 1,
            "elapsed": 0.0,
            "dirs_per_sec": 0.0,
            "files_per_sec": 0.0,
            "images_per_sec": 0.0,
            "eta_secs": None,
            "eta_source": "",
            "previous_total": previous_total,
            "verification": False,
            "dirs_new": 0,
            "dirs_changed": 0,
            "dirs_unchanged": 0,
            "images_new": 0,
            "images_changed": 0,
        })
    global _INDEX_THREAD
    _INDEX_THREAD = threading.Thread(
        target=_local_index_worker,
        args=(str(source), root),
        daemon=True,
        name="local-volume-index",
    )
    _INDEX_THREAD.start()
    return {
        "started": root,
        "source": str(source),
        "previous_total": previous_total,
        "read_only": True,
    }


@app.post("/api/fs/index")
def fs_index_start(payload: dict = Body(...)):
    if INDEXJOB["running"]:
        raise HTTPException(409, "an index run is already in progress")
    if SID_INDEX_JOB["running"]:
        raise HTTPException(409, "wait for the SID metadata refresh to finish first")
    root = (payload.get("root") or "/").rstrip("/") or "/"
    if root == "/" and not bool(payload.get("confirm_root")):
        raise HTTPException(
            409,
            "Indexing / scans every attached storage device and can take a "
            "long time. Open USB0 or a specific collection folder first, or "
            "confirm the full root scan.",
        )
    store = _index_store()
    previous = store.index_roots().get(root, {})
    previous_total = int(previous.get("dirs", 0)) + int(previous.get("images", 0))
    local_imports = store.local_imports()
    verification = any(
        (imp_root := (str(item.get("root") or "/").rstrip("/") or "/")) == "/"
        or root == imp_root
        or root.startswith(imp_root + "/")
        for item in local_imports
    )
    # A local USB import is already a complete index. Keep it available while
    # an optional FTP verification runs; browsing and searches should not
    # temporarily fall back to a full network walk. For a normal network
    # rebuild, retain the older behaviour and withdraw the completion marker.
    if not verification:
        store.invalidate_path(root)
    DEVICE_OP.set_background_paused(False)
    with _INDEX_JOB_LOCK:
        INDEXJOB.update({
            "running": True,
            "mode": "ultimate",
            "root": root,
            "source": "Ultimate FTP",
            "dirs": 0,
            "files": 0,
            "images": 0,
            "images_cached": 0,
            "bytes_read": 0,
            "scan_errors": 0,
            "error_samples": [],
            "current": root,
            "stop": False,
            "started": time.time(),
            "error": "",
            "paused": False,
            "manual_paused": False,
            "pause_reason": "",
            "pending_dirs": 1,
            "elapsed": 0.0,
            "dirs_per_sec": 0.0,
            "images_per_sec": 0.0,
            "eta_secs": None,
            "eta_source": "",
            "previous_total": previous_total,
            "verification": verification,
            "dirs_new": 0,
            "dirs_changed": 0,
            "dirs_unchanged": 0,
            "images_new": 0,
            "images_changed": 0,
        })
    global _INDEX_THREAD
    _INDEX_THREAD = threading.Thread(
        target=_index_worker,
        args=(root,),
        daemon=True,
        name="volume-index",
    )
    _INDEX_THREAD.start()
    return {"started": root, "previous_total": previous_total,
            "verification": verification}


@app.get("/api/fs/index/status")
def fs_index_status():
    store = _index_store()
    _index_update_progress()
    with _INDEX_JOB_LOCK:
        out = {k: v for k, v in INDEXJOB.items() if k != "stop"}
    out["indexed_roots"] = store.index_roots()
    out["local_imports"] = store.local_imports()
    out["dirs_cached_total"] = store.directory_count()
    snap = DEVICE_OP.snapshot()
    out["device_queue"] = {
        "active_priority": snap.active_priority,
        "active_reason": snap.active_reason,
        "waiting_interactive": snap.waiting_interactive,
        "waiting_status": snap.waiting_status,
        "waiting_background": snap.waiting_background,
    }
    return out


@app.post("/api/fs/index/pause")
def fs_index_pause(payload: dict = Body(...)):
    if not INDEXJOB["running"]:
        raise HTTPException(409, "no index run is in progress")
    paused = payload.get("paused")
    if not isinstance(paused, bool):
        raise HTTPException(400, "paused must be true or false")
    mode = str(INDEXJOB.get("mode") or "ultimate")
    if mode == "ultimate":
        DEVICE_OP.set_background_paused(paused, "paused by user")
    with _INDEX_PAUSE_COND:
        INDEXJOB["manual_paused"] = paused
        if paused:
            INDEXJOB["paused"] = True
            INDEXJOB["pause_reason"] = "paused by user"
        elif INDEXJOB["pause_reason"] == "paused by user":
            INDEXJOB["paused"] = False
            INDEXJOB["pause_reason"] = ""
        _INDEX_PAUSE_COND.notify_all()
    return {"paused": paused}


@app.post("/api/fs/index/stop")
def fs_index_stop():
    with _INDEX_PAUSE_COND:
        INDEXJOB["stop"] = True
        INDEXJOB["manual_paused"] = False
        _INDEX_PAUSE_COND.notify_all()
    DEVICE_OP.set_background_paused(False)
    DEVICE_OP.wake()
    return {"stopping": True}


def _fs_search_events(root: str, query: str, inside: bool,
                      max_results: int, max_dirs: int, max_images: int,
                      budget: float):
    """Generator of search events: {'type':'hit'|'progress'|'done', ...}."""
    store = _index_store()
    t0 = time.monotonic()

    # A completed index that covers this folder can be searched directly in
    # SQLite without walking tens of thousands of cached directories in Python.
    cover = store.complete_cover(root)
    if cover is not None:
        hits = store.search_cached(
            root, query, inside_images=inside, limit=max_results
        )
        for hit in hits:
            yield {"type": "hit", **hit}
        counts = store.scope_counts(root)
        yield {
            "type": "done",
            "dirs": 0,
            "dirs_cached": counts["directories"],
            "images": 0,
            "images_cached": counts["images"],
            "hits": len(hits),
            "truncated": f"result cap ({max_results}) reached"
                         if len(hits) >= max_results else None,
            "elapsed": round(time.monotonic() - t0, 3),
            "sqlite": True,
        }
        return

    pend = deque([root])
    dirs_walked = images_opened = images_cached = dirs_cached = hits = 0
    stopped = ""
    img_exts = (".d64", ".d71", ".d81")
    last_prog = 0.0

    def prog(force=False):
        nonlocal last_prog
        now = time.monotonic()
        if force or now - last_prog > 0.4:
            last_prog = now
            return {"type": "progress", "dirs": dirs_walked,
                    "dirs_cached": dirs_cached,
                    "images": images_opened, "images_cached": images_cached,
                    "hits": hits, "elapsed": round(now - t0, 1),
                    "scanning": pend[0] if pend else ""}
        return None

    while pend and not stopped:
        folder = pend.popleft()
        if max_dirs and dirs_walked >= max_dirs:
            stopped = f"dir cap ({max_dirs}) reached"; break
        if budget and time.monotonic() - t0 > budget:
            stopped = f"time budget ({budget:.0f}s) reached"; break
        dirs_walked += 1
        p = prog()
        if p:
            yield p
        entries = store.get_directory(folder)
        if entries is None:
            try:
                entries = devfs.list_dir(folder)
                store.put_directory(folder, entries)
            except Exception:
                continue
        else:
            dirs_cached += 1
        base = "" if folder == "/" else folder
        for e in sorted(entries, key=lambda x: _natkey(x["name"])):
            full = base + "/" + e["name"]
            low = e["name"].lower()
            if e.get("dir"):
                pend.append(full)
                if query in low:
                    hits += 1
                    yield {"type": "hit", "kind": "dir", "path": full,
                           "name": e["name"]}
            else:
                if query in low:
                    hits += 1
                    yield {"type": "hit", "kind": "file", "path": full,
                           "name": e["name"], "size": e.get("size", 0)}
                if inside and low.endswith(img_exts):
                    path, size, mtime = _image_cache_key(e, full)
                    cached = store.get_image_compatible(path, size, mtime)
                    if cached is None:
                        if max_images and images_opened >= max_images:
                            continue
                        if budget and time.monotonic() - t0 > budget:
                            stopped = f"time budget ({budget:.0f}s) reached"
                            break
                        images_opened += 1
                        p = prog(force=True)
                        if p:
                            yield p
                        parse_error = ""
                        try:
                            img = DiskImage(devfs.fetch(full), name_hint=full)
                            cached = [
                                {"name": f.name, "file_type": f.file_type,
                                 "blocks": f.blocks}
                                for f in img.entries
                            ]
                            parse_ok = True
                        except Exception as exc:
                            cached = []
                            parse_ok = False
                            parse_error = str(exc)
                        store.put_image(path, size, mtime, cached,
                                        parse_ok=parse_ok, parse_error=parse_error)
                    else:
                        images_cached += 1
                    for idx, f in enumerate(cached):
                        if query in f["name"].lower():
                            hits += 1
                            yield {"type": "hit", "kind": "in-image",
                                   "path": full, "name": f["name"],
                                   "index": idx,
                                   "file_type": f["file_type"],
                                   "blocks": f["blocks"]}
            if hits >= max_results:
                stopped = f"result cap ({max_results}) reached"; break
    yield {"type": "done", "dirs": dirs_walked, "dirs_cached": dirs_cached,
           "images": images_opened,
           "images_cached": images_cached, "hits": hits,
           "truncated": stopped or None,
           "elapsed": round(time.monotonic() - t0, 1),
           "sqlite": False}


def _fs_search_params(payload: dict):
    root = (payload.get("root") or "/").rstrip("/") or "/"
    query = str(payload.get("query") or "").strip().lower()
    if len(query) < 2:
        raise HTTPException(400, "query needs at least 2 characters")
    return dict(root=root, query=query,
                inside=bool(payload.get("inside_images", True)),
                max_results=min(int(payload.get("max_results", 300)), 1000),
                max_dirs=int(payload.get("max_dirs", 0)),
                max_images=int(payload.get("max_images", 0)),
                budget=(lambda b: 0.0 if b <= 0 else min(b, 1800))(
                    float(payload.get("budget_secs", 60))))


@app.post("/api/fs/search")
def fs_search(payload: dict = Body(...)):
    params = _fs_search_params(payload)
    results, done = [], {}
    for ev in _fs_search_events(**params):
        if ev["type"] == "hit":
            e = dict(ev); e.pop("type"); results.append(e)
        elif ev["type"] == "done":
            done = ev
    return {"query": params["query"], "root": params["root"],
            "results": results, "dirs_walked": done.get("dirs", 0),
            "dirs_cached": done.get("dirs_cached", 0),
            "images_opened": done.get("images", 0),
            "images_cached": done.get("images_cached", 0),
            "truncated": done.get("truncated"),
            "elapsed": done.get("elapsed", 0),
            "sqlite": done.get("sqlite", False)}


@app.post("/api/fs/search/stream")
def fs_search_stream(payload: dict = Body(...)):
    params = _fs_search_params(payload)

    def gen():
        for ev in _fs_search_events(**params):
            yield json.dumps(ev) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


# --- cache statistics -----------------------------------------------------

@app.get("/api/cache/stats")
def cache_stats():
    store = _index_store()
    stats = store.stats()
    sl_cache = ROOT / ".songlengths.cache"
    legacy_counts = store.metadata_get("legacy_import_counts", "")
    try:
        legacy_import = json.loads(legacy_counts) if legacy_counts else {}
    except ValueError:
        legacy_import = {}
    return {
        "database": {
            "path": store.path.name,
            "disk_bytes": stats["disk_bytes"],
            "directories": stats["directories"],
            "file_entries": stats["file_entries"],
            "images": stats["images"],
            "image_entries": stats["image_entries"],
            "parse_failures": stats.get("parse_failures", 0),
            "sid_metadata": stats.get("sid_metadata", 0),
            "sid_index_runs": store.sid_index_runs(),
            "legacy_import": legacy_import,
            "local_imports": store.local_imports(),
            "migration": dict(_INDEX_MIGRATION_STATUS),
        },
        # Backward-compatible shape for older frontends.
        "image_cache": {
            "images": stats["images"],
            "file_entries": stats["image_entries"],
            "disk_bytes": stats["disk_bytes"],
        },
        "songlengths": {"entries": len(SONGLENGTHS),
                        "state": SL_STATE["state"],
                        "cached_on_disk": sl_cache.is_file()},
        "hvsc_index": {"paths": len(HVSC_INDEX)},
    }


@app.get("/api/cache/parse_errors")
def cache_parse_errors(limit: int = Query(200)):
    errors = _index_store().parse_errors(limit)
    return {"count": len(errors), "errors": errors}


@app.post("/api/cache/clear_images")
def cache_clear_images():
    if INDEXJOB["running"]:
        raise HTTPException(409, "stop the indexer before clearing the image cache")
    return {"cleared": _index_store().clear_images(), "roots_invalidated": True}


@app.post("/api/cache/clear_index")
def cache_clear_index():
    if INDEXJOB["running"]:
        raise HTTPException(409, "stop the indexer before clearing the storage index")
    if SID_INDEX_JOB["running"]:
        raise HTTPException(409, "stop the SID metadata refresh before clearing the storage index")
    previous = _index_store().clear_all()
    return {"cleared": previous}


# --- blank disk creation --------------------------------------------------

_CREATE_KINDS = {"d64": "create_d64", "d71": "create_d71",
                 "d81": "create_d81", "dnp": "create_dnp"}


@app.post("/api/fs/create_disk")
def fs_create_disk(payload: dict = Body(...)):
    """Create a blank, formatted disk image on Ultimate storage via the
    firmware's own creator. kinds: d64 (tracks 35-41), d71, d81,
    dnp (tracks required). G64 isn't exposed by the firmware API."""
    kind = str(payload.get("kind", "d64")).lower()
    if kind not in _CREATE_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(_CREATE_KINDS)}")
    # The top-level '/' shown by the browser is a virtual device list
    # (USB0, SD, Flash, Temp), not a writable filesystem directory.  The
    # firmware returns the misleading "PATH DOESN'T EXIST" if asked to create
    # a file there, so reject it with a useful message before making the call.
    folder = "/" + str(payload.get("folder") or "/").strip("/")
    if folder == "/":
        raise HTTPException(
            400,
            "Open a storage device or folder (for example USB0) before "
            "creating a disk image; the Ultimate's top-level / is virtual "
            "and cannot contain files.",
        )
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise HTTPException(400, "name must be a file name, not a path")
    if not name.lower().endswith("." + kind):
        name += "." + kind
    diskname = (payload.get("diskname") or name.rsplit(".", 1)[0])[:16]
    params = {"diskname": diskname}
    tracks = payload.get("tracks")
    if kind == "d64":
        params["tracks"] = int(tracks or 35)
        if not 35 <= params["tracks"] <= 41:
            raise HTTPException(400, "d64 tracks must be 35-41")
    elif kind == "dnp":
        if not tracks:
            raise HTTPException(400, "dnp needs tracks (1-255)")
        params["tracks"] = int(tracks)
    path = f"{folder}/{name}"
    # Build the URL by hand: the firmware expects %20 rather than '+' in the
    # query string.  Quote the path as well so spaces, # and other valid FAT
    # filename characters cannot be interpreted as URL syntax.
    from urllib.parse import quote as _q
    quoted_path = _q(path, safe="/")
    qs = "&".join(f"{k}={_q(str(v), safe='')}" for k, v in params.items())
    was_indexing = bool(INDEXJOB["running"] and INDEXJOB.get("mode") == "ultimate")
    try:
        # Formatting a disk is a higher-priority mutation. Pause background
        # FTP indexing, wait for any in-flight transfer to finish, and allow
        # more than the normal 8-second REST timeout for slow USB media.
        with DEVICE_OP.operation("interactive", "creating blank disk"):
            result = rest.put(
                f"/v1/files{quoted_path}:{_CREATE_KINDS[kind]}?{qs}",
                request_timeout=30.0,
            )
        # Older firmware responses do not always echo the created path.
        if isinstance(result, dict):
            result.setdefault("path", path)
            result.setdefault("index_was_paused", was_indexing)
        # The containing directory has changed, so a previously complete
        # SQLite index must not claim it still represents the live volume.
        try:
            _index_store().invalidate_path(folder)
        except Exception as cache_error:
            _warn_event("invalidate-index", f"could not invalidate storage index: {cache_error}")
        return result
    except httpx.TimeoutException:
        try:
            _index_store().invalidate_path(folder)
        except Exception:
            pass
        raise HTTPException(
            504,
            "The Ultimate did not answer the disk-creation request within "
            "30 seconds. Refresh this folder before retrying because the "
            "image may still have been created.",
        )
    except UltimateError as e:
        message = str(e)
        if "PATH DOESN'T EXIST" in message.upper():
            raise HTTPException(
                400,
                f"The Ultimate could not find or write to {folder}. "
                "Open a mounted storage device/folder and try again.",
            )
        if kind in {"d71", "d81", "dnp"} and any(token in message.upper() for token in ("UNKNOWN", "NOT FOUND", "UNSUPPORTED", "404")):
            raise HTTPException(
                400,
                f"This Ultimate firmware does not appear to support blank {kind.upper()} creation through the REST API. "
                "Create it from the on-device U64 menu or update the firmware.",
            )
        err(e)
    except httpx.HTTPError as e:
        err(e)


# --- disk swap (multi-disk software, mid-demo) ---------------------------
# A swap set is the ordered list of disk images belonging together (disk 1,
# disk 2, side A/B...). Swapping mounts the chosen image into the same
# drive WITHOUT a reset, so running software just sees the disk change —
# exactly like walking to the machine and swapping floppies.

import re as _re

SWAP = {"items": [], "index": -1, "drive": "a", "mode": "readwrite",
        "source": "none", "decision": {}}


def _natkey(name: str):
    return [int(t) if t.isdigit() else t.lower()
            for t in _re.split(r"(\d+)", name)]


_SWAP_IMAGE_EXTENSIONS = (".d64", ".d71", ".d81", ".g64")
_SWAP_MARKER_ALIASES = {
    "disk": "disk", "disc": "disk",
    "side": "side", "part": "part",
    "volume": "volume", "vol": "volume",
}


def _swap_normalize_title(value: str) -> str:
    """Normalise separators without weakening the actual title match."""
    return _re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _swap_token(value: str):
    value = value.casefold()
    if value.isdigit():
        return "number", (0, int(value))
    return "letter", (1, value)


def _swap_signature(filename: str):
    """Return a strict multi-disk family signature and ordering token.

    Only an explicit disk marker (disk/disc/side/part/volume), an ``N of M``
    suffix, or a separator-delimited trailing number is recognised.  This is
    intentionally conservative: an uncertain filename becomes a one-disk set
    rather than pulling unrelated images from the folder.
    """
    name = filename.rsplit("/", 1)[-1]
    stem, dot, ext = name.rpartition(".")
    ext = "." + ext.casefold() if dot else ""
    if ext not in _SWAP_IMAGE_EXTENSIONS or not stem.strip():
        return None
    stem = stem.strip()

    def marked(base: str, body: str):
        match = _re.fullmatch(
            r"(?P<marker>disk|disc|side|part|volume|vol)[\s._-]*(?P<token>\d+|[a-z])",
            body.strip(),
            flags=_re.IGNORECASE,
        )
        if not match:
            return None
        title = _swap_normalize_title(base)
        if not title:
            return None
        token_kind, token_sort = _swap_token(match.group("token"))
        marker = _SWAP_MARKER_ALIASES[match.group("marker").casefold()]
        return (ext, "marked", title, marker, token_kind), token_sort

    def numbered_total(base: str, body: str):
        match = _re.fullmatch(
            r"(?P<token>\d+)\s*(?:of|/)\s*(?P<total>\d+)",
            body.strip(),
            flags=_re.IGNORECASE,
        )
        if not match:
            return None
        title = _swap_normalize_title(base)
        if not title:
            return None
        token_kind, token_sort = _swap_token(match.group("token"))
        return (ext, "of-total", title, int(match.group("total")), token_kind), token_sort

    # Parenthesised/bracketed forms: Game (Disk 1), Game [Side B], Game (1 of 3).
    wrapped = _re.fullmatch(
        r"(?P<base>.+?)[\s._-]*[\(\[]\s*(?P<body>[^\)\]]+)\s*[\)\]]",
        stem,
    )
    if wrapped:
        result = marked(wrapped.group("base"), wrapped.group("body"))
        if result:
            return result
        result = numbered_total(wrapped.group("base"), wrapped.group("body"))
        if result:
            return result

    # Explicit markers must be separated from the title, preventing a title
    # such as "Riverside1" from being misread as "River / side 1".  A stable
    # release/language suffix after the disk token is allowed, e.g.
    # ``ThePhoenixCode-Disk1-BZ.d64`` / ``ThePhoenixCode-Disk2-BZ.d64``.  The
    # suffix becomes part of the family signature, so unrelated editions are
    # never grouped together.
    explicit = _re.fullmatch(
        r"(?P<base>.+?)[\s._-]+(?P<marker>disk|disc|side|part|volume|vol)"
        r"[\s._-]*(?P<token>\d+|[a-z])"
        r"(?:[\s._-]+(?P<tail>.+?))?",
        stem,
        flags=_re.IGNORECASE,
    )
    if explicit:
        result = marked(
            explicit.group("base"),
            explicit.group("marker") + explicit.group("token"),
        )
        if result:
            family, token_sort = result
            tail = _swap_normalize_title(explicit.group("tail") or "")
            return family + (tail,), token_sort

    of_total = _re.fullmatch(
        r"(?P<base>.+?)[\s._-]+(?P<token>\d+)\s*(?:of|/)\s*(?P<total>\d+)",
        stem,
        flags=_re.IGNORECASE,
    )
    if of_total:
        return numbered_total(
            of_total.group("base"),
            of_total.group("token") + " of " + of_total.group("total"),
        )

    # Generic numbered series such as Scratch-1.d64 / Scratch-2.d64.  The
    # separator and complete normalised prefix are mandatory, and the caller
    # will only accept the family when at least two siblings match.
    trailing = _re.fullmatch(r"(?P<base>.+?)[\s._-]+(?P<token>\d+)", stem)
    if trailing:
        title = _swap_normalize_title(trailing.group("base"))
        if title:
            token_kind, token_sort = _swap_token(trailing.group("token"))
            return (ext, "numbered", title, token_kind), token_sort
    return None


def _swap_group_candidates(current_name: str, sibling_names) -> list[str]:
    """Return only siblings confidently belonging to the current disk set."""
    current_name = current_name.rsplit("/", 1)[-1]
    current_signature = _swap_signature(current_name)
    if current_signature is None:
        return [current_name]
    family, _current_token = current_signature
    matches = []
    seen = set()
    for name in sibling_names:
        signature = _swap_signature(name)
        if signature is None or signature[0] != family:
            continue
        folded = name.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        matches.append((signature[1], name))

    if current_name.casefold() not in seen:
        matches.append((_current_token, current_name))

    # One apparent member is not evidence of a set.  Stay solo rather than
    # guessing, while the manual Disk Swap Queue remains available.
    if len(matches) < 2:
        return [current_name]
    matches.sort(key=lambda item: (item[0], _natkey(item[1])))
    return [name for _token, name in matches]


def _swap_decision(current_name: str, names: list[str], source: str) -> dict:
    if len(names) > 1:
        return {
            "kind": "related",
            "count": len(names),
            "message": f"Disk Swap: {len(names)} related images found",
            "detail": " → ".join(names),
            "source": source,
        }
    return {
        "kind": "single",
        "count": 1,
        "message": "No confident multi-disk match — using this image only",
        "detail": current_name,
        "source": source,
    }


def _swap_state():
    return {"items": [{"label": i["label"]} for i in SWAP["items"]],
            "index": SWAP["index"], "drive": SWAP["drive"],
            "mode": SWAP.get("mode", "unlinked"),
            "source": SWAP.get("source", "none"),
            "decision": dict(SWAP.get("decision") or {})}


def _swap_build_from_device(image_path: str, drive: str, mode: str,
                            source: str = "auto") -> dict:
    """Populate a conservative swap set from related sibling filenames."""
    folder = image_path.rsplit("/", 1)[0] or "/"
    current_name = image_path.rsplit("/", 1)[-1]
    try:
        entries = devfs.list_dir(folder)
    except Exception:
        entries = []
    sibling_names = [
        e["name"] for e in entries
        if not e.get("dir") and e["name"].casefold().endswith(_SWAP_IMAGE_EXTENSIONS)
    ]
    names = _swap_group_candidates(current_name, sibling_names)
    items = [{"label": name, "kind": "device",
              "path": (folder.rstrip("/") + "/" + name)} for name in names]
    idx = next((i for i, it in enumerate(items)
                if it["label"].casefold() == current_name.casefold()), 0)
    decision = _swap_decision(current_name, names, source)
    SWAP.update({"items": items, "index": idx, "drive": drive, "mode": mode,
                 "source": source, "decision": decision})
    return decision


def _swap_response(out):
    """Attach the current matcher decision without hiding the device response."""
    result = dict(out) if isinstance(out, dict) else {"result": out}
    result["swap"] = _swap_state()
    result["swap_decision"] = dict(SWAP.get("decision") or {})
    return result


def _drive_image_path(value: dict, remembered: dict) -> str:
    """Extract a mounted device path without resurrecting an ejected image.

    Firmware variants use several field names.  An explicitly present but
    empty image field means the drive has no media, so stale local state must
    not be used.  A basename-only report can reuse the remembered full path
    when both names agree.
    """
    keys = ("image_file", "image_path", "path", "filename")
    present = [key for key in keys if key in value]
    remembered_path = remembered.get("path")
    remembered_path = remembered_path if isinstance(remembered_path, str) else ""
    for key in present:
        candidate = value.get(key)
        if not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        if candidate.startswith("/"):
            return candidate
        if candidate and remembered_path.startswith("/"):
            if remembered_path.rsplit("/", 1)[-1].casefold() == candidate.casefold():
                return remembered_path
    if present:
        return ""
    return remembered_path if remembered_path.startswith("/") else ""


def _drive_report_rows(out: dict):
    for row in out.get("drives", []) if isinstance(out, dict) else []:
        if not isinstance(row, dict):
            continue
        for drive in ("a", "b"):
            value = row.get(drive)
            if isinstance(value, dict):
                yield drive, value


def _reconcile_swap_from_drives(out: dict) -> dict | None:
    """Restore or realign an automatic swap set from currently mounted media.

    Manual/upload queues are preserved when the mounted image still belongs to
    them. A different image mounted outside u64deck becomes the new automatic
    family, which also covers application restarts where SWAP is empty.
    """
    mounted = []
    for drive, value in _drive_report_rows(out):
        remembered = dict(MOUNT_STATE.get(drive) or {})
        path = _drive_image_path(value, remembered)
        if not path:
            continue
        raw_mode = remembered.get("mode") or value.get("mode") or value.get("mount_mode")
        try:
            mode = _mount_mode(raw_mode)
        except HTTPException:
            mode = _mount_mode(None)
        name = path.rsplit("/", 1)[-1]
        if remembered.get("path") != path or not remembered.get("name"):
            _remember_mount(drive, mode, path=path, name=name)
        mounted.append((drive, mode, path))

    if not mounted:
        if SWAP.get("source") in {"auto", "reconstructed"}:
            SWAP.update({"items": [], "index": -1, "source": "none", "decision": {}})
        return None

    # Keep an existing automatic or manual set when the actual mounted path is
    # one of its members; merely align the highlighted disk after a restart or
    # a physical/remote swap.
    for drive, mode, path in mounted:
        for index, item in enumerate(SWAP.get("items") or []):
            if item.get("kind") == "device" and str(item.get("path", "")).casefold() == path.casefold():
                SWAP.update({"index": index, "drive": drive, "mode": mode})
                return None

    # An explicitly armed manual or uploaded queue is user intent.  Keep it
    # even when Arm Queue was used without mounting its first member yet.
    if SWAP.get("source") in {"manual", "upload"} and SWAP.get("items"):
        return None

    # Prefer drive A when both contain images, matching the normal mount/run
    # workflow and the Screen tab's swap controls.
    drive, mode, path = next((row for row in mounted if row[0] == "a"), mounted[0])
    return _swap_build_from_device(path, drive, mode, source="reconstructed")


@app.get("/api/swap")
def swap_get():
    return _swap_state()


@app.post("/api/swap/set_paths")
def swap_set_paths(payload: dict = Body(...)):
    """Arm a swap set from explicitly chosen device paths, in given order.

    payload: {paths: [...], drive?, mode?, mount_first?: bool}
    """
    paths = [p for p in (payload.get("paths") or []) if isinstance(p, str) and p]
    if not paths:
        raise HTTPException(400, "paths required")
    drive = payload.get("drive", "a")
    mode = payload.get("mode", "readwrite")
    items = [{"label": p.rsplit("/", 1)[-1], "kind": "device", "path": p}
             for p in paths]
    SWAP.update({"items": items, "index": -1, "drive": drive, "mode": mode,
                 "source": "manual", "decision": {
                     "kind": "manual", "count": len(items),
                     "message": f"Disk Swap: manual queue armed with {len(items)} images",
                     "detail": " → ".join(i["label"] for i in items),
                     "source": "manual",
                 }})
    if payload.get("mount_first"):
        return _swap_go(0)
    return _swap_state()


@app.post("/api/swap/upload")
async def swap_upload(drive: str = Form("a"), mode: str = Form("readwrite"),
                      files: list[UploadFile] = File(...)):
    """Build a swap set from uploaded images (kept in memory) and mount #1."""
    if len(files) > MAX_SWAP_FILES:
        raise HTTPException(413, f"swap sets are limited to {MAX_SWAP_FILES} files")
    items = []
    total = 0
    for f in files:
        if not (f.filename or "").lower().endswith((".d64", ".d71", ".d81", ".g64")):
            continue
        name, data = await _read_upload(f, MAX_SWAP_TOTAL)
        total += len(data)
        if total > MAX_SWAP_TOTAL:
            raise HTTPException(413, "swap set exceeds the 96 MiB memory limit")
        items.append({"label": name, "kind": "mem",
                      "name": name, "data": data})
    if not items:
        raise HTTPException(400, "no disk images among the files")
    items.sort(key=lambda i: _natkey(i["label"]))
    SWAP.update({"items": items, "index": -1, "drive": drive, "mode": mode,
                 "source": "upload", "decision": {
                     "kind": "manual", "count": len(items),
                     "message": f"Disk Swap: local set armed with {len(items)} images",
                     "detail": " → ".join(i["label"] for i in items),
                     "source": "upload",
                 }})
    return await run_in_threadpool(_swap_go, 0)


def _swap_go(index: int):
    if not SWAP["items"]:
        raise HTTPException(400, "no swap set loaded — mount a disk first")
    index = index % len(SWAP["items"])
    it = SWAP["items"][index]
    try:
        if it["kind"] == "device":
            rest.mount_path(SWAP["drive"], it["path"], mode=SWAP["mode"])
        else:
            rest.mount_attachment(SWAP["drive"], it["name"], it["data"],
                                  mode=SWAP["mode"])
        _remember_mount(SWAP["drive"], SWAP["mode"],
                        path=it.get("path", ""), name=it.get("name", it.get("label", "")))
    except (UltimateError, httpx.HTTPError) as e:
        err(e)
    SWAP["index"] = index
    out = _swap_state()
    out["swapped_to"] = it["label"]
    return out


@app.put("/api/swap/go")
def swap_go(index: int = Query(...)):
    return _swap_go(index)


@app.put("/api/swap/next")
def swap_next():
    return _swap_go(SWAP["index"] + 1)


@app.put("/api/swap/prev")
def swap_prev():
    return _swap_go(SWAP["index"] - 1)


# --- SID jukebox ---------------------------------------------------------
import hashlib
import threading as _threading

JUKE = {"items": [], "index": -1, "playing": False, "shuffle": False,
        "song": 0, "timer": None, "folder": "", "loading": False,
        "source": "", "generation": 0}
SONGLENGTHS = {}          # md5 -> [seconds per subsong]
SL_STATE = {"state": "idle"}   # idle | loading | ready | empty | error
HVSC_INDEX = []           # [(lowercase rel path, rel path)] from Songlengths

SID_INDEX_JOB = {
    "running": False,
    "mode": "",
    "root": "",
    "source": "",
    "dirs": 0,
    "files": 0,
    "parsed": 0,
    "cached": 0,
    "errors": 0,
    "error_samples": [],
    "bytes_read": 0,
    "current": "",
    "pending_dirs": 0,
    "started": 0.0,
    "elapsed": 0.0,
    "files_per_sec": 0.0,
    "stop": False,
    "paused": False,
    "manual_paused": False,
    "pause_reason": "",
    "force": False,
    "error": "",
}
_SID_INDEX_LOCK = threading.RLock()
_SID_INDEX_PAUSE = threading.Condition(_SID_INDEX_LOCK)
_SID_INDEX_THREAD = None


def _parse_sid(data: bytes, *, compute_md5: bool = True) -> dict:
    """PSID/RSID header -> metadata. Returns {} if not a SID."""
    if len(data) < 0x76 or data[:4] not in (b"PSID", b"RSID"):
        return {}
    def w(o): return (data[o] << 8) | data[o + 1]
    def s(o): return data[o:o + 32].split(b"\0")[0].decode("latin-1", "replace")
    version = w(4)
    meta = {"format": data[:4].decode(), "version": version,
            "songs": max(1, w(0x0E)), "start_song": max(1, w(0x10)),
            "name": s(0x16), "author": s(0x36), "released": s(0x56),
            "md5": hashlib.md5(data).hexdigest() if compute_md5 else "",
            "chip": "?", "clock": "?", "sids": 1}
    if version >= 2 and len(data) >= 0x78:
        flags = w(0x76)
        meta["clock"] = {1: "PAL", 2: "NTSC", 3: "PAL/NTSC"}.get((flags >> 2) & 3, "?")
        model = {1: "6581", 2: "8580", 3: "either"}.get((flags >> 4) & 3, "?")
        meta["chip"] = model
        # v3/v4: 2nd/3rd SID addresses + their models
        sids = 1
        if version >= 3 and len(data) > 0x7A and data[0x7A]:
            sids = 2
            m2 = {1: "6581", 2: "8580", 3: "either"}.get((flags >> 6) & 3)
            if m2 and m2 != model:
                meta["chip"] = f"{model}+{m2}"
        if version >= 4 and len(data) > 0x7B and data[0x7B]:
            sids = 3
            m3 = {1: "6581", 2: "8580", 3: "either"}.get((flags >> 8) & 3)
            if m3 and m3 not in str(meta["chip"]).split("+"):
                meta["chip"] = f"{meta['chip']}+{m3}"
        meta["sids"] = sids
    return meta


def _parse_songlength_times(spec: str):
    out = []
    for tok in spec.split():
        m = _re.match(r"(\d+):(\d+(?:\.\d+)?)", tok)
        if m:
            out.append(int(m.group(1)) * 60 + float(m.group(2)))
    return out


def load_songlengths():
    """Load HVSC Songlengths.md5 (keyed by full-file md5) if configured.

    songlengths_path may be a LOCAL file, or a DEVICE path (e.g.
    /Usb0/HVSC/DOCUMENTS/Songlengths.md5) fetched over FTP and cached
    locally, so a device-resident HVSC needs no PC-side copy."""
    SONGLENGTHS.clear()
    path = CFG.get("songlengths_path") or ""
    if not path:
        return 0
    p = Path(path)
    text = None
    if p.is_file():
        text = p.read_text(errors="replace")
    else:
        cache = ROOT / ".songlengths.cache"
        try:
            data = devfs.fetch(path)
            text = data.decode(errors="replace")
            try:
                cache.write_text(text)
            except OSError:
                pass
        except Exception:
            if cache.is_file():
                text = cache.read_text(errors="replace")
    if text is None:
        return 0
    HVSC_INDEX.clear()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(";"):
            # "; /MUSICIANS/H/Hubbard_Rob/Sanxion.sid" — a free full index
            rel = line[1:].strip()
            if rel.startswith("/") and rel.lower().endswith(".sid"):
                HVSC_INDEX.append((rel.lower(), rel))
            continue
        if line.startswith("["):
            continue
        if "=" in line:
            md5, _, spec = line.partition("=")
            times = _parse_songlength_times(spec)
            if len(md5) == 32 and times:
                SONGLENGTHS[md5.lower()] = times
    return len(SONGLENGTHS)


def _juke_new_generation() -> int:
    JUKE["generation"] = int(JUKE.get("generation", 0)) + 1
    return JUKE["generation"]


def _juke_cancel_timer():
    t = JUKE.get("timer")
    if t:
        t.cancel()
        JUKE["timer"] = None


def _juke_state():
    now = None
    if 0 <= JUKE["index"] < len(JUKE["items"]):
        it = JUKE["items"][JUKE["index"]]
        now = {"label": it["label"], "meta": it["meta"], "song": JUKE["song"],
               "path": it.get("path", ""),
               "length": _juke_length(it, JUKE["song"])}
    return {"items": [{"label": i["label"], "meta": i["meta"],
                       "path": i.get("path", ""),
                       "lazy": i.get("data") is None,
                       "length": _juke_length(i, i["meta"].get("start_song", 1))}
                      for i in JUKE["items"]],
            "index": JUKE["index"], "playing": JUKE["playing"],
            "shuffle": JUKE["shuffle"], "now": now, "folder": JUKE["folder"],
            "loading": bool(JUKE.get("loading")),
            "source": JUKE.get("source", ""),
            "songlengths_loaded": len(SONGLENGTHS)}


def _juke_length(it, song: int):
    times = SONGLENGTHS.get(it["meta"].get("md5", ""))
    if times and 1 <= song <= len(times):
        return round(times[song - 1], 1)
    return None


def _sid_placeholder(name: str) -> dict:
    title = str(name or "SID").rsplit("/", 1)[-1]
    if title.lower().endswith(".sid"):
        title = title[:-4]
    return {"format": "", "version": 0, "songs": 1, "start_song": 1,
            "name": title, "author": "", "released": "", "md5": "",
            "chip": "?", "clock": "?", "sids": 1, "lazy": True}


def _sid_meta_from_row(row: dict | None, fallback_name: str = "SID") -> dict:
    """Translate a SQLite SID metadata row into the jukebox header shape."""
    meta = _sid_placeholder(fallback_name)
    if not row:
        return meta
    meta.update({
        "format": str(row.get("format") or ""),
        "version": int(row.get("version", 0) or 0),
        "songs": max(1, int(row.get("songs", 1) or 1)),
        "start_song": max(1, int(row.get("start_song", 1) or 1)),
        "name": str(row.get("title") or row.get("name") or meta["name"]),
        "author": str(row.get("author") or ""),
        "released": str(row.get("released") or ""),
        "md5": str(row.get("md5") or ""),
        "chip": str(row.get("chip") or "?"),
        "clock": str(row.get("clock") or "?"),
        "sids": max(1, int(row.get("sids", 1) or 1)),
        "lazy": True,
    })
    return meta


def _sid_metadata_for_paths(store, paths) -> dict[str, dict]:
    getter = getattr(store, "sid_metadata_for_paths", None)
    if not callable(getter):
        return {}
    try:
        return getter(paths)
    except Exception:
        return {}


def _sid_metadata_get(store, path: str) -> dict | None:
    getter = getattr(store, "sid_metadata_get", None)
    if not callable(getter):
        return None
    try:
        return getter(path)
    except Exception:
        return None


def _juke_lazy_item(path: str, name: str | None = None,
                    metadata: dict | None = None) -> dict:
    name = name or str(path).rsplit("/", 1)[-1]
    return {"label": name, "data": None,
            "meta": _sid_meta_from_row(metadata, name),
            "path": path}


def _juke_materialise(it: dict) -> dict:
    """Fetch and parse one lazy SID only when it is actually played.

    Bulk-fetching an entire folder after starting the Ultimate's native SID
    player can monopolise firmware FTP/REST services and make the web UI appear
    dead. Lazy entries keep the playlist responsive and limit each play action
    to one bounded device fetch.
    """
    if it.get("data") is not None and not it.get("meta", {}).get("lazy"):
        return it
    path = it.get("path") or ""
    if not path:
        raise HTTPException(400, "this play-queue entry has no device path")
    try:
        data = devfs.fetch(path)
    except Exception as e:
        err(e)
    meta = _parse_sid(data)
    if not meta:
        raise HTTPException(400, f"{it.get('label') or path} is not a valid SID")
    it.update({"data": data, "meta": meta,
               "label": it.get("label") or path.rsplit("/", 1)[-1]})
    try:
        _index_store().put_sid_metadata(
            path, len(data), "", meta, source="played SID"
        )
    except Exception:
        pass
    return it


def _juke_play(index: int, song: int = 0):
    if not JUKE["items"]:
        raise HTTPException(400, "jukebox is empty")
    index = index % len(JUKE["items"])
    it = _juke_materialise(JUKE["items"][index])
    song = song or it["meta"].get("start_song", 1)
    try:
        _run_cart_safe(lambda: rest.post_file("/v1/runners:sidplay",
                                              it["label"], it["data"],
                                              songnr=song))
    except (UltimateError, httpx.HTTPError) as e:
        err(e)
    JUKE.update({"index": index, "song": song, "playing": True})
    _juke_cancel_timer()
    length = _juke_length(it, song)
    if length is None:
        length = float(CFG.get("sid_default_secs", 180) or 0)
    if length > 0:
        t = _threading.Timer(length + 1.0, _juke_auto_next)
        t.daemon = True
        JUKE["timer"] = t
        t.start()
    out = _juke_state()
    out["started"] = it["label"]
    return out


def _juke_next_index():
    n = len(JUKE["items"])
    if n < 2:
        return JUKE["index"]
    if JUKE["shuffle"]:
        import random
        choices = [i for i in range(n) if i != JUKE["index"]]
        return random.choice(choices)
    return (JUKE["index"] + 1) % n


def _juke_auto_next():
    try:
        if JUKE["playing"] and JUKE["items"]:
            _juke_play(_juke_next_index())
    except Exception:
        pass


def _juke_add_bytes(label: str, data: bytes):
    meta = _parse_sid(data)
    if not meta:
        return False
    JUKE["items"].append({"label": label, "data": data, "meta": meta})
    return True


@app.get("/api/juke")
def juke_get():
    return _juke_state()


# --- saved playlists ------------------------------------------------------
_PLAYLISTS_FILE = ROOT / "playlists.json"


def _playlists_load() -> dict:
    value = _read_json(_PLAYLISTS_FILE, {})
    return value if isinstance(value, dict) else {}


def _playlists_save(pl: dict):
    try:
        _write_json_atomic(_PLAYLISTS_FILE, pl, indent=1)
    except OSError as e:
        raise HTTPException(500, f"could not save playlists: {e}")


@app.get("/api/playlists")
def playlists_list():
    pl = _playlists_load()
    return {"playlists": [{"name": k, "count": len(v.get("tunes", [])),
                           "saved": v.get("saved", "")}
                          for k, v in sorted(pl.items())]}


@app.post("/api/playlists/save")
def playlists_save(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "play queue name required")
    tunes = [it["path"] for it in JUKE["items"] if it.get("path")]
    local_only = len(JUKE["items"]) - len(tunes)
    if not tunes:
        raise HTTPException(400, "nothing saveable — play queue is empty or "
                                 "contains only locally-uploaded tunes")
    pl = _playlists_load()
    pl[name] = {"saved": __import__("time").strftime("%Y-%m-%d %H:%M"),
                "tunes": tunes}
    _playlists_save(pl)
    return {"saved": name, "count": len(tunes), "local_skipped": local_only}


@app.post("/api/playlists/load")
def playlists_load_one(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    pl = _playlists_load()
    if name not in pl:
        raise HTTPException(404, f"no saved play queue called {name!r}")
    paths = [path for path in pl[name].get("tunes", []) if path]
    metadata = _sid_metadata_for_paths(_index_store(), paths)
    items = [_juke_lazy_item(path, metadata=metadata.get(str(path).casefold()))
             for path in paths]
    if not items:
        raise HTTPException(400, "the saved play queue is empty")
    _juke_cancel_timer()
    _juke_new_generation()
    JUKE.update({"items": items, "index": -1, "playing": False, "song": 0,
                 "folder": name, "loading": False, "source": "saved play queue"})
    out = _juke_state()
    out["skipped"] = 0
    return out


@app.post("/api/playlists/delete")
def playlists_delete(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    pl = _playlists_load()
    if name not in pl:
        raise HTTPException(404, f"no saved play queue called {name!r}")
    del pl[name]
    _playlists_save(pl)
    return {"deleted": name}


@app.post("/api/juke/play_path")
def juke_play_path(payload: dict = Body(...)):
    """Fetch ONE sid from the device and play it immediately — the fast
    first beat while a folder playlist loads behind it."""
    path = payload.get("path") or ""
    if not path:
        raise HTTPException(400, "path required")
    try:
        data = devfs.fetch(path)
    except Exception as e:
        err(e)
    name = path.rsplit("/", 1)[-1]
    meta = _parse_sid(data)
    if not meta:
        raise HTTPException(400, f"{name} is not a valid SID")
    _juke_cancel_timer()
    _juke_new_generation()
    JUKE.update({"items": [{"label": name, "data": data, "meta": meta,
                            "path": path}],
                 "index": -1, "playing": False, "song": 0,
                 "folder": path.rsplit("/", 1)[0] or "/",
                 "loading": False, "source": "Ultimate storage"})
    try:
        _index_store().put_sid_metadata(path, len(data), "", meta,
                                        source="played SID")
    except Exception:
        pass
    return _juke_play(0)


@app.post("/api/juke/add_path")
def juke_add_path(payload: dict = Body(...)):
    """Append one device SID lazily without interrupting native playback."""
    path = payload.get("path") or ""
    if not path:
        raise HTTPException(400, "path required")
    name = path.rsplit("/", 1)[-1]
    if not name.lower().endswith(".sid"):
        raise HTTPException(400, "only .sid files can be added to the jukebox")
    _juke_new_generation()
    metadata = _sid_metadata_get(_index_store(), path)
    JUKE["items"].append(_juke_lazy_item(path, name, metadata))
    folder = path.rsplit("/", 1)[0] or "/"
    if JUKE["folder"] and JUKE["folder"] != folder:
        JUKE["folder"] = "custom picks"
    elif not JUKE["folder"]:
        JUKE["folder"] = folder
    out = _juke_state()
    out["added"] = name
    return out


@app.post("/api/juke/remove")
def juke_remove(payload: dict = Body(...)):
    i = int(payload.get("index", -1))
    if not (0 <= i < len(JUKE["items"])):
        raise HTTPException(400, "bad index")
    removing_current = (i == JUKE["index"])
    _juke_new_generation()
    JUKE["items"].pop(i)
    if removing_current:
        _juke_cancel_timer()
        JUKE["playing"] = False
        JUKE["index"] = -1
    elif JUKE["index"] > i:
        JUKE["index"] -= 1
    return _juke_state()


@app.post("/api/juke/folder")
def juke_folder(payload: dict = Body(...)):
    """Build a lazy playlist without bulk-fetching SIDs during playback."""
    folder = (payload.get("path") or "/").rstrip("/") or "/"
    store = _index_store()
    rows = store.files_in_directory(folder, ".sid", limit=300)
    source = "SQLite index"
    if rows:
        rows = sorted(rows, key=lambda r: _natkey(r["name"]))
    else:
        try:
            entries = devfs.list_dir(folder)
        except Exception as e:
            err(e)
        rows = [{"name": e["name"],
                 "path": (folder if folder != "/" else "") + "/" + e["name"]}
                for e in entries
                if not e.get("dir") and e["name"].lower().endswith(".sid")]
        rows.sort(key=lambda r: _natkey(r["name"]))
        rows = rows[:300]
        source = "Ultimate folder"
    if not rows:
        raise HTTPException(404, "no .sid files in that folder")
    metadata = _sid_metadata_for_paths(store, (row["path"] for row in rows))
    items = [_juke_lazy_item(
        row["path"], row["name"], metadata.get(str(row["path"]).casefold())
    ) for row in rows]
    # Keep the one already-materialised, currently playing item. Everything
    # else remains path-only until selected, so the native SID player never
    # triggers a background FTP storm.
    keep = (JUKE["playing"] and 0 <= JUKE["index"] < len(JUKE["items"])
            and JUKE["folder"].casefold() == folder.casefold())
    current = JUKE["items"][JUKE["index"]] if keep else None
    if not keep:
        _juke_cancel_timer()
    new_index = -1
    if current is not None:
        current_path = str(current.get("path") or "").casefold()
        new_index = next((i for i, it in enumerate(items)
                          if str(it.get("path") or "").casefold() == current_path), -1)
        if new_index >= 0:
            items[new_index] = current
        else:
            _juke_cancel_timer()
            keep = False
    _juke_new_generation()
    JUKE.update({"items": items, "index": new_index,
                 "playing": bool(keep and new_index >= 0),
                 "folder": folder, "loading": False, "source": source})
    out = _juke_state()
    out["skipped"] = 0
    out["lazy"] = True
    return out


@app.post("/api/juke/upload")
async def juke_upload(files: list[UploadFile] = File(...)):
    _juke_cancel_timer()
    _juke_new_generation()
    JUKE.update({"items": [], "index": -1, "playing": False, "song": 0,
                 "folder": "(local files)", "loading": False,
                 "source": "local upload"})
    skipped, total = 0, 0
    for f in files:
        try:
            name, data = await _read_upload(f, MAX_SID_UPLOAD)
        except HTTPException as e:
            if e.status_code == 413:
                skipped += 1
                continue
            raise
        total += len(data)
        if total > MAX_SID_TOTAL:
            raise HTTPException(413, "SID upload batch exceeds the 64 MiB limit")
        if not _juke_add_bytes(name, data):
            skipped += 1
    if not JUKE["items"]:
        raise HTTPException(400, "no valid SID files")
    out = _juke_state()
    out["skipped"] = skipped
    return out


def _looks_like_hvsc(path: str) -> bool:
    try:
        names = {e["name"].upper() for e in devfs.list_dir(path) if e.get("dir")}
    except Exception:
        return False
    return "MUSICIANS" in names and "DOCUMENTS" in names


def _hvsc_detect() -> str:
    """Find an HVSC root on device storage: drive roots and their first-level
    children (dirs named HVSC/C64Music checked first)."""
    try:
        drives = [e["name"] for e in devfs.list_dir("/") if e.get("dir")]
    except Exception:
        return ""
    for drive in drives:
        base = "/" + drive
        if _looks_like_hvsc(base):
            return base
        try:
            kids = [e["name"] for e in devfs.list_dir(base) if e.get("dir")]
        except Exception:
            continue
        kids.sort(key=lambda n: 0 if n.upper() in ("HVSC", "C64MUSIC") else 1)
        for kid in kids[:12]:
            cand = base + "/" + kid
            if _looks_like_hvsc(cand):
                return cand
    return ""


@app.get("/api/juke/hvsc")
def juke_hvsc(force: bool = Query(False)):
    """Configured or auto-detected HVSC root; wires songlengths on detect.
    force=true re-runs detection (collection moved to another stick etc.)."""
    path = "" if force else (CFG.get("hvsc_path") or "")
    detected = False
    if not path:
        path = _hvsc_detect()
        if path:
            detected = True
            CFG["hvsc_path"] = path
            sl = CFG.get("songlengths_path") or ""
            # (re)wire songlengths unless the user points at a local file
            if not sl or not Path(sl).is_file():
                CFG["songlengths_path"] = path + "/DOCUMENTS/Songlengths.md5"
            load_songlengths()
            save_config()
    return {"path": path or None, "detected": detected,
            "songlengths_loaded": len(SONGLENGTHS)}


def _sid_index_update_progress(*, pending_dirs: int | None = None) -> None:
    with _SID_INDEX_LOCK:
        if pending_dirs is not None:
            SID_INDEX_JOB["pending_dirs"] = max(0, int(pending_dirs))
        started = float(SID_INDEX_JOB.get("started") or 0)
        if not started:
            return
        elapsed = max(0.001, time.time() - started)
        SID_INDEX_JOB["elapsed"] = round(elapsed, 1)
        SID_INDEX_JOB["files_per_sec"] = round(
            int(SID_INDEX_JOB.get("files", 0)) / elapsed, 2
        )


def _sid_index_wait_state(reason: str) -> None:
    with _SID_INDEX_LOCK:
        SID_INDEX_JOB["paused"] = bool(reason)
        SID_INDEX_JOB["pause_reason"] = reason


def _sid_background_device_call(reason: str, fn):
    try:
        with DEVICE_OP.operation(
            "background",
            reason,
            wait_callback=_sid_index_wait_state,
            cancel_check=lambda: bool(SID_INDEX_JOB["stop"]),
        ):
            _sid_index_wait_state("")
            return fn()
    except OperationCancelled:
        return None


def _sid_local_pause_wait() -> bool:
    with _SID_INDEX_PAUSE:
        while SID_INDEX_JOB["manual_paused"] and not SID_INDEX_JOB["stop"]:
            SID_INDEX_JOB["paused"] = True
            SID_INDEX_JOB["pause_reason"] = "paused by user"
            _SID_INDEX_PAUSE.wait(timeout=0.5)
        if SID_INDEX_JOB["pause_reason"] == "paused by user":
            SID_INDEX_JOB["paused"] = False
            SID_INDEX_JOB["pause_reason"] = ""
        return not bool(SID_INDEX_JOB["stop"])


def _sid_index_progress(snapshot: dict) -> None:
    with _SID_INDEX_LOCK:
        for key in ("dirs", "files", "parsed", "cached", "errors",
                    "bytes_read", "pending_dirs"):
            if key in snapshot:
                SID_INDEX_JOB[key] = int(snapshot.get(key, 0) or 0)
        SID_INDEX_JOB["error_samples"] = list(snapshot.get("error_samples", []))
        SID_INDEX_JOB["current"] = str(snapshot.get("current", "") or "")
    _sid_index_update_progress(
        pending_dirs=int(snapshot.get("pending_dirs", SID_INDEX_JOB["pending_dirs"]) or 0)
    )


def _juke_refresh_lazy_metadata() -> None:
    """Refresh visible lazy queue rows after a metadata index completes."""
    lazy = [it for it in JUKE["items"] if it.get("data") is None and it.get("path")]
    if not lazy:
        return
    try:
        rows = _sid_metadata_for_paths(_index_store(), (it["path"] for it in lazy))
    except Exception:
        return
    for item in lazy:
        row = rows.get(str(item.get("path") or "").casefold())
        if row:
            item["meta"] = _sid_meta_from_row(row, item.get("label") or "SID")


def _sid_scan_parse_header(data: bytes) -> dict:
    return _parse_sid(data, compute_md5=False)


def _sid_index_note_error(path: str, exc: BaseException) -> None:
    with _SID_INDEX_LOCK:
        SID_INDEX_JOB["errors"] += 1
        if len(SID_INDEX_JOB["error_samples"]) < 12:
            SID_INDEX_JOB["error_samples"].append(f"{path}: {exc}")


def _sid_index_device_file(store: IndexStore, row: dict,
                           force: bool) -> tuple[dict | None, str]:
    path = str(row.get("path") or "")
    if not path:
        return None, ""
    size = int(row.get("size", 0) or 0)
    mtime = str(row.get("mtime", "") or "")
    with _SID_INDEX_LOCK:
        SID_INDEX_JOB["files"] += 1
        SID_INDEX_JOB["current"] = path
    if not force and store.sid_metadata_is_current(path, size, mtime):
        with _SID_INDEX_LOCK:
            SID_INDEX_JOB["cached"] += 1
        _sid_index_update_progress()
        return None, path
    try:
        header = _sid_background_device_call(
            f"reading SID header {path}",
            lambda p=path: devfs.fetch_head(p, SID_HEADER_BYTES),
        )
    except Exception as exc:
        _sid_index_note_error(path, exc)
        return None, path
    if header is None:
        return None, path
    with _SID_INDEX_LOCK:
        SID_INDEX_JOB["bytes_read"] += len(header)
    meta = _sid_scan_parse_header(header)
    if not meta:
        _sid_index_note_error(path, ValueError("not a valid PSID/RSID header"))
        return None, path
    result = {
        "path": path,
        "size": size,
        "mtime": mtime,
        "meta": meta,
        "source": "Ultimate SID refresh",
    }
    with _SID_INDEX_LOCK:
        SID_INDEX_JOB["parsed"] += 1
    _sid_index_update_progress()
    return result, path


def _sid_index_ultimate_worker(root: str, force: bool) -> None:
    store = _index_store()
    scan_id = ""
    t0 = time.monotonic()
    try:
        scan_id = store.begin_sid_scan(root, "ultimate", root)
        batch_rows: list[dict] = []
        batch_seen: list[str] = []

        def flush() -> None:
            nonlocal batch_rows, batch_seen
            if batch_rows or batch_seen:
                store.put_sid_scan_batch(scan_id, batch_rows, batch_seen)
                batch_rows = []
                batch_seen = []

        def process(row: dict) -> None:
            parsed, seen = _sid_index_device_file(store, row, force)
            if seen:
                batch_seen.append(seen)
            if parsed:
                batch_rows.append(parsed)
            if len(batch_rows) + len(batch_seen) >= 250:
                flush()

        indexed_rows = (store.files_below(root, ".sid")
                        if store.complete_cover(root) is not None else [])
        if indexed_rows:
            for row in indexed_rows:
                if SID_INDEX_JOB["stop"]:
                    break
                if not _sid_local_pause_wait():
                    break
                process(row)
        else:
            pending = deque([root])
            while pending and not SID_INDEX_JOB["stop"]:
                if not _sid_local_pause_wait():
                    break
                with _SID_INDEX_LOCK:
                    SID_INDEX_JOB["pending_dirs"] = len(pending)
                folder = pending.popleft()
                with _SID_INDEX_LOCK:
                    SID_INDEX_JOB["current"] = folder
                entries = _sid_background_device_call(
                    f"listing SID folder {folder}",
                    lambda f=folder: devfs.list_dir(f),
                )
                if entries is None:
                    if SID_INDEX_JOB["stop"]:
                        break
                    continue
                with _SID_INDEX_LOCK:
                    SID_INDEX_JOB["dirs"] += 1
                base = "" if folder == "/" else folder
                for entry in entries:
                    if SID_INDEX_JOB["stop"]:
                        break
                    if not _sid_local_pause_wait():
                        break
                    path = base + "/" + entry["name"]
                    if entry.get("dir"):
                        pending.append(path)
                    elif entry["name"].lower().endswith(".sid"):
                        process({
                            "path": path,
                            "name": entry["name"],
                            "size": int(entry.get("size", 0) or 0),
                            "mtime": str(entry.get("mtime", "") or ""),
                        })
                _sid_index_update_progress(pending_dirs=len(pending))
        flush()
        summary = {
            "files": SID_INDEX_JOB["files"],
            "parsed": SID_INDEX_JOB["parsed"],
            "cached": SID_INDEX_JOB["cached"],
            "errors": SID_INDEX_JOB["errors"],
            "secs": round(time.monotonic() - t0, 1),
        }
        if not SID_INDEX_JOB["stop"]:
            store.finish_sid_scan(scan_id, root, "ultimate", root, summary)
            _juke_refresh_lazy_metadata()
        else:
            store.abort_sid_scan(scan_id)
    except Exception as exc:
        if scan_id:
            try:
                store.abort_sid_scan(scan_id)
            except Exception:
                pass
        with _SID_INDEX_LOCK:
            SID_INDEX_JOB["error"] = str(exc)
    finally:
        _sid_index_update_progress(pending_dirs=0)
        with _SID_INDEX_PAUSE:
            SID_INDEX_JOB["running"] = False
            SID_INDEX_JOB["paused"] = False
            SID_INDEX_JOB["manual_paused"] = False
            SID_INDEX_JOB["pause_reason"] = ""
            SID_INDEX_JOB["current"] = ""
            _SID_INDEX_PAUSE.notify_all()


def _sid_index_local_worker(source_text: str, root: str, force: bool) -> None:
    store = _index_store()
    scan_id = ""
    t0 = time.monotonic()
    try:
        source = resolve_source(source_text)
        scan_id = store.begin_sid_scan(root, "local", str(source))

        def commit(rows, seen):
            store.put_sid_scan_batch(scan_id, rows, seen)

        summary = scan_local_sid_tree(
            source,
            root,
            parse_sid=_sid_scan_parse_header,
            is_cached=store.sid_metadata_is_current,
            commit_batch=commit,
            stop_check=lambda: bool(SID_INDEX_JOB["stop"]),
            pause_wait=_sid_local_pause_wait,
            progress=_sid_index_progress,
            force=force,
        )
        summary["secs"] = round(time.monotonic() - t0, 1)
        _sid_index_progress(summary)
        if not SID_INDEX_JOB["stop"]:
            store.finish_sid_scan(scan_id, root, "local", str(source), summary)
            _juke_refresh_lazy_metadata()
        else:
            store.abort_sid_scan(scan_id)
    except Exception as exc:
        if scan_id:
            try:
                store.abort_sid_scan(scan_id)
            except Exception:
                pass
        with _SID_INDEX_LOCK:
            SID_INDEX_JOB["error"] = str(exc)
    finally:
        _sid_index_update_progress(pending_dirs=0)
        with _SID_INDEX_PAUSE:
            SID_INDEX_JOB["running"] = False
            SID_INDEX_JOB["paused"] = False
            SID_INDEX_JOB["manual_paused"] = False
            SID_INDEX_JOB["pause_reason"] = ""
            SID_INDEX_JOB["current"] = ""
            _SID_INDEX_PAUSE.notify_all()


def _sid_index_start(mode: str, root: str, source: str = "", force: bool = False):
    global _SID_INDEX_THREAD
    if SID_INDEX_JOB["running"]:
        raise HTTPException(409, "a SID metadata refresh is already running")
    if INDEXJOB["running"]:
        raise HTTPException(409, "wait for the storage index job to finish first")
    with _SID_INDEX_PAUSE:
        SID_INDEX_JOB.update({
            "running": True, "mode": mode, "root": root, "source": source,
            "dirs": 0, "files": 0, "parsed": 0, "cached": 0,
            "errors": 0, "error_samples": [], "bytes_read": 0,
            "current": root, "pending_dirs": 1, "started": time.time(),
            "elapsed": 0.0, "files_per_sec": 0.0, "stop": False,
            "paused": False, "manual_paused": False, "pause_reason": "",
            "force": bool(force), "error": "",
        })
    target = _sid_index_local_worker if mode == "local" else _sid_index_ultimate_worker
    args = (source, root, force) if mode == "local" else (root, force)
    _SID_INDEX_THREAD = threading.Thread(
        target=target, args=args, daemon=True, name=f"sid-index-{mode}"
    )
    _SID_INDEX_THREAD.start()


@app.get("/api/juke/index/status")
def juke_index_status():
    _sid_index_update_progress()
    with _SID_INDEX_LOCK:
        state = dict(SID_INDEX_JOB)
    store = _index_store()
    state["metadata_count"] = store.sid_metadata_count(
        state.get("root") or _configured_hvsc_root() or "/"
    )
    state["runs"] = store.sid_index_runs()
    state["local_source"] = str(CFG.get("sid_local_source") or "")
    state["configured_root"] = str(
        CFG.get("sid_index_root") or CFG.get("hvsc_path") or ""
    )
    return state


@app.post("/api/juke/index/ultimate")
def juke_index_ultimate_start(payload: dict = Body(default={})):
    root = _normalise_device_path(
        payload.get("root") or _configured_hvsc_root() or CFG.get("hvsc_path") or ""
    )
    if root == "/" and not CFG.get("hvsc_path"):
        raise HTTPException(400, "detect or enter the HVSC root first")
    _sid_index_start("ultimate", root, force=bool(payload.get("force")))
    return {"started": root, "mode": "ultimate", "force": bool(payload.get("force"))}


@app.post("/api/juke/index/local")
def juke_index_local_start(payload: dict = Body(...)):
    if not str(CFG.get("u64_host") or "").strip():
        raise HTTPException(409, "select the target Ultimate before building its SID index")
    try:
        source = resolve_source(payload.get("source", ""))
        names = {p.name.upper() for p in source.iterdir() if p.is_dir()}
        if not {"MUSICIANS", "DOCUMENTS"}.issubset(names):
            candidates = []
            for child_name in ("HVSC", "C64Music", "C64MUSIC"):
                child = source / child_name
                if child.is_dir():
                    try:
                        child_names = {p.name.upper() for p in child.iterdir() if p.is_dir()}
                    except OSError:
                        continue
                    if {"MUSICIANS", "DOCUMENTS"}.issubset(child_names):
                        candidates.append(child)
            if len(candidates) == 1:
                source = candidates[0].resolve()
            else:
                raise ValueError(
                    "choose the HVSC folder itself (the folder containing "
                    "MUSICIANS and DOCUMENTS)"
                )
        root = normalise_ultimate_root(
            payload.get("root") or CFG.get("hvsc_path") or "/USB0/HVSC"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    CFG["sid_local_source"] = str(source)
    CFG["sid_index_root"] = root
    if not CFG.get("hvsc_path"):
        CFG["hvsc_path"] = root
    save_config()
    _sid_index_start("local", root, str(source), bool(payload.get("force")))
    return {"started": root, "source": str(source), "mode": "local",
            "force": bool(payload.get("force")), "read_only": True}


@app.post("/api/juke/index/pause")
def juke_index_pause(payload: dict = Body(...)):
    if not SID_INDEX_JOB["running"]:
        raise HTTPException(409, "no SID metadata refresh is running")
    paused = bool(payload.get("paused"))
    with _SID_INDEX_PAUSE:
        SID_INDEX_JOB["manual_paused"] = paused
        if not paused:
            _SID_INDEX_PAUSE.notify_all()
    return {"paused": paused}


@app.post("/api/juke/index/stop")
def juke_index_stop():
    if not SID_INDEX_JOB["running"]:
        return {"stopped": False}
    with _SID_INDEX_PAUSE:
        SID_INDEX_JOB["stop"] = True
        SID_INDEX_JOB["manual_paused"] = False
        _SID_INDEX_PAUSE.notify_all()
    return {"stopped": True}


_HVSC_VER_PATTERNS = [r"STIL\s+v(\d{2,3})", r"Release\s+(\d{2,3})\b",
                      r"HVSC\s*(?:Update\s*)?#\s*(\d{2,3})",
                      r"[Vv]ersion:?\s*#?(\d{2,3})\b"]


def _hvsc_installed_version() -> int:
    """Best-effort: read the release number from the collection's own
    DOCUMENTS (STIL.txt and BUGlist.txt headers carry it)."""
    root = (CFG.get("hvsc_path") or "").rstrip("/")
    if not root:
        return 0
    for doc in ("STIL.txt", "BUGlist.txt"):
        try:
            head = devfs.fetch_head(f"{root}/DOCUMENTS/{doc}",
                                    4096).decode("latin-1", "replace")
        except Exception:
            continue
        for pat in _HVSC_VER_PATTERNS:
            m = _re.search(pat, head[:4000])
            if m:
                v = int(m.group(1))
                if 40 <= v <= 200:
                    return v
    return 0


def _hvsc_latest_version() -> int:
    """Latest release number from hvsc.c64.org (graceful 0 on failure).

    Primary source: the news directory's dated announcement files, whose
    headers carry "Resulting Version: NN". Fallback: pattern-scraping the
    site pages."""
    # primary: CSDb's HVSC Crew page — plain HTML list of every release,
    # newest first ("High Voltage SID Collection #85")
    try:
        r = httpx.get("https://csdb.dk/group/?id=1431", timeout=8,
                      follow_redirects=True)
        nums = [int(n) for n in _re.findall(
                r"High Voltage SID Collection #(\d{2,3})\b", r.text)
                if 40 <= int(n) <= 200]
        if nums:
            return max(nums)
    except Exception:
        pass
    base = "https://hvsc.c64.org/download/files/news/"
    try:
        idx = httpx.get(base, timeout=8, follow_redirects=True)
        dates = sorted(set(_re.findall(r"(\d{8})\.txt", idx.text)))
        for date in reversed(dates[-3:]):
            try:
                txt = httpx.get(base + date + ".txt", timeout=8,
                                follow_redirects=True).text[:2000]
            except Exception:
                continue
            m = (_re.search(r"Resulting Version:\s*(\d{2,3})", txt) or
                 _re.search(r"Update\s*#?\s*(\d{2,3})", txt))
            if m and 40 <= int(m.group(1)) <= 200:
                return int(m.group(1))
    except Exception:
        pass
    for url in ("https://www.hvsc.c64.org/", "https://www.hvsc.c64.org/downloads"):
        try:
            r = httpx.get(url, timeout=8, follow_redirects=True)
            nums = [int(n) for n in
                    _re.findall(r"(?:Update|HVSC)[\s_#]*?(\d{2,3})\b", r.text)
                    if 40 <= int(n) <= 200]
            if nums:
                return max(nums)
        except Exception:
            continue
    return 0


@app.get("/api/juke/hvsc_version")
def juke_hvsc_version():
    installed = _hvsc_installed_version()
    latest = _hvsc_latest_version()
    return {"installed": installed or None, "latest": latest or None,
            "up_to_date": bool(installed and latest and installed >= latest),
            "downloads": "https://www.hvsc.c64.org/downloads",
            "note": "apply updates with the official HVSC Update Tool on "
                    "your PC, then press ↻ here to re-index"}


@app.get("/api/juke/search")
def juke_search(q: str = Query(""), limit: int = Query(100),
                chip: str = Query("all"), sid_format: str = Query("all", alias="format")):
    """Search the SID metadata catalogue, with Songlengths path fallback.

    A text query may be omitted when a Chip or Format filter is selected.
    Metadata-backed results include title, author, chip and PSID/RSID format;
    older installs without a metadata scan retain the original path search.
    """
    q = q.strip().lower()
    chip = str(chip or "all").lower()
    sid_format = str(sid_format or "all").upper()
    filtered = chip != "all" or sid_format != "ALL"
    if q and len(q) < 2:
        raise HTTPException(400, "query needs at least 2 characters")
    if not q and not filtered:
        raise HTTPException(400, "enter a search term or choose a Chip/Format filter")

    limit = min(max(1, limit), 300)
    root = _configured_hvsc_root() or (CFG.get("hvsc_path") or "").rstrip("/") or "/"
    store = _index_store()
    metadata_count = store.sid_metadata_count(root)
    if metadata_count:
        result = store.sid_metadata_search(
            root, q, chip=chip, sid_format=sid_format, limit=limit
        )
        rows = []
        for row in result["results"]:
            path = str(row["path"])
            rel = _path_relative_to(path, root) or path
            rows.append({
                "rel": rel,
                "name": row["name"],
                "folder": row["parent"],
                "path": path,
                "meta": _sid_meta_from_row(row, row["name"]),
            })
        return {"query": q, "chip": chip, "format": sid_format,
                "total": result["total"], "results": rows,
                "indexed": metadata_count, "backend": "SID metadata"}

    if filtered:
        raise HTTPException(
            409,
            "Chip and Format filters require a SID metadata refresh. Build it "
            "from the local HVSC folder or refresh it from the Ultimate first.",
        )
    if not HVSC_INDEX:
        raise HTTPException(409, "no SID path index — configure/detect the "
                                 "collection or build the SID metadata index first")
    terms = q.split()
    hits = []
    for low, rel in HVSC_INDEX:
        if all(t in low for t in terms):
            name = rel.rsplit("/", 1)[-1]
            folder = rel.rsplit("/", 1)[0] or "/"
            rank = 0 if all(t in name.lower() for t in terms) else 1
            hits.append((rank, {"rel": rel, "name": name,
                                "folder": (root + folder) if root != "/" else folder,
                                "path": (root + rel) if root != "/" else rel,
                                "meta": _sid_placeholder(name)}))
            if len(hits) >= 3000:
                break
    hits.sort(key=lambda h: (h[0], h[1]["rel"]))
    return {"query": q, "chip": chip, "format": sid_format,
            "total": len(hits), "results": [h[1] for h in hits[:limit]],
            "indexed": len(HVSC_INDEX), "backend": "HVSC path index"}


def _normalise_device_path(path: str) -> str:
    parts = [part for part in str(path or "/").replace("\\", "/").split("/")
             if part and part != "."]
    return "/" + "/".join(parts) if parts else "/"


def _path_relative_to(path: str, root: str) -> str | None:
    """Case-insensitive device-path relative lookup preserving path spelling."""
    path = _normalise_device_path(path)
    root = _normalise_device_path(root)
    pkey, rkey = path.casefold(), root.casefold()
    if pkey == rkey:
        return "/"
    if rkey != "/" and pkey.startswith(rkey + "/"):
        return path[len(root):] or "/"
    return None


def _configured_hvsc_root() -> str:
    root = _normalise_device_path(CFG.get("hvsc_path") or "")
    if root != "/":
        return root
    songlengths = str(CFG.get("songlengths_path") or "").replace("\\", "/")
    marker = "/documents/"
    pos = songlengths.casefold().find(marker)
    if pos > 0 and songlengths.startswith("/"):
        return _normalise_device_path(songlengths[:pos])
    return ""


def _hvsc_rows_below(root: str) -> tuple[list[dict], str]:
    """Map an Ultimate folder to Songlengths paths without case assumptions."""
    hvsc_root = _configured_hvsc_root()
    if not hvsc_root or not HVSC_INDEX:
        return [], ""
    rel_root = _path_relative_to(root, hvsc_root)
    if rel_root is None:
        return [], "mapping"
    rel_key = rel_root.casefold().rstrip("/") or "/"
    rows = []
    for _low, rel in HVSC_INDEX:
        rel_norm = _normalise_device_path(rel)
        key = rel_norm.casefold()
        if rel_key != "/" and not (key == rel_key or key.startswith(rel_key + "/")):
            continue
        path = hvsc_root.rstrip("/") + rel_norm
        name = rel_norm.rsplit("/", 1)[-1]
        rows.append({"path": path, "name": name,
                     "parent": path.rsplit("/", 1)[0] or "/"})
    return rows, "hvsc"


def _juke_install_lazy_folder(folder: str, rows: list[dict], selected_path: str,
                              source: str) -> None:
    selected = None
    if JUKE.get("items") and 0 <= JUKE.get("index", -1) < len(JUKE["items"]):
        current = JUKE["items"][JUKE["index"]]
        if str(current.get("path") or "").casefold() == selected_path.casefold():
            selected = current
    metadata = _sid_metadata_for_paths(_index_store(), (row["path"] for row in rows[:300]))
    items = []
    for row in rows[:300]:
        item = (_juke_lazy_item(
                    row["path"], row.get("name"),
                    metadata.get(str(row["path"]).casefold()))
                if selected is None or row["path"].casefold() != selected_path.casefold()
                else selected)
        items.append(item)
    current_index = next((i for i, it in enumerate(items)
                          if str(it.get("path") or "").casefold() == selected_path.casefold()), -1)
    JUKE.update({"items": items, "index": current_index,
                 "playing": bool(JUKE.get("playing") and current_index >= 0),
                 "folder": folder, "loading": False, "source": source})


@app.post("/api/juke/random")
def juke_random(payload: dict = Body(...)):
    """Pick one random SID with SQLite first and an HVSC path-index fallback."""
    root = _normalise_device_path(payload.get("root") or "/")
    store = _index_store()
    hit = store.random_file(root, ".sid")
    backend = "sqlite"
    mapping_state = ""
    fallback_rows: list[dict] = []
    if not hit:
        fallback_rows, mapping_state = _hvsc_rows_below(root)
        if fallback_rows:
            import secrets
            chosen = fallback_rows[secrets.randbelow(len(fallback_rows))]
            hit = {**chosen, "candidates": len(fallback_rows)}
            backend = "hvsc"
    if not hit:
        if mapping_state == "mapping":
            raise HTTPException(
                409,
                f"HVSC path mapping could not be resolved for {root}. Open the "
                "HVSC home folder once or re-detect the collection.",
            )
        if store.complete_cover(root) is None:
            raise HTTPException(
                409,
                f"{root} has not been indexed. Build the storage index for a "
                "parent folder, or use HVSC re-detect if this is the collection.",
            )
        raise HTTPException(404, f"No SID files exist beneath {root}.")

    attempted: set[str] = set()
    out = None
    last_error = None
    candidates = fallback_rows if backend == "hvsc" else None
    tries = min(8, max(1, int(hit.get("candidates", 1))))
    for _ in range(tries):
        path_key = str(hit["path"]).casefold()
        if path_key in attempted:
            if backend == "sqlite":
                hit = store.random_file(root, ".sid") or hit
            elif candidates:
                import secrets
                hit = {**candidates[secrets.randbelow(len(candidates))],
                       "candidates": len(candidates)}
            continue
        attempted.add(path_key)
        try:
            out = juke_play_path({"path": hit["path"]})
            break
        except HTTPException as exc:
            last_error = exc
            if backend == "sqlite":
                hit = store.random_file(root, ".sid") or hit
            elif candidates:
                import secrets
                hit = {**candidates[secrets.randbelow(len(candidates))],
                       "candidates": len(candidates)}
    if out is None:
        detail = getattr(last_error, "detail", "selected SID could not be fetched")
        raise HTTPException(502, f"{detail}; refresh or verify the affected storage folder")

    folder = hit.get("parent") or hit["path"].rsplit("/", 1)[0] or "/"
    if backend == "sqlite":
        rows = store.files_in_directory(folder, ".sid", limit=300)
    else:
        rows = [row for row in fallback_rows
                if row["parent"].casefold() == folder.casefold()][:300]
    if not rows:
        rows = [{"path": hit["path"], "name": hit["name"]}]
    _juke_install_lazy_folder(folder, rows, hit["path"],
                              "SQLite index" if backend == "sqlite" else "HVSC path index")
    out = _juke_state()
    out.update({"selected": hit["path"],
                "indexed_candidates": hit["candidates"],
                "indexed_folder_size": len(rows),
                "backend": backend})
    return out


@app.put("/api/juke/play")
def juke_play(index: int = Query(...), song: int = Query(0)):
    return _juke_play(index, song)


@app.put("/api/juke/next")
def juke_next():
    return _juke_play(_juke_next_index())


@app.put("/api/juke/prev")
def juke_prev():
    n = len(JUKE["items"])
    return _juke_play((JUKE["index"] - 1) % n if n else 0)


@app.put("/api/juke/stop")
def juke_stop():
    _juke_cancel_timer()
    JUKE["playing"] = False
    try:
        rest.put("/v1/machine:reset")
    except Exception:
        pass
    return _juke_state()


@app.put("/api/juke/shuffle")
def juke_shuffle(on: bool = Query(...)):
    JUKE["shuffle"] = bool(on)
    return _juke_state()


# --- quick-launch library ------------------------------------------------
# Files dropped into ./library (next to server.py / the exe) become
# one-click launch buttons in the UI — e.g. a Compunet Reborn CRT.

LIB_EXT = (".crt", ".prg", ".t64", ".sid", ".mod",
           ".d64", ".d71", ".d81", ".g64", ".dnp")


def _library_dir() -> Path:
    d = ROOT / "library"
    if not d.exists():
        try:
            d.mkdir()
            (d / "README.txt").write_text(
                "Drop .crt/.prg/.t64/.sid/.mod/.d64/.d71/.d81/.g64/.dnp files here.\n"
                "Each appears as a Quick Launch button in u64deck's SCREEN tab.\n")
        except OSError:
            pass
    return d


@app.get("/api/library")
def library_list():
    d = _library_dir()
    out = []
    for f in sorted(d.iterdir()):
        if f.is_file() and f.suffix.lower() in LIB_EXT:
            out.append({"name": f.name, "size": f.stat().st_size})
    return {"files": out}


@app.post("/api/library/upload")
async def library_upload(file: UploadFile = File(...)):
    name, data = await _read_upload(file, MAX_LIBRARY_UPLOAD)
    name = os.path.basename(name)
    if not name or Path(name).suffix.lower() not in LIB_EXT:
        raise HTTPException(400, f"only {', '.join(LIB_EXT)} files")
    await run_in_threadpool((_library_dir() / name).write_bytes, data)
    return library_list()


@app.post("/api/library/delete")
def library_delete(name: str = Query(...)):
    p = _library_dir() / os.path.basename(name)
    if p.suffix.lower() not in LIB_EXT:
        raise HTTPException(400, "not a launchable library file")
    if p.is_file():
        p.unlink()
    return library_list()


@app.put("/api/library/run")
def library_run(name: str = Query(...), drive: str = Query("a")):
    p = _library_dir() / os.path.basename(name)
    if not p.is_file():
        raise HTTPException(404, "not in library")
    data = p.read_bytes()
    low = p.name.lower()
    try:
        if low.endswith((".d64", ".d71", ".d81", ".g64", ".dnp")):
            drive, mode = _drive_key(drive), _mount_mode(None)
            with DEVICE_OP.operation("interactive", "launching library disk"):
                out = rest.mount_attachment(drive, p.name, data, mode=mode)
                _remember_mount(drive, mode, name=p.name)
                rest.put("/v1/machine:reset")
                return out
        if p.name.lower().endswith(".t64"):
            name, prg = _t64_first_prg(data)
            return _run_cart_safe(lambda: rest.run_prg(name + ".prg", prg))
        runner = _runner_for(p.name)
        if runner == "run_crt":
            return rest.post_file(f"/v1/runners:{runner}", p.name, data)
        return _run_cart_safe(
            lambda: rest.post_file(f"/v1/runners:{runner}", p.name, data))
    except ValueError as e:
        err(e, 400)
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


# --- stream statistics and diagnostics ---------------------------------

def _receiver_stats(receiver, kind: str) -> dict:
    if not receiver:
        return {"kind": kind, "running": False, "packets": 0, "dropped": 0}
    return {"kind": kind, "running": bool(getattr(receiver, "running", False)),
            "packets": int(getattr(receiver, "packets", 0)),
            "dropped": int(getattr(receiver, "dropped", 0)),
            "frames": int(getattr(receiver, "frame_no", 0)),
            "started_at": float(getattr(receiver, "started_at", 0) or 0),
            "last_packet_bytes": int(getattr(receiver, "last_pkt_len", 0) or 0)}

@app.get("/api/stream/stats")
def stream_stats():
    return {"video": _receiver_stats(video, "video"),
            "audio": _receiver_stats(audio, "audio"),
            "state": dict(STREAM_STATE), "last": dict(STREAM_LAST)}

def _diagnostic_clean(value, key: str = ""):
    low = key.lower()
    if any(token in low for token in ("password", "secret", "token", "keyfile")):
        return "<redacted>" if value else ""
    if isinstance(value, dict):
        return {str(k): _diagnostic_clean(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_diagnostic_clean(v, key) for v in value]
    if isinstance(value, str):
        text = value
        # Preserve Ultimate paths (/USB0, /SD...) but hide host filesystem paths.
        if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\"):
            return "<local-path>/" + text.replace("\\", "/").rsplit("/", 1)[-1]
        if text.startswith(("/home/", "/Users/", "/mnt/")):
            return "<local-path>/" + text.rsplit("/", 1)[-1]
        root_text = str(ROOT)
        if root_text and root_text in text:
            return text.replace(root_text, "<u64deck-folder>")
        return text[:4000]
    return value

def _sanitised_config() -> dict:
    return _diagnostic_clean(copy.deepcopy(CFG))

@app.post("/api/diagnostics/export")
def diagnostics_export(payload: dict = Body(default={})):
    browser = payload.get("browser") if isinstance(payload, dict) else {}
    if not isinstance(browser, dict):
        browser = {}
    try:
        device = rest.info() if rest else {"error": "device backend unavailable"}
    except Exception as exc:
        device = {"error": str(exc)}
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "u64deck": {"version": VERSION, "release_label": RELEASE_LABEL,
                    "build": BUILD, "frozen": FROZEN},
        "runtime": {"python": sys.version, "platform": platform.platform(),
                    "executable": Path(sys.executable).name},
        "browser": {str(k): str(v)[:1000] for k, v in list(browser.items())[:30]},
        "device": device,
        "stream": stream_stats(),
        "index": fs_index_status(),
        "sid_index": juke_index_status(),
        "cache": cache_stats(),
        "events": list(DIAG_EVENTS),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.json", json.dumps(_diagnostic_clean(report), indent=2, ensure_ascii=False, default=str))
        zf.writestr("config-sanitised.json", json.dumps(_sanitised_config(), indent=2, ensure_ascii=False))
        zf.writestr("image-parse-errors.json", json.dumps(cache_parse_errors(500), indent=2, ensure_ascii=False))
        zf.writestr("README.txt",
            "u64deck diagnostics export\n\nPasswords and secret/token/key fields are redacted. "
            "No game, SID or disk-image content is included.\n")
    data = buf.getvalue()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(content=data, media_type="application/zip",
                    headers=_attachment_headers(f"u64deck-diagnostics-{stamp}.zip"))


# --- static UI ----------------------------------------------------------

@app.get("/")
def index():
    # no-store: the UI is a single file that changes every release — never
    # let the browser serve a stale copy after an update
    return FileResponse(ASSETS / "static" / "index.html",
                        headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=ASSETS / "static"), name="static")


def main():
    ap = argparse.ArgumentParser(description="u64deck — Ultimate 64 control deck")
    ap.add_argument("--u64", help="Ultimate 64 IP or hostname (overrides config.json)")
    ap.add_argument("--port", type=int, help="HTTP port for this UI")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't auto-open the browser on start")
    args = ap.parse_args()
    if args.u64:
        CFG["u64_host"] = args.u64
    if args.port:
        CFG["http_port"] = args.port
    init_backends()
    # Perform the one-time per-IP database consolidation before the browser
    # opens. This avoids a long first cache/search request timing out while a
    # large index is copied and merged.
    _prepare_stable_index()

    def _songlengths_bg():
        SL_STATE["state"] = "loading"
        try:
            # Cache warming is deliberately lower priority than buttons and
            # status checks, just like the storage indexer.
            with DEVICE_OP.operation("background", "loading Songlengths"):
                n = load_songlengths()
            SL_STATE["state"] = "ready" if n else "empty"
            if n:
                print(f"  songlengths: {n} entries loaded")
        except Exception as e:
            SL_STATE["state"] = "error: " + str(e)[:80]
    _threading.Thread(target=_songlengths_bg, daemon=True,
                      name="songlengths").start()
    print(f"  u64deck v{VERSION} · {RELEASE_LABEL} · build {BUILD}")
    scheme = "https" if (CFG.get("tls_certfile") and CFG.get("tls_keyfile")) else "http"
    url = f"{scheme}://localhost:{CFG['http_port']}"
    print(f"\n  u64deck -> device {CFG['u64_host'] or '(none — use Select Ultimate\u2026 in the UI)'}")
    print(f"  open {url}\n")
    if not args.no_browser:
        _schedule_browser_launch(url, CFG.get("browser_startup"))
    ssl_kwargs = {}
    if CFG.get("tls_certfile") and CFG.get("tls_keyfile"):
        ssl_kwargs = {"ssl_certfile": CFG["tls_certfile"],
                      "ssl_keyfile": CFG["tls_keyfile"]}
        print("  TLS enabled for the web UI\n")
    uvicorn.run(app, host=CFG.get("http_host", "0.0.0.0"),
                port=CFG["http_port"], log_level="warning", **ssl_kwargs)


if __name__ == "__main__":
    main()

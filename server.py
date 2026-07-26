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
from network_awareness import (
    LINK_ETHERNET, LINK_UNKNOWN, LINK_WIFI, LinkDetector,
    classify_address_group, device_identity, load_oui_cache, merge_known_device,
    preferred_address, refresh_oui_cache,
)
from d64 import DiskImage, ascii_to_petscii
from ultimate import (AudioReceiver, CommandSocket, DeviceFS, UltimateError,
                      UltimateREST, VideoReceiver)
from device_coordinator import DeviceOperationCoordinator, OperationCancelled
from index_store import IndexStore
from index_migration import STABLE_INDEX_NAME, prepare_stable_index
from local_indexer import (list_local_volumes, normalise_ultimate_root,
                           resolve_source, scan_local_tree, volume_identity)
from sid_indexer import SID_HEADER_BYTES, scan_local_sid_tree
from sidflow_similarity import (CHECKSUMS as SIDFLOW_CHECKSUMS, FULL_MANIFEST as SIDFLOW_FULL_MANIFEST,
                                FULL_SQLITE as SIDFLOW_FULL_SQLITE, LATEST_DOWNLOAD_BASE as SIDFLOW_DOWNLOAD_BASE,
                                LATEST_RELEASE_API as SIDFLOW_RELEASE_API, MOBILE_MANIFEST as SIDFLOW_MOBILE_MANIFEST,
                                MOBILE_SQLITE as SIDFLOW_MOBILE_SQLITE, SCHEMA_VERSION as SIDFLOW_SCHEMA,
                                SimilarityStore, build_track_id as sidflow_track_id,
                                normalise_hvsc_relative, parse_sha256sums, sha256_file,
                                slim_and_promote, validate_manifest)
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
    # When Ethernet is selected and the same Ultimate has a live Wi-Fi
    # address, REST control can use Wi-Fi while command/FTP/streams remain
    # bound to Ethernet. This avoids the device firmware's ~2.5 s wired REST
    # response delay when both interfaces are enabled.
    "rest_control_host": "",
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
    # Device identities and all previously observed interface addresses.
    # Optional/backward-compatible; populated by discovery and connection.
    "known_devices": {},
    "active_device_identity": "",
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
if not isinstance(CFG.get("known_devices"), dict):
    CFG["known_devices"] = {}
_config_cleanup_pending = CFG.pop("input_method_preferences", None) is not None
if CFG.pop("_config_migration_pending", False) or _config_cleanup_pending:
    save_config()
USER_ITEMS = UserItemsStore(ROOT / "user_items.json")
DIAG_EVENTS = deque(maxlen=200)
MOUNT_STATE = {"a": {}, "b": {}}
VALID_MOUNT_MODES = {"readonly", "readwrite", "unlinked"}

# Firmware 3.15+ on Ultimate 64-class hardware exposes matrix-level input.
# Older firmware returns 404; hardware without the CIA1 implementation returns
# 501. Keep capability state per device so normal UI polling does not probe on
# every request, while reconnect/device-switch paths can force a fresh check.
MATRIX_KEYBOARD_INPUTS = {
    *list("abcdefghijklmnopqrstuvwxyz"), *list("0123456789"),
    "inst_del", "return", "cursor_left_right", "cursor_up_down",
    "f1", "f3", "f5", "f7", "left_shift", "right_shift",
    "plus", "minus", "period", "colon", "at", "comma", "pound",
    "star", "semicolon", "clr_home", "equals", "arrow_up",
    "arrow_left", "slash", "ctrl", "space", "commodore",
    "run_stop", "restore",
}
MATRIX_TRANSITIONS = {"press", "release", "tap"}
INPUT_CAPABILITIES: dict[str, dict] = {}
INPUT_CAP_LOCK = threading.Lock()
# Serialise every CIA1 matrix action and legacy keyboard-buffer write through
# one re-entrant lock.  The device coordinator protects the wider Ultimate
# network stack, but browser keyboard events, Mount & Run and WebSocket safety
# cleanup can originate on different worker threads.  This lock prevents an
# input cleanup or matrix tap from crossing a legacy command-buffer sequence.
INPUT_IO_LOCK = threading.RLock()
# Video disconnect cleanup is a safety action, not a reason to queue repeated
# release_all calls.  Keep at most one cleanup request in flight.
MATRIX_CLEANUP_LOCK = threading.Lock()

# Ethernet/Wi-Fi awareness. The optional Wireshark cache is additive-only;
# the bundled 2026-07 Espressif list remains the permanent floor.
OUI_CACHE_PATH = ROOT / ".espressif-ouis-cache.json"
_OUI_SET, _OUI_META = load_oui_cache(OUI_CACHE_PATH)
LINK_DETECTOR = LinkDetector(_OUI_SET)
LINK_STATE_LOCK = threading.RLock()
# Addresses confirmed by the most recent discovery scan or successful
# /v1/info call in this process. Persisted addresses are history only.
DISCOVERY_LIVE_ADDRESSES: dict[str, set[str]] = {}
# Finder uses dedicated short-lived HTTP clients, but routine status/drive
# polling must still stand down while a /24 scan is active.  This prevents the
# browser from competing with discovery for the Ultimate's constrained REST
# service and also blocks overlapping Finder scans.
DISCOVERY_ACTIVE = threading.Event()
DISCOVERY_SCAN_LOCK = threading.Lock()


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


def _refresh_ouis_background() -> None:
    # Fail-soft by contract: offline installs use the bundled list forever.
    effective = refresh_oui_cache(OUI_CACHE_PATH)
    LINK_DETECTOR.merge_ouis(effective)


@asynccontextmanager
async def _lifespan(_app):
    threading.Thread(target=_refresh_ouis_background, daemon=True,
                     name="espressif-oui-refresh").start()
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
    selected_host = str(CFG.get("u64_host") or "").strip()
    control_host = _saved_control_host_for_selected(selected_host)
    rest = UltimateREST(control_host, CFG.get("password", ""), coordinator=DEVICE_OP)
    control_link = _persisted_address(control_host).get("link_type", LINK_UNKNOWN)
    timeout_link = LINK_ETHERNET if control_host != selected_host else control_link
    _configure_rest_timeout(rest, timeout_link)
    cmd = CommandSocket(selected_host, coordinator=DEVICE_OP)
    devfs = DeviceFS(selected_host, CFG.get("ftp_user", "anonymous"),
                     CFG.get("ftp_password", ""), coordinator=DEVICE_OP)
    video = VideoReceiver(CFG["video_port"]); video.start()
    audio = AudioReceiver(CFG["audio_port"]); audio.start()
    if CFG.get("stream_transport") == "multicast":
        video.set_multicast(CFG["multicast_video"], CFG.get("local_ip", ""))
        audio.set_multicast(CFG["multicast_audio"], CFG.get("local_ip", ""))


def err(e: Exception, code: int = 502):
    _diag_event("error", str(e), status=code)
    raise HTTPException(status_code=code, detail=str(e))


DEFAULT_REST_TIMEOUT = 8.0
WIFI_REST_TIMEOUT = 45.0


def _known_devices() -> dict:
    devices = CFG.get("known_devices")
    if not isinstance(devices, dict):
        devices = {}
        CFG["known_devices"] = devices
    return devices


def _known_identity_for_host(host: str) -> tuple[str, dict | None]:
    host = str(host or "").strip()
    for identity, record in _known_devices().items():
        addresses = record.get("addresses") if isinstance(record, dict) else None
        if isinstance(addresses, dict) and host in addresses:
            return str(identity), record
    return "", None


def _persisted_address(host: str) -> dict:
    _identity, record = _known_identity_for_host(host)
    if not isinstance(record, dict):
        return {}
    addresses = record.get("addresses")
    row = addresses.get(host) if isinstance(addresses, dict) else None
    return dict(row) if isinstance(row, dict) else {}


def _host_was_verified_by_discovery(host: str) -> bool:
    """Return True only for an address verified in the current process."""
    identity, _record = _known_identity_for_host(host)
    return bool(identity and host in DISCOVERY_LIVE_ADDRESSES.get(identity, set()))


def _discovery_info_for_host(host: str) -> dict:
    """Rebuild the /v1/info fields saved by the latest verified Finder hit.

    Finder has already completed a live /v1/info request for this address.
    Reusing that result during Connect avoids immediately repeating a slow REST
    call while still requiring manual addresses to be verified normally.
    """
    if not _host_was_verified_by_discovery(host):
        return {}
    _identity, record = _known_identity_for_host(host)
    if not isinstance(record, dict):
        return {}
    return {
        "product": str(record.get("product") or "Ultimate"),
        "firmware_version": str(record.get("firmware") or ""),
        "core_version": str(record.get("core") or ""),
        "hostname": str(record.get("hostname") or ""),
        "unique_id": str(record.get("unique_id") or ""),
    }


def _same_known_device(first_host: str, second_host: str) -> bool:
    first_identity, _first = _known_identity_for_host(first_host)
    second_identity, _second = _known_identity_for_host(second_host)
    return bool(first_identity and first_identity == second_identity)


def _verified_control_host_for_selected(selected_host: str) -> str:
    """Choose the live REST-control address for a selected device interface.

    Ultimate firmware on both the U64 and older C64 Ultimate hardware delays
    Ethernet REST replies by roughly 2.5 seconds whenever Wi-Fi is enabled.
    Finder has already verified both addresses and grouped them by firmware
    identity.  When the user selects Ethernet, keep command socket, FTP and
    streaming bound to Ethernet but route ordinary REST control through the
    currently verified Wi-Fi address.  Historical/stale Wi-Fi addresses are
    never selected automatically.
    """
    selected_host = str(selected_host or "").strip()
    identity, record = _known_identity_for_host(selected_host)
    if not identity or not isinstance(record, dict):
        return selected_host
    addresses = record.get("addresses")
    if not isinstance(addresses, dict):
        return selected_host
    selected = addresses.get(selected_host)
    if not isinstance(selected, dict) or selected.get("link_type") != LINK_ETHERNET:
        return selected_host
    live = DISCOVERY_LIVE_ADDRESSES.get(identity, set())
    for ip, row in addresses.items():
        if (ip in live and isinstance(row, dict)
                and row.get("link_type") == LINK_WIFI):
            return str(ip)
    return selected_host


def _saved_control_host_for_selected(selected_host: str) -> str:
    """Return a persisted split-control route only for the same known device."""
    selected_host = str(selected_host or "").strip()
    saved = str(CFG.get("rest_control_host") or "").strip()
    if not selected_host or not saved:
        return selected_host
    if saved == selected_host:
        return selected_host
    if not _same_known_device(selected_host, saved):
        return selected_host
    selected = _persisted_address(selected_host)
    control = _persisted_address(saved)
    if (selected.get("link_type") == LINK_ETHERNET
            and control.get("link_type") == LINK_WIFI):
        return saved
    return selected_host


def _control_route_payload(selected_host: str, client: UltimateREST | None) -> dict:
    control_host = str(getattr(client, "host", "") or selected_host or "").strip()
    row = _persisted_address(control_host)
    control_link = str(row.get("link_type") or LINK_UNKNOWN)
    split = bool(control_host and selected_host and control_host != selected_host)
    return {
        "control_ip": control_host,
        "control_link_type": control_link,
        "rest_via_alternate": split,
        "rest_route_label": (
            "REST via Wi-Fi" if split and control_link == LINK_WIFI
            else "REST via Ethernet" if control_link == LINK_ETHERNET
            else "REST via selected address"
        ),
    }


def _configure_rest_timeout(client: UltimateREST | None, link_type: str) -> float:
    timeout = WIFI_REST_TIMEOUT if link_type == LINK_WIFI else DEFAULT_REST_TIMEOUT
    if client is not None and hasattr(client, "set_timeout"):
        client.set_timeout(timeout)
    return timeout


def _link_payload(host: str, *, info_payload: dict | None = None, force: bool = False,
                  persist: bool = True, client: UltimateREST | None = None) -> dict:
    """Classify one address and expose only currently verified alternatives.

    ``known_devices`` is historical state. An address becomes eligible for the
    connected header or Switch-to-Ethernet control only after a successful
    discovery hit or a live ``/v1/info`` response in this process.
    """
    host = str(host or "").strip()
    if not host:
        return {"ip": "", "link_type": LINK_UNKNOWN, "label": "Unknown",
                "addresses": [], "ethernet_ip": "", "wifi_ip": "",
                "identity": "", "rest_timeout": DEFAULT_REST_TIMEOUT}
    with LINK_STATE_LOCK:
        existing_identity, existing_record = _known_identity_for_host(host)
        previous = _persisted_address(host)
        observation = LINK_DETECTOR.detect(host, force=force)
        address = observation.as_dict()
        if observation.link_type == LINK_UNKNOWN and previous.get("link_type") in {LINK_ETHERNET, LINK_WIFI}:
            # Classification history may describe a *live* current address when
            # ARP is temporarily incomplete, but it never establishes liveness.
            address.update({k: v for k, v in previous.items() if k not in {"last_seen"}})
            address["ip"] = host

        verified_info = dict(info_payload or {})
        identity = existing_identity
        record = existing_record or {}
        if verified_info:
            identity, record = merge_known_device(
                _known_devices(), info=verified_info, address=address)
            DISCOVERY_LIVE_ADDRESSES.setdefault(identity, set()).add(host)
        elif not identity:
            identity, _source = device_identity({}, host)
            record = {
                "identity": identity, "identity_source": "ip",
                "unique_id": "", "hostname": "", "addresses": {},
            }

        all_addresses = record.get("addresses") if isinstance(record, dict) else {}
        live_ips = set(DISCOVERY_LIVE_ADDRESSES.get(identity, set()))
        addresses_map = {
            ip: row for ip, row in (all_addresses.items() if isinstance(all_addresses, dict) else [])
            if ip in live_ips and isinstance(row, dict)
        }

        # Only compare interfaces that were verified in the current process.
        if force and len(addresses_map) == 2:
            current_types = [str(row.get("link_type") or LINK_UNKNOWN)
                             for row in addresses_map.values()]
            needs_race = (LINK_UNKNOWN in current_types or
                          (len(current_types) == 2 and current_types[0] == current_types[1]
                           and current_types[0] in {LINK_ETHERNET, LINK_WIFI}))
            if needs_race:
                try:
                    raced = asyncio.run(classify_address_group(
                        list(addresses_map), LINK_DETECTOR))
                    for race_ip, race_obs in raced.items():
                        previous_row = addresses_map.get(race_ip)
                        if isinstance(previous_row, dict):
                            previous_row.update(race_obs.as_dict())
                except Exception as race_error:
                    _warn_event("link-latency-race",
                                f"could not compare Ultimate interfaces: {race_error}")

        addresses = [dict(row) for row in addresses_map.values()]
        order = {LINK_ETHERNET: 0, LINK_WIFI: 1, LINK_UNKNOWN: 2}
        addresses.sort(key=lambda row: (
            order.get(row.get("link_type"), 2), str(row.get("ip", ""))))
        selected = next((row for row in addresses if row.get("ip") == host), address)
        link_type = str(selected.get("link_type") or LINK_UNKNOWN)
        ethernet_ip = next((str(row.get("ip")) for row in addresses
                            if row.get("link_type") == LINK_ETHERNET), "")
        wifi_ip = next((str(row.get("ip")) for row in addresses
                        if row.get("link_type") == LINK_WIFI), "")
        preferred = preferred_address(addresses)
        if persist and verified_info:
            CFG["active_device_identity"] = identity
            save_config()
        route = _control_route_payload(host, client)
        timeout_link = LINK_ETHERNET if route["rest_via_alternate"] else route["control_link_type"]
        timeout = _configure_rest_timeout(client, timeout_link)
        return {
            "identity": identity,
            "identity_source": record.get("identity_source", "") if isinstance(record, dict) else "",
            "unique_id": record.get("unique_id", "") if isinstance(record, dict) else "",
            "hostname": record.get("hostname", "") if isinstance(record, dict) else "",
            "ip": host,
            "link_type": link_type,
            "label": "Ethernet" if link_type == LINK_ETHERNET else "Wi-Fi" if link_type == LINK_WIFI else "Unknown",
            "method": selected.get("method", "unknown"),
            "mac": selected.get("mac", ""),
            "addresses": addresses,
            "ethernet_ip": ethernet_ip,
            "wifi_ip": wifi_ip,
            "preferred_ip": preferred.get("ip") if preferred else host,
            "rest_timeout": timeout,
            "streaming_available": link_type != LINK_WIFI,
            **route,
        }


def _current_link_payload(*, force: bool = False, info_payload: dict | None = None,
                          persist: bool = False) -> dict:
    return _link_payload(CFG.get("u64_host", ""), info_payload=info_payload,
                         force=force, persist=persist, client=rest)


def _input_cache_key(client: UltimateREST | None = None) -> str:
    client = client or rest
    return str(getattr(client, "host", "") or CFG.get("u64_host", "")).strip()


def _cached_input_status(client: UltimateREST | None = None) -> dict:
    """Return capability state without issuing an Ultimate REST request."""
    client = client or rest
    host = _input_cache_key(client)
    with INPUT_CAP_LOCK:
        cached = INPUT_CAPABILITIES.get(host)
        if cached:
            return dict(cached)
    return {
        "available": None, "pending": True, "mode": "unknown", "status": 0,
        "label": "Input capability pending", "host": host,
        "detail": "Capability will be checked after connection or on first use",
    }


def _transfer_input_capability(first_host: str, second_host: str) -> dict:
    """Carry a cached capability across two addresses of one physical U64."""
    if not _same_known_device(first_host, second_host):
        return _cached_input_status()
    with INPUT_CAP_LOCK:
        cached = INPUT_CAPABILITIES.get(first_host)
        if not cached:
            return _cached_input_status()
        transferred = dict(cached)
        transferred["host"] = second_host
        INPUT_CAPABILITIES[second_host] = dict(transferred)
        return transferred


def _input_status(client: UltimateREST | None = None, force: bool = False) -> dict:
    """Return cached/probed matrix-input capability for one Ultimate."""
    client = client or rest
    host = _input_cache_key(client)
    if not client or not host:
        return {"available": False, "mode": "buffer", "status": 0,
                "label": "Legacy KERNAL buffer", "host": host,
                "detail": "No device configured"}
    with INPUT_CAP_LOCK:
        cached = INPUT_CAPABILITIES.get(host)
        if cached and not force:
            return dict(cached)
    try:
        probe = client.probe_machine_input()
        available = bool(probe.get("available"))
        status = int(probe.get("status") or 0)
        result = {
            "available": available,
            "mode": "matrix" if available else "buffer",
            "status": status,
            "label": "CIA1 keyboard matrix" if available else "Legacy KERNAL buffer",
            "host": host,
            "detail": str(probe.get("detail") or "")[:200],
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    except Exception as exc:
        # A failed probe must never make the rest of u64deck unusable. Use the
        # established buffer path for this session and retry on reconnect.
        result = {"available": False, "mode": "buffer", "status": 0,
                  "label": "Legacy KERNAL buffer", "host": host,
                  "detail": str(exc)[:200],
                  "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with INPUT_CAP_LOCK:
        INPUT_CAPABILITIES[host] = dict(result)
    return result


def _validate_matrix_events(events) -> list[dict]:
    if not isinstance(events, list) or not events:
        raise ValueError("events must be a non-empty list")
    if len(events) > 64:
        raise ValueError("machine input accepts at most 64 events per request")
    clean = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"events[{index}] must be an object")
        kind = str(event.get("kind") or "")
        if kind == "release_all":
            clean.append({"kind": "release_all"})
            continue
        if kind != "keyboard":
            raise ValueError(f"events[{index}].kind must be keyboard or release_all")
        inputs = event.get("inputs")
        transition = str(event.get("transition") or "")
        if not isinstance(inputs, list) or not inputs or len(inputs) > 8:
            raise ValueError(f"events[{index}].inputs must contain 1 to 8 keys")
        normalised = []
        for value in inputs:
            key = str(value or "").strip().lower()
            if key not in MATRIX_KEYBOARD_INPUTS:
                raise ValueError(f"events[{index}] contains unknown keyboard input {key!r}")
            if key not in normalised:
                normalised.append(key)
        if transition not in MATRIX_TRANSITIONS:
            raise ValueError(f"events[{index}].transition must be press, release or tap")
        clean.append({"kind": "keyboard", "inputs": normalised,
                      "transition": transition})
    return clean


def _matrix_send(events, *, client: UltimateREST | None = None,
                 force_probe: bool = False):
    with INPUT_IO_LOCK:
        client = client or rest
        status = _input_status(client, force=force_probe)
        if not status.get("available"):
            raise UltimateError("CIA1 matrix input is not available on this device")
        clean = _validate_matrix_events(events)
        try:
            return client.machine_input(clean)
        except Exception:
            # Re-probe after the next successful reconnect rather than trusting a
            # stale capability entry after a firmware/device restart.
            with INPUT_CAP_LOCK:
                INPUT_CAPABILITIES.pop(_input_cache_key(client), None)
            raise


def _matrix_release_all(*, client: UltimateREST | None = None,
                        silent: bool = True, cached_only: bool = False,
                        caller: str = "unspecified") -> bool:
    with INPUT_IO_LOCK:
        client = client or rest
        try:
            if cached_only:
                with INPUT_CAP_LOCK:
                    status = dict(INPUT_CAPABILITIES.get(_input_cache_key(client)) or {})
            else:
                status = _input_status(client)
            if not status.get("available"):
                return False
            _matrix_send([{"kind": "release_all"}], client=client)
            return True
        except Exception as exc:
            if not silent:
                raise
            label = str(caller or "unspecified")
            _warn_event(
                "matrix-release-all",
                f"could not release matrix input ({label}): {exc}",
            )
            return False


def _matrix_release_cleanup(*, caller: str) -> bool:
    """Run one non-blocking, coalesced WebSocket safety release.

    This helper is called from a worker thread so an Ultimate timeout cannot
    block the asyncio event loop and make the entire UI appear unresponsive.
    """
    if not MATRIX_CLEANUP_LOCK.acquire(blocking=False):
        _diag_event("info", f"matrix release cleanup skipped ({caller}) — already active")
        return False
    try:
        return _matrix_release_all(
            silent=True,
            cached_only=True,
            caller=caller,
        )
    finally:
        MATRIX_CLEANUP_LOCK.release()


def _legacy_type(data: bytes, *, chunk: int = 8, delay: float = 0.02):
    """Serialised wrapper for legacy KERNAL keyboard-buffer injection."""
    with INPUT_IO_LOCK:
        if chunk == 8 and abs(float(delay) - 0.02) < 0.0001:
            return cmd.type_petscii(data)
        return cmd.type_petscii(data, chunk=chunk, delay=delay)


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

def _discovery_candidate_ips() -> list[str]:
    candidates = []
    configured = str(CFG.get("u64_host") or "").strip()
    if configured:
        candidates.append(configured)
    for record in _known_devices().values():
        addresses = record.get("addresses") if isinstance(record, dict) else None
        if not isinstance(addresses, dict):
            continue
        for ip in addresses:
            value = str(ip or "").strip()
            if value and value not in candidates:
                candidates.append(value)
    return candidates


def _record_discovery_diagnostics(result: dict) -> None:
    for message in result.get("diagnostics") or []:
        _diag_event("info", str(message))


async def _run_discovery(subnet: str = "", port: int = 80) -> dict:
    if not DISCOVERY_SCAN_LOCK.acquire(blocking=False):
        raise HTTPException(409, "A device discovery scan is already running")
    DISCOVERY_ACTIVE.set()
    try:
        extra = [subnet] if subnet else None
        old_host = str(CFG.get("u64_host") or "").strip()
        active_identity = str(CFG.get("active_device_identity") or "").strip()
        if not active_identity and old_host:
            active_identity, _record = _known_identity_for_host(old_host)
        result = await discovery.discover(
            extra, port, known_devices=_known_devices(), detector=LINK_DETECTOR,
            candidate_ips=_discovery_candidate_ips())
        with LINK_STATE_LOCK:
            DISCOVERY_LIVE_ADDRESSES.clear()
            for device in result.get("devices", []):
                identity = str(device.get("identity") or "").strip()
                if identity:
                    DISCOVERY_LIVE_ADDRESSES[identity] = {
                        str(row.get("ip") or "").strip()
                        for row in device.get("addresses", [])
                        if str(row.get("ip") or "").strip()
                    }
        _record_discovery_diagnostics(result)

        # If DHCP moved the currently selected device, retain the identity but
        # update the saved host to a verified address. The live backend is not
        # switched silently; the scanner's Use button remains the explicit action.
        if active_identity:
            device = next((row for row in result.get("devices", [])
                           if row.get("identity") == active_identity), None)
            if device:
                live_ips = {str(row.get("ip") or "") for row in device.get("addresses", [])}
                preferred = str(device.get("preferred_ip") or "").strip()
                if preferred and old_host not in live_ips and preferred != old_host:
                    CFG["u64_host"] = preferred
                    _diag_event("info", f"Preferred address updated: {old_host or '(none)'} → {preferred}")
        save_config()
        return result
    finally:
        DISCOVERY_ACTIVE.clear()
        DISCOVERY_SCAN_LOCK.release()


@app.get("/api/discover")
async def api_discover(subnet: str = Query(""), port: int = Query(80)):
    """Sweep local /24 subnet(s) and return only verified Ultimate devices."""
    try:
        return await _run_discovery(subnet, port)
    except HTTPException:
        raise
    except Exception as e:
        err(e, 500)


def _disconnect_discovery_session() -> None:
    """Drop the active device clients without disturbing local receivers."""
    global rest, cmd, devfs
    # Backend replacement is a device operation too. Without this lifecycle
    # gate, browser /api/info polling can enter the old client while Clear or
    # Connect closes it, producing httpx's "client has been closed" runtime
    # failure and leaving the UI offline.
    with DEVICE_OP.operation("interactive", "disconnecting Ultimate session"):
        old_rest, old_cmd = rest, cmd
        try:
            if old_rest:
                _matrix_release_all(client=old_rest, silent=True, cached_only=True, caller="disconnect-session")
        except Exception:
            pass
        if old_rest and "STREAM_STATE" in globals():
            for stream_name in ("video", "audio"):
                if STREAM_STATE.get(stream_name):
                    try:
                        old_rest.stream_stop(stream_name)
                    except Exception:
                        pass
                    STREAM_STATE[stream_name] = False
        for resource in (old_cmd, old_rest):
            try:
                if resource:
                    resource.close()
            except Exception:
                pass
        rest = UltimateREST("", CFG.get("password", ""), coordinator=DEVICE_OP)
        cmd = CommandSocket("", coordinator=DEVICE_OP)
        devfs = DeviceFS("", CFG.get("ftp_user", "anonymous"),
                         CFG.get("ftp_password", ""), coordinator=DEVICE_OP)
        with INPUT_CAP_LOCK:
            INPUT_CAPABILITIES.clear()
        _READMEM_SUPPORT.clear()
        LINK_DETECTOR.clear()
        with LINK_STATE_LOCK:
            DISCOVERY_LIVE_ADDRESSES.clear()


def _clear_discovery_state() -> None:
    """Clear saved discovery identity and replace clients as one operation."""
    with DEVICE_OP.operation("interactive", "clearing discovery history"):
        CFG["known_devices"] = {}
        CFG["active_device_identity"] = ""
        CFG["u64_host"] = ""
        CFG["rest_control_host"] = ""
        CFG.pop("input_method_preferences", None)
        _disconnect_discovery_session()
        save_config()


@app.post("/api/discover/clear")
async def api_discover_clear(subnet: str = Query(""), port: int = Query(80)):
    """Clear remembered hosts, disconnect, then perform a genuinely fresh scan."""
    await run_in_threadpool(_clear_discovery_state)
    _diag_event("info", "Discovery history cleared — starting fresh scan")
    try:
        result = await _run_discovery(subnet, port)
    except HTTPException:
        raise
    except Exception as e:
        err(e, 500)
    result["cleared"] = True
    return result


@app.post("/api/connect")
def api_connect(payload: dict = Body(...)):
    """Switch device and report timing for each connection stage."""
    started = time.perf_counter()
    timing: dict[str, float | bool | str] = {}

    def mark(name: str, stage_started: float) -> float:
        now = time.perf_counter()
        timing[name] = round((now - stage_started) * 1000.0, 1)
        return now

    def finish(success: bool, *, error: str = "") -> None:
        timing["success"] = bool(success)
        timing["total_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        if error:
            timing["error"] = str(error)[:500]
        rendered = ", ".join(f"{key}={value}" for key, value in timing.items())
        _diag_event("info" if success else "warning", f"Connect timing: {rendered}")

    host = (payload.get("host") or "").strip()
    if not host:
        raise HTTPException(400, "host required")
    if INDEXJOB.get("running"):
        raise HTTPException(409, "stop the storage index before switching devices")
    global rest, cmd, devfs
    password = payload.get("password", CFG.get("password", ""))

    stage = time.perf_counter()
    control_host = _verified_control_host_for_selected(host)
    if control_host == host:
        control_host = _saved_control_host_for_selected(host)
    mark("persisted_route_lookup_ms", stage)
    timing["selected_host"] = host
    timing["control_host"] = control_host

    stage = time.perf_counter()
    new_rest = new_cmd = new_devfs = None
    try:
        new_rest = UltimateREST(control_host, password, coordinator=DEVICE_OP)
        new_cmd = CommandSocket(host, coordinator=DEVICE_OP)
        new_devfs = DeviceFS(host, CFG.get("ftp_user", "anonymous"),
                             CFG.get("ftp_password", ""), coordinator=DEVICE_OP)
    except Exception as e:
        mark("client_creation_ms", stage)
        for resource in (new_devfs, new_cmd, new_rest):
            try:
                if resource and hasattr(resource, "close"):
                    resource.close()
            except Exception:
                pass
        finish(False, error=str(e))
        return {"connected": False, "host": host, "error": str(e),
                "connect_timing": timing}
    mark("client_creation_ms", stage)

    # Finder has already verified both addresses of the grouped device. Reuse
    # either live result; manual addresses retain the original verification.
    stage = time.perf_counter()
    device_info = (_discovery_info_for_host(host)
                   or _discovery_info_for_host(control_host))
    reused_discovery = bool(device_info)
    mark("verified_result_lookup_ms", stage)
    timing["reused_discovery"] = reused_discovery

    stage = time.perf_counter()
    if not device_info:
        try:
            device_info = new_rest.info()
        except Exception as first_error:
            # A persisted Wi-Fi control address may have changed under DHCP.
            # Fall back to the explicitly selected address rather than leaving
            # Connect offline behind a stale alternate route.
            if control_host != host:
                try:
                    new_rest.close()
                    control_host = host
                    timing["control_host"] = control_host
                    timing["alternate_fallback"] = True
                    new_rest = UltimateREST(host, password, coordinator=DEVICE_OP)
                    device_info = new_rest.info()
                except Exception as fallback_error:
                    mark("live_verification_ms", stage)
                    for resource in (new_devfs, new_cmd, new_rest):
                        try:
                            if resource and hasattr(resource, "close"):
                                resource.close()
                        except Exception:
                            pass
                    finish(False, error=str(fallback_error))
                    return {"connected": False, "host": host,
                            "error": str(fallback_error),
                            "alternate_error": str(first_error),
                            "connect_timing": timing}
            else:
                mark("live_verification_ms", stage)
                for resource in (new_devfs, new_cmd, new_rest):
                    try:
                        if resource and hasattr(resource, "close"):
                            resource.close()
                    except Exception:
                        pass
                finish(False, error=str(first_error))
                return {"connected": False, "host": host, "error": str(first_error),
                        "connect_timing": timing}
    mark("live_verification_ms", stage)

    wait_started = time.perf_counter()
    with DEVICE_OP.operation("interactive", "switching Ultimate device"):
        acquired = time.perf_counter()
        timing["coordinator_wait_ms"] = round((acquired - wait_started) * 1000.0, 1)
        commit_started = acquired
        old_rest, old_cmd, old_devfs = rest, cmd, devfs
        old_selected_host = str(CFG.get("u64_host", "") or "").strip()
        old_control_host = str(getattr(old_rest, "host", "") or old_selected_host).strip()
        same_device = _same_known_device(old_selected_host, host)
        host_changed = bool(old_selected_host and old_selected_host != host)

        if host_changed and old_rest and "STREAM_STATE" in globals():
            for stream_name in ("video", "audio"):
                if STREAM_STATE.get(stream_name):
                    try:
                        # Split-control streams are driven through the selected
                        # Ethernet command socket, not the alternate REST host.
                        old_cmd.stream_off({"video": 0, "audio": 1}[stream_name])
                    except Exception:
                        try:
                            old_rest.stream_stop(stream_name)
                        except Exception:
                            pass
                    STREAM_STATE[stream_name] = False

        if host_changed and old_rest and not same_device:
            _matrix_release_all(
                client=old_rest,
                silent=True,
                cached_only=True,
                caller="connect-device-switch",
            )

        rest, cmd, devfs = new_rest, new_cmd, new_devfs
        if host_changed or old_control_host != control_host:
            _reset_readmem_support(control_host)
        capability_started = time.perf_counter()
        input_status = (_transfer_input_capability(old_control_host, control_host)
                        if same_device else _cached_input_status(new_rest))
        timing["capability_handling_ms"] = round(
            (time.perf_counter() - capability_started) * 1000.0, 1)
        CFG["u64_host"] = host
        CFG["rest_control_host"] = control_host
        CFG["password"] = password
        link_status = _link_payload(host, info_payload=device_info, force=False,
                                    persist=True, client=new_rest)
        save_config()
        timing["backend_replace_commit_ms"] = round(
            (time.perf_counter() - commit_started) * 1000.0, 1)
        if control_host != host:
            _diag_event(
                "info",
                f"Dual-interface route active: selected Ethernet {host}; "
                f"REST control via verified Wi-Fi {control_host}; "
                "streams and command socket remain on Ethernet",
            )
        cleanup_started = time.perf_counter()
        try:
            if old_cmd and old_cmd is not new_cmd:
                old_cmd.close()
            if old_rest and old_rest is not new_rest:
                old_rest.close()
            if old_devfs and old_devfs is not new_devfs and hasattr(old_devfs, "close"):
                old_devfs.close()
        except Exception:
            pass
        timing["old_client_cleanup_ms"] = round(
            (time.perf_counter() - cleanup_started) * 1000.0, 1)

    finish(True)
    return {
        "connected": True, "host": host, "control_host": control_host,
        "rest_via_alternate": control_host != host,
        "info": device_info, "input": input_status, "link": link_status,
        "reused_discovery": reused_discovery, "connect_timing": timing,
    }


# --- basic / machine ----------------------------------------------------

def _clean_shutdown():
    _juke_cancel_timer()
    _matrix_release_all(silent=True, cached_only=True, caller="shutdown")
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
    if DISCOVERY_ACTIVE.is_set():
        return {
            "u64deck_discovery_busy": True,
            "u64deck_busy_label": "DISCOVERY — scanning network…",
            "u64deck_retry_ms": 1000,
        }
    # Mount & Run can keep the Ultimate's small HTTP service occupied for a
    # long genuine-drive load.  Report that known local operation immediately
    # instead of queueing a status request behind it until the browser's
    # 15-second timeout makes a healthy, busy machine look offline.
    busy = _mount_run_busy_payload()
    if busy:
        return busy
    snapshot = DEVICE_OP.snapshot()
    if (getattr(snapshot, "active_priority", None) == "interactive" or
            getattr(snapshot, "waiting_interactive", 0)):
        return {
            "u64deck_operation_busy": True,
            "u64deck_busy_label": getattr(snapshot, "active_reason", "") or "Ultimate operation in progress",
            "u64deck_retry_ms": 1000,
        }
    try:
        # Status polling sits behind user actions but ahead of background work.
        with DEVICE_OP.operation("status", "checking Ultimate status"):
            active_rest = rest
            active_host = str(CFG.get("u64_host", "") or "").strip()
            payload = active_rest.info()
            if isinstance(payload, dict):
                payload = dict(payload)
                known_identity, _known_record = _known_identity_for_host(active_host)
                payload["u64deck_link"] = _link_payload(
                    active_host, info_payload=payload,
                    persist=not bool(known_identity), client=active_rest)
                payload["u64deck_input"] = _cached_input_status(active_rest)
        return payload
    except (UltimateError, httpx.HTTPError, RuntimeError) as e:
        err(e)


@app.get("/api/link/status")
def link_status(refresh: bool = Query(False)):
    if not CFG.get("u64_host"):
        return _current_link_payload(force=bool(refresh))
    try:
        return _current_link_payload(force=bool(refresh))
    except Exception as exc:
        _warn_event("link-status", f"could not classify active interface: {exc}")
        return {"ip": CFG.get("u64_host", ""), "link_type": LINK_UNKNOWN,
                "label": "Unknown", "addresses": [], "ethernet_ip": "",
                "wifi_ip": "", "streaming_available": True,
                "rest_timeout": DEFAULT_REST_TIMEOUT}


@app.get("/api/input/status")
def input_status(refresh: bool = Query(False)):
    if not CFG.get("u64_host"):
        return _cached_input_status()
    if refresh:
        return _input_status(force=True)
    return _cached_input_status()


@app.post("/api/input/events")
def input_events(payload: dict = Body(...)):
    try:
        clean = _validate_matrix_events(payload.get("events"))
        state = _matrix_send(clean)
        return {"errors": [], "mode": "matrix", "sent": len(clean),
                "state": state}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (UltimateError, httpx.HTTPError) as exc:
        err(exc)


@app.post("/api/input/release_all")
def input_release_all():
    status = _input_status()
    if not status.get("available"):
        return {"errors": [], "released": False, "mode": "buffer"}
    try:
        _matrix_send([{"kind": "release_all"}])
        return {"errors": [], "released": True, "mode": "matrix"}
    except (UltimateError, httpx.HTTPError) as exc:
        err(exc)


@app.put("/api/machine/{action}")
def machine(action: str):
    allowed = {"reset", "reboot", "pause", "resume", "poweroff", "menu_button"}
    if action not in allowed:
        raise HTTPException(400, "unknown action")
    try:
        # Keep the reset/reboot and optional post-boot key as one interactive
        # operation so indexing cannot resume during the cartridge-menu wait.
        with DEVICE_OP.operation("interactive", f"machine {action}"):
            if action in {"reset", "reboot"}:
                _juke_disarm_machine_takeover(f"machine {action}")
                _matrix_release_all(silent=True, caller=f"machine-{action}")
            result = rest.put(f"/v1/machine:{action}")
            if action == "reboot":
                _sid_runner_clear_reboot_required(rest)
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


def _mount_state_snapshot() -> dict[str, dict]:
    """Return a detached copy of u64deck's last confirmed mount state."""
    return {drive: dict(MOUNT_STATE.get(drive) or {}) for drive in ("a", "b")}


def _mount_run_busy_payload() -> dict | None:
    """Describe an active Mount & Run without touching the occupied device.

    The mount endpoint records a drive only after the Ultimate confirms the
    mount.  Exposing that local state lets the browser update Mounted Drives
    immediately while the synchronous endpoint continues through reset, LOAD
    and RUN.  No speculative mount is shown before the device accepts it.
    """
    snapshot = DEVICE_OP.snapshot()
    if (snapshot.active_priority == "interactive" and
            snapshot.active_reason == "mounting and booting disk"):
        return {
            "u64deck_busy": True,
            "u64deck_busy_reason": "mount_run",
            "u64deck_busy_label": "BUSY — loading program…",
            "u64deck_retry_ms": 2000,
            "u64deck_mounts": _mount_state_snapshot(),
        }
    return None

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

def _drive_status_unavailable_payload(exc: Exception) -> dict:
    """Return a controlled transient response for a backend lifecycle handover."""
    message = "Drive status temporarily unavailable while the device connection changes — retrying…"
    _diag_event("warning", message, error=str(exc))
    return {
        "u64deck_drives_unavailable": True,
        "u64deck_drives_message": message,
        "u64deck_retry_ms": 1000,
        "u64deck_mounts": _mount_state_snapshot(),
    }


def _decorate_drive_status(out: object) -> object:
    """Attach local mount/swap state to one device-reported drive payload."""
    decision = _reconcile_swap_from_drives(out)
    for row in out.get("drives", []) if isinstance(out, dict) else []:
        if not isinstance(row, dict):
            continue
        for key in ("a", "b"):
            if isinstance(row.get(key), dict):
                row[key]["u64deck_mount"] = dict(MOUNT_STATE.get(key) or {})
    if isinstance(out, dict):
        out["swap_reconstructed"] = bool(decision and decision.get("source") == "reconstructed")
        out["swap_decision"] = dict(decision or SWAP.get("decision") or {})
    return out


@app.get("/api/drives")
def drives():
    if DISCOVERY_ACTIVE.is_set():
        return {
            "u64deck_discovery_busy": True,
            "u64deck_drives_unavailable": True,
            "u64deck_drives_message": "Drive status paused while device discovery is running.",
            "u64deck_mounts": copy.deepcopy(MOUNT_STATE),
            "u64deck_retry_ms": 1000,
        }
    busy = _mount_run_busy_payload()
    if busy:
        return busy
    snapshot = DEVICE_OP.snapshot()
    if (getattr(snapshot, "active_priority", None) == "interactive" or
            getattr(snapshot, "waiting_interactive", 0)):
        return {
            "u64deck_operation_busy": True,
            "u64deck_drives_unavailable": True,
            "u64deck_drives_message": getattr(snapshot, "active_reason", "") or "Ultimate operation in progress",
            "u64deck_mounts": copy.deepcopy(MOUNT_STATE),
            "u64deck_retry_ms": 1000,
        }

    # Capture the active REST backend only after the status operation owns the
    # device coordinator. Connect/Clear use the same coordinator before they
    # replace and close a backend, so a cold-start drive poll can no longer
    # retain the old client while waiting behind the switch operation.
    active_rest = None
    for attempt in range(2):
        try:
            with DEVICE_OP.operation("status", "checking mounted drives"):
                active_rest = rest
                if active_rest is None:
                    raise RuntimeError("Ultimate REST client is unavailable")
                out = active_rest.get_json("/v1/drives")
            return _decorate_drive_status(out)
        except RuntimeError as exc:
            replacement = rest
            if attempt == 0 and replacement is not None and replacement is not active_rest:
                _diag_event(
                    "warning",
                    "Mounted Drives backend changed during refresh — retrying current connection",
                    error=str(exc),
                )
                continue
            if "closed" in str(exc).casefold() or "unavailable" in str(exc).casefold():
                return _drive_status_unavailable_payload(exc)
            err(exc)
        except (UltimateError, httpx.HTTPError) as exc:
            err(exc)


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
        _matrix_release_all(silent=True, caller="mount-upload")
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
        _matrix_release_all(silent=True, caller="mount-device")
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
        _matrix_release_all(silent=True, caller="copy-device-file")
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
        _matrix_release_all(silent=True, caller="backup-then-rw")
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
            return _run_direct_takeover(
                lambda: rest.post_file("/v1/runners:load_prg", f.name + ".prg", data),
                "loading PRG",
            )
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
            _juke_disarm_machine_takeover("Mount & Load")
            _matrix_release_all(silent=True, caller="mount-load")
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
            _legacy_type(line)
            time.sleep(0.4)
            _legacy_type(b"RUN\r")
            return _swap_response({"errors": [], "typed": f'LOAD"{f.name}",{bus_id},1 + RUN'}) if device_path else {"errors": [], "typed": f'LOAD"{f.name}",{bus_id},1 + RUN'}
    except (UltimateError, httpx.HTTPError) as e:
        err(e)


_PREKEYS = {"F1": 133, "F2": 137, "F3": 134, "F4": 138, "F5": 135,
            "F6": 139, "F7": 136, "F8": 140, "RETURN": 13, "SPACE": 32}
_PREKEY_MATRIX = {
    "F1": ["f1"], "F2": ["left_shift", "f1"],
    "F3": ["f3"], "F4": ["left_shift", "f3"],
    "F5": ["f5"], "F6": ["left_shift", "f5"],
    "F7": ["f7"], "F8": ["left_shift", "f7"],
    "RETURN": ["return"], "SPACE": ["space"],
}


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
    prefer_matrix = _input_status().get("available", False)
    time.sleep(max(0.0, delay))
    deadline = time.monotonic() + max(0.0, retry_window)
    attempts = 0
    while True:
        try:
            if prefer_matrix:
                _matrix_send([{"kind": "keyboard",
                               "inputs": _PREKEY_MATRIX[prekey],
                               "transition": "tap"}],
                             force_probe=attempts > 0)
            else:
                _legacy_type(bytes([_PREKEYS[prekey]]))
            return prekey
        except (UltimateError, httpx.HTTPError):
            if time.monotonic() >= deadline:
                raise
            attempts += 1
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


# ---------------------------------------------------------------------------
# BASIC readiness gate for Mount & Run.
#
# The KERNAL only consumes injected keyboard-buffer data when the screen
# editor is at its input loop. Typing during a reset/cartridge boot can lose
# the start of LOAD; typing RUN during a serial load can lose RUN entirely.
# Zero-page $CC is the KERNAL cursor-blink enable flag: 0 means the editor is
# accepting input. The gate debounces that state before either command.

_BASIC_GATE_POLL = 0.5
_BASIC_GATE_TIMEOUT = 120.0
_READMEM_SUPPORT: dict[str, bool] = {}

# Hardware diagnostics showed that the CIA1-capable U64 could acknowledge
# port-64 keyboard-buffer writes while discarding the first eight-byte LOAD
# chunk. RC15 therefore uses one ordered matrix-input batch for the complete
# command on CIA1 firmware and retains the proven one-shot buffer path only for
# Legacy devices.


def _readmem_cache_key() -> str:
    return str(getattr(rest, "host", "") or CFG.get("u64_host", "")).strip()


def _reset_readmem_support(host: str | None = None) -> None:
    """Forget a cached readmem result after a reconnect or firmware change."""
    key = str(host or _readmem_cache_key()).strip()
    if key:
        _READMEM_SUPPORT.pop(key, None)


def _read_basic_ready_flag() -> int | None:
    """Return $CC (0 = editor input loop), or ``None`` if unsupported."""
    key = _readmem_cache_key()
    if key and _READMEM_SUPPORT.get(key) is False:
        return None
    try:
        data = rest.read_memory("00CC", 1)
    except Exception:
        return -1  # transient failure: keep polling until timeout
    if data is None:
        if not key or key not in _READMEM_SUPPORT:
            _diag_event(
                "info",
                "machine:readmem not available on this firmware — "
                "Mount & Run uses fixed delays",
            )
        if key:
            _READMEM_SUPPORT[key] = False
        return None
    if key:
        _READMEM_SUPPORT[key] = True
    return data[0] if data else -1


_MOUNT_RUN_MATRIX_CHAR_INPUTS = {
    '"': ["left_shift", "2"],
    "*": ["star"],
    ",": ["comma"],
    "\r": ["return"],
    "\n": ["return"],
}


def _mount_run_matrix_events(text: str, *, release_first: bool = True) -> list[dict]:
    """Translate the small BASIC command subset used by Mount & Run.

    CIA1-capable firmware receives the complete LOAD/RUN line as one ordered
    ``machine:input`` event batch.  This deliberately avoids the port-64
    KERNAL-buffer boundary which hardware diagnostics showed could discard the
    first eight-byte LOAD chunk on repeated launches.
    """
    events: list[dict] = []
    if release_first:
        events.append({"kind": "release_all"})
    for char in str(text):
        lower = char.lower()
        if "a" <= lower <= "z" or "0" <= char <= "9":
            inputs = [lower if char.isalpha() else char]
        else:
            inputs = _MOUNT_RUN_MATRIX_CHAR_INPUTS.get(char)
        if not inputs:
            raise ValueError(f"unsupported Mount & Run matrix character {char!r}")
        events.append({"kind": "keyboard", "inputs": list(inputs), "transition": "tap"})
    return events


def _type_mount_run_load(data: bytes) -> tuple[bool, str]:
    """Deliver the complete Mount & Run LOAD line on the proven device path.

    CIA1-capable Ultimate 64 firmware uses one ordered matrix-input batch for
    the entire command.  Legacy-only C64 Ultimate firmware retains the exact
    established one-shot KERNAL-buffer path that did not reproduce the fault.
    An ambiguous matrix failure is never followed by a Legacy resend.
    """
    payload = bytes(data)
    if not payload:
        return False, "LOAD command is empty"

    status = _input_status(rest)
    if status.get("available"):
        try:
            command = payload.decode("ascii")
            events = _mount_run_matrix_events(command)
            _matrix_send(events, client=rest)
        except Exception as exc:
            note = f"CIA1 matrix LOAD delivery failed — LOAD not resent ({exc})"
            _warn_event("mount-load-delivery", f"Mount & Run: {note}")
            _diag_event("warning", "Mount & Run LOAD delivery: CIA1 matrix failed")
            return False, note
        taps = sum(1 for event in events if event.get("kind") == "keyboard")
        _diag_event(
            "info",
            f"Mount & Run LOAD delivery: CIA1 matrix ({taps} ordered key taps)",
        )
        return True, "CIA1 matrix"

    _legacy_type(payload)
    _diag_event(
        "info",
        "Mount & Run LOAD delivery: Legacy KERNAL buffer "
        "(established one-shot path retained)",
    )
    return True, "Legacy KERNAL buffer"


def _basic_ready_gate(stage: str, *, timeout: float = _BASIC_GATE_TIMEOUT,
                      poll: float = _BASIC_GATE_POLL, grace: float = 0.0,
                      reader=None, sleeper=time.sleep,
                      clock=time.monotonic) -> str:
    """Wait until two consecutive reads show the BASIC editor is ready.

    Returns ``ready``, ``timeout`` or ``unsupported``. Every completion is
    recorded in Diagnostics so hardware behaviour can be verified without
    changing the timings speculatively.
    """
    reader = reader or _read_basic_ready_flag
    started = clock()
    last_value: int | None = None

    def finish(result: str) -> str:
        elapsed = max(0.0, clock() - started)
        shown = "unsupported" if last_value is None else str(last_value)
        _diag_event(
            "info",
            f"Mount & Run gate '{stage}': {result} after {elapsed:.1f}s "
            f"(last $CC read: {shown})",
        )
        return result

    if grace > 0:
        sleeper(grace)
    deadline = started + max(1.0, float(timeout))
    consecutive = 0
    while True:
        last_value = reader()
        if last_value is None:
            return finish("unsupported")
        if last_value == 0:
            consecutive += 1
            if consecutive >= 2:
                return finish("ready")
        else:
            consecutive = 0
        if clock() >= deadline:
            return finish("timeout")
        sleeper(max(0.0, float(poll)))


def _bus_id_for(drive: str) -> int:
    try:
        for d in rest.get_json("/v1/drives").get("drives", []):
            if drive in d and d[drive].get("enabled"):
                return d[drive].get("bus_id", 8)
    except Exception:
        pass
    return 8


_RUN_MATRIX_EVENTS = [
    {"kind": "keyboard", "inputs": ["r"], "transition": "tap"},
    {"kind": "keyboard", "inputs": ["u"], "transition": "tap"},
    {"kind": "keyboard", "inputs": ["n"], "transition": "tap"},
    {"kind": "keyboard", "inputs": ["return"], "transition": "tap"},
]


def _dispatch_run_after_gate() -> tuple[bool, str]:
    """Deliver RUN after the load gate using the best proven transport.

    CIA1-capable U64 sessions use matrix key taps for both LOAD and RUN. Legacy
    C64U sessions keep the established command-buffer delivery. A failed matrix
    request is not followed by a second transport, avoiding a late duplicate
    command after an ambiguous timeout.
    """
    status = _input_status(rest)
    if status.get("available"):
        try:
            _matrix_send(list(_RUN_MATRIX_EVENTS), client=rest)
        except Exception as exc:
            note = f"CIA1 matrix RUN delivery failed — RUN not resent ({exc})"
            _warn_event("mount-run-delivery", f"Mount & Run: {note}")
            _diag_event("warning", "Mount & Run RUN delivery: CIA1 matrix failed")
            return False, note
        _diag_event("info", "Mount & Run RUN delivery: CIA1 matrix")
        return True, "CIA1 matrix"

    _legacy_type(b"RUN\r")
    _diag_event("info", "Mount & Run RUN delivery: Legacy KERNAL buffer")
    return True, "Legacy KERNAL buffer"


def _mount_and_boot(drive: str, mode: str, *, device_path: str = None,
                    name: str = None, data: bytes = None):
    """Mount an image and autostart it: reset, LOAD"*",{bus},1 + RUN.

    Readiness gates protect both stages when machine:readmem is available.
    CIA1-capable U64s deliver the complete LOAD and RUN lines through ordered
    matrix key taps. Legacy devices retain the established one-shot KERNAL
    buffer path; a readmem 404 retains the fixed-delay readiness behaviour.
    """
    drive, mode = _drive_key(drive), _mount_mode(mode)
    with DEVICE_OP.operation("interactive", "mounting and booting disk"):
        _juke_disarm_machine_takeover("Mount & Run")
        cached_input = _cached_input_status(rest)
        matrix_expected = cached_input.get("available") is True
        released = _matrix_release_all(
            silent=True,
            cached_only=True,
            caller="mount-run-start",
        )
        if matrix_expected and not released:
            note = "CIA1 matrix release failed — Mount & Run aborted"
            _warn_event("mount-input-release", f"Mount & Run: {note}")
            return {"errors": [], "typed": "", "note": note}
        _sid_runner_reboot_before_mount_run()
        if device_path:
            rest.mount_path(drive, device_path, mode=mode)
        else:
            rest.mount_attachment(drive, name, data, mode=mode)
        _remember_mount(drive, mode, path=device_path or "", name=name or (device_path or "").rsplit("/", 1)[-1])
        rest.put("/v1/machine:reset")
        _boot_settle()
        bus_id = _bus_id_for(drive)
        load_line = f'LOAD"*",{bus_id},1'

        boot_gate = _basic_ready_gate("boot")
        if boot_gate == "timeout":
            note = "machine not ready — LOAD not typed"
            _warn_event("mount-boot-gate", f"Mount & Run: {note}")
            return {"errors": [], "typed": "", "note": note}

        load_sent, load_delivery = _type_mount_run_load(
            (load_line + "\r").encode()
        )
        if not load_sent:
            note = f"LOAD not completed — {load_delivery}"
            _warn_event("mount-load-delivery", f"Mount & Run: {note}")
            return {"errors": [], "typed": "", "note": note}

        if boot_gate == "unsupported":
            time.sleep(0.4)
            delivered, delivery = _dispatch_run_after_gate()
            if delivered:
                return {"errors": [], "typed": f"{load_line} + RUN"}
            return {"errors": [], "typed": load_line, "note": delivery}

        load_gate = _basic_ready_gate("load", grace=1.0)
        if load_gate == "ready":
            delivered, delivery = _dispatch_run_after_gate()
            if delivered:
                return {"errors": [], "typed": f"{load_line} + RUN"}
            return {"errors": [], "typed": load_line,
                    "note": delivery}
        if load_gate == "unsupported":
            time.sleep(0.4)
            delivered, delivery = _dispatch_run_after_gate()
            if delivered:
                return {"errors": [], "typed": f"{load_line} + RUN"}
            return {"errors": [], "typed": load_line, "note": delivery}

        note = "load still running — RUN not sent"
        _warn_event("mount-load-gate", f"Mount & Run: {note}")
        return {"errors": [], "typed": load_line, "note": note}


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


def _run_cart_safe(fn, *, preserve_jukebox: bool = False,
                   timings: dict | None = None):
    """Run a DMA action with any freezer cartridge parked.

    ``timings`` is an optional caller-owned dictionary used by the SID
    Jukebox diagnostics.  Normal runner callers remain unchanged, while the
    Jukebox can distinguish device upload time from cartridge lookup, park and
    restore time when hardware reports a visible delay.
    """
    started = time.monotonic()
    with DEVICE_OP.operation("interactive", "running software"):
        acquired = time.monotonic()
        if timings is not None:
            timings["cart_safe_wait_ms"] = round((acquired - started) * 1000.0, 1)
        if not preserve_jukebox:
            _juke_disarm_machine_takeover("runner action")
        stage = time.monotonic()
        cart = _cart_configured() if CFG.get("cart_safe_run", True) else ""
        if timings is not None:
            timings["cart_lookup_ms"] = round((time.monotonic() - stage) * 1000.0, 1)
            timings["cartridge_configured"] = bool(cart)
        if cart:
            stage = time.monotonic()
            try:
                rest.put(f"/v1/configs/{_CART_CAT}/{_CART_ITEM}", value="")
            except Exception:
                cart = ""
            finally:
                if timings is not None:
                    timings["cart_park_ms"] = round(
                        (time.monotonic() - stage) * 1000.0, 1
                    )
                    timings["cartridge_parked"] = bool(cart)
        try:
            stage = time.monotonic()
            return fn()
        finally:
            if timings is not None:
                timings["runner_action_ms"] = round(
                    (time.monotonic() - stage) * 1000.0, 1
                )
            if cart:
                stage = time.monotonic()
                try:
                    rest.put(f"/v1/configs/{_CART_CAT}/{_CART_ITEM}", value=cart)
                except Exception:
                    pass
                finally:
                    if timings is not None:
                        timings["cart_restore_ms"] = round(
                            (time.monotonic() - stage) * 1000.0, 1
                        )
            if timings is not None:
                timings["cart_safe_total_ms"] = round(
                    (time.monotonic() - started) * 1000.0, 1
                )


def _run_direct_takeover(fn, reason: str = "runner action"):
    """Run a non-cartridge-safe machine action after disarming SID timers."""
    with DEVICE_OP.operation("interactive", reason):
        _juke_disarm_machine_takeover(reason)
        return fn()


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


def _bcd_byte(value: int) -> int:
    """Encode a non-negative decimal value from 0 to 99 as packed BCD."""
    value = max(0, min(99, int(value)))
    return ((value // 10) << 4) | (value % 10)


def _sid_ssl_payload(data: bytes) -> bytes | None:
    """Build the Ultimate player's compact per-SID ``.ssl`` length array.

    The firmware reads at most 512 bytes and expects two packed-BCD bytes per
    subtune: minutes followed by seconds. Missing subtunes are represented by
    zeroes, which makes the player retain its normal default for those entries.
    """
    meta = _parse_sid(data)
    if not meta:
        return None
    times = SONGLENGTHS.get(str(meta.get("md5") or "").lower())
    if not times:
        return None

    songs = min(256, max(1, int(meta.get("songs") or 1)))
    payload = bytearray()
    for index in range(songs):
        if index >= len(times):
            payload.extend((0, 0))
            continue
        try:
            total = max(0, int(float(times[index]) + 0.5))
        except (TypeError, ValueError, OverflowError):
            payload.extend((0, 0))
            continue
        minutes, seconds = divmod(total, 60)
        if minutes > 99:
            minutes, seconds = 99, 59
        payload.extend((_bcd_byte(minutes), _bcd_byte(seconds)))
    return bytes(payload) if any(payload) else None


def _sid_runner_device_key(client=None) -> str:
    active = client or rest
    return str(getattr(active, "host", "") or "").strip().casefold()


def _sid_runner_mark_reboot_required(client=None) -> None:
    key = _sid_runner_device_key(client)
    if not key:
        return
    with JUKE_TIMER_LOCK:
        SID_RUNNER_REBOOT_REQUIRED.add(key)


def _sid_runner_reboot_required(client=None) -> bool:
    key = _sid_runner_device_key(client)
    if not key:
        return False
    with JUKE_TIMER_LOCK:
        return key in SID_RUNNER_REBOOT_REQUIRED


def _sid_runner_clear_reboot_required(client=None) -> None:
    key = _sid_runner_device_key(client)
    if not key:
        return
    with JUKE_TIMER_LOCK:
        SID_RUNNER_REBOOT_REQUIRED.discard(key)


def _wait_for_ultimate_after_reboot(client, *, timeout: float = 12.0,
                                    poll: float = 0.4, sleeper=time.sleep,
                                    clock=time.monotonic) -> dict:
    """Wait for the Ultimate REST service after a full machine reboot."""
    deadline = clock() + max(1.0, float(timeout))
    last_error = None
    while True:
        try:
            probe = getattr(client, "probe_info", None)
            if callable(probe):
                return probe(request_timeout=min(1.5, max(0.2, float(poll) * 3)))
            return client.info()
        except Exception as exc:
            last_error = exc
        if clock() >= deadline:
            raise UltimateError(
                f"Ultimate did not return after SID-player recovery reboot: {last_error}"
            ) from last_error
        sleeper(max(0.05, float(poll)))


def _sid_runner_reboot_before_mount_run() -> bool:
    """Clear native SID-runner state before the next disk autostart.

    A normal ``machine:reset`` is sufficient for ordinary Mount & Run actions,
    but hardware testing showed that the native SID player can leave the C64 in
    a residual state where LOAD/RUN are accepted yet do not start the mounted
    program.  One full machine reboot restores the normal boot path.  The
    reboot is conditional and per device, so ordinary Mount & Run remains fast.
    """
    active_rest = rest
    if not _sid_runner_reboot_required(active_rest):
        return False
    _diag_event(
        "info",
        "Mount & Run: rebooting Ultimate to clear previous SID-player state",
    )
    active_rest.put("/v1/machine:reboot", request_timeout=4.0)
    if cmd:
        cmd.close()
    time.sleep(max(2.5, float(CFG.get("boot_wait", 2.8))))
    _wait_for_ultimate_after_reboot(active_rest)
    _sid_runner_clear_reboot_required(active_rest)
    _diag_event(
        "info",
        "Mount & Run: Ultimate reboot completed — continuing with mount",
    )
    return True


def _post_sid_upload(filename: str, data: bytes, songnr: int | None = None):
    """Upload a SID and remember that a later disk launch may need reboot."""
    params = {"songnr": int(songnr)} if songnr else {}
    ssl_payload = _sid_ssl_payload(data)
    ssl_name = Path(filename).with_suffix(".ssl").name
    active_rest = rest
    result = active_rest.post_sid(
        filename,
        data,
        songlengths=ssl_payload,
        songlengths_filename=ssl_name,
        **params,
    )
    _sid_runner_mark_reboot_required(active_rest)
    return result


@app.put("/api/run/device")
def run_device(path: str = Query(...)):
    try:
        if path.lower().endswith(".t64"):
            name, prg = _t64_first_prg(devfs.fetch(path))
            return _run_cart_safe(
                lambda: rest.run_prg(name + ".prg", prg))
        runner = _runner_for(path)
        if runner == "run_crt":
            return _run_direct_takeover(
                lambda: rest.put(f"/v1/runners:{runner}", file=path),
                "running cartridge",
            )
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
                _run_direct_takeover,
                lambda: rest.post_file(f"/v1/runners:{runner}", name, data),
                "running cartridge",
            )
        if runner == "sidplay":
            return await run_in_threadpool(
                _run_cart_safe, lambda: _post_sid_upload(name, data))
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
        _legacy_type(data)
        return {"errors": [], "sent": len(data)}
    except UltimateError as e:
        err(e)


# --- streams ------------------------------------------------------------

STREAM_STATE = {"video": False, "audio": False}
STREAM_LAST = {}    # per-stream record of the last start/stop attempt


def _stream_ctl(name: str, on: bool):
    if on and _current_link_payload().get("link_type") == LINK_WIFI:
        raise UltimateError(
            "Streaming is not available over the Ultimate Wi-Fi interface; connect using Ethernet")
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
    split_control = bool(getattr(rest, "host", "") and
                         getattr(rest, "host", "") != CFG.get("u64_host", ""))
    if split_control:
        # The selected interface is Ethernet but ordinary REST control is using
        # the paired Wi-Fi address. Start/stop streams over port 64 first so the
        # stream remains bound to the selected wired path and avoids the slow
        # Ethernet REST listener entirely.
        rec["via"] = "socket"
        try:
            if on:
                cmd.stream_on(stream_id, dest)
            else:
                cmd.stream_off(stream_id)
            rec["ok"] = True
        except Exception as socket_error:
            rec["socket_error"] = str(socket_error)
            rec["via"] = "rest"
            try:
                if on:
                    rest.stream_start(name, dest)
                else:
                    rest.stream_stop(name)
                rec["ok"] = True
            except Exception as rest_error:
                rec["rest_error"] = str(rest_error)
                rec["ok"] = False
                raise
    else:
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
    if state == "start" and _current_link_payload().get("link_type") == LINK_WIFI:
        raise HTTPException(409,
            "Streaming is not available over Wi-Fi. Connect using the Ultimate's Ethernet address.")
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
        "link": _current_link_payload(),
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
        # Keyboard capture is associated with the screen/video session.  Run
        # the safety release in a worker thread so an Ultimate REST timeout
        # cannot block the asyncio event loop and freeze the whole UI.
        await run_in_threadpool(
            lambda: _matrix_release_cleanup(caller="ws-video-disconnect")
        )


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
        # Audio has no keyboard ownership.  Releasing matrix input here caused
        # duplicate cleanup requests when video and audio sockets closed
        # together, so only the video session performs the safety release.


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
        _matrix_release_all(silent=True, caller=f"assembly64-{action}")
        out = rest.mount_attachment(drive, filename, data, mode=mode)
        _remember_mount(drive, mode, name=filename)
        return out
    if action == "mount_run":
        if not low.endswith((".d64", ".d71", ".d81", ".g64")):
            raise ValueError("Mount & Run is only available for disk images")
        return _mount_and_boot("a", _mount_mode(None), name=filename, data=data)
    if low.endswith((".d64", ".d71", ".d81", ".g64")):
        mode = _mount_mode(None)
        def mount_and_reset():
            _matrix_release_all(silent=True, caller="assembly64-disk-run")
            out = rest.mount_attachment("a", filename, data, mode=mode)
            _remember_mount("a", mode, name=filename)
            rest.put("/v1/machine:reset")
            return out
        return _run_direct_takeover(mount_and_reset, "running Assembly64 disk")
    if low.endswith(".crt"):
        return _run_direct_takeover(
            lambda: rest.post_file("/v1/runners:run_crt", filename, data),
            "running Assembly64 cartridge",
        )
    if low.endswith(".sid"):
        return _run_cart_safe(lambda: _post_sid_upload(filename, data))
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
        return "number", (0, int(value), "")
    compound = _re.fullmatch(r"(\d+)([a-z])", value)
    if compound:
        return "number", (0, int(compound.group(1)), compound.group(2))
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
        r"(?P<base>.+?)[\s._-]*(?P<open>[\(\[])\s*(?P<body>[^\)\]]+)\s*[\)\]]",
        stem,
    )
    if wrapped:
        result = marked(wrapped.group("base"), wrapped.group("body"))
        if result:
            return result
        result = numbered_total(wrapped.group("base"), wrapped.group("body"))
        if result:
            return result
        # Bare wrapped tokens: WeAreDemo(A)/(B), Game(1)/(2). Parenthesised
        # letters are accepted; square-bracket letters are GoodTools-style
        # alternate-dump markers and deliberately remain ungrouped.
        body = wrapped.group("body").strip().casefold()
        bare = None
        if _re.fullmatch(r"[a-z]", body) and wrapped.group("open") == "(":
            bare = body
        elif body.isdigit():
            bare = body
        if bare is not None:
            title = _swap_normalize_title(wrapped.group("base"))
            if title:
                token_kind, token_sort = _swap_token(bare)
                return (ext, "bare-wrapped", title, token_kind), token_sort

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

    # Generic numbered series such as Scratch-1.d64 / Scratch-2.d64 and
    # compound demo numbering such as EdgeOfDisgrace_1a.d64. A separator is
    # mandatory, so glued sequel-like names remain deliberately excluded.
    trailing = _re.fullmatch(
        r"(?P<base>.+?)[\s._-]+(?P<token>\d+[a-z]?)",
        stem,
        flags=_re.IGNORECASE,
    )
    if trailing:
        title = _swap_normalize_title(trailing.group("base"))
        if title:
            token_kind, token_sort = _swap_token(trailing.group("token"))
            return (ext, "numbered", title, token_kind), token_sort

    # Title-less marker names such as side1.d64 / side2.d64. The complete
    # stem must be marker+token, so Riverside1 can never be misclassified.
    untitled = _re.fullmatch(
        r"(?P<marker>disk|disc|side|part|volume|vol)[\s._-]*(?P<token>\d+|[a-z])",
        stem,
        flags=_re.IGNORECASE,
    )
    if untitled:
        marker = _SWAP_MARKER_ALIASES[untitled.group("marker").casefold()]
        token_kind, token_sort = _swap_token(untitled.group("token"))
        return (ext, "marked-untitled", marker, token_kind), token_sort
    return None


def _swap_group_candidates(current_name: str, sibling_names) -> list[str]:
    """Return only siblings confidently belonging to the current disk set."""
    current_name = current_name.rsplit("/", 1)[-1]
    current_signature = _swap_signature(current_name)
    if current_signature is None:
        return [current_name]
    family, _current_token = current_signature

    # Game.d64 beside Game(a).d64 / Game(b).d64 is an alternate-dump family,
    # not a multi-disk set. The unsuffixed sibling veto keeps it solo.
    if len(family) >= 3 and family[1] == "bare-wrapped":
        ext, _kind, title = family[0], family[1], family[2]
        for name in list(sibling_names) + [current_name]:
            plain = name.rsplit("/", 1)[-1]
            plain_stem, plain_dot, plain_ext = plain.rpartition(".")
            if not plain_dot or ("." + plain_ext.casefold()) != ext:
                continue
            if (_swap_signature(plain) is None and
                    _swap_normalize_title(plain_stem) == title):
                return [current_name]

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
        _matrix_release_all(silent=True, caller="disk-swap")
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
        "radio": False, "song": 0, "timer": None, "folder": "", "loading": False,
        "source": "", "generation": 0, "stop_after_current": False}
JUKE_TIMER_LOCK = _threading.RLock()
JUKE_PLAYED: set[str] = set()
JUKE_RECENT_TRACKS = deque(maxlen=80)
# Native SID playback can leave some firmware/device combinations in a state
# that a normal C64 reset does not fully clear.  Track this per Ultimate so the
# next Mount & Run can perform one full machine reboot before mounting.  The
# flag deliberately survives Jukebox Stop and timer disarming; only a confirmed
# reboot clears it.
SID_RUNNER_REBOOT_REQUIRED: set[str] = set()
SIDFLOW_DB_PATH = ROOT / ".sidflow-similarity.sqlite"
SIDFLOW_STORE = SimilarityStore(SIDFLOW_DB_PATH)
SIDFLOW_PRESENT_CACHE = {"signature": None, "paths": {}}
SIDFLOW_JOB = {
    "running": False, "stage": "idle", "downloaded": 0, "total": 0,
    "processed": 0, "process_total": 0, "message": "", "error": "",
    "started": 0.0, "completed": "", "asset": "", "release": "",
}
SIDFLOW_LOCK = threading.RLock()
SIDFLOW_THREAD = None
_SIDFLOW_STALE_WARNED: set[str] = set()
# Interrupted downloads/builds are restartable rather than resumable. Stale
# artifacts are diagnostic-only: a locked remnant must never become the current
# user-visible import error or block the unique build path used by a new run.
def _sidflow_stale_artifacts(include_downloads: bool = True) -> list[Path]:
    paths: list[Path] = []
    if include_downloads:
        paths.extend([
            ROOT / ".sidflow-source.sqlite.download",
            ROOT / ".sidflow-manifest.json.download",
            ROOT / ".sidflow-SHA256SUMS.download",
        ])
    paths.extend(ROOT.glob(SIDFLOW_DB_PATH.name + ".building*"))
    paths.extend(ROOT.glob(SIDFLOW_DB_PATH.name + ".ready-*"))
    # Preserve order while de-duplicating on case-insensitive Windows paths.
    seen = set()
    out = []
    for path in paths:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key); out.append(path)
    return out


def _sidflow_cleanup_stale_artifacts(include_downloads: bool = True) -> list[str]:
    """Best-effort cleanup that never becomes the current import error.

    Windows antivirus/indexers can retain a handle to an interrupted build. A
    new import uses a unique build name, so cleanup is diagnostic only. Report
    each still-locked path once until it is eventually removed.
    """
    warnings = []
    for path in _sidflow_stale_artifacts(include_downloads):
        key = str(getattr(path, "name", path)).casefold()
        try:
            path.unlink()
            _SIDFLOW_STALE_WARNED.discard(key)
        except FileNotFoundError:
            _SIDFLOW_STALE_WARNED.discard(key)
            continue
        except OSError as exc:
            message = f"SIDFlow stale artifact cleanup deferred for {path.name}: {exc}"
            warnings.append(message)
            if key not in _SIDFLOW_STALE_WARNED:
                _SIDFLOW_STALE_WARNED.add(key)
                _diag_event("warning", message)
    return warnings

_sidflow_cleanup_stale_artifacts()
SONGLENGTHS = {}          # md5 -> [seconds per subsong]
SONGLENGTHS_BY_PATH = {}  # casefolded HVSC-relative path -> [seconds per subsong]
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


def _sidflow_job_update(**values) -> None:
    with SIDFLOW_LOCK:
        SIDFLOW_JOB.update(values)


def _sidflow_public_status() -> dict:
    status = SIDFLOW_STORE.status()
    with SIDFLOW_LOCK:
        job = dict(SIDFLOW_JOB)
    if job.get("running"):
        elapsed = max(0.0, time.monotonic() - float(job.get("started") or 0.0))
    else:
        elapsed = 0.0
    job["elapsed"] = round(elapsed, 1)
    status.update({"job": job, "schema_required": SIDFLOW_SCHEMA,
                   "attribution": "Powered by SIDFlow (Chris Gleissner)"})
    return status


def _sidflow_asset_plan(client: httpx.Client) -> dict:
    """Resolve the latest full export containing perceptual features."""
    assets = {}
    tag = "latest"
    published = ""
    try:
        response = client.get(SIDFLOW_RELEASE_API)
        response.raise_for_status()
        release = response.json()
        tag = str(release.get("tag_name") or "latest")
        published = str(release.get("published_at") or "")
        assets = {
            str(item.get("name") or ""): str(item.get("browser_download_url") or "")
            for item in release.get("assets", []) if item.get("name")
        }
    except Exception:
        # Stable latest/download URLs are the documented fallback and also
        # avoid making the feature depend on GitHub's unauthenticated API quota.
        assets = {}

    # The current mobile profile omits features_json, which is required for
    # musically useful recommendations. Prefer the full export until a future
    # mobile manifest explicitly advertises the same perceptual feature payload.
    candidates = [(SIDFLOW_FULL_SQLITE, SIDFLOW_FULL_MANIFEST, "full")]
    for sqlite_name, manifest_name, profile in candidates:
        if assets and sqlite_name not in assets:
            continue
        if assets and manifest_name not in assets:
            continue
        return {
            "sqlite_name": sqlite_name,
            "manifest_name": manifest_name,
            "profile": profile,
            "sqlite_url": assets.get(sqlite_name) or f"{SIDFLOW_DOWNLOAD_BASE}/{sqlite_name}",
            "manifest_url": assets.get(manifest_name) or f"{SIDFLOW_DOWNLOAD_BASE}/{manifest_name}",
            "checksums_url": assets.get(SIDFLOW_CHECKSUMS) or f"{SIDFLOW_DOWNLOAD_BASE}/{SIDFLOW_CHECKSUMS}",
            "tag": tag, "published_at": published,
        }
    # No mobile/full pair was visible in the release listing. Fall back to the
    # stable full names; schema validation still prevents an unsafe import.
    return {
        "sqlite_name": SIDFLOW_FULL_SQLITE,
        "manifest_name": SIDFLOW_FULL_MANIFEST,
        "profile": "full",
        "sqlite_url": f"{SIDFLOW_DOWNLOAD_BASE}/{SIDFLOW_FULL_SQLITE}",
        "manifest_url": f"{SIDFLOW_DOWNLOAD_BASE}/{SIDFLOW_FULL_MANIFEST}",
        "checksums_url": f"{SIDFLOW_DOWNLOAD_BASE}/{SIDFLOW_CHECKSUMS}",
        "tag": tag, "published_at": published,
    }


def _sidflow_download_file(client: httpx.Client, url: str, target: Path) -> int:
    """Stream one release asset to disk while updating UI progress."""
    target.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    with client.stream("GET", url) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        _sidflow_job_update(stage="downloading", downloaded=0, total=total,
                            message="Downloading SIDFlow similarity export…")
        with target.open("wb") as fh:
            for chunk in response.iter_bytes(1024 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                downloaded += len(chunk)
                _sidflow_job_update(downloaded=downloaded, total=total)
    return downloaded


def _sidflow_import_worker() -> None:
    download = ROOT / ".sidflow-source.sqlite.download"
    manifest_temp = ROOT / ".sidflow-manifest.json.download"
    checksum_temp = ROOT / ".sidflow-SHA256SUMS.download"
    # Clean what we can, but never let a locked legacy build file poison this
    # import. slim_and_promote always chooses a fresh unique destination.
    _sidflow_cleanup_stale_artifacts()
    try:
        headers = {"User-Agent": f"u64deck/{VERSION}", "Accept": "application/vnd.github+json"}
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, read=180.0),
                          headers=headers) as client:
            _sidflow_job_update(stage="manifest", message="Checking the latest SIDFlow export…")
            plan = _sidflow_asset_plan(client)
            manifest_response = client.get(plan["manifest_url"])
            manifest_response.raise_for_status()
            manifest_bytes = manifest_response.content
            manifest = validate_manifest(manifest_response.json())
            manifest.update({
                "u64deck_source_asset": plan["sqlite_name"],
                "u64deck_manifest_asset": plan["manifest_name"],
                "u64deck_release_tag": plan["tag"],
                "u64deck_release_published_at": plan["published_at"],
                "u64deck_downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            manifest_temp.write_bytes(manifest_bytes)
            sums_response = client.get(plan["checksums_url"])
            sums_response.raise_for_status()
            checksum_temp.write_bytes(sums_response.content)
            checksums = parse_sha256sums(sums_response.text)
            expected_manifest = checksums.get(plan["manifest_name"])
            if expected_manifest:
                actual_manifest = hashlib.sha256(manifest_bytes).hexdigest()
                if actual_manifest != expected_manifest:
                    raise ValueError("SIDFlow manifest checksum verification failed")
            expected_sqlite = checksums.get(plan["sqlite_name"])
            if not expected_sqlite:
                expected_sqlite = str((manifest.get("file_checksums") or {}).get("sqlite_sha256") or "").lower()
            if len(expected_sqlite) != 64:
                raise ValueError(f"No SHA-256 checksum was published for {plan['sqlite_name']}")
            _sidflow_job_update(asset=plan["sqlite_name"], release=plan["tag"])
            _sidflow_download_file(client, plan["sqlite_url"], download)

        _sidflow_job_update(stage="verifying", message="Verifying SIDFlow download…")
        if sha256_file(download) != expected_sqlite:
            raise ValueError("SIDFlow SQLite checksum verification failed")

        def progress(stage: str, current: int, total: int) -> None:
            messages = {
                "extracting": "Extracting SIDFlow perceptual features…",
                "normalising": "Normalising compact SIDFlow vectors…",
                "neighbors": "Importing SIDFlow neighbour rows…",
            }
            _sidflow_job_update(stage=stage, processed=current, process_total=total,
                                message=messages.get(stage, "Building compact SIDFlow database…"))

        SIDFLOW_STORE.invalidate()
        result = slim_and_promote(
            download, SIDFLOW_DB_PATH, manifest, progress,
            promotion_lock=SIDFLOW_STORE.file_lock,
        )
        SIDFLOW_STORE.invalidate()
        SIDFLOW_STORE.warm()
        delete_note = ("; source cleanup will retry at restart"
                       if result.get("source_delete_warning") else "")
        _sidflow_job_update(running=False, stage="ready", downloaded=0, total=0,
                            processed=result["tracks"], process_total=result["tracks"],
                            completed=time.strftime("%Y-%m-%d %H:%M"), error="",
                            message=f"SIDFlow ready — {result['tracks']:,} tracks{delete_note}")
    except Exception as exc:
        _diag_event("error", f"SIDFlow import failed: {exc}")
        _sidflow_job_update(running=False, stage="error", error=str(exc),
                            message=f"SIDFlow import failed: {exc}")
    finally:
        _sidflow_cleanup_stale_artifacts()


@app.get("/api/sidflow/status")
def sidflow_status():
    return _sidflow_public_status()


@app.post("/api/sidflow/download")
def sidflow_download():
    global SIDFLOW_THREAD
    with SIDFLOW_LOCK:
        if SIDFLOW_JOB.get("running"):
            return _sidflow_public_status()
        SIDFLOW_JOB.update({
            "running": True, "stage": "starting", "downloaded": 0, "total": 0,
            "processed": 0, "process_total": 0, "message": "Starting SIDFlow download…",
            "error": "", "started": time.monotonic(), "completed": "", "asset": "",
            "release": "",
        })
    SIDFLOW_THREAD = threading.Thread(target=_sidflow_import_worker, daemon=True,
                                      name="sidflow-import")
    SIDFLOW_THREAD.start()
    return _sidflow_public_status()


@app.delete("/api/sidflow")
def sidflow_remove():
    with SIDFLOW_LOCK:
        if SIDFLOW_JOB.get("running"):
            raise HTTPException(409, "SIDFlow data is currently downloading")
    removed = False
    for suffix in ("", "-wal", "-shm"):
        path = SIDFLOW_DB_PATH.with_name(SIDFLOW_DB_PATH.name + suffix)
        try:
            path.unlink()
            removed = True
        except OSError:
            pass
    SIDFLOW_STORE.invalidate()
    return {"removed": removed, **_sidflow_public_status()}



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
    locally, so a device-resident HVSC needs no PC-side copy. The parsed
    lengths are also used to generate a compact per-SID ``.ssl`` attachment
    for the Ultimate's native player.
    """
    SONGLENGTHS.clear()
    SONGLENGTHS_BY_PATH.clear()
    HVSC_INDEX.clear()
    path = CFG.get("songlengths_path") or ""
    if not path:
        return 0
    p = Path(path)
    raw = None
    if p.is_file():
        try:
            raw = p.read_bytes()
        except OSError:
            raw = None
    else:
        cache = ROOT / ".songlengths.cache"
        try:
            raw = devfs.fetch(path)
            try:
                cache.write_bytes(raw)
            except OSError:
                pass
        except Exception:
            if cache.is_file():
                try:
                    raw = cache.read_bytes()
                except OSError:
                    raw = None
    if raw is None:
        return 0
    text = bytes(raw).decode(errors="replace")
    pending_rel = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(";"):
            # "; /MUSICIANS/H/Hubbard_Rob/Sanxion.sid" is immediately
            # followed by that tune's digest/lengths entry. Retain the path
            # association so lazy queue rows can show Length before the SID
            # itself is fetched and hashed.
            rel = line[1:].strip()
            if rel.startswith("/") and rel.lower().endswith(".sid"):
                HVSC_INDEX.append((rel.lower(), rel))
                pending_rel = rel.lstrip("/").replace("\\", "/").casefold()
            else:
                pending_rel = None
            continue
        if line.startswith("["):
            pending_rel = None
            continue
        if "=" in line:
            md5, _, spec = line.partition("=")
            times = _parse_songlength_times(spec)
            if len(md5) == 32 and times:
                SONGLENGTHS[md5.lower()] = times
                if pending_rel:
                    SONGLENGTHS_BY_PATH[pending_rel] = times
            pending_rel = None
    return len(SONGLENGTHS)


def _juke_new_generation() -> int:
    with JUKE_TIMER_LOCK:
        JUKE["generation"] = int(JUKE.get("generation", 0)) + 1
        return JUKE["generation"]


def _juke_reset_similarity_session(*, disable_radio: bool = True) -> None:
    JUKE_PLAYED.clear()
    JUKE_RECENT_TRACKS.clear()
    if disable_radio:
        JUKE["radio"] = False


def _juke_cancel_timer():
    with JUKE_TIMER_LOCK:
        t = JUKE.get("timer")
        if t:
            t.cancel()
            JUKE["timer"] = None


def _juke_disarm_machine_takeover(reason: str = "machine takeover") -> int:
    """Invalidate pending SID callbacks before another action owns the C64.

    Cancelling a ``threading.Timer`` is not enough once its callback has begun.
    The generation bump makes every previously scheduled callback stale, while
    the state reset prevents Radio or stop-after-current from taking the
    machine back after a mount, runner action, reset or reboot has started.
    """
    with JUKE_TIMER_LOCK:
        active = bool(JUKE.get("playing") or JUKE.get("stop_after_current") or
                      JUKE.get("timer") or JUKE.get("radio"))
        _juke_cancel_timer()
        generation = _juke_new_generation()
        JUKE.update({"playing": False, "stop_after_current": False,
                     "loading": False, "radio": False})
    if active:
        _diag_event("info", f"SID Jukebox disarmed for {reason}")
    return generation


def _juke_state():
    now = None
    if 0 <= JUKE["index"] < len(JUKE["items"]):
        it = JUKE["items"][JUKE["index"]]
        now = {"label": it["label"], "meta": it["meta"], "song": JUKE["song"],
               "path": it.get("path", ""), "similarity": it.get("similarity"),
               "length": _juke_length(it, JUKE["song"])}
    sidflow = SIDFLOW_STORE.status()
    return {"items": [{"label": i["label"], "meta": i["meta"],
                       "path": i.get("path", ""),
                       "song": int(i.get("song") or i["meta"].get("start_song", 1) or 1),
                       "similarity": i.get("similarity"),
                       "lazy": i.get("data") is None,
                       "length": _juke_length(i, int(i.get("song") or i["meta"].get("start_song", 1) or 1))}
                      for i in JUKE["items"]],
            "index": JUKE["index"], "playing": JUKE["playing"],
            "shuffle": JUKE["shuffle"], "radio": bool(JUKE.get("radio")),
            "now": now, "folder": JUKE["folder"],
            "loading": bool(JUKE.get("loading")),
            "source": JUKE.get("source", ""),
            "sidflow": {"available": bool(sidflow.get("available")),
                        "tracks": int(sidflow.get("tracks") or 0)},
            "songlengths_loaded": len(SONGLENGTHS)}


def _juke_songlengths(it: dict):
    """Resolve lengths by digest first, then by the HVSC path catalogue.

    Lazy indexed queue entries intentionally have not fetched the full SID and
    therefore may not have a digest yet. Songlengths.md5 also carries the
    canonical HVSC path immediately before each digest, allowing the queue to
    show its duration without loading the tune from the Ultimate.
    """
    meta = it.get("meta") or {}
    digest = str(meta.get("md5") or "").casefold()
    if digest:
        times = SONGLENGTHS.get(digest)
        if times:
            return times
    path = str(it.get("path") or "")
    root = _configured_hvsc_root()
    rel = normalise_hvsc_relative(path, root) if path and root else None
    if rel:
        return SONGLENGTHS_BY_PATH.get(rel.lstrip("/").replace("\\", "/").casefold())
    return None


def _juke_length(it, song: int):
    times = _juke_songlengths(it)
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


def _sidflow_track_context(path: str, song: int) -> tuple[dict | None, str]:
    if not path:
        return None, "local uploads do not have an HVSC path"
    root = _configured_hvsc_root()
    if not root:
        return None, "the HVSC root has not been detected"
    rel = normalise_hvsc_relative(path, root)
    if rel is None:
        return None, "the tune is outside the configured HVSC collection"
    match = SIDFLOW_STORE.lookup(rel, max(1, int(song or 1)))
    if not match:
        return None, "this tune/subsong is not present in the SIDFlow export"
    match["device_path"] = path
    return match, ""


def _sidflow_present_paths() -> dict[str, str]:
    """HVSC-relative lowercase path -> exact device path from the SID index."""
    root = _configured_hvsc_root()
    if not root:
        return {}
    store = _index_store()
    try:
        stat = store.path.stat()
        metadata_count = int(store.sid_metadata_count(root))
        signature = (root.casefold(), stat.st_mtime_ns, stat.st_size, metadata_count)
    except Exception:
        signature = (root.casefold(), 0, 0, 0)
    if SIDFLOW_PRESENT_CACHE.get("signature") == signature:
        return dict(SIDFLOW_PRESENT_CACHE.get("paths") or {})
    paths = []
    getter = getattr(store, "sid_metadata_paths", None)
    if callable(getter):
        try:
            paths = getter(root)
        except Exception:
            paths = []
    if not paths:
        try:
            paths = [row["path"] for row in store.files_below(root, ".sid")]
        except Exception:
            paths = []
    out = {}
    for path in paths:
        rel = normalise_hvsc_relative(path, root)
        if rel:
            out.setdefault(rel.casefold(), path)
    SIDFLOW_PRESENT_CACHE.update({"signature": signature, "paths": dict(out)})
    return out


def _sidflow_item_track_id(item: dict, song: int | None = None) -> str | None:
    path = str(item.get("path") or "")
    if not path:
        return None
    root = _configured_hvsc_root()
    rel = normalise_hvsc_relative(path, root) if root else None
    if rel is None:
        return None
    selected = int(song or item.get("song") or item.get("meta", {}).get("start_song", 1) or 1)
    try:
        match = SIDFLOW_STORE.lookup(rel, selected)
    except Exception:
        match = None
    return str(match.get("track_id")) if match else sidflow_track_id(rel, selected)


def _sidflow_recommendations(path: str, song: int, limit: int = 20) -> tuple[list[dict], dict]:
    status = SIDFLOW_STORE.status()
    if not status.get("available"):
        detail = status.get("error") or "SIDFlow similarity data is not installed"
        raise HTTPException(409, detail)
    if status.get("quality_warning"):
        raise HTTPException(409, status["quality_warning"])
    seed, reason = _sidflow_track_context(path, song)
    if not seed:
        raise HTTPException(404, reason)
    present = _sidflow_present_paths()
    if not present:
        raise HTTPException(409, "the SID index has no mapped HVSC tunes; refresh the SID index first")
    excluded = set(JUKE_PLAYED)
    excluded.update(JUKE_RECENT_TRACKS)
    for item in JUKE.get("items", []):
        track = _sidflow_item_track_id(item)
        if track:
            excluded.add(track)
    ranked = SIDFLOW_STORE.rank(
        seed["track_id"], limit=max(1, min(int(limit or 20), 100)),
        present_paths=set(present), exclude_track_ids=excluded,
    )
    device_paths = [present[row["sid_path"].casefold()] for row in ranked
                    if row["sid_path"].casefold() in present]
    metadata = _sid_metadata_for_paths(_index_store(), device_paths)
    items = []
    for row in ranked:
        device_path = present.get(row["sid_path"].casefold())
        if not device_path:
            continue
        name = device_path.rsplit("/", 1)[-1]
        item = _juke_lazy_item(device_path, name,
                               metadata.get(device_path.casefold()))
        item["song"] = int(row["song_index"])
        item["similarity"] = round(float(row["similarity"]), 4)
        item["sidflow_track_id"] = row["track_id"]
        items.append(item)
    return items, seed


def _sidflow_append(path: str, song: int, limit: int = 20, *, radio: bool = False,
                    insert_after: int | None = None) -> dict:
    items, seed = _sidflow_recommendations(path, song, limit)
    if not items:
        raise HTTPException(404, "SIDFlow found no unseen matching tunes present on this Ultimate")

    # Manual recommendations belong immediately after the tune the user is
    # listening to, not at the bottom of an unrelated composer/folder queue.
    # Radio remains an end-of-queue top-up so it never jumps ahead of explicit
    # choices already waiting to play.
    if radio or insert_after is None:
        insert_at = len(JUKE["items"])
    else:
        insert_at = max(0, min(int(insert_after) + 1, len(JUKE["items"])))
    JUKE["items"][insert_at:insert_at] = items
    JUKE["stop_after_current"] = False
    if JUKE.get("index", -1) >= insert_at:
        JUKE["index"] += len(items)

    if JUKE.get("folder") not in ("SIDFlow Radio", "SIDFlow recommendations"):
        JUKE["folder"] = "SIDFlow Radio" if radio else "SIDFlow recommendations"
    JUKE["source"] = "SIDFlow similarity"
    _juke_new_generation()
    out = _juke_state()
    out.update({"added": len(items), "inserted_at": insert_at,
                "seed_track_id": seed["track_id"],
                "powered_by": "SIDFlow (Chris Gleissner)"})
    return out


def _sidflow_radio_topup() -> int:
    if not JUKE.get("radio") or not JUKE.get("playing"):
        return 0
    index = int(JUKE.get("index", -1))
    if index < 0 or index >= len(JUKE.get("items", [])):
        return 0
    if len(JUKE["items"]) - index > 5:
        return 0
    current = JUKE["items"][index]
    try:
        before = len(JUKE["items"])
        _sidflow_append(str(current.get("path") or ""), int(JUKE.get("song") or 1), 20,
                        radio=True)
        return len(JUKE["items"]) - before
    except HTTPException as exc:
        _diag_event("warning", f"SIDFlow Radio top-up paused: {exc.detail}")
        if index >= len(JUKE["items"]) - 1:
            JUKE["radio"] = False
        return 0



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


def _juke_play(index: int, song: int = 0, *, expected_generation: int | None = None):
    if not JUKE["items"]:
        raise HTTPException(400, "jukebox is empty")
    index = index % len(JUKE["items"])
    play_started = time.monotonic()
    play_timing: dict[str, float | bool] = {}

    # Keep the device operation from lazy materialisation through timer commit.
    # A Mount & Run waiting behind us will disarm the replacement timer; if it
    # acquired first, the generation check exits before any FTP or runner work.
    with DEVICE_OP.operation("interactive", "playing SID"):
        play_timing["coordinator_wait_ms"] = round(
            (time.monotonic() - play_started) * 1000.0, 1
        )
        with JUKE_TIMER_LOCK:
            if (expected_generation is not None and
                    int(JUKE.get("generation", 0)) != int(expected_generation)):
                return _juke_state()
        stage = time.monotonic()
        it = _juke_materialise(JUKE["items"][index])
        play_timing["materialise_ms"] = round(
            (time.monotonic() - stage) * 1000.0, 1
        )
        song = song or int(it.get("song") or it["meta"].get("start_song", 1) or 1)
        try:
            _run_cart_safe(lambda: _post_sid_upload(
                it["label"], it["data"], songnr=song
            ), preserve_jukebox=True, timings=play_timing)
        except (UltimateError, httpx.HTTPError) as e:
            err(e)

        stage = time.monotonic()
        with JUKE_TIMER_LOCK:
            if (expected_generation is not None and
                    int(JUKE.get("generation", 0)) != int(expected_generation)):
                return _juke_state()
            JUKE.update({"index": index, "song": song, "playing": True,
                         "stop_after_current": False})
            track_id = _sidflow_item_track_id(it, song)
            if track_id:
                JUKE_PLAYED.add(track_id.casefold())
                JUKE_RECENT_TRACKS.append(track_id.casefold())
            _sidflow_radio_topup()
            _juke_cancel_timer()
            generation = _juke_new_generation()
            length = _juke_length(it, song)
            if length is None:
                length = float(CFG.get("sid_default_secs", 180) or 0)
            if length > 0:
                t = _threading.Timer(length + 1.0, _juke_auto_next,
                                     args=(generation,))
                t.daemon = True
                JUKE["timer"] = t
                t.start()
        play_timing["state_commit_ms"] = round(
            (time.monotonic() - stage) * 1000.0, 1
        )
        play_timing["total_ms"] = round(
            (time.monotonic() - play_started) * 1000.0, 1
        )
        _diag_event(
            "info",
            "SID Jukebox Play timing: "
            + ", ".join(
                f"{key}={value}"
                for key, value in play_timing.items()
                if key.endswith("_ms")
            ),
        )
        out = _juke_state()
        out["started"] = it["label"]
        out["play_timing"] = play_timing
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


def _juke_auto_next(generation: int | None = None):
    """Advance only if this is still the timer for the active SID session."""
    try:
        with JUKE_TIMER_LOCK:
            current_generation = int(JUKE.get("generation", 0))
            if generation is not None and int(generation) != current_generation:
                return
            stop_after_current = bool(JUKE.get("stop_after_current"))
            should_advance = bool(JUKE.get("playing") and JUKE.get("items"))
            next_index = _juke_next_index() if should_advance else -1

        if stop_after_current:
            # Serialise the timer-triggered reset with Mount & Run and re-check
            # after acquiring the device. Whichever operation acquires first
            # completes; the later one observes the generation change.
            with DEVICE_OP.operation("interactive", "finishing SID playback"):
                with JUKE_TIMER_LOCK:
                    if (generation is not None and
                            int(JUKE.get("generation", 0)) != int(generation)):
                        return
                    JUKE["stop_after_current"] = False
                juke_stop()
        elif should_advance:
            if generation is None:
                _juke_play(next_index)
            else:
                _juke_play(next_index, expected_generation=generation)
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
    _juke_reset_similarity_session()
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
    _juke_reset_similarity_session()
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
    JUKE["stop_after_current"] = False
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


@app.post("/api/juke/clear")
def juke_clear():
    """Clear the pending play queue without abruptly silencing a live SID.

    When a tune is currently playing it remains as the sole queue item and the
    auto-advance timer is cancelled, so the Ultimate finishes that tune and
    then stops naturally. Radio is always disarmed before the queue changes;
    otherwise its top-up worker would immediately refill an empty queue.
    """
    items = JUKE.get("items", [])
    current_index = int(JUKE.get("index", -1))
    keep_current = bool(JUKE.get("playing") and 0 <= current_index < len(items))
    current = items[current_index] if keep_current else None
    removed = len(items) - (1 if keep_current else 0)

    if current is None:
        _juke_cancel_timer()
    _juke_new_generation()
    _juke_reset_similarity_session(disable_radio=True)
    if current is not None:
        JUKE.update({"items": [current], "index": 0, "playing": True,
                     "folder": "Current tune", "loading": False,
                     "stop_after_current": True})
    else:
        JUKE.update({"items": [], "index": -1, "playing": False, "song": 0,
                     "folder": "", "loading": False, "source": "",
                     "stop_after_current": False})
    out = _juke_state()
    out.update({"cleared": removed, "kept_current": keep_current})
    return out


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
    if not keep:
        _juke_reset_similarity_session()
    JUKE.update({"items": items, "index": new_index,
                 "playing": bool(keep and new_index >= 0),
                 "folder": folder, "loading": False, "source": source,
                 "stop_after_current": False})
    out = _juke_state()
    out["skipped"] = 0
    out["lazy"] = True
    return out


@app.post("/api/juke/upload")
async def juke_upload(files: list[UploadFile] = File(...)):
    _juke_cancel_timer()
    _juke_new_generation()
    _juke_reset_similarity_session()
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


def _juke_stop_command_reset() -> tuple[bool, str, str]:
    """Attempt Stop through a newly connected port-64 socket."""
    try:
        if cmd is None:
            return False, "failed", "no command socket available"
        resetter = getattr(cmd, "reset_fresh", None) or getattr(cmd, "reset", None)
        if resetter is None:
            return False, "failed", "command socket reset is unavailable"
        resetter()
        route = "fresh command socket" if hasattr(cmd, "reset_fresh") else "command socket"
        return True, route, ""
    except Exception as exc:
        return False, "failed", str(exc)


def _split_rest_control_active() -> bool:
    """Return True when REST is routed through a paired control address."""
    selected = str(CFG.get("u64_host") or "").strip()
    control = str(getattr(rest, "host", "") or selected).strip()
    saved = str(CFG.get("rest_control_host") or "").strip()
    return bool(
        selected and control and selected != control
        and (saved == control or _same_known_device(selected, control))
    )


def _juke_stop_rest_reset(*, cartridge_safe: bool = False) -> tuple[bool, str]:
    """Reset through REST, optionally parking a configured cartridge first.

    With split routing the REST client is the verified Wi-Fi control path, so
    it avoids the delayed wired command-socket path.  Parking the configured
    cartridge before reset returns the C64 to its normal screen; restoring the
    setting afterwards does not activate the cartridge until a later reset.
    """
    cart = ""
    parked = False
    restore_error = ""
    try:
        if rest is None:
            raise RuntimeError("no REST client available")
        if cartridge_safe and CFG.get("cart_safe_run", True):
            cart = _cart_configured()
            if cart:
                rest.put(
                    f"/v1/configs/{_CART_CAT}/{_CART_ITEM}",
                    value="", request_timeout=4.0,
                )
                parked = True
        rest.put("/v1/machine:reset", request_timeout=4.0)
    except Exception as exc:
        return False, str(exc)
    finally:
        if parked:
            try:
                rest.put(
                    f"/v1/configs/{_CART_CAT}/{_CART_ITEM}",
                    value=cart, request_timeout=4.0,
                )
            except Exception as exc:
                restore_error = str(exc)
                _warn_event(
                    "juke-stop-cart-restore",
                    "SID Jukebox Stop reset succeeded but the configured "
                    f"cartridge could not be restored: {restore_error}",
                )
    return True, ""


def _juke_stop_cached_matrix_available() -> bool:
    """Return the connected device's cached CIA1 capability without probing.

    Stop is an immediate user action.  A missing capability cache therefore
    takes the safe Legacy/REST-first route rather than adding a network probe
    before reset delivery.
    """
    with INPUT_CAP_LOCK:
        status = dict(INPUT_CAPABILITIES.get(_input_cache_key(rest)) or {})
    return bool(status.get("available"))


def _juke_stop_impl():
    stop_started = time.monotonic()
    with JUKE_TIMER_LOCK:
        _juke_cancel_timer()
        _juke_new_generation()
        JUKE["playing"] = False
        JUKE["stop_after_current"] = False

    matrix_capable = _juke_stop_cached_matrix_available()
    split_control = _split_rest_control_active()
    mode = "Split REST" if split_control else ("CIA1" if matrix_capable else "Legacy")
    primary_error = ""

    # Under RC12 split routing, ordinary REST is already using the responsive
    # verified Wi-Fi control address while the command socket remains bound to
    # Ethernet.  Use cartridge-safe REST first so Stop does not inherit the
    # measured ~3-second wired command delay and does not boot the configured
    # fast cartridge.  Single-interface routing keeps the hardware-verified
    # CIA1/Legacy order from Public Beta 17.
    if split_control:
        primary_ok, primary_error = _juke_stop_rest_reset(cartridge_safe=True)
        primary_route = "cartridge-safe REST"
    elif matrix_capable:
        primary_ok, primary_route, primary_error = _juke_stop_command_reset()
    else:
        primary_ok, primary_error = _juke_stop_rest_reset()
        primary_route = "REST"

    try:
        _matrix_release_all(silent=True, cached_only=True, caller="juke-stop")
    except Exception:
        pass

    delivery = primary_route if primary_ok else "failed"
    fallback_error = ""
    if not primary_ok:
        if split_control:
            fallback_ok, fallback_route, fallback_error = _juke_stop_command_reset()
            if fallback_ok:
                delivery = f"{fallback_route} fallback"
        elif matrix_capable:
            fallback_ok, fallback_error = _juke_stop_rest_reset()
            if fallback_ok:
                delivery = "REST fallback"
        else:
            fallback_ok, fallback_route, fallback_error = _juke_stop_command_reset()
            if fallback_ok:
                delivery = f"{fallback_route} fallback"

    elapsed_ms = round((time.monotonic() - stop_started) * 1000.0, 1)
    if delivery == "failed":
        detail = "; ".join(part for part in (primary_error, fallback_error) if part)
        _warn_event(
            "juke-stop-reset",
            f"SID Jukebox Stop reset failed ({mode}): {detail}",
        )
    else:
        failure_note = f"; primary failed: {primary_error}" if primary_error else ""
        _diag_event(
            "info",
            f"SID Jukebox Stop: reset delivered via {delivery} "
            f"({mode}{failure_note}); elapsed="
            f"{elapsed_ms}ms",
        )
    out = _juke_state()
    out["stop_delivery"] = delivery
    out["stop_elapsed_ms"] = elapsed_ms
    out["stop_cartridge_safe"] = bool(split_control and primary_ok)
    return out


@app.put("/api/juke/stop")
def juke_stop():
    return _juke_stop_impl()


@app.put("/api/juke/shuffle")
def juke_shuffle(on: bool = Query(...)):
    JUKE["shuffle"] = bool(on)
    return _juke_state()


@app.post("/api/juke/more_like")
def juke_more_like(payload: dict = Body(...)):
    index = int(payload.get("index", JUKE.get("index", -1)))
    if not (0 <= index < len(JUKE.get("items", []))):
        raise HTTPException(400, "choose a SID from the play queue first")
    item = JUKE["items"][index]
    song = int(payload.get("song") or item.get("song") or
               item.get("meta", {}).get("start_song", 1) or 1)
    current_index = int(JUKE.get("index", -1))
    insert_after = current_index if 0 <= current_index < len(JUKE["items"]) else index
    return _sidflow_append(str(item.get("path") or ""), song,
                           int(payload.get("limit") or 20), radio=False,
                           insert_after=insert_after)


@app.put("/api/juke/radio")
def juke_radio(on: bool = Query(...)):
    enabled = bool(on)
    if enabled:
        status = SIDFLOW_STORE.status()
        if not status.get("available"):
            raise HTTPException(409, "SIDFlow similarity data is not installed")
        JUKE_PLAYED.clear()
        JUKE_RECENT_TRACKS.clear()
        if 0 <= JUKE.get("index", -1) < len(JUKE.get("items", [])):
            current = JUKE["items"][JUKE["index"]]
            track = _sidflow_item_track_id(current, JUKE.get("song") or 1)
            if track:
                JUKE_PLAYED.add(track.casefold())
                JUKE_RECENT_TRACKS.append(track.casefold())
    JUKE["radio"] = enabled
    if enabled:
        JUKE["stop_after_current"] = False
        _sidflow_radio_topup()
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
                _juke_disarm_machine_takeover("launching library disk")
                _matrix_release_all(silent=True, caller="library-disk-run")
                out = rest.mount_attachment(drive, p.name, data, mode=mode)
                _remember_mount(drive, mode, name=p.name)
                rest.put("/v1/machine:reset")
                return out
        if p.name.lower().endswith(".t64"):
            name, prg = _t64_first_prg(data)
            return _run_cart_safe(lambda: rest.run_prg(name + ".prg", prg))
        runner = _runner_for(p.name)
        if runner == "run_crt":
            return _run_direct_takeover(
                lambda: rest.post_file(f"/v1/runners:{runner}", p.name, data),
                "launching library cartridge",
            )
        if runner == "sidplay":
            return _run_cart_safe(lambda: _post_sid_upload(p.name, data))
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
        "input": _input_status(),
        "stream": stream_stats(),
        "index": fs_index_status(),
        "sid_index": juke_index_status(),
        "sidflow": _sidflow_public_status(),
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

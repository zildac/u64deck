"""Build the ephemeral Linux/XDG runtime facade without modifying RC48 core files."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from .build_id import BASE_BUILD, PREVIEW_LABEL, PREVIEW_VERSION, linux_build_id

CORE_MANIFEST = Path(__file__).with_name("core-manifest.sha256")


def xdg_paths() -> dict[str, Path]:
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    data_home = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
    state_home = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    return {
        "config": config_home / "u64deck",
        "data": data_home / "u64deck",
        "cache": cache_home / "u64deck",
        "state": state_home / "u64deck",
    }


def ensure_xdg_paths() -> dict[str, Path]:
    paths = xdg_paths()
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def verify_core(source_root: Path) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if not CORE_MANIFEST.is_file():
        return False, [f"missing manifest: {CORE_MANIFEST}"]
    for raw in CORE_MANIFEST.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        expected, rel = raw.split(None, 1)
        path = source_root / rel.strip()
        if not path.is_file():
            failures.append(f"missing {rel}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            failures.append(f"changed {rel}: {actual}")
    return not failures, failures


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _generated_release(build: str) -> str:
    return f'''"""Generated Linux Preview release metadata; RC48 source remains unchanged."""
VERSION = {PREVIEW_VERSION!r}
RELEASE_LABEL = {PREVIEW_LABEL!r}
BASE_RELEASE_LABEL = "Release Candidate 48"
BASE_BUILD = {BASE_BUILD!r}
BUILD_STAMP_NAME = "u64deck-build-id.txt"

def source_build_id(asset_root, source_root):
    return {build!r}

def read_build_stamp(asset_root):
    return {build!r}

def build_id(asset_root, source_root, *, frozen=None):
    return {build!r}
'''


def _generated_server(source_root: Path, config_dir: Path, data_dir: Path) -> bytes:
    text = (source_root / "server.py").read_text(encoding="utf-8")
    old = 'ROOT = Path(sys.executable).parent if FROZEN else Path(__file__).parent\n'
    new = (
        'ROOT = Path(os.environ["U64DECK_DATA_DIR"]).expanduser().resolve()\n'
        'CONFIG_ROOT = Path(os.environ["U64DECK_CONFIG_DIR"]).expanduser().resolve()\n'
        'ROOT.mkdir(parents=True, exist_ok=True)\n'
        'CONFIG_ROOT.mkdir(parents=True, exist_ok=True)\n'
    )
    if old not in text:
        raise RuntimeError("RC48 ROOT declaration no longer matches the reviewed baseline")
    text = text.replace(old, new, 1)
    text = text.replace('path = ROOT / "config.json"', 'path = CONFIG_ROOT / "config.json"', 1)
    text = text.replace('_write_json_atomic(ROOT / "config.json", CFG, indent=2)',
                        '_write_json_atomic(CONFIG_ROOT / "config.json", CFG, indent=2)', 1)
    text = text.replace(
        '"u64deck": {"version": VERSION, "release_label": RELEASE_LABEL,\n                    "build": BUILD, "frozen": FROZEN},',
        '"u64deck": {"version": VERSION, "release_label": RELEASE_LABEL,\n                    "build": BUILD, "frozen": FROZEN,\n                    "base_release": "Windows v1.9.0 RC48",\n                    "base_build": "c0d1fb0"},',
        1,
    )
    return text.encode("utf-8")


def _copy_static_overlay(source_root: Path, target: Path) -> None:
    source = source_root / "static"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    index = target / "index.html"
    text = index.read_text(encoding="utf-8")
    text = text.replace("<title>u64deck</title>",
                        "<title>u64deck v1.9.0 · Linux Preview 7</title>", 1)
    text = text.replace("U64DECK HELP", "U64DECK LINUX PREVIEW HELP", 1)
    text = text.replace("close its dedicated Edge app window",
                        "close its dedicated Linux app window", 1)
    index.write_text(text, encoding="utf-8")


def prepare_runtime(source_root: Path) -> tuple[Path, dict[str, Path], str]:
    source_root = Path(source_root).resolve()
    ok, failures = verify_core(source_root)
    if not ok:
        raise RuntimeError("RC48 core-integrity check failed:\n  " + "\n  ".join(failures))
    paths = ensure_xdg_paths()
    build = linux_build_id(source_root)
    runtime = paths["cache"] / f"runtime-{build}"
    runtime.mkdir(parents=True, exist_ok=True)

    for module in sorted(source_root.glob("*.py")):
        if module.name in {"server.py", "release.py"}:
            continue
        target = runtime / module.name
        try:
            if target.is_symlink() or target.exists():
                target.unlink()
            target.symlink_to(module)
        except OSError:
            shutil.copy2(module, target)

    _atomic_write(runtime / "server.py",
                  _generated_server(source_root, paths["config"], paths["data"]))
    _atomic_write(runtime / "release.py", _generated_release(build).encode("utf-8"))
    _copy_static_overlay(source_root, runtime / "static")
    return runtime, paths, build

"""Local filesystem scanner for building an Ultimate-style SQLite index.

The browser UI runs on the same PC as u64deck, so a USB stick removed from the
Ultimate can be scanned directly without transferring every directory listing
and disk image through the Ultimate's FTP server.  Paths are translated into
Ultimate form (for example ``E:\\Games`` -> ``/USB0/Games``) before they are
written to the shared IndexStore.
"""

from __future__ import annotations

import os
import shutil
import string
import time
from collections import deque
from pathlib import Path
from typing import Callable

from d64 import DiskImage


IMAGE_EXTENSIONS = (".d64", ".d71", ".d81")
MAX_LOCAL_IMAGE_BYTES = 4 * 1024 * 1024
ROOT_SYSTEM_DIRECTORIES = {"system volume information", "$recycle.bin"}


def normalise_ultimate_root(value: str) -> str:
    """Return a safe absolute Ultimate path for a locally attached volume."""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw.startswith("/"):
        raw = "/" + raw
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("map the local drive to a storage path such as /USB0")
    return "/" + "/".join(parts)


def resolve_source(value: str) -> Path:
    """Resolve and validate the local directory selected for indexing."""
    raw = str(value or "").strip().strip('"')
    if not raw:
        raise ValueError("choose a local USB drive or folder")
    source = Path(raw).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"local path is not available: {raw}") from exc
    if not source.is_dir():
        raise ValueError(f"local path is not a folder: {source}")
    return source


def _display_name(value: str) -> str:
    """Make OS filenames safe for SQLite while preserving legacy bytes."""
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        # POSIX can expose undecodable bytes as surrogate escapes.  Latin-1 is
        # the same byte-preserving fallback used by the Ultimate FTP client.
        return value.encode("utf-8", "surrogateescape").decode("latin-1")


def ftp_mtime(timestamp: float) -> str:
    """Format local mtimes like an FTP MLSD ``modify`` fact (UTC)."""
    try:
        return time.strftime("%Y%m%d%H%M%S", time.gmtime(float(timestamp)))
    except (OverflowError, OSError, ValueError):
        return ""


def _join_ultimate(parent: str, name: str) -> str:
    return parent.rstrip("/") + "/" + name


def _windows_volume_info(root: str) -> dict:
    info = {"label": "", "serial": "", "filesystem": ""}
    try:
        import ctypes
        from ctypes import wintypes

        label = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        max_component = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root,
            label,
            len(label),
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            filesystem,
            len(filesystem),
        )
        if ok:
            info.update({
                "label": label.value,
                "serial": f"{serial.value:08X}",
                "filesystem": filesystem.value,
            })
    except Exception:
        pass
    return info


def list_local_volumes() -> list[dict]:
    """List likely local-volume roots available to the u64deck process."""
    volumes: list[dict] = []
    if os.name == "nt":
        try:
            import ctypes

            mask = int(ctypes.windll.kernel32.GetLogicalDrives())
            drive_types = {
                0: "unknown",
                1: "invalid",
                2: "removable",
                3: "fixed",
                4: "network",
                5: "optical",
                6: "ramdisk",
            }
            for idx, letter in enumerate(string.ascii_uppercase):
                if not mask & (1 << idx):
                    continue
                root = f"{letter}:\\"
                dtype_num = int(ctypes.windll.kernel32.GetDriveTypeW(root))
                dtype = drive_types.get(dtype_num, "unknown")
                if dtype in {"invalid", "optical"} or not os.path.isdir(root):
                    continue
                extra = _windows_volume_info(root)
                try:
                    usage = shutil.disk_usage(root)
                    total, free = int(usage.total), int(usage.free)
                except OSError:
                    total = free = 0
                volumes.append({
                    "path": root,
                    "type": dtype,
                    "removable": dtype == "removable",
                    "label": extra["label"],
                    "serial": extra["serial"],
                    "filesystem": extra["filesystem"],
                    "total_bytes": total,
                    "free_bytes": free,
                })
        except Exception:
            pass
    else:
        candidates: list[Path] = []
        for base in (Path("/media"), Path("/run/media"), Path("/mnt"), Path("/Volumes")):
            if not base.is_dir():
                continue
            try:
                children = list(base.iterdir())
            except OSError:
                continue
            # /media and /run/media commonly contain a username level.
            for child in children:
                if child.is_dir():
                    candidates.append(child)
                    if base.name in {"media"}:
                        try:
                            candidates.extend(p for p in child.iterdir() if p.is_dir())
                        except OSError:
                            pass
        seen = set()
        for root in candidates:
            try:
                resolved = root.resolve()
            except OSError:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            try:
                usage = shutil.disk_usage(resolved)
                total, free = int(usage.total), int(usage.free)
            except OSError:
                total = free = 0
            volumes.append({
                "path": key,
                "type": "mounted",
                "removable": True,
                "label": resolved.name,
                "serial": "",
                "filesystem": "",
                "total_bytes": total,
                "free_bytes": free,
            })
    volumes.sort(key=lambda item: (not item.get("removable", False), item["path"].lower()))
    return volumes


def volume_identity(source: Path) -> str:
    """Return a stable-enough identifier for reconciliation diagnostics."""
    if os.name == "nt":
        anchor = source.anchor or str(source)
        info = _windows_volume_info(anchor)
        bits = [info.get("serial", ""), info.get("label", ""), info.get("filesystem", "")]
        return "|".join(str(bit) for bit in bits if bit)
    try:
        st = source.stat()
        return f"dev:{st.st_dev}"
    except OSError:
        return ""


def scan_local_tree(
    source: Path,
    ultimate_root: str,
    *,
    image_is_cached: Callable[[str, int, str], bool],
    commit_batch: Callable[[list[tuple[str, list[dict]]], list[dict], list[str]], None],
    stop_check: Callable[[], bool],
    pause_wait: Callable[[], bool],
    progress: Callable[[dict], None],
    batch_directories: int = 100,
) -> dict:
    """Scan ``source`` and feed batched Ultimate-style entries to SQLite.

    ``pause_wait`` returns False when the job should stop.  Errors affecting an
    individual file/folder are counted and skipped so one Windows metadata
    directory or damaged image cannot abort a large import.
    """
    source = resolve_source(str(source))
    ultimate_root = normalise_ultimate_root(ultimate_root)
    pending = deque([(source, ultimate_root)])
    directory_batch: list[tuple[str, list[dict]]] = []
    image_batch: list[dict] = []
    cached_image_paths: list[str] = []
    summary = {
        "dirs": 0,
        "files": 0,
        "images": 0,
        "images_cached": 0,
        "bytes_read": 0,
        "errors": 0,
        "error_samples": [],
        "pending_dirs": 1,
        "current": ultimate_root,
    }

    def note_error(path: object, exc: BaseException) -> None:
        summary["errors"] += 1
        if len(summary["error_samples"]) < 12:
            summary["error_samples"].append(f"{path}: {exc}")

    def flush() -> None:
        nonlocal directory_batch, image_batch, cached_image_paths
        if not directory_batch and not image_batch and not cached_image_paths:
            return
        commit_batch(directory_batch, image_batch, cached_image_paths)
        directory_batch = []
        image_batch = []
        cached_image_paths = []

    while pending and not stop_check():
        if not pause_wait() or stop_check():
            break
        local_dir, device_dir = pending.popleft()
        summary["current"] = device_dir
        summary["pending_dirs"] = len(pending)
        try:
            scandir_entries = list(os.scandir(local_dir))
        except OSError as exc:
            # Never publish an empty/pruned index when the selected drive was
            # removed or cannot be opened.  Child folders such as Windows'
            # System Volume Information may legitimately be inaccessible and
            # are skipped without aborting the rest of the stick.
            if local_dir == source or not source.exists():
                raise RuntimeError(f"local source became unavailable: {source}") from exc
            note_error(local_dir, exc)
            progress(dict(summary))
            continue

        db_entries: list[dict] = []
        child_dirs: list[tuple[Path, str]] = []
        for entry in scandir_entries:
            if stop_check():
                break
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                stat_result = entry.stat(follow_symlinks=False)
            except OSError as exc:
                note_error(entry.path, exc)
                continue

            name = _display_name(entry.name)
            if is_dir and local_dir == source and name.casefold() in ROOT_SYSTEM_DIRECTORIES:
                continue
            mtime = ftp_mtime(stat_result.st_mtime)
            size = 0 if is_dir else int(stat_result.st_size)
            db_entries.append({
                "name": name,
                "dir": is_dir,
                "size": size,
                "mtime": mtime,
            })
            device_path = _join_ultimate(device_dir, name)
            if is_dir:
                child_dirs.append((Path(entry.path), device_path))
                continue

            summary["files"] += 1
            if not name.lower().endswith(IMAGE_EXTENSIONS):
                continue
            if size > MAX_LOCAL_IMAGE_BYTES:
                note_error(entry.path, ValueError(
                    f"image is larger than {MAX_LOCAL_IMAGE_BYTES // (1024 * 1024)} MiB"
                ))
                image_batch.append({
                    "path": device_path,
                    "size": size,
                    "mtime": mtime,
                    "entries": [],
                    "parse_ok": False,
                    "parse_error": f"image is larger than {MAX_LOCAL_IMAGE_BYTES // (1024 * 1024)} MiB",
                })
                summary["images"] += 1
                continue
            if image_is_cached(device_path, size, mtime):
                cached_image_paths.append(device_path)
                summary["images_cached"] += 1
                continue
            if not pause_wait() or stop_check():
                break
            parse_error = ""
            try:
                data = Path(entry.path).read_bytes()
                summary["bytes_read"] += len(data)
                image = DiskImage(data, name_hint=name)
                image_entries = [
                    {"name": item.name, "file_type": item.file_type, "blocks": item.blocks}
                    for item in image.entries
                ]
                parse_ok = True
            except Exception as exc:
                note_error(entry.path, exc)
                image_entries = []
                parse_ok = False
                parse_error = str(exc)
            image_batch.append({
                "path": device_path,
                "size": size,
                "mtime": mtime,
                "entries": image_entries,
                "parse_ok": parse_ok,
                "parse_error": parse_error,
            })
            summary["images"] += 1

        db_entries.sort(key=lambda item: (not item["dir"], item["name"].lower()))
        directory_batch.append((device_dir, db_entries))
        pending.extend(child_dirs)
        summary["dirs"] += 1
        summary["pending_dirs"] = len(pending)

        if len(directory_batch) >= max(1, int(batch_directories)) or len(image_batch) >= 100:
            flush()
        progress(dict(summary))

    flush()
    summary["stopped"] = bool(stop_check())
    summary["pending_dirs"] = len(pending)
    progress(dict(summary))
    return summary

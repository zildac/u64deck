"""SID header metadata scanners used by the u64deck jukebox index.

The local scanner mirrors the storage indexer's path mapping: a PC-side HVSC
folder is mapped to the path it has when attached to the Ultimate. Only the
small PSID/RSID header is read; tune data is never modified or copied.
"""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path
from typing import Callable

from local_indexer import (_display_name, ftp_mtime, normalise_ultimate_root,
                           resolve_source)


SID_HEADER_BYTES = 0x7C
ROOT_SYSTEM_DIRECTORIES = {"system volume information", "$recycle.bin"}


def _join_ultimate(parent: str, name: str) -> str:
    return parent.rstrip("/") + "/" + name


def scan_local_sid_tree(
    source: Path,
    ultimate_root: str,
    *,
    parse_sid: Callable[[bytes], dict],
    is_cached: Callable[[str, int, str], bool],
    commit_batch: Callable[[list[dict], list[str]], None],
    stop_check: Callable[[], bool],
    pause_wait: Callable[[], bool],
    progress: Callable[[dict], None],
    force: bool = False,
    batch_size: int = 250,
) -> dict:
    """Scan a local HVSC tree and commit SID metadata in bounded batches."""
    source = resolve_source(str(source))
    ultimate_root = normalise_ultimate_root(ultimate_root)
    pending = deque([(source, ultimate_root)])
    rows: list[dict] = []
    seen: list[str] = []
    summary = {
        "dirs": 0,
        "files": 0,
        "parsed": 0,
        "cached": 0,
        "errors": 0,
        "error_samples": [],
        "bytes_read": 0,
        "pending_dirs": 1,
        "current": ultimate_root,
    }

    def note_error(path: object, exc: BaseException) -> None:
        summary["errors"] += 1
        if len(summary["error_samples"]) < 12:
            summary["error_samples"].append(f"{path}: {exc}")

    def flush() -> None:
        nonlocal rows, seen
        if rows or seen:
            commit_batch(rows, seen)
            rows = []
            seen = []

    while pending and not stop_check():
        if not pause_wait():
            break
        local_dir, device_dir = pending.popleft()
        summary["current"] = device_dir
        summary["pending_dirs"] = len(pending)
        progress(dict(summary))
        try:
            entries = list(os.scandir(local_dir))
        except OSError as exc:
            note_error(local_dir, exc)
            continue
        summary["dirs"] += 1
        entries.sort(key=lambda e: e.name.lower())
        for entry in entries:
            if stop_check():
                break
            name = _display_name(entry.name)
            if entry.is_dir(follow_symlinks=False):
                if (local_dir == source and
                        name.casefold() in ROOT_SYSTEM_DIRECTORIES):
                    continue
                pending.append((Path(entry.path), _join_ultimate(device_dir, name)))
                continue
            if not name.lower().endswith(".sid"):
                continue
            summary["files"] += 1
            device_path = _join_ultimate(device_dir, name)
            try:
                stat = entry.stat(follow_symlinks=False)
                size = int(stat.st_size)
                mtime = ftp_mtime(stat.st_mtime)
            except OSError as exc:
                note_error(entry.path, exc)
                continue
            seen.append(device_path)
            if not force and is_cached(device_path, size, mtime):
                summary["cached"] += 1
            else:
                try:
                    with open(entry.path, "rb") as handle:
                        header = handle.read(SID_HEADER_BYTES)
                    summary["bytes_read"] += len(header)
                    meta = parse_sid(header)
                    if not meta:
                        raise ValueError("not a valid PSID/RSID header")
                    rows.append({
                        "path": device_path,
                        "size": size,
                        "mtime": mtime,
                        "meta": meta,
                        "source": "local HVSC",
                    })
                    summary["parsed"] += 1
                except (OSError, ValueError) as exc:
                    note_error(entry.path, exc)
            if len(rows) + len(seen) >= max(25, int(batch_size)):
                flush()
                progress(dict(summary))
        summary["pending_dirs"] = len(pending)
        progress(dict(summary))

    flush()
    summary["pending_dirs"] = 0
    progress(dict(summary))
    return summary

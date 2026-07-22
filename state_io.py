"""Small, thread-safe helpers for u64deck JSON state files."""

from __future__ import annotations

import copy
import json
import os
import stat
import threading
import time
import uuid
from pathlib import Path

_JSON_WRITE_LOCK = threading.RLock()
_WARNING_LOCK = threading.Lock()
_WARNING_LAST: dict[str, float] = {}


def read_json(path: Path, default):
    """Read JSON without allowing one damaged state file to kill startup."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"  warning: ignoring unreadable {path.name}: {exc}")
        return copy.deepcopy(default)


def warn_throttled(key: str, message: str, interval: float = 30.0):
    """Print a recurring warning at most once per interval."""
    now = time.monotonic()
    with _WARNING_LOCK:
        if now - _WARNING_LAST.get(key, -interval) < interval:
            return
        _WARNING_LAST[key] = now
    print(f"  warning: {message}")


def write_json_atomic(path: Path, value, *, indent=None):
    """Persist JSON safely, with a Windows-compatible replacement fallback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, indent=indent)

    with _JSON_WRITE_LOCK:
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass

            last_error = None
            for delay in (0.0, 0.03, 0.10, 0.25):
                if delay:
                    time.sleep(delay)
                try:
                    os.replace(temp, path)
                    last_error = None
                    break
                except PermissionError as exc:
                    last_error = exc
                    if path.exists():
                        try:
                            path.chmod(stat.S_IREAD | stat.S_IWRITE)
                        except OSError:
                            pass

            if last_error is not None:
                try:
                    with path.open("w", encoding="utf-8", newline="\n") as handle:
                        handle.write(payload)
                        handle.flush()
                        try:
                            os.fsync(handle.fileno())
                        except OSError:
                            pass
                except OSError:
                    raise last_error

            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

"""Persistent favourites and recently-used items for u64deck."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from state_io import read_json, write_json_atomic

MAX_RECENTS = 60
MAX_FAVOURITES = 250


class UserItemsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, list[dict]]:
        raw = read_json(self.path, {})
        if not isinstance(raw, dict):
            raw = {}
        favs = raw.get("favorites") if isinstance(raw.get("favorites"), list) else []
        recents = raw.get("recents") if isinstance(raw.get("recents"), list) else []
        return {
            "favorites": [x for x in favs if isinstance(x, dict)][:MAX_FAVOURITES],
            "recents": [x for x in recents if isinstance(x, dict)][:MAX_RECENTS],
        }

    @staticmethod
    def _safe_text(value: Any, limit: int) -> str:
        text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        return text[:limit]

    @classmethod
    def _normalise_payload(cls, value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return None
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:2048]
        if isinstance(value, list):
            return [cls._normalise_payload(v, depth + 1) for v in value[:40]]
        if isinstance(value, dict):
            out = {}
            for key, val in list(value.items())[:40]:
                out[cls._safe_text(key, 80)] = cls._normalise_payload(val, depth + 1)
            return out
        return cls._safe_text(value, 512)

    @classmethod
    def normalise_item(cls, item: dict) -> dict:
        if not isinstance(item, dict):
            raise ValueError("item must be an object")
        kind = cls._safe_text(item.get("type"), 40)
        label = cls._safe_text(item.get("label"), 240)
        action = cls._safe_text(item.get("action"), 80)
        if not kind or not label or not action:
            raise ValueError("item requires type, label and action")
        payload = cls._normalise_payload(item.get("payload") or {})
        detail = cls._safe_text(item.get("detail"), 500)
        canonical = json.dumps(
            {"type": kind, "action": action, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        item_id = hashlib.sha1(canonical.encode("utf-8", "surrogatepass")).hexdigest()[:16]
        return {
            "id": item_id,
            "type": kind,
            "label": label,
            "detail": detail,
            "action": action,
            "payload": payload,
        }

    def _save_locked(self) -> None:
        write_json_atomic(self.path, self._data, indent=2)

    def snapshot(self) -> dict[str, list[dict]]:
        with self._lock:
            return {
                "favorites": [dict(x) for x in self._data["favorites"]],
                "recents": [dict(x) for x in self._data["recents"]],
            }

    def favourite(self, item: dict) -> dict:
        clean = self.normalise_item(item)
        now = time.time()
        with self._lock:
            existing = next((x for x in self._data["favorites"] if x.get("id") == clean["id"]), None)
            if existing:
                existing.update(clean)
                existing["added_at"] = existing.get("added_at", now)
            else:
                clean["added_at"] = now
                self._data["favorites"].insert(0, clean)
                self._data["favorites"] = self._data["favorites"][:MAX_FAVOURITES]
            self._save_locked()
        return clean

    def unfavourite(self, item_id: str) -> bool:
        item_id = self._safe_text(item_id, 64)
        with self._lock:
            before = len(self._data["favorites"])
            self._data["favorites"] = [x for x in self._data["favorites"] if x.get("id") != item_id]
            changed = len(self._data["favorites"]) != before
            if changed:
                self._save_locked()
        return changed

    def recent(self, item: dict) -> dict:
        clean = self.normalise_item(item)
        clean["used_at"] = time.time()
        with self._lock:
            self._data["recents"] = [x for x in self._data["recents"] if x.get("id") != clean["id"]]
            self._data["recents"].insert(0, clean)
            self._data["recents"] = self._data["recents"][:MAX_RECENTS]
            self._save_locked()
        return clean

    def clear_recents(self) -> int:
        with self._lock:
            count = len(self._data["recents"])
            self._data["recents"] = []
            self._save_locked()
        return count

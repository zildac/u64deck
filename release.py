"""Release metadata shared by the server, UI API and packaging checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

VERSION = "1.9.0"
RELEASE_LABEL = "Release Candidate 16"


def build_id(asset_root: Path, source_root: Path) -> str:
    """Return a short fingerprint of all shipped Python modules and the UI.

    Hashing every top-level module means a maintenance refactor changes the
    build identifier automatically without another hand-maintained file list.
    """
    digest = hashlib.sha1()
    static_root = asset_root / "static"
    files = sorted(static_root.glob("*"), key=lambda p: p.name.lower())
    files.extend(sorted(source_root.glob("*.py"), key=lambda p: p.name.lower()))
    for path in files:
        try:
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:7]

"""Release metadata shared by the server, UI API and packaging checks."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

VERSION = "1.9.0"
RELEASE_LABEL = "Release Candidate 45"
BUILD_STAMP_NAME = "u64deck-build-id.txt"
_BUILD_ID_RE = re.compile(r"^[0-9a-f]{7}$")


def source_build_id(asset_root: Path, source_root: Path) -> str:
    """Return the source-tree fingerprint used for release identity.

    Every shipped top-level Python module and UI asset participates in the
    fingerprint. Repository metadata, documentation and generated build output
    are intentionally excluded so the value is stable across LF-normalised
    source checkouts and Windows packaging.
    """
    digest = hashlib.sha1()
    static_root = Path(asset_root) / "static"
    files = sorted(static_root.glob("*"), key=lambda p: p.name.lower())
    files.extend(sorted(Path(source_root).glob("*.py"), key=lambda p: p.name.lower()))
    for path in files:
        try:
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:7]


def read_build_stamp(asset_root: Path) -> str:
    """Read and validate the packaging-time build stamp from a frozen bundle."""
    stamp_path = Path(asset_root) / BUILD_STAMP_NAME
    try:
        value = stamp_path.read_text(encoding="ascii").strip().lower()
    except OSError as exc:
        raise RuntimeError(f"Frozen build stamp is missing: {stamp_path}") from exc
    if not _BUILD_ID_RE.fullmatch(value):
        raise RuntimeError(f"Frozen build stamp is invalid: {value!r}")
    return value


def build_id(asset_root: Path, source_root: Path, *, frozen: bool | None = None) -> str:
    """Return the documented source build ID or the frozen packaging stamp.

    PyInstaller bundles do not contain the original top-level ``*.py`` files,
    so recomputing the source fingerprint at runtime would produce a different
    identity. The spec therefore writes the source fingerprint into the bundle
    and frozen runs consume that exact stamp.
    """
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if is_frozen:
        return read_build_stamp(asset_root)
    return source_build_id(asset_root, source_root)

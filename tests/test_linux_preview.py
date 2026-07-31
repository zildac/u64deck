from __future__ import annotations

import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_linux_identity_is_separate_from_windows_rc44():
    from linux.build_id import BASE_BUILD, PREVIEW_LABEL, identity, linux_build_id
    build = linux_build_id(ROOT)
    assert PREVIEW_LABEL == "Linux Preview 4"
    assert BASE_BUILD == "fc1e0fb"
    assert len(build) == 7 and all(c in "0123456789abcdef" for c in build)
    assert identity(ROOT) == f"u64deck v1.9.0 · Linux Preview 4 · build {build}"


def test_rc44_core_manifest_matches_reviewed_files():
    from linux.runtime import verify_core
    ok, failures = verify_core(ROOT)
    assert ok, failures


def test_xdg_paths_are_per_user(monkeypatch, tmp_path):
    from linux.runtime import xdg_paths
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    paths = xdg_paths()
    assert paths["config"] == tmp_path / "config/u64deck"
    assert paths["data"] == tmp_path / "data/u64deck"
    assert paths["cache"] == tmp_path / "cache/u64deck"
    assert paths["state"] == tmp_path / "state/u64deck"


def test_generated_runtime_redirects_config_and_data(monkeypatch, tmp_path):
    from linux.runtime import prepare_runtime
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runtime, paths, build = prepare_runtime(ROOT)
    server = (runtime / "server.py").read_text(encoding="utf-8")
    release = (runtime / "release.py").read_text(encoding="utf-8")
    assert 'U64DECK_DATA_DIR' in server
    assert 'CONFIG_ROOT / "config.json"' in server
    assert "Linux Preview 4" in release and build in release
    assert paths["config"].is_dir() and paths["data"].is_dir()


def test_generated_static_overlay_is_linux_specific(monkeypatch, tmp_path):
    from linux.runtime import prepare_runtime
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runtime, _, _ = prepare_runtime(ROOT)
    html = (runtime / "static/index.html").read_text(encoding="utf-8")
    assert "<title>u64deck v1.9.0 · Linux Preview 4</title>" in html
    assert "U64DECK LINUX PREVIEW HELP" in html
    assert "dedicated Linux app window" in html
    assert (ROOT / "static/index.html").read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert "<title>u64deck</title>" in (ROOT / "static/index.html").read_text(encoding="utf-8")


def test_linux_scripts_are_executable_and_strict():
    for name in ("install.sh", "u64deck.sh", "update-linux.sh",
                 "uninstall-linux.sh", "import-existing-data.sh"):
        path = ROOT / name
        assert path.stat().st_mode & stat.S_IXUSR
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")
        subprocess.run(["bash", "-n", str(path)], check=True)
    launcher = (ROOT / "u64deck.sh").read_text(encoding="utf-8")
    assert 'exec "$PYTHON"' in launcher
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'Exec="$SCRIPT_DIR/u64deck.sh"' in installer
    entry = (ROOT / "linux/entry.py").read_text(encoding="utf-8")
    assert 'startswith("/snap/")' in entry


def test_readme_explicitly_documents_windows_and_linux_storage():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (ROOT / "LINUX-PREVIEW.md").read_text(encoding="utf-8")
    assert "`config.json` beside `u64deck.exe`" in readme
    assert "${XDG_CONFIG_HOME:-~/.config}/u64deck/config.json" in readme
    assert "${XDG_DATA_HOME:-~/.local/share}/u64deck/.u64deck-index.sqlite3" in readme
    assert "./import-existing-data.sh /path/to/old/u64deck" in readme
    assert '"boot_prekey": "F7"' in guide


def test_importer_skips_wal_shm_and_preserves_source():
    text = (ROOT / "import-existing-data.sh").read_text(encoding="utf-8")
    assert "*-wal" in text and "*-shm" in text
    assert "Source files were left untouched" in text
    assert "Windows paths" in text
    assert "import-backups" in text


def test_packager_excludes_runtime_and_staging_paths(tmp_path):
    from linux.package_release import build_tarball
    out = tmp_path / "preview.tar.gz"
    digest, manifest = build_tarball(ROOT, out)
    assert len(digest) == 64
    names = "\n".join(manifest)
    for forbidden in ("/.venv/", "/config.json", "/.u64deck-index.sqlite3",
                      "/.sidflow-similarity.sqlite", "/artifacts-preliminary/",
                      "/__pycache__/"):
        assert forbidden not in names
    with tarfile.open(out, "r:gz") as tf:
        modes = {m.name: stat.S_IMODE(m.mode) for m in tf.getmembers() if m.isfile()}
    assert modes["u64deck/install.sh"] == 0o755
    assert modes["u64deck/u64deck.sh"] == 0o755


def test_no_personal_paths_or_identifiers_in_release_sources():
    import re
    patterns = (
        re.compile(r"/home/[A-Za-z0-9._-]+/"),
        re.compile(r"C:\\Users\\[A-Za-z0-9._-]+", re.I),
    )
    forbidden_host = "NUC-" + "1"
    allowed_suffixes = {".py", ".sh", ".md", ".json", ".yml", ".yaml", ".txt"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".venv" in path.parts or path.suffix.lower() not in allowed_suffixes:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert forbidden_host not in text, path
        assert not any(pattern.search(text) for pattern in patterns), path

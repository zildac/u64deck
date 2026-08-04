from __future__ import annotations

import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

import sys

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux preview tests run on Linux only",
)

ROOT = Path(__file__).resolve().parents[1]

def test_linux_identity_is_separate_from_windows_rc51():
    from linux.build_id import BASE_BUILD, PREVIEW_LABEL, identity, linux_build_id
    build = linux_build_id(ROOT)
    assert PREVIEW_LABEL == "Linux Preview 9"
    assert BASE_BUILD == "ea5a1b6"
    assert len(build) == 7 and all(c in "0123456789abcdef" for c in build)
    assert identity(ROOT) == f"u64deck v1.9.0 · Linux Preview 9 · build {build}"

def test_rc51_core_manifest_matches_reviewed_files():
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
    assert "Linux Preview 9" in release and build in release
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
    assert "<title>u64deck v1.9.0 · Linux Preview 9</title>" in html
    assert "U64DECK LINUX PREVIEW HELP" in html
    assert "dedicated Linux app window" in html
    assert (ROOT / "static/index.html").read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert "<title>u64deck v1.9.0 · Release Candidate 51</title>" in (ROOT / "static/index.html").read_text(encoding="utf-8")

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

def _run_importer(tmp_path: Path, config_text: str, extra_files: dict[str, bytes] | None = None):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.json").write_text(config_text, encoding="utf-8")
    for name, data in (extra_files or {}).items():
        (source / name).write_bytes(data)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
    })
    result = subprocess.run(
        [str(ROOT / "import-existing-data.sh"), str(source)],
        text=True, capture_output=True, env=env, check=True,
    )
    return result, Path(env["XDG_DATA_HOME"]) / "u64deck"


def test_importer_does_not_treat_url_scheme_as_windows_drive_path(tmp_path):
    result, _ = _run_importer(
        tmp_path,
        '{"assembly64_url":"http://hackerswithstyle.se/leet"}\n',
    )
    assert "Warning: imported JSON contains Windows paths" not in result.stdout


def test_importer_still_warns_for_real_windows_drive_paths(tmp_path):
    result, _ = _run_importer(
        tmp_path,
        '{"hvsc_path":"D:\\\\HVSC","other":"C:\\\\Users\\\\Example\\\\Files"}\n',
    )
    assert "Warning: imported JSON contains Windows paths" in result.stdout


def test_importer_rejects_live_sqlite_sidecars_from_migration(tmp_path):
    result, data_dir = _run_importer(
        tmp_path,
        '{}\n',
        {
            ".u64deck-index.sqlite3": b"database",
            ".u64deck-index.sqlite3-wal": b"live-wal",
            ".u64deck-index.sqlite3-shm": b"live-shm",
        },
    )
    assert (data_dir / ".u64deck-index.sqlite3").read_bytes() == b"database"
    assert not (data_dir / ".u64deck-index.sqlite3-wal").exists()
    assert not (data_dir / ".u64deck-index.sqlite3-shm").exists()
    assert "Source files were left untouched" in result.stdout


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


def test_preview9_manifest_excludes_windows_only_files_and_has_lf_coverage():
    manifest = (ROOT / "linux/core-manifest.sha256").read_text(encoding="utf-8")
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "Windows RC51 shared application core" in manifest
    for rel in (".github/workflows/build-exe.yml", "u64deck.spec", "start.bat"):
        assert rel not in manifest
    assert "*.sh   text eol=lf" in attrs
    assert "*.sha256 text eol=lf" in attrs
    assert "*.txt  text eol=lf" in attrs


def test_preview9_release_workflows_use_private_artifact_names():
    ci = (ROOT / ".github/workflows/linux-ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/linux-release.yml").read_text(encoding="utf-8")
    assert "Linux Preview 9" in ci
    assert "u64deck-v1.9.0-linux-preview.9-private.tar.gz" in release
    assert "private" in release.lower()


def test_preview9_packager_preserves_every_shell_executable(tmp_path):
    from linux.package_release import build_tarball
    out = tmp_path / "preview9.tar.gz"
    _, _ = build_tarball(ROOT, out)
    with tarfile.open(out, "r:gz") as tf:
        modes = {m.name: stat.S_IMODE(m.mode) for m in tf.getmembers() if m.isfile()}
    for name in ("install.sh", "u64deck.sh", "update-linux.sh",
                 "uninstall-linux.sh", "import-existing-data.sh"):
        assert modes[f"u64deck/{name}"] == 0o755


def test_preview9_assembly64_final_clause_matches_support_copy_font_size():
    css = (ROOT / "static/app.css").read_text(encoding="utf-8")
    assert ".asm-support-copy span{color:var(--txt);font-size:14px;line-height:1.5" in css
    assert ".asm-support-copy small{color:var(--dim);font-size:14px;line-height:1.5" in css


def test_preview9_library_and_desktop_icon_contract():
    canonical = (
        "Drop .crt/.prg/.t64/.sid/.mod/.d64/.d71/.d81/.g64/.dnp files here.\n"
        "Each appears as a Quick Launch button in u64deck's SCREEN tab.\n"
    ).encode("utf-8")
    library = ROOT / "library"
    assert sorted(p.name for p in library.iterdir()) == ["README.txt"]
    assert (library / "README.txt").read_bytes() == canonical
    assert (ROOT / "u64deck-icon-256.png").is_file()
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "Icon=$SCRIPT_DIR/u64deck-icon-256.png" in installer

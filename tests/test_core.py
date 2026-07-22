import asyncio
import io
import json
import re
import time
import threading
from pathlib import Path
from contextlib import contextmanager

import pytest
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

import server
import ultimate
from d64 import DiskImage
from index_store import IndexStore
from sid_indexer import scan_local_sid_tree


def test_odd_sized_image_uses_extension_hint():
    image = DiskImage(bytes(174849), name_hint="odd-sized.d64")
    assert image.geo.kind == "d64"
    assert image.geo.tracks == 35


def test_atomic_json_round_trip(tmp_path: Path):
    target = tmp_path / "state.json"
    server._write_json_atomic(target, {"ok": True, "items": [1, 2]}, indent=2)
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "ok": True,
        "items": [1, 2],
    }
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_json_falls_back_when_replace_is_denied(tmp_path: Path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"old": true}', encoding="utf-8")

    def denied(_source, _target):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(server.os, "replace", denied)
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)

    server._write_json_atomic(target, {"new": True}, indent=2)
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
    assert not list(tmp_path.glob("*.tmp"))



def test_sqlite_directory_update_merges_duplicate_paths(tmp_path: Path):
    store = IndexStore(tmp_path / "index.sqlite3")
    try:
        store.put_directory("/", [
            {"name": "USB0", "dir": True, "size": 0, "mtime": ""},
            {"name": "USB0", "dir": True, "size": 0, "mtime": "20260720180000"},
        ])
        entries = store.get_directory("/")
        assert entries == [{
            "name": "USB0",
            "dir": True,
            "size": 0,
            "mtime": "20260720180000",
        }]
        assert store.stats()["file_entries"] == 1
    finally:
        store.close()


def test_legacy_cache_import_tolerates_duplicate_paths(tmp_path: Path):
    dir_cache = tmp_path / ".dircache.json"
    image_cache = tmp_path / ".imagecache.json"
    index_meta = tmp_path / ".indexmeta.json"
    dir_cache.write_text(json.dumps({
        "/": [
            {"name": "USB0", "dir": True, "size": 0},
            {"name": "USB0", "dir": True, "size": 0},
        ],
        "/USB0": [
            {"name": "Games", "dir": True, "size": 0},
            {"name": "Games", "dir": True, "size": 0},
        ],
    }), encoding="utf-8")
    image_cache.write_text("{}", encoding="utf-8")
    index_meta.write_text("{}", encoding="utf-8")

    store = IndexStore(tmp_path / "index.sqlite3")
    try:
        result = store.import_legacy(dir_cache, image_cache, index_meta)
        assert result["imported"] is True
        assert result["directories"] == 2
        assert store.stats()["file_entries"] == 2
        assert store.metadata_get("legacy_import_complete") == "1"
    finally:
        store.close()

def test_ftp_listing_falls_back_for_invalid_utf8_filename(monkeypatch):
    connections = []
    transfers = []

    class FakeFTP:
        def __init__(self, *args, encoding="utf-8", **kwargs):
            self.encoding = encoding
            connections.append(encoding)

        def connect(self, *args, **kwargs):
            return None

        def login(self, *args, **kwargs):
            return None

        def cwd(self, _path):
            return None

        def mlsd(self):
            if self.encoding == "utf-8":
                # Exact byte reported by the real indexer.  ftplib raises while
                # decoding the control-channel listing before yielding a name.
                b"legacy-\xf8.d64".decode("utf-8")
            return iter([(
                "legacy-ø.d64",
                {"type": "file", "size": "174848", "modify": "20260720"},
            )])

        def retrbinary(self, command, callback):
            transfers.append((self.encoding, command))
            callback(b"disk")

        def quit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(ultimate, "FTP", FakeFTP)
    fs = ultimate.DeviceFS("192.0.2.64")

    entries = fs.list_dir("/USB0")
    assert entries == [{
        "name": "legacy-ø.d64",
        "dir": False,
        "size": 174848,
        "mtime": "20260720",
    }]
    assert connections[:2] == ["utf-8", "latin-1"]
    assert fs._ftp_encoding == "latin-1"

    assert fs.fetch("/USB0/legacy-ø.d64") == b"disk"
    assert transfers == [("latin-1", "RETR /USB0/legacy-ø.d64")]


def test_attachment_header_strips_control_characters():
    header = server._attachment_headers('demo"\r\nname.prg')["Content-Disposition"]
    assert "\r" not in header
    assert "\n" not in header
    assert 'filename="demoname.prg"' in header


def test_upload_limit_is_enforced():
    upload = UploadFile(filename="large.bin", file=io.BytesIO(b"12345"), size=5)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server._read_upload(upload, 4))
    assert exc.value.status_code == 413


def test_mount_upload_does_not_block_event_loop():
    class SlowRest:
        def mount_attachment(self, drive, filename, data, mode="readwrite"):
            time.sleep(0.15)
            return {"drive": drive, "filename": filename, "size": len(data)}

    async def scenario():
        previous = server.rest
        server.rest = SlowRest()
        try:
            upload = UploadFile(filename="demo.d64", file=io.BytesIO(b"disk"), size=4)
            task = asyncio.create_task(server.mount_upload("a", "readwrite", upload))
            started = time.monotonic()
            await asyncio.sleep(0.03)
            elapsed = time.monotonic() - started
            assert elapsed < 0.10
            result = await task
            assert result["filename"] == "demo.d64"
        finally:
            server.rest = previous

    asyncio.run(scenario())


def test_root_serves_security_headers():
    with TestClient(server.app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_boot_options_toggle_persists_f7(monkeypatch):
    previous = server.CFG.get("boot_prekey", "")
    saves = []
    monkeypatch.setattr(server, "save_config", lambda: saves.append(True))
    try:
        server.CFG["boot_prekey"] = ""
        enabled = server.boot_options_set({"auto_fastload": True})
        assert enabled["auto_fastload"] is True
        assert server.CFG["boot_prekey"] == "F7"

        disabled = server.boot_options_set({"auto_fastload": False})
        assert disabled["auto_fastload"] is False
        assert server.CFG["boot_prekey"] == ""
        assert len(saves) == 2
    finally:
        server.CFG["boot_prekey"] = previous


def test_machine_reset_sends_configured_prekey(monkeypatch):
    class FakeRest:
        def put(self, path):
            return {"errors": [], "path": path}

    previous_rest = server.rest
    previous_prekey = server.CFG.get("boot_prekey", "")
    sent = []
    try:
        server.rest = FakeRest()
        server.CFG["boot_prekey"] = "F7"
        monkeypatch.setattr(server, "_send_boot_prekey", lambda *args, **kwargs: sent.append("F7") or "F7")
        result = server.machine("reset")
        assert sent == ["F7"]
        assert result["u64deck_boot_prekey"] == "F7"
    finally:
        server.rest = previous_rest
        server.CFG["boot_prekey"] = previous_prekey


def test_machine_reboot_reconnects_before_prekey(monkeypatch):
    calls = []

    class FakeRest:
        def put(self, path):
            calls.append(("put", path))
            return {"errors": [], "path": path}

    class FakeCmd:
        def close(self):
            calls.append(("close", None))

    previous_rest = server.rest
    previous_cmd = server.cmd
    previous_prekey = server.CFG.get("boot_prekey", "")
    previous_wait = server.CFG.get("boot_wait", 2.8)
    try:
        server.rest = FakeRest()
        server.cmd = FakeCmd()
        server.CFG["boot_prekey"] = "F7"
        server.CFG["boot_wait"] = 2.8

        def fake_send(*, delay=0.0, retry_window=0.0):
            calls.append(("prekey", (delay, retry_window)))
            return "F7"

        monkeypatch.setattr(server, "_send_boot_prekey", fake_send)
        result = server.machine("reboot")

        assert calls[0] == ("put", "/v1/machine:reboot")
        assert calls[1] == ("close", None)
        assert calls[2] == ("prekey", (2.8, 8.0))
        assert result["u64deck_boot_prekey"] == "F7"
    finally:
        server.rest = previous_rest
        server.cmd = previous_cmd
        server.CFG["boot_prekey"] = previous_prekey
        server.CFG["boot_wait"] = previous_wait


def test_machine_reset_does_not_send_prekey_when_disabled(monkeypatch):
    class FakeRest:
        def put(self, path):
            return {"errors": [], "path": path}

    previous_rest = server.rest
    previous_prekey = server.CFG.get("boot_prekey", "")
    try:
        server.rest = FakeRest()
        server.CFG["boot_prekey"] = ""
        called = []
        monkeypatch.setattr(
            server,
            "_send_boot_prekey",
            lambda *args, **kwargs: called.append(True),
        )
        result = server.machine("reset")
        assert called == [True]
        assert "u64deck_boot_prekey" not in result
    finally:
        server.rest = previous_rest
        server.CFG["boot_prekey"] = previous_prekey


def test_safe_get_retries_incomplete_body_over_fresh_connection(monkeypatch):
    import httpx
    import ultimate

    request = httpx.Request("GET", "http://u64/v1/info")

    class BrokenClient:
        def get(self, path, params=None):
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body",
                request=request,
            )

    fresh_calls = []

    class FreshClient:
        def __init__(self, *args, **kwargs):
            fresh_calls.append((args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            return httpx.Response(
                200,
                json={"product": "Ultimate 64", "firmware_version": "3.x"},
                request=request,
            )

    rest = ultimate.UltimateREST.__new__(ultimate.UltimateREST)
    rest.host = "u64"
    rest.base = "http://u64"
    rest._timeout = 8.0
    rest._headers = {}
    rest.client = BrokenClient()

    monkeypatch.setattr(ultimate.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ultimate.httpx, "Client", FreshClient)

    result = rest.get_json("/v1/info")
    assert result["product"] == "Ultimate 64"
    assert len(fresh_calls) == 1


def test_stream_rate_badges_are_grouped_together():
    html = (Path(server.ASSETS) / "static" / "index.html").read_text(encoding="utf-8")
    start = html.index('<span class="meter-group"')
    end = html.index("</span>", html.index('id="aud"', start))
    grouped = html[start:end]
    assert 'id="fps"' in grouped
    assert 'id="aud"' in grouped
    assert grouped.index('id="fps"') < grouped.index('id="aud"')



def test_blank_disk_rejects_virtual_root():
    with pytest.raises(HTTPException) as exc:
        server.fs_create_disk({
            "kind": "d64", "folder": "/", "name": "scratch", "tracks": 35
        })
    assert exc.value.status_code == 400
    assert "top-level / is virtual" in exc.value.detail


def test_blank_disk_uses_storage_path_and_quotes_filename(monkeypatch):
    calls = []

    class FakeRest:
        def put(self, path, *, request_timeout=None):
            calls.append((path, request_timeout))
            return {"errors": []}

    previous = server.rest
    server.rest = FakeRest()
    try:
        result = server.fs_create_disk({
            "kind": "d64",
            "folder": "/USB0/My Disks/",
            "name": "Scratch #1",
            "diskname": "Scratch #1",
            "tracks": 35,
        })
    finally:
        server.rest = previous

    assert calls == [(
        "/v1/files/USB0/My%20Disks/Scratch%20%231.d64:create_d64"
        "?diskname=Scratch%20%231&tracks=35",
        30.0,
    )]
    assert result["path"] == "/USB0/My Disks/Scratch #1.d64"


def test_blank_disk_maps_missing_storage_to_clear_client_error():
    class MissingRest:
        def put(self, path, *, request_timeout=None):
            raise server.UltimateError(
                "HTTP 500: {\"errors\":[\"PATH DOESN'T EXIST\"]}"
            )

    previous = server.rest
    server.rest = MissingRest()
    try:
        with pytest.raises(HTTPException) as exc:
            server.fs_create_disk({
                "kind": "d64", "folder": "/USB0", "name": "scratch"
            })
    finally:
        server.rest = previous

    assert exc.value.status_code == 400
    assert "could not find or write to /USB0" in exc.value.detail


def test_new_disk_button_is_guarded_at_virtual_root():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="newDiskBtn"' in html
    assert 'if(FS.path==="/")' in js
    assert "virtual device list" in js


def test_blank_disk_uses_interactive_device_priority():
    events = []

    class FakeCoordinator:
        @contextmanager
        def operation(self, priority, reason, **kwargs):
            events.append(("enter", priority, reason))
            yield
            events.append(("exit", priority, reason))

    class FakeRest:
        def put(self, path, *, request_timeout=None):
            events.append(("put", request_timeout))
            return {"errors": []}

    previous_rest, previous_coord = server.rest, server.DEVICE_OP
    previous_job = dict(server.INDEXJOB)
    server.rest = FakeRest()
    server.DEVICE_OP = FakeCoordinator()
    server.INDEXJOB["running"] = True
    try:
        result = server.fs_create_disk({
            "kind": "d64", "folder": "/USB0", "name": "scratch"
        })
    finally:
        server.rest, server.DEVICE_OP = previous_rest, previous_coord
        server.INDEXJOB.clear()
        server.INDEXJOB.update(previous_job)

    assert events == [
        ("enter", "interactive", "creating blank disk"),
        ("put", 30.0),
        ("exit", "interactive", "creating blank disk"),
    ]
    assert result["index_was_paused"] is True


def test_blank_disk_timeout_warns_that_creation_may_have_completed():
    class SlowRest:
        def put(self, path, *, request_timeout=None):
            raise httpx.ReadTimeout("timed out")

    previous = server.rest
    server.rest = SlowRest()
    try:
        with pytest.raises(HTTPException) as exc:
            server.fs_create_disk({
                "kind": "d64", "folder": "/USB0", "name": "scratch"
            })
    finally:
        server.rest = previous

    assert exc.value.status_code == 504
    assert "may still have been created" in exc.value.detail


def test_index_status_ui_reports_priority_pause():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert "indexing paused" in js
    assert "Pausing index…" in js


def test_device_coordinator_prioritises_interactive_over_queued_background():
    from device_coordinator import DeviceOperationCoordinator

    coordinator = DeviceOperationCoordinator()
    order = []
    first_entered = threading.Event()
    release_first = threading.Event()

    def first_background():
        with coordinator.operation("background", "first background"):
            order.append("background-1")
            first_entered.set()
            release_first.wait(2)

    def second_background():
        with coordinator.operation("background", "second background"):
            order.append("background-2")

    def interactive():
        with coordinator.operation("interactive", "button click"):
            order.append("interactive")

    t1 = threading.Thread(target=first_background)
    t2 = threading.Thread(target=second_background)
    ti = threading.Thread(target=interactive)
    t1.start()
    assert first_entered.wait(1)
    t2.start()
    time.sleep(0.03)
    ti.start()
    time.sleep(0.03)
    release_first.set()
    for t in (t1, t2, ti):
        t.join(2)

    assert order == ["background-1", "interactive", "background-2"]


def test_sqlite_index_imports_legacy_json_and_searches(tmp_path: Path):
    from index_store import IndexStore

    dirs = tmp_path / ".dircache.json"
    imgs = tmp_path / ".imagecache.json"
    roots = tmp_path / ".indexmeta.json"
    dirs.write_text(json.dumps({
        "/USB0": [
            {"name": "Games", "dir": True, "size": 0, "mtime": ""},
            {"name": "Demo.d64", "dir": False, "size": 174848, "mtime": "20260101"},
        ],
        "/USB0/Games": [
            {"name": "Delta.prg", "dir": False, "size": 100, "mtime": "20260102"},
        ],
    }), encoding="utf-8")
    imgs.write_text(json.dumps({
        "/USB0/Demo.d64|174848|20260101": [
            {"name": "DELTA", "file_type": "PRG", "blocks": 12}
        ]
    }), encoding="utf-8")
    roots.write_text(json.dumps({
        "/USB0": {"completed": "2026-07-20 18:00", "dirs": 2, "images": 1, "secs": 4.2}
    }), encoding="utf-8")

    store = IndexStore(tmp_path / "index.sqlite3")
    try:
        imported = store.import_legacy(dirs, imgs, roots)
        assert imported["directories"] == 2
        assert imported["images"] == 1
        assert store.complete_cover("/USB0/Games") is not None
        results = store.search_cached("/USB0", "delta", inside_images=True, limit=10)
        assert {r["kind"] for r in results} == {"file", "in-image"}
        stats = store.stats()
        assert stats["directories"] == 2
        assert stats["images"] == 1
        assert stats["image_entries"] == 1
    finally:
        store.close()


def test_index_root_requires_explicit_confirmation():
    previous = dict(server.INDEXJOB)
    server.INDEXJOB["running"] = False
    try:
        with pytest.raises(HTTPException) as exc:
            server.fs_index_start({"root": "/"})
    finally:
        server.INDEXJOB.clear()
        server.INDEXJOB.update(previous)
    assert exc.value.status_code == 409
    assert "specific collection folder" in exc.value.detail


def test_index_pause_endpoint_controls_background_queue():
    previous_job = dict(server.INDEXJOB)
    try:
        server.INDEXJOB["running"] = True
        result = server.fs_index_pause({"paused": True})
        assert result == {"paused": True}
        assert server.DEVICE_OP.background_paused() is True
        result = server.fs_index_pause({"paused": False})
        assert result == {"paused": False}
        assert server.DEVICE_OP.background_paused() is False
    finally:
        server.DEVICE_OP.set_background_paused(False)
        server.INDEXJOB.clear()
        server.INDEXJOB.update(previous_job)


def test_index_ui_has_pause_eta_and_root_confirmation():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="idxPauseBtn"' in html
    assert "remaining" in js
    assert "confirm_root" in js
    assert "SQLite storage index" in js


def test_clearing_image_cache_invalidates_completed_roots(tmp_path: Path):
    from index_store import IndexStore

    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        store.put_directory("/USB0", [
            {"name": "disk.d64", "dir": False, "size": 174848, "mtime": "1"}
        ])
        store.put_image(
            "/USB0/disk.d64", 174848, "1",
            [{"name": "GAME", "file_type": "PRG", "blocks": 20}],
        )
        store.set_index_root("/USB0", {
            "completed": "now", "dirs": 1, "images": 1, "secs": 1
        })
        assert store.complete_cover("/USB0") is not None
        assert store.clear_images() == 1
        assert store.complete_cover("/USB0") is None
        assert store.get_directory("/USB0") is not None
        assert store.get_image("/USB0/disk.d64", 174848, "1") is None
    finally:
        store.close()


def test_invalidating_changed_folder_removes_covering_root(tmp_path: Path):
    from index_store import IndexStore

    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        store.put_directory("/USB0/Games", [])
        store.set_index_root("/USB0", {
            "completed": "now", "dirs": 1, "images": 0, "secs": 1
        })
        store.invalidate_path("/USB0/Games")
        assert store.complete_cover("/USB0/Games") is None
        assert store.get_directory("/USB0/Games") is None
    finally:
        store.close()


def test_index_database_is_stable_across_device_ip_changes():
    first = server._index_db_path("192.168.1.10")
    second = server._index_db_path("192.168.1.11")
    assert first == second
    assert first.name == ".u64deck-index.sqlite3"


def test_local_index_mapping_requires_storage_root():
    from local_indexer import normalise_ultimate_root

    assert normalise_ultimate_root("USB0/Games/") == "/USB0/Games"
    with pytest.raises(ValueError):
        normalise_ultimate_root("/")
    with pytest.raises(ValueError):
        normalise_ultimate_root("/USB0/../SD")


def test_local_usb_scan_builds_ultimate_paths_and_sqlite_index(tmp_path: Path):
    from local_indexer import scan_local_tree

    source = tmp_path / "stick"
    games = source / "Games"
    games.mkdir(parents=True)
    (source / "README.txt").write_text("hello", encoding="utf-8")
    (games / "Demo.d64").write_bytes(bytes(174848))

    store = IndexStore(tmp_path / "local.sqlite3")
    scan_id = store.begin_local_scan("/USB0", str(source))
    try:
        summary = scan_local_tree(
            source,
            "/USB0",
            image_is_cached=lambda path, size, mtime: store.get_image(path, size, mtime) is not None,
            commit_batch=lambda dirs, images, cached: store.put_local_batch(
                scan_id, dirs, images, cached
            ),
            stop_check=lambda: False,
            pause_wait=lambda: True,
            progress=lambda _snapshot: None,
            batch_directories=1,
        )
        summary["secs"] = 0.1
        result = store.finish_local_scan(
            scan_id, "/USB0", str(source), summary, volume_id="TEST"
        )

        root_entries = store.get_directory("/USB0")
        assert {e["name"] for e in root_entries} == {"Games", "README.txt"}
        assert store.get_directory("/USB0/Games")[0]["name"] == "Demo.d64"
        assert store.get_image(
            "/USB0/Games/Demo.d64",
            (games / "Demo.d64").stat().st_size,
            store.get_directory("/USB0/Games")[0]["mtime"],
        ) is not None
        assert store.complete_cover("/USB0/Games") is not None
        assert result["dirs"] == 2
        assert result["images"] == 1
        assert store.local_imports()[0]["volume_id"] == "TEST"
    finally:
        store.close()


def test_local_scan_completion_prunes_stale_paths(tmp_path: Path):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        store.put_directory("/USB0", [
            {"name": "Old", "dir": True, "size": 0, "mtime": "1"},
            {"name": "keep.txt", "dir": False, "size": 4, "mtime": "1"},
        ])
        store.put_directory("/USB0/Old", [])
        store.put_image("/USB0/old.d64", 174848, "1", [])

        scan_id = store.begin_local_scan("/USB0", "E:\\")
        store.put_local_batch(scan_id, [(
            "/USB0",
            [{"name": "keep.txt", "dir": False, "size": 4, "mtime": "2"}],
        )], [], [])
        store.finish_local_scan(scan_id, "/USB0", "E:\\", {
            "dirs": 1, "files": 1, "images": 0, "images_cached": 0,
            "errors": 0, "secs": 1,
        })

        assert store.get_directory("/USB0/Old") is None
        assert store.get_image("/USB0/old.d64", 174848, "1") is None
        assert store.get_directory("/USB0")[0]["mtime"] == "2"
    finally:
        store.close()


def test_local_usb_index_controls_are_present():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "BUILD INDEX FROM LOCAL USB" in html
    assert 'id="localIndexSource"' in html
    assert 'id="localIndexRoot"' in html
    assert '"/api/fs/index/local"' in js
    assert "does not copy or change files" in js


def test_local_usb_index_requires_selected_ultimate(tmp_path: Path):
    previous_cfg = dict(server.CFG)
    previous_job = dict(server.INDEXJOB)
    try:
        server.CFG["u64_host"] = ""
        server.INDEXJOB["running"] = False
        with pytest.raises(HTTPException) as exc:
            server.fs_index_local_start({"source": str(tmp_path), "root": "/USB0"})
        assert exc.value.status_code == 409
        assert "select the target Ultimate" in exc.value.detail
    finally:
        server.CFG.clear()
        server.CFG.update(previous_cfg)
        server.INDEXJOB.clear()
        server.INDEXJOB.update(previous_job)


def test_swap_autobuild_groups_only_matching_side_images():
    siblings = [
        "Shape-The_Shores_of_Reflection_side1.d64",
        "Shape-The_Shores_of_Reflection_side2.d64",
        "Shape-The_Shores_of_Reflection_side10.d64",
        "Another_Title_side1.d64",
        "Scratch-3.d64",
        "Unrelated-27.d64",
        "Shape-The_Shores_of_Reflection_side3.g64",
    ]
    assert server._swap_group_candidates(
        "Shape-The_Shores_of_Reflection_side1.d64", siblings
    ) == [
        "Shape-The_Shores_of_Reflection_side1.d64",
        "Shape-The_Shores_of_Reflection_side2.d64",
        "Shape-The_Shores_of_Reflection_side10.d64",
    ]


def test_swap_autobuild_supports_release_suffix_after_disk_number():
    siblings = [
        "ThePhoenixCode-Disk1-BZ.D64",
        "ThePhoenixCode-Disk2-BZ.D64",
        "ThePhoenixCode-Disk3-EN.D64",
        "AnotherTitle-Disk2-BZ.D64",
    ]
    assert server._swap_group_candidates(
        "ThePhoenixCode-Disk1-BZ.D64", siblings
    ) == [
        "ThePhoenixCode-Disk1-BZ.D64",
        "ThePhoenixCode-Disk2-BZ.D64",
    ]


def test_swap_autobuild_supports_strict_numbered_series():
    siblings = [
        "Scratch-10.d64", "Scratch-3.d64", "Scratch-1.d64",
        "Scratchpad-2.d64", "Other-4.d64",
    ]
    assert server._swap_group_candidates("Scratch-3.d64", siblings) == [
        "Scratch-1.d64", "Scratch-3.d64", "Scratch-10.d64",
    ]


def test_swap_autobuild_unknown_or_single_filename_stays_solo():
    assert server._swap_group_candidates(
        "Riverside1.d64", ["Riverside1.d64", "Riverside2.d64"]
    ) == ["Riverside1.d64"]
    assert server._swap_group_candidates(
        "Only_Disk_1.d64", ["Only_Disk_1.d64", "Other_Disk_2.d64"]
    ) == ["Only_Disk_1.d64"]


def test_swap_autobuild_understands_wrapped_and_of_total_names():
    assert server._swap_group_candidates(
        "Game (Disk 1).d64",
        ["Game (Disk 2).d64", "Game (Disk 1).d64", "Other (Disk 3).d64"],
    ) == ["Game (Disk 1).d64", "Game (Disk 2).d64"]
    assert server._swap_group_candidates(
        "Demo 2 of 3.d64",
        ["Demo 3 of 3.d64", "Demo 1 of 3.d64", "Demo 2 of 3.d64", "Demo 1 of 4.d64"],
    ) == ["Demo 1 of 3.d64", "Demo 2 of 3.d64", "Demo 3 of 3.d64"]


def test_release_version_is_consistent_in_code_ui_and_changelog():
    assert server.VERSION == "1.8.0"
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    changelog = (Path(server.ROOT) / "CHANGELOG.md").read_text(encoding="utf-8")
    assert 'id="ver"' in html
    assert '"/api/app_config"' in js
    assert "## 1.8.0 — Public Beta 6" in changelog.split("## 1.8.0 — Public Beta 2", 1)[0]


def test_built_in_help_contains_no_release_specific_versions():
    static = Path(server.ASSETS) / "static"
    help_js = (static / "help_content.js").read_text(encoding="utf-8")
    pattern = re.compile(r"\b(?:Public Beta\s+\d+|Beta\s+\d+|v\d+\.\d+\.\d+)\b", re.I)
    assert not pattern.search(help_js)


def test_structured_parse_error_rows_do_not_trigger_device_toast():
    static = Path(server.ASSETS) / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "Array.isArray(j.errors)" in js
    assert 'typeof e==="string"&&e.trim()' in js
    assert 'if(deviceErrors.length)toast("Device: "+deviceErrors.join("; "),"err");' in js


def test_header_identity_and_device_details_share_an_aligned_status_block():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    assert 'class="header-status"' in html
    assert 'class="header-mainline"' in html
    assert 'class="brand-version" id="ver"' in html
    assert 'class="ready-line"' in html
    assert ".header-mainline{display:flex;align-items:baseline" in css
    assert ".brand-version{color:var(--dim);font-size:13px" in css
    assert "#devinfo{color:var(--dim);font-size:13px" in css
    assert ".ready-line{color:var(--dim);font-size:14px" in css



def test_experimental_menu_remote_is_removed_and_c64_keyboard_remains():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="kbTarget"' not in html
    assert "/api/menu_remote" not in js
    assert "Ultimate Menu (experimental)" not in html + js
    assert '"/api/keys"' in js
    assert "keys go to the C64" in js
    assert not hasattr(ultimate, "UltimateTelnet")
    assert not hasattr(ultimate, "VT100Screen")


def test_frontend_is_split_without_a_build_tool():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/static/app.css">' in html
    assert '<script src="/static/app.js"></script>' in html
    assert (static / "app.css").stat().st_size > 1000
    assert (static / "app.js").stat().st_size > 1000
    assert "<style>" not in html
    assert "<script>" not in html


def test_release_metadata_is_centralised():
    import release
    assert server.VERSION == release.VERSION == "1.8.0"
    assert release.RELEASE_LABEL == "Public Beta 6"
    assert server.BUILD == release.build_id(server.ASSETS, Path(server.__file__).parent)


def test_sqlite_random_sid_selection_stays_within_scope(tmp_path: Path):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        store.put_directory("/USB0/HVSC", [
            {"name": "A", "dir": True, "size": 0, "mtime": ""},
            {"name": "outside.txt", "dir": False, "size": 1, "mtime": "1"},
        ])
        store.put_directory("/USB0/HVSC/A", [
            {"name": "one.sid", "dir": False, "size": 10, "mtime": "1"},
            {"name": "two.sid", "dir": False, "size": 11, "mtime": "2"},
        ])
        store.put_directory("/USB0/Other", [
            {"name": "other.sid", "dir": False, "size": 12, "mtime": "3"},
        ])
        hit = store.random_file("/USB0/HVSC", ".sid")
        assert hit is not None
        assert hit["path"] in {"/USB0/HVSC/A/one.sid", "/USB0/HVSC/A/two.sid"}
        assert hit["candidates"] == 2
        rows = store.files_in_directory("/USB0/HVSC/A", ".sid")
        assert [row["name"] for row in rows] == ["one.sid", "two.sid"]
    finally:
        store.close()


def test_local_import_turns_network_reindex_into_verification(tmp_path: Path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    scan_id = store.begin_local_scan("/USB0", "F:\\")
    store.put_local_batch(scan_id, [("/USB0", [])], [], [])
    store.finish_local_scan(scan_id, "/USB0", "F:\\", {
        "dirs": 1, "files": 0, "images": 0, "images_cached": 0,
        "errors": 0, "secs": 1,
    })

    class NoStartThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def start(self):
            return None

    previous_job = dict(server.INDEXJOB)
    previous_thread = server._INDEX_THREAD
    try:
        server.INDEXJOB["running"] = False
        monkeypatch.setattr(server, "_index_store", lambda: store)
        monkeypatch.setattr(server.threading, "Thread", NoStartThread)
        result = server.fs_index_start({"root": "/USB0"})
        assert result["verification"] is True
        assert server.INDEXJOB["verification"] is True
        assert store.complete_cover("/USB0") is not None
    finally:
        server.INDEXJOB.clear()
        server.INDEXJOB.update(previous_job)
        server._INDEX_THREAD = previous_thread
        store.close()



def test_random_dive_starts_from_sqlite_without_ftp_folder_walk(monkeypatch):
    sid = bytearray(0x78)
    sid[:4] = b"PSID"
    sid[4:6] = b"\x00\x02"
    sid[0x0E:0x10] = b"\x00\x01"
    sid[0x10:0x12] = b"\x00\x01"
    sid[0x16:0x1A] = b"TEST"

    class FakeStore:
        def random_file(self, root, suffix):
            assert root == "/USB0/HVSC"
            assert suffix == ".sid"
            return {
                "parent": "/USB0/HVSC/A",
                "path": "/USB0/HVSC/A/test.sid",
                "name": "test.sid",
                "size": len(sid),
                "mtime": "1",
                "candidates": 42,
            }

        def files_in_directory(self, parent, suffix, limit=300):
            assert parent == "/USB0/HVSC/A"
            return [{"path": "/USB0/HVSC/A/test.sid", "name": "test.sid"}]

    class FakeFS:
        def fetch(self, path):
            assert path == "/USB0/HVSC/A/test.sid"
            return bytes(sid)

    class FakeRest:
        def post_file(self, path, name, data, **kwargs):
            assert path == "/v1/runners:sidplay"
            assert name == "test.sid"
            return {"ok": True}

    previous_fs, previous_rest = server.devfs, server.rest
    previous_cfg = server.CFG.get("cart_safe_run", True)
    previous_juke = dict(server.JUKE)
    try:
        monkeypatch.setattr(server, "_index_store", lambda: FakeStore())
        server.devfs = FakeFS()
        server.rest = FakeRest()
        server.CFG["cart_safe_run"] = False
        server.JUKE.update({"items": [], "index": -1, "playing": False,
                            "shuffle": False, "song": 0, "timer": None,
                            "folder": "", "loading": False, "source": "",
                            "generation": 0})
        result = server.juke_random({"root": "/USB0/HVSC"})
        assert result["backend"] == "sqlite"
        assert result["indexed_candidates"] == 42
        assert result["selected"] == "/USB0/HVSC/A/test.sid"
        assert result["playing"] is True
    finally:
        server._juke_cancel_timer()
        server.devfs, server.rest = previous_fs, previous_rest
        server.CFG["cart_safe_run"] = previous_cfg
        server.JUKE.clear()
        server.JUKE.update(previous_juke)


def test_user_items_store_persists_and_deduplicates(tmp_path: Path):
    from user_items import UserItemsStore

    path = tmp_path / "user_items.json"
    store = UserItemsStore(path)
    item = {
        "type": "disk", "label": "Test Disk", "detail": "/USB0/Test.d64",
        "action": "disk_run", "payload": {"path": "/USB0/Test.d64"},
    }
    first = store.favourite(item)
    second = store.favourite({**item, "label": "Renamed Label"})
    assert first["id"] == second["id"]
    snap = store.snapshot()
    assert len(snap["favorites"]) == 1
    assert snap["favorites"][0]["label"] == "Renamed Label"

    store.recent(item)
    store.recent(item)
    assert len(store.snapshot()["recents"]) == 1
    assert UserItemsStore(path).snapshot()["favorites"][0]["id"] == first["id"]
    assert store.unfavourite(first["id"]) is True
    assert store.clear_recents() == 1


def test_index_store_records_parse_errors_and_tolerates_fat_timestamps(tmp_path: Path):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        store.put_image(
            "/USB0/bad.d64", 174848, "20260721120000", [],
            parse_ok=False, parse_error="illegal track/sector 70/0",
        )
        errors = store.parse_errors()
        assert errors[0]["path"] == "/USB0/bad.d64"
        assert "illegal track" in errors[0]["error"]
        assert store.stats()["parse_failures"] == 1

        store.put_image(
            "/USB0/good.d64", 174848, "20260721120000",
            [{"name": "GAME", "file_type": "PRG", "blocks": 1}],
        )
        # FAT two-second precision and UTC/local whole-hour shifts both reuse
        # the existing parsed directory rather than reading the image again.
        assert store.get_image_compatible(
            "/USB0/good.d64", 174848, "20260721120002"
        )[0]["name"] == "GAME"
        assert store.get_image_compatible(
            "/USB0/good.d64", 174848, "20260721130000",
            allow_timezone_shift=True,
        )[0]["name"] == "GAME"
        assert store.get_image_compatible(
            "/USB0/good.d64", 174848, "20260721130000"
        ) is None
    finally:
        store.close()


def test_v150_ui_has_favourites_recording_and_one_u64_menu_button():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'data-tab="home"' in html
    assert 'id="btnRecord"' in html
    assert 'id="recStatus"' in html
    assert "MediaRecorder" in js and "createMediaStreamDestination" in js
    assert '"/api/user_items/favorite"' in js
    assert html.count("machine('menu_button')") == 1


def test_user_items_api_roundtrip(tmp_path: Path, monkeypatch):
    from user_items import UserItemsStore

    store = UserItemsStore(tmp_path / "items.json")
    monkeypatch.setattr(server, "USER_ITEMS", store)
    payload = {
        "type": "folder", "label": "Games", "detail": "/USB0/Games",
        "action": "fs_browse", "payload": {"path": "/USB0/Games"},
    }
    fav = server.user_items_favorite(payload)["favorite"]
    assert server.user_items_list()["favorites"][0]["id"] == fav["id"]
    server.user_items_recent(payload)
    assert len(server.user_items_list()["recents"]) == 1
    assert server.user_items_unfavorite(fav["id"])["removed"] is True
    assert server.user_items_clear_recents()["cleared"] == 1


def test_v160_ui_has_help_recording_modes_and_internal_favourites():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    help_js = (static / "help_content.js").read_text(encoding="utf-8")
    assert 'id="helpBtn"' in html and '>Help<' in html
    assert 'id="recMode"' in html
    assert 'value="video"' in html and 'value="audio"' in html
    assert 'id="recDuration"' in html and 'id="recQuality"' in html
    assert 'id="recFormat"' in html and 'value="mp4"' in html and 'value="webm"' in html
    assert 'id="recSummary"' in html and 'Stream frames' in html
    assert 'id="recFilename"' in html and 'id="recChooseLocation"' in html
    assert 'disk_entry_run' in js and 'disk_entry_dma' in js
    assert 'refreshStarButtons' in js
    assert 'VIDEO NOT CONNECTED' in js
    assert 'AUDIO STILL CONNECTED' in js
    assert 'Audio live · ${rate}/s' in js
    assert 'Audio reconnecting…' in js
    assert 'window.HELP_SECTIONS' in help_js


def test_keyboard_batches_are_serialised_across_threads(monkeypatch):
    cmd = ultimate.CommandSocket("example.invalid")
    sent = []
    barrier = threading.Barrier(3)

    def fake_send(_command, payload=b""):
        sent.append(payload.decode("ascii"))
        time.sleep(0.002)

    monkeypatch.setattr(cmd, "_send", fake_send)

    def worker(data):
        barrier.wait()
        cmd.type_petscii(data, chunk=1, delay=0.001)

    a = threading.Thread(target=worker, args=(b"RUN",))
    b = threading.Thread(target=worker, args=(b"ABC",))
    a.start(); b.start(); barrier.wait(); a.join(); b.join()
    result = "".join(sent)
    assert result in {"RUNABC", "ABCRUN"}


def test_keyboard_frontend_has_single_inflight_queue():
    js = ((Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8"))
    assert "keySending=false" in js
    assert "while(keyq.length)" in js
    assert "keyq.splice(0,8)" in js
    assert "setTimeout(flushKeys,6)" in js


def test_swap_controls_reclaim_screen_focus_and_block_repeat():
    js = ((Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8"))
    assert "let swapBusy=false" in js
    assert "if(swapBusy)return" in js
    assert "swapControlsBusy(true)" in js
    assert "screenEl.focus({preventScroll:true})" in js
    assert 'screenEl.addEventListener("pointerdown"' in js


def test_mount_mode_options_persist(monkeypatch):
    previous = dict(server.CFG)
    called = []
    monkeypatch.setattr(server, "save_config", lambda: called.append(True))
    try:
        result = server.mount_options_set({"default_mode": "readonly"})
        assert result["default_mode"] == "readonly"
        assert server.CFG["default_mount_mode"] == "readonly"
        assert called
        with pytest.raises(HTTPException):
            server.mount_options_set({"default_mode": "unsafe"})
    finally:
        server.CFG.clear()
        server.CFG.update(previous)


def test_device_duplicate_copies_and_invalidates(tmp_path: Path, monkeypatch):
    class FakeFS:
        def __init__(self):
            self.uploaded = None
        def fetch(self, path, max_size=0):
            assert path == "/USB0/work.d64"
            return b"disk-data"
        def upload(self, path, data):
            self.uploaded = (path, data)
    class FakeStore:
        def __init__(self): self.invalidated = None
        def invalidate_path(self, path): self.invalidated = path
    fake_fs, fake_store = FakeFS(), FakeStore()
    monkeypatch.setattr(server, "devfs", fake_fs)
    monkeypatch.setattr(server, "_index_store", lambda: fake_store)
    result = server._copy_device_file("/USB0/work.d64", "/USB0/work-copy.d64")
    assert result["destination"] == "/USB0/work-copy.d64"
    assert fake_fs.uploaded == ("/USB0/work-copy.d64", b"disk-data")
    assert fake_store.invalidated == "/USB0"


def test_diagnostics_export_is_zip_and_redacts(monkeypatch):
    class FakeRest:
        def info(self): return {"product": "Ultimate", "firmware": "test"}
    monkeypatch.setattr(server, "rest", FakeRest())
    monkeypatch.setattr(server, "stream_stats", lambda: {"video": {"packets": 1}})
    monkeypatch.setattr(server, "fs_index_status", lambda: {"running": False})
    monkeypatch.setattr(server, "cache_stats", lambda: {"database": {"images": 2}})
    monkeypatch.setattr(server, "cache_parse_errors", lambda limit=200: {"count": 0, "errors": []})
    previous = dict(server.CFG)
    try:
        server.CFG["password"] = "secret"
        response = server.diagnostics_export({"browser": {"userAgent": "pytest"}})
        assert response.media_type == "application/zip"
        import io, zipfile, json
        with zipfile.ZipFile(io.BytesIO(response.body)) as zf:
            names = set(zf.namelist())
            assert {"summary.json", "config-sanitised.json", "image-parse-errors.json"} <= names
            cfg = json.loads(zf.read("config-sanitised.json"))
            assert cfg["password"] == "<redacted>"
    finally:
        server.CFG.clear()
        server.CFG.update(previous)


def test_local_browser_startup_setting_persists(monkeypatch):
    previous = dict(server.CFG)
    saves = []
    monkeypatch.setattr(server, "save_config", lambda: saves.append(True))
    monkeypatch.setattr(server, "_find_edge_executable", lambda env=None: Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"))
    try:
        result = server.local_settings_set({"browser_startup": "system"})
        assert result["browser_startup"] == "system"
        assert result["edge_available"] is True
        assert server.CFG["browser_startup"] == "system"
        assert saves == [True]
        with pytest.raises(HTTPException):
            server.local_settings_set({"browser_startup": "netscape"})
    finally:
        server.CFG.clear()
        server.CFG.update(previous)


def test_edge_app_launcher_uses_isolated_profile(tmp_path: Path, monkeypatch):
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"")
    profile = tmp_path / "profile"
    calls = []

    class DummyProcess:
        pass

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return DummyProcess()

    monkeypatch.setattr(server, "_find_edge_executable", lambda env=None: edge)
    monkeypatch.setattr(server, "_edge_profile_dir", lambda env=None: profile)
    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    result = server._launch_local_browser("http://localhost:8064", "edge_app")
    assert result["opened"] is True and result["browser"] == "edge"
    args = calls[0][0]
    assert str(edge) == args[0]
    assert "--app=http://localhost:8064" in args
    assert f"--user-data-dir={profile}" in args
    assert profile.is_dir()


def test_v162_ui_has_launcher_recording_compatibility_and_stable_meters():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    help_js = (static / "help_content.js").read_text(encoding="utf-8")
    assert 'id="localBrowserStartup"' in html
    assert 'value="edge_app"' in html and 'value="system"' in html and 'value="none"' in html
    assert '"/api/local_settings"' in js
    assert "fixWebmDuration" in js and "ebmlEncodeVint" in js
    assert "MP4 (unsupported in this browser)" in js
    assert "recordingSupport" in js
    assert "#btnRecord{min-width:132px" in css
    assert ".meter-group #aud{min-width:152px" in css
    assert "dedicated u64deck window" in help_js


def test_config_example_documents_edge_app_startup():
    cfg = json.loads((Path(server.ROOT) / "config.example.json").read_text(encoding="utf-8"))
    assert cfg["browser_startup"] == "edge_app"
    assert server._normalise_browser_startup("edge") == "edge_app"
    assert server._normalise_browser_startup("default") == "system"
    assert server._normalise_browser_startup("off") == "none"



def test_v163_ux_names_tooltips_and_recording_indicator():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    assert '>FAVOURITES</button>' in html
    assert '>STORAGE</button>' in html
    assert '>SID JUKEBOX</button>' in html
    assert '>DISKS</button>' not in html
    assert 'id="globalTooltip"' in html
    assert 'id="recordDot"' in html
    assert ".global-tooltip" in css and ".record-dot.active" in css
    assert "tooltipShow" in js and "tooltipHide" in js
    assert 'classList.toggle("active",REC.active)' in js


def test_v163_mount_policy_and_create_workflow_are_source_aware():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    cfg = json.loads((Path(server.ROOT) / "config.example.json").read_text(encoding="utf-8"))
    assert server.DEFAULT_CONFIG["default_mount_mode"] == "unlinked"
    assert server.DEFAULT_CONFIG["mount_mode_policy_version"] == 2
    assert cfg["default_mount_mode"] == "unlinked"
    assert cfg["mount_mode_policy_version"] == 2
    assert "Create &amp; Mount RW" in html
    assert 'mode=readwrite' in js
    assert 'let DEFAULT_MOUNT_MODE="unlinked"' in js


def test_v163_jukebox_folder_is_lazy_and_does_not_bulk_fetch(monkeypatch):
    class FakeStore:
        def files_in_directory(self, parent, suffix, limit=300):
            assert parent == "/USB0/HVSC/MUSICIANS/H"
            return [
                {"path": parent + "/one.sid", "name": "one.sid"},
                {"path": parent + "/two.sid", "name": "two.sid"},
            ]

    class NoFetchFS:
        def fetch(self, path):
            raise AssertionError("folder loading must not fetch SID bodies")

    previous_fs = server.devfs
    previous_juke = dict(server.JUKE)
    try:
        monkeypatch.setattr(server, "_index_store", lambda: FakeStore())
        server.devfs = NoFetchFS()
        server.JUKE.update({"items": [], "index": -1, "playing": False,
                            "shuffle": False, "song": 0, "timer": None,
                            "folder": "", "loading": False, "source": "",
                            "generation": 0})
        result = server.juke_folder({"path": "/USB0/HVSC/MUSICIANS/H"})
        assert result["lazy"] is True
        assert len(result["items"]) == 2
        assert all(item["lazy"] for item in result["items"])
    finally:
        server._juke_cancel_timer()
        server.devfs = previous_fs
        server.JUKE.clear()
        server.JUKE.update(previous_juke)


def test_v163_hvsc_mapping_is_case_insensitive(monkeypatch):
    previous_cfg = dict(server.CFG)
    previous_index = list(server.HVSC_INDEX)
    try:
        server.CFG["hvsc_path"] = "/Usb0/HVSC"
        server.HVSC_INDEX[:] = [
            ("/musicians/h/test.sid", "/MUSICIANS/H/Test.sid"),
            ("/games/g.sid", "/GAMES/G.sid"),
        ]
        rows, source = server._hvsc_rows_below("/USB0/hvsc/musicians")
        assert source == "hvsc"
        assert [row["path"] for row in rows] == ["/Usb0/HVSC/MUSICIANS/H/Test.sid"]
    finally:
        server.CFG.clear()
        server.CFG.update(previous_cfg)
        server.HVSC_INDEX[:] = previous_index


def test_v163_sqlite_paths_are_case_insensitive(tmp_path: Path):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        store.put_directory("/USB0/HVSC", [
            {"name": "MUSICIANS", "dir": True, "size": 0, "mtime": "1"},
        ])
        store.put_directory("/USB0/HVSC/MUSICIANS", [
            {"name": "Tune.sid", "dir": False, "size": 10, "mtime": "2"},
        ])
        store.set_index_root("/USB0/HVSC", {
            "completed": "test", "completed_at": 1, "dirs": 2,
            "images": 0, "secs": 0.1,
        })
        assert store.complete_cover("/usb0/hvsc/musicians") is not None
        rows = store.files_in_directory("/usb0/hvsc/musicians", ".sid")
        assert [r["name"] for r in rows] == ["Tune.sid"]
        assert store.random_file("/usb0/hvsc", ".sid")["name"] == "Tune.sid"
    finally:
        store.close()


def test_v163_frontend_bounds_requests_and_jukebox_polling():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert "AbortController" in js
    assert "pollBusy" in js
    assert "listSignature" in js
    assert "timeoutMs:5000" in js


def test_swap_decision_reports_related_members(monkeypatch):
    previous = dict(server.SWAP)
    try:
        monkeypatch.setattr(server, "devfs", type("DevFS", (), {"list_dir": staticmethod(lambda folder: [
            {"name": "ThePhoenixCode-Disk1-BZ.D64", "dir": False},
            {"name": "ThePhoenixCode-Disk2-BZ.D64", "dir": False},
            {"name": "Other-Disk3-BZ.D64", "dir": False},
        ])})())
        decision = server._swap_build_from_device(
            "/USB0/New-Demos/ThePhoenixCode-Disk1-BZ.D64", "a", "unlinked"
        )
        assert decision["kind"] == "related"
        assert decision["count"] == 2
        assert decision["detail"] == (
            "ThePhoenixCode-Disk1-BZ.D64 → ThePhoenixCode-Disk2-BZ.D64"
        )
        assert server.SWAP["source"] == "auto"
    finally:
        server.SWAP.clear()
        server.SWAP.update(previous)


def test_swap_reconstructs_from_mounted_drive_after_restart(monkeypatch):
    previous_swap = dict(server.SWAP)
    previous_mount = {k: dict(v) for k, v in server.MOUNT_STATE.items()}
    try:
        server.SWAP.clear()
        server.SWAP.update({"items": [], "index": -1, "drive": "a", "mode": "unlinked", "source": "none", "decision": {}})
        server.MOUNT_STATE.clear()
        server.MOUNT_STATE.update({"a": {}, "b": {}})
        monkeypatch.setattr(server, "devfs", type("DevFS", (), {"list_dir": staticmethod(lambda folder: [
            {"name": "ThePhoenixCode-Disk1-BZ.D64", "dir": False},
            {"name": "ThePhoenixCode-Disk2-BZ.D64", "dir": False},
        ])})())
        decision = server._reconcile_swap_from_drives({"drives": [{"a": {
            "enabled": True,
            "image_file": "/USB0/New-Demos/ThePhoenixCode-Disk2-BZ.D64",
            "mode": "unlinked",
        }}]})
        assert decision["source"] == "reconstructed"
        assert [item["label"] for item in server.SWAP["items"]] == [
            "ThePhoenixCode-Disk1-BZ.D64", "ThePhoenixCode-Disk2-BZ.D64"
        ]
        assert server.SWAP["index"] == 1
        assert server.MOUNT_STATE["a"]["path"].endswith("Disk2-BZ.D64")
        # A second refresh aligns state without claiming a new reconstruction.
        assert server._reconcile_swap_from_drives({"drives": [{"a": {
            "enabled": True,
            "image_file": "/USB0/New-Demos/ThePhoenixCode-Disk2-BZ.D64",
            "mode": "unlinked",
        }}]}) is None
    finally:
        server.SWAP.clear()
        server.SWAP.update(previous_swap)
        server.MOUNT_STATE.clear()
        server.MOUNT_STATE.update(previous_mount)


def test_drive_summary_and_matcher_feedback_are_present():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="screenDriveSummary"' in html
    assert 'id="storageDriveSummary"' in html
    assert 'id="mountedDrivesPanel"' in html
    assert 'id="swapDecision"' in html
    assert "jumpToMountedDrives" in js
    assert "swap_reconstructed" in js
    assert "No confident multi-disk match" in (Path(server.ROOT) / "server.py").read_text(encoding="utf-8")


def test_drive_image_path_does_not_restore_ejected_media():
    remembered = {"path": "/USB0/Games/Game-Disk1.d64"}
    assert server._drive_image_path({"image_file": ""}, remembered) == ""
    assert server._drive_image_path({"image_file": "Game-Disk1.d64"}, remembered) == remembered["path"]
    assert server._drive_image_path({}, remembered) == remembered["path"]


def test_manual_swap_queue_survives_drive_refresh_before_first_mount(monkeypatch):
    previous_swap = dict(server.SWAP)
    previous_mount = {k: dict(v) for k, v in server.MOUNT_STATE.items()}
    try:
        server.SWAP.clear()
        server.SWAP.update({
            "items": [
                {"label": "Chosen1.d64", "kind": "device", "path": "/USB0/Chosen1.d64"},
                {"label": "Chosen2.d64", "kind": "device", "path": "/USB0/Chosen2.d64"},
            ],
            "index": -1, "drive": "a", "mode": "unlinked",
            "source": "manual", "decision": {"source": "manual"},
        })
        server.MOUNT_STATE.clear()
        server.MOUNT_STATE.update({"a": {}, "b": {}})
        monkeypatch.setattr(server, "devfs", type("DevFS", (), {"list_dir": staticmethod(lambda folder: [])})())
        assert server._reconcile_swap_from_drives({"drives": [{"a": {
            "enabled": True,
            "image_file": "/USB0/PreviouslyMounted.d64",
            "mode": "unlinked",
        }}]}) is None
        assert server.SWAP["source"] == "manual"
        assert [item["label"] for item in server.SWAP["items"]] == ["Chosen1.d64", "Chosen2.d64"]
        assert server.SWAP["index"] == -1
    finally:
        server.SWAP.clear()
        server.SWAP.update(previous_swap)
        server.MOUNT_STATE.clear()
        server.MOUNT_STATE.update(previous_mount)


def _sid_header(*, fmt=b"PSID", chip_bits=1, songs=1, title=b"Tune", author=b"Author"):
    data = bytearray(0x7C)
    data[:4] = fmt
    data[4:6] = (2).to_bytes(2, "big")
    data[0x0E:0x10] = int(songs).to_bytes(2, "big")
    data[0x10:0x12] = (1).to_bytes(2, "big")
    data[0x16:0x16 + len(title)] = title
    data[0x36:0x36 + len(author)] = author
    data[0x76:0x78] = (int(chip_bits) << 4).to_bytes(2, "big")
    return bytes(data)


def test_sid_metadata_index_supports_chip_and_format_filters(tmp_path: Path):
    store = IndexStore(tmp_path / "sid.sqlite3")
    try:
        rows = [
            ("/USB0/HVSC/A/old.sid", "PSID", "6581", 1, "Old Tune"),
            ("/USB0/HVSC/A/new.sid", "RSID", "8580", 1, "New Tune"),
            ("/USB0/HVSC/A/multi.sid", "PSID", "6581+8580", 2, "Multi Tune"),
        ]
        for path, fmt, chip, sids, title in rows:
            store.put_sid_metadata(path, 124, "20260722010101", {
                "format": fmt, "version": 2, "songs": 1, "start_song": 1,
                "name": title, "author": "Composer", "released": "2026",
                "chip": chip, "clock": "PAL", "sids": sids, "md5": "",
            }, source="test")

        assert store.sid_metadata_search(
            "/USB0/HVSC", chip="6581", sid_format="PSID"
        )["total"] == 1
        assert store.sid_metadata_search(
            "/USB0/HVSC", chip="8580", sid_format="RSID"
        )["results"][0]["title"] == "New Tune"
        assert store.sid_metadata_search(
            "/USB0/HVSC", chip="mixed", sid_format="all"
        )["results"][0]["name"] == "multi.sid"
        assert store.sid_metadata_search(
            "/USB0/HVSC", "composer", chip="all", sid_format="all"
        )["total"] == 3
        mapped = store.sid_metadata_for_paths(["/usb0/hvsc/a/OLD.SID"])
        assert mapped["/usb0/hvsc/a/old.sid"]["chip"] == "6581"
    finally:
        store.close()


def test_local_sid_metadata_scan_maps_hvsc_folder_to_ultimate_path(tmp_path: Path):
    hvsc = tmp_path / "HVSC"
    (hvsc / "MUSICIANS" / "A").mkdir(parents=True)
    (hvsc / "DOCUMENTS").mkdir()
    (hvsc / "MUSICIANS" / "A" / "Tune.sid").write_bytes(
        _sid_header(chip_bits=2, title=b"Indexed Tune", author=b"Coder")
    )
    store = IndexStore(tmp_path / "sid.sqlite3")
    scan_id = store.begin_sid_scan("/USB0/HVSC", "local", str(hvsc))
    try:
        summary = scan_local_sid_tree(
            hvsc, "/USB0/HVSC",
            parse_sid=lambda data: server._parse_sid(data, compute_md5=False),
            is_cached=store.sid_metadata_is_current,
            commit_batch=lambda rows, seen: store.put_sid_scan_batch(scan_id, rows, seen),
            stop_check=lambda: False,
            pause_wait=lambda: True,
            progress=lambda _state: None,
        )
        summary["secs"] = 0.1
        store.finish_sid_scan(scan_id, "/USB0/HVSC", "local", str(hvsc), summary)
        row = store.sid_metadata_get("/usb0/hvsc/musicians/a/tune.sid")
        assert row is not None
        assert row["title"] == "Indexed Tune"
        assert row["author"] == "Coder"
        assert row["chip"] == "8580"
        assert row["format"] == "PSID"
        assert summary["parsed"] == 1
    finally:
        store.close()


def test_sid_playlist_prepopulates_chip_from_metadata_without_fetch(tmp_path: Path, monkeypatch):
    store = IndexStore(tmp_path / "sid.sqlite3")
    folder = "/USB0/HVSC/MUSICIANS/A"
    store.put_directory(folder, [
        {"name": "Tune.sid", "dir": False, "size": 124, "mtime": "1"},
    ])
    store.put_sid_metadata(folder + "/Tune.sid", 124, "1", {
        "format": "RSID", "version": 2, "songs": 2, "start_song": 1,
        "name": "Tune Title", "author": "Composer", "released": "2026",
        "chip": "8580", "clock": "PAL", "sids": 1, "md5": "",
    }, source="test")

    class NoFetchFS:
        def fetch(self, _path):
            raise AssertionError("loading a folder must remain lazy")

    previous_fs = server.devfs
    previous_juke = dict(server.JUKE)
    try:
        monkeypatch.setattr(server, "_index_store", lambda: store)
        server.devfs = NoFetchFS()
        server.JUKE.update({"items": [], "index": -1, "playing": False,
                            "shuffle": False, "song": 0, "timer": None,
                            "folder": "", "loading": False, "source": "",
                            "generation": 0})
        result = server.juke_folder({"path": folder})
        assert result["items"][0]["lazy"] is True
        assert result["items"][0]["meta"]["chip"] == "8580"
        assert result["items"][0]["meta"]["format"] == "RSID"
        assert result["items"][0]["meta"]["name"] == "Tune Title"
    finally:
        server.devfs = previous_fs
        server.JUKE.clear()
        server.JUKE.update(previous_juke)
        store.close()


def test_sid_metadata_controls_and_filters_are_present():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="jkChip"' in html
    assert 'id="jkFormat"' in html
    assert 'id="jkSidSource"' in html
    assert 'id="jkSidRoot"' in html
    assert 'id="jkSidUltimateBtn"' in html
    assert 'id="jkSidLocalBtn"' in html
    assert 'id="jkSidIndexToggle"' in html
    assert "jkSidIndexTogglePanel" in js
    assert "/api/juke/index/local" in js
    assert "/api/juke/index/ultimate" in js


def test_sid_chip_badges_have_distinct_consistent_styles():
    static = Path(server.ASSETS) / "static"
    css = (static / "app.css").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    for kind in ("6581", "8580", "either", "mixed", "unknown"):
        assert f".badge.chip-{kind}" in css
        assert f'chip-{kind}' in js or kind in js
    assert "sidChipBadge(m)" in js
    assert "jkSidBadges(m)" in js


def test_sid_index_help_readme_and_accessible_toggle_are_current():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    help_js = (static / "help_content.js").read_text(encoding="utf-8")
    readme = (Path(server.ROOT) / "README.md").read_text(encoding="utf-8")
    assert 'aria-controls="jkSidIndex"' in html
    assert 'aria-expanded="false"' in html
    assert 'setAttribute("aria-expanded"' in js
    for text in (
        "SID Index · 61,157",
        "blank search with only Chip or Format selected",
        "amber 6581",
        "cyan 8580",
        "rescan unchanged files",
    ):
        assert text in help_js
    for text in (
        "clearly labelled **SID Index** button",
        "box returns all matching indexed tunes",
        "amber 6581",
        "cyan 8580",
        "Leave **rescan unchanged files** off",
    ):
        assert text in readme


def test_sid_play_queue_uses_responsive_full_height_layout():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'class="panel sid-queue-panel"' in html
    assert 'id="jkListContext"' in html
    assert 'class="sid-queue-list"' in html
    assert "#tab-sid.active{display:flex;flex-direction:column" in css
    assert ".sid-queue-panel{flex:1 1 auto" in css
    assert ".jkq-head{position:sticky" in css
    assert "singleAuthor=knownAuthors.length===1" in js
    assert "PLAY QUEUE — ${knownAuthors[0]}" in js
    assert 'role="table"' in html


def test_completed_sid_scan_reconciles_removed_tunes(tmp_path: Path):
    store = IndexStore(tmp_path / "sid.sqlite3")
    meta = {
        "format": "PSID", "version": 2, "songs": 1, "start_song": 1,
        "name": "Tune", "author": "", "released": "", "chip": "6581",
        "clock": "PAL", "sids": 1, "md5": "",
    }
    try:
        keep = "/USB0/HVSC/A/keep.sid"
        stale = "/USB0/HVSC/A/removed.sid"
        store.put_sid_metadata(keep, 124, "1", meta, source="old")
        store.put_sid_metadata(stale, 124, "1", meta, source="old")
        scan_id = store.begin_sid_scan("/USB0/HVSC", "local", "E:/HVSC")
        store.put_sid_scan_batch(scan_id, [], [keep])
        store.finish_sid_scan(scan_id, "/USB0/HVSC", "local", "E:/HVSC", {
            "files": 1, "parsed": 0, "cached": 1, "errors": 0, "secs": 0.1,
        })
        assert store.sid_metadata_get(keep) is not None
        assert store.sid_metadata_get(stale) is None
    finally:
        store.close()


def test_stable_index_migration_merges_storage_images_and_sid_metadata(tmp_path: Path):
    from index_migration import prepare_stable_index

    older = tmp_path / ".u64deck-index-192.168.1.10-old.sqlite3"
    store = IndexStore(older)
    try:
        store.put_directory("/", [{"name": "USB0", "dir": True}])
        store.put_directory("/USB0", [
            {"name": "Games", "dir": True},
            {"name": "Demo.d64", "dir": False, "size": 174848, "mtime": "1"},
        ])
        store.put_image("/USB0/Demo.d64", 174848, "1", [
            {"name": "DEMO", "file_type": "PRG", "blocks": 12},
        ])
    finally:
        store.close()

    newer = tmp_path / ".u64deck-index-192.168.1.20-new.sqlite3"
    store = IndexStore(newer)
    try:
        store.put_directory("/USB0", [
            {"name": "Games", "dir": True},
            {"name": "Demo.d64", "dir": False, "size": 174848, "mtime": "1"},
            {"name": "New.d64", "dir": False, "size": 174848, "mtime": "2"},
        ])
        store.put_sid_metadata("/USB0/HVSC/MUSICIANS/A/Tune.sid", 124, "2", {
            "format": "PSID", "version": 2, "songs": 1, "start_song": 1,
            "name": "Tune", "author": "Coder", "released": "2026",
            "chip": "8580", "clock": "PAL", "sids": 1, "md5": "",
        }, source="local")
    finally:
        store.close()

    result = prepare_stable_index(tmp_path, log=None)
    assert result["status"] == "migrated"
    assert result["migrated_sources"] == 2
    stable = tmp_path / ".u64deck-index.sqlite3"
    assert stable.is_file()
    merged = IndexStore(stable)
    try:
        stats = merged.stats()
        assert stats["directories"] == 2
        assert stats["images"] == 1
        assert stats["sid_metadata"] == 1
        names = {entry["name"] for entry in merged.get_directory("/USB0")}
        assert names == {"Games", "Demo.d64", "New.d64"}
        assert merged.get_image("/USB0/Demo.d64", 174848, "1")[0]["name"] == "DEMO"
        assert merged.sid_metadata_get("/usb0/hvsc/musicians/a/tune.sid")["chip"] == "8580"
    finally:
        merged.close()
    backups = list((tmp_path / "index-backups").rglob("*.sqlite3"))
    assert len(backups) == 2
    assert older.is_file() and newer.is_file()
    assert prepare_stable_index(tmp_path, log=None)["status"] == "ready"



def test_stable_index_migration_retries_windows_file_sharing_violation(tmp_path: Path, monkeypatch):
    import index_migration

    legacy = tmp_path / ".u64deck-index-192.168.1.64-test.sqlite3"
    store = IndexStore(legacy)
    try:
        store.put_directory("/USB0", [{"name": "Games", "dir": True}])
    finally:
        store.close()

    real_replace = index_migration.os.replace
    attempts = []

    def sharing_violation(source, target):
        attempts.append((source, target))
        if len(attempts) < 3:
            exc = PermissionError(13, "file is being used by another process")
            exc.winerror = 32
            raise exc
        return real_replace(source, target)

    monkeypatch.setattr(index_migration.os, "replace", sharing_violation)
    monkeypatch.setattr(index_migration.time, "sleep", lambda _seconds: None)

    result = index_migration.prepare_stable_index(tmp_path, log=None)
    assert result["status"] == "migrated"
    assert len(attempts) == 3
    assert (tmp_path / ".u64deck-index.sqlite3").is_file()


def test_stable_index_migration_cleanup_failure_does_not_hide_original_error(tmp_path: Path, monkeypatch):
    import index_migration

    legacy = tmp_path / ".u64deck-index-192.168.1.64-test.sqlite3"
    store = IndexStore(legacy)
    store.close()

    def denied(_source, _target):
        exc = PermissionError(13, "file is being used by another process")
        exc.winerror = 32
        raise exc

    monkeypatch.setattr(index_migration, "_replace_with_retry", denied)
    monkeypatch.setattr(
        index_migration, "_cleanup_database_family",
        lambda path: [f"{path.name}: cleanup sharing violation"],
    )
    messages = []
    result = index_migration.prepare_stable_index(tmp_path, log=messages.append)

    assert result["status"] == "failed"
    assert "being used by another process" in result["error"]
    assert any("migration cleanup also failed" in message for message in messages)

def test_juke_state_exposes_current_sid_path_for_now_playing_favourite():
    previous = dict(server.JUKE)
    try:
        path = "/USB0/HVSC/MUSICIANS/A/Tune.sid"
        server.JUKE.update({
            "items": [{"label": "Tune.sid", "path": path, "data": b"x",
                       "meta": {"name": "Tune", "songs": 1, "start_song": 1}}],
            "index": 0, "playing": True, "shuffle": False, "song": 1,
            "folder": "/USB0/HVSC/MUSICIANS/A", "loading": False,
            "source": "test", "generation": 0, "timer": None,
        })
        assert server._juke_state()["now"]["path"] == path
    finally:
        server.JUKE.clear()
        server.JUKE.update(previous)


def test_public_beta_help_and_now_playing_star_are_present():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    help_js = (static / "help_content.js").read_text(encoding="utf-8")
    readme = (Path(server.ROOT) / "README.md").read_text(encoding="utf-8")
    assert 'id="jkNowStar"' in html
    assert "jkToggleNowFavourite" in js
    assert ".u64deck-index.sqlite3" in help_js
    assert "star beside the playback controls" in help_js
    assert "Public Beta 6" in readme
    assert ".u64deck-index.sqlite3" in readme


def test_stable_index_migration_skips_legacy_orphan_image_entries(tmp_path: Path):
    import sqlite3
    from index_migration import prepare_stable_index

    clean = tmp_path / ".u64deck-index-192.168.1.10-clean.sqlite3"
    store = IndexStore(clean)
    try:
        store.put_directory("/USB0", [
            {"name": "Good.d64", "dir": False, "size": 174848, "mtime": "1"},
        ])
        store.put_image("/USB0/Good.d64", 174848, "1", [
            {"name": "GOOD", "file_type": "PRG", "blocks": 10},
        ])
    finally:
        store.close()

    corrupt = tmp_path / ".u64deck-index-192.168.1.20-corrupt.sqlite3"
    store = IndexStore(corrupt)
    store.close()
    with sqlite3.connect(corrupt) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO image_entries(image_path,image_size,image_mtime,entry_index,"
            "name,name_lower,file_type,blocks) VALUES(?,?,?,?,?,?,?,?)",
            ("/USB0/Missing.d64", 174848, "9", 0, "ORPHAN", "orphan", "PRG", 1),
        )
        conn.commit()
        assert conn.execute("PRAGMA foreign_key_check").fetchall()

    messages = []
    result = prepare_stable_index(tmp_path, log=messages.append)
    assert result["status"] == "migrated"
    stable = IndexStore(tmp_path / ".u64deck-index.sqlite3")
    try:
        assert stable.get_image("/USB0/Good.d64", 174848, "1")[0]["name"] == "GOOD"
        assert stable._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert stable._conn.execute(
            "SELECT COUNT(*) FROM image_entries WHERE image_path='/USB0/Missing.d64'"
        ).fetchone()[0] == 0
    finally:
        stable.close()


def test_stable_index_migration_replaces_changed_image_as_coherent_unit(tmp_path: Path):
    import sqlite3
    from index_migration import prepare_stable_index

    older = tmp_path / ".u64deck-index-192.168.1.10-old-image.sqlite3"
    store = IndexStore(older)
    try:
        store.put_image("/USB0/Demo.d64", 174848, "1", [
            {"name": "OLD", "file_type": "PRG", "blocks": 1},
        ])
    finally:
        store.close()

    newer = tmp_path / ".u64deck-index-192.168.1.20-new-image.sqlite3"
    store = IndexStore(newer)
    try:
        store.put_image("/USB0/Demo.d64", 175531, "2", [
            {"name": "NEW", "file_type": "PRG", "blocks": 2},
        ])
    finally:
        store.close()

    # Ensure deterministic newest-source ordering for the same image path.
    older.touch()
    import os, time
    now = time.time()
    os.utime(older, (now - 20, now - 20))
    os.utime(newer, (now, now))

    result = prepare_stable_index(tmp_path, log=None)
    assert result["status"] == "migrated"
    with sqlite3.connect(tmp_path / ".u64deck-index.sqlite3") as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        rows = conn.execute(
            "SELECT image_size,image_mtime,name FROM image_entries "
            "WHERE image_path='/USB0/Demo.d64'"
        ).fetchall()
        assert rows == [(175531, "2", "NEW")]
        caches = conn.execute(
            "SELECT size,mtime FROM image_cache WHERE path='/USB0/Demo.d64'"
        ).fetchall()
        assert caches == [(175531, "2")]


def test_assembly64_is_polished_full_height_workspace():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    help_js = (static / "help_content.js").read_text(encoding="utf-8")

    assert 'class="asm-workspace"' in html
    assert 'id="asmResMeta"' in html and 'id="asmFilesMeta"' in html
    assert '#tab-asm64.active{display:flex;flex-direction:column;overflow:hidden}' in css
    assert '.asm-workspace{display:grid;' in css
    for label in ("Release Name", "Group", "Handle", "Event"):
        assert label in js
    for heading in ("Release", "Category", "Rating", "Updated", "File", "Type", "Actions"):
        assert heading in js
    assert 'asmPresetLabel("category",e.category)' in js
    assert '1:"Demos"' in js and 'ASM_CATEGORY_LABELS' in js
    assert 'asmUpdatedLabel(e.updated)' in js and 'title="${esc(e.updated??"")}"' in js
    assert "mount_run" in js and "Mount & Run" in js
    assert 'results remain in the left pane' in help_js.lower()


def test_assembly64_mount_run_uses_shared_boot_sequence(monkeypatch):
    called = {}

    def fake_mount_and_boot(drive, mode, **kwargs):
        called.update(drive=drive, mode=mode, **kwargs)
        return {"errors": [], "typed": 'LOAD"*",8,1 + RUN'}

    monkeypatch.setattr(server, "_mount_and_boot", fake_mount_and_boot)
    monkeypatch.setattr(server, "_mount_mode", lambda mode: "unlinked")
    result = server._asm_deploy_bytes("Demo.d64", "mount_run", b"disk")
    assert result["typed"].endswith("+ RUN")
    assert called == {
        "drive": "a", "mode": "unlinked", "name": "Demo.d64", "data": b"disk"
    }


def test_assembly64_mount_run_rejects_non_disk_content():
    import pytest
    with pytest.raises(ValueError, match="disk images"):
        server._asm_deploy_bytes("Demo.prg", "mount_run", b"program")

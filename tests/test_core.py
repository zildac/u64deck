import asyncio
import hashlib
import copy
import io
import json
import re
import time
import threading
import zipfile
from pathlib import Path
from contextlib import closing, contextmanager

import pytest
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

import server
import ultimate
from d64 import DiskImage
from index_store import IndexStore
from network_awareness import LinkObservation
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

def test_sid_upload_uses_duplicate_file_parts_in_required_order():
    captured = {}

    class FakeClient:
        def post(self, path, params=None, files=None):
            captured.update({"path": path, "params": params, "files": files})
            request = httpx.Request("POST", "http://u64" + path)
            return httpx.Response(200, json={"errors": []}, request=request)

    rest = ultimate.UltimateREST.__new__(ultimate.UltimateREST)
    rest.coordinator = None
    rest.client = FakeClient()
    result = rest.post_sid(
        "Tune.sid", b"SID", songlengths=b"\x01\x23", songlengths_filename="Tune.ssl", songnr=3
    )

    assert result == {"errors": []}
    assert captured["path"] == "/v1/runners:sidplay"
    assert captured["params"] == {"songnr": 3}
    assert captured["files"] == [
        ("file", ("Tune.sid", b"SID", "application/octet-stream")),
        ("file", ("Tune.ssl", b"\x01\x23", "application/octet-stream")),
    ]


def test_sid_upload_omits_optional_songlength_part_when_unavailable():
    captured = {}

    class FakeClient:
        def post(self, path, params=None, files=None):
            captured["files"] = files
            request = httpx.Request("POST", "http://u64" + path)
            return httpx.Response(200, json={"errors": []}, request=request)

    rest = ultimate.UltimateREST.__new__(ultimate.UltimateREST)
    rest.coordinator = None
    rest.client = FakeClient()
    rest.post_sid("Tune.sid", b"SID")
    assert captured["files"] == [
        ("file", ("Tune.sid", b"SID", "application/octet-stream")),
    ]


def test_rest_timeout_replacement_waits_for_inflight_request(monkeypatch):
    request_started = threading.Event()
    release_request = threading.Event()
    old_closed = threading.Event()
    created = []

    class BlockingClient:
        def get(self, path, params=None):
            request_started.set()
            assert release_request.wait(2.0)
            request = httpx.Request("GET", "http://u64" + path)
            return httpx.Response(200, json={"ok": True}, request=request)
        def close(self):
            old_closed.set()

    class ReplacementClient:
        def close(self):
            pass

    def client_factory(*args, **kwargs):
        client = BlockingClient() if not created else ReplacementClient()
        created.append(client)
        return client

    monkeypatch.setattr(ultimate.httpx, "Client", client_factory)
    rest = ultimate.UltimateREST("u64")
    result = {}
    reader = threading.Thread(
        target=lambda: result.update(rest.get_json("/v1/info")), daemon=True)
    reader.start()
    assert request_started.wait(1.0)

    reconfigure = threading.Thread(target=lambda: rest.set_timeout(45.0), daemon=True)
    reconfigure.start()
    time.sleep(0.05)
    assert not old_closed.is_set()

    release_request.set()
    reader.join(1.0)
    reconfigure.join(1.0)
    assert result == {"ok": True}
    assert old_closed.is_set()
    assert rest.client is created[1]


def test_songlength_loader_parses_lengths_and_path_index(tmp_path: Path):
    raw = (
        b"; /MUSICIANS/T/Test/Tune.sid\r\n"
        b"0123456789abcdef0123456789abcdef=0:10 1:02.5\r\n"
    )
    path = tmp_path / "Songlengths.md5"
    path.write_bytes(raw)
    previous_path = server.CFG.get("songlengths_path", "")
    previous_lengths = dict(server.SONGLENGTHS)
    previous_path_lengths = dict(server.SONGLENGTHS_BY_PATH)
    previous_index = list(server.HVSC_INDEX)
    try:
        server.CFG["songlengths_path"] = str(path)
        assert server.load_songlengths() == 1
        assert server.SONGLENGTHS["0123456789abcdef0123456789abcdef"] == [10.0, 62.5]
        assert server.SONGLENGTHS_BY_PATH["musicians/t/test/tune.sid"] == [10.0, 62.5]
        assert server.HVSC_INDEX == [
            ("/musicians/t/test/tune.sid", "/MUSICIANS/T/Test/Tune.sid")
        ]
    finally:
        server.CFG["songlengths_path"] = previous_path
        server.SONGLENGTHS.clear()
        server.SONGLENGTHS.update(previous_lengths)
        server.SONGLENGTHS_BY_PATH.clear()
        server.SONGLENGTHS_BY_PATH.update(previous_path_lengths)
        server.HVSC_INDEX[:] = previous_index


def _minimal_sid(*, songs=1):
    data = bytearray(0x7C)
    data[:4] = b"PSID"
    data[4:6] = (2).to_bytes(2, "big")
    data[0x0E:0x10] = int(songs).to_bytes(2, "big")
    data[0x10:0x12] = (1).to_bytes(2, "big")
    return bytes(data)


def test_sid_ssl_payload_is_compact_bcd_per_subtune():
    sid = _minimal_sid(songs=4)
    digest = server.hashlib.md5(sid).hexdigest()
    previous = dict(server.SONGLENGTHS)
    try:
        server.SONGLENGTHS.clear()
        server.SONGLENGTHS[digest] = [12, 62.5, 5999, 6001]
        assert server._sid_ssl_payload(sid) == bytes([
            0x00, 0x12,  # 0:12
            0x01, 0x03,  # 1:03 (fractional seconds round half-up)
            0x99, 0x59,  # 99:59
            0x99, 0x59,  # clamped to the firmware's BCD range
        ])
    finally:
        server.SONGLENGTHS.clear()
        server.SONGLENGTHS.update(previous)


def test_sid_ssl_payload_uses_zeroes_for_missing_subtunes_and_caps_at_512_bytes():
    sid = _minimal_sid(songs=300)
    digest = server.hashlib.md5(sid).hexdigest()
    previous = dict(server.SONGLENGTHS)
    try:
        server.SONGLENGTHS.clear()
        server.SONGLENGTHS[digest] = [65]
        payload = server._sid_ssl_payload(sid)
        assert payload is not None
        assert len(payload) == 512
        assert payload[:4] == bytes([0x01, 0x05, 0x00, 0x00])
        assert payload[4:] == bytes(508)
    finally:
        server.SONGLENGTHS.clear()
        server.SONGLENGTHS.update(previous)


def test_sid_ssl_payload_falls_back_when_sid_or_length_match_is_unavailable():
    previous = dict(server.SONGLENGTHS)
    try:
        server.SONGLENGTHS.clear()
        assert server._sid_ssl_payload(b"not a sid") is None
        assert server._sid_ssl_payload(_minimal_sid()) is None
    finally:
        server.SONGLENGTHS.clear()
        server.SONGLENGTHS.update(previous)


def test_sid_upload_rejects_songlength_payloads_above_firmware_limit():
    rest = ultimate.UltimateREST.__new__(ultimate.UltimateREST)
    rest.coordinator = None
    rest.client = object()
    with pytest.raises(ValueError, match="exceeds 512 bytes"):
        rest.post_sid("Tune.sid", b"SID", songlengths=bytes(513))


def test_local_sid_upload_uses_enhanced_sidplay_path(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_post_sid_upload",
                        lambda name, data, songnr=None: calls.append((name, data, songnr)) or {"ok": True})
    monkeypatch.setattr(server, "_run_cart_safe", lambda action: action())
    upload = UploadFile(filename="Local.sid", file=io.BytesIO(b"SID"))
    result = asyncio.run(server.run_upload(upload))
    assert result == {"ok": True}
    assert calls == [("Local.sid", b"SID", None)]


def test_assembly64_sid_deploy_uses_enhanced_sidplay_path(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_post_sid_upload",
                        lambda name, data, songnr=None: calls.append((name, data, songnr)) or {"ok": True})
    monkeypatch.setattr(server, "_run_cart_safe", lambda action: action())
    result = server._asm_deploy_bytes("Assembly.sid", "run", b"SID")
    assert result == {"ok": True}
    assert calls == [("Assembly.sid", b"SID", None)]


def test_quick_launch_sid_uses_enhanced_sidplay_path(tmp_path: Path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    (library / "Favourite.sid").write_bytes(b"SID")
    calls = []
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "_post_sid_upload",
                        lambda name, data, songnr=None: calls.append((name, data, songnr)) or {"ok": True})
    monkeypatch.setattr(server, "_run_cart_safe", lambda action: action())
    result = server.library_run("Favourite.sid")
    assert result == {"ok": True}
    assert calls == [("Favourite.sid", b"SID", None)]


def test_sidplay_helper_forwards_compact_ssl_and_subtune_then_falls_back():
    calls = []

    class FakeRest:
        def post_sid(self, name, data, **kwargs):
            calls.append((name, data, kwargs))
            return {"errors": []}

    sid = _minimal_sid(songs=2)
    digest = server.hashlib.md5(sid).hexdigest()
    previous_rest = server.rest
    previous_lengths = dict(server.SONGLENGTHS)
    try:
        server.rest = FakeRest()
        server.SONGLENGTHS.clear()
        server.SONGLENGTHS[digest] = [10, 75]
        server._post_sid_upload("Tune.sid", sid, songnr=2)
        server.SONGLENGTHS.clear()
        server._post_sid_upload("Loose.sid", sid)
    finally:
        server.rest = previous_rest
        server.SONGLENGTHS.clear()
        server.SONGLENGTHS.update(previous_lengths)

    assert calls == [
        ("Tune.sid", sid, {
            "songlengths": bytes([0x00, 0x10, 0x01, 0x15]),
            "songlengths_filename": "Tune.ssl",
            "songnr": 2,
        }),
        ("Loose.sid", sid, {
            "songlengths": None,
            "songlengths_filename": "Loose.ssl",
        }),
    ]


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

        def fake_send(*, delay=0.0, retry_window=0.0, cartridge_state=None):
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
    assert server.VERSION == "1.9.0"
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    changelog = (Path(server.ROOT) / "CHANGELOG.md").read_text(encoding="utf-8")
    assert 'id="ver"' in html
    assert '"/api/app_config"' in js
    assert "## 1.9.0 — Release Candidate 10" in changelog.split("## 1.9.0 — Release Candidate 5", 1)[0]


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
    assert ".brand-name{font-size:16px" in css
    assert ".brand-version{color:var(--dim);font-size:15px;font-weight:600" in css
    assert "#devinfo{color:var(--dim);font-size:15px;font-weight:600" in css
    assert ".ready-line{color:var(--dim);font-size:13px" in css


def test_mount_run_busy_status_is_local_and_does_not_touch_device(monkeypatch):
    calls = []

    class Snapshot:
        active_priority = "interactive"
        active_reason = "mounting and booting disk"

    class FakeCoordinator:
        def snapshot(self): return Snapshot()
        @contextmanager
        def operation(self, *args, **kwargs):
            pytest.fail("busy /api/info must not wait on the device coordinator")
            yield

    class FakeRest:
        def info(self):
            calls.append("info")
            pytest.fail("busy /api/info must not query the Ultimate")

    original_cfg = dict(server.CFG)
    original_mounts = {key: dict(value) for key, value in server.MOUNT_STATE.items()}
    try:
        server.CFG["u64_host"] = "192.0.2.64"
        server.MOUNT_STATE["a"] = {"mode": "unlinked", "path": "/USB0/Demo.d64",
                                   "name": "Demo.d64", "mounted_at": 1.0}
        server.MOUNT_STATE["b"] = {}
        monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
        monkeypatch.setattr(server, "rest", FakeRest())
        payload = server.info()
        assert payload == {
            "u64deck_busy": True,
            "u64deck_busy_reason": "mount_run",
            "u64deck_busy_label": "BUSY — loading program…",
            "u64deck_retry_ms": 2000,
            "u64deck_mounts": {
                "a": {"mode": "unlinked", "path": "/USB0/Demo.d64",
                      "name": "Demo.d64", "mounted_at": 1.0},
                "b": {},
            },
        }
        assert calls == []
    finally:
        server.CFG.clear(); server.CFG.update(original_cfg)
        server.MOUNT_STATE.clear(); server.MOUNT_STATE.update(original_mounts)


def test_drive_status_uses_confirmed_mount_snapshot_while_mount_run_is_busy(monkeypatch):
    calls = []

    class Snapshot:
        active_priority = "interactive"
        active_reason = "mounting and booting disk"

    class FakeCoordinator:
        def snapshot(self): return Snapshot()

    class FakeRest:
        def get_json(self, path):
            calls.append(path)
            pytest.fail("busy /api/drives must not query the Ultimate")

    original_mounts = {key: dict(value) for key, value in server.MOUNT_STATE.items()}
    try:
        server.MOUNT_STATE["a"] = {"mode": "readonly", "name": "Upload.d64",
                                   "path": "", "mounted_at": 2.0}
        server.MOUNT_STATE["b"] = {}
        monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
        monkeypatch.setattr(server, "rest", FakeRest())
        payload = server.drives()
        assert payload["u64deck_busy"] is True
        assert payload["u64deck_mounts"]["a"]["name"] == "Upload.d64"
        assert payload["u64deck_mounts"]["a"]["mode"] == "readonly"
        assert calls == []
    finally:
        server.MOUNT_STATE.clear(); server.MOUNT_STATE.update(original_mounts)


def test_drive_status_waits_for_backend_switch_before_capturing_rest(monkeypatch):
    coordinator = server.DeviceOperationCoordinator()
    switch_active = threading.Event()
    release_switch = threading.Event()
    old_calls = []
    result = {}

    class OldRest:
        def get_json(self, path):
            old_calls.append(path)
            raise RuntimeError("Ultimate REST client is closed")
        def close(self): pass

    class NewRest:
        def get_json(self, path):
            return {"drives": [{"a": {"enabled": True, "image_file": "Fresh.d64"}}]}

    original_mounts = {key: dict(value) for key, value in server.MOUNT_STATE.items()}
    old_rest = OldRest()
    new_rest = NewRest()
    try:
        server.MOUNT_STATE["a"] = {}
        server.MOUNT_STATE["b"] = {}
        monkeypatch.setattr(server, "DEVICE_OP", coordinator)
        monkeypatch.setattr(server, "rest", old_rest)
        monkeypatch.setattr(server, "_reconcile_swap_from_drives", lambda out: {})

        def switch_backend():
            with coordinator.operation("interactive", "switching Ultimate device"):
                switch_active.set()
                assert release_switch.wait(2.0)
                monkeypatch.setattr(server, "rest", new_rest)
                old_rest.close()

        switcher = threading.Thread(target=switch_backend, daemon=True)
        switcher.start()
        assert switch_active.wait(1.0)

        poller = threading.Thread(target=lambda: result.setdefault("payload", server.drives()), daemon=True)
        poller.start()
        time.sleep(0.05)
        assert old_calls == []
        release_switch.set()
        switcher.join(1.0)
        poller.join(1.0)

        assert result["payload"]["u64deck_operation_busy"] is True
        assert result["payload"]["u64deck_drives_unavailable"] is True
        assert "switching Ultimate device" in result["payload"]["u64deck_drives_message"]
        assert old_calls == []
    finally:
        server.MOUNT_STATE.clear(); server.MOUNT_STATE.update(original_mounts)


def test_drive_status_retries_current_backend_after_closed_client_handover(monkeypatch):
    calls = []

    class FakeCoordinator:
        def snapshot(self):
            class Snapshot:
                active_priority = None
                active_reason = ""
            return Snapshot()
        @contextmanager
        def operation(self, *args, **kwargs):
            yield

    class NewRest:
        def get_json(self, path):
            calls.append(("new", path))
            return {"drives": []}

    new_rest = NewRest()

    class OldRest:
        def get_json(self, path):
            calls.append(("old", path))
            monkeypatch.setattr(server, "rest", new_rest)
            raise RuntimeError("Ultimate REST client is closed")

    monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
    monkeypatch.setattr(server, "_DRIVE_STATUS_READY_AT", server.time.time())
    monkeypatch.setattr(server, "rest", OldRest())
    monkeypatch.setattr(server, "_reconcile_swap_from_drives", lambda out: {})
    payload = server.drives()

    assert payload["drives"] == []
    assert calls == [("old", "/v1/drives"), ("new", "/v1/drives")]


def test_drive_status_returns_controlled_snapshot_when_client_is_closed(monkeypatch):
    class FakeCoordinator:
        def snapshot(self):
            class Snapshot:
                active_priority = None
                active_reason = ""
            return Snapshot()
        @contextmanager
        def operation(self, *args, **kwargs):
            yield

    class ClosedRest:
        def get_json(self, path):
            raise RuntimeError("Ultimate REST client is closed")

    original_mounts = {key: dict(value) for key, value in server.MOUNT_STATE.items()}
    try:
        server.MOUNT_STATE["a"] = {"name": "Known.d64", "mode": "unlinked"}
        server.MOUNT_STATE["b"] = {}
        monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
        monkeypatch.setattr(server, "_DRIVE_STATUS_READY_AT", server.time.time())
        monkeypatch.setattr(server, "rest", ClosedRest())
        payload = server.drives()

        assert payload["u64deck_drives_unavailable"] is True
        assert payload["u64deck_retry_ms"] == 1000
        assert payload["u64deck_mounts"]["a"]["name"] == "Known.d64"
        assert "temporarily unavailable" in payload["u64deck_drives_message"]
    finally:
        server.MOUNT_STATE.clear(); server.MOUNT_STATE.update(original_mounts)


def test_frontend_handles_transient_drive_handover_without_error_text():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert "u64deck_drives_unavailable" in js
    assert "DRIVE_STATUS_RETRY_TIMER" in js
    assert "Drive status temporarily unavailable — retrying…" in js


def test_frontend_renders_expected_mount_run_busy_state_and_retries():
    static = Path(server.ASSETS) / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    assert "if(i?.u64deck_busy)" in js
    assert 'i.u64deck_busy_label||"BUSY — loading program…"' in js
    assert "u64deck_retry_ms" in js
    assert "applyBusyMountSnapshot(i.u64deck_mounts)" in js
    assert "Loading program — device status will refresh when complete." in js
    assert 'class="device-busy"' in js
    assert "#devinfo .device-busy{color:var(--warn)}" in css
    assert ".drive-busy-note{color:var(--warn)}" in css


def test_all_mount_run_frontend_paths_refresh_status_when_finished():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    device = js.split("async function mountRunDevice(path){", 1)[1].split(
        "async function localMountRun()", 1
    )[0]
    upload = js.split("async function localMountRun(){", 1)[1].split(
        "async function mountDevice", 1
    )[0]
    assembly = js.split("async function asmDeploy(item,filename,action){", 1)[1].split(
        "/* ---------- disk swap ---------- */", 1
    )[0]
    assert "beginMountRunStatusWatch()" in device
    assert "finally{finishMountRunStatusWatch()}" in device
    assert "beginMountRunStatusWatch()" in upload
    assert "finally{finishMountRunStatusWatch()}" in upload
    assert 'if(action==="mount_run")beginMountRunStatusWatch()' in assembly
    assert 'finally{if(action==="mount_run")finishMountRunStatusWatch()}' in assembly



def test_experimental_telnet_remote_is_removed_and_matrix_keyboard_is_present():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="kbTarget"' not in html
    assert "/api/menu_remote" not in js
    assert "Ultimate Menu (experimental)" not in html + js
    assert '"/api/keys"' in js
    assert '"/api/input/events"' in js
    assert "CIA1 matrix input active" in js
    assert not hasattr(ultimate, "UltimateTelnet")
    assert not hasattr(ultimate, "VT100Screen")


# --- v1.9.0 Release Candidate 20: expanded readable health telemetry ---

def test_rc20_health_dashboard_groups_extended_metrics_and_status_colours():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    for marker in (
        'id="healthSummaryBadge"', 'id="healthRestSuccess"',
        'id="healthVideoLongestGap"', 'id="healthAudioQueue"',
        'id="healthQueueWaitStats"', 'id="healthIndexRates"',
        'id="healthImageCacheHits"', 'id="healthOperationHistory"',
        'id="healthStreamHistory"', 'id="healthLifecycleHistory"',
    ):
        assert marker in html
    assert 'api("/api/health/browser"' in js
    assert 'audioUnderruns' in js
    assert 'video_render_fps' in js
    assert '.health-summary.state-healthy' in css
    assert '.health-summary.state-degraded' in css
    assert '.health-summary.state-attention' in css
    assert '.health-details' in css


def test_rc20_rest_reliability_tracks_failures_streak_and_p95():
    previous = (
        list(server._HEALTH_REST_LATENCIES), server._HEALTH_REST_SUCCESSES,
        server._HEALTH_REST_FAILURES, server._HEALTH_REST_CONSECUTIVE_FAILURES,
        dict(server._HEALTH_LAST_DEVICE), server._HEALTH_LAST_DEVICE_AT,
        server._HEALTH_LAST_INFO_ERROR, server._HEALTH_LAST_INFO_ERROR_AT,
    )
    try:
        server._HEALTH_REST_LATENCIES.clear()
        server._HEALTH_REST_SUCCESSES = 0
        server._HEALTH_REST_FAILURES = 0
        server._HEALTH_REST_CONSECUTIVE_FAILURES = 0
        for value in (10, 20, 30, 40, 200):
            server._health_record_info({"product": "Ultimate 64"}, value)
        server._health_record_info_error(RuntimeError("timeout"), 2500)
        server._health_record_info_error(RuntimeError("timeout"), 2500)
        snap = server._health_latency_snapshot()
        assert snap["p95_ms"] == 200.0
        assert snap["successes"] == 5
        assert snap["failures"] == 2
        assert snap["consecutive_failures"] == 2
        assert snap["success_rate_percent"] == round(5 / 7 * 100, 2)
    finally:
        samples, successes, failures, consecutive, device, device_at, error, error_at = previous
        server._HEALTH_REST_LATENCIES.clear(); server._HEALTH_REST_LATENCIES.extend(samples)
        server._HEALTH_REST_SUCCESSES = successes
        server._HEALTH_REST_FAILURES = failures
        server._HEALTH_REST_CONSECUTIVE_FAILURES = consecutive
        server._HEALTH_LAST_DEVICE.clear(); server._HEALTH_LAST_DEVICE.update(device)
        server._HEALTH_LAST_DEVICE_AT = device_at
        server._HEALTH_LAST_INFO_ERROR = error
        server._HEALTH_LAST_INFO_ERROR_AT = error_at


def test_rc20_coordinator_exposes_wait_distribution_and_recent_history():
    from device_coordinator import DeviceOperationCoordinator
    coordinator = DeviceOperationCoordinator()
    with coordinator.operation("interactive", "first"):
        pass
    with coordinator.operation("status", "second"):
        pass
    snap = coordinator.snapshot()
    assert snap.completed == 2
    assert snap.completed_interactive == 1
    assert snap.completed_status == 1
    assert snap.p95_wait_seconds >= 0
    history = coordinator.recent_operations(10)
    assert [row["reason"] for row in history] == ["first", "second"]
    assert all(row["outcome"] == "ok" for row in history)


def test_rc20_browser_telemetry_report_is_local_only_and_in_health_snapshot():
    previous = dict(server._HEALTH_BROWSER_TELEMETRY)
    previous_at = server._HEALTH_BROWSER_TELEMETRY_AT
    try:
        request = type("Request", (), {"client": type("Client", (), {"host": "127.0.0.1"})()})()
        result = server.health_browser_report(request, {
            "video_render_fps": 49.9, "audio_queue_ms": 82,
            "audio_underruns": 1, "ignored": "nope",
        })
        assert result == {"ok": True}
        assert server._HEALTH_BROWSER_TELEMETRY["video_render_fps"] == 49.9
        assert server._HEALTH_BROWSER_TELEMETRY["audio_queue_ms"] == 82
        assert "ignored" not in server._HEALTH_BROWSER_TELEMETRY
        assert server._health_stream_snapshot()["browser"]["audio_underruns"] == 1
    finally:
        server._HEALTH_BROWSER_TELEMETRY.clear(); server._HEALTH_BROWSER_TELEMETRY.update(previous)
        server._HEALTH_BROWSER_TELEMETRY_AT = previous_at


def test_rc20_cache_snapshot_tracks_image_and_sid_cache_contract():
    previous = dict(server._HEALTH_CACHE_STATS)
    previous_cache = server.IMAGE_CACHE.copy()
    try:
        server._HEALTH_CACHE_STATS.update({
            "image_hits": 3, "image_misses": 1, "image_evictions": 2,
            "sid_materialise_hits": 4, "sid_materialise_misses": 1,
        })
        server.IMAGE_CACHE.clear()
        server.IMAGE_CACHE["abc"] = {"name": "demo.d64", "data": b"1234", "ts": server.time.time()}
        snap = server._health_cache_snapshot()
        assert snap["image_entries"] == 1
        assert snap["image_bytes"] == 4
        assert snap["image_hit_rate_percent"] == 75.0
        assert snap["sid_hit_rate_percent"] == 80.0
    finally:
        server._HEALTH_CACHE_STATS.clear(); server._HEALTH_CACHE_STATS.update(previous)
        server.IMAGE_CACHE.clear(); server.IMAGE_CACHE.update(previous_cache)


# --- v1.9.0 Release Candidate 21: robust readable health status ---

def test_rc21_health_ignores_recovered_diagnostic_history_but_flags_unresolved_errors():
    previous_events = list(server.DIAG_EVENTS)
    previous_success_at = server._HEALTH_LAST_DEVICE_AT
    now = server.time.time()
    try:
        server.DIAG_EVENTS.clear()
        server.DIAG_EVENTS.append({
            "time": server.datetime.fromtimestamp(now - 60).isoformat(timespec="seconds"),
            "level": "error", "message": "transient startup failure",
        })
        server._HEALTH_LAST_DEVICE_AT = now - 10
        recovered = server._health_event_snapshot()
        assert recovered["errors"] == 1
        assert recovered["active_errors"] == 0
        assert recovered["events"][0]["recovered"] is True
        summary = server._health_summary(
            {"configured": True, "last_success_age_seconds": 1, "consecutive_failures": 0},
            {"video": {}, "audio": {}}, {"active_seconds": 0}, recovered,
            {"config": {"last": {"success": True}}},
        )
        assert summary["state"] == "healthy"

        server._HEALTH_LAST_DEVICE_AT = now - 120
        unresolved = server._health_event_snapshot()
        assert unresolved["active_errors"] == 1
        assert unresolved["events"][0]["recovered"] is False
        summary = server._health_summary(
            {"configured": True, "last_success_age_seconds": 1, "consecutive_failures": 0},
            {"video": {}, "audio": {}}, {"active_seconds": 0}, unresolved,
            {"config": {"last": {"success": True}}},
        )
        assert summary["state"] == "degraded"
        assert "recent unresolved diagnostic error" in " ".join(summary["reasons"])
    finally:
        server.DIAG_EVENTS.clear(); server.DIAG_EVENTS.extend(previous_events)
        server._HEALTH_LAST_DEVICE_AT = previous_success_at


def test_rc21_old_stream_gaps_are_history_not_current_degradation():
    base = {"configured": True, "last_success_age_seconds": 1, "consecutive_failures": 0}
    diagnostics = {"active_errors": 0, "active_warnings": 0}
    persistence = {"config": {"last": {"success": True}}}
    old = server._health_summary(
        base,
        {"video": {"gap_events": 5, "dropped": 2, "recent_gap_age_seconds": 61}, "audio": {}},
        {"active_seconds": 0}, diagnostics, persistence,
    )
    assert old["state"] == "healthy"
    recent = server._health_summary(
        base,
        {"video": {"gap_events": 5, "dropped": 2, "recent_gap_age_seconds": 10}, "audio": {}},
        {"active_seconds": 0}, diagnostics, persistence,
    )
    assert recent["state"] == "degraded"
    assert "Recent video packet gap" in recent["reasons"]


def test_rc21_health_ui_auto_refreshes_without_manual_button_and_avoids_duplicate_badges():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    assert 'onclick="healthLoad(true)"' not in html
    assert 'id="healthStreamsState"' in html
    assert 'id="healthVideoState"' not in html
    assert 'id="healthAudioState"' not in html
    assert 'id="healthActivityState"' not in html
    assert 'id="healthIndexState"' not in html
    assert 'id="healthDiagnosticHistory"' in html
    assert 'Auto-updated ' in js and 'every 2 s' in js
    assert '.health-outcome' in css
    assert 'width:max-content' in css
    assert 'justify-content:center' in css


def test_rc21_latest_config_result_not_historical_failure_count_drives_badge():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert 'lastConfig.success===false' in js
    assert 'config.failures?"save errors"' not in js



# --- v1.9.0 Release Candidate 22: ordered drive-status startup ---

def test_rc22_drive_status_waits_for_device_readiness_without_rest_or_diagnostic_error(monkeypatch):
    calls = []

    class FakeCoordinator:
        def snapshot(self):
            class Snapshot:
                active_priority = None
                active_reason = ""
                waiting_interactive = 0
            return Snapshot()
        @contextmanager
        def operation(self, *args, **kwargs):
            yield

    class FakeRest:
        host = "192.0.2.64"
        def get_json(self, path):
            calls.append(path)
            pytest.fail("cold-start drive status must wait for successful device info")

    previous_ready = server._DRIVE_STATUS_READY_AT
    previous_failures = server._DRIVE_STATUS_TRANSIENT_FAILURES
    previous_failure_at = server._DRIVE_STATUS_LAST_FAILURE_AT
    previous_events = list(server.DIAG_EVENTS)
    try:
        server._drive_status_mark_not_ready()
        server.DIAG_EVENTS.clear()
        monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
        monkeypatch.setattr(server, "rest", FakeRest())
        payload = server.drives()
        assert payload["u64deck_drives_waiting"] is True
        assert payload["u64deck_drives_error"] is False
        assert payload["u64deck_retry_ms"] == 750
        assert "Waiting for the Ultimate connection" in payload["u64deck_drives_message"]
        assert calls == []
        assert list(server.DIAG_EVENTS) == []
    finally:
        server._DRIVE_STATUS_READY_AT = previous_ready
        server._DRIVE_STATUS_TRANSIENT_FAILURES = previous_failures
        server._DRIVE_STATUS_LAST_FAILURE_AT = previous_failure_at
        server.DIAG_EVENTS.clear(); server.DIAG_EVENTS.extend(previous_events)


def test_rc22_transient_drive_refusal_is_bounded_before_becoming_diagnostic_error(monkeypatch):
    class FakeCoordinator:
        def snapshot(self):
            class Snapshot:
                active_priority = None
                active_reason = ""
                waiting_interactive = 0
            return Snapshot()
        @contextmanager
        def operation(self, *args, **kwargs):
            yield

    class RefusedRest:
        host = "192.0.2.64"
        def get_json(self, path):
            raise RuntimeError("[WinError 10061] connection actively refused")

    previous_ready = server._DRIVE_STATUS_READY_AT
    previous_failures = server._DRIVE_STATUS_TRANSIENT_FAILURES
    previous_failure_at = server._DRIVE_STATUS_LAST_FAILURE_AT
    previous_events = list(server.DIAG_EVENTS)
    try:
        server._drive_status_mark_ready()
        server.DIAG_EVENTS.clear()
        monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
        monkeypatch.setattr(server, "rest", RefusedRest())
        for attempt in range(1, server.DRIVE_STATUS_MAX_TRANSIENT_RETRIES + 1):
            payload = server.drives()
            assert payload["u64deck_drives_waiting"] is True
            assert payload["u64deck_drives_error"] is False
            assert payload["u64deck_drive_attempt"] == attempt
            assert not [e for e in server.DIAG_EVENTS if e.get("level") == "error"]
        terminal = server.drives()
        assert terminal["u64deck_drives_waiting"] is False
        assert terminal["u64deck_drives_error"] is True
        assert terminal["u64deck_retry_ms"] == 0
        errors = [e for e in server.DIAG_EVENTS if e.get("level") == "error"]
        assert len(errors) == 1
        assert "after automatic retries" in errors[0]["message"]
    finally:
        server._DRIVE_STATUS_READY_AT = previous_ready
        server._DRIVE_STATUS_TRANSIENT_FAILURES = previous_failures
        server._DRIVE_STATUS_LAST_FAILURE_AT = previous_failure_at
        server.DIAG_EVENTS.clear(); server.DIAG_EVENTS.extend(previous_events)


def test_rc22_successful_drive_status_resets_transient_failure_streak(monkeypatch):
    class FakeCoordinator:
        def snapshot(self):
            class Snapshot:
                active_priority = None
                active_reason = ""
                waiting_interactive = 0
            return Snapshot()
        @contextmanager
        def operation(self, *args, **kwargs):
            yield

    class HealthyRest:
        host = "192.0.2.64"
        def get_json(self, path):
            return {"drives": []}

    previous_ready = server._DRIVE_STATUS_READY_AT
    previous_failures = server._DRIVE_STATUS_TRANSIENT_FAILURES
    previous_failure_at = server._DRIVE_STATUS_LAST_FAILURE_AT
    try:
        server._drive_status_mark_ready()
        server._DRIVE_STATUS_TRANSIENT_FAILURES = 2
        server._DRIVE_STATUS_LAST_FAILURE_AT = server.time.time()
        monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
        monkeypatch.setattr(server, "rest", HealthyRest())
        monkeypatch.setattr(server, "_reconcile_swap_from_drives", lambda out: {})
        assert server.drives()["drives"] == []
        assert server._DRIVE_STATUS_TRANSIENT_FAILURES == 0
        assert server._DRIVE_STATUS_LAST_FAILURE_AT == 0.0
    finally:
        server._DRIVE_STATUS_READY_AT = previous_ready
        server._DRIVE_STATUS_TRANSIENT_FAILURES = previous_failures
        server._DRIVE_STATUS_LAST_FAILURE_AT = previous_failure_at


def test_rc22_frontend_sequences_info_before_first_drive_request_and_retries_neutrally():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    boot = js[js.index("/* ---------- boot ---------- */"): ]
    assert "qlRefresh();refreshDrives();idxPollStart()" not in boot
    assert 'renderDrivePanel("Waiting for the Ultimate connection before reading drive status…")' in boot
    assert "const firstDriveReady=!DRIVE_STATUS_READY" in js
    assert "if(firstDriveReady)setTimeout(refreshDrives,0)" in js
    assert "if(!DRIVE_STATUS_READY)" in js
    assert "if(!r.u64deck_drives_error&&retry>0)" in js



# --- v1.9.0 Release Candidate 24: SID grace default and empty-queue playlist UI ---

def test_rc24_songlength_auto_advance_uses_configurable_half_second_grace(monkeypatch):
    previous_lengths = dict(server.SONGLENGTHS_BY_PATH)
    previous_grace = server.CFG.get("sid_jukebox_end_grace_secs")
    previous_fade = server.CFG.get("sid_jukebox_browser_fade_enabled")
    try:
        server.SONGLENGTHS_BY_PATH.clear()
        server.SONGLENGTHS_BY_PATH["musicians/a/a.sid"] = [120.0]
        monkeypatch.setattr(server, "_configured_hvsc_root", lambda: "/USB0/HVSC")
        server.CFG["sid_jukebox_end_grace_secs"] = 0.5
        server.CFG["sid_jukebox_browser_fade_enabled"] = False
        item = server._juke_lazy_item("/USB0/HVSC/MUSICIANS/A/A.sid", "A.sid")
        length, grace, delay, source = server._juke_auto_advance_plan(item, 1)
        assert (length, grace, delay, source) == (120.0, 0.5, 120.5, "songlengths")
    finally:
        server.SONGLENGTHS_BY_PATH.clear()
        server.SONGLENGTHS_BY_PATH.update(previous_lengths)
        if previous_grace is None:
            server.CFG.pop("sid_jukebox_end_grace_secs", None)
        else:
            server.CFG["sid_jukebox_end_grace_secs"] = previous_grace
        if previous_fade is None:
            server.CFG.pop("sid_jukebox_browser_fade_enabled", None)
        else:
            server.CFG["sid_jukebox_browser_fade_enabled"] = previous_fade


def test_rc24_juke_play_arms_the_planned_deadline_and_reports_it(monkeypatch):
    previous_juke = dict(server.JUKE)
    previous_cfg = dict(server.CFG)
    captured = {}
    item = {
        "label": "Grace.sid",
        "path": "/HVSC/Grace.sid",
        "data": b"sid-data",
        "meta": {"name": "Grace", "start_song": 1, "songs": 1},
    }

    class FakeTimer:
        def __init__(self, interval, callback, args=()):
            captured.update(interval=interval, callback=callback, args=args, started=False)
        def start(self):
            captured["started"] = True
        def cancel(self):
            captured["cancelled"] = True

    try:
        server.JUKE.clear(); server.JUKE.update({
            "items": [item], "index": -1, "playing": False,
            "shuffle": False, "radio": False, "song": 0, "timer": None,
            "folder": "", "loading": False, "source": "test",
            "generation": 0, "stop_after_current": False,
        })
        server.CFG["sid_jukebox_browser_fade_enabled"] = False
        monkeypatch.setattr(server, "_cart_configured", lambda: "")
        monkeypatch.setattr(server, "_post_sid_upload", lambda *args, **kwargs: {})
        monkeypatch.setattr(server, "_juke_auto_advance_plan",
                            lambda item, song: (120.0, 0.5, 120.5, "songlengths"))
        monkeypatch.setattr(server._threading, "Timer", FakeTimer)
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)

        out = server._juke_play(0)

        assert captured["interval"] == 120.5
        assert captured["callback"] is server._juke_auto_next
        assert captured["args"] == (server.JUKE["generation"],)
        assert captured["started"] is True
        assert out["play_timing"]["duration_source"] == "songlengths"
        assert out["play_timing"]["song_length_secs"] == 120.0
        assert out["play_timing"]["end_grace_secs"] == 0.5
        assert out["play_timing"]["auto_advance_secs"] == 120.5
    finally:
        timer = server.JUKE.get("timer")
        if timer is not None and hasattr(timer, "cancel"):
            timer.cancel()
        server.JUKE.clear(); server.JUKE.update(previous_juke)
        server.CFG.clear(); server.CFG.update(previous_cfg)


def test_rc24_jukebox_end_grace_is_bounded_and_invalid_values_fall_back():
    previous = server.CFG.get("sid_jukebox_end_grace_secs")
    try:
        server.CFG["sid_jukebox_end_grace_secs"] = -9
        assert server._juke_end_grace_seconds() == 0.0
        server.CFG["sid_jukebox_end_grace_secs"] = 99
        assert server._juke_end_grace_seconds() == 30.0
        server.CFG["sid_jukebox_end_grace_secs"] = "not-a-number"
        assert server._juke_end_grace_seconds() == 0.5
    finally:
        if previous is None:
            server.CFG.pop("sid_jukebox_end_grace_secs", None)
        else:
            server.CFG["sid_jukebox_end_grace_secs"] = previous


def test_rc24_unknown_length_retains_existing_fallback_allowance(monkeypatch):
    previous_default = server.CFG.get("sid_default_secs")
    try:
        monkeypatch.setattr(server, "_juke_length", lambda item, song: None)
        server.CFG["sid_default_secs"] = 180
        assert server._juke_auto_advance_plan({}, 1) == (180.0, 1.0, 181.0, "fallback")
        server.CFG["sid_default_secs"] = 0
        assert server._juke_auto_advance_plan({}, 1) == (0.0, 0.0, 0.0, "fallback")
    finally:
        if previous_default is None:
            server.CFG.pop("sid_default_secs", None)
        else:
            server.CFG["sid_default_secs"] = previous_default


def test_rc24_documentation_retains_end_grace_configuration():
    root = Path(server.__file__).parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    help_js = (Path(server.ASSETS) / "static" / "help_content.js").read_text(encoding="utf-8")
    example = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    assert example["sid_jukebox_end_grace_secs"] == 0.5
    assert "sid_jukebox_end_grace_secs" in readme
    assert "browser fade defaults to 2.5 seconds" in readme
    assert "sid_jukebox_end_grace_secs" in help_js


def test_rc24_empty_queue_keeps_saved_playlist_controls_visible():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    assert 'id="jkListPanel" style="display:flex"' in html
    assert 'id="plSel"' in html and 'id="plLoadBtn"' in html
    assert 'No tunes queued' in html
    assert 'lp.style.display="flex"' in js
    assert 'if(!items.length)' in js
    assert 'PLAY QUEUE — 0 TUNES' in js
    assert '.sid-queue-empty{' in css
    assert 'lp.style.display="none"' not in js


def test_rc24_empty_queue_controls_are_context_sensitive():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="plSaveBtn"' in html and 'disabled>💾 Save</button>' in html
    assert 'id="jkClearQueueBtn"' in html and 'disabled>Clear Queue</button>' in html
    assert 'id="plDeleteBtn"' in html and 'disabled>🗑</button>' in html
    assert 'onchange="plControlsSync()"' in html
    assert 'save.disabled=!hasItems' in js
    assert 'clear.disabled=!hasItems' in js
    assert 'del.disabled=!selected' in js
    assert 'plControlsSync(s);' in js
    assert 'plControlsSync();' in js


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
    assert server.VERSION == release.VERSION == "1.9.0"
    assert release.RELEASE_LABEL == "Release Candidate 44"
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
        def post_sid(self, name, data, **kwargs):
            assert name == "test.sid"
            assert kwargs["songnr"] == 1
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
        "any metadata filter can be used by itself",
        "amber 6581",
        "cyan 8580",
        "rescan unchanged files",
    ):
        assert text in help_js
    for text in (
        "clearly labelled **SID Index** button",
        "used alone or combined with the others",
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


def test_release_help_and_now_playing_star_are_present():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    help_js = (static / "help_content.js").read_text(encoding="utf-8")
    readme = (Path(server.ROOT) / "README.md").read_text(encoding="utf-8")
    assert 'id="jkNowStar"' in html
    assert "jkToggleNowFavourite" in js
    assert ".u64deck-index.sqlite3" in help_js
    assert "star beside the playback controls" in help_js
    assert "v1.9.0 — Release Candidate 44" in readme
    assert ".u64deck-index.sqlite3" in readme
    assert "Install every release into a **new, empty folder**" in readme
    assert "Do not copy `*-wal`" in readme
    assert "user_items.json" in readme and "playlists.json" in readme


def test_machine_input_capability_probe_caches_supported_and_fallback_paths():
    class FakeClient:
        host = "192.0.2.64"
        def __init__(self, status): self.status = status; self.probes = 0
        def probe_machine_input(self):
            self.probes += 1
            return {"available": self.status == 200, "status": self.status,
                    "state": {} if self.status == 200 else None, "detail": ""}
    server.INPUT_CAPABILITIES.clear()
    supported = FakeClient(200)
    first = server._input_status(supported, force=True)
    second = server._input_status(supported)
    assert first["mode"] == "matrix" and first["available"] is True
    assert second == first and supported.probes == 1
    unsupported = FakeClient(501)
    fallback = server._input_status(unsupported, force=True)
    assert fallback["mode"] == "buffer" and fallback["status"] == 501


def test_machine_input_event_validation_enforces_schema_and_limits():
    valid = server._validate_matrix_events([
        {"kind": "keyboard", "inputs": ["left_shift", "f1"], "transition": "tap"},
        {"kind": "release_all"},
    ])
    assert valid == [
        {"kind": "keyboard", "inputs": ["left_shift", "f1"], "transition": "tap"},
        {"kind": "release_all"},
    ]
    with pytest.raises(ValueError, match="at most 64 events"):
        server._validate_matrix_events([{"kind": "release_all"}] * 65)
    with pytest.raises(ValueError, match="1 to 8 keys"):
        server._validate_matrix_events([{"kind": "keyboard", "inputs": ["a"] * 9,
                                         "transition": "press"}])
    with pytest.raises(ValueError, match="unknown keyboard input"):
        server._validate_matrix_events([{"kind": "keyboard", "inputs": ["escape"],
                                         "transition": "tap"}])
    with pytest.raises(ValueError, match="keyboard or release_all"):
        server._validate_matrix_events([{"kind": "joystick", "port": 2,
                                         "inputs": ["fire"], "transition": "tap"}])


def test_matrix_send_and_release_all_use_firmware_event_payload():
    class FakeClient:
        host = "192.0.2.65"
        def __init__(self): self.sent = []
        def probe_machine_input(self):
            return {"available": True, "status": 200, "state": {}, "detail": ""}
        def machine_input(self, events):
            self.sent.append(events); return {"keyboard": []}
    fake = FakeClient(); server.INPUT_CAPABILITIES.clear()
    result = server._matrix_send([
        {"kind": "keyboard", "inputs": ["left_shift", "cursor_left_right"],
         "transition": "press"}], client=fake, force_probe=True)
    assert result == {"keyboard": []}
    assert server._matrix_release_all(client=fake, silent=False) is True
    assert fake.sent[-1] == [{"kind": "release_all"}]


def test_boot_prekey_uses_matrix_f7_when_capable(monkeypatch):
    previous = dict(server.CFG); sent = []
    try:
        server.CFG["boot_prekey"] = "F7"
        monkeypatch.setattr(server, "_input_status", lambda *a, **k: {"available": True})
        monkeypatch.setattr(server, "_matrix_send", lambda events, **kwargs: sent.append(events) or {})
        monkeypatch.setattr(server.time, "sleep", lambda _delay: None)
        assert server._send_boot_prekey(
            delay=0,
            cartridge_state={"classification": "retro_replay", "label": "Retro Replay"},
        ) == "F7"
        assert sent == [[{"kind": "keyboard", "inputs": ["f7"], "transition": "tap"}]]
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_beta7_frontend_tracks_held_keys_chords_and_release_safety():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "const MATRIX_HELD=new Map()" in js
    assert "if(ev.repeat||MATRIX_HELD.has(id))return" in js
    assert 'transition:"press"' in js and 'transition:"release"' in js
    assert '["left_shift","f1"]' in js
    assert '["left_shift","cursor_left_right"]' in js
    assert 'matrixReleaseAll("screen blur")' in js
    assert 'matrixReleaseAll("tab switch")' in js
    assert 'keepalive:true' in js
    assert "SPACE</button>" in html and "RUN/STOP</button>" in html and "RESTORE</button>" in html


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


def _sidflow_source_db(path: Path, rows: list[tuple], *, with_payload: bool = False) -> None:
    import sqlite3
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE tracks (
                track_id TEXT PRIMARY KEY,
                sid_path TEXT NOT NULL,
                song_index INTEGER NOT NULL,
                vector_json TEXT NULL,
                e REAL NOT NULL,
                m REAL NOT NULL,
                c REAL NOT NULL,
                p REAL NULL,
                likes INTEGER NOT NULL DEFAULT 0,
                dislikes INTEGER NOT NULL DEFAULT 0,
                skips INTEGER NOT NULL DEFAULT 0,
                plays INTEGER NOT NULL DEFAULT 0,
                last_played TEXT NULL,
                classified_at TEXT NULL,
                source TEXT NULL,
                render_engine TEXT NULL,
                feature_schema_version TEXT NULL,
                features_json TEXT NULL
            ) WITHOUT ROWID;
            CREATE TABLE neighbors (
                profile TEXT NOT NULL,
                seed_track_id TEXT NOT NULL,
                neighbor_track_id TEXT NOT NULL,
                rank INTEGER NOT NULL,
                similarity REAL NOT NULL,
                PRIMARY KEY(profile, seed_track_id, rank)
            ) WITHOUT ROWID;
        """)
        import json
        values = []
        for row in rows:
            base = tuple(row[:7])
            if len(row) > 7 and isinstance(row[7], dict):
                features = dict(row[7])
            else:
                features = {
                    "energy": float(base[3]),
                    "dynamicRange": float(base[4]),
                    "bpm": float(base[5]) * 100.0,
                    "pitchSalience": 0.0 if base[6] is None else float(base[6]),
                }
            if with_payload:
                features["unused_payload"] = "x" * 12000
            values.append((*base, json.dumps(features)))
        conn.executemany(
            "INSERT INTO tracks(track_id,sid_path,song_index,e,m,c,p,features_json) "
            "VALUES(?,?,?,?,?,?,?,?)", values,
        )


def _sidflow_manifest(count: int, **extra) -> dict:
    value = {
        "schema_version": "sidcorr-1", "track_count": count,
        "vector_dimensions": 4, "include_vectors": True,
        "neighbor_row_count": 0, "generated_at": "2026-04-07T00:00:00Z",
        "export_profile": "full",
    }
    value.update(extra)
    return value


def _sidflow_080_manifest(count: int, **extra) -> dict:
    value = _sidflow_manifest(
        count,
        vector_dimensions=58,
        neighbor_row_count=max(count, 1),
        feature_schema_version="1.5.0",
        hvsc_version="HVSC 85 + Update 85",
        similarity_metric="weighted-cosine",
        vector_weights=[1.0] * 58,
        file_checksums={
            "sqlite_sha256": "d3d825ae0ed38139802b57cb7e8c2f01fc40bdbb824bf6669e9d2ef51bb176da"
        },
    )
    value.update(extra)
    return value


def test_sidflow_manifest_schema_gate():
    import sidflow_similarity as sf
    assert sf.validate_manifest(_sidflow_manifest(1))["schema_version"] == "sidcorr-1"
    with pytest.raises(ValueError, match="sidcorr-2.*not supported"):
        sf.validate_manifest(_sidflow_manifest(1, schema_version="sidcorr-2"))


def test_sidflow_080_manifest_contract_is_strict():
    import sidflow_similarity as sf
    manifest = sf.validate_pinned_manifest(_sidflow_080_manifest(10, neighbor_row_count=250))
    assert manifest["vector_dimensions"] == 58
    assert manifest["similarity_metric"] == "weighted-cosine"
    with pytest.raises(ValueError, match="58 vector weights"):
        sf.validate_pinned_manifest(_sidflow_080_manifest(10, vector_weights=[1.0] * 57))
    with pytest.raises(ValueError, match="unexpected HVSC"):
        sf.validate_pinned_manifest(_sidflow_080_manifest(10, hvsc_version="HVSC 84"))


def test_sidflow_gzip_streaming_and_size_validation(tmp_path: Path):
    import gzip
    import sidflow_similarity as sf
    raw = (b"sidflow-0.8.0" * 8192) + b"end"
    compressed = tmp_path / "source.sqlite.gz"
    output = tmp_path / "source.sqlite"
    with gzip.open(compressed, "wb") as fh:
        fh.write(raw)
    progress = []
    assert sf.gunzip_file(
        compressed, output, expected_bytes=len(raw),
        progress=lambda stage, current, total: progress.append((stage, current, total)),
    ) == len(raw)
    assert output.read_bytes() == raw
    assert progress and progress[-1] == ("decompressing", len(raw), len(raw))
    bad = tmp_path / "bad.sqlite"
    with pytest.raises(ValueError, match="decompressed size"):
        sf.gunzip_file(compressed, bad, expected_bytes=len(raw) + 1)
    assert not bad.exists()


def test_sidflow_slimming_preserves_rows_bounds_size_and_deletes_source(tmp_path: Path):
    import sidflow_similarity as sf
    source = tmp_path / "full.sqlite"
    live = tmp_path / ".sidflow-similarity.sqlite"
    rows = [
        ("MUSICIANS/A/A.sid#1", "MUSICIANS/A/A.sid", 1, 1.0, 0.0, 0.0, None),
        ("MUSICIANS/B/B.sid#2", "MUSICIANS/B/B.sid", 2, 0.0, 1.0, 0.0, 0.5),
        ("MUSICIANS/C/C.sid#1", "MUSICIANS/C/C.sid", 1, 0.0, 0.0, 1.0, 0.2),
    ]
    _sidflow_source_db(source, rows, with_payload=True)
    source_size = source.stat().st_size
    result = sf.slim_and_promote(source, live, _sidflow_manifest(len(rows)))
    assert result["tracks"] == len(rows)
    assert not source.exists()
    assert live.is_file() and live.stat().st_size < source_size
    store = sf.SimilarityStore(live)
    assert store.status()["tracks"] == len(rows)
    assert store.lookup("musicians/b/b.sid", 2)["track_id"] == rows[1][0]


def test_sidflow_path_normalisation_and_track_id_join():
    import sidflow_similarity as sf
    assert sf.normalise_hvsc_relative(
        r"\USB0\C64Music\MUSICIANS\G\Galway_Martin\Parallax.sid",
        "/usb0/c64music/",
    ) == "MUSICIANS/G/Galway_Martin/Parallax.sid"
    assert sf.normalise_hvsc_relative(
        "/USB0/HVSC/MUSICIANS/A/Tune.sid", "/USB0/Other"
    ) is None
    assert sf.build_track_id("/MUSICIANS/A/Tune.sid", 3) == "MUSICIANS/A/Tune.sid#3"


def test_sidflow_feature_ranking_and_present_filter(tmp_path: Path):
    import sidflow_similarity as sf
    source = tmp_path / "source.sqlite"
    live = tmp_path / "live.sqlite"
    rows = [
        ("A.sid#1", "A.sid", 1, 3.0, 3.0, 3.0, None, {"bpm": 120, "energy": .8, "spectralCentroid": 1000}),
        ("B.sid#1", "B.sid", 1, 3.0, 3.0, 3.0, None, {"bpm": 121, "energy": .79, "spectralCentroid": 1010}),
        ("C.sid#1", "C.sid", 1, 3.0, 3.0, 3.0, None, {"bpm": 80, "energy": .2, "spectralCentroid": 400}),
        ("D.sid#1", "D.sid", 1, 3.0, 3.0, 3.0, None, {"bpm": 82, "energy": .25, "spectralCentroid": 420}),
    ]
    _sidflow_source_db(source, rows)
    sf.slim_and_promote(source, live, _sidflow_manifest(len(rows)))
    store = sf.SimilarityStore(live)
    ranked = store.rank("A.sid#1", limit=3)
    assert ranked[0]["track_id"] == "B.sid#1"
    filtered = store.rank("A.sid#1", limit=3, present_paths={"c.sid", "d.sid"})
    assert {row["track_id"] for row in filtered} == {"C.sid#1", "D.sid#1"}


def test_sidflow_more_like_filters_to_device_and_radio_does_not_repeat(monkeypatch):
    class FakeStore:
        def status(self): return {"available": True, "tracks": 3}
        def lookup(self, rel, song):
            return {"track_id": f"{rel}#{song}", "sid_path": rel, "song_index": song}
        def rank(self, seed, *, limit, present_paths, exclude_track_ids,
                 exclude_sid_paths=(), same_file_policy="prefer_other"):
            excluded = {str(x).casefold() for x in exclude_track_ids}
            candidates = [
                {"track_id": "MUSICIANS/B/B.sid#1", "sid_path": "MUSICIANS/B/B.sid", "song_index": 1, "similarity": .99, "recommendation_source": "sidflow-neighbors"},
                {"track_id": "MUSICIANS/C/C.sid#2", "sid_path": "MUSICIANS/C/C.sid", "song_index": 2, "similarity": .95, "recommendation_source": "sidflow-neighbors"},
            ]
            return [row for row in candidates if row["track_id"].casefold() not in excluded]
    class FakeIndex:
        def sid_metadata_paths(self, root):
            return ["/USB0/HVSC/MUSICIANS/A/A.sid", "/USB0/HVSC/MUSICIANS/B/B.sid", "/USB0/HVSC/MUSICIANS/C/C.sid"]
        def sid_metadata_for_paths(self, paths): return {}

    previous_juke = dict(server.JUKE)
    previous_played = set(server.JUKE_PLAYED)
    previous_recent = list(server.JUKE_RECENT_TRACKS)
    monkeypatch.setattr(server, "SIDFLOW_STORE", FakeStore())
    monkeypatch.setattr(server, "_configured_hvsc_root", lambda: "/USB0/HVSC")
    monkeypatch.setattr(server, "_index_store", lambda: FakeIndex())
    try:
        server.JUKE_PLAYED.clear(); server.JUKE_RECENT_TRACKS.clear()
        server.JUKE.clear(); server.JUKE.update({
            "items": [server._juke_lazy_item("/USB0/HVSC/MUSICIANS/A/A.sid", "A.sid")],
            "index": 0, "playing": True, "shuffle": False, "radio": True,
            "song": 1, "timer": None, "folder": "", "loading": False,
            "source": "", "generation": 0,
        })
        assert server._sidflow_radio_topup() == 2
        assert len(server.JUKE["items"]) == 3
        assert [item.get("song") for item in server.JUKE["items"][1:]] == [1, 2]
        # A second top-up sees both candidate track IDs in the current queue.
        assert server._sidflow_radio_topup() == 0
        assert len(server.JUKE["items"]) == 3
    finally:
        server.JUKE.clear(); server.JUKE.update(previous_juke)
        server.JUKE_PLAYED.clear(); server.JUKE_PLAYED.update(previous_played)
        server.JUKE_RECENT_TRACKS.clear(); server.JUKE_RECENT_TRACKS.extend(previous_recent)


def test_sidflow_absent_and_unmatched_flows_are_graceful(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(server, "SIDFLOW_STORE", __import__("sidflow_similarity").SimilarityStore(tmp_path / "missing.sqlite"))
    with pytest.raises(HTTPException, match="not installed"):
        server._sidflow_recommendations("/USB0/HVSC/A.sid", 1)

    class Available:
        def status(self): return {"available": True}
        def lookup(self, rel, song): return None
    monkeypatch.setattr(server, "SIDFLOW_STORE", Available())
    monkeypatch.setattr(server, "_configured_hvsc_root", lambda: "/USB0/HVSC")
    with pytest.raises(HTTPException, match="not present in the SIDFlow export"):
        server._sidflow_recommendations("/USB0/HVSC/MUSICIANS/A/Missing.sid", 1)


def test_sidflow_ui_attribution_docs_and_archive_hygiene_are_present():
    root = Path(server.ROOT)
    html = (Path(server.ASSETS) / "static" / "index.html").read_text(encoding="utf-8")
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    help_js = (Path(server.ASSETS) / "static" / "help_content.js").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert html.count("Powered by SIDFlow (Chris Gleissner)") >= 2
    assert "♪ More like this" in html and 'id="jkRadio"' in html
    assert "/api/juke/more_like" in js and "/api/juke/radio" in js
    assert "SIDFlow" in help_js and "Chris Gleissner" in help_js
    assert "SIDFlow" in readme and "Chris Gleissner" in readme
    assert ".sidflow-*" in gitignore
    assert not list(root.glob(".sidflow-*"))


def test_sidflow_asset_plan_pins_080_compressed_full_export():
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {
                "tag_name": "0.8.0",
                "published_at": "2026-07-27T20:52:00Z",
                "assets": [
                    {"name": "sidcorr-hvsc-full-sidcorr-1.sqlite.gz", "browser_download_url": "https://example/full.sqlite.gz", "size": 194351886},
                    {"name": "sidcorr-hvsc-full-sidcorr-1.manifest.json", "browser_download_url": "https://example/full.json", "size": 1234},
                    {"name": "SHA256SUMS", "browser_download_url": "https://example/SHA256SUMS", "size": 999},
                ],
            }
    class Client:
        def get(self, url): return Response()

    plan = server._sidflow_asset_plan(Client())
    assert plan["tag"] == "0.8.0"
    assert plan["profile"] == "full"
    assert plan["gzip_name"] == "sidcorr-hvsc-full-sidcorr-1.sqlite.gz"
    assert plan["gzip_url"] == "https://example/full.sqlite.gz"
    assert plan["manifest_url"] == "https://example/full.json"
    assert plan["checksums_url"] == "https://example/SHA256SUMS"
    assert plan["gzip_sha256"] == "7eeddad82666fbf75ce93d0d412a7b31797475bee1b77c960542c7edb7b2fcf0"
    assert plan["sqlite_sha256"] == "d3d825ae0ed38139802b57cb7e8c2f01fc40bdbb824bf6669e9d2ef51bb176da"


def test_sidflow_disk_preflight_blocks_before_download(monkeypatch):
    plan = {"gzip_bytes": 200, "sqlite_bytes": 1000}
    class Usage:
        free = 100
    monkeypatch.setattr(server.shutil, "disk_usage", lambda path: Usage())
    with pytest.raises(ValueError, match="Not enough free disk space"):
        server._sidflow_check_disk_space(plan)




def test_sidflow_slimming_retains_and_prefers_future_neighbors(tmp_path: Path):
    import sqlite3
    import sidflow_similarity as sf

    source = tmp_path / "source.sqlite"
    live = tmp_path / "live.sqlite"
    rows = [
        ("A.sid#1", "A.sid", 1, 1.0, 0.0, 0.0, None),
        ("B.sid#1", "B.sid", 1, 0.99, 0.01, 0.0, None),
        ("C.sid#1", "C.sid", 1, 0.0, 1.0, 0.0, None),
    ]
    _sidflow_source_db(source, rows)
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute(
            "INSERT INTO neighbors(profile,seed_track_id,neighbor_track_id,rank,similarity) "
            "VALUES(?,?,?,?,?)",
            ("default", "A.sid#1", "C.sid#1", 1, 0.777),
        )
    result = sf.slim_and_promote(
        source, live, _sidflow_manifest(len(rows), neighbor_row_count=1)
    )
    assert result["neighbors"] == 1
    store = sf.SimilarityStore(live)
    ranked = store.rank("A.sid#1", limit=1)
    assert ranked[0]["track_id"] == "C.sid#1"
    assert ranked[0]["similarity"] == pytest.approx(0.777)
    assert ranked[0]["recommendation_source"] == "sidflow-neighbors"


def test_sidflow_neighbor_graph_prefers_other_files_and_radio_excludes_siblings(tmp_path: Path):
    import sqlite3
    import sidflow_similarity as sf
    source = tmp_path / "source.sqlite"
    live = tmp_path / "live.sqlite"
    rows = [
        ("A.sid#1", "A.sid", 1, 1.0, 0.0, 0.0, None),
        ("A.sid#2", "A.sid", 2, 0.99, 0.0, 0.0, None),
        ("B.sid#1", "B.sid", 1, 0.9, 0.1, 0.0, None),
        ("C.sid#1", "C.sid", 1, 0.8, 0.2, 0.0, None),
    ]
    _sidflow_source_db(source, rows)
    edges = [
        ("default", "A.sid#1", "A.sid#2", 1, .99),
        ("default", "A.sid#1", "B.sid#1", 2, .95),
        ("default", "A.sid#1", "C.sid#1", 3, .90),
    ]
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.executemany(
            "INSERT INTO neighbors(profile,seed_track_id,neighbor_track_id,rank,similarity) VALUES(?,?,?,?,?)",
            edges,
        )
    sf.slim_and_promote(source, live, _sidflow_manifest(len(rows), neighbor_row_count=len(edges)))
    store = sf.SimilarityStore(live)
    preferred = store.rank("A.sid#1", limit=3, same_file_policy="prefer_other")
    assert [row["track_id"] for row in preferred] == ["B.sid#1", "C.sid#1", "A.sid#2"]
    radio = store.rank("A.sid#1", limit=3, same_file_policy="exclude")
    assert [row["track_id"] for row in radio] == ["B.sid#1", "C.sid#1"]


def test_sidflow_fallback_is_explicitly_labelled(tmp_path: Path):
    import sidflow_similarity as sf
    source = tmp_path / "source.sqlite"
    live = tmp_path / "live.sqlite"
    rows = [
        ("A.sid#1", "A.sid", 1, 3, 3, 3, None, {"bpm": 120, "energy": .8}),
        ("B.sid#1", "B.sid", 1, 3, 3, 3, None, {"bpm": 121, "energy": .79}),
    ]
    _sidflow_source_db(source, rows)
    sf.slim_and_promote(source, live, _sidflow_manifest(len(rows)))
    ranked = sf.SimilarityStore(live).rank("A.sid#1", limit=1)
    assert ranked[0]["recommendation_source"] == "u64deck-fallback"


def test_sidflow_080_compact_status_reports_supported_contract(tmp_path: Path):
    import sqlite3
    import sidflow_similarity as sf
    source = tmp_path / "source.sqlite"
    live = tmp_path / "live.sqlite"
    rows = [
        ("A.sid#1", "A.sid", 1, 1.0, 0.0, 0.0, None),
        ("B.sid#1", "B.sid", 1, 0.9, 0.1, 0.0, None),
    ]
    _sidflow_source_db(source, rows)
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.executemany(
            "INSERT INTO neighbors(profile,seed_track_id,neighbor_track_id,rank,similarity) VALUES(?,?,?,?,?)",
            [
                ("default", "A.sid#1", "B.sid#1", 1, .95),
                ("default", "B.sid#1", "A.sid#1", 1, .95),
            ],
        )
    manifest = _sidflow_080_manifest(
        2,
        neighbor_row_count=2,
        u64deck_release_tag="0.8.0",
        u64deck_source_asset=sf.FULL_SQLITE_GZ,
    )
    sf.slim_and_promote(source, live, manifest)
    status = sf.SimilarityStore(live).status()
    assert status["available"] is True
    assert status["needs_update"] is False
    assert status["release_tag"] == "0.8.0"
    assert status["hvsc_version"] == "HVSC 85 + Update 85"
    assert status["similarity_metric"] == "weighted-cosine"
    assert status["vector_dimensions"] == 58
    assert status["neighbors"] == 2
    assert status["recommendation_engine"] == "SIDFlow weighted neighbour graph"


def test_sidflow_recommendations_reject_older_installed_data(monkeypatch):
    class OldStore:
        def status(self):
            return {"available": True, "needs_update": True}
    monkeypatch.setattr(server, "SIDFLOW_STORE", OldStore())
    with pytest.raises(HTTPException, match="0.8.0 data is required"):
        server._sidflow_recommendations("/USB0/HVSC/A.sid", 1)


def test_sidflow_sha256_parser_accepts_common_relative_names():
    import sidflow_similarity as sf
    value = "a" * 64
    assert sf.parse_sha256sums(f"{value}  ./bundle.sqlite\n")["bundle.sqlite"] == value


def test_sidflow_feature_extraction_missing_values_and_real_shape():
    import sidflow_similarity as sf
    payload = {name: index + 0.5 for index, name in enumerate(sf.FEATURE_DIMENSIONS)}
    payload.update({f"unusedRealField{index}": index for index in range(25)})
    payload["energy"] = "not numeric"
    payload["rms"] = None
    vector = sf.extract_feature_vector(payload)
    assert len(vector) == 48
    assert vector[sf.FEATURE_DIMENSIONS.index("energy")] == 0.0
    assert vector[sf.FEATURE_DIMENSIONS.index("rms")] == 0.0
    assert vector[sf.FEATURE_DIMENSIONS.index("bpm")] == pytest.approx(0.5)


def test_sidflow_zscore_l2_pipeline_matches_precomputed_values():
    import sidflow_similarity as sf
    unit = sf.normalise_vector((2.0, 4.0, 8.0), (1.0, 2.0, 2.0), (1.0, 2.0, 3.0))
    expected_raw = (1.0, 1.0, 2.0)
    norm = sum(value * value for value in expected_raw) ** 0.5
    assert unit == pytest.approx(tuple(value / norm for value in expected_raw))
    assert sum(value * value for value in unit) == pytest.approx(1.0)


def test_sidflow_path_mapping_is_case_insensitive_and_strips_c64music():
    import sidflow_similarity as sf
    assert sf.normalise_hvsc_relative(
        "/usb0/hvsc/C64Music/MUSICIANS/G/Galway_Martin/Parallax.sid",
        "/USB0/HVSC",
    ) == "MUSICIANS/G/Galway_Martin/Parallax.sid"
    assert sf.normalise_hvsc_relative(
        "/USB0/OTHER/C64Music/MUSICIANS/G/Tune.sid", "/USB0/HVSC"
    ) is None


def test_sidflow_vector_schema_meta_and_casefold_lookup(tmp_path: Path):
    import sqlite3
    import sidflow_similarity as sf
    source = tmp_path / "source.sqlite"
    live = tmp_path / "live.sqlite"
    rows = [
        ("MUSICIANS/A/Tune.sid#1", "MUSICIANS/A/Tune.sid", 1, 3, 3, 3, None,
         {"bpm": 100, "energy": .4}),
        ("MUSICIANS/B/Other.sid#1", "MUSICIANS/B/Other.sid", 1, 3, 3, 3, None,
         {"bpm": 140, "energy": .9}),
    ]
    _sidflow_source_db(source, rows)
    sf.slim_and_promote(source, live, _sidflow_manifest(len(rows)))
    with closing(sqlite3.connect(live)) as conn:
        meta = dict(conn.execute("SELECT key,value FROM meta"))
    assert meta["vector_schema_version"] == "u64deck-featvec-1"
    assert len(__import__("json").loads(meta["feature_dimensions_json"])) == 48
    store = sf.SimilarityStore(live)
    assert store.lookup("musicians/a/tUNE.sid", 1)["track_id"] == "MUSICIANS/A/Tune.sid#1"


def test_sidflow_degenerate_feature_data_warns_and_blocks_ranking(tmp_path: Path):
    import sidflow_similarity as sf
    source = tmp_path / "source.sqlite"
    live = tmp_path / "live.sqlite"
    same = {"bpm": 100, "energy": .5}
    rows = [(f"{name}.sid#1", f"{name}.sid", 1, 3, 3, 3, None, same)
            for name in ("A", "B", "C")]
    _sidflow_source_db(source, rows)
    sf.slim_and_promote(source, live, _sidflow_manifest(len(rows)))
    store = sf.SimilarityStore(live)
    status = store.status()
    assert status["available"] is True
    assert "degenerate" in status["quality_warning"]
    with pytest.raises(ValueError, match="degenerate"):
        store.rank("A.sid#1")


def test_sidflow_windows_locked_build_uses_validated_ready_copy(monkeypatch, tmp_path: Path):
    import os
    import sidflow_similarity as sf
    build = tmp_path / "db.building"
    live = tmp_path / "db.sqlite"
    with closing(__import__("sqlite3").connect(build)) as conn, conn:
        conn.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
            INSERT INTO meta VALUES('vector_schema_version','u64deck-featvec-1');
        """)
    real_replace = os.replace
    calls = []
    def flaky(source, destination):
        calls.append(str(source))
        if str(source).endswith(".building"):
            raise PermissionError(5, "Access is denied", str(source), str(destination))
        return real_replace(source, destination)
    monkeypatch.setattr(sf.os, "replace", flaky)
    sf.atomic_replace_database(build, live, attempts=2)
    assert live.is_file()
    assert any(".ready-" in value for value in calls)







def test_sidflow_promotion_uses_sqlite_backup_when_all_renames_are_blocked(monkeypatch, tmp_path: Path):
    import sidflow_similarity as sf
    build = tmp_path / "db.building"
    live = tmp_path / "db.sqlite"
    with closing(__import__("sqlite3").connect(build)) as conn, conn:
        conn.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
            INSERT INTO meta VALUES('vector_schema_version','u64deck-featvec-1');
            CREATE TABLE tracks(track_id TEXT PRIMARY KEY, sid_path TEXT, sid_path_key TEXT,
              song_index INTEGER, e REAL, m REAL, c REAL, p REAL, feature_vector BLOB) WITHOUT ROWID;
            CREATE TABLE neighbors(profile TEXT,seed_track_id TEXT,neighbor_track_id TEXT,
              rank INTEGER,similarity REAL,PRIMARY KEY(profile,seed_track_id,rank)) WITHOUT ROWID;
        """)
    def blocked(*args, **kwargs):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(sf.os, "replace", blocked)
    sf.atomic_replace_database(build, live, attempts=1)
    assert live.is_file()
    sf._validate_compact_database(live)


def test_sidflow_sequential_imports_use_unique_build_names(monkeypatch, tmp_path: Path):
    import sidflow_similarity as sf
    seen = []
    original = sf.slim_database

    def recording(source, destination, manifest, progress=None):
        seen.append(Path(destination).name)
        return original(source, destination, manifest, progress)

    monkeypatch.setattr(sf, "slim_database", recording)
    live = tmp_path / ".sidflow-similarity.sqlite"
    rows = [
        ("A.sid#1", "A.sid", 1, 1, 2, 3, None, {"bpm": 100, "energy": .2}),
        ("B.sid#1", "B.sid", 1, 2, 3, 4, None, {"bpm": 140, "energy": .8}),
    ]
    for index in range(2):
        source = tmp_path / f"source-{index}.sqlite"
        _sidflow_source_db(source, rows)
        sf.slim_and_promote(source, live, _sidflow_manifest(len(rows)))
    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert all(".building-" in name for name in seen)
    assert not list(tmp_path.glob("*.building-*"))


def test_sidflow_stale_fixed_build_file_does_not_block_new_import(tmp_path: Path):
    import sidflow_similarity as sf
    live = tmp_path / ".sidflow-similarity.sqlite"
    stale = live.with_name(live.name + ".building-deadbeef")
    stale.write_bytes(b"locked legacy artifact")
    source = tmp_path / "source.sqlite"
    rows = [
        ("A.sid#1", "A.sid", 1, 1, 2, 3, None, {"bpm": 100, "energy": .2}),
        ("B.sid#1", "B.sid", 1, 2, 3, 4, None, {"bpm": 140, "energy": .8}),
    ]
    _sidflow_source_db(source, rows)
    with stale.open("rb") as held:
        assert held.read(1) == b"l"
        sf.slim_and_promote(source, live, _sidflow_manifest(len(rows)))
    assert live.is_file()
    assert stale.is_file()


def test_sidflow_connections_are_balanced_after_status_warm_rank_and_validate(monkeypatch, tmp_path: Path):
    import sidflow_similarity as sf
    source = tmp_path / "source.sqlite"
    live = tmp_path / "live.sqlite"
    rows = [
        ("A.sid#1", "A.sid", 1, 1, 2, 3, None, {"bpm": 100, "energy": .2}),
        ("B.sid#1", "B.sid", 1, 2, 3, 4, None, {"bpm": 140, "energy": .8}),
        ("C.sid#1", "C.sid", 1, 3, 4, 5, None, {"bpm": 180, "energy": .5}),
    ]
    _sidflow_source_db(source, rows)
    sf.slim_and_promote(source, live, _sidflow_manifest(len(rows)))

    real_connect = sf.sqlite3.connect
    balance = {"opened": 0, "closed": 0}

    class TrackedConnection:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)
            object.__setattr__(self, "_closed", False)
            balance["opened"] += 1
        def __getattr__(self, name):
            return getattr(self._conn, name)
        def __setattr__(self, name, value):
            if name.startswith("_"):
                object.__setattr__(self, name, value)
            else:
                setattr(self._conn, name, value)
        def close(self):
            if not self._closed:
                self._conn.close()
                object.__setattr__(self, "_closed", True)
                balance["closed"] += 1

    monkeypatch.setattr(sf.sqlite3, "connect", lambda *a, **k: TrackedConnection(real_connect(*a, **k)))
    store = sf.SimilarityStore(live)
    assert store.status()["available"] is True
    assert store.warm() == len(rows)
    assert store.rank("A.sid#1", limit=1)
    sf._validate_compact_database(live)
    assert balance["opened"] == balance["closed"]


def test_sidflow_redownload_replaces_live_after_repeated_status_calls(tmp_path: Path):
    import sidflow_similarity as sf
    live = tmp_path / "live.sqlite"
    rows1 = [
        ("A.sid#1", "A.sid", 1, 1, 2, 3, None, {"bpm": 100, "energy": .2}),
        ("B.sid#1", "B.sid", 1, 2, 3, 4, None, {"bpm": 140, "energy": .8}),
    ]
    source1 = tmp_path / "source1.sqlite"
    _sidflow_source_db(source1, rows1)
    sf.slim_and_promote(source1, live, _sidflow_manifest(len(rows1)))
    store = sf.SimilarityStore(live)
    for _ in range(5):
        assert store.status()["available"] is True

    rows2 = [
        ("C.sid#1", "C.sid", 1, 1, 3, 5, None, {"bpm": 90, "energy": .3}),
        ("D.sid#1", "D.sid", 1, 2, 4, 6, None, {"bpm": 160, "energy": .9}),
    ]
    source2 = tmp_path / "source2.sqlite"
    _sidflow_source_db(source2, rows2)
    sf.slim_and_promote(source2, live, _sidflow_manifest(len(rows2)))
    fresh = sf.SimilarityStore(live)
    assert fresh.lookup("C.sid", 1)["track_id"] == "C.sid#1"
    assert fresh.lookup("A.sid", 1) is None


def test_sidflow_ready_fallback_replaces_existing_live_after_status_calls(monkeypatch, tmp_path: Path):
    import sidflow_similarity as sf
    live = tmp_path / "live.sqlite"
    first_source = tmp_path / "first.sqlite"
    first_rows = [
        ("A.sid#1", "A.sid", 1, 1, 2, 3, None, {"bpm": 100, "energy": .2}),
        ("B.sid#1", "B.sid", 1, 2, 3, 4, None, {"bpm": 140, "energy": .8}),
    ]
    _sidflow_source_db(first_source, first_rows)
    sf.slim_and_promote(first_source, live, _sidflow_manifest(len(first_rows)))
    store = sf.SimilarityStore(live)
    for _ in range(4):
        assert store.status()["available"] is True
    assert store.warm() == 2

    second_source = tmp_path / "second.sqlite"
    second_rows = [
        ("C.sid#1", "C.sid", 1, 1, 3, 5, None, {"bpm": 90, "energy": .3}),
        ("D.sid#1", "D.sid", 1, 2, 4, 6, None, {"bpm": 160, "energy": .9}),
    ]
    _sidflow_source_db(second_source, second_rows)
    real_replace = sf.os.replace
    blocked_once = {"value": False}

    def force_ready_path(source, destination):
        if ".building-" in Path(source).name and not blocked_once["value"]:
            blocked_once["value"] = True
            raise PermissionError(5, "Access is denied", str(source), str(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(sf.os, "replace", force_ready_path)
    sf.slim_and_promote(
        second_source, live, _sidflow_manifest(len(second_rows)),
        promotion_lock=store.file_lock,
    )
    store.invalidate()
    assert store.lookup("C.sid", 1)["track_id"] == "C.sid#1"
    assert store.lookup("A.sid", 1) is None
    assert blocked_once["value"] is True
    assert not list(tmp_path.glob("*.ready-*"))


def test_sidflow_stale_cleanup_warning_is_diagnostic_only(monkeypatch):
    class LockedArtifact:
        name = ".sidflow-similarity.sqlite.building-deadbeef"
        def unlink(self):
            raise PermissionError(5, "Access is denied", self.name)

    previous_error = server.SIDFLOW_JOB.get("error", "")
    previous_warned = set(server._SIDFLOW_STALE_WARNED)
    server.SIDFLOW_JOB["error"] = ""
    server._SIDFLOW_STALE_WARNED.clear()
    before = len(server.DIAG_EVENTS)
    monkeypatch.setattr(server, "_sidflow_stale_artifacts", lambda include_downloads=True: [LockedArtifact()])
    warnings = server._sidflow_cleanup_stale_artifacts()
    warnings_again = server._sidflow_cleanup_stale_artifacts()
    assert warnings and warnings_again and "cleanup deferred" in warnings[0]
    assert server.SIDFLOW_JOB["error"] == ""
    assert len(server.DIAG_EVENTS) == before + 1
    public = server._sidflow_public_status()
    assert public["job"]["error"] == ""
    server.SIDFLOW_JOB["error"] = previous_error
    server._SIDFLOW_STALE_WARNED.clear()
    server._SIDFLOW_STALE_WARNED.update(previous_warned)


def test_sidflow_obsolete_local_vector_schema_requires_redownload(tmp_path: Path):
    import sqlite3
    import sidflow_similarity as sf
    source = tmp_path / "source.sqlite"
    live = tmp_path / "live.sqlite"
    rows = [
        ("A.sid#1", "A.sid", 1, 3, 3, 3, None, {"bpm": 100, "energy": .5}),
        ("B.sid#1", "B.sid", 1, 3, 3, 3, None, {"bpm": 140, "energy": .9}),
    ]
    _sidflow_source_db(source, rows)
    sf.slim_and_promote(source, live, _sidflow_manifest(len(rows)))
    with closing(sqlite3.connect(live)) as conn, conn:
        conn.execute("UPDATE meta SET value='obsolete' WHERE key='vector_schema_version'")
    status = sf.SimilarityStore(live).status()
    assert status["available"] is False
    assert "re-download" in status["error"]



def test_beta104_lazy_queue_lengths_resolve_by_hvsc_path_and_subsong(monkeypatch):
    previous_lengths = dict(server.SONGLENGTHS_BY_PATH)
    try:
        server.SONGLENGTHS_BY_PATH.clear()
        server.SONGLENGTHS_BY_PATH["musicians/h/hubbard_rob/delta.sid"] = [61.0, 142.5]
        monkeypatch.setattr(server, "_configured_hvsc_root", lambda: "/USB0/HVSC")
        item = server._juke_lazy_item(
            "/usb0/hvsc/MUSICIANS/H/Hubbard_Rob/Delta.sid", "Delta.sid",
            {"songs": 2, "start_song": 1, "title": "Delta", "md5": ""},
        )
        assert item["data"] is None and item["meta"]["md5"] == ""
        assert server._juke_length(item, 1) == 61.0
        assert server._juke_length(item, 2) == 142.5
        item["song"] = 2
        state_items = [{
            "label": item["label"], "meta": item["meta"], "path": item["path"],
            "song": item["song"], "similarity": None, "lazy": True,
            "length": server._juke_length(item, item["song"]),
        }]
        assert state_items[0]["length"] == 142.5
    finally:
        server.SONGLENGTHS_BY_PATH.clear()
        server.SONGLENGTHS_BY_PATH.update(previous_lengths)


def test_beta104_queue_uses_full_length_heading_and_stop_ui_path():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'role="columnheader">Length</div>' in js
    assert 'role="columnheader">Len</div>' not in js
    assert 'if(s==null||s==="")return"—"' in js
    assert 'onclick="jkStop()"' in html
    assert 'if(JK.pollBusy||JK.stopPending)return' in js
    assert '"/api/juke/stop"' in js


def test_command_socket_reset_uses_reset_opcode_without_coordinator():
    sent = []
    command = ultimate.CommandSocket.__new__(ultimate.CommandSocket)
    command._send = lambda opcode, payload=b"": sent.append((opcode, payload))
    command.reset()
    assert sent == [(ultimate.CMD_RESET, b"")]


def test_juke_stop_legacy_uses_rest_first_and_skips_command_socket(monkeypatch):
    previous = dict(server.JUKE)
    calls = []
    host = "192.0.2.17"

    class FakeCommand:
        def reset_fresh(self): calls.append("fresh-reset")

    class FakeRest:
        def __init__(self): self.host = host
        def put(self, path, **kwargs):
            calls.append(("rest", path, kwargs))
            return {}

    monkeypatch.setattr(
        server, "_matrix_release_all",
        lambda **kwargs: calls.append(("release", kwargs)),
    )
    monkeypatch.setattr(server, "rest", FakeRest())
    monkeypatch.setitem(server.INPUT_CAPABILITIES, host, {"available": False})
    try:
        server.JUKE.update({"items": [], "index": -1, "playing": True,
                            "stop_after_current": False, "timer": None})
        monkeypatch.setattr(server, "cmd", FakeCommand())
        server.juke_stop()
        assert calls[0][0:2] == ("rest", "/v1/machine:reset")
        assert calls[0][2]["request_timeout"] == 4.0
        assert calls[1] == ("release", {"silent": True, "cached_only": True, "caller": "juke-stop"})
        assert "fresh-reset" not in calls
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)



def test_beta103_individual_sid_play_and_queue_controls_are_explicit():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'onclick="jkClearQueue()"' in html
    assert 'title="Clear queued tunes, turn Radio off and let the current SID finish"' in html
    assert 'async function jkClearQueue()' in js
    assert '"/api/juke/clear"' in js
    assert 'if(!removeCount&&!s.radio)' in js
    assert 'playing as a one-tune queue' in js
    # Individual playback must no longer follow play_path with a hidden folder load.
    play_from = js.split("async function jkPlayFrom(folder,name){", 1)[1].split(
        "async function jkPlay(i,song)", 1
    )[0]
    assert '"/api/juke/play_path"' in play_from
    assert '"/api/juke/folder"' not in play_from
    assert 'title="Play only this tune now"' in js
    assert 'title="Add only this tune to the current play queue' in js


def test_juke_clear_disarms_radio_and_preserves_current_playback():
    class FakeTimer:
        def __init__(self): self.cancelled = False
        def cancel(self): self.cancelled = True

    previous = dict(server.JUKE)
    previous_played = set(server.JUKE_PLAYED)
    previous_recent = list(server.JUKE_RECENT_TRACKS)
    timer = FakeTimer()
    try:
        items = [
            server._juke_lazy_item("/HVSC/A.sid", "A.sid"),
            server._juke_lazy_item("/HVSC/B.sid", "B.sid"),
            server._juke_lazy_item("/HVSC/C.sid", "C.sid"),
        ]
        server.JUKE.clear(); server.JUKE.update({
            "items": items, "index": 1, "playing": True, "shuffle": False,
            "radio": True, "song": 1, "timer": timer, "folder": "composer",
            "loading": False, "source": "test", "generation": 0,
        })
        server.JUKE_PLAYED.clear(); server.JUKE_PLAYED.add("played#1")
        server.JUKE_RECENT_TRACKS.clear(); server.JUKE_RECENT_TRACKS.append("recent#1")
        out = server.juke_clear()
        assert out["cleared"] == 2 and out["kept_current"] is True
        assert [item["label"] for item in server.JUKE["items"]] == ["B.sid"]
        assert server.JUKE["index"] == 0 and server.JUKE["playing"] is True
        assert server.JUKE["radio"] is False and server.JUKE["timer"] is timer
        assert server.JUKE["stop_after_current"] is True
        assert timer.cancelled is False
        assert not server.JUKE_PLAYED and not server.JUKE_RECENT_TRACKS
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)
        server.JUKE_PLAYED.clear(); server.JUKE_PLAYED.update(previous_played)
        server.JUKE_RECENT_TRACKS.clear(); server.JUKE_RECENT_TRACKS.extend(previous_recent)





def test_juke_clear_timer_stops_instead_of_restarting_single_sid(monkeypatch):
    previous = dict(server.JUKE)
    calls = []
    monkeypatch.setattr(server, "juke_stop", lambda: calls.append("stop"))
    monkeypatch.setattr(server, "_juke_play", lambda index: calls.append(("play", index)))
    try:
        server.JUKE.clear(); server.JUKE.update({
            "items": [server._juke_lazy_item("/HVSC/A.sid", "A.sid")],
            "index": 0, "playing": True, "shuffle": False, "radio": False,
            "song": 1, "timer": None, "folder": "Current tune", "loading": False,
            "source": "test", "generation": 0, "stop_after_current": True,
        })
        server._juke_auto_next()
        assert calls == ["stop"]
        assert server.JUKE["stop_after_current"] is False
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_more_like_this_inserts_after_current_while_radio_appends(monkeypatch):
    previous = dict(server.JUKE)
    recs = [
        server._juke_lazy_item("/HVSC/R1.sid", "R1.sid"),
        server._juke_lazy_item("/HVSC/R2.sid", "R2.sid"),
    ]
    monkeypatch.setattr(server, "_sidflow_recommendations", lambda *a, **k: (
        [dict(item) for item in recs], {"track_id": "seed.sid#1"}
    ))
    try:
        base = [
            server._juke_lazy_item("/HVSC/A.sid", "A.sid"),
            server._juke_lazy_item("/HVSC/B.sid", "B.sid"),
            server._juke_lazy_item("/HVSC/C.sid", "C.sid"),
        ]
        server.JUKE.clear(); server.JUKE.update({
            "items": base, "index": 1, "playing": True, "shuffle": False,
            "radio": False, "song": 1, "timer": None, "folder": "folder",
            "loading": False, "source": "test", "generation": 0,
        })
        out = server._sidflow_append("/HVSC/B.sid", 1, insert_after=1)
        assert out["inserted_at"] == 2
        assert [item["label"] for item in server.JUKE["items"]] == [
            "A.sid", "B.sid", "R1.sid", "R2.sid", "C.sid"
        ]
        assert server.JUKE["index"] == 1

        base2 = [
            server._juke_lazy_item("/HVSC/A.sid", "A.sid"),
            server._juke_lazy_item("/HVSC/B.sid", "B.sid"),
            server._juke_lazy_item("/HVSC/C.sid", "C.sid"),
        ]
        server.JUKE["items"] = base2
        server.JUKE["index"] = 1
        out = server._sidflow_append("/HVSC/B.sid", 1, radio=True, insert_after=1)
        assert out["inserted_at"] == 3
        assert [item["label"] for item in server.JUKE["items"]][-2:] == ["R1.sid", "R2.sid"]
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_readme_has_canonical_screenshots_and_three_tier_quick_start():
    import gallery_check
    readme = (Path(server.ROOT) / "README.md").read_text(encoding="utf-8")
    screenshots = readme.index("## Screenshots")
    quick = readme.index("## Quick start")
    mount = readme.index("## Mount safety modes")
    assert screenshots < quick < mount
    for image in gallery_check.CANONICAL_IMAGES:
        assert image in readme
    assert gallery_check.gallery_images() == list(gallery_check.CANONICAL_IMAGES)
    assert len(gallery_check.CANONICAL_IMAGES) == 13
    assert '*SID Jukebox: instant search across the entire HVSC, one-click playback through the machine\'s own audio — plus "More like this" and Radio mode powered by SIDFlow similarity data (Chris Gleissner), with persistent play queues.*' in readme
    section = readme[quick:mount]
    tier1 = section.index("### Tier 1 — Windows, no Python")
    tier2 = section.index("### Tier 2 — Windows from source")
    tier3 = section.index("### Tier 3 — Anywhere else (or by hand)")
    assert tier1 < tier2 < tier3
    assert "https://github.com/zildac/u64deck/releases/latest" in section
    assert "u64deck.exe" in section and "needs no Python installation" in section
    assert "Windows SmartScreen" in section and "SHA-256" in section
    assert "double-click `start.bat` (installs dependencies on first run" in section
    assert "pip install -r requirements.txt\npython server.py" in section
    assert "which u64deck **creates and updates by itself**" in section
    assert "Always run u64deck as a normal user, and always the same way" in section
    assert readme.index("### Standalone .exe (no Python needed)") > quick
    assert readme.index("### Publishing / releases") > quick


def test_sidflow_help_explains_radio_queue_and_local_privacy():
    help_js = (Path(server.ASSETS) / "static" / "help_content.js").read_text(encoding="utf-8")
    for text in (
        "Selecting an individual search or browser result now creates a one-tune queue",
        "Clear Queue always disarms Radio first",
        "cancel pending SID completion/auto-advance callbacks",
        "inserts up to 20 results immediately after the current tune",
        "A session-level played set prevents repeats",
        "does not upload listening activity",
        "Powered by SIDFlow (Chris Gleissner)",
    ):
        assert text in help_js

# --- Beta 11 Ethernet / Wi-Fi interface awareness ----------------------

def test_link_mac_classification_is_positive_only():
    from network_awareness import classify_mac
    assert classify_mac("02:15:41:aa:bb:cc") == "ethernet"
    assert classify_mac("24:0A:C4:11:22:33") == "wifi"
    assert classify_mac("00:11:22:33:44:55") == "unknown"
    assert classify_mac("") == "unknown"


def test_off_link_mac_lookup_never_trusts_gateway_or_calls_resolver():
    import ipaddress
    from network_awareness import LinkDetector
    calls = []
    detector = LinkDetector()
    result = detector.detect(
        "198.51.100.64",
        networks=[ipaddress.ip_network("192.168.249.0/24")],
        resolver=lambda *a, **k: calls.append(True) or "02:15:41:aa:bb:cc",
    )
    assert result.link_type == "unknown"
    assert result.method == "off-link"
    assert calls == []


def test_latency_fallback_labels_only_a_clear_dual_address_race():
    from network_awareness import latency_race

    async def wide(ip):
        return {"192.0.2.10": 20.0, "192.0.2.20": 200.0}[ip]

    labelled = asyncio.run(latency_race(["192.0.2.10", "192.0.2.20"], sampler=wide))
    assert labelled["192.0.2.10"].link_type == "ethernet"
    assert labelled["192.0.2.20"].link_type == "wifi"

    async def close(ip):
        return {"192.0.2.10": 20.0, "192.0.2.20": 30.0}[ip]

    inconclusive = asyncio.run(latency_race(["192.0.2.10", "192.0.2.20"], sampler=close))
    assert {row.link_type for row in inconclusive.values()} == {"unknown"}
    single = asyncio.run(latency_race(["192.0.2.10"], sampler=wide))
    assert single["192.0.2.10"].method == "latency-not-run"


def test_contradictory_dual_mac_classification_is_demoted_and_raced():
    from network_awareness import LinkObservation, classify_address_group

    class SameDetector:
        def detect(self, ip):
            return LinkObservation(ip, "ethernet", "02:15:41:00:00:01", "wired-prefix")

    async def sample(ip):
        return 20.0 if ip.endswith(".10") else 200.0

    out = asyncio.run(classify_address_group(
        ["192.0.2.10", "192.0.2.20"], SameDetector(), latency_sampler=sample))
    assert out["192.0.2.10"].method == "latency-race"
    assert out["192.0.2.10"].link_type == "ethernet"
    assert out["192.0.2.20"].link_type == "wifi"


def test_discovery_dedupes_same_unique_id_and_prefers_ethernet():
    import discovery
    from network_awareness import LinkObservation

    class Detector:
        def detect(self, ip):
            if ip.endswith(".124"):
                return LinkObservation(ip, "ethernet", "02:15:41:aa:bb:cc", "wired-prefix")
            return LinkObservation(ip, "wifi", "24:0A:C4:11:22:33", "espressif-oui")

    info = {
        "product": "Ultimate 64", "firmware_version": "3.15",
        "hostname": "Ultimate-64-F06606", "unique_id": "Ultimate-64-F06606",
    }
    hits = [
        {"ip": "192.168.249.124", "product": "Ultimate 64", "firmware": "3.15",
         "hostname": info["hostname"], "unique_id": info["unique_id"], "core": "1.4B", "info": dict(info)},
        {"ip": "192.168.249.201", "product": "Ultimate 64", "firmware": "3.15",
         "hostname": info["hostname"], "unique_id": info["unique_id"], "core": "1.4B", "info": dict(info)},
    ]
    known = {}
    rows = asyncio.run(discovery._group_hits(hits, known, Detector()))
    assert len(rows) == 1
    assert rows[0]["preferred_ip"] == "192.168.249.124"
    assert [a["link_type"] for a in rows[0]["addresses"]] == ["ethernet", "wifi"]
    assert len(next(iter(known.values()))["addresses"]) == 2


def test_oui_refresh_is_additive_and_rejects_incomplete_data(tmp_path: Path):
    from espressif_ouis import BUNDLED_ESPRESSIF_OUIS, WIRED_PREFIX
    from network_awareness import refresh_oui_cache

    class Response:
        def __init__(self, text, status=200):
            self.text = text; self.status_code = status
            self.headers = {"etag": '"test"', "last-modified": "today"}

    class Client:
        def __init__(self, response): self.response = response
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def get(self, *args, **kwargs): return self.response

    path = tmp_path / "ouis.json"
    small = refresh_oui_cache(path, now=1000, client_factory=lambda: Client(Response("AA:BB:CC Espressif")))
    assert small == set(BUNDLED_ESPRESSIF_OUIS)
    assert not path.exists()

    lines = [f"{prefix} Espressif Inc" for prefix in sorted(BUNDLED_ESPRESSIF_OUIS)]
    lines.append("AA:BB:CC Espressif Systems")
    merged = refresh_oui_cache(path, now=2000, client_factory=lambda: Client(Response("\n".join(lines))))
    assert "AA:BB:CC" in merged
    assert set(BUNDLED_ESPRESSIF_OUIS) <= merged
    assert WIRED_PREFIX not in merged


def test_oui_refresh_timeout_is_silent_and_keeps_bundled_set(tmp_path: Path):
    from espressif_ouis import BUNDLED_ESPRESSIF_OUIS
    from network_awareness import refresh_oui_cache

    class Broken:
        def __enter__(self): raise TimeoutError("offline")
        def __exit__(self, *args): return False

    assert refresh_oui_cache(tmp_path / "ouis.json", client_factory=Broken) == set(BUNDLED_ESPRESSIF_OUIS)


def test_rest_timeout_scales_only_for_positive_wifi():
    class FakeREST:
        def __init__(self): self.values = []
        def set_timeout(self, value): self.values.append(value)

    fake = FakeREST()
    assert server._configure_rest_timeout(fake, "wifi") == server.WIFI_REST_TIMEOUT
    assert fake.values[-1] == 45.0
    assert server._configure_rest_timeout(fake, "ethernet") == server.DEFAULT_REST_TIMEOUT
    assert fake.values[-1] == 8.0
    assert server._configure_rest_timeout(fake, "unknown") == server.DEFAULT_REST_TIMEOUT
    assert fake.values[-1] == 8.0


def test_wifi_gates_only_stream_start_and_rest_features_remain_available(monkeypatch):
    class FakeREST:
        def get_json(self, path): return {"path": path}

    previous = server.rest
    server.rest = FakeREST()
    monkeypatch.setattr(server, "_current_link_payload", lambda **kwargs: {"link_type": "wifi"})
    try:
        with pytest.raises(HTTPException) as exc:
            server.stream_ctl("video", "start")
        assert exc.value.status_code == 409
        assert server.configs()["path"] == "/v1/configs"
    finally:
        server.rest = previous


def test_frontend_wifi_gating_and_unknown_no_frame_hint_are_present():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    for control in ("btnVideo", "btnAudio", "btnRecord", "btnFullscreen"):
        assert f'id="{control}"' in html
    assert 'id="streamLinkStatus"' in html
    assert 'id="wifiSwitchInline"' in html
    assert "STREAMING NOT AVAILABLE OVER WI-FI" in js
    assert "No video frames received. If this Ultimate is connected over Wi-Fi" in js
    assert 'LINK_STATUS.link_type==="wifi"?45000:15000' in js


def test_interface_awareness_help_readme_and_expanded_screenshot_gallery():
    readme = (Path(server.ROOT) / "README.md").read_text(encoding="utf-8")
    help_js = (Path(server.ASSETS) / "static" / "help_content.js").read_text(encoding="utf-8")
    assert "docs/settings.png" in readme
    assert "docs/device-finder.png" in readme
    assert "docs/wifi-ethernet-header.png" in readme
    assert "docs/wifi-streaming-gated.png" in readme
    assert "docs/mount-and-run.png" in readme
    assert "docs/busy-loading.png" in readme
    assert "docs/disk-swap.png" in readme
    assert "Full firmware settings access — every category editable from the browser" in readme
    assert "## Interface-aware Ultimate discovery" in readme
    assert "STREAMING NOT AVAILABLE OVER WI-FI" in readme
    assert "groups the two addresses into one row and recommends Ethernet" in help_js
    assert "all other REST features remain available" in help_js
    assert "guidance rather than a diagnosis" in help_js


def test_help_covers_finder_mount_state_quick_launch_and_major_workflows():
    help_js = (Path(server.ASSETS) / "static" / "help_content.js").read_text(encoding="utf-8")
    for text in (
        'title:"Find Ultimate Devices"',
        "Previously verified addresses on the current local subnet are checked first",
        "an address must answer during this scan before it can be displayed",
        "updates as soon as the Ultimate confirms the mount",
        "confirmed filename remains visible with an amber loading note",
        'title:"Quick Launch"',
        "right-click a Quick Launch button to remove",
        "Results remain in the left pane",
        "These controls affect the device; they are separate from u64deck's local",
        "Mount &amp; Run shows BUSY",
    ):
        assert text.lower() in help_js.lower()


# --- Public Beta 15: readmem readiness gates and conservative swap additions ---


def _http_response(status: int, *, content: bytes = b"", json_data=None,
                   content_type: str = "application/octet-stream"):
    request = httpx.Request("GET", "http://u64/v1/machine:readmem")
    if json_data is not None:
        return httpx.Response(status, json=json_data, request=request)
    return httpx.Response(status, content=content,
                          headers={"content-type": content_type}, request=request)


def test_read_memory_uses_hex_address_and_returns_binary():
    captured = {}

    class FakeClient:
        def get(self, path, params=None):
            captured.update(path=path, params=params)
            return _http_response(200, content=b"\x00\x01")

    client = ultimate.UltimateREST.__new__(ultimate.UltimateREST)
    client.coordinator = None
    client.client = FakeClient()
    assert client.read_memory(0x00CC, 2) == b"\x00\x01"
    assert captured == {
        "path": "/v1/machine:readmem",
        "params": {"address": "00CC", "length": "2"},
    }


def test_read_memory_404_is_capability_fallback():
    class FakeClient:
        def get(self, path, params=None):
            return _http_response(404)

    client = ultimate.UltimateREST.__new__(ultimate.UltimateREST)
    client.coordinator = None
    client.client = FakeClient()
    assert client.read_memory("$00CC", 1) is None


def test_read_memory_tolerates_json_list_and_hex_payloads():
    class FakeClient:
        def __init__(self, payload): self.payload = payload
        def get(self, path, params=None):
            return _http_response(200, json_data=self.payload)

    client = ultimate.UltimateREST.__new__(ultimate.UltimateREST)
    client.coordinator = None
    client.client = FakeClient({"data": [0, 255]})
    assert client.read_memory("00CC", 2) == b"\x00\xff"
    client.client = FakeClient({"data": "00 ff"})
    assert client.read_memory("00CC", 2) == b"\x00\xff"


def _gate_with(sequence, monkeypatch, *, timeout=120.0, grace=0.0):
    reads = list(sequence)
    now = [0.0]
    events = []

    def reader():
        return reads.pop(0) if reads else 1

    def sleeper(seconds):
        now[0] += seconds

    monkeypatch.setattr(server, "_diag_event",
                        lambda level, message, **extra: events.append((level, message)))
    result = server._basic_ready_gate(
        "test", timeout=timeout, poll=0.5, grace=grace,
        reader=reader, sleeper=sleeper, clock=lambda: now[0],
    )
    return result, events


def test_basic_ready_gate_debounces_and_logs_completion(monkeypatch):
    result, events = _gate_with([1, 0, 1, 0, 0], monkeypatch)
    assert result == "ready"
    assert len(events) == 1
    assert "Mount & Run gate 'test': ready" in events[0][1]
    assert "last $CC read: 0" in events[0][1]


def test_basic_ready_gate_timeout_and_unsupported_are_logged(monkeypatch):
    result, events = _gate_with([1] * 10, monkeypatch, timeout=2.0)
    assert result == "timeout"
    assert "gate 'test': timeout" in events[0][1]
    result, events = _gate_with([None], monkeypatch)
    assert result == "unsupported"
    assert "last $CC read: unsupported" in events[0][1]


def test_readmem_support_cache_is_per_device(monkeypatch):
    previous = server.rest
    server._READMEM_SUPPORT.clear()

    class FakeRest:
        def __init__(self, host, result):
            self.host = host
            self.result = result
            self.reads = 0
        def read_memory(self, address, length):
            self.reads += 1
            return self.result

    try:
        unsupported = FakeRest("192.0.2.10", None)
        server.rest = unsupported
        assert server._read_basic_ready_flag() is None
        assert server._read_basic_ready_flag() is None
        assert unsupported.reads == 1

        supported = FakeRest("192.0.2.11", b"\x00")
        server.rest = supported
        assert server._read_basic_ready_flag() == 0
        assert supported.reads == 1
    finally:
        server.rest = previous
        server._READMEM_SUPPORT.clear()


def _mount_boot_harness(monkeypatch, gate_results):
    typed = []
    gates = list(gate_results)
    matrix_events = []
    monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: None)
    monkeypatch.setattr(server, "_matrix_send",
                        lambda *args, **kwargs: matrix_events.append((args, kwargs)))
    monkeypatch.setattr(server, "_boot_settle", lambda **kwargs: None)
    monkeypatch.setattr(server, "_temporary_crt_reboot_before_mount_run", lambda: False)
    monkeypatch.setattr(server, "_cartridge_preflight", lambda **kwargs: {
        "known": True, "classification": "none", "label": "None",
        "source": "test", "age_seconds": 0.0,
    })
    monkeypatch.setattr(server, "_remember_mount", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_bus_id_for", lambda drive: 8)
    monkeypatch.setattr(server, "_basic_ready_gate",
                        lambda stage, **kwargs: gates.pop(0))
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)

    class FakeRest:
        host = "192.0.2.64"
        def mount_path(self, *args, **kwargs): pass
        def mount_attachment(self, *args, **kwargs): pass
        def put(self, *args, **kwargs): pass

    class FakeCmd:
        def type_petscii(self, data, **kwargs):
            typed.append(bytes(data))

    monkeypatch.setattr(server, "rest", FakeRest())
    monkeypatch.setattr(server, "cmd", FakeCmd())
    out = server._mount_and_boot("a", "unlinked", device_path="/Usb0/demo.d64")
    return typed, matrix_events, out


def test_mount_and_run_ready_path_types_exact_buffer_commands(monkeypatch):
    typed, matrix_events, out = _mount_boot_harness(monkeypatch, ["ready", "ready"])
    assert typed == [b'LOAD"*",8,1\r', b"RUN\r"]
    assert matrix_events == []
    assert out == {"errors": [], "typed": 'LOAD"*",8,1 + RUN'}


def test_mount_and_run_ready_path_uses_cia1_matrix_for_load_and_run(monkeypatch):
    diagnostics = []
    monkeypatch.setattr(server, "_input_status",
                        lambda *args, **kwargs: {"available": True})
    monkeypatch.setattr(server, "_diag_event",
                        lambda level, message, **kwargs: diagnostics.append((level, message)))

    typed, matrix_events, out = _mount_boot_harness(monkeypatch, ["ready", "ready"])

    expected_load = server._mount_run_matrix_events('LOAD"*",8,1\r')
    assert typed == []
    assert matrix_events == [
        ((expected_load,), {"client": server.rest}),
        ((server._RUN_MATRIX_EVENTS,), {"client": server.rest}),
    ]
    assert out == {"errors": [], "typed": 'LOAD"*",8,1 + RUN'}
    assert any("Mount & Run LOAD delivery: CIA1 matrix" in message
               for _level, message in diagnostics)
    assert ("info", "Mount & Run RUN delivery: CIA1 matrix") in diagnostics


def test_mount_and_run_matrix_failure_does_not_duplicate_via_buffer(monkeypatch):
    typed = []
    monkeypatch.setattr(server, "_input_status",
                        lambda *args, **kwargs: {"available": True})
    monkeypatch.setattr(server, "_matrix_send",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            httpx.ReadTimeout("ambiguous matrix timeout")))
    monkeypatch.setattr(server, "_warn_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)

    class FakeCmd:
        def type_petscii(self, data, **kwargs):
            typed.append(bytes(data))

    monkeypatch.setattr(server, "cmd", FakeCmd())
    delivered, note = server._dispatch_run_after_gate()

    assert delivered is False
    assert typed == []
    assert "RUN not resent" in note


def test_mount_and_run_boot_timeout_types_nothing(monkeypatch):
    typed, matrix_events, out = _mount_boot_harness(monkeypatch, ["timeout"])
    assert typed == [] and matrix_events == []
    assert out["typed"] == ""
    assert out["note"] == "machine not ready — LOAD not typed"


def test_mount_and_run_load_timeout_withholds_run(monkeypatch):
    typed, matrix_events, out = _mount_boot_harness(monkeypatch, ["ready", "timeout"])
    assert typed == [b'LOAD"*",8,1\r'] and matrix_events == []
    assert out["typed"] == 'LOAD"*",8,1'
    assert out["note"] == "load still running — RUN not sent"


def test_mount_and_run_unsupported_readmem_keeps_fixed_delay_but_uses_cia1(monkeypatch):
    monkeypatch.setattr(server, "_input_status",
                        lambda *args, **kwargs: {"available": True})
    typed, matrix_events, out = _mount_boot_harness(monkeypatch, ["unsupported"])
    assert typed == []
    assert matrix_events == [
        ((server._mount_run_matrix_events('LOAD"*",8,1\r'),), {"client": server.rest}),
        ((server._RUN_MATRIX_EVENTS,), {"client": server.rest}),
    ]
    assert out == {"errors": [], "typed": 'LOAD"*",8,1 + RUN'}


def test_swap_groups_compound_numbered_tokens_without_changing_plain_signature():
    siblings = ["EdgeOfDisgrace_1b.d64", "EdgeOfDisgrace_0.d64",
                "EdgeOfDisgrace_1a.d64", "Other_1a.d64"]
    assert server._swap_group_candidates("EdgeOfDisgrace_0.d64", siblings) == [
        "EdgeOfDisgrace_0.d64", "EdgeOfDisgrace_1a.d64", "EdgeOfDisgrace_1b.d64",
    ]
    family, _token = server._swap_signature("Uncensored_1.d64")
    assert family == (".d64", "numbered", "uncensored", "number")


def test_swap_groups_safe_bare_wrapped_tokens():
    assert server._swap_group_candidates(
        "WeAreDemo(A).d64", ["WeAreDemo(B).d64", "WeAreDemo(A).d64"]
    ) == ["WeAreDemo(A).d64", "WeAreDemo(B).d64"]
    assert server._swap_group_candidates(
        "Game(1).d64", ["Game(2).d64", "Game(1).d64"]
    ) == ["Game(1).d64", "Game(2).d64"]


def test_swap_vetoes_alternate_dump_and_glued_digit_shapes():
    assert server._swap_group_candidates(
        "Game(a).d64", ["Game.d64", "Game(a).d64", "Game(b).d64"]
    ) == ["Game(a).d64"]
    assert server._swap_group_candidates(
        "Game[a].d64", ["Game[a].d64", "Game[b].d64"]
    ) == ["Game[a].d64"]
    assert server._swap_group_candidates(
        "Turrican1.d64", ["Turrican1.d64", "Turrican2.d64"]
    ) == ["Turrican1.d64"]


def test_swap_groups_titleless_markers_but_never_mixes_marker_families():
    assert server._swap_group_candidates(
        "side1.d64", ["side2.d64", "side1.d64"]
    ) == ["side1.d64", "side2.d64"]
    assert server._swap_group_candidates(
        "side1.d64", ["side1.d64", "disk2.d64"]
    ) == ["side1.d64"]


def test_scanner_displays_hostname_before_unique_id():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    hostname_branch = '${d.hostname?" · "+esc(d.hostname):(d.unique_id?" · "+esc(d.unique_id):"")}'
    assert hostname_branch in js
    assert '${d.unique_id?" · "+esc(d.unique_id):(d.hostname?" · "+esc(d.hostname):"")}' not in js


# --- Public Beta 15: verified-only discovery and reliable Jukebox Stop ---


def _discovery_hit(ip: str, *, unique_id: str = "101090",
                   hostname: str = "Ultimate-64-F06606") -> dict:
    info = {
        "product": "Ultimate 64",
        "firmware_version": "3.15",
        "hostname": hostname,
        "unique_id": unique_id,
    }
    return {
        "ip": ip, "product": "Ultimate 64", "firmware": "3.15",
        "hostname": hostname, "unique_id": unique_id, "core": "1.4B",
        "info": info,
    }


def _transport_result(ip: str, *, unique_id: str = "101090",
                      hostname: str = "Ultimate-64-F06606",
                      stage: str = "direct-subnet") -> dict:
    return {
        "ip": ip,
        "stage": stage,
        "status": "ultimate",
        "elapsed_ms": 25.0,
        "found_after_ms": 100.0,
        "payload": {
            "product": "Ultimate 64",
            "firmware_version": "3.15",
            "hostname": hostname,
            "unique_id": unique_id,
            "core_version": "1.4B",
        },
    }


class _MappedLinkDetector:
    def __init__(self, mapping):
        self.mapping = mapping

    def detect(self, ip, *, force=False, **_kwargs):
        from network_awareness import LinkObservation
        link, mac, method = self.mapping[ip]
        return LinkObservation(ip, link, mac, method)


def test_discovery_returns_only_addresses_verified_in_current_scan():
    import discovery

    known = {
        "uid:101090": {
            "identity": "uid:101090", "unique_id": "101090",
            "hostname": "Ultimate-64-F06606", "product": "Ultimate 64",
            "addresses": {
                "192.168.249.143": {
                    "ip": "192.168.249.143", "link_type": "wifi",
                    "mac": "24:0A:C4:9F:77:C8", "last_seen": "old-wifi",
                },
                "192.168.249.144": {
                    "ip": "192.168.249.144", "link_type": "ethernet",
                    "mac": "02:15:41:F0:66:06", "last_seen": "old-ethernet",
                },
            },
        }
    }
    detector = _MappedLinkDetector({
        "192.168.249.144": ("ethernet", "02:15:41:F0:66:06", "wired-prefix"),
    })
    events = []
    devices = asyncio.run(discovery._group_hits(
        [_discovery_hit("192.168.249.144")], known, detector, events=events))

    assert [row["ip"] for row in devices[0]["addresses"]] == ["192.168.249.144"]
    assert devices[0]["preferred_ip"] == "192.168.249.144"
    assert known["uid:101090"]["addresses"]["192.168.249.143"]["last_seen"] == "old-wifi"
    assert any("192.168.249.143" in event and "no response" in event for event in events)


def test_discovery_dhcp_replacement_removes_superseded_ips_by_mac():
    import discovery

    known = {
        "uid:101090": {
            "identity": "uid:101090", "unique_id": "101090",
            "hostname": "Ultimate-64-F06606", "product": "Ultimate 64",
            "addresses": {
                "192.168.249.124": {
                    "ip": "192.168.249.124", "link_type": "ethernet",
                    "mac": "02:15:41:F0:66:06", "last_seen": "old",
                },
                "192.168.249.129": {
                    "ip": "192.168.249.129", "link_type": "wifi",
                    "mac": "24:0A:C4:9F:77:C8", "last_seen": "old",
                },
            },
        }
    }
    detector = _MappedLinkDetector({
        "192.168.249.143": ("wifi", "24:0A:C4:9F:77:C8", "espressif-oui"),
        "192.168.249.144": ("ethernet", "02:15:41:F0:66:06", "wired-prefix"),
    })
    events = []
    devices = asyncio.run(discovery._group_hits([
        _discovery_hit("192.168.249.143"),
        _discovery_hit("192.168.249.144"),
    ], known, detector, events=events))

    assert [row["ip"] for row in devices[0]["addresses"]] == [
        "192.168.249.144", "192.168.249.143",
    ]
    assert set(known["uid:101090"]["addresses"]) == {
        "192.168.249.143", "192.168.249.144",
    }
    assert sum("Discovery address replaced" in event for event in events) == 2


def test_discovery_remembered_addresses_are_prioritised_not_trusted(monkeypatch):
    import discovery

    stages = []
    monkeypatch.setattr(discovery, "local_subnets", lambda: ["192.0.2."])

    def scan_direct(ips, workers, connect_timeout, response_timeout, overall_started, stage, port=80):
        stages.append((list(ips), workers, connect_timeout, response_timeout, stage, port))
        return []

    monkeypatch.setattr(discovery.discovery_transport, "scan_direct", scan_direct)
    known = {"uid:1": {"addresses": {"192.0.2.44": {"ip": "192.0.2.44"}}}}
    result = asyncio.run(discovery.discover(known_devices=known))

    assert len(stages) == 1
    ips, workers, connect_timeout, response_timeout, stage, port = stages[0]
    assert ips[0] == "192.0.2.44"
    assert len(ips) == len(set(ips)) == 254
    assert (workers, connect_timeout, response_timeout, stage, port) == (
        64, 1.5, 3.25, "direct-all", 80)
    assert result["devices"] == []
    assert result["candidate_count"] == 254
    assert result["cached_candidate_count"] == 1
    assert any("192.0.2.44" in line and "non-blocking" in line
               for line in result["diagnostics"])

def test_discovery_updates_saved_host_only_to_verified_active_identity(monkeypatch):
    original = dict(server.CFG)
    original_live = {key: set(value) for key, value in server.DISCOVERY_LIVE_ADDRESSES.items()}
    diagnostics = []

    async def fake_discover(*args, **kwargs):
        return {
            "devices": [{
                "identity": "uid:101090", "preferred_ip": "192.168.249.144",
                "addresses": [{"ip": "192.168.249.144", "link_type": "ethernet"}],
            }],
            "diagnostics": ["Discovery scan: test"], "subnets": [],
        }

    try:
        server.CFG.clear(); server.CFG.update(original)
        server.CFG["u64_host"] = "192.168.249.124"
        server.CFG["active_device_identity"] = "uid:101090"
        monkeypatch.setattr(server.discovery, "discover", fake_discover)
        monkeypatch.setattr(server, "save_config", lambda: None)
        monkeypatch.setattr(server, "_diag_event",
                            lambda level, message, **extra: diagnostics.append(message))
        asyncio.run(server._run_discovery())
        assert server.CFG["u64_host"] == "192.168.249.144"
        assert server.DISCOVERY_LIVE_ADDRESSES == {"uid:101090": {"192.168.249.144"}}
        assert any("Preferred address updated" in message for message in diagnostics)
    finally:
        server.CFG.clear(); server.CFG.update(original)
        server.DISCOVERY_LIVE_ADDRESSES.clear()
        server.DISCOVERY_LIVE_ADDRESSES.update(original_live)


def test_clear_discovered_devices_preserves_unrelated_settings_and_rescans(monkeypatch):
    original = dict(server.CFG)
    calls = []

    async def fake_scan(subnet="", port=80):
        calls.append((subnet, port))
        return {"devices": [], "subnets": ["192.168.1.0/24"], "diagnostics": []}

    try:
        server.CFG.clear(); server.CFG.update({
            **original,
            "u64_host": "192.168.249.144",
            "known_devices": {"uid:1": {"addresses": {"192.168.249.144": {}}}},
            "active_device_identity": "uid:1",
            "input_method_preferences": {"uid:1": "legacy"},
            "sid_local_source": "F:\\HVSC",
            "http_port": 8064,
        })
        monkeypatch.setattr(server, "_disconnect_discovery_session",
                            lambda: calls.append("disconnect"))
        monkeypatch.setattr(server, "save_config", lambda: calls.append("save"))
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_discovery", fake_scan)
        result = asyncio.run(server.api_discover_clear("192.168.249.", 80))

        assert server.CFG["known_devices"] == {}
        assert server.CFG["active_device_identity"] == ""
        assert server.CFG["u64_host"] == ""
        assert "input_method_preferences" not in server.CFG
        assert server.CFG["sid_local_source"] == "F:\\HVSC"
        assert server.CFG["http_port"] == 8064
        assert calls == ["disconnect", "save", ("192.168.249.", 80)]
        assert result["cleared"] is True
    finally:
        server.CFG.clear(); server.CFG.update(original)


def test_disconnect_waits_for_active_status_before_closing_client(monkeypatch):
    coordinator = server.DeviceOperationCoordinator()
    status_active = threading.Event()
    release_status = threading.Event()
    rest_closed = threading.Event()
    cmd_closed = threading.Event()
    original_stream_state = dict(server.STREAM_STATE)

    class OldRest:
        host = "192.0.2.64"
        def close(self):
            rest_closed.set()

    class OldCmd:
        def close(self):
            cmd_closed.set()

    class EmptyRest:
        host = ""

    monkeypatch.setattr(server, "DEVICE_OP", coordinator)
    monkeypatch.setattr(server, "rest", OldRest())
    monkeypatch.setattr(server, "cmd", OldCmd())
    monkeypatch.setattr(server, "devfs", object())
    monkeypatch.setattr(server, "UltimateREST",
                        lambda *args, **kwargs: EmptyRest())
    monkeypatch.setattr(server, "CommandSocket",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr(server, "DeviceFS",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr(server, "_matrix_release_all",
                        lambda *args, **kwargs: False)
    server.STREAM_STATE.update({"video": False, "audio": False})

    def hold_status():
        with coordinator.operation("status", "polling /api/info"):
            status_active.set()
            assert release_status.wait(2.0)

    holder = threading.Thread(target=hold_status, daemon=True)
    holder.start()
    assert status_active.wait(1.0)

    disconnect = threading.Thread(
        target=server._disconnect_discovery_session, daemon=True)
    disconnect.start()
    time.sleep(0.05)
    assert not rest_closed.is_set()
    assert not cmd_closed.is_set()

    release_status.set()
    holder.join(1.0)
    disconnect.join(1.0)
    assert rest_closed.is_set()
    assert cmd_closed.is_set()
    assert isinstance(server.rest, EmptyRest)
    server.STREAM_STATE.clear(); server.STREAM_STATE.update(original_stream_state)


def test_discovery_ui_has_nuclear_clear_and_verified_only_wording():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert "Clear discovered devices" in js
    assert "/api/discover/clear" in js
    assert "Only interfaces verified during this scan are shown" in js
    assert "Other settings will not be changed" in js


def test_command_socket_fresh_reset_discards_idle_socket(monkeypatch):
    closed = []
    sent = []

    class FakeSocket:
        def close(self): closed.append(self)
        def sendall(self, payload): sent.append(bytes(payload))
        def setsockopt(self, *args): pass

    old_socket = FakeSocket()
    new_socket = FakeSocket()
    monkeypatch.setattr(ultimate.socket, "create_connection",
                        lambda address, timeout=4: new_socket)
    command = ultimate.CommandSocket("192.0.2.64")
    command._sock = old_socket
    command.reset_fresh()

    assert closed == [old_socket]
    assert sent == [b"\x04\xff\x00\x00"]
    assert command._sock is new_socket


def test_juke_stop_cia1_uses_fresh_reset_once_and_skips_rest(monkeypatch):
    previous = dict(server.JUKE)
    calls = []
    host = "192.0.2.18"

    class FakeCommand:
        def reset(self): calls.append("old-reset")
        def reset_fresh(self): calls.append("fresh-reset")

    class FakeRest:
        def __init__(self): self.host = host
        def put(self, *args, **kwargs): calls.append("rest")

    try:
        server.JUKE.update({"items": [], "index": -1, "playing": True,
                            "stop_after_current": False, "timer": None})
        monkeypatch.setattr(server, "cmd", FakeCommand())
        monkeypatch.setattr(server, "rest", FakeRest())
        monkeypatch.setitem(server.INPUT_CAPABILITIES, host, {"available": True})
        monkeypatch.setattr(server, "_matrix_release_all",
                            lambda **kwargs: calls.append(("release", kwargs)))
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
        server.juke_stop()
        assert calls == [
            "fresh-reset",
            ("release", {"silent": True, "cached_only": True, "caller": "juke-stop"}),
        ]
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_juke_stop_cia1_fresh_failure_releases_matrix_then_uses_rest(monkeypatch):
    previous = dict(server.JUKE)
    calls = []
    host = "192.0.2.19"

    class FakeCommand:
        def reset_fresh(self):
            calls.append("fresh-reset")
            raise RuntimeError("stale socket")

    class FakeRest:
        def __init__(self): self.host = host
        def put(self, path, **kwargs):
            calls.append(("rest", path, kwargs))
            return {}

    try:
        server.JUKE.update({"items": [], "index": -1, "playing": True,
                            "stop_after_current": False, "timer": None})
        monkeypatch.setattr(server, "cmd", FakeCommand())
        monkeypatch.setattr(server, "rest", FakeRest())
        monkeypatch.setitem(server.INPUT_CAPABILITIES, host, {"available": True})
        monkeypatch.setattr(server, "_matrix_release_all",
                            lambda **kwargs: calls.append(("release", kwargs)))
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
        server.juke_stop()
        assert calls[0:2] == [
            "fresh-reset",
            ("release", {"silent": True, "cached_only": True, "caller": "juke-stop"}),
        ]
        assert calls[2][0:2] == ("rest", "/v1/machine:reset")
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_juke_stop_legacy_rest_failure_falls_back_to_fresh_command(monkeypatch):
    previous = dict(server.JUKE)
    calls = []
    host = "192.0.2.20"

    class FakeCommand:
        def reset_fresh(self): calls.append("fresh-reset")

    class FakeRest:
        def __init__(self): self.host = host
        def put(self, path, **kwargs):
            calls.append(("rest", path, kwargs))
            raise RuntimeError("REST busy")

    try:
        server.JUKE.update({"items": [], "index": -1, "playing": True,
                            "stop_after_current": False, "timer": None})
        monkeypatch.setattr(server, "cmd", FakeCommand())
        monkeypatch.setattr(server, "rest", FakeRest())
        monkeypatch.setitem(server.INPUT_CAPABILITIES, host, {"available": False})
        monkeypatch.setattr(server, "_matrix_release_all",
                            lambda **kwargs: calls.append(("release", kwargs)))
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
        server.juke_stop()
        assert calls[0][0:2] == ("rest", "/v1/machine:reset")
        assert calls[1] == ("release", {"silent": True, "cached_only": True, "caller": "juke-stop"})
        assert calls[2] == "fresh-reset"
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_juke_stop_missing_capability_cache_defaults_to_rest_without_probe(monkeypatch):
    previous = dict(server.JUKE)
    calls = []
    host = "192.0.2.21"

    class FakeCommand:
        def reset_fresh(self): calls.append("fresh-reset")

    class FakeRest:
        def __init__(self): self.host = host
        def put(self, path, **kwargs): calls.append(("rest", path, kwargs))

    server.INPUT_CAPABILITIES.pop(host, None)
    try:
        server.JUKE.update({"items": [], "index": -1, "playing": True,
                            "stop_after_current": False, "timer": None})
        monkeypatch.setattr(server, "cmd", FakeCommand())
        monkeypatch.setattr(server, "rest", FakeRest())
        monkeypatch.setattr(server, "_input_status",
                            lambda *args, **kwargs: pytest.fail("Stop must not probe"))
        monkeypatch.setattr(server, "_matrix_release_all",
                            lambda **kwargs: calls.append(("release", kwargs)))
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
        server.juke_stop()
        assert calls[0][0:2] == ("rest", "/v1/machine:reset")
        assert "fresh-reset" not in calls
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_connected_link_payload_never_offers_unverified_historical_address(monkeypatch):
    from network_awareness import LinkObservation

    original_cfg = dict(server.CFG)
    original_live = {key: set(value) for key, value in server.DISCOVERY_LIVE_ADDRESSES.items()}
    try:
        server.CFG.clear(); server.CFG.update({
            **original_cfg,
            "known_devices": {
                "uid:101090": {
                    "identity": "uid:101090", "identity_source": "unique_id",
                    "unique_id": "101090", "hostname": "Ultimate-64-F06606",
                    "product": "Ultimate 64", "firmware": "3.15",
                    "addresses": {
                        "192.168.249.143": {
                            "ip": "192.168.249.143", "link_type": "wifi",
                            "mac": "24:0A:C4:9F:77:C8", "last_seen": "wifi-old",
                        },
                        "192.168.249.144": {
                            "ip": "192.168.249.144", "link_type": "ethernet",
                            "mac": "02:15:41:F0:66:06", "last_seen": "ethernet-old",
                        },
                    },
                }
            },
            "active_device_identity": "uid:101090",
            "u64_host": "192.168.249.144",
        })
        server.DISCOVERY_LIVE_ADDRESSES.clear()
        server.DISCOVERY_LIVE_ADDRESSES["uid:101090"] = {"192.168.249.144"}
        monkeypatch.setattr(server.LINK_DETECTOR, "detect", lambda ip, force=False:
                            LinkObservation(ip, "ethernet", "02:15:41:F0:66:06", "wired-prefix"))
        payload = server._link_payload("192.168.249.144", persist=False)

        assert [row["ip"] for row in payload["addresses"]] == ["192.168.249.144"]
        assert payload["ethernet_ip"] == "192.168.249.144"
        assert payload["wifi_ip"] == ""
        addresses = server.CFG["known_devices"]["uid:101090"]["addresses"]
        assert addresses["192.168.249.143"]["last_seen"] == "wifi-old"
        assert addresses["192.168.249.144"]["last_seen"] == "ethernet-old"
    finally:
        server.CFG.clear(); server.CFG.update(original_cfg)
        server.DISCOVERY_LIVE_ADDRESSES.clear()
        server.DISCOVERY_LIVE_ADDRESSES.update(original_live)


def test_rc3_machine_takeover_disarms_jukebox_timer_and_generation(monkeypatch):
    class FakeTimer:
        def __init__(self):
            self.cancelled = False
        def cancel(self):
            self.cancelled = True

    previous = dict(server.JUKE)
    timer = FakeTimer()
    diagnostics = []
    monkeypatch.setattr(server, "_diag_event",
                        lambda level, message: diagnostics.append((level, message)))
    try:
        server.JUKE.clear(); server.JUKE.update({
            "items": [server._juke_lazy_item("/HVSC/A.sid", "A.sid")],
            "index": 0, "playing": True, "shuffle": False, "radio": True,
            "song": 1, "timer": timer, "folder": "Current tune",
            "loading": False, "source": "test", "generation": 41,
            "stop_after_current": True,
        })
        generation = server._juke_disarm_machine_takeover("Mount & Run")
        assert generation == 42
        assert timer.cancelled is True
        assert server.JUKE["timer"] is None
        assert server.JUKE["playing"] is False
        assert server.JUKE["stop_after_current"] is False
        assert server.JUKE["radio"] is False
        assert diagnostics == [("info", "SID Jukebox disarmed for Mount & Run")]
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_rc3_stale_jukebox_callback_cannot_stop_or_restart_machine(monkeypatch):
    previous = dict(server.JUKE)
    calls = []
    monkeypatch.setattr(server, "juke_stop", lambda: calls.append("stop"))
    monkeypatch.setattr(
        server, "_juke_play",
        lambda index, **kwargs: calls.append(("play", index, kwargs)),
    )
    try:
        server.JUKE.clear(); server.JUKE.update({
            "items": [server._juke_lazy_item("/HVSC/A.sid", "A.sid")],
            "index": 0, "playing": True, "shuffle": False, "radio": False,
            "song": 1, "timer": None, "folder": "Current tune",
            "loading": False, "source": "test", "generation": 8,
            "stop_after_current": True,
        })
        server._juke_auto_next(7)
        assert calls == []
        assert server.JUKE["stop_after_current"] is True
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_rc3_mount_and_run_invalidates_due_sid_completion_callback(monkeypatch):
    class FakeTimer:
        def __init__(self):
            self.cancelled = False
        def cancel(self):
            self.cancelled = True

    previous = dict(server.JUKE)
    timer = FakeTimer()
    calls = []
    monkeypatch.setattr(server, "juke_stop", lambda: calls.append("stop"))
    monkeypatch.setattr(
        server, "_juke_play",
        lambda index, **kwargs: calls.append(("play", index, kwargs)),
    )
    try:
        server.JUKE.clear(); server.JUKE.update({
            "items": [server._juke_lazy_item("/HVSC/A.sid", "A.sid")],
            "index": 0, "playing": True, "shuffle": False, "radio": False,
            "song": 1, "timer": timer, "folder": "Current tune",
            "loading": False, "source": "test", "generation": 15,
            "stop_after_current": True,
        })
        typed, matrix_events, out = _mount_boot_harness(monkeypatch, ["unsupported"])
        server._juke_auto_next(15)
        assert timer.cancelled is True
        assert server.JUKE["generation"] == 16
        assert server.JUKE["playing"] is False
        assert server.JUKE["stop_after_current"] is False
        assert calls == []
        assert typed == [b'LOAD"*",8,1\r', b"RUN\r"]
        assert matrix_events == []
        assert out == {"errors": [], "typed": 'LOAD"*",8,1 + RUN'}
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_rc3_non_jukebox_runner_disarms_pending_sid_callback(monkeypatch):
    class FakeTimer:
        def __init__(self):
            self.cancelled = False
        def cancel(self):
            self.cancelled = True

    previous = dict(server.JUKE)
    timer = FakeTimer()
    calls = []
    monkeypatch.setattr(server, "_cart_configured", lambda: "")
    monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
    try:
        server.JUKE.clear(); server.JUKE.update({
            "items": [server._juke_lazy_item("/HVSC/A.sid", "A.sid")],
            "index": 0, "playing": True, "shuffle": False, "radio": True,
            "song": 1, "timer": timer, "folder": "Current tune",
            "loading": False, "source": "test", "generation": 21,
            "stop_after_current": True,
        })
        result = server._run_cart_safe(lambda: calls.append("run") or {"ok": True})
        assert result == {"ok": True}
        assert calls == ["run"]
        assert timer.cancelled is True
        assert server.JUKE["generation"] == 22
        assert server.JUKE["playing"] is False
        assert server.JUKE["stop_after_current"] is False
        assert server.JUKE["radio"] is False
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)



def test_rc4_successful_sid_upload_marks_current_device_for_reboot(monkeypatch):
    previous_rest = server.rest
    previous_required = set(server.SID_RUNNER_REBOOT_REQUIRED)

    class FakeRest:
        host = "192.0.2.64"
        def post_sid(self, *args, **kwargs):
            return {"ok": True}

    try:
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.rest = FakeRest()
        monkeypatch.setattr(server, "_sid_ssl_payload", lambda data: None)
        out = server._post_sid_upload("Tune.sid", b"PSID" + b"\0" * 200)
        assert out == {"ok": True}
        assert server._sid_runner_reboot_required(server.rest) is True
    finally:
        server.rest = previous_rest
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.SID_RUNNER_REBOOT_REQUIRED.update(previous_required)


def test_rc4_failed_sid_upload_does_not_mark_reboot_required(monkeypatch):
    previous_rest = server.rest
    previous_required = set(server.SID_RUNNER_REBOOT_REQUIRED)

    class FakeRest:
        host = "192.0.2.65"
        def post_sid(self, *args, **kwargs):
            raise ultimate.UltimateError("upload failed")

    try:
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.rest = FakeRest()
        monkeypatch.setattr(server, "_sid_ssl_payload", lambda data: None)
        with pytest.raises(ultimate.UltimateError, match="upload failed"):
            server._post_sid_upload("Tune.sid", b"PSID" + b"\0" * 200)
        assert server._sid_runner_reboot_required(server.rest) is False
    finally:
        server.rest = previous_rest
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.SID_RUNNER_REBOOT_REQUIRED.update(previous_required)


def test_rc4_mount_and_run_reboots_after_sid_before_mount(monkeypatch):
    previous_rest, previous_cmd = server.rest, server.cmd
    previous_required = set(server.SID_RUNNER_REBOOT_REQUIRED)
    events = []

    class FakeRest:
        host = "192.0.2.66"
        def put(self, path, **kwargs):
            events.append(("put", path))
            return {"ok": True}
        def probe_info(self, request_timeout=1.5):
            events.append(("probe", request_timeout))
            return {"product": "Ultimate"}
        def mount_path(self, drive, path, mode=None):
            events.append(("mount", drive, path, mode))
        def mount_attachment(self, *args, **kwargs):
            raise AssertionError("unexpected attachment mount")

    class FakeCmd:
        def close(self):
            events.append(("cmd-close",))
        def type_petscii(self, data, **kwargs):
            events.append(("type", bytes(data)))

    try:
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.rest, server.cmd = FakeRest(), FakeCmd()
        server._sid_runner_mark_reboot_required(server.rest)
        monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: None)
        monkeypatch.setattr(server, "_juke_disarm_machine_takeover", lambda reason: 1)
        monkeypatch.setattr(server, "_remember_mount", lambda *args, **kwargs: events.append(("remember",)))
        monkeypatch.setattr(server, "_boot_settle", lambda **kwargs: events.append(("settle",)))
        monkeypatch.setattr(server, "_temporary_crt_reboot_before_mount_run", lambda: False)
        monkeypatch.setattr(server, "_cartridge_preflight", lambda **kwargs: {
            "known": True, "classification": "none", "label": "None",
            "source": "test", "age_seconds": 0.0,
        })
        monkeypatch.setattr(server, "_bus_id_for", lambda drive: 8)
        monkeypatch.setattr(server, "_basic_ready_gate", lambda stage, **kwargs: "unsupported")
        monkeypatch.setattr(server.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)

        out = server._mount_and_boot("a", "unlinked", device_path="/Usb0/demo.d64")

        reboot_at = events.index(("put", "/v1/machine:reboot"))
        mount_at = events.index(("mount", "a", "/Usb0/demo.d64", "unlinked"))
        reset_at = events.index(("put", "/v1/machine:reset"))
        assert reboot_at < mount_at < reset_at
        assert ("cmd-close",) in events
        assert any(item[0] == "probe" for item in events)
        assert server._sid_runner_reboot_required(server.rest) is False
        assert out == {"errors": [], "typed": 'LOAD"*",8,1 + RUN'}
    finally:
        server.rest, server.cmd = previous_rest, previous_cmd
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.SID_RUNNER_REBOOT_REQUIRED.update(previous_required)


def test_rc4_failed_recovery_reboot_aborts_before_mount_and_stays_armed(monkeypatch):
    previous_rest, previous_cmd = server.rest, server.cmd
    previous_required = set(server.SID_RUNNER_REBOOT_REQUIRED)
    events = []

    class FakeRest:
        host = "192.0.2.67"
        def put(self, path, **kwargs):
            events.append(path)
            if path == "/v1/machine:reboot":
                raise ultimate.UltimateError("reboot failed")
        def mount_path(self, *args, **kwargs):
            events.append("mounted")

    class FakeCmd:
        def close(self):
            events.append("closed")

    try:
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.rest, server.cmd = FakeRest(), FakeCmd()
        server._sid_runner_mark_reboot_required(server.rest)
        monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: None)
        monkeypatch.setattr(server, "_juke_disarm_machine_takeover", lambda reason: 1)
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
        with pytest.raises(ultimate.UltimateError, match="reboot failed"):
            server._mount_and_boot("a", "unlinked", device_path="/Usb0/demo.d64")
        assert "mounted" not in events
        assert server._sid_runner_reboot_required(server.rest) is True
    finally:
        server.rest, server.cmd = previous_rest, previous_cmd
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.SID_RUNNER_REBOOT_REQUIRED.update(previous_required)


def test_rc4_ordinary_mount_and_run_does_not_reboot(monkeypatch):
    previous_required = set(server.SID_RUNNER_REBOOT_REQUIRED)
    try:
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        typed, matrix_events, out = _mount_boot_harness(monkeypatch, ["unsupported"])
        assert typed == [b'LOAD"*",8,1\r', b"RUN\r"]
        assert matrix_events == []
        assert out == {"errors": [], "typed": 'LOAD"*",8,1 + RUN'}
    finally:
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.SID_RUNNER_REBOOT_REQUIRED.update(previous_required)


def test_rc4_explicit_reboot_clears_sid_recovery_flag(monkeypatch):
    previous_rest, previous_cmd = server.rest, server.cmd
    previous_required = set(server.SID_RUNNER_REBOOT_REQUIRED)

    class FakeRest:
        host = "192.0.2.68"
        def put(self, path, **kwargs):
            assert path == "/v1/machine:reboot"
            return {"ok": True}

    class FakeCmd:
        def close(self):
            pass

    try:
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.rest, server.cmd = FakeRest(), FakeCmd()
        server._sid_runner_mark_reboot_required(server.rest)
        monkeypatch.setattr(server, "_juke_disarm_machine_takeover", lambda reason: 1)
        monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: None)
        monkeypatch.setattr(server, "_send_boot_prekey", lambda **kwargs: None)
        assert server.machine("reboot") == {"ok": True}
        assert server._sid_runner_reboot_required(server.rest) is False
    finally:
        server.rest, server.cmd = previous_rest, previous_cmd
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.SID_RUNNER_REBOOT_REQUIRED.update(previous_required)


def test_rc4_mount_and_run_while_sid_is_playing_reboots_before_mount(monkeypatch):
    previous_rest, previous_cmd = server.rest, server.cmd
    previous_required = set(server.SID_RUNNER_REBOOT_REQUIRED)
    previous_juke = dict(server.JUKE)
    events = []

    class FakeTimer:
        def __init__(self):
            self.cancelled = False
        def cancel(self):
            self.cancelled = True

    class FakeRest:
        host = "192.0.2.69"
        def put(self, path, **kwargs):
            events.append(("put", path))
            return {"ok": True}
        def probe_info(self, request_timeout=1.5):
            events.append(("probe", request_timeout))
            return {"product": "Ultimate"}
        def mount_path(self, drive, path, mode=None):
            events.append(("mount", drive, path, mode))
        def mount_attachment(self, *args, **kwargs):
            raise AssertionError("unexpected attachment mount")

    class FakeCmd:
        def close(self):
            events.append(("cmd-close",))
        def type_petscii(self, data, **kwargs):
            events.append(("type", bytes(data)))

    timer = FakeTimer()
    try:
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.rest, server.cmd = FakeRest(), FakeCmd()
        server._sid_runner_mark_reboot_required(server.rest)
        server.JUKE.clear(); server.JUKE.update({
            "items": [server._juke_lazy_item("/HVSC/Live.sid", "Live.sid")],
            "index": 0, "playing": True, "shuffle": False, "radio": True,
            "song": 1, "timer": timer, "folder": "Current tune",
            "loading": False, "source": "test", "generation": 31,
            "stop_after_current": False,
        })
        monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: None)
        monkeypatch.setattr(server, "_temporary_crt_reboot_before_mount_run", lambda: False)
        monkeypatch.setattr(server, "_cartridge_preflight", lambda **kwargs: {
            "known": True, "classification": "none", "label": "None",
            "source": "test", "age_seconds": 0.0,
        })
        monkeypatch.setattr(server, "_remember_mount", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_boot_settle", lambda **kwargs: events.append(("settle",)))
        monkeypatch.setattr(server, "_temporary_crt_reboot_before_mount_run", lambda: False)
        monkeypatch.setattr(server, "_cartridge_preflight", lambda **kwargs: {
            "known": True, "classification": "none", "label": "None",
            "source": "test", "age_seconds": 0.0,
        })
        monkeypatch.setattr(server, "_bus_id_for", lambda drive: 8)
        monkeypatch.setattr(server, "_basic_ready_gate", lambda stage, **kwargs: "unsupported")
        monkeypatch.setattr(server.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)

        out = server._mount_and_boot("a", "unlinked", device_path="/Usb0/live-test.d64")

        reboot_at = events.index(("put", "/v1/machine:reboot"))
        mount_at = events.index(("mount", "a", "/Usb0/live-test.d64", "unlinked"))
        assert reboot_at < mount_at
        assert timer.cancelled is True
        assert server.JUKE["playing"] is False
        assert server.JUKE["radio"] is False
        assert server.JUKE["generation"] == 32
        assert server._sid_runner_reboot_required(server.rest) is False
        assert out == {"errors": [], "typed": 'LOAD"*",8,1 + RUN'}
    finally:
        server.rest, server.cmd = previous_rest, previous_cmd
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.SID_RUNNER_REBOOT_REQUIRED.update(previous_required)
        server.JUKE.clear(); server.JUKE.update(previous_juke)

# --- v1.9.0 Release Candidate 11: responsive Finder hand-off ---

def test_rc16_discovery_files_match_release_checksums():
    root = Path(server.ROOT)
    expected = {}
    for line in (root / "DISCOVERY-FROZEN.sha256").read_text(encoding="utf-8").splitlines():
        digest, filename = line.split(maxsplit=1)
        expected[filename.strip()] = digest
    assert hashlib.sha256((root / "discovery.py").read_bytes()).hexdigest() == expected["discovery.py"]
    assert hashlib.sha256((root / "discovery_transport.py").read_bytes()).hexdigest() == expected["discovery_transport.py"]

def test_rc11_connect_reuses_verified_discovery_and_skips_blocking_probes(monkeypatch):
    original_cfg = dict(server.CFG)
    original_live = {key: set(value) for key, value in server.DISCOVERY_LIVE_ADDRESSES.items()}
    original_caps = dict(server.INPUT_CAPABILITIES)
    calls = []

    class FakeRest:
        def __init__(self, host, password, coordinator=None): self.host = host
        def info(self): calls.append("info"); raise AssertionError("verified Finder result must be reused")
        def stream_stop(self, name): calls.append(("stop", name))
        def close(self): calls.append("close-rest")
        def set_timeout(self, timeout): calls.append(("timeout", timeout))

    class FakeCmd:
        def __init__(self, host, coordinator=None): self.host = host
        def close(self): calls.append("close-cmd")

    class FakeFS:
        def __init__(self, *args, **kwargs): pass

    class FakeCoordinator:
        @contextmanager
        def operation(self, *args, **kwargs): yield

    old_rest = FakeRest("192.0.2.10", "")
    old_cmd = FakeCmd("192.0.2.10")
    try:
        server.CFG.clear(); server.CFG.update(original_cfg)
        server.CFG["u64_host"] = "192.0.2.10"
        server.CFG["known_devices"] = {
            "uid:1": {
                "identity": "uid:1", "identity_source": "unique_id",
                "unique_id": "1", "hostname": "Ultimate", "product": "Ultimate 64",
                "firmware": "3.15", "core": "1.4B",
                "addresses": {
                    "192.0.2.10": {"ip": "192.0.2.10", "link_type": "wifi"},
                    "192.0.2.11": {"ip": "192.0.2.11", "link_type": "ethernet",
                                     "method": "wired-prefix", "mac": "02:15:41:00:00:01"},
                },
            }
        }
        server.DISCOVERY_LIVE_ADDRESSES.clear(); server.DISCOVERY_LIVE_ADDRESSES["uid:1"] = {"192.0.2.10", "192.0.2.11"}
        server.INPUT_CAPABILITIES.clear(); server.INPUT_CAPABILITIES["192.0.2.10"] = {
            "available": True, "pending": False, "mode": "matrix", "status": 200,
            "label": "CIA1 keyboard matrix", "host": "192.0.2.10",
        }
        monkeypatch.setattr(server, "UltimateREST", FakeRest)
        monkeypatch.setattr(server, "CommandSocket", FakeCmd)
        monkeypatch.setattr(server, "DeviceFS", FakeFS)
        monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
        monkeypatch.setattr(server, "rest", old_rest)
        monkeypatch.setattr(server, "cmd", old_cmd)
        monkeypatch.setattr(server, "devfs", FakeFS())
        monkeypatch.setattr(server.LINK_DETECTOR, "detect", lambda host, force=False: LinkObservation(host, "ethernet", "02:15:41:00:00:01", "wired-prefix"))
        monkeypatch.setattr(server, "save_config", lambda: None)
        monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: calls.append("release"))
        result = server.api_connect({"host": "192.0.2.11"})
        assert result["connected"] is True
        assert result["reused_discovery"] is True
        assert result["input"]["available"] is True
        assert result["input"]["host"] == "192.0.2.10"
        assert "info" not in calls
        assert "release" not in calls
    finally:
        server.CFG.clear(); server.CFG.update(original_cfg)
        server.DISCOVERY_LIVE_ADDRESSES.clear(); server.DISCOVERY_LIVE_ADDRESSES.update(original_live)
        server.INPUT_CAPABILITIES.clear(); server.INPUT_CAPABILITIES.update(original_caps)


def test_rc11_manual_connect_still_verifies(monkeypatch):
    calls = []
    class FakeRest:
        def __init__(self, host, password, coordinator=None): self.host = host
        def info(self): calls.append("info"); return {"product": "Ultimate 64", "unique_id": "manual"}
        def close(self): pass
        def set_timeout(self, timeout): pass
    class FakeCmd:
        def __init__(self, host, coordinator=None): self.host = host
        def close(self): pass
    class FakeFS:
        def __init__(self, *args, **kwargs): pass
    class FakeCoordinator:
        @contextmanager
        def operation(self, *args, **kwargs): yield
    monkeypatch.setattr(server, "UltimateREST", FakeRest)
    monkeypatch.setattr(server, "CommandSocket", FakeCmd)
    monkeypatch.setattr(server, "DeviceFS", FakeFS)
    monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
    monkeypatch.setattr(server, "rest", None)
    monkeypatch.setattr(server, "cmd", None)
    monkeypatch.setattr(server, "save_config", lambda: None)
    monkeypatch.setattr(server.LINK_DETECTOR, "detect", lambda host, force=False: LinkObservation(host, "unknown", "", "unknown"))
    result = server.api_connect({"host": "198.51.100.64"})
    assert result["connected"] is True
    assert result["reused_discovery"] is False
    assert calls == ["info"]


def test_rc11_frontend_does_not_refresh_after_finder_and_has_explicit_selection():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    finder_tail = js[js.index("async function runDiscover()"):js.index("async function clearDiscoveredDevices()")]
    assert "loadInfo();refreshDrives();" not in finder_tail
    assert "Use selected address" in js
    assert "disc-choice" in js and "selected" in js
    assert "const hadState=MATRIX_HELD.size>0" in js
    assert "INFO_IN_FLIGHT" in js and "DRIVES_IN_FLIGHT" in js


# --- v1.9.0 Release Candidate 10: shared proven discovery transport ---


def test_discovery_constants_match_proven_direct_get_design():
    import discovery

    assert discovery.CONNECT_TIMEOUT == 1.5
    assert discovery.RESPONSE_TIMEOUT == 3.25
    assert discovery.SCAN_CONCURRENCY == 64
    source = Path(discovery.__file__).read_text(encoding="utf-8")
    assert "discovery_transport.scan_direct" in source
    assert "httpx.AsyncClient" not in source
    assert "classify_address_group" not in source
    assert "_port_open" not in source
    assert "VERIFY_RETRY_TIMEOUT" not in source
    transport_source = Path(discovery.discovery_transport.__file__).read_text(encoding="utf-8")
    assert "socket.create_connection" in transport_source
    assert "urllib.request" not in transport_source


def test_supplied_discovery_diagnostic_imports_production_transport():
    source = (Path(server.ROOT) / "discovery_diagnostic.py").read_text(encoding="utf-8")
    assert "import discovery_transport" in source
    assert "urllib.request" not in source
    assert "ThreadPoolExecutor" not in source


def test_shared_transport_requests_each_address_exactly_once(monkeypatch):
    import discovery_transport

    calls = []

    def get_info(ip, connect_timeout, response_timeout, overall_started, stage, port=80):
        calls.append((ip, connect_timeout, response_timeout, stage, port))
        return {"ip": ip, "stage": stage, "status": "timeout", "elapsed_ms": 1.0}

    monkeypatch.setattr(discovery_transport, "get_info", get_info)
    ips = ["192.0.2.1", "192.0.2.2", "192.0.2.3"]
    rows = discovery_transport.scan_direct(
        ips, 2, 1.5, 3.25, time.perf_counter(), "direct-subnet", 80)

    assert sorted(ip for ip, _connect, _response, _stage, _port in calls) == ips
    assert all(connect == 1.5 for _ip, connect, _response, _stage, _port in calls)
    assert all(response == 3.25 for _ip, _connect, response, _stage, _port in calls)
    assert all(stage == "direct-subnet" for _ip, _connect, _response, stage, _port in calls)
    assert len(rows) == 3


def test_split_timeout_transport_applies_response_budget_only_after_connect(monkeypatch):
    import discovery_transport

    raw = (
        b"HTTP/1.1 200 OK\r\n"
        b"Connection: close\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 86\r\n\r\n"
        b'{"product":"Ultimate 64","hostname":"Unit","unique_id":"101090","errors":[]}'
    )
    # Keep the advertised length accurate for the test body.
    body = raw.split(b"\r\n\r\n", 1)[1]
    raw = raw.replace(b"Content-Length: 86", f"Content-Length: {len(body)}".encode())

    class FakeSocket:
        def __init__(self):
            self.response_timeouts = []
            self.sent = []
            self.chunks = [raw, b""]

        def settimeout(self, value):
            self.response_timeouts.append(value)

        def sendall(self, value):
            self.sent.append(value)

        def recv(self, _size):
            return self.chunks.pop(0)

        def close(self):
            pass

    fake = FakeSocket()
    connects = []

    def create_connection(address, timeout):
        connects.append((address, timeout))
        return fake

    monkeypatch.setattr(discovery_transport.socket, "create_connection", create_connection)
    row = discovery_transport.get_info(
        "192.0.2.64", 1.5, 3.25, time.perf_counter(), "direct-subnet", 80)

    assert connects == [(('192.0.2.64', 80), 1.5)]
    assert fake.response_timeouts == [3.25]
    assert b"GET /v1/info HTTP/1.1" in fake.sent[0]
    assert row["status"] == "ultimate"
    assert row["payload"]["unique_id"] == "101090"
    assert "connect_ms" in row
    assert "time_to_first_byte_ms" in row


def test_split_timeout_transport_distinguishes_connect_and_response_timeouts(monkeypatch):
    import discovery_transport

    def connect_timeout(_address, timeout):
        assert timeout == 1.5
        raise discovery_transport.socket.timeout("timed out")

    monkeypatch.setattr(discovery_transport.socket, "create_connection", connect_timeout)
    row = discovery_transport.get_info(
        "192.0.2.1", 1.5, 3.25, time.perf_counter(), "direct-subnet")
    assert row["status"] == "connect_timeout"

    class SlowResponseSocket:
        def settimeout(self, value):
            assert value == 3.25

        def sendall(self, _value):
            pass

        def recv(self, _size):
            raise discovery_transport.socket.timeout("timed out")

        def close(self):
            pass

    monkeypatch.setattr(
        discovery_transport.socket,
        "create_connection",
        lambda _address, timeout: SlowResponseSocket(),
    )
    row = discovery_transport.get_info(
        "192.0.2.64", 1.5, 3.25, time.perf_counter(), "direct-subnet")
    assert row["status"] == "response_timeout"
    assert "connect_ms" in row


def test_persisted_addresses_share_one_transport_pass_and_are_not_repeated(monkeypatch):
    import discovery

    ethernet = "192.0.2.10"
    wifi = "192.0.2.20"
    stages = []
    progress = []
    monkeypatch.setattr(discovery, "local_subnets", lambda: ["192.0.2."])

    def scan_direct(ips, workers, connect_timeout, response_timeout, overall_started, stage, port=80):
        ips = list(ips)
        stages.append((ips, workers, connect_timeout, response_timeout, stage, port))
        return [
            _transport_result(ip, stage=stage)
            for ip in ips if ip in {ethernet, wifi}
        ]

    monkeypatch.setattr(discovery.discovery_transport, "scan_direct", scan_direct)
    detector = _MappedLinkDetector({
        ethernet: ("ethernet", "02:15:41:F0:66:06", "wired-prefix"),
        wifi: ("wifi", "24:0A:C4:9F:77:C8", "espressif-oui"),
    })

    result = asyncio.run(discovery.discover(
        candidate_ips=[ethernet, wifi], detector=detector,
        progress_callback=lambda row: progress.append(row)))

    assert len(stages) == 1
    ips, workers, connect_timeout, response_timeout, stage, port = stages[0]
    assert ips[:2] == [ethernet, wifi]
    assert len(ips) == len(set(ips)) == 254
    assert (workers, connect_timeout, response_timeout, stage, port) == (
        64, 1.5, 3.25, "direct-all", 80)
    assert len(progress) == 1 and progress[0]["complete"] is True
    assert progress[0]["cached_verified_count"] == 2
    assert result["verified_count"] == 2
    assert result["devices"][0]["preferred_ip"] == ethernet
    assert any("persisted candidates" in line.lower() and ethernet in line and wifi in line
               for line in result["diagnostics"])

def test_off_subnet_persisted_address_is_skipped(monkeypatch):
    import discovery

    stages = []
    monkeypatch.setattr(discovery, "local_subnets", lambda: ["192.0.2."])

    def scan_direct(ips, workers, connect_timeout, response_timeout, overall_started, stage, port=80):
        stages.append((list(ips), workers, stage))
        return []

    monkeypatch.setattr(discovery.discovery_transport, "scan_direct", scan_direct)
    result = asyncio.run(discovery.discover(
        candidate_ips=["198.51.100.44"], detector=_MappedLinkDetector({})))

    assert len(stages) == 1
    assert len(stages[0][0]) == 254
    assert stages[0][1:] == (64, "direct-all")
    assert "198.51.100.44" not in stages[0][0]
    assert result["cached_candidate_count"] == 0
    assert result["candidate_count"] == 254

def test_fresh_shared_transport_scan_groups_interfaces_and_prefers_ethernet(monkeypatch):
    import discovery

    ethernet = "192.0.2.10"
    wifi = "192.0.2.20"
    stages = []
    monkeypatch.setattr(discovery, "local_subnets", lambda: ["192.0.2."])

    def scan_direct(ips, workers, connect_timeout, response_timeout, overall_started, stage, port=80):
        ips = list(ips)
        stages.append((ips, workers, connect_timeout, response_timeout, stage, port))
        return [
            _transport_result(ip, stage=stage)
            for ip in ips if ip in {ethernet, wifi}
        ]

    monkeypatch.setattr(discovery.discovery_transport, "scan_direct", scan_direct)
    detector = _MappedLinkDetector({
        ethernet: ("ethernet", "02:15:41:F0:66:06", "wired-prefix"),
        wifi: ("wifi", "24:0A:C4:9F:77:C8", "espressif-oui"),
    })

    result = asyncio.run(discovery.discover(detector=detector))

    assert len(stages) == 1
    assert len(stages[0][0]) == len(set(stages[0][0])) == 254
    assert stages[0][1:] == (64, 1.5, 3.25, "direct-all", 80)
    assert result["verified_count"] == 2
    assert len(result["devices"]) == 1
    device = result["devices"][0]
    assert device["preferred_ip"] == ethernet
    assert [(row["ip"], row["link_type"]) for row in device["addresses"]] == [
        (ethernet, "ethernet"), (wifi, "wifi"),
    ]

def test_discovery_classification_issues_no_latency_rest_requests(monkeypatch):
    import discovery

    ethernet = "192.0.2.10"
    wifi = "192.0.2.20"
    monkeypatch.setattr(discovery, "local_subnets", lambda: ["192.0.2."])

    def scan_direct(ips, workers, connect_timeout, response_timeout, overall_started, stage, port=80):
        return [
            _transport_result(ip, stage=stage)
            for ip in ips if ip in {ethernet, wifi}
        ]

    class CountingDetector(_MappedLinkDetector):
        def __init__(self, mapping):
            super().__init__(mapping)
            self.calls = []

        def detect(self, ip, *, force=False, **kwargs):
            self.calls.append((ip, force))
            return super().detect(ip, force=force, **kwargs)

    monkeypatch.setattr(discovery.discovery_transport, "scan_direct", scan_direct)
    detector = CountingDetector({
        ethernet: ("ethernet", "02:15:41:F0:66:06", "wired-prefix"),
        wifi: ("wifi", "24:0A:C4:9F:77:C8", "espressif-oui"),
    })
    result = asyncio.run(discovery.discover(detector=detector))

    assert result["verified_count"] == 2
    assert sorted(detector.calls) == sorted([(ethernet, True), (wifi, True)])


def test_stale_cached_ethernet_is_not_promoted(monkeypatch):
    import discovery

    ethernet = "192.0.2.10"
    wifi = "192.0.2.20"
    monkeypatch.setattr(discovery, "local_subnets", lambda: ["192.0.2."])

    def scan_direct(ips, workers, connect_timeout, response_timeout, overall_started, stage, port=80):
        return [
            _transport_result(ip, stage=stage)
            for ip in ips if ip == wifi
        ]

    monkeypatch.setattr(discovery.discovery_transport, "scan_direct", scan_direct)
    known = {
        "uid:101090": {
            "identity": "uid:101090", "unique_id": "101090",
            "hostname": "Ultimate-64-F06606", "product": "Ultimate 64",
            "addresses": {
                ethernet: {"ip": ethernet, "link_type": "ethernet",
                           "mac": "02:15:41:F0:66:06", "last_seen": "old"},
                wifi: {"ip": wifi, "link_type": "wifi",
                       "mac": "24:0A:C4:9F:77:C8", "last_seen": "old"},
            },
        }
    }
    detector = _MappedLinkDetector({
        wifi: ("wifi", "24:0A:C4:9F:77:C8", "espressif-oui"),
    })

    result = asyncio.run(discovery.discover(
        candidate_ips=[ethernet, wifi], known_devices=known, detector=detector))

    assert result["verified_count"] == 1
    assert result["devices"][0]["preferred_ip"] == wifi
    assert [row["ip"] for row in result["devices"][0]["addresses"]] == [wifi]
    assert known["uid:101090"]["addresses"][ethernet]["last_seen"] == "old"

def test_discovery_server_pauses_status_and_drives_polling(monkeypatch):
    original_host = server.CFG.get("u64_host")
    original_mount = copy.deepcopy(server.MOUNT_STATE)
    server.CFG["u64_host"] = "192.0.2.64"
    server.DISCOVERY_ACTIVE.set()
    try:
        info = server.info()
        drives = server.drives()
        assert info["u64deck_discovery_busy"] is True
        assert drives["u64deck_discovery_busy"] is True
        assert drives["u64deck_drives_unavailable"] is True
        assert "paused" in drives["u64deck_drives_message"].lower()
    finally:
        server.DISCOVERY_ACTIVE.clear()
        server.CFG["u64_host"] = original_host
        server.MOUNT_STATE.clear(); server.MOUNT_STATE.update(original_mount)


def test_discovery_scan_gate_rejects_overlap(monkeypatch):
    server.DISCOVERY_SCAN_LOCK.acquire()
    try:
        with pytest.raises(server.HTTPException) as exc:
            asyncio.run(server._run_discovery())
        assert exc.value.status_code == 409
    finally:
        server.DISCOVERY_SCAN_LOCK.release()
        server.DISCOVERY_ACTIVE.clear()


def test_discovery_frontend_pauses_polling_and_uses_bounded_window():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert "let DISCOVERY_SCAN_ACTIVE=false,DISCOVERY_DIALOG_OPEN=false;" in js
    assert "if(DISCOVERY_SCAN_ACTIVE||DISCOVERY_DIALOG_OPEN||uiInteractive()||INFO_IN_FLIGHT)return;" in js
    assert 'api("/api/discover"+' in js and "{timeoutMs:30000}" in js
    assert 'api("/api/discover/clear"+' in js and 'method:"POST",timeoutMs:30000' in js
    assert 'let DEVICE_REQUEST_TIMEOUT_MS=15000;' in js


def test_rc8_discovery_documentation_covers_identity_and_rest_etiquette():
    readme = (Path(server.ROOT) / "README.md").read_text(encoding="utf-8")
    help_js = (Path(server.ASSETS) / "static" / "help_content.js").read_text(encoding="utf-8")
    for text in (
        "Interface-aware Ultimate discovery",
        "Prioritised single-pass discovery",
        "REST service etiquette",
        "U64 Manager",
        "Assembly64",
        "every candidate receives exactly one direct",
    ):
        assert text in readme
    assert "one direct <code>GET /v1/info</code> request" in help_js
    assert "Routine status and Mounted Drives polling pause" in help_js


# --- v1.9.0 Release Candidate 12: split dual-interface routing ---

def _rc12_known_device():
    return {
        "uid:101090": {
            "identity": "uid:101090", "identity_source": "unique_id",
            "unique_id": "101090", "hostname": "Ultimate-64-F06606",
            "product": "Ultimate 64", "firmware": "3.15", "core": "1.4B",
            "addresses": {
                "192.0.2.170": {"ip": "192.0.2.170", "link_type": "wifi",
                                  "method": "esp32-oui", "mac": "24:0A:C4:00:00:01"},
                "192.0.2.171": {"ip": "192.0.2.171", "link_type": "ethernet",
                                  "method": "wired-prefix", "mac": "02:15:41:00:00:01"},
            },
        }
    }


def test_rc12_control_host_uses_only_live_verified_wifi():
    previous_cfg = dict(server.CFG)
    previous_live = {key: set(value) for key, value in server.DISCOVERY_LIVE_ADDRESSES.items()}
    try:
        server.CFG["known_devices"] = _rc12_known_device()
        server.DISCOVERY_LIVE_ADDRESSES.clear()
        server.DISCOVERY_LIVE_ADDRESSES["uid:101090"] = {"192.0.2.171"}
        assert server._verified_control_host_for_selected("192.0.2.171") == "192.0.2.171"
        server.DISCOVERY_LIVE_ADDRESSES["uid:101090"].add("192.0.2.170")
        assert server._verified_control_host_for_selected("192.0.2.171") == "192.0.2.170"
        assert server._verified_control_host_for_selected("192.0.2.170") == "192.0.2.170"
    finally:
        server.CFG.clear(); server.CFG.update(previous_cfg)
        server.DISCOVERY_LIVE_ADDRESSES.clear(); server.DISCOVERY_LIVE_ADDRESSES.update(previous_live)


def test_rc12_connect_keeps_ethernet_for_command_and_routes_rest_via_wifi(monkeypatch):
    previous_cfg = dict(server.CFG)
    previous_live = {key: set(value) for key, value in server.DISCOVERY_LIVE_ADDRESSES.items()}
    previous_rest, previous_cmd, previous_devfs = server.rest, server.cmd, server.devfs
    made = {"rest": [], "cmd": [], "fs": []}

    class FakeRest:
        def __init__(self, host, password, coordinator=None):
            self.host = host; made["rest"].append(host)
        def info(self): raise AssertionError("Finder result should be reused")
        def close(self): pass
        def set_timeout(self, timeout): self.timeout = timeout
    class FakeCmd:
        def __init__(self, host, coordinator=None): self.host = host; made["cmd"].append(host)
        def close(self): pass
    class FakeFS:
        def __init__(self, host, *args, **kwargs): self.host = host; made["fs"].append(host)
        def close(self): pass
    class FakeCoordinator:
        @contextmanager
        def operation(self, *args, **kwargs): yield

    try:
        server.CFG["u64_host"] = ""
        server.CFG["rest_control_host"] = ""
        server.CFG["known_devices"] = _rc12_known_device()
        server.DISCOVERY_LIVE_ADDRESSES.clear()
        server.DISCOVERY_LIVE_ADDRESSES["uid:101090"] = {"192.0.2.170", "192.0.2.171"}
        monkeypatch.setattr(server, "UltimateREST", FakeRest)
        monkeypatch.setattr(server, "CommandSocket", FakeCmd)
        monkeypatch.setattr(server, "DeviceFS", FakeFS)
        monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
        monkeypatch.setattr(server, "rest", FakeRest("", ""))
        monkeypatch.setattr(server, "cmd", FakeCmd(""))
        monkeypatch.setattr(server, "devfs", FakeFS(""))
        monkeypatch.setattr(server.LINK_DETECTOR, "detect", lambda host, force=False: LinkObservation(host, "ethernet", "02:15:41:00:00:01", "wired-prefix"))
        monkeypatch.setattr(server, "save_config", lambda: None)
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
        result = server.api_connect({"host": "192.0.2.171"})
        assert result["connected"] is True
        assert result["host"] == "192.0.2.171"
        assert result["control_host"] == "192.0.2.170"
        assert result["rest_via_alternate"] is True
        assert server.rest.host == "192.0.2.170"
        assert server.cmd.host == "192.0.2.171"
        assert server.devfs.host == "192.0.2.171"
        assert server.CFG["u64_host"] == "192.0.2.171"
        assert server.CFG["rest_control_host"] == "192.0.2.170"
        assert result["link"]["rest_route_label"] == "REST via Wi-Fi"
    finally:
        server.CFG.clear(); server.CFG.update(previous_cfg)
        server.DISCOVERY_LIVE_ADDRESSES.clear(); server.DISCOVERY_LIVE_ADDRESSES.update(previous_live)
        server.rest, server.cmd, server.devfs = previous_rest, previous_cmd, previous_devfs


def test_rc12_split_stream_uses_ethernet_command_socket_first(monkeypatch):
    previous_cfg = dict(server.CFG)
    previous_rest, previous_cmd = server.rest, server.cmd
    previous_state = dict(server.STREAM_STATE)
    calls = []
    class FakeRest:
        host = "192.0.2.170"
        def stream_start(self, name, dest): calls.append(("rest-start", name, dest))
        def stream_stop(self, name): calls.append(("rest-stop", name))
    class FakeCmd:
        host = "192.0.2.171"
        def stream_on(self, stream_id, dest): calls.append(("socket-start", stream_id, dest))
        def stream_off(self, stream_id): calls.append(("socket-stop", stream_id))
    class FakeRecv:
        def set_multicast(self, *args): pass
    try:
        server.CFG["u64_host"] = "192.0.2.171"
        server.CFG["stream_transport"] = "unicast"
        server.rest, server.cmd = FakeRest(), FakeCmd()
        monkeypatch.setattr(server, "video", FakeRecv())
        monkeypatch.setattr(server, "_local_ip", lambda: "192.0.2.50")
        monkeypatch.setattr(server, "_current_link_payload", lambda: {"link_type": "ethernet"})
        server._stream_ctl("video", True)
        assert calls == [("socket-start", 0, "192.0.2.50:11000")]
        assert server.STREAM_LAST["video"]["via"] == "socket"
    finally:
        server.CFG.clear(); server.CFG.update(previous_cfg)
        server.rest, server.cmd = previous_rest, previous_cmd
        server.STREAM_STATE.clear(); server.STREAM_STATE.update(previous_state)


def test_rc12_ui_explains_split_route():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert "REST via Wi-Fi" in js
    assert "REST control via "+"" in js


# --- v1.9.0 Release Candidate 14: split SID play/stop routing ---

def test_rc13_split_juke_stop_parks_cartridge_and_uses_wifi_rest_first(monkeypatch):
    previous_cfg = dict(server.CFG)
    previous_juke = dict(server.JUKE)
    previous_rest, previous_cmd = server.rest, server.cmd
    previous_required = set(server.SID_RUNNER_REBOOT_REQUIRED)
    calls = []
    ethernet = "192.0.2.171"
    wifi = "192.0.2.170"
    cartridge = "/Flash/RetroReplay.crt"

    class FakeRest:
        host = wifi

        def get_json(self, path):
            calls.append(("get", path))
            return {server._CART_CAT: {server._CART_ITEM: cartridge}}

        def put(self, path, **kwargs):
            calls.append(("put", path, kwargs))
            return {}

    class FakeCommand:
        def reset_fresh(self):
            calls.append("fresh-reset")

    try:
        server.CFG.update({
            "u64_host": ethernet,
            "rest_control_host": wifi,
            "cart_safe_run": True,
        })
        server.JUKE.update({"items": [], "index": -1, "playing": True,
                            "stop_after_current": False, "timer": None})
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.SID_RUNNER_REBOOT_REQUIRED.add(wifi)
        monkeypatch.setattr(server, "rest", FakeRest())
        monkeypatch.setattr(server, "cmd", FakeCommand())
        monkeypatch.setattr(server, "_matrix_release_all",
                            lambda **kwargs: calls.append(("release", kwargs)))
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_warn_event", lambda *args, **kwargs: None)

        out = server.juke_stop()

        assert calls == [
            ("get", f"/v1/configs/{server._CART_CAT}"),
            ("put", f"/v1/configs/{server._CART_CAT}/{server._CART_ITEM}",
             {"value": "", "request_timeout": 4.0}),
            ("put", "/v1/machine:reset", {"request_timeout": 4.0}),
            ("put", f"/v1/configs/{server._CART_CAT}/{server._CART_ITEM}",
             {"value": cartridge, "request_timeout": 4.0}),
            ("release", {"silent": True, "cached_only": True, "caller": "juke-stop"}),
        ]
        assert "fresh-reset" not in calls
        assert wifi in server.SID_RUNNER_REBOOT_REQUIRED
        assert out["stop_delivery"] == "cartridge-safe REST"
        assert out["stop_cartridge_safe"] is True
        assert out["stop_elapsed_ms"] >= 0
    finally:
        server.CFG.clear(); server.CFG.update(previous_cfg)
        server.JUKE.clear(); server.JUKE.update(previous_juke)
        server.rest, server.cmd = previous_rest, previous_cmd
        server.SID_RUNNER_REBOOT_REQUIRED.clear()
        server.SID_RUNNER_REBOOT_REQUIRED.update(previous_required)


def test_rc13_split_juke_stop_rest_failure_restores_cart_then_falls_back(monkeypatch):
    previous_cfg = dict(server.CFG)
    previous_juke = dict(server.JUKE)
    previous_rest, previous_cmd = server.rest, server.cmd
    calls = []
    ethernet = "192.0.2.171"
    wifi = "192.0.2.170"
    cartridge = "/Flash/RetroReplay.crt"

    class FakeRest:
        host = wifi

        def get_json(self, path):
            calls.append(("get", path))
            return {server._CART_CAT: {server._CART_ITEM: cartridge}}

        def put(self, path, **kwargs):
            calls.append(("put", path, kwargs))
            if path == "/v1/machine:reset":
                raise RuntimeError("REST reset failed")
            return {}

    class FakeCommand:
        def reset_fresh(self):
            calls.append("fresh-reset")

    try:
        server.CFG.update({
            "u64_host": ethernet,
            "rest_control_host": wifi,
            "cart_safe_run": True,
        })
        server.JUKE.update({"items": [], "index": -1, "playing": True,
                            "stop_after_current": False, "timer": None})
        monkeypatch.setattr(server, "rest", FakeRest())
        monkeypatch.setattr(server, "cmd", FakeCommand())
        monkeypatch.setattr(server, "_matrix_release_all",
                            lambda **kwargs: calls.append(("release", kwargs)))
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_warn_event", lambda *args, **kwargs: None)

        server.juke_stop()

        reset_index = calls.index(("put", "/v1/machine:reset", {"request_timeout": 4.0}))
        restore_index = calls.index((
            "put", f"/v1/configs/{server._CART_CAT}/{server._CART_ITEM}",
            {"value": cartridge, "request_timeout": 4.0},
        ))
        release_index = calls.index(("release", {"silent": True, "cached_only": True, "caller": "juke-stop"}))
        fallback_index = calls.index("fresh-reset")
        assert reset_index < restore_index < release_index < fallback_index
    finally:
        server.CFG.clear(); server.CFG.update(previous_cfg)
        server.JUKE.clear(); server.JUKE.update(previous_juke)
        server.rest, server.cmd = previous_rest, previous_cmd


def test_rc13_juke_play_returns_stage_timings_and_logs_them(monkeypatch):
    previous_juke = dict(server.JUKE)
    previous_cfg = dict(server.CFG)
    diagnostics = []
    item = {
        "label": "Timing.sid",
        "path": "/HVSC/Timing.sid",
        "data": b"sid-data",
        "meta": {"name": "Timing", "start_song": 1, "songs": 1},
    }
    try:
        server.CFG["sid_default_secs"] = 0
        server.JUKE.clear(); server.JUKE.update({
            "items": [item], "index": -1, "playing": False,
            "shuffle": False, "radio": False, "song": 0, "timer": None,
            "folder": "", "loading": False, "source": "test",
            "generation": 0, "stop_after_current": False,
        })
        monkeypatch.setattr(server, "_cart_configured", lambda: "")
        monkeypatch.setattr(server, "_post_sid_upload", lambda *args, **kwargs: {})
        monkeypatch.setattr(server, "_diag_event",
                            lambda level, message, **kwargs: diagnostics.append((level, message)))

        out = server._juke_play(0)

        timing = out["play_timing"]
        for key in (
            "coordinator_wait_ms", "materialise_ms", "cart_lookup_ms",
            "runner_action_ms", "cart_safe_total_ms", "state_commit_ms", "total_ms",
        ):
            assert key in timing
            assert timing[key] >= 0
        assert timing["cartridge_configured"] is False
        assert any("SID Jukebox Play timing:" in message for _level, message in diagnostics)
    finally:
        server.JUKE.clear(); server.JUKE.update(previous_juke)
        server.CFG.clear(); server.CFG.update(previous_cfg)


# --- v1.9.0 Release Candidate 16: persisted Finder state and UI responsiveness ---

def test_rc16_frontend_flushes_scheduled_audio_and_caps_queue_ahead():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert "const AUDIO_SOURCES=new Set(),AUDIO_MAX_AHEAD=0.32" in js
    assert "AUDIO_JUKE_STOP_MUTED" in js
    assert "function flushBrowserAudio()" in js
    assert "AUDIO_MAX_AHEAD" in js
    assert "audioDroppedAhead" in js
    stop = js[js.index("async function jkStop()") : js.index("async function jk(a)")]
    assert "flushBrowserAudio();" in stop
    assert "AUDIO_JUKE_STOP_MUTED=true" in stop
    assert "AUDIO_JUKE_STOP_MUTED=false" in stop
    audio_stop = js[js.index("async function toggleAudio()") : js.index("/* ---------- flexible video")]
    assert "flushBrowserAudio();" in audio_stop


def test_rc16_diagnostics_export_explicitly_writes_and_closes_file_handle():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    block = js[js.index("async function downloadDiagnostics()") : js.index("/* ---------- discovery ----------")]
    assert "showSaveFilePicker" in block
    assert "handle.createWritable()" in block
    assert "await writable.write(blob)" in block
    assert "await writable.close()" in block
    assert "await writable.abort()" in block
    assert "document.body.appendChild(a);a.click();a.remove()" in block


def test_rc16_connect_ui_is_immediate_and_backend_returns_stage_timings(monkeypatch):
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    connect_block = js[js.index("async function connectTo(host)") : js.index("/* ---------- stream quality")]
    assert 'toast("Connecting to "+host+"…","ok")' in connect_block
    assert "connect_timing?.total_ms" in connect_block

    calls = []
    class FakeRest:
        def __init__(self, host, password, coordinator=None): self.host = host
        def info(self): calls.append("info"); return {"product": "Ultimate 64", "unique_id": "rc16"}
        def close(self): calls.append("close-rest")
        def set_timeout(self, timeout): pass
    class FakeCmd:
        def __init__(self, host, coordinator=None): self.host = host
        def close(self): calls.append("close-cmd")
    class FakeFS:
        def __init__(self, *args, **kwargs): pass
        def close(self): calls.append("close-fs")
    class FakeCoordinator:
        @contextmanager
        def operation(self, *args, **kwargs): yield

    original_cfg = dict(server.CFG)
    events = []
    try:
        server.CFG["u64_host"] = ""
        server.CFG["rest_control_host"] = ""
        server.CFG["known_devices"] = {}
        monkeypatch.setattr(server, "UltimateREST", FakeRest)
        monkeypatch.setattr(server, "CommandSocket", FakeCmd)
        monkeypatch.setattr(server, "DeviceFS", FakeFS)
        monkeypatch.setattr(server, "DEVICE_OP", FakeCoordinator())
        monkeypatch.setattr(server, "rest", None)
        monkeypatch.setattr(server, "cmd", None)
        monkeypatch.setattr(server, "devfs", None)
        monkeypatch.setattr(server, "save_config", lambda: None)
        monkeypatch.setattr(server.LINK_DETECTOR, "detect", lambda host, force=False: LinkObservation(host, "unknown", "", "unknown"))
        monkeypatch.setattr(server, "_diag_event", lambda level, message, **extra: events.append((level, message)))
        result = server.api_connect({"host": "198.51.100.64"})
        assert result["connected"] is True
        timing = result["connect_timing"]
        for key in (
            "persisted_route_lookup_ms", "client_creation_ms", "verified_result_lookup_ms",
            "live_verification_ms", "coordinator_wait_ms", "capability_handling_ms",
            "backend_replace_commit_ms", "old_client_cleanup_ms", "total_ms",
        ):
            assert key in timing and timing[key] >= 0
        assert timing["success"] is True
        assert any("Connect timing:" in message for _level, message in events)
    finally:
        server.CFG.clear(); server.CFG.update(original_cfg)


def test_rc16_finder_dialog_pauses_routine_browser_polling():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert "DISCOVERY_DIALOG_OPEN=true" in js
    assert "function closeDiscover(resumePolling=true)" in js
    assert "DISCOVERY_SCAN_ACTIVE||DISCOVERY_DIALOG_OPEN||uiInteractive()||INFO_IN_FLIGHT" in js
    assert "DISCOVERY_SCAN_ACTIVE||DISCOVERY_DIALOG_OPEN||uiInteractive()||DRIVES_IN_FLIGHT" in js


def test_rc16_discovery_diagnostic_uses_one_production_pass():
    source = (Path(server.ROOT) / "discovery_diagnostic.py").read_text(encoding="utf-8")
    assert "ONE PASS" in source
    assert 'started, "direct-all"' in source
    assert '"cached-first"' not in source
    assert '"direct-subnet"' not in source

# --- v1.9.0 Release Candidate 15: full CIA1 Mount & Run command delivery ---

def test_rc15_mount_run_matrix_events_encode_complete_load_line_in_one_batch():
    events = server._mount_run_matrix_events('LOAD"*",8,1\r')

    assert events[0] == {"kind": "release_all"}
    assert events[1:5] == [
        {"kind": "keyboard", "inputs": ["l"], "transition": "tap"},
        {"kind": "keyboard", "inputs": ["o"], "transition": "tap"},
        {"kind": "keyboard", "inputs": ["a"], "transition": "tap"},
        {"kind": "keyboard", "inputs": ["d"], "transition": "tap"},
    ]
    assert {"kind": "keyboard", "inputs": ["left_shift", "2"], "transition": "tap"} in events
    assert {"kind": "keyboard", "inputs": ["star"], "transition": "tap"} in events
    assert events[-1] == {"kind": "keyboard", "inputs": ["return"], "transition": "tap"}
    assert len(events) == 13  # release_all plus 12 key taps


def test_rc15_mount_run_load_uses_one_matrix_request_and_never_legacy(monkeypatch):
    matrix_calls = []
    typed = []
    diagnostics = []
    monkeypatch.setattr(server, "_input_status", lambda *args, **kwargs: {"available": True})
    monkeypatch.setattr(server, "_matrix_send",
                        lambda events, **kwargs: matrix_calls.append((events, kwargs)))
    monkeypatch.setattr(server, "_legacy_type", lambda data, **kwargs: typed.append(bytes(data)))
    monkeypatch.setattr(server, "_diag_event",
                        lambda level, message, **kwargs: diagnostics.append((level, message)))

    ok, delivery = server._type_mount_run_load(b'LOAD"*",8,1\r')

    assert ok is True
    assert delivery == "CIA1 matrix"
    assert typed == []
    assert matrix_calls == [
        (server._mount_run_matrix_events('LOAD"*",8,1\r'), {"client": server.rest})
    ]
    assert any("12 ordered key taps" in message for _level, message in diagnostics)


def test_rc15_matrix_load_failure_is_not_resent_through_legacy(monkeypatch):
    typed = []
    warnings = []
    monkeypatch.setattr(server, "_input_status", lambda *args, **kwargs: {"available": True})
    monkeypatch.setattr(
        server, "_matrix_send",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("ambiguous timeout")),
    )
    monkeypatch.setattr(server, "_legacy_type", lambda data, **kwargs: typed.append(bytes(data)))
    monkeypatch.setattr(server, "_warn_event",
                        lambda kind, message, **kwargs: warnings.append((kind, message)))
    monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)

    ok, note = server._type_mount_run_load(b'LOAD"*",8,1\r')

    assert ok is False
    assert typed == []
    assert "LOAD not resent" in note
    assert warnings and warnings[0][0] == "mount-load-delivery"


def test_rc15_legacy_mount_run_load_retains_one_shot_buffer_path(monkeypatch):
    typed = []
    matrix_calls = []
    monkeypatch.setattr(server, "_input_status", lambda *args, **kwargs: {"available": False})
    monkeypatch.setattr(server, "_matrix_send", lambda *args, **kwargs: matrix_calls.append(args))
    monkeypatch.setattr(server, "_legacy_type", lambda data, **kwargs: typed.append(bytes(data)))
    monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)

    ok, delivery = server._type_mount_run_load(b'LOAD"*",8,1\r')

    assert ok is True
    assert delivery == "Legacy KERNAL buffer"
    assert typed == [b'LOAD"*",8,1\r']
    assert matrix_calls == []


def test_rc14_mount_run_aborts_when_known_matrix_release_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_cached_input_status", lambda client=None: {"available": True})
    monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: False)
    monkeypatch.setattr(server, "_juke_disarm_machine_takeover", lambda reason: calls.append("disarm"))
    monkeypatch.setattr(server, "_warn_event", lambda *args, **kwargs: calls.append("warn"))

    class FakeRest:
        host = "192.0.2.140"
        def mount_path(self, *args, **kwargs):
            calls.append("mount")

    previous = server.rest
    try:
        server.rest = FakeRest()
        out = server._mount_and_boot("a", "unlinked", device_path="/Usb0/test.d64")
    finally:
        server.rest = previous

    assert out["typed"] == ""
    assert "matrix release failed" in out["note"]
    assert calls == ["disarm", "warn"]


def test_rc14_matrix_cleanup_coalesces_duplicate_request(monkeypatch):
    called = []
    monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: called.append(kwargs) or True)
    assert server.MATRIX_CLEANUP_LOCK.acquire(blocking=False)
    try:
        assert server._matrix_release_cleanup(caller="duplicate") is False
    finally:
        server.MATRIX_CLEANUP_LOCK.release()
    assert called == []


def test_rc14_matrix_release_warning_identifies_caller(monkeypatch):
    warnings = []
    monkeypatch.setattr(server, "_input_status", lambda *args, **kwargs: {"available": True})
    monkeypatch.setattr(
        server, "_matrix_send",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("timed out")),
    )
    monkeypatch.setattr(
        server, "_warn_event",
        lambda kind, message, **kwargs: warnings.append((kind, message)),
    )

    assert server._matrix_release_all(silent=True, caller="unit-test") is False
    assert warnings == [
        ("matrix-release-all", "could not release matrix input (unit-test): timed out")
    ]


def test_rc14_audio_disconnect_no_longer_releases_matrix_and_video_offloads_cleanup():
    import inspect
    audio_source = inspect.getsource(server.ws_audio)
    video_source = inspect.getsource(server.ws_video)

    assert "_matrix_release_all" not in audio_source
    assert "_matrix_release_cleanup" not in audio_source
    assert "run_in_threadpool" in video_source
    assert "_matrix_release_cleanup" in video_source
    assert "ws-video-disconnect" in video_source

def test_rc15_browser_socket_closes_do_not_issue_matrix_release_requests():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    video_close = js[js.index("wsV.onclose"):js.index("async function toggleVideo")]
    audio_close = js[js.index("wsA.onclose"):js.index("async function toggleAudio")]

    assert 'matrixReleaseAll("video WebSocket closed")' not in video_close
    assert "matrixClearLocalState()" in video_close
    assert 'matrixReleaseAll("audio WebSocket closed")' not in audio_close
    assert "matrixClearLocalState()" not in audio_close


def test_rc15_stream_control_no_longer_releases_matrix_on_stop():
    import inspect
    source = inspect.getsource(server._stream_ctl)
    assert "_matrix_release_all" not in source
    assert "_matrix_release_cleanup" not in source



# --- v1.9.0 Release Candidate 18: local graceful exit ---

def test_rc17_exit_ui_and_help_are_present():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    help_js = (static / "help_content.js").read_text(encoding="utf-8")
    assert 'id="btnAppExit"' in html
    assert 'onclick="exitU64deck()"' in html
    assert '"/api/app/exit"' in js
    assert '"X-U64deck-Local-Exit":"1"' in js
    assert "The connected Ultimate is still running." in js
    assert "Exit u64deck" in help_js
    assert "The Ultimate itself keeps running" in help_js


def test_rc17_exit_is_loopback_only_and_requires_confirmation_header(monkeypatch):
    from types import SimpleNamespace

    scheduled = []
    events = []
    monkeypatch.setattr(server, "_schedule_app_exit", lambda delay=0.20: scheduled.append(delay))
    monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: events.append((args, kwargs)))
    server._APP_EXIT_REQUESTED.clear()
    try:
        remote = SimpleNamespace(
            client=SimpleNamespace(host="192.168.1.25"),
            headers={server._LOCAL_EXIT_HEADER: "1"},
        )
        with pytest.raises(HTTPException) as exc:
            server.app_exit(remote)
        assert exc.value.status_code == 403
        assert scheduled == []

        missing_header = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"), headers={}
        )
        with pytest.raises(HTTPException) as exc:
            server.app_exit(missing_header)
        assert exc.value.status_code == 403
        assert scheduled == []

        local = SimpleNamespace(
            client=SimpleNamespace(host="::1"),
            headers={server._LOCAL_EXIT_HEADER: "1"},
        )
        result = server.app_exit(local)
        assert result["stopping"] is True
        assert scheduled == [0.20]
        assert events and "Exit requested" in events[0][0][1]

        # A double click is idempotent and does not schedule another shutdown.
        assert server.app_exit(local)["stopping"] is True
        assert scheduled == [0.20]
    finally:
        server._APP_EXIT_REQUESTED.clear()


def test_rc17_managed_server_exit_and_dedicated_edge_cleanup(monkeypatch):
    class DummyServer:
        should_exit = False

    class DummyProcess:
        def __init__(self): self.terminated = False
        def poll(self): return None
        def terminate(self): self.terminated = True

    dummy_server = DummyServer()
    dummy_process = DummyProcess()
    previous_server = server._UVICORN_SERVER
    previous_process = server._BROWSER_PROCESS
    server._UVICORN_SERVER = dummy_server
    server._BROWSER_PROCESS = dummy_process
    monkeypatch.setattr(server.time, "sleep", lambda _delay: None)
    try:
        thread = server._schedule_app_exit()
        thread.join(timeout=1.0)
        assert dummy_server.should_exit is True
        server._close_launched_edge_app()
        assert dummy_process.terminated is True
        assert server._BROWSER_PROCESS is None
    finally:
        server._UVICORN_SERVER = previous_server
        server._BROWSER_PROCESS = previous_process


def test_rc18_frozen_exit_waits_for_cleanup_and_retires_process(monkeypatch):
    class DummyServer:
        should_exit = False

    dummy_server = DummyServer()
    previous_server = server._UVICORN_SERVER
    previous_frozen = server.FROZEN
    exits = []
    server._UVICORN_SERVER = dummy_server
    server.FROZEN = True
    server._APP_EXIT_CLEANUP_COMPLETE.set()
    monkeypatch.setattr(server.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(server.os, "_exit", lambda code: exits.append(code))
    try:
        thread = server._schedule_app_exit(frozen_exit_timeout=1.0)
        assert thread.daemon is False
        thread.join(timeout=1.0)
        assert dummy_server.should_exit is True
        assert exits == [0]
    finally:
        server._UVICORN_SERVER = previous_server
        server.FROZEN = previous_frozen
        server._APP_EXIT_CLEANUP_COMPLETE.clear()


def test_rc18_cleanup_marks_completion_and_closes_owned_edge(monkeypatch):
    closed = []
    previous_requested = server._APP_EXIT_REQUESTED.is_set()
    previous_store = server._INDEX_STORE
    previous_store_path = server._INDEX_STORE_PATH
    previous_thread = server._INDEX_THREAD
    previous_running = server.INDEXJOB.get("running")
    previous_resources = (server.cmd, server.rest, server.video, server.audio)
    server._APP_EXIT_REQUESTED.set()
    server._APP_EXIT_CLEANUP_COMPLETE.clear()
    server._INDEX_STORE = None
    server._INDEX_STORE_PATH = ""
    server._INDEX_THREAD = None
    server.INDEXJOB["running"] = False
    server.cmd = server.rest = server.video = server.audio = None
    monkeypatch.setattr(server, "_juke_cancel_timer", lambda: None)
    monkeypatch.setattr(server, "_matrix_release_all", lambda **_kwargs: None)
    monkeypatch.setattr(server, "_close_launched_edge_app", lambda: closed.append(True))
    try:
        server._clean_shutdown()
        assert server._APP_EXIT_CLEANUP_COMPLETE.is_set()
        assert closed == [True]
    finally:
        if not previous_requested:
            server._APP_EXIT_REQUESTED.clear()
        server._APP_EXIT_CLEANUP_COMPLETE.clear()
        server._INDEX_STORE = previous_store
        server._INDEX_STORE_PATH = previous_store_path
        server._INDEX_THREAD = previous_thread
        server.INDEXJOB["running"] = previous_running
        server.cmd, server.rest, server.video, server.audio = previous_resources


def test_rc18_executable_icon_and_launcher_exit_contract():
    root = Path(server.ROOT)
    icon = root / "u64deck.ico"
    assert icon.is_file()
    data = icon.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00"
    count = int.from_bytes(data[4:6], "little")
    assert count == 6
    sizes = []
    for entry in range(count):
        offset = 6 + (entry * 16)
        width = data[offset] or 256
        height = data[offset + 1] or 256
        sizes.append((width, height))
    assert sizes == [(16, 16), (32, 32), (48, 48),
                     (64, 64), (128, 128), (256, 256)]

    spec = (root / "u64deck.spec").read_text(encoding="utf-8")
    assert 'icon="u64deck.ico"' in spec
    assert "icon=None" not in spec

    launcher = (root / "start.bat").read_text(encoding="utf-8").lower()
    assert 'set "rc=%errorlevel%"' in launcher
    assert 'if not "%rc%"=="0"' in launcher
    assert launcher.rstrip().endswith("exit /b %rc%")
    assert "\npause\n" not in launcher


def test_rc18_windows_workflow_exercises_icon_and_normal_edge_exit():
    workflow = (Path(server.ROOT) / ".github" / "workflows" / "build-exe.yml").read_text(encoding="utf-8")
    assert "Verify embedded executable icon" in workflow
    assert "RT_GROUP_ICON resource is missing" in workflow
    assert "Smoke test normal Edge app exit" in workflow
    assert 'ArgumentList "--u64","203.0.113.9"' in workflow
    assert '"--no-browser"' not in workflow
    assert "u64deck.exe and its console remained" in workflow
    assert "dedicated Edge app process remained" in workflow


def test_rc17_readme_carries_canonical_dual_interface_warnings_verbatim():
    readme = (Path(server.ROOT) / "README.md").read_text(encoding="utf-8")
    block1 = """> **⚠️ Recommendation: run your Ultimate with a single active interface.**
> With both Ethernet and Wi-Fi enabled, the firmware behaviour above makes
> control intermittently unreliable in practice — the same operation can
> succeed one minute and time out the next, even with u64deck's split
> routing working around the worst of it. **Ethernet-only** (disable Wi-Fi
> in the Ultimate's network settings, then power-cycle) is the
> configuration everything is happiest in. Dual-interface operation works,
> but treat it as best-effort until the firmware behaviour changes."""
    block2 = """- **Dual-interface (Ethernet + Wi-Fi both enabled) is best-effort.** The
  firmware's ~2.5 s wired REST delay makes mixed-interface control
  intermittently flaky; split routing reduces but does not eliminate it.
  Single-interface — ideally Ethernet-only — is the reliable setup."""
    assert block1 in readme
    assert block2 in readme
    assert readme.index(block1) < readme.index("# u64deck")
    limitations = readme.index("## Known limitations (honest ones)")
    cache_bullet = readme.index("- One image at a time is cached in RAM per browse (last 8 kept).", limitations)
    assert readme.index(block2, limitations) > cache_bullet
    assert readme.index(block2, limitations) < readme.index("## Files", limitations)


# --- v1.9.0 Release Candidate 19: local System Health dashboard ---

def test_rc19_health_ui_help_and_local_poll_contract():
    static = Path(server.ASSETS) / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    help_js = (static / "help_content.js").read_text(encoding="utf-8")
    assert 'data-tab="health"' in html
    assert 'id="tab-health"' in html
    assert 'id="healthRestLatestP95"' in html
    assert 'id="healthVideoRate"' in html
    assert 'id="healthProcessLoad"' in html
    assert 'id="healthActiveOperation"' in html
    assert 'id="healthSummaryBadge"' in html
    assert 'id="healthOperationHistory"' in html
    assert 'api("/api/health"' in js
    assert 'setInterval(healthLoad,2000)' in js
    assert 'document.hidden' in js
    assert '#tab-health.active' in css
    assert 'does not call the Ultimate' in help_js
    assert 'FPGA utilisation or temperature' in help_js


def test_rc19_health_latency_summary_uses_existing_info_samples():
    previous_samples = list(server._HEALTH_REST_LATENCIES)
    previous_device = dict(server._HEALTH_LAST_DEVICE)
    previous_device_at = server._HEALTH_LAST_DEVICE_AT
    previous_error = server._HEALTH_LAST_INFO_ERROR
    previous_error_at = server._HEALTH_LAST_INFO_ERROR_AT
    previous_successes = server._HEALTH_REST_SUCCESSES
    previous_failures = server._HEALTH_REST_FAILURES
    previous_consecutive = server._HEALTH_REST_CONSECUTIVE_FAILURES
    try:
        server._HEALTH_REST_LATENCIES.clear()
        server._HEALTH_LAST_DEVICE.clear()
        server._HEALTH_LAST_DEVICE_AT = 0.0
        server._HEALTH_LAST_INFO_ERROR = ""
        server._HEALTH_LAST_INFO_ERROR_AT = 0.0
        server._HEALTH_REST_SUCCESSES = 0
        server._HEALTH_REST_FAILURES = 0
        server._HEALTH_REST_CONSECUTIVE_FAILURES = 0
        server._health_record_info({
            "product": "Ultimate 64", "firmware_version": "3.15",
            "fpga_version": "122", "core_version": "1.4B",
            "hostname": "Ultimate-Test", "unique_id": "101090",
        }, 20.0)
        server._health_record_info({"product": "Ultimate 64"}, 40.0)
        snap = server._health_latency_snapshot()
        assert snap["samples"] == 2
        assert snap["latest_ms"] == 40.0
        assert snap["minimum_ms"] == 20.0
        assert snap["average_ms"] == 30.0
        assert snap["maximum_ms"] == 40.0
        assert snap["p95_ms"] == 40.0
        assert snap["successes"] == 2
        assert snap["success_rate_percent"] == 100.0
        assert snap["device"]["product"] == "Ultimate 64"
        assert snap["last_success_age_seconds"] is not None
    finally:
        server._HEALTH_REST_LATENCIES.clear()
        server._HEALTH_REST_LATENCIES.extend(previous_samples)
        server._HEALTH_LAST_DEVICE.clear()
        server._HEALTH_LAST_DEVICE.update(previous_device)
        server._HEALTH_LAST_DEVICE_AT = previous_device_at
        server._HEALTH_LAST_INFO_ERROR = previous_error
        server._HEALTH_LAST_INFO_ERROR_AT = previous_error_at
        server._HEALTH_REST_SUCCESSES = previous_successes
        server._HEALTH_REST_FAILURES = previous_failures
        server._HEALTH_REST_CONSECUTIVE_FAILURES = previous_consecutive


def test_rc19_health_endpoint_never_calls_ultimate(monkeypatch):
    class NoPollRest:
        host = "192.0.2.64"
        def info(self):
            raise AssertionError("health endpoint must not call the Ultimate")

    previous_cfg = dict(server.CFG)
    previous_rest = server.rest
    try:
        server.CFG["u64_host"] = "192.0.2.64"
        server.CFG["rest_control_host"] = "192.0.2.64"
        server.rest = NoPollRest()
        monkeypatch.setattr(server, "_health_process_snapshot", lambda: {"uptime_seconds": 1.0})
        monkeypatch.setattr(server, "_health_stream_snapshot", lambda: {"video": {}, "audio": {}})
        monkeypatch.setattr(server, "_health_index_snapshot", lambda: {"images": 2})
        monkeypatch.setattr(server, "_health_event_snapshot", lambda: {"retained": 0, "warnings": 0, "errors": 0})
        payload = server.health_get()
        assert payload["poll_hint_ms"] == 2000
        assert payload["ultimate"]["selected_ip"] == "192.0.2.64"
        assert payload["limitations"]["fpga_utilisation_available"] is False
    finally:
        server.CFG.clear()
        server.CFG.update(previous_cfg)
        server.rest = previous_rest


def test_rc19_stream_health_calculates_local_rates_and_packet_age(monkeypatch):
    class DummyReceiver:
        running = True
        packets = 200
        dropped = 3
        frame_no = 50
        bytes_received = 125000
        started_at = 1.0
        last_packet_at = 99.8
        last_pkt_len = 770

    previous_video, previous_audio = server.video, server.audio
    previous_samples = dict(server._HEALTH_STREAM_SAMPLE)
    previous_rates = {k: dict(v) for k, v in server._HEALTH_STREAM_RATES.items()}
    previous_state = dict(server.STREAM_STATE)
    try:
        server.video = DummyReceiver()
        server.audio = DummyReceiver()
        server.STREAM_STATE.update(video=True, audio=True)
        server._HEALTH_STREAM_SAMPLE.clear()
        server._HEALTH_STREAM_SAMPLE.update({
            "video": {"time": 99.0, "bytes": 100000, "packets": 150, "frames": 25},
            "audio": {"time": 99.0, "bytes": 100000, "packets": 150, "frames": 25},
        })
        server._HEALTH_STREAM_RATES["video"] = {}
        server._HEALTH_STREAM_RATES["audio"] = {}
        monkeypatch.setattr(server.time, "time", lambda: 100.0)
        snap = server._health_stream_snapshot()
        assert snap["video"]["bitrate_bps"] == 200000
        assert snap["video"]["packets_per_second"] == 50.0
        assert snap["video"]["frames_per_second"] == 25.0
        assert snap["video"]["packet_age_seconds"] == 0.2
        assert snap["audio"]["commanded_on"] is True
    finally:
        server.video, server.audio = previous_video, previous_audio
        server._HEALTH_STREAM_SAMPLE.clear()
        server._HEALTH_STREAM_SAMPLE.update(previous_samples)
        server._HEALTH_STREAM_RATES.clear()
        server._HEALTH_STREAM_RATES.update(previous_rates)
        server.STREAM_STATE.clear()
        server.STREAM_STATE.update(previous_state)


def test_rc19_diagnostics_contains_health_snapshot(monkeypatch):
    class FakeRest:
        def info(self): return {"product": "Ultimate"}
    monkeypatch.setattr(server, "rest", FakeRest())
    monkeypatch.setattr(server, "stream_stats", lambda: {"video": {"packets": 1}})
    monkeypatch.setattr(server, "health_snapshot", lambda: {"u64deck": {"process": {"cpu_percent": 1.0}}, "limitations": {"fpga_utilisation_available": False}})
    monkeypatch.setattr(server, "fs_index_status", lambda: {"running": False})
    monkeypatch.setattr(server, "cache_stats", lambda: {"database": {"images": 2}})
    monkeypatch.setattr(server, "cache_parse_errors", lambda limit=200: {"count": 0, "errors": []})
    response = server.diagnostics_export({"browser": {"userAgent": "pytest"}})
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        summary = json.loads(archive.read("summary.json"))
    assert summary["health"]["u64deck"]["process"]["cpu_percent"] == 1.0
    assert summary["health"]["limitations"]["fpga_utilisation_available"] is False


# --- v1.9.0 Release Candidate 32 packaging and inherited RC30 contract ---

def test_rc32_documentation_identity_and_inherited_sidflow_contract():
    root = Path(server.ROOT)
    readme = (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    build = server.BUILD
    assert f"v1.9.0 — Release Candidate 44 · build {build}" in readme
    assert f"**Public release-candidate build:** `{build}`" in changelog
    assert "git tag v1.9.0-rc.44 && git push --tags" in readme
    assert "Analyse Disk-Image Names" in readme
    assert "terminal hyphen-delimited" in readme
    assert "SQLite schema v5" in changelog
    assert "v1.9.0-rc.25" not in readme
    normalized_readme = " ".join(readme.split())
    for text in (
        "194,351,886 bytes",
        "982,155,264-byte",
        "269,389,824-byte",
        "about 48 seconds",
        "roughly 1.8 GiB",
        "Update required",
        "With no network available",
        "ordinary SID browsing, queueing and playback continue",
        "u64deck-fallback",
        "2,196,700",
        "58-dimensional",
        "48-dimensional",
    ):
        assert text in normalized_readme
    for url in (
        "https://github.com/chrisgleissner/sidflow",
        "https://github.com/chrisgleissner/sidflow-data/releases/tag/0.8.0",
        "https://github.com/chrisgleissner/sidflow/blob/main/doc/migration/0.5-to-0.8-u64deck.md",
    ):
        assert url in readme
    assert "without network, recommendations remain unavailable while normal SID playback continues" in help_js
    assert "about 48 seconds" in help_js


def test_rc26_docs_retain_playlist_selection_before_playback_entry_paths():
    root = Path(server.ROOT)
    readme = (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    assert "Saved play queues can be loaded before any tune is selected" in readme
    for text in ("fresh start", "direct Jukebox visit", "Clear Queue", "Search", "folder browsing", "local-SID picker", "Storage", "Favourites", "Recent Items"):
        assert text in readme
    assert "Saved play queues before playback" in help_js
    assert "post-Clear Queue state" in changelog

# --- v1.9.0 Release Candidate 26: stale manual-operation containment ---


def test_rc26_coordinator_expires_only_opted_in_waiters():
    from device_coordinator import DeviceOperationCoordinator, OperationExpired

    coordinator = DeviceOperationCoordinator()
    active = threading.Event()
    release = threading.Event()

    def holder():
        with coordinator.operation("interactive", "long device action"):
            active.set()
            assert release.wait(2.0)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert active.wait(1.0)
    try:
        with pytest.raises(OperationExpired, match="expired while waiting"):
            with coordinator.operation(
                "interactive",
                "manual Legacy key",
                max_wait_seconds=0.04,
            ):
                pytest.fail("expired operation must never acquire the device")
    finally:
        release.set()
        thread.join(1.0)

    snap = coordinator.snapshot()
    assert snap.expired == 1
    expired = [row for row in coordinator.recent_operations(10)
               if row["outcome"] == "expired"]
    assert expired and expired[-1]["reason"] == "manual Legacy key"
    # Existing unlimited callers remain unchanged.
    with coordinator.operation("interactive", "ordinary operation"):
        pass


def test_rc26_legacy_keys_are_rejected_immediately_during_mount_run(monkeypatch):
    calls = []

    class FailCmd:
        def type_petscii(self, *args, **kwargs):
            calls.append((args, kwargs))
            pytest.fail("Legacy bytes must not be queued during Mount & Run")

    request = type("Request", (), {
        "headers": {"X-U64deck-Key-Origin": "screen-mirror"}
    })()
    monkeypatch.setattr(server, "cmd", FailCmd())
    monkeypatch.setattr(server, "_mount_run_busy_payload", lambda: {"u64deck_busy": True})

    with pytest.raises(HTTPException) as exc:
        server.keys(request, {"petscii": [136]})
    assert exc.value.status_code == 409
    assert "not queued" in exc.value.detail
    assert calls == []


def test_rc26_legacy_keys_expire_without_delivery_behind_other_work(monkeypatch):
    coordinator = server.DeviceOperationCoordinator()
    command = ultimate.CommandSocket("192.0.2.64", coordinator=coordinator)
    sends = []
    monkeypatch.setattr(command, "_send", lambda command_id, payload=b"":
                        sends.append((command_id, bytes(payload))))
    monkeypatch.setattr(server, "DEVICE_OP", coordinator)
    monkeypatch.setattr(server, "cmd", command)
    monkeypatch.setattr(server, "_MANUAL_INPUT_MAX_WAIT", 0.04)
    monkeypatch.setattr(server, "_mount_run_busy_payload", lambda: None)

    active = threading.Event()
    release = threading.Event()

    def holder():
        with coordinator.operation("interactive", "playing SID"):
            active.set()
            assert release.wait(2.0)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert active.wait(1.0)
    request = type("Request", (), {
        "headers": {"X-U64deck-Key-Origin": "screen-mirror"}
    })()
    try:
        with pytest.raises(HTTPException) as exc:
            server.keys(request, {"petscii": [133]})
        assert exc.value.status_code == 409
        assert "nothing was sent" in exc.value.detail
        assert sends == []
    finally:
        release.set()
        thread.join(1.0)

    expired = [row for row in coordinator.recent_operations(10)
               if row["outcome"] == "expired"]
    assert expired
    assert expired[-1]["reason"] == "keyboard input [screen-mirror]"


def test_rc26_manual_reset_is_not_queued_during_mount_run(monkeypatch):
    calls = []

    class FailRest:
        def put(self, path, **kwargs):
            calls.append(path)
            pytest.fail("manual Reset must not reach the Ultimate during Mount & Run")

    monkeypatch.setattr(server, "rest", FailRest())
    monkeypatch.setattr(server, "_mount_run_busy_payload", lambda: {"u64deck_busy": True})
    monkeypatch.setattr(server, "_MANUAL_MACHINE_PENDING", "")

    with pytest.raises(HTTPException) as exc:
        server.machine("reset")
    assert exc.value.status_code == 409
    assert "Reset was not queued" in exc.value.detail
    assert calls == []


def test_rc26_manual_reset_expires_and_pending_claim_is_released(monkeypatch):
    coordinator = server.DeviceOperationCoordinator()
    puts = []

    class FakeRest:
        def put(self, path, **kwargs):
            puts.append(path)
            return {"errors": []}

    monkeypatch.setattr(server, "DEVICE_OP", coordinator)
    monkeypatch.setattr(server, "rest", FakeRest())
    monkeypatch.setattr(server, "_MANUAL_MACHINE_MAX_WAIT", 0.04)
    monkeypatch.setattr(server, "_mount_run_busy_payload", lambda: None)
    monkeypatch.setattr(server, "_MANUAL_MACHINE_PENDING", "")

    active = threading.Event()
    release = threading.Event()

    def holder():
        with coordinator.operation("interactive", "playing SID"):
            active.set()
            assert release.wait(2.0)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert active.wait(1.0)
    try:
        with pytest.raises(HTTPException) as exc:
            server.machine("reset")
        assert exc.value.status_code == 409
        assert "nothing was sent" in exc.value.detail
        assert puts == []
        assert server._MANUAL_MACHINE_PENDING == ""
    finally:
        release.set()
        thread.join(1.0)


def test_rc26_duplicate_reset_reboot_requests_are_coalesced(monkeypatch):
    monkeypatch.setattr(server, "_mount_run_busy_payload", lambda: None)
    monkeypatch.setattr(server, "_MANUAL_MACHINE_PENDING", "reset")
    with pytest.raises(HTTPException) as exc:
        server.machine("reboot")
    assert exc.value.status_code == 409
    assert "Reset is already pending" in exc.value.detail


def test_rc26_cia1_input_and_release_all_do_not_use_manual_legacy_deadline(monkeypatch):
    calls = []

    def fake_matrix(events, **kwargs):
        calls.append((list(events), dict(kwargs)))
        return {"held": []}

    monkeypatch.setattr(server, "_matrix_send", fake_matrix)
    monkeypatch.setattr(server, "_input_status", lambda *args, **kwargs: {
        "available": True,
    })
    monkeypatch.setattr(server, "_mount_run_busy_payload", lambda: {
        "u64deck_busy": True,
    })

    result = server.input_events({
        "events": [{"kind": "keyboard", "inputs": ["f7"], "transition": "tap"}]
    })
    released = server.input_release_all()
    assert result["mode"] == "matrix" and result["sent"] == 1
    assert released["released"] is True and released["mode"] == "matrix"
    assert calls[0][0][0]["inputs"] == ["f7"]
    assert calls[1][0] == [{"kind": "release_all"}]


def test_rc26_browser_marks_legacy_origin_and_blocks_stale_manual_controls_only():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert '"X-U64deck-Key-Origin":"screen-mirror"' in js
    assert '"X-U64deck-Key-Origin":"type-line"' in js
    assert "Mount & Run is in progress — Legacy keys were not queued" in js
    assert 'MOUNT_RUN_BUSY&&(a==="reset"||a==="reboot")' in js
    # CIA1 key events continue to use their existing endpoint and queue.
    assert 'api("/api/input/events"' in js


# --- v1.9.0 Release Candidate 27: Settings readiness and final documentation ---

def test_rc27_settings_backend_names_exact_failed_operation(monkeypatch):
    class BrokenRest:
        def get_json(self, path):
            raise RuntimeError("[WinError 10061] actively refused")

    monkeypatch.setattr(server, "rest", BrokenRest())
    before = len(server.DIAG_EVENTS)
    with pytest.raises(HTTPException) as exc:
        server.configs()
    assert exc.value.status_code == 502
    assert "Settings category list read failed" in exc.value.detail
    events = list(server.DIAG_EVENTS)[before:]
    assert events
    assert events[-1]["message"].startswith("Settings category list read failed")
    assert events[-1]["extra"]["operation"] == "category list read"


def test_rc27_settings_category_diagnostic_names_category_and_detail(monkeypatch):
    class BrokenRest:
        def get_json(self, path):
            raise ultimate.UltimateError("device unavailable")

    monkeypatch.setattr(server, "rest", BrokenRest())
    with pytest.raises(HTTPException) as exc:
        server.config_category("Audio Mixer", detail=True)
    assert "Settings category 'Audio Mixer' detail read failed" in exc.value.detail


def test_rc27_frontend_gates_settings_until_info_and_retries_neutrally():
    js = (Path(server.ROOT) / "static" / "app.js").read_text(encoding="utf-8")
    html = (Path(server.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
    for text in (
        "if(!LAST_DEVICE_INFO)",
        "Ultimate connection not ready — retrying",
        "settingsRetry",
        "SET.maxRetries",
        "Retry settings",
        "settingsTransientError",
        "if(document.querySelector(\"#tab-settings.active\")&&!SET.loaded)settingsRetry(0)",
    ):
        assert text in js
    assert "Waiting for the Ultimate connection" in html
    assert "Firmware settings load after the connection is ready" in html


def test_rc27_settings_mutations_are_not_automatically_replayed():
    js = (Path(server.ROOT) / "static" / "app.js").read_text(encoding="utf-8")
    # Retry logic is confined to read-only category loading. Existing writes
    # still call api() once and report their result directly.
    save = js[js.index("async function saveDirty"):js.index("async function cfgAction") ]
    action = js[js.index("async function cfgAction"):js.index("/* ---------- assembly64 ----------") ]
    assert "settingsRetry" not in save
    assert "settingsRetry" not in action
    assert 'method:"POST"' in save
    assert 'put("/api/configs_action/"+a)' in action


def test_rc27_docs_record_legacy_retro_replay_scope_and_workaround():
    root = Path(server.ROOT)
    readme = (root / "README.md").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    for text in (
        "Legacy Retro Replay F7 requires the physical C64 keyboard",
        "no buffer-injected F7 is sent",
        "Automatic fastload-menu handling is currently supported and hardware-tested only for **Retro Replay**",
        "CIA1 matrix-input path is unaffected",
    ):
        assert text in readme
    assert "Physical F7 required" in help_js
    assert "u64deck suppresses remote Legacy F7" in help_js
    assert "No Mount & Run sequence, SID Stop path, CIA1 input" in changelog


def test_rc27_docs_describe_settings_hard_refresh_fix():
    root = Path(server.ROOT)
    readme = (root / "README.md").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Connection-ready Settings startup" in readme
    assert "Ctrl+F5" in readme
    assert "hard browser refresh" in help_js
    assert "bounded persistent failure" in help_js
    assert "Ctrl+F5 refresh" in changelog

# --- v1.9.0 Release Candidate 28: indexed disk-name analysis and approvals ---

def _rc28_put_disk_names(store, parent, names):
    store.put_directory(parent, [
        {"name": name, "dir": False, "size": 174848, "mtime": "20260729070000"}
        for name in names
    ])


def test_rc28_builtin_terminal_hyphen_ab_grouping_and_unsuffixed_veto():
    names = ["the-hat-7a825b1-a.d64", "the-hat-7a825b1-b.d64"]
    assert server._swap_group_candidates(names[0], names) == names
    assert server._swap_group_candidates(
        "Game-a.d64", ["Game.d64", "Game-a.d64", "Game-b.d64"]
    ) == ["Game-a.d64"]
    assert server._swap_group_candidates(
        "Game-a.d64", ["Game-a.d64"]
    ) == ["Game-a.d64"]
    assert server._swap_group_candidates(
        "Game-a.d64", ["Game-a.d64", "Game-c.d64"]
    ) == ["Game-a.d64"]


def test_rc28_index_schema_persists_approved_rules_across_clear(tmp_path):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        rule = store.add_disk_group_rule({
            "pattern_key": "terminal-letter:_:a,b:.d64",
            "label": "terminal _a/b",
            "kind": "terminal-letter",
            "delimiter": "_",
            "tokens": ["a", "b"],
            "extensions": [".d64"],
            "scope": "/",
            "examples": [],
            "last_match_count": 2,
        })
        override = store.add_disk_group_override(
            "/USB0/Games", ["Boot.d64", "Data.d64"], "manual exact set"
        )
        assert store.metadata_get("schema_version") == "5"
        before = store.stats()
        assert before["disk_group_rules"] == 1
        assert before["disk_group_overrides"] == 1
        store.clear_all()
        assert store.disk_group_rules()[0]["id"] == rule["id"]
        assert store.disk_group_overrides()[0]["id"] == override["id"]
    finally:
        store.close()


def test_rc28_disk_naming_analysis_classifies_realistic_patterns(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        _rc28_put_disk_names(store, "/USB0/Games", [
            "the-hat-7a825b1-a.d64",
            "the-hat-7a825b1-b.d64",
            "Other_a.d64",
            "Other_b.d64",
            "Game[a].d64",
            "Game[b].d64",
            "Turrican1.d64",
            "Turrican2.d64",
        ])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        report = server.fs_disk_naming_analysis()
        assert report["summary"] == {
            "indexed_disk_images": 8,
            "directories": 1,
            "recognised_sets": 1,
            "recognised_files": 2,
            "candidate_sets": 1,
            "candidate_files": 2,
            "ambiguous_sets": 1,
            "rejected_sets": 1,
        }
        assert report["recognised"][0]["pattern"] == "terminal -a/-b"
        candidate = report["candidates"][0]
        assert candidate["delimiter"] == "_"
        assert candidate["tokens"] == ["a", "b"]
        assert candidate["examples"][0]["names"] == ["Other_a.d64", "Other_b.d64"]
        assert report["ambiguous"][0]["pattern"] == "glued trailing numbers"
        assert report["rejected"][0]["pattern"] == "GoodTools [a]/[b] alternatives"
        assert "u64deck disk-image naming analysis" in report["report_text"]
    finally:
        store.close()


def test_rc28_unsuffixed_sibling_is_rejected_not_approvable(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        _rc28_put_disk_names(store, "/USB0/Games", [
            "Other.d64", "Other_a.d64", "Other_b.d64",
        ])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        report = server.fs_disk_naming_analysis()
        assert report["candidates"] == []
        assert report["rejected"][0]["pattern"] == "unsuffixed sibling veto"
    finally:
        store.close()


def test_rc28_candidate_rule_requires_confirmation_api_and_applies_immediately(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        names = ["Other_a.d64", "Other_b.d64"]
        _rc28_put_disk_names(store, "/USB0/Games", names)
        monkeypatch.setattr(server, "_index_store", lambda: store)
        candidate = server.fs_disk_naming_analysis()["candidates"][0]
        result = server.fs_disk_naming_rule_approve({
            "pattern_key": candidate["pattern_key"],
            "scope": "/",
        })
        saved = result["approved"]
        assert saved["kind"] == "terminal-letter"
        assert saved["delimiter"] == "_"
        assert saved["last_match_count"] == 1
        assert server._swap_group_candidates(
            names[0], names, "/USB0/Games"
        ) == names

        disabled = server.fs_disk_naming_rule_update(saved["id"], {"enabled": False})
        assert disabled["rule"]["enabled"] is False
        assert server._swap_group_candidates(
            names[0], names, "/USB0/Games"
        ) == [names[0]]

        enabled = server.fs_disk_naming_rule_update(saved["id"], {"enabled": True})
        assert enabled["rule"]["enabled"] is True
        assert server.fs_disk_naming_rule_delete(saved["id"])["removed"] == saved["id"]
        assert store.disk_group_rules() == []
    finally:
        store.close()


def test_rc28_rule_api_does_not_accept_arbitrary_patterns(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        _rc28_put_disk_names(store, "/USB0/Games", ["Other_a.d64", "Other_b.d64"])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        with pytest.raises(HTTPException) as exc:
            server.fs_disk_naming_rule_approve({
                "pattern_key": "regex:.*:anything",
                "scope": "/",
            })
        assert exc.value.status_code == 409
        assert store.disk_group_rules() == []
    finally:
        store.close()


def test_rc28_folder_scoped_rule_does_not_leak_to_other_folders(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        names = ["Other_a.d64", "Other_b.d64"]
        _rc28_put_disk_names(store, "/USB0/Games", names)
        _rc28_put_disk_names(store, "/USB0/Demos", names)
        monkeypatch.setattr(server, "_index_store", lambda: store)
        candidate = server.fs_disk_naming_analysis()["candidates"][0]
        server.fs_disk_naming_rule_approve({
            "pattern_key": candidate["pattern_key"],
            "scope": "/USB0/Games",
        })
        assert server._swap_group_candidates(names[0], names, "/USB0/Games") == names
        assert server._swap_group_candidates(names[0], names, "/USB0/Demos") == [names[0]]
    finally:
        store.close()


def test_rc28_exact_set_override_preserves_order_and_is_reversible(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        names = ["Boot.d64", "Data.d64", "Extras.d64"]
        _rc28_put_disk_names(store, "/USB0/Custom", names)
        monkeypatch.setattr(server, "_index_store", lambda: store)
        approved = server.fs_disk_naming_override_approve({
            "parent": "/USB0/Custom",
            "names": ["Data.d64", "Boot.d64"],
            "label": "boot after data",
        })["approved"]
        assert approved["names"] == ["Data.d64", "Boot.d64"]
        assert server._swap_group_candidates(
            "Boot.d64", names, "/USB0/Custom"
        ) == ["Data.d64", "Boot.d64"]
        server.fs_disk_naming_override_update(approved["id"], {"enabled": False})
        assert server._swap_group_candidates(
            "Boot.d64", names, "/USB0/Custom"
        ) == ["Boot.d64"]
        assert server.fs_disk_naming_override_delete(approved["id"])["removed"] == approved["id"]
    finally:
        store.close()


def test_rc28_disk_naming_frontend_is_explicit_and_non_automatic():
    root = Path(server.ROOT)
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    for text in (
        "DISK IMAGE NAMING ANALYSIS",
        "Uses only filenames already stored in the SQLite index",
        "Analysis is read-only",
        "Analyse Disk-Image Names",
        "Copy Analysis Report",
    ):
        assert text in html
    for text in (
        "diskNamingApproveRule",
        "diskNamingApproveRuleForFolder",
        "diskNamingApproveExactByIndex",
        "Approve this reusable disk-grouping pattern?",
        "No files will be renamed or modified",
        "/api/fs/index/disk-naming",
    ):
        assert text in js


def test_rc28_candidate_aggregates_observed_disk_extensions(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        _rc28_put_disk_names(store, "/USB0/D64", ["Pair_a.d64", "Pair_b.d64"])
        _rc28_put_disk_names(store, "/USB0/G64", ["Pair_a.g64", "Pair_b.g64"])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        report = server.fs_disk_naming_analysis()
        assert len(report["candidates"]) == 1
        candidate = report["candidates"][0]
        assert candidate["extensions"] == [".d64", ".g64"]
        assert candidate["sets"] == 2
        assert candidate["files"] == 4
    finally:
        store.close()


def test_rc28_analysis_reports_mixed_marker_families(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        _rc28_put_disk_names(store, "/USB0/Mixed", [
            "Game-Disk1.d64", "Game-Side2.d64",
        ])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        report = server.fs_disk_naming_analysis()
        mixed = next(row for row in report["rejected"]
                     if row["pattern"] == "mixed marker families")
        assert mixed["sets"] == 1
        assert mixed["examples"][0]["names"] == [
            "Game-Disk1.d64", "Game-Side2.d64",
        ]
    finally:
        store.close()


def test_rc28_frontend_allows_explicit_exact_override_for_ambiguous_and_rejected_sets():
    js = (Path(server.ROOT) / "static" / "app.js").read_text(encoding="utf-8")
    assert 'map={candidate:"candidates",ambiguous:"ambiguous",rejected:"rejected"}' in js
    assert "This set is ambiguous" in js
    assert "This set was rejected by a safety rule" in js
    assert "Exact approval overrides that protection only for these filenames" in js
    assert "document.execCommand(\"copy\")" in js


def test_rc28_schema_upgrade_from_v4_preserves_existing_index_rows(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-v4.sqlite3"
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE directories (path TEXT PRIMARY KEY, scanned_at REAL NOT NULL);
            CREATE TABLE fs_entries (
                parent TEXT NOT NULL,
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_lower TEXT NOT NULL,
                is_dir INTEGER NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                mtime TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(parent) REFERENCES directories(path) ON DELETE CASCADE
            );
            INSERT INTO metadata VALUES('schema_version','4');
            INSERT INTO directories VALUES('/USB0/Games', 1.0);
            INSERT INTO fs_entries VALUES(
                '/USB0/Games','/USB0/Games/Existing.d64','Existing.d64',
                'existing.d64',0,174848,'20260729070000'
            );
        """)
        conn.commit()
    finally:
        conn.close()

    store = IndexStore(path)
    try:
        assert store.metadata_get("schema_version") == "5"
        assert store.get_directory("/USB0/Games")[0]["name"] == "Existing.d64"
        assert store.disk_group_rules() == []
        assert store.disk_group_overrides() == []
    finally:
        store.close()


# --- v1.9.0 Release Candidate 29: ambiguous batch approvals and reset controls ---

def test_rc29_batch_approves_selected_ambiguous_sets_as_exact_overrides(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        _rc28_put_disk_names(store, "/USB0/A", ["Pack1.d64", "Pack2.d64"])
        _rc28_put_disk_names(store, "/USB0/B", ["Film A.d64", "Film B.d64"])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        report = server.fs_disk_naming_analysis()
        set_ids = [example["set_id"] for bucket in report["ambiguous"]
                   for example in bucket["examples"]]
        result = server.fs_disk_naming_override_batch_approve({"set_ids": set_ids})
        assert result["summary"] == {"sets": 2, "files": 4, "folders": 2}
        assert len(store.disk_group_overrides()) == 2
        assert server._swap_group_candidates(
            "Pack1.d64", ["Pack1.d64", "Pack2.d64"], "/USB0/A"
        ) == ["Pack1.d64", "Pack2.d64"]
        assert server._swap_group_candidates(
            "Film A.d64", ["Film A.d64", "Film B.d64"], "/USB0/B"
        ) == ["Film A.d64", "Film B.d64"]
    finally:
        store.close()


def test_rc29_folder_batch_approves_every_ambiguous_set_in_that_folder_only(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        _rc28_put_disk_names(store, "/USB0/Many", [
            "Alpha1.d64", "Alpha2.d64", "Beta1.d64", "Beta2.d64",
        ])
        _rc28_put_disk_names(store, "/USB0/Other", ["Gamma1.d64", "Gamma2.d64"])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        report = server.fs_disk_naming_analysis()
        stats = next(item for item in report["ambiguous_folders"]
                     if item["parent"] == "/USB0/Many")
        assert stats == {"parent": "/USB0/Many", "sets": 2, "files": 4}
        result = server.fs_disk_naming_override_batch_approve({"parent": "/USB0/Many"})
        assert result["summary"] == {"sets": 2, "files": 4, "folders": 1}
        assert {row["parent"] for row in store.disk_group_overrides()} == {"/USB0/Many"}
        assert server._swap_group_candidates(
            "Gamma1.d64", ["Gamma1.d64", "Gamma2.d64"], "/USB0/Other"
        ) == ["Gamma1.d64"]
    finally:
        store.close()


def test_rc29_batch_endpoint_rejects_protected_sets(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        _rc28_put_disk_names(store, "/USB0/Games", ["Game[a].d64", "Game[b].d64"])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        protected_id = server.fs_disk_naming_analysis()["rejected"][0]["examples"][0]["set_id"]
        with pytest.raises(HTTPException) as exc:
            server.fs_disk_naming_override_batch_approve({"set_ids": [protected_id]})
        assert exc.value.status_code == 409
        assert store.disk_group_overrides() == []
    finally:
        store.close()


def test_rc29_bulk_exact_set_management_is_atomic(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        first = store.add_disk_group_override("/USB0/A", ["A1.d64", "A2.d64"])
        second = store.add_disk_group_override("/USB0/B", ["B1.d64", "B2.d64"])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        disabled = server.fs_disk_naming_override_batch_manage({
            "action": "disable", "ids": [first["id"], second["id"]],
        })
        assert disabled["result"]["count"] == 2
        assert all(not row["enabled"] for row in store.disk_group_overrides())
        server.fs_disk_naming_override_batch_manage({
            "action": "enable", "ids": [first["id"], second["id"]],
        })
        assert all(row["enabled"] for row in store.disk_group_overrides())
        server.fs_disk_naming_override_batch_manage({
            "action": "remove", "ids": [first["id"], second["id"]],
        })
        assert store.disk_group_overrides() == []
    finally:
        store.close()


def test_rc29_remove_all_local_approvals_preserves_builtin_grouping(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        store.add_disk_group_rule({
            "pattern_key": "terminal-letter:_:a,b:.d64",
            "label": "terminal _a/b", "kind": "terminal-letter", "delimiter": "_",
            "tokens": ["a", "b"], "extensions": [".d64"], "scope": "/",
            "examples": [], "last_match_count": 1,
        })
        store.add_disk_group_override("/USB0/Custom", ["Boot.d64", "Data.d64"])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        result = server.fs_disk_naming_remove_all_approvals()
        assert result["removed"] == {"rules": 1, "overrides": 1, "total": 2}
        assert store.disk_group_rules() == []
        assert store.disk_group_overrides() == []
        names = ["the-hat-7a825b1-a.d64", "the-hat-7a825b1-b.d64"]
        assert server._swap_group_candidates(names[0], names, "/USB0/Games") == names
    finally:
        store.close()


def test_rc29_remove_all_local_approvals_rolls_back_if_second_delete_fails(tmp_path):
    import sqlite3

    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        store.add_disk_group_rule({
            "pattern_key": "terminal-letter:_:a,b:.d64",
            "label": "terminal _a/b", "kind": "terminal-letter", "delimiter": "_",
            "tokens": ["a", "b"], "extensions": [".d64"], "scope": "/",
            "examples": [], "last_match_count": 1,
        })
        store.add_disk_group_override("/USB0/Custom", ["Boot.d64", "Data.d64"])
        with store._lock, store._conn:
            store._conn.execute("""
                CREATE TRIGGER fail_override_delete BEFORE DELETE ON disk_group_overrides
                BEGIN SELECT RAISE(ABORT, 'forced rollback'); END
            """)
        with pytest.raises(sqlite3.IntegrityError):
            store.remove_all_disk_group_approvals()
        assert len(store.disk_group_rules()) == 1
        assert len(store.disk_group_overrides()) == 1
    finally:
        store.close()


def test_rc29_frontend_exposes_safe_ambiguous_batch_and_reset_controls():
    root = Path(server.ROOT)
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    for text in (
        "Approve selected examples", "Approve all ambiguous", "Approve folder",
        "Approve all covers every current ambiguous set",
        "Remove all local approvals", "Final confirmation",
        "/api/fs/index/disk-naming/overrides/batch",
        "/api/fs/index/disk-naming/overrides/manage",
        "/api/fs/index/disk-naming/approvals",
    ):
        assert text in js
    assert "Analysis is optional" in readme
    assert "exact-set overrides only" in readme
    assert "Remove all local approvals" in help_js


# --- v1.9.0 Release Candidate 30: streamed SID fade ---

def test_rc30_fade_defaults_and_bounds():
    previous = dict(server.CFG)
    try:
        assert server.DEFAULT_CONFIG["sid_jukebox_browser_fade_enabled"] is True
        assert server.DEFAULT_CONFIG["sid_jukebox_browser_fade_secs"] == 2.5
        server.CFG["sid_jukebox_browser_fade_enabled"] = True
        server.CFG["sid_jukebox_browser_fade_secs"] = 2.5
        assert server._juke_browser_fade_seconds() == 2.5
        server.CFG["sid_jukebox_browser_fade_secs"] = 99
        assert server._juke_browser_fade_seconds() == 10.0
        server.CFG["sid_jukebox_browser_fade_secs"] = 0.1
        assert server._juke_browser_fade_seconds() == 0.5
        server.CFG["sid_jukebox_browser_fade_enabled"] = False
        assert server._juke_browser_fade_seconds() == 0.0
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc30_songlength_plan_uses_complete_fade_and_unknown_tunes_do_not(monkeypatch):
    previous = dict(server.CFG)
    try:
        server.CFG["sid_jukebox_browser_fade_enabled"] = True
        server.CFG["sid_jukebox_browser_fade_secs"] = 2.5
        server.CFG["sid_jukebox_end_grace_secs"] = 5.0
        monkeypatch.setattr(server, "_juke_length", lambda item, song: 120.0)
        # Fade replaces the separate grace instead of combining with it.
        assert server._juke_auto_advance_plan({}, 1) == (120.0, 2.5, 122.5, "songlengths")
        monkeypatch.setattr(server, "_juke_length", lambda item, song: None)
        server.CFG["sid_default_secs"] = 180
        assert server._juke_auto_advance_plan({}, 1) == (180.0, 1.0, 181.0, "fallback")
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc30_ssl_extension_changes_only_selected_subtune(monkeypatch):
    previous = dict(server.SONGLENGTHS)
    try:
        server.SONGLENGTHS.clear(); server.SONGLENGTHS["abc"] = [60.0, 90.0, 120.0]
        monkeypatch.setattr(server, "_parse_sid", lambda data: {"md5": "abc", "songs": 3})
        payload = server._sid_ssl_payload(b"sid", songnr=2, extension_secs=2.5)
        assert payload == bytes([0x01, 0x00, 0x01, 0x33, 0x02, 0x00])
    finally:
        server.SONGLENGTHS.clear(); server.SONGLENGTHS.update(previous)


def test_rc30_fade_settings_endpoint_validates_and_persists(monkeypatch):
    previous = dict(server.CFG); saved = []
    try:
        monkeypatch.setattr(server, "save_config", lambda: saved.append(True))
        monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
        result = server.juke_fade_set({"enabled": True, "duration_secs": 3.0})
        assert result["enabled"] is True and result["duration_secs"] == 3.0
        assert result["applies_from_next_sid"] is True and saved == [True]
        with pytest.raises(server.HTTPException, match="between 0.5 and 10"):
            server.juke_fade_set({"enabled": True, "duration_secs": 20})
        with pytest.raises(server.HTTPException, match="enabled must"):
            server.juke_fade_set({"enabled": "yes", "duration_secs": 2.5})
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc30_active_fade_state_is_reconstructable(monkeypatch):
    previous_juke = dict(server.JUKE); previous_cfg = dict(server.CFG)
    try:
        server.CFG["sid_jukebox_browser_fade_enabled"] = True
        server.CFG["sid_jukebox_browser_fade_secs"] = 2.5
        server.JUKE.clear(); server.JUKE.update({
            "items": [], "index": -1, "playing": True, "shuffle": False,
            "radio": False, "song": 1, "timer": None, "folder": "", "loading": False,
            "source": "test", "generation": 7, "stop_after_current": False,
            "playback_started_monotonic": 100.0, "playback_length_secs": 120.0,
            "playback_auto_advance_secs": 122.5, "playback_duration_source": "songlengths",
            "playback_fade_secs": 2.5, "playback_id": 7,
        })
        monkeypatch.setattr(server.time, "monotonic", lambda: 121.0)
        state = server._juke_state()["active_browser_fade"]
        assert state["enabled"] is True and state["playback_id"] == 7
        assert state["starts_in_secs"] == 99.0
        assert state["remaining_secs"] == 101.5
    finally:
        server.JUKE.clear(); server.JUKE.update(previous_juke)
        server.CFG.clear(); server.CFG.update(previous_cfg)


def test_rc30_frontend_uses_shared_gain_and_calls_out_hardware_limitation():
    root = Path(server.ROOT); js = (root / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    assert "AUDIO_OUTPUT_GAIN" in js and "linearRampToValueAtTime(0" in js
    assert "src.connect(ensureAudioOutputGain())" in js
    assert "audioRecordDestinationSet(REC.audioDest)" in js
    assert 'id="jkFadeEnabled"' in html and 'id="jkFadeSecs"' in html
    assert "Ultimate HDMI and analogue audio do not fade" in html
    assert "Ultimate HDMI and analogue output remain at full volume" in help_js


def test_rc30_documentation_and_example_describe_browser_only_fade():
    root = Path(server.ROOT); readme = (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    example = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    assert example["sid_jukebox_browser_fade_enabled"] is True
    assert example["sid_jukebox_browser_fade_secs"] == 2.5
    normalized = " ".join(readme.split())
    assert "Fade streamed SID ending" in normalized
    assert "browser recordings only" in normalized
    assert "Release Candidate 30" in changelog


def test_rc30_play_timing_reports_fade_and_native_extension(monkeypatch):
    previous_juke = dict(server.JUKE); previous_cfg = dict(server.CFG); captured = {}
    item = {"label":"Fade.sid","path":"/HVSC/Fade.sid","data":b"sid",
            "meta":{"name":"Fade","start_song":1,"songs":1}}
    class FakeTimer:
        def __init__(self, interval, callback, args=()): captured.update(interval=interval,args=args)
        def start(self): captured["started"] = True
        def cancel(self): pass
    try:
        server.CFG["sid_jukebox_browser_fade_enabled"] = True
        server.CFG["sid_jukebox_browser_fade_secs"] = 2.5
        server.JUKE.clear(); server.JUKE.update({"items":[item],"index":-1,"playing":False,
            "shuffle":False,"radio":False,"song":0,"timer":None,"folder":"","loading":False,
            "source":"test","generation":0,"stop_after_current":False})
        monkeypatch.setattr(server, "_cart_configured", lambda: "")
        monkeypatch.setattr(server, "_juke_auto_advance_plan", lambda item,song:(120.0,2.5,122.5,"songlengths"))
        monkeypatch.setattr(server, "_post_sid_upload", lambda *a, **kw: captured.update(upload=kw) or {})
        monkeypatch.setattr(server._threading, "Timer", FakeTimer)
        monkeypatch.setattr(server, "_diag_event", lambda *a, **kw: None)
        out = server._juke_play(0)
        assert captured["upload"]["songlength_extension_secs"] == 2.5
        assert captured["interval"] == 122.5 and captured["started"] is True
        assert out["play_timing"]["browser_fade_secs"] == 2.5
        assert out["play_timing"]["native_length_extension_secs"] == 2.5
    finally:
        server.JUKE.clear(); server.JUKE.update(previous_juke)
        server.CFG.clear(); server.CFG.update(previous_cfg)


# --- v1.9.0 Release Candidate 32: silent fade handoff and favourite queue action ---

def test_rc31_juke_state_exposes_playback_generation_for_browser_handoff():
    previous = dict(server.JUKE)
    try:
        server.JUKE["playback_id"] = 42
        state = server._juke_state()
        assert state["playback_id"] == 42
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_rc31_frontend_holds_zero_until_replacement_generation_and_clears_tail():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert "jukeFadeHoldSilence" in js
    assert "jukeFadeAwaitReplacement" in js
    assert 'const current=String(s?.playback_id||s?.active_browser_fade?.playback_id||"")' in js
    assert "flushBrowserAudio();JUKE_FADE_HELD=false;jkRender(s)" in js
    assert "jukeFadeGainImmediate(1)},d*1000+50" not in js
    assert "JUKE_FADE_RESTORE_TIMER" not in js
    assert "jukeFadePrepareReplacement" in js
    assert "AUDIO_JUKE_STOP_MUTED||JUKE_FADE_HELD" in js


def test_rc31_favourite_sid_cards_offer_non_interrupting_queue_action():
    js = (Path(server.ASSETS) / "static" / "app.js").read_text(encoding="utf-8")
    assert 'item.type==="sid"&&item.action==="sid_play"' in js
    assert "queueSavedSid(JSON.parse" in js
    assert "Add to the SID play queue without interrupting playback" in js
    assert 'return jkAdd(p.folder||"/",p.name)' in js


def test_rc31_docs_explain_fade_handoff_and_favourite_queue_action():
    root = Path(server.ROOT)
    readme = (root / "README.md").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())
    assert "holds browser gain at zero" in normalized
    assert "＋ appends the SID" in normalized
    assert "browser remains silent until a new playback generation is confirmed" in help_js
    assert "Release Candidate 32" in changelog


# --- v1.9.0 Release Candidate 32: stable queue mutation, year search and Now Playing polish ---

class _Rc32Timer:
    def cancel(self): pass


def _rc32_juke_state(items, index=0, generation=9, queue_revision=3):
    return {"items": items, "index": index, "playing": True, "shuffle": False,
            "radio": False, "song": 1, "timer": _Rc32Timer(), "folder": "/HVSC",
            "loading": False, "source": "test", "generation": generation,
            "queue_revision": queue_revision, "stop_after_current": False,
            "playback_started_monotonic": 0.0, "playback_length_secs": 0.0,
            "playback_auto_advance_secs": 0.0, "playback_duration_source": "",
            "playback_fade_secs": 0.0, "playback_id": generation}


def test_rc32_add_path_mutates_queue_without_invalidating_playback(monkeypatch):
    previous = dict(server.JUKE)
    try:
        current = {"label":"Current.sid", "path":"/HVSC/Current.sid", "data":b"sid",
                   "meta":{"name":"Current", "songs":1, "start_song":1}}
        server.JUKE.clear(); server.JUKE.update(_rc32_juke_state([current]))
        monkeypatch.setattr(server, "_sid_metadata_get", lambda store, path: {})
        monkeypatch.setattr(server, "_index_store", lambda: object())
        out = server.juke_add_path({"path":"/HVSC/Next.sid"})
        assert server.JUKE["generation"] == 9
        assert server.JUKE["queue_revision"] == 4
        assert len(server.JUKE["items"]) == 2 and out["added"] == "Next.sid"
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_rc32_sidflow_insert_keeps_current_generation(monkeypatch):
    previous = dict(server.JUKE)
    try:
        current = {"label":"Current.sid", "path":"/HVSC/Current.sid", "data":b"sid",
                   "meta":{"name":"Current", "songs":1, "start_song":1}}
        recommended = {"label":"Match.sid", "path":"/HVSC/Match.sid", "data":None,
                       "meta":{"name":"Match", "songs":1, "start_song":1}}
        server.JUKE.clear(); server.JUKE.update(_rc32_juke_state([current]))
        monkeypatch.setattr(server, "_sidflow_recommendations",
                            lambda *a, **k: ([recommended], {"track_id":"seed#1"}))
        monkeypatch.setattr(server, "_diag_event", lambda *a, **k: None)
        out = server._sidflow_append("/HVSC/Current.sid", 1, insert_after=0)
        assert server.JUKE["generation"] == 9
        assert server.JUKE["queue_revision"] == 4
        assert out["added"] == 1 and server.JUKE["items"][1]["label"] == "Match.sid"
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_rc32_removing_future_entry_keeps_timer_generation_but_current_removal_invalidates():
    previous = dict(server.JUKE)
    try:
        items = [{"label":f"{n}.sid", "path":f"/HVSC/{n}.sid", "data":b"sid",
                  "meta":{"name":n, "songs":1, "start_song":1}} for n in ("A","B","C")]
        server.JUKE.clear(); server.JUKE.update(_rc32_juke_state(items, index=0))
        server.juke_remove({"index":2})
        assert server.JUKE["generation"] == 9 and server.JUKE["queue_revision"] == 4
        server.juke_remove({"index":0})
        assert server.JUKE["generation"] == 10 and server.JUKE["queue_revision"] == 5
        assert server.JUKE["playing"] is False
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_rc32_release_year_extraction_and_metadata_filter(tmp_path: Path):
    from index_store import _release_year
    assert _release_year("1987") == "1987"
    assert _release_year("(C) 1988 Ocean") == "1988"
    assert _release_year("version 11987") == ""
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        base = {"size":1,"mtime":"","format":"PSID","version":2,"songs":1,
                "start_song":1,"title":"","author":"","chip":"6581","clock":"PAL",
                "sids":1,"md5":"","source":"test"}
        store.put_sid_metadata("/HVSC/A.sid", 1, "", {**base,"name":"A","released":"1987"})
        store.put_sid_metadata("/HVSC/B.sid", 1, "", {**base,"name":"B","released":"(C) 1988"})
        store.put_sid_metadata("/HVSC/C.sid", 1, "", {**base,"name":"C","released":"11987"})
        result = store.sid_metadata_search("/HVSC", year="1987")
        assert result["total"] == 1 and result["results"][0]["name"] == "A.sid"
    finally:
        store.close()


def test_rc32_search_endpoint_validates_and_combines_year(monkeypatch):
    class Store:
        def sid_metadata_count(self, root): return 3
        def sid_metadata_search(self, root, q, **kwargs):
            assert kwargs["year"] == "1987" and kwargs["chip"] == "6581"
            return {"total":0,"results":[]}
    monkeypatch.setattr(server, "_index_store", lambda: Store())
    monkeypatch.setattr(server, "_configured_hvsc_root", lambda: "/HVSC")
    out = server.juke_search(q="", limit=100, chip="6581", sid_format="all", year="1987")
    assert out["year"] == "1987" and out["total"] == 0
    with pytest.raises(server.HTTPException, match="four-digit"):
        server.juke_search(q="", limit=100, chip="all", sid_format="all", year="87")


def test_rc32_frontend_has_larger_now_playing_year_filter_and_clear_row_tooltip():
    root = Path(server.ROOT)
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    css = (root / "static" / "app.css").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    assert 'id="jkNow" class="jk-now"' in html
    assert ".jk-now{color:var(--txt);font-size:15px" in css
    assert 'id="jkYear"' in html and "Chip/Format/Year filter" in js
    assert "new URLSearchParams({q,chip,format,year})" in js
    assert "Find tunes similar to this queue entry" in js
    assert "standalone year" in help_js


def test_rc32_docs_describe_queue_contract_and_identity():
    root = Path(server.ROOT)
    readme = (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Release Candidate 44" in readme
    assert "Release Candidate 44" in changelog
    assert "does not invalidate" in readme


def test_rc32_auto_advance_remains_live_after_appending_future_sid(monkeypatch):
    previous = dict(server.JUKE); calls = []
    try:
        current = {"label":"Current.sid", "path":"/HVSC/Current.sid", "data":b"sid",
                   "meta":{"name":"Current", "songs":1, "start_song":1}}
        server.JUKE.clear(); server.JUKE.update(_rc32_juke_state([current]))
        monkeypatch.setattr(server, "_sid_metadata_get", lambda store, path: {})
        monkeypatch.setattr(server, "_index_store", lambda: object())
        server.juke_add_path({"path":"/HVSC/Next.sid"})
        monkeypatch.setattr(server, "_juke_play",
                            lambda index, **kwargs: calls.append((index, kwargs)))
        server._juke_auto_next(9)
        assert calls == [(1, {"expected_generation": 9})]
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)

# --- v1.9.0 Release Candidate 33: complete SIDFlow local-presence catalogue ---

def test_rc33_sid_presence_catalogue_unions_partial_metadata_with_file_index(tmp_path: Path):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        store.put_directory("/USB0/HVSC", [
            {"name": "A.sid", "dir": False, "size": 10, "mtime": "1"},
            {"name": "B.sid", "dir": False, "size": 11, "mtime": "2"},
        ])
        store.put_sid_metadata(
            "/USB0/HVSC/A.sid", 10, "1",
            {"name": "A.sid", "format": "PSID", "version": 2,
             "songs": 1, "start_song": 1, "title": "A", "author": "",
             "released": "", "chip": "6581", "clock": "PAL", "sids": 1,
             "md5": "", "source": "played SID"},
            source="played SID",
        )
        catalogue = store.sid_presence_catalogue("/USB0/HVSC")
        assert catalogue["metadata_count"] == 1
        assert catalogue["file_count"] == 2
        assert set(catalogue["metadata_paths"]) == {"/USB0/HVSC/A.sid"}
        assert set(catalogue["file_paths"]) == {
            "/USB0/HVSC/A.sid", "/USB0/HVSC/B.sid"
        }
    finally:
        store.close()


def test_rc33_sidflow_uses_file_index_when_metadata_catalogue_is_partial(monkeypatch):
    class FakeStore:
        def status(self): return {"available": True, "tracks": 3}
        def lookup(self, rel, song):
            return {"track_id": f"{rel}#{song}", "sid_path": rel,
                    "song_index": song}
        def rank(self, seed, *, limit, present_paths, exclude_track_ids,
                 exclude_sid_paths=(), same_file_policy="prefer_other"):
            assert "musicians/b/b.sid" in present_paths
            assert "musicians/c/c.sid" in present_paths
            return [{"track_id": "MUSICIANS/B/B.sid#1",
                     "sid_path": "MUSICIANS/B/B.sid", "song_index": 1,
                     "similarity": .99,
                     "recommendation_source": "sidflow-neighbors"}]

    class PartialIndex:
        def sid_presence_catalogue(self, root):
            return {
                "metadata_paths": ["/USB0/HVSC/MUSICIANS/A/A.sid"],
                "file_paths": [
                    "/USB0/HVSC/MUSICIANS/A/A.sid",
                    "/USB0/HVSC/MUSICIANS/B/B.sid",
                    "/USB0/HVSC/MUSICIANS/C/C.sid",
                ],
            }
        def sid_metadata_for_paths(self, paths): return {}

    previous_juke = dict(server.JUKE)
    previous_played = set(server.JUKE_PLAYED)
    previous_recent = list(server.JUKE_RECENT_TRACKS)
    events = []
    monkeypatch.setattr(server, "SIDFLOW_STORE", FakeStore())
    monkeypatch.setattr(server, "_configured_hvsc_root", lambda: "/USB0/HVSC")
    monkeypatch.setattr(server, "_index_store", lambda: PartialIndex())
    monkeypatch.setattr(server, "_diag_event", lambda level, message: events.append((level, message)))
    try:
        server.JUKE_PLAYED.clear(); server.JUKE_RECENT_TRACKS.clear()
        server.JUKE.clear(); server.JUKE.update({
            "items": [server._juke_lazy_item(
                "/USB0/HVSC/MUSICIANS/A/A.sid", "A.sid")],
            "index": 0, "playing": True, "shuffle": False, "radio": False,
            "song": 1, "timer": None, "folder": "", "loading": False,
            "source": "", "generation": 1, "queue_revision": 0,
        })
        items, seed = server._sidflow_recommendations(
            "/USB0/HVSC/MUSICIANS/A/A.sid", 1
        )
        assert seed["track_id"] == "MUSICIANS/A/A.sid#1"
        assert [item["path"] for item in items] == [
            "/USB0/HVSC/MUSICIANS/B/B.sid"
        ]
        assert any("metadata_paths=1" in message and
                   "file_index_paths=3" in message and
                   "mapped_paths=3" in message
                   for _level, message in events)
    finally:
        server.JUKE.clear(); server.JUKE.update(previous_juke)
        server.JUKE_PLAYED.clear(); server.JUKE_PLAYED.update(previous_played)
        server.JUKE_RECENT_TRACKS.clear(); server.JUKE_RECENT_TRACKS.extend(previous_recent)


def test_rc33_sidflow_zero_result_records_mapping_counts(monkeypatch):
    class FakeStore:
        def status(self): return {"available": True, "tracks": 2}
        def lookup(self, rel, song):
            return {"track_id": f"{rel}#{song}", "sid_path": rel,
                    "song_index": song}
        def rank(self, *args, **kwargs): return []

    class Index:
        def sid_presence_catalogue(self, root):
            return {"metadata_paths": ["/HVSC/A.sid"],
                    "file_paths": ["/HVSC/A.sid", "/HVSC/B.sid"]}
        def sid_metadata_for_paths(self, paths): return {}

    previous = dict(server.JUKE)
    events = []
    monkeypatch.setattr(server, "SIDFLOW_STORE", FakeStore())
    monkeypatch.setattr(server, "_configured_hvsc_root", lambda: "/HVSC")
    monkeypatch.setattr(server, "_index_store", lambda: Index())
    monkeypatch.setattr(server, "_diag_event", lambda level, message: events.append((level, message)))
    try:
        server.JUKE.clear(); server.JUKE.update({"items": [], "index": -1,
            "playing": False, "shuffle": False, "radio": False, "song": 0,
            "timer": None, "folder": "", "loading": False, "source": "",
            "generation": 0, "queue_revision": 0})
        items, _seed = server._sidflow_recommendations("/HVSC/A.sid", 1)
        assert items == []
        assert any(level == "warning" and "ranked=0 final=0" in message and
                   "metadata_paths=1" in message and "file_index_paths=2" in message
                   for level, message in events)
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)



def test_rc35_header_labels_and_star_alignment():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    css = (root / "static" / "app.css").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert '<span class="brand-stars">****</span> u64deck <span class="brand-stars">****</span>' in html
    assert ".brand-stars{display:inline-block;transform:translateY(1px)}" in css
    assert "· FW ${esc(i.firmware_version||\"?\")}" in js
    assert "· Core ${esc(i.core_version)}" in js
    assert '· Input: ${matrix?"CIA1":"Legacy KERNAL buffer"}' in js


def test_rc35_settings_index_precedes_collapsed_firmware_configuration():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    assert html.index("SEARCH INDEX &amp; CACHE") < html.index("Ultimate firmware configuration")
    assert '<details class="panel firmware-settings-details">' in html
    assert '<summary>Ultimate firmware configuration</summary>' in html
    assert '<details class="panel firmware-settings-details" open>' not in html
    assert html.count('id="catlist"') == 1
    assert html.count('id="setitems"') == 1
    assert html.count('id="btnApply"') == 1


def test_rc35_recording_labels_and_songlengths_presentation():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    css = (root / "static" / "app.css").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    labels = [
        "Video + Audio", "Video Only", "Audio Only", "Manual", "30 Seconds",
        "1 Minute", "5 Minutes", "10 Minutes", "Compact", "Standard", "High",
        "AUTO", "MP4", "WebM", "Native 384×272", "Pixel-Perfect 2×",
    ]
    for label in labels:
        assert label in html
    assert "Number(s.songlengths_loaded).toLocaleString()" in js
    assert "#jkSlInfo{font-size:13px}" in css
    assert ".sidflow-juke-credit-row .sidflow-credit{font-size:12px}" in css


def test_rc35_queue_layout_and_context_are_non_redundant():
    root = Path(__file__).resolve().parents[1]
    css = (root / "static" / "app.css").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    server_text = (root / "server.py").read_text(encoding="utf-8")
    assert "minmax(240px,440px) minmax(210px,360px)" in css
    assert ".jkq-title,.jkq-author{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" in css
    assert "if(s.recommendation_seed_label)return `More like ${s.recommendation_seed_label}`" in js
    assert '"recommendation_seed_label": JUKE.get("recommendation_seed_label", "")' in server_text
    assert 'JUKE["recommendation_seed_label"] = str(' in server_text
    assert 's.folder||"",s.source||""' in js


def test_rc35_playing_row_is_distinct_and_locatable_without_poll_scroll():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    css = (root / "static" / "app.css").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    assert 'id="jkLocateCurrentBtn"' in html
    assert 'onclick="jkLocateCurrent(true)"' in html
    assert '<span class="jkq-current-marker" aria-hidden="true">▶</span>' in js
    assert 'row.classList.toggle("playing",current)' in js
    assert 'row.setAttribute("aria-current","true")' in js
    assert '.jkq-row.playing{background:' in css
    assert '.jkq-row[role="row"]:hover{background:' in css
    assert 'activeRowKey&&activeRowKey!==JK.activeRowKey' in js
    assert 'requestAnimationFrame(()=>jkLocateCurrent(true))' in js
    assert 'JK.activeRowKey=activeRowKey' in js
    assert '<b>◎ Current</b>' in help_js


def test_rc35_more_like_context_records_the_manual_seed_without_changing_generation(monkeypatch):
    previous = dict(server.JUKE)
    events = []
    alpha = server._juke_lazy_item("/USB0/HVSC/A.sid", "A.sid", {
        "name": "Alpha Tune", "author": "Composer", "songs": 1,
        "start_song": 1,
    })
    recommendation = server._juke_lazy_item("/USB0/HVSC/B.sid", "B.sid", {
        "name": "Beta Tune", "author": "Other", "songs": 1,
        "start_song": 1,
    })
    recommendation["song"] = 1
    recommendation["recommendation_source"] = "sidflow-neighbors"
    monkeypatch.setattr(server, "_sidflow_recommendations",
                        lambda *args, **kwargs: ([recommendation], {"track_id": "A.sid#1"}))
    monkeypatch.setattr(server, "_diag_event", lambda level, message: events.append((level, message)))
    monkeypatch.setattr(server, "_juke_state", lambda: {
        "generation": server.JUKE["generation"],
        "queue_revision": server.JUKE["queue_revision"],
        "recommendation_seed_label": server.JUKE.get("recommendation_seed_label", ""),
    })
    try:
        server.JUKE.clear(); server.JUKE.update({
            "items": [alpha], "index": 0, "playing": True, "shuffle": False,
            "radio": False, "song": 1, "timer": None, "folder": "Custom picks",
            "loading": False, "source": "", "recommendation_seed_label": "",
            "generation": 41, "queue_revision": 7, "stop_after_current": False,
        })
        result = server._sidflow_append("/USB0/HVSC/A.sid", 1, insert_after=0)
        assert server.JUKE["recommendation_seed_label"] == "Alpha Tune"
        assert result["generation"] == 41
        assert result["queue_revision"] == 8
        assert [item["path"] for item in server.JUKE["items"]] == [
            "/USB0/HVSC/A.sid", "/USB0/HVSC/B.sid",
        ]
    finally:
        server.JUKE.clear(); server.JUKE.update(previous)


def test_rc36_queue_reveal_uses_scroll_container_coordinates():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert 'listRect=list.getBoundingClientRect(),rowRect=row.getBoundingClientRect()' in js
    assert 'const rowTop=list.scrollTop+(rowRect.top-listRect.top)' in js
    assert 'list.scrollHeight-list.clientHeight' in js
    assert 'row.offsetTop-headHeight' not in js


def test_rc36_health_hierarchy_and_status_case_are_consistent():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    css = (root / "static" / "app.css").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert '.health-card-title h2{margin:0;font-size:15px' in css
    assert '.health-primary{font-size:13px' in css
    assert '.health-metrics span{color:var(--dim);font-size:12px' in css
    assert 'function healthStatusLabel(value)' in js
    assert 'Auto-updated ' in js
    assert '"Indexer Idle"' in js
    assert 'healthStatusLabel(shutdown.state||"not requested")' in js
    assert 'Auto-update waiting for first sample' in html
    assert '>Source</span>' in html and '>Ready</span>' in html

# --- v1.9.0 Release Candidate 37: Assembly64 first-mount swap context and scalable naming approvals ---

def test_rc37_assembly64_manifest_match_is_conservative_and_ordered():
    manifest = [
        {"item": "12", "filename": "the-hat-7a825b1-b.d64"},
        {"item": "99", "filename": "unrelated.d64"},
        {"item": "11", "filename": "the-hat-7a825b1-a.d64"},
        {"item": "13", "filename": "notes.txt"},
    ]
    related = server._asm_related_manifest(
        "the-hat-7a825b1-b.d64", "12", manifest
    )
    assert [(row["item"], row["filename"]) for row in related] == [
        ("11", "the-hat-7a825b1-a.d64"),
        ("12", "the-hat-7a825b1-b.d64"),
    ]


def test_rc37_assembly64_first_mount_run_downloads_pair_and_arms_selected_disk(monkeypatch):
    previous = copy.deepcopy(server.SWAP)
    fetched, mounted = [], []

    async def fake_fetch(_release_id, _category, item):
        fetched.append(item)
        return ("disk-" + item).encode("ascii")

    async def inline_threadpool(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_mount_and_boot(drive, mode, **kwargs):
        mounted.append((drive, mode, kwargs["name"], kwargs["data"]))
        return {"typed": 'LOAD"*",8,1 + RUN'}

    monkeypatch.setattr(server, "_asm_fetch_binary", fake_fetch)
    monkeypatch.setattr(server, "run_in_threadpool", inline_threadpool)
    monkeypatch.setattr(server, "_mount_and_boot", fake_mount_and_boot)
    try:
        server.SWAP.clear(); server.SWAP.update({
            "items": [], "index": -1, "drive": "a", "mode": "unlinked",
            "source": "none", "decision": {},
        })
        result = asyncio.run(server.asm64_deploy({
            "id": "release-1", "category": 0, "item": 12,
            "filename": "the-hat-7a825b1-b.d64", "action": "mount_run",
            "manifest": [
                {"item": 11, "filename": "the-hat-7a825b1-a.d64"},
                {"item": 12, "filename": "the-hat-7a825b1-b.d64"},
                {"item": 90, "filename": "another-release-a.d64"},
                {"item": 91, "filename": "another-release-b.d64"},
            ],
        }))
        assert fetched == ["11", "12"]
        assert mounted == [("a", "unlinked", "the-hat-7a825b1-b.d64", b"disk-12")]
        assert [row["label"] for row in server.SWAP["items"]] == [
            "the-hat-7a825b1-a.d64", "the-hat-7a825b1-b.d64",
        ]
        assert server.SWAP["index"] == 1
        assert server.SWAP["source"] == "assembly64"
        assert result["swap_decision"]["kind"] == "related"
        assert result["swap_decision"]["count"] == 2
    finally:
        server.SWAP.clear(); server.SWAP.update(previous)


def test_rc37_assembly64_old_client_without_manifest_remains_single_disk(monkeypatch):
    previous = copy.deepcopy(server.SWAP)
    fetched = []

    async def fake_fetch(_release_id, _category, item):
        fetched.append(item)
        return b"disk"

    async def inline_threadpool(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(server, "_asm_fetch_binary", fake_fetch)
    monkeypatch.setattr(server, "run_in_threadpool", inline_threadpool)
    monkeypatch.setattr(server, "_mount_and_boot", lambda *args, **kwargs: {"typed": "ok"})
    try:
        result = asyncio.run(server.asm64_deploy({
            "id": "release-2", "category": 0, "item": 21,
            "filename": "solo-a.d64", "action": "mount_run",
        }))
        assert fetched == ["21"]
        assert result["swap_decision"]["kind"] == "single"
        assert [row["label"] for row in server.SWAP["items"]] == ["solo-a.d64"]
    finally:
        server.SWAP.clear(); server.SWAP.update(previous)


def test_rc37_approve_all_ambiguous_saves_every_current_set(tmp_path, monkeypatch):
    store = IndexStore(tmp_path / "idx.sqlite3")
    try:
        _rc28_put_disk_names(store, "/USB0/Many", [
            "Alpha1.d64", "Alpha2.d64",
            "Beta1.d64", "Beta2.d64",
            "Gamma1.d64", "Gamma2.d64",
        ])
        monkeypatch.setattr(server, "_index_store", lambda: store)
        before = server.fs_disk_naming_analysis()
        assert before["summary"]["ambiguous_sets"] == 3
        result = server.fs_disk_naming_override_batch_approve({"all_ambiguous": True})
        assert result["summary"] == {"sets": 3, "files": 6, "folders": 1}
        assert len(store.disk_group_overrides()) == 3
    finally:
        store.close()


def test_rc37_frontend_casing_manifest_and_large_approval_controls():
    root = Path(server.ROOT)
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert '<div id="devinfo">Connecting…</div>' in html
    assert "· Reconnecting…" in js
    assert "Copy Analysis Report" in html
    assert "Approve selected examples" in js
    assert "Approve all ambiguous" in js
    assert "all_ambiguous:true" in js
    assert "Selection applies to the current page" in js
    assert "pageSize=50" in js
    assert "overrideOpen=true" in js
    assert "manifest=(ASM.files||[])" in js
    assert "Approve all shown" not in js
    assert '"manual", "upload", "assembly64"' in (root / "server.py").read_text(encoding="utf-8")


# --- v1.9.0 Release Candidate 38: network-interface display casing ---

def test_rc38_network_interface_auto_label_is_uppercase_display_only():
    root = Path(server.ROOT)
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert '<option value="">AUTO</option>' in html
    assert '`<option value="">AUTO${r.auto?' in js
    assert '"AUTO: "+r.auto' in js
    assert '<option value="">Auto</option>' not in html
    assert '`<option value="">Auto${r.auto?' not in js

# --- v1.9.0 Release Candidate 39: Legacy physical-F7 handoff ---

def test_rc39_boot_options_preserve_saved_auto_f7_but_disable_it_on_legacy(monkeypatch):
    previous = dict(server.CFG)
    try:
        server.CFG["boot_prekey"] = "F7"
        monkeypatch.setattr(server, "_cartridge_cache_snapshot",
                            lambda client=None: {"known": True, "classification": "retro_replay", "label": "Retro Replay"})
        monkeypatch.setattr(server, "_cached_input_status",
                            lambda client=None: {"available": False, "pending": False})
        payload = server._boot_options_payload()
        assert payload["auto_fastload"] is True
        assert payload["effective_auto_fastload"] is False
        assert payload["physical_f7_required"] is True

        monkeypatch.setattr(server, "_cached_input_status",
                            lambda client=None: {"available": True, "pending": False})
        payload = server._boot_options_payload()
        assert payload["auto_fastload"] is True
        assert payload["effective_auto_fastload"] is True
        assert payload["physical_f7_required"] is False
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc39_send_boot_prekey_suppresses_only_legacy_f7(monkeypatch):
    previous = dict(server.CFG)
    diagnostics = []
    matrix = []
    legacy = []
    try:
        server.CFG["boot_prekey"] = "F7"
        monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(server, "_diag_event",
                            lambda level, message, **kwargs: diagnostics.append((level, message)))
        monkeypatch.setattr(server, "_legacy_type",
                            lambda data, **kwargs: legacy.append((bytes(data), kwargs)))
        monkeypatch.setattr(server, "_matrix_send",
                            lambda events, **kwargs: matrix.append((events, kwargs)))

        monkeypatch.setattr(server, "_input_status",
                            lambda *args, **kwargs: {"available": False})
        cart = {"known": True, "classification": "retro_replay", "label": "Retro Replay"}
        assert server._send_boot_prekey(cartridge_state=cart) is None
        assert legacy == [] and matrix == []
        assert any("physical F7 required" in message for _level, message in diagnostics)

        monkeypatch.setattr(server, "_input_status",
                            lambda *args, **kwargs: {"available": True})
        assert server._send_boot_prekey(cartridge_state=cart) == "F7"
        assert legacy == []
        assert matrix == [([
            {"kind": "keyboard", "inputs": ["f7"], "transition": "tap"}
        ], {"force_probe": False})]
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc39_legacy_mount_run_prompts_for_physical_f7_without_injecting_it(monkeypatch):
    previous_cfg = dict(server.CFG)
    typed = []
    prompt_events = []
    fake_cancel = type("Event", (), {
        "wait": lambda self, seconds=0: False,
        "is_set": lambda self: False,
    })()
    fake_continue = type("Event", (), {
        "wait": lambda self, seconds=0: False,
        "is_set": lambda self: False,
    })()

    class FakeRest:
        host = "192.0.2.64"
        def mount_path(self, *args, **kwargs): pass
        def mount_attachment(self, *args, **kwargs): pass
        def put(self, *args, **kwargs): pass

    try:
        server.CFG["boot_prekey"] = "F7"
        monkeypatch.setattr(server, "rest", FakeRest())
        monkeypatch.setattr(server, "_cached_input_status",
                            lambda client=None: {"available": False, "pending": False})
        monkeypatch.setattr(server, "_input_status",
                            lambda *args, **kwargs: {"available": False})
        monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: False)
        monkeypatch.setattr(server, "_juke_disarm_machine_takeover", lambda reason: None)
        monkeypatch.setattr(server, "_temporary_crt_reboot_before_mount_run", lambda: False)
        monkeypatch.setattr(server, "_sid_runner_reboot_before_mount_run", lambda: False)
        monkeypatch.setattr(server, "_cartridge_preflight", lambda **kwargs: {
            "known": True, "classification": "retro_replay", "label": "Retro Replay",
            "source": "test", "age_seconds": 0.0,
        })
        monkeypatch.setattr(server, "_remember_mount", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_bus_id_for", lambda drive: 8)
        monkeypatch.setattr(server, "_mount_run_prompt_begin",
                            lambda plan=None: (7, fake_cancel, fake_continue))
        monkeypatch.setattr(server, "_mount_run_prompt_update",
                            lambda generation, **kwargs: prompt_events.append(("update", generation, kwargs)))
        monkeypatch.setattr(server, "_mount_run_prompt_finish",
                            lambda generation: prompt_events.append(("finish", generation)))
        gates = iter(["ready", "ready"])
        def fake_gate(stage, **kwargs):
            if stage == "boot":
                assert kwargs.get("cancel_event") is fake_cancel
            return next(gates)
        monkeypatch.setattr(server, "_basic_ready_gate", fake_gate)
        monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(server, "_legacy_type",
                            lambda data, **kwargs: typed.append((bytes(data), kwargs.get("origin"))))

        out = server._mount_and_boot("a", "unlinked", device_path="/Usb0/demo.d64")

        assert out == {"errors": [], "typed": 'LOAD"*",8,1 + RUN'}
        assert typed == [
            (b'LOAD"*",8,1\r', "mount-run-load"),
            (b"RUN\r", "mount-run-run"),
        ]
        assert all(data != bytes([136]) for data, _origin in typed)
        assert any(event[0] == "update" and event[2]["phase"] == "detected"
                   for event in prompt_events)
        assert ("finish", 7) in prompt_events
    finally:
        server.CFG.clear(); server.CFG.update(previous_cfg)


def test_rc39_mount_run_cancel_sets_event_without_device_access(monkeypatch):
    event = threading.Event()
    with server._MOUNT_RUN_PROMPT_LOCK:
        server._MOUNT_RUN_PROMPT.clear()
        server._MOUNT_RUN_PROMPT.update({
            "active": True,
            "generation": 99,
            "phase": "physical_f7",
            "cancel_available": True,
            "cancel_event": event,
            "continue_event": threading.Event(),
        })
    try:
        result = server.mount_run_cancel()
        assert result == {"cancelled": True}
        assert event.is_set()
        snapshot = server._mount_run_prompt_snapshot()
        assert snapshot["phase"] == "cancelling"
        assert snapshot["cancel_available"] is False
    finally:
        with server._MOUNT_RUN_PROMPT_LOCK:
            server._MOUNT_RUN_PROMPT.clear()


def test_rc39_basic_ready_gate_can_be_cancelled_before_read(monkeypatch):
    event = threading.Event(); event.set()
    reads = []
    monkeypatch.setattr(server, "_diag_event", lambda *args, **kwargs: None)
    result = server._basic_ready_gate(
        "boot", reader=lambda: reads.append(True) or 0,
        sleeper=lambda seconds: None, cancel_event=event,
    )
    assert result == "cancelled"
    assert reads == []


def test_rc39_frontend_has_persistent_legacy_f7_overlay_and_disables_auto_f7():
    root = Path(server.ROOT)
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    css = (root / "static" / "app.css").read_text(encoding="utf-8")
    for text in (
        'id="legacyF7Overlay"',
        "Physical F7 required",
        "Cancel Mount &amp; Run",
        'id="autoFastloadHint"',
        "Legacy: cartridge checked at launch",
    ):
        assert text in html
    for text in (
        "renderMountRunPrompt",
        "/api/mount/run/cancel",
        "/api/mount/run/continue",
        "EFFECTIVE_AUTO_FASTLOAD",
        "MOUNT_RUN_REQUEST_TIMEOUT_MS=300000",
        "Physical F7 is required on Legacy KERNAL-buffer input",
        "code===136",
    ):
        assert text in js
    assert ".screen-operation-overlay" in css
    assert ".screen-operation-overlay.detected" in css


def test_rc39_help_and_readme_describe_physical_f7_compromise():
    root = Path(server.ROOT)
    readme = (root / "README.md").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    for text in (
        "Physical F7 required",
        "The saved preference is retained across device changes",
        "Remote Screen-Mirror F7 remains suppressed",
        "CIA1 matrix-input path is unaffected",
    ):
        assert text in readme
    for text in (
        "u64deck suppresses remote Legacy F7",
        "Cancel Mount &amp; Run",
        "saved preference is retained",
        "no automatic F7 is injected",
    ):
        assert text in help_js


# --- v1.9.0 Release Candidate 40: cartridge-aware boot preflight ---

def test_rc40_cartridge_classification_is_conservative():
    assert server._classify_cartridge("") == "none"
    assert server._classify_cartridge("Disabled") == "none"
    assert server._classify_cartridge("/Flash/Retro Replay.crt") == "retro_replay"
    assert server._classify_cartridge("retro_replay_v3.8.crt") == "retro_replay"
    assert server._classify_cartridge("Action Replay.crt") == "other"


def test_rc40_boot_plan_only_automates_confirmed_retro_replay():
    previous = dict(server.CFG)
    try:
        server.CFG["boot_prekey"] = "F7"
        none = {"classification": "none", "label": "None"}
        retro = {"classification": "retro_replay", "label": "Retro Replay"}
        other = {"classification": "other", "label": "Action Replay"}

        plan = server._cartridge_boot_plan({"available": True}, none)
        assert plan["automatic_prekey"] is False
        assert plan["prompt_kind"] == ""

        plan = server._cartridge_boot_plan({"available": True}, retro)
        assert plan["automatic_prekey"] is True
        assert plan["prompt_kind"] == ""

        plan = server._cartridge_boot_plan({"available": False}, retro)
        assert plan["automatic_prekey"] is False
        assert plan["prompt_kind"] == "physical_f7"
        assert "Retro Replay" in plan["prompt_message"]

        plan = server._cartridge_boot_plan({"available": False}, other)
        assert plan["automatic_prekey"] is False
        assert plan["prompt_kind"] == "cartridge_attention"
        assert "not recognised for automatic startup handling" in plan["prompt_message"]
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc40_non_f7_advanced_prekeys_remain_explicit_and_unchanged():
    previous = dict(server.CFG)
    try:
        server.CFG["boot_prekey"] = "F3"
        plan = server._cartridge_boot_plan(
            {"available": False},
            {"classification": "other", "label": "Custom cartridge"},
        )
        assert plan["configured_prekey"] == "F3"
        assert plan["automatic_prekey"] is True
        assert plan["prompt_kind"] == ""
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc40_no_cartridge_mount_run_has_no_f7_or_overlay(monkeypatch):
    previous_cfg = dict(server.CFG)
    typed = []
    settles = []

    class FakeRest:
        host = "192.0.2.40"
        def mount_path(self, *args, **kwargs): pass
        def mount_attachment(self, *args, **kwargs): pass
        def put(self, *args, **kwargs): pass

    try:
        server.CFG["boot_prekey"] = "F7"
        monkeypatch.setattr(server, "rest", FakeRest())
        monkeypatch.setattr(server, "_cached_input_status",
                            lambda client=None: {"available": False, "pending": False})
        monkeypatch.setattr(server, "_input_status",
                            lambda *args, **kwargs: {"available": False})
        monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: False)
        monkeypatch.setattr(server, "_juke_disarm_machine_takeover", lambda reason: None)
        monkeypatch.setattr(server, "_temporary_crt_reboot_before_mount_run", lambda: False)
        monkeypatch.setattr(server, "_sid_runner_reboot_before_mount_run", lambda: False)
        monkeypatch.setattr(server, "_cartridge_preflight", lambda **kwargs: {
            "known": True, "classification": "none", "label": "None",
            "source": "live firmware read", "age_seconds": 0.0,
        })
        monkeypatch.setattr(server, "_remember_mount", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_bus_id_for", lambda drive: 8)
        monkeypatch.setattr(server, "_mount_run_prompt_begin",
                            lambda plan=None: pytest.fail("no cartridge must not show an overlay"))
        monkeypatch.setattr(server, "_boot_settle",
                            lambda **kwargs: settles.append(kwargs))
        gates = iter(["ready", "ready"])
        monkeypatch.setattr(server, "_basic_ready_gate",
                            lambda stage, **kwargs: next(gates))
        monkeypatch.setattr(server, "_legacy_type",
                            lambda data, **kwargs: typed.append((bytes(data), kwargs.get("origin"))))

        out = server._mount_and_boot("a", "unlinked", device_path="/Usb0/plain.d64")

        assert out == {"errors": [], "typed": 'LOAD"*",8,1 + RUN'}
        assert settles == [{"allow_prekey": False, "cartridge_state": {
            "known": True, "classification": "none", "label": "None",
            "source": "live firmware read", "age_seconds": 0.0,
        }}]
        assert typed == [
            (b'LOAD"*",8,1\r', "mount-run-load"),
            (b"RUN\r", "mount-run-run"),
        ]
    finally:
        server.CFG.clear(); server.CFG.update(previous_cfg)


def test_rc40_live_cartridge_preflight_updates_same_device_cache(monkeypatch):
    previous = dict(server.CFG)
    try:
        server.CFG["active_device_identity"] = "device-40"
        monkeypatch.setattr(server, "_read_cartridge_live",
                            lambda client=None: "/Flash/RetroReplay.crt")
        state = server._cartridge_preflight(require_confirmation=True)
        assert state["classification"] == "retro_replay"
        assert state["source"] == "live firmware read"
        cached = server._cartridge_cache_snapshot()
        assert cached["classification"] == "retro_replay"
        assert cached["device_key"] == "identity:device-40"
    finally:
        with server._CART_STATE_LOCK:
            server._CART_CONFIG_CACHE.clear()
        server.CFG.clear(); server.CFG.update(previous)


def test_rc40_mount_preflight_uses_only_recent_same_device_cache(monkeypatch):
    previous = dict(server.CFG)
    diagnostics = []
    try:
        server.CFG["active_device_identity"] = "device-40-cache"
        server._cartridge_cache_set(
            "/Flash/RetroReplay.crt", source="test cache",
            device_key="identity:device-40-cache",
        )
        monkeypatch.setattr(server, "_read_cartridge_live",
                            lambda client=None: (_ for _ in ()).throw(RuntimeError("offline")))
        monkeypatch.setattr(server, "_diag_event",
                            lambda level, message, **kwargs: diagnostics.append((level, message, kwargs)))
        state = server._cartridge_preflight(require_confirmation=True)
        assert state["classification"] == "retro_replay"
        assert state["source"] == "recent same-device cache"
        assert any("using recent same-device cache" in row[1] for row in diagnostics)

        with server._CART_STATE_LOCK:
            server._CART_CONFIG_CACHE["identity:device-40-cache"]["updated_at"] = (
                time.time() - server._CART_CACHE_FALLBACK_SECONDS - 1
            )
        with pytest.raises(server.UltimateError, match="could not be confirmed"):
            server._cartridge_preflight(require_confirmation=True)
    finally:
        with server._CART_STATE_LOCK:
            server._CART_CONFIG_CACHE.clear()
        server.CFG.clear(); server.CFG.update(previous)


def test_rc40_direct_crt_marker_is_set_only_after_success(monkeypatch):
    previous = dict(server.CFG)
    try:
        server.CFG["active_device_identity"] = "device-40-crt"
        monkeypatch.setattr(server, "_run_direct_takeover",
                            lambda fn, reason="": fn())
        result = server._run_temporary_crt(lambda: {"errors": []}, "test CRT", "game.crt")
        assert result == {"errors": []}
        marker = server._temporary_crt_snapshot()
        assert marker["active"] is True
        assert marker["label"] == "game.crt"

        server._temporary_crt_clear(reason="test")
        with pytest.raises(RuntimeError):
            server._run_temporary_crt(
                lambda: (_ for _ in ()).throw(RuntimeError("runner failed")),
                "test CRT", "bad.crt",
            )
        assert server._temporary_crt_snapshot()["active"] is False
    finally:
        with server._CART_STATE_LOCK:
            server._TEMPORARY_CRT_ACTIVE.clear()
        server.CFG.clear(); server.CFG.update(previous)


def test_rc40_temporary_crt_is_cleared_by_one_full_reboot(monkeypatch):
    previous = dict(server.CFG)
    calls = []

    class FakeRest:
        host = "192.0.2.40"
        def put(self, path, **kwargs):
            calls.append((path, kwargs)); return {"errors": []}

    class FakeCmd:
        def close(self): calls.append(("cmd.close", {}))

    try:
        server.CFG["active_device_identity"] = "device-40-reboot"
        monkeypatch.setattr(server, "rest", FakeRest())
        monkeypatch.setattr(server, "cmd", FakeCmd())
        server._temporary_crt_mark("temporary.crt")
        monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(server, "_wait_for_ultimate_after_reboot",
                            lambda client: {"product": "C64 Ultimate"})
        monkeypatch.setattr(server, "_sid_runner_clear_reboot_required",
                            lambda client=None: calls.append(("sid.clear", {})))
        monkeypatch.setattr(server, "_cartridge_cache_invalidate",
                            lambda client=None: calls.append(("cart.invalidate", {})))

        assert server._temporary_crt_reboot_before_mount_run() is True
        assert calls[0][0] == "/v1/machine:reboot"
        assert server._temporary_crt_snapshot()["active"] is False
        assert server._temporary_crt_reboot_before_mount_run() is False
    finally:
        with server._CART_STATE_LOCK:
            server._TEMPORARY_CRT_ACTIVE.clear()
        server.CFG.clear(); server.CFG.update(previous)


def test_rc40_ui_and_help_describe_cartridge_aware_scope():
    root = Path(server.ROOT)
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    for text in (
        "Retro Replay not configured",
        "Legacy: physical F7 for Retro Replay",
        "Cartridge startup requires attention",
        "BOOT_CARTRIDGE",
    ):
        assert text in js
    for text in (
        "limited to the hardware-tested <b>Retro Replay</b> flow",
        "With no configured cartridge",
        "temporary runner state",
        "Other configured cartridges are not rejected",
    ):
        assert text in help_js

# --- v1.9.0 Release Candidate 41: mode-specific Retro Replay guidance ---

def test_rc41_recognises_firmware_retro_replay_identifiers_conservatively():
    assert server._classify_cartridge("rr38pal") == "retro_replay"
    assert server._classify_cartridge("rr38ntsc") == "retro_replay"
    assert server._classify_cartridge("/Flash/rr38pal.crt") == "retro_replay"
    assert server._classify_cartridge("rr-custom") == "other"
    assert server._classify_cartridge("Action Replay.crt") == "other"


def test_rc41_retro_replay_prompt_wording_follows_detected_input_mode():
    previous = dict(server.CFG)
    try:
        retro = {"classification": "retro_replay", "label": "Retro Replay"}
        server.CFG["boot_prekey"] = "F7"
        legacy = server._cartridge_boot_plan({"available": False}, retro)
        assert legacy["prompt_kind"] == "physical_f7"
        assert "Legacy KERNAL-buffer input" in legacy["prompt_message"]
        assert "Freeze Menu" in legacy["prompt_message"]
        assert "physical C64 keyboard" in legacy["prompt_message"]

        cia1 = server._cartridge_boot_plan({"available": True}, retro)
        assert cia1["automatic_prekey"] is True
        assert cia1["prompt_kind"] == ""

        server.CFG["boot_prekey"] = ""
        legacy_manual = server._cartridge_boot_plan({"available": False}, retro)
        cia1_manual = server._cartridge_boot_plan({"available": True}, retro)
        assert legacy_manual["prompt_title"] == "Physical F7 required"
        assert "Legacy KERNAL-buffer input" in legacy_manual["prompt_message"]
        assert cia1_manual["prompt_title"] == "Retro Replay startup"
        assert "Screen controls" in cia1_manual["prompt_message"]
        assert "Legacy" not in cia1_manual["prompt_message"]
        assert "Freeze Menu" not in cia1_manual["prompt_message"]
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc41_other_cartridge_wording_is_mode_specific_not_unsupported():
    previous = dict(server.CFG)
    try:
        server.CFG["boot_prekey"] = "F7"
        other = {"classification": "other", "label": "Custom Cart"}
        legacy = server._cartridge_boot_plan({"available": False}, other)
        cia1 = server._cartridge_boot_plan({"available": True}, other)
        assert "not recognised for automatic startup handling" in legacy["prompt_message"]
        assert "Legacy KERNAL-buffer input" in legacy["prompt_message"]
        assert "physical C64 keyboard" in legacy["prompt_message"]
        assert "not recognised for automatic startup handling" in cia1["prompt_message"]
        assert "Screen controls" in cia1["prompt_message"]
        assert "Legacy" not in cia1["prompt_message"]
        assert "not supported" not in legacy["prompt_message"]
        assert "not supported" not in cia1["prompt_message"]
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc41_standalone_reset_reboot_notice_is_informational_and_legacy_only():
    previous = dict(server.CFG)
    try:
        server.CFG["boot_prekey"] = "F7"
        retro = {"classification": "retro_replay", "label": "Retro Replay"}
        legacy = {"available": False, "pending": False}
        matrix = {"available": True, "pending": False}
        notice = server._standalone_cartridge_notice(
            "reset", status=legacy, cartridge=retro, temporary_active=False
        )
        assert notice["kind"] == "standalone_f7"
        assert notice["dismiss_label"] == "Dismiss"
        assert "Legacy KERNAL-buffer input" in notice["message"]
        assert server._standalone_cartridge_notice(
            "reset", status=legacy, cartridge=retro, temporary_active=True
        ) is None
        assert server._standalone_cartridge_notice(
            "reboot", status=matrix, cartridge=retro
        ) is None
        server.CFG["boot_prekey"] = ""
        assert server._standalone_cartridge_notice(
            "reboot", status=legacy, cartridge=retro
        ) is None
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc41_frontend_has_dismissible_nonblocking_notice_and_mode_tooltips():
    root = Path(server.ROOT)
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    for text in (
        'id="legacyF7Dismiss"',
        "dismissStandaloneScreenNotice()",
        "Automatically presses F7 after Reset, Reboot and Mount &amp; Run",
        "automatic F7 is disabled because an emulated F7 can open the Freeze Menu",
    ):
        assert text in html
    for text in (
        "STANDALONE_SCREEN_NOTICE",
        "showStandaloneScreenNotice",
        "clearStandaloneScreenNotice",
        "informational:true",
        "u64deck_screen_notice",
        "Your saved Auto F7 preference is retained for CIA1-capable devices",
        "Disable this option to handle the Retro Replay startup menu manually",
    ):
        assert text in js


def test_rc41_docs_distinguish_standalone_and_mount_run_overlays():
    root = Path(server.ROOT)
    readme = (root / "README.md").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    for text in (
        "`rr38pal`",
        "Standalone Reset/Reboot uses an informational Dismiss overlay",
        "Mount & Run actively waits for BASIC readiness",
        "two short BASIC-ready samples",
    ):
        assert text in readme
    for text in (
        "Standalone <b>Reset</b> and <b>Reboot</b>",
        "two consecutive ready readings",
        "mode-specific wording",
        "<code>rr38pal</code>",
    ):
        assert text in help_js
    assert "Release Candidate 44" in changelog
    assert "mode-specific cartridge-startup wording" not in changelog  # wording is concise, not a placeholder


def test_rc41_machine_reset_returns_nonblocking_legacy_retro_replay_notice(monkeypatch):
    previous_cfg = dict(server.CFG)
    previous_rest = server.rest

    class FakeRest:
        host = "192.0.2.41"
        def put(self, path, **kwargs):
            assert path == "/v1/machine:reset"
            return {"ok": True}

    try:
        server.CFG["boot_prekey"] = "F7"
        server.CFG["active_device_identity"] = "rc41-device"
        server.rest = FakeRest()
        cartridge = {
            "known": True,
            "classification": "retro_replay",
            "label": "Retro Replay",
            "source": "test",
            "age_seconds": 0.0,
        }
        monkeypatch.setattr(server, "_mount_run_busy_payload", lambda: None)
        monkeypatch.setattr(server, "_juke_disarm_machine_takeover", lambda reason: None)
        monkeypatch.setattr(server, "_matrix_release_all", lambda **kwargs: None)
        monkeypatch.setattr(server, "_cartridge_preflight", lambda **kwargs: cartridge)
        monkeypatch.setattr(server, "_send_boot_prekey", lambda **kwargs: None)
        monkeypatch.setattr(server, "_cached_input_status",
                            lambda client=None: {"available": False, "pending": False})
        monkeypatch.setattr(server, "_temporary_crt_snapshot",
                            lambda client=None: {"active": False})
        result = server.machine("reset")
        assert result["ok"] is True
        assert result["u64deck_cartridge"]["classification"] == "retro_replay"
        notice = result["u64deck_screen_notice"]
        assert notice["kind"] == "standalone_f7"
        assert notice["dismiss_label"] == "Dismiss"
        assert "Legacy KERNAL-buffer input" in notice["message"]
    finally:
        server.rest = previous_rest
        server.CFG.clear(); server.CFG.update(previous_cfg)


# --- v1.9.0 Release Candidate 42: self-clearing standalone F7 guidance ---

def test_rc42_standalone_notice_enables_basic_readiness_monitor():
    previous = dict(server.CFG)
    try:
        server.CFG["boot_prekey"] = "F7"
        notice = server._standalone_cartridge_notice(
            "reset",
            status={"available": False, "pending": False},
            cartridge={"classification": "retro_replay", "label": "Retro Replay"},
        )
        assert notice["monitor_basic_ready"] is True
        assert notice["dismiss_label"] == "Dismiss"
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc42_standalone_basic_ready_endpoint_takes_one_status_sample(monkeypatch):
    previous = dict(server.CFG)
    captured = []

    @contextmanager
    def operation(*args, **kwargs):
        captured.append((args, kwargs))
        yield

    try:
        server.CFG["u64_host"] = "192.0.2.42"
        monkeypatch.setattr(server.DEVICE_OP, "operation", operation)
        samples = []
        monkeypatch.setattr(
            server, "_read_basic_ready_flag",
            lambda: samples.append("read") or 0,
        )
        result = server.standalone_basic_ready_status()
        assert result == {
            "busy": False, "supported": True, "ready": True, "value": 0
        }
        assert samples == ["read"]
        args, kwargs = captured[0]
        assert args == ("status", "checking standalone BASIC readiness")
        assert kwargs["max_wait_seconds"] == pytest.approx(0.25)
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc42_standalone_basic_ready_endpoint_reports_busy_and_unsupported(monkeypatch):
    previous = dict(server.CFG)

    @contextmanager
    def expired(*_args, **_kwargs):
        raise server.OperationExpired("busy")
        yield

    @contextmanager
    def available(*_args, **_kwargs):
        yield

    try:
        server.CFG["u64_host"] = "192.0.2.42"
        monkeypatch.setattr(server.DEVICE_OP, "operation", expired)
        assert server.standalone_basic_ready_status() == {
            "busy": True, "supported": True, "ready": False
        }
        monkeypatch.setattr(server.DEVICE_OP, "operation", available)
        monkeypatch.setattr(server, "_read_basic_ready_flag", lambda: None)
        assert server.standalone_basic_ready_status() == {
            "busy": False, "supported": False, "ready": False, "value": None
        }
    finally:
        server.CFG.clear(); server.CFG.update(previous)


def test_rc42_frontend_debounces_ready_and_selects_legacy_tooltip_robustly():
    root = Path(server.ROOT)
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    for text in (
        '/api/machine/basic-ready',
        'STANDALONE_READY_CONSECUTIVE>=2',
        'Fastload detected — ready',
        'notice.monitor_basic_ready===true',
        'isLegacyInput()',
        'prompt.phase!=="detected"',
    ):
        assert text in js
    assert 'INPUT_STATUS.available===false;' not in js


def test_rc42_docs_describe_self_clearing_status_probe_without_key_detection():
    root = Path(server.ROOT)
    readme = (root / "README.md").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    for text in (
        "does not attempt to detect the physical",
        "two consecutive ready readings",
        "Fastload detected — ready",
        "keeps the Dismiss-only behaviour",
    ):
        assert text in readme
    for text in (
        "does not try to detect the physical keypress",
        "releases the coordinator after every sample",
        "Fastload detected — ready",
        "Dismiss-only behaviour",
    ):
        assert text in help_js
    assert "Release Candidate 44" in changelog


# --- v1.9.0 Release Candidate 43: shared input-mode classification ---

def test_rc43_frontend_uses_shared_input_mode_classifier_for_real_response_shapes(tmp_path: Path):
    import json
    import subprocess

    root = Path(server.ROOT)
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    start = js.index("function inputModeKind")
    end = js.index("let MOUNT_RUN_PROMPT_KEY", start)
    helpers = js[start:end]
    assert "const legacy=isLegacyInput();" in js
    assert "INPUT_STATUS.pending===false&&INPUT_STATUS.available!==true" not in js
    assert "INPUT_STATUS.pending===false&&!INPUT_STATUS.available" not in js

    cases = [
        ({"available": False, "pending": False, "mode": "buffer"}, "legacy"),
        ({"available": 0, "mode": "buffer"}, "legacy"),
        ({"available": False, "mode": "buffer"}, "legacy"),
        ({"available": True, "mode": "matrix"}, "matrix"),
        ({"available": None, "pending": True, "mode": "unknown"}, "pending"),
    ]
    script = helpers + "\n" + "console.log(JSON.stringify(" + json.dumps([
        {"status": status, "expected": expected} for status, expected in cases
    ]) + ".map(item => ({actual: inputModeKind(item.status), expected: item.expected}))));"
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    observed = json.loads(result.stdout)
    assert all(item["actual"] == item["expected"] for item in observed)


def test_rc43_release_notes_describe_frontend_only_tooltip_fix():
    root = Path(server.ROOT)
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Release Candidate 44" in changelog
    assert "one shared input-mode classifier" in changelog
    assert "frontend-only input-mode consistency correction" in changelog


# --- v1.9.0 Release Candidate 44: sentence-case UI copy audit ---

def test_rc44_static_tooltips_use_sentence_case_and_preserve_technical_names():
    root = Path(server.ROOT)
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")

    attributes = re.findall(
        r'(?:title|data-tip|data-wifi-tip|aria-label)="([^"]+)"', html
    )
    assert attributes
    for value in attributes:
        first_alpha = next((ch for ch in value if ch.isalpha()), "")
        assert not first_alpha or first_alpha.isupper(), value

    assert "Audio WebSocket chunks received per second" in js
    assert "audio WebSocket chunks received per second" not in js
    for expected in (
        "Open the Ultimate menu on the C64 display",
        "Save the current frame as PNG",
        "Filter favourites and recent items",
        "Show disk images whose internal directories could not be parsed",
        "Compare your collection's release against the latest on hvsc.c64.org",
        "Find tunes similar to this queue entry and insert them after the current tune",
    ):
        assert expected in html or expected in js


def test_rc44_helper_and_status_copy_is_presentation_only_and_documented():
    root = Path(server.ROOT)
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")

    for expected in (
        "Choose location",
        "Drops: video 0 · audio 0",
        "Empty — add files below",
        "Loading…",
        "Search inside disk images",
        "Detecting local drives…",
        "Saved play queues",
    ):
        assert expected in html
    for expected in (
        "Drops: video ${vd} · audio ${ad}",
        "Building sanitised diagnostics…",
        "Recording ${cfg.mode.replace",
        "Default mount mode:",
        "Disk naming analysis report copied",
        "Local SID metadata scan started",
    ):
        assert expected in js
    assert "Release Candidate 44" in changelog
    assert "presentation-only sentence-case audit" in changelog
    assert "No input, Mount & Run, Retro Replay" in changelog


# --- v1.9.0 RC44 public promotion: frozen build identity and canonical carries ---

def test_rc44_public_frozen_build_stamp_contract(tmp_path: Path):
    import release
    stamp = tmp_path / release.BUILD_STAMP_NAME
    stamp.write_text(server.BUILD + "\n", encoding="ascii")
    assert release.build_id(tmp_path, tmp_path, frozen=True) == server.BUILD
    stamp.write_text("not-a-build\n", encoding="ascii")
    with pytest.raises(RuntimeError, match="invalid"):
        release.build_id(tmp_path, tmp_path, frozen=True)
    stamp.unlink()
    with pytest.raises(RuntimeError, match="missing"):
        release.build_id(tmp_path, tmp_path, frozen=True)


def test_rc44_public_spec_and_workflow_assert_frozen_identity():
    root = Path(server.ROOT)
    spec = (root / "u64deck.spec").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "build-exe.yml").read_text(encoding="utf-8")
    assert "BUILD_STAMP_NAME" in spec
    assert "source_build_id" in spec
    assert '(str(stamp_path), ".")' in spec
    assert 'icon="u64deck.ico"' in spec
    assert "U64DECK_EXPECTED_BUILD" in workflow
    assert "frozen API identity OK" in workflow
    assert "frozen banner identity OK" in workflow
    assert "unconfirmed local Exit rejection OK: HTTP 403" in workflow
    assert "RT_ICON resource is missing" in workflow
    assert "RT_GROUP_ICON resource is missing" in workflow
    assert "$p.ExitCode -ne 0" in workflow


def test_rc44_public_gitattributes_and_legacy_carry_are_present():
    root = Path(server.ROOT)
    attrs = (root / ".gitattributes").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    help_js = (root / "static" / "help_content.js").read_text(encoding="utf-8")
    for rule in ("*.py", "*.js", "*.html", "*.css"):
        assert rule in attrs and "eol=lf" in attrs
    for text in (
        "approximately 3–5 seconds",
        "Other freezer/fastload cartridges have not been comprehensively tested",
        "CIA1 matrix-input path",
    ):
        assert text in readme
        assert text.replace("&", "&amp;") in help_js or text in help_js


def test_rc44_public_exit_accepts_ipv4_mapped_loopback():
    assert server._client_is_loopback("::ffff:127.0.0.1") is True
    assert server._client_is_loopback("::ffff:192.168.1.10") is False


def test_rc44_corrected_exit_ignores_spoofed_forwarded_for_and_disables_proxy_headers(monkeypatch):
    from types import SimpleNamespace
    import inspect

    scheduled = []
    monkeypatch.setattr(server, "_schedule_app_exit", lambda delay=0.20: scheduled.append(delay))
    server._APP_EXIT_REQUESTED.clear()
    try:
        request = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"),
            headers={
                server._LOCAL_EXIT_HEADER: "1",
                "X-Forwarded-For": "198.51.100.25",
            },
        )
        assert server.app_exit(request)["stopping"] is True
        assert scheduled == [0.20]
        source = inspect.getsource(server.main)
        assert "proxy_headers=False" in source
    finally:
        server._APP_EXIT_REQUESTED.clear()


def test_rc44_corrected_concurrent_exit_requests_schedule_once(monkeypatch):
    from types import SimpleNamespace

    class SlowEvent:
        def __init__(self):
            self.value = False
        def is_set(self):
            value = self.value
            time.sleep(0.03)
            return value
        def set(self):
            self.value = True
        def clear(self):
            self.value = False

    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={server._LOCAL_EXIT_HEADER: "1"},
    )
    scheduled = []
    original_event = server._APP_EXIT_REQUESTED
    server._APP_EXIT_REQUESTED = SlowEvent()
    monkeypatch.setattr(server, "_schedule_app_exit", lambda delay=0.20: scheduled.append(delay))
    try:
        start = threading.Barrier(3)
        errors = []
        def call_exit():
            try:
                start.wait(timeout=1.0)
                server.app_exit(request)
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=call_exit) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=1.0)
        for thread in threads:
            thread.join(timeout=2.0)
        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert scheduled == [0.20]
    finally:
        server._APP_EXIT_REQUESTED = original_event
        server._APP_EXIT_REQUESTED.clear()


def test_rc44_corrected_exit_button_is_hidden_until_local_config_confirms():
    root = Path(server.ROOT)
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert re.search(r'id="btnAppExit"[^>]*style="display:none"', html)
    assert 'exitBtn.style.display=c.local_exit_available!==false?"":"none"' in js

"""
ultimate.py — client for the Ultimate 64.

Four transports:
  * REST API (fw 3.11+)  — config, runners, drives, streams
  * TCP command socket (port 64) — keyboard injection, stream fallback
  * FTP — directory browsing and file transfer
  * UDP receivers — VIC video (384x272 @4bpp) and audio (S16LE stereo)

The firmware's separate Telnet UI is intentionally not wrapped here: it does
not control the menu visible in the VIC stream and duplicated the web UI's
storage/settings functions.
"""

import io
import socket
import struct
import threading
import time
from contextlib import contextmanager, nullcontext
from ftplib import FTP, error_perm

import httpx

# TCP command socket opcodes (port 64)
CMD_KEYB = 0xFF03
CMD_RESET = 0xFF04
STREAM_ON = 0xFF20   # + stream id (0 video, 1 audio, 2 debug)
STREAM_OFF = 0xFF30

VIDEO_W, VIDEO_H = 384, 272
FRAME_BYTES = VIDEO_W * VIDEO_H // 2      # 4bpp packed => 52224


class UltimateError(Exception):
    pass


class UltimateREST:
    def __init__(self, host: str, password: str = "", timeout: float = 8.0, coordinator=None):
        self.host = host
        self.coordinator = coordinator
        self.base = f"http://{host}"
        self._timeout = timeout
        self._headers = {}
        if password:
            self._headers["X-Password"] = password
        self.client = httpx.Client(
            base_url=self.base, timeout=self._timeout, headers=self._headers)

    def _operation(self, reason: str):
        coordinator = getattr(self, "coordinator", None)
        return coordinator.operation("interactive", reason) if coordinator else nullcontext()

    def close(self):
        self.client.close()

    def _check(self, r: httpx.Response):
        if r.status_code == 403:
            raise UltimateError("403 Forbidden — check the network password in config.json")
        if r.status_code >= 400:
            raise UltimateError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r

    def _safe_get(self, path: str, params=None):
        """Perform an idempotent GET with one fresh-connection retry.

        Some Ultimate firmware builds occasionally send a Content-Length and
        then close the HTTP connection before the body arrives.  httpx reports
        that as a TransportError.  Retrying a GET is safe, and the one-shot
        client guarantees that the retry does not reuse the damaged keep-alive
        connection.  Mutating PUT/POST requests are deliberately not retried.
        """
        try:
            return self.client.get(path, params=params)
        except httpx.TransportError as first_error:
            time.sleep(0.12)
            try:
                with httpx.Client(
                        base_url=self.base, timeout=self._timeout,
                        headers=self._headers) as retry_client:
                    return retry_client.get(path, params=params)
            except httpx.TransportError as retry_error:
                raise retry_error from first_error

    def get_json(self, path: str, **params):
        with self._operation(f"REST GET {path}"):
            r = self._check(self._safe_get(path, params=params or None))
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}

    def put(self, path: str, *, request_timeout: float | None = None, **params):
        with self._operation(f"REST PUT {path}"):
            kwargs = {"params": params or None}
            if request_timeout is not None:
                kwargs["timeout"] = request_timeout
            r = self._check(self.client.put(path, **kwargs))
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}

    def post_json(self, path: str, payload):
        with self._operation(f"REST POST {path}"):
            r = self._check(self.client.post(path, json=payload))
            return r.json()

    def post_file(self, path: str, filename: str, data: bytes, **params):
        with self._operation(f"REST upload {path}"):
            files = {"file": (filename, data, "application/octet-stream")}
            r = self._check(self.client.post(path, params=params or None, files=files))
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}

    def post_sid(self, filename: str, data: bytes, *, songlengths: bytes | None = None,
                 songlengths_filename: str = "songlengths.ssl", **params):
        """Upload a SID and, when available, its compact ``.ssl`` length array.

        The Ultimate SID runner accepts the SID as the first multipart ``file``
        attachment and an optional per-SID song-length file as the second part,
        again using the ``file`` field name. Firmware reads at most 512 bytes
        from that second file, so reject larger payloads before any request can
        overload the device.
        """
        if songlengths and len(songlengths) > 512:
            raise ValueError("SID song-length payload exceeds 512 bytes")
        with self._operation("REST upload /v1/runners:sidplay"):
            files = [
                ("file", (filename, data, "application/octet-stream")),
            ]
            if songlengths:
                files.append((
                    "file",
                    (songlengths_filename, songlengths, "application/octet-stream"),
                ))
            r = self._check(self.client.post(
                "/v1/runners:sidplay", params=params or None, files=files
            ))
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}

    # convenience wrappers ----------------------------------------------
    def info(self):
        return self.get_json("/v1/info")

    def probe_machine_input(self):
        """Probe the firmware's CIA1 keyboard/joystick input endpoint.

        Ultimate 64 firmware returns a state snapshot with HTTP 200 when the
        matrix-level API is available. Older firmware returns 404 and hardware
        without the Ultimate 64 CIA implementation returns 501. Those two
        responses are capabilities, not transport failures, so expose them to
        the caller without raising UltimateError.
        """
        path = "/v1/machine:input"
        with self._operation(f"REST GET {path}"):
            r = self._safe_get(path)
            if r.status_code in {404, 501}:
                return {"available": False, "status": r.status_code,
                        "state": None, "detail": r.text[:200]}
            self._check(r)
            try:
                state = r.json()
            except ValueError:
                state = {"raw": r.text}
            return {"available": True, "status": r.status_code,
                    "state": state, "detail": ""}

    def machine_input(self, events):
        """Submit CIA1 keyboard/joystick state transitions."""
        return self.post_json("/v1/machine:input", {"events": events})

    def run_prg(self, name: str, data: bytes):
        return self.post_file("/v1/runners:run_prg", name, data)

    def mount_attachment(self, drive: str, filename: str, data: bytes,
                         img_type: str = None, mode: str = "readwrite"):
        params = {"mode": mode}
        if img_type:
            params["type"] = img_type
        return self.post_file(f"/v1/drives/{drive}:mount", filename, data, **params)

    def mount_path(self, drive: str, image_path: str, img_type: str = None,
                   mode: str = "readwrite"):
        params = {"image": image_path, "mode": mode}
        if img_type:
            params["type"] = img_type
        return self.put(f"/v1/drives/{drive}:mount", **params)

    def stream_start(self, name: str, dest: str):
        return self.put(f"/v1/streams/{name}:start", ip=dest)

    def stream_stop(self, name: str):
        return self.put(f"/v1/streams/{name}:stop")

    def local_ip_towards_device(self) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((self.host, 80))
            return s.getsockname()[0]
        finally:
            s.close()


class CommandSocket:
    """Persistent TCP connection to port 64 for low-latency keyboard input."""

    def __init__(self, host: str, port: int = 64, coordinator=None):
        self.host, self.port = host, port
        self.coordinator = coordinator
        self._sock = None
        self._lock = threading.Lock()
        self._type_lock = threading.Lock()

    def _operation(self, reason: str):
        coordinator = getattr(self, "coordinator", None)
        return coordinator.operation("interactive", reason) if coordinator else nullcontext()

    def _ensure(self):
        if self._sock is None:
            s = socket.create_connection((self.host, self.port), timeout=4)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock = s

    def _send(self, command: int, payload: bytes = b""):
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._ensure()
                    self._sock.sendall(struct.pack("<HH", command, len(payload)) + payload)
                    return
                except OSError:
                    self.close_nolock()
                    if attempt == 2:
                        raise UltimateError(
                            f"cannot reach command socket {self.host}:{self.port}")

    def close_nolock(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self):
        with self._lock:
            self.close_nolock()

    def reset(self):
        """Reset the C64 immediately over the low-latency command socket.

        This deliberately bypasses the shared REST/FTP operation coordinator.
        A reset is a four-byte, user-initiated emergency-style command and must
        not sit behind an in-flight status request when the user presses Stop.
        The command socket's own lock still serialises it safely with keyboard
        and stream commands.
        """
        self._send(CMD_RESET)

    def type_petscii(self, data: bytes, chunk: int = 8, delay: float = 0.02):
        """Inject PETSCII bytes into the KERNAL keyboard buffer in order.

        FastAPI may run multiple /api/keys requests on different worker
        threads.  Keep the complete character sequence under one lock so
        separate browser batches cannot overtake or interleave one another.
        """
        with self._type_lock:
            with self._operation("keyboard input"):
                for i in range(0, len(data), chunk):
                    self._send(CMD_KEYB, data[i:i + chunk])
                    if i + chunk < len(data):
                        time.sleep(delay)

    def stream_on(self, stream_id: int, dest: str = ""):
        payload = b"\x00\x00" + dest.encode("ascii")   # duration 0 = forever
        with self._operation("start stream"):
            self._send(STREAM_ON + stream_id, payload)

    def stream_off(self, stream_id: int):
        with self._operation("stop stream"):
            self._send(STREAM_OFF + stream_id)



class MulticastMixin:
    """Join/leave a multicast group on a bound UDP receive socket.

    Lets u64deck listen to a shared stream (e.g. prkl_ultimate's default
    239.0.1.64/65) alongside other receivers like VLC, instead of requiring
    a unicast stream pointed at this machine. The join can be pinned to a
    specific local interface IP — important on multi-homed hosts (virtual
    adapters, VPNs) where INADDR_ANY picks the wrong NIC.
    """
    _sock = None
    _mcast_group = None
    _mcast_iface = "0.0.0.0"

    def set_multicast(self, group: str | None, iface: str = "0.0.0.0"):
        sock = self._sock
        old_g, old_i = self._mcast_group, self._mcast_iface
        iface = iface or "0.0.0.0"
        if group == old_g and iface == old_i:
            return
        if sock is not None:
            if old_g:
                try:
                    mreq = socket.inet_aton(old_g) + socket.inet_aton(old_i)
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
                except OSError:
                    pass
            if group:
                mreq = socket.inet_aton(group) + socket.inet_aton(iface)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self._mcast_group, self._mcast_iface = group, iface

    def _sock_ready(self, sock):
        """Call from run() once the socket is bound."""
        self._sock = sock
        if self._mcast_group:               # group set before thread started
            g, i = self._mcast_group, self._mcast_iface
            self._mcast_group = None
            self.set_multicast(g, i)


class VideoReceiver(threading.Thread, MulticastMixin):
    """Reassembles 780-byte VIC packets into full 4bpp frames.

    Subscribers register a callback taking (frame_bytes). Callbacks run on
    the receiver thread and must be quick (e.g. call_soon_threadsafe).
    """

    def __init__(self, port: int = 11000):
        super().__init__(daemon=True)
        self.port = port
        self.running = True
        self.latest: bytes = b"\x00" * FRAME_BYTES
        self.frame_no = 0
        self.packets = 0
        self.dropped = 0
        self._last_seq = None
        self.started_at = time.time()
        self._subs = set()
        self._subs_lock = threading.Lock()
        self._buf = bytearray(FRAME_BYTES)

    def subscribe(self, cb):
        with self._subs_lock:
            self._subs.add(cb)

    def unsubscribe(self, cb):
        with self._subs_lock:
            self._subs.discard(cb)

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.settimeout(1.0)
        self._sock_ready(sock)
        while self.running:
            try:
                pkt, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(pkt) < 12:
                continue
            _seq, _frm, line_raw, width, lpp, _bpp, _enc = struct.unpack_from("<HHHHBBH", pkt, 0)
            if self._last_seq is not None:
                gap = (_seq - self._last_seq - 1) & 0xFFFF
                if 0 < gap < 0x8000:
                    self.dropped += gap
            self._last_seq = _seq
            line = line_raw & 0x7FFF
            last = bool(line_raw & 0x8000)
            if width != VIDEO_W or lpp == 0:
                continue
            payload = pkt[12:]
            offset = line * (VIDEO_W // 2)
            end = offset + len(payload)
            if end <= FRAME_BYTES:
                self._buf[offset:end] = payload
            self.packets += 1
            if last:
                self.latest = bytes(self._buf)
                self.frame_no += 1
                with self._subs_lock:
                    subscribers = tuple(self._subs)
                for cb in subscribers:
                    try:
                        cb(self.latest)
                    except Exception:
                        pass
        sock.close()

    def stop(self):
        self.running = False
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class AudioReceiver(threading.Thread, MulticastMixin):
    """Receives 770-byte audio packets (2-byte seq + 192 stereo S16LE)."""

    def __init__(self, port: int = 11001):
        super().__init__(daemon=True)
        self.port = port
        self.running = True
        self.packets = 0
        self.dropped = 0
        self._last_seq = None
        self.started_at = time.time()
        self.last_pkt_len = 0
        self._subs = set()
        self._subs_lock = threading.Lock()

    def subscribe(self, cb):
        with self._subs_lock:
            self._subs.add(cb)

    def unsubscribe(self, cb):
        with self._subs_lock:
            self._subs.discard(cb)

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.settimeout(1.0)
        self._sock_ready(sock)
        batch = bytearray()
        while self.running:
            try:
                pkt, _ = sock.recvfrom(2048)
            except socket.timeout:
                if batch:
                    self._emit(bytes(batch)); batch.clear()
                continue
            except OSError:
                break
            if len(pkt) <= 2:
                continue
            seq = int.from_bytes(pkt[:2], "little")
            if self._last_seq is not None:
                gap = (seq - self._last_seq - 1) & 0xFFFF
                if 0 < gap < 0x8000:
                    self.dropped += gap
            self._last_seq = seq
            self.packets += 1
            self.last_pkt_len = len(pkt)
            batch += pkt[2:]
            if len(batch) >= 768 * 8:          # ~32 ms per websocket message
                self._emit(bytes(batch)); batch.clear()
        sock.close()

    def _emit(self, data: bytes):
        with self._subs_lock:
            subscribers = tuple(self._subs)
        for cb in subscribers:
            try:
                cb(data)
            except Exception:
                pass

    def stop(self):
        self.running = False
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class DeviceFS:
    """Browse / fetch / upload files on the Ultimate over FTP."""

    # The Ultimate's FTP daemon dislikes overlapping sessions and rapid
    # connect/quit churn (transient "425 Can't open data connection" etc).
    # All operations are serialized through one lock and retried on
    # transient failures.
    _TRANSIENT = ("425", "421", "426", "timed out", "connection reset",
                  "refused", "eof", "broken pipe")

    def __init__(self, host: str, user: str = "anonymous", password: str = "", coordinator=None):
        self.host, self.user, self.password = host, user, password
        self.coordinator = coordinator
        self._lock = threading.RLock()
        # Modern FTP defaults to UTF-8, but storage attached to the Ultimate can
        # contain legacy 8-bit FAT filenames.  Start with UTF-8 and switch to a
        # byte-preserving Latin-1 control-channel encoding only if a listing
        # contains invalid UTF-8.  Using one encoding for later commands is
        # important: a name decoded from byte 0xF8 must be sent back as 0xF8,
        # not re-encoded as the two-byte UTF-8 sequence C3 B8.
        self._ftp_encoding = "utf-8"
        self._legacy_encoding_reported = False

    def _operation(self, reason: str):
        coordinator = getattr(self, "coordinator", None)
        return coordinator.operation("interactive", reason) if coordinator else nullcontext()

    def _guarded(self, op, reason="FTP operation"):
        """Serialize + retry an FTP operation (op opens its own session)."""
        with self._operation(reason), self._lock:
            for attempt in range(3):
                try:
                    return op()
                except Exception as e:
                    if attempt < 2 and any(t in str(e).lower()
                                           for t in self._TRANSIENT):
                        time.sleep(0.25 * (attempt + 1))
                        continue
                    raise

    def _connect(self, encoding: str | None = None) -> FTP:
        ftp = FTP(encoding=encoding or self._ftp_encoding)
        ftp.connect(self.host, 21, timeout=10)
        ftp.login(self.user or "anonymous", self.password or "")
        return ftp

    @staticmethod
    def _close(ftp: FTP, *, abort: bool = False):
        """Close without masking the operation's real exception."""
        try:
            if abort:
                ftp.close()
            else:
                ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    def _list_dir_once(self, path: str, encoding: str):
        ftp = self._connect(encoding)
        entries = []
        try:
            ftp.cwd(path or "/")
            try:
                for name, facts in ftp.mlsd():
                    if name in (".", ".."):
                        continue
                    entries.append({
                        "name": name,
                        "dir": facts.get("type") == "dir",
                        "size": int(facts.get("size", 0) or 0),
                        "mtime": facts.get("modify", ""),
                    })
            except (error_perm, AttributeError):
                names = [n for n in ftp.nlst() if n not in (".", "..")]
                for name in names:
                    try:
                        size = ftp.size(name)
                        entries.append({"name": name, "dir": False, "size": size or 0})
                    except error_perm:
                        entries.append({"name": name, "dir": True, "size": 0})
        finally:
            self._close(ftp)
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return entries

    def _list_dir_raw(self, path: str = "/"):
        try:
            return self._list_dir_once(path, self._ftp_encoding)
        except UnicodeDecodeError:
            if self._ftp_encoding.lower().replace("_", "-") not in ("latin-1", "iso-8859-1"):
                # Latin-1 maps every byte 1:1 to Unicode.  It therefore keeps
                # legacy filenames navigable and round-trippable instead of
                # aborting the entire index at the first extended byte.
                entries = self._list_dir_once(path, "latin-1")
                self._ftp_encoding = "latin-1"
                if not self._legacy_encoding_reported:
                    print("  FTP filenames: legacy 8-bit encoding detected; using Latin-1")
                    self._legacy_encoding_reported = True
                return entries
            raise

    def _fetch_raw(self, path: str, max_size: int = 32 * 1024 * 1024) -> bytes:
        ftp = self._connect()
        buf = io.BytesIO()
        try:
            def cb(chunk):
                if buf.tell() + len(chunk) > max_size:
                    raise UltimateError("file too large")
                buf.write(chunk)
            ftp.retrbinary(f"RETR {path}", cb)
        finally:
            self._close(ftp)
        return buf.getvalue()

    def _fetch_head_raw(self, path: str, n: int = 65536) -> bytes:
        """First n bytes of a file: abort the transfer once we have enough
        (for reading headers of multi-MB files like STIL.txt)."""
        ftp = self._connect()
        buf = io.BytesIO()

        class _Enough(Exception):
            pass

        def cb(chunk):
            buf.write(chunk)
            if buf.tell() >= n:
                raise _Enough()
        try:
            ftp.retrbinary(f"RETR {path}", cb)
        except _Enough:
            pass
        finally:
            self._close(ftp, abort=True)   # abort mid-transfer: no polite QUIT
        return buf.getvalue()[:n]

    def _upload_raw(self, path: str, data: bytes):
        ftp = self._connect()
        try:
            ftp.storbinary(f"STOR {path}", io.BytesIO(data))
        finally:
            self._close(ftp)

    # guarded public API ---------------------------------------------------
    def list_dir(self, path: str = "/"):
        return self._guarded(lambda: self._list_dir_raw(path), f"FTP list {path}")

    def fetch(self, path: str, max_size: int = 32 * 1024 * 1024) -> bytes:
        return self._guarded(lambda: self._fetch_raw(path, max_size), f"FTP fetch {path}")

    def fetch_head(self, path: str, n: int = 65536) -> bytes:
        return self._guarded(lambda: self._fetch_head_raw(path, n), f"FTP read {path}")

    def upload(self, path: str, data: bytes):
        return self._guarded(lambda: self._upload_raw(path, data), f"FTP upload {path}")

    @contextmanager
    def exclusive(self):
        """Temporarily reserve the device filesystem transport.

        The Ultimate's FTP and REST services share limited firmware resources.
        Holding this lock lets a higher-priority REST mutation wait for the
        current FTP operation to finish and prevents the background indexer
        from immediately starting another one.
        """
        with self._lock:
            yield

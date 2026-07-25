"""Synchronous transport shared by u64deck discovery and its diagnostic.

This module owns the production network behaviour used by Finder. Every
supplied address receives exactly one direct ``GET /v1/info`` request through a
short-lived TCP socket, with bounded fan-out from ``ThreadPoolExecutor``. There
is no preliminary port probe, shared asynchronous HTTP pool, or same-scan retry
pass.

Ultimate firmware can exhibit an important timing split between its two active
network interfaces. On verified firmware 3.15 hardware, Ethernet established
TCP in a few milliseconds but sometimes took roughly 2.1--2.6 seconds to return
the first HTTP response byte. Wi-Fi normally returned within tens of
milliseconds. A single 1.5-second request timeout therefore rejected a healthy
wired interface even though the connection itself had succeeded.

Discovery deliberately uses two separate budgets:

* ``connect_timeout`` limits only TCP connection establishment. Unused subnet
  addresses still fail quickly and do not inherit the longer response budget.
* ``response_timeout`` applies only after TCP has connected, allowing the
  constrained Ultimate HTTP service enough time to answer ``/v1/info``.

The hardware-proven production values are 1.5 seconds to connect and 3.25
seconds for the response. Each address is still queried once only.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import re
import socket
import time
from typing import Any

ULTIMATE_HINT_KEYS = {
    "unique_id", "hostname", "product", "firmware_version", "core_version"
}
MAX_RESPONSE_BYTES = 1024 * 1024


def looks_like_ultimate(payload: Any) -> bool:
    """Return whether a JSON object plausibly came from an Ultimate device."""
    if not isinstance(payload, dict):
        return False

    keys = {str(key).lower() for key in payload}
    if len(keys.intersection(ULTIMATE_HINT_KEYS)) >= 2:
        return True

    combined = " ".join(
        str(payload.get(key, ""))
        for key in ("product", "hostname", "device", "model")
    ).lower()

    return any(token in combined for token in ("ultimate", "u64", "1541"))


def _decode_chunked(body: bytes) -> bytes:
    """Decode a complete HTTP/1.1 chunked body."""
    decoded = bytearray()
    position = 0
    while True:
        line_end = body.find(b"\r\n", position)
        if line_end < 0:
            raise ValueError("incomplete chunk size")
        size_text = body[position:line_end].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise ValueError("invalid chunk size") from exc
        position = line_end + 2
        if size == 0:
            return bytes(decoded)
        chunk_end = position + size
        if chunk_end + 2 > len(body):
            raise ValueError("incomplete chunk data")
        decoded.extend(body[position:chunk_end])
        if body[chunk_end:chunk_end + 2] != b"\r\n":
            raise ValueError("invalid chunk terminator")
        position = chunk_end + 2


def _parse_http_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    """Parse the small HTTP/1.x response returned by ``/v1/info``."""
    header_blob, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("incomplete HTTP response headers")

    lines = header_blob.decode("iso-8859-1", errors="replace").split("\r\n")
    if not lines:
        raise ValueError("missing HTTP status line")

    match = re.match(r"HTTP/\d(?:\.\d)?\s+(\d+)", lines[0])
    if not match:
        raise ValueError(f"invalid HTTP status line: {lines[0]!r}")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()

    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _decode_chunked(body)
    elif headers.get("content-length"):
        expected = int(headers["content-length"])
        if len(body) < expected:
            raise ValueError(
                f"incomplete body: received {len(body)} of {expected} bytes"
            )
        body = body[:expected]

    return int(match.group(1)), headers, body


def get_info(
    ip: str,
    connect_timeout: float,
    response_timeout: float,
    overall_started: float,
    stage: str,
    port: int = 80,
) -> dict[str, Any]:
    """Perform one split-timeout direct ``GET /v1/info`` request."""
    request_started = time.perf_counter()
    port = int(port or 80)
    result: dict[str, Any] = {
        "ip": ip,
        "stage": stage,
        "status": "unknown",
        "request_started_after_ms": round(
            (request_started - overall_started) * 1000.0,
            1,
        ),
    }

    request = (
        "GET /v1/info HTTP/1.1\r\n"
        f"Host: {ip}\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n"
        "User-Agent: u64deck-split-timeout-discovery/1.0\r\n"
        "\r\n"
    ).encode("ascii")

    sock: socket.socket | None = None
    try:
        connect_started = time.perf_counter()
        sock = socket.create_connection(
            (ip, port),
            timeout=max(0.01, float(connect_timeout)),
        )
        result["connect_ms"] = round(
            (time.perf_counter() - connect_started) * 1000.0,
            1,
        )

        sock.settimeout(max(0.01, float(response_timeout)))
        send_started = time.perf_counter()
        sock.sendall(request)

        chunks: list[bytes] = []
        total_bytes = 0
        first_byte_at: float | None = None
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            if first_byte_at is None:
                first_byte_at = time.perf_counter()
            chunks.append(chunk)
            total_bytes += len(chunk)
            if total_bytes > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeded 1 MiB")

        if first_byte_at is not None:
            result["time_to_first_byte_ms"] = round(
                (first_byte_at - send_started) * 1000.0,
                1,
            )

        status_code, headers, body = _parse_http_response(b"".join(chunks))
        result["http_status"] = status_code
        result["response_headers"] = headers
        result["body_bytes"] = len(body)

        if status_code != 200:
            result["status"] = "http_error"
            result["detail"] = f"HTTP {status_code}"
            return result

        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            result["status"] = "invalid_json"
            result["detail"] = str(exc)
            return result

        result["payload"] = payload
        if looks_like_ultimate(payload):
            result["status"] = "ultimate"
            result["found_after_ms"] = round(
                (time.perf_counter() - overall_started) * 1000.0,
                1,
            )
        else:
            result["status"] = "non_ultimate_http"

        return result

    except (socket.timeout, TimeoutError):
        if "connect_ms" in result:
            result["status"] = "response_timeout"
            result["detail"] = (
                f"connected in {result['connect_ms']} ms but no complete "
                f"response within {float(response_timeout):.2f} seconds"
            )
        else:
            result["status"] = "connect_timeout"
            result["detail"] = (
                f"TCP connection not established within "
                f"{float(connect_timeout):.2f} seconds"
            )
    except ConnectionRefusedError as exc:
        result["status"] = "connection_refused"
        result["detail"] = str(exc)
    except OSError as exc:
        result["status"] = "network_error"
        result["detail"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        result["status"] = "exception"
        result["detail"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        result["elapsed_ms"] = round(
            (time.perf_counter() - request_started) * 1000.0,
            1,
        )

    return result


def scan_direct(
    ips: list[str],
    workers: int,
    connect_timeout: float,
    response_timeout: float,
    overall_started: float,
    stage: str,
    port: int = 80,
) -> list[dict[str, Any]]:
    """Scan each supplied address once using the shared threaded transport."""
    results: list[dict[str, Any]] = []

    if not ips:
        return results

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, workers)
    ) as executor:
        future_map = {
            executor.submit(
                get_info,
                ip,
                connect_timeout,
                response_timeout,
                overall_started,
                stage,
                port,
            ): ip
            for ip in ips
        }

        for future in concurrent.futures.as_completed(future_map):
            ip = future_map[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {
                    "ip": ip,
                    "stage": stage,
                    "status": "worker_exception",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            results.append(row)

    return sorted(results, key=lambda row: ipaddress.ip_address(row["ip"]))


def outcome_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    """Summarise transport outcomes for application diagnostics."""
    counts: dict[str, int] = {}
    for row in results:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts

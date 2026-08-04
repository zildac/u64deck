"""Interface-aware Ultimate discovery using the proven synchronous transport.

Previously verified addresses on the current local /24 are placed at the front
of the same concurrent pass as every other address. Each candidate receives
exactly one direct ``GET /v1/info`` through :mod:`discovery_transport`, the
same split-timeout ``ThreadPoolExecutor + socket`` implementation used by the
successful hardware diagnostic. The transport allows 1.5 seconds for TCP connection establishment
and, only after connection, 3.25 seconds for the HTTP response. There is no TCP
port pre-scan, asynchronous HTTP substitution, or same-scan retry pass.

Verified responses are grouped by ``unique_id`` (hostname fallback).  Each
currently responding address is classified from live MAC/ARP evidence as
Ethernet, Wi-Fi or unknown.  Classification does not issue additional REST
requests.  Persisted addresses remain candidates only and are never copied into
a live result unless that exact address responds during the current scan.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import socket
import time
from collections.abc import Callable

import discovery_transport
from network_awareness import (
    LinkDetector,
    device_identity,
    merge_known_device,
    preferred_address,
)

CONNECT_TIMEOUT = 1.5
RESPONSE_TIMEOUT = 3.25
SCAN_CONCURRENCY = 64


def local_subnets() -> list[str]:
    """Best-effort list of local /24 network prefixes (``192.168.1.``)."""
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ips.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except OSError:
        pass
    subnets = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_loopback or addr.is_link_local or addr.version != 4:
                continue
        except ValueError:
            continue
        prefix = ip.rsplit(".", 1)[0] + "."
        if prefix not in subnets:
            subnets.append(prefix)
    return subnets


def _valid_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return ""
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        return ""
    return str(address)


def _remembered_ips(known_devices: dict | None) -> list[str]:
    out: list[str] = []
    if not isinstance(known_devices, dict):
        return out
    for record in known_devices.values():
        addresses = record.get("addresses") if isinstance(record, dict) else None
        if not isinstance(addresses, dict):
            continue
        for value in addresses:
            ip = _valid_ipv4(value)
            if ip and ip not in out:
                out.append(ip)
    return out


def _network_from_prefix(prefix: str) -> ipaddress.IPv4Network | None:
    value = str(prefix or "").strip()
    if not value:
        return None
    try:
        if "/" in value:
            network = ipaddress.ip_network(value, strict=False)
        else:
            normalised = value if value.endswith(".") else value + "."
            network = ipaddress.ip_network(normalised + "0/24", strict=False)
    except ValueError:
        return None
    return network if network.version == 4 else None


def _scan_networks(extra_subnets: list[str] | None = None) -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    for value in [*local_subnets(), *(extra_subnets or [])]:
        network = _network_from_prefix(value)
        if network is not None and network not in networks:
            networks.append(network)
    return networks


def _in_scan_networks(ip: str, networks: list[ipaddress.IPv4Network]) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _transport_hits(results: list[dict]) -> list[dict]:
    """Convert successful shared-transport rows into discovery hit records."""
    hits: list[dict] = []
    for result in results:
        if result.get("status") != "ultimate":
            continue
        payload = result.get("payload")
        if not isinstance(payload, dict):
            continue
        ip = _valid_ipv4(result.get("ip", ""))
        if not ip:
            continue
        hits.append({
            "ip": ip,
            "product": str(payload.get("product") or "Ultimate (unknown model)"),
            "firmware": payload.get("firmware_version") or payload.get("firmwareVersion") or "",
            "hostname": str(payload.get("hostname") or ""),
            "core": payload.get("core_version") or payload.get("coreVersion") or "",
            "unique_id": payload.get("unique_id") or payload.get("uniqueId") or "",
            "verification_ms": result.get("elapsed_ms"),
            "found_after_ms": result.get("found_after_ms"),
            "stage": result.get("stage"),
            "info": payload,
        })
    return hits


async def _scan_stage(ips: list[str], port: int, workers: int,
                      overall_started: float, stage: str, *,
                      result_callback: Callable[[dict], object] | None = None,
                      cancel_event: object | None = None) -> list[dict]:
    """Run one stage through the exact shared threaded scanner.

    Older injected test scanners use the original positional-only signature.
    Production transport accepts optional progressive-result and cancellation
    hooks; detecting support here keeps those tests and third-party diagnostics
    compatible without changing the wire behaviour.
    """
    kwargs = {}
    try:
        parameters = inspect.signature(discovery_transport.scan_direct).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "result_callback" in parameters:
        kwargs["result_callback"] = result_callback
    if "cancel_event" in parameters:
        kwargs["cancel_event"] = cancel_event
    return await asyncio.to_thread(
        discovery_transport.scan_direct,
        ips,
        workers,
        CONNECT_TIMEOUT,
        RESPONSE_TIMEOUT,
        overall_started,
        stage,
        port,
        **kwargs,
    )


def _address_sort(row: dict) -> tuple:
    order = {"ethernet": 0, "wifi": 1, "unknown": 2}
    try:
        ip_key = tuple(int(part) for part in str(row.get("ip", "")).split("."))
    except ValueError:
        ip_key = (999, 999, 999, 999)
    return order.get(str(row.get("link_type")), 2), ip_key


def _replace_matching_mac(record: dict, current_ip: str, current_address: dict,
                          events: list[str]) -> None:
    """Remove superseded DHCP addresses for the same physical interface."""
    mac = str(current_address.get("mac") or "").strip().upper()
    if not mac:
        return
    addresses = record.get("addresses") if isinstance(record.get("addresses"), dict) else {}
    for old_ip, old_row in list(addresses.items()):
        if old_ip == current_ip or not isinstance(old_row, dict):
            continue
        if str(old_row.get("mac") or "").strip().upper() == mac:
            del addresses[old_ip]
            events.append(f"Discovery address replaced: {old_ip} → {current_ip} ({mac})")


async def _group_hits(hits: list[dict], known_devices: dict, detector: LinkDetector,
                      *, events: list[str] | None = None,
                      persist: bool = True) -> list[dict]:
    """Group verified hits and classify interfaces without further REST calls.

    Progressive Finder snapshots use ``persist=False`` so an early live result
    can be shown immediately without updating remembered-device state before
    the bounded subnet pass has completed.
    """
    events = events if events is not None else []
    working_known = known_devices if persist else {}
    current: dict[str, dict[str, dict]] = {}
    for hit in hits:
        ip = _valid_ipv4(hit.get("ip", ""))
        if not ip:
            continue
        identity, _ = device_identity(hit.get("info") or hit, ip)
        current.setdefault(identity, {})[ip] = hit

    devices: list[dict] = []
    for identity, by_ip in current.items():
        rows = list(by_ip.values())
        first = rows[0]
        known = working_known.get(identity) if isinstance(working_known.get(identity), dict) else {}
        old_addresses = dict(known.get("addresses") or {}) if isinstance(known, dict) else {}
        live_ips = list(by_ip)

        # Force a current MAC/ARP observation where the detector supports it.
        # There is no latency-race fallback and therefore no /v1/version request
        # after the direct /v1/info discovery stage.
        def detect_current(ip: str):
            try:
                return detector.detect(ip, force=True)
            except TypeError:
                # Compatibility for injected/test detectors with the original
                # one-argument interface. Production LinkDetector supports force.
                return detector.detect(ip)

        observations_list = await asyncio.gather(*(
            asyncio.to_thread(detect_current, ip)
            for ip in live_ips
        ))
        observations = {item.ip: item for item in observations_list}

        record = known
        live_addresses: list[dict] = []
        for row in rows:
            ip = row["ip"]
            observation = observations.get(ip)
            address = (
                observation.as_dict()
                if observation is not None
                else {"ip": ip, "link_type": "unknown", "mac": "", "method": "unknown"}
            )
            if persist and isinstance(record, dict):
                _replace_matching_mac(record, ip, address, events)
            _identity, record = merge_known_device(
                working_known, info=row.get("info") or row, address=address)
            persisted = (record.get("addresses") or {}).get(ip, address)
            live_addresses.append(dict(persisted))

        stale = sorted(ip for ip in old_addresses if ip not in live_ips) if persist else []
        for ip in stale:
            events.append(f"Historical address omitted: {ip} (no response)")

        live_addresses.sort(key=_address_sort)
        preferred = preferred_address(live_addresses)
        devices.append({
            "identity": identity,
            "identity_source": record.get("identity_source", ""),
            "unique_id": record.get("unique_id", ""),
            "product": record.get("product") or first.get("product") or "Ultimate",
            "firmware": record.get("firmware") or first.get("firmware") or "",
            "hostname": record.get("hostname") or first.get("hostname") or "",
            "core": record.get("core") or first.get("core") or "",
            "addresses": live_addresses,
            "preferred_ip": preferred.get("ip") if preferred else first.get("ip", ""),
            "preferred_link_type": preferred.get("link_type") if preferred else "unknown",
        })

    devices.sort(key=lambda item: (
        str(item.get("product", "")).casefold(), str(item.get("identity", ""))))
    return devices


async def _publish_progress(callback: Callable[[dict], object] | None,
                            result: dict) -> None:
    if callback is None:
        return
    value = callback(result)
    if inspect.isawaitable(value):
        await value


def _counts_text(results: list[dict]) -> str:
    counts = discovery_transport.outcome_counts(results)
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"


async def discover(extra_subnets: list[str] | None = None, port: int = 80,
                   *, known_devices: dict | None = None,
                   detector: LinkDetector | None = None,
                   candidate_ips: list[str] | None = None,
                   progress_callback: Callable[[dict], object] | None = None,
                   cancel_event: object | None = None) -> dict:
    """Return live Ultimate devices using one prioritised direct REST pass.

    Configured and remembered addresses remain first in the same 64-worker pass
    as the rest of the selected local networks. Every candidate still receives
    at most one direct ``/v1/info`` request with the proven split timeout
    budgets and no retry. RC50 adds progressive delivery only: completed rows
    are surfaced while the remaining futures continue in the background.
    """
    started = time.perf_counter()
    networks = _scan_networks(extra_subnets)
    known_devices = known_devices if isinstance(known_devices, dict) else {}
    detector = detector or LinkDetector()

    all_candidates: set[str] = {
        str(address)
        for network in networks
        for address in network.hosts()
    }

    priority_ips: list[str] = []
    for value in [*(candidate_ips or []), *_remembered_ips(known_devices)]:
        ip = _valid_ipv4(value)
        if ip and _in_scan_networks(ip, networks) and ip not in priority_ips:
            priority_ips.append(ip)
            all_candidates.add(ip)

    if not all_candidates:
        result = {
            "subnets": [str(network) for network in networks],
            "devices": [],
            "candidate_count": 0,
            "checked_count": 0,
            "remaining_count": 0,
            "verified_count": 0,
            "cached_candidate_count": 0,
            "cached_verified_count": 0,
            "time_to_first_verified_ms": None,
            "phase": "complete",
            "complete": True,
            "cancelled": False,
            "elapsed_ms": 0.0,
            "diagnostics": ["Discovery scan: no candidate addresses"],
        }
        await _publish_progress(progress_callback, result)
        return result

    priority_set = set(priority_ips)
    remaining_ips = sorted(
        (ip for ip in all_candidates if ip not in priority_set),
        key=ipaddress.ip_address,
    )
    scan_order = [*priority_ips, *remaining_ips]
    candidate_count = len(scan_order)
    progress_queue: asyncio.Queue[dict] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    try:
        transport_parameters = inspect.signature(discovery_transport.scan_direct).parameters
    except (TypeError, ValueError):
        transport_parameters = {}
    progressive_delivery = progress_callback is not None and "result_callback" in transport_parameters

    def completed_row(row: dict) -> None:
        loop.call_soon_threadsafe(progress_queue.put_nowait, dict(row))

    initial = {
        "subnets": [str(network) for network in networks],
        "devices": [],
        "candidate_count": candidate_count,
        "checked_count": 0,
        "remaining_count": candidate_count,
        "verified_count": 0,
        "cached_candidate_count": len(priority_ips),
        "cached_verified_count": 0,
        "time_to_first_verified_ms": None,
        "phase": "scanning",
        "complete": False,
        "cancelled": False,
        "elapsed_ms": 0.0,
        "diagnostics": [],
    }
    if progressive_delivery:
        await _publish_progress(progress_callback, initial)

    scan_started = time.perf_counter()
    scan_task = asyncio.create_task(_scan_stage(
        scan_order, port, SCAN_CONCURRENCY, started, "direct-all",
        result_callback=completed_row if progressive_delivery else None,
        cancel_event=cancel_event))
    observed_rows: list[dict] = []
    observed_by_ip: dict[str, dict] = {}
    live_hits: list[dict] = []
    partial_devices: list[dict] = []
    first_verified_ms: float | None = None
    last_publish = 0.0

    async def process_row(row: dict) -> None:
        nonlocal live_hits, partial_devices, first_verified_ms, last_publish
        ip = str(row.get("ip") or "")
        if ip and ip in observed_by_ip:
            return
        if ip:
            observed_by_ip[ip] = row
        observed_rows.append(row)
        is_ultimate = row.get("status") == "ultimate"
        if is_ultimate:
            live_hits = _transport_hits(observed_rows)
            if first_verified_ms is None:
                value = row.get("found_after_ms")
                first_verified_ms = float(value) if value is not None else round(
                    (time.perf_counter() - started) * 1000.0, 1)
            if progressive_delivery:
                partial_devices = await _group_hits(
                    live_hits, known_devices, detector, persist=False)

        now = time.perf_counter()
        should_publish = (
            is_ultimate
            or len(observed_rows) == candidate_count
            or len(observed_rows) % 16 == 0
            or now - last_publish >= 0.25
        )
        if not progressive_delivery or not should_publish:
            return
        last_publish = now
        hit_ips = {str(item.get("ip") or "") for item in live_hits}
        snapshot = {
            "subnets": [str(network) for network in networks],
            "devices": partial_devices,
            "candidate_count": candidate_count,
            "checked_count": len(observed_rows),
            "remaining_count": max(0, candidate_count - len(observed_rows)),
            "verified_count": len(live_hits),
            "cached_candidate_count": len(priority_ips),
            "cached_verified_count": sum(1 for ip in priority_ips if ip in hit_ips),
            "time_to_first_verified_ms": first_verified_ms,
            "phase": "cancelling" if cancel_event is not None and bool(cancel_event.is_set()) else "scanning",
            "complete": False,
            "cancelled": False,
            "elapsed_ms": round((now - started) * 1000.0, 1),
            "diagnostics": [],
        }
        await _publish_progress(progress_callback, snapshot)

    while True:
        if scan_task.done() and progress_queue.empty():
            await asyncio.sleep(0)
            if progress_queue.empty():
                break
        try:
            row = await asyncio.wait_for(progress_queue.get(), timeout=0.10)
        except asyncio.TimeoutError:
            continue
        await process_row(row)

    scan_results = await scan_task
    for row in scan_results:
        if str(row.get("ip") or "") not in observed_by_ip:
            await process_row(row)

    hits = _transport_hits(scan_results)
    scan_elapsed = round((time.perf_counter() - scan_started) * 1000.0, 1)
    cancelled = (
        cancel_event is not None and bool(cancel_event.is_set())
        and len(scan_results) < candidate_count
    )

    events: list[str] = []
    if priority_ips:
        by_ip = {str(row.get("ip") or ""): row for row in scan_results}
        candidate_details = []
        for ip in priority_ips:
            row = by_ip.get(ip, {})
            status = str(row.get("status") or ("cancelled" if cancelled else "missing"))
            elapsed = row.get("elapsed_ms")
            shown = f"{elapsed} ms" if elapsed is not None else "no timing"
            candidate_details.append(f"{ip}={status} ({shown})")
        events.append(
            "Discovery persisted candidates (non-blocking): "
            + "; ".join(candidate_details)
        )

    devices = await _group_hits(hits, known_devices, detector, events=events)
    for device in devices:
        label = str(
            device.get("hostname") or device.get("unique_id")
            or device.get("identity") or "Ultimate"
        )
        links = []
        for row in device.get("addresses", []):
            link = str(row.get("link_type") or "unknown")
            shown = "Ethernet" if link == "ethernet" else "Wi-Fi" if link == "wifi" else "Unknown"
            links.append(f"{shown} {row.get('ip', '')}")
        events.append(f"{label}: {', '.join(links) if links else 'no verified interfaces'}")

    hit_ips = {str(row.get("ip") or "") for row in hits}
    cached_verified = sum(1 for ip in priority_ips if ip in hit_ips)
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)
    first_text = f"; first verified {first_verified_ms} ms" if first_verified_ms is not None else ""
    cancel_text = "; cancelled after verified results" if cancelled else ""
    events.insert(
        0,
        f"Discovery scan: {candidate_count} candidates, "
        f"{len(scan_results)} checked, {len(hits)} Ultimate responses, "
        f"{len(devices)} devices ({elapsed} ms; one prioritised pass "
        f"{scan_elapsed} ms{first_text}; persisted {len(priority_ips)} "
        f"candidates/{cached_verified} verified; outcomes "
        f"{_counts_text(scan_results)}{cancel_text})",
    )
    result = {
        "subnets": [str(network) for network in networks],
        "devices": devices,
        "candidate_count": candidate_count,
        "checked_count": len(scan_results),
        "remaining_count": max(0, candidate_count - len(scan_results)),
        "verified_count": len(hits),
        "cached_candidate_count": len(priority_ips),
        "cached_verified_count": cached_verified,
        "time_to_first_verified_ms": first_verified_ms,
        "phase": "cancelled" if cancelled else "complete",
        "complete": not cancelled,
        "cancelled": cancelled,
        "elapsed_ms": elapsed,
        "diagnostics": events,
    }
    await _publish_progress(progress_callback, result)
    return result

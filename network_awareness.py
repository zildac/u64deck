"""Ultimate Ethernet/Wi-Fi identity and link-awareness helpers.

The firmware does not expose interface details. u64deck therefore identifies
one physical device with ``/v1/info`` and classifies each on-link address from
its neighbour-table MAC:

* 02:15:41:* is the Ultimate firmware's deterministic wired signature.
* a bundled/refreshable Espressif OUI is the ESP32 Wi-Fi interface.
* anything else is unknown; it is never guessed to be Ethernet.

All potentially blocking OS/network work is designed to be called through
``asyncio.to_thread`` or from a background thread.
"""

from __future__ import annotations

import asyncio
import ctypes
import ipaddress
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import httpx
import psutil

from espressif_ouis import BUNDLED_ESPRESSIF_OUIS, WIRED_PREFIX

LINK_ETHERNET = "ethernet"
LINK_WIFI = "wifi"
LINK_UNKNOWN = "unknown"
LINK_TYPES = {LINK_ETHERNET, LINK_WIFI, LINK_UNKNOWN}

OUI_SOURCE_URL = "https://www.wireshark.org/download/automated/data/manuf"
OUI_REFRESH_SECONDS = 7 * 24 * 60 * 60
_MAC_RE = re.compile(r"(?i)\b([0-9a-f]{2})[:-]([0-9a-f]{2})[:-]([0-9a-f]{2})[:-]([0-9a-f]{2})[:-]([0-9a-f]{2})[:-]([0-9a-f]{2})\b")
_OUI_RE = re.compile(r"(?i)^([0-9a-f]{2}(?::[0-9a-f]{2}){2})(?:/24)?$")


def normalise_mac(value: object) -> str:
    text = str(value or "").strip()
    match = _MAC_RE.search(text)
    if not match:
        return ""
    return ":".join(part.upper() for part in match.groups())


def mac_prefix(value: object) -> str:
    mac = normalise_mac(value)
    return mac[:8] if mac else ""


def classify_mac(value: object, espressif_ouis: Iterable[str] | None = None) -> str:
    prefix = mac_prefix(value)
    if not prefix:
        return LINK_UNKNOWN
    if prefix == WIRED_PREFIX:
        return LINK_ETHERNET
    ouis = set(espressif_ouis or BUNDLED_ESPRESSIF_OUIS)
    return LINK_WIFI if prefix in ouis else LINK_UNKNOWN


def local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    """Return usable local IPv4 networks without inventing netmasks."""
    networks: list[ipaddress.IPv4Network] = []
    seen: set[str] = set()
    try:
        adapters = psutil.net_if_addrs()
    except Exception:
        adapters = {}
    for rows in adapters.values():
        for row in rows:
            if row.family != socket.AF_INET or not row.address or not row.netmask:
                continue
            try:
                iface = ipaddress.IPv4Interface(f"{row.address}/{row.netmask}")
            except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
                continue
            if iface.ip.is_loopback or iface.ip.is_link_local:
                continue
            key = str(iface.network)
            if key not in seen:
                seen.add(key)
                networks.append(iface.network)
    return networks


def is_on_link(ip: str, networks: Iterable[ipaddress.IPv4Network] | None = None) -> bool:
    try:
        address = ipaddress.IPv4Address(str(ip))
    except ipaddress.AddressValueError:
        return False
    candidates = list(networks) if networks is not None else local_ipv4_networks()
    return any(address in network for network in candidates)


def _send_arp_windows(ip: str) -> str:
    if os.name != "nt":
        return ""
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
        ws2_32 = ctypes.WinDLL("ws2_32.dll")
        inet_addr = ws2_32.inet_addr
        inet_addr.argtypes = [ctypes.c_char_p]
        inet_addr.restype = ctypes.c_ulong
        dest = inet_addr(ip.encode("ascii"))
        if dest == 0xFFFFFFFF:
            return ""
        mac = (ctypes.c_ubyte * 8)()
        length = ctypes.c_ulong(6)
        result = iphlpapi.SendARP(dest, 0, ctypes.byref(mac), ctypes.byref(length))
        if result != 0 or length.value < 6:
            return ""
        return ":".join(f"{mac[i]:02X}" for i in range(6))
    except Exception:
        return ""


def _command_output(args: list[str]) -> str:
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=2.0,
            creationflags=flags, check=False,
        )
        return (completed.stdout or "") + "\n" + (completed.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def _neighbour_table_mac(ip: str) -> str:
    commands = []
    if os.name == "nt":
        commands = [["arp", "-a", ip], ["arp", "-a"]]
    else:
        commands = [["ip", "neigh", "show", ip], ["arp", "-n", ip], ["arp", "-a", ip]]
    for command in commands:
        output = _command_output(command)
        for line in output.splitlines():
            if ip not in line:
                continue
            mac = normalise_mac(line)
            if mac:
                return mac
    return ""


def resolve_on_link_mac(ip: str, *, networks: Iterable[ipaddress.IPv4Network] | None = None) -> str:
    """Resolve an on-link IP to a MAC; never return a gateway MAC off-link."""
    if not is_on_link(ip, networks):
        return ""
    mac = _send_arp_windows(ip)
    if mac:
        return normalise_mac(mac)
    return _neighbour_table_mac(ip)


@dataclass(frozen=True)
class LinkObservation:
    ip: str
    link_type: str
    mac: str = ""
    method: str = "unknown"
    latency_ms: float | None = None

    def as_dict(self) -> dict:
        out = {
            "ip": self.ip,
            "link_type": self.link_type,
            "mac": self.mac,
            "method": self.method,
        }
        if self.latency_ms is not None:
            out["latency_ms"] = round(float(self.latency_ms), 1)
        return out


class LinkDetector:
    """Session-cached MAC/OUI detector."""

    def __init__(self, ouis: Iterable[str] | None = None):
        self._ouis = set(ouis or BUNDLED_ESPRESSIF_OUIS)
        self._cache: dict[str, LinkObservation] = {}
        self._lock = threading.RLock()

    @property
    def ouis(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._ouis)

    def merge_ouis(self, additions: Iterable[str]) -> None:
        clean = {str(item).strip().upper() for item in additions if _OUI_RE.fullmatch(str(item).strip())}
        clean.discard(WIRED_PREFIX)
        with self._lock:
            self._ouis.update(clean)
            self._cache.clear()

    def clear(self, ip: str | None = None) -> None:
        with self._lock:
            if ip is None:
                self._cache.clear()
            else:
                self._cache.pop(str(ip), None)

    def detect(self, ip: str, *, force: bool = False,
               networks: Iterable[ipaddress.IPv4Network] | None = None,
               resolver: Callable[..., str] = resolve_on_link_mac) -> LinkObservation:
        ip = str(ip).strip()
        with self._lock:
            cached = self._cache.get(ip)
            if cached and not force:
                return cached
        if not is_on_link(ip, networks):
            result = LinkObservation(ip, LINK_UNKNOWN, "", "off-link")
        else:
            mac = resolver(ip, networks=networks)
            link = classify_mac(mac, self.ouis)
            method = "wired-prefix" if link == LINK_ETHERNET else "espressif-oui" if link == LINK_WIFI else "mac-unknown"
            result = LinkObservation(ip, link, normalise_mac(mac), method)
        with self._lock:
            self._cache[ip] = result
        return result


async def median_version_latency(ip: str, *, samples: int = 3, timeout: float = 3.0,
                                 client_factory=None) -> float | None:
    timings: list[float] = []
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=timeout))
    try:
        async with factory() as client:
            for _ in range(max(1, samples)):
                started = time.perf_counter()
                try:
                    response = await client.get(f"http://{ip}/v1/version")
                    if response.status_code != 200:
                        return None
                    payload = response.json()
                    if not isinstance(payload, dict) or "version" not in payload:
                        return None
                except Exception:
                    return None
                timings.append((time.perf_counter() - started) * 1000.0)
    except Exception:
        return None
    return statistics.median(timings) if timings else None


async def latency_race(ips: list[str], *, sampler=median_version_latency) -> dict[str, LinkObservation]:
    unique = list(dict.fromkeys(str(ip).strip() for ip in ips if str(ip).strip()))
    if len(unique) != 2:
        return {ip: LinkObservation(ip, LINK_UNKNOWN, method="latency-not-run") for ip in unique}
    values = await asyncio.gather(*(sampler(ip) for ip in unique))
    if any(value is None or value <= 0 for value in values):
        return {ip: LinkObservation(ip, LINK_UNKNOWN, method="latency-inconclusive",
                                    latency_ms=value) for ip, value in zip(unique, values)}
    first, second = float(values[0]), float(values[1])
    faster_i = 0 if first <= second else 1
    slower_i = 1 - faster_i
    ratio = max(first, second) / min(first, second)
    if ratio <= 2.0:
        return {ip: LinkObservation(ip, LINK_UNKNOWN, method="latency-inconclusive",
                                    latency_ms=value) for ip, value in zip(unique, values)}
    result = {}
    for index, (ip, value) in enumerate(zip(unique, values)):
        link = LINK_ETHERNET if index == faster_i else LINK_WIFI
        result[ip] = LinkObservation(ip, link, method="latency-race", latency_ms=value)
    return result


async def classify_address_group(ips: list[str], detector: LinkDetector, *, latency_sampler=median_version_latency) -> dict[str, LinkObservation]:
    unique = list(dict.fromkeys(str(ip).strip() for ip in ips if str(ip).strip()))
    observations_list = await asyncio.gather(*(asyncio.to_thread(detector.detect, ip) for ip in unique))
    observations = {item.ip: item for item in observations_list}
    if len(unique) != 2:
        return observations
    known = [observations[ip].link_type for ip in unique if observations[ip].link_type != LINK_UNKNOWN]
    contradictory = len(known) == 2 and known[0] == known[1]
    incomplete = any(observations[ip].link_type == LINK_UNKNOWN for ip in unique)
    if contradictory or incomplete:
        return await latency_race(unique, sampler=latency_sampler)
    return observations


def device_identity(info: dict, ip: str = "") -> tuple[str, str]:
    unique_id = str(info.get("unique_id") or info.get("uniqueId") or "").strip()
    hostname = str(info.get("hostname") or "").strip()
    if unique_id:
        return f"uid:{unique_id.casefold()}", "unique_id"
    if hostname:
        return f"host:{hostname.casefold()}", "hostname"
    return f"ip:{str(ip).strip()}", "ip"


def merge_known_device(known_devices: dict, *, info: dict, address: dict) -> tuple[str, dict]:
    """Persist one observation without discarding addresses absent this scan."""
    identity, identity_source = device_identity(info, address.get("ip", ""))
    existing = known_devices.get(identity) if isinstance(known_devices, dict) else None
    record = dict(existing) if isinstance(existing, dict) else {}
    record.update({
        "identity": identity,
        "identity_source": identity_source,
        "unique_id": str(info.get("unique_id") or info.get("uniqueId") or record.get("unique_id") or ""),
        "hostname": str(info.get("hostname") or record.get("hostname") or ""),
        "product": str(info.get("product") or record.get("product") or "Ultimate"),
        "firmware": str(info.get("firmware_version") or info.get("firmwareVersion") or record.get("firmware") or ""),
        "core": str(info.get("core_version") or info.get("coreVersion") or record.get("core") or ""),
        "last_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    addresses = record.get("addresses") if isinstance(record.get("addresses"), dict) else {}
    ip = str(address.get("ip") or "").strip()
    if ip:
        previous = addresses.get(ip) if isinstance(addresses.get(ip), dict) else {}
        merged = dict(previous)
        merged.update({key: value for key, value in address.items() if value not in (None, "")})
        merged["ip"] = ip
        merged["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        addresses[ip] = merged
    record["addresses"] = addresses
    known_devices[identity] = record
    return identity, record


def preferred_address(addresses: Iterable[dict]) -> dict | None:
    rows = [dict(row) for row in addresses if isinstance(row, dict) and row.get("ip")]
    if not rows:
        return None
    order = {LINK_ETHERNET: 0, LINK_UNKNOWN: 1, LINK_WIFI: 2}
    rows.sort(key=lambda row: (order.get(row.get("link_type"), 1),
                               tuple(int(part) for part in str(row["ip"]).split("."))))
    return rows[0]


def parse_wireshark_espressif(text: str) -> set[str]:
    """Extract explicit three-byte Espressif prefixes from Wireshark manuf."""
    found: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s+", line, maxsplit=2)
        if len(parts) < 2:
            continue
        vendor = " ".join(parts[1:])
        if "espressif" not in vendor.casefold():
            continue
        match = _OUI_RE.fullmatch(parts[0].upper())
        if match:
            prefix = match.group(1).upper()
            if prefix != WIRED_PREFIX:
                found.add(prefix)
    return found


def load_oui_cache(path: Path) -> tuple[set[str], dict]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return set(BUNDLED_ESPRESSIF_OUIS), {}
    prefixes = {str(item).upper() for item in data.get("prefixes", []) if _OUI_RE.fullmatch(str(item))}
    prefixes.discard(WIRED_PREFIX)
    if len(prefixes) < len(BUNDLED_ESPRESSIF_OUIS):
        return set(BUNDLED_ESPRESSIF_OUIS), data if isinstance(data, dict) else {}
    return set(BUNDLED_ESPRESSIF_OUIS) | prefixes, data if isinstance(data, dict) else {}


def refresh_oui_cache(path: Path, *, now: float | None = None, url: str = OUI_SOURCE_URL,
                      client_factory=None) -> set[str]:
    """Fail-soft additive refresh. Returns the effective prefix set."""
    now = time.time() if now is None else float(now)
    current, meta = load_oui_cache(path)
    checked = float(meta.get("checked_at", 0) or 0)
    if checked and now - checked < OUI_REFRESH_SECONDS:
        return current
    headers = {}
    if meta.get("etag"):
        headers["If-None-Match"] = str(meta["etag"])
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = str(meta["last_modified"])
    factory = client_factory or (lambda: httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=True))
    try:
        with factory() as client:
            response = client.get(url, headers=headers)
        if response.status_code == 304:
            merged = dict(meta)
            merged["checked_at"] = now
        elif response.status_code == 200:
            parsed = parse_wireshark_espressif(response.text)
            if len(parsed) < len(BUNDLED_ESPRESSIF_OUIS):
                return current
            effective = set(BUNDLED_ESPRESSIF_OUIS) | parsed
            effective.discard(WIRED_PREFIX)
            merged = {
                "checked_at": now,
                "etag": response.headers.get("etag", ""),
                "last_modified": response.headers.get("last-modified", ""),
                "source": url,
                "prefixes": sorted(effective),
            }
            current = effective
        else:
            return current
        temp = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
        temp.write_text(json.dumps(merged, indent=2, sort_keys=True), "utf-8")
        os.replace(temp, path)
    except Exception:
        return current
    return current

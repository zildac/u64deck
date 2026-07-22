"""Ultimate device discovery.

Same approach as Ultimate64 Manager (and Assembly64): there is no true
broadcast/mDNS announce on the Ultimate firmware, so "discovery" is a fast
parallel sweep of the local /24 subnets:

  phase 1: async TCP connect to port 80 on every host (short timeout)
  phase 2: GET /v1/info on each hit and check it looks like an Ultimate

Works the same on Windows, macOS and Linux — no raw sockets, no admin rights.
"""

import asyncio
import ipaddress
import socket

import httpx

# Products the /v1/info endpoint may report
_PRODUCT_HINTS = ("ultimate", "u64", "u2", "1541", "commodore")

CONNECT_TIMEOUT = 0.25   # phase-1 TCP timeout (generous for Wi-Fi)
VERIFY_TIMEOUT = 1.5     # phase-2 HTTP timeout
CONCURRENCY = 256


def local_subnets() -> list[str]:
    """Best-effort list of local /24 network prefixes ("192.168.1.")."""
    ips = set()
    # Primary route trick — works everywhere incl. Windows, no packet sent
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    # Extra interfaces (multi-homed hosts)
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


async def _port_open(ip: str, port: int, sem: asyncio.Semaphore) -> str | None:
    async with sem:
        try:
            fut = asyncio.open_connection(ip, port)
            reader, writer = await asyncio.wait_for(fut, CONNECT_TIMEOUT)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return ip
        except (OSError, asyncio.TimeoutError):
            return None


async def _verify(client: httpx.AsyncClient, ip: str, port: int = 80) -> dict | None:
    try:
        r = await client.get(f"http://{ip}:{port}/v1/info")
        if r.status_code != 200:
            return None
        j = r.json()
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    product = str(j.get("product", ""))
    hostname = str(j.get("hostname", ""))
    blob = (product + hostname).lower()
    if not any(h in blob for h in _PRODUCT_HINTS):
        return None
    return {
        "ip": ip,
        "product": product or "Ultimate (unknown model)",
        "firmware": j.get("firmware_version") or j.get("firmwareVersion") or "",
        "hostname": hostname,
        "core": j.get("core_version") or j.get("coreVersion") or "",
    }


async def discover(extra_subnets: list[str] | None = None, port: int = 80) -> dict:
    """Sweep local /24s; return {'subnets': [...], 'devices': [...]}."""
    subnets = local_subnets()
    for s in extra_subnets or []:
        s = s if s.endswith(".") else s + "."
        if s not in subnets:
            subnets.append(s)
    if not subnets:
        return {"subnets": [], "devices": []}

    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        asyncio.create_task(_port_open(f"{net}{i}", port, sem))
        for net in subnets
        for i in range(1, 255)
    ]
    hits = [ip for ip in await asyncio.gather(*tasks) if ip]

    devices = []
    if hits:
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT) as client:
            results = await asyncio.gather(*(_verify(client, ip, port) for ip in hits))
        devices = [d for d in results if d]
    devices.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
    return {"subnets": [s + "0/24" for s in subnets], "devices": devices}

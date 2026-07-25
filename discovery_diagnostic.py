#!/usr/bin/env python3
"""Run the production u64deck discovery transport outside the web application.

This diagnostic imports :mod:`discovery_transport`; it does not contain a
second scanner implementation. It reads no u64deck configuration and changes
no Ultimate state.
"""

from __future__ import annotations

import argparse
import ipaddress
import time

import discovery_transport

CONNECT_TIMEOUT = 1.5
RESPONSE_TIMEOUT = 3.25


def _print_hits(results: list[dict]) -> None:
    for row in results:
        if row.get("status") != "ultimate":
            continue
        payload = row.get("payload") or {}
        print(
            f"  {row['ip']:<15} "
            f"connect {row.get('connect_ms', '?'):>7} ms  "
            f"TTFB {row.get('time_to_first_byte_ms', '?'):>7} ms  "
            f"request {row.get('elapsed_ms', '?'):>7} ms  "
            f"{payload.get('hostname', '')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise the exact transport used by u64deck Finder."
    )
    parser.add_argument("--subnet", required=True, help="A /24 such as 192.168.249.0/24")
    parser.add_argument(
        "--cached", action="append", default=[], metavar="IP",
        help="Previously known address to verify first; may be repeated.",
    )
    parser.add_argument("--port", type=int, default=80)
    args = parser.parse_args()

    try:
        network = ipaddress.IPv4Network(args.subnet, strict=False)
    except ValueError as exc:
        parser.error(str(exc))
    if network.prefixlen != 24:
        parser.error("The production Finder diagnostic intentionally requires a /24")

    cached = []
    for value in args.cached:
        try:
            address = ipaddress.IPv4Address(value)
        except ValueError:
            parser.error(f"Invalid cached address: {value}")
        if address in network and str(address) not in cached:
            cached.append(str(address))

    started = time.perf_counter()
    print("STAGE 1 — cached-first production transport")
    cached_results = discovery_transport.scan_direct(
        cached, 4, CONNECT_TIMEOUT, RESPONSE_TIMEOUT,
        started, "cached-first", args.port)
    _print_hits(cached_results)

    cached_set = set(cached)
    remaining = [str(address) for address in network.hosts() if str(address) not in cached_set]
    print("STAGE 2 — direct /24 production transport")
    subnet_results = discovery_transport.scan_direct(
        remaining, 64, CONNECT_TIMEOUT, RESPONSE_TIMEOUT,
        started, "direct-subnet", args.port)
    _print_hits(subnet_results)

    all_results = [*cached_results, *subnet_results]
    counts = discovery_transport.outcome_counts(all_results)
    total_ms = round((time.perf_counter() - started) * 1000.0, 1)
    print(f"Total runtime: {total_ms / 1000.0:.3f} seconds")
    print("Outcomes: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    return 0 if counts.get("ultimate", 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())

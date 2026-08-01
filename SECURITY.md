# Security Policy

## Reporting a vulnerability

If you believe you've found a security issue in u64deck, please report it privately
rather than opening a public issue:

- Use GitHub's **[Report a vulnerability](../../security/advisories/new)** (Security → Advisories)
  for a private, coordinated report, **or**
- Open a normal GitHub issue for anything low-risk or clearly non-sensitive.

Please include the affected version/build (the banner shows a build ID, e.g. `fc1e0fb`),
the platform (Windows or Linux), and enough detail to reproduce. I'll acknowledge reports
as soon as I reasonably can — this is a hobby project maintained in spare time, so please be
patient with response times.

## What u64deck is, security-wise

u64deck is a **locally-run control server** (Python / FastAPI) that serves a web UI to a
browser and relays commands to a Commodore Ultimate device over the device's own
plain-HTTP REST API. Understanding that shape matters for what is and isn't a meaningful
issue:

- **The primary concern is the host.** The question that matters most is whether a request
  reaching the u64deck server could compromise the *machine it runs on* — code execution,
  arbitrary file access, or similar.
- **Control of the attached Commodore hardware is a lower bar by design.** On a
  network-reachable deployment, a peer on your LAN can drive the machine — that's an
  intended capability of a remote-control tool, not a vulnerability in itself.
- **Transport:** the u64deck → Ultimate leg is plain HTTP because the device firmware
  offers no TLS; this cannot be changed from u64deck. See the note on network binding below.

## Default network binding

By default u64deck binds to `0.0.0.0`, so the control API is reachable from the local
network without authentication. This is a deliberate default for ease of use on a trusted
home network. If you'd prefer u64deck to listen on loopback only, set the interface to
`127.0.0.1` in the configuration; LAN access then becomes an explicit opt-in.

If you deploy u64deck on an untrusted network, treat the control API as unauthenticated and
restrict access at the network layer.

## Static security review

A host-compromise-focused **static security review** has been carried out on the
application source (`server.py`, all shared modules, and the Linux runtime/scripts). In
summary, no request-reachable path to remote code execution, SQL injection, filesystem
traversal, arbitrary file write, or parser-level denial of service was found; the one
observation recorded is the default network binding described above, which is a deployment
preference rather than a host-compromise vector.

The review is a point-in-time, static assessment of the reviewed build — a good-faith
engineering review, not a warranty or certification. It does not cover dynamic/runtime
testing, third-party dependency CVEs, the browser-side JavaScript, or the device firmware.

## Supported versions

Security fixes target the current release line. Please make sure you can reproduce an issue
on the latest published build before reporting.

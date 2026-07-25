# Changelog

## 1.9.0 — Release Candidate 13

- Changed SID Jukebox Stop under split dual-interface routing to use the
  verified Wi-Fi REST-control path first instead of the selected Ethernet
  command socket. This avoids the wired path associated with the repeatable
  approximately three-second Stop delay seen when both Ultimate interfaces are
  enabled; hardware confirmation remains the RC13 acceptance gate.
- Made split-route Stop cartridge-safe. A configured fast cartridge is parked,
  the C64 is reset to its normal screen, and the cartridge setting is restored
  afterwards without activating it. Command-socket reset remains the fallback
  if REST delivery fails.
- Preserved the per-device SID-runner recovery flag. The first Mount & Run
  after native SID playback still performs the proven full Ultimate reboot
  before mounting, even after Stop has returned the machine to the C64 screen.
- Added per-stage SID Play diagnostics covering coordinator wait, lazy SID
  fetch, cartridge lookup/park, native SID upload, cartridge restore and state
  commit. The API response includes the same timings so any remaining Play
  delay can be attributed from hardware evidence rather than guessed.
- Retained RC12 split Ethernet/Wi-Fi routing, hybrid post-load RUN delivery,
  post-SID Mount & Run recovery and the frozen discovery implementation.

## 1.9.0 — Release Candidate 12

- Added split dual-interface routing for Ultimate devices with both Ethernet
  and Wi-Fi verified. Selecting Ethernet keeps port 64, FTP and streaming on
  the wired address while ordinary REST control uses the paired Wi-Fi address.
- Hardware isolation reproduced the same approximately 2.5-second Ethernet
  REST response delay on an Ultimate 64 running firmware 3.15 and an older C64
  Ultimate running different firmware. Wi-Fi merely being enabled at boot was
  sufficient; discovery and Wi-Fi HTTP requests were not required to trigger it.
- The connected header and Screen status now show **Ethernet · REST via Wi-Fi**
  whenever split routing is active. Connect responses and Diagnostics also
  expose the selected and REST-control addresses.
- Stream start/stop uses the selected Ethernet command socket first under split
  routing, preserving the wired media path without waiting on the slow Ethernet
  REST listener. REST remains the fallback.
- Persisted split routing is accepted only when both addresses still belong to
  the same known device. A newly discovered Wi-Fi address is used automatically
  only when it was verified in the current Finder scan.
- Retained RC11's reduced polling/hand-off behaviour, the hybrid post-load RUN
  delivery and the automatic post-SID recovery reboot before the next Mount & Run.

## 1.9.0 — Release Candidate 11

- Preserved the hardware-passed RC10 discovery implementation byte-for-byte,
  including the 1.5-second TCP-connect deadline, 3.25-second post-connect
  response allowance, cached-first ordering, 64-worker subnet phase, one
  `/v1/info` request per address, identity grouping and Ethernet preference.
- Removed the automatic Info and Mounted Drives refreshes that previously ran
  as soon as Finder completed.
- Connect now reuses the live Finder `/v1/info` result and current link
  classification. Manual addresses retain a fresh verification request.
- Removed the blocking CIA1 capability probe and redundant old-interface
  matrix release from same-device Ethernet/Wi-Fi hand-off. Cached capability
  follows the physical device; otherwise the check runs after Connect.
- Prevented routine status and drive polling from queueing behind interactive
  device work, and coalesced duplicate browser refreshes.
- Avoided sending a matrix `release_all` request when no key or chord is held.
  Opening Finder, changing tabs or losing screen focus therefore creates no
  unnecessary Ultimate REST request.
- Replaced the ambiguous address controls with a visibly selected Ethernet or
  Wi-Fi choice and one explicit **Use selected address** button.
- RC10 passed discovery hardware testing but failed connection/UI responsiveness
  acceptance. RC11 is the contained integration correction and requires fresh
  hardware validation.

## 1.9.0 — Release Candidate 10

- Replaced RC9's single 1.5-second discovery request timeout with the
  hardware-proven split-timeout transport. Each address receives 1.5 seconds
  for TCP connection establishment and, only after connection succeeds, 3.25
  seconds for the `/v1/info` response.
- Hardware investigation on Ultimate 64 firmware 3.15 showed the wired endpoint
  connecting in roughly 5–54 ms while sometimes delaying the first HTTP byte
  for about 2.1–2.6 seconds. The single RC9 deadline therefore rejected a live
  Ethernet interface even though TCP had already connected.
- Kept the bounded design: four cached-address workers, 64 subnet workers, one
  request per address, no port pre-scan, no retry pass, no asynchronous HTTP
  pool and no follow-up REST probes for link classification.
- The production Finder and `discovery_diagnostic.py` continue to import the
  same implementation from `discovery_transport.py`; the diagnostic is not a
  separate scanner.
- Two consecutive clean-cache `/24` hardware scans found Ethernet and Wi-Fi,
  grouped both responses by firmware `unique_id`, preferred verified Ethernet
  and completed in about 6.1 seconds. A cached-first hardware scan also found
  both interfaces and preferred Ethernet.
- Expanded README and built-in Help documentation to explain why connection and
  response deadlines are separate. No Mount & Run, SID, Jukebox, keyboard,
  streaming, Mounted Drives or other machine-control behaviour changed.

## 1.9.0 — Release Candidate 9

- Replaced RC8's unvalidated asynchronous discovery transport with the exact
  scanner model proven by the standalone hardware diagnostic:
  `ThreadPoolExecutor + urllib`, four cached-address workers, 64 subnet workers,
  a 1.5-second timeout, one direct `/v1/info` request per address, no port
  pre-scan and no same-scan retry.
- Moved that scanner into `discovery_transport.py`. Both the Finder and the
  supplied `discovery_diagnostic.py` import the same production module; there is
  no second implementation of the network transport.
- Removed discovery's latency-race classification path. Ethernet/Wi-Fi labels
  now use current MAC/ARP evidence only, so interface enumeration cannot add
  `/v1/version` probes after the bounded `/v1/info` scan.
- Retained RC8's polling suspension, overlap guard, verified-only results,
  `unique_id` grouping, DHCP replacement handling and Ethernet preference.
- Added transport-level and application-level regression coverage proving that
  cached addresses are scanned first, are excluded from the subnet stage and
  every candidate is submitted at most once.
- RC8 failed dual-interface hardware acceptance and should not be published.
  No Mount & Run, SID, Jukebox, keyboard, streaming, Mounted Drives or other
  machine-control behaviour changed in RC9.

## 1.9.0 — Release Candidate 8

- Replaced the RC5–RC7 discovery experiments with the bounded cached-first
  design proven by standalone hardware tests. Previously verified addresses on
  the current local `/24` are requested first using direct `/v1/info` calls.
- Every remaining subnet address then receives exactly one direct `/v1/info`
  request using 64 workers and a 1.5-second per-address timeout. There is no TCP
  port pre-scan, no second port pass and no same-scan retry storm.
- Hardware measurement on U64 firmware 3.15 found both live interfaces reliably:
  Ethernet responded in about 53 ms, Wi-Fi in about 38 ms, both appeared after
  about 3.7 seconds and the complete fresh `/24` scan finished in about 6.7
  seconds. The earlier apparent multi-second Ethernet delay was scan contention,
  not intrinsic wired-interface latency.
- Routine `/api/info` and `/api/drives` polling now pauses while Finder is
  active, overlapping scans are rejected and normal polling resumes after the
  bounded scan. Discovery uses separate short-lived HTTP clients, so it cannot
  queue work on the active Ultimate REST backend.
- Expanded README and built-in Help documentation covering interface-aware
  identity, cached-first verification, Ethernet preference, link-dependent UI
  features and REST etiquette for Ultimate network clients. Existing U64
  Manager and Assembly64 acknowledgements are retained and clarified.
- No Mount & Run, SID, keyboard, streaming, Mounted Drives or machine-control
  behaviour changed.

## 1.9.0 — Release Candidate 7

- Fixed the evidence-confirmed dual-interface discovery failure on U64 firmware
  3.15. A standalone direct `/v1/info` scan found Wi-Fi in about 45 ms but the
  healthy Ethernet interface required about 2.2 seconds; the previous 1.5-second
  verification limit therefore discarded Ethernet before identity grouping.
- Retained the fast TCP subnet sweep and the verified-only grouping rules, but
  increased the normal `/v1/info` verification window to 3.25 seconds and the
  controlled post-batch retry to 4.5 seconds. The retry remains sequential so
  two interfaces on one Ultimate do not compete for its firmware HTTP service.
- The Find Ultimate Devices request now has a discovery-specific 45-second
  browser window. The normal 15-second timeout for ordinary device operations
  is unchanged.
- Added diagnostics for slow `/v1/info` responses and regression coverage for
  the real hardware timing shape. No Mount & Run, SID, keyboard, streaming,
  Mounted Drives or machine-control code changed.

## 1.9.0 — Release Candidate 6

- Fixed the remaining dual-interface discovery failure where Ethernet could
  lose the initial TCP port-80 pre-probe when Ethernet and Wi-Fi were enabled
  on the same Ultimate. RC5 retried only `/v1/info`, so an address discarded
  before verification could never be recovered.
- Configured and remembered addresses now bypass the competing TCP pre-probe
  and are verified directly, sequentially. Fresh scans retain the fast bulk
  sweep, then perform one lower-contention second port pass on the subnet of a
  verified Ultimate before sequentially verifying any recovered addresses.
- Successfully recovered Ethernet and Wi-Fi responses continue to be grouped
  by firmware `unique_id`, with verified Ethernet recommended. An interface
  that fails both port passes or verification remains historical only and is
  never displayed or selected from stale data.
- Added hardware-shaped regression coverage in which Ethernet loses the TCP
  pre-probe itself, then is recovered, grouped with Wi-Fi and preferred. No
  Mount & Run, SID, input, streaming or machine-control code changed.

## 1.9.0 — Release Candidate 5

- Fixed dual-interface discovery when Ethernet and Wi-Fi are enabled on the
  same Ultimate. The fast concurrent verification pass is retained, but any
  address that loses the initial `/v1/info` race is retried once after the
  parallel batch has finished.
- Verification retries are sequential, preventing the two interfaces of one
  physical Ultimate from competing for the firmware HTTP service. Successfully
  recovered addresses are grouped by the existing firmware identity and the
  verified Ethernet address remains preferred.
- Verified-only safeguards remain unchanged: an interface that does not answer
  either verification attempt is omitted from the live result and cannot be
  recommended merely because it exists in discovery history.
- Added end-to-end regression coverage for recovery of a competing Ethernet
  interface and for a genuinely unavailable historical interface remaining
  absent after retry. No Mount & Run, SID, input or streaming code changed.

## 1.9.0 — Release Candidate 4

- Added conditional SID-runner recovery before Mount & Run. Successful native
  SID playback is remembered per Ultimate, including after Stop or natural
  completion, because affected firmware can retain player state that a normal
  C64 reset does not fully clear.
- The first Mount & Run after SID-player activity now disarms Jukebox callbacks,
  performs one full `machine:reboot`, closes the pre-reboot command socket,
  waits for the Ultimate REST service to return, and only then mounts the disk
  and continues through the established reset, readiness gates, LOAD and RUN
  sequence. Mount & Run is unchanged when no SID has played.
- A successful explicit Reboot clears the pending recovery state. Failed reboot
  or return-to-service checks leave the state armed and abort before mounting,
  preventing a false successful launch.
- Added regression coverage for per-device SID-runner state, successful and
  failed recovery reboots, operation ordering, manual Reboot clearing and the
  unchanged ordinary Mount & Run path. Jukebox Stop routing is unchanged.

## 1.9.0 — Release Candidate 3

- Fixed a state-dependent Mount & Run race after native SID playback. Every
  SID completion timer now carries a generation token, and stale callbacks exit
  without resetting the C64, starting another SID or reclaiming the machine
  after a disk launch has begun.
- Mount & Run, Mount & Load, explicit Reset/Reboot and non-Jukebox runner
  actions now disarm pending SID timers, stop-after-current and Radio state
  before taking ownership of the machine. The current play queue remains
  available but is no longer marked as playing.
- SID auto-advance is serialised with other interactive device operations and
  rechecks its generation after waiting. Whichever action acquires the device
  first completes normally; a later stale Jukebox callback is ignored.
- Added regression coverage for timer cancellation, generation invalidation,
  stale completion callbacks, Mount & Run after a finished SID and non-Jukebox
  runner takeover. Jukebox Stop transport routing is unchanged.

## 1.9.0 — Release Candidate 2

- Fixed a cold-start lifecycle race in `/api/drives`. Mounted Drives now takes
  the same status-priority coordinator lock as `/api/info` before capturing the
  active REST backend, so Connect or Clear cannot replace and close that client
  while a waiting drive refresh still holds it.
- A closed-client handover is retried once against the current backend. If no
  usable backend is available, the API returns a controlled temporary status
  with the last confirmed mount snapshot and the browser retries, rather than
  exposing an ASGI traceback or generic Internal Server Error.
- Added defensive handling for malformed rows in a device drive payload without
  changing normal `/v1/drives`, mount, swap or BUSY behaviour.
- Updated the README to require clean-folder upgrades and document exactly which
  settings, favourites, playlists and SQLite data files may be copied after the
  previous process has stopped. Overlay installs and stale runtime files are
  explicitly unsupported.
- SID Jukebox Stop routing is unchanged from the hardware-verified RC1 code; the
  reported delay was not reproducible after extraction to a clean folder.

## 1.9.0 — Release Candidate 1

- Mount & Run now exposes its locally confirmed mount state while reset, LOAD
  and RUN continue in the same synchronous operation. The Mounted Drives strip
  updates as soon as the Ultimate accepts the image, retains that filename and
  mode through a slow genuine-drive load, and reconciles with `/v1/drives`
  immediately when loading finishes.
- `/api/drives` recognises the same expected Mount & Run BUSY state as
  `/api/info`, returning the confirmed local mount snapshot without contacting
  the occupied Ultimate HTTP service. Raw drive-status timeout text is therefore
  suppressed only during the known loading window; ordinary errors remain
  visible at all other times.
- Promoted the release line to v1.9.0 Release Candidate 1 and updated active
  backend, UI, README, packaging, workflow and test metadata consistently.
- Expanded the searchable built-in Help with a dedicated Find Ultimate Devices
  guide, clearer Mounted Drives and image-inspection behaviour, Quick Launch,
  Assembly64, indexing, Settings and BUSY-state troubleshooting details. Help
  remains version-agnostic.
- Expanded the canonical README gallery from six to twelve entries covering
  the device finder, link-aware header, Wi-Fi streaming gate, Mount & Run,
  BUSY loading and disk swap. Added a standalone gallery release gate that
  verifies both canonical order and matching PNG files before publication.
- Feature scope remains frozen: readiness gates, hybrid RUN delivery, reset
  routing, discovery, disk grouping, SID playback and streaming behaviour are
  unchanged from the hardware-verified beta baseline.

## 1.8.0 — Public Beta 17

- Jukebox Stop now uses the fastest proven reset route for the connected
  device. Legacy/C64U sessions reset directly through REST and use a fresh
  port-64 command connection only as a fallback; CIA1-capable U64 sessions
  retain fresh-command-socket-first delivery with REST fallback.
- Stop routing uses the capability result already cached during connection and
  never adds a new CIA1 probe to the button path. Matrix release is also
  cached-only, keeping Stop responsive when the device is busy.
- During a genuine-drive Mount & Run load, `/api/info` now reports the local
  expected state `BUSY — loading program…` without contacting the occupied
  Ultimate HTTP service. The UI shows this as an amber status rather than a red
  offline timeout and retries status locally every two seconds.
- All browser Mount & Run entry points request an immediate status refresh when
  the operation completes or fails. Normal offline handling resumes after the
  Mount & Run operation has ended.
- Mount & Run readiness gates, hybrid CIA1 RUN delivery, Legacy RUN delivery,
  REST-client lifecycle locking, discovery and disk-swap behaviour remain
  unchanged from Public Beta 16.

## 1.8.0 — Public Beta 16

- Serialised device-backend replacement with active Ultimate operations so
  Connect and Clear discovered devices cannot close the shared REST client
  while `/api/info` or another request is using it.
- Added a per-client lifecycle lock around the internal `httpx.Client`,
  including timeout reconfiguration, request execution and closure. This
  prevents the `Cannot send a request, as the client has been closed` race
  observed during Beta 15 hardware testing.
- Mount & Run continues to send `LOAD"*",8,1` through the established command
  buffer and retains both `$CC` readiness gates. After a successful load gate,
  CIA1-capable U64 firmware now receives `R`, `U`, `N` and Return as matrix key
  taps; Legacy/C64U sessions retain command-buffer RUN delivery.
- A failed CIA1 RUN request is not automatically repeated through the buffer,
  avoiding a duplicate RUN after an ambiguous transport timeout. Diagnostics
  records the selected RUN delivery method or failure.
- Firmware without `machine:readmem` still uses the complete Public Beta 11
  fixed-delay command-buffer sequence unchanged.

## 1.8.0 — Public Beta 15

- Discovery now treats persisted addresses as history and probe candidates,
  never as proof that an interface is online. The device list, preferred
  address and switch controls contain only addresses that answered `/v1/info`
  during the current scan or current verified connection.
- Disabled Wi-Fi and disconnected Ethernet interfaces disappear on the next
  scan. DHCP replacements remove the superseded address when the same interface
  MAC is observed at a new IP, while non-responding history keeps its original
  `last_seen` value instead of being refreshed falsely.
- Added **Clear discovered devices** to the Select Ultimate dialog. After a
  confirmation it clears remembered hosts and the active connection, preserves
  all unrelated settings, writes `config.json` atomically and immediately runs
  a clean subnet scan.
- Added concise discovery diagnostics for candidate/response/device counts,
  omitted historical addresses, DHCP replacements and preferred-host updates.
- Jukebox Stop now discards an idle port-64 connection and sends reset over a
  newly established command socket. REST reset remains the compatibility
  fallback and the chosen delivery path is recorded in Diagnostics.
- Retains the hostname-first scanner label, `$CC`-gated Mount & Run behaviour
  and conservative disk-swap filename additions introduced in the previous
  bench build, while preserving automatic CIA1/Legacy capability handling.

## 1.8.0 — Public Beta 14

- Rebuilt from the verified Public Beta 11 baseline, retaining its
  Ethernet/Wi-Fi discovery, interface classification and capability-driven
  CIA1/Legacy input behaviour unchanged.
- Mount & Run now waits for the KERNAL screen editor before typing both
  `LOAD"*",8,1` and `RUN`. The readiness gate polls zero-page `$CC` every
  500 ms, requires two consecutive zero readings, waits up to 120 seconds and
  records the result of both the boot and load gates in Diagnostics.
- Firmware without `/v1/machine:readmem` automatically keeps Public Beta 11's
  fixed-delay Mount & Run behaviour. Automation continues to use the existing
  keyboard-buffer command path on every device; no input-method selector or
  unified keyboard-routing refactor is included.
- The scanner now presents the device hostname first, retaining `unique_id`
  as the internal deduplication key and as a visible fallback only.
- Automatic disk-swap grouping now recognises compound numbered tokens such
  as `_0/_1a/_1b`, safe parenthesised tokens such as `(A)/(B)` and `(1)/(2)`,
  and title-less marker sets such as `side1/side2`. GoodTools square-bracket
  letters, families with an unsuffixed sibling and glued sequel-like digits
  remain deliberately excluded.

## 1.8.0 — Public Beta 11

- Added interface-aware discovery for Ultimate 64 and C64 Ultimate systems with
  simultaneous Ethernet and ESP32 Wi-Fi addresses. Scan results are grouped by
  `/v1/info` `unique_id` (hostname fallback), retain every known address in
  `config.json`, and prefer the wired address without preventing an explicit
  Wi-Fi connection.
- Added deterministic link classification from on-link neighbour-table MACs:
  the firmware's `02:15:41` wired signature is Ethernet, bundled Espressif OUIs
  identify Wi-Fi, and every other or off-link address remains Unknown. Dual
  addresses with contradictory classifications fall back to a three-sample
  `/v1/version` median-latency race.
- Added a fail-soft, additive-only weekly Espressif OUI refresh. The bundled
  330-prefix 2026-07 snapshot is permanent, malformed or incomplete downloads
  are discarded, and the wired signature can never enter the Wi-Fi set.
- Added Ethernet/Wi-Fi awareness to the scanner, connected-device header and
  Screen status. A Wi-Fi connection disables only video, audio, recording and
  full-screen controls, explains that firmware streaming is wired-only, and
  offers one-click switching to a remembered Ethernet address.
- Kept Storage, Settings, SID Jukebox, Assembly64 and file operations available
  over Wi-Fi, while extending the device REST timeout only for positively
  identified Wi-Fi links. Unknown links retain existing behaviour and show a
  soft Ethernet hint only when a stream starts but no video frames arrive.
- Expanded Help and README documentation, retained the canonical six-image
  screenshot gallery including `docs/settings.png`, and added regression
  coverage for identity deduplication, MAC classification, contradiction and
  latency fallbacks, OUI refresh safety, Wi-Fi gating and timeout scaling.
- Updated release metadata consistently for Public Beta 11 / archive 75.

## 1.8.0 — Public Beta 10.4

- Renamed the SID Jukebox queue column from **Len** to **Length** and now
  pre-populates it as soon as tunes enter the queue. Lazy HVSC entries use the
  path catalogue already present in `Songlengths.md5`, including the selected
  subsong, so the complete SID does not need to be fetched first; unknown
  durations display as an em dash.
- Made Jukebox **Stop** responsive immediately in the browser and changed the
  backend to send the command-socket reset packet first, with REST reset as a
  compatibility fallback. This avoids waiting behind ordinary device-status
  traffic while retaining the existing, expected reset to the C64 command line.
- Suppressed periodic Jukebox refreshes while Stop is pending so an older
  playing snapshot cannot briefly reappear in the UI.
- Corrected remaining README Jukebox wording so Search and Random Dive describe
  the one-tune queue behaviour introduced in Public Beta 10.3.
- Added regression coverage for path-based queued lengths, selected subsongs,
  the complete **Length** heading, command-socket reset framing and REST fallback.
- Updated release metadata consistently for Public Beta 10.4 / archive 74.

## 1.8.0 — Public Beta 10.3

- Changed individual SID results to create and play a one-tune queue instead of
  silently loading every SID in the containing composer folder. Folder queues
  are now loaded only through the explicit **Play This Folder** action, while
  **＋** appends only the selected tune.
- Changed **♪ More like this** to insert SIDFlow recommendations immediately
  after the current tune. Radio top-ups still append at the end, preserving
  explicit queue choices.
- Added **Clear Queue**. It disarms Radio, resets the SIDFlow session and clears
  pending tunes while allowing a currently playing SID to finish naturally;
  saved play queues, favourites and similarity data are unaffected. Non-trivial
  clears receive a confirmation in the UI.
- Expanded Help with the complete SIDFlow acquisition, local-processing, More
  like this, Radio, matching/fallback and queue-management behaviour, with
  prominent credit to **SIDFlow (Chris Gleissner)**.
- Made the README screenshot gallery canonical, updated the Jukebox caption for
  SIDFlow More like this and Radio, and restructured Quick start into standalone
  Windows executable, Windows source and cross-platform tiers. Added SmartScreen,
  SHA-256 and normal-user launch guidance.
- Added a final SIDFlow promotion fallback using SQLite's backup API when both
  the completed build and validated ready-copy remain locked against Windows
  filesystem renames. This avoids requiring elevation or another download.
- Added regression coverage for one-tune playback, recommendation insertion,
  queue clearing/Radio disarming, rename-free SIDFlow promotion and the
  canonical README structure.
- Updated release metadata consistently for Public Beta 10.3 / archive 73.

## 1.8.0 — Public Beta 10.2

- Fixed repeated Windows SIDFlow import failures caused by the fixed
  `.sidflow-similarity.sqlite.building` filename. Every import now uses a
  unique `.building-<id>` database, so a stale or externally locked artifact
  cannot block a new download.
- Closed every short-lived SIDFlow SQLite connection explicitly. Python's
  connection context manager handles transactions but does not close the file;
  the leaked handles previously blocked re-downloads and replacement of the
  live compact database on Windows.
- Made the validated `.ready-*` promotion fallback functional by closing its
  validation connection before `os.replace`, preventing u64deck from locking
  its own rescue copy.
- Changed stale `.building*`, `.ready-*` and interrupted-download cleanup to
  best-effort diagnostics. An undeletable remnant is reported once, is never
  shown as the current similarity-data error and cannot stop a fresh import.
- Added regression coverage for unique sequential build names, locked stale
  artifacts, balanced connection lifecycles, ready-copy fallback, repeated
  status polling followed by re-download, and diagnostic-only cleanup errors.
- Retained the Public Beta 10.1 perceptual-vector ranking, path corrections and
  SIDFlow attribution unchanged.
- Updated release metadata consistently for Public Beta 10.2 / archive 72.

## 1.8.0 — Public Beta 10.1

- Reworked SIDFlow recommendations to use the production export's perceptual
  `features_json` fingerprints rather than the highly quantised `e/m/c/p`
  classifier values. The importer extracts 48 documented numeric dimensions,
  computes corpus mean/standard deviation, z-scores and L2-normalises each
  track, then stores compact float32 unit vectors under local schema
  `u64deck-featvec-1`.
- Added a data-quality guard that reports and blocks recommendations when more
  than half of the extracted vectors are identical instead of silently
  returning meaningless 100%-similar matches.
- Changed acquisition to require the full SIDFlow feature export. The current
  mobile profile is no longer preferred because it omits `features_json`; the
  Settings copy now explains that the roughly 400 MB source is downloaded once,
  slimmed to a normally sub-40 MB local database and deleted.
- Fixed Windows final-import promotion failures by coordinating local database
  readers, extending lock retries, and using a validated temporary ready-copy
  fallback when the just-closed `.building` SQLite file remains locked.
- Corrected HVSC path joins to remain case-insensitive end-to-end, preserve the
  canonical SIDFlow track ID, strip an optional leading `C64Music/` segment and
  continue rejecting non-HVSC paths cleanly.
- Corrected the vertical alignment of the Jukebox Songlengths status and
  **Powered by SIDFlow (Chris Gleissner)** attribution.
- Expanded SIDFlow regression coverage for real feature extraction, missing
  values, normalisation, local vector-schema gating, degenerate data, Windows
  promotion fallback, case-insensitive mapping and `C64Music/` paths.
- Updated release metadata consistently for Public Beta 10.1 / archive 71.

## 1.8.0 — Public Beta 10

- Added **♪ More like this** to the SID Jukebox Now Playing area and Play Queue
  rows. Recommendations use the active SID/subsong as the seed and append the
  closest unseen matches already present in the current Ultimate SID index.
- Added **Radio** mode, which tops up the queue near its end using the most
  recently played tune while maintaining a session played-set to prevent
  repeats.
- Added a Settings → Search Index & Cache acquisition workflow for SIDFlow's
  portable similarity export: manifest-first schema gating, `SHA256SUMS`
  verification, streamed progress, mobile-profile preference, clean restart
  after interruption and atomic promotion of a completed local database.
- Added a compact `.sidflow-similarity.sqlite` importer that retains only track
  identity and the `e/m/c/p` similarity vectors, preserves future precomputed
  neighbour rows when supplied, and deletes the large downloaded source after
  a successful import.
- Added case-insensitive HVSC-root path mapping, exact subsong identity,
  cosine-similarity ranking with three-dimensional fallback when `p` is NULL,
  device-presence filtering and clear absence/schema/path-drift messages.
- Added prominent **Powered by SIDFlow (Chris Gleissner)** attribution in the
  Jukebox, Settings, Help, README and release notes.
- Added regression coverage for schema gating, slimming and source cleanup,
  path/subsong mapping, cosine ordering, device filtering, Radio no-repeat,
  graceful absence and release-archive hygiene.
- Updated release metadata consistently for Public Beta 10 / archive 70.

## 1.8.0 — Public Beta 9

- Corrected uploaded SID duration metadata to use the firmware-native per-SID
  `.ssl` format: two packed-BCD bytes per subtune (minutes and seconds), with a
  maximum attachment size of 512 bytes.
- Removed the Public Beta 8 behaviour that uploaded the complete multi-megabyte
  `Songlengths.md5` database for every SID. This prevents SID-player lag and
  avoids blocking unrelated REST requests such as mounted-drive status polling.
- Added a hard 512-byte client guard and automatic single-file fallback when a
  SID has no matching Songlengths entry, so playback never depends on duration
  metadata.
- Applied the corrected compact attachment consistently to SID Jukebox, local
  uploads, Quick Launch and Assembly64 playback while preserving `songnr` and
  the required duplicate multipart `file` field order.
- Refreshed the GitHub Actions build workflow to the current supported major
  versions of checkout, setup-python and upload-artifact before publication.
- Updated Help, README, release metadata and regression coverage consistently
  for Public Beta 9 / archive 69.

## 1.8.0 — Public Beta 8

- Added accurate duration metadata for uploaded SID playback. When HVSC
  `Songlengths.md5` is available, u64deck now sends it as the optional second
  multipart attachment accepted by `POST /v1/runners:sidplay`, allowing the
  Ultimate's native SID-player screen to display the documented subtune length
  instead of its generic five-minute estimate.
- Added a dedicated SID upload client that preserves the required attachment
  order and duplicate `file` field names: SID first, Songlengths second.
- Applied the enhanced upload path consistently to SID Jukebox playback, local
  uploads, Quick Launch library SIDs and Assembly64 SID deployment while
  retaining the selected `songnr`. Device-resident `PUT` playback remains
  unchanged.
- Retained exact Songlengths bytes alongside the parsed auto-advance index and
  preserved graceful single-file playback whenever Songlengths is unavailable.
- Updated Help, README, release metadata and regression coverage consistently
  for Public Beta 8 / archive 68.

## 1.8.0 — Public Beta 7

- Added capability-detected CIA1 keyboard-matrix input through the firmware's
  `machine:input` REST endpoint. Supported Ultimate 64 devices now receive real
  key press/release events, held keys and chords from the Screen tab, while
  older firmware and unsupported hardware retain the KERNAL-buffer fallback.
- Added browser-to-C64 matrix translation for letters, digits, punctuation,
  cursor directions, function keys, Shift, CTRL, Commodore, RUN/STOP and
  RESTORE. F2/F4/F6/F8 and left/up cursor movement use atomic Shift chords.
- Added visible SPACE, RUN/STOP and RESTORE quick actions and surfaced the
  detected input mode in the device header, Screen panel and diagnostics.
- Added held-key tracking and browser auto-repeat suppression, plus ordered
  matrix event batching within the firmware limits of 64 events per request and
  eight keys per keyboard event.
- Added `release_all` safety on focus loss, page/tab exit, stream and WebSocket
  shutdown, device changes, reset/reboot and mount flows so a remote key cannot
  remain stuck on the physical machine.
- Retained the existing keyboard-buffer path for Send Text, LOAD/RUN automation
  and non-capable devices. Retro Replay Auto-F7 now uses a matrix tap when the
  endpoint is available and falls back to PETSCII otherwise.
- Updated Help, README, release metadata and regression coverage consistently
  for Public Beta 7 / archive 67.

## 1.8.0 — Public Beta 6

- Added a genuine **Mount & Run** action for Assembly64 disk images. The image is
  mounted in drive A using the selected safety mode, the C64 is reset, and
  `LOAD"*",8,1` followed by `RUN` is entered after the configured boot delay.
- Resolved numeric Assembly64 category identifiers through both the service's
  preset metadata and a built-in canonical fallback, so results display labels
  such as **Demos** instead of raw values such as `1`.
- Added the Assembly64 **Rating** field to the search results table and retained
  it in the responsive full-height results layout.
- Replaced raw microsecond-precision Assembly64 timestamps with concise values
  such as **21 Jul 2026, 12:59**, while retaining the original value as a tooltip.
- Extended Assembly64 disk-image actions to recognise G64 images consistently.
- Updated README, release metadata and regression coverage consistently for
  Public Beta 6 / archive 66.

## 1.8.0 — Public Beta 5

- Rebuilt the Assembly64 tab as a responsive full-height workspace so it uses
  the complete available page rather than leaving unused space below the
  results. Search results and release files now remain visible in coordinated
  side-by-side panes, with a stacked fallback for narrow windows.
- Replaced raw lowercase search labels and abbreviated table headings with
  polished field names, descriptive placeholders, complete column titles,
  persistent empty/loading/error states and clearer release/file context.
- Added sticky table headings, selected-release highlighting, responsive
  metadata folding and consistent file-type/action columns.
- Added a Clear action and hardened generated file buttons so filenames
  containing quotes or other punctuation are passed safely to deploy actions.
- Updated Help, README, release metadata and regression coverage consistently
  for Public Beta 5 / archive 65.

## 1.8.0 — Public Beta 4

- Removed release-specific version references from the built-in Help content so
  operational guidance remains accurate across future updates. Diagnostics and
  index-migration help now describe behaviour directly rather than referring to
  the release that introduced it.
- Added regression coverage that rejects Public Beta labels and semantic
  version references in the built-in Help document.
- Refreshed the GitHub Actions workflow to current supported action majors and
  made the Windows executable smoke test poll for readiness, fail clearly if the
  process exits early, and always clean up the test process.
- Removed a stale historical version reference from the mount-policy migration
  comment and updated release metadata consistently for Public Beta 4 / archive
  64.

## 1.8.0 — Public Beta 3

- Fixed the misleading error toast shown when **Image Parse Details** loaded
  successfully. The shared API helper now treats only plain-text entries in an
  `errors` array as device errors, while structured parse-failure records remain
  available to the details panel without producing an `[object Object]`
  notification.
- Retains the Public Beta 2 Windows index-migration fixes, including explicit
  SQLite connection closure, bounded sharing-violation retries and safe cleanup
  after migration failures.
- Added frontend regression coverage and updated release metadata consistently
  for Public Beta 3 / archive 63.

## 1.8.0 — Public Beta 2

- Fixed the Windows first-run migration failure where SQLite connections used
  for metadata reads, snapshots and coverage validation were committed but not
  explicitly closed before the temporary database was promoted. This could
  leave both `.u64deck-index.sqlite3` and the `.migrating-*` file locked by the
  u64deck process itself and raise `WinError 32`.
- Added bounded retries around the final atomic replacement and temporary-file
  removal for brief antivirus or Windows Search sharing locks.
- Migration cleanup is now best-effort and can no longer replace the original
  migration error with a second cleanup traceback; the application falls back
  safely and reports both conditions.
- Added regression coverage for Windows sharing-violation retries and cleanup
  failure handling, and updated release metadata consistently for Public Beta 2
  / archive 62.

## 1.8.0 — Public Beta 1

- Hardened the automatic legacy-index migration for older databases containing
  orphaned cache rows from interrupted or historic writes. Unusable child rows
  are skipped while valid directory, image and SID records continue to merge.
- Foreign-key migration failures now report readable table and row details
  instead of raw Python `sqlite3.Row` object addresses.
- Updated Public Beta 1 migration regression coverage for archive 61.
- Replaced the IP-derived SQLite filename with one stable installation-local
  `.u64deck-index.sqlite3`, so DHCP changes no longer select a new empty or
  partial storage/image/SID index.
- Added an automatic first-run migration for older per-IP databases. u64deck
  creates consistent snapshots in a dated `index-backups` folder, merges the
  newest directory, disk-image and SID records, validates coverage, and only
  then atomically promotes the merged database. Original databases remain
  untouched, and migration failure falls back safely to the current legacy
  index.
- Search Index & Cache now shows the active database filename and the number of
  legacy databases merged.
- Added a Now Playing star beside the SID playback controls. It favourites the
  exact device SID currently playing and stays synchronised with the matching
  Play Queue and Favourites stars.
- Updated in-app Help, README, diagnostics/release metadata and regression
  coverage for Public Beta 1 / archive 61.

## 1.7.2 — responsive full-height SID play queue

- Reworked the SID Jukebox as a full-height workspace. The Play Queue now
  expands into the remaining window space instead of stopping at a fixed 480px
  height and leaving a large unused area below it.
- Replaced the widely spaced fixed table with a responsive queue grid. Title
  uses the available width, Chip/Length/Actions remain compact, column headings
  stay visible while rows scroll, and narrow windows fold author/release
  details beneath the title.
- Composer-folder queues with one fully indexed author now show that author once
  in the Play Queue heading rather than repeating it on every row. Mixed-author
  and incomplete-metadata queues retain the Author column.
- Updated Help, README, accessibility roles and regression coverage consistently
  to v1.7.2 / archive 59.

## 1.7.1 — visible SID index status and chip colour badges

- Added a clearly labelled **SID Index** button to the SID Jukebox browser
  toolbar. It opens the metadata controls directly and shows the live parsed
  count while scanning or the total indexed tune count when complete.
- Added consistent semantic chip badges throughout search results, play queues
  and now-playing details: amber for 6581, cyan for 8580, violet for Either,
  pink for Mixed/Multi-SID and grey for Unknown.
- Kept the active search filter indication in the Chip dropdown so badge colour
  identifies metadata rather than implying selection.
- Updated Help, README and regression coverage consistently to v1.7.1 /
  archive 58.

## 1.7.0 — indexed SID metadata and filtered HVSC search

- Added a persistent SQLite SID metadata catalogue populated from PSID/RSID
  headers. Play queues remain lazy, but title, author, chip, format, release,
  subtune count, default subtune, clock and SID count can now appear before a
  tune is played.
- Added **Chip** search filters for 6581, 8580, Either, Mixed/Multi-SID and
  Unknown, plus a separate **Format** filter for PSID and RSID. Filter-only
  searches are supported without a text term.
- Added **Refresh From Ultimate**, which incrementally reads only SID headers
  over FTP, with pause, resume, stop, progress and optional forced rescanning.
- Added **Build From Local HVSC**, using the same local-path-to-Ultimate-path
  model as the storage indexer. It accepts the HVSC folder directly or an
  unambiguous drive root containing HVSC/C64Music, reads only the first 124
  bytes of each SID and reconciles deleted tunes after a completed scan.
- Updated Settings cache statistics, SID Jukebox Help, README, configuration
  examples and regression coverage. Release metadata is consistent with
  v1.7.0 / archive 57.

## 1.6.7 — aligned application and firmware header

- Reworked the top status area as a two-row header so the u64deck identity,
  version/build and connected Ultimate firmware details share a consistent
  baseline instead of appearing as unrelated blocks.
- Increased the version and firmware detail text to a readable scale beside
  the top-bar buttons, while retaining emphasis on **u64deck**, **C64 Ultimate**
  and the deliberate `READY.` status line.
- Added responsive stacking for narrower windows without returning to the
  previous offset or undersized firmware presentation.
- Updated release metadata and regression checks consistently to v1.6.7 /
  archive 56.

## 1.6.6 — persistent drive visibility and swap restoration

- Reconstructs an automatic Disk Swap family from the image currently mounted
  in Drive A or Drive B when u64deck starts or **Refresh Drive Status** is used.
  Existing manual and uploaded queues are preserved when the mounted image is
  still one of their members.
- Added a compact mounted-drive summary at the top of both SCREEN and STORAGE,
  showing the current image and RO/RW/UNLINKED mode for each drive. Clicking it
  opens the full **Mounted Drives** controls.
- Automatic matcher decisions are now reported after device-image mounts and
  retained in the Disk Swap panel, including the related filenames or the safe
  single-image fallback.
- Added regression coverage and updated Help, README and release metadata
  consistently to v1.6.6 / archive 55.

## 1.6.5 — disk-set suffix compatibility

- Fixed automatic Disk Swap detection for valid multi-disk releases whose disk
  marker is followed by a shared edition or release suffix, such as
  `ThePhoenixCode-Disk1-BZ.D64` / `ThePhoenixCode-Disk2-BZ.D64`.
- The trailing suffix is part of the strict family signature, so different
  editions, languages or release groups are not mixed into the same swap set.
- Added regression coverage and updated Help, README and release metadata
  consistently to v1.6.5 / archive 54.

## 1.6.4 — conservative disk-swap matching

- Fixed automatic Disk Swap sets including every disk image from the mounted
  image's folder. Auto-build now requires the complete normalised title and a
  recognised disk suffix to match.
- Supports explicit families such as `disk 1`, `disc 2`, `side A`, `part 3`,
  `volume 2`, parenthesised forms such as `(Disk 1)`, `1 of 3`, and strict
  separator-delimited sequences such as `Scratch-1` / `Scratch-2`.
- Matching is extension-aware and naturally ordered. Unrecognised or ambiguous
  names safely remain a one-disk set; the manual Disk Swap Queue is unchanged.
- Updated inline guidance, Help, README, release metadata and regression tests
  consistently to v1.6.4 / archive 53.

## 1.6.3 — clearer UX, safer mount defaults and responsive SID playback

- Renamed the main **DISKS** navigation item to **STORAGE** and **JUKEBOX** to
  **SID JUKEBOX**, while retaining the familiar **FAVOURITES** name. Updated
  section headings, actions, Help and documentation to use consistent,
  task-focused terminology.
- Replaced panel-bound pseudo-tooltips with a single opaque, viewport-aware
  tooltip layer. Tooltips no longer inherit recording animations or become
  clipped by scrolling result panels. Keyboard focus and reduced-motion users
  are supported.
- Moved recording activity animation to a dedicated red dot so the Record
  button, elapsed time and tooltip remain steady while capture is active.
- Changed the safe default for existing disk images to **Unlinked**, preserving
  temporary drive writes without changing the original image. Existing
  configurations migrate once, while later user choices remain respected.
- Blank D64/D71/D81/DNP images created through u64deck are mounted immediately
  on drive A as **Read/write**. Reopening the image later treats it like any
  other existing image and uses the selected default mount mode.
- Mount actions now show their effective mode directly, such as
  **Mount & Run · UNLINKED**, with full explanations in the selector and Help.
- Fixed Random Dive path handling with case-insensitive SQLite scopes and an
  HVSC/Songlengths fallback that maps `/USB0/HVSC/...` correctly. Errors now
  distinguish unindexed folders, folders with no SID files and HVSC mapping
  failures.
- Made SID play queues lazy: folder and saved-playlist loading stores paths and
  fetches only the SID selected for playback. Browser requests are bounded,
  Jukebox polls cannot overlap and large play queues are not rebuilt on every
  status refresh, keeping the UI responsive while the Ultimate's native SID
  player is active.
- Updated code, startup banner, UI, Help, README, changelog and release metadata
  consistently to v1.6.3 / archive 52.

## 1.6.2 — desktop app launch and recording compatibility

- Added Edge app-mode startup with an isolated u64deck browser profile, plus
  system-browser and no-browser choices.
- Added MP4 capability detection where supported, automatic WebM fallback and
  WebM duration metadata repair for reliable seeking.
- Stabilised the width of live FPS, audio and recording indicators and expanded
  diagnostics with browser recording capability details.

## 1.6.1 — keyboard ordering and recording clarity

- Serialised keyboard requests and reduced batch delay to prevent reordered
  input during normal typing and disk-swap workflows.
- Returned focus to the streamed screen after disk changes and clarified the
  distinction between stream-frame handling and recording quality settings.

## 1.6.0 — internal favourites, safer disks, flexible recording and help

- Added favourites and recent-history support for individual files inside
  D64/D71/D81 images. PRG favourites retain the parent image and Commodore
  filename and can be relaunched through Mount+LOAD; other entry types reopen
  and highlight the parent image without altering it.
- Star toggles now update in place, preserving long search results, selection
  and scroll position instead of rebuilding the list from the top.
- Added a persistent default mount mode (read-only, read/write or unlinked),
  mode badges for drives mounted through u64deck, unlinked-eject warnings,
  image duplication, and a backup-then-mount-read/write workflow.
- Blank-image creation covers D64, D71, D81 and DNP, with clearer messages when
  a connected firmware does not expose a requested creator.
- Expanded browser recording to combined video/audio, video-only or audio-only,
  manual or fixed duration, bitrate presets, native or pixel-doubled video, a
  filename template, optional save-location picker and per-recording UDP drop
  counters. Stopped or disconnected video now shows a C64-style VIDEO NOT
  CONNECTED panel instead of leaving a frozen frame; the panel reflects whether
  audio remains active, and the audio badge exposes clear connection states plus
  its live chunks-per-second rate.
- Added a sanitised diagnostics ZIP containing release/runtime details, device
  information, stream/index/cache statistics, parse failures and recent errors.
- Added contextual tooltips and a searchable full Help panel opened by a text
  **Help** button in the top bar. Help covers all major tabs, workflows, safety
  choices, examples and common troubleshooting scenarios.
- Updated code, startup banner, visible UI version, README, changelog and release
  artefact naming consistently to v1.6.0.

## 1.5.0 — favourites, recent items and WebM recording

- Added a dedicated **Favourites** tab with persistent starred items and an
  automatically maintained recent-items list. Ultimate folders, disk images,
  PRG/CRT/SID files, Assembly64 releases, SID composer folders/tunes and Quick
  Launch files can be reopened with one click.
- Added star controls to the storage browser, Assembly64 results, Jukebox
  browser/search/playlist and Quick Launch library. State is stored locally in
  `user_items.json`, which is excluded from release archives and source control.
- Added combined browser-side **video + audio recording** from the live mirror.
  Recording automatically starts both streams, shows elapsed time, warns before
  leaving the page and downloads a timestamped WebM file without FFmpeg.
- Removed the duplicate U64 Menu button from the Mirror panel; the global header
  button remains the single menu control.
- Persist disk-image parse failures in SQLite and expose their paths/reasons in
  Settings → Caches & Indexes as **images not parsed**. Originals are never
  modified and remain mountable/searchable as storage files.
- Reduced false verification changes by tolerating FAT two-second timestamp
  precision and whole-hour UTC/local-time offsets when path and size match.
- Updated code, startup banner, visible UI version, README, changelog and release
  artefact naming consistently to v1.5.0.

## 1.4.1 — menu-remote removal and maintenance cleanup

- Removed the experimental TCP/23 Ultimate-menu selector, terminal panel, API
  endpoints and Telnet/VT100 implementation after hardware testing confirmed
  that it controls a separate firmware display rather than the menu shown in
  the VIC mirror.
- Restored the SCREEN tab to one unambiguous keyboard path: the established C64
  port-64 keyboard-buffer input remains unchanged.
- Moved release metadata/build fingerprinting and resilient JSON persistence
  into focused modules, reducing server.py and eliminating duplicated
  maintenance concerns.
- Split the large inline web page into `index.html`, `app.css` and `app.js` while
  retaining the zero-build vanilla frontend; all static assets are served with
  `no-store` to prevent stale files after an upgrade.
- Build identifiers now hash every shipped top-level Python module
  automatically, so newly split modules are included without a manual list.
- Updated code, startup banner, visible UI version, README, changelog and release
  artefact naming consistently to v1.4.1.

## 1.4.0 — indexed Random Dive and experimental Ultimate menu remote

- Reworked **Random dive** to choose directly from the SQLite storage index.
  It starts one random SID immediately, then fills the containing folder in a
  background worker instead of wandering through FTP subfolders and sometimes
  reaching a dead end.
- Added a separate **Keyboard target** selector on the SCREEN tab. Existing C64
  keyboard input remains the default and continues to use port 64 unchanged.
- Added an experimental **Ultimate Menu** keyboard target over the firmware's
  TCP/23 Telnet interface, including cursor/Return/Space/Backspace/F1–F8 input,
  reconnect handling and a small VT100 terminal view.
- Added a lightweight VT100/ANSI screen parser and Telnet option negotiation;
  menu traffic is never duplicated to the C64 keyboard-buffer endpoint.
- Local USB imports are now recognised in the DISKS UI. A subsequent network
  crawl is labelled **Verify from Ultimate**, warns that it walks the whole
  subtree over FTP, keeps the completed local index searchable while running,
  and reports checked/unchanged/new/changed counts separately.
- Renamed index parse failures in the status line from “skipped” to the clearer
  “images not parsed”.
- Updated backend, startup banner, UI, README, changelog and release artefact
  naming consistently to v1.4.0.

## 1.3.0 — local USB index import

- Added a **Local USB index** workflow under Settings → Caches & Indexes.
  A USB stick can be removed from the Ultimate, attached to the u64deck PC and
  scanned directly into the same per-device SQLite database.
- Map a detected Windows drive or manually entered folder to an Ultimate path
  such as `/USB0`; stored paths remain identical to those used when the stick is
  returned to the machine.
- Parse D64/D71/D81 images locally, reuse matching SQLite image entries, batch
  database commits and report folders/files/images per second, ETA, bytes read
  and skipped filesystem errors.
- Local scans support Pause/Resume/Stop and are read-only: no source files are
  copied, renamed or modified.
- Completed imports reconcile and prune stale paths only after a successful
  full scan. Interrupted imports retain safely committed partial results without
  publishing a false completion marker.
- Record the last local source, Ultimate mapping, counts and completion time in
  the cache statistics panel.

## 1.2.2 — legacy FTP filename compatibility

- Fixed background indexing stopping when an Ultimate storage filename contains
  a legacy 8-bit byte that is not valid UTF-8 (including the observed `0xF8`).
- Retry the affected listing using a byte-preserving Latin-1 FTP control-channel
  encoding and retain that encoding for later browse/fetch operations so the
  filename remains addressable.
- Added regression coverage for automatic UTF-8 fallback and subsequent FTP
  operations using the selected legacy encoding.

## 1.2.1 — duplicate-safe SQLite cache updates

- Fixed `UNIQUE constraint failed: fs_entries.path` when Ultimate FTP listings
  or legacy JSON caches contain duplicate directory entries.
- Merge duplicate paths before writing a folder to SQLite and safely upsert a
  path if stale cache data previously associated it with another parent.
- Added regression tests covering live directory refresh and legacy migration
  with duplicate paths.

## 1.2.0 — coordinated SQLite indexer

- Replaced the large directory, disk-image and completion JSON caches with an
  incremental per-device SQLite database using WAL mode.
- Automatically import existing JSON caches on first use.
- Prioritise interactive Ultimate operations over status polling and background
  indexing, with automatic yielding around device work.
- Added index pause/resume, progress rates, ETA, root-scan confirmation and
  separate controls for clearing image and storage indexes.

## 1.1.6 — index-aware disk creation

- Pause background volume indexing while the Ultimate creates a blank disk,
  wait for any active FTP transfer to finish, then resume indexing automatically.
- Give disk-format operations a dedicated 30-second timeout instead of the
  normal eight-second REST timeout used for routine device calls.
- Show the index as paused in the UI and prevent duplicate Create clicks while
  the firmware is working.
- If the Ultimate still times out, advise refreshing the folder before retrying
  because the image may have been created even though the response was lost.

## 1.1.5 — blank disk creation guard

- Prevent blank disk creation at the Ultimate's virtual top-level `/`, which
  contains storage devices but is not itself a writable filesystem.
- Disable the New disk control at `/` and show a clear prompt to enter USB0,
  SD, Flash, Temp or another mounted storage folder first.
- Quote device paths safely and turn the firmware's misleading
  `PATH DOESN'T EXIST` response into an actionable client error.

## 1.1.4

- Fixed repeated `WinError 5` failures when Windows allowed JSON files to be
  written but temporarily denied atomic replacement.
- Serialised state/cache writes, retried Windows replacements, cleared a
  read-only target attribute where possible, and added a safe in-place fallback.
- Throttled recurring persistence warnings to prevent console flooding.

## 1.1.3 — transient status recovery and Mirror layout fix

- Retry idempotent Ultimate REST GET requests once over a fresh HTTP connection
  when the embedded web server closes an incomplete response body.
- Keep the last known device identity visible through the first failed status
  poll, show a brief **reconnecting…** state and retry after two seconds before
  declaring the device offline.
- Group the FPS and audio-rate badges into a non-breaking inline unit so the
  Auto F7 checkbox can no longer split or misalign them.

## 1.1.2 — optional Retro Replay F7 automation

- Added a persistent **Auto F7 Fastload** checkbox to the SCREEN/MIRROR panel.
- When enabled, u64deck presses F7 after Reset and Reboot actions initiated
  from the UI, selecting **INSTALL FASTLOAD** on the Retro Replay boot screen.
- The same option also enables the existing cartridge-menu pre-key handling for
  Mount+Run and Mount+LOAD workflows.
- Full reboots now discard the old command-socket connection and retry against
  the restarted port-64 service before sending F7.
- The option is disabled by default and is stored in `config.json` using the
  existing `boot_prekey` setting, preserving compatibility with earlier builds.

## 1.1.1 — optimisation and reliability pass

- Moved blocking upload/deploy device operations off FastAPI's event loop so
  video and audio WebSockets remain responsive during mounts, runs and boot waits.
- Kept audio latency bounded by dropping stale queued chunks instead of allowing
  a slow browser to accumulate roughly a second of delayed audio.
- Added bounded upload handling for mounts, runners, image inspection, swap
  sets, SID batches, the quick-launch library and Assembly64 downloads.
- Replaced direct JSON writes with atomic replacement for configuration,
  playlists and search/index caches; malformed JSON now falls back safely.
- Changed the disk-image cache to least-recently-used eviction and passed the
  filename hint into geometry detection for unusual but valid image sizes.
- Replaced list-front removal with deque traversal in recursive indexing/search.
- Verify a newly selected Ultimate before replacing the current working device.
- Close REST, command-socket, UDP and FTP resources more cleanly.
- Harden download filenames and add basic browser security headers.
- Escaped device metadata and inline-action values in the UI so unusual device
  names or storage filenames cannot break the generated markup.
- Added T64 and DNP quick-launch support where the backend already knew how to
  handle those formats.
- Added automated tests and made the Windows release workflow run them before
  building the executable.
- Added upper dependency bounds to avoid accidental major-version breakage.

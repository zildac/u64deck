# Changelog

## 1.9.0 — Release Candidate 48

**Public soak candidate:** `c0d1fb0`

- Fixes the Legacy Auto F7 usability trap found during RC47 soak testing. The Auto F7 checkbox remains selectable on Legacy KERNAL-buffer connections instead of being disabled.
- Defaults Auto F7 to enabled for new installations and older configurations where `boot_prekey` is absent, while preserving an explicit user-disabled empty value.
- On Legacy input, enabling Auto F7 stores the preference and enables physical-F7 guidance only. u64deck still never injects F7 through the Legacy keyboard buffer.
- Keeps CIA1 automatic matrix F7 behaviour unchanged for confirmed Retro Replay cartridges.
- Carries RC47 Assembly64 client identification/support attribution and the RC46 SIDFlow recommendation diversification unchanged. No unrelated backend, device-control, discovery, streaming, indexing, modal or packaging workflow behaviour is intentionally changed.

## 1.9.0 — Release Candidate 47

**Public soak candidate:** `45fbea8`

- Registers every Assembly64 search, preset, entry, binary-download and raw-debug request with the activated dedicated `Client-Id: u64deck` application identifier.
- Removes the obsolete configurable Assembly64 client-ID override and cleans it from existing configuration on startup; the service base URL remains configurable.
- Adds a compact, on-brand Support Assembly64 panel with direct Ko-fi and PayPal donation links using safe external-link attributes.
- Adds matching Assembly64 attribution and support information to in-app Help and README without implying endorsement, licensing or mandatory donation.
- Carries the hardware-tested RC46 SIDFlow recommendation diversification unchanged. No discovery, device-control, Mount & Run, streaming, indexing, SIDFlow data import, confirmation-modal or Exit-security behaviour is intentionally changed.

## 1.9.0 — Release Candidate 46

**Private hardware-test build:** `e873eaa`

- Diversifies SIDFlow **More like this** and Radio results at tune/file level instead of presenting every distinct SIDFlow track ID as an independent visible recommendation.
- Keeps the highest-ranked representative from each SID file first and suppresses display-equivalent copies found at different paths.
- Defers additional song indices from an already selected multi-subtune SID until distinct files cannot fill the requested queue.
- Labels deliberately retained sibling subtunes as `song X/Y` (or `song X` when the indexed total is unavailable) so fallback entries remain distinguishable.
- Requests a wider ordered SIDFlow candidate set before diversification, allowing a normal 20-tune queue to remain full when repeated variants occupy the highest raw ranks.
- Adds Diagnostics counts for mapped candidates, distinct primary tunes, deferred subtunes, duplicate track IDs and equivalent tune rows suppressed.
- Adds regression coverage for exact duplicate track IDs, multiple subtunes from one SID, equivalent metadata at different paths, fallback song labels and oversampled queue filling.
- Carries the complete RC45 custom confirmation-modal implementation unchanged. No discovery, device-control, Mount & Run, streaming, indexing, SIDFlow data import or Exit-security behaviour is intentionally changed.

## 1.9.0 — Release Candidate 45

**Public release-candidate build:** `6feaede`

- Replaces browser-native confirmation popups with one C64 boot-screen-themed modal shared across Windows Edge and other supported Chromium-family browsers.
- Uses the established C64 screen blue, light-blue inset frame and severity accents within the existing palette: blue for normal confirmations, C64 yellow for important actions and muted C64 red only for destructive actions.
- Preserves the existing confirmation wording and Confirm/Cancel outcomes, with keyboard support for Enter, Escape and trapped Tab focus.
- Leaves red/green toast notifications unchanged.
- Leaves backend confirmation requirements, local-only Exit enforcement and all Ultimate/device operations unchanged from RC44.
- Published as a soak candidate alongside RC44. It does not replace RC44 as the established baseline and is not the final v1.9.0 release.

## 1.9.0 — Release Candidate 44

**Public release-candidate build:** `fc1e0fb`

- Corrects IPv4-mapped IPv6 loopback handling for `/api/app/exit` by unmapping the embedded IPv4 address before applying the loopback test. This is portable across the Python 3.12 Windows build runner and later Python versions.
- Sets `proxy_headers=False` explicitly in the managed Uvicorn configuration so forwarded headers cannot influence the client address used by local-only Exit enforcement.
- Serialises the Exit request check-and-set with a module-level lock so concurrent confirmed requests schedule shutdown exactly once.
- Hides **Exit u64deck** in the initial HTML and reveals it only after `/api/app_config` confirms the control is locally available.
- Retains the GitHub executable smoke check requiring an unconfirmed Exit request to return HTTP 403.
- Supersedes the first public RC44 package (`490d847`). That package contained the mapped-loopback test without the corresponding Python 3.12-compatible implementation, so its reported all-green validation was not valid for the target Windows CI environment.
- Applies a presentation-only sentence-case audit across tooltips, helper text, status copy, dropdown labels and user-facing notifications. Technical names and abbreviations such as WebSocket, SIDFlow, CIA1, REST, CPU, RAM, D64/D71/D81 and HVSC retain their established casing.
- Corrects the live audio-rate tooltip to **Audio WebSocket chunks received per second** and capitalises the remaining literal tooltip text consistently throughout Screen, Storage, Settings, Assembly64 and SID Jukebox.
- Aligns dynamically generated tooltips and concise status/toast messages with the same sentence-case rule, including recording, indexing, disk-image actions, queue controls and Health history labels.
- No input, Mount & Run, Retro Replay, Reset/Reboot, readiness polling, streaming, recording, SID/Jukebox, discovery, routing, firmware-settings, SQLite or device-control behaviour changed from RC43.
- Public promotion packaging now writes the exact source build ID into the PyInstaller bundle. Frozen `release.py` reads that stamp instead of recomputing from a bundle without top-level Python sources.
- The Windows workflow records the documented source identity, verifies the generated stamp, checks the frozen API and startup banner against it, and exercises the rejected HTTP 403 Exit path before the confirmed graceful Exit smoke.
- Adds `.gitattributes` LF rules for Python, JavaScript, HTML and CSS so the source fingerprint remains stable on the Windows CI runner.
- Restores the explicit Legacy/Retro Replay best-effort carry in README and Help: tested C64U + Retro Replay only, allow approximately 3–5 seconds between Reset/Reboot and Mount & Run, other freezer/fastload cartridges not comprehensively tested, and no occurrence observed on CIA1.

## 1.9.0 — Release Candidate 43

**Local release-candidate build:** `4f7723b`

- Fixes the Auto F7 tooltip on confirmed Legacy devices whose input-capability response omits `pending` or uses non-boolean values. The header, input badge, effective Auto F7 state, tooltip, Legacy F7 suppression and cartridge guidance now use one shared input-mode classifier.
- Adds regression coverage for Legacy responses with `available=false`, `available=0` and omitted `pending`, plus normal CIA1 and capability-pending responses.
- This is a frontend-only input-mode consistency correction. Standalone Reset/Reboot readiness monitoring, Mount & Run, CIA1 matrix F7, Legacy LOAD/RUN delivery and cartridge handling are unchanged.

## 1.9.0 — Release Candidate 42

**Local release-candidate build:** `26d60ed`

- Makes the standalone Legacy + Retro Replay Reset/Reboot guidance self-clearing. The informational overlay still does not detect the physical F7 keypress or hold the coordinator; instead, the browser requests one short `$00CC` BASIC-readiness sample at a time and requires two consecutive ready readings.
- Each standalone readiness sample uses status priority, expires quickly rather than waiting behind another device operation, and releases the coordinator immediately. When Fastload/BASIC is detected, the overlay briefly shows **Fastload detected — ready** and then disappears. Firmware without `machine:readmem` retains the existing **Dismiss**-only behaviour.
- Fixes the Auto F7 tooltip mode selection when Legacy capability is represented by a falsey non-boolean value. Detected Legacy devices now reliably show the Legacy/Freeze Menu explanation; CIA1 devices retain the automatic-F7 wording.
- Updates README and in-app Help. Mount & Run readiness, CIA1 Auto F7, Legacy LOAD/RUN delivery, cartridge preflight and all unrelated RC41 behaviour remain unchanged.

## 1.9.0 — Release Candidate 41

**Local release-candidate build:** `3196caf`

- Recognises the Ultimate firmware cartridge identifiers `rr38pal` and `rr38ntsc` as Retro Replay rather than treating the supported cartridge as unknown.
- Uses distinct cartridge-startup wording for CIA1 and Legacy input. Legacy prompts explain that only automatic F7 is unavailable and require the physical C64 keyboard; CIA1 prompts never mention the Legacy Freeze Menu limitation.
- Adds a non-blocking **Physical F7 required** Screen-Mirror overlay after standalone Reset/Reboot when Legacy input, Retro Replay and the saved Auto F7 preference are active. The card has **Dismiss** and performs no key polling, coordinator hold or timeout.
- Keeps the existing active Mount & Run overlay separate: it still waits for BASIC readiness, then continues with LOAD and RUN automatically.
- Expands the Auto F7 tooltip to explain the detected-mode behaviour and retained preference. Informational overlays are cleared by another Reset/Reboot, Mount & Run, device change, CIA1 detection or a temporary CRT launch.
- Updates README and in-app Help. CIA1 F7 automation, Legacy LOAD/RUN delivery, readiness gates, cartridge preflight, temporary-CRT normalisation and all unrelated RC40 behaviour remain unchanged.

## 1.9.0 — Release Candidate 40

**Local release-candidate build:** `0d58cba`

- Makes Auto F7 cartridge-aware. Before Mount & Run, u64deck reads the current firmware **C64 and Cartridge Settings → Cartridge** value and records its classification and source in Diagnostics.
- With no configured cartridge, Mount & Run follows the normal BASIC readiness, LOAD and RUN path with no F7 action and no Screen-Mirror prompt.
- Limits automatic fastload-menu handling to the hardware-tested **Retro Replay** flow: CIA1 retains automatic matrix F7, while Legacy retains the physical-F7 overlay and never receives buffer-injected F7.
- Uses a generic **Cartridge startup requires attention** overlay for other configured cartridges and sends no guessed function key.
- Maintains a per-device cartridge cache for UI/Diagnostics, refreshing it on device connection, firmware Cartridge changes, Load from Flash, Factory Defaults and Reboot. Mount & Run still performs a live preflight; only a recent same-device cache may be used as a short fallback, otherwise the operation stops before reset.
- Tracks CRT images launched directly through u64deck as temporary runner-cartridge state. The next Mount & Run performs one full Ultimate reboot to clear that temporary CRT, restores the firmware-configured cartridge, and then performs the live cartridge preflight.
- Updates README and in-app Help to state that Legacy fastload handling is currently supported for Retro Replay. Existing explicitly configured non-F7 advanced boot keys remain unchanged. CIA1 input, Legacy LOAD/RUN delivery, readiness gates, SID playback, streaming, recording, discovery, routing and SQLite behaviour are unchanged.

## 1.9.0 — Release Candidate 39

**Local release-candidate build:** `0407287`

- Replaces unreliable Retro Replay F7 injection on detected Legacy KERNAL-buffer devices with a physical-key handoff. Hardware testing showed that injected PETSCII code 136 can enter the cartridge Freeze Menu while the physical C64 F7 key remains reliable.
- Keeps the saved **Auto F7 Fastload** preference intact but disables it as an effective capability while Legacy input is active. CIA1-capable devices retain the existing fully automatic matrix F7 path unchanged.
- During Legacy Mount & Run, overlays a compact persistent **Physical F7 required** card on the live Screen Mirror, continues automatically once BASIC readiness is detected, and provides **Cancel Mount & Run**. Firmware without `machine:readmem` receives an explicit **Continue** confirmation after BASIC READY.
- Suppresses remote Screen-Mirror F7 on Legacy input and suppresses automatic Legacy F7 after u64deck Reset/Reboot actions. Other Legacy keys and the established Legacy LOAD/RUN command-buffer delivery remain unchanged.
- Extends browser-side Mount & Run request deadlines so the persistent prompt and genuine long disk loads are not abandoned by the normal short device-request timeout. Local busy/status polling remains available throughout.
- Updates README and in-app Help to describe the hardware-tested split accurately. No CIA1 keyboard, LOAD/RUN readiness gate, SID, streaming, recording, discovery, routing, SQLite or firmware-settings behaviour changed.

## 1.9.0 — Release Candidate 38

**Local release-candidate build:** `39b03b5`

- Displays the automatic Screen & Recording network-interface choice as **AUTO**, including the resolved interface address, to match the existing recording Format enum presentation. The stored interface value remains unchanged.
- Retains RC37's verified Assembly64 first-mount multi-disk handling, scalable disk-image naming controls, Play Queue scrolling and Health presentation. No streaming route, recording, input, Mount & Run, SID, discovery or device-control behaviour changed.

## 1.9.0 — Release Candidate 37

**Local release-candidate build:** `01f377b`

- Capitalises the primary connection states as **Connecting…** and **Reconnecting…**.
- Fixes Assembly64 multi-disk detection on the first clean mount. The release-file manifest is matched before deployment, only the confident disk family is downloaded, Disk Swap is armed in memory, and the disk selected by the user is mounted or started. Single and ambiguous releases remain unchanged.
- Replaces **Approve all shown** with **Approve all ambiguous (count)** so large disk-image catalogues can approve every current ambiguous set rather than only representative examples. Large approvals receive an extra confirmation.
- Renames **Copy Report** to **Copy Analysis Report** and pages approved exact sets 50 at a time so collections with hundreds or thousands of approvals do not create an impractically large panel.
- Retains RC36's verified Play Queue scrolling and Health presentation. No Legacy/CIA1 input, Mount & Run command delivery, SID playback, fade, discovery, routing or firmware-settings behaviour changed.

## 1.9.0 — Release Candidate 36

**Local release-candidate build:** `76f90cf`

- Corrects the Play Queue current-row reveal calculation to use the row and scroll-container bounding rectangles, so automatic reveal and **◎ Current** position the actual playing row instead of jumping roughly ten rows ahead.
- Strengthens the Health dashboard hierarchy by making panel headings larger than live status text, slightly enlarging metric labels and badges, and adding a little more row spacing.
- Standardises visible Health status casing, including **Online**, **Both Receiving**, **Idle**, **Indexer Idle**, **Running**, **Ready**, **Not Requested** and **Auto-updated**.
- No playback, queue order, SIDFlow, recording, Health calculations, polling, Diagnostics or device-control behaviour changed.

## 1.9.0 — Release Candidate 35

**Local release-candidate build:** `8bc12c1`

- Refines the primary header presentation: vertically centres the decorative stars and standardises device labels as **FW**, **Core** and **Input: CIA1/Legacy KERNAL buffer**.
- Moves the occasional-use Ultimate firmware configuration into a collapsed section below **Search Index & Cache**, keeping index statistics and SIDFlow controls visible without scrolling while firmware settings continue to load normally in the background.
- Improves SID Jukebox readability by enlarging the Songlengths/SIDFlow information line, adding thousands separators and capitalising visible recording option labels without changing their stored values.
- Rebalances Play Queue columns so Title and Author stay visually associated, adds full-name hover text, and replaces redundant SIDFlow provenance with **More like &lt;seed tune&gt;** or **SIDFlow Radio**.
- Makes the playing queue row unmistakable with a persistent ▶ marker, stronger row highlight and wider left accent distinct from hover. Playback changes reveal the row once, and **◎ Current** jumps back to it without continuous forced scrolling.
- Makes no recommendation-ranking, queue-ordering, fade, recording, Settings API, SQLite, discovery, routing, Mount & Run, input or machine-control changes from RC34.

## 1.9.0 — Release Candidate 34

**Local release-candidate build:** `95872f5`

- Enlarges the primary header identity and connected-device status so the u64deck version, release/build, Ultimate model, firmware, core, hostname, active interface and input method are easier to read at a glance.
- Uses the same larger status styling for connecting, reconnecting and busy messages.
- Reduces the secondary flashing `READY.` line slightly so it no longer visually outranks the primary status information.
- Makes no queue, SIDFlow, SID-index, fade, storage-index, discovery, routing, Settings, Mount & Run, input or machine-control changes from RC33. The earlier SIDFlow failure was confirmed to be caused by copying an unpopulated SQLite index rather than by an upgrade or database migration.

## 1.9.0 — Release Candidate 33

**Local release-candidate build:** `550c763`

- Fixes SIDFlow local-presence filtering when the SID metadata catalogue is only partially populated. u64deck now unions metadata-backed paths with every indexed `.sid` file beneath the configured HVSC root instead of treating any non-empty metadata table as complete.
- Prevents playing or indexing one SID from making **More like this** or Radio incorrectly report that no matching tunes are present while the full collection still exists in the storage index.
- Removes the old SQLite file-stat presence cache from the recommendation path because WAL-backed index changes can be live without changing the main database file timestamp.
- Adds SIDFlow candidate Diagnostics with metadata-path, file-index, combined mapped, played/recent, queue, excluded, ranked and final result counts.
- Leaves the hardware-verified RC31 fade handoff and the working RC32 queue auto-advance, Currently Playing presentation and Year filter unchanged.

## 1.9.0 — Release Candidate 32

**Local release-candidate build:** `29bdf7b`

- Separates active SID playback generation from Play Queue revision. Adding individual SIDs, inserting **More like this** recommendations, or removing a non-current queue entry no longer invalidates the current tune's auto-advance timer; the queue continues in order regardless of how future entries were added.
- Keeps current-entry removal, direct playback replacement, Stop, Reset, Reboot and other machine takeovers on the existing generation-invalidating path.
- Enlarges the Currently Playing title, chip badge, subtune and duration presentation for improved readability.
- Clarifies the small ♪ queue-row action: it uses that row as the SIDFlow similarity seed and inserts matches after the currently playing tune, while the larger **More like this** action uses Now Playing.
- Adds an exact four-digit **Year** filter to SID search. It combines with text, Chip and Format and matches a standalone 1900–2099 year extracted from indexed SID release metadata.
- Leaves the RC31 streamed fade handoff, live PCM audio quality, native Screen Mirror stream, disk-image analysis, Finder, routing, Mount & Run and Legacy/Retro Replay safeguards unchanged.

## 1.9.0 — Release Candidate 31

**Local release-candidate build:** `d36c50e`

- Fixes the streamed SID fade transition so browser gain remains locked at zero after the fade completes. u64deck now waits for a new backend playback generation, clears any buffered tail from the previous SID, and only then restores full gain for the replacement tune.
- While streamed fade is enabled for a matched Songlengths tune, the fade duration replaces the separate `sid_jukebox_end_grace_secs` allowance rather than being combined with it. Unknown-length fallback timing remains unchanged.
- Manual Next, Previous, direct queue/subtune selection and one-tune replacement mute and clear the outgoing browser audio while the replacement starts; a failed replacement restores normal gain.
- Adds a **＋** action beside SID entries in Favourites and Recently Used so an individual favourite SID can be appended to the current play queue without interrupting playback. The existing **Play** action still creates and starts a one-tune queue.
- Leaves the uncompressed live audio mirror, fixed native 384×272 screen-mirror stream, RC29 disk-image analysis, Finder, routing, Mount & Run and Legacy/Retro Replay safeguards unchanged.

## 1.9.0 — Release Candidate 30

**Local release-candidate build:** `e074d2d`

- Adds an optional **Fade streamed SID ending** control to the SID Jukebox, enabled by default at 2.5 seconds and adjustable from 1 to 5 seconds in the UI.
- For matched Songlengths tunes, starts a linear browser gain fade at the documented endpoint, extends only the selected subtune's compact `.ssl` duration by the fade allowance, and waits for the fade to complete before auto-advance. Unknown-length fallback tunes retain their existing timing and do not fade.
- Applies the fade to audio heard through the u64deck browser and to browser recordings. The UI and Help explicitly state that Ultimate HDMI and analogue output do not fade; they remain at full volume until the extended native endpoint.
- Uses the existing Jukebox generation/state model to cancel stale fades on Next, Previous, direct selection, Stop and replacement playback. Reloading or starting browser audio during a tune reconstructs the remaining fade from backend monotonic timing.
- Keeps RC29 disk-image analysis, batch approvals, local-rule reset, Finder, routing, Mount & Run, Legacy/Retro Replay safeguards, SIDFlow and Settings readiness behaviour unchanged.

## 1.9.0 — Release Candidate 29

**Local release-candidate build:** `3f024f1`

- Adds checkbox-based **Approve selected**, **Approve all shown** and per-row **Approve folder** actions for ambiguous disk-image naming results. Batch approval always creates exact ordered set overrides; it never creates a reusable filename pattern.
- Shows a confirmation summary with the affected set, file and folder counts before any ambiguous batch approval is written. Protected/rejected naming families remain excluded from batch approval.
- Adds bulk enable, disable and remove controls for approved exact sets.
- Adds **Remove all local approvals**, protected by two confirmations, to remove every user-approved reusable rule and exact-set override in one rollback-safe SQLite transaction. Built-in grouping, files and index entries are not changed.
- Makes the optional behaviour explicit: users who never run the analyser receive only the built-in matcher; analysis itself remains read-only, while previously approved local rules continue to apply automatically.
- No Finder, routing, Settings readiness, SIDFlow, Jukebox timing, streaming, Mount & Run, stale-operation protection or Legacy/Retro Replay workaround behaviour changed from RC28.

## 1.9.0 — Release Candidate 28

**Local release-candidate build:** `f6f6794`

- Adds case-insensitive automatic grouping for terminal hyphen-delimited disk pairs such as `the-hat-7a825b1-a.d64` / `the-hat-7a825b1-b.d64`, while retaining the unsuffixed-sibling veto and all established ambiguity safeguards.
- Adds **Settings → Search Index & Cache → Analyse Disk-Image Names**, a read-only analysis of D64, D71, D81 and G64 filenames already present in the SQLite index. It reports recognised sets, high-confidence unrecognised patterns, ambiguous candidates and protected/rejected naming families with counts, examples and a copyable text report.
- Allows a high-confidence analyser result to be approved only after explicit preview and confirmation, either globally or for one folder. Approved reusable rules use a constrained terminal/delimiter/marker/extension/scope model; arbitrary regular expressions are not accepted.
- Adds confirmed exact ordered set overrides for one-off naming conventions, plus enable, disable and remove controls for both reusable rules and exact sets. Local approvals persist across index refreshes, index rebuilds and Clear Storage Index, and apply immediately without renaming or modifying any files.
- Keeps GoodTools `[a]` / `[b]`, matching unsuffixed siblings, glued sequel-like numbering and other ambiguous families protected from reusable automatic approval.
- Adds SQLite schema v5 tables for local disk-grouping rules and exact-set overrides, surfaces their counts in index statistics and records approval/state/removal operations in Diagnostics.
- No Finder, routing, Settings readiness, SIDFlow, Jukebox timing, streaming, Mount & Run command delivery, stale-operation protection or Legacy/Retro Replay workaround behaviour changed from RC27.

## 1.9.0 — Release Candidate 27

**Local final-candidate build:** `2fa9f8e`

- Gates the firmware Settings category reads on the normal successful Ultimate information check. A startup or Ctrl+F5 refresh now shows a neutral waiting/retrying state and automatically loads Settings when the connection is ready instead of surfacing an early connection refusal.
- Retries transient Settings category-list and category-detail reads within bounded limits. A persistent failure remains visible with a manual retry control; mutating Apply/Save/Load/Factory actions are not automatically replayed.
- Makes backend Settings diagnostics name the exact operation that failed, including category-list reads, category-detail reads, item writes, bulk apply and firmware actions.
- Documents the hardware-tested Legacy/Retro Replay timing limitation: rapid Reset, Reboot or Mount & Run transitions can intermittently enter the Freeze Menu; allow approximately 3–5 seconds between transitions. RC26 stale-operation protections remain enabled. Other freezer cartridges have not been comprehensively tested, and the issue was not observed with CIA1 matrix input.
- No Mount & Run sequence, SID Stop path, CIA1 input, Legacy key mapping, discovery, routing, streaming, SIDFlow, Jukebox timing, storage or index behaviour changed from RC26.

## 1.9.0 — Release Candidate 26

**Local hardware-test build:** `9ed2b85`

- Prevents manual Legacy keyboard requests from waiting behind a long device operation and arriving after the C64 or cartridge has changed state. Screen-mirror and type-line requests are rejected while Mount & Run owns the device and otherwise expire after two seconds without sending any byte.
- Prevents manual Reset and Reboot requests from being queued during Mount & Run. Duplicate Reset/Reboot requests are coalesced, and a request that cannot reach the device within two seconds expires with a clear message rather than executing later.
- Records expired coordinator operations separately from cancellations. Legacy delivery diagnostics now include the request origin, byte count, decimal codes and measured wait for boot prekeys, Mount & Run LOAD/RUN and manual Legacy input.
- Adds matching browser-side guards so Legacy screen-mirror keys, typed text and manual Reset/Reboot controls report that they were not queued while Mount & Run is active. Backend enforcement remains authoritative.
- Deliberately leaves CIA1 matrix input, held-key handling, `release_all`, Jukebox Stop's immediate/fallback reset paths, Mount & Run's inline reset, SID playback, streaming, discovery and SIDFlow behaviour unchanged.
- This is a local hardware-test build focused on reproducing the previously observed Retro Replay Fastload/Freeze interaction without stale keyboard or machine-control operations contaminating the sequence.

## 1.9.0 — Release Candidate 25

**Public build:** `bd0bbaf`

- Pins u64deck to the SIDFlow 0.8.0 data contract for HVSC 85 instead of following a mutable latest release. The installer now selects the tag-specific compressed full export and verifies the published manifest plus hard-pinned SHA-256 digests for both the 194 MB gzip asset and its byte-identical 982 MB SQLite payload.
- Adds streamed gzip decompression, explicit download/decompression/import progress and a preflight free-space check. The updater allows approximately 1.8 GiB for the compressed source, decompressed source and compact build to coexist, while preserving the previous working similarity database unless validation and promotion complete successfully.
- Validates the 58-dimensional weighted-cosine contract, published vector weights, HVSC 85 identity and populated neighbour graph before import. Settings, Health and Diagnostics now surface the SIDFlow release, HVSC version, feature schema, metric, dimensions, track count, neighbour count and recommendation engine.
- Uses SIDFlow's 2,196,700 precomputed weighted neighbour rows as the primary engine. **More like this** prefers different SID files before sibling subtunes; **Radio** excludes the current, recently played and already queued SID files to reduce repetitive multi-subtune runs.
- Keeps the existing local 48-dimensional scan only as a clearly labelled fallback when the fixed-depth SIDFlow graph is exhausted after local-library and session filtering. Diagnostics records `sidflow-neighbors` and `u64deck-fallback` counts for each recommendation batch.
- Marks older installed SIDFlow data as **Update required** and blocks recommendation use until 0.8.0 is installed. If the network is unavailable, the failed update leaves the older database untouched but still gated; ordinary SID browsing and playback continue while More like this and Radio remain unavailable.
- Hardware validation completed the real 194 MB download/import in about 48 seconds, produced a 269,389,824-byte (about 257 MiB) compact database, and confirmed two More like this batches used `sidflow-neighbors=20` with no fallback results.
- Credits Christian Gleissner and links the SIDFlow repository, 0.8.0 data release and u64deck migration guide in README and Help. No Finder, routing, streaming, Mount & Run, drive-status, Jukebox timing, playlist or machine-control behaviour changed from RC24.

## 1.9.0 — Release Candidate 24

- Changes the local-evaluation default for `sid_jukebox_end_grace_secs` from 3.0 to 0.5 seconds after hardware listening tests found the shorter allowance produced more natural transitions across several HVSC tunes. The value remains configurable and the Ultimate still receives the original documented Songlengths duration.
- Keeps the Play Queue panel and saved-play-queue controls visible when the active queue is empty, so an existing saved queue can be selected and loaded before playback. This covers fresh/direct Jukebox entry, the post-Clear Queue state, and the shared empty state used before Search, folder-browser, local-upload, Storage, Favourites or Recent Items entry has populated the queue.
- Adds a clear **No tunes queued** empty state. **Save** and **Clear Queue** are disabled until the active queue contains a tune, while **Delete** is enabled only when a saved queue is selected.
- No Finder, routing, streaming, Mount & Run, drive-status, Health, native SID duration or other device-control behaviour changed from RC23. This remains a local evaluation build while additional Ultimate hardware feedback is collected.

## 1.9.0 — Release Candidate 23

- Adds a configurable post-Songlengths Jukebox end grace so the next track does not replace a SID during its final audible tail or fade. The local-evaluation default is 3 seconds, replacing the previous fixed one-second allowance for matched Songlengths entries.
- Keeps the original HVSC duration in the generated `.ssl` attachment and native Ultimate display; only u64deck's own auto-advance deadline is delayed.
- Preserves the established one-second allowance for unknown-length tunes using `sid_default_secs`, avoiding an unrelated fallback-timing change.
- Records duration source, base song length, end grace and final auto-advance deadline in the existing SID Jukebox Play timing diagnostic.
- No Finder, routing, streaming, Mount & Run, drive-status, Health or device-control behaviour changed from RC22. This remains a local evaluation build.

## 1.9.0 — Release Candidate 22

- Orders cold-start status so the UI waits for a successful Ultimate information response before requesting mounted-drive state. Drive A/B now populate automatically without opening Storage.
- Shows a neutral **Waiting for the Ultimate connection…** state during startup and device handover instead of surfacing a refused connection as a drive fault.
- Retries transient drive-status connection/lifecycle failures a bounded three times without adding Diagnostics errors; a persistent fourth failure becomes a genuine visible error and Health degradation.
- Resets the transient failure streak after a successful drive response and preserves manual Storage refresh as a recovery path.
- No Finder, routing, mounting, streaming, SID, Health telemetry cadence or other device behaviour changed from RC21. This remains a local evaluation build.

## 1.9.0 — Release Candidate 21

- Makes System Health status current-state driven: recovered diagnostic errors and old packet gaps remain visible but no longer keep the dashboard degraded.
- Adds an inspectable Recent warnings and errors history with active/recovered state and event age.
- Removes the redundant manual Health refresh button; the visible timestamp now states the automatic two-second cadence.
- Consolidates stream state into one card badge, removes duplicated idle badges and makes history status indicators compact and centred.

## 1.9.0 — Release Candidate 20

- Expanded the local **System Health** dashboard with REST success rate, p95
  latency, failure streaks and client-replacement counts; coordinator average,
  p95 and longest waits; priority totals, cancellations and recent operation
  history; stream gap events, longest gaps, restart/WebSocket lifecycle and
  browser render/audio queue telemetry; live task phases; index throughput and
  ETA; image/SID cache hit rates; config-save, route and shutdown history.
- Added a derived HEALTHY / DEGRADED / ATTENTION summary using cautious rules,
  while keeping all raw values visible. Healthy states use restrained green,
  transient activity and degradation use amber, and red is reserved for actual
  failures or stale required services.
- Kept the expanded view readable through grouped cards, compact badges and
  collapsible history panels rather than displaying every diagnostic counter at
  once.
- Added local browser telemetry reporting for rendered video FPS, WebSocket
  reconnects, Web Audio queue depth and underruns. The report is sent only to
  the local u64deck backend while Health is visible and never calls the Ultimate.
- Extended sanitised Diagnostics with the same operation, route, stream, cache,
  config and lifecycle histories. No telemetry database or additional recurring
  Ultimate REST polling was introduced.
- No Finder, routing, Mount & Run, SID playback, storage, settings or device
  control behaviour changed from RC19. This remains a local evaluation build.

## 1.9.0 — Release Candidate 19

- Added a local-only **System Health** dashboard showing cached Ultimate identity
  and REST latency, video/audio bitrate and packet health, u64deck process CPU,
  memory, threads and uptime, device-operation queue state, index size/counts and
  retained warning/error totals.
- Health refreshes every two seconds only while its tab is visible. It reads
  local counters and the existing 30-second status samples; it does not create a
  new recurring Ultimate REST poll. Index statistics are cached for 15 seconds.
- Added byte and last-packet counters to the UDP receivers and active-duration
  reporting to the device coordinator so stream and queue diagnostics are based
  on measured local activity.
- Added the complete health snapshot to sanitised Diagnostics exports, including
  an explicit note that current Ultimate firmware does not expose CPU load, FPGA
  utilisation or temperature telemetry through REST.
- No Finder, routing, Mount & Run, SID, storage, settings, machine-control or
  existing stream-control behaviour changed from RC18. This RC is intended for
  local evaluation before any publication decision.

## 1.9.0 — Release Candidate 18

- Fixed the confirmed Windows frozen-executable lifecycle issue where **Exit
  u64deck** closed the dedicated Edge app but could leave `u64deck.exe` and its
  console resident. The frozen build now waits for normal Uvicorn lifespan
  cleanup and then retires the process explicitly through a bounded watchdog.
- Added a canonical multi-resolution `u64deck.ico` and embedded it into the
  PyInstaller executable instead of building with `icon=None`.
- Updated `start.bat` so a normal source shutdown returns cleanly without an
  unconditional `Press any key to continue`; genuine non-zero exits remain
  visible for troubleshooting.
- Strengthened the Windows workflow to verify PE icon resources and exercise
  the normal dedicated Edge app path, confirming the HTTP exit response,
  Edge-profile process closure, EXE termination and zero exit status.
- Retained RC17's loopback/header protections, response-before-shutdown order
  and graceful resource cleanup. No Finder, routing, Mount & Run, SID,
  streaming, storage, settings or device-control behaviour changed.

## 1.9.0 — Release Candidate 17

- Added a confirmed **Exit u64deck** action in the main header. It stops the
  managed Uvicorn server, runs the existing graceful cleanup path and leaves
  the connected Ultimate running.
- Restricted the exit endpoint to loopback clients and a same-origin custom
  request header, so another device on the LAN or a simple cross-site form
  cannot stop u64deck. The action is hidden when the UI is opened remotely.
- Retained the Edge app process launched by u64deck and closes that dedicated
  window after the server response and cleanup. System/default browser windows
  are never terminated; they show a final stopped page instead.
- Carried the canonical dual-interface best-effort README warnings verbatim
  from the published documentation update, without changing routing behaviour,
  screenshots or gallery ordering.
- No discovery, connection, Mount & Run, SID, streaming, storage, settings or
  device-control behaviour changed from RC16.


## 1.9.0 — Release Candidate 16

- Removed the separate blocking remembered-address phase from Finder. Configured
  and historical candidates are now placed at the front of the same 64-worker
  direct `/v1/info` pass as the local `/24`, so stale `config.json` entries can
  no longer delay the fresh subnet scan. Each address is still requested once,
  with the proven split connect/response deadlines and no retry storm.
- Added per-candidate discovery diagnostics for persisted addresses, including
  their outcome and elapsed time, while retaining verified-only display, device
  grouping, DHCP replacement and historical-address safeguards. Opening Finder
  now also pauses routine Info and Mounted Drives polling immediately.
- Made SID Stop flush all browser-scheduled Web Audio immediately and capped the
  amount of audio that can be queued ahead, preventing stale sound from
  continuing while the fast backend reset completes.
- Changed Diagnostics export to use an explicit save, write and close sequence
  through the browser File System Access API where available, with a clean
  abort/close failure path and download fallback for other browsers.
- Added immediate **Connecting…** feedback and backend per-stage timings for
  persisted-route lookup, client creation, Finder-result reuse/live verification,
  coordinator wait, capability handling, backend replacement and cleanup.
- Retained RC15's full CIA1 Mount & Run delivery, Legacy fallback, split routing,
  cartridge-safe SID recovery, streaming cleanup and disk-swap behaviour.

## 1.9.0 — Release Candidate 15

- Replaced the mixed Mount & Run input path on CIA1-capable Ultimate 64
  firmware. The complete `LOAD"*",8,1` line is now sent as one ordered CIA1
  matrix event batch, followed by matrix `RUN` after the existing readiness
  gate. This removes the port-64 eight-byte boundary that repeatedly left only
  `8,1` on screen during immediate consecutive disk launches.
- Kept the Legacy-only C64 Ultimate path unchanged: LOAD and RUN continue to
  use the established one-shot KERNAL keyboard buffer delivery which did not
  reproduce the failure in hardware testing.
- Removed browser-side matrix-release requests from both audio and video
  WebSocket close handlers. Audio disconnects no longer touch keyboard state;
  video disconnects clear local browser state while the backend performs one
  coalesced hardware safety release outside the asyncio event loop.
- Removed matrix release from generic stream stop control, preventing explicit
  stream stop plus WebSocket close from producing duplicate input cleanup
  requests and avoidable timeout warnings.
- Retained RC14 input serialisation and caller-labelled diagnostics, plus RC13
  split routing, cartridge-safe SID Stop, post-SID recovery reboot, disk swap
  and the frozen Finder implementation.

## 1.9.0 — Release Candidate 14

- Fixed an intermittent Mount & Run failure on CIA1-capable Ultimate 64
  firmware where the first eight-byte `LOAD"*",` command-buffer chunk could be
  replaced before the C64 consumed it, leaving only `8,1` on screen. On
  firmware with `machine:readmem`, u64deck now verifies the KERNAL keyboard
  buffer count at `$C6`, sends the LOAD line in bounded chunks and waits for
  each chunk to drain before continuing.
- Preserved the established one-shot Legacy KERNAL-buffer delivery on older
  C64 Ultimate firmware where `machine:readmem` is unavailable. The hybrid
  post-load RUN dispatcher remains unchanged: CIA1 matrix on supported U64
  firmware and Legacy buffer delivery elsewhere.
- Serialised CIA1 matrix actions and legacy keyboard-buffer writes through one
  input lock so browser input, Auto-F7, Mount & Run and cleanup requests cannot
  cross one another.
- Removed unrelated matrix cleanup from the audio WebSocket. Video-disconnect
  cleanup is now coalesced and executed in a worker thread, preventing an
  Ultimate input timeout from blocking the asyncio server and making the whole
  UI appear unresponsive.
- Matrix-release warnings now identify their caller. Mount & Run aborts before
  mounting or typing when a known CIA1 session cannot complete its initial
  safety release, rather than continuing with uncertain held-key state.
- Retained RC13 split routing, cartridge-safe SID Stop, post-SID recovery
  reboot, disk-swap behaviour and the frozen Finder implementation.

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

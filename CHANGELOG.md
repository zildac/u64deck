# Changelog

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

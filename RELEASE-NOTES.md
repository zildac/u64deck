# u64deck v1.9.0 — Windows Final

**Build:** `579a5f8`

u64deck v1.9.0 Final is the Windows release of the hardware-tested RC51 application code. The application logic is unchanged apart from the release identity.

## What is included

- The redesigned u64deck interface and C64-styled confirmation modals.
- Progressive Ultimate Finder behaviour that displays verified devices as they are found.
- Corrected SID Jukebox fade timing, source-agnostic queue advancement and diversified SIDFlow recommendations.
- Drain-confirmed Legacy Mount & Run command delivery on firmware that supports memory reads, with the conservative compatibility fallback retained elsewhere.
- The registered `u64deck` Assembly64 client identity, service attribution and support links.
- The canonical empty `library/` directory containing only `library/README.txt`.
- The compact Windows application icon with verified 16, 24, 32, 48, 64, 128 and 256 pixel frames.
- PyInstaller packaging checks for the source build stamp, PE VERSIONINFO, icon resources, library contents and the final Windows ZIP.

## About this release

v1.9.0 Final is RC51's application code, unchanged apart from the version identity.

RC49 and RC50 were private builds carrying the UI redesign, the progressive Ultimate Finder and the SID Jukebox corrections. That work had already been exercised on the maintainer's available Windows and Linux systems and Ultimate hardware before RC51 was produced.

RC51 itself changed only packaging and identity: the compact application icon, the restored `library/` scaffold and the release string. There was therefore very little new application behaviour left to shake out, which is why Final followed RC51 within a day.

Testing covers the hardware and configurations available to the maintainer, including an Ultimate 64 on firmware 3.15 and a C64 Ultimate on firmware 1.1.0s2p8 / core 1.49, using Windows and Ubuntu. Within that scope the release is stable in isolation. It cannot represent every firmware revision, cartridge, network arrangement or disk image in use.

An unexpected problem is therefore more likely to be an untested combination than something overlooked. Useful issue reports should include the u64deck build ID, Ultimate model and firmware, input method, cartridge configuration, the action being performed and a Diagnostics export where available. Those reports are what close the remaining coverage gap.

## Windows installation

1. Download `u64deck-v1.9.0-windows.zip` from the release.
2. Extract it into a new, empty folder. Do not overwrite an older installation.
3. Copy only the persistent files you need from the old folder after the old u64deck process has stopped: `config.json`, `user_items.json`, `playlists.json`, `.u64deck-index.sqlite3` and `.sidflow-similarity.sqlite`.
4. Do not copy `*-wal`, `*-shm`, caches, bytecode, old source/static files or the previous executable.
5. Run `u64deck.exe`. Use **Exit u64deck** to close the dedicated app window and stop the local server cleanly.

The archive includes the empty `library/` scaffold. Users may place their own supported Quick Launch files there.

## Linux position

v1.9.0 Final is a Windows release. Linux remains at Preview 9 and is not rebuilt, retagged or re-identified for this release.

The Linux application itself runs well. The remaining gap is the upgrade experience: `update-linux.sh` does not yet handle every update cleanly and the process is still partly manual. Linux will reach Final once that packaging and upgrade path is sorted. Preview 9 remains available in the meantime, and its stated RC51 base lineage remains accurate because Final uses the same application code.

## Known limitations

### Dual-interface Ultimates

Running Ethernet and Wi-Fi together remains best-effort. On the tested hardware, enabling both interfaces can make wired REST responses intermittently slow even when u64deck's split routing avoids the worst cases. A single active interface, preferably Ethernet, remains the reliable setup.

### Legacy input and freezer cartridges

On devices using the Legacy KERNAL keyboard-buffer path, rapid Reset, Reboot or Mount & Run transitions with a freezer or fastloader cartridge can enter the cartridge Freeze Menu instead of the expected startup path. On the tested C64 Ultimate with Retro Replay, u64deck suppresses unreliable injected F7 and requests the physical C64 F7 key. Allow approximately 3–5 seconds between Reset or Reboot actions and before Mount & Run. This behaviour was not observed on the CIA1 matrix-input path.

Other freezer and fastloader cartridges have not been comprehensively tested.

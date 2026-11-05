# Changelog

All notable changes to Idus SC Toolbox are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · Versioning: `vMAJOR.MINOR`, plus `.PATCH` for look-and-fix releases.

## [1.3.3] - 2026-11-05

### Fixed
- Status → **Wineserver now** showed "other Wine + other Wine" (warning) while Star Citizen ran
  fine. Wine starts its server through a path like `…/lib/wine/x86_64-unix/../../../bin/wineserver`,
  which didn't match the runner folder. Paths are now resolved first, and only servers for the
  Star Citizen prefix count. Other Wine apps (another prefix) are shown as a note, not a warning.
- Status → **Launcher log** kept warning about an old verify loop after it was fixed: the game
  files' timestamp doesn't always change when a patch finishes. Now the warning clears as soon as
  the game was patched *or played* (`Game.log`) after the error.

## [1.3.2] - 2026-11-05

### Fixed
- The RSI Launcher could open outside the visible screens: you heard it, but no window showed,
  whether it was started from the Toolbox or the desktop icon. "Fix windows now" brought it back.
  The Toolbox now does that automatically when it starts the launcher (▶ Launch, Restart, Patch /
  verify, `--launch`, start-menu actions): the launcher is centred on the main screen and brought
  to the front, and for 3 minutes it is pulled back if it lands off-screen. Moving it yourself
  to another screen is left alone. Starting from the plain desktop icon bypasses this — use
  "Fix windows now" there, or start via the Toolbox.

## [1.3.1] - 2026-11-05

### Fixed
- The RSI Launcher window could open *behind* the Toolbox (or other windows) when started with
  ▶ Launch, Restart launcher or Patch / verify. A launcher started from inside an app gets no
  KDE start token, so KWin's focus-stealing prevention keeps it in the background. The Toolbox
  now loads a short-lived KWin script that brings the launcher window to the front when it
  opens (unloaded again after 3 minutes). Starting from the desktop icon was not affected.

## [1.3] - 2026-11-04

### Added
- ▶ **Launch** button (top right). Checks RSI's public `latest.yml` for a newer RSI Launcher
  before starting. If one is out, it offers **🩹 Patch mode** (default), **Launch anyway** or
  **Cancel**, because the launcher updates itself on start. Offline, it starts normally.
- Launcher watch: while the launcher runs on the play runner, the Toolbox follows its log and
  offers patch mode if the launcher starts patching the game or loops with `did not converge`.
- **Status** panel with 8 tiles: play runner (+ locale / åäö input), patch runner, running
  wineserver, game version and build, RSI Launcher version vs. latest, last patch error in the
  launcher log, free disk space (with bar) and PipeWire quantum.
- 📄 **Launcher log (follow live)** in group 2.
- Live VRAM bar in group 6, "last backup" line in group 3, orange frame on the quantum button
  while 2048 is forced.
- CLI: `--launch` (newer launcher → patch mode, no prompt) and `--status`.
- Start-menu right-click actions: **▶ Launch** and **🩹 Patch / verify launcher**.

### Changed
- Two-column layout (groups 1–3 left, 4–7 and the log right); icon beside the title.
  The window is about 1290 × 1290 px instead of ~740 × 1600.
- Includes the 1.2.1 look (Saira title, orange group rings, status pill, fonts via the
  installer), which was not released on its own.

## [1.2.1] - 2026-11-04

### Changed
- 🎨 New look, following the Idus visual style:
  - Saira for the title, IBM Plex Sans for text, JetBrains Mono for the version line and the log.
  - Group headings get an orange number ring and a clear title. The backup path sits muted next to its title.
  - The SC status is a pill: green "● SC running" or muted "○ SC not running".
  - Log box, checkboxes and the environment menu use the same palette. Orange is kept for markers, links and focus.
- The installer downloads the three fonts to `~/.local/share/fonts/idus` when they are missing (no sudo,
  SIL Open Font License). Without them the app uses system fonts and says so in the log.

### Fixed
- Buttons and the environment menu stay dark on GTK themes that paint buttons with a background image
  (e.g. the default GTK theme), where they used to turn white.
- Footer links are orange as intended. GTK4 ignored the old `a` selector.

## [1.2] - 2026-11-04

### Added
- 🩹 **Patch / verify launcher** (group 1). Kills everything, then runs
  `sc-launch.sh patch`, which starts the RSI Launcher on a separate PATCH runner for game
  updates and Verify. Click **Restart launcher** afterwards to play. Useful when the runner
  that plays best can't patch (e.g. RSI Launcher 2.19.1 looping with
  `p4k verification did not converge`). Needs patch mode in `sc-launch.sh` (see README →
  *Patch mode (two runners)*). Without it, the button logs a message and starts nothing.
- CLI: `--restart` (Restart launcher) and `--restart-patch` (Patch / verify launcher).

### Fixed
- **Kill** now runs `wineserver -k` with *every* runner `sc-launch.sh` points at (play +
  patch). A wineserver can only stop a server of its own Wine version. Before this, a patch-mode
  `sc-launch.sh` made the toolbox fall back to the system `wineserver`, which couldn't stop
  the runner's server.
- Safety: **Clear shader cache → incl. GPU driver cache** never deletes the Wine prefix
  itself, a folder above it, or `$HOME`, even if `sc-launch.sh` points a driver-cache variable
  there with a literal path.

AI was used as part of the development.

## [1.1] - 2026-10-09

### Added
- 📊 **VRAM check**: GPU-memory meter with a per-process bar chart. Star Citizen stays green,
  other apps turn amber above 10% and red above 20% of total VRAM. CLI: `--vram`.
  NVIDIA only.

### Changed
- Process names are shortened (full command lines → "Discord", "Star Citizen", …).
- Version number shown in the app header.
- Heavy-app flagging is proportional (share of total VRAM) instead of a fixed MiB value.

### Fixed
- **Clear shader cache** removes SC's real cache (`shaders` and `vulkanshadercache` inside
  the build folder), plus the install-dir and launcher (`rsilauncher`) shader caches.

## [1.0] - 2026-10-03

### Added
- First public release: kill/restart, config files in Kate, Game.log follow, backup/restore
  with auto-backup, joystick diagnostics, KWin window fixes and rule, shader cache clear,
  PipeWire quantum, StarStrings updater, a CLI flag for every action.

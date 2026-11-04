# Changelog

All notable changes to Idus SC Toolbox are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · Versioning: `vMAJOR.MINOR`.

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

## Idus SC Toolbox v1.1

One-click fixes for running **Star Citizen on Linux** (LUG/Wine, KDE Plasma 6).

### ✨ New
- 📊 **VRAM check** — a visual GPU-memory meter with a per-process bar chart. Star Citizen
  stays green; other apps turn **amber above 10%** and **red above 20%** of total VRAM, so
  you can spot and close the memory hogs (OrcaSlicer, browsers, …) that cause hard freezes
  a few seconds into a session. Also on the CLI: `idus-sc-toolbox --vram`.
  **NVIDIA only** (reads `nvidia-smi`, works on any NVIDIA card).

### 🔧 Changes
- Process names are shortened (full command lines → "Discord", "Star Citizen", …).
- Version number shown in the app header.
- Heavy-app flagging is now proportional (share of total VRAM) instead of a fixed MiB value,
  so small apps are no longer falsely flagged.

### 🐛 Fixed
- **Clear shader cache** now removes SC's real cache — both `shaders` **and** the large
  `vulkanshadercache` inside the build folder (`…/Star Citizen/starcitizen_(sc-alpha-…)/`).
  Previously it found neither and only cleared the optional driver cache. Also clears the
  install-dir and launcher (`rsilauncher`) shader caches.

### Install
Download `idus-sc-toolbox.py`, `install-idus-sc-toolbox.sh` and `idus-sc-toolbox.png`, put
them in one folder, then:

```bash
bash install-idus-sc-toolbox.sh
```

Or clone and install in one line:

```bash
git clone https://github.com/Idus/idus-sc-toolbox.git && cd idus-sc-toolbox && bash install-idus-sc-toolbox.sh
```

Requires `python-gobject` + `gtk4`. The Wine prefix is auto-detected from your LUG config.
See the [README](../../blob/main/README.md) for details.

### Notes
- VRAM check is **NVIDIA only**; joystick diagnostics are tuned for **VIRPIL** HOTAS. Everything else is vendor-agnostic.
- Window rules are machine-specific — adjust the `Screen` number if a window lands on the wrong monitor.
- Not affiliated with Cloud Imperium Games or the LUG. Ships a backup button for a reason. 🙂
- **AI was used as part of the development.**

**Full changelog:** v1.0...v1.1

o7

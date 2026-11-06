#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
#  Idus SC Toolbox  ·  v1.3.6  ·  build 2026-11-06
#  (bump APP_VERSION + BUILD below and this line together on every update)
# ─────────────────────────────────────────────────────────────────────────────
"""
Idus SC Toolbox — a small companion to LUG Helper for Star Citizen on Linux (KDE Plasma 6).
GTK4. Self-contained folder: app + icon + settings live together, so one folder backup
captures everything.

GUI launches with no arguments. Same actions via CLI (KDE shortcuts / Stream Deck):
  idus-sc-toolbox --status          Status: runners, wineserver, game build, disk, launcher log, audio
  idus-sc-toolbox --launch          ▶ Launch: checks RSI for a launcher update first (→ patch mode)
  idus-sc-toolbox --kill            Close SC + launcher + wineserver
  idus-sc-toolbox --restart         Kill everything, then run sc-launch.sh (play)
  idus-sc-toolbox --restart-patch   Kill everything, then run "sc-launch.sh patch" (PATCH runner)
  idus-sc-toolbox --backup          Back up config/keybinds (all environments)
  idus-sc-toolbox --restore [FILE]  Restore from backup (no FILE: latest). Safety copy first.
  idus-sc-toolbox --diag            Joystick diagnostics (OS devices, duplicates, what SC expects)
  idus-sc-toolbox --fix-windows     Move launcher to centre + SC to main screen (one-shot, KWin)
  idus-sc-toolbox --kwin-rule       Install permanent KWin rule: pin windows to the main screen
  idus-sc-toolbox --shaders         Clear SC's shader cache (NO prompt in CLI! use with care)
  idus-sc-toolbox --driver-cache    (with --shaders) also clear the GPU driver cache
  idus-sc-toolbox --starstrings     Download latest StarStrings (LIVE) english global.ini
  idus-sc-toolbox --quantum on|off  PipeWire clock.force-quantum 2048 / reset
  --env PTU                         target an environment other than LIVE
  --screen N|NAME                   main screen for the window fixes, e.g. HDMI-A-1 (default: KDE primary)

Dependencies (CachyOS / Arch):  sudo pacman -S --needed python-gobject gtk4
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- configuration
APP_VERSION = "1.3.6"
BUILD = "2026-11-06"  # keep in sync with the header comment at the top of this file
PREFIX = Path(os.environ.get("SC_PREFIX", Path.home() / "Games/star-citizen"))
BACKUP_DIR = Path(os.environ.get("SC_BACKUP_DIR", Path.home() / "SC-backups"))
KEEP_BACKUPS = 15
EDITOR = os.environ.get("SC_EDITOR", "kate")
WIN_USER = os.environ.get("SC_WIN_USER", os.environ.get("USER", "user"))
ENVIRONMENTS = ["LIVE", "PTU", "EPTU", "TECH-PREVIEW", "HOTFIX"]

# Self-contained folder: icon + settings next to the app (like WolfLight)
APP_DIR = Path(__file__).resolve().parent
ICON_FILE = APP_DIR / "idus-sc-toolbox.png"
SETTINGS_FILE = APP_DIR / "idus-sc-toolbox-settings.json"

GAME_ROOT = PREFIX / "drive_c/Program Files/Roberts Space Industries/StarCitizen"
LAUNCH_SH = PREFIX / "sc-launch.sh"
APPDATA_LOCAL = PREFIX / f"drive_c/users/{WIN_USER}/AppData/Local"
PROCESS_PATTERNS = ["StarCitizen.exe", "RSI Launcher.exe", "RSI Launcher", "rsilauncher"]

DEFAULT_SETTINGS = {"autobackup": False, "last_autobackup": "", "main_screen": 0, "main_screen_name": ""}


# ---------------------------------------------------------------- settings
def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text()))
    except Exception:
        pass
    return s


def save_settings(s: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))
    except Exception:
        pass


# ---------------------------------------------------------------- helpers
def ifind(base: Path, *parts: str):
    """Case-insensitive path (Wine ignores case, ext4 does not)."""
    cur = base
    for part in parts:
        if not cur.is_dir():
            return None
        hit = next((c for c in cur.iterdir() if c.name.lower() == part.lower()), None)
        if hit is None:
            return None
        cur = hit
    return cur


def env_dir(env: str) -> Path:
    return ifind(GAME_ROOT, env) or GAME_ROOT / env


def profile_file(env: str, name: str):
    return ifind(env_dir(env), "user", "client", "0", "Profiles", "default", name)


def human(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def dir_size(p: Path) -> int:
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def launch_sh_assignments():
    """Every VAR="value" line in sc-launch.sh, in file order (also inside if/case blocks)."""
    out = []
    if LAUNCH_SH.is_file():
        for line in LAUNCH_SH.read_text(errors="ignore").splitlines():
            m = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)="?([^"#]*)"?', line)
            if m:
                out.append((m.group(1),
                            os.path.expandvars(m.group(2).strip()).replace("~", str(Path.home()), 1)))
    return out


def launch_sh_vars() -> dict:
    return dict(launch_sh_assignments())  # last assignment wins (unchanged since v1.0)


def runner_bins() -> list:
    """Every Wine runner bin dir sc-launch.sh points at, in file order (play runner first,
    then e.g. the patch runner). Values like "$patch_wine_path" don't exist on disk and are
    skipped. A wineserver can only stop a server of its own Wine version, so act_kill
    has to run -k with each of these."""
    out = []
    for key, val in launch_sh_assignments():
        if key.endswith("wine_path") and (Path(val) / "wineserver").exists() and val not in out:
            out.append(val)
    return out


def wineserver_bin() -> str:
    bins = runner_bins()
    if bins:
        return str(Path(bins[0]) / "wineserver")
    return shutil.which("wineserver") or "wineserver"


def launch_sh_has_patch() -> bool:
    """True if sc-launch.sh understands "sc-launch.sh patch" (see README → Patch mode)."""
    try:
        txt = LAUNCH_SH.read_text(errors="ignore")
    except OSError:
        return False
    return bool(re.search(r'\$\{?1\}?"?\s*==?\s*"?patch\b', txt)
                or re.search(r'^\s*"?patch"?\s*\)', txt, re.M))


def game_running() -> bool:
    return subprocess.run(["pgrep", "-f", "StarCitizen.exe"], capture_output=True).returncode == 0


# ---------------------------------------------------------------- action: process
def act_kill(log):
    for pat in PROCESS_PATTERNS:
        r = subprocess.run(["pkill", "-9", "-f", pat], capture_output=True)
        log(("✅ killed " if r.returncode == 0 else "· nothing matched ") + pat)
    env = dict(os.environ, WINEPREFIX=str(PREFIX))
    servers = [str(Path(b) / "wineserver") for b in runner_bins()] or [wineserver_bin()]
    for ws in servers:
        name = Path(ws).parent.parent.name if len(servers) > 1 else ws
        try:
            r = subprocess.run([ws, "-k"], env=env, capture_output=True, text=True)
        except OSError as e:
            log(f"⏳ wineserver -k ({name}): {e}")
            continue
        log(f"✅ wineserver -k ({name})" if r.returncode == 0
            else f"· wineserver -k ({name}): {r.stderr.strip() or 'nothing to kill'}")
    time.sleep(1)
    left = subprocess.run(["pgrep", "-af", "\\.exe"], capture_output=True, text=True).stdout.strip()
    log("✅ no .exe processes left" if not left else f"❌ still running:\n{left}")


def act_start_launcher(log, patch=False):
    """Kill everything, then run sc-launch.sh — or "sc-launch.sh patch" (RSI Launcher on the
    PATCH runner, for game updates / Verify). Patch mode is refused if sc-launch.sh lacks it,
    so it never silently starts the normal runner instead."""
    if not LAUNCH_SH.is_file():
        log(f"❌ cannot find {LAUNCH_SH} — set SC_PREFIX if the prefix is elsewhere")
        return
    if patch:
        if not launch_sh_has_patch():
            log("❌ sc-launch.sh has no patch mode — nothing was started.")
            log("   Add it first: README → 'Patch mode (two runners)'.")
            return
        pw = launch_sh_vars().get("patch_wine_path")
        if pw and "$" not in pw and not (Path(pw) / "wine").exists():
            log(f"❌ PATCH runner not found: {pw} — nothing was started.")
            return
    act_kill(log)
    if kwin_raise_launcher(log):
        log("· KWin centres the launcher on the main screen and brings it to the front")
    cmd = ["bash", str(LAUNCH_SH)] + (["patch"] if patch else [])
    subprocess.Popen(cmd, start_new_session=True)
    if patch:
        pw = launch_sh_vars().get("patch_wine_path")
        runner = Path(pw).parent.name if pw and "$" not in pw else "patch runner"
        log(f"✅ started sc-launch.sh patch → {runner}")
        log("   Update / Verify in the launcher, then 'Restart launcher' to play.")
    else:
        bins = runner_bins()
        log("✅ started sc-launch.sh" + (f" → {Path(bins[0]).parent.name}" if bins else ""))


# ---------------------------------------------------------------- status panel (read-only)
DISK_WARN_GB = 50  # free space on the prefix disk below this is flagged (patches need room)


def runner_short(bin_dir) -> str:
    """'…/lug-wine-tkg-staging-experimental-wayland-git-11.17-1/bin' → '11.17-1 wayland'."""
    name = Path(bin_dir).parent.name
    m = re.search(r"(\d+\.\d+(?:[.-]\d+)*)$", name)
    ver = m.group(1) if m else name
    flavour = "wayland" if "wayland" in name else ("staging" if "staging" in name else "")
    return f"{ver} {flavour}".strip()


def _proc_prefix(pid: str) -> str:
    """WINEPREFIX of a running process (resolved), '' if unknown."""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return ""
    for e in env:
        if e.startswith(b"WINEPREFIX="):
            return os.path.realpath(e[len(b"WINEPREFIX="):].decode(errors="ignore"))
    return ""


def running_wineservers() -> list:
    """Every running wineserver as {'bin': resolved bin dir, 'prefix': resolved WINEPREFIX}.
    Wine starts the server through paths like …/lib/wine/x86_64-unix/../../../bin/wineserver,
    so both sides are resolved before comparing."""
    r = subprocess.run(["pgrep", "-a", "wineserver"], capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        try:  # skip zombies (already exited, not yet reaped)
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                continue
        except (OSError, IndexError):
            continue
        exe = cmd.split()[0] if cmd.split() else ""
        if "/" in exe:
            bin_dir = os.path.realpath(os.path.dirname(exe))
        else:
            try:
                bin_dir = os.path.dirname(os.path.realpath(f"/proc/{pid}/exe"))
            except OSError:
                bin_dir = ""
        out.append({"bin": bin_dir, "prefix": _proc_prefix(pid)})
    return out


def game_build(env="LIVE"):
    """(version, build) from <env>/build_manifest.id, e.g. ('4.10.2', '12913470'); ('', '') if unknown."""
    f = ifind(env_dir(env), "build_manifest.id")
    if not f:
        return "", ""
    try:
        d = json.loads(f.read_text(errors="ignore")).get("Data", {})
    except Exception:
        return "", ""
    ver = re.sub(r"^sc-alpha-", "", str(d.get("Branch", ""))).strip()
    return ver, str(d.get("RequestedP4ChangeNum", "")).strip()


def launcher_log_file():
    return ifind(PREFIX, "drive_c", "users", WIN_USER, "AppData", "Roaming",
                 "rsilauncher", "logs", "log.log")


RSI_LATEST_YML = os.environ.get("SC_LAUNCHER_YML",
                                "https://install.robertsspaceindustries.com/rel/2/latest.yml")
_LATEST_CACHE = {"t": 0.0, "val": ("", "")}
LOG_LINE_RE = re.compile(r'"t":"([^"]+)",\s*"\[[^\]]*\]\[(\w+)\]\s*":\s*"(.*)"\s*}')


def vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def installed_launcher_version() -> str:
    """'2.19.1' from the RSI Launcher uninstall entry in the prefix registry ('' if not found)."""
    for reg in ("user.reg", "system.reg"):
        try:
            txt = (PREFIX / reg).read_text(errors="ignore")
        except OSError:
            continue
        for block in txt.split("\n["):
            if '"DisplayName"="RSI Launcher' in block:
                m = re.search(r'"DisplayVersion"="([^"]+)"', block)
                if m:
                    return m.group(1)
    return ""


def latest_launcher_version(timeout=5, max_age=600):
    """(version, '') from RSI's public latest.yml (the file LUG Helper installs from),
    or ('', reason). Cached for max_age seconds; max_age=0 forces a fresh check."""
    if max_age and time.time() - _LATEST_CACHE["t"] < max_age:
        return _LATEST_CACHE["val"]
    try:
        req = urllib.request.Request(RSI_LATEST_YML, headers={"User-Agent": "Idus-SC-Toolbox"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read(65536).decode(errors="ignore")
        m = re.search(r"^version:\s*['\"]?([\d.]+)", txt, re.M) or re.search(r"Setup-([\d.]+)\.exe", txt)
        val = (m.group(1), "") if m else ("", "unexpected latest.yml format")
    except Exception as e:  # offline, DNS, timeout …
        val = ("", (str(getattr(e, "reason", "")) or str(e))[:60])
    _LATEST_CACHE.update(t=time.time(), val=val)
    return val


def launcher_log_events(max_bytes=600_000):
    """[(datetime, level, message)] from the end of the RSI Launcher log, None if there is no log."""
    f = launcher_log_file()
    if not f:
        return None
    try:
        size = f.stat().st_size
        with open(f, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            lines = fh.read().decode(errors="ignore").splitlines()
    except OSError:
        return None
    out = []
    for line in lines:
        m = LOG_LINE_RE.search(line)
        if m:
            try:
                out.append((dt.datetime.strptime(m.group(1)[:19], "%Y-%m-%d %H:%M:%S"), m.group(2), m.group(3)))
            except ValueError:
                pass
    return out


def launcher_log_state(env="LIVE"):
    """(state, value, detail). The log keeps old errors, so an error only counts while the
    game files (build_manifest.id) are older than it, i.e. no patch has finished since."""
    ev = launcher_log_events()
    if ev is None:
        return "idle", "no log yet", "appears after the first launcher start"
    errs = [e for e in ev if "did not converge" in e[2] or "ERR_PATCHER" in e[2]]
    if not errs:
        return "ok", "no patch errors", f"{len(ev)} recent log lines checked"
    when, _lvl, msg = errs[-1]
    kind = "verify loop" if "did not converge" in msg else "patch error"
    # Resolved if the game was patched (build_manifest.id) or played (Game.log) after the error.
    after = [dt.datetime.fromtimestamp(f.stat().st_mtime)
             for f in (ifind(env_dir(env), "build_manifest.id"), ifind(env_dir(env), "Game.log")) if f]
    if after and max(after) > when:
        return "ok", "resolved since", f"{kind} {when:%m-%d %H:%M} · game patched or played after"
    return "warn", kind, f"{when:%m-%d %H:%M} · use 🩹 Patch / verify"


def launcher_update_state():
    """(state, value, detail) for the RSI Launcher tile."""
    inst = installed_launcher_version()
    latest, err = latest_launcher_version()
    if not inst:
        return "idle", "not found", "no RSI Launcher entry in the prefix registry"
    if latest and vtuple(latest) > vtuple(inst):
        return "warn", f"{inst} → {latest}", "update out · ▶ Launch uses patch mode"
    if latest:
        return "ok", inst, "up to date (checked with RSI)"
    return "idle", inst, f"update check offline · {err}" if err else "update check offline"


def quantum_state():
    """Forced PipeWire quantum: int (0 = not forced) or None if pw-metadata is missing."""
    if not shutil.which("pw-metadata"):
        return None
    try:
        r = subprocess.run(["pw-metadata", "-n", "settings", "0", "clock.force-quantum"],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"value:'(\d+)'", r.stdout)
    return int(m.group(1)) if m else 0


def mount_point(p: Path) -> Path:
    p = Path(p).resolve()
    while not os.path.ismount(p) and p != p.parent:
        p = p.parent
    return p


def status_snapshot(env="LIVE") -> list:
    """Tiles for the status panel / --status: dicts with key, label, value, detail, state
    (ok | warn | error | idle) and, for the disk tile, frac (0..1 used)."""
    tiles = []
    v = launch_sh_vars()
    bins = runner_bins()
    play = bins[0] if bins else None
    lang = v.get("LANG", "")
    xim = v.get("GTK_IM_MODULE", "").lower() == "xim" or v.get("XMODIFIERS", "") == "@im=none"
    locale = " · ".join(x for x in (lang, "XIM (åäö)" if xim else "") if x) or "system locale"
    tiles.append(dict(key="play", label="PLAY RUNNER",
                      value=runner_short(play) if play else "system Wine",
                      detail=locale if play else "no runner set in sc-launch.sh",
                      state="ok" if play else "idle"))

    has_patch = launch_sh_has_patch()
    pw = v.get("patch_wine_path", "")
    pw_ok = bool(has_patch and pw and "$" not in pw and (Path(pw) / "wine").exists())
    if pw_ok:
        tiles.append(dict(key="patch", label="PATCH RUNNER", value=runner_short(pw),
                          detail="used by 🩹 Patch / verify", state="ok"))
    else:
        tiles.append(dict(key="patch", label="PATCH RUNNER",
                          value="runner missing" if has_patch else "not set up",
                          detail=pw if has_patch else "optional · README → Patch mode",
                          state="warn" if has_patch else "idle"))

    # Only servers for the SC prefix count; other Wine apps (another prefix) are left alone.
    real_bins = {os.path.realpath(b): b for b in bins}
    pre = os.path.realpath(PREFIX)
    servers = running_wineservers()
    ours = [s for s in servers if s["prefix"] == pre or (not s["prefix"] and s["bin"] in real_bins)]
    others = len(servers) - len(ours)
    other_txt = f" · {others} other Wine app{'s' if others != 1 else ''}" if others else ""
    if not ours:
        tiles.append(dict(key="wine", label="WINESERVER NOW", value="not running",
                          detail="starts with the launcher" + other_txt, state="idle"))
    else:
        names = []
        for s in ours:
            b = real_bins.get(s["bin"])
            names.append(f"{runner_short(b).split()[0]} · {'play' if b == play else 'patch'}"
                         if b else "unknown Wine")
        clean = len(ours) == 1 and names[0] != "unknown Wine"
        if clean:
            det = "1 server" + other_txt
        elif len(ours) > 1:
            det = f"{len(ours)} servers for SC · 💀 Kill stops all"
        else:
            det = "not a runner from sc-launch.sh"
        tiles.append(dict(key="wine", label="WINESERVER NOW", value=" + ".join(names),
                          detail=det, state="ok" if clean else "warn"))

    ver, build = game_build(env)
    tiles.append(dict(key="game", label=f"GAME · {env}", value=ver or "unknown",
                      detail=f"build {build}" if build else "no build_manifest.id",
                      state="ok" if ver else "idle"))

    st, val, det = launcher_update_state()
    tiles.append(dict(key="launcher", label="RSI LAUNCHER", value=val, detail=det, state=st))

    try:
        du = shutil.disk_usage(PREFIX)
        free_gb = du.free / 1e9
        tiles.append(dict(key="disk", label="DISK", value=f"{free_gb:,.0f} GB free".replace(",", " "),
                          detail=f"{du.used / du.total:.0%} used · {mount_point(PREFIX)}", frac=du.used / du.total,
                          state="warn" if free_gb < DISK_WARN_GB else "ok"))
    except OSError:
        tiles.append(dict(key="disk", label="DISK", value="unknown", detail=str(PREFIX),
                          frac=0.0, state="idle"))

    st, val, det = launcher_log_state(env)
    tiles.append(dict(key="llog", label="LAUNCHER LOG", value=val, detail=det, state=st))

    q = quantum_state()
    if q is None:
        tiles.append(dict(key="audio", label="AUDIO · PIPEWIRE", value="unknown",
                          detail="pw-metadata not found", state="idle"))
    elif q:
        tiles.append(dict(key="audio", label="AUDIO · PIPEWIRE", value=f"quantum {q}",
                          detail="forced · anti-stutter on", state="ok"))
    else:
        tiles.append(dict(key="audio", label="AUDIO · PIPEWIRE", value="default quantum",
                          detail="🔊 2048 if audio crackles", state="idle"))
    return tiles


def act_launch(log):
    """▶ Launch: check RSI for a newer launcher first. Newer launcher → patch mode
    (the launcher updates itself on start, which has failed on the play runner)."""
    inst = installed_launcher_version()
    latest, err = latest_launcher_version(max_age=0)
    if latest and inst and vtuple(latest) > vtuple(inst):
        log(f"⚠️ RSI Launcher {latest} is out (installed: {inst}).")
        if launch_sh_has_patch():
            log("   Starting in patch mode — the launcher updates itself on the patch runner.")
            act_start_launcher(log, True)
            return
        log("   sc-launch.sh has no patch mode — starting normally.")
    elif latest:
        log(f"✅ RSI Launcher {inst or '?'} is up to date")
    else:
        log(f"· launcher update check failed ({err}) — starting normally")
    act_start_launcher(log)


def act_status(log, env="LIVE"):
    icon = {"ok": "✅", "warn": "⚠️ ", "error": "❌", "idle": "· "}
    for t in status_snapshot(env):
        log(f"{icon[t['state']]} {t['label']:<18} {t['value']:<22} {t['detail']}")


# ---------------------------------------------------------------- action: backup
def _skip_shaders(ti):
    return None if "/shaders" in ti.name.lower() or ti.name.lower().endswith(".log") else ti


def act_backup(log, env_list=None, quiet=False):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"sc-backup-{stamp}.tar.gz"
    added = 0
    with tarfile.open(target, "w:gz") as tar:
        for env in (env_list or ENVIRONMENTS):
            ed = ifind(GAME_ROOT, env)
            if not ed:
                continue
            for item in ["user", "USER.cfg"]:
                p = ifind(ed, item)
                if p:
                    tar.add(p, arcname=f"{env}/{p.name}", filter=_skip_shaders)
                    added += 1
                    if not quiet:
                        log(f"  + {env}/{p.name}")
        if LAUNCH_SH.is_file():
            tar.add(LAUNCH_SH, arcname="sc-launch.sh")
            added += 1
            if not quiet:
                log("  + sc-launch.sh")
    if added == 0:
        target.unlink(missing_ok=True)
        log(f"❌ nothing found to back up under {GAME_ROOT}")
        return None
    log(f"✅ backup: {target.name} ({human(target.stat().st_size)})")
    old = sorted(BACKUP_DIR.glob("sc-backup-*.tar.gz"))[:-KEEP_BACKUPS]
    for o in old:
        o.unlink()
    if old and not quiet:
        log(f"· pruned {len(old)} older backups (keeping {KEEP_BACKUPS})")
    return target


def list_backups():
    return sorted(BACKUP_DIR.glob("sc-backup-*.tar.gz"), reverse=True)


def _safe_extract(tar: tarfile.TarFile, dest: Path):
    """Extract without paths escaping dest (path traversal guard)."""
    dest = dest.resolve()
    for m in tar.getmembers():
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(dest) + os.sep) and target != dest:
            raise RuntimeError(f"refusing suspicious path: {m.name}")
    try:
        tar.extractall(dest, filter="data")  # py3.12+
    except TypeError:
        tar.extractall(dest)


def act_restore(log, backup_path=None, make_safety=True):
    if game_running():
        log("❌ Star Citizen is running — close the game first (button 1).")
        return
    if backup_path is None:
        backups = list_backups()
        if not backups:
            log(f"❌ no backup found in {BACKUP_DIR}")
            return
        backup_path = backups[0]
    backup_path = Path(backup_path)
    if not backup_path.is_file():
        log(f"❌ does not exist: {backup_path}")
        return

    if make_safety:
        log("· taking a safety copy of the current state before restoring …")
        act_backup(log, quiet=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            with tarfile.open(backup_path, "r:gz") as tar:
                _safe_extract(tar, tmp)
        except Exception as e:  # noqa
            log(f"❌ could not unpack: {e}")
            return

        restored = 0
        for child in tmp.iterdir():
            if child.name == "sc-launch.sh" and child.is_file():
                shutil.copy2(child, LAUNCH_SH)
                log("  ↩ sc-launch.sh")
                restored += 1
                continue
            if child.is_dir():  # environment name (LIVE/PTU/…)
                ed = env_dir(child.name)
                ed.mkdir(parents=True, exist_ok=True)
                src_user = child / "user"
                if src_user.is_dir():
                    dst_user = ifind(ed, "user") or ed / "user"
                    if dst_user.exists():
                        shutil.rmtree(dst_user, ignore_errors=True)
                    shutil.copytree(src_user, dst_user)
                    log(f"  ↩ {child.name}/user")
                    restored += 1
                src_cfg = child / "USER.cfg"
                if src_cfg.is_file():
                    shutil.copy2(src_cfg, ed / "USER.cfg")
                    log(f"  ↩ {child.name}/USER.cfg")
                    restored += 1
        if restored:
            log(f"✅ restored {restored} items from {backup_path.name}")
        else:
            log("❌ the backup contained nothing recognisable to restore")


# ---------------------------------------------------------------- action: joystick diagnostics
def act_diag(log, env="LIVE"):
    log("— Joystick diagnostics —")
    byid = Path("/dev/input/by-id")
    js_links, hits = [], []
    if byid.is_dir():
        for p in sorted(byid.iterdir()):
            low = p.name.lower()
            if any(k in low for k in ("virpil", "vpc", "3344")):
                hits.append(p.name)
                if low.endswith("-joystick"):
                    js_links.append(p.name)
    if hits:
        log(f"· /dev/input/by-id: {len(hits)} VIRPIL entries, {len(js_links)} joystick nodes:")
        for j in js_links:
            log(f"    {j}")
    else:
        log("· no VIRPIL devices in /dev/input/by-id")

    sticks = []
    try:
        block = {}
        for line in Path("/proc/bus/input/devices").read_text(errors="ignore").splitlines() + [""]:
            if not line.strip():
                if block.get("virpil"):
                    sticks.append(block)
                block = {}
                continue
            if line.startswith("N:"):
                block["name"] = line.split("=", 1)[-1].strip().strip('"')
                block["virpil"] = "virpil" in block["name"].lower()
            elif line.startswith("U: Uniq="):
                block["uniq"] = line.split("=", 1)[-1].strip()
            elif line.startswith("H: Handlers="):
                block["handlers"] = line.split("=", 1)[-1].strip()
    except Exception as e:  # noqa
        log(f"· could not read /proc/bus/input/devices: {e}")
    if sticks:
        log(f"· kernel sees {len(sticks)} VIRPIL device(s):")
        seen_js = {}
        for s in sticks:
            js = [h for h in s.get("handlers", "").split() if h.startswith("js")]
            log(f"    {s.get('name','?')}  Uniq={s.get('uniq','?')}  →  {' '.join(js) or '(no js)'}")
            for j in js:
                seen_js.setdefault(j, 0)
                seen_js[j] += 1
        dupe = [j for j, n in seen_js.items() if n > 1]
        if dupe:
            log(f"⚠️  KERNEL-LEVEL DUPLICATE: {', '.join(dupe)} shared by several devices")
        else:
            log("✅ no kernel duplicate (each js belongs to one device)")

    am = profile_file(env, "actionmaps.xml")
    if am and am.is_file():
        txt = am.read_text(errors="ignore")
        opts = re.findall(r'<options[^>]*type="joystick"[^>]*>', txt)
        if opts:
            log(f"· SC ({env}) actionmaps.xml bound joystick instances:")
            for o in opts:
                inst = re.search(r'instance="(\d+)"', o)
                prod = re.search(r'Product="([^"]+)"', o)
                log(f"    instance {inst.group(1) if inst else '?'}  {prod.group(1) if prod else ''}")
            log("  (matches Product GUID against the devices above — changed GUID = bindings point wrong)")
        else:
            log(f"· SC ({env}): no joystick instances in actionmaps.xml")
    else:
        log(f"· no actionmaps.xml found for {env}")
    log("— end —")


# ---------------------------------------------------------------- action: KWin windows
def kde_screens() -> list:
    """Enabled screens as KDE sees them, left to right:
    [{'name', 'primary', 'x', 'y', 'w', 'h'}] from `kscreen-doctor -o` ([] if unavailable)."""
    try:
        r = subprocess.run(["kscreen-doctor", "-o"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out, cur = [], None
    for line in re.sub(r"\x1b\[[0-9;]*m", "", r.stdout).splitlines():
        m = re.match(r"\s*Output:\s+\d+\s+(\S+)", line)
        if m:
            cur = dict(name=m.group(1), enabled=False, primary=False, x=0, y=0, w=0, h=0)
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s == "enabled":
            cur["enabled"] = True
        elif s.startswith("priority"):
            cur["primary"] = s.split()[-1] == "1"
        elif s == "primary":  # older Plasma
            cur["primary"] = True
        m = re.match(r"Geometry:\s*(-?\d+),(-?\d+)\s+(\d+)x(\d+)", s)
        if m:
            cur.update(x=int(m.group(1)), y=int(m.group(2)), w=int(m.group(3)), h=int(m.group(4)))
    return sorted((o for o in out if o["enabled"]), key=lambda o: (o["x"], o["y"]))


def kwin_screen_order() -> list:
    """Screen names in KWin's own numbering (used by window rules and KWin scripts)."""
    try:
        r = _dbus("/KWin", "org.kde.KWin.supportInformation")
    except OSError:
        return []
    names, want = [], False
    for line in r.stdout.splitlines():
        s = line.strip()
        if re.match(r"Screen \d+:", s):
            want = True
        elif want and s.startswith("Name:"):
            names.append(s.split(":", 1)[1].strip())
            want = False
    return names


def main_screen(screen=None):
    """(KWin index, name) of the main screen. Order: --screen (index or name) > the screen chosen
    in the Toolbox > KDE's primary screen > the old index setting > 0. KWin numbers screens its
    own way (not left to right, not primary first), so the screen is tracked by name."""
    order = kwin_screen_order()
    s = load_settings()
    choice = screen if screen not in (None, "") else (s.get("main_screen_name") or None)
    if choice is not None and str(choice).isdigit():
        i = int(choice)
        return i, (order[i] if i < len(order) else "")
    name = choice or next((o["name"] for o in kde_screens() if o["primary"]), None)
    if name and name in order:
        return order.index(name), name
    i = int(s.get("main_screen", 0) or 0)
    return i, (order[i] if i < len(order) else (name or ""))


def _main_screen_arg(screen=None) -> int:
    return main_screen(screen)[0]


def _screen_js(src: str, screen=None) -> str:
    idx, name = main_screen(screen)
    return src.replace('"__SCREEN_NAME__"', json.dumps(name)).replace("__SCREEN__", str(idx))


KWIN_FIX_JS = r"""
const WANT = "__SCREEN_NAME__";
function geoForScreen(idx) {
    try {
        const scr = workspace.screens;
        if (scr && scr.length) {
            for (const s of scr) { if (WANT && s.name === WANT) return s.geometry; }
            const s = (idx >= 0 && idx < scr.length) ? scr[idx] : scr[0];
            return s.geometry;
        }
    } catch (e) {}
    return workspace.clientArea(KWin.MaximizeArea, 0, workspace.currentDesktop);
}
const main = geoForScreen(__SCREEN__);
const wins = (typeof workspace.windowList === "function")
    ? workspace.windowList()
    : (typeof workspace.clientList === "function" ? workspace.clientList() : []);
for (const w of wins) {
    const cap = (w.caption || "").toLowerCase();
    const cls = (w.resourceClass || "").toLowerCase();
    if (cap === "wine system tray") { w.minimized = true; continue; }
    if (cls.includes("rsi launcher") || cap.includes("rsi launcher")) {
        if (w.normalWindow) {
            const g = w.frameGeometry;
            w.frameGeometry = { x: Math.round(main.x + (main.width - g.width) / 2),
                                y: Math.round(main.y + (main.height - g.height) / 2),
                                width: g.width, height: g.height };
            workspace.activeWindow = w;
        }
        continue;
    }
    if (cls.includes("starcitizen") || cap.includes("star citizen")) {
        w.frameGeometry = { x: main.x, y: main.y, width: main.width, height: main.height };
    }
}
"""


def _dbus(*args):
    return subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=org.kde.KWin", *args],
                          capture_output=True, text=True)


KWIN_RAISE_JS = r"""
// Idus SC Toolbox: when the RSI Launcher opens, centre it on the main screen and bring it
// to the front (the same as "Fix windows now").
// - A launcher started from inside an app gets no KDE start token, so KWin's focus-stealing
//   prevention would open it *behind* the active window.
// - The launcher can also restore an old position outside the visible screens; while this
//   script is loaded (3 min) it is pulled back to the main screen if it lands off-screen.
const seen = {};
function screenList() {
    try { return workspace.screens || []; } catch (e) { return []; }
}
const WANT = "__SCREEN_NAME__";
function mainGeo() {
    const scr = screenList();
    if (scr.length) {
        for (const s of scr) { if (WANT && s.name === WANT) return s.geometry; }
        const s = (__SCREEN__ >= 0 && __SCREEN__ < scr.length) ? scr[__SCREEN__] : scr[0];
        return s.geometry;
    }
    return workspace.clientArea(KWin.MaximizeArea, 0, workspace.currentDesktop);
}
function onScreen(g) {
    const cx = g.x + g.width / 2, cy = g.y + g.height / 2;
    const scr = screenList();
    if (!scr.length) return true;
    for (const s of scr) {
        const a = s.geometry;
        if (cx >= a.x && cx < a.x + a.width && cy >= a.y && cy < a.y + a.height) return true;
    }
    return false;
}
function centre(w) {
    const m = mainGeo(), g = w.frameGeometry;
    w.frameGeometry = { x: Math.round(m.x + (m.width - g.width) / 2),
                        y: Math.round(m.y + (m.height - g.height) / 2),
                        width: g.width, height: g.height };
}
function isLauncher(w) {
    const cap = (w.caption || "").toLowerCase();
    const cls = (w.resourceClass || "").toLowerCase();
    const nm = (w.resourceName || "").toLowerCase();
    return cls.includes("rsi launcher") || nm.includes("rsi launcher") || cap.includes("rsi launcher");
}
function bring(w) {
    if (!w || !w.normalWindow || !isLauncher(w)) return;
    const id = String(w.internalId);
    if (seen[id]) return;
    seen[id] = true;
    w.minimized = false;
    centre(w);
    if ("activeWindow" in workspace) { workspace.activeWindow = w; } else { workspace.activeClient = w; }
    if (w.frameGeometryChanged) {
        w.frameGeometryChanged.connect(function () { if (!onScreen(w.frameGeometry)) centre(w); });
    }
}
const added = workspace.windowAdded || workspace.clientAdded;
added.connect(function (w) {
    bring(w);
    if (w.captionChanged) { w.captionChanged.connect(function () { bring(w); }); }
});
"""
KWIN_RAISE_NAME = "idus-sc-toolbox-raise"


def kwin_raise_launcher(log, seconds=180) -> bool:
    """Load a short-lived KWin script that activates the RSI Launcher window when it appears,
    and unload it again after `seconds` (detached, so it also works from the CLI)."""
    if not shutil.which("dbus-send"):
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(_screen_js(KWIN_RAISE_JS))
        js = f.name
    _dbus("/Scripting", "org.kde.kwin.Scripting.unloadScript", f"string:{KWIN_RAISE_NAME}")
    r = _dbus("/Scripting", "org.kde.kwin.Scripting.loadScript", f"string:{js}", f"string:{KWIN_RAISE_NAME}")
    m = re.search(r"int32 (-?\d+)", r.stdout)
    if not m or m.group(1) == "-1":
        os.unlink(js)
        log("· KWin not available — if the launcher doesn't show, use 🎯 Fix windows now")
        return False
    _dbus(f"/Scripting/Script{m.group(1)}", "org.kde.kwin.Script.run")
    unload = ("dbus-send --session --dest=org.kde.KWin /Scripting "
              f"org.kde.kwin.Scripting.unloadScript string:{KWIN_RAISE_NAME} >/dev/null 2>&1")
    subprocess.Popen(["sh", "-c", f"sleep {int(seconds)}; {unload}; rm -f '{js}'"],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def act_fix_windows(log, screen=None):
    idx, sname = main_screen(screen)
    js_src = _screen_js(KWIN_FIX_JS, screen)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js_src)
        js = f.name
    name = "idus-sc-toolbox-fix"
    _dbus("/Scripting", "org.kde.kwin.Scripting.unloadScript", f"string:{name}")
    r = _dbus("/Scripting", "org.kde.kwin.Scripting.loadScript", f"string:{js}", f"string:{name}")
    m = re.search(r"int32 (-?\d+)", r.stdout)
    if not m or m.group(1) == "-1":
        log(f"❌ KWin refused to load the script: {r.stderr.strip() or r.stdout.strip()}")
        os.unlink(js)
        return
    _dbus(f"/Scripting/Script{m.group(1)}", "org.kde.kwin.Script.run")
    time.sleep(0.5)
    _dbus("/Scripting", "org.kde.kwin.Scripting.unloadScript", f"string:{name}")
    os.unlink(js)
    log(f"✅ launcher centred + SC moved to the main screen {sname or idx} ('Wine System Tray' minimised)")


def act_install_kwin_rule(log, screen=None):
    """Writes a permanent KWin rule that forces the RSI Launcher and Star Citizen
    onto the main screen. Backs up kwinrulesrc first and is idempotent."""
    idx, sname = main_screen(screen)
    rc = Path.home() / ".config/kwinrulesrc"
    import configparser
    cp = configparser.RawConfigParser()
    cp.optionxform = str
    if rc.is_file():
        shutil.copy2(rc, rc.with_suffix(".sctoolbox.bak"))
        try:
            cp.read(rc, encoding="utf-8")
        except Exception as e:  # noqa
            log(f"❌ could not read kwinrulesrc: {e} — aborting without writing")
            return

    # Remove any previous SC Toolbox rules (idempotent). Description prefix kept as
    # "SC Toolbox" for backwards compatibility with existing rules.
    old_uuids = [s for s in cp.sections()
                 if s != "General" and cp.has_option(s, "Description")
                 and cp.get(s, "Description").startswith("SC Toolbox")]
    for s in old_uuids:
        cp.remove_section(s)

    def add_rule(desc, wmclass, extra=None):
        u = str(uuid.uuid4())
        cp.add_section(u)
        cp.set(u, "Description", desc)
        cp.set(u, "wmclass", wmclass)
        cp.set(u, "wmclassmatch", "2")   # 2 = substring
        cp.set(u, "wmclasscomplete", "false")
        cp.set(u, "screen", str(idx))
        cp.set(u, "screenrule", "2")      # 2 = force
        cp.set(u, "ignoregeometry", "true")
        cp.set(u, "ignoregeometryrule", "2")
        for k, v in (extra or {}).items():
            cp.set(u, k, v)
        return u

    new = [
        add_rule("SC Toolbox – RSI Launcher", "rsi launcher.exe",
                 {"placement": "Centered", "placementrule": "2"}),
        add_rule("SC Toolbox – Star Citizen", "starcitizen.exe",
                 {"fullscreen": "true", "fullscreenrule": "2"}),
    ]

    existing = [s for s in cp.sections() if s != "General" and s not in new]
    rules_list = [s for s in existing] + new
    if not cp.has_section("General"):
        cp.add_section("General")
    cp.set("General", "count", str(len(rules_list)))
    cp.set("General", "rules", ",".join(rules_list))

    try:
        rc.parent.mkdir(parents=True, exist_ok=True)
        with open(rc, "w", encoding="utf-8") as f:
            cp.write(f, space_around_delimiters=False)
    except Exception as e:  # noqa
        log(f"❌ could not write kwinrulesrc: {e}")
        return
    subprocess.run(["dbus-send", "--session", "--dest=org.kde.KWin", "/KWin",
                    "org.kde.KWin.reconfigure"], capture_output=True)
    log(f"✅ KWin rule installed: pins RSI Launcher + Star Citizen to {sname or 'screen'} (KWin screen {idx})")
    log("⚠️  verify in System Settings → Window Management → Window Rules. "
        "Backup saved as kwinrulesrc.sctoolbox.bak")


def act_open_kwin_settings(log):
    for cmd in (["kcmshell6", "kwinrules"], ["systemsettings", "kcm_kwinrules"],
                ["systemsettings", "kwinrules"]):
        if shutil.which(cmd[0]):
            subprocess.Popen(cmd, start_new_session=True)
            log(f"✅ opened {cmd[0]} (Window Rules)")
            return
    log("❌ neither kcmshell6 nor systemsettings found")


# ---------------------------------------------------------------- action: shaders / audio
SHADER_SUBDIRS = ("shaders", "vulkanshadercache")  # the big Vulkan cache lives in the latter


def _deletable_cache(p: Path) -> bool:
    """Guard for the driver-cache paths read from sc-launch.sh: never the Wine prefix
    itself, a folder above it, $HOME or a folder above it."""
    try:
        r = p.resolve()
        home, pre = Path.home().resolve(), PREFIX.resolve()
    except OSError:
        return False
    return r not in {home, pre, *home.parents, *pre.parents}


def shader_targets(env: str, include_driver: bool):
    targets = []
    # The real SC cache lives under AppData/Local/Star Citizen/<build>/{shaders,vulkanshadercache}
    # where <build> is e.g. 'starcitizen_(sc-alpha-4.10.0)_qsmxke_0'. Descend into every build.
    sc_local = ifind(APPDATA_LOCAL, "Star Citizen")
    if sc_local:
        for build in sc_local.iterdir():
            if not build.is_dir():
                continue
            for sub in SHADER_SUBDIRS:
                d = ifind(build, sub)
                if d:
                    targets.append(d)
        # also catch caches sitting directly under 'Star Citizen' (older layouts)
        for sub in SHADER_SUBDIRS:
            d = ifind(sc_local, sub)
            if d:
                targets.append(d)
    # Secondary cache inside the install dir
    sec = ifind(env_dir(env), "user", "client", "0", "shaders")
    if sec:
        targets.append(sec)
    # Launcher shader cache in Roaming AppData (%APPDATA%\rsilauncher\shaders)
    roaming = ifind(PREFIX, "drive_c", "users", WIN_USER, "AppData", "Roaming", "rsilauncher", "shaders")
    if roaming:
        targets.append(roaming)
    if include_driver:
        v = launch_sh_vars()
        for key in ["__GL_SHADER_DISK_CACHE_PATH", "MESA_SHADER_CACHE_DIR", "DXVK_STATE_CACHE_PATH"]:
            if v.get(key) and Path(v[key]).is_dir() and _deletable_cache(Path(v[key])):
                targets.append(Path(v[key]))
        nv = Path.home() / ".cache/nvidia/GLCache"
        if nv.is_dir():
            targets.append(nv)
    return list(dict.fromkeys(targets))


def act_shaders(log, env, include_driver):
    if game_running():
        log("❌ Star Citizen is running — close the game first (button 1).")
        return
    tg = shader_targets(env, include_driver)
    if not tg:
        log("· no shader cache found")
        return
    for t in tg:
        size = dir_size(t)
        shutil.rmtree(t, ignore_errors=True)
        log(f"✅ deleted {t} ({human(size)})")
    log("· the first launch afterwards takes longer while the cache rebuilds.")


# ---------------------------------------------------------------- action: StarStrings
STARSTRINGS_API = "https://api.github.com/repos/MrKraken/StarStrings/releases/latest"


def _http_get(url, accept=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Idus-SC-Toolbox",
        "Accept": accept or "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _extract_global_ini(zip_bytes):
    """Return the global.ini content (bytes) from a zip, or None."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = z.namelist()
        pri = [n for n in names if n.lower().endswith("global.ini")
               and "localization/english" in n.lower().replace("\\", "/")]
        alt = [n for n in names if n.lower().endswith("global.ini")]
        hit = (pri or alt)
        return z.read(hit[0]) if hit else None


def act_starstrings(log, env="LIVE"):
    log("— StarStrings: fetching latest LIVE —")
    try:
        rel = json.loads(_http_get(STARSTRINGS_API))
    except urllib.error.HTTPError as e:  # noqa
        log(f"❌ GitHub API returned {e.code} (maybe rate limit, 60/hr). Try again later.")
        return
    except Exception as e:  # noqa
        log(f"❌ could not reach GitHub: {e}")
        return
    tag = rel.get("tag_name", "?")
    log(f"· latest release: {tag}")

    data = None
    assets = rel.get("assets", [])
    direct = next((a for a in assets if a["name"].lower() == "global.ini"), None)
    if direct:
        log(f"· downloading {direct['name']} …")
        data = _http_get(direct["browser_download_url"], accept="application/octet-stream")
    if data is None:
        zasset = next((a for a in assets if a["name"].lower().endswith(".zip")), None)
        src = zasset["browser_download_url"] if zasset else rel.get("zipball_url")
        if not src:
            log("❌ found neither global.ini nor a zip in the release.")
            return
        log(f"· downloading {'asset ' + zasset['name'] if zasset else 'source zip'} …")
        try:
            data = _extract_global_ini(_http_get(src, accept="application/octet-stream"))
        except Exception as e:  # noqa
            log(f"❌ could not unpack zip: {e}")
            return
    if not data:
        log("❌ no global.ini in the package.")
        return
    log(f"· global.ini fetched ({human(len(data))})")

    eng = ifind(env_dir(env), "data", "Localization", "english")
    if eng is None:
        eng = env_dir(env) / "data" / "Localization" / "english"
        eng.mkdir(parents=True, exist_ok=True)
        log(f"· created {eng}")
    target = (ifind(eng, "global.ini") or (eng / "global.ini"))

    if target.exists():
        bak = target.with_name(f"global.ini.BAK-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.move(str(target), str(bak))
        log(f"· backed up old → {bak.name}")

    try:
        target.write_bytes(data)
    except Exception as e:  # noqa
        log(f"❌ could not write {target}: {e}")
        return
    log(f"✅ StarStrings installed: {target}")
    log("⚠️  StarStrings also needs USER.cfg in the game root (g_language). Already present? Otherwise see the mod readme.")


def act_quantum(log, on: bool):
    val = "2048" if on else "0"
    r = subprocess.run(["pw-metadata", "-n", "settings", "0", "clock.force-quantum", val],
                       capture_output=True, text=True)
    log((f"✅ clock.force-quantum = {val}" if r.returncode == 0 else f"❌ {r.stderr.strip()}")
        + ("" if on else " (reset)"))


# ---------------------------------------------------------------- VRAM check (NVIDIA)
# Processes that count as "the game stack" rather than heavy extra apps.
VRAM_GAME_HINTS = ("starcitizen", "rsi launcher", "wine", "explorer.exe", "winedevice")
VRAM_DESKTOP_HINTS = ("kwin", "plasmashell", "xwayland", "xdg-desktop-portal", "opendeck",
                      "idus-sc-toolbox", "python3")


def vram_query():
    """Returns (used_mib, total_mib, gpu_name) or None if nvidia-smi is unavailable."""
    if not shutil.which("nvidia-smi"):
        return None
    r = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used,memory.total,name",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
    try:
        return int(parts[0]), int(parts[1]), (parts[2] if len(parts) > 2 else "GPU")
    except ValueError:
        return None


def vram_processes():
    """List of (pid, mem_mib, name) for GPU processes, biggest first. [] if none/unavailable."""
    if not shutil.which("nvidia-smi"):
        return []
    r = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,used_memory,process_name",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True)
    procs = []
    if r.returncode == 0 and r.stdout.strip():
        for line in r.stdout.strip().splitlines():
            cols = [c.strip() for c in line.split(",")]
            if len(cols) >= 3:
                try:
                    procs.append((int(cols[0]), int(cols[1]), cols[2]))
                except ValueError:
                    pass
    # compute-apps misses pure-graphics apps on some driver versions; fall back to
    # parsing the full process table if we got nothing.
    if not procs:
        r = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            m = re.search(r"\|\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\S+\s+(.+?)\s+(\d+)MiB\s+\|", line)
            if m:
                procs.append((int(m.group(1)), int(m.group(3)), m.group(2).strip()))
    procs.sort(key=lambda p: p[1], reverse=True)
    return procs


def vram_short_name(raw):
    """Turn a full command line into a short app name.
    'Z:\\...\\StarCitizen.exe' -> 'Star Citizen'; '/opt/brave-bin/brave --type=...' -> 'brave'."""
    raw = (raw or "").strip()
    m = re.search(r"(.*?\.exe)", raw, flags=re.I)        # Windows exe (handles spaces in path)
    path = m.group(1) if m else re.split(r"\s+-", raw, maxsplit=1)[0]  # else cut at first arg
    base = re.split(r"[\\/]", path)[-1]
    base = re.sub(r"\.exe$", "", base, flags=re.I)
    pretty = {"starcitizen": "Star Citizen", "rsi launcher": "RSI Launcher",
              "kwin_wayland": "KWin", "plasmashell": "Plasma", "xwayland": "Xwayland"}
    return pretty.get(base.lower(), base) or raw[:24]


def vram_assessment():
    """Returns a dict with everything the UI needs, or {'ok': False} if no NVIDIA GPU."""
    q = vram_query()
    if q is None:
        return {"ok": False}
    used, total, name = q
    pct = used / total if total else 0
    procs = vram_processes()

    def classify(pname):
        low = pname.lower()
        if "starcitizen" in low:
            return "sc"
        if any(h in low for h in VRAM_GAME_HINTS):
            return "game"
        if any(h in low for h in VRAM_DESKTOP_HINTS):
            return "desktop"
        return "other"

    tagged = [(pid, mem, nm, classify(nm)) for pid, mem, nm in procs]
    # "heavy" = an extra (non-game, non-desktop) app using >10% of total VRAM
    heavy = [p for p in tagged if p[3] == "other" and total and p[1] / total > 0.10]
    sc_running = any(p[3] == "sc" for p in tagged)

    free = total - used
    # Verdict: SC wants ~10 GiB and can grow. Judge by free headroom + heavy extras.
    if sc_running:
        level, verdict = ("ok", "🟢 SC is running")
    elif free >= 7000 and not heavy:
        level, verdict = ("ok", "🟢 Plenty free — safe to launch")
    elif free >= 4500:
        level = "warn"
        verdict = "🟡 Getting tight" + (" — close heavy apps first" if heavy else "")
    else:
        level, verdict = ("crit", "🔴 Free up VRAM before launching SC")
    return {"ok": True, "used": used, "total": total, "free": free, "pct": pct,
            "name": name, "level": level, "verdict": verdict,
            "procs": tagged, "heavy": heavy}


def act_vram(log):
    a = vram_assessment()
    if not a["ok"]:
        log("❌ nvidia-smi not found — VRAM check needs an NVIDIA GPU.")
        return
    log(f"— VRAM: {a['name']} —")
    log(f"  {a['used']/1024:.1f} / {a['total']/1024:.1f} GiB used ({a['pct']*100:.0f}%), "
        f"{a['free']/1024:.1f} GiB free")
    log(f"  {a['verdict']}")
    if a["procs"]:
        tot = a["total"]
        log("  Top GPU processes:")
        for pid, mem, nm, kind in a["procs"][:8]:
            share = mem / tot if tot else 0
            flag = " ⚠️ heavy extra app" if kind == "other" and share > 0.10 else ""
            tag = {"sc": "[SC]", "game": "[game]", "desktop": "[desktop]", "other": ""}[kind]
            log(f"    {mem/1024:5.1f} GiB ({share*100:3.0f}%)  {vram_short_name(nm)} {tag}{flag}")
    if a["heavy"] and not any(p[3] == "sc" for p in a["procs"]):
        names = ", ".join(sorted({vram_short_name(p[2]) for p in a["heavy"]}))
        log(f"  💡 Close these before launching SC: {names}")


# ---------------------------------------------------------------- CLI
def cli():
    ap = argparse.ArgumentParser(description="Idus SC Toolbox (CLI mode)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--kill", action="store_true")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--restart-patch", action="store_true")
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--restore", nargs="?", const="__latest__", metavar="FILE")
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--fix-windows", action="store_true")
    ap.add_argument("--kwin-rule", action="store_true")
    ap.add_argument("--shaders", action="store_true")
    ap.add_argument("--driver-cache", action="store_true")
    ap.add_argument("--starstrings", action="store_true")
    ap.add_argument("--vram", action="store_true")
    ap.add_argument("--quantum", choices=["on", "off"])
    ap.add_argument("--env", default="LIVE")
    ap.add_argument("--screen", default=None, help="main screen: KWin index or name, e.g. HDMI-A-1")
    a = ap.parse_args()
    triggers = [a.status, a.launch, a.kill, a.restart, a.restart_patch, a.backup, a.restore is not None, a.diag,
                a.fix_windows, a.kwin_rule, a.shaders, a.starstrings, a.vram, a.quantum]
    if not any(triggers):
        return False
    if a.status:
        act_status(print, a.env)
    if a.kill:
        act_kill(print)
    if a.launch:
        act_launch(print)
    if a.restart or a.restart_patch:
        act_start_launcher(print, patch=a.restart_patch)
    if a.backup:
        act_backup(print)
    if a.restore is not None:
        act_restore(print, None if a.restore == "__latest__" else a.restore)
    if a.diag:
        act_diag(print, a.env)
    if a.fix_windows:
        act_fix_windows(print, a.screen)
    if a.kwin_rule:
        act_install_kwin_rule(print, a.screen)
    if a.shaders:
        act_shaders(print, a.env, a.driver_cache)
    if a.starstrings:
        act_starstrings(print, a.env)
    if a.vram:
        act_vram(print)
    if a.quantum:
        act_quantum(print, a.quantum == "on")
    return True


# ---------------------------------------------------------------- GUI (GTK4)
def gui():
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, Gio, GLib, Pango  # noqa: E402

    settings = load_settings()
    S = 1.15

    def sc(n):
        return int(round(n * S))

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="se.wolfpack.idussctoolbox",
                             flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

        def do_activate(self):
            Win(self).present()

    class Win(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Idus SC Toolbox")
            self.set_default_size(sc(1120), sc(760))
            try:
                if ICON_FILE.exists():
                    self.set_icon_name("idus-sc-toolbox")
                    from gi.repository import Gdk
                    self._icon_tex = Gdk.Texture.new_from_filename(str(ICON_FILE))
            except Exception:
                pass

            self._buttons = []
            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sc(14))
            for s in ("top", "bottom", "start", "end"):
                getattr(root, f"set_margin_{s}")(sc(16))
            self.set_child(root)

            # Header: icon beside title + version, centred as one unit (saves height in 2 columns)
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sc(16))
            hb.set_halign(Gtk.Align.CENTER)
            if ICON_FILE.exists():
                logo = Gtk.Image.new_from_file(str(ICON_FILE))
                logo.set_pixel_size(sc(85))  # 25% larger than before (was 68)
                hb.append(logo)
            tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sc(8), valign=Gtk.Align.CENTER)
            title = Gtk.Label(label="Idus SC Toolbox", xalign=0)
            title.add_css_class("app-title")
            tb.append(title)
            ver = Gtk.Label(label=f"V{APP_VERSION} · BUILD {BUILD}", xalign=0)
            ver.add_css_class("app-ver")
            tb.append(ver)
            hb.append(tb)
            header = Gtk.CenterBox()
            header.set_center_widget(hb)
            header.set_end_widget(self._launch_button())
            root.append(header)

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sc(8))
            env_lbl = Gtk.Label(label="Environment:")
            env_lbl.add_css_class("env-lbl")
            top.append(env_lbl)
            self.env_box = Gtk.DropDown.new_from_strings(
                [e for e in ENVIRONMENTS if ifind(GAME_ROOT, e)] or ["LIVE"])
            top.append(self.env_box)
            self.env_box.connect("notify::selected", lambda *_: self._refresh_panel())
            top.append(Gtk.Box(hexpand=True))
            refresh = Gtk.Button(label="⟳  Refresh")
            refresh.add_css_class("btn-small")
            refresh.set_tooltip_text("Re-read runners, wineserver, game build, disk, launcher log and audio")
            refresh.connect("clicked", lambda _w: (self._refresh_status(), self._refresh_panel()))
            top.append(refresh)
            self.status = Gtk.Label(label="")
            self.status.add_css_class("pill")
            self.status.set_valign(Gtk.Align.CENTER)
            top.append(self.status)
            root.append(top)

            self.log_buf = Gtk.TextView()
            self.log_buf.set_editable(False)
            self.log_buf.set_monospace(True)
            self.log_buf.add_css_class("logbox")
            scroller = Gtk.ScrolledWindow()
            scroller.set_vexpand(True)
            scroller.add_css_class("logwrap")
            scroller.set_overflow(Gtk.Overflow.HIDDEN)
            scroller.set_child(self.log_buf)

            self._status_panel(root)
            cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sc(14), homogeneous=True)
            cols.set_vexpand(True)
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sc(14))
            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sc(14))
            cols.append(left)
            cols.append(right)
            root.append(cols)
            self._build_groups(left, right)
            scroller.set_min_content_height(sc(120))
            right.append(scroller)  # the log fills the rest of the right column

            # Footer: author + RSI referral (clickable)
            foot = Gtk.Label()
            foot.set_markup(
                'Made by Idus · referral '
                '<a href="https://www.robertsspaceindustries.com/enlist?referral=STAR-2GDF-FDKH">'
                'STAR-2GDF-FDKH</a>'
                ' · <a href="https://ko-fi.com/N4N2QZHKK">☕ Tip me on Ko-fi</a>')
            foot.set_halign(Gtk.Align.CENTER)
            foot.add_css_class("foot")
            foot.connect("activate-link", lambda _l, uri: (
                subprocess.Popen(["xdg-open", uri], start_new_session=True), True)[1])
            root.append(foot)

            self._install_css()
            self._refresh_status()
            self._font_hint()
            self._refresh_panel()
            if not GAME_ROOT.is_dir():
                self.log(f"❌ cannot find {GAME_ROOT} — set SC_PREFIX if the prefix is elsewhere")

            if settings.get("autobackup"):
                today = dt.date.today().isoformat()
                if settings.get("last_autobackup") != today:
                    settings["last_autobackup"] = today
                    save_settings(settings)
                    self.log("· auto-backup on launch …")
                    self._run(lambda lg: act_backup(lg, quiet=True))

        def env(self):
            model = self.env_box.get_model()
            return model.get_string(self.env_box.get_selected()) if model else "LIVE"

        def log(self, text):
            def append():
                b = self.log_buf.get_buffer()
                b.insert(b.get_end_iter(), text + "\n")
                self.log_buf.scroll_to_iter(b.get_end_iter(), 0.0, False, 0, 0)
                return False
            GLib.idle_add(append)

        def _refresh_status(self):
            running = game_running()
            self.status.set_label("●  SC running" if running else "○  SC not running")
            self.status.remove_css_class("pill-idle" if running else "pill-ok")
            self.status.add_css_class("pill-ok" if running else "pill-idle")

        def _font_hint(self):
            """The look needs Saira / IBM Plex Sans / JetBrains Mono (installed by the installer)."""
            try:
                fams = {f.get_name().lower() for f in self.get_pango_context().get_font_map().list_families()}
            except Exception:
                return
            missing = [n for n in ("Saira", "IBM Plex Sans", "JetBrains Mono") if n.lower() not in fams]
            if missing:
                self.log("· fonts missing: " + ", ".join(missing)
                         + " — run install-idus-sc-toolbox.sh again (system fonts used meanwhile)")

        def _run(self, fn, *args):
            for b in self._buttons:
                b.set_sensitive(False)
            self.log(f"— {dt.datetime.now():%H:%M:%S} —")

            def job():
                try:
                    fn(self.log, *args)
                except Exception as e:  # noqa
                    self.log(f"❌ error: {e}")

                def done():
                    for b in self._buttons:
                        b.set_sensitive(True)
                    self._refresh_status()
                    self._refresh_panel()
                    return False
                GLib.idle_add(done)
            threading.Thread(target=job, daemon=True).start()

        def _btn(self, grid, r, c, text, cb, tip="", w=1):
            b = Gtk.Button(label=text)
            b.set_hexpand(True)
            if tip:
                b.set_tooltip_text(tip)
            b.connect("clicked", lambda _w: cb())
            grid.attach(b, c, r, w, 1)
            self._buttons.append(b)
            return b

        def _group(self, root, title):
            """Framed group. "1 · Name → extra" becomes an orange number ring + title (+ muted extra)."""
            frame = Gtk.Frame()
            frame.add_css_class("group")
            num, _, name = title.partition(" · ")
            if not name:
                num, name = "", title
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sc(8))
            head.add_css_class("group-head")
            if num:
                ring = Gtk.Label(label=num)
                ring.add_css_class("group-num")
                ring.set_valign(Gtk.Align.CENTER)
                head.append(ring)
            main, arrow, extra = name.partition("→")
            name_lbl = Gtk.Label(label=main.strip(), xalign=0)
            name_lbl.add_css_class("group-title")
            name_lbl.set_valign(Gtk.Align.CENTER)
            head.append(name_lbl)
            if arrow:
                extra_lbl = Gtk.Label(label="→  " + extra.strip(), xalign=0)
                extra_lbl.add_css_class("group-extra")
                extra_lbl.set_valign(Gtk.Align.CENTER)
                head.append(extra_lbl)
            frame.set_label_widget(head)
            grid = Gtk.Grid(column_spacing=sc(8), row_spacing=sc(8),
                            column_homogeneous=True)
            for s in ("top", "bottom", "start", "end"):
                getattr(grid, f"set_margin_{s}")(sc(10))
            frame.set_child(grid)
            root.append(frame)
            return grid

        def _open(self, path, label, create=False):
            if path is None or (not path.exists() and not create):
                self.log(f"❌ cannot find {label}")
                return
            if create and not path.exists():
                path.write_text("; USER.cfg — created by Idus SC Toolbox\n")
                self.log(f"· created {path}")
            subprocess.Popen([EDITOR, str(path)], start_new_session=True)
            self.log(f"✅ opened {label} in {EDITOR}")

        def _confirm(self, text, on_yes):
            dlg = Gtk.AlertDialog()
            dlg.set_message(text)
            dlg.set_buttons(["Cancel", "Run"])
            dlg.set_default_button(0)
            dlg.set_cancel_button(0)

            def cb(d, res):
                try:
                    if d.choose_finish(res) == 1:
                        on_yes()
                except Exception:
                    pass
            dlg.choose(self, None, cb)

        def _build_groups(self, left, right):
            # 1 · Process
            g = self._group(left, "1 · Close / clean")
            self._btn(g, 0, 0, "💀  Kill SC + launcher + wineserver",
                      lambda: self._run(act_kill))
            self._btn(g, 0, 1, "🔄  Restart launcher",
                      lambda: self._run(act_start_launcher),
                      "Kills everything and runs LUG's sc-launch.sh (normal / play runner)")
            self._btn(g, 1, 0, "🩹  Patch / verify launcher",
                      lambda: self._run(act_start_launcher, True),
                      "Kills everything and runs 'sc-launch.sh patch': the RSI Launcher on your "
                      "PATCH runner, for game updates and Verify. Then 'Restart launcher' to play. "
                      "Needs patch mode in sc-launch.sh — see the README.",
                      w=2)

            # 2 · Config in Kate
            g = self._group(left, "2 · Open config in Kate")
            self._btn(g, 0, 0, "USER.cfg",
                      lambda: self._open(ifind(env_dir(self.env()), "USER.cfg") or env_dir(self.env()) / "USER.cfg",
                                         "USER.cfg", create=True))
            self._btn(g, 0, 1, "attributes.xml",
                      lambda: self._open(profile_file(self.env(), "attributes.xml"), "attributes.xml"))
            self._btn(g, 1, 0, "actionmaps.xml (keybinds)",
                      lambda: self._open(profile_file(self.env(), "actionmaps.xml"), "actionmaps.xml"))
            self._btn(g, 1, 1, "sc-launch.sh (LUG)",
                      lambda: self._open(LAUNCH_SH, "sc-launch.sh"))
            self._btn(g, 2, 0, "📜  Game.log (follow live)",
                      lambda: (subprocess.Popen(
                          ["konsole", "-e", "tail", "-n", "200", "-F",
                           str(ifind(env_dir(self.env()), "Game.log") or env_dir(self.env()) / "Game.log")],
                          start_new_session=True), self.log("✅ following Game.log in Konsole")))
            self._btn(g, 2, 1, "📄  Launcher log (follow live)",
                      self._follow_launcher_log,
                      "RSI Launcher log.log in Konsole — watch updates, Verify and patcher errors live")
            self._btn(g, 3, 0, "📁  Game folder in Dolphin",
                      lambda: subprocess.Popen(["dolphin", str(env_dir(self.env()))], start_new_session=True),
                      w=2)

            # 3 · Backup / restore
            g = self._group(left, f"3 · Backup  →  {BACKUP_DIR}")
            self._btn(g, 0, 0, "💾  Back up now (all environments)",
                      lambda: self._run(act_backup),
                      "user/ folders (keybinds, attributes), USER.cfg, sc-launch.sh. Shaders/logs excluded.")
            self._btn(g, 0, 1, "🔁  Restore latest backup",
                      lambda: self._confirm(
                          "Restore the latest backup?\n\nThe current state is backed up first. "
                          "Keybinds, attributes and USER.cfg get overwritten.",
                          lambda: self._run(act_restore, None, True)),
                      "Takes a safety copy of the current state, then unpacks the latest backup in place.")
            self._btn(g, 1, 0, "📂  Open backup folder",
                      lambda: (BACKUP_DIR.mkdir(parents=True, exist_ok=True),
                               subprocess.Popen(["dolphin", str(BACKUP_DIR)], start_new_session=True)))
            self.autobackup_chk = Gtk.CheckButton(label="Auto-backup on launch (1×/day)")
            self.autobackup_chk.set_active(bool(settings.get("autobackup")))
            self.autobackup_chk.connect("toggled", self._toggle_autobackup)
            g.attach(self.autobackup_chk, 1, 1, 1, 1)
            self.backup_note = Gtk.Label(xalign=0)
            self.backup_note.add_css_class("group-note")
            g.attach(self.backup_note, 0, 2, 2, 1)

            # 4 · Joystick & windows
            g = self._group(right, "4 · Joystick & windows")
            self._btn(g, 0, 0, "🎮  Joystick diagnostics",
                      lambda: self._run(act_diag, self.env()),
                      "Lists VIRPIL devices, flags duplicates and shows what SC expects")
            self._btn(g, 0, 1, "🎯  Fix windows now",
                      lambda: self._run(act_fix_windows, None),
                      "Centres the launcher + moves SC to the main screen (one-shot, via KWin)")
            self._btn(g, 1, 0, "📌  Install KWin rule (permanent)",
                      lambda: self._confirm(
                          "Install a permanent KWin rule that pins launcher + SC to the main screen?\n\n"
                          "kwinrulesrc is backed up first.",
                          lambda: self._run(act_install_kwin_rule, None)),
                      "Experimental – verify in Window Rules afterwards")
            self._btn(g, 1, 1, "⚙️  Open Window Rules",
                      lambda: self._run(act_open_kwin_settings))
            row = Gtk.Box(spacing=sc(10))
            lbl = Gtk.Label(label="Main screen:")
            lbl.add_css_class("env-lbl")
            row.append(lbl)
            self.screens = kde_screens()
            names = [o["name"] for o in self.screens]
            pos = (["only screen"] if len(names) == 1 else ["left", "right"] if len(names) == 2
                   else ["left", "middle", "right"] if len(names) == 3
                   else [f"{i + 1}. from left" for i in range(len(names))])
            labels = [f"{o['name']} · {p} · {o['w']}×{o['h']}" + (" · KDE primary" if o["primary"] else "")
                      for o, p in zip(self.screens, pos)] or ["(no screen info)"]
            self.screen_box = Gtk.DropDown.new_from_strings(labels)
            self.screen_box.set_hexpand(True)
            cur = main_screen()[1]
            if cur in names:
                self.screen_box.set_selected(names.index(cur))
            self.screen_box.set_tooltip_text(
                "Where Fix windows, the KWin rule and ▶ Launch put the launcher and the game. "
                "Default: the screen KDE calls primary.")
            self.screen_box.connect("notify::selected", lambda *_: self._screen_changed())
            row.append(self.screen_box)
            g.attach(row, 0, 2, 2, 1)

            # 5 · Shaders / audio
            g = self._group(right, "5 · Shaders & audio")
            self.drv_chk = Gtk.CheckButton(label="incl. GPU driver cache")
            self._btn(g, 0, 0, "🧹  Clear shader cache", self._shaders_confirm)
            g.attach(self.drv_chk, 1, 0, 1, 1)
            self.q_btn = self._btn(g, 1, 0, "🔊  PipeWire quantum 2048",
                      lambda: self._run(act_quantum, True),
                      "clock.force-quantum 2048 against audio stutter — lasts until PipeWire restarts")
            self._btn(g, 1, 1, "↩️  Reset quantum",
                      lambda: self._run(act_quantum, False))

            # 6 · VRAM
            g = self._group(right, "6 · VRAM")
            meter = Gtk.Box(spacing=sc(10))
            self.vram_bar = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER)
            self.vram_bar.add_css_class("vram-inline")
            self.vram_lbl = Gtk.Label(xalign=1)
            self.vram_lbl.add_css_class("vram-inline-lbl")
            meter.append(self.vram_bar)
            meter.append(self.vram_lbl)
            g.attach(meter, 0, 0, 2, 1)
            self._btn(g, 1, 0, "📊  VRAM check (details)",
                      self._open_vram,
                      "NVIDIA only. Shows GPU memory use with a meter and flags heavy apps "
                      "(e.g. OrcaSlicer) to close before launching SC",
                      w=2)

            # 7 · StarStrings (english global.ini)
            g = self._group(right, "7 · StarStrings")
            self._btn(g, 0, 0, "🌐  Update StarStrings (LIVE)",
                      lambda: self._confirm(
                          "Fetch the latest StarStrings (LIVE) and replace the english global.ini?\n\n"
                          "The old one is renamed to global.ini.BAK-[date] first.",
                          lambda: self._run(act_starstrings, self.env())),
                      "Downloads the latest LIVE release from GitHub, backs up the old global.ini, "
                      "places the new one in data/Localization/english")

        def _launch_button(self):
            """Big ▶ Launch, top right. Checks RSI for a launcher update before starting."""
            self.launch_btn = Gtk.Button(valign=Gtk.Align.CENTER)
            self.launch_btn.add_css_class("launch")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sc(1))
            big = Gtk.Label(label="▶  LAUNCH")
            big.add_css_class("launch-big")
            self.launch_sub = Gtk.Label(label="checks for updates first")
            self.launch_sub.add_css_class("launch-sub")
            box.append(big)
            box.append(self.launch_sub)
            self.launch_btn.set_child(box)
            self.launch_btn.set_tooltip_text(
                "Starts the RSI Launcher. Checks RSI for a newer launcher first — if one is out, "
                "you can start in patch mode instead. Watches the launcher log while it runs.")
            self.launch_btn.connect("clicked", lambda _w: self._launch_clicked())
            self._buttons.append(self.launch_btn)
            return self.launch_btn

        def _launch_clicked(self):
            if game_running():
                self._confirm("Star Citizen is running.\n\nLaunch closes it first. Continue?",
                              self._launch_check)
            else:
                self._launch_check()

        def _launch_check(self):
            self.launch_sub.set_label("checking RSI …")
            for b in self._buttons:
                b.set_sensitive(False)

            def job():
                inst = installed_launcher_version()
                latest, err = latest_launcher_version(max_age=0)
                GLib.idle_add(self._launch_decide, inst, latest, err)
            threading.Thread(target=job, daemon=True).start()

        def _launch_decide(self, inst, latest, err):
            for b in self._buttons:
                b.set_sensitive(True)
            self.launch_sub.set_label("checks for updates first")
            if latest and inst and vtuple(latest) > vtuple(inst):
                patch_ok = launch_sh_has_patch()
                dlg = Gtk.AlertDialog()
                dlg.set_message(f"RSI Launcher {latest} is out — you have {inst}.")
                dlg.set_detail(
                    "The launcher updates itself when it starts. That has failed on the play runner "
                    "before, so patch mode (the patch runner) is the safer choice. Afterwards, "
                    "click ▶ Launch again to play." if patch_ok else
                    "The launcher updates itself when it starts. sc-launch.sh has no patch mode, "
                    "so it starts on the normal runner.")
                dlg.set_buttons(["Cancel", "Launch anyway", "🩹  Patch mode"] if patch_ok
                                else ["Cancel", "Launch"])
                dlg.set_default_button(2 if patch_ok else 1)
                dlg.set_cancel_button(0)

                def cb(d, res):
                    try:
                        choice = d.choose_finish(res)
                    except Exception:
                        return
                    if choice == 1:
                        self._launch(False)
                    elif choice == 2:
                        self._launch(True)
                dlg.choose(self, None, cb)
            else:
                if latest:
                    self.log(f"✅ RSI Launcher {inst or '?'} is up to date")
                else:
                    self.log(f"· launcher update check failed ({err}) — starting normally")
                self._launch(False)
            return False

        def _launch(self, patch):
            def go(lg):
                act_start_launcher(lg, patch)
                if not patch:
                    self._watch_launcher()
            self._run(go)

        def _watch_launcher(self):
            """Follow the launcher log while it runs; offer patch mode if it starts patching
            the game on the play runner, or loops on verify."""
            if getattr(self, "_watching", False):
                return
            self._watching = True

            def job():
                path = launcher_log_file()
                pos = path.stat().st_size if path else 0
                asked_patch = asked_loop = told_update = False
                t0 = time.time()
                while True:
                    time.sleep(3)
                    path = path or launcher_log_file()
                    if path and path.exists():
                        size = path.stat().st_size
                        if size < pos:  # log rotated
                            pos = 0
                        if size > pos:
                            with open(path, "rb") as fh:
                                fh.seek(pos)
                                chunk = fh.read().decode(errors="ignore")
                            pos = size
                            if not asked_loop and "did not converge" in chunk:
                                asked_loop = True
                                GLib.idle_add(self._watch_alert, "loop")
                            elif not asked_patch and ("[CigDataPatcher]" in chunk or "installer@" in chunk):
                                asked_patch = True
                                GLib.idle_add(self._watch_alert, "patch")
                            if not told_update and ("Found version" in chunk or "Downloading update" in chunk):
                                told_update = True
                                self.log("⚠️ the launcher is downloading a launcher update (play runner)")
                    if time.time() - t0 > 30 and subprocess.run(
                            ["pgrep", "-f", "RSI Launcher"], capture_output=True).returncode != 0:
                        break
                self._watching = False
            threading.Thread(target=job, daemon=True).start()

        def _watch_alert(self, kind):
            if kind == "loop":
                self.log("❌ launcher: verify loop (“did not converge”) on the play runner")
                msg = "Verify loop — the launcher can't finish checking Data.p4k."
                det = ("This is what happened on the play runner before. Switch to patch mode? "
                       "The launcher restarts on the patch runner and finishes there.")
            else:
                self.log("⚠️ launcher: started patching the game on the play runner")
                msg = "The launcher started patching the game on the PLAY runner."
                det = ("Patching has failed on the play runner before. Switch to patch mode now, "
                       "or keep going — the Toolbox warns you if it starts to loop.")
            if not launch_sh_has_patch():
                return False
            dlg = Gtk.AlertDialog()
            dlg.set_message(msg)
            dlg.set_detail(det)
            dlg.set_buttons(["Keep going", "🩹  Switch to patch mode"])
            dlg.set_default_button(1)
            dlg.set_cancel_button(0)

            def cb(d, res):
                try:
                    if d.choose_finish(res) == 1:
                        self._run(act_start_launcher, True)
                except Exception:
                    pass
            dlg.choose(self, None, cb)
            return False

        def _screen_changed(self):
            i = self.screen_box.get_selected()
            if not self.screens or i >= len(self.screens):
                return
            name = self.screens[i]["name"]
            s = load_settings()
            if s.get("main_screen_name") == name:
                return
            s["main_screen_name"] = name
            save_settings(s)
            self.log(f"✅ main screen: {name}")
            rc = Path.home() / ".config/kwinrulesrc"
            if rc.is_file() and "SC Toolbox" in rc.read_text(errors="ignore"):
                self.log("   updating the permanent KWin rule to the new screen …")
                self._run(act_install_kwin_rule, None)

        def _follow_launcher_log(self):
            f = launcher_log_file()
            if not f:
                self.log("❌ no RSI Launcher log yet — start the launcher once")
                return
            subprocess.Popen(["konsole", "-e", "tail", "-n", "200", "-F", str(f)], start_new_session=True)
            self.log("✅ following the launcher log in Konsole")

        def _status_panel(self, root):
            """Read-only status tiles at the top (4 per row)."""
            frame = Gtk.Frame()
            frame.add_css_class("group")
            frame.add_css_class("status-panel")
            head = Gtk.Box(spacing=sc(8))
            head.add_css_class("group-head")
            dot = Gtk.Label(label="●")
            dot.add_css_class("head-dot")
            head.append(dot)
            title = Gtk.Label(label="Status", xalign=0)
            title.add_css_class("group-title")
            head.append(title)
            frame.set_label_widget(head)
            grid = Gtk.Grid(column_spacing=sc(8), row_spacing=sc(8), column_homogeneous=True)
            for s in ("top", "bottom", "start", "end"):
                getattr(grid, f"set_margin_{s}")(sc(10))
            frame.set_child(grid)
            self.tiles = {}
            keys = ["play", "patch", "wine", "game", "launcher", "llog", "disk", "audio"]
            for i, key in enumerate(keys):
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sc(3))
                box.add_css_class("tile")
                lab = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
                lab.add_css_class("tile-label")
                val = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
                val.add_css_class("tile-value")
                box.append(lab)
                box.append(val)
                bar = None
                if key == "disk":
                    bar = Gtk.ProgressBar()
                    bar.add_css_class("tile-bar")
                    box.append(bar)
                row = Gtk.Box(spacing=sc(6))
                d = Gtk.Label(label="●")
                d.add_css_class("dot")
                det = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
                det.add_css_class("tile-detail")
                row.append(d)
                row.append(det)
                box.append(row)
                grid.attach(box, i % 4, i // 4, 1, 1)
                self.tiles[key] = (box, lab, val, d, det, bar)
            root.append(frame)

        def _refresh_panel(self):
            env = self.env()

            def job():
                try:
                    tiles = status_snapshot(env)
                except Exception as e:  # noqa
                    tiles = [dict(key="play", label="STATUS", value="error", detail=str(e), state="error")]
                q = vram_query()
                backups = list_backups()
                GLib.idle_add(self._apply_panel, tiles, q, backups)
            threading.Thread(target=job, daemon=True).start()

        def _apply_panel(self, tiles, q, backups):
            for t in tiles:
                if t["key"] not in self.tiles:
                    continue
                box, lab, val, d, det, bar = self.tiles[t["key"]]
                lab.set_label(t["label"])
                val.set_label(t["value"])
                det.set_label(t["detail"])
                box.set_tooltip_text(f"{t['value']}\n{t['detail']}")
                for s in ("ok", "warn", "error", "idle"):
                    d.remove_css_class(f"dot-{s}")
                d.add_css_class(f"dot-{t['state']}")
                if bar is not None:
                    bar.set_fraction(t.get("frac", 0.0))
                    bar.remove_css_class("level-warn")
                    if t["state"] == "warn":
                        bar.add_css_class("level-warn")
            # launch button hint follows the launcher tile
            lt = next((t for t in tiles if t["key"] == "launcher"), None)
            if lt and lt["state"] == "warn":
                self.launch_sub.set_label("launcher update out → 🩹 offered")
            else:
                self.launch_sub.set_label("checks for updates first")
            # quantum button shows when 2048 is active
            audio = next((t for t in tiles if t["key"] == "audio"), None)
            if audio and audio["state"] == "ok":
                self.q_btn.add_css_class("active")
            else:
                self.q_btn.remove_css_class("active")
            # inline VRAM meter
            for s in ("ok", "warn", "crit"):
                self.vram_bar.remove_css_class(f"vram-{s}")
            if q:
                used, total, _name = q
                free = total - used
                level = "ok" if game_running() or free >= 7000 else ("warn" if free >= 4500 else "crit")
                self.vram_bar.set_fraction(used / total if total else 0)
                self.vram_bar.add_css_class(f"vram-{level}")
                self.vram_lbl.set_label(f"{used / 1024:.1f} / {total / 1024:.1f} GiB")
            else:
                self.vram_bar.set_fraction(0)
                self.vram_lbl.set_label("NVIDIA only")
            # backup note
            if backups:
                ts = dt.datetime.fromtimestamp(backups[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                self.backup_note.set_label(f"Last backup {ts} · {len(backups)} kept (max {KEEP_BACKUPS})")
            else:
                self.backup_note.set_label("No backups yet")
            return False

        def _open_vram(self):
            win = Gtk.Window(title="VRAM Check")
            win.set_transient_for(self)
            win.set_default_size(sc(460), sc(440))
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sc(12))
            for s in ("top", "bottom", "start", "end"):
                getattr(box, f"set_margin_{s}")(sc(16))
            win.set_child(box)

            gpu_lbl = Gtk.Label(xalign=0)
            gpu_lbl.add_css_class("vram-gpu")
            box.append(gpu_lbl)

            bar = Gtk.ProgressBar()
            bar.set_show_text(True)
            box.append(bar)

            verdict = Gtk.Label(xalign=0)
            verdict.add_css_class("vram-verdict")
            box.append(verdict)

            sep = Gtk.Separator()
            box.append(sep)

            proc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sc(6))
            scroller = Gtk.ScrolledWindow()
            scroller.set_vexpand(True)
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)  # no horizontal growth
            scroller.set_child(proc_box)
            box.append(scroller)

            btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sc(8))
            refresh = Gtk.Button(label="↻  Refresh")
            refresh.set_hexpand(True)
            close = Gtk.Button(label="Close")
            close.set_hexpand(True)
            close.connect("clicked", lambda _b: win.close())
            btns.append(refresh)
            btns.append(close)
            box.append(btns)

            note = Gtk.Label(label="NVIDIA only — reads nvidia-smi (any NVIDIA card).",
                             xalign=0.5)
            note.add_css_class("vram-note")
            box.append(note)

            def clear(container):
                c = container.get_first_child()
                while c:
                    nxt = c.get_next_sibling()
                    container.remove(c)
                    c = nxt

            def refresh_vram(*_):
                a = vram_assessment()
                clear(proc_box)
                if not a["ok"]:
                    gpu_lbl.set_text("No NVIDIA GPU detected")
                    bar.set_fraction(0)
                    bar.set_text("nvidia-smi not available")
                    verdict.set_text("VRAM check works on NVIDIA GPUs only.")
                    verdict.remove_css_class("vram-ok")
                    verdict.remove_css_class("vram-warn")
                    verdict.add_css_class("vram-crit")
                    return
                gpu_lbl.set_text(a["name"])
                bar.set_fraction(min(a["pct"], 1.0))
                bar.set_text(f"{a['used']/1024:.1f} / {a['total']/1024:.1f} GiB  "
                             f"({a['pct']*100:.0f}%) · {a['free']/1024:.1f} GiB free")
                for cls in ("vram-ok", "vram-warn", "vram-crit"):
                    bar.remove_css_class(cls)
                    verdict.remove_css_class(cls)
                css = {"ok": "vram-ok", "warn": "vram-warn", "crit": "vram-crit"}[a["level"]]
                bar.add_css_class(css)
                verdict.add_css_class(css)
                verdict.set_text(a["verdict"])
                if a["procs"]:
                    total = a["total"]
                    for pid, mem, nm, kind in a["procs"][:10]:
                        share = (mem / total) if total else 0
                        # SC stays green (expected big user). Others: >20% red, >10% amber.
                        if kind == "sc":
                            lvl, name_cls, warn = "vram-sc", "vram-sc", False
                        elif kind == "other" and share > 0.20:
                            lvl, name_cls, warn = "vram-crit-bar", "vram-crit", True
                        elif kind == "other" and share > 0.10:
                            lvl, name_cls, warn = "vram-heavy-bar", "vram-heavy", True
                        else:
                            lvl, name_cls, warn = "vram-plain-bar", None, False
                        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sc(8))

                        # Warn icon in its own fixed slot — only shown when amber/red,
                        # always aligned, never swallowed by the name ellipsis.
                        warn_lbl = Gtk.Label(label="⚠️" if warn else "", xalign=0.5)
                        warn_lbl.set_size_request(sc(18), -1)
                        row.append(warn_lbl)

                        name = Gtk.Label(label=vram_short_name(nm), xalign=0)
                        name.set_ellipsize(Pango.EllipsizeMode.END)
                        name.set_size_request(sc(118), -1)
                        if name_cls:
                            name.add_css_class(name_cls)
                        row.append(name)

                        pbar = Gtk.ProgressBar()
                        pbar.set_fraction(min(share, 1.0))
                        pbar.set_valign(Gtk.Align.CENTER)
                        pbar.set_hexpand(True)
                        pbar.add_css_class("vram-rowbar")
                        pbar.add_css_class(lvl)
                        row.append(pbar)

                        size = Gtk.Label(label=f"{mem/1024:.1f} GiB", xalign=1)
                        size.add_css_class("vram-size")
                        size.set_size_request(sc(60), -1)
                        row.append(size)

                        proc_box.append(row)
                else:
                    proc_box.append(Gtk.Label(label="No GPU processes reported.", xalign=0))

            refresh.connect("clicked", refresh_vram)
            refresh_vram()
            win.present()

        def _toggle_autobackup(self, chk):
            settings["autobackup"] = chk.get_active()
            save_settings(settings)
            self.log(f"· auto-backup on launch: {'on' if chk.get_active() else 'off'}")

        def _shaders_confirm(self):
            tg = shader_targets(self.env(), self.drv_chk.get_active())
            if not tg:
                self.log("· no shader cache found")
                return
            txt = "Deleting:\n\n" + "\n".join(f"{t}  ({human(dir_size(t))})" for t in tg)
            self._confirm(txt, lambda: self._run(act_shaders, self.env(), self.drv_chk.get_active()))

        def _install_css(self):
            from gi.repository import Gtk as _Gtk
            f = int(round(13 * S))
            # Idus visual style (same tokens as the Idus web pages): dark blue-grey ground,
            # amber title, orange only for markers/details, blue-grey soft text.
            C = dict(bg="#16181d", surface="#1e222b", deep="#0f1115", line="#2a2f3a",
                     fg="#e8eaed", muted="#8a93a3", soft="#9fb4d4", title="#ffd9a0",
                     accent="#ff9a3c", ok="#46d160", warn="#ffb13c", error="#e0525b",
                     btn="#262b36", btn_hover="#2f3644", log="#c7d1df", dim="#5a6270")
            DISPLAY = '"Saira", "Segoe UI", sans-serif'
            BODY = '"IBM Plex Sans", "Noto Sans", sans-serif'
            MONO = '"JetBrains Mono", "DejaVu Sans Mono", monospace'
            css = f"""
            window {{ background-color: {C['bg']}; color: {C['fg']}; font-family: {BODY}; font-size: {f}px; }}
            .app-title {{ font-family: {DISPLAY}; font-size: {int(30*S)}px; font-weight: 700;
                          letter-spacing: 0.3px; color: {C['title']}; padding-bottom: {sc(2)}px; }}
            .app-ver {{ font-family: {MONO}; font-size: {int(13*S)}px; font-weight: 600;
                        letter-spacing: 1px; color: {C['soft']}; }}
            .env-lbl {{ color: {C['muted']}; }}
            .pill {{ font-size: {int(12*S)}px; padding: {sc(3)}px {sc(10)}px; border-radius: 999px;
                     border: 1px solid {C['line']}; background-color: {C['surface']}; }}
            .pill-ok {{ color: {C['ok']}; border-color: {C['ok']}; }}
            .pill-idle {{ color: {C['muted']}; }}
            frame.group {{ background-color: {C['surface']}; border-radius: {sc(12)}px;
                           border: 1px solid {C['line']}; }}
            .group-head {{ margin: {sc(8)}px {sc(4)}px {sc(2)}px {sc(4)}px; }}
            .group-num {{ font-family: {MONO}; font-weight: 600; font-size: {int(12*S)}px; color: {C['accent']};
                          border: 1.5px solid {C['accent']}; border-radius: 999px;
                          min-width: {sc(22)}px; min-height: {sc(22)}px; }}
            .group-title {{ font-size: {int(14*S)}px; font-weight: 600; color: {C['fg']}; }}
            .group-extra {{ font-family: {MONO}; font-size: {int(11.5*S)}px; color: {C['muted']}; }}
            button {{ background-color: {C['btn']}; background-image: none; box-shadow: none; text-shadow: none;
                      color: {C['fg']}; border-radius: {sc(8)}px; border: 1px solid {C['line']};
                      padding: {sc(9)}px {sc(10)}px; }}
            button:hover {{ background-color: {C['btn_hover']}; }}
            button:active {{ background-color: {C['line']}; }}
            button:focus-visible {{ outline: 2px solid {C['accent']}; outline-offset: 2px; }}
            button:disabled {{ color: {C['dim']}; }}
            checkbutton {{ color: {C['soft']}; font-size: {int(12*S)}px; }}
            checkbutton check {{ background-color: {C['deep']}; background-image: none;
                                 border: 1px solid {C['line']}; box-shadow: none; }}
            checkbutton check:checked {{ background-color: {C['accent']}; border-color: {C['accent']};
                                         color: {C['bg']}; }}
            popover > contents, popover > arrow {{ background-color: {C['surface']}; color: {C['fg']};
                                                   border-color: {C['line']}; }}
            popover row:hover, popover row:selected {{ background-color: {C['btn_hover']}; }}
            .logwrap {{ border: 1px solid {C['line']}; border-radius: {sc(8)}px; background-color: {C['deep']}; }}
            textview.logbox, textview.logbox text {{ background-color: {C['deep']}; color: {C['log']}; }}
            textview.logbox {{ font-family: {MONO}; font-size: {int(12*S)}px; padding: {sc(8)}px; }}
            textview.logbox text selection {{ background-color: alpha({C['accent']}, 0.35); }}
            .foot {{ font-size: {int(11*S)}px; color: {C['muted']}; margin-top: {sc(4)}px; }}
            .foot link, .foot link:visited, .foot link:hover {{ color: {C['accent']}; }}
            .foot link:focus-visible {{ outline: 1px solid {C['accent']}; outline-offset: 1px; }}
            button.launch {{ background-color: {C['accent']}; color: {C['bg']}; border: 1px solid {C['accent']};
                             border-radius: {sc(12)}px; padding: {sc(10)}px {sc(26)}px; }}
            button.launch:hover {{ background-color: #ffad5c; }}
            button.launch:disabled {{ background-color: {C['btn']}; border-color: {C['line']}; color: {C['dim']}; }}
            .launch-big {{ font-family: {DISPLAY}; font-weight: 700; font-size: {int(20*S)}px; letter-spacing: 1px; }}
            .launch-sub {{ font-family: {MONO}; font-size: {int(10*S)}px; }}
            window.dialog, window.messagedialog, messagedialog {{ background-color: {C['surface']}; color: {C['fg']}; }}
            messagedialog .titlebar, window.messagedialog .titlebar {{ background-color: {C['surface']}; }}
            button.suggested-action, button.default {{ background-color: {C['accent']}; color: {C['bg']};
                                                     border-color: {C['accent']}; }}
            .status-panel .head-dot {{ color: {C['accent']}; font-size: {int(11*S)}px; }}
            .tile {{ background-color: {C['deep']}; border: 1px solid {C['line']}; border-radius: {sc(10)}px;
                     padding: {sc(8)}px {sc(10)}px; }}
            .tile-label {{ font-family: {MONO}; font-size: {int(10*S)}px; font-weight: 500;
                           letter-spacing: 1px; color: {C['muted']}; }}
            .tile-value {{ font-size: {int(14*S)}px; font-weight: 600; color: {C['fg']}; }}
            .tile-detail {{ font-size: {int(11*S)}px; color: {C['soft']}; }}
            .dot {{ font-size: {int(10*S)}px; }}
            .dot-ok {{ color: {C['ok']}; }}
            .dot-warn {{ color: {C['warn']}; }}
            .dot-error {{ color: {C['error']}; }}
            .dot-idle {{ color: {C['dim']}; }}
            progressbar.tile-bar trough {{ min-height: {sc(6)}px; border-radius: {sc(3)}px; background-color: {C['line']}; }}
            progressbar.tile-bar progress {{ min-height: {sc(6)}px; border-radius: {sc(3)}px; background-color: {C['soft']}; }}
            progressbar.tile-bar.level-warn progress {{ background-color: {C['warn']}; }}
            .group-note {{ font-size: {int(11.5*S)}px; color: {C['muted']}; }}
            button.btn-small {{ padding: {sc(4)}px {sc(10)}px; font-size: {int(12*S)}px; }}
            button.active {{ border-color: {C['accent']}; color: {C['title']}; }}
            progressbar.vram-inline trough {{ min-height: {sc(10)}px; border-radius: {sc(5)}px;
                                              background-color: {C['deep']}; border: 1px solid {C['line']}; }}
            progressbar.vram-inline progress {{ min-height: {sc(10)}px; border-radius: {sc(5)}px; }}
            .vram-inline-lbl {{ font-family: {MONO}; font-size: {int(12*S)}px; color: {C['soft']}; }}
            .vram-gpu {{ font-size: {int(15*S)}px; font-weight: 700; color: {C['fg']}; }}
            .vram-verdict {{ font-size: {int(14*S)}px; font-weight: 700; }}
            progressbar text {{ color: {C['fg']}; font-size: {int(12*S)}px; }}
            progressbar.vram-ok progress   {{ background-color: {C['ok']}; }}
            progressbar.vram-warn progress {{ background-color: {C['warn']}; }}
            progressbar.vram-crit progress {{ background-color: {C['error']}; }}
            .vram-ok   {{ color: {C['ok']}; }}
            .vram-warn {{ color: {C['warn']}; }}
            .vram-crit {{ color: {C['error']}; }}
            .vram-size {{ font-family: {MONO}; color: {C['soft']}; font-size: {int(12*S)}px; }}
            .vram-heavy {{ color: {C['warn']}; font-weight: 700; }}
            .vram-sc {{ color: {C['ok']}; }}
            .vram-note {{ font-size: {int(10*S)}px; color: #6b7280; }}
            progressbar.vram-rowbar {{ min-height: {sc(6)}px; }}
            progressbar.vram-rowbar trough {{ min-height: {sc(6)}px; border-radius: {sc(3)}px;
                                              background-color: {C['deep']}; }}
            progressbar.vram-rowbar progress {{ min-height: {sc(6)}px; border-radius: {sc(3)}px; }}
            progressbar.vram-rowbar.vram-sc progress {{ background-color: {C['ok']}; }}
            progressbar.vram-rowbar.vram-heavy-bar progress {{ background-color: {C['warn']}; }}
            progressbar.vram-rowbar.vram-crit-bar progress {{ background-color: {C['error']}; }}
            progressbar.vram-rowbar.vram-plain-bar progress {{ background-color: {C['dim']}; }}
            """
            provider = _Gtk.CssProvider()
            provider.load_from_data(css.encode())
            _Gtk.StyleContext.add_provider_for_display(
                self.get_display(), provider, _Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    App().run([sys.argv[0]])


if __name__ == "__main__":
    if not cli():
        gui()

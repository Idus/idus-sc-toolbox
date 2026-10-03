#!/usr/bin/env python3
"""
Idus SC Toolbox — a small companion to LUG Helper for Star Citizen on Linux (KDE Plasma 6).
GTK4. Self-contained folder: app + icon + settings live together, so one folder backup
captures everything.

GUI launches with no arguments. Same actions via CLI (KDE shortcuts / Stream Deck):
  idus-sc-toolbox --kill            Close SC + launcher + wineserver
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
  --screen N                        main-screen index for the window fixes (default from settings)

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

DEFAULT_SETTINGS = {"autobackup": False, "last_autobackup": "", "main_screen": 0}


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


def launch_sh_vars() -> dict:
    out = {}
    if LAUNCH_SH.is_file():
        for line in LAUNCH_SH.read_text(errors="ignore").splitlines():
            m = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)="?([^"#]*)"?', line)
            if m:
                out[m.group(1)] = os.path.expandvars(m.group(2).strip()).replace("~", str(Path.home()), 1)
    return out


def wineserver_bin() -> str:
    wp = launch_sh_vars().get("wine_path")
    if wp and (Path(wp) / "wineserver").exists():
        return str(Path(wp) / "wineserver")
    return shutil.which("wineserver") or "wineserver"


def game_running() -> bool:
    return subprocess.run(["pgrep", "-f", "StarCitizen.exe"], capture_output=True).returncode == 0


# ---------------------------------------------------------------- action: process
def act_kill(log):
    for pat in PROCESS_PATTERNS:
        r = subprocess.run(["pkill", "-9", "-f", pat], capture_output=True)
        log(("✅ killed " if r.returncode == 0 else "· nothing matched ") + pat)
    ws = wineserver_bin()
    env = dict(os.environ, WINEPREFIX=str(PREFIX))
    r = subprocess.run([ws, "-k"], env=env, capture_output=True, text=True)
    log(f"✅ wineserver -k ({ws})" if r.returncode == 0 else f"⏳ wineserver -k: {r.stderr.strip() or 'nothing to kill'}")
    time.sleep(1)
    left = subprocess.run(["pgrep", "-af", "\\.exe"], capture_output=True, text=True).stdout.strip()
    log("✅ no .exe processes left" if not left else f"❌ still running:\n{left}")


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
def _main_screen_arg(screen=None) -> int:
    if screen is None:
        screen = load_settings().get("main_screen", 0)
    try:
        return int(screen)
    except (TypeError, ValueError):
        return 0


KWIN_FIX_JS = r"""
function geoForScreen(idx) {
    try {
        const scr = workspace.screens;
        if (scr && scr.length) {
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


def act_fix_windows(log, screen=None):
    idx = _main_screen_arg(screen)
    js_src = KWIN_FIX_JS.replace("__SCREEN__", str(idx))
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
    log(f"✅ launcher centred + SC moved to screen {idx} ('Wine System Tray' minimised)")


def act_install_kwin_rule(log, screen=None):
    """Writes a permanent KWin rule that forces the RSI Launcher and Star Citizen
    onto the main screen. Backs up kwinrulesrc first and is idempotent."""
    idx = _main_screen_arg(screen)
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
    log(f"✅ KWin rule installed: pins RSI Launcher + Star Citizen to screen {idx}")
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
def shader_targets(env: str, include_driver: bool):
    targets = []
    sc_local = ifind(APPDATA_LOCAL, "Star Citizen")
    if sc_local:
        targets += [p for p in sc_local.iterdir()
                    if p.is_dir() and (p.name.lower().startswith("sc-alpha") or p.name.lower() == "shaders")]
    sec = ifind(env_dir(env), "user", "client", "0", "shaders")
    if sec:
        targets.append(sec)
    if include_driver:
        v = launch_sh_vars()
        for key in ["__GL_SHADER_DISK_CACHE_PATH", "MESA_SHADER_CACHE_DIR", "DXVK_STATE_CACHE_PATH"]:
            if v.get(key) and Path(v[key]).is_dir():
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


# ---------------------------------------------------------------- CLI
def cli():
    ap = argparse.ArgumentParser(description="Idus SC Toolbox (CLI mode)")
    ap.add_argument("--kill", action="store_true")
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--restore", nargs="?", const="__latest__", metavar="FILE")
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--fix-windows", action="store_true")
    ap.add_argument("--kwin-rule", action="store_true")
    ap.add_argument("--shaders", action="store_true")
    ap.add_argument("--driver-cache", action="store_true")
    ap.add_argument("--starstrings", action="store_true")
    ap.add_argument("--quantum", choices=["on", "off"])
    ap.add_argument("--env", default="LIVE")
    ap.add_argument("--screen", type=int, default=None)
    a = ap.parse_args()
    triggers = [a.kill, a.backup, a.restore is not None, a.diag, a.fix_windows,
                a.kwin_rule, a.shaders, a.starstrings, a.quantum]
    if not any(triggers):
        return False
    if a.kill:
        act_kill(print)
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
    if a.quantum:
        act_quantum(print, a.quantum == "on")
    return True


# ---------------------------------------------------------------- GUI (GTK4)
def gui():
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, Gio, GLib  # noqa: E402

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
            self.set_default_size(sc(640), sc(800))
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

            hb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sc(4))
            hb.set_halign(Gtk.Align.CENTER)
            if ICON_FILE.exists():
                logo = Gtk.Image.new_from_file(str(ICON_FILE))
                logo.set_pixel_size(sc(85))  # 25% larger than before (was 68)
                hb.append(logo)
            title = Gtk.Label(label="Idus SC Toolbox", xalign=0.5)
            title.add_css_class("app-title")
            hb.append(title)
            root.append(hb)

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sc(8))
            top.append(Gtk.Label(label="Environment:"))
            self.env_box = Gtk.DropDown.new_from_strings(
                [e for e in ENVIRONMENTS if ifind(GAME_ROOT, e)] or ["LIVE"])
            top.append(self.env_box)
            self.status = Gtk.Label(label="", xalign=1, hexpand=True)
            top.append(self.status)
            root.append(top)

            self.log_buf = Gtk.TextView()
            self.log_buf.set_editable(False)
            self.log_buf.set_monospace(True)
            self.log_buf.add_css_class("logbox")
            scroller = Gtk.ScrolledWindow()
            scroller.set_vexpand(True)
            scroller.set_child(self.log_buf)

            self._build_groups(root)
            root.append(scroller)

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
            self.status.set_label("🟢 SC running" if game_running() else "⚪ SC not running")

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
                    return False
                GLib.idle_add(done)
            threading.Thread(target=job, daemon=True).start()

        def _btn(self, grid, r, c, text, cb, tip=""):
            b = Gtk.Button(label=text)
            b.set_hexpand(True)
            if tip:
                b.set_tooltip_text(tip)
            b.connect("clicked", lambda _w: cb())
            grid.attach(b, c, r, 1, 1)
            self._buttons.append(b)
            return b

        def _group(self, root, title):
            frame = Gtk.Frame(label=title)
            frame.add_css_class("group")
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

        def _build_groups(self, root):
            # 1 · Process
            g = self._group(root, "1 · Close / clean")
            self._btn(g, 0, 0, "💀  Kill SC + launcher + wineserver",
                      lambda: self._run(act_kill))
            self._btn(g, 0, 1, "🔄  Restart launcher",
                      lambda: self._run(lambda lg: (act_kill(lg),
                                                    subprocess.Popen(["bash", str(LAUNCH_SH)],
                                                                     start_new_session=True),
                                                    lg("✅ started sc-launch.sh"))),
                      "Kills everything and runs LUG's sc-launch.sh")

            # 2 · Config in Kate
            g = self._group(root, "2 · Open config in Kate")
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
            self._btn(g, 2, 1, "📁  Game folder in Dolphin",
                      lambda: subprocess.Popen(["dolphin", str(env_dir(self.env()))], start_new_session=True))

            # 3 · Backup / restore
            g = self._group(root, f"3 · Backup  →  {BACKUP_DIR}")
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

            # 4 · Joystick & windows
            g = self._group(root, "4 · Joystick & windows")
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

            # 5 · Shaders / audio
            g = self._group(root, "5 · Shaders & audio")
            self.drv_chk = Gtk.CheckButton(label="incl. GPU driver cache")
            self._btn(g, 0, 0, "🧹  Clear shader cache", self._shaders_confirm)
            g.attach(self.drv_chk, 1, 0, 1, 1)
            self._btn(g, 1, 0, "🔊  PipeWire quantum 2048",
                      lambda: self._run(act_quantum, True),
                      "clock.force-quantum 2048 against audio stutter — lasts until PipeWire restarts")
            self._btn(g, 1, 1, "↩️  Reset quantum",
                      lambda: self._run(act_quantum, False))

            # 6 · StarStrings (english global.ini)
            g = self._group(root, "6 · StarStrings")
            self._btn(g, 0, 0, "🌐  Update StarStrings (LIVE)",
                      lambda: self._confirm(
                          "Fetch the latest StarStrings (LIVE) and replace the english global.ini?\n\n"
                          "The old one is renamed to global.ini.BAK-[date] first.",
                          lambda: self._run(act_starstrings, self.env())),
                      "Downloads the latest LIVE release from GitHub, backs up the old global.ini, "
                      "places the new one in data/Localization/english")

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
            css = f"""
            window {{ background-color: #16181d; color: #e8eaed; font-size: {f}px; }}
            .app-title {{ font-size: {int(28*S)}px; font-weight: 800; letter-spacing: 0.5px; color: #ffd9a0; }}
            .foot {{ font-size: {int(11*S)}px; color: #8a93a3; margin-top: {sc(4)}px; }}
            .foot a {{ color: #ff9a3c; text-decoration: none; }}
            frame.group > label {{ font-size: {int(13*S)}px; font-weight: 700; color: #8a93a3;
                                   margin-left: {sc(4)}px; }}
            frame.group {{ background-color: #1e222b; border-radius: {sc(12)}px;
                           border: 1px solid #2a2f3a; }}
            button {{ background-color: #262b36; color: #e8eaed; border-radius: {sc(8)}px;
                      border: 1px solid #333a47; padding: {sc(9)}px {sc(10)}px; }}
            button:hover {{ background-color: #2f3644; }}
            button:disabled {{ color: #5a6270; }}
            .logbox {{ background-color: #0f1115; color: #c7d1df; padding: {sc(8)}px;
                       border-radius: {sc(8)}px; }}
            checkbutton {{ color: #9fb4d4; font-size: {int(12*S)}px; }}
            """
            provider = _Gtk.CssProvider()
            provider.load_from_data(css.encode())
            _Gtk.StyleContext.add_provider_for_display(
                self.get_display(), provider, _Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    App().run([sys.argv[0]])


if __name__ == "__main__":
    if not cli():
        gui()

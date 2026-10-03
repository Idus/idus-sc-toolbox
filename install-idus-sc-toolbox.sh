#!/usr/bin/env bash
# Installs Idus SC Toolbox (GTK4) as a self-contained folder in ~/.local/share/idus-sc-toolbox
# + a start-menu entry with icon and right-click actions.
# The Wine prefix is auto-detected from the LUG config → works on any machine.
# Run:  bash install-idus-sc-toolbox.sh
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/idus-sc-toolbox"
BIN="$HOME/.local/bin"
ICONS="$HOME/.local/share/icons/hicolor/512x512/apps"

# ─────────────────────────────────────────────────────────────────────────────
# Find the Wine prefix. Order:
#   1. SC_PREFIX from the environment  (SC_PREFIX=/path bash install-idus-sc-toolbox.sh)
#   2. LUG config game_path (reads the line with drive_c, trims there)
#   3. sc-launch.sh somewhere under $HOME (skips backup/download copies)
#   4. Fallback: ~/Games/star-citizen (LUG default)
# ─────────────────────────────────────────────────────────────────────────────
detect_prefix() {
    [ -n "$SC_PREFIX" ] && { echo "$SC_PREFIX"; return; }
    local p
    p="$(grep -h "drive_c" "$HOME/.config/starcitizen-lug/"* 2>/dev/null \
          | sed 's#/drive_c.*##' | head -1)"
    [ -n "$p" ] && [ -d "$p" ] && { echo "$p"; return; }
    p="$(find "$HOME" -name sc-launch.sh 2>/dev/null \
          | grep -vi -e backup -e download | head -1)"
    [ -n "$p" ] && { dirname "$p"; return; }
    echo "$HOME/Games/star-citizen"
}
SC_PREFIX="$(detect_prefix)"
SC_BACKUP_DIR="${SC_BACKUP_DIR:-}"   # empty = app default (~/SC-backups)

mkdir -p "$APP" "$BIN" "$ICONS" ~/.local/share/applications

# App + icon in the same folder (one folder backup captures everything, like WolfLight)
install -m 755 "$SRC/idus-sc-toolbox.py" "$APP/idus-sc-toolbox.py"
# Accept the icon as idus-sc-toolbox.png or the older sc-toolbox.png
ICON_SRC=""
[ -f "$SRC/idus-sc-toolbox.png" ] && ICON_SRC="$SRC/idus-sc-toolbox.png"
[ -z "$ICON_SRC" ] && [ -f "$SRC/sc-toolbox.png" ] && ICON_SRC="$SRC/sc-toolbox.png"
if [ -n "$ICON_SRC" ]; then
    install -m 644 "$ICON_SRC" "$APP/idus-sc-toolbox.png"
    install -m 644 "$ICON_SRC" "$ICONS/idus-sc-toolbox.png"
    ICON_LINE="Icon=idus-sc-toolbox"
else
    echo "⚠️  no icon PNG found next to the script — using a generic game icon."
    ICON_LINE="Icon=applications-games"
fi

# PATH wrapper with baked-in paths
{
    echo '#!/usr/bin/env bash'
    echo "export SC_PREFIX=\"$SC_PREFIX\""
    [ -n "$SC_BACKUP_DIR" ] && echo "export SC_BACKUP_DIR=\"$SC_BACKUP_DIR\""
    echo "exec python3 \"$APP/idus-sc-toolbox.py\" \"\$@\""
} > "$BIN/idus-sc-toolbox"
chmod 755 "$BIN/idus-sc-toolbox"

# Start-menu entry + right-click actions
cat > ~/.local/share/applications/idus-sc-toolbox.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Idus SC Toolbox
Comment=Star Citizen tools: kill, backup/restore, joystick diag, windows, shaders, StarStrings
Exec=$BIN/idus-sc-toolbox
$ICON_LINE
Categories=Game;Utility;
Actions=kill;fixwin;backup;starstrings;

[Desktop Action kill]
Name=Kill SC + wineserver
Exec=$BIN/idus-sc-toolbox --kill

[Desktop Action fixwin]
Name=Fix windows (launcher + screen)
Exec=$BIN/idus-sc-toolbox --fix-windows

[Desktop Action backup]
Name=Back up now
Exec=$BIN/idus-sc-toolbox --backup

[Desktop Action starstrings]
Name=Update StarStrings (LIVE)
Exec=$BIN/idus-sc-toolbox --starstrings
DESK

# Bust icon & menu caches (NO plasma restart — do that yourself if needed)
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
rm -f "$HOME/.cache/icon-cache.kcache" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 >/dev/null 2>&1 || \
    { command -v kbuildsycoca5 >/dev/null 2>&1 && kbuildsycoca5 >/dev/null 2>&1; } || true

# Dependency check (GTK4)
if ! python3 -c "import gi; gi.require_version('Gtk','4.0')" 2>/dev/null; then
    echo "⚠️  Missing GTK4 bindings. Run:  sudo pacman -S --needed python-gobject gtk4"
fi

echo "✅ Done."
echo "   • Prefix (auto):  SC_PREFIX=$SC_PREFIX"
[ -n "$SC_BACKUP_DIR" ] && echo "   • Backup dir:     SC_BACKUP_DIR=$SC_BACKUP_DIR"
echo "   • Launch: search 'Idus SC Toolbox' in the menu, or run 'idus-sc-toolbox' in a terminal."
echo "   • Wrong prefix? Run:  SC_PREFIX=/your/path bash install-idus-sc-toolbox.sh"
echo "   • New icon not showing? Log out/in (the icon cache is stubborn)."
echo
echo "   NOTE: the old 'sc-toolbox' install (if any) is left in place. To remove it:"
echo "     rm -f ~/.local/bin/sc-toolbox ~/.local/share/applications/sc-toolbox.desktop"
echo "     rm -rf ~/.local/share/sc-toolbox ~/.local/share/icons/hicolor/512x512/apps/sc-toolbox.png"

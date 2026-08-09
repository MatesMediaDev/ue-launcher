#!/usr/bin/env bash
# SteamOS / Steam Deck friendly launcher.
# AppImages need FUSE to mount; Deck often lacks it, so double-click does nothing.
# This wrapper always extract-and-runs and surfaces errors in a dialog.
set -euo pipefail

LOG="${XDG_CACHE_HOME:-$HOME/.cache}/mates-unreal-launcher/appimage.log"
mkdir -p "$(dirname "$LOG")"
log() { printf '%s\n' "$*" | tee -a "$LOG" >&2; }

die() {
  log "ERROR: $*"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="Unreal Launcher" --width=480 --text="$*" 2>/dev/null || true
  elif command -v kdialog >/dev/null 2>&1; then
    kdialog --error "$*" 2>/dev/null || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send -u critical "Unreal Launcher" "$*" 2>/dev/null || true
  fi
  exit 1
}

HERE="$(cd "$(dirname "$0")" && pwd)"
SELF="$(readlink -f "$0" 2>/dev/null || realpath "$0")"
IMG=""

# Prefer embedded payload when this file IS the Discord single-file .run
if grep -a -q '^__APPIMAGE_BELOW__' "$SELF" 2>/dev/null; then
  SKIP="$(awk '/^__APPIMAGE_BELOW__/ {print NR+1; exit}' "$SELF" || true)"
  if [[ -n "${SKIP:-}" ]]; then
    TMP="${XDG_CACHE_HOME:-$HOME/.cache}/mates-unreal-launcher"
    mkdir -p "$TMP"
    IMG="${TMP}/Unreal_Launcher-x86_64.AppImage"
    if [[ ! -f "$IMG" || "$SELF" -nt "$IMG" ]]; then
      log "Extracting embedded AppImage → $IMG"
      tail -n+"$SKIP" "$SELF" > "$IMG"
      chmod +x "$IMG"
    fi
  fi
fi

# Sibling AppImage (thin helper script next to the real AppImage)
if [[ -z "$IMG" || ! -f "$IMG" ]]; then
  IMG="${HERE}/Unreal_Launcher-x86_64.AppImage"
fi

[[ -f "$IMG" ]] || die "Missing Unreal_Launcher-x86_64.AppImage next to this script."
chmod +x "$IMG" 2>/dev/null || true

export APPIMAGE_EXTRACT_AND_RUN=1
# Keep extracted tree so relaunches on Deck are faster
export NO_CLEANUP="${NO_CLEANUP:-1}"

log "Launching via extract-and-run: $IMG"
# Don't exec — capture failures for zenity
set +e
"$IMG" "$@" >>"$LOG" 2>&1
rc=$?
set -e
if [[ "$rc" -ne 0 ]]; then
  die "Unreal Launcher exited with code $rc

Try from Konsole:
  APPIMAGE_EXTRACT_AND_RUN=1 ./Unreal_Launcher-x86_64.AppImage

Log: $LOG"
fi
exit "$rc"

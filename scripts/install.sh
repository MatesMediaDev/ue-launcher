#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor"
WRAPPER="${BIN_DIR}/mates-unreal-launcher"
OLD_WRAPPER="${BIN_DIR}/ue-launcher"
DESKTOP="${APP_DIR}/mates-unreal-launcher.desktop"
OLD_DESKTOP="${APP_DIR}/ue-launcher.desktop"

mkdir -p "${BIN_DIR}" "${APP_DIR}" "${ICON_DIR}"

cat > "${WRAPPER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="${ROOT}\${PYTHONPATH:+:\$PYTHONPATH}"
# Prefer user-installed mates icons
export XDG_DATA_DIRS="${HOME}/.local/share\${XDG_DATA_DIRS:+:\$XDG_DATA_DIRS}"
exec /usr/bin/python3 -m ue_launcher "\$@"
EOF
chmod +x "${WRAPPER}"

# Keep old command as a thin alias
ln -sfn "${WRAPPER}" "${OLD_WRAPPER}"

# Install app icons (hicolor theme)
if [[ -d "${ROOT}/data/icons/hicolor" ]]; then
  cp -a "${ROOT}/data/icons/hicolor/." "${ICON_DIR}/"
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f "${ICON_DIR}" 2>/dev/null || true
  fi
fi

cp "${ROOT}/data/mates-unreal-launcher.desktop" "${DESKTOP}"
sed -i "s|^Exec=.*|Exec=${WRAPPER}|" "${DESKTOP}"
sed -i "s|^Icon=.*|Icon=mates-unreal-launcher|" "${DESKTOP}"
rm -f "${OLD_DESKTOP}"

# Migrate auth/config from old app id if present
/usr/bin/python3 - <<'PY'
from pathlib import Path
import shutil

old = Path.home() / ".config" / "ue-launcher"
new = Path.home() / ".config" / "mates-unreal-launcher"
new.mkdir(parents=True, mode=0o700, exist_ok=True)
if old.is_dir():
    for name in ("auth.json", "config.json"):
        src, dst = old / name, new / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            dst.chmod(0o600)
            print(f"Migrated {name} → ~/.config/mates-unreal-launcher/")

from ue_launcher.config import Config
from ue_launcher import branding
Config.load().save()
assert branding.mark_path() is not None, "unreal-mark.png missing"
print("Config ready at ~/.config/mates-unreal-launcher/config.json")
print(f"Brand mark: {branding.mark_path()}")
PY

echo "Installed:"
echo "  ${WRAPPER}"
echo "  ${DESKTOP}"
echo "  ${ICON_DIR}/*/apps/mates-unreal-launcher.png"
echo
echo "Run: mates-unreal-launcher"
echo "Or open 'Unreal Launcher' from the app menu."

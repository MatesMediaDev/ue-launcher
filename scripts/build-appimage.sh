#!/usr/bin/env bash
# Build a relocatable AppImage for Unreal Launcher.
#
# Default: self-contained build inside Ubuntu 24.04 (podman) so it runs on
# SteamOS / Bazzite / Fedora without host PyGObject+GTK packages.
#
# Thin host-only build (dev): HOST_ONLY=1 ./scripts/build-appimage.sh
#
# Output: dist/Unreal_Launcher-x86_64.AppImage
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${ROOT}/dist"
APPDIR="${DIST}/UnrealLauncher.AppDir"
ARCH="$(uname -m)"
APPIMAGE_NAME="Unreal_Launcher-${ARCH}.AppImage"
TOOLS="${DIST}/tools"
HOST_ONLY="${HOST_ONLY:-0}"

mkdir -p "${DIST}" "${TOOLS}"
chmod +x "${ROOT}/scripts/bundle-gtk-runtime.sh" "${ROOT}/packaging/appimage/AppRun"

fill_appdir_common() {
  local appdir="$1"
  rm -rf "${appdir}"
  mkdir -p \
    "${appdir}/usr/bin" \
    "${appdir}/usr/lib/python3/site-packages" \
    "${appdir}/usr/share/applications" \
    "${appdir}/usr/share/icons/hicolor/256x256/apps"

  cat > "${appdir}/usr/bin/unreal-launcher" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
APPDIR="$(cd "${HERE}/../.." && pwd)"
exec "${APPDIR}/AppRun" "$@"
EOF
  chmod +x "${appdir}/usr/bin/unreal-launcher"

  cp "${ROOT}/packaging/appimage/AppRun" "${appdir}/AppRun"
  chmod +x "${appdir}/AppRun"
  cp "${ROOT}/packaging/appimage/unreal-launcher.desktop" "${appdir}/unreal-launcher.desktop"
  cp "${ROOT}/packaging/appimage/unreal-launcher.desktop" \
    "${appdir}/usr/share/applications/unreal-launcher.desktop"

  ICON_SRC="${ROOT}/data/icons/hicolor/256x256/apps/mates-unreal-launcher.png"
  [[ -f "${ICON_SRC}" ]] || { echo "Missing icon: ${ICON_SRC}" >&2; exit 1; }
  cp "${ICON_SRC}" "${appdir}/mates-unreal-launcher.png"
  cp "${ICON_SRC}" "${appdir}/.DirIcon"
  cp "${ICON_SRC}" "${appdir}/usr/share/icons/hicolor/256x256/apps/mates-unreal-launcher.png"
}

install_python_app() {
  local appdir="$1"
  local py="${2:-python3}"
  "${py}" -m pip install \
    --upgrade \
    --target "${appdir}/usr/lib/python3/site-packages" \
    --no-compile \
  "${ROOT}" \
  "requests>=2.31" \
  "curl_cffi>=0.7"
  find "${appdir}/usr/lib/python3/site-packages" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
  # Keep *.dist-info — curl_cffi imports metadata at load time.
}

pack_appimage() {
  local appdir="$1"
  local out_name="$2"
  APPIMAGETOOL="${TOOLS}/appimagetool-${ARCH}.AppImage"
  if [[ ! -x "${APPIMAGETOOL}" ]]; then
    echo "==> Fetching appimagetool"
    curl -fsSL -o "${APPIMAGETOOL}" \
      "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
    chmod +x "${APPIMAGETOOL}"
  fi
  echo "==> Slimming AppDir for Discord size limit"
  slim_appdir "${appdir}"
  echo "==> Building ${out_name}"
  local out_abs="${DIST}/${out_name}"
  rm -f "${out_abs}"
  (
    cd "${DIST}"
    # Absolute output path — appimagetool otherwise may write relative to $HOME/cwd
    if ! ARCH="${ARCH}" "${APPIMAGETOOL}" "${appdir}" "${out_abs}"; then
      ARCH="${ARCH}" APPIMAGE_EXTRACT_AND_RUN=1 \
        "${APPIMAGETOOL}" "${appdir}" "${out_abs}"
    fi
  )
  chmod +x "${out_abs}"
}

slim_appdir() {
  local appdir="$1"
  # GTK emoji data unused by this app (~6 MiB)
  rm -rf "${appdir}/usr/share/gtk-4.0/emoji" 2>/dev/null || true
  # Prefer symbolic-only Adwaita; drop huge scalable trees if present
  rm -rf "${appdir}/usr/share/icons/Adwaita/scalable" 2>/dev/null || true
  find "${appdir}/usr/share/icons/Adwaita" -type d -name '64x64' -prune -exec rm -rf {} + 2>/dev/null || true
  find "${appdir}/usr/share/icons/Adwaita" -type d -name '96x96' -prune -exec rm -rf {} + 2>/dev/null || true
  find "${appdir}/usr/share/icons/Adwaita" -type d -name '256x256' -prune -exec rm -rf {} + 2>/dev/null || true
  # Python stdlib we never import
  if [[ -f "${appdir}/usr/pyversion" ]]; then
    local pyv
    pyv="$(cat "${appdir}/usr/pyversion")"
    local pydir="${appdir}/usr/lib/python${pyv}"
    if [[ -d "$pydir" ]]; then
      rm -rf \
        "${pydir}/pydoc_data" \
        "${pydir}/unittest" \
        "${pydir}/ensurepip" \
        "${pydir}/idlelib" \
        "${pydir}/turtledemo" \
        "${pydir}/tkinter" \
        "${pydir}/turtle.py" \
        "${pydir}/doctest.py" \
        "${pydir}/config-"* \
        "${pydir}/__pycache__" \
        2>/dev/null || true
      find "$pydir" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
      # Unused extension modules
      find "${pydir}/lib-dynload" -type f \( \
        -name '_tkinter*' -o -name 'audioop*' -o -name 'ossaudiodev*' -o \
        -name '_curses*' -o -name 'nis*' -o -name 'spwd*' \
      \) -delete 2>/dev/null || true
    fi
  fi
  # curl_cffi CLI / tests not needed at runtime
  rm -rf "${appdir}/usr/lib/python3/site-packages/curl_cffi/cli" 2>/dev/null || true
  rm -f "${appdir}/usr/lib/python3/site-packages/bin/curl-cffi" 2>/dev/null || true
  # GI typelibs we don't use
  if [[ -d "${appdir}/usr/lib/girepository-1.0" ]]; then
    find "${appdir}/usr/lib/girepository-1.0" -type f ! \( \
      -name 'Gtk-4.0.typelib' -o -name 'Gdk-4.0.typelib' -o -name 'Gsk-4.0.typelib' -o \
      -name 'Adw-1.typelib' -o \
      -name 'Gio-2.0.typelib' -o -name 'GObject-2.0.typelib' -o -name 'GLib-2.0.typelib' -o \
      -name 'GModule-2.0.typelib' -o -name 'GioUnix-2.0.typelib' -o \
      -name 'Pango-1.0.typelib' -o -name 'PangoCairo-1.0.typelib' -o -name 'HarfBuzz-0.0.typelib' -o \
      -name 'cairo-1.0.typelib' -o -name 'GdkPixbuf-2.0.typelib' -o -name 'Graphene-1.0.typelib' -o \
      -name 'freetype2-2.0.typelib' -o -name 'fontconfig-2.0.typelib' -o \
      -name 'xlib-2.0.typelib' -o -name 'xfix-1.0.typelib' -o \
      -name 'Vulkan-1.0.typelib' -o -name 'Gst*-1.0.typelib' -o -name 'Soup-3.0.typelib' \
    \) -delete 2>/dev/null || true
  fi
  # Strip shared objects / binaries (safe size win)
  if command -v strip >/dev/null 2>&1; then
    find "${appdir}/usr/lib" -type f \( -name '*.so' -o -name '*.so.*' \) -print0 \
      | xargs -0 -r strip --strip-unneeded 2>/dev/null || true
    find "${appdir}/usr/bin" -type f -executable -print0 \
      | xargs -0 -r strip --strip-unneeded 2>/dev/null || true
  fi
  echo "    AppDir after slim: $(du -sh "${appdir}" | awk '{print $1}')"
}

build_host_only() {
  echo "==> HOST_ONLY build (requires host GTK4 + PyGObject)"
  fill_appdir_common "${APPDIR}"
  install_python_app "${APPDIR}"
  pack_appimage "${APPDIR}" "${APPIMAGE_NAME}"
}

build_portable() {
  if ! command -v podman >/dev/null 2>&1; then
    echo "podman not found — falling back to HOST_ONLY=1" >&2
    build_host_only
    return
  fi

  local image="${APPIMAGE_BASE_IMAGE:-docker.io/library/ubuntu:24.04}"
  echo "==> Portable AppImage via ${image}"
  echo "==> Pulling base image (first run may take a bit)"
  podman pull "${image}"

  # Build inside container; write into mounted dist/
  # Container root maps to a subuid — host must remove any previous AppDir first.
  rm -rf "${APPDIR}"
  podman run --rm -i \
    --security-opt label=disable \
    -v "${ROOT}:/src:ro" \
    -v "${DIST}:/out:rw" \
    -e DEBIAN_FRONTEND=noninteractive \
    "${image}" \
    bash -s <<'CONTAINER'
set -euo pipefail
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  python3 python3-pip python3-gi python3-gi-cairo \
  gir1.2-gtk-4.0 gir1.2-adw-1 \
  libgtk-4-1 libadwaita-1-0 \
  libgdk-pixbuf-2.0-0 libgdk-pixbuf2.0-bin librsvg2-2 webp-pixbuf-loader \
  adwaita-icon-theme hicolor-icon-theme \
  shared-mime-info \
  glib-networking \
  ca-certificates curl file binutils \
  >/dev/null

APPDIR=/out/UnrealLauncher.AppDir
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}"

# Common skeleton from host-mounted scripts
bash /src/scripts/bundle-gtk-runtime.sh "${APPDIR}"

# App skeleton pieces
mkdir -p \
  "${APPDIR}/usr/bin" \
  "${APPDIR}/usr/lib/python3/site-packages" \
  "${APPDIR}/usr/share/applications" \
  "${APPDIR}/usr/share/icons/hicolor/256x256/apps"

cat > "${APPDIR}/usr/bin/unreal-launcher" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
APPDIR="$(cd "${HERE}/../.." && pwd)"
exec "${APPDIR}/AppRun" "$@"
EOF
chmod +x "${APPDIR}/usr/bin/unreal-launcher"

cp /src/packaging/appimage/AppRun "${APPDIR}/AppRun"
chmod +x "${APPDIR}/AppRun"
cp /src/packaging/appimage/unreal-launcher.desktop "${APPDIR}/unreal-launcher.desktop"
cp /src/packaging/appimage/unreal-launcher.desktop \
  "${APPDIR}/usr/share/applications/unreal-launcher.desktop"

ICON=/src/data/icons/hicolor/256x256/apps/mates-unreal-launcher.png
cp "${ICON}" "${APPDIR}/mates-unreal-launcher.png"
cp "${ICON}" "${APPDIR}/.DirIcon"
cp "${ICON}" "${APPDIR}/usr/share/icons/hicolor/256x256/apps/mates-unreal-launcher.png"

# /src is read-only — pip needs to write egg-info while building
cp -a /src /tmp/ue-launcher-src
python3 -m pip install \
  --upgrade \
  --break-system-packages \
  --target "${APPDIR}/usr/lib/python3/site-packages" \
  --no-compile \
  /tmp/ue-launcher-src \
  "requests>=2.31" \
  "curl_cffi>=0.7"

find "${APPDIR}/usr/lib/python3/site-packages" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
# Keep *.dist-info — curl_cffi imports metadata at load time.

# Smoke: bundled python must import Gtk/Adw + curl_cffi without host paths
export LD_LIBRARY_PATH="${APPDIR}/usr/lib"
export GI_TYPELIB_PATH="${APPDIR}/usr/lib/girepository-1.0"
export PYTHONPATH="${APPDIR}/usr/lib/python3/site-packages"
export PYTHONHOME="${APPDIR}/usr"
PYV="$(cat "${APPDIR}/usr/pyversion")"
export PYTHONPATH="${APPDIR}/usr/lib/python${PYV}:${PYTHONPATH}"

"${APPDIR}/usr/bin/python3" - <<'PY'
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
import ue_launcher
from ue_launcher.epic.http_browser import has_curl_cffi
assert has_curl_cffi(), "curl_cffi missing from AppDir"
print("container smoke ok", Gtk.get_major_version(), Adw.get_major_version(), "curl_cffi", has_curl_cffi())
PY
CONTAINER

  pack_appimage "${APPDIR}" "${APPIMAGE_NAME}"
}

if [[ "${HOST_ONLY}" == "1" ]]; then
  build_host_only
else
  build_portable
fi

cp "${ROOT}/packaging/appimage/Unreal_Launcher.sh" "${DIST}/Unreal_Launcher.sh"
chmod +x "${DIST}/Unreal_Launcher.sh"

OUT="${DIST}/${APPIMAGE_NAME}"

# Single-file Deck launcher (no FUSE): shell stub + embedded AppImage
RUN="${DIST}/Unreal_Launcher-${ARCH}.run"
{
  cat "${ROOT}/packaging/appimage/Unreal_Launcher.sh"
  printf '\n__APPIMAGE_BELOW__\n'
  cat "${OUT}"
} > "${RUN}"
chmod +x "${RUN}"

echo
echo "Built: ${OUT}"
echo "Deck/Discord share this instead (FUSE-free): ${RUN}"
ls -lh "${OUT}" "${RUN}" "${DIST}/Unreal_Launcher.sh"
echo
echo "Steam Deck:"
echo "  chmod +x Unreal_Launcher-x86_64.run"
echo "  ./Unreal_Launcher-x86_64.run"
echo "Or: APPIMAGE_EXTRACT_AND_RUN=1 ./Unreal_Launcher-x86_64.AppImage"
echo "Log: ~/.cache/mates-unreal-launcher/appimage.log"

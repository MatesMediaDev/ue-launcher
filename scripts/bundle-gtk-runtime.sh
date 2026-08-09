#!/usr/bin/env bash
# Bundle host Python + PyGObject + GTK4 + libadwaita into an AppDir.
# Intended to run on Ubuntu 24.04 (glibc 2.39) so the AppImage runs on SteamOS (glibc 2.41+).
set -euo pipefail

APPDIR="${1:?AppDir path}"
mkdir -p \
  "${APPDIR}/usr/bin" \
  "${APPDIR}/usr/lib" \
  "${APPDIR}/usr/lib/python3/site-packages" \
  "${APPDIR}/usr/share"

PY="$(readlink -f "$(command -v python3)")"
PY_REAL="$(readlink -f "${PY}")"
echo "Bundling Python: ${PY_REAL}"

cp -a "${PY_REAL}" "${APPDIR}/usr/bin/python3"
chmod +x "${APPDIR}/usr/bin/python3"

# libpython next to the interpreter (or via ldd)
copy_file() {
  local src="$1"
  local dest="$2"
  [[ -e "$src" ]] || return 0
  mkdir -p "$(dirname "$dest")"
  if [[ -L "$src" ]]; then
    local target
    target="$(readlink -f "$src")"
    cp -a "$target" "$dest" 2>/dev/null || cp -aL "$src" "$dest"
    # keep soname symlink if dest is a versioned name
  else
    cp -a "$src" "$dest"
  fi
}

# Collect shared libs via ldd (skip glibc / ld-linux)
collect_libs() {
  local bin="$1"
  ldd "$bin" 2>/dev/null | awk '
    /=>/ {
      lib=$3
      if (lib != "" && lib != "not" && system("test -f " lib) == 0) print lib
    }
    /^\s*\// {
      lib=$1
      if (system("test -f " lib) == 0) print lib
    }
  '
}

should_skip_lib() {
  local base
  base="$(basename "$1")"
  case "$base" in
    # Never bundle these — use the host (glibc, TLS fingerprint for Cloudflare, GPU).
    libc.so.*|libm.so.*|libdl.so.*|libpthread.so.*|libresolv.so.*|librt.so.*|libutil.so.*|ld-linux*.so.*|linux-vdso.so.*)
      return 0 ;;
    # Host OpenSSL = working Cloudflare JA3. Keep curl-gnutls (Ubuntu GTK stack).
    libssl.so.*|libcrypto.so.*)
      return 0 ;;
    libcurl.so.*)
      # Prefer gnutls curl from the bundle; skip OpenSSL-linked libcurl.so
      return 0 ;;
    libGL.so.*|libGLX.so.*|libOpenGL.so.*|libEGL.so.*|libGLdispatch.so.*|libvulkan.so.*|libdrm.so.*)
      return 0 ;;
  esac
  return 1
}

LIBDIR="${APPDIR}/usr/lib"
mkdir -p "${LIBDIR}"

queue=()
seen=()

enqueue() {
  local f="$1"
  [[ -n "$f" && -e "$f" ]] || return 0
  local real
  real="$(readlink -f "$f")"
  local s
  for s in "${seen[@]:-}"; do
    [[ "$s" == "$real" ]] && return 0
  done
  seen+=("$real")
  queue+=("$real")
}

enqueue "${PY_REAL}"

# PyGObject / cairo bindings
PY_SITE="$(${PY_REAL} -c 'import site,sys; print(site.getsitepackages()[0])')"
GI_DIR="$(${PY_REAL} -c 'import gi, os; print(os.path.dirname(gi.__file__))')"
echo "Copying gi from ${GI_DIR}"
mkdir -p "${APPDIR}/usr/lib/python3/site-packages"
cp -a "${GI_DIR}" "${APPDIR}/usr/lib/python3/site-packages/"
# cairo if present
${PY_REAL} -c 'import cairo' 2>/dev/null && {
  CAIRO_DIR="$(${PY_REAL} -c 'import cairo, os; print(os.path.dirname(cairo.__file__))')"
  cp -a "${CAIRO_DIR}" "${APPDIR}/usr/lib/python3/site-packages/" || true
} || true

# Also copy from dist-packages (Debian/Ubuntu layout)
for extra in /usr/lib/python3/dist-packages/gi /usr/lib/python3/dist-packages/cairo; do
  if [[ -d "$extra" ]]; then
    cp -a "$extra" "${APPDIR}/usr/lib/python3/site-packages/" 2>/dev/null || true
  fi
done

# Seed with GI extension modules + GTK/Adwaita
while IFS= read -r -d '' so; do
  enqueue "$so"
done < <(find "${APPDIR}/usr/lib/python3/site-packages/gi" -name '*.so' -print0 2>/dev/null)

for lib in \
  /usr/lib/x86_64-linux-gnu/libgtk-4.so.1 \
  /usr/lib/x86_64-linux-gnu/libadwaita-1.so.0 \
  /usr/lib64/libgtk-4.so.1 \
  /usr/lib64/libadwaita-1.so.0
 do
  [[ -e "$lib" ]] && enqueue "$lib"
done

stage_lib() {
  local cur="$1"
  local base dest soname
  base="$(basename "$cur")"
  case "$base" in
    *.so|*.so.*) ;;
    *) return 0 ;;
  esac
  dest="${LIBDIR}/${base}"
  if [[ ! -e "$dest" ]]; then
    cp -aL "$cur" "$dest"
  fi
  if command -v objdump >/dev/null 2>&1; then
    soname="$(objdump -p "$dest" 2>/dev/null | awk '/SONAME/ {print $2; exit}')"
    if [[ -n "${soname:-}" && "$soname" != "$base" && ! -e "${LIBDIR}/${soname}" ]]; then
      ln -s "$base" "${LIBDIR}/${soname}"
    fi
  fi
}

# Walk dependency closure
i=0
while (( i < ${#queue[@]} )); do
  cur="${queue[$i]}"
  i=$((i + 1))
  base="$(basename "$cur")"
  if should_skip_lib "$cur"; then
    continue
  fi
  # Only stage shared objects into usr/lib (never the python interpreter binary)
  case "$base" in
    *.so|*.so.*)
      stage_lib "$cur"
      ;;
    *)
      ;;
  esac
  # Always recurse deps of seeds (e.g. python3.12 binary)
  while IFS= read -r dep; do
    should_skip_lib "$dep" && continue
    enqueue "$dep"
  done < <(collect_libs "$cur")
done

# Also seed common SONAME symlink names from the original ldd paths (pre-resolve)
# Re-walk any new items from gdk-pixbuf etc. happens later.

# Typelibs
TYPELIB_DEST="${APPDIR}/usr/lib/girepository-1.0"
mkdir -p "${TYPELIB_DEST}"
TYPELIB_SRC=""
for d in /usr/lib/x86_64-linux-gnu/girepository-1.0 /usr/lib64/girepository-1.0 /usr/lib/girepository-1.0; do
  [[ -d "$d" ]] && TYPELIB_SRC="$d" && break
done
[[ -n "$TYPELIB_SRC" ]] || { echo "No girepository found" >&2; exit 1; }

# Copy required typelibs + common deps Gtk/Adw pull in
NEEDED_TYPELIBS=(
  Gtk-4.0 Gdk-4.0 Gsk-4.0 Adw-1
  Gio-2.0 GObject-2.0 GLib-2.0 GModule-2.0 GioUnix-2.0
  Graphene-1.0 Pango-1.0 PangoCairo-1.0 HarfBuzz-0.0
  cairo-1.0 cairo-1.0 GdkPixbuf-2.0
  GdkPixdata-2.0 Gdk-4.0
  Soup-3.0 Soup-2.4
  Vulkan-1.0
  freetype2-2.0 fontconfig-2.0
  xlib-2.0 xfix-1.0
  Atk-1.0 Atspi-2.0
)
# Copy all typelibs — safer for GI resolution, still small
cp -a "${TYPELIB_SRC}/." "${TYPELIB_DEST}/"

# Gdk-pixbuf loaders + query tool (cache regenerated at runtime for mount path)
for pb in /usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0 /usr/lib64/gdk-pixbuf-2.0; do
  if [[ -d "$pb" ]]; then
    mkdir -p "${APPDIR}/usr/lib/gdk-pixbuf-2.0"
    cp -a "$pb/." "${APPDIR}/usr/lib/gdk-pixbuf-2.0/"
    break
  fi
done
if command -v gdk-pixbuf-query-loaders >/dev/null 2>&1; then
  cp -aL "$(command -v gdk-pixbuf-query-loaders)" "${APPDIR}/usr/bin/gdk-pixbuf-query-loaders"
  chmod +x "${APPDIR}/usr/bin/gdk-pixbuf-query-loaders"
  enqueue "$(command -v gdk-pixbuf-query-loaders)"
fi
# Loader plugins themselves
while IFS= read -r -d '' so; do
  enqueue "$so"
done < <(find "${APPDIR}/usr/lib/gdk-pixbuf-2.0" -name '*.so' -print0 2>/dev/null)

# GLib schemas (adwaita / gtk)
mkdir -p "${APPDIR}/usr/share/glib-2.0/schemas"
for sch in /usr/share/glib-2.0/schemas; do
  if [[ -d "$sch" ]]; then
    # copy only relevant + compiled if present
    cp -a "$sch"/*.gschema.xml "${APPDIR}/usr/share/glib-2.0/schemas/" 2>/dev/null || true
    cp -a "$sch"/gschemas.compiled "${APPDIR}/usr/share/glib-2.0/schemas/" 2>/dev/null || true
  fi
done
if command -v glib-compile-schemas >/dev/null; then
  glib-compile-schemas "${APPDIR}/usr/share/glib-2.0/schemas" 2>/dev/null || true
fi

# Minimal icon theme for symbolic toolbar icons
mkdir -p "${APPDIR}/usr/share/icons"
for theme in Adwaita hicolor; do
  src="/usr/share/icons/${theme}"
  if [[ -d "$src" ]]; then
    # Prefer scalable/symbolic to keep size down
    mkdir -p "${APPDIR}/usr/share/icons/${theme}"
    if [[ -d "${src}/scalable" ]]; then
      cp -a "${src}/scalable" "${APPDIR}/usr/share/icons/${theme}/" 2>/dev/null || true
    fi
    if [[ -d "${src}/symbolic" ]]; then
      cp -a "${src}/symbolic" "${APPDIR}/usr/share/icons/${theme}/" 2>/dev/null || true
    fi
    # index.theme required
    [[ -f "${src}/index.theme" ]] && cp -a "${src}/index.theme" "${APPDIR}/usr/share/icons/${theme}/"
    # common sizes used by GTK
    for sz in 16x16 22x22 24x24 32x32 48x48; do
      if [[ -d "${src}/${sz}" ]]; then
        mkdir -p "${APPDIR}/usr/share/icons/${theme}/${sz}"
        for cat in actions status devices places apps mimetypes; do
          [[ -d "${src}/${sz}/${cat}" ]] || continue
          mkdir -p "${APPDIR}/usr/share/icons/${theme}/${sz}/${cat}"
          # symbolic only when present
          find "${src}/${sz}/${cat}" -maxdepth 1 \( -name '*-symbolic*' -o -name 'image-missing*' \) \
            -exec cp -a {} "${APPDIR}/usr/share/icons/${theme}/${sz}/${cat}/" \; 2>/dev/null || true
        done
      fi
    done
  fi
done

# GTK 4 / Adwaita data files
for d in gtk-4.0 libadwaita-1; do
  if [[ -d "/usr/share/${d}" ]]; then
    cp -a "/usr/share/${d}" "${APPDIR}/usr/share/"
  fi
done

# Ensure python can find stdlib (Ubuntu: /usr/lib/python3.12)
PY_VERSION="$(${PY_REAL} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
for stdlib in "/usr/lib/python${PY_VERSION}" "/usr/lib64/python${PY_VERSION}"; do
  if [[ -d "$stdlib" ]]; then
    mkdir -p "${APPDIR}/usr/lib/python${PY_VERSION}"
    cp -a "${stdlib}/." "${APPDIR}/usr/lib/python${PY_VERSION}/"
    find "${APPDIR}/usr/lib/python${PY_VERSION}" -type d \( -name __pycache__ -o -name test -o -name tests \) -prune -exec rm -rf {} + 2>/dev/null || true
    break
  fi
done

# lib-dynload modules need their .so deps too
if [[ -d "${APPDIR}/usr/lib/python${PY_VERSION}/lib-dynload" ]]; then
  while IFS= read -r -d '' so; do
    enqueue "$so"
  done < <(find "${APPDIR}/usr/lib/python${PY_VERSION}/lib-dynload" -name '*.so' -print0)
  while (( i < ${#queue[@]} )); do
    cur="${queue[$i]}"
    i=$((i + 1))
    if should_skip_lib "$cur"; then continue; fi
    stage_lib "$cur"
    while IFS= read -r dep; do
      should_skip_lib "$dep" && continue
      enqueue "$dep"
    done < <(collect_libs "$cur")
  done
fi

# Do NOT bundle OpenSSL/curl — host TLS stack avoids Cloudflare JA3 blocks.
# (Explicit seed removed; should_skip_lib already excludes them.)

# Record python version for AppRun
echo "${PY_VERSION}" > "${APPDIR}/usr/pyversion"

# Strip OpenSSL (host TLS) but keep curl-gnutls for libadwaita.
find "${LIBDIR}" -maxdepth 1 \( \
  -name 'libssl.so*' -o -name 'libcrypto.so*' -o -name 'libcurl.so*' \
\) ! -name 'libcurl-gnutls*' -delete 2>/dev/null || true

# Ensure curl-gnutls is present for Adwaita/Soup
for lib in \
  /usr/lib/x86_64-linux-gnu/libcurl-gnutls.so.4 \
  /usr/lib/x86_64-linux-gnu/libgnutls.so.30
do
  [[ -e "$lib" ]] || continue
  real="$(readlink -f "$lib")"
  enqueue "$real"
  stage_lib "$real"
  base="$(basename "$real")"
  linkname="$(basename "$lib")"
  if [[ "$base" != "$linkname" && ! -e "${LIBDIR}/${linkname}" ]]; then
    ln -sf "$base" "${LIBDIR}/${linkname}"
  fi
done
# One more dep walk for newly enqueued curl/gnutls
while (( i < ${#queue[@]} )); do
  cur="${queue[$i]}"
  i=$((i + 1))
  if should_skip_lib "$cur"; then continue; fi
  stage_lib "$cur"
  while IFS= read -r dep; do
    should_skip_lib "$dep" && continue
    enqueue "$dep"
  done < <(collect_libs "$cur")
done

echo "Bundled libs: $(find "${LIBDIR}" -maxdepth 1 -type f | wc -l)"
echo "AppDir size: $(du -sh "${APPDIR}" | awk '{print $1}')"

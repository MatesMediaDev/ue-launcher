#!/usr/bin/env bash
# Bake Lucide SVG sources into PNGs for GTK/AppImage (no SVG loader in bundle).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${ROOT}/ue_launcher/assets/icons-src"
OUT="${ROOT}/ue_launcher/assets/icons/hicolor"

if ! command -v rsvg-convert >/dev/null 2>&1; then
  echo "rsvg-convert required (librsvg)" >&2
  exit 1
fi

shopt -s nullglob
svgs=("${SRC}"/mates-*.svg)
if ((${#svgs[@]} == 0)); then
  echo "No SVG sources in ${SRC}" >&2
  exit 1
fi

for size in 16 24 32; do
  mkdir -p "${OUT}/${size}x${size}/actions"
  for svg in "${svgs[@]}"; do
    name="$(basename "${svg}" .svg)"
    rsvg-convert -w "${size}" -h "${size}" "${svg}" \
      -o "${OUT}/${size}x${size}/actions/${name}.png"
  done
done

echo "Generated $(( ${#svgs[@]} * 3 )) PNGs under ${OUT}"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "${OUT}" 2>/dev/null || true
fi

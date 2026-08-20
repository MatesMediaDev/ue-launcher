# Unreal Launcher

Linux-native Unreal Engine launcher from **[Mates Media](https://mates.dev)** ([`MatesMediaDev`](https://github.com/MatesMediaDev)).

**Primary targets:** [Bazzite](https://bazzite.gg), Fedora, and SteamOS (Steam Deck desktop) — where Epic Games Launcher does not ship.

Stack: **Python 3 + GTK4 / libadwaita**.

## Features

- Discover local Unreal installs (e.g. `~/UnrealEngine/UE_5.7.4`)
- Sign in with Epic and **download official Linux engine builds** in-app (or open [unrealengine.com/linux](https://www.unrealengine.com/linux))
- Scan for `.uproject` files and launch the editor
- New projects from engine templates and **[MinUEmal](https://github.com/litruv/MinUEmal)**
- **Import from Git** (clone a repo that contains a `.uproject`)
- Install owned Fab **code plugins** and complete projects (virtualized library list)
- Sanitized editor launch env (strips Cursor AppImage `LD_LIBRARY_PATH`, sensible Vulkan / display defaults)

Marketplace *content* packs still belong in the in-editor Fab plugin. This app focuses on engines + Linux-installable plugins/projects.

## Requirements

| | |
|---|---|
| Python | 3.11+ |
| UI | PyGObject (GTK 4) + libadwaita |
| Network | `requests`, `curl_cffi` (bundled in the AppImage) |

| Distro | Notes |
|---|---|
| **Bazzite / Fedora** | Usually already have GTK4 + libadwaita + `python3-gobject`. Dev: `sudo dnf install python3-gobject libadwaita gtk4` |
| **SteamOS / Deck** | Prefer the **AppImage** (bundles GTK). FUSE is often missing — use extract-and-run (below). |

## AppImage (SteamOS + sharing)

Build (needs [podman](https://podman.io) on the build machine):

```bash
chmod +x scripts/build-appimage.sh
./scripts/build-appimage.sh
```

Output (~43 MiB):

- `dist/Unreal_Launcher-x86_64.AppImage` — portable build (Ubuntu 24.04 + GTK stack + Python)

```bash
chmod +x Unreal_Launcher-x86_64.AppImage
APPIMAGE_EXTRACT_AND_RUN=1 ./Unreal_Launcher-x86_64.AppImage
```

On Steam Deck (or any host without FUSE), keep `APPIMAGE_EXTRACT_AND_RUN=1`.

If it fails, check `~/.cache/mates-unreal-launcher/appimage.log`.

Thin host-only build (dev): `HOST_ONLY=1 ./scripts/build-appimage.sh`

## Install from source (Bazzite / Fedora)

```bash
chmod +x scripts/install.sh
./scripts/install.sh
```

Then run `mates-unreal-launcher` or open **Unreal Launcher** from the app menu.

Dev run without installing:

```bash
PYTHONPATH=. python3 -m ue_launcher
```

## Epic sign-in & engines

1. **Account** / Engines → **Sign in with Epic**
2. Complete login + MFA in the browser
3. Paste the `authorizationCode` (or the whole JSON page) into the dialog
4. **Refresh** available Linux builds → **Download & Install**

Tokens: `~/.config/mates-unreal-launcher/auth.json` (mode `600`).  
Default install dir: `~/UnrealEngine/UE_X.Y.Z`. Engine zips are large (~25–40 GiB); keep ~2× free space for download + extract.

## Config

`~/.config/mates-unreal-launcher/config.json`

| Key | Purpose |
|---|---|
| `engine_roots` | Folders to scan for UE installs |
| `engine_install_dir` | Where new engines are extracted |
| `engine_cache_dir` | Zip download cache |
| `keep_engine_zips` | Keep zip after extract (`false` by default) |
| `project_scan_roots` | Folders to scan for `.uproject` |
| `preferred_engine` | Preferred engine key (e.g. `UE_5.7`) |
| `vulkan_icd` | Optional Vulkan ICD JSON. Empty = auto (NVIDIA if present, else RADV). Don’t force NVIDIA on Steam Deck. |
| `prefer_x11` | Prefer X11 for the editor when set |

## Notes

- Not affiliated with Epic Games.
- Uses the public `launcherAppClient2` OAuth client (same family as Legendary / Heroic).
- Studio: [mates.dev](https://mates.dev) · GitHub: [MatesMediaDev](https://github.com/MatesMediaDev)

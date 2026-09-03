"""App branding for Unreal Launcher (mates.dev)."""

from __future__ import annotations

from pathlib import Path

STUDIO_NAME = "Mates Media"
STUDIO_DOMAIN = "mates.dev"
STUDIO_URL = "https://mates.dev"
APP_TAGLINE = "Linux Unreal Engine launcher"
ICON_NAME = "mates-unreal-launcher"

# Dark charcoal UI with purple accents (mates.dev)
COLOR_PURPLE = "#9B6CFF"
COLOR_PURPLE_SOFT = "#C4A8FF"
COLOR_PURPLE_DEEP = "#5A3A9E"
COLOR_LIME = "#7CFF3A"
COLOR_INK = "#121212"
COLOR_PANEL = "#1A1A1F"
COLOR_GRID = "#2A2A30"

_PACKAGE_ROOT = Path(__file__).resolve().parent
ASSETS_DIR = _PACKAGE_ROOT / "assets"
MARK_PATH = ASSETS_DIR / "unreal-mark.png"
ICONS_HICOLOR = _PACKAGE_ROOT.parent / "data" / "icons" / "hicolor"


def mark_path() -> Path | None:
    return MARK_PATH if MARK_PATH.is_file() else None


def css() -> str:
    """Quiet dark UI — accent sparingly, no pill clusters."""
    return f"""
    @define-color accent_bg_color {COLOR_PURPLE};
    @define-color accent_fg_color #ffffff;
    @define-color accent_color {COLOR_PURPLE};

    window {{
      background-color: {COLOR_INK};
    }}

    .mates-section-title {{
      font-weight: 600;
      font-size: 0.95em;
      letter-spacing: 0.02em;
      opacity: 0.9;
    }}

    .mates-brand-title {{
      font-weight: 600;
      letter-spacing: 0.01em;
    }}

    .mates-brand-sub {{
      opacity: 0.65;
      font-size: 0.85em;
    }}

    .mates-accent {{
      color: {COLOR_PURPLE_SOFT};
      font-weight: 500;
    }}

    .mates-status {{
      padding: 4px 14px;
      border-top: 1px solid alpha(#ffffff, 0.06);
      font-size: 0.85em;
      opacity: 0.7;
    }}

    .mates-about {{
      padding: 12px 4px 4px 4px;
      opacity: 0.9;
    }}

    .mates-mark {{
      margin: 0;
      padding: 0;
      opacity: 0.95;
    }}

    headerbar .mates-header-mark {{
      margin-start: 0;
      margin-end: 0;
    }}

    headerbar .mates-header-start {{
      margin-start: 16px;
      margin-end: 4px;
    }}

    .mates-plugin-icon,
    .mates-project-icon {{
      margin-right: 8px;
    }}

    image.mates-icon-16 {{
      min-width: 16px;
      max-width: 16px;
      min-height: 16px;
      max-height: 16px;
    }}

    image.mates-icon-32 {{
      min-width: 32px;
      max-width: 32px;
      min-height: 32px;
      max-height: 32px;
    }}

    image.mates-icon-40 {{
      min-width: 40px;
      max-width: 40px;
      min-height: 40px;
      max-height: 40px;
    }}

    .mates-panel {{
      background-color: alpha({COLOR_PANEL}, 0.55);
      border-radius: 10px;
      padding: 12px;
    }}

    .mates-row-btn {{
      min-width: 28px;
      min-height: 28px;
      padding: 4px;
      transition: opacity 120ms ease;
    }}

    list row button.mates-row-btn,
    listview row button.mates-row-btn {{
      opacity: 0;
    }}

    list row:hover button.mates-row-btn,
    list row:selected button.mates-row-btn,
    list row:focus-within button.mates-row-btn,
    listview row:hover button.mates-row-btn,
    listview row:selected button.mates-row-btn,
    listview row:focus-within button.mates-row-btn {{
      opacity: 1;
    }}

    button.mates-header-action {{
      min-height: 32px;
      min-width: 32px;
      padding: 6px 8px;
    }}

    .linked > button.mates-header-action {{
      min-width: 32px;
    }}

    button.mates-view-tab {{
      min-height: 32px;
      padding-left: 10px;
      padding-right: 12px;
    }}

    button.suggested-action {{
      background-image: none;
      background-color: {COLOR_PURPLE};
      color: #ffffff;
      font-weight: 560;
      border-radius: 8px;
      border: none;
      padding-left: 14px;
      padding-right: 14px;
    }}

    button.suggested-action:hover {{
      background-image: none;
      background-color: {COLOR_PURPLE_SOFT};
      color: {COLOR_INK};
    }}

    list.boxed-list {{
      background-color: transparent;
      border-radius: 8px;
    }}

    list.boxed-list > row {{
      border-radius: 6px;
      margin: 1px 0;
    }}

    .mates-settings row.entry .edit-icon {{
      opacity: 0;
      transition: opacity 120ms ease;
    }}

    .mates-settings row.entry:not(.focused):hover .edit-icon {{
      opacity: 1;
    }}

    .mates-settings button.mates-settings-btn {{
      min-width: 28px;
      min-height: 28px;
      padding: 4px;
      opacity: 0.75;
    }}

    .mates-settings button.mates-settings-btn:hover {{
      opacity: 1;
    }}

    .mates-settings-page {{
      margin-top: 4px;
    }}
    """

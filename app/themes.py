"""Global display appearance: theme, font, text size, and dashboard background."""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import AppSettings

THEMES: frozenset[str] = frozenset({
    "system",
    "light",
    "dark",
    "midnight",
    "catppuccin-mocha",
    "catppuccin-latte",
    "nord",
    "tokyo-night",
    "tokyo-night-light",
    "dracula",
    "gruvbox-dark",
    "gruvbox-light",
    "solarized-light",
    "solarized-dark",
    "rose-pine",
    "rose-pine-dawn",
    "everforest-dark",
    "everforest-light",
    "one-dark",
    "one-light",
    "ayu-mirage",
    "ayu-light",
})

THEME_OPTIONS: list[dict] = [
    {"id": "system", "label": "System", "kind": "auto",
     "swatches": ["#f5f5f5", "#e8e8e8", "#1a1a1a", "#3b82f6", "#22c55e"]},
    {"id": "light", "label": "Light", "kind": "light",
     "swatches": ["#fafafa", "#f0f0f0", "#141414", "#3b82f6", "#ef4444"]},
    {"id": "dark", "label": "Dark", "kind": "dark",
     "swatches": ["#141414", "#262626", "#f0f0f0", "#60a5fa", "#f87171"]},
    {"id": "midnight", "label": "Midnight", "kind": "dark",
     "swatches": ["#0b1020", "#141b2d", "#e8eefc", "#6ea8fe", "#c792ea"]},
    {"id": "catppuccin-mocha", "label": "Catppuccin Mocha", "kind": "dark",
     "swatches": ["#1e1e2e", "#313244", "#cdd6f4", "#89b4fa", "#cba6f7"]},
    {"id": "catppuccin-latte", "label": "Catppuccin Latte", "kind": "light",
     "swatches": ["#eff1f5", "#e6e9ef", "#4c4f69", "#1e66f5", "#8839ef"]},
    {"id": "nord", "label": "Nord", "kind": "dark",
     "swatches": ["#2e3440", "#3b4252", "#eceff4", "#88c0d0", "#b48ead"]},
    {"id": "tokyo-night", "label": "Tokyo Night", "kind": "dark",
     "swatches": ["#1a1b26", "#24283b", "#c0caf5", "#7aa2f7", "#bb9af7"]},
    {"id": "tokyo-night-light", "label": "Tokyo Night Light", "kind": "light",
     "swatches": ["#e1e2e7", "#d5d6db", "#3760bf", "#2e7de9", "#7847bd"]},
    {"id": "dracula", "label": "Dracula", "kind": "dark",
     "swatches": ["#282a36", "#44475a", "#f8f8f2", "#8be9fd", "#bd93f9"]},
    {"id": "gruvbox-dark", "label": "Gruvbox Dark", "kind": "dark",
     "swatches": ["#282828", "#3c3836", "#ebdbb2", "#83a598", "#d3869b"]},
    {"id": "gruvbox-light", "label": "Gruvbox Light", "kind": "light",
     "swatches": ["#fbf1c7", "#f2e5bc", "#3c3836", "#076678", "#8f3f71"]},
    {"id": "solarized-dark", "label": "Solarized Dark", "kind": "dark",
     "swatches": ["#002b36", "#073642", "#839496", "#268bd2", "#2aa198"]},
    {"id": "solarized-light", "label": "Solarized Light", "kind": "light",
     "swatches": ["#fdf6e3", "#eee8d5", "#657b83", "#268bd2", "#6c71c4"]},
    {"id": "rose-pine", "label": "Rosé Pine", "kind": "dark",
     "swatches": ["#191724", "#1f1d2e", "#e0def4", "#c4a7e7", "#eb6f92"]},
    {"id": "rose-pine-dawn", "label": "Rosé Pine Dawn", "kind": "light",
     "swatches": ["#faf4ed", "#fffaf3", "#575279", "#907aa9", "#b4637a"]},
    {"id": "everforest-dark", "label": "Everforest Dark", "kind": "dark",
     "swatches": ["#2d353b", "#343f44", "#d3c6aa", "#7fbbb3", "#a7c080"]},
    {"id": "everforest-light", "label": "Everforest Light", "kind": "light",
     "swatches": ["#fdf6e3", "#f4f0d9", "#5c6a72", "#3a94c5", "#8da101"]},
    {"id": "one-dark", "label": "One Dark", "kind": "dark",
     "swatches": ["#282c34", "#21252b", "#abb2bf", "#61afef", "#e06c75"]},
    {"id": "one-light", "label": "One Light", "kind": "light",
     "swatches": ["#fafafa", "#f0f0f0", "#383a42", "#4078f2", "#e45649"]},
    {"id": "ayu-mirage", "label": "Ayu Mirage", "kind": "dark",
     "swatches": ["#1f2430", "#232834", "#cbccc6", "#ffd580", "#f28779"]},
    {"id": "ayu-light", "label": "Ayu Light", "kind": "light",
     "swatches": ["#fafafa", "#f3f4f5", "#5c6166", "#f2ae49", "#f07171"]},
]

FONTS: frozenset[str] = frozenset({"system", "sans", "serif", "mono"})

FONT_OPTIONS: list[dict] = [
    {
        "id": "system",
        "label": "System default",
        "hint": "Matches your device's usual font",
        "sample": "The quick brown fox",
    },
    {
        "id": "sans",
        "label": "Clean sans",
        "hint": "Simple and easy to read on screens",
        "sample": "The quick brown fox",
    },
    {
        "id": "serif",
        "label": "Classic serif",
        "hint": "A book-like look with soft edges",
        "sample": "The quick brown fox",
    },
    {
        "id": "mono",
        "label": "Monospace",
        "hint": "Even spacing — great if you like a technical feel",
        "sample": "The quick brown fox",
    },
]

FONT_SIZES: frozenset[str] = frozenset({"sm", "md", "lg", "xl"})

FONT_SIZE_OPTIONS: list[dict] = [
    {"id": "sm", "label": "Smaller", "hint": "Pack more on the screen"},
    {"id": "md", "label": "Default", "hint": "Comfortable everyday size"},
    {"id": "lg", "label": "Larger", "hint": "Easier on the eyes"},
    {"id": "xl", "label": "Extra large", "hint": "Maximum readability"},
]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_BG_STEM = "dashboard-bg"
DASHBOARD_BG_MAX_BYTES = 5 * 1024 * 1024
DASHBOARD_BG_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
DEFAULT_DASHBOARD_BG_OPACITY = 0.35


def uploads_dir() -> Path:
    return Path(os.environ.get("PARA_SCOPE_UPLOADS_DIR", str(_PROJECT_ROOT / "data" / "uploads")))


def get_app_settings(db: Session) -> AppSettings:
    """Return the singleton AppSettings row, creating it if missing."""
    row = db.query(AppSettings).filter(AppSettings.id == 1).first()
    if not row:
        row = AppSettings(
            id=1,
            theme="system",
            font="system",
            font_size="md",
            dashboard_bg_opacity=DEFAULT_DASHBOARD_BG_OPACITY,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def get_theme(db: Session) -> str:
    theme = get_app_settings(db).theme or "system"
    return theme if theme in THEMES else "system"


def get_font(db: Session) -> str:
    font = getattr(get_app_settings(db), "font", None) or "system"
    return font if font in FONTS else "system"


def get_font_size(db: Session) -> str:
    size = getattr(get_app_settings(db), "font_size", None) or "md"
    return size if size in FONT_SIZES else "md"


def clamp_opacity(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DASHBOARD_BG_OPACITY
    return max(0.0, min(1.0, v))


def get_dashboard_bg_opacity(db: Session) -> float:
    raw = getattr(get_app_settings(db), "dashboard_bg_opacity", None)
    if raw is None:
        return DEFAULT_DASHBOARD_BG_OPACITY
    return clamp_opacity(raw)


def get_dashboard_bg_filename(db: Session) -> str | None:
    name = getattr(get_app_settings(db), "dashboard_bg_filename", None) or ""
    name = name.strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not name.startswith(DASHBOARD_BG_STEM + "."):
        return None
    path = uploads_dir() / name
    return name if path.is_file() else None


def dashboard_bg_path(filename: str | None = None) -> Path | None:
    name = filename
    if not name:
        return None
    return uploads_dir() / name


def appearance_context(db: Session) -> dict:
    """Values injected into every template for html data-* attributes."""
    bg = get_dashboard_bg_filename(db)
    return {
        "theme": get_theme(db),
        "font": get_font(db),
        "font_size": get_font_size(db),
        "dashboard_bg": bool(bg),
        "dashboard_bg_opacity": get_dashboard_bg_opacity(db),
    }


def _clear_dashboard_bg_files() -> None:
    d = uploads_dir()
    if not d.is_dir():
        return
    for p in d.glob(DASHBOARD_BG_STEM + ".*"):
        try:
            p.unlink()
        except OSError:
            pass


def remove_dashboard_bg(db: Session) -> None:
    settings = get_app_settings(db)
    settings.dashboard_bg_filename = None
    _clear_dashboard_bg_files()


def save_dashboard_bg(db: Session, data: bytes, content_type: str | None) -> tuple[str | None, str | None]:
    """Write uploaded image bytes. Returns (filename, error)."""
    if not data:
        return None, "The uploaded file was empty."
    if len(data) > DASHBOARD_BG_MAX_BYTES:
        return None, "Background image must be 5 MB or smaller."
    ct = (content_type or "").split(";")[0].strip().lower()
    # sniff from magic bytes if content-type missing/wrong
    ext = DASHBOARD_BG_TYPES.get(ct)
    if not ext:
        if data[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            ext = ".gif"
        else:
            return None, "Please upload a JPEG, PNG, WebP, or GIF image."
    d = uploads_dir()
    d.mkdir(parents=True, exist_ok=True)
    _clear_dashboard_bg_files()
    filename = DASHBOARD_BG_STEM + ext
    path = d / filename
    path.write_bytes(data)
    settings = get_app_settings(db)
    settings.dashboard_bg_filename = filename
    return filename, None


def update_style(
    db: Session,
    theme: str,
    font: str,
    font_size: str,
    *,
    dashboard_bg_opacity: float | None = None,
    clear_dashboard_bg: bool = False,
    dashboard_bg_bytes: bytes | None = None,
    dashboard_bg_content_type: str | None = None,
) -> tuple[dict | None, str | None]:
    """Persist appearance settings. Returns (saved, error)."""
    if theme not in THEMES:
        return None, "Please choose a valid color theme."
    if font not in FONTS:
        return None, "Please choose a valid font."
    if font_size not in FONT_SIZES:
        return None, "Please choose a valid text size."

    settings = get_app_settings(db)
    settings.theme = theme
    settings.font = font
    settings.font_size = font_size

    if dashboard_bg_opacity is not None:
        settings.dashboard_bg_opacity = clamp_opacity(dashboard_bg_opacity)

    if clear_dashboard_bg:
        remove_dashboard_bg(db)
    elif dashboard_bg_bytes is not None:
        _filename, err = save_dashboard_bg(db, dashboard_bg_bytes, dashboard_bg_content_type)
        if err:
            return None, err

    db.commit()
    return {
        "theme": theme,
        "font": font,
        "font_size": font_size,
        "dashboard_bg": bool(get_dashboard_bg_filename(db)),
        "dashboard_bg_opacity": get_dashboard_bg_opacity(db),
    }, None


if __name__ == "__main__":
    assert "nord" in THEMES and "not-a-theme" not in THEMES
    assert {o["id"] for o in THEME_OPTIONS} == THEMES
    assert {o["id"] for o in FONT_OPTIONS} == FONTS
    assert {o["id"] for o in FONT_SIZE_OPTIONS} == FONT_SIZES
    assert clamp_opacity("0.5") == 0.5
    assert clamp_opacity(2) == 1.0
    print("themes ok")

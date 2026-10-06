#!/usr/bin/env python3
"""Draws the README artwork into assets/: the logo, a header per plugin and a small icon per plugin, each in a
light and a dark version (READMEs pick one with <picture> and prefers-color-scheme). Icons are from Lucide
(https://lucide.dev, ISC license, notice in assets/LICENSE-lucide.txt), embedded as paths. Run after changing a plugin's name, command or tagline."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

# GitHub's own light and dark palettes, so the artwork sits naturally on a README page.
THEMES = {
    "light": {"bg": "#f6f8fa", "border": "#d0d7de", "text": "#1f2328", "muted": "#59636e", "chip": "#ffffff"},
    "dark": {"bg": "#151b23", "border": "#3d444d", "text": "#f0f6fc", "muted": "#9198a1", "chip": "#0d1117"},
}

# Lucide icon paths (24x24 viewBox, stroked).
ICONS = {
    "history": ['<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>', '<path d="M3 3v5h5"/>',
                '<path d="M12 7v5l4 2"/>'],
    "square-terminal": ['<path d="m7 11 2-2-2-2"/>', '<path d="M11 13h4"/>',
                        '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/>'],
    "shield-check": ['<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 '
                     '1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
                     '<path d="m9 12 2 2 4-4"/>'],
}

PLUGINS = [
    {"name": "remote-resume", "command": "/rresume", "icon": "history",
     "tagline": "Resume any Claude Code session from your phone",
     "accent": {"light": "#8250df", "dark": "#ab7df8"}},
    {"name": "remote-terminal", "command": "! or /!", "icon": "square-terminal",
     "tagline": "Run shell commands from the Claude app",
     "accent": {"light": "#1a7f37", "dark": "#3fb950"}},
    {"name": "mode-picker", "command": "/mode", "icon": "shield-check",
     "tagline": "Switch to any permission mode, Auto and Bypass included",
     "accent": {"light": "#bc4c00", "dark": "#f0883e"}},
]


def icon(name, x, y, size, color, width=2):
    scale = size / 24
    return (f'<g transform="translate({x} {y}) scale({scale})" fill="none" stroke="{color}" stroke-width="{width}" '
            f'stroke-linecap="round" stroke-linejoin="round">{"".join(ICONS[name])}</g>')


def tile(name, x, y, size, accent, theme):
    """A rounded square tinted with the accent, holding the icon."""
    pad = size * 0.22
    return (f'<rect x="{x}" y="{y}" width="{size}" height="{size}" rx="{size * 0.24}" fill="{accent}" '
            f'fill-opacity="{0.14 if theme == "light" else 0.2}" stroke="{accent}" stroke-opacity="0.45"/>'
            + icon(name, x + pad, y + pad, size - 2 * pad, accent))


def svg(width, height, body, label):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="{label}"><title>{label}</title>{body}</svg>\n')


def logo(theme):
    t = THEMES[theme]
    body = "".join(tile(p["icon"], 4 + i * 64, 4, 56, p["accent"][theme], theme) for i, p in enumerate(PLUGINS))
    body += (f'<text x="212" y="44" font-family="{FONT}" font-size="34" font-weight="700" fill="{t["text"]}" '
             f'letter-spacing="-0.5">claude-code-minis</text>')
    return svg(520, 64, body, "claude-code-minis")


def logo_card():
    """The logo on its own dark card, for viewers that ignore prefers-color-scheme (GitHub on mobile): its contrast
    does not depend on the page behind it."""
    t = THEMES["dark"]
    body = (f'<rect x="1" y="1" width="598" height="110" rx="20" fill="{t["bg"]}" stroke="{t["border"]}"/>'
            + "".join(tile(p["icon"], 28 + i * 60, 30, 52, p["accent"]["dark"], "dark") for i, p in enumerate(PLUGINS))
            + f'<text x="226" y="67" font-family="{FONT}" font-size="31" font-weight="700" fill="{t["text"]}" '
              f'letter-spacing="-0.5">claude-code-minis</text>')
    return svg(600, 112, body, "claude-code-minis")


def header(plugin, theme):
    t, accent = THEMES[theme], plugin["accent"][theme]
    chip_width = 24 + 11 * len(plugin["command"])
    clip = f'card-{plugin["name"]}-{theme}'
    body = (f'<clipPath id="{clip}"><rect x="1" y="1" width="878" height="138" rx="14"/></clipPath>'
            f'<rect x="1" y="1" width="878" height="138" rx="14" fill="{t["bg"]}"/>'
            f'<rect x="1" y="1" width="6" height="138" fill="{accent}" clip-path="url(#{clip})"/>'
            f'<rect x="1" y="1" width="878" height="138" rx="14" fill="none" stroke="{t["border"]}"/>'
            + tile(plugin["icon"], 36, 34, 72, accent, theme)
            + f'<text x="132" y="66" font-family="{FONT}" font-size="34" font-weight="700" fill="{t["text"]}" '
              f'letter-spacing="-0.5">{plugin["name"]}</text>'
            + f'<text x="132" y="102" font-family="{FONT}" font-size="19" fill="{t["muted"]}">{plugin["tagline"]}</text>'
            + f'<rect x="{856 - chip_width}" y="36" width="{chip_width}" height="36" rx="18" fill="{t["chip"]}" '
              f'stroke="{accent}" stroke-opacity="0.6"/>'
            + f'<text x="{856 - chip_width / 2}" y="60" text-anchor="middle" font-family="{MONO}" font-size="17" '
              f'font-weight="600" fill="{accent}">{plugin["command"]}</text>')
    return svg(880, 140, body, f'{plugin["name"]}: {plugin["tagline"]}')


def small_icon(plugin, theme):
    return svg(48, 48, tile(plugin["icon"], 2, 2, 44, plugin["accent"][theme], theme), plugin["name"])


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "logo-card.svg").write_text(logo_card())
    for theme in THEMES:
        (OUT / f"logo-{theme}.svg").write_text(logo(theme))
        for p in PLUGINS:
            (OUT / f"{p['name']}-header-{theme}.svg").write_text(header(p, theme))
            (OUT / f"{p['name']}-icon-{theme}.svg").write_text(small_icon(p, theme))
    print("\n".join(sorted(f.name for f in OUT.glob("*.svg"))))


if __name__ == "__main__":
    main()

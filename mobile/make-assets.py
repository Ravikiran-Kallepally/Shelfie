"""Render Shelfie's logo into the PNGs @capacitor/assets turns into Android icons and splash screens.
Run with any Python that has Playwright:  python make-assets.py  then  npx capacitor-assets generate --android"""
import pathlib

from playwright.sync_api import sync_playwright

OUT = pathlib.Path(__file__).parent / "assets"
OUT.mkdir(exist_ok=True)
# the open book from static/icon.svg, without its rounded square
BOOK = '''<path d="M96 150c56-22 112-22 160 10v228c-48-32-104-32-160-10z" fill="#fffdf8"/>
  <path d="M416 150c-56-22-112-22-160 10v228c48-32 104-32 160-10z" fill="#f4e6d4"/>
  <path d="M256 160v228" stroke="#8a3b1c" stroke-width="10" stroke-linecap="round"/>'''


def svg(inner, size, bg=None, scale=1.0):
    pad = 512 * (1 - scale) / 2
    rect = f'<rect width="512" height="512" fill="{bg}"/>' if bg else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 512 512">{rect}'
            f'<g transform="translate({pad},{pad}) scale({scale})">{inner}</g></svg>')


def splash(bg, size=2732):
    icon = svg(f'<rect width="512" height="512" rx="112" fill="#b4532a"/>{BOOK}', 560)
    return (f'<div style="width:{size}px;height:{size}px;background:{bg};display:grid;place-items:center">{icon}</div>')


jobs = {
    # legacy / full icon: the rounded terracotta square from the web app
    "icon-only.png": (svg(f'<rect width="512" height="512" rx="112" fill="#b4532a"/>{BOOK}', 1024), 1024),
    # adaptive icon: the book sits inside Android's safe zone (centre ~60%) on a solid layer
    "icon-foreground.png": (svg(BOOK, 1024, scale=0.62), 1024),
    "icon-background.png": (svg("", 1024, bg="#b4532a"), 1024),
    "splash.png": (splash("#f6f1e7"), 2732),
    "splash-dark.png": (splash("#1c1814"), 2732),
}
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    for name, (html, size) in jobs.items():
        pg = b.new_page(viewport={"width": size, "height": size})
        pg.set_content(f'<body style="margin:0;background:transparent">{html}</body>')
        pg.screenshot(path=str(OUT / name), omit_background=True)
        print("wrote", name)
    b.close()

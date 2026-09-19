from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index.html").read_text()
CSS = (ROOT / "static" / "styles.css").read_text()
LOCK = json.loads((ROOT / "docs" / "brand" / "HIRDA_BRAND_REFERENCE_LOCK.json").read_text())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reference_lock_identifies_exact_approved_board():
    board = LOCK["source_board"]
    assert LOCK["brand"] == "HIRDA"
    assert LOCK["status"] == "approved"
    assert board["filename"] == "ChatGPT Image Sep 19, 2026, 03_18_05 PM.png"
    assert (board["width"], board["height"]) == (1536, 1024)
    assert board["sha256"] == "0c3093bc30a03381e4f7c1d02dd1e4ba02c56e7973b25f7d930353d9e71eb9bf"
    assert board["role"] == "human_visual_source_of_truth"

    # The original approved board lives in the design-review source.
    # This lock records its exact identity so production assets can be checked
    # without silently substituting a regenerated board.
def test_sidebar_uses_approved_image_lockups_not_redrawn_mark():
    sidebar = LOCK["sidebar"]
    light = sidebar["light_asset"]
    dark = sidebar["dark_asset"]

    assert light in HTML
    assert dark in HTML
    light_path = ROOT / light.lstrip("/")
    dark_path = ROOT / dark.lstrip("/")
    assert light_path.is_file()
    assert dark_path.is_file()
    assert sha256(light_path) == sidebar["light_asset_sha256"]
    assert sha256(dark_path) == sidebar["dark_asset_sha256"]

    assert '/static/brand/hirda-sidebar-light.svg' not in HTML
    assert '/static/brand/hirda-sidebar-dark.svg' not in HTML
    assert '/static/brand/hirda-sidebar-approved-dark.webp' not in HTML
    assert 'class="brand brand-lockup"' in HTML
    assert "object-fit:contain" in CSS
    assert "filter:none!important" in CSS


def test_core_theme_tokens_match_reference_lock():
    colors = LOCK["colors"]
    expected = {
        "primary": "--hirda-brand-primary",
        "secondary": "--hirda-brand-secondary",
        "accent": "--hirda-brand-accent",
        "dark_bg": "--hirda-brand-dark",
        "light_bg": "--hirda-brand-light",
    }
    for key, css_name in expected.items():
        assert f"{css_name}:{colors[key]}" in CSS

    assert colors["success"] == "#10A37F"
    assert colors["warning"] == "#D97706"
    assert colors["danger"] == "#DC2626"


def test_typefaces_match_reference_lock():
    typography = LOCK["typography"]
    assert typography["ui"] == "Kanit"
    assert typography["technical"] == "JetBrains Mono"
    assert "family=Kanit" in HTML
    assert "family=JetBrains+Mono" in HTML
    assert '"JetBrains Mono"' in CSS


def test_light_and_dark_sidebar_follow_reference_board():
    assert LOCK["theme_rules"]["light_sidebar_is_light"] is True
    assert LOCK["theme_rules"]["dark_sidebar_is_dark"] is True

    assert 'html[data-theme="light"] .focus-sidebar' in CSS
    assert 'background:var(--hirda-brand-light)' in CSS
    assert 'html[data-theme="dark"] .focus-sidebar' in CSS
    assert 'background:var(--hirda-brand-dark)' in CSS

    assert 'html[data-theme="light"] .focus-sidebar .brand-lockup-image .brand-lockup-light{display:block}' in CSS
    assert 'html[data-theme="light"] .focus-sidebar .brand-lockup-image .brand-lockup-dark{display:none}' in CSS
    assert 'html[data-theme="dark"] .focus-sidebar .brand-lockup-image .brand-lockup-light{display:none}' in CSS
    assert 'html[data-theme="dark"] .focus-sidebar .brand-lockup-image .brand-lockup-dark{display:block}' in CSS

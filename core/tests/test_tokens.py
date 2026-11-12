import re
from pathlib import Path

from django.test import SimpleTestCase

CSS = Path(__file__).resolve().parent.parent.parent / "static" / "css" / "tokens.css"


def parse_blocks():
    text = CSS.read_text()
    root = re.search(r":root\s*\{(.*?)\}", text, re.S).group(1)
    dark = re.search(r'\[data-theme="dark"\]\s*\{(.*?)\}', text, re.S).group(1)

    def vars_of(block):
        return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))

    light = vars_of(root)
    return light, {**light, **vars_of(dark)}


def luminance(hex_color):
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    hi, lo = sorted([luminance(a), luminance(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


PAIRS = [
    ("text", "bg"),
    ("text", "surface"),
    ("text", "surface-2"),
    ("text", "input-bg"),
    ("text-muted", "bg"),
    ("text-muted", "surface"),
    ("text-muted", "input-bg"),
    ("primary", "bg"),
    ("on-primary", "primary"),
    ("link", "bg"),
    ("link", "surface"),
    ("link", "surface-2"),
    ("danger", "bg"),
    ("danger", "surface"),
    ("accent-green", "bg"),
    ("sidebar-text", "sidebar-bg"),
]
# Non-text contrast (WCAG 1.4.11): input boundaries and the focus ring need 3:1.
UI_PAIRS = [
    ("input-border", "bg"),
    ("input-border", "surface"),
    ("input-border", "input-bg"),
    ("primary", "bg"),
    ("primary", "surface"),
    ("primary", "input-bg"),
]


class DesignTokenContrastTests(SimpleTestCase):
    def test_wcag_aa_in_both_themes(self):
        light, dark = parse_blocks()
        for theme_name, tokens in (("light", light), ("dark", dark)):
            for fg, bg in PAIRS:
                ratio = contrast(tokens[fg], tokens[bg])
                self.assertGreaterEqual(
                    ratio, 4.5, f"{theme_name}: {fg} on {bg} = {ratio:.2f} (< 4.5)"
                )

    def test_interface_boundaries_and_focus_ring_meet_3_to_1(self):
        light, dark = parse_blocks()
        for theme_name, tokens in (("light", light), ("dark", dark)):
            for fg, bg in UI_PAIRS:
                ratio = contrast(tokens[fg], tokens[bg])
                self.assertGreaterEqual(
                    ratio, 3.0, f"{theme_name}: {fg} on {bg} = {ratio:.2f} (< 3.0)"
                )

"""Rendered components must take their colours from the tokens (not Bootstrap's blue/white)."""

import re
from pathlib import Path

from django.test import SimpleTestCase

STATIC = Path(__file__).resolve().parent.parent.parent / "static" / "css"
APP = (STATIC / "app.css").read_text()


def rule(selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", APP)
    assert match, f"CSS rule not found: {selector}"
    return match.group(1)


class ThemeWiringTests(SimpleTestCase):
    def test_app_css_has_no_hard_coded_colours(self):
        stripped = re.sub(r'url\("data:[^"]*"\)', "", APP)  # the dark select arrow is an image
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", stripped), [])

    def test_links_use_the_accessible_link_token(self):
        self.assertIn("color: var(--link)", rule("a"))

    def test_tables_and_headers_use_tokens(self):
        table = rule(".table")
        for needle in (
            "--bs-table-color: var(--text)",
            "--bs-table-bg: transparent",
            "--bs-table-border-color: var(--border)",
        ):
            self.assertIn(needle, table)
        header = rule(".table thead th")
        self.assertIn("background: var(--surface-2)", header)
        self.assertIn("color: var(--text)", header)

    def test_inputs_use_tokens_and_a_visible_focus_state(self):
        controls = rule(".form-control, .form-select")
        for needle in ("var(--input-bg)", "var(--text)", "var(--input-border)"):
            self.assertIn(needle, controls)
        focus = rule(".form-control:focus, .form-select:focus")
        self.assertIn("outline: 3px solid var(--primary)", focus)
        self.assertIn("outline: 3px solid var(--primary)", rule(":focus-visible"))
        self.assertIn("var(--primary)", rule(".form-check-input:checked"))

    def test_error_text_uses_the_danger_token(self):
        self.assertIn("var(--danger)", rule(".text-danger"))

    def test_bootstrap_body_variables_follow_the_theme(self):
        root = rule(":root")
        self.assertIn("--bs-body-bg: var(--bg)", root)
        self.assertIn("--bs-body-color: var(--text)", root)

    def test_small_screens_stack_the_sidebar(self):
        self.assertIn("@media (max-width: 768px)", APP)
        self.assertRegex(APP, r"\.app-shell\s*\{\s*flex-direction:\s*column")

    def test_login_template_loads_both_stylesheets_in_order(self):
        base = (STATIC.parent.parent / "templates" / "base.html").read_text()
        self.assertLess(base.index("bootstrap.min.css"), base.index("css/tokens.css"))
        self.assertLess(base.index("css/tokens.css"), base.index("css/app.css"))

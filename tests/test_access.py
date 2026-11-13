from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse

# Routes that may be reached without logging in (extended in Phase 1b with login/reset routes).
PUBLIC_URL_NAMES = {
    "accounts:login",
    "accounts:password_reset",
    "accounts:password_reset_done",
    "accounts:password_reset_confirm",
    "accounts:password_reset_complete",
    "accounts:recover",
    "accounts:logout",
}


def iter_patterns(patterns, prefix="", namespace=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            ns = f"{namespace}{p.namespace}:" if p.namespace else namespace
            yield from iter_patterns(p.url_patterns, prefix + str(p.pattern), ns)
        elif isinstance(p, URLPattern):
            yield f"{namespace}{p.name}" if p.name else None, prefix + str(p.pattern), p


class LayoutAndAccessTests(TestCase):
    def test_every_route_requires_login_except_public_allowlist(self):
        for name, route, _ in iter_patterns(get_resolver().url_patterns):
            if name in PUBLIC_URL_NAMES or "<" in route:
                continue
            url = "/" + route
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, f"{url} is reachable without login")
            self.assertIn("/accounts/login/", response["Location"], url)

    def test_dashboard_renders_layout_for_owner(self):
        user = get_user_model().objects.create_user("owner", password="pw-Strong-123!")
        self.client.force_login(user)
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Coven&#x27;s Library", html)
        self.assertNotIn("Coffee", html)
        for item in ("Dashboard", "Members", "Books &amp; Inventory", "Settings"):
            self.assertIn(item, html)
        self.assertIn("theme-toggle", html)


class TopBarAndLoginLayoutTests(TestCase):
    """UI-only: no repeated page title in the top bar, the control is called Mode, and the sign-in
    form is laid out with the spacing classes."""

    def topbar(self, html):
        return html.split('<header class="topbar">')[1].split("</header>")[0]

    def test_authenticated_pages_have_no_page_title_in_the_top_bar(self):
        user = get_user_model().objects.create_user("owner", password="pw-Strong-123!")
        self.client.force_login(user)
        for name in (
            "core:dashboard",
            "core:settings",
            "core:calendar",
            "members:list",
            "reports:index",
            "communications:list",
        ):
            html = self.client.get(reverse(name)).content.decode()
            bar = self.topbar(html)
            self.assertNotIn("<strong>", bar, name)  # no heading in the bar
            self.assertIn("Sign out", bar, name)
            self.assertIn('id="theme-toggle"', bar, name)
            self.assertIn(">Mode</button>", bar, name)
            self.assertNotIn(">Theme<", html, name)
            if name in {"core:dashboard", "core:settings"}:
                self.assertIn('aria-current="page"', html.split("</nav>")[0], name)

    def test_the_login_page_has_no_sign_in_heading_and_a_roomier_form(self):
        html = self.client.get(reverse("accounts:login")).content.decode()
        self.assertNotIn("<strong>", self.topbar(html))  # no "Sign in" title in the bar
        self.assertEqual(html.split("<body>")[1].count("Sign in"), 1)  # only the submit button
        self.assertIn('class="login-form"', html)
        self.assertEqual(html.count('class="login-field"'), 2)
        self.assertIn(">Mode</button>", html)

    def test_other_signed_out_pages_keep_their_own_heading(self):
        html = self.client.get(reverse("accounts:password_reset")).content.decode()
        self.assertIn("<strong>", self.topbar(html))

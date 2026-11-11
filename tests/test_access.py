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

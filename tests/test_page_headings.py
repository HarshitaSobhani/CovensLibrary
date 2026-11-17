"""O-08: every main signed-in screen has a meaningful document title and exactly one h1."""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalogue.models import Category

SCREENS = (
    "core:dashboard",
    "core:settings",
    "core:calendar",
    "members:list",
    "members:create",
    "catalogue:edition_list",
    "catalogue:edition_create",
    "catalogue:category_list",
    "catalogue:import_start",
    "circulation:list",
    "memberships:register",
    "reports:index",
    "communications:list",
)


class PageHeadingTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("owner", password="pw-Strong-123!")
        self.client.force_login(user)
        Category.objects.create(name="Fiction")

    def test_each_signed_in_screen_has_a_page_title_and_one_primary_heading(self):
        for name in SCREENS:
            html = self.client.get(reverse(name)).content.decode()
            title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
            self.assertIn(" - ", title, f"{name}: title '{title}' is not page-specific")
            self.assertTrue(title.endswith("Coven&#x27;s Library"), name)
            heads = re.findall(r"<h1\b[^>]*>(.*?)</h1>", html, re.S)
            self.assertEqual(len(heads), 1, f"{name}: expected one h1, found {len(heads)}")
            self.assertTrue(re.sub(r"<[^>]+>", "", heads[0]).strip(), name)

    def test_the_heading_is_hidden_visually_but_not_from_screen_readers(self):
        html = self.client.get(reverse("members:list")).content.decode()
        bar = html.split('<header class="topbar">')[1].split("</header>")[0]
        self.assertIn('<h1 class="visually-hidden">', bar)  # not displayed
        self.assertNotIn("<strong>", bar)  # no visible page name in the top bar

    def test_signed_out_pages_keep_their_visible_top_bar_title(self):
        self.client.logout()
        html = self.client.get(reverse("accounts:password_reset")).content.decode()
        self.assertIn("<strong>Reset password</strong>", html)


class BrowserIconTests(TestCase):
    def test_pages_declare_the_c_icon_and_the_files_exist(self):
        from pathlib import Path

        self.client.force_login(
            get_user_model().objects.create_user("icon", password="pw-Strong-123!")
        )
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn('rel="icon" href="/static/favicon.svg"', html)
        self.assertIn('rel="alternate icon" href="/static/favicon.png"', html)
        static = Path(__file__).resolve().parent.parent / "static"
        self.assertTrue((static / "favicon.svg").is_file())
        self.assertTrue((static / "favicon.png").is_file())
        ico = Path(__file__).resolve().parent.parent / "deploy" / "windows" / "coven-c.ico"
        self.assertEqual(ico.read_bytes()[:4], b"\x00\x00\x01\x00")  # a real .ico header

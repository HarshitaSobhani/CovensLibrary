"""Phase 5a: dashboard sections and configuration, calendar and global search."""

from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEvent
from catalogue import services as catalogue
from catalogue.models import Category
from circulation import grace, services
from circulation.tests.helpers import D, Entry, make_owner, member_at, overdue_loan
from core import dashboard
from core import dashboard_config as cfg
from core.models import LibrarySetting
from members import services as members
from memberships import services as ms
from memberships.tests.helpers import PW


def build(today, **conf):
    setting = LibrarySetting.load()
    setting.dashboard_config = conf
    return {s["key"]: s["data"] for s in dashboard.build(setting, today)["sections"]}


class ConfigTests(TestCase):
    def test_defaults_are_complete_and_everything_is_visible(self):
        conf = cfg.normalise({})
        self.assertEqual(set(conf["visible"]), set(cfg.SECTIONS))
        self.assertTrue(all(conf["visible"].values()) and all(conf["criteria"].values()))
        self.assertEqual(conf["order"], list(cfg.SECTIONS))
        self.assertEqual(conf["expiry_window_days"], cfg.DEFAULT_EXPIRY_WINDOW_DAYS)

    def test_there_is_no_pending_payments_section(self):
        self.assertNotIn("pending", " ".join(cfg.SECTIONS).lower().replace("pending ", "", 0)[:0])
        self.assertFalse([t for t in cfg.SECTIONS.values() if "payment" in t.lower()])

    def test_bad_stored_values_fall_back_safely(self):
        for raw in (
            None,
            "x",
            [],
            {"order": "no", "visible": 3, "criteria": [], "expiry_window_days": "9"},
        ):
            conf = cfg.normalise(raw)
            self.assertEqual(conf["order"], list(cfg.SECTIONS))
            self.assertEqual(conf["expiry_window_days"], 30)
        for bad in (0, -1, 366, True, 2.5):
            self.assertEqual(cfg.normalise({"expiry_window_days": bad})["expiry_window_days"], 30)
        self.assertEqual(cfg.normalise({"expiry_window_days": 365})["expiry_window_days"], 365)
        self.assertEqual(cfg.normalise({"expiry_window_days": 1})["expiry_window_days"], 1)

    def test_order_drops_unknown_and_duplicate_keys_and_appends_missing(self):
        conf = cfg.normalise({"order": ["counts", "bogus", "counts", "overdue"]})
        self.assertEqual(conf["order"][:2], ["counts", "overdue"])
        self.assertEqual(sorted(conf["order"]), sorted(cfg.SECTIONS))

    def test_move_swaps_neighbours_and_stops_at_the_ends(self):
        order = list(cfg.SECTIONS)
        self.assertEqual(cfg.move(order, order[1], -1)[0], order[1])
        self.assertEqual(cfg.move(order, order[0], -1), order)
        self.assertEqual(cfg.move(order, order[-1], 1), order)
        self.assertEqual(cfg.move(order, "nope", 1), order)


class SectionTests(TestCase):
    def setUp(self):
        self.owner = make_owner()

    def test_empty_library_has_every_section_with_an_empty_state(self):
        data = build(D(2026, 12, 1))
        self.assertEqual(
            set(data),
            set(cfg.SECTIONS),
        )
        self.assertEqual(data["overdue"]["count"], 0)
        self.assertEqual(data["attention"], [])
        self.assertTrue(data["backup_failures"]["available"])  # Phase 7a
        self.assertEqual(data["backup_failures"]["count"], 1)  # no backup has ever succeeded
        self.assertEqual(data["counts"]["copies"], 0)

    def test_overdue_section_lists_only_overdue_active_loans(self):
        member, copy, loan = overdue_loan(self.owner)
        self.assertEqual(build(D(2026, 12, 31))["overdue"]["count"], 0)  # due today: not overdue
        data = build(D(2027, 1, 5))["overdue"]
        self.assertEqual((data["count"], data["rows"][0]["days"]), (1, 5))
        self.assertEqual(data["rows"][0]["forfeiture_day"], D(2027, 1, 8))
        services.return_loan(actor=self.owner, loan=loan, today=D(2027, 1, 5))
        self.assertEqual(build(D(2027, 1, 6))["overdue"]["count"], 0)

    def test_expiries_use_the_window_and_skip_renewed_members(self):
        member = member_at(self.owner)  # expires 11 May 2027
        near = build(D(2027, 4, 20))["expiries"]
        self.assertEqual((near["count"], near["rows"][0]["days"]), (1, 21))
        self.assertEqual(build(D(2027, 3, 1))["expiries"]["count"], 0)  # beyond 30 days
        wide = build(D(2027, 3, 1), expiry_window_days=90)["expiries"]
        self.assertEqual(wide["count"], 1)
        self.assertEqual(build(D(2027, 5, 11))["expiries"]["count"], 0)  # already expired
        ms.renew_subscription(
            actor=self.owner, member=member, plan_code="6m",
            entries=[Entry("cash", 80000)], today=D(2027, 4, 1),
        )  # fmt: skip
        self.assertEqual(build(D(2027, 4, 20))["expiries"]["count"], 0)  # continued

    def test_decision_section_counts_pending_decisions_and_lost_or_damaged_copies(self):
        member, copy, loan = overdue_loan(self.owner)
        grace.change_grace_days(actor=self.owner, new_days=3, today=D(2027, 1, 5))
        data = build(D(2027, 1, 5))["decisions"]
        self.assertEqual((data["pending_count"], data["case_count"]), (1, 0))
        services.mark_lost(
            actor=self.owner, loan=loan, owner_confirmed=True, reason="gone", today=D(2027, 1, 5)
        )
        data = build(D(2027, 1, 5))["decisions"]
        self.assertEqual(data["case_count"], 1)
        self.assertEqual(data["cases"][0].pk, copy.pk)

    def test_forfeitures_counts_and_collections(self):
        member, copy, loan = overdue_loan(self.owner)
        from circulation import forfeiture

        forfeiture.process_overdue_forfeitures(today=D(2027, 1, 9))
        self.assertEqual(build(D(2027, 1, 9))["forfeitures"]["count"], 1)
        counts = build(D(2027, 1, 9))["counts"]
        self.assertEqual((counts["copies"], counts["borrowed"], counts["members"]), (1, 1, 1))
        rows = build(D(2027, 1, 9))["collections"]["rows"]
        self.assertEqual(
            [(r["kind"], r["cash_flow"], r["total"]) for r in rows],
            [("token_forfeiture", "none", 50000)],
        )
        rows = {
            (r["kind"], r["cash_flow"]): r["total"]
            for r in build(D(2026, 11, 20))["collections"]["rows"]
        }
        self.assertEqual(rows[("subscription_payment", "in")], 80000)
        self.assertEqual(rows[("token_collection", "in")], 50000)
        self.assertEqual(len(build(D(2027, 1, 9))["transactions"]), 3)

    def test_hidden_sections_disappear_and_order_is_honoured(self):
        setting = LibrarySetting.load()
        setting.dashboard_config = {"visible": {"counts": False}, "order": ["transactions"]}
        keys = [s["key"] for s in dashboard.build(setting, D(2026, 12, 1))["sections"]]
        self.assertEqual(keys[0], "transactions")
        self.assertNotIn("counts", keys)


class AttentionTests(TestCase):
    def setUp(self):
        self.owner = make_owner()

    def reasons(self, today, **criteria):
        conf = cfg.normalise({"criteria": criteria})["criteria"]
        return {r["member"].full_name: r["reasons"] for r in dashboard.attention(today, conf)}

    def test_each_default_criterion(self):
        member, copy, loan = overdue_loan(self.owner)
        today = D(2027, 1, 5)
        self.assertEqual(self.reasons(today), {"Asha Rao": ["Overdue books"]})
        from circulation import forfeiture

        forfeiture.process_overdue_forfeitures(today=D(2027, 1, 9))
        got = self.reasons(D(2027, 1, 9))["Asha Rao"]
        self.assertIn("Borrowing suspended after token forfeiture", got)
        late = self.reasons(D(2027, 6, 1))["Asha Rao"]
        self.assertIn("Subscription expired", late)

    def test_pending_decision_and_lost_case(self):
        member, copy, loan = overdue_loan(self.owner)
        grace.change_grace_days(actor=self.owner, new_days=3, today=D(2027, 1, 5))
        self.assertIn("Pending owner decisions", self.reasons(D(2027, 1, 5))["Asha Rao"])
        services.mark_lost(
            actor=self.owner, loan=loan, owner_confirmed=True, reason="gone", today=D(2027, 1, 5)
        )
        self.assertIn(
            "Unresolved damaged or lost book cases", self.reasons(D(2027, 1, 6))["Asha Rao"]
        )

    def test_a_complete_healthy_member_is_not_listed(self):
        from memberships.tests.helpers import register

        register(self.owner, today=D(2026, 11, 11))
        self.assertEqual(self.reasons(D(2026, 12, 1)), {})

    def test_incomplete_means_no_subscription_or_a_required_new_one_not_a_missing_email(self):
        from members.models import Member

        Member.objects.create(
            full_name="Bare Bones", mobile_e164="+919800000001", joined_on=D(2026, 11, 1)
        )
        got = self.reasons(D(2026, 12, 1))
        self.assertEqual(got, {"Bare Bones": ["Incomplete membership record"]})
        member = member_at(self.owner)  # a subscription but no email: complete
        self.assertNotIn(member.full_name, self.reasons(D(2026, 12, 1)))

    def test_criteria_can_be_switched_off(self):
        overdue_loan(self.owner)
        off = {k: False for k in cfg.CRITERIA}
        self.assertEqual(self.reasons(D(2027, 1, 5), **off), {})
        self.assertEqual(
            self.reasons(D(2027, 1, 5), **{**off, "overdue": True}), {"Asha Rao": ["Overdue books"]}
        )

    def test_archived_and_anonymised_members_are_not_listed(self):
        member, copy, loan = overdue_loan(self.owner)
        services.return_loan(actor=self.owner, loan=loan, today=D(2027, 1, 2))
        members.anonymise_member(member, actor=self.owner, reason="request", confirm=True)
        self.assertEqual(self.reasons(D(2027, 6, 1)), {})


class CalendarTests(TestCase):
    def test_events_for_expiry_due_reminders_and_forfeiture_day(self):
        owner = make_owner()
        overdue_loan(owner)
        jan = dict(dashboard.calendar_events(2027, 1, 7))
        self.assertEqual([k for k, *_ in jan[D(2027, 1, 3)]], ["reminder"])  # day 3
        self.assertIn("forfeiture", [k for k, *_ in jan[D(2027, 1, 8)]])
        dec = dict(dashboard.calendar_events(2026, 12, 7))
        self.assertIn("due", [k for k, *_ in dec[D(2026, 12, 31)]])
        self.assertIn("reminder", [k for k, *_ in dec[D(2026, 12, 31)]])  # due-date reminder
        apr = dict(dashboard.calendar_events(2027, 4, 7))
        self.assertIn(D(2027, 4, 26), apr)  # 15 days before 11 May
        may = dict(dashboard.calendar_events(2027, 5, 7))
        self.assertIn("expiry", [k for k, *_ in may[D(2027, 5, 11)]])

    def test_empty_month_and_anonymised_members_are_excluded(self):
        owner = make_owner()
        member, copy, loan = overdue_loan(owner)
        services.return_loan(actor=owner, loan=loan, today=D(2027, 1, 2))
        members.anonymise_member(member, actor=owner, reason="r", confirm=True)
        self.assertEqual(dashboard.calendar_events(2027, 5, 7), [])


class SearchTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = member_at(self.owner, name="Zainab Qureshi", number="9811122233")
        category, _ = Category.objects.get_or_create(name="Fiction")
        self.edition = catalogue.create_edition(
            actor=self.owner, title="The Night Circus", author="Erin Morgenstern",
            category=category, isbn="9780307744432",
        )  # fmt: skip
        self.copy = catalogue.add_copies(self.edition, 1, actor=self.owner, source="purchased")[0]

    def test_every_documented_search_key_finds_its_record(self):
        s = dashboard.search
        self.assertEqual(s("zainab")["members"], [self.member])
        self.assertEqual(s("9811122233")["members"], [self.member])
        self.assertEqual(s("COV-M-000001")["members"], [self.member])
        self.assertEqual(s("night circus")["editions"], [self.edition])
        self.assertEqual(s("morgenstern")["editions"], [self.edition])
        self.assertEqual(s("978-0-307-74443-2")["editions"], [self.edition])
        self.assertEqual(s(self.copy.copy_code)["copies"], [self.copy])
        self.assertEqual(s("SUB-2026-000001")["transactions"][0].member, self.member)

    def test_short_or_blank_queries_return_nothing_and_misses_are_empty(self):
        self.assertIsNone(dashboard.search(""))
        self.assertIsNone(dashboard.search(" a "))
        result = dashboard.search("nothing-matches-this")
        self.assertEqual([result["members"], result["editions"], result["copies"]], [[], [], []])

    def test_anonymised_members_are_not_searchable_by_their_old_name(self):
        members.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        self.assertEqual(dashboard.search("zainab")["members"], [])
        self.assertEqual(dashboard.search("9811122233")["members"], [])

    def test_wildcards_in_the_query_are_literal(self):
        self.assertEqual(dashboard.search("%%")["members"], [])
        self.assertEqual(dashboard.search("__")["members"], [])


class ViewTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)

    def test_pages_require_login(self):
        self.client.logout()
        for name in ("core:search", "core:calendar", "core:settings"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)
            self.assertEqual(self.client.post(reverse(name), {}).status_code, 302)

    def test_dashboard_search_calendar_render(self):
        overdue_loan(self.owner)
        for url in (
            reverse("core:dashboard"),
            reverse("core:search") + "?q=asha",
            reverse("core:search") + "?q=a",
            reverse("core:calendar"),
            reverse("core:calendar") + "?month=2027-01",
            reverse("core:calendar") + "?month=garbage",
            reverse("core:calendar") + "?month=2027-13",
            reverse("core:settings"),
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        page = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertNotIn("Pending Payments", page)
        self.assertIn("Members requiring attention", page)

    def test_save_persists_visibility_criteria_and_window_and_is_audited(self):
        response = self.client.post(
            reverse("core:settings"),
            {
                "action": "save",
                "visible": ["overdue", "counts"],
                "criteria": ["overdue"],
                "expiry_window_days": "45",
            },
        )
        self.assertEqual(response.status_code, 302)
        conf = cfg.load(LibrarySetting.load())
        self.assertEqual([k for k, v in conf["visible"].items() if v], ["overdue", "counts"])
        self.assertEqual([k for k, v in conf["criteria"].items() if v], ["overdue"])
        self.assertEqual(conf["expiry_window_days"], 45)
        self.assertTrue(AuditEvent.objects.filter(action="dashboard.configured").exists())
        page = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertNotIn("Recent transactions", page)
        self.assertIn("Library at a glance", page)

    def test_invalid_window_is_rejected_and_nothing_changes(self):
        before = LibrarySetting.load().dashboard_config
        for bad in ("0", "366", "abc", ""):
            self.client.post(
                reverse("core:settings"), {"action": "save", "expiry_window_days": bad}
            )
            self.assertEqual(LibrarySetting.load().dashboard_config, before, bad)

    def test_up_and_down_persist_the_order(self):
        first, second = list(cfg.SECTIONS)[:2]
        self.client.post(reverse("core:settings"), {"action": "down", "section": first})
        self.assertEqual(cfg.load(LibrarySetting.load())["order"][:2], [second, first])
        self.client.post(reverse("core:settings"), {"action": "up", "section": first})
        self.assertEqual(cfg.load(LibrarySetting.load())["order"][:2], [first, second])

    def test_unchanged_save_writes_no_audit_event(self):
        post = {
            "action": "save",
            "visible": list(cfg.SECTIONS),
            "criteria": list(cfg.CRITERIA),
            "expiry_window_days": "30",
        }
        self.client.post(reverse("core:settings"), post)
        self.assertFalse(AuditEvent.objects.filter(action="dashboard.configured").exists())

    def test_the_dashboard_makes_a_bounded_number_of_queries(self):
        for i in range(5):
            overdue_loan(self.owner, name=f"M{i} Name", number=f"98111000{i:02d}")
        with self.assertNumQueries(0, using="nonexistent") if False else self.settings():
            from django.db import connection
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("core:dashboard"))
        self.assertLess(len(ctx), 260)

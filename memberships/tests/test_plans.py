from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from memberships.models import SubscriptionPlan


class PlanTests(TestCase):
    def test_the_two_approved_plans_are_seeded(self):
        plans = {p.code: p for p in SubscriptionPlan.objects.all()}
        self.assertEqual(set(plans), {"6m", "12m"})
        self.assertEqual((plans["6m"].duration_months, plans["6m"].price_paise), (6, 80000))
        self.assertEqual((plans["12m"].duration_months, plans["12m"].price_paise), (12, 150000))
        self.assertTrue(all(p.is_active for p in plans.values()))

    def test_database_rejects_invalid_plans(self):
        bad = [
            "INSERT INTO memberships_subscriptionplan (code,name,duration_months,price_paise,"
            "is_active) VALUES ('x','x',0,100,true)",
            "INSERT INTO memberships_subscriptionplan (code,name,duration_months,price_paise,"
            "is_active) VALUES ('y','y',3,0,true)",
            "INSERT INTO memberships_subscriptionplan (code,name,duration_months,price_paise,"
            "is_active) VALUES ('6m','dup',3,100,true)",
        ]
        for sql in bad:
            with self.subTest(sql=sql[-40:]), self.assertRaises(IntegrityError):
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(sql)

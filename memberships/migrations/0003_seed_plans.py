from django.db import migrations

PLANS = (
    ("6m", "6-month subscription", 6, 80000),
    ("12m", "12-month subscription", 12, 150000),
)


def seed(apps, schema_editor):
    Plan = apps.get_model("memberships", "SubscriptionPlan")
    for code, name, months, price in PLANS:
        Plan.objects.get_or_create(
            code=code,
            defaults={"name": name, "duration_months": months, "price_paise": price},
        )


class Migration(migrations.Migration):
    dependencies = [("memberships", "0002_subscriptions")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]

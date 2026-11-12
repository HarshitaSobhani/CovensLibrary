from django.db import migrations

FUNCTIONS = (
    "audit_event_reject_change()",
    "catalogue_copyevent_reject_change()",
    "members_member_no_immutable()",
)


def forward():
    return "\n".join(f"ALTER FUNCTION {f} SET search_path = pg_catalog;" for f in FUNCTIONS)


def backward():
    return "\n".join(f"ALTER FUNCTION {f} RESET search_path;" for f in FUNCTIONS)


class Migration(migrations.Migration):
    """Pin the trigger functions' search_path (the triggers themselves are unchanged)."""

    dependencies = [
        ("core", "0001_initial"),
        ("audit", "0002_append_only"),
        ("catalogue", "0002_copyevent_append_only"),
        ("members", "0002_membership_no_immutable"),
    ]
    operations = [migrations.RunSQL(forward(), backward())]

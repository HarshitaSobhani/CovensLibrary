from django.db import migrations

FORWARD = """
CREATE TRIGGER core_domainevent_append_only
BEFORE UPDATE OR DELETE ON core_domainevent
FOR EACH ROW EXECUTE FUNCTION core_reject_change();
"""
BACKWARD = "DROP TRIGGER IF EXISTS core_domainevent_append_only ON core_domainevent;"


class Migration(migrations.Migration):
    """The outbox is history: events are never edited or deleted (function core_reject_change
    from core.0006 pins search_path = pg_catalog)."""

    dependencies = [("core", "0007_domain_event")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

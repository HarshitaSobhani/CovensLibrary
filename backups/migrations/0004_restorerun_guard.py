from django.db import migrations

FORWARD = """
CREATE TRIGGER backups_restorerun_append_only
BEFORE UPDATE OR DELETE ON backups_restorerun
FOR EACH ROW EXECUTE FUNCTION core_reject_change();
"""
BACKWARD = "DROP TRIGGER IF EXISTS backups_restorerun_append_only ON backups_restorerun;"


class Migration(migrations.Migration):
    dependencies = [("backups", "0003_restorerun"), ("core", "0008_domain_event_protection")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION audit_event_reject_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_auditevent is append-only (% rejected)', TG_OP;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER audit_event_append_only
BEFORE UPDATE OR DELETE ON audit_auditevent
FOR EACH ROW EXECUTE FUNCTION audit_event_reject_change();
"""
DROP = """
DROP TRIGGER IF EXISTS audit_event_append_only ON audit_auditevent;
DROP FUNCTION IF EXISTS audit_event_reject_change();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]
    operations = [migrations.RunSQL(CREATE, DROP)]

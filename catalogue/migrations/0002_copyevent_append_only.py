from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION catalogue_copyevent_reject_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'catalogue_copyevent is append-only (% rejected)', TG_OP;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER catalogue_copyevent_append_only
BEFORE UPDATE OR DELETE ON catalogue_copyevent
FOR EACH ROW EXECUTE FUNCTION catalogue_copyevent_reject_change();
"""
DROP = """
DROP TRIGGER IF EXISTS catalogue_copyevent_append_only ON catalogue_copyevent;
DROP FUNCTION IF EXISTS catalogue_copyevent_reject_change();
"""


class Migration(migrations.Migration):
    dependencies = [("catalogue", "0001_initial")]
    operations = [migrations.RunSQL(CREATE, DROP)]

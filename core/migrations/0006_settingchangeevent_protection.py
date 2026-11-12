from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION core_reject_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP;
END;
$$;
CREATE TRIGGER core_settingchangeevent_append_only
BEFORE UPDATE OR DELETE ON core_settingchangeevent
FOR EACH ROW EXECUTE FUNCTION core_reject_change();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS core_settingchangeevent_append_only ON core_settingchangeevent;
DROP FUNCTION IF EXISTS core_reject_change();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0005_settingchangeevent")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

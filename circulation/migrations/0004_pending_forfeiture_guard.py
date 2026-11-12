from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION circulation_pending_forfeiture_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pending forfeitures are never deleted';
    END IF;
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'a resolved pending forfeiture cannot change';
    END IF;
    IF NEW.loan_id IS DISTINCT FROM OLD.loan_id
       OR NEW.setting_change_id IS DISTINCT FROM OLD.setting_change_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'the identity of a pending forfeiture never changes';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER circulation_pending_forfeiture_guard
BEFORE UPDATE OR DELETE ON circulation_pendingforfeiture
FOR EACH ROW EXECUTE FUNCTION circulation_pending_forfeiture_guard();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS circulation_pending_forfeiture_guard ON circulation_pendingforfeiture;
DROP FUNCTION IF EXISTS circulation_pending_forfeiture_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("circulation", "0003_pendingforfeiture")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

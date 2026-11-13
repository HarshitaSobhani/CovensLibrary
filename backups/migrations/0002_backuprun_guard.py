from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION backups_backuprun_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'backup history is never deleted';
    END IF;
    IF OLD.status IN ('succeeded', 'failed') THEN
        -- a finished run changes only to record a later verification or the retention removal
        IF NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.trigger IS DISTINCT FROM OLD.trigger
           OR NEW.status IS DISTINCT FROM OLD.status
           OR NEW.started_at IS DISTINCT FROM OLD.started_at
           OR NEW.finished_at IS DISTINCT FROM OLD.finished_at
           OR NEW.file_name IS DISTINCT FROM OLD.file_name
           OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
           OR NEW.sha256 IS DISTINCT FROM OLD.sha256
           OR NEW.key_fingerprint IS DISTINCT FROM OLD.key_fingerprint
           OR NEW.destination IS DISTINCT FROM OLD.destination
           OR NEW.remote_id IS DISTINCT FROM OLD.remote_id
           OR NEW.manifest IS DISTINCT FROM OLD.manifest
           OR NEW.error_summary IS DISTINCT FROM OLD.error_summary THEN
            RAISE EXCEPTION 'a finished backup record cannot change';
        END IF;
        IF OLD.remote_deleted_at IS NOT NULL AND NEW.remote_deleted_at IS DISTINCT FROM OLD.remote_deleted_at THEN
            RAISE EXCEPTION 'a removed backup stays removed';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status = 'running' AND OLD.status <> 'running' THEN
        RAISE EXCEPTION 'a backup run cannot return to running';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER backups_backuprun_guard
BEFORE UPDATE OR DELETE ON backups_backuprun
FOR EACH ROW EXECUTE FUNCTION backups_backuprun_guard();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS backups_backuprun_guard ON backups_backuprun;
DROP FUNCTION IF EXISTS backups_backuprun_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("backups", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

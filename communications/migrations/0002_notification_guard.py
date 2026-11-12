from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION communications_notification_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'notifications are never deleted';
    END IF;
    IF NEW.dedupe_key IS DISTINCT FROM OLD.dedupe_key
       OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.event_id IS DISTINCT FROM OLD.event_id
       OR NEW.kind IS DISTINCT FROM OLD.kind
       OR NEW.member_id IS DISTINCT FROM OLD.member_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'the identity of a notification never changes';
    END IF;
    IF OLD.status IN ('smtp_accepted', 'failed', 'skipped') THEN
        RAISE EXCEPTION 'a finished notification cannot change';
    END IF;
    IF NEW.status = 'prepared' AND OLD.status <> 'prepared' THEN
        RAISE EXCEPTION 'a notification cannot return to prepared';
    END IF;
    IF NEW.status = 'skipped' AND OLD.status NOT IN ('prepared', 'smtp_failed') THEN
        RAISE EXCEPTION 'only a notification that SMTP never accepted can be skipped';
    END IF;
    IF NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'the attempt count never decreases';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER communications_notification_guard
BEFORE UPDATE OR DELETE ON communications_notification
FOR EACH ROW EXECUTE FUNCTION communications_notification_guard();

CREATE TRIGGER communications_notificationattempt_append_only
BEFORE UPDATE OR DELETE ON communications_notificationattempt
FOR EACH ROW EXECUTE FUNCTION core_reject_change();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS communications_notificationattempt_append_only
    ON communications_notificationattempt;
DROP TRIGGER IF EXISTS communications_notification_guard ON communications_notification;
DROP FUNCTION IF EXISTS communications_notification_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("communications", "0001_initial"),
        ("core", "0008_domain_event_protection"),
    ]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

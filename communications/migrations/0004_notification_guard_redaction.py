from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION communications_notification_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    anon timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'notifications are never deleted';
    END IF;
    IF NEW.dedupe_key IS DISTINCT FROM OLD.dedupe_key
       OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.event_id IS DISTINCT FROM OLD.event_id
       OR NEW.kind IS DISTINCT FROM OLD.kind
       OR NEW.member_id IS DISTINCT FROM OLD.member_id
       OR NEW.scheduled_for IS DISTINCT FROM OLD.scheduled_for
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'the identity of a notification never changes';
    END IF;
    IF OLD.status IN ('smtp_accepted', 'failed', 'skipped') THEN
        -- A finished notification changes only to erase an anonymised member's personal data.
        SELECT m.anonymised_at INTO anon FROM public.members_member m WHERE m.id = OLD.member_id;
        IF anon IS NULL THEN
            RAISE EXCEPTION 'a finished notification cannot change';
        END IF;
        IF NEW.attempt_count IS DISTINCT FROM OLD.attempt_count
           OR NEW.accepted_at IS DISTINCT FROM OLD.accepted_at
           OR NEW.next_attempt_at IS NOT NULL
           OR NEW.subject <> '' OR NEW.body <> '' THEN
            RAISE EXCEPTION 'only the personal data of an anonymised member may be erased';
        END IF;
        IF NEW.status <> OLD.status AND NOT (OLD.status = 'failed' AND NEW.status = 'skipped') THEN
            RAISE EXCEPTION 'a finished notification cannot change status';
        END IF;
        IF NEW.recipient_email <> OLD.recipient_email
           AND (NEW.recipient_email <> ''
                OR (OLD.status = 'smtp_accepted' AND anon > now() - interval '90 days')) THEN
            RAISE EXCEPTION 'an accepted notification keeps its address for 90 days after anonymisation';
        END IF;
        IF OLD.status = 'failed' AND NEW.status = 'skipped'
           AND (NEW.recipient_email <> '' OR NEW.skip_code <> 'anonymised') THEN
            RAISE EXCEPTION 'a failed notification is skipped only with its address erased';
        END IF;
        RETURN NEW;
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
"""

BACKWARD = """
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
"""


class Migration(migrations.Migration):
    dependencies = [("communications", "0003_retention_and_backfill")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

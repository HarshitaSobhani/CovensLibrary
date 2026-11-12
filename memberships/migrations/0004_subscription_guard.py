from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION memberships_subscription_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'subscriptions are never deleted';
    END IF;
    IF OLD.cancelled_at IS NOT NULL THEN
        RAISE EXCEPTION 'a cancelled subscription cannot change';
    END IF;
    IF NEW.member_id IS DISTINCT FROM OLD.member_id
       OR NEW.token_cycle_id IS DISTINCT FROM OLD.token_cycle_id
       OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
       OR NEW.plan_name_snapshot IS DISTINCT FROM OLD.plan_name_snapshot
       OR NEW.duration_months_snapshot IS DISTINCT FROM OLD.duration_months_snapshot
       OR NEW.price_paise_snapshot IS DISTINCT FROM OLD.price_paise_snapshot
       OR NEW.starts_on IS DISTINCT FROM OLD.starts_on
       OR NEW.expires_on IS DISTINCT FROM OLD.expires_on
       OR NEW.payment_txn_id IS DISTINCT FROM OLD.payment_txn_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'only the cancellation fields of a subscription may change';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER memberships_subscription_guard
BEFORE UPDATE OR DELETE ON memberships_membersubscription
FOR EACH ROW EXECUTE FUNCTION memberships_subscription_guard();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS memberships_subscription_guard ON memberships_membersubscription;
DROP FUNCTION IF EXISTS memberships_subscription_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("memberships", "0003_seed_plans")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

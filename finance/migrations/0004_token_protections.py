from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION finance_check_token_balance() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    amt bigint; bal bigint;
BEGIN
    SELECT amount_snapshot_paise INTO amt FROM public.finance_tokencycle WHERE id = NEW.cycle_id;
    SELECT COALESCE(SUM(delta_paise), 0) INTO bal
        FROM public.finance_tokenledgerentry WHERE cycle_id = NEW.cycle_id;
    IF bal < 0 OR bal > amt THEN
        RAISE EXCEPTION 'token cycle % balance % is outside 0..%', NEW.cycle_id, bal, amt;
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER finance_tokenaccount_append_only
BEFORE UPDATE OR DELETE ON finance_tokenaccount
FOR EACH ROW EXECUTE FUNCTION finance_reject_change();
CREATE TRIGGER finance_tokencycle_append_only
BEFORE UPDATE OR DELETE ON finance_tokencycle
FOR EACH ROW EXECUTE FUNCTION finance_reject_change();
CREATE TRIGGER finance_tokenledgerentry_append_only
BEFORE UPDATE OR DELETE ON finance_tokenledgerentry
FOR EACH ROW EXECUTE FUNCTION finance_reject_change();
CREATE CONSTRAINT TRIGGER finance_token_balance
AFTER INSERT ON finance_tokenledgerentry
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_check_token_balance();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS finance_token_balance ON finance_tokenledgerentry;
DROP TRIGGER IF EXISTS finance_tokenledgerentry_append_only ON finance_tokenledgerentry;
DROP TRIGGER IF EXISTS finance_tokencycle_append_only ON finance_tokencycle;
DROP TRIGGER IF EXISTS finance_tokenaccount_append_only ON finance_tokenaccount;
DROP FUNCTION IF EXISTS finance_check_token_balance();
"""


class Migration(migrations.Migration):
    dependencies = [("finance", "0003_token_ledger")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

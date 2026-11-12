from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION finance_check_ledger_attribution() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    system_name text;
BEGIN
    SELECT recorded_by_system INTO system_name
        FROM public.finance_financialtransaction WHERE id = NEW.transaction_id;
    IF system_name <> '' AND NOT (
        NEW.entry_type = 'forfeiture' AND NEW.forfeiture_trigger = 'overdue_auto'
    ) THEN
        RAISE EXCEPTION 'a system attribution is valid only for an automatic overdue forfeiture';
    END IF;
    IF NEW.entry_type = 'forfeiture' AND NEW.forfeiture_trigger = 'overdue_auto'
       AND system_name = '' THEN
        RAISE EXCEPTION 'an automatic overdue forfeiture must carry the system attribution';
    END IF;
    IF NEW.entry_type = 'forfeiture' AND NEW.forfeiture_trigger <> 'overdue_auto'
       AND system_name <> '' THEN
        RAISE EXCEPTION 'a confirmed forfeiture must be attributed to the owner';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER finance_ledger_attribution
BEFORE INSERT ON finance_tokenledgerentry
FOR EACH ROW EXECUTE FUNCTION finance_check_ledger_attribution();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS finance_ledger_attribution ON finance_tokenledgerentry;
DROP FUNCTION IF EXISTS finance_check_ledger_attribution();
"""


class Migration(migrations.Migration):
    """D-15: the system actor is valid only for the automatic day-8 forfeiture, and that
    forfeiture must carry it (so the system attribution is not a general bypass)."""

    dependencies = [("finance", "0008_system_actor")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

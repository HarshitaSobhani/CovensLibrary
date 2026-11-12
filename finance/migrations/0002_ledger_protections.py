from django.db import migrations

TABLES = ("finance_financialtransaction", "finance_paymententry", "finance_receiptdocument")

FORWARD = """
CREATE OR REPLACE FUNCTION finance_reject_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP;
END;
$$;

CREATE OR REPLACE FUNCTION finance_check_payment_sum() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    tid bigint; flow text; amt bigint; total bigint;
BEGIN
    IF TG_TABLE_NAME = 'finance_financialtransaction' THEN
        tid := NEW.id;
    ELSE
        tid := NEW.transaction_id;
    END IF;
    SELECT cash_flow, amount_paise INTO flow, amt
        FROM public.finance_financialtransaction WHERE id = tid;
    SELECT COALESCE(SUM(amount_paise), 0) INTO total
        FROM public.finance_paymententry WHERE transaction_id = tid;
    IF flow = 'none' AND total <> 0 THEN
        RAISE EXCEPTION 'transaction % moves no cash but has payment entries', tid;
    END IF;
    IF flow <> 'none' AND total <> amt THEN
        RAISE EXCEPTION 'payment entries (%) must equal the transaction amount (%) for transaction %',
            total, amt, tid;
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER finance_financialtransaction_append_only
BEFORE UPDATE OR DELETE ON finance_financialtransaction
FOR EACH ROW EXECUTE FUNCTION finance_reject_change();
CREATE TRIGGER finance_paymententry_append_only
BEFORE UPDATE OR DELETE ON finance_paymententry
FOR EACH ROW EXECUTE FUNCTION finance_reject_change();
CREATE TRIGGER finance_receiptdocument_append_only
BEFORE UPDATE OR DELETE ON finance_receiptdocument
FOR EACH ROW EXECUTE FUNCTION finance_reject_change();

CREATE CONSTRAINT TRIGGER finance_txn_payment_sum
AFTER INSERT ON finance_financialtransaction
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_check_payment_sum();
CREATE CONSTRAINT TRIGGER finance_entry_payment_sum
AFTER INSERT ON finance_paymententry
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_check_payment_sum();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS finance_entry_payment_sum ON finance_paymententry;
DROP TRIGGER IF EXISTS finance_txn_payment_sum ON finance_financialtransaction;
DROP TRIGGER IF EXISTS finance_receiptdocument_append_only ON finance_receiptdocument;
DROP TRIGGER IF EXISTS finance_paymententry_append_only ON finance_paymententry;
DROP TRIGGER IF EXISTS finance_financialtransaction_append_only ON finance_financialtransaction;
DROP FUNCTION IF EXISTS finance_check_payment_sum();
DROP FUNCTION IF EXISTS finance_reject_change();
"""


class Migration(migrations.Migration):
    dependencies = [("finance", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

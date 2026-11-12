from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION finance_check_correction_rules() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    target_kind text;
BEGIN
    IF NEW.kind = 'reversal' THEN
        SELECT kind INTO target_kind
            FROM public.finance_financialtransaction WHERE id = NEW.reverses_id;
        IF target_kind IN ('subscription_payment', 'token_refund', 'reversal') THEN
            RAISE EXCEPTION 'a % cannot be reversed', target_kind;
        END IF;
    END IF;
    IF NEW.kind = 'adjustment' AND NEW.cash_flow = 'out' AND NEW.adjusts_id IS NOT NULL THEN
        SELECT kind INTO target_kind
            FROM public.finance_financialtransaction WHERE id = NEW.adjusts_id;
        IF target_kind = 'subscription_payment' THEN
            RAISE EXCEPTION 'an adjustment cannot pay out against a subscription payment';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER finance_txn_correction_rules
BEFORE INSERT ON finance_financialtransaction
FOR EACH ROW EXECUTE FUNCTION finance_check_correction_rules();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS finance_txn_correction_rules ON finance_financialtransaction;
DROP FUNCTION IF EXISTS finance_check_correction_rules();
"""


class Migration(migrations.Migration):
    """D-7 backstop: subscription payments, paid refunds and reversals are never reversed, and an
    adjustment never pays out against a subscription payment (subscription fees are not refunded)."""

    dependencies = [("finance", "0005_paise_bigint_and_limits")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION circulation_reject_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP;
END;
$$;

CREATE OR REPLACE FUNCTION circulation_loan_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'loans are never deleted';
    END IF;
    IF NEW.member_id IS DISTINCT FROM OLD.member_id
       OR NEW.copy_id IS DISTINCT FROM OLD.copy_id
       OR NEW.subscription_id IS DISTINCT FROM OLD.subscription_id
       OR NEW.issued_on IS DISTINCT FROM OLD.issued_on
       OR NEW.issued_processed_at IS DISTINCT FROM OLD.issued_processed_at
       OR NEW.issue_condition IS DISTINCT FROM OLD.issue_condition
       OR NEW.backdated IS DISTINCT FROM OLD.backdated
       OR NEW.backdate_reason IS DISTINCT FROM OLD.backdate_reason
       OR NEW.backdate_confirmed IS DISTINCT FROM OLD.backdate_confirmed
       OR NEW.issued_by_id IS DISTINCT FROM OLD.issued_by_id THEN
        RAISE EXCEPTION 'the issue facts of a loan never change';
    END IF;
    IF OLD.forfeiture_entry_id IS NOT NULL
       AND NEW.forfeiture_entry_id IS DISTINCT FROM OLD.forfeiture_entry_id THEN
        RAISE EXCEPTION 'the forfeiture link of a loan never changes';
    END IF;
    IF OLD.status <> 'active' THEN
        IF NEW.status IS DISTINCT FROM OLD.status
           OR NEW.due_on IS DISTINCT FROM OLD.due_on
           OR NEW.returned_on IS DISTINCT FROM OLD.returned_on
           OR NEW.returned_processed_at IS DISTINCT FROM OLD.returned_processed_at
           OR NEW.return_condition IS DISTINCT FROM OLD.return_condition
           OR NEW.return_backdated IS DISTINCT FROM OLD.return_backdated
           OR NEW.return_backdate_reason IS DISTINCT FROM OLD.return_backdate_reason
           OR NEW.returned_by_id IS DISTINCT FROM OLD.returned_by_id
           OR NEW.notes IS DISTINCT FROM OLD.notes THEN
            RAISE EXCEPTION 'a closed loan cannot change (only its forfeiture link may be set once)';
        END IF;
    ELSIF NEW.due_on < OLD.due_on THEN
        RAISE EXCEPTION 'a due date can only move forward (renewal)';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER circulation_loan_guard
BEFORE UPDATE OR DELETE ON circulation_loan
FOR EACH ROW EXECUTE FUNCTION circulation_loan_guard();
CREATE TRIGGER circulation_loanrenewal_append_only
BEFORE UPDATE OR DELETE ON circulation_loanrenewal
FOR EACH ROW EXECUTE FUNCTION circulation_reject_change();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS circulation_loanrenewal_append_only ON circulation_loanrenewal;
DROP TRIGGER IF EXISTS circulation_loan_guard ON circulation_loan;
DROP FUNCTION IF EXISTS circulation_loan_guard();
DROP FUNCTION IF EXISTS circulation_reject_change();
"""


class Migration(migrations.Migration):
    dependencies = [("circulation", "0001_loans")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]

from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION members_member_no_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.membership_no IS DISTINCT FROM OLD.membership_no THEN
        RAISE EXCEPTION 'members_member.membership_no is permanent and cannot change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER members_member_no_immutable
BEFORE UPDATE ON members_member
FOR EACH ROW EXECUTE FUNCTION members_member_no_immutable();
"""
DROP = """
DROP TRIGGER IF EXISTS members_member_no_immutable ON members_member;
DROP FUNCTION IF EXISTS members_member_no_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [("members", "0001_initial")]
    operations = [migrations.RunSQL(CREATE, DROP)]

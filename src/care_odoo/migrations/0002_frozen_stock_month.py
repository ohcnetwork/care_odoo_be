from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("care_odoo", "0001_initial")]

    operations = [
        migrations.RunSQL(
            sql="""
        CREATE FUNCTION care_odoo_guard_stock_month() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Frozen stock months cannot be deleted';
          END IF;
          IF OLD.state = 'closed' OR
             ROW(NEW.facility_id, NEW.month, NEW.fiscal_month, NEW.target, NEW.source_hash, NEW.plan, NEW.created_at)
             IS DISTINCT FROM
             ROW(OLD.facility_id, OLD.month, OLD.fiscal_month, OLD.target, OLD.source_hash, OLD.plan, OLD.created_at)
          THEN
            RAISE EXCEPTION 'Frozen stock source plans and closed snapshots cannot be changed';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER care_odoo_stock_month_immutable BEFORE UPDATE OR DELETE ON care_odoo_stockmonth
        FOR EACH ROW EXECUTE FUNCTION care_odoo_guard_stock_month();
        """,
            reverse_sql="""
        DROP TRIGGER care_odoo_stock_month_immutable ON care_odoo_stockmonth;
        DROP FUNCTION care_odoo_guard_stock_month();
        """,
        )
    ]

from uuid import uuid4

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("care_odoo", "0001_initial")]

    operations = [
        # Existing months share an initial revision. All revision lookups are
        # facility-scoped; new runs get a fresh UUID from the model default.
        migrations.AddField("stockmonth", "revision", models.UUIDField(default=uuid4, editable=False, db_index=True)),
        migrations.AddField("stockmonth", "is_current", models.BooleanField(default=True)),
        migrations.AddField("stockmonth", "archived_at", models.DateTimeField(null=True)),
        migrations.RemoveConstraint("stockmonth", "care_odoo_stock_month_unique"),
        migrations.AddConstraint(
            "stockmonth",
            models.UniqueConstraint(fields=["facility", "month", "revision"], name="care_odoo_stock_revision_unique"),
        ),
        migrations.AddConstraint(
            "stockmonth",
            models.UniqueConstraint(
                fields=["facility", "month"], condition=models.Q(is_current=True), name="care_odoo_stock_current_unique"
            ),
        ),
    ]

"""Saved monthly reports, with one current revision and archived predecessors."""

from uuid import uuid4

from django.db import models


class StockMonth(models.Model):
    facility = models.ForeignKey("facility.Facility", on_delete=models.PROTECT)
    month = models.CharField(max_length=7)
    fiscal_month = models.CharField(max_length=7)
    revision = models.UUIDField(default=uuid4, editable=False, db_index=True)
    is_current = models.BooleanField(default=True)
    archived_at = models.DateTimeField(null=True)
    state = models.CharField(max_length=16, default="planned", choices=[("planned", "Planned"), ("closed", "Closed")])
    target = models.JSONField()
    source_hash = models.CharField(max_length=64)
    plan = models.JSONField()
    progress = models.JSONField(default=dict)
    closing = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["facility", "month", "revision"], name="care_odoo_stock_revision_unique"),
            models.UniqueConstraint(
                fields=["facility", "month"], condition=models.Q(is_current=True), name="care_odoo_stock_current_unique"
            ),
        ]
        ordering = ["month"]

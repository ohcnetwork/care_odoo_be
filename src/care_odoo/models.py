"""Durable, frozen source plans and month-end valuation exports."""

from django.db import models


class StockMonth(models.Model):
    facility = models.ForeignKey("facility.Facility", on_delete=models.PROTECT)
    month = models.CharField(max_length=7)
    fiscal_month = models.CharField(max_length=7)
    state = models.CharField(max_length=16, default="planned", choices=[("planned", "Planned"), ("closed", "Closed")])
    target = models.JSONField()
    source_hash = models.CharField(max_length=64)
    plan = models.JSONField()
    progress = models.JSONField(default=dict)
    closing = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["facility", "month"], name="care_odoo_stock_month_unique")]
        ordering = ["month"]

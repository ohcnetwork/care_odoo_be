"""Explicit freeze, resumable delivery and consecutive monthly close."""

from decimal import Decimal

from django.db import connection, transaction
from django.utils import timezone

from care_odoo.models import StockMonth
from care_odoo.resources.stock.monthly import build_monthly_plan, fingerprint
from care_odoo.resources.stock.period import chunks, month_bounds
from care_odoo.resources.stock.resource import OdooStockResource
from care_odoo.settings import plugin_settings


def target():
    return {
        "protocol": plugin_settings.CARE_ODOO_PROTOCOL,
        "host": plugin_settings.CARE_ODOO_HOST,
        "port": plugin_settings.CARE_ODOO_PORT,
        "database": plugin_settings.CARE_ODOO_DATABASE,
    }


def lock(facility_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"care-odoo-stock:{facility_id}"])


@transaction.atomic
def freeze_month(facility, month, fiscal_month, approved_opening=None, progress=lambda _message: None):
    lock(facility.pk)
    if month_bounds(month)[1] > timezone.now():
        raise ValueError("A month cannot be closed before it has ended")
    existing = StockMonth.objects.filter(facility=facility, month=month).first()
    if existing:
        if existing.target != target() or existing.fiscal_month != fiscal_month:
            raise ValueError("The frozen month belongs to another Odoo target or fiscal opening")
        if (
            approved_opening
            and existing.plan["metadata"]["approved_opening"]["source_sha256"] != approved_opening["source_sha256"]
        ):
            raise ValueError("The approved opening differs from the frozen source")
        return existing
    prior = StockMonth.objects.filter(facility=facility, month__lt=month).order_by("-month").first()
    if prior and (prior.state != "closed" or prior.target != target() or prior.fiscal_month != fiscal_month):
        raise ValueError("Close the previous month on the same Odoo target first")
    if prior and approved_opening:
        raise ValueError("Carry forward the previous close; do not import another opening")
    if not prior and month != fiscal_month:
        raise ValueError("Begin with the approved fiscal opening month")
    if StockMonth.objects.filter(facility=facility, month__gt=month).exists():
        raise ValueError("A later month already exists")
    plan = build_monthly_plan(
        facility, month, fiscal_month, progress, approved_opening, prior.closing if prior else None
    )
    return StockMonth.objects.create(
        facility=facility,
        month=month,
        fiscal_month=fiscal_month,
        plan=plan,
        source_hash=fingerprint(plan),
        target=target(),
    )


@transaction.atomic
def post_month(stock_month_id, progress=lambda _message: None, batch_size=200):
    row = StockMonth.objects.select_related("facility").get(pk=stock_month_id)
    lock(row.facility_id)
    row.refresh_from_db()
    if not plugin_settings.CARE_ODOO_STOCK_SYNC_ENABLED:
        raise ValueError("Monthly stock posting is disabled; enable CARE_ODOO_STOCK_SYNC_ENABLED explicitly")
    if row.target != target() or row.source_hash != fingerprint(row.plan):
        raise ValueError("The frozen source plan or Odoo target changed")
    if not 1 <= batch_size <= 500:
        raise ValueError("Batch size must be between 1 and 500")
    if row.state == "closed":
        return row.closing
    plan = row.plan
    period = {**plan["period"], "stock_month": row.month}
    remote = OdooStockResource.call("status", period)
    if remote.get("closed"):
        snapshot = remote["snapshot"]
        if snapshot["source_hash"] != row.source_hash or snapshot["month"] != row.month:
            raise ValueError("The remote close belongs to another frozen source plan")
        row.closing, row.state, row.closed_at = snapshot, "closed", timezone.now()
        row.save(update_fields=["closing", "state", "closed_at"])
        return snapshot
    for index, items in enumerate(chunks(plan["items"], batch_size)):
        key = f"prepare:{index}"
        if key not in row.progress:
            row.progress[key] = OdooStockResource.prepare(period, items, [], [], plan["opening_at"])
            row.save(update_fields=["progress"])
        progress(f"Prepared {min((index + 1) * batch_size, len(plan['items']))}/{len(plan['items'])}")
    # JSONB object order is not a delivery order. This ordering is part of costing.
    for phase in ["opening_in", "opening_out", "receipts", "recoveries", "issues"]:
        events = plan["phases"][phase]
        for index, batch in enumerate(chunks(events, batch_size)):
            key = f"{phase}:{index}"
            if key not in row.progress:
                row.progress[key] = OdooStockResource.post_events(period, batch)
                row.save(update_fields=["progress"])
            progress(f"{phase}: {min((index + 1) * batch_size, len(events))}/{len(events)}")
        if phase == "opening_out" and row.month == row.fiscal_month:
            rows = []
            for items in chunks(plan["items"], batch_size):
                rows.extend(
                    OdooStockResource.report(period, [item["uuid"] for item in items], plan["opening_at"])["items"]
                )
            targets = {item["uuid"]: Decimal(item["opening_value_target"]) for item in plan["items"]}
            if any(Decimal(str(item["reported_total_value"])) != targets[item["item_uuid"]] for item in rows):
                raise ValueError("Native Odoo opening values differ from the approved per-item allocation")
            row.progress["opening_snapshot"] = rows
            row.save(update_fields=["progress"])
    payload = {
        "source_hash": row.source_hash,
        "quantity_basis": plan["metadata"].get("stock_basis", "available"),
        "closing_quantities": plan["closing_quantities"],
        "available_quantities": plan.get("available_quantities", plan["closing_quantities"]),
        "in_transit_quantities": plan.get("in_transit_quantities", {}),
        "event_keys": [event["key"] for events in plan["phases"].values() for event in events],
        "location_uuids": plan["location_uuids"],
        "items": plan["items"],
    }
    snapshot = OdooStockResource.call("close", period, **payload)["snapshot"]
    row.closing, row.state, row.closed_at = snapshot, "closed", timezone.now()
    row.save(update_fields=["closing", "state", "closed_at", "progress"])
    return snapshot

"""Monthly summary delivery, and archive-and-rebuild corrections."""

from decimal import Decimal
from uuid import uuid4

from django.db import connection, transaction
from django.utils import timezone

from care_odoo.models import StockMonth
from care_odoo.resources.stock.monthly import build_monthly_plan, fingerprint, stock_details
from care_odoo.resources.stock.opening import capture_opening_report, opening_total
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
def freeze_month(
    facility,
    month,
    fiscal_month,
    expected_opening_total=None,
    progress=lambda _message: None,
    *,
    revision=None,
    approved_opening=None,
    period=None,
    is_current=True,
):
    lock(facility.pk)
    if month_bounds(month)[1] > timezone.now():
        raise ValueError("A month cannot be closed before it has ended")
    months = StockMonth.objects.filter(facility=facility)
    months = months.filter(revision=revision) if revision else months.filter(is_current=True)
    existing = months.filter(month=month).first()
    if existing:
        if existing.target != target() or existing.fiscal_month != fiscal_month:
            raise ValueError("The frozen month belongs to another Odoo target or fiscal opening")
        if expected_opening_total is not None and month != fiscal_month:
            raise ValueError("Only the first month accepts an expected opening total")
        if expected_opening_total is not None and Decimal(
            existing.plan["metadata"]["approved_opening"]["total"]
        ) != opening_total(expected_opening_total):
            raise ValueError("The approved opening total differs from the frozen source")
        return existing
    prior = months.filter(month__lt=month).order_by("-month").first()
    if prior and (prior.state != "closed" or prior.target != target() or prior.fiscal_month != fiscal_month):
        raise ValueError("Close the previous month on the same Odoo target first")
    if prior and expected_opening_total is not None:
        raise ValueError("Carry forward the previous close; do not import another opening")
    if not prior and month != fiscal_month:
        raise ValueError("Begin with the approved fiscal opening month")
    if months.filter(month__gt=month).exists():
        raise ValueError("A later month already exists")
    revision = revision or (prior.revision if prior else uuid4())
    period = period or (
        prior.plan["period"]
        if prior
        else {
            "period_key": f"care-monthly:{facility.external_id}:{fiscal_month}:{revision}",
            "facility_uuid": str(facility.external_id),
            "month": fiscal_month,
            "mode": "monthly",
            "staged": True,
            "metadata": {"facility_name": facility.name},
        }
    )
    if not prior and approved_opening is None:
        approved_opening = capture_opening_report(facility, month_bounds(month)[0], expected_opening_total)
    plan = build_monthly_plan(
        facility, month, fiscal_month, progress, approved_opening, prior.closing if prior else None, period
    )
    return StockMonth.objects.create(
        facility=facility,
        month=month,
        fiscal_month=fiscal_month,
        revision=revision,
        is_current=is_current,
        plan=plan,
        source_hash=fingerprint(plan),
        target=target(),
    )


@transaction.atomic
def post_month(stock_month_id, progress=lambda _message: None, batch_size=200, *, publish=True):
    row = StockMonth.objects.select_related("facility").get(pk=stock_month_id)
    lock(row.facility_id)
    row.refresh_from_db()
    if not plugin_settings.CARE_ODOO_STOCK_SYNC_ENABLED:
        raise ValueError("Monthly stock posting is disabled; enable CARE_ODOO_STOCK_SYNC_ENABLED explicitly")
    if row.target != target() or row.source_hash != fingerprint(row.plan):
        raise ValueError("The frozen source plan or Odoo target changed")
    if not 1 <= batch_size <= 500:
        raise ValueError("Batch size must be between 1 and 500")
    if row.archived_at:
        raise ValueError("This revision is archived; export its saved report or rebuild the current series")
    if row.state == "closed":
        if publish and row.month == row.fiscal_month and row.plan["period"].get("staged"):
            publish_revision(row, row.month)
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
        if publish and row.month == row.fiscal_month and period.get("staged"):
            publish_revision(row, row.month)
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
        "stock_details": stock_details(plan),
    }
    snapshot = OdooStockResource.call("close", period, **payload)["snapshot"]
    row.closing, row.state, row.closed_at = snapshot, "closed", timezone.now()
    row.save(update_fields=["closing", "state", "closed_at", "progress"])
    if publish and row.month == row.fiscal_month and period.get("staged"):
        publish_revision(row, row.month)
    return snapshot


@transaction.atomic
def activate_revision(row):
    lock(row.facility_id)
    StockMonth.objects.filter(facility_id=row.facility_id, archived_at__isnull=True).exclude(
        revision=row.revision
    ).update(is_current=False, archived_at=timezone.now())
    StockMonth.objects.filter(facility_id=row.facility_id, revision=row.revision).update(
        is_current=True, archived_at=None
    )


def publish_revision(row, through_month):
    OdooStockResource.call("publish", row.plan["period"], through_month=through_month)
    activate_revision(row)


def rebuild_months(facility, month, fiscal_month, expected_opening_total=None, progress=lambda _message: None):
    """One operator action rebuilds the opening and every subsequent processed month."""
    if not plugin_settings.CARE_ODOO_STOCK_SYNC_ENABLED:
        raise ValueError("Enable CARE_ODOO_STOCK_SYNC_ENABLED explicitly before rebuilding")
    month_bounds(month)
    month_bounds(fiscal_month)
    if month < fiscal_month:
        raise ValueError("The last month cannot precede the fiscal opening")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", [f"care-odoo-stock:{facility.pk}"])
    try:
        current = list(StockMonth.objects.filter(facility=facility, is_current=True).order_by("month"))
        if any(row.target != target() or row.fiscal_month != fiscal_month for row in current):
            raise ValueError("Rebuild on the same Odoo target and fiscal opening")
        # Recover a publish committed by Odoo whose response/local commit was lost.
        remote = OdooStockResource.call("current", {"facility_uuid": str(facility.external_id), "mode": "monthly"})
        if remote.get("period_key") and (
            not current or current[0].plan["period"]["period_key"] != remote["period_key"]
        ):
            recovered = StockMonth.objects.filter(
                facility=facility,
                plan__period__period_key=remote["period_key"],
                state="closed",
                month=remote["through_month"],
            ).first()
            if not recovered or recovered.target != target() or recovered.fiscal_month != fiscal_month:
                raise ValueError("The current Odoo revision is not available in this Care database")
            activate_revision(recovered)
            current = list(StockMonth.objects.filter(facility=facility, is_current=True).order_by("month"))
        through = max([month, *(row.month for row in current)])
        if month_bounds(through)[1] > timezone.now():
            raise ValueError("A month cannot be rebuilt before it has ended")
        opening = current[0].plan.get("opening_source") if current else None
        approved_total = current[0].plan["metadata"].get("approved_opening", {}).get("total") if current else None
        if approved_total is not None:
            if expected_opening_total is not None and opening_total(expected_opening_total) != Decimal(approved_total):
                raise ValueError("The expected total differs from the approved opening")
            expected_opening_total = approved_total
        if (
            opening is not None
            and expected_opening_total is not None
            and opening_total(expected_opening_total) != Decimal(opening["total"])
        ):
            raise ValueError("The expected total differs from the approved opening")
        revision = uuid4()
        period = {
            "period_key": f"care-monthly:{facility.external_id}:{fiscal_month}:{revision}",
            "facility_uuid": str(facility.external_id),
            "month": fiscal_month,
            "mode": "monthly",
            "staged": True,
            "replaces_period_key": remote.get("period_key"),
            "metadata": {"facility_name": facility.name},
        }
        rows = []
        next_month = fiscal_month
        while next_month <= through:
            row = freeze_month(
                facility,
                next_month,
                fiscal_month,
                expected_opening_total if not rows and opening is None else None,
                progress,
                revision=revision,
                approved_opening=opening if not rows else None,
                period=period,
                is_current=False,
            )
            post_month(row.pk, progress, publish=False)
            row.refresh_from_db()
            rows.append(row)
            next_month = month_bounds(next_month)[1].strftime("%Y-%m")
        publish_revision(rows[-1], through)
        for row in rows:
            row.refresh_from_db()
        progress(f"Published replacement {revision} through {through}; previous reports archived")
        return rows
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [f"care-odoo-stock:{facility.pk}"])

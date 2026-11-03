"""One valuation location in Odoo; detailed quantities and evidence stay in Care."""

import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from care_odoo.resources.stock.period import build_period_plan, month_bounds


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def quantities(dimensions):
    totals = defaultdict(Decimal)
    for row in dimensions:
        totals[row["item_uuid"]] += Decimal(row["quantity"])
    return dict(totals)


def build_monthly_plan(
    facility, month, fiscal_month, progress=lambda _message: None, approved_opening=None, previous_close=None
):
    if (approved_opening is None) == (previous_close is None):
        raise ValueError("Supply either the approved first opening or the previous close")
    if previous_close and month_bounds(previous_close["month"])[1] != month_bounds(month)[0]:
        raise ValueError("Close months consecutively")
    if previous_close and previous_close.get("quantity_basis") != "owned":
        raise ValueError("The prior close uses the old availability basis; replay a separate owned-stock ledger")
    detailed = build_period_plan(facility, month, progress, approved_opening, previous_close, owned_stock=True)
    opening, closing = quantities(detailed["opening"]), quantities(detailed["closing"])
    if previous_close:
        prior = {row["item_uuid"]: Decimal(str(row["quantity"])) for row in previous_close["items"]}
        differences = {
            key: str(opening.get(key, 0) - prior.get(key, 0))
            for key in opening.keys() | prior.keys()
            if opening.get(key, 0) != prior.get(key, 0)
        }
        if differences:
            raise ValueError(f"Care opening changed since the previous close: {differences}")
    batch_items = {row["uuid"]: row["item_uuid"] for row in detailed["batches"]}
    # Carry even depleted/retired products forward, so no closed stock silently disappears.
    items = {row["uuid"]: row for row in detailed["items"]}
    if previous_close:
        for row in previous_close["items"]:
            items.setdefault(
                row["item_uuid"],
                {
                    "uuid": row["item_uuid"],
                    "name": row["name"],
                    "base_unit": row.get("base_unit"),
                    "opening_quantity": str(row["quantity"]),
                    "opening_unit_cost": str(row["average_cost"]),
                    "opening_value_target": str(row["reported_total_value"]),
                },
            )
    groups = {}
    for phase, events in detailed["phases"].items():
        if phase == "internal" or (previous_close and phase.startswith("opening")):
            continue
        for event in events:
            item = batch_items[event["batch_uuid"]]
            source = (
                "valuation"
                if event["source"] not in {"external", "consumption", "adjustment", "transit"}
                else event["source"]
            )
            dest = (
                "valuation"
                if event["destination"] not in {"external", "consumption", "adjustment", "transit"}
                else event["destination"]
            )
            key = (phase, item, source, dest, event["kind"])
            group = groups.setdefault(key, {"quantity": Decimal(0), "value": Decimal(0), "evidence": []})
            group["quantity"] += Decimal(event["quantity"])
            group["value"] += Decimal(event.get("input_value", 0))
            group["evidence"].append(event)
    phases = {key: [] for key in ["opening_in", "opening_out", "receipts", "recoveries", "issues"]}
    start, end = month_bounds(month)
    for (phase, item, source, dest, kind), group in sorted(groups.items()):
        when = (
            start - timedelta(seconds=1)
            if phase.startswith("opening")
            else end - timedelta(seconds={"receipts": 4, "recoveries": 3, "issues": 2}[phase])
        )
        event = {
            "key": f"care-monthly:{facility.external_id}:{month}:{phase}:{item}:{source}:{dest}:{kind}",
            "item_uuid": item,
            "source": source,
            "destination": dest,
            "quantity": str(group["quantity"]),
            "kind": kind,
            "date": when.isoformat(),
            "cost_basis": "monthly_receipt"
            if phase == "receipts"
            else "opening"
            if phase == "opening_in"
            else "native_average",
            "source_hash": fingerprint(group["evidence"]),
        }
        if phase in {"opening_in", "receipts"}:
            event["input_value"] = str(group["value"])
        if phase == "receipts":
            event["receipts"] = group["evidence"]
            event["bill_line_ids"] = sorted({line for r in group["evidence"] for line in r.get("bill_line_ids", [])})
        phases[phase].append(event)
    # This check uses actual recorded movements, never a balancing consumption plug.
    projected = defaultdict(Decimal, opening)
    for phase in ["receipts", "recoveries", "issues"]:
        for event in phases[phase]:
            projected[event["item_uuid"]] += Decimal(event["quantity"]) * (
                1 if event["destination"] == "valuation" else -1
            )
    differences = {
        key: str(projected.get(key, 0) - closing.get(key, 0))
        for key in projected.keys() | closing.keys()
        if projected.get(key, 0) != closing.get(key, 0)
    }
    if differences:
        raise ValueError(f"Recorded movement totals do not explain Care closing stock: {differences}")
    return {
        "period": {
            "period_key": f"care-monthly:{facility.external_id}:{fiscal_month}",
            "facility_uuid": str(facility.external_id),
            "month": fiscal_month,
            "mode": "monthly",
        },
        "month": month,
        "opening_source": approved_opening,
        "opening_at": detailed["opening_at"],
        "closing_at": detailed["closing_at"],
        "items": list(items.values()),
        "phases": phases,
        "opening_quantities": {k: str(v) for k, v in opening.items()},
        "closing_quantities": {k: str(v) for k, v in closing.items()},
        "available_quantities": {k: str(v) for k, v in quantities(detailed["available_closing"]).items()},
        "in_transit_quantities": {k: str(v) for k, v in quantities(detailed["in_transit_closing"]).items()},
        "location_uuids": [row["uuid"] for row in detailed["locations"]],
        "metadata": {
            **detailed["metadata"],
            "model": "Monthly periodic native AVCO; purchases before recoveries and issues",
            "summary_event_counts": {key: len(value) for key, value in phases.items()},
        },
        "evidence": detailed,
    }

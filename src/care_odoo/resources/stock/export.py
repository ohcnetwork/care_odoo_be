"""Exports read frozen records only, never current stock or mutable prices."""

import csv
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from care_odoo.resources.stock.opening import balanced_round, item_name


def export_month(row, output):
    if row.state != "closed":
        raise ValueError("Only a closed month has an official valuation export")
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    plan, close = row.plan, row.closing
    (folder / "closing.json").write_text(json.dumps(close, indent=2))

    def write(name, records, fields):
        with (folder / name).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)

    items = {item["item_uuid"]: item for item in close["items"]}
    detail = plan["evidence"]
    batches = {batch["uuid"]: batch for batch in detail["batches"]}
    batch_items = {batch["care_product_id"]: batch["item_uuid"] for batch in detail["batches"]}
    flags = defaultdict(set)
    absent_names = {flag["item"] for flag in detail["flags"] if flag["kind"] == "approved_opening_item_absent"}
    for key, item in items.items():
        if item_name(item["name"]) in absent_names:
            flags[key].add("unpriced_legacy_opening")
        if Decimal(str(item["quantity"])) < 0:
            flags[key].add("negative_stock")
        if Decimal(str(item["average_cost"])) < 0:
            flags[key].add("negative_unit_cost")
    for flag in detail["flags"]:
        if flag["kind"] == "missing_batch_price" and flag["product_id"] in batch_items:
            flags[batch_items[flag["product_id"]]].add("missing_batch_price")
    for receipt in plan["phases"]["receipts"]:
        for part in receipt["receipts"]:
            if part["cost_basis"] != "posted_bill":
                flags[receipt["item_uuid"]].add(
                    "internal_stock_adjustment_care_cost"
                    if part.get("receipt_type") == "stock_adjustment"
                    else "receipt_without_posted_bill"
                )
    write(
        "stock-by-item.csv",
        [
            {
                "month": row.month,
                "item_uuid": key,
                "item": item["name"],
                "base_unit": item.get("base_unit", {}).get("display", "")
                if isinstance(item.get("base_unit"), dict)
                else item.get("base_unit"),
                "quantity": item["quantity"],
                "available_quantity": item.get("available_quantity", item["quantity"]),
                "incoming_quantity": item.get("in_transit_quantity", 0),
                "effective_cost": item["average_cost"],
                "value": item["reported_total_value"],
                "source_hash": row.source_hash,
                "flags": ";".join(sorted(flags[key])),
            }
            for key, item in items.items()
        ],
        [
            "month",
            "item_uuid",
            "item",
            "base_unit",
            "quantity",
            "available_quantity",
            "incoming_quantity",
            "effective_cost",
            "value",
            "source_hash",
            "flags",
        ],
    )
    locations = {loc["uuid"]: loc["name"] for loc in detail["locations"]}
    grouped = defaultdict(list)
    transit = {
        (d["batch_uuid"], d["location_uuid"]): Decimal(d["quantity"]) for d in detail.get("in_transit_closing", [])
    }
    for dimension in detail["closing"]:
        grouped[dimension["item_uuid"]].append(dimension)
    rows = []
    for key, dimensions in grouped.items():
        item = items[key]
        raw = {index: Decimal(d["quantity"]) * Decimal(str(item["average_cost"])) for index, d in enumerate(dimensions)}
        values = balanced_round(raw, Decimal(str(item["reported_total_value"])))
        for index, d in enumerate(dimensions):
            value = values[index]
            batch = batches[d["batch_uuid"]]
            rows.append(
                {
                    "month": row.month,
                    "item": item["name"],
                    "item_uuid": key,
                    "batch_uuid": d["batch_uuid"],
                    "batch": batch["lot_number"],
                    "location": locations[d["location_uuid"]],
                    "location_uuid": d["location_uuid"],
                    "quantity": d["quantity"],
                    "available_quantity": str(
                        Decimal(d["quantity"]) - transit.get((d["batch_uuid"], d["location_uuid"]), Decimal(0))
                    ),
                    "incoming_quantity": str(transit.get((d["batch_uuid"], d["location_uuid"]), Decimal(0))),
                    "effective_cost": item["average_cost"],
                    "value": str(value),
                    "source_hash": row.source_hash,
                    "flags": ";".join(sorted(flags[key])),
                }
            )
    write(
        "stock-by-batch-location.csv",
        rows,
        [
            "month",
            "item",
            "item_uuid",
            "batch_uuid",
            "batch",
            "location",
            "location_uuid",
            "quantity",
            "available_quantity",
            "incoming_quantity",
            "effective_cost",
            "value",
            "source_hash",
            "flags",
        ],
    )
    movements = []
    for phase, events in detail["phases"].items():
        if phase.startswith("opening"):
            continue
        for event in events:
            batch = batches[event["batch_uuid"]]
            movements.append(
                {
                    "month": row.month,
                    "kind": "stock_adjustment" if event.get("receipt_type") == "stock_adjustment" else event["kind"],
                    "item": items[batch["item_uuid"]]["name"],
                    "batch_uuid": event["batch_uuid"],
                    "from": locations.get(event["source"], event["source"]),
                    "to": locations.get(event["destination"], event["destination"]),
                    "quantity": event["quantity"],
                    "source_ids": json.dumps(event.get("source_ids", [event.get("care_line_uuid")])),
                }
            )
    write("movements.csv", movements, ["month", "kind", "item", "batch_uuid", "from", "to", "quantity", "source_ids"])
    write(
        "exceptions.csv",
        [{"kind": flag["kind"], "detail": json.dumps(flag)} for flag in detail["flags"]],
        ["kind", "detail"],
    )
    (folder / "report-metadata.json").write_text(
        json.dumps(
            {
                "month": row.month,
                "facility_uuid": plan["period"]["facility_uuid"],
                "source_hash": row.source_hash,
                "frozen_at": row.created_at.isoformat(),
                "closed_at": row.closed_at.isoformat(),
                "total_value": close["total_value"],
                "source_basis": plan["metadata"],
                "exception_count": len(detail["flags"]),
            },
            indent=2,
        )
    )

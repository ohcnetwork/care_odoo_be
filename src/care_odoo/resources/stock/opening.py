"""Capture the prior-day Care report values and allocate them over owned stock."""

import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import connection

CENT = Decimal("0.01")
ZERO = Decimal(0)


def money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def item_name(value):
    return value.removeprefix("Internal Stock Update ").strip()


def balanced_round(values, total):
    """Preserve the approved currency total using largest-remainder rounding."""
    result = {key: money(value) for key, value in values.items()}
    delta = int((total - sum(result.values(), ZERO)) / CENT)
    if delta:
        order = sorted(values, key=lambda key: (values[key] - result[key], str(key)), reverse=delta > 0)
        if not order:
            raise ValueError("A nonzero approved value has no quantity allocation")
        for index in range(abs(delta)):
            result[order[index % len(order)]] += CENT if delta > 0 else -CENT
    return result


def opening_total(value):
    try:
        total = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("The first opening requires a numeric --expected-opening-total") from error
    if not total.is_finite() or total != money(total):
        raise ValueError("The expected opening total must be finite with at most two decimal places")
    return total


def capture_opening_report(facility, cutoff, expected_total):
    """Reproduce the existing report's value basis, independently of owned quantities.

    The transition value must preserve the previously approved financial closing.
    Its historical report includes priced rows and pending returns. Those rules
    are retained for this one-time value capture only; period.py independently
    reconstructs owned quantities, including unpriced stock and internal transit.
    """
    expected = opening_total(expected_total)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH parameters AS (
                SELECT %s::bigint AS facility_id, %s::timestamptz AS cutoff
            ), excluded AS (
                SELECT id FROM emr_facilitylocation
                WHERE inventory_usage IN ('consumption', 'adjustment', 'supplier')
            ), supplies AS (
                SELECT sd.status, sd.supplied_item_quantity AS quantity,
                    sd.supplied_item_id, ii.product_id AS source_product_id,
                    d.origin_id, d.destination_id
                FROM emr_supplydelivery sd
                JOIN emr_deliveryorder d ON d.id = sd.order_id
                JOIN emr_facilitylocation dst ON dst.id = d.destination_id
                LEFT JOIN emr_inventoryitem ii ON ii.id = sd.supplied_inventory_item_id
                CROSS JOIN parameters p
                WHERE sd.deleted = FALSE AND d.deleted = FALSE
                    AND dst.facility_id = p.facility_id AND sd.created_date < p.cutoff
            ), entries AS (
                SELECT supplied_item_id AS product_id, destination_id AS location_id, quantity
                FROM supplies WHERE status = 'completed' AND origin_id IS NULL
                UNION ALL
                SELECT source_product_id,
                    CASE WHEN destination_id IN (SELECT id FROM excluded)
                        THEN COALESCE(origin_id, destination_id) ELSE origin_id END,
                    -quantity
                FROM supplies WHERE status IN ('completed', 'in_progress')
                    AND source_product_id IS NOT NULL AND (
                        destination_id IN (SELECT id FROM excluded) OR (
                            origin_id IS NOT NULL AND origin_id NOT IN (SELECT id FROM excluded)
                            AND destination_id NOT IN (SELECT id FROM excluded)
                        )
                    )
                UNION ALL
                SELECT source_product_id, destination_id, quantity
                FROM supplies WHERE status IN ('completed', 'in_progress')
                    AND source_product_id IS NOT NULL AND (
                        origin_id IN (SELECT id FROM excluded) OR (
                            origin_id IS NOT NULL AND origin_id NOT IN (SELECT id FROM excluded)
                            AND destination_id NOT IN (SELECT id FROM excluded)
                        )
                    )
                UNION ALL
                SELECT ii.product_id,
                    CASE WHEN ii.location_id IN (SELECT id FROM excluded) THEN NULL ELSE ii.location_id END,
                    -md.quantity
                FROM emr_medicationdispense md
                JOIN emr_dispenseorder d ON d.id = md.order_id
                JOIN emr_inventoryitem ii ON ii.id = md.item_id
                JOIN emr_facilitylocation l ON l.id = ii.location_id
                CROSS JOIN parameters p
                WHERE md.deleted = FALSE AND d.deleted = FALSE
                    AND md.status NOT IN ('cancelled', 'entered_in_error', 'stopped', 'declined')
                    AND l.facility_id = p.facility_id AND md.created_date < p.cutoff
            )
            SELECT REPLACE(pk.name, 'Internal Stock Update ', '') AS name,
                COALESCE(l.name, '(no location)') AS location,
                SUM(e.quantity) AS quantity, SUM(e.quantity * product.purchase_price) AS value
            FROM entries e
            JOIN emr_product product ON product.id = e.product_id
            JOIN emr_productknowledge pk ON pk.id = product.product_knowledge_id
            LEFT JOIN emr_facilitylocation l ON l.id = e.location_id
            WHERE product.purchase_price IS NOT NULL
                AND (e.location_id IS NULL OR e.location_id NOT IN (SELECT id FROM excluded))
            GROUP BY REPLACE(pk.name, 'Internal Stock Update ', ''), l.id, l.name
            HAVING SUM(e.quantity) <> 0 OR SUM(e.quantity * product.purchase_price) <> 0
            ORDER BY name, location, l.id
            """,
            [facility.pk, cutoff],
        )
        rows = [
            {"name": name, "location": location, "quantity": str(quantity), "value": str(value)}
            for name, location, quantity, value in cursor.fetchall()
        ]
    raw_total = sum((Decimal(row["value"]) for row in rows), ZERO)
    total = money(raw_total)
    if total != expected:
        raise ValueError(
            f"Care opening report total {total} differs from the approved total {expected}. "
            "Review changed prices or source records; opening values will not be rescaled."
        )
    as_of = (cutoff - timedelta(microseconds=1)).date().isoformat()
    source = {"facility_uuid": str(facility.external_id), "as_of": as_of, "rows": rows}
    return {
        "rows": rows,
        "source_type": "care_closing_report",
        "source_name": f"Care stock report {as_of}",
        "as_of": as_of,
        "source_sha256": hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "raw_total": str(raw_total),
        "total": str(total),
    }


def allocate_opening(snapshot, products, used, quantities, flags):
    values, report_quantities = defaultdict(Decimal), defaultdict(Decimal)
    for row in snapshot["rows"]:
        name = item_name(row["name"])
        values[name] += Decimal(row["value"])
        report_quantities[name] += Decimal(row["quantity"])
    total = money(sum(values.values(), ZERO))
    if total != Decimal(snapshot["total"]):
        raise ValueError("Approved opening rows no longer match their frozen total")
    group_targets = balanced_round(values, total)
    names = {products[pid]["product_knowledge_id"]: item_name(products[pid]["product_knowledge__name"]) for pid in used}
    groups = defaultdict(list)
    for identifier, name in names.items():
        groups[name].append(identifier)
    targets = defaultdict(Decimal)
    allocations = []
    for name in sorted(set(groups) | set(values)):
        members = groups[name]
        quantity = sum((quantities[i] for i in members), ZERO)
        value = group_targets.get(name, ZERO)
        if value and not quantity:
            raise ValueError(f"Approved item {name!r} has value {value} but no Care opening quantity")
        if quantity:
            shares = {i: value * quantities[i] / quantity for i in members if quantities[i]}
            targets.update(balanced_round(shares, value))
        if quantity != report_quantities[name]:
            flags.append(
                {
                    "kind": "approved_opening_quantity_differs",
                    "item": name,
                    "report_quantity": str(report_quantities[name]),
                    "care_quantity": str(quantity),
                }
            )
        if name not in values and quantity:
            flags.append(
                {
                    "kind": "approved_opening_item_absent",
                    "item": name,
                    "care_quantity": str(quantity),
                    "opening_value": "0",
                }
            )
        allocations.append(
            {
                "item": name,
                "raw_report_value": str(values.get(name, ZERO)),
                "allocated_value": str(value),
                "report_quantity": str(report_quantities[name]),
                "care_quantity": str(quantity),
                "care_item_count": len(members),
            }
        )
    rates = {
        identifier: targets[identifier] / quantities[identifier] if quantities[identifier] else ZERO
        for identifier in names
    }
    return (
        rates,
        targets,
        {
            "source_name": snapshot["source_name"],
            "source_type": snapshot.get("source_type", "approved_values"),
            "as_of": snapshot.get("as_of"),
            "source_sha256": snapshot["source_sha256"],
            "raw_total": snapshot["raw_total"],
            "total": snapshot["total"],
            "basis": (
                "Approved report values allocated by Care opening base-unit quantities; no opening quantity changes"
            ),
            "currency_rounding": "Largest remainder by item name and then Care item ID",
            "allocations": allocations,
        },
    )

"""Build a replayable stock plan from Care movements and final Odoo bill inputs.

This is quantity/cost-source integration. Item average costing remains in Odoo.
Historical balances use current transaction states and creation-date cutoffs.
"""

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from care.emr.models.inventory_item import InventoryItem
from care.emr.models.location import FacilityLocation
from care.emr.models.medication_dispense import MedicationDispense
from care.emr.models.product import Product
from care.emr.models.supply_delivery import SupplyDelivery

from care_odoo.resources.stock.opening import allocate_opening
from care_odoo.resources.stock.resource import OdooStockResource
from care_odoo.settings import plugin_settings

ZERO = Decimal(0)
PAISA = Decimal("0.01")


def money(value):
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


def chunks(values, size=200):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def month_bounds(month):
    first = date.fromisoformat(f"{month}-01")
    if first.strftime("%Y-%m") != month:
        raise ValueError("Month must be YYYY-MM")
    next_month = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    zone = ZoneInfo("Asia/Kolkata")
    return datetime.combine(first, datetime.min.time(), zone), datetime.combine(next_month, datetime.min.time(), zone)


def build_period_plan(
    facility, month, progress=lambda _message: None, approved_opening=None, previous_close=None, owned_stock=False
):
    start, end = month_bounds(month)
    configured = plugin_settings.CARE_ODOO_INTERNAL_SUPPLIER_ID
    internal_suppliers = set(re.split(r"[\s,;]+", configured)) if isinstance(configured, str) else set(configured)
    opening_at = start - timedelta(seconds=1)
    closing_at = end - timedelta(seconds=1)
    flow_at = end - timedelta(seconds=2)
    period = {
        "period_key": f"care-stock:{facility.external_id}:{month}",
        "month": month,
        "facility_uuid": str(facility.external_id),
    }
    locations = {
        row["id"]: row
        for row in FacilityLocation._base_manager.filter(facility=facility).values(
            "id", "external_id", "name", "inventory_usage"
        )
    }
    stores = {
        key
        for key, row in locations.items()
        if row["inventory_usage"] == "store" or (owned_stock and row["inventory_usage"] == "none")
    }
    if previous_close and not owned_stock:
        scope = set(previous_close["location_uuids"])
        stores = {key for key, row in locations.items() if str(row["external_id"]) in scope}
        if len(stores) != len(scope):
            raise ValueError("A location from the previous closed period is missing")
    if approved_opening and not owned_stock:
        opening_locations = {r["location"] for r in approved_opening["rows"]}
        stores |= {
            key
            for key, row in locations.items()
            if row["name"] in opening_locations and row["inventory_usage"] == "none"
        }
        missing = opening_locations - {locations[key]["name"] for key in stores}
        if missing:
            raise ValueError(f"Opening locations cannot be mapped to Care: {sorted(missing)}")
    inventory = {
        row["id"]: row
        for row in InventoryItem._base_manager.filter(location__facility=facility).values(
            "id", "product_id", "location_id"
        )
    }
    products = {
        row["id"]: row
        for row in Product._base_manager.filter(facility=facility).values(
            "id",
            "external_id",
            "product_knowledge_id",
            "product_knowledge__external_id",
            "product_knowledge__name",
            "product_knowledge__base_unit",
            "charge_item_definition__external_id",
            "purchase_price",
            "batch",
        )
    }
    supplies = list(
        SupplyDelivery.objects.filter(created_date__lt=end, order__destination__facility=facility).values(
            "id",
            "external_id",
            "created_date",
            "modified_date",
            "status",
            "supplied_inventory_item_id",
            "supplied_item_id",
            "supplied_item_quantity",
            "supplied_item_pack_quantity",
            "supplied_item_pack_size",
            "total_purchase_price",
            "extensions",
            "order__external_id",
            "order__origin_id",
            "order__destination_id",
            "order__patient_id",
            "order__supplier_id",
            "order__supplier__external_id",
            "order__status",
        )
    )
    # Care's stock algorithm uses the source inventory product, not a display-name match.
    for row in supplies:
        source = inventory.get(row["supplied_inventory_item_id"])
        row["product_id"] = source["product_id"] if source else row["supplied_item_id"]
        row["source_location_id"] = source["location_id"] if source else row["order__origin_id"]
    purchases = [
        r
        for r in supplies
        if r["status"] == "completed"
        and r["order__origin_id"] is None
        and r["order__patient_id"] is None
        and r["order__destination_id"] in stores
        and (not (approved_opening or previous_close) or r["created_date"] >= start)
    ]
    financial = {}
    for batch in chunks(purchases, 400):
        response = OdooStockResource.bill_sources(
            period, [str(r["external_id"]) for r in batch], list({str(r["order__external_id"]) for r in batch})
        )
        for line in response["bill_lines"]:
            financial[line["id"]] = line
        progress(f"Read bill sources for {len(financial):,} Odoo lines")
    direct, header = defaultdict(list), defaultdict(list)
    for line in financial.values():
        if line["state"] == "posted" and line["move_type"] == "in_invoice":
            direct[line["care_line_uuid"]].append(line)
            header[(line["care_order_uuid"], line["product_care_id"])].append(line)
    costs, flags = {}, []
    allocated = set()
    pending = defaultdict(list)
    for row in purchases:
        matched = direct.get(str(row["external_id"]), [])
        if matched:
            costs[row["id"]] = (
                sum((Decimal(line["company_value"]) for line in matched), ZERO),
                "posted_bill",
                matched,
                "1",
            )
            allocated.update(line["id"] for line in matched)
        else:
            product = products[row["product_id"]]
            pending[(str(row["order__external_id"]), str(product["charge_item_definition__external_id"]))].append(row)
    for key, rows in pending.items():
        matched = [line for line in header.get(key, []) if line["id"] not in allocated]
        total_qty = sum((r["supplied_item_quantity"] or ZERO for r in rows), ZERO)
        if matched and total_qty > 0:
            value = sum((Decimal(line["company_value"]) for line in matched), ZERO)
            allocated_value = ZERO
            for index, row in enumerate(rows):
                quantity_fraction = (row["supplied_item_quantity"] or ZERO) / total_qty
                amount = value - allocated_value if index == len(rows) - 1 else money(value * quantity_fraction)
                allocated_value += amount
                fraction = amount / value if value else quantity_fraction
                costs[row["id"]] = (amount, "posted_bill", matched, str(fraction))
            allocated.update(line["id"] for line in matched)
    for row in purchases:
        if row["id"] in costs:
            continue
        product = products[row["product_id"]]
        quantity = row["supplied_item_quantity"] or ZERO
        rate = product["purchase_price"]
        basis = "care_legacy" if row["created_date"] < start else "care_unmatched"
        if rate is None and row["total_purchase_price"] and row["supplied_item_pack_size"]:
            rate = row["total_purchase_price"] / row["supplied_item_pack_size"]
        if rate is None:
            costs[row["id"]] = (None, basis, [], "1")
            continue
        ext = (row["extensions"] or {}).get("supply_delivery_extension") or {}
        free = Decimal(str(ext.get("free_quantity") or 0))
        discount = Decimal(str(ext.get("purchase_discount") or 0))
        billed = quantity - free * (row["supplied_item_pack_size"] or 1)
        if billed < 0 or discount < 0 or discount > 100:
            flags.append({"kind": "invalid_care_offer", "supply_id": row["id"]})
            billed, discount = quantity, ZERO
        costs[row["id"]] = (money(rate * billed * (1 - discount / 100)), basis, [], "1")

    opening, closing = defaultdict(Decimal), defaultdict(Decimal)
    transit_opening, transit_closing = defaultdict(Decimal), defaultdict(Decimal)
    transfer_evidence = []
    flow_groups = defaultdict(lambda: {"quantity": ZERO, "source_ids": []})
    receipt_rows = []

    def loc_key(identifier):
        return str(locations[identifier]["external_id"])

    def external_key(identifier):
        usage = locations.get(identifier, {}).get("inventory_usage")
        return "adjustment" if usage == "adjustment" else "consumption" if usage == "consumption" else "transit"

    def add_flow(pid, source, destination, kind, quantity, evidence):
        if source == destination or not quantity:
            return
        if quantity < 0:
            source, destination, quantity = destination, source, -quantity
            flags.append({"kind": "negative_movement", "source_id": evidence})
        group = flow_groups[(pid, source, destination, kind)]
        group["quantity"] += quantity
        group["source_ids"].append(evidence)

    for row in supplies:
        pid, quantity = row["product_id"], row["supplied_item_quantity"] or ZERO
        if pid is None or row["status"] not in {"in_progress", "completed"}:
            continue
        pending_internal = (
            owned_stock
            and row["status"] == "in_progress"
            and row["order__origin_id"] is not None
            and row["source_location_id"] in stores
            and row["order__destination_id"] in stores
        )
        incoming = (row["status"] == "completed" or pending_internal) and row["order__destination_id"] in stores
        outgoing = row["order__origin_id"] is not None and row["source_location_id"] in stores
        if pending_internal:
            key = (pid, row["order__destination_id"])
            transit_closing[key] += quantity
            if row["created_date"] < start:
                transit_opening[key] += quantity
            transfer_evidence.append(
                {
                    "supply_id": row["id"],
                    "care_line_uuid": str(row["external_id"]),
                    "care_order_uuid": str(row["order__external_id"]),
                    "parent_status": row["order__status"],
                    "created_at": row["created_date"].isoformat(),
                    "item_uuid": str(products[pid]["product_knowledge__external_id"]),
                    "source_location_uuid": loc_key(row["source_location_id"]),
                    "destination_location_uuid": loc_key(row["order__destination_id"]),
                    "quantity": str(quantity),
                }
            )
            # Care stock follows child-line status. Older parent workflows did
            # not always update children; retain the parent as evidence only.
        if incoming:
            key = (pid, row["order__destination_id"])
            closing[key] += quantity
            if row["created_date"] < start:
                opening[key] += quantity
        if outgoing:
            key = (pid, row["source_location_id"])
            closing[key] -= quantity
            if row["created_date"] < start:
                opening[key] -= quantity
        if row["created_date"] < start:
            continue
        is_purchase = incoming and row["order__origin_id"] is None and row["order__patient_id"] is None
        if is_purchase and quantity > 0:
            receipt_rows.append(row)
        elif incoming and outgoing:
            add_flow(
                pid,
                loc_key(row["source_location_id"]),
                loc_key(row["order__destination_id"]),
                "internal_transfer",
                quantity,
                str(row["external_id"]),
            )
        elif incoming:
            source = "consumption" if row["order__patient_id"] else external_key(row["source_location_id"])
            kind = (
                "patient_return"
                if owned_stock and (row["order__patient_id"] or source == "consumption")
                else "recovery"
            )
            add_flow(pid, source, loc_key(row["order__destination_id"]), kind, quantity, str(row["external_id"]))
        elif outgoing:
            destination = external_key(row["order__destination_id"])
            kind = "store_issue"
            if owned_stock:
                usage = locations.get(row["order__destination_id"], {}).get("inventory_usage")
                if usage == "supplier":
                    destination, kind = "external", "vendor_return"
                elif usage == "consumption":
                    kind = "consumption_issue"
                elif usage == "adjustment":
                    kind = "stock_adjustment_out"
            add_flow(pid, loc_key(row["source_location_id"]), destination, kind, quantity, str(row["external_id"]))
    for row in (
        MedicationDispense.objects.filter(created_date__lt=end, item__location_id__in=stores)
        .exclude(status__in=["cancelled", "entered_in_error", "stopped", "declined"])
        .values("external_id", "created_date", "item__product_id", "item__location_id", "quantity")
    ):
        key = (row["item__product_id"], row["item__location_id"])
        closing[key] -= row["quantity"]
        if row["created_date"] < start:
            opening[key] -= row["quantity"]
        else:
            kind = "patient_return" if owned_stock and row["quantity"] < 0 else "dispense"
            add_flow(key[0], loc_key(key[1]), "consumption", kind, row["quantity"], str(row["external_id"]))
    used = {p for (p, _loc), q in opening.items() if q} | {p for (p, _loc), q in closing.items() if q}
    used |= {r["product_id"] for r in receipt_rows} | {key[0] for key in flow_groups}
    used |= {pid for (pid, _loc), q in transit_closing.items() if q}
    historical = defaultdict(lambda: {"quantity": ZERO, "value": ZERO, "sources": Counter()})
    for row in purchases:
        amount, basis, _lines, _fraction = costs[row["id"]]
        if row["created_date"] < start and amount is not None and (row["supplied_item_quantity"] or 0) > 0:
            entry = historical[row["product_id"]]
            entry["quantity"] += row["supplied_item_quantity"]
            entry["value"] += amount
            entry["sources"][basis] += 1
    batch_rate, batch_basis = {}, {}
    for pid in used:
        entry = historical[pid]
        if entry["quantity"]:
            batch_rate[pid] = entry["value"] / entry["quantity"]
            batch_basis[pid] = dict(entry["sources"])
        else:
            batch_rate[pid] = products[pid]["purchase_price"]
            batch_basis[pid] = {"care_recorded_price": 1}
    known = defaultdict(lambda: [ZERO, ZERO])
    for (pid, _loc), quantity in opening.items():
        if quantity > 0 and batch_rate.get(pid) is not None:
            entry = known[products[pid]["product_knowledge_id"]]
            entry[0] += quantity
            entry[1] += quantity * batch_rate[pid]
    for pid in used:
        if batch_rate[pid] is None:
            qty, value = known[products[pid]["product_knowledge_id"]]
            batch_rate[pid] = value / qty if qty else ZERO
            batch_basis[pid] = {"same_item_opening_average" if qty else "unpriced": 1}
            flags.append(
                {
                    "kind": "missing_batch_price",
                    "product_id": pid,
                    "fallback": {"approved_opening_allocation": 1} if approved_opening else batch_basis[pid],
                }
            )
    item_quantity, item_value = defaultdict(Decimal), defaultdict(Decimal)
    for (pid, _loc), quantity in opening.items():
        if not quantity:
            continue
        item = products[pid]["product_knowledge_id"]
        item_quantity[item] += quantity
        item_value[item] += quantity * batch_rate[pid]
    item_rates = {}
    items = {}
    approved_metadata = None
    if approved_opening:
        item_rates, approved_targets, approved_metadata = allocate_opening(
            approved_opening, products, used, item_quantity, flags
        )
        for pid in used:
            batch_rate[pid] = item_rates[products[pid]["product_knowledge_id"]]
            batch_basis[pid] = {"approved_opening_quantity_allocation": 1}
    previous_items = {row["item_uuid"]: row for row in previous_close["items"]} if previous_close else {}
    if previous_close:
        for pid in used:
            item = products[pid]["product_knowledge_id"]
            uuid = str(products[pid]["product_knowledge__external_id"])
            previous = previous_items.get(uuid, {})
            item_rates[item] = Decimal(str(previous.get("average_cost", 0)))
            batch_rate[pid] = item_rates[item]
            batch_basis[pid] = {"previous_closed_month": 1}
    for pid in used:
        product = products[pid]
        item = product["product_knowledge_id"]
        if item not in items:
            rate = (
                item_rates[item]
                if approved_opening or previous_close
                else item_value[item] / item_quantity[item]
                if item_quantity[item]
                else batch_rate[pid]
            )
            if rate < 0 and not approved_opening:
                flags.append({"kind": "negative_opening_unit_value", "item_id": item, "value": str(rate)})
                rate = batch_rate[pid]
            item_rates[item] = rate
            items[item] = {
                "uuid": str(product["product_knowledge__external_id"]),
                "name": product["product_knowledge__name"],
                "opening_unit_cost": str(rate),
                "base_unit": product["product_knowledge__base_unit"],
                "opening_quantity": str(item_quantity[item]),
                "opening_value_target": str(
                    approved_targets[item]
                    if approved_opening
                    else previous_items.get(str(product["product_knowledge__external_id"]), {}).get(
                        "reported_total_value", 0
                    )
                    if previous_close
                    else money(item_value[item])
                ),
            }
    phases = {key: [] for key in ["opening_in", "opening_out", "receipts", "recoveries", "internal", "issues"]}

    def event(pid, key, source, destination, quantity, kind, date_at, basis="native_average", value=None, **extra):
        result = {
            "key": f"{period['period_key']}:{key}",
            "batch_uuid": str(products[pid]["external_id"]),
            "source": source,
            "destination": destination,
            "quantity": str(quantity),
            "kind": kind,
            "date": date_at.isoformat(),
            "cost_basis": basis,
            **extra,
        }
        if value is not None:
            result["input_value"] = str(value)
        return result

    positive = defaultdict(list)
    for (pid, loc), quantity in opening.items():
        item = products[pid]["product_knowledge_id"]
        if quantity > 0:
            positive[item].append((pid, loc, quantity))
        elif quantity < 0:
            phases["opening_out"].append(
                event(
                    pid, f"opening:{pid}:{loc}", loc_key(loc), "adjustment", -quantity, "opening_negative", opening_at
                )
            )
    for item, rows in positive.items():
        target = money(sum((q for _p, _l, q in rows), ZERO) * item_rates[item])
        allocated_value = ZERO
        for index, (pid, loc, quantity) in enumerate(rows):
            value = target - allocated_value if index == len(rows) - 1 else money(quantity * item_rates[item])
            allocated_value += value
            phases["opening_in"].append(
                event(
                    pid,
                    f"opening:{pid}:{loc}",
                    "external",
                    loc_key(loc),
                    quantity,
                    "opening",
                    opening_at,
                    "opening",
                    value,
                )
            )
    for row in receipt_rows:
        supplier_uuid = str(row["order__supplier__external_id"]) if row["order__supplier__external_id"] else None
        receipt_type = "stock_adjustment" if supplier_uuid in internal_suppliers else "purchase"
        amount, basis, lines, fraction = costs[row["id"]]
        if amount is None:
            amount = money(batch_rate[row["product_id"]] * row["supplied_item_quantity"])
            flags.append({"kind": "unpriced_receipt", "supply_id": row["id"]})
        if basis != "posted_bill":
            flags.append(
                {
                    "kind": "internal_stock_adjustment_care_cost"
                    if receipt_type == "stock_adjustment"
                    else "receipt_without_posted_bill",
                    "supply_id": row["id"],
                    "supplier_id": row["order__supplier_id"],
                    "value": str(amount),
                    "item_uuid": str(products[row["product_id"]]["product_knowledge__external_id"]),
                }
            )
        else:
            received_packs = sum((Decimal(line["received_packs"]) for line in lines), ZERO) * Decimal(fraction)
            if row["supplied_item_pack_quantity"] is not None and received_packs != row["supplied_item_pack_quantity"]:
                flags.append(
                    {
                        "kind": "bill_received_quantity_differs",
                        "supply_id": row["id"],
                        "care_packs": row["supplied_item_pack_quantity"],
                        "odoo_packs": str(received_packs),
                    }
                )
        phases["receipts"].append(
            event(
                row["product_id"],
                f"receipt:{row['external_id']}",
                "external",
                loc_key(row["order__destination_id"]),
                row["supplied_item_quantity"],
                "receipt",
                row["created_date"],
                basis,
                amount,
                care_line_uuid=str(row["external_id"]),
                care_order_uuid=str(row["order__external_id"]),
                care_supply_id=row["id"],
                supplier_uuid=supplier_uuid,
                receipt_type=receipt_type,
                product_care_id=str(products[row["product_id"]]["charge_item_definition__external_id"]),
                bill_line_ids=[line["id"] for line in lines],
                bill_fraction=fraction,
                pack_quantity=row["supplied_item_pack_quantity"],
                pack_size=row["supplied_item_pack_size"],
            )
        )
    for (pid, source, destination, kind), group in flow_groups.items():
        signature = hashlib.sha256(json.dumps([pid, source, destination, kind]).encode()).hexdigest()[:24]
        phase = (
            "recoveries"
            if source in {"external", "consumption", "adjustment", "transit"}
            else "internal"
            if destination not in {"external", "consumption", "adjustment", "transit"}
            else "issues"
        )
        phases[phase].append(
            event(
                pid,
                f"flow:{signature}",
                source,
                destination,
                group["quantity"],
                kind,
                flow_at,
                source_ids=sorted(group["source_ids"]),
            )
        )

    def dimensions(values, retain=()):
        return [
            {
                "batch_uuid": str(products[pid]["external_id"]),
                "item_uuid": str(products[pid]["product_knowledge__external_id"]),
                "location_uuid": loc_key(loc),
                "quantity": str(values.get((pid, loc), ZERO)),
            }
            for pid, loc in sorted(set(values) | set(retain))
            if values.get((pid, loc), ZERO) or (pid, loc) in retain
        ]

    period_bill_ids = {line["bill_id"] for row in receipt_rows for line in costs[row["id"]][2]}
    for line in financial.values():
        if (
            line["bill_id"] in period_bill_ids
            and line["id"] not in allocated
            and line["state"] == "posted"
            and line["move_type"] == "in_invoice"
            and not line["care_line_uuid"]
            and not line["product_care_id"]
            and Decimal(line["company_value"])
        ):
            flags.append(
                {
                    "kind": "bill_line_without_care_product_link",
                    "bill_line_id": line["id"],
                    "bill_id": line["bill_id"],
                    "bill_name": line["bill_name"],
                    "value": line["company_value"],
                }
            )

    plan = {
        "period": period,
        "opening_at": opening_at.isoformat(),
        "closing_at": closing_at.isoformat(),
        "locations": [{"uuid": loc_key(loc), "name": locations[loc]["name"]} for loc in sorted(stores)],
        "items": list(items.values()),
        "batches": [
            {
                "uuid": str(products[pid]["external_id"]),
                "item_uuid": str(products[pid]["product_knowledge__external_id"]),
                "lot_number": (products[pid]["batch"] or {}).get("lot_number"),
                "care_product_id": pid,
                "opening_price_basis": batch_basis[pid],
                "opening_batch_rate": str(batch_rate[pid]),
            }
            for pid in sorted(used)
        ],
        "opening": dimensions(opening, transit_opening if owned_stock else ()),
        "closing": dimensions(closing, transit_closing if owned_stock else ()),
        "available_opening": dimensions(
            {
                key: opening.get(key, ZERO) - transit_opening.get(key, ZERO)
                for key in opening.keys() | transit_opening.keys()
            }
        ),
        "available_closing": dimensions(
            {
                key: closing.get(key, ZERO) - transit_closing.get(key, ZERO)
                for key in closing.keys() | transit_closing.keys()
            }
        ),
        "in_transit_opening": dimensions(transit_opening),
        "in_transit_closing": dimensions(transit_closing),
        "pending_internal_transfers": transfer_evidence,
        "phases": phases,
        "flags": flags,
        "metadata": {
            "quantity_basis": "Current transaction states, created_date cutoffs, store locations only",
            "opening_value_basis": (
                "Pre-period receipt cost average per Care batch; posted Odoo bill value where linked, "
                "otherwise Care price; pooled by item for AVCO"
            ),
            "financial_basis": (
                "Posted company-currency product-line balance, excluding GST; "
                "unmatched receipts use Care prices and are flagged"
            ),
            "phase_counts": {key: len(value) for key, value in phases.items()},
            "negative_opening_dimensions": sum(q < 0 for q in opening.values()),
            "negative_closing_dimensions": sum(q < 0 for q in closing.values()),
            "supply_rows_modified_after_period": sum(r["modified_date"] >= end for r in supplies),
            "flag_counts": dict(Counter(row["kind"] for row in flags)),
        },
    }
    if approved_metadata:
        plan["metadata"]["approved_opening"] = approved_metadata
        plan["metadata"]["quantity_basis"] = (
            "Current Care transaction states and cutoffs; stores plus non-store opening locations carried forward"
        )
        plan["metadata"]["opening_value_basis"] = (
            "Frozen approved opening report values divided by Care opening quantities"
        )
    if previous_close:
        plan["metadata"]["opening_value_basis"] = "Previous immutable Odoo month-end snapshot"
        plan["metadata"]["quantity_basis"] = (
            "Current Care transaction states and cutoffs; previous month's frozen location scope"
        )
    plan["metadata"]["stock_basis"] = "owned" if owned_stock else "available"
    if owned_stock:
        plan["metadata"]["quantity_basis"] = (
            "Hospital-owned stock: available stock plus internal stock in transit; "
            "store and none locations included, consumption/adjustment/supplier locations excluded"
        )
        plan["metadata"]["pending_internal_transfer_lines"] = len(transfer_evidence)
    return plan

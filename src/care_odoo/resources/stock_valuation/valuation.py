"""
FIFO valuation of a facility's stock as of a moment, one row per batch.

A Care Product is one batch, and dispenses and transfers always name the batch,
so cost never mixes across batches. Within a batch, every purchase receipt is a
cost layer. Whatever the facility still holds is taken from the newest layers
(FIFO: the oldest units left first).

The facility holds what sits in store or general ("none") locations that are not
deleted, including
stock moving between them. Stock leaves when it is dispensed or sent to a
consumption, adjustment or supplier location. All dates are created_date and
statuses are as they are today, so a later cancellation also changes earlier
dates.

With by_location, each batch row is split by where its stock sits, every share at the
batch's facility FIFO unit cost, so the split always adds up to the batch row. Stock
moving between two held locations counts at its destination.
"""

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from care.emr.models.location import FacilityLocation
from care.emr.models.medication_dispense import MedicationDispense
from care.emr.models.product import Product
from care.emr.models.supply_delivery import SupplyDelivery
from care.emr.resources.inventory.supply_delivery.spec import (
    SupplyDeliveryStatusOptions,
)
from care.emr.resources.medication.dispense.spec import (
    MEDICATION_DISPENSE_CANCELLED_STATUSES,
)
from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from care_odoo.extensions import VALUATION_COST
from care_odoo.resources.stock_valuation.costing import care_unit_cost

ZERO = Decimal(0)
HELD_USAGES = ("store", "none")


def held(location):
    """Locations whose stock counts: store or general ones that have not been deleted."""
    return Q(**{f"{location}__inventory_usage__in": HELD_USAGES, f"{location}__deleted": False})


COMPLETED = SupplyDeliveryStatusOptions.completed.value
LEAVING = [SupplyDeliveryStatusOptions.in_progress.value, COMPLETED]
COLUMNS = [
    "product_id",
    "product_name",
    "batch",
    "expiration_date",
    "quantity",
    "unit_cost",
    "value",
    "cost_sources",
    "flags",
]
LOCATION_COLUMNS = COLUMNS[:4] + ["location_id", "location_name"] + COLUMNS[4:]


@dataclass
class Layer:
    quantity: Decimal
    unit_cost: Decimal | None
    source: str


@dataclass
class BatchValue:
    quantity: Decimal = ZERO
    value: Decimal = ZERO
    unvalued_quantity: Decimal = ZERO
    sources: set = field(default_factory=set)

    @property
    def unit_cost(self):
        valued = self.quantity - self.unvalued_quantity
        return self.value / valued if valued > 0 else None


def held_quantities(facility, as_of):
    """Quantity per batch held by the facility, from movements up to as_of."""
    deliveries = SupplyDelivery.objects.filter(
        deleted=False,
        order__deleted=False,
        order__destination__facility=facility,
        supplied_inventory_item__isnull=False,
        created_date__lte=as_of,
    )
    into_held = held("order__destination")
    from_held = Q(order__origin__isnull=False) & held("supplied_inventory_item__location")
    incoming = deliveries.filter(into_held & ~from_held, status=COMPLETED)
    outgoing = deliveries.filter(from_held & ~into_held, status__in=LEAVING)
    dispensed = MedicationDispense.objects.filter(
        held("item__location"),
        deleted=False,
        item__location__facility=facility,
        created_date__lte=as_of,
    ).exclude(status__in=MEDICATION_DISPENSE_CANCELLED_STATUSES)
    quantities = defaultdict(lambda: ZERO)
    for sign, rows, batch, amount in (
        (1, incoming, "supplied_inventory_item__product_id", "supplied_item_quantity"),
        (-1, outgoing, "supplied_inventory_item__product_id", "supplied_item_quantity"),
        (-1, dispensed, "item__product_id", "quantity"),
    ):
        totals = rows.values(batch_id=F(batch)).annotate(total=Sum(amount))
        for row in totals:
            quantities[row["batch_id"]] += sign * (row["total"] or ZERO)
    return quantities


def location_quantities(facility, as_of):
    """
    Quantity per (batch, location) in held locations, as Care keeps it per location
    (sync_inventory_item), with held-to-held transfers not yet received counted at the destination.
    """
    deliveries = SupplyDelivery.objects.filter(
        deleted=False,
        order__deleted=False,
        order__destination__facility=facility,
        supplied_inventory_item__isnull=False,
        created_date__lte=as_of,
    )
    incoming = deliveries.filter(held("order__destination"), status=COMPLETED)
    outgoing = deliveries.filter(
        held("supplied_inventory_item__location"), order__origin__isnull=False, status__in=LEAVING
    )
    in_transit = outgoing.filter(held("order__destination"), status=SupplyDeliveryStatusOptions.in_progress.value)
    dispensed = MedicationDispense.objects.filter(
        held("item__location"),
        deleted=False,
        item__location__facility=facility,
        created_date__lte=as_of,
    ).exclude(status__in=MEDICATION_DISPENSE_CANCELLED_STATUSES)
    quantities = defaultdict(lambda: ZERO)
    for sign, rows, batch, place, amount in (
        (1, incoming, "supplied_inventory_item__product_id", "order__destination_id", "supplied_item_quantity"),
        (
            -1,
            outgoing,
            "supplied_inventory_item__product_id",
            "supplied_inventory_item__location_id",
            "supplied_item_quantity",
        ),
        (1, in_transit, "supplied_inventory_item__product_id", "order__destination_id", "supplied_item_quantity"),
        (-1, dispensed, "item__product_id", "item__location_id", "quantity"),
    ):
        for row in rows.values(batch_id=F(batch), place_id=F(place)).annotate(total=Sum(amount)):
            quantities[(row["batch_id"], row["place_id"])] += sign * (row["total"] or ZERO)
    return quantities


def cost_layers(facility, as_of):
    """Purchase receipts per batch, oldest first, each with an effective unit cost."""
    lines = (
        SupplyDelivery.objects.filter(
            deleted=False,
            order__deleted=False,
            order__destination__facility=facility,
            order__origin__isnull=True,
            order__patient__isnull=True,
            status=COMPLETED,
            created_date__lte=as_of,
        )
        .order_by("created_date", "id")
        .values(
            "supplied_item_quantity",
            "supplied_item_pack_size",
            "total_purchase_price",
            "extensions",
            product_id=Coalesce("supplied_item_id", "supplied_inventory_item__product_id"),
            product_purchase_price=Coalesce(
                "supplied_item__purchase_price",
                "supplied_inventory_item__product__purchase_price",
            ),
        )
    )
    layers = defaultdict(list)
    for line in lines.iterator(chunk_size=2000):
        confirmed = (line["extensions"] or {}).get(VALUATION_COST) or {}
        if confirmed.get("unit_cost"):
            unit_cost = Decimal(confirmed["unit_cost"])
            source = confirmed.get("source") or "confirmed"
        else:
            unit_cost, source = care_unit_cost(line)
        layers[line["product_id"]].append(
            Layer(
                quantity=line["supplied_item_quantity"] or ZERO,
                unit_cost=unit_cost,
                source=source,
            )
        )
    return layers


def fifo_batch_value(quantity, layers, fallback_cost):
    """Value `quantity` units from the newest layers backwards."""
    result = BatchValue(quantity=quantity)
    remaining = quantity
    for layer in reversed(layers):
        if remaining <= 0:
            break
        taken = min(remaining, layer.quantity)
        remaining -= taken
        result.sources.add(layer.source)
        if layer.unit_cost is None:
            result.unvalued_quantity += taken
        else:
            result.value += taken * layer.unit_cost
    if remaining > 0:
        # Stock with no purchase layer left: opening stock or non-purchase inflows.
        if fallback_cost is None:
            result.unvalued_quantity += remaining
            result.sources.add("no_layer_no_price")
        else:
            result.value += remaining * fallback_cost
            result.sources.add("no_layer_product_price")
    return result


def value_inventory(facility, as_of, by_location=False):
    """One row per batch the facility holds (or per batch and location), with FIFO value and any gaps flagged."""
    quantities = {batch_id: quantity for batch_id, quantity in held_quantities(facility, as_of).items() if quantity}
    layers = cost_layers(facility, as_of)
    products = Product.objects.filter(id__in=quantities).values(
        "id",
        "external_id",
        "batch",
        "expiration_date",
        "purchase_price",
        name=F("product_knowledge__name"),
    )
    rows = []
    for product in products.iterator(chunk_size=2000):
        quantity = quantities[product["id"]]
        batch = fifo_batch_value(
            max(quantity, ZERO),
            layers.get(product["id"], []),
            product["purchase_price"],
        )
        flags = sorted(source for source in batch.sources if source.startswith(("no_", "missing", "invalid")))
        if quantity < 0:
            flags.append("negative_quantity")
        unit_cost = batch.unit_cost
        rows.append(
            {
                "batch_id": product["id"],
                "product_id": str(product["external_id"]),
                "product_name": product["name"],
                "batch": (product["batch"] or {}).get("lot_number"),
                "expiration_date": product["expiration_date"],
                "quantity": quantity,
                "unit_cost": unit_cost,
                "value": quantity * unit_cost if unit_cost is not None else None,
                "cost_sources": ",".join(sorted(batch.sources)),
                "flags": ",".join(flags),
            }
        )
    if by_location:
        rows = split_by_location(facility, as_of, rows)
    for row in rows:
        del row["batch_id"]
    rows.sort(key=lambda row: (row["product_name"] or "", row["product_id"], row.get("location_name") or ""))
    return rows


def split_by_location(facility, as_of, batch_rows):
    """Each batch row once per location holding it, valued at the batch's unit cost."""
    by_batch = defaultdict(list)
    for (batch_id, place_id), quantity in location_quantities(facility, as_of).items():
        if quantity:
            by_batch[batch_id].append((place_id, quantity))
    places = {
        place["id"]: place
        for place in FacilityLocation.objects.filter(facility=facility).values("id", "external_id", "name")
    }
    rows = []
    for row in batch_rows:
        for place_id, quantity in by_batch[row["batch_id"]]:
            place = places[place_id]
            rows.append(
                {
                    **row,
                    "location_id": str(place["external_id"]),
                    "location_name": place["name"],
                    "quantity": quantity,
                    "value": quantity * row["unit_cost"] if row["unit_cost"] is not None else None,
                }
            )
    return rows


def end_of_day(value):
    """The last moment of a YYYY-MM-DD day in Care's time zone."""
    return datetime.combine(date.fromisoformat(value), time.max, tzinfo=timezone.get_default_timezone())


def write_csv(rows, handle, by_location=False):
    writer = csv.DictWriter(handle, fieldnames=LOCATION_COLUMNS if by_location else COLUMNS)
    writer.writeheader()
    writer.writerows(rows)

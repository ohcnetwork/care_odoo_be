"""Approved opening values are frozen inputs, allocated over Care quantities."""

import hashlib
import xml.etree.ElementTree as ET
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zipfile import ZipFile

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


def load_opening_report(path, expected_total):
    if expected_total is None:
        raise ValueError("An opening report requires --expected-opening-total")
    path = Path(path)
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = []
    with ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = [
                "".join(node.itertext())
                for node in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", ns)
            ]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        for row in sheet.findall("s:sheetData/s:row", ns):
            cells = {}
            for cell in row.findall("s:c", ns):
                if cell.find("s:f", ns) is not None:
                    raise ValueError("The approved opening must contain fixed values, not formulas")
                column = "".join(c for c in cell.attrib["r"] if c.isalpha())
                value = cell.find("s:v", ns)
                if cell.get("t") == "inlineStr":
                    cells[column] = "".join(cell.find("s:is", ns).itertext())
                elif cell.get("t") == "s":
                    cells[column] = strings[int(value.text)]
                elif value is not None:
                    cells[column] = value.text
            if row.get("r") == "1":
                if [cells.get(c) for c in "ABCD"] != ["location", "name", "total_qty", "stock_value"]:
                    raise ValueError("Expected location, name, total_qty, stock_value columns")
                continue
            if not cells:
                continue
            quantity, value = Decimal(cells["C"]), Decimal(cells["D"])
            if not quantity.is_finite() or not value.is_finite() or not cells["A"] or not cells["B"]:
                raise ValueError("Opening rows need names and finite quantities/values")
            rows.append(
                {
                    "location": cells["A"],
                    "name": cells["B"],
                    "quantity": str(quantity),
                    "value": str(value),
                    "excel_row": int(row.get("r")),
                }
            )
    raw_total = sum((Decimal(row["value"]) for row in rows), ZERO)
    total = money(raw_total)
    expected = Decimal(expected_total)
    if not expected.is_finite() or total != expected:
        raise ValueError(f"Workbook total {total} differs from the approved total {expected_total}")
    return {
        "rows": rows,
        "source_name": path.name,
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
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

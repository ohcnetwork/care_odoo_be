# Monthly inventory valuation

Care supplies base-unit quantities and receipt evidence. Odoo supplies final posted
bill costs and calculates native average cost (AVCO). The integration posts monthly
product totals to a dedicated valuation location. Care retains batch and location
detail for exports and investigation.

Install the companion `care_connector` stock API in Odoo 19, run Django migrations,
and configure the existing Odoo connection. The service user needs Inventory
Administrator access and access to vendor bills in the intended company.

## Opening stock from Care

The first month captures the previous day's facility stock report directly from
Care. For an April start, this is the 31 March closing. A Metabase export of that
report is unnecessary: there is no file upload or Metabase connection to configure.

Supply `--expected-opening-total` once to check the approved financial closing.
The command stops if the Care report differs; it never rescales values to force a
match. The report rows, hash and allocated opening values are frozen in Care and
exported as `opening-source.json`. A retry uses that saved source even if prices
have since changed.

Opening values reproduce the historical report's Care prices and original status
rules, including its priced-row filter and pending-return treatment. This preserves
the approved financial baseline. Owned quantities are reconstructed independently
using the corrected rules below, so unpriced stock is retained and pending returns
from non-stock locations do not become owned stock. Any quantity differences and
unvalued items remain visible in the exception export.

The legacy `Internal Stock Update ` prefix is removed when matching item names.
Duplicate legacy/current names share their approved value in proportion to their
Care opening quantities. The first run posts the opening once; later months carry
the previous close. Do not separately import the same opening into Odoo.

Freeze and inspect the first month before posting:

```sh
python manage.py close_stock_month --facility <facility-uuid> \
  --month 2026-04 --fiscal-month 2026-04 \
  --expected-opening-total 800.00 \
  --output /path/reports/2026-04
```

Odoo's opening quantities and per-item values are checked against the approved
allocation. Opening capture uses current Care records and prices, not archived
price versions. Review a mismatch with the approved closing before the first
freeze. Existing frozen sources and issued reports are never regenerated.

## Closing and exporting

Set `CARE_ODOO_STOCK_SYNC_ENABLED=true` explicitly, then repeat the command with
`--post`. The setting defaults to false and is separate from invoice/payment sync.
The next month uses the same first opening month and omits the opening arguments:

```sh
python manage.py close_stock_month --facility <facility-uuid> \
  --month 2026-05 --fiscal-month 2026-04 --post --output /path/reports/2026-05
```

Plans and closed snapshots are stored in Care's `StockMonth` model. PostgreSQL
guards prevent changes to frozen plans and issued snapshots. Event keys, facility
locks and the Odoo close status support retries, including a lost close response.
Months must close consecutively and cannot close before month-end. The Odoo
connection is bound to each frozen source plan.

The command exports item valuation, batch/location valuation, source transactions,
exceptions and frozen JSON. Batch/location values allocate the native rounded item
totals. Negative quantities and missing costs stay visible. Historical negative
balances are not automatically written off or removed from reports.

Export an issued report without fetching current quantities or prices:

```sh
python manage.py export_stock_month --facility <facility-uuid> \
  --month 2026-04 --output /path/reports/2026-04
```

The Celery entry point is
`care_odoo.tasks.close_stock_month(facility_uuid, month, fiscal_month)`. It becomes
usable after the approved first opening has been frozen. No recurring schedule is
installed. `CARE_ODOO_AUTO_SYNC_ENABLED` defaults to true and can disable the
existing invoice/payment signals independently for an isolated valuation instance.
`CARE_ODOO_REQUEST_TIMEOUT` configures HTTP timeouts (default 120 seconds).

## Quantity rules

- Include all Care locations typed `store` or `none`, including locations first
  stocked after the opening. Exclude consumption, adjustment and supplier locations.
- Include quantities even when Care's purchase price is missing.
- Issued internal transfers reduce source availability and remain owned as incoming
  stock at the destination. The destination quantity becomes available on completion.
- Returns from non-stock locations enter owned stock only after completion.
- Child delivery and dispense statuses drive the calculation. Parent statuses are
  historical context and do not create inventory-error warnings.

[The quantity-only SQL example](stock-quantities.sql) applies these rules to a dated
Care stock report. Join Odoo prices by Care item UUID, not display name. The SQL is
a reporting template; installation does not change saved Metabase questions.

Dates use Asia/Kolkata. Historical reconstruction uses current child transaction
statuses and creation-date cutoffs; earlier versions of edited records cannot be
recovered. Issued reports remain frozen rather than recalculating from live data.

## Cost inputs and corrections

Purchase quantities include free units. Receipt value uses final posted vendor-bill
product-line company-currency balances after discounts, excluding GST. Missing
matches use recorded Care costs and are flagged. Configure
`CARE_ODOO_INTERNAL_SUPPLIER_ID` to classify the hospital's own stock additions;
those are not vendor purchases and do not require a vendor bill.

Odoo pools monthly purchases before recoveries and issues. The integration uses
one base-unit stock product per Care item/facility UUID, separate from existing
pack-based billing products. Odoo's Product Price precision becomes 12 decimals and
Product Unit precision becomes six; these settings affect the Odoo instance.

Changes to linked bill amounts or posting state stop a later close for review.
Already issued reports remain unchanged. Automatic forward financial adjustments
are not implemented, and newly added bills for previously unmatched receipts need
separate reconciliation. Resolve rejected source data before scheduling: pending
plans are immutable too. This first version does not post an opening or month-end
general-ledger journal.

## Verification

Run the tests from a configured Care backend with this plugin and the backend's
test dependencies installed:

```sh
python manage.py test care_odoo.tests --keepdb
ruff check src/care_odoo/resources/stock src/care_odoo/tests
```

The tests exercise actual Care transactions, free quantities and discounts,
internal transit, completed returns, roll-forward, retries, frozen database records
and export totals. Native Odoo costing and the stock protocol are tested in the
companion add-on.

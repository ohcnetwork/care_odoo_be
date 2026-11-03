# Monthly inventory valuation

Care supplies base-unit quantities and receipt evidence. Odoo supplies final posted
bill costs and calculates native average cost (AVCO). Only monthly product summaries
are posted to stock. Each report includes location, batch, expiry, available and
incoming quantities, effective unit cost and value in both systems.

Install the companion `care_connector` API in Odoo 19, run Django migrations and
configure the existing Odoo connection. The service user needs Inventory
Administrator and vendor-bill access in the intended company.

## First opening

The first month captures the previous day's Care stock report directly. For an
April start, this is the 31 March closing. There is no Excel upload or Metabase
connection. Supply the approved financial total once:

```sh
python manage.py close_stock_month --facility <facility-uuid> \
  --month 2026-04 --fiscal-month 2026-04 --expected-opening-total 800.00 \
  --output /path/reports/2026-04
```

This prepares a plan for inspection. Set `CARE_ODOO_STOCK_SYNC_ENABLED=true` and
repeat with `--post` to post and publish it. The setting defaults to false and is
separate from invoice/payment sync. No scheduler is installed automatically.

Opening values reproduce the historical report's Care prices and original status
rules, including its priced-row filter and pending-return treatment, to preserve
the approved financial baseline. Owned quantities are reconstructed independently
using the corrected rules below. Quantity differences and unvalued items remain
visible in the exceptions. Values are never rescaled to force the approved total.

The legacy `Internal Stock Update ` prefix is removed when matching item names.
Duplicate legacy/current names share their approved value in proportion to Care
opening quantities. Opening report rows and allocated values are saved and exported
as `opening-source.json`. Retries and rebuilds reuse this financial opening even
when Care prices change. Do not separately import the same opening into Odoo.

Initial capture uses current Care records and prices; earlier versions of edited
records cannot be recovered. Review any mismatch before the first publication.

## Following months

```sh
python manage.py close_stock_month --facility <facility-uuid> \
  --month 2026-05 --fiscal-month 2026-04 --post --output /path/reports/2026-05
```

Months run consecutively after month-end. Later months carry the previous close.
Saved plans, event keys and facility locks support retries without duplicate stock,
including a lost close response. Repeating a completed command returns its saved
report; use `--rebuild` to apply source corrections.

## Corrections: rebuild and archive

```sh
python manage.py close_stock_month --facility <facility-uuid> \
  --month 2026-04 --fiscal-month 2026-04 --rebuild --post \
  --output /path/reports/rebuilt
```

One action rebuilds from the saved opening through the latest processed month,
including months after `--month`. It re-reads Care quantities and current Odoo bills.
The old reports stay current until every replacement month is complete. Publication
retires the previous native Odoo stock balances, activates the replacement and
archives previous report revisions in both databases. It does not rewrite old
issued reports. A failed rebuild can be rerun after correcting the source; no
manual unlocking is needed. Failed earlier attempts are retired on publication too.

Rebuilds use separate integration stock products so old and new cost histories do
not mix. Building products are inactive. The connector's published reports are the
month-end reporting source; generic stock queries that include inactive products
can also see work in progress. Old product balances are zero after publication.
Care holds a facility lock for the rebuild, and Odoo checks the current revision
before switching. A lost publish response is recovered on the next rebuild.

There are no database freeze triggers or custom ORM locks on historical records.
Correct source records and rebuild rather than editing generated stock postings.
Saved report revisions remain available for comparison:

```sh
python manage.py export_stock_month --facility <facility-uuid> \
  --month 2026-04 --output /path/reports/current
# Add --revision <revision-uuid> to export an archived report.
```

## Reports

Exports contain item valuation, batch/location valuation, source transactions,
exceptions and saved JSON. Batch/location quantities reconcile with product totals;
values allocate the native rounded product total exactly. One monthly weighted
average applies to a product across its batches and locations. Missing prices and
negative balances remain flagged.

In Odoo, open **Inventory > Reporting > Care Monthly Stock**. Current reports are
shown by default; filters also expose archived and building revisions. Open a
report and choose **Batch and Location Report** for an exportable list. These
locations and batches are report dimensions; native stock uses one valuation
location and no lot tracking for each revision.

## Quantity and cost rules

- Include Care locations typed `store` or `none`, including newly stocked locations.
  Exclude consumption, adjustment and supplier locations.
- Keep quantities even when Care purchase price is missing.
- Issued internal transfers reduce source availability and remain owned as incoming
  stock at the destination. They become available there on completion.
- Returns from non-stock locations enter owned stock only after completion.
- Child delivery and dispense statuses drive quantity calculations. Parent statuses
  are historical context.
- Include free purchase units. Receipt value uses final posted vendor-bill product
  line company-currency balances after discounts, excluding GST.
- Missing bill matches use recorded Care costs and are flagged. Configure
  `CARE_ODOO_INTERNAL_SUPPLIER_ID` to identify internal stock additions.

Odoo pools purchases before recoveries and issues. Integration products use base
units and are separate from pack-based billing products. Product Price precision
becomes 12 decimals and Product Unit precision six; these are instance-wide settings.

Changes to previously linked bills stop a following close with a request to rebuild.
A rebuild also picks up newly posted bills for previously unmatched receipts.
This integration uses periodic valuation and does not create opening or closing
general-ledger journals.

[The quantity-only SQL example](stock-quantities.sql) uses the same owned-stock
rules. Join prices by Care item UUID, not display name. Installation does not change
saved Metabase questions. Dates use Asia/Kolkata; reconstruction uses current child
statuses and creation-date cutoffs. Saved reports do not recalculate when viewed.

The opt-in Celery entry point is
`care_odoo.tasks.close_stock_month(facility_uuid, month, fiscal_month)`.
`CARE_ODOO_AUTO_SYNC_ENABLED` controls existing invoice/payment signals independently.
`CARE_ODOO_REQUEST_TIMEOUT` configures HTTP timeouts (default 120 seconds).

## Verification

```sh
python manage.py test care_odoo.tests --keepdb
ruff check src/care_odoo/resources/stock src/care_odoo/tests
```

Tests cover actual Care transactions, free units and discounts, owned/internal
transit quantities, roll-forward, saved exports, rebuilds and lost-response recovery.
Native Odoo costing, replacement publication and retirement of old stock are tested
in the companion add-on.

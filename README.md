# Care Odoo Backend

Care Odoo is a plugin for Care to integrate Odoo ERP specifications. By integrating Odoo with CARE, it creates a seamless, interconnected system where patient data, inventory management, and business processes are synchronized across healthcare facilities.

## Features

* **Patient Synchronization**: Seamlessly sync patient records between Care and Odoo, ensuring consistent data across both systems.
* **Inventory Management**: Real-time inventory tracking and management through Odoo's robust ERP capabilities.
* **Financial Integration**: Automated billing, invoicing, and payment processing through Odoo's financial modules.
* **Business Process Automation**: Streamlined workflows connecting healthcare operations with business management.
* **Interoperability**: Standardized protocols enable smooth data exchange between Care's healthcare system and Odoo's business management platform.

## Installation

https://care-be-docs.ohc.network/pluggable-apps/configuration.html

https://github.com/ohcnetwork/care/blob/develop/plug_config.py

To install Care Odoo, you can add the plugin config in care/plug_config.py as follows:

```python

odoo_plug = Plug(
    name="care_odoo",
        package_name="git+https://github.com/ohcnetwork/care_odoo_be.git",
        version="@main",
        configs={
            "CARE_ODOO_HOST": "your-odoo-host.com",
            "CARE_ODOO_PORT": "8069",
            "CARE_ODOO_PROTOCOL": "https",
            "CARE_ODOO_DATABASE": "odoo_database_name",
            "CARE_ODOO_USERNAME": "odoo_username",
            "CARE_ODOO_PASSWORD": "odoo_password",
        },
)
plugs = [odoo_plug]
```

## Configuration

The following configuration variables are available for Care Odoo:

* **CARE_ODOO_HOST**: The hostname or IP address of the Odoo instance.

* **CARE_ODOO_PORT**: The port number for the Odoo instance (default: 8069).

* **CARE_ODOO_PROTOCOL**: The protocol to use for connecting to Odoo (http or https).

* **CARE_ODOO_DATABASE**: The database name for the Odoo instance.

* **CARE_ODOO_USERNAME**: The username for Odoo authentication.

* **CARE_ODOO_PASSWORD**: The password for Odoo authentication.

* **CARE_ODOO_GRN_SEQUENCE_MAP**: Maps a delivery order's destination location (external ID) to the Odoo GRN
  sequence code used for its vendor bill. For a mapped location, Odoo assigns the GRN number when it creates the
  draft vendor bill, and Care saves it as the delivery order's name. Vendor bills for unmapped locations get their
  GRN number on posting, as before. Only the exact destination location matches, so a child location needs its own
  entry.

  As an environment variable, comma-separated `location_id=sequence_code` pairs:

  ```bash
  CARE_ODOO_GRN_SEQUENCE_MAP=3f2b1c9e-8a4d-4e7b-9c1a-2d5e6f7a8b9c=grn.lab.sequence,7d6e5f4a-3b2c-4d1e-8f9a-0b1c2d3e4f5a=grn.number.sequence
  ```

  Or as a dict in `configs`:

  ```python
  "CARE_ODOO_GRN_SEQUENCE_MAP": {
      "3f2b1c9e-8a4d-4e7b-9c1a-2d5e6f7a8b9c": "grn.lab.sequence",  # Lab store
      "7d6e5f4a-3b2c-4d1e-8f9a-0b1c2d3e4f5a": "grn.number.sequence",  # Pharmacy
  },
  ```


The plugin will try to find the configuration from the settings first and then from environment variables.

## Purchase deliveries and vendor bills

Completing a delivery order from a supplier (not an internal transfer or a medication return) creates a draft
vendor bill in Odoo. While that bill is still a draft:

* **Mark as Draft** lets Care add items. Completing the order again updates the same bill with the order's items,
  vendor bill number and vendor bill date. The bill keeps its GRN number.
* **Mark as Abandoned** or **Mark as Entered in Error** cancels the bill.

Once the bill is posted in Odoo, Care refuses both and names the bill in the error. To change the order after that,
reset the bill to draft in Odoo first.

## Stock valuation (FIFO)

Values a facility's stock at any date. It produces one row per batch, using Care's own receipts, transfers and dispenses. It adds no tables and no core fields.

- A Care `Product` is one batch. Every completed purchase receipt line is a cost layer for that batch, and the stock still held comes from the newest layers.
- The facility holds what sits in `store` or `none` locations that haven't been deleted, including stock moving between them. Stock leaves through dispenses and through transfers to `consumption`, `adjustment` or `supplier` locations.
- Unit cost per receipt line:
  1. The confirmed cost stored on the line at `extensions.valuation_cost`.
  2. The Care pack price, after free packs and percentage discount. Lines that store a line total instead are detected against the batch's `purchase_price`.
  3. The batch's `purchase_price`.

  Tax is excluded. Rows that fall back to an estimate are flagged.
- All dates are `created_date`, and statuses are read as they are now.

Load confirmed costs from posted vendor bills, then value a day (end of day, India time):

```bash
# Reads every posted vendor bill line that came from Care straight from Odoo
# (care_connector's GET /api/account/move/vendor-bill/lines) and stores its cost on the receipt line,
# per base unit: line subtotal excl. tax / supplied_item_quantity. Costs whose bill is no longer posted
# are taken off. Lines it can't match are skipped and listed, as are bills that received another
# quantity than Care sent. Safe to rerun: it rewrites only lines whose cost changed, without save
# hooks or resync. --dry-run shows what would change.
python manage.py sync_valuation_costs
python manage.py value_stock --facility <uuid> --as-of 2026-04-30 --output april.csv
```

Set `CARE_ODOO_VALUATION_COST_SYNC=true` to run the sync every night at 2 am (India time) through Celery beat.

Costs can also come from a CSV. Columns: care_line_id, price_subtotal (bill line total excl. tax), optional reference. Several rows for one Care line are added together, and any bad row means nothing is saved. This never takes a cost off.

```bash
python manage.py import_valuation_costs --file posted-bill-lines.csv --source odoo_bill
```

For superusers, `GET /api/care_odoo/stock-valuation/?facility=<uuid>&as_of=YYYY-MM-DD&output=csv` returns the same rows.

To see where the stock sits, add `group_by=location` to the API or `--by-location` to the command. Each batch row is then split across the locations holding it, valued at the batch's facility-level FIFO unit cost, so the split always adds up to the batch row. Stock moving between two held locations counts at its destination. Leave out `output` to get JSON. A Google Sheet can read from it directly.

## License

This project is licensed under the terms of the MIT license.

## Credits

This package was created with [Cookiecutter](https://github.com/cookiecutter/cookiecutter) and the [ohcnetwork/care-plugin-cookiecutter](https://github.com/ohcnetwork/care-plugin-cookiecutter) project template.

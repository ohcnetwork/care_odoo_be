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


The plugin will try to find the configuration from the settings first and then from environment variables.

## License

This project is licensed under the terms of the MIT license.

## Credits

This package was created with [Cookiecutter](https://github.com/cookiecutter/cookiecutter) and the [ohcnetwork/care-plugin-cookiecutter](https://github.com/ohcnetwork/care-plugin-cookiecutter) project template.

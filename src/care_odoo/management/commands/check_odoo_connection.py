"""
Management command to verify Odoo connection and configuration.

Usage:
    python manage.py check_odoo_connection
"""

from django.core.management.base import BaseCommand

from care_odoo.connector.connector import OdooConnector
from care_odoo.settings import plugin_settings


class Command(BaseCommand):
    """
    Verify Odoo connection and display configuration status.
    """

    help = "Verify Odoo connection and configuration"

    def handle(self, *args, **options):
        self.stdout.write("Checking Odoo configuration and connection...\n")

        # Display current configuration (hiding sensitive data)
        self.stdout.write("Configuration:")
        self.stdout.write("-" * 50)
        self.stdout.write(f"  Host:     {plugin_settings.CARE_ODOO_HOST}")
        self.stdout.write(f"  Port:     {plugin_settings.CARE_ODOO_PORT or 'Not set'}")
        self.stdout.write(f"  Protocol: {plugin_settings.CARE_ODOO_PROTOCOL or 'Not set'}")
        self.stdout.write(f"  Database: {plugin_settings.CARE_ODOO_DATABASE}")
        self.stdout.write(f"  Username: {plugin_settings.CARE_ODOO_USERNAME}")
        self.stdout.write(f"  Password: {'*' * 8 if plugin_settings.CARE_ODOO_PASSWORD else 'Not set'}")
        self.stdout.write("-" * 50)

        # Check required settings
        missing_settings = []
        if not plugin_settings.CARE_ODOO_HOST:
            missing_settings.append("CARE_ODOO_HOST")
        if not plugin_settings.CARE_ODOO_DATABASE:
            missing_settings.append("CARE_ODOO_DATABASE")
        if not plugin_settings.CARE_ODOO_USERNAME:
            missing_settings.append("CARE_ODOO_USERNAME")
        if not plugin_settings.CARE_ODOO_PASSWORD:
            missing_settings.append("CARE_ODOO_PASSWORD")

        if missing_settings:
            self.stdout.write(
                self.style.ERROR(f"\n✗ Missing required settings: {', '.join(missing_settings)}")
            )
            return

        self.stdout.write(self.style.SUCCESS("\n✓ All required settings are configured"))

        # Build the URL
        url = f"{plugin_settings.CARE_ODOO_PROTOCOL}://{plugin_settings.CARE_ODOO_HOST}"
        if plugin_settings.CARE_ODOO_PORT:
            url += f":{plugin_settings.CARE_ODOO_PORT}"
        self.stdout.write(f"\nOdoo URL: {url}")

        # Test connection with a simple health check
        self.stdout.write("\nTesting connection...")
        try:
            # Try a simple API call - this endpoint may need to be adjusted
            # based on your Odoo addon's available endpoints
            response = OdooConnector.call_api("api/health", {}, method="GET")
            self.stdout.write(
                self.style.SUCCESS(f"\n✓ Connection successful! Response: {response}")
            )
        except Exception as e:
            error_msg = str(e)
            self.stdout.write(self.style.WARNING(f"\n⚠ Connection test result: {error_msg}"))
            self.stdout.write(
                "\nNote: The health endpoint may not be available. "
                "This doesn't necessarily mean the connection is broken. "
                "Try running a sync command to verify full functionality."
            )

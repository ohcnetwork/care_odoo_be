"""
Management command to sync Care data to Odoo.

This is a comprehensive sync command that can sync multiple resource types.

Usage examples:
    # Sync all users
    python manage.py sync_to_odoo users

    # Sync specific users by username
    python manage.py sync_to_odoo users --filter username=john

    # Sync all products (ChargeItemDefinitions)
    python manage.py sync_to_odoo products

    # Sync all categories (ResourceCategories)
    python manage.py sync_to_odoo categories

    # Sync all suppliers (Organizations with org_type=product_supplier)
    python manage.py sync_to_odoo suppliers

    # Dry run to see what would be synced
    python manage.py sync_to_odoo users --dry-run

    # Sync with progress bar
    python manage.py sync_to_odoo users --progress

    # Continue on errors
    python manage.py sync_to_odoo users --continue-on-error
"""

import logging
import time

from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)


def extract_error_message(exception):
    """Extract a clean error message from various exception types."""
    if isinstance(exception, ValidationError):
        # Handle DRF ValidationError which stores errors as ErrorDetail objects
        detail = exception.detail
        if isinstance(detail, list):
            return "; ".join(
                str(item) if not hasattr(item, "string") else item
                for item in detail
            )
        elif isinstance(detail, dict):
            messages = []
            for key, value in detail.items():
                if isinstance(value, list):
                    messages.append(f"{key}: {', '.join(str(v) for v in value)}")
                else:
                    messages.append(f"{key}: {value}")
            return "; ".join(messages)
        return str(detail)
    return str(exception)


class Command(BaseCommand):
    """
    Sync Care data to Odoo ERP.

    This command supports syncing multiple resource types including users,
    products, categories, and suppliers.
    """

    help = "Sync Care data to Odoo ERP"

    # Available resource types and their sync handlers
    RESOURCE_TYPES = {
        "users": {
            "description": "Sync Care users to Odoo",
            "handler": "_sync_users",
        },
        "products": {
            "description": "Sync ChargeItemDefinitions as products to Odoo",
            "handler": "_sync_products",
        },
        "categories": {
            "description": "Sync ResourceCategories to Odoo",
            "handler": "_sync_categories",
        },
        "suppliers": {
            "description": "Sync supplier Organizations to Odoo",
            "handler": "_sync_suppliers",
        },
    }

    def add_arguments(self, parser):
        parser.add_argument(
            "resource_type",
            type=str,
            nargs="?",
            choices=list(self.RESOURCE_TYPES.keys()) + ["all", "list"],
            help="The type of resource to sync (or 'all' to sync everything, 'list' to show available types)",
        )
        parser.add_argument(
            "--filter",
            type=str,
            action="append",
            help="Filter queryset (format: field=value). Can be specified multiple times.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Show what would be synced without making changes",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=50,
            help="Number of records to process in each batch (default: 50)",
        )
        parser.add_argument(
            "--continue-on-error",
            action="store_true",
            default=False,
            help="Continue syncing even if individual records fail",
        )
        parser.add_argument(
            "--progress",
            action="store_true",
            default=False,
            help="Show detailed progress for each record",
        )
        parser.add_argument(
            "--include-deleted",
            action="store_true",
            default=False,
            help="Include soft-deleted records (where applicable)",
        )

    def handle(self, *args, **options):
        resource_type = options.get("resource_type")

        if not resource_type or resource_type == "list":
            self._list_resource_types()
            return

        dry_run = options.get("dry_run")
        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made\n")
            )

        if resource_type == "all":
            self._sync_all(options)
        else:
            handler_name = self.RESOURCE_TYPES[resource_type]["handler"]
            handler = getattr(self, handler_name)
            stats = handler(options)
            self._display_summary(resource_type, stats)

    def _list_resource_types(self):
        """List all available resource types."""
        self.stdout.write("\nAvailable resource types:\n")
        self.stdout.write("-" * 60)
        for name, info in self.RESOURCE_TYPES.items():
            self.stdout.write(f"  {name:15} - {info['description']}")
        self.stdout.write("-" * 60)
        self.stdout.write("\nUsage: python manage.py sync_to_odoo <resource_type>")
        self.stdout.write("       python manage.py sync_to_odoo all  (sync everything)")

    def _sync_all(self, options):
        """Sync all resource types."""
        self.stdout.write("Syncing all resource types to Odoo...\n")
        self.stdout.write("=" * 60)

        all_stats = {}
        for resource_type, info in self.RESOURCE_TYPES.items():
            self.stdout.write(f"\n>>> Syncing {resource_type}...")
            handler = getattr(self, info["handler"])
            try:
                stats = handler(options)
                all_stats[resource_type] = stats
                self._display_summary(resource_type, stats)
            except Exception as e:
                clean_error = extract_error_message(e)
                self.stdout.write(self.style.ERROR(f"Failed to sync {resource_type}: {clean_error}"))
                if not options.get("continue_on_error"):
                    raise

        self._display_overall_summary(all_stats)

    def _parse_filters(self, filter_args):
        """Parse filter arguments into a dictionary."""
        filters = {}
        if filter_args:
            for f in filter_args:
                if "=" in f:
                    key, value = f.split("=", 1)
                    filters[key] = value
        return filters

    def _sync_users(self, options):
        """Sync users to Odoo."""
        from care.users.models import User
        from care_odoo.resources.res_user.resource import OdooUserResource

        filters = self._parse_filters(options.get("filter"))
        include_deleted = options.get("include_deleted")
        dry_run = options.get("dry_run")
        batch_size = options.get("batch_size")
        continue_on_error = options.get("continue_on_error")
        show_progress = options.get("progress")

        # Build queryset
        if include_deleted:
            queryset = User.objects.get_entire_queryset()
        else:
            queryset = User.objects.all()

        if filters:
            queryset = queryset.filter(**filters)

        queryset = queryset.order_by("id")

        return self._process_queryset(
            queryset=queryset,
            resource_class=OdooUserResource,
            sync_method="sync_user_to_odoo_api",
            dry_run=dry_run,
            batch_size=batch_size,
            continue_on_error=continue_on_error,
            show_progress=show_progress,
            identifier_field="username",
        )

    def _sync_products(self, options):
        """Sync ChargeItemDefinitions as products to Odoo."""
        from care.emr.models.charge_item_definition import ChargeItemDefinition
        from care_odoo.resources.product_product.resource import OdooProductProductResource

        filters = self._parse_filters(options.get("filter"))
        dry_run = options.get("dry_run")
        batch_size = options.get("batch_size")
        continue_on_error = options.get("continue_on_error")
        show_progress = options.get("progress")

        queryset = ChargeItemDefinition.objects.filter(deleted=False)
        if filters:
            queryset = queryset.filter(**filters)
        queryset = queryset.order_by("id")

        return self._process_queryset(
            queryset=queryset,
            resource_class=OdooProductProductResource,
            sync_method="sync_product_to_odoo_api",
            dry_run=dry_run,
            batch_size=batch_size,
            continue_on_error=continue_on_error,
            show_progress=show_progress,
            identifier_field="code",
        )

    def _sync_categories(self, options):
        """Sync ResourceCategories to Odoo."""
        from care.emr.models.resource_category import ResourceCategory
        from care.emr.resources.resource_category.spec import ResourceCategoryResourceTypeOptions
        from care_odoo.resources.product_category.category import OdooCategoryResource

        filters = self._parse_filters(options.get("filter"))
        dry_run = options.get("dry_run")
        batch_size = options.get("batch_size")
        continue_on_error = options.get("continue_on_error")
        show_progress = options.get("progress")

        queryset = ResourceCategory.objects.filter(
            deleted=False,
            resource_type=ResourceCategoryResourceTypeOptions.charge_item_definition.value,
        )
        if filters:
            queryset = queryset.filter(**filters)
        queryset = queryset.order_by("id")

        return self._process_queryset(
            queryset=queryset,
            resource_class=OdooCategoryResource,
            sync_method="sync_category_to_odoo_api",
            dry_run=dry_run,
            batch_size=batch_size,
            continue_on_error=continue_on_error,
            show_progress=show_progress,
            identifier_field="name",
        )

    def _sync_suppliers(self, options):
        """Sync supplier Organizations to Odoo."""
        from care.emr.models.organization import Organization
        from care.emr.resources.organization.spec import OrganizationTypeChoices
        from care_odoo.resources.res_partner.resource import OdooPartnerResource

        filters = self._parse_filters(options.get("filter"))
        dry_run = options.get("dry_run")
        batch_size = options.get("batch_size")
        continue_on_error = options.get("continue_on_error")
        show_progress = options.get("progress")

        queryset = Organization.objects.filter(
            deleted=False,
            org_type=OrganizationTypeChoices.product_supplier.value,
        )
        if filters:
            queryset = queryset.filter(**filters)
        queryset = queryset.order_by("id")

        return self._process_queryset(
            queryset=queryset,
            resource_class=OdooPartnerResource,
            sync_method="sync_partner_to_odoo_api",
            dry_run=dry_run,
            batch_size=batch_size,
            continue_on_error=continue_on_error,
            show_progress=show_progress,
            identifier_field="name",
        )

    def _process_queryset(
        self,
        queryset,
        resource_class,
        sync_method,
        dry_run,
        batch_size,
        continue_on_error,
        show_progress,
        identifier_field,
    ):
        """Generic method to process a queryset and sync to Odoo."""
        stats = {
            "total": queryset.count(),
            "success": 0,
            "failed": 0,
            "errors": [],
        }

        if stats["total"] == 0:
            self.stdout.write(self.style.WARNING("No records found matching criteria"))
            return stats

        self.stdout.write(f"Found {stats['total']} record(s) to sync")

        if dry_run:
            self._show_dry_run_records(queryset, identifier_field)
            return stats

        resource = resource_class()
        sync_func = getattr(resource, sync_method)
        processed = 0
        start_time = time.time()

        # Get IDs and process in batches
        record_ids = list(queryset.values_list("id", flat=True))

        for i in range(0, len(record_ids), batch_size):
            batch_ids = record_ids[i : i + batch_size]
            batch = queryset.model.objects.filter(id__in=batch_ids)

            for record in batch:
                processed += 1
                identifier = getattr(record, identifier_field, str(record.id))

                try:
                    if show_progress:
                        self.stdout.write(
                            f"[{processed}/{stats['total']}] Syncing: {identifier}..."
                        )

                    sync_func(record)
                    stats["success"] += 1

                    if show_progress:
                        self.stdout.write(self.style.SUCCESS("  ✓ Synced"))

                except Exception as e:
                    stats["failed"] += 1
                    clean_error = extract_error_message(e)
                    error_msg = f"{identifier}: {clean_error}"
                    stats["errors"].append(error_msg)

                    if show_progress:
                        self.stdout.write(self.style.ERROR(f"  ✗ Failed: {clean_error}"))

                    logger.error(f"Failed to sync {identifier}: {clean_error}")

                    if not continue_on_error:
                        raise CommandError(
                            f"Sync failed for {identifier}: {clean_error}. "
                            "Use --continue-on-error to skip failures."
                        )

            # Progress update
            if not show_progress and processed % batch_size == 0:
                elapsed = time.time() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                self.stdout.write(
                    f"Progress: {processed}/{stats['total']} ({rate:.1f}/sec)"
                )

        stats["elapsed_time"] = time.time() - start_time
        return stats

    def _show_dry_run_records(self, queryset, identifier_field):
        """Show records that would be synced in dry run mode."""
        self.stdout.write("\nRecords that would be synced:")
        self.stdout.write("-" * 50)

        for record in queryset[:15]:
            identifier = getattr(record, identifier_field, str(record.id))
            self.stdout.write(f"  - {identifier}")

        remaining = queryset.count() - 15
        if remaining > 0:
            self.stdout.write(f"\n  ... and {remaining} more records")
        self.stdout.write("-" * 50)

    def _display_summary(self, resource_type, stats):
        """Display summary for a single resource type."""
        self.stdout.write(f"\n{resource_type.upper()} SYNC SUMMARY")
        self.stdout.write("-" * 40)
        self.stdout.write(f"Total:      {stats['total']}")
        self.stdout.write(self.style.SUCCESS(f"Success:    {stats['success']}"))
        if stats["failed"] > 0:
            self.stdout.write(self.style.ERROR(f"Failed:     {stats['failed']}"))
        if "elapsed_time" in stats:
            self.stdout.write(f"Time:       {stats['elapsed_time']:.2f}s")

        if stats["errors"]:
            self.stdout.write("\nErrors (first 5):")
            for error in stats["errors"][:5]:
                self.stdout.write(self.style.ERROR(f"  - {error}"))

    def _display_overall_summary(self, all_stats):
        """Display overall summary for all resource types."""
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write("OVERALL SYNC SUMMARY")
        self.stdout.write("=" * 60)

        total_success = sum(s.get("success", 0) for s in all_stats.values())
        total_failed = sum(s.get("failed", 0) for s in all_stats.values())
        total_records = sum(s.get("total", 0) for s in all_stats.values())

        self.stdout.write(f"Total records:  {total_records}")
        self.stdout.write(self.style.SUCCESS(f"Total success:  {total_success}"))
        if total_failed > 0:
            self.stdout.write(self.style.ERROR(f"Total failed:   {total_failed}"))

        self.stdout.write("=" * 60)

        if total_failed == 0:
            self.stdout.write(self.style.SUCCESS("\n✓ All syncs completed successfully!"))
        else:
            self.stdout.write(
                self.style.WARNING(f"\n⚠ Completed with {total_failed} failure(s)")
            )

import logging

from django.contrib.auth import get_user_model
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from care.emr.models.charge_item import ChargeItem
from care.emr.models.invoice import Invoice
from care.emr.resources.base import model_from_cache
from care.emr.resources.tag.config_spec import TagConfigReadSpec
from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.account_move.spec import (
    AccountMoveApiRequest,
    AccountMoveReturnApiRequest,
    BillType,
    InvoiceItem,
)
from care_odoo.resources.product_category.spec import CategoryData
from care_odoo.resources.product_product.spec import ProductData, TaxData
from care_odoo.resources.res_partner.spec import PartnerData, PartnerType
from care_odoo.resources.utils import (
    get_all_discounts,
    get_base_price_from_charge_item,
    get_purchase_price_from_charge_item,
    get_taxes_from_definition,
)
from care_odoo.settings import plugin_settings

logger = logging.getLogger(__name__)


class OdooInvoiceResource:
    def render_tags_ids(self, tags: list[str]) -> list[str]:
        rendered_tags_ids: list[str] = []
        for tag in tags or []:
            cached_tag = model_from_cache(TagConfigReadSpec, id=tag)
            if cached_tag:
                logger.info("Cached Tag: %s", cached_tag)
                rendered_tags_ids.append(str(cached_tag["id"]))
        return rendered_tags_ids

    def has_insurance_tag(self, account_tags: list[int], insurance_tag_external_id: str) -> bool:
        """
        Check if any tag in account_tags has an external_id matching insurance_tag_external_id.

        Args:
            account_tags: List of tag database IDs from the account
            insurance_tag_external_id: The external_id (UUID) of the insurance tag from settings

        Returns:
            True if account has the insurance tag, False otherwise
        """
        if not insurance_tag_external_id or not account_tags:
            return False

        for tag_id in account_tags:
            cached_tag = model_from_cache(TagConfigReadSpec, id=tag_id)
            if cached_tag and str(cached_tag.get("id")) == insurance_tag_external_id:
                return True
        return False

    def sync_invoice_to_odoo_api(self, invoice_id: str) -> int | None:
        """
        Synchronize a Django invoice to Odoo using the custom addon API.

        Args:
            invoice_id: External ID of the Django invoice

        Returns:
            Odoo invoice ID if successful, None otherwise
        """
        invoice = Invoice.objects.select_related("facility", "patient").get(external_id=invoice_id)

        # Prepare partner data
        partner_data = PartnerData(
            name=invoice.patient.name,
            x_care_id=str(invoice.patient.external_id),
            partner_type=PartnerType.person,
            phone=invoice.patient.phone_number,
            state="kerala",
            email="",
            agent=False,
            gender=invoice.patient.gender if invoice.patient.gender else None,
            birthdate=invoice.patient.date_of_birth.strftime("%d-%m-%Y") if invoice.patient.date_of_birth else None,
            street=invoice.patient.address if invoice.patient.address else None,
        )

        # Prepare invoice items
        invoice_items = []
        for charge_item in ChargeItem.objects.filter(paid_invoice=invoice).select_related("charge_item_definition"):
            if charge_item.charge_item_definition:
                base_price = get_base_price_from_charge_item(charge_item, raise_if_not_found=True)
                purchase_price = get_purchase_price_from_charge_item(charge_item)
                taxes = []
                for tax in get_taxes_from_definition(charge_item.charge_item_definition):
                    taxes.append(
                        TaxData(
                            tax_name=tax["code"]["display"],
                            tax_percentage=float(tax["factor"]),
                        )
                    )
                product_data = ProductData(
                    product_name=f"{charge_item.charge_item_definition.title}",
                    x_care_id=str(charge_item.charge_item_definition.external_id),
                    mrp=float(base_price),
                    cost=float(purchase_price),
                    category=CategoryData(
                        category_name=charge_item.charge_item_definition.category.title,
                        parent_x_care_id=str(charge_item.charge_item_definition.category.parent.external_id)
                        if charge_item.charge_item_definition.category.parent
                        else "",
                        x_care_id=str(charge_item.charge_item_definition.category.external_id),
                    ),
                    status=charge_item.charge_item_definition.status,
                    taxes=taxes,
                )

                # Get all discounts if available
                discounts = get_all_discounts(charge_item)

                item = InvoiceItem(
                    product_data=product_data,
                    quantity=str(charge_item.quantity),
                    free_qty=str(0),
                    sale_price=str(base_price),
                    x_care_id=str(charge_item.external_id),
                    discounts=discounts,
                )

                if charge_item.performer_actor:
                    requester = charge_item.performer_actor
                else:
                    requester = None

                if requester:
                    item.agent_id = str(requester.external_id)
                invoice_items.append(item)
        patient_official_identifier_id = plugin_settings.CARE_PATIENT_OFFICIAL_IDENTIFIER
        if patient_official_identifier_id:
            x_identifier = next(
                (
                    identifier["value"]
                    for identifier in invoice.patient.instance_identifiers
                    if identifier["config"] in patient_official_identifier_id
                ),
                None,
            )
        logger.info("Tags: %s", invoice.account.tags)
        logger.info("Invoice Account Tags: %s", invoice.account.tags)

        # Check if account has the insurance tag
        # Note: CARE_INSURANCE_TAG_ID is an external_id (UUID), account_tags contains database IDs
        insurance_tag_external_id = plugin_settings.CARE_INSURANCE_TAG_ID
        account_tags_list = invoice.account.tags or []
        has_insurance_tag_flag = self.has_insurance_tag(account_tags_list, insurance_tag_external_id)

        # Extract encounter - first try primary_encounter from account, then fall back to charge items
        encounter = None
        if invoice.account and getattr(invoice.account, "primary_encounter", None):
            encounter = invoice.account.primary_encounter
        else:
            # Fallback: get encounter from first charge item with same account that has an encounter
            charge_item_with_encounter = (
                ChargeItem.objects.filter(
                    account=invoice.account, encounter__isnull=False, encounter__encounter_class="imp"
                )
                .select_related("encounter", "encounter__current_location")
                .first()
            )
            encounter = charge_item_with_encounter.encounter if charge_item_with_encounter else None

        # Get room number from encounter's current location
        room_number = encounter.current_location.name if encounter and encounter.current_location else None

        # Extract insurance details only if account has insurance tag
        doctor = None
        admission_date = None
        discharge_date = None
        account_tags = None

        if has_insurance_tag_flag:
            account_tags = self.render_tags_ids(invoice.account.tags)
            logger.info("Account Tags: %s", account_tags)

            if not encounter:
                raise ValidationError("No encounter found for charge items with this account")

            # Get doctor name from encounter's care team (first member)
            care_team = encounter.care_team or []
            if care_team:
                first_member = care_team[0] if isinstance(care_team, list) else None
                if first_member and first_member.get("user_id"):
                    User = get_user_model()
                    try:
                        user = User.objects.get(id=first_member["user_id"])
                        doctor = user.full_name
                    except User.DoesNotExist:
                        pass
            # Get admission and discharge dates from encounter period
            period = encounter.period or {}
            if period.get("start"):
                start_date = period["start"]
                if isinstance(start_date, str):
                    start_date = parse_datetime(start_date)
                if start_date:
                    admission_date = start_date.strftime("%d-%m-%Y %H:%M:%S")
            if period.get("end"):
                end_date = period["end"]
                if isinstance(end_date, str):
                    end_date = parse_datetime(end_date)
                if end_date:
                    discharge_date = end_date.strftime("%d-%m-%Y %H:%M:%S")

        data = AccountMoveApiRequest(
            partner_data=partner_data,
            invoice_items=invoice_items,
            invoice_date=invoice.created_date.strftime("%d-%m-%Y"),
            x_care_id=str(invoice.external_id),
            bill_type=BillType.customer,
            due_date=invoice.created_date.strftime("%d-%m-%Y"),
            reason="",
            x_created_by=invoice.updated_by.full_name if invoice.updated_by else None,
            x_identifier=x_identifier,
            insurance_tag=account_tags,
            doctor=doctor,
            admission_date=admission_date,
            discharge_date=discharge_date,
            x_account=invoice.account.name if invoice.account else None,
            is_refund=getattr(invoice, "is_refund", False),
            room_number=room_number,
        ).model_dump()
        logger.info("Odoo Invoice Data: %s", data)

        response = OdooConnector.call_api("api/account/move", data)
        invoice_number = response.get("invoice", {}).get("name")
        invoice.number = invoice_number
        invoice.save(update_fields=["number"])
        return response["invoice"]["id"]

    def sync_invoice_return_to_odoo_api(self, invoice_id: str) -> int | None:
        """
        Synchronize a cancelled Django invoice to Odoo using the custom addon API.

        Args:
            invoice_id: External ID of the Django invoice

        Returns:
            Odoo invoice ID if successful, None otherwise
        """
        invoice = Invoice.objects.select_related("facility", "patient").get(external_id=invoice_id)

        data = AccountMoveReturnApiRequest(
            x_care_id=str(invoice.external_id),
            reason=invoice.status,
        ).model_dump()

        logger.info("Odoo Invoice Return Data: %s", data)
        response = OdooConnector.call_api("api/account/move/return", data)
        return response["invoice"]["id"]

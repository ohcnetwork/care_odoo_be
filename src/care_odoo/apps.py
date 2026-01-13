from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _
PLUGIN_NAME = "care_odoo"

class OdooConfig(AppConfig):
    name = PLUGIN_NAME
    verbose_name = _("Odoo")

    def ready(self):
        import care_odoo.signals  # noqa
        import care_odoo.extensions  # noqa

import sys

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

PLUGIN_NAME = "care_odoo"

BUILD_TIME_COMMANDS = {
    "collectstatic",
    "makemigrations",
    "migrate",
    "compilemessages",
    "makemessages",
    "spectacular",
    "test",
}


class OdooConfig(AppConfig):
    name = PLUGIN_NAME
    verbose_name = _("Odoo")

    def ready(self):
        import care_odoo.signals  # noqa
        import care_odoo.extensions  # noqa

        if len(sys.argv) > 1 and sys.argv[1] in BUILD_TIME_COMMANDS:
            return

        from care_odoo.settings import plugin_settings

        plugin_settings.validate()

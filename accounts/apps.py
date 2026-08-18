from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        # Registers the pre_delete guard that makes hard_delete_user() the only
        # way a User row can be removed. Importing for the side effect is the
        # point: without this the receiver is only connected if something
        # happens to import the module, and a guard that depends on import
        # order is not a guard.
        from . import deletion  # noqa: F401

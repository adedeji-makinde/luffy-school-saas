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

        # Same reasoning: a check that is only registered when something else
        # happens to import the module is not a check. These are `deploy=True`,
        # so registering them costs nothing until `manage.py check --deploy`.
        from . import checks  # noqa: F401

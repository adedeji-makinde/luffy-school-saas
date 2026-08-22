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

        # Put the throttle on the admin's login form. Done here rather than in
        # an `accounts/admin.py` picked up by `admin.autodiscover()`, on the
        # same reasoning as the two above: this is a security control, and one
        # that only takes effect if a module happens to be imported is not one.
        from django.contrib import admin

        from .forms import ThrottledAdminAuthenticationForm

        admin.site.login_form = ThrottledAdminAuthenticationForm

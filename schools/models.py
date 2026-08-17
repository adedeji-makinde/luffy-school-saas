from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


class School(TenantMixin):
    """One customer school. Lives in the public schema; owns a private schema.

    A School row is the thing every Membership points at, so it is also the
    unit of access control: being able to see a school's data means holding a
    live Membership here.
    """

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=60, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Schemas are created on save in real use; tests turn this off per instance.
    auto_create_schema = True
    auto_drop_schema = False

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Domain(DomainMixin):
    """Hostname a school is reached on, e.g. stmarys.luffy.school."""

    pass

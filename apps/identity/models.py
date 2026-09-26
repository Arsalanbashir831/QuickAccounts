from typing import ClassVar

from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models
from django.db.models.expressions import RawSQL

from apps.identity.managers import UserManager


class User(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, db_default=RawSQL("uuidv7()", []), editable=False)
    email = models.EmailField(max_length=254, unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)
    auth_revision = models.BigIntegerField(default=1)

    objects = UserManager()

    USERNAME_FIELD: ClassVar[str] = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        db_table = '"identity"."users"'
        managed = False

    def has_perm(self, perm: str, obj: object | None = None) -> bool:
        return self.is_active and self.is_superuser

    def has_module_perms(self, app_label: str) -> bool:
        return self.is_active and self.is_superuser

from django.db import migrations, models
from django.db.models.expressions import RawSQL

import apps.identity.managers


class Migration(migrations.Migration):
    initial = True
    dependencies = [("database", "0001_adopt_authoritative_schema")]
    operations = [
        migrations.CreateModel(
            name="User",
            fields=[
                ("password", models.CharField(max_length=128, verbose_name="password")),
                (
                    "last_login",
                    models.DateTimeField(blank=True, null=True, verbose_name="last login"),
                ),
                (
                    "id",
                    models.UUIDField(
                        db_default=RawSQL("uuidv7()", []),
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("first_name", models.CharField(blank=True, max_length=150)),
                ("last_name", models.CharField(blank=True, max_length=150)),
                ("is_active", models.BooleanField(default=True)),
                ("is_staff", models.BooleanField(default=False)),
                ("is_superuser", models.BooleanField(default=False)),
                ("date_joined", models.DateTimeField(auto_now_add=True)),
                ("auth_revision", models.BigIntegerField(default=1)),
            ],
            options={"db_table": '"identity"."users"', "managed": False},
            managers=[("objects", apps.identity.managers.UserManager())],
        )
    ]

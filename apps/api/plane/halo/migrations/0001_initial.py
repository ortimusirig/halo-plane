# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="PageTextBehind",
            fields=[
                ("page_id", models.UUIDField(primary_key=True, serialize=False)),
                ("since", models.DateTimeField()),
            ],
            options={"db_table": "halo_page_text_behind"},
        ),
    ]

# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Halo's own table (app label "halo"), separate from Plane's: no foreign key into Plane's schema."""

from django.db import models


class PageTextBehind(models.Model):
    """A page whose stored HTML lags its document: an editor's save was merged with an API edit it had not
    received. Written in the same transaction as that save; removed by the editor's next complete save."""

    page_id = models.UUIDField(primary_key=True)
    since = models.DateTimeField()

    class Meta:
        db_table = "halo_page_text_behind"


class PageBeingCopied(models.Model):
    """A page just made by Plane's duplicate, whose copy task (assets, then a save of the whole page) has not
    finished. API edits are refused and editors' loads wait until the task removes the row."""

    page_id = models.UUIDField(primary_key=True)
    since = models.DateTimeField()

    class Meta:
        db_table = "halo_page_being_copied"

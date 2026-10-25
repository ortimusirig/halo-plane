# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Halo's own table (app label "halo"), separate from Plane's: no foreign key into Plane's schema."""

from django.db import models


class PageTextBehind(models.Model):
    """A page whose stored HTML and document disagree (an editor's save merged with an API edit it had not
    received, or a save of only one of them). Written in the same transaction as that save; removed by the next
    complete save, which also brings Plane's mention index in line with the whole text. merged: the stored HTML
    is a merged save's (it lacks what that editor missed), so the index is not brought in line with it."""

    page_id = models.UUIDField(primary_key=True)
    since = models.DateTimeField()
    merged = models.BooleanField(default=False)

    class Meta:
        db_table = "halo_page_text_behind"


class PageBeingCopied(models.Model):
    """A page just made by Plane's duplicate whose copy task (tasks.finish_page_copy: its images copied and
    pointed at) has not run yet. API edits of it are refused until the task removes the row (or it expires)."""

    page_id = models.UUIDField(primary_key=True)
    since = models.DateTimeField()

    class Meta:
        db_table = "halo_page_being_copied"

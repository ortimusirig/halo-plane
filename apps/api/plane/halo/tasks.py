# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Background tasks of Halo's page patch (found by Celery's autodiscovery in installed apps)."""

from celery import shared_task
from django.db import transaction

from plane.bgtasks.copy_s3_object import copy_s3_objects_of_description_and_assets
from plane.bgtasks.page_version_task import track_page_version
from plane.db.models import Page

from .models import PageBeingCopied, PageTextBehind


@shared_task
def track_page_version_after_save(page_id, existing_instance, user_id):
    """Plane's version task for an editor's save, run under the page's row lock (so it sees a save and its
    mark together) and skipped while the stored HTML lags the document, so no version mixes the two."""
    with transaction.atomic():
        if Page.objects.select_for_update().filter(pk=page_id).first() is None:
            return
        if PageTextBehind.objects.filter(page_id=page_id).exists():
            return
        # Called directly, the task runs here, inside this transaction.
        track_page_version(page_id=page_id, existing_instance=existing_instance, user_id=user_id)


@shared_task
def finish_page_copy(page_id, project_id, slug, user_id):
    """Plane's copy task for a duplicated page (it copies the assets, then saves the whole page), then the
    "being copied" row goes, so API edits and editors' loads can no longer be overwritten by it."""
    try:
        # Called directly, the task runs here; it catches and logs its own errors.
        copy_s3_objects_of_description_and_assets(
            entity_name="PAGE", entity_identifier=page_id, project_id=project_id, slug=slug, user_id=user_id
        )
    finally:
        PageBeingCopied.objects.filter(page_id=page_id).delete()

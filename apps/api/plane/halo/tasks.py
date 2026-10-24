# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Background tasks of Halo's page patch (found by Celery's autodiscovery in installed apps)."""

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from plane.bgtasks.copy_s3_object import copy_assets, extract_asset_ids, replace_asset_ids
from plane.bgtasks.page_version_task import track_page_version
from plane.db.models import Page
from plane.utils.exception_logger import log_exception

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
def finish_page_copy(page_id, project_id, user_id):
    """Plane's copy task for a duplicated page (copy_s3_objects_of_description_and_assets), with Plane's own
    helpers but none of its whole-page saves: the page's images are copied, and its HTML is pointed at the copies
    in one update of that HTML alone, made only if nobody changed the text or built the page's document since it
    was read. So the task can never write back other fields (name, archive, lock, access) or overwrite an edit,
    however late it runs. The document is then built from the HTML on the first load, as for every new page
    (views.HaloPagesDescriptionViewSet), not by the task. The "being copied" mark goes at the end, also on
    errors."""
    try:
        page = Page.objects.select_related("workspace").filter(pk=page_id).first()
        if page is None:
            return
        html = page.description_html
        asset_ids = extract_asset_ids(html, "image-component")
        if not asset_ids:
            return
        copies = copy_assets(page, page_id, project_id, asset_ids, user_id)
        if copies:
            Page.objects.filter(pk=page_id, description_html=html, description_binary__isnull=True).update(
                description_html=replace_asset_ids(html, "image-component", copies), updated_at=timezone.now()
            )
    except Exception as error:  # as in Plane's task: logged, and the copy keeps showing the original's images
        log_exception(error)
    finally:
        PageBeingCopied.objects.filter(page_id=page_id).delete()

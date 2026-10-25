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

from . import live, ydoc
from .models import PageBeingCopied, PageTextBehind

IMAGE_NODES = ("imageComponent", "image")


def _json_with_image_sources(node, sources: dict):
    """ProseMirror JSON with each image node's attrs.src found in sources replaced (a copy of the input)."""
    if isinstance(node, list):
        return [_json_with_image_sources(item, sources) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: _json_with_image_sources(value, sources) for key, value in node.items()}
    attrs = node.get("attrs")
    if node.get("type") in IMAGE_NODES and isinstance(attrs, dict) and attrs.get("src") in sources:
        node["attrs"] = {**attrs, "src": sources[attrs["src"]]}
    return node


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
    helpers but none of its whole-page saves: the page's images are copied, and the copy's HTML, JSON and (when
    it has one) document are pointed at the copies in one update, made only if none of them changed since they
    were read. So the task can never write back other fields (name, archive, lock, access) or overwrite an edit,
    however late it runs. The document change is a lineage edit of the document it read; it is published to
    live, so an editor that has the copy open receives it (a save that missed it is merged). The "being copied"
    mark goes at the end, also on errors."""
    try:
        page = Page.objects.select_related("workspace").filter(pk=page_id).first()
        if page is None:
            return
        html, json_data = page.description_html, page.description_json
        document = bytes(page.description_binary) if page.description_binary else None
        asset_ids = extract_asset_ids(html, "image-component")
        if document:  # the copy took the source's document, which has every image (its HTML may lag)
            asset_ids = list(dict.fromkeys(asset_ids + ydoc.image_sources(document)))
        if not asset_ids:
            return
        copies = copy_assets(page, page_id, project_id, asset_ids, user_id)
        if not copies:
            return
        sources = {copy["old_asset_id"]: copy["new_asset_id"] for copy in copies}
        changes = {
            "description_html": replace_asset_ids(html, "image-component", copies),
            "description_json": _json_with_image_sources(json_data, sources),
            "updated_at": timezone.now(),
        }
        if document:
            changes["description_binary"] = ydoc.with_image_sources(document, sources)
        written = False
        with transaction.atomic():
            current = Page.objects.select_for_update().filter(pk=page_id).values(
                "description_html", "description_json", "description_binary").first()
            stored = current and current["description_binary"]
            if (current and current["description_html"] == html and current["description_json"] == json_data
                    and (bytes(stored) if stored else None) == document):
                Page.objects.filter(pk=page_id).update(**changes)
                written = True
        if written and document:
            try:  # editors that have the copy open; one that misses it is merged on its next save
                live.publish_update(page_id, changes["description_binary"])
            except Exception as error:
                log_exception(error)
    except Exception as error:  # as in Plane's task: logged, and the copy keeps showing the original's images
        log_exception(error)
    finally:
        PageBeingCopied.objects.filter(page_id=page_id).delete()

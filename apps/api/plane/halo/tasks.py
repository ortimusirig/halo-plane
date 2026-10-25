# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Background tasks of Halo's page patch (found by Celery's autodiscovery in installed apps)."""

import uuid

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from plane.bgtasks.copy_s3_object import copy_assets, extract_asset_ids, replace_asset_ids
from plane.bgtasks.page_transaction_task import page_transaction
from plane.bgtasks.page_version_task import track_page_version
from plane.db.models import Page, PageLog
from plane.utils.exception_logger import log_exception

from . import live, ydoc
from .models import PageBeingCopied, PageTextBehind

IMAGE_NODES = ("imageComponent", "image")
IMAGE_TAGS = ("image-component", "img")  # the same images in the HTML


def _asset_ids(sources) -> list[str]:
    """Uploaded images are named by their asset's UUID; any other source (an image from the web) stays as it is."""
    ids = []
    for source in sources:
        try:
            uuid.UUID(str(source))
        except ValueError:
            continue
        if source not in ids:
            ids.append(source)
    return ids


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


def _indexed_html(page_id) -> str:
    """The page's entries in Plane's mention index (PageLog), written as the HTML page_transaction reads."""
    parts = []
    for transaction_id, entity_name in PageLog.objects.filter(page_id=page_id).values_list("transaction",
                                                                                         "entity_name"):
        tag = "image-component" if entity_name == "image" else "mention-component"
        parts.append(f'<{tag} id="{transaction_id}"></{tag}>')
    return "".join(parts)


@shared_task
def add_to_index(page_id, html):
    """Plane's page_transaction in the form Plane's create uses (old None: the HTML's entries added, none deleted),
    for a save made while the page is merged-marked (views): its document keeps parts that HTML may lack, and a
    deleted entry could not come back (PageLog is unique per page and transaction; Plane's delete is a soft
    delete). Runs under the page's row lock and only while the page is still merged-marked: after the mark is
    cleared it does nothing, since the clearing save's index_page reconciles the whole text."""
    with transaction.atomic():
        page = Page.objects.select_for_update().filter(pk=page_id).only("id").first()
        if page is None or not PageTextBehind.objects.filter(page_id=page_id, merged=True).exists():
            return
        page_transaction(new_description_html=html, old_description_html=None, page_id=page_id)


@shared_task
def index_page(page_id):
    """Plane's mention index (PageLog) brought in line with the page's stored text, both read when the job runs,
    under the page's row lock. Queued only by the complete save that clears a merged mark (views): the saves
    while the page was merged-marked indexed add-only (add_to_index), so the removals they held back, and anything
    a late add_to_index added, are settled here. Every other save keeps Plane's own page_transaction. A page
    merged-marked again by the time the job runs is left alone; the save that clears that mark queues it again."""
    with transaction.atomic():
        page = Page.objects.select_for_update().filter(pk=page_id).only("description_html").first()
        if page is None or PageTextBehind.objects.filter(page_id=page_id, merged=True).exists():
            return
        # Called directly, Plane's task runs here: what is indexed and not in the text goes, anything new comes.
        page_transaction(new_description_html=page.description_html or "",
                         old_description_html=_indexed_html(page_id), page_id=page_id)


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
        sources = [source for tag in IMAGE_TAGS for source in extract_asset_ids(html, tag)]
        if document:  # the copy took the source's document, which has every image (its HTML may lag)
            sources += ydoc.image_sources(document)
        asset_ids = _asset_ids(sources)
        if not asset_ids:
            return
        copies = copy_assets(page, page_id, project_id, asset_ids, user_id)
        if not copies:
            return
        sources = {copy["old_asset_id"]: copy["new_asset_id"] for copy in copies}
        new_html = html
        for tag in IMAGE_TAGS:
            new_html = replace_asset_ids(new_html, tag, copies)
        changes = {
            "description_html": new_html,
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

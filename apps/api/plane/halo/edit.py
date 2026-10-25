# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A name or body edit through the API that people's open and cached editors can merge.

Every edit carries expected_updated_at, the page's updated_at when the caller read it. Refused while the page is
locked, archived, open in Halo, or saved since the caller read it. The new HTML is
sanitized like Plane's own description endpoint, converted by live, and applied as a Yjs update on the page's
document (ydoc). The update is published on live's channel after commit, and again a few seconds later, so an
editor that opened the page in that moment still receives it.
"""

import copy
import threading
from datetime import timedelta

from bs4 import BeautifulSoup

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from plane.bgtasks.page_transaction_task import page_transaction
from plane.db.models import Page
from plane.utils.content_validator import validate_binary_data, validate_html_content
from plane.utils.exception_logger import log_exception

from . import lease, live, versions, ydoc
from .models import PageBeingCopied, PageTextBehind

REPUBLISH_AFTER_SECONDS = 3


class EditRefused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def payload(self) -> dict:
        return {"error": self.message, "code": self.code}


def sanitize(html: str) -> str:
    """Plane's sanitizer, and text without a tag made a paragraph (stored as the editor will show it)."""
    valid, error, clean = validate_html_content(html)
    if not valid:
        raise EditRefused(400, "invalid_html", error)
    return live.convertible(clean if clean is not None else html)


IMAGE_TAGS = ("image-component", "img")  # the document's imageComponent and image nodes, in the HTML
IMAGE_NODES = ("imageComponent", "image")


def _json_images(node):
    if isinstance(node, dict):
        if node.get("type") in IMAGE_NODES and isinstance(node.get("attrs"), dict):
            yield node
        for child in node.get("content") or []:
            yield from _json_images(child)


def with_document_image_ids(html: str, json_data, binary: bytes):
    """The HTML and JSON with each image's id taken from the document, the authoritative part: Plane's mention
    index keys an image by the id in the HTML, while the document keeps a retained image's own id whatever the
    edit's HTML said (the id is the editor's own bookkeeping, ignored in the comparison). Images are matched in
    order, and only when their sources agree; anything else is left as it is."""
    images = ydoc.image_ids(binary)
    soup = BeautifulSoup(html, "html.parser")
    tags = soup.find_all(list(IMAGE_TAGS))
    if len(tags) == len(images) and all(tag.get("src") == src for tag, (src, _) in zip(tags, images)):
        changed = False
        for tag, (_, image_id) in zip(tags, images):
            if image_id and tag.get("id") != image_id:
                tag["id"] = image_id
                changed = True
        if changed:
            html = str(soup)
    nodes = list(_json_images(json_data))
    if nodes and len(nodes) == len(images) and all(node["attrs"].get("src") == src for node, (src, _) in
                                                   zip(nodes, images)):
        json_data = copy.deepcopy(json_data)
        for node, (_, image_id) in zip(_json_images(json_data), images):
            if image_id:
                node["attrs"]["id"] = image_id
    return html, json_data


def _check(page: Page, expected_updated_at) -> None:
    if page.is_locked:
        raise EditRefused(400, "page_locked", "The page is locked in Halo; it must be unlocked there first.")
    if page.archived_at:
        raise EditRefused(400, "page_archived", "The page is archived; unarchive it before editing.")
    # Every edit names the version it was made on, so a save in Halo since then is never overwritten.
    if not expected_updated_at:
        raise EditRefused(400, "expected_updated_at_required",
                          "Give expected_updated_at: the page's updated_at from when it was read.")
    try:
        expected = parse_datetime(str(expected_updated_at))
    except ValueError:  # well formed but not a date, such as February 30
        expected = None
    if expected is None:
        raise EditRefused(400, "invalid_expected_updated_at", "expected_updated_at must be the page's updated_at.")
    if expected != page.updated_at:
        raise EditRefused(409, "page_changed", "The page changed since it was read; read it again first.")


TOO_LARGE = (
    413,
    "page_too_large_for_editor",
    "This page is too large for Halo's editor service to convert (100 kB of HTML); edit it in Halo.",
)


def _convert(html: str):
    try:
        return live.convert_html(html)
    except live.LiveTooLarge:
        raise EditRefused(*TOO_LARGE)


def first_document(html: str, name: str, timeout: float = live.CONVERT_SECONDS) -> tuple[bytes, dict]:
    """The first document of a page nobody can be loading yet (one the API is creating): live's conversion of
    its HTML, with the page's name as title, like Plane's own first document (getBinaryDataFromDocumentEditorHTML
    String). Raises live.LiveTooLarge or live.LiveUnavailable. Halo never builds a first document for an
    existing page: live does that when the page is first opened, as in Plane."""
    binary, json_data = live.convert_html(html or "<p></p>", timeout=timeout)
    return ydoc.with_title(binary, name or ""), json_data


PAGE_OPEN = (409, "page_open", "Someone has this page open in Halo. Ask them to close it, then try again.")

NOT_OPENED = (
    409,
    "page_not_ready",
    "This page has not been opened in Halo since it was made, so it has no editor document yet. Open it once in "
    "Halo; then its text can be edited here.",
)


def _has_document(page_pk) -> bool:
    return bool(Page.objects.filter(pk=page_pk).values_list("description_binary", flat=True).first())


def _publish(page_id, update: bytes):
    try:
        return live.publish_update(page_id, update)
    except Exception as error:  # the edit is saved; only the push to open editors failed
        log_exception(error)
        return None


def _joined_json(old: dict, added: dict) -> dict:
    """ProseMirror JSON of the old document followed by the added blocks; empty when the old JSON is unknown
    (some stored pages and versions have none), since the added blocks alone would claim to be the whole page.
    Plane's version viewer then shows the HTML."""
    if "content" not in (old or {}):
        return {}
    return {**old, "content": list(old["content"] or []) + list(added.get("content") or [])}


def edit_page(
    page: Page,
    user,
    *,
    expected_updated_at,
    html=None,
    name=None,
    append=False,
    allow_removal=False,
    restore_from=None,
) -> dict:
    """restore_from: a PageVersion of this page whose text replaces the current text (an undo)."""
    _check(page, expected_updated_at)
    try:
        with lease.edit_lease(page.id):
            return _edit_leased(page, user, expected_updated_at, html, name, append, allow_removal, restore_from)
    except lease.PageBusy:
        raise EditRefused(409, "edit_in_progress", "Another edit of this page is in progress; try again shortly.")


def text_behind(page_id) -> bool:
    """Whether the stored HTML lags the document: marked by a merged editor save, cleared by that editor's next
    complete save (models.PageTextBehind, written in the same transaction as those saves)."""
    return PageTextBehind.objects.filter(page_id=page_id).exists()


# A copy task that has not finished by then has failed or is stuck; its late write is safe (tasks.finish_page_copy).
COPY_STALE_AFTER = timedelta(minutes=2)


def being_copied(page_id) -> bool:
    """Whether a duplicated page's copy task has still to run (models.PageBeingCopied)."""
    return PageBeingCopied.objects.filter(page_id=page_id, since__gt=timezone.now() - COPY_STALE_AFTER).exists()


BEING_COPIED = (
    409,
    "page_not_ready",
    "Halo is still finishing this page, a copy of another page. Try again in a moment.",
)


TEXT_BEHIND = (
    409,
    "page_text_behind",
    "Halo is still taking in the latest text of this page from someone's editor. Ask them to make any small "
    "change in the page (or wait until they do), then read the page again.",
)


def _check_size(html: str, binary) -> None:
    """Plane's own limits for a saved page (content_validator), on the whole page after the edit."""
    valid, error, _ = validate_html_content(html)
    if valid and binary:
        valid, error = validate_binary_data(bytes(binary))
    if not valid:
        raise EditRefused(400, "page_too_large", f"The page would be too large after this edit: {error}")


def _edit_leased(page, user, expected_updated_at, html, name, append, allow_removal, restore_from) -> dict:
    if text_behind(page.id):
        raise EditRefused(*TEXT_BEHIND)
    if being_copied(page.id):
        raise EditRefused(*BEING_COPIED)
    if not _has_document(page.id):
        raise EditRefused(*NOT_OPENED)
    if live.is_open(page.id):
        raise EditRefused(*PAGE_OPEN)
    clean = new_body = new_json = None
    if restore_from is not None:
        # The version's own document, not its HTML: nothing in it passes through the sanitizer or converter.
        clean = restore_from.description_html or "<p></p>"
        if restore_from.description_binary:
            new_body = bytes(restore_from.description_binary)
            new_json = restore_from.description_json or {}
        else:
            # Older versions keep HTML only: the document and its JSON come from converting it, together.
            new_body, converted_json = _convert(clean)
            new_json = restore_from.description_json or converted_json
        html, append, allow_removal = clean, False, True  # an undo may remove what came after the version
    elif html is not None:
        clean = sanitize(html) or "<p></p>"
        new_body, new_json = _convert(clean)

    with transaction.atomic():
        page = Page.objects.select_for_update().get(pk=page.pk)
        _check(page, expected_updated_at)
        if text_behind(page.id):
            raise EditRefused(*TEXT_BEHIND)
        if being_copied(page.id):
            raise EditRefused(*BEING_COPIED)
        if not page.description_binary:  # never edit a page without its document (live builds it on first open)
            raise EditRefused(*NOT_OPENED)
        # Again, right before the edit: someone may have opened the page while its HTML was being converted.
        if live.is_open(page.id):
            raise EditRefused(*PAGE_OPEN)
        old = {
            "description_html": page.description_html,
            "description_binary": page.description_binary,
            "description_json": page.description_json,
        }
        if html is not None and append:
            full_html, full_json = old["description_html"] + clean, _joined_json(old["description_json"], new_json)
        else:
            full_html, full_json = clean, new_json
        current = bytes(page.description_binary)
        document = ydoc.edit_document(current, new_body, name, append=append)
        update = document.update
        body_changed = new_body is not None and ydoc.body_signature(document.state) != ydoc.body_signature(current)
        if body_changed and not append and not allow_removal:
            lost = ydoc.removed_parts(current, document.state)
            if lost:
                raise EditRefused(
                    400, "would_remove_parts",
                    "This edit would remove " + "; ".join(lost) + ". Keep them as they are, or, if the person "
                    "agreed to remove them, send allow_removal.",
                )
        name_changed = name is not None and name != page.name
        if not (body_changed or name_changed):
            return {"changed": False, "before_version_id": None, "after_version_id": None, "open_editors_updated": 0}
        if body_changed:
            full_html, full_json = with_document_image_ids(full_html, full_json, document.state)
            page.description_html, page.description_json = full_html, full_json
        if document.changed:
            page.description_binary = document.state
        if name_changed:
            page.name = name
        if body_changed:
            _check_size(page.description_html, page.description_binary)
        page.save()
        before = after = None
        if body_changed:
            before, after = versions.record_edit(page, user.id, old)

    reached = None
    if update:
        reached = _publish(page.id, update)
        timer = threading.Timer(REPUBLISH_AFTER_SECONDS, _publish, args=(page.id, update))
        timer.daemon = True
        timer.start()
    if body_changed:
        try:
            page_transaction.delay(
                new_description_html=full_html, old_description_html=old["description_html"], page_id=page.id
            )
        except Exception as error:  # the mentions index only; the edit itself is saved
            log_exception(error)
    return {
        "changed": True,
        "before_version_id": str(before.id) if before else None,
        "after_version_id": str(after.id) if after else None,
        "open_editors_updated": reached,
    }

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

import threading

from django.db import transaction
from django.utils.dateparse import parse_datetime

from plane.bgtasks.page_transaction_task import page_transaction
from plane.db.models import Page
from plane.utils.content_validator import validate_html_content
from plane.utils.exception_logger import log_exception

from . import lease, live, versions, ydoc

REPUBLISH_AFTER_SECONDS = 3


class EditRefused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def payload(self) -> dict:
        return {"error": self.message, "code": self.code}


def sanitize(html: str) -> str:
    valid, error, clean = validate_html_content(html)
    if not valid:
        raise EditRefused(400, "invalid_html", error)
    return clean if clean is not None else html


def _check(page: Page, expected_updated_at) -> None:
    if page.is_locked:
        raise EditRefused(400, "page_locked", "The page is locked in Halo; it must be unlocked there first.")
    if page.archived_at:
        raise EditRefused(400, "page_archived", "The page is archived; unarchive it before editing.")
    # Every edit names the version it was made on, so a save in Halo since then is never overwritten.
    if not expected_updated_at:
        raise EditRefused(400, "expected_updated_at_required",
                          "Give expected_updated_at: the page's updated_at from when it was read.")
    expected = parse_datetime(str(expected_updated_at))
    if expected is None:
        raise EditRefused(400, "invalid_expected_updated_at", "expected_updated_at must be the page's updated_at.")
    if expected != page.updated_at:
        raise EditRefused(409, "page_changed", "The page changed since it was read; read it again first.")


def _publish(page_id, update: bytes):
    try:
        return live.publish_update(page_id, update)
    except Exception as error:  # the edit is saved; only the push to open editors failed
        log_exception(error)
        return None


def _joined_json(old: dict, added: dict) -> dict:
    """ProseMirror JSON of the old document followed by the added blocks."""
    return {**(old or {"type": "doc"}), "content": list((old or {}).get("content") or []) + list(added.get("content") or [])}


def edit_page(
    page: Page, user, *, expected_updated_at, html=None, name=None, append=False, allow_removal=False
) -> dict:
    _check(page, expected_updated_at)
    try:
        with lease.edit_lease(page.id):
            return _edit_leased(page, user, expected_updated_at, html, name, append, allow_removal)
    except lease.PageBusy:
        raise EditRefused(409, "edit_in_progress", "Another edit of this page is in progress; try again shortly.")


def _edit_leased(page, user, expected_updated_at, html, name, append, allow_removal) -> dict:
    if live.is_open(page.id):
        raise EditRefused(409, "page_open", "Someone has this page open in Halo. Ask them to close it, then try again.")
    clean = new_body = new_json = None
    if html is not None:
        clean = sanitize(html) or "<p></p>"
        new_body, new_json = live.convert_html(clean)

    with transaction.atomic():
        page = Page.objects.select_for_update().get(pk=page.pk)
        _check(page, expected_updated_at)
        old = {
            "description_html": page.description_html,
            "description_binary": page.description_binary,
            "description_json": page.description_json,
        }
        update = b""
        if html is not None and append:
            full_html, full_json = old["description_html"] + clean, _joined_json(old["description_json"], new_json)
        else:
            full_html, full_json = clean, new_json
        if page.description_binary:
            current = bytes(page.description_binary)
            document = ydoc.edit_document(current, new_body, name, append=append)
            update = document.update
            body_changed = new_body is not None and ydoc.body_signature(document.state) != ydoc.body_signature(current)
            before_body, after_body = current, document.state
        else:
            # Nobody has opened the page yet, so there is no document: live builds it from the HTML on first open.
            body_changed = html is not None and full_html != old["description_html"]
            before_body = live.convert_html(old["description_html"])[0] if body_changed and not append else None
            after_body = new_body
        if body_changed and not append and not allow_removal:
            lost = ydoc.removed_parts(before_body, after_body)
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
            page.description_html, page.description_json = full_html, full_json
            if page.description_binary:
                page.description_binary = document.state
        if name_changed:
            page.name = name
            if page.description_binary and document.changed:
                page.description_binary = document.state
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

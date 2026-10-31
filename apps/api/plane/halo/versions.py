# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Versions for one API edit, in Plane's own version history (page_versions).

Plane merges a person's saves within ten minutes into their newest version (bgtasks/page_version_task.py). Both
versions of an API edit are marked (sub_pages_data, unused by Plane CE), and Halo's version task never merges a
save into a marked version, so the text before and after each API edit stays restorable from Halo.
"""

from datetime import timedelta

from django.utils import timezone

from plane.db.models import PageVersion

MAX_VERSIONS = 20  # Plane's own limit (bgtasks/page_version_task.py)
MARK = {"halo_api_edit": True}


def _version(page, user_id, saved_at, html, binary, json_data):
    return PageVersion.objects.create(
        page_id=page.id,
        workspace_id=page.workspace_id,
        owned_by_id=user_id,
        last_saved_at=saved_at,
        description_html=html,
        description_binary=binary,
        description_json=json_data or {},
        sub_pages_data=dict(MARK),
    )


def record_edit(page, user_id, old: dict):
    """Write the before and after versions of an edit already saved on page. Returns before, after and the ids of
    the older versions removed to stay within Plane's limit of MAX_VERSIONS, so the caller can name them."""
    now = timezone.now()
    # Always a new, marked version: an existing unmarked one could still be rewritten by Plane's version task.
    before = _version(page, user_id, now, old["description_html"], old["description_binary"], old["description_json"])
    after = _version(
        page,
        user_id,
        now + timedelta(milliseconds=1),
        page.description_html,
        page.description_binary,
        page.description_json,
    )
    surplus = [
        version.id
        for version in PageVersion.objects.filter(page_id=page.id).order_by("-last_saved_at")[MAX_VERSIONS:]
        if version.id not in (before.id, after.id)
    ]
    # Plane's soft delete, done in the database alone: an instance delete also queues a task, and a broker failure
    # there would undo the whole edit.
    PageVersion.objects.filter(pk__in=surplus).delete()
    return before, after, [str(version_id) for version_id in surplus]

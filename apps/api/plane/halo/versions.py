# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Versions for one API edit, in Plane's own version history (page_versions).

Plane merges a person's saves within ten minutes into their newest version (bgtasks/page_version_task.py).
The "before" version of an edit is therefore written older than its "after" version, so later saves merge into
"after" and the content before the edit stays restorable from Halo's version panel.
"""

from datetime import timedelta

from django.utils import timezone

from plane.db.models import PageVersion

MAX_VERSIONS = 20  # Plane's own limit (bgtasks/page_version_task.py)


def _version(page, user_id, saved_at, html, binary, json_data):
    return PageVersion.objects.create(
        page_id=page.id,
        workspace_id=page.workspace_id,
        owned_by_id=user_id,
        last_saved_at=saved_at,
        description_html=html,
        description_binary=binary,
        description_json=json_data or {},
    )


def record_edit(page, user_id, old: dict):
    """Write the before and after versions of an edit already saved on page."""
    now = timezone.now()
    newest = PageVersion.objects.filter(page_id=page.id).order_by("-last_saved_at").first()
    if newest is not None and newest.description_html == old["description_html"]:
        before = newest
    else:
        before = _version(
            page, user_id, now, old["description_html"], old["description_binary"], old["description_json"]
        )
    after = _version(
        page,
        user_id,
        now + timedelta(milliseconds=1),
        page.description_html,
        page.description_binary,
        page.description_json,
    )
    surplus = PageVersion.objects.filter(page_id=page.id).order_by("-last_saved_at")[MAX_VERSIONS:]
    for version in surplus:
        if version.id not in (before.id, after.id):
            version.delete()
    return before, after

# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Plane CE v1.4.2 never wrote page versions: its task read page.description (AttributeError)."""

import json

import pytest

from plane.bgtasks.page_version_task import track_page_version
from plane.db.models import PageVersion


@pytest.mark.django_db
def test_a_save_in_halo_creates_a_version(world):
    page = world.public
    old = json.dumps({"description_html": page.description_html})
    page.description_html = "<p>Saved in the editor</p>"
    page.description_json = {"type": "doc", "content": []}
    page.save()
    track_page_version(page_id=page.id, existing_instance=old, user_id=world.owner.user.id)
    version = PageVersion.objects.get(page=page)
    assert version.description_html == "<p>Saved in the editor</p>"
    assert version.description_json == {"type": "doc", "content": []}


@pytest.mark.django_db
def test_saves_within_ten_minutes_merge_into_one_version(world):
    page = world.public
    for text in ("one", "two"):
        old = json.dumps({"description_html": page.description_html})
        page.description_html = f"<p>{text}</p>"
        page.save()
        track_page_version(page_id=page.id, existing_instance=old, user_id=world.owner.user.id)
    assert list(PageVersion.objects.filter(page=page).values_list("description_html", flat=True)) == ["<p>two</p>"]


@pytest.mark.django_db
def test_a_save_is_never_merged_into_an_api_edit_version(world):
    from django.utils import timezone

    page = world.public
    PageVersion.objects.create(
        page=page,
        workspace=page.workspace,
        owned_by=world.owner.user,
        last_saved_at=timezone.now(),
        description_html=page.description_html,
        sub_pages_data={"halo_api_edit": True},
    )
    old = json.dumps({"description_html": page.description_html})
    page.description_html = "<p>Typed after the API edit</p>"
    page.save()
    track_page_version(page_id=page.id, existing_instance=old, user_id=world.owner.user.id)
    texts = set(PageVersion.objects.filter(page=page).values_list("description_html", flat=True))
    assert texts == {"<p>Alpha</p><p>Beta</p><p>Gamma</p>", "<p>Typed after the API edit</p>"}

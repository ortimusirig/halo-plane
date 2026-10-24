# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import patch

import pytest
from crum import impersonate

from plane.db.models import Page, PageVersion
from plane.halo import edit, live, ydoc
from plane.halo.tests.conftest import doc_for
from plane.settings.redis import redis_instance

ORIGINAL = "<p>Alpha</p><p>Beta</p><p>Gamma</p>"


def _edit(world, **kwargs):
    # In a request Plane's middleware sets the current user (crum); here the test does.
    with impersonate(world.owner.user):
        return edit.edit_page(Page.objects.get(pk=world.public.pk), world.owner.user, **kwargs)


@pytest.mark.django_db
def test_body_edit_saves_document_html_and_versions(world, fake_live):
    result = _edit(world, html="<p>Alpha</p><p>Beta, by AURA</p><p>Gamma</p>")
    page = Page.objects.get(pk=world.public.pk)
    assert result["changed"]
    assert page.description_html == "<p>Alpha</p><p>Beta, by AURA</p><p>Gamma</p>"
    assert ydoc.body_signature(page.description_binary) == ydoc.body_signature(
        doc_for(["Alpha", "Beta, by AURA", "Gamma"])
    )
    assert PageVersion.objects.get(pk=result["before_version_id"]).description_html == ORIGINAL
    assert PageVersion.objects.get(pk=result["after_version_id"]).description_html == page.description_html
    assert page.updated_by_id == world.owner.user.id


@pytest.mark.django_db
def test_open_page_is_refused_and_unchanged(world, fake_live):
    pubsub = redis_instance().pubsub()
    pubsub.subscribe(live.channel(world.public.id))
    try:
        pubsub.get_message(timeout=2)
        with pytest.raises(edit.EditRefused) as refused:
            _edit(world, html="<p>Other</p>")
    finally:
        pubsub.close()
    assert (refused.value.status, refused.value.code) == (409, "page_open")
    assert Page.objects.get(pk=world.public.pk).description_html == ORIGINAL
    assert fake_live.calls == []


@pytest.mark.django_db
def test_stale_read_is_refused(world, fake_live):
    with pytest.raises(edit.EditRefused) as refused:
        _edit(world, html="<p>Other</p>", expected_updated_at="2020-01-01T00:00:00Z")
    assert (refused.value.status, refused.value.code) == (409, "page_changed")


@pytest.mark.django_db
def test_matching_read_is_accepted(world, fake_live):
    stamp = Page.objects.get(pk=world.public.pk).updated_at.isoformat()
    assert _edit(world, html="<p>Other</p>", expected_updated_at=stamp)["changed"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value,code", [("is_locked", True, "page_locked"), ("archived_at", "2026-10-24", "page_archived")]
)
def test_locked_or_archived_page_is_refused(world, fake_live, field, value, code):
    Page.objects.filter(pk=world.public.pk).update(**{field: value})
    with pytest.raises(edit.EditRefused) as refused:
        _edit(world, html="<p>Other</p>")
    assert refused.value.code == code


@pytest.mark.django_db
def test_live_down_changes_nothing(world, fake_live):
    fake_live.down = True
    with pytest.raises(live.LiveUnavailable):
        _edit(world, html="<p>Other</p>")
    assert PageVersion.objects.count() == 0
    assert Page.objects.get(pk=world.public.pk).description_html == ORIGINAL


@pytest.mark.django_db
def test_page_never_opened_keeps_an_empty_document(world, fake_live):
    page = world.make_page(world.owner, "Fresh", "<p>One</p>", binary=False)
    result = edit.edit_page(page, world.owner.user, html="<p>Two</p>")
    page.refresh_from_db()
    assert result["changed"]
    assert page.description_binary is None
    assert page.description_html == "<p>Two</p>"


@pytest.mark.django_db
def test_same_text_is_no_change_and_no_versions(world, fake_live):
    result = _edit(world, html=ORIGINAL)
    assert not result["changed"]
    assert PageVersion.objects.count() == 0


@pytest.mark.django_db
def test_script_is_sanitized_before_conversion(world, fake_live):
    _edit(world, html="<p>Alpha</p><script>alert(1)</script><p>Beta</p><p>Gamma 2</p>")
    assert "<script" not in fake_live.calls[0]
    assert "<script" not in Page.objects.get(pk=world.public.pk).description_html


@pytest.mark.django_db
def test_rename_only_changes_name_without_versions(world, fake_live):
    result = _edit(world, name="Plan v2")
    assert result["changed"]
    assert Page.objects.get(pk=world.public.pk).name == "Plan v2"
    assert PageVersion.objects.count() == 0
    assert fake_live.calls == []


@pytest.mark.django_db
def test_update_is_published_to_open_editors_after_commit(world, fake_live):
    published = []
    with patch("plane.halo.live.publish_update", side_effect=lambda page_id, update: published.append(
        (str(page_id), update)) or 0), patch("plane.halo.edit.REPUBLISH_AFTER_SECONDS", 0):
        _edit(world, html="<p>Alpha</p><p>B2</p><p>Gamma</p>")
        import time

        time.sleep(0.5)
    assert len(published) == 2  # right after commit, and once more for an editor that was just opening
    assert published[0] == published[1]
    assert published[0][0] == str(world.public.id) and published[0][1]

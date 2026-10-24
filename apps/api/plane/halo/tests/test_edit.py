# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import patch

import pytest
from crum import impersonate

from plane.db.models import Page, PageVersion
from plane.halo import edit, live, ydoc
from plane.halo.tests.conftest import doc_for, live_channel

ORIGINAL = "<p>Alpha</p><p>Beta</p><p>Gamma</p>"


def _edit(world, page=None, **kwargs):
    """Edit as the owner, by default on the page as it is now (the read the caller made)."""
    page = Page.objects.get(pk=(page or world.public).pk)
    kwargs.setdefault("expected_updated_at", page.updated_at.isoformat())
    # In a request Plane's middleware sets the current user (crum); here the test does.
    with impersonate(world.owner.user):
        return edit.edit_page(page, world.owner.user, **kwargs)


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
    with live_channel(world.public.id):
        with pytest.raises(edit.EditRefused) as refused:
            _edit(world, html="<p>Other</p>")
    assert (refused.value.status, refused.value.code) == (409, "page_open")
    assert Page.objects.get(pk=world.public.pk).description_html == ORIGINAL
    assert fake_live.calls == []


@pytest.mark.django_db
def test_stale_read_is_refused(world, fake_live):
    with pytest.raises(edit.EditRefused) as refused:
        _edit(world, html="<p>Other</p>", expected_updated_at="2020-01-01T00:00:00Z")
    assert (refused.value.status, refused.value.code) == (409, "page_changed")


@pytest.mark.django_db
def test_matching_read_in_another_timezone_is_accepted(world, fake_live):
    from datetime import timedelta, timezone

    stamp = Page.objects.get(pk=world.public.pk).updated_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert _edit(world, html="<p>Other</p>", expected_updated_at=stamp.isoformat())["changed"]


@pytest.mark.django_db
@pytest.mark.parametrize("stamp,code", [(None, "expected_updated_at_required"), ("", "expected_updated_at_required"),
                                        ("yesterday", "invalid_expected_updated_at")])
def test_edit_without_a_valid_read_stamp_is_refused(world, fake_live, stamp, code):
    with pytest.raises(edit.EditRefused) as refused:
        _edit(world, html="<p>Other</p>", expected_updated_at=stamp)
    assert (refused.value.status, refused.value.code) == (400, code)
    assert Page.objects.get(pk=world.public.pk).description_html == ORIGINAL


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
    result = _edit(world, page=page, html="<p>Two</p>")
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
def test_unsafe_links_frames_and_handlers_are_removed_but_page_parts_stay(world, fake_live):
    _edit(world, html=(
        '<p>Alpha</p><p><a href="javascript:alert(1)">bad</a> <a href="https://ok.test/">ok</a></p>'
        '<iframe src="https://evil.test/"></iframe><p onclick="alert(1)">Gamma</p>'
        '<image-component src="11111111-1111-4111-8111-111111111111"></image-component>'
        '<p><mention-component id="m1" entity_identifier="u1" entity_name="user_mention"></mention-component></p>'))
    html = Page.objects.get(pk=world.public.pk).description_html
    for unsafe in ("javascript:", "<iframe", "onclick"):
        assert unsafe not in html
    for kept in ('href="https://ok.test/"', "<image-component", "<mention-component"):
        assert kept in html


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


@pytest.mark.django_db
def test_edit_that_drops_or_swaps_an_image_is_refused_unless_allowed(world, fake_live):
    page = world.make_page(world.owner, "Pics", '<p>Intro</p><image-component src="a1"></image-component>')
    with pytest.raises(edit.EditRefused) as refused:
        _edit(world, page=page, html='<p>Intro</p><image-component src="b2"></image-component>')
    assert (refused.value.status, refused.value.code) == (400, "would_remove_parts")
    assert "image-component a1" in refused.value.message
    assert _edit(world, page=page, html="<p>Intro</p>", allow_removal=True)["changed"]


@pytest.mark.django_db
def test_append_keeps_every_block_and_adds_at_the_end(world, fake_live):
    result = _edit(world, html="<p>Delta</p>", append=True)
    page = Page.objects.get(pk=world.public.pk)
    assert result["changed"]
    assert ydoc.body_signature(page.description_binary) == ydoc.body_signature(
        doc_for(["Alpha", "Beta", "Gamma", "Delta"])
    )
    assert page.description_html == ORIGINAL + "<p>Delta</p>"


@pytest.mark.django_db
def test_a_second_edit_while_one_runs_is_refused(world, fake_live):
    from plane.halo import lease

    with lease.edit_lease(world.public.id):
        with pytest.raises(edit.EditRefused) as refused:
            _edit(world, html="<p>Other</p>")
    assert (refused.value.status, refused.value.code) == (409, "edit_in_progress")
    assert _edit(world, html="<p>Other</p>")["changed"]  # the lease is free again


@pytest.mark.django_db
def test_api_versions_are_marked(world, fake_live):
    result = _edit(world, html="<p>Other</p>")
    for version_id in (result["before_version_id"], result["after_version_id"]):
        assert PageVersion.objects.get(pk=version_id).sub_pages_data == {"halo_api_edit": True}

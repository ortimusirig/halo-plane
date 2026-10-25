# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import base64
import threading
import time
from unittest.mock import patch

import pytest
from pycrdt import Doc, XmlFragment

from plane.db.models import APIToken, Page, PageVersion, Project, ProjectMember, ProjectPage
from plane.halo import lease, ydoc
from plane.halo.tests.conftest import doc_for, live_channel

pytestmark = pytest.mark.django_db

ORIGINAL = "<p>Alpha</p><p>Beta</p><p>Gamma</p>"


@pytest.fixture(autouse=True)
def no_live_unless_faked(monkeypatch):
    """Tests without fake_live reach no live: connections to it are refused at once."""
    monkeypatch.setattr("plane.halo.live.LIVE_URL", "http://127.0.0.1:9/live/")


def ids(response):
    assert response.status_code == 200, response.content
    return {page["id"] for page in response.json()["results"]}


def test_list_shows_public_and_own_private_pages_only(world):
    assert ids(world.member.client.get(world.base)) == {str(world.public.id)}
    assert ids(world.admin.client.get(world.base)) == {str(world.public.id), str(world.private.id)}
    item = world.member.client.get(world.base).json()["results"][0]
    assert item["url"].endswith(f"/projects/{world.project.id}/pages/{world.public.id}")


def test_guest_without_full_view_sees_only_own_pages(world):
    Project.objects.filter(pk=world.project.pk).update(guest_view_all_features=False)
    assert ids(world.guest.client.get(world.base)) == set()


def test_non_member_is_refused(world):
    assert world.outsider.client.get(world.base).status_code == 403
    assert world.outsider.client.get(f"{world.base}{world.public.id}/").status_code == 403


def test_no_token_is_refused(world, api_client):
    assert api_client.get(world.base).status_code in (401, 403)


def test_retrieve_adds_text_and_url(world):
    response = world.member.client.get(f"{world.base}{world.public.id}/")
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["description_html"] == ORIGINAL
    assert data["description_text"] == "AlphaBetaGamma"
    assert data["url"] == (
        f"https://pm.applexus.net/{world.workspace.slug}/projects/{world.project.id}/pages/{world.public.id}"
    )
    assert data["work_item_ids"] == []
    assert data["updated_at"]


def test_private_page_of_someone_else_is_refused(world):
    url = f"{world.base}{world.private.id}/"
    assert world.member.client.get(url).status_code == 403
    assert world.member.client.patch(url, {"name": "x"}, format="json").status_code == 403


def test_create_takes_only_name_text_and_access(world):
    """No parent, lock or archive date from the caller: a parent could name a page of another project."""
    for field, value in (("parent", str(world.private.id)), ("is_locked", True), ("archived_at", "2026-10-24")):
        response = world.member.client.post(
            world.base, {"name": "x", "description_html": "<p>x</p>", field: value}, format="json"
        )
        assert (response.status_code, response.json()["code"]) == (400, "unsupported_field"), field
    assert not Page.objects.filter(name="x").exists()
    private = world.member.client.post(world.base, {"name": "Mine", "access": 1}, format="json")
    assert private.status_code == 201 and Page.objects.get(pk=private.json()["id"]).access == 1


def test_guest_cannot_create_or_edit(world):
    response = world.guest.client.post(world.base, {"name": "x", "description_html": "<p>x</p>"}, format="json")
    assert response.status_code == 403
    response = world.guest.client.patch(f"{world.base}{world.public.id}/", {"name": "x"}, format="json")
    assert response.status_code == 403


def test_edit_returns_versions_and_links(world, fake_live):
    url = f"{world.base}{world.public.id}/"
    stamp = world.member.client.get(url).json()["updated_at"]
    response = world.member.client.patch(
        url, {"description_html": "<p>Alpha</p><p>Beta by AURA</p><p>Gamma</p>", "expected_updated_at": stamp},
        format="json",
    )
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["changed"] is True
    assert data["description_html"] == "<p>Alpha</p><p>Beta by AURA</p><p>Gamma</p>"
    assert data["before_version_url"] == f"{data['url']}?paneTab=info&version={data['before_version_id']}"
    assert Page.objects.get(pk=world.public.pk).updated_by_id == world.member.user.id
    versions = world.member.client.get(f"{url}versions/").json()
    assert {v["id"] for v in versions} == {data["before_version_id"], data["after_version_id"]}
    before = world.member.client.get(f"{url}versions/{data['before_version_id']}/").json()
    assert before["description_html"] == ORIGINAL


def test_edit_refusals_have_codes(world, fake_live):
    url = f"{world.base}{world.public.id}/"
    stamp = world.member.client.get(url).json()["updated_at"]
    missing = world.member.client.patch(url, {"description_html": "<p>x</p>"}, format="json")
    assert (missing.status_code, missing.json()["code"]) == (400, "expected_updated_at_required")
    renamed = world.member.client.patch(url, {"name": "x"}, format="json")
    assert (renamed.status_code, renamed.json()["code"]) == (400, "expected_updated_at_required")
    with live_channel(world.public.id):
        response = world.member.client.patch(
            url, {"description_html": "<p>x</p>", "expected_updated_at": stamp}, format="json"
        )
    assert (response.status_code, response.json()["code"]) == (409, "page_open")
    stale = world.member.client.patch(
        url, {"description_html": "<p>x</p>", "expected_updated_at": "2020-01-01T00:00:00Z"}, format="json"
    )
    assert (stale.status_code, stale.json()["code"]) == (409, "page_changed")
    fake_live.down = True
    down = world.member.client.patch(url, {"description_html": "<p>x</p>", "expected_updated_at": stamp}, format="json")
    assert (down.status_code, down.json()["code"]) == (503, "live_unavailable")
    assert Page.objects.get(pk=world.public.pk).description_html == ORIGINAL


def test_other_fields_and_empty_patch_are_refused(world):
    url = f"{world.base}{world.public.id}/"
    response = world.member.client.patch(url, {"access": 1}, format="json")
    assert response.status_code == 400 and "access" in response.json()["error"]
    assert world.member.client.patch(url, {}, format="json").status_code == 400
    assert world.member.client.patch(url, {"name": 5}, format="json").status_code == 400


def test_archive_follows_plane_rule_owner_or_admin(world):
    url = f"{world.base}{world.public.id}/archive/"
    assert world.member.client.post(url).status_code == 400  # a member who does not own the page
    assert world.owner.client.post(url).status_code == 200
    assert Page.objects.get(pk=world.public.pk).archived_at is not None
    assert world.admin.client.delete(url).status_code == 204
    assert Page.objects.get(pk=world.public.pk).archived_at is None


def test_no_delete_route(world):
    assert world.admin.client.delete(f"{world.base}{world.public.id}/").status_code == 405


def test_create_removes_unsafe_links_frames_and_handlers(world):
    response = world.member.client.post(world.base, {"name": "x", "description_html": (
        '<p><a href="javascript:alert(1)">bad</a></p><iframe src="https://evil.test/"></iframe>'
        '<p onmouseover="alert(1)">hi</p><p><a href="https://ok.test/">ok</a></p>')}, format="json")
    html = Page.objects.get(pk=response.json()["id"]).description_html
    for unsafe in ("javascript:", "<iframe", "onmouseover"):
        assert unsafe not in html
    assert 'href="https://ok.test/"' in html


def test_ids_from_another_project_are_refused(world, fake_live):
    """A page, or a version of it, in project B is never reached through project A's routes."""
    other = Project.objects.create(name="Other", identifier="OT", workspace=world.workspace)
    ProjectMember.objects.create(project=other, workspace=world.workspace, member=world.member.user, role=15)
    elsewhere = Page.objects.create(workspace=world.workspace, name="B", owned_by=world.member.user,
                                    description_html="<p>B</p>")
    ProjectPage.objects.create(workspace=world.workspace, project=other, page=elsewhere)
    version = PageVersion.objects.create(page=elsewhere, workspace=world.workspace, owned_by=world.member.user,
                                         description_html="<p>B</p>")
    url = f"{world.base}{elsewhere.id}/"
    stamp = elsewhere.updated_at.isoformat()
    assert world.member.client.get(url).status_code == 403
    assert world.member.client.patch(url, {"name": "x", "expected_updated_at": stamp},
                                     format="json").status_code == 403
    assert world.member.client.post(f"{url}archive/").status_code == 403
    assert world.member.client.get(f"{url}versions/").status_code == 403
    # A version id of page B under page A's route.
    response = world.member.client.get(f"{world.base}{world.public.id}/versions/{version.id}/")
    assert response.status_code == 404
    elsewhere.refresh_from_db()
    assert (elsewhere.name, elsewhere.archived_at) == ("B", None)


def test_removed_member_and_revoked_token_lose_access(world):
    url = f"{world.base}{world.public.id}/"
    ProjectMember.objects.filter(project=world.project, member=world.member.user).update(is_active=False)
    assert world.member.client.get(url).status_code == 403
    assert world.member.client.get(world.base).status_code == 403
    APIToken.objects.filter(user=world.owner.user).update(is_active=False)
    assert world.owner.client.get(url).status_code in (401, 403)


def test_owner_can_edit_own_private_page_but_admin_cannot_read_others_private(world, fake_live):
    mine = world.make_page(world.member, "Mine", "<p>Private</p>", access=1)
    url = f"{world.base}{mine.id}/"
    stamp = world.member.client.get(url).json()["updated_at"]
    response = world.member.client.patch(url, {"description_html": "<p>Private, edited</p>",
                                               "expected_updated_at": stamp}, format="json")
    assert response.status_code == 200, response.content
    assert world.admin.client.get(url).status_code == 403
    assert world.admin.client.get(f"{url}versions/").status_code == 403


def test_guest_without_full_view_cannot_read_versions_of_others_pages(world, fake_live):
    url = f"{world.base}{world.public.id}/"
    stamp = world.member.client.get(url).json()["updated_at"]
    edited = world.member.client.patch(
        url, {"description_html": "<p>New</p>", "expected_updated_at": stamp}, format="json"
    ).json()
    assert world.guest.client.get(f"{url}versions/").status_code == 200  # guest_view_all_features on
    Project.objects.filter(pk=world.project.pk).update(guest_view_all_features=False)
    assert world.guest.client.get(f"{url}versions/").status_code == 403
    assert world.guest.client.get(f"{url}versions/{edited['before_version_id']}/").status_code == 403
    detail = world.member.client.get(f"{url}versions/{edited['before_version_id']}/").json()
    assert detail["description_text"] == "AlphaBetaGamma"


def test_archive_is_refused_while_the_page_is_open(world):
    url = f"{world.base}{world.public.id}/archive/"
    with live_channel(world.public.id):
        response = world.owner.client.post(url)
    assert (response.status_code, response.json()["code"]) == (409, "page_open")
    assert Page.objects.get(pk=world.public.pk).archived_at is None


def test_append_html_adds_at_the_end(world, fake_live):
    url = f"{world.base}{world.public.id}/"
    stamp = world.member.client.get(url).json()["updated_at"]
    both = world.member.client.patch(
        url, {"description_html": "<p>x</p>", "append_html": "<p>y</p>", "expected_updated_at": stamp},
        format="json",
    )
    assert both.status_code == 400
    response = world.member.client.patch(
        url, {"append_html": "<p>Delta</p>", "expected_updated_at": stamp}, format="json"
    )
    assert response.status_code == 200, response.content
    assert response.json()["description_html"] == ORIGINAL + "<p>Delta</p>"


def _description_url(world):
    return f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{world.public.id}/description/"


def test_editor_save_that_missed_an_api_edit_is_merged(world, fake_live, api_client,
                                                       django_capture_on_commit_callbacks):
    """The live editor saves the whole document; a save from before an API edit must not undo it."""
    stale = Doc()
    stale.apply_update(bytes(world.public.description_binary))
    text = stale.get(ydoc.BODY, type=XmlFragment).children[0].children[0]
    text.insert(len(text), " typed")  # typing made before the API edit, saved after it
    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    edited = world.owner.client.patch(
        url, {"description_html": "<p>Alpha</p><p>Beta by AURA</p><p>Gamma</p>", "expected_updated_at": stamp},
        format="json",
    )
    assert edited.status_code == 200
    api_client.force_authenticate(user=world.owner.user)  # the web app's and live's session sign-in
    published = []
    with patch("plane.halo.live.publish_update", side_effect=lambda page_id, update: published.append(update) or 1), \
            django_capture_on_commit_callbacks(execute=False) as callbacks:
        response = api_client.patch(
            _description_url(world),
            {
                "description_binary": base64.b64encode(stale.get_update()).decode(),
                "description_html": "<p>Alpha typed</p><p>Beta</p><p>Gamma</p>",
            },
            format="json",
        )
    assert response.status_code == 200, response.content
    # A merged save queues only add-only indexing (tasks.add_to_index): its HTML is older, so no version, and no
    # index entry is deleted (that would drop the API edit's mentions); the editor's next, complete save does both.
    assert len(callbacks) == 1
    saved = Doc()
    saved.apply_update(bytes(Page.objects.get(pk=world.public.pk).description_binary))
    assert str(saved.get(ydoc.BODY, type=XmlFragment)) == (
        "<paragraph>Alpha typed</paragraph><paragraph>Beta by AURA</paragraph><paragraph>Gamma</paragraph>"
    )
    assert published  # the editor that saved gets the part it missed
    stale.apply_update(published[0])
    assert str(stale.get(ydoc.BODY, type=XmlFragment)) == str(saved.get(ydoc.BODY, type=XmlFragment))
    # The stored HTML is that editor's older text until it saves again: reads say so, edits wait.
    assert world.owner.client.get(url).json()["text_complete"] is False
    stamp = world.owner.client.get(url).json()["updated_at"]
    waiting = world.owner.client.patch(url, {"description_html": "<p>x</p>", "expected_updated_at": stamp},
                                       format="json")
    assert (waiting.status_code, waiting.json()["code"]) == (409, "page_text_behind")
    # The editor's next save, made after it received the missing part, is complete and clears the mark.
    text.insert(len(text), "!")
    complete = api_client.patch(_description_url(world), {
        "description_binary": base64.b64encode(stale.get_update()).decode(),
        "description_html": "<p>Alpha typed!</p><p>Beta by AURA</p><p>Gamma</p>"}, format="json")
    assert complete.status_code == 200
    assert world.owner.client.get(url).json()["text_complete"] is True


def test_sub_page_edits_are_refused_before_anything_changes(world, fake_live):
    child = world.make_page(world.owner, "Child", "<p>c</p>")
    Page.objects.filter(pk=child.pk).update(parent=world.public)
    url = f"{world.base}{child.id}/"
    response = world.owner.client.patch(
        url, {"description_html": "<p>new</p>", "expected_updated_at": child.updated_at.isoformat()}, format="json"
    )
    assert (response.status_code, response.json()["code"]) == (400, "page_hierarchy")
    assert Page.objects.get(pk=child.pk).description_html == "<p>c</p>"
    assert not PageVersion.objects.filter(page=child).exists()


def test_a_save_without_html_does_not_clear_the_lagging_mark(world, api_client):
    from django.utils import timezone

    from plane.halo.models import PageTextBehind

    PageTextBehind.objects.create(page_id=world.public.id, since=timezone.now())
    api_client.force_authenticate(user=world.owner.user)
    binary_only = {"description_binary": base64.b64encode(bytes(world.public.description_binary)).decode()}
    assert api_client.patch(_description_url(world), binary_only, format="json").status_code == 200
    assert PageTextBehind.objects.filter(page_id=world.public.id).exists()
    complete = {**binary_only, "description_html": ORIGINAL}
    assert api_client.patch(_description_url(world), complete, format="json").status_code == 200
    assert not PageTextBehind.objects.filter(page_id=world.public.id).exists()


def test_version_task_waits_for_the_page_lock_and_skips_lagging_pages(world):
    import json as json_lib

    from django.utils import timezone

    from plane.halo.models import PageTextBehind
    from plane.halo.tasks import track_page_version_after_save

    page = world.public
    old = json_lib.dumps({"description_html": "<p>before</p>"})
    PageTextBehind.objects.create(page_id=page.id, since=timezone.now())
    track_page_version_after_save(page_id=page.id, existing_instance=old, user_id=world.owner.user.id)
    assert not PageVersion.objects.filter(page=page).exists()  # an earlier queued task cannot mix the two
    PageTextBehind.objects.filter(page_id=page.id).delete()
    track_page_version_after_save(page_id=page.id, existing_instance=old, user_id=world.owner.user.id)
    assert PageVersion.objects.get(page=page).description_html == ORIGINAL


def test_editor_save_starts_version_tasks_only_after_commit(world, api_client, no_celery,
                                                            django_capture_on_commit_callbacks):
    current = Doc()
    current.apply_update(bytes(world.public.description_binary))
    text = current.get(ydoc.BODY, type=XmlFragment).children[0].children[0]
    text.insert(len(text), " typed")
    api_client.force_authenticate(user=world.owner.user)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        response = api_client.patch(_description_url(world), {
            "description_binary": base64.b64encode(current.get_update()).decode(),
            "description_html": "<p>Alpha typed</p><p>Beta</p><p>Gamma</p>"}, format="json")
        assert response.status_code == 200
        no_celery.assert_not_called()  # nothing queued while the save is not committed
    assert len(callbacks) == 2  # page_transaction and track_page_version, once committed
    for callback in callbacks:
        callback()
    assert no_celery.call_count == 2


def test_editor_save_that_knows_everything_is_plane_unchanged(world, api_client):
    current = Doc()
    current.apply_update(bytes(world.public.description_binary))
    text = current.get(ydoc.BODY, type=XmlFragment).children[0].children[0]
    text.insert(len(text), " typed")
    api_client.force_authenticate(user=world.owner.user)
    with patch("plane.halo.live.publish_update") as publish:
        response = api_client.patch(
            _description_url(world),
            {"description_binary": base64.b64encode(current.get_update()).decode(),
             "description_html": "<p>Alpha typed</p><p>Beta</p><p>Gamma</p>"},
            format="json",
        )
    assert response.status_code == 200
    assert bytes(Page.objects.get(pk=world.public.pk).description_binary) == current.get_update()
    publish.assert_not_called()


def test_restore_uses_the_version_document_itself(world, fake_live):
    """An embedded work item would not survive Plane's HTML sanitizer; restoring from the document keeps it."""
    from pycrdt import XmlElement

    kept = Doc()
    kept.apply_update(bytes(world.public.description_binary))
    with kept.transaction():
        kept.get(ydoc.BODY, type=XmlFragment).children.append(
            XmlElement("issue-embed-component", {"entity_identifier": "w1"}))
    version = PageVersion.objects.create(page=world.public, workspace=world.workspace, owned_by=world.owner.user,
                                         description_html=ORIGINAL, description_binary=kept.get_update())
    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    response = world.owner.client.patch(url, {"restore_version_id": str(version.id), "expected_updated_at": stamp},
                                        format="json")
    assert response.status_code == 200, response.content
    restored = Page.objects.get(pk=world.public.pk).description_binary
    assert ydoc.protected_parts(restored) == ydoc.protected_parts(kept.get_update())
    assert fake_live.calls == []  # nothing went through HTML


def test_restore_refuses_a_version_of_another_page(world):
    other = world.make_page(world.owner, "Other", "<p>x</p>")
    version = PageVersion.objects.create(page=other, workspace=world.workspace, owned_by=world.owner.user,
                                         description_html="<p>x</p>")
    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    response = world.owner.client.patch(url, {"restore_version_id": str(version.id), "expected_updated_at": stamp},
                                        format="json")
    assert (response.status_code, response.json()["code"]) == (404, "unknown_version")


def test_archive_refuses_pages_with_sub_pages(world):
    child = world.make_page(world.owner, "Child", "<p>c</p>")
    Page.objects.filter(pk=child.pk).update(parent=world.public)
    for page in (world.public, child):
        url = f"{world.base}{page.id}/archive/"
        assert world.owner.client.post(url).json()["code"] == "page_hierarchy"
        assert world.admin.client.delete(url).json()["code"] == "page_hierarchy"
    assert not Page.objects.filter(pk__in=[world.public.pk, child.pk], archived_at__isnull=False).exists()


def test_edit_in_an_archived_project_is_refused_before_anything_changes(world, fake_live):
    from django.utils import timezone

    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    Project.objects.filter(pk=world.project.pk).update(archived_at=timezone.now())
    response = world.owner.client.patch(url, {"description_html": "<p>new</p>", "expected_updated_at": stamp},
                                        format="json")
    assert (response.status_code, response.json()["code"]) == (404, "page_not_found")
    assert Page.objects.get(pk=world.public.pk).description_html == ORIGINAL
    assert not PageVersion.objects.filter(page=world.public).exists()


def _app_page_url(world, suffix=""):
    return f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{world.public.id}/{suffix}"


def test_web_app_page_routes_are_halo_s_and_still_work(world, api_client):
    from django.urls import resolve

    from plane.halo.views import HaloAppPageViewSet

    for suffix in ("", "lock/", "access/"):
        assert resolve(_app_page_url(world, suffix)).func.cls is HaloAppPageViewSet
    api_client.force_authenticate(user=world.owner.user)
    assert api_client.get(_app_page_url(world)).status_code == 200
    assert api_client.post(_app_page_url(world, "lock/")).status_code == 204
    assert Page.objects.get(pk=world.public.pk).is_locked is True
    assert api_client.delete(_app_page_url(world, "lock/")).status_code == 204
    assert api_client.patch(_app_page_url(world), {"name": "Renamed in Halo"}, format="json").status_code == 200
    assert Page.objects.get(pk=world.public.pk).name == "Renamed in Halo"


@pytest.mark.django_db(transaction=True)
def test_a_lock_click_during_an_api_edit_does_not_write_the_old_text_back(world, api_client):
    """Plane's lock reads and saves the whole page; it must wait for a running edit, then read the new text."""
    from django.db import connection, transaction

    editing, done = threading.Event(), threading.Event()

    def api_edit():
        try:
            with transaction.atomic():
                page = Page.objects.select_for_update().get(pk=world.public.pk)
                page.description_html = "<p>Written by the API</p>"
                page.save()
                editing.set()
                time.sleep(0.8)  # the lock click arrives while this edit has not committed
        finally:
            connection.close()
            done.set()

    thread = threading.Thread(target=api_edit)
    thread.start()
    editing.wait(5)
    api_client.force_authenticate(user=world.owner.user)
    assert api_client.post(_app_page_url(world, "lock/")).status_code == 204
    thread.join()
    page = Page.objects.get(pk=world.public.pk)
    assert page.is_locked is True
    assert page.description_html == "<p>Written by the API</p>"


def test_a_save_without_html_marks_the_text_as_behind(world, api_client):
    """A save of the document alone leaves the stored HTML older than the document it saved."""
    from plane.halo.models import PageTextBehind

    api_client.force_authenticate(user=world.owner.user)
    current = Doc()
    current.apply_update(bytes(world.public.description_binary))
    text = current.get(ydoc.BODY, type=XmlFragment).children[0].children[0]
    text.insert(len(text), " typed")
    binary_only = {"description_binary": base64.b64encode(current.get_update()).decode()}
    assert api_client.patch(_description_url(world), binary_only, format="json").status_code == 200
    assert PageTextBehind.objects.filter(page_id=world.public.id).exists()
    assert world.owner.client.get(f"{world.base}{world.public.id}/").json()["text_complete"] is False


def test_create_in_an_archived_project_is_refused_and_creates_nothing(world):
    from django.utils import timezone

    pages = Page.objects.count()
    Project.objects.filter(pk=world.project.pk).update(archived_at=timezone.now())
    response = world.owner.client.post(world.base, {"name": "New", "description_html": "<p>x</p>"}, format="json")
    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")
    assert Page.objects.count() == pages


def test_create_commits_nothing_when_the_answer_cannot_be_built(world, no_celery):
    from plane.halo.views import HaloProjectPageViewSet

    pages = Page.objects.count()
    with patch.object(HaloProjectPageViewSet, "get_queryset", return_value=Page.objects.none()):
        response = world.owner.client.post(world.base, {"name": "New"}, format="json")
    assert response.status_code == 404
    assert Page.objects.count() == pages
    # No page task queued (Plane's API request log task may be).
    assert not any("new_description_html" in call.kwargs for call in no_celery.call_args_list)


def test_duplicate_is_halo_s_and_marks_the_copy_until_its_copy_task_ran(world, api_client, no_celery):
    from django.urls import resolve

    from plane.halo.models import PageBeingCopied
    from plane.halo.tasks import finish_page_copy
    from plane.halo.views import HaloPageDuplicateEndpoint

    assert resolve(_app_page_url(world, "duplicate/")).func.view_class is HaloPageDuplicateEndpoint
    api_client.force_authenticate(user=world.owner.user)
    response = api_client.post(_app_page_url(world, "duplicate/"))
    assert response.status_code == 201, response.content
    copy = Page.objects.get(pk=response.json()["id"])
    assert (copy.name, copy.description_html) == ("Plan (Copy)", ORIGINAL)
    # The copy takes the source's document, with its own name as title; nothing is converted.
    assert ydoc.body_signature(bytes(copy.description_binary)) == ydoc.body_signature(
        bytes(world.public.description_binary))
    copied = Doc()
    copied.apply_update(bytes(copy.description_binary))
    assert str(copied.get(ydoc.TITLE, type=XmlFragment)).endswith(">Plan (Copy)</heading>")
    assert ProjectPage.objects.filter(page=copy, project=world.project).exists()
    assert PageBeingCopied.objects.filter(page_id=copy.id).exists()
    queued = [call.kwargs for call in no_celery.call_args_list]
    # Plane's copy task is not queued: Halo's finish_page_copy does its work, then removes the mark.
    assert not any("entity_name" in kwargs for kwargs in queued)
    copy_kwargs = next(kwargs for kwargs in queued if set(kwargs) == {"page_id", "project_id", "user_id"})
    assert copy_kwargs == {"page_id": copy.id, "project_id": world.project.id, "user_id": world.owner.user.id}
    finish_page_copy(**copy_kwargs)
    assert not PageBeingCopied.objects.filter(page_id=copy.id).exists()
    assert Page.objects.get(pk=copy.pk).description_html == ORIGINAL  # no images, nothing to change


IMAGE_HTML = '<p>Alpha</p><image-component src="0a0a0a0a-0000-4000-8000-0000000000aa"></image-component>'
COPIED = [{"old_asset_id": "0a0a0a0a-0000-4000-8000-0000000000aa", "new_asset_id": "0b0b0b0b-0000-4000-8000-0000000000bb"}]


def _copy_in_progress(world):
    from django.utils import timezone

    from plane.halo.models import PageBeingCopied

    copy = world.make_page(world.owner, "Plan (Copy)", IMAGE_HTML, binary=False)
    PageBeingCopied.objects.create(page_id=copy.id, since=timezone.now())
    return copy


def _finish(world, copy):
    from plane.halo.tasks import finish_page_copy

    finish_page_copy(page_id=copy.id, project_id=world.project.id, user_id=world.owner.user.id)


def test_copy_task_points_only_the_copy_s_html_at_the_copied_images(world):
    from plane.halo.models import PageBeingCopied

    copy = _copy_in_progress(world)
    before = Page.objects.get(pk=copy.pk)
    with patch("plane.halo.tasks.copy_assets", return_value=COPIED) as copy_assets:
        _finish(world, copy)
    assert copy_assets.call_args.args[1:] == (copy.id, world.project.id, ["0a0a0a0a-0000-4000-8000-0000000000aa"], world.owner.user.id)
    after = Page.objects.get(pk=copy.pk)
    assert 'src="0b0b0b0b-0000-4000-8000-0000000000bb"' in after.description_html and "0a0a0a0a-0000-4000-8000-0000000000aa" not in after.description_html
    assert after.updated_at > before.updated_at  # a stamp read before the task no longer passes
    assert (after.name, after.description_binary) == ("Plan (Copy)", None)
    assert not PageBeingCopied.objects.filter(page_id=copy.id).exists()


@pytest.mark.parametrize("meanwhile", ["edited", "document built", "archived and renamed"])
def test_a_late_copy_task_never_overwrites_what_happened_meanwhile(world, meanwhile):
    """However late the task runs (a stalled worker after its mark expired), it changes nothing but the HTML it
    read, and that only while the text is unchanged and the page has no document yet."""
    from django.utils import timezone

    copy = _copy_in_progress(world)

    def copy_assets(*args):
        if meanwhile == "edited":
            Page.objects.filter(pk=copy.pk).update(description_html="<p>Edited meanwhile</p>")
        elif meanwhile == "document built":
            Page.objects.filter(pk=copy.pk).update(description_binary=doc_for(["Alpha"]))
        else:
            Page.objects.filter(pk=copy.pk).update(archived_at=timezone.now(), name="Renamed meanwhile")
        return COPIED

    with patch("plane.halo.tasks.copy_assets", side_effect=copy_assets):
        _finish(world, copy)
    after = Page.objects.get(pk=copy.pk)
    if meanwhile == "edited":
        assert after.description_html == "<p>Edited meanwhile</p>"
    elif meanwhile == "document built":
        assert after.description_html == IMAGE_HTML
    else:
        assert 'src="0b0b0b0b-0000-4000-8000-0000000000bb"' in after.description_html
        assert after.archived_at is not None and after.name == "Renamed meanwhile"


def test_a_failed_copy_task_still_removes_the_mark(world):
    from plane.halo.models import PageBeingCopied

    copy = _copy_in_progress(world)
    with patch("plane.halo.tasks.copy_assets", side_effect=RuntimeError("s3 is down")):
        _finish(world, copy)  # logged, as in Plane's task
    assert not PageBeingCopied.objects.filter(page_id=copy.id).exists()
    assert Page.objects.get(pk=copy.pk).description_html == IMAGE_HTML


def test_duplicate_of_a_page_whose_html_lags_takes_its_document(world, api_client):
    """Plane's duplicate is never refused. While the source's stored HTML lags its document, the copy takes the
    document (the authoritative part, with the latest text) and the same mark, instead of the stale HTML alone."""
    from django.utils import timezone
    from pycrdt import XmlElement, XmlText

    from plane.halo.models import PageTextBehind

    document = Doc()
    document.apply_update(doc_for(["Alpha", "Beta, typed in the editor", "Gamma"]))
    with document.transaction():
        heading = document.get(ydoc.TITLE, type=XmlFragment).children.append(XmlElement("heading", {"level": 1.0}))
        heading.children.append(XmlText()).insert(0, "Plan")
    latest = document.get_update()
    Page.objects.filter(pk=world.public.pk).update(description_binary=latest)
    PageTextBehind.objects.create(page_id=world.public.id, since=timezone.now())
    api_client.force_authenticate(user=world.owner.user)
    response = api_client.post(_app_page_url(world, "duplicate/"))
    assert response.status_code == 201, response.content
    copy = Page.objects.get(pk=response.json()["id"])
    assert ydoc.body_signature(bytes(copy.description_binary)) == ydoc.body_signature(latest)
    copied = Doc()
    copied.apply_update(bytes(copy.description_binary))
    assert str(copied.get(ydoc.TITLE, type=XmlFragment)).endswith(">Plan (Copy)</heading>")
    assert PageTextBehind.objects.filter(page_id=copy.id).exists()
    assert world.owner.client.get(f"{world.base}{copy.id}/").json()["text_complete"] is False


def test_duplicate_keeps_plane_s_private_page_rule(world, api_client):
    from plane.halo.models import PageBeingCopied

    api_client.force_authenticate(user=world.owner.user)
    url = f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{world.private.id}/duplicate/"
    pages = Page.objects.count()
    assert api_client.post(url).status_code == 403
    assert Page.objects.count() == pages and not PageBeingCopied.objects.exists()


def test_api_edit_of_a_page_being_copied_is_refused(world, fake_live):
    """Plane's copy task saves the whole page it read, so an edit before it finished would be overwritten."""
    from django.utils import timezone

    from plane.halo.models import PageBeingCopied

    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    PageBeingCopied.objects.create(page_id=world.public.id, since=timezone.now())
    response = world.owner.client.patch(url, {"description_html": "<p>new</p>", "expected_updated_at": stamp},
                                        format="json")
    assert (response.status_code, response.json()["code"]) == (409, "page_not_ready")
    assert Page.objects.get(pk=world.public.pk).description_html == ORIGINAL
    assert not PageVersion.objects.filter(page=world.public).exists()


def test_an_expired_copy_mark_is_ignored(world, fake_live, api_client):
    """A worker that died mid-copy never removes its mark; after two minutes the page is usable again."""
    from datetime import timedelta

    from django.utils import timezone

    from plane.halo.models import PageBeingCopied

    PageBeingCopied.objects.create(page_id=world.public.id, since=timezone.now() - timedelta(minutes=3))
    api_client.force_authenticate(user=world.owner.user)
    assert api_client.get(_description_url(world)).status_code == 200
    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    response = world.owner.client.patch(url, {"description_html": "<p>new</p>", "expected_updated_at": stamp},
                                        format="json")
    assert response.status_code == 200, response.content


def test_create_from_text_without_a_tag_stores_a_paragraph(world):
    response = world.owner.client.post(world.base, {"name": "Plain", "description_html": "Hello world"}, format="json")
    assert response.status_code == 201, response.content
    assert Page.objects.get(pk=response.json()["id"]).description_html == "<p>Hello world</p>"


def test_web_app_properties_patch_keeps_plane_s_behaviour_and_marks_changed_text(world, api_client):
    """The web app's page store PATCHes the whole page, its own copy of the text included. Plane writes that
    HTML (kept); the document stays, so Halo marks the page for its API."""
    from plane.halo.models import PageTextBehind

    api_client.force_authenticate(user=world.owner.user)
    document = bytes(world.public.description_binary)
    same = api_client.patch(_app_page_url(world), {"name": "Renamed", "description_html": ORIGINAL}, format="json")
    assert same.status_code == 200, same.content
    assert not PageTextBehind.objects.filter(page_id=world.public.id).exists()  # the same HTML: nothing to mark
    older = api_client.patch(_app_page_url(world), {"description_html": "<p>Older text</p>"}, format="json")
    assert older.status_code == 200, older.content
    page = Page.objects.get(pk=world.public.pk)
    assert (page.name, page.description_html) == ("Renamed", "<p>Older text</p>")
    assert bytes(page.description_binary) == document
    assert PageTextBehind.objects.filter(page_id=world.public.id).exists()
    assert world.owner.client.get(f"{world.base}{world.public.id}/").json()["text_complete"] is False


def test_a_save_of_html_alone_marks_the_text_as_behind(world, api_client):
    from plane.halo.models import PageTextBehind

    api_client.force_authenticate(user=world.owner.user)
    response = api_client.patch(_description_url(world), {"description_html": "<p>Older text</p>"}, format="json")
    assert response.status_code == 200
    assert PageTextBehind.objects.filter(page_id=world.public.id).exists()
    assert world.owner.client.get(f"{world.base}{world.public.id}/").json()["text_complete"] is False


def test_create_answers_201_once_although_the_broker_is_down(world):
    pages = Page.objects.count()
    with patch("plane.halo.views.page_transaction") as page_transaction:
        page_transaction.delay.side_effect = RuntimeError("broker down")
        response = world.owner.client.post(world.base, {"name": "New"}, format="json")
    assert response.status_code == 201, response.content
    assert Page.objects.count() == pages + 1


def test_duplicate_without_a_broker_leaves_the_copy_usable(world, api_client):
    from plane.halo.models import PageBeingCopied

    api_client.force_authenticate(user=world.owner.user)
    with patch("plane.halo.views.finish_page_copy") as finish_page_copy:
        finish_page_copy.delay.side_effect = RuntimeError("broker down")
        response = api_client.post(_app_page_url(world, "duplicate/"))
    assert response.status_code == 201, response.content
    assert not PageBeingCopied.objects.filter(page_id=response.json()["id"]).exists()


@pytest.mark.parametrize("value", [123, ["<p>x</p>"], {"html": "<p>x</p>"}])
def test_create_refuses_html_that_is_not_text(world, value):
    pages = Page.objects.count()
    response = world.owner.client.post(world.base, {"name": "New", "description_html": value}, format="json")
    assert (response.status_code, response.json()["code"]) == (400, "invalid_field")
    assert Page.objects.count() == pages


def test_a_save_of_empty_html_alone_marks_the_text_as_behind(world, api_client):
    """Plane's serializer writes an empty description_html too, while the document stays."""
    from plane.halo.models import PageTextBehind

    api_client.force_authenticate(user=world.owner.user)
    assert api_client.patch(_description_url(world), {"description_html": ""}, format="json").status_code == 200
    assert PageTextBehind.objects.filter(page_id=world.public.id).exists()


def _copy_with_document(world):
    from django.utils import timezone

    from plane.halo.models import PageBeingCopied

    copy = world.make_page(world.owner, "Plan (Copy)", IMAGE_HTML)  # make_page builds its document
    PageBeingCopied.objects.create(page_id=copy.id, since=timezone.now())
    return copy


def test_copy_task_points_the_copied_document_at_the_copied_images(world):
    copy = _copy_with_document(world)
    assert ydoc.image_sources(bytes(copy.description_binary)) == ["0a0a0a0a-0000-4000-8000-0000000000aa"]
    with patch("plane.halo.tasks.copy_assets", return_value=COPIED):
        _finish(world, copy)
    after = Page.objects.get(pk=copy.pk)
    assert ydoc.image_sources(bytes(after.description_binary)) == ["0b0b0b0b-0000-4000-8000-0000000000bb"]
    assert 'src="0b0b0b0b-0000-4000-8000-0000000000bb"' in after.description_html


def test_copy_task_leaves_a_copied_document_someone_changed_meanwhile(world):
    copy = _copy_with_document(world)
    edited = doc_for(["Edited", ("image", "0a0a0a0a-0000-4000-8000-0000000000aa")])

    def copy_assets(*args):
        Page.objects.filter(pk=copy.pk).update(description_binary=edited)
        return COPIED

    with patch("plane.halo.tasks.copy_assets", side_effect=copy_assets):
        _finish(world, copy)
    after = Page.objects.get(pk=copy.pk)
    assert bytes(after.description_binary) == edited and after.description_html == IMAGE_HTML


def test_web_app_properties_patch_is_saved_although_the_broker_is_down(world, api_client):
    """As in Plane, the save stands; the page task is queued after commit and its failure only logged."""
    from plane.halo.models import PageTextBehind

    api_client.force_authenticate(user=world.owner.user)
    with patch("plane.halo.views.page_transaction") as page_transaction:
        page_transaction.delay.side_effect = RuntimeError("broker down")
        response = api_client.patch(_app_page_url(world), {"name": "Renamed", "description_html": "<p>New text</p>"},
                                    format="json")
    assert response.status_code == 200, response.content
    page = Page.objects.get(pk=world.public.pk)
    assert (page.name, page.description_html) == ("Renamed", "<p>New text</p>")
    assert PageTextBehind.objects.filter(page_id=world.public.id).exists()


def test_web_app_properties_patch_keeps_plane_s_answers(world, api_client):
    """Halo's copy of Plane's properties PATCH answers as Plane does: locked page, others' access change."""
    api_client.force_authenticate(user=world.member.user)
    response = api_client.patch(_app_page_url(world), {"access": 1}, format="json")
    assert (response.status_code, response.json()) == (
        400, {"error": "Access cannot be updated since this page is owned by someone else"})
    Page.objects.filter(pk=world.public.pk).update(is_locked=True)
    response = api_client.patch(_app_page_url(world), {"name": "Renamed"}, format="json")
    assert (response.status_code, response.json()) == (400, {"error": "Page is locked"})


# ---- Plane's page load is stock (step-back, 2026-10-24) ----

def test_editor_load_is_plane_s_own_handler(world):
    from django.urls import resolve

    from plane.app.views.page.base import PagesDescriptionViewSet
    from plane.halo.views import HaloPagesDescriptionViewSet

    assert resolve(_description_url(world)).func.cls is HaloPagesDescriptionViewSet
    assert HaloPagesDescriptionViewSet.retrieve is PagesDescriptionViewSet.retrieve
    defined = {name for name, value in vars(HaloPagesDescriptionViewSet).items() if callable(value)}
    assert defined == {"partial_update", "_visible_page"}
    for attribute in ("authentication_classes", "permission_classes", "throttle_classes", "dispatch", "initial"):
        assert getattr(HaloPagesDescriptionViewSet, attribute) == getattr(PagesDescriptionViewSet, attribute)


def test_editor_load_needs_neither_redis_nor_live(world, fake_live, api_client):
    """With Halo's Redis down and live's converter down, loads answer as in stock Plane: the stored document, or
    nothing for a page without one (live converts that page itself)."""
    import redis

    fresh = world.make_page(world.owner, "Fresh", "<p>x</p>", binary=False)
    fake_live.down = True
    api_client.force_authenticate(user=world.owner.user)
    with patch("plane.halo.lease.redis_client", side_effect=redis.ConnectionError("redis is down")):
        stored = api_client.get(_description_url(world))
        empty = api_client.get(
            f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{fresh.id}/description/")
    assert stored.status_code == 200
    assert b"".join(stored.streaming_content) == bytes(world.public.description_binary)
    assert empty.status_code == 200 and b"".join(empty.streaming_content) == b""
    assert fake_live.calls == [] and Page.objects.get(pk=fresh.pk).description_binary is None


def test_editor_load_waits_for_neither_an_api_edit_nor_a_copy_task(world, api_client):
    from django.utils import timezone

    from plane.halo.models import PageBeingCopied

    PageBeingCopied.objects.create(page_id=world.public.id, since=timezone.now())
    api_client.force_authenticate(user=world.owner.user)
    with lease.edit_lease(world.public.id):
        started = time.monotonic()
        response = api_client.get(_description_url(world))
        took = time.monotonic() - started
    assert response.status_code == 200 and took < 0.5


# ---- first documents only at API create ----

def test_create_stores_a_first_document_with_the_title(world, fake_live):
    response = world.member.client.post(
        world.base, {"name": "Notes", "description_html": "<p>Hi</p><script>alert(1)</script>"}, format="json"
    )
    assert response.status_code == 201, response.content
    page = Page.objects.get(pk=response.json()["id"])
    assert "<script" not in page.description_html
    assert page.owned_by_id == world.member.user.id
    assert response.json()["url"].endswith(f"/pages/{page.id}")
    assert response.json()["text_editable"] is True
    loaded = Doc()
    loaded.apply_update(bytes(page.description_binary))
    assert str(loaded.get(ydoc.TITLE, type=XmlFragment)).endswith(">Notes</heading>")
    assert page.description_json["content"][0]["content"][0]["text"] == "Hi"
    assert ids(world.member.client.get(world.base)) == {str(world.public.id), str(page.id)}


@pytest.mark.parametrize("failure,reason", [("down", "live_unavailable"), ("too_large", "page_too_large_for_editor")])
def test_create_without_a_document_says_so_and_keeps_the_text(world, fake_live, failure, reason):
    setattr(fake_live, failure, True)
    response = world.owner.client.post(world.base, {"name": "Notes", "description_html": "<p>Kept</p>"}, format="json")
    assert response.status_code == 201, response.content
    answer = response.json()
    assert (answer["text_editable"], answer["text_editable_reason"]) == (False, reason)
    assert answer["text_editable_hint"]
    page = Page.objects.get(pk=answer["id"])
    assert (page.description_html, page.description_binary) == ("<p>Kept</p>", None)
    setattr(fake_live, failure, False)
    edit = world.owner.client.patch(f"{world.base}{page.id}/", {"description_html": "<p>New</p>",
                                                                "expected_updated_at": answer["updated_at"]},
                                    format="json")
    assert (edit.status_code, edit.json()["code"]) == (409, "page_not_ready")


def test_retrieve_says_whether_the_text_can_be_edited(world):
    assert world.owner.client.get(f"{world.base}{world.public.id}/").json()["text_editable"] is True
    fresh = world.make_page(world.owner, "Fresh", "<p>x</p>", binary=False)
    answer = world.owner.client.get(f"{world.base}{fresh.id}/").json()
    assert (answer["text_editable"], answer["text_editable_reason"]) == (False, "not_opened_yet")
    assert "Open the page once in Halo" in answer["text_editable_hint"]


def test_api_edit_of_a_page_never_opened_is_refused(world, fake_live):
    """A page made in the web app and not opened since has no editor document; live builds it on first open."""
    fresh = world.make_page(world.owner, "Fresh", "<p>Made in the web app</p>", binary=False)
    url = f"{world.base}{fresh.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    response = world.owner.client.patch(url, {"append_html": "<p>more</p>", "expected_updated_at": stamp},
                                        format="json")
    assert (response.status_code, response.json()["code"]) == (409, "page_not_ready")
    assert "Open it once in Halo" in response.json()["error"]
    assert fake_live.calls == []
    assert Page.objects.get(pk=fresh.pk).description_html == "<p>Made in the web app</p>"


# ---- saves merge only with shared history ----

def _save(api_client, world, binary, html=ORIGINAL):
    return api_client.patch(_description_url(world), {"description_binary": base64.b64encode(binary).decode(),
                                                      "description_html": html}, format="json")


def test_two_separately_built_first_documents_are_not_merged(world, api_client):
    """Two first documents of one page (live's own conversion and the browser's offline fallback): the later
    save replaces the earlier one, as in Plane, instead of a union that would hold the text twice."""
    separate = doc_for(["Alpha", "Beta", "Gamma"])  # converted on its own, like world.public's document
    api_client.force_authenticate(user=world.owner.user)
    with patch("plane.halo.live.publish_update") as publish:
        assert _save(api_client, world, separate).status_code == 200
    stored = bytes(Page.objects.get(pk=world.public.pk).description_binary)
    assert stored == separate
    assert ydoc.body_signature(stored) == ydoc.body_signature(doc_for(["Alpha", "Beta", "Gamma"]))
    publish.assert_not_called()


def test_a_separately_built_document_saved_after_an_api_edit_replaces_it_as_plane_would(world, fake_live,
                                                                                         api_client):
    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    edited = world.owner.client.patch(url, {"description_html": "<p>Alpha</p><p>Beta by AURA</p><p>Gamma</p>",
                                            "expected_updated_at": stamp}, format="json").json()
    separate = doc_for(["Alpha", "Beta", "Gamma"])
    api_client.force_authenticate(user=world.owner.user)
    assert _save(api_client, world, separate).status_code == 200
    assert bytes(Page.objects.get(pk=world.public.pk).description_binary) == separate
    # The API edit stays restorable from its versions.
    after = PageVersion.objects.get(pk=edited["after_version_id"])
    assert "Beta by AURA" in after.description_html


# ---- duplicates and the copy race ----

def test_duplicate_makes_no_live_call(world, api_client):
    api_client.force_authenticate(user=world.owner.user)
    with patch("plane.halo.live.convert_html", side_effect=AssertionError("no conversion in a duplicate")):
        response = api_client.post(_app_page_url(world, "duplicate/"))
    assert response.status_code == 201, response.content


def test_duplicate_of_a_page_without_a_document_has_none(world, api_client):
    fresh = world.make_page(world.owner, "Fresh", "<p>x</p>", binary=False)
    api_client.force_authenticate(user=world.owner.user)
    response = api_client.post(
        f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{fresh.id}/duplicate/")
    assert response.status_code == 201
    assert Page.objects.get(pk=response.json()["id"]).description_binary is None


IMAGE_JSON = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Alpha"}]},
                                         {"type": "imageComponent", "attrs": {"src": "0a0a0a0a-0000-4000-8000-0000000000aa"}}]}


def _copy_with_json(world):
    copy = _copy_with_document(world)
    Page.objects.filter(pk=copy.pk).update(description_json=IMAGE_JSON)
    return Page.objects.get(pk=copy.pk)


def test_copy_task_repoints_html_json_and_document_and_tells_live(world):
    """The copy only opened: the task writes, and an editor that has the copy open gets the change."""
    copy = _copy_with_json(world)
    with patch("plane.halo.tasks.copy_assets", return_value=COPIED), \
            patch("plane.halo.live.publish_update") as publish:
        _finish(world, copy)
    after = Page.objects.get(pk=copy.pk)
    assert 'src="0b0b0b0b-0000-4000-8000-0000000000bb"' in after.description_html
    assert after.description_json["content"][1]["attrs"]["src"] == "0b0b0b0b-0000-4000-8000-0000000000bb"
    assert ydoc.image_sources(bytes(after.description_binary)) == ["0b0b0b0b-0000-4000-8000-0000000000bb"]
    publish.assert_called_once_with(copy.id, bytes(after.description_binary))


def test_copy_saved_before_the_task_reads_it_keeps_the_typing_and_gets_the_images(world, api_client):
    copy = _copy_with_json(world)
    typed = Doc()
    typed.apply_update(bytes(copy.description_binary))
    text = typed.get(ydoc.BODY, type=XmlFragment).children[0].children[0]
    text.insert(len(text), " typed")
    api_client.force_authenticate(user=world.owner.user)
    url = f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{copy.id}/description/"
    html = '<p>Alpha typed</p><image-component src="0a0a0a0a-0000-4000-8000-0000000000aa"></image-component>'
    assert api_client.patch(url, {"description_binary": base64.b64encode(typed.get_update()).decode(),
                                  "description_html": html}, format="json").status_code == 200
    with patch("plane.halo.tasks.copy_assets", return_value=COPIED), patch("plane.halo.live.publish_update"):
        _finish(world, copy)
    after = Page.objects.get(pk=copy.pk)
    assert "Alpha typed" in after.description_html and 'src="0b0b0b0b-0000-4000-8000-0000000000bb"' in after.description_html
    assert ydoc.image_sources(bytes(after.description_binary)) == ["0b0b0b0b-0000-4000-8000-0000000000bb"]
    assert ydoc.body_signature(ydoc.with_image_sources(bytes(after.description_binary), {"0b0b0b0b-0000-4000-8000-0000000000bb": "0a0a0a0a-0000-4000-8000-0000000000aa"})) \
        == ydoc.body_signature(typed.get_update())


def test_copy_without_a_document_is_replaced_by_live_s_own_first_save_as_in_plane(world, api_client):
    """Live converts such a copy from the HTML it read; if that was before the task wrote, live's first save
    brings the original's image references back, as stock Plane can end too."""
    copy = _copy_in_progress(world)  # no document
    with patch("plane.halo.tasks.copy_assets", return_value=COPIED):
        _finish(world, copy)
    assert 'src="0b0b0b0b-0000-4000-8000-0000000000bb"' in Page.objects.get(pk=copy.pk).description_html
    lives_own = doc_for(["Alpha", ("image", "0a0a0a0a-0000-4000-8000-0000000000aa")])
    api_client.force_authenticate(user=world.owner.user)
    url = f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{copy.id}/description/"
    assert api_client.patch(url, {"description_binary": base64.b64encode(lives_own).decode(),
                                  "description_html": IMAGE_HTML}, format="json").status_code == 200
    after = Page.objects.get(pk=copy.pk)
    assert after.description_html == IMAGE_HTML and bytes(after.description_binary) == lives_own


def test_an_api_append_after_the_copy_task_keeps_the_copied_images(world, fake_live):
    copy = _copy_with_json(world)
    with patch("plane.halo.tasks.copy_assets", return_value=COPIED), patch("plane.halo.live.publish_update"):
        _finish(world, copy)
    url = f"{world.base}{copy.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    response = world.owner.client.patch(url, {"append_html": "<p>Added</p>", "expected_updated_at": stamp},
                                        format="json")
    assert response.status_code == 200, response.content
    after = Page.objects.get(pk=copy.pk)
    assert 'src="0b0b0b0b-0000-4000-8000-0000000000bb"' in after.description_html and "0a0a0a0a-0000-4000-8000-0000000000aa" not in after.description_html
    assert after.description_json["content"][1]["attrs"]["src"] == "0b0b0b0b-0000-4000-8000-0000000000bb"
    assert ydoc.image_sources(bytes(after.description_binary)) == ["0b0b0b0b-0000-4000-8000-0000000000bb"]
    for version_id in (response.json()["before_version_id"], response.json()["after_version_id"]):
        version = PageVersion.objects.get(pk=version_id)
        assert "0b0b0b0b-0000-4000-8000-0000000000bb" in version.description_html and "0a0a0a0a-0000-4000-8000-0000000000aa" not in version.description_html


MIXED_HTML = ('<p>Alpha</p><image-component src="11111111-1111-4111-8111-111111111111"></image-component>'
              '<img src="https://example.com/logo.png"><img src="22222222-2222-4222-8222-222222222222">')
MIXED_COPIES = [{"old_asset_id": "11111111-1111-4111-8111-111111111111", "new_asset_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
                {"old_asset_id": "22222222-2222-4222-8222-222222222222", "new_asset_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}]


def test_copy_task_copies_uploaded_images_and_leaves_web_images_alone(world):
    """Only asset UUIDs are copied (a web image's URL would make Plane's asset query fail for the whole page),
    and an uploaded image in an img tag is pointed at its copy too."""
    from django.utils import timezone

    from plane.halo.models import PageBeingCopied

    copy = world.make_page(world.owner, "Mixed (Copy)", MIXED_HTML, binary=False)
    PageBeingCopied.objects.create(page_id=copy.id, since=timezone.now())
    with patch("plane.halo.tasks.copy_assets", return_value=MIXED_COPIES) as copy_assets:
        _finish(world, copy)
    assert copy_assets.call_args.args[3] == ["11111111-1111-4111-8111-111111111111",
                                             "22222222-2222-4222-8222-222222222222"]
    html = Page.objects.get(pk=copy.pk).description_html
    assert 'src="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"' in html and 'src="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"' in html
    assert 'src="https://example.com/logo.png"' in html
    assert "11111111-1111" not in html and "22222222-2222" not in html


MENTION_ID = "0c0c0c0c-0000-4000-8000-0000000000cc"
NEW_MENTION_ID = "0e0e0e0e-0000-4000-8000-0000000000ee"
TARGET = "0d0d0d0d-0000-4000-8000-0000000000dd"  # PageLog.transaction and entity_identifier are UUIDs


def _mention(mention_id):
    return f'<mention-component id="{mention_id}" entity_name="user_mention" entity_identifier="{TARGET}"></mention-component>'


def _run_queued(no_celery):
    """Run the queued indexing in queue order, as the worker does: Plane's page_transaction and Halo's
    index_page and add_to_index (the other queued tasks do not touch the index)."""
    from plane.bgtasks.page_transaction_task import page_transaction
    from plane.halo.tasks import add_to_index, index_page

    ran = []
    for call in no_celery.call_args_list:
        if "new_description_html" in call.kwargs:
            page_transaction(**call.kwargs)
            ran.append("page_transaction")
        elif set(call.kwargs) == {"page_id"}:
            index_page(**call.kwargs)
            ran.append("index_page")
        elif set(call.kwargs) == {"page_id", "html"}:
            add_to_index(**call.kwargs)
            ran.append("add_to_index")
    no_celery.reset_mock()
    return ran


def _merged_save(world, api_client, stale, html):
    published = []
    with patch("plane.halo.live.publish_update", side_effect=lambda page_id, update: published.append(update) or 1):
        response = api_client.patch(_description_url(world), {
            "description_binary": base64.b64encode(stale.get_update()).decode(), "description_html": html},
            format="json")
    assert response.status_code == 200 and published
    return published[0]


def _api_append(world):
    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    assert world.owner.client.patch(url, {"append_html": "<p>Delta by AURA</p>", "expected_updated_at": stamp},
                                    format="json").status_code == 200


def test_mentions_removed_in_a_merged_save_leave_plane_s_index(world, fake_live, api_client, no_celery,
                                                               django_capture_on_commit_callbacks):
    """A stale editor removes a mention while it misses an API edit; its merged save deletes no index entry, and
    the complete save that follows reconciles the index with the whole text in the worker."""
    from plane.bgtasks.page_transaction_task import page_transaction
    from plane.db.models import PageLog

    with_mention = f"<p>Alpha</p><p>Beta {_mention(MENTION_ID)}</p><p>Gamma</p>"
    Page.objects.filter(pk=world.public.pk).update(description_html=with_mention)
    page_transaction(new_description_html=with_mention, old_description_html=None, page_id=world.public.id)
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION_ID).exists()
    stale = Doc()
    stale.apply_update(bytes(world.public.description_binary))
    _api_append(world)
    no_celery.reset_mock()
    api_client.force_authenticate(user=world.owner.user)
    text = stale.get(ydoc.BODY, type=XmlFragment).children[0].children[0]
    text.insert(len(text), " typed")
    with django_capture_on_commit_callbacks(execute=True):
        missing = _merged_save(world, api_client, stale, "<p>Alpha typed</p><p>Beta</p><p>Gamma</p>")  # removed
        stale.apply_update(missing)
        assert api_client.patch(_description_url(world), {
            "description_binary": base64.b64encode(stale.get_update()).decode(),
            "description_html": "<p>Alpha typed</p><p>Beta</p><p>Gamma</p><p>Delta by AURA</p>"},
            format="json").status_code == 200
    assert _run_queued(no_celery) == ["add_to_index", "index_page"]  # add-only (a no-op once cleared), then Halo's
    assert not PageLog.objects.filter(page_id=world.public.id, transaction=MENTION_ID).exists()


def test_a_mention_indexed_between_a_merged_and_a_complete_save_is_removed_when_gone(
        world, fake_live, api_client, no_celery, django_capture_on_commit_callbacks):
    """An HTML-only save between them adds a mention, the complete save removes it, and the worker runs both
    jobs only after the complete save, in queue order: the mention's row is gone at the end."""
    from plane.db.models import PageLog

    stale = Doc()
    stale.apply_update(bytes(world.public.description_binary))
    _api_append(world)
    no_celery.reset_mock()
    api_client.force_authenticate(user=world.owner.user)
    with django_capture_on_commit_callbacks(execute=True):
        missing = _merged_save(world, api_client, stale, ORIGINAL)
        assert api_client.patch(_description_url(world), {  # HTML only, with a new mention
            "description_html": f"<p>Alpha</p><p>Beta {_mention(NEW_MENTION_ID)}</p><p>Gamma</p>"},
            format="json").status_code == 200
        stale.apply_update(missing)
        assert api_client.patch(_description_url(world), {  # complete, without the mention
            "description_binary": base64.b64encode(stale.get_update()).decode(),
            "description_html": "<p>Alpha</p><p>Beta</p><p>Gamma</p><p>Delta by AURA</p>"},
            format="json").status_code == 200
    assert _run_queued(no_celery) == ["add_to_index", "add_to_index", "index_page"]  # the HTML-only one too
    assert not PageLog.objects.filter(page_id=world.public.id, transaction=NEW_MENTION_ID).exists()


def test_an_index_job_leaves_a_page_alone_while_a_merged_save_s_text_is_stored(world, fake_live, api_client,
                                                                                no_celery,
                                                                                django_capture_on_commit_callbacks):
    """A complete save keeps a mention; another stale editor's merged save, without it, lands before the jobs
    run. Neither job may drop the mention the document has (the merged save's is add-only); nor does the next
    complete save's index job."""
    from plane.bgtasks.page_transaction_task import page_transaction
    from plane.db.models import PageLog

    with_mention = f"<p>Alpha</p><p>Beta {_mention(MENTION_ID)}</p><p>Gamma</p>"
    Page.objects.filter(pk=world.public.pk).update(description_html=with_mention)
    page_transaction(new_description_html=with_mention, old_description_html=None, page_id=world.public.id)
    stale = Doc()
    stale.apply_update(bytes(world.public.description_binary))
    _api_append(world)
    no_celery.reset_mock()
    current = Doc()
    current.apply_update(bytes(Page.objects.get(pk=world.public.pk).description_binary))
    api_client.force_authenticate(user=world.owner.user)
    with django_capture_on_commit_callbacks(execute=True):
        assert api_client.patch(_description_url(world), {  # complete, keeps the mention
            "description_binary": base64.b64encode(current.get_update()).decode(),
            "description_html": with_mention + "<p>Delta by AURA</p>"}, format="json").status_code == 200
        missing = _merged_save(world, api_client, stale, ORIGINAL)  # older text, without the mention
    assert _run_queued(no_celery) == ["page_transaction", "add_to_index"]  # the complete save's, the merged one's
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION_ID).exists()
    stale.apply_update(missing)
    with django_capture_on_commit_callbacks(execute=True):
        assert api_client.patch(_description_url(world), {
            "description_binary": base64.b64encode(stale.get_update()).decode(),
            "description_html": with_mention + "<p>Delta by AURA</p>"}, format="json").status_code == 200
    assert _run_queued(no_celery) == ["index_page"]
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION_ID).exists()


def test_ordinary_saves_keep_plane_s_own_mention_indexing(world, api_client, no_celery,
                                                          django_capture_on_commit_callbacks):
    """No API edit involved: an editor's save and a properties PATCH queue Plane's page_transaction with Plane's
    own arguments (the new HTML, and the HTML stored before the save), and no Halo index job."""
    current = Doc()
    current.apply_update(bytes(world.public.description_binary))
    text = current.get(ydoc.BODY, type=XmlFragment).children[0].children[0]
    text.insert(len(text), " typed")
    api_client.force_authenticate(user=world.owner.user)
    typed = "<p>Alpha typed</p><p>Beta</p><p>Gamma</p>"
    with django_capture_on_commit_callbacks(execute=True):
        assert api_client.patch(_description_url(world), {
            "description_binary": base64.b64encode(current.get_update()).decode(),
            "description_html": typed}, format="json").status_code == 200
        assert api_client.patch(_app_page_url(world), {"description_html": ORIGINAL}, format="json").status_code == 200
    indexing = [call.kwargs for call in no_celery.call_args_list
                if "new_description_html" in call.kwargs or set(call.kwargs) == {"page_id"}]
    assert indexing == [
        {"new_description_html": typed, "old_description_html": ORIGINAL, "page_id": world.public.id},
        {"new_description_html": ORIGINAL, "old_description_html": typed, "page_id": world.public.id},
    ]


def test_a_merged_save_that_missed_only_the_copy_task_indexes_its_new_mention_at_once(
        world, api_client, no_celery, django_capture_on_commit_callbacks):
    """No API edit: an editor of a duplicate missed the copy task's image change, adds a mention and saves. The
    save is merged (the image change is kept) and its mention is indexed at once, through Plane's own task."""
    from plane.db.models import PageLog

    copy = _copy_with_json(world)
    stale = Doc()
    stale.apply_update(bytes(copy.description_binary))
    with patch("plane.halo.tasks.copy_assets", return_value=COPIED), patch("plane.halo.live.publish_update"):
        _finish(world, copy)
    text = stale.get(ydoc.BODY, type=XmlFragment).children[0].children[0]
    text.insert(len(text), " typed")
    api_client.force_authenticate(user=world.owner.user)
    url = f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{copy.id}/description/"
    html = (f'<p>Alpha typed {_mention(NEW_MENTION_ID)}</p>'
            '<image-component src="0a0a0a0a-0000-4000-8000-0000000000aa"></image-component>')
    no_celery.reset_mock()
    with patch("plane.halo.live.publish_update", return_value=1) as publish, \
            django_capture_on_commit_callbacks(execute=True):
        assert api_client.patch(url, {"description_binary": base64.b64encode(stale.get_update()).decode(),
                                      "description_html": html}, format="json").status_code == 200
    assert publish.called  # merged: the editor gets the image change it missed
    after = Page.objects.get(pk=copy.pk)
    assert ydoc.image_sources(bytes(after.description_binary)) == ["0b0b0b0b-0000-4000-8000-0000000000bb"]
    indexing = [call.kwargs for call in no_celery.call_args_list if "html" in call.kwargs]
    assert indexing == [{"page_id": copy.id, "html": html}]
    assert _run_queued(no_celery) == ["add_to_index"]
    assert PageLog.objects.filter(page_id=copy.id, transaction=NEW_MENTION_ID).exists()


def test_a_stale_save_after_another_editor_s_complete_save_keeps_an_api_edit_s_mention(
        world, fake_live, api_client, no_celery, django_capture_on_commit_callbacks):
    """An API edit adds a mention; editor A saves completely; editor B, stale since before the API edit, saves
    without the mention (merged), then saves completely once it has the missing part. Run in queue order with
    Plane's real task, the mention's entry is never deleted (a deleted one could not come back)."""
    from plane.bgtasks.page_transaction_task import page_transaction
    from plane.db.models import PageLog

    stale = Doc()
    stale.apply_update(bytes(world.public.description_binary))
    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    no_celery.reset_mock()
    assert world.owner.client.patch(url, {"append_html": f"<p>Delta {_mention(MENTION_ID)}</p>",
                                          "expected_updated_at": stamp}, format="json").status_code == 200
    edits = [call.kwargs for call in no_celery.call_args_list if "new_description_html" in call.kwargs]
    no_celery.reset_mock()
    for kwargs in edits:  # the API edit's own page_transaction
        page_transaction(**kwargs)
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION_ID).exists()
    stored = Page.objects.get(pk=world.public.pk)
    current = Doc()
    current.apply_update(bytes(stored.description_binary))
    api_client.force_authenticate(user=world.owner.user)
    with django_capture_on_commit_callbacks(execute=True):
        assert api_client.patch(_description_url(world), {  # editor A: complete
            "description_binary": base64.b64encode(current.get_update()).decode(),
            "description_html": stored.description_html}, format="json").status_code == 200
        missing = _merged_save(world, api_client, stale, ORIGINAL)  # editor B: stale, without the mention
    assert _run_queued(no_celery) == ["page_transaction", "add_to_index"]
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION_ID).exists()
    stale.apply_update(missing)
    with django_capture_on_commit_callbacks(execute=True):
        assert api_client.patch(_description_url(world), {  # editor B: complete
            "description_binary": base64.b64encode(stale.get_update()).decode(),
            "description_html": stored.description_html}, format="json").status_code == 200
    assert _run_queued(no_celery) == ["index_page"]
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION_ID).exists()


def test_a_properties_patch_while_merged_marked_never_drops_a_mention_the_document_keeps(
        world, fake_live, api_client, no_celery, django_capture_on_commit_callbacks):
    """A stale editor's merged save adds a mention; a properties PATCH sends HTML without it while the page is
    merged-marked; then the complete save. With real PageLog rows, the mention's entry is never deleted."""
    from plane.db.models import PageLog

    stale = Doc()
    stale.apply_update(bytes(world.public.description_binary))
    _api_append(world)
    no_celery.reset_mock()
    api_client.force_authenticate(user=world.owner.user)
    with_mention = f"<p>Alpha</p><p>Beta {_mention(MENTION_ID)}</p><p>Gamma</p>"
    with django_capture_on_commit_callbacks(execute=True):
        missing = _merged_save(world, api_client, stale, with_mention)
    assert _run_queued(no_celery) == ["add_to_index"]
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION_ID).exists()
    with django_capture_on_commit_callbacks(execute=True):
        assert api_client.patch(_app_page_url(world), {"description_html": ORIGINAL},
                                format="json").status_code == 200
    assert _run_queued(no_celery) == ["add_to_index"]  # add-only while merged-marked: nothing deleted
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION_ID).exists()
    stale.apply_update(missing)
    with django_capture_on_commit_callbacks(execute=True):
        assert api_client.patch(_description_url(world), {
            "description_binary": base64.b64encode(stale.get_update()).decode(),
            "description_html": with_mention + "<p>Delta by AURA</p>"}, format="json").status_code == 200
    assert _run_queued(no_celery) == ["index_page"]
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION_ID).exists()


def test_a_duplicate_of_a_merged_marked_page_indexes_add_only_until_its_complete_save(
        world, fake_live, api_client, no_celery, django_capture_on_commit_callbacks):
    """The source is merged-marked (its document keeps a mention its HTML may lack); the copy takes that document
    and the mark as it is, merged included. An HTML-only save of the copy without the mention then deletes no
    index entry, and the copy's complete save keeps it."""
    from django.utils import timezone

    from plane.db.models import PageLog
    from plane.halo.models import PageTextBehind

    with_mention = f"<p>Alpha</p><p>Beta {_mention(MENTION_ID)}</p><p>Gamma</p>"
    Page.objects.filter(pk=world.public.pk).update(description_html=with_mention)
    PageTextBehind.objects.create(page_id=world.public.id, since=timezone.now(), merged=True)
    api_client.force_authenticate(user=world.owner.user)
    with django_capture_on_commit_callbacks(execute=True):
        response = api_client.post(_app_page_url(world, "duplicate/"))
    assert response.status_code == 201, response.content
    copy = Page.objects.get(pk=response.json()["id"])
    assert PageTextBehind.objects.get(page_id=copy.id).merged is True
    _run_queued(no_celery)  # the duplicate's own page_transaction (old None)
    assert PageLog.objects.filter(page_id=copy.id, transaction=MENTION_ID).exists()
    url = f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{copy.id}/description/"
    with django_capture_on_commit_callbacks(execute=True):
        assert api_client.patch(url, {"description_html": ORIGINAL}, format="json").status_code == 200
    assert _run_queued(no_celery) == ["add_to_index"]
    assert PageLog.objects.filter(page_id=copy.id, transaction=MENTION_ID).exists()
    with django_capture_on_commit_callbacks(execute=True):
        assert api_client.patch(url, {
            "description_binary": base64.b64encode(bytes(copy.description_binary)).decode(),
            "description_html": with_mention}, format="json").status_code == 200
    assert _run_queued(no_celery) == ["index_page"]
    assert PageLog.objects.filter(page_id=copy.id, transaction=MENTION_ID).exists()


IMAGE_ID = "0f0f0f0f-0000-4000-8000-0000000000ff"


@pytest.mark.parametrize("sent_id", ["0a1a1a1a-0000-4000-8000-0000000000a1", None])
def test_an_api_edit_keeps_the_document_s_image_id_in_the_html_and_the_index(world, fake_live, api_client,
                                                                                 no_celery, sent_id):
    """The page's image has the editor's id; an API edit changes the text and sends the image with another id, or
    none. The document keeps the image's own id, so the stored HTML and JSON take it too, and Plane's index keeps
    the image's entry through the API edit and the editor's next save."""
    from pycrdt import XmlElement, XmlText

    from plane.bgtasks.page_transaction_task import page_transaction
    from plane.db.models import PageLog
    from plane.halo.edit import with_document_image_ids

    src = "0a0a0a0a-0000-4000-8000-0000000000aa"
    doc = Doc()
    body = doc.get(ydoc.BODY, type=XmlFragment)
    with doc.transaction():
        body.children.append(XmlElement("paragraph", {})).children.append(XmlText()).insert(0, "Alpha")
        body.children.append(XmlElement("imageComponent", {"src": src, "id": IMAGE_ID}))
    html = f'<p>Alpha</p><image-component src="{src}" id="{IMAGE_ID}"></image-component>'
    Page.objects.filter(pk=world.public.pk).update(description_binary=doc.get_update(), description_html=html)
    page_transaction(new_description_html=html, old_description_html=None, page_id=world.public.id)
    assert PageLog.objects.filter(page_id=world.public.id, transaction=IMAGE_ID).exists()
    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    sent = f' id="{sent_id}"' if sent_id else ""
    no_celery.reset_mock()
    response = world.owner.client.patch(url, {
        "description_html": f'<p>Alpha edited</p><image-component src="{src}"{sent}></image-component>',
        "expected_updated_at": stamp}, format="json")
    assert response.status_code == 200, response.content
    stored = Page.objects.get(pk=world.public.pk)
    assert ydoc.image_ids(bytes(stored.description_binary)) == [(src, IMAGE_ID)]
    assert f'id="{IMAGE_ID}"' in stored.description_html and "Alpha edited" in stored.description_html
    assert not sent_id or sent_id not in stored.description_html
    for kwargs in [call.kwargs for call in no_celery.call_args_list if "new_description_html" in call.kwargs]:
        page_transaction(**kwargs)  # the API edit's own indexing
    assert PageLog.objects.filter(page_id=world.public.id, transaction=IMAGE_ID).exists()
    # The JSON takes the document's id too; images whose sources disagree are left alone.
    json_data = {"type": "doc", "content": [{"type": "imageComponent", "attrs": {"src": src}}]}
    assert with_document_image_ids("<p>x</p>", json_data, bytes(stored.description_binary))[1] == {
        "type": "doc", "content": [{"type": "imageComponent", "attrs": {"src": src, "id": IMAGE_ID}}]}
    other = f'<image-component src="0b0b0b0b-0000-4000-8000-0000000000bb" id="{sent_id or "x"}"></image-component>'
    assert with_document_image_ids(other, None, bytes(stored.description_binary))[0] == other

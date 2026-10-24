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


def test_create_sanitizes_and_leaves_document_empty(world):
    response = world.member.client.post(
        world.base, {"name": "Notes", "description_html": "<p>Hi</p><script>alert(1)</script>"}, format="json"
    )
    assert response.status_code == 201, response.content
    page = Page.objects.get(pk=response.json()["id"])
    assert "<script" not in page.description_html
    assert page.description_binary is None
    assert page.owned_by_id == world.member.user.id
    assert response.json()["url"].endswith(f"/pages/{page.id}")
    assert ids(world.member.client.get(world.base)) == {str(world.public.id), str(page.id)}


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
    assert len(callbacks) == 1  # page_transaction only: a merged save makes no version (its HTML is older)
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


def test_first_load_of_a_page_too_large_for_the_converter_is_left_to_live(world, fake_live, api_client):
    page = world.make_page(world.owner, "Big", "<p>big</p>", binary=False)
    fake_live.too_large = True
    api_client.force_authenticate(user=world.owner.user)
    url = f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{page.id}/description/"
    response = api_client.get(url)
    assert response.status_code == 200 and b"".join(response.streaming_content) == b""
    page.refresh_from_db()
    assert page.description_binary is None  # live converts it itself, as in stock Plane


def test_first_load_fails_rather_than_let_live_convert_when_the_converter_is_down(world, fake_live, api_client):
    page = world.make_page(world.owner, "Fresh", "<p>x</p>", binary=False)
    fake_live.down = True
    api_client.force_authenticate(user=world.owner.user)
    url = f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{page.id}/description/"
    response = api_client.get(url)
    assert (response.status_code, response.json()["code"]) == (503, "live_unavailable")


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


def test_first_load_builds_the_document_from_the_html(world, fake_live, api_client):
    page = world.make_page(world.owner, "Fresh", "<p>One</p><p>Two</p>", binary=False)
    before = page.updated_at
    api_client.force_authenticate(user=world.owner.user)
    url = f"/api/workspaces/{world.workspace.slug}/projects/{world.project.id}/pages/{page.id}/description/"
    response = api_client.get(url)
    assert response.status_code == 200
    body = b"".join(response.streaming_content)
    page.refresh_from_db()
    assert body == bytes(page.description_binary) and body
    assert ydoc.body_signature(body) == ydoc.body_signature(doc_for(["One", "Two"]))
    assert page.updated_at == before  # building the document is not a change
    assert fake_live.calls == ["<p>One</p><p>Two</p>"]


def test_editor_load_fails_rather_than_read_a_page_an_edit_still_holds(world, api_client):
    api_client.force_authenticate(user=world.owner.user)
    with patch("plane.halo.lease.WAIT_SECONDS", 0.3), lease.edit_lease(world.public.id):
        response = api_client.get(_description_url(world))
    assert (response.status_code, response.json()["code"]) == (503, "edit_in_progress")


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


def test_editor_load_waits_for_a_running_api_edit(world, api_client):
    api_client.force_authenticate(user=world.owner.user)
    held = threading.Event()

    def hold():
        with lease.edit_lease(world.public.id):
            held.set()
            time.sleep(0.6)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(2)
    started = time.monotonic()
    response = api_client.get(_description_url(world))
    waited = time.monotonic() - started
    thread.join()
    assert response.status_code == 200 and waited >= 0.4


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
    no_celery.assert_not_called()


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
    assert (copy.name, copy.description_html, copy.description_binary) == ("Plan (Copy)", ORIGINAL, None)
    assert ProjectPage.objects.filter(page=copy, project=world.project).exists()
    assert PageBeingCopied.objects.filter(page_id=copy.id).exists()
    queued = [call.kwargs for call in no_celery.call_args_list]
    # Plane's copy task is not queued itself: Halo's finish_page_copy runs it, then removes the mark.
    assert not any("entity_name" in kwargs for kwargs in queued)
    copy_kwargs = next(kwargs for kwargs in queued if set(kwargs) == {"page_id", "project_id", "slug", "user_id"})
    with patch("plane.halo.tasks.copy_s3_objects_of_description_and_assets") as plane_copy:
        finish_page_copy(**copy_kwargs)
    plane_copy.assert_called_once_with(entity_name="PAGE", entity_identifier=copy.id, project_id=world.project.id,
                                       slug=world.workspace.slug, user_id=world.owner.user.id)
    assert not PageBeingCopied.objects.filter(page_id=copy.id).exists()


def test_a_failed_copy_task_still_removes_the_mark(world):
    from django.utils import timezone

    from plane.halo.models import PageBeingCopied
    from plane.halo.tasks import finish_page_copy

    PageBeingCopied.objects.create(page_id=world.public.id, since=timezone.now())
    with patch("plane.halo.tasks.copy_s3_objects_of_description_and_assets", side_effect=RuntimeError("s3")):
        with pytest.raises(RuntimeError):
            finish_page_copy(page_id=world.public.id, project_id=world.project.id, slug=world.workspace.slug,
                             user_id=world.owner.user.id)
    assert not PageBeingCopied.objects.filter(page_id=world.public.id).exists()


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


def test_editor_load_of_a_page_being_copied_waits_then_fails(world, api_client):
    from django.utils import timezone

    from plane.halo.models import PageBeingCopied

    api_client.force_authenticate(user=world.owner.user)
    PageBeingCopied.objects.create(page_id=world.public.id, since=timezone.now())
    with patch("plane.halo.lease.WAIT_SECONDS", 0.3):
        response = api_client.get(_description_url(world))
    assert (response.status_code, response.json()["code"]) == (503, "page_not_ready")


def test_editor_load_waits_until_the_copy_task_ran(world, api_client):
    api_client.force_authenticate(user=world.owner.user)
    with patch("plane.halo.views.being_copied", side_effect=[True, True, False]) as copying, \
            patch("plane.halo.views.time.sleep") as sleep:
        response = api_client.get(_description_url(world))
    assert response.status_code == 200
    assert copying.call_count == 3 and sleep.call_count == 2


def test_a_copy_mark_older_than_an_hour_is_ignored(world, fake_live, api_client):
    """A worker that died mid-copy never removes its mark; after an hour the page is usable again."""
    from datetime import timedelta

    from django.utils import timezone

    from plane.halo.models import PageBeingCopied

    PageBeingCopied.objects.create(page_id=world.public.id, since=timezone.now() - timedelta(hours=2))
    api_client.force_authenticate(user=world.owner.user)
    assert api_client.get(_description_url(world)).status_code == 200
    url = f"{world.base}{world.public.id}/"
    stamp = world.owner.client.get(url).json()["updated_at"]
    response = world.owner.client.patch(url, {"description_html": "<p>new</p>", "expected_updated_at": stamp},
                                        format="json")
    assert response.status_code == 200, response.content

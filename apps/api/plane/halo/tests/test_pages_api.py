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
from plane.halo.tests.conftest import live_channel

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


def test_editor_save_that_missed_an_api_edit_is_merged(world, fake_live, api_client):
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
    with patch("plane.halo.live.publish_update", side_effect=lambda page_id, update: published.append(update) or 1):
        response = api_client.patch(
            _description_url(world),
            {
                "description_binary": base64.b64encode(stale.get_update()).decode(),
                "description_html": "<p>Alpha typed</p><p>Beta</p><p>Gamma</p>",
            },
            format="json",
        )
    assert response.status_code == 200, response.content
    saved = Doc()
    saved.apply_update(bytes(Page.objects.get(pk=world.public.pk).description_binary))
    assert str(saved.get(ydoc.BODY, type=XmlFragment)) == (
        "<paragraph>Alpha typed</paragraph><paragraph>Beta by AURA</paragraph><paragraph>Gamma</paragraph>"
    )
    assert published  # the editor that saved gets the part it missed
    stale.apply_update(published[0])
    assert str(stale.get(ydoc.BODY, type=XmlFragment)) == str(saved.get(ydoc.BODY, type=XmlFragment))


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

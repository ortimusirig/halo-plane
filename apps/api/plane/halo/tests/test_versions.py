# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta

import pytest
from django.utils import timezone

from plane.db.models import PageVersion
from plane.halo import versions


def _old(page):
    return {
        "description_html": page.description_html,
        "description_binary": page.description_binary,
        "description_json": page.description_json,
    }


@pytest.mark.django_db
def test_edit_keeps_old_content_before_and_new_content_after(world):
    page = world.public
    old = _old(page)
    page.description_html = "<p>Alpha</p><p>New</p><p>Gamma</p>"
    page.save()
    before, after, _ = versions.record_edit(page, world.owner.user.id, old)
    assert before.description_html == "<p>Alpha</p><p>Beta</p><p>Gamma</p>"
    assert after.description_html == page.description_html
    assert before.last_saved_at < after.last_saved_at
    assert before.owned_by_id == after.owned_by_id == world.owner.user.id


@pytest.mark.django_db
def test_before_is_always_a_new_marked_version(world):
    """An existing unmarked version could still be rewritten by Plane's version task, so it is never reused."""
    page = world.public
    kept = PageVersion.objects.create(
        page=page,
        workspace=page.workspace,
        owned_by=world.member.user,
        last_saved_at=timezone.now() - timedelta(hours=1),
        description_html=page.description_html,
    )
    old = _old(page)
    page.description_html = "<p>Changed</p>"
    page.save()
    before, after, _ = versions.record_edit(page, world.owner.user.id, old)
    assert before.id != kept.id and before.sub_pages_data == {"halo_api_edit": True}
    assert PageVersion.objects.filter(page=page).count() == 3


@pytest.mark.django_db
def test_cap_drops_the_oldest_beyond_twenty(world):
    page = world.public
    start = timezone.now() - timedelta(days=1)
    for minute in range(20):
        PageVersion.objects.create(
            page=page,
            workspace=page.workspace,
            owned_by=world.owner.user,
            last_saved_at=start + timedelta(minutes=minute),
            description_html=f"<p>{minute}</p>",
        )
    old = _old(page)
    page.description_html = "<p>Changed</p>"
    page.save()
    before, after, _ = versions.record_edit(page, world.owner.user.id, old)
    left = PageVersion.objects.filter(page=page)
    assert left.count() == versions.MAX_VERSIONS
    assert {before.id, after.id} <= set(left.values_list("id", flat=True))
    assert not left.filter(description_html="<p>0</p>").exists()
    assert not left.filter(description_html="<p>1</p>").exists()



@pytest.mark.django_db
def test_the_cap_needs_no_task_broker(world):
    """Pruning is a soft delete in the database alone, so a broker failure cannot undo the edit around it."""
    from unittest.mock import patch

    page = world.public
    start = timezone.now() - timedelta(days=1)
    for minute in range(20):
        PageVersion.objects.create(page=page, workspace=page.workspace, owned_by=world.owner.user,
                                   last_saved_at=start + timedelta(minutes=minute), description_html=f"<p>{minute}</p>")
    old = _old(page)
    page.description_html = "<p>Changed</p>"
    page.save()
    with patch("celery.app.task.Task.delay", side_effect=RuntimeError("broker down")):
        versions.record_edit(page, world.owner.user.id, old)
    assert PageVersion.objects.filter(page=page).count() == versions.MAX_VERSIONS
    assert PageVersion.all_objects.filter(page=page, deleted_at__isnull=False).count() == 2


@pytest.mark.django_db
def test_pruned_versions_are_reported(world):
    """Plan review round 20: the 20-version limit is Plane's own, and every version an edit prunes is named."""
    page = world.public
    start = timezone.now() - timedelta(days=1)
    made = [PageVersion.objects.create(page=page, workspace=page.workspace, owned_by=world.owner.user,
                                       last_saved_at=start + timedelta(minutes=minute),
                                       description_html=f"<p>{minute}</p>")
            for minute in range(20)]
    old = _old(page)
    page.description_html = "<p>Changed</p>"
    page.save()
    _, _, pruned = versions.record_edit(page, world.owner.user.id, old)
    assert sorted(pruned) == sorted([str(made[0].id), str(made[1].id)])


@pytest.mark.django_db
def test_no_pruning_reports_an_empty_list(world):
    page = world.public
    old = _old(page)
    page.description_html = "<p>Changed</p>"
    page.save()
    _, _, pruned = versions.record_edit(page, world.owner.user.id, old)
    assert pruned == []

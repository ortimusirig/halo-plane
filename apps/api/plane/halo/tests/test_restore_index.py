# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only

"""Restore indexing runs real Plane tasks and PageLog queries, with only the broker/live stubbed."""

from unittest.mock import patch

import pytest
from celery.exceptions import Retry
from django.utils import timezone
from pycrdt import Doc, XmlElement, XmlFragment

from plane.bgtasks.page_transaction_task import page_transaction
from plane.db.models import Page, PageLog
from plane.halo import ydoc
from plane.halo.models import PageTextBehind

pytestmark = pytest.mark.django_db

MENTION = "10000000-0000-4000-8000-000000000001"
LATER = "10000000-0000-4000-8000-000000000002"
TARGET = "20000000-0000-4000-8000-000000000001"


def mention_html(transaction):
    return (f'<mention-component id="{transaction}" entity_name="issue" '
            f'entity_identifier="{TARGET}"></mention-component>')


@pytest.fixture
def indexing_jobs():
    # Preserve task identity, then execute actual indexing task bodies as a worker would.
    queued = []

    def enqueue(task, *args, **kwargs):
        queued.append((task, args, kwargs))

    with patch("celery.app.task.Task.delay", new=enqueue), \
            patch("plane.halo.live.is_open", return_value=False), \
            patch("plane.halo.edit._publish", return_value=0), \
            patch("plane.halo.edit.threading.Timer"):
        def take():
            jobs = [(task, args, kwargs) for task, args, kwargs in queued
                    if task.name == page_transaction.name or task.name.startswith("plane.halo.tasks.")]
            queued.clear()
            return jobs
        yield take


def run_jobs(jobs):
    for task, args, kwargs in jobs:
        task(*args, **kwargs)


def edit(world, **payload):
    url = f"{world.base}{world.public.id}/"
    payload["expected_updated_at"] = world.owner.client.get(url).json()["updated_at"]
    response = world.owner.client.patch(url, payload, format="json")
    assert response.status_code == 200, response.content
    return response.json()


def removed_mention(world, indexing_jobs):
    document = Doc()
    document.apply_update(bytes(world.public.description_binary))
    with document.transaction():
        document.get(ydoc.BODY, type=XmlFragment).children.append(
            XmlElement("mention-component", {"id": MENTION, "entity_name": "issue", "entity_identifier": TARGET}))
    html = "<p>Before removal</p>" + mention_html(MENTION)
    Page.objects.filter(pk=world.public.pk).update(description_html=html, description_binary=document.get_update())
    page_transaction(new_description_html=html, old_description_html=None, page_id=world.public.id)
    row = PageLog.objects.get(page_id=world.public.id, transaction=MENTION)
    result = edit(world, description_html="<p>Removed</p>", allow_removal=True)
    run_jobs(indexing_jobs())
    row.refresh_from_db()
    assert row.deleted_at is not None
    return result["before_version_id"], row


def test_restore_reactivates_removed_work_item_in_api_response(world, fake_live, indexing_jobs):
    version, row = removed_mention(world, indexing_jobs)
    edit(world, restore_version_id=version)
    jobs = indexing_jobs()
    run_jobs(jobs)
    url = f"{world.base}{world.public.id}/"
    assert world.owner.client.get(url).json()["work_item_ids"] == [TARGET]
    row.refresh_from_db()
    assert row.deleted_at is None
    run_jobs(jobs)  # retries do not create duplicates
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION).count() == 1


def test_delayed_restore_does_not_resurrect_a_later_removed_mention(world, fake_live, indexing_jobs):
    version, row = removed_mention(world, indexing_jobs)
    edit(world, restore_version_id=version)
    delayed = indexing_jobs()
    edit(world, description_html="<p>Removed again</p>", allow_removal=True)
    run_jobs(indexing_jobs())
    run_jobs(delayed)
    row.refresh_from_db()
    assert row.deleted_at is not None
    assert world.owner.client.get(f"{world.base}{world.public.id}/").json()["work_item_ids"] == []


def test_restore_reconciliation_never_changes_another_pages_rows(world, fake_live, indexing_jobs):
    version, row = removed_mention(world, indexing_jobs)
    other = world.make_page(world.owner, "Unrelated", mention_html(LATER))
    other_deleted = PageLog.all_objects.create(page=other, workspace=world.workspace, transaction=MENTION,
                                               entity_name="issue", entity_identifier=TARGET,
                                               deleted_at=row.deleted_at)
    page_transaction(new_description_html=mention_html(LATER), old_description_html=None, page_id=other.id)
    other_active = PageLog.objects.get(page_id=other.id, transaction=LATER)
    # A later mention is indexed before undo removes it. Its ID may also exist in a copy.
    Page.objects.filter(pk=world.public.pk).update(description_html="<p>Removed</p>" + mention_html(LATER))
    page_transaction(new_description_html=mention_html(LATER), old_description_html=None, page_id=world.public.id)
    edit(world, restore_version_id=version)
    run_jobs(indexing_jobs())
    other_deleted.refresh_from_db()
    other_active.refresh_from_db()
    assert other_deleted.deleted_at == row.deleted_at
    assert other_active.deleted_at is None
    assert not PageLog.objects.filter(page_id=world.public.id, transaction=LATER).exists()


@pytest.mark.parametrize("hard_delete", [False, True])
def test_restore_job_ignores_deleted_or_missing_pages(world, fake_live, indexing_jobs, hard_delete):
    version, row = removed_mention(world, indexing_jobs)
    edit(world, restore_version_id=version)
    delayed = indexing_jobs()
    Page.objects.filter(pk=world.public.pk).delete(soft=not hard_delete)
    run_jobs(delayed)
    assert not PageLog.objects.filter(page_id=world.public.id).exists()


@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("kept", [False, True])
def test_restore_waits_for_complete_text_then_indexes_current_mentions(
        world, fake_live, indexing_jobs, merged, kept):
    version, row = removed_mention(world, indexing_jobs)
    edit(world, restore_version_id=version)
    delayed = indexing_jobs()
    PageTextBehind.objects.create(page_id=world.public.id, since=timezone.now(), merged=merged)
    # HTML still has the restored mention, but is no longer authoritative after a partial editor save.
    with pytest.raises(Retry):
        run_jobs(delayed)
    row.refresh_from_db()
    assert row.deleted_at is not None
    Page.objects.filter(pk=world.public.pk).update(
        description_html="<p>Complete save</p>" + (mention_html(MENTION) if kept else ""))
    PageTextBehind.objects.filter(page_id=world.public.id).delete()
    run_jobs(delayed)
    assert PageLog.objects.filter(page_id=world.public.id, transaction=MENTION).exists() is kept

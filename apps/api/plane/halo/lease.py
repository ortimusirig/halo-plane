# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Coordination between API edits and the editors that load and save a page (Redis keys per page).

- An edit lease: one API edit of a page at a time; an editor's load waits for it, and fails rather than load
  a page an edit is still writing.
- A "text behind" mark: set when an editor's save had to be merged with an API edit it never received; the
  stored HTML then lags the document until that editor saves again, and API edits wait for that.
"""

import time
import uuid
from contextlib import contextmanager

from plane.settings.redis import redis_instance

LEASE_SECONDS = 30  # longest an edit may hold a page; a crashed edit frees it after this
WAIT_SECONDS = 15  # longest an editor's load waits for an edit (live gives up on the API after 20 s)
TEXT_BEHIND_SECONDS = 7 * 24 * 3600


class PageBusy(Exception):
    pass


def key(page_id) -> str:
    return f"halo:page-edit:{page_id}"


def text_behind_key(page_id) -> str:
    return f"halo:text-behind:{page_id}"


@contextmanager
def edit_lease(page_id):
    client, token = redis_instance(), uuid.uuid4().hex
    if not client.set(key(page_id), token, nx=True, ex=LEASE_SECONDS):
        raise PageBusy(str(page_id))
    try:
        yield
    finally:
        # Free it only if it is still ours (it may have expired and been taken by another edit).
        client.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0",
            1,
            key(page_id),
            token,
        )


def wait_until_free(page_id, seconds: float | None = None) -> bool:
    """True once no edit holds the page; False if one still does after seconds (default WAIT_SECONDS)."""
    client, deadline = redis_instance(), time.monotonic() + (WAIT_SECONDS if seconds is None else seconds)
    while client.exists(key(page_id)):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def mark_text_behind(page_id) -> None:
    redis_instance().set(text_behind_key(page_id), "1", ex=TEXT_BEHIND_SECONDS)


def clear_text_behind(page_id) -> None:
    redis_instance().delete(text_behind_key(page_id))


def text_behind(page_id) -> bool:
    return bool(redis_instance().exists(text_behind_key(page_id)))

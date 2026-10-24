# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One API edit of a page at a time, and editors that load a page wait for it (Redis key per page)."""

import time
import uuid
from contextlib import contextmanager

from plane.settings.redis import redis_instance

LEASE_SECONDS = 30  # longest an edit may hold a page; a crashed edit frees it after this
WAIT_SECONDS = 5  # longest an editor's load waits for an edit to finish


class PageBusy(Exception):
    pass


def key(page_id) -> str:
    return f"halo:page-edit:{page_id}"


@contextmanager
def edit_lease(page_id):
    client, token = redis_instance(), uuid.uuid4().hex
    if not client.set(key(page_id), token, nx=True, ex=LEASE_SECONDS):
        raise PageBusy(str(page_id))
    try:
        yield
    finally:
        # Free it only if it is still ours (it may have expired and been taken by another edit).
        client.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0",
                    1, key(page_id), token)


def wait_until_free(page_id, seconds: float = WAIT_SECONDS) -> None:
    client, deadline = redis_instance(), time.monotonic() + seconds
    while client.exists(key(page_id)) and time.monotonic() < deadline:
        time.sleep(0.1)

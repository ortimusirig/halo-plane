# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One API edit of a page at a time, and editors that load the page wait a moment for it (a Redis key per
page)."""

import time
import uuid
from contextlib import contextmanager

from plane.settings.redis import redis_instance
from plane.utils.exception_logger import log_exception

LEASE_SECONDS = 60  # longer than any edit (two 10 s conversions at most); a crashed edit frees it after this
WAIT_SECONDS = 15  # longest an editor's load waits for an edit (live gives up on the API after 20 s)


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
        # Free it only if it is still ours (it may have expired and been taken by another edit). When Redis fails
        # here the edit is already saved: logged, and the key expires by itself.
        try:
            client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0",
                1,
                key(page_id),
                token,
            )
        except Exception as error:
            log_exception(error)


def wait_until_free(page_id, seconds: float | None = None) -> bool:
    """True once no edit holds the page; False if one still does after seconds (default WAIT_SECONDS)."""
    client, deadline = redis_instance(), time.monotonic() + (WAIT_SECONDS if seconds is None else seconds)
    while client.exists(key(page_id)):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True

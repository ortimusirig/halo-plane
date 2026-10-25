# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One API edit of a page at a time, and editors that load the page wait a moment for it (a Redis key per
page)."""

import time
import uuid
from contextlib import contextmanager

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from plane.settings.redis import redis_instance
from plane.utils.exception_logger import log_exception

LEASE_SECONDS = 60  # longer than any edit (two 10 s conversions at most); a crashed edit frees it after this
WAIT_SECONDS = 15  # longest an editor's load waits for an edit (capped by the load's budget, views)
SOCKET_SECONDS = 1.0  # Halo's Redis calls never wait longer than this for a reply

_client = None


def bounded(client: redis.Redis) -> redis.Redis:
    """A client for the same Redis as client (Plane's settings, SSL included), whose connects and replies time
    out after SOCKET_SECONDS: a Redis that stops answering must not hold up a page load."""
    pool = client.connection_pool
    kwargs = {
        **pool.connection_kwargs,
        "socket_timeout": SOCKET_SECONDS,
        "socket_connect_timeout": SOCKET_SECONDS,
        "retry": Retry(NoBackoff(), 0),  # no retries, whatever a newer redis-py's default (6.x retries thrice)
    }
    return redis.Redis(connection_pool=redis.ConnectionPool(connection_class=pool.connection_class, **kwargs))


def redis_client() -> redis.Redis:
    """Halo's Redis client (leases, open-page probes, live updates), bounded as above."""
    global _client
    if _client is None:
        _client = bounded(redis_instance())
    return _client


class PageBusy(Exception):
    pass


def key(page_id) -> str:
    return f"halo:page-edit:{page_id}"


@contextmanager
def edit_lease(page_id):
    client, token = redis_client(), uuid.uuid4().hex
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
    client, deadline = redis_client(), time.monotonic() + (WAIT_SECONDS if seconds is None else seconds)
    while client.exists(key(page_id)):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True

# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Halo's live service (Plane's collaborative editor server), reached the ways Plane itself uses.

convert_html calls live's own converter, as Plane's copy_s3_object task does. Live keeps an open page in memory
and talks to other live servers on the Redis channel hocuspocus:<page_id> (@hocuspocus/extension-redis 2.15.2,
default prefix): a sync update published there reaches every open editor of the page, and a server that holds
the page answers an awareness query.
"""

import base64
import os
import time
from urllib.parse import urljoin

import redis
import requests

from plane.settings.redis import redis_instance

LIVE_URL = os.environ.get("HALO_LIVE_URL", "http://live:3000/live/")
CHANNEL_PREFIX = "hocuspocus"
IDENTIFIER = b"halo-api"  # live skips only messages that carry its own identifier
SYNC = 0  # Hocuspocus MessageType.Sync
YJS_UPDATE = 2  # y-protocols messageYjsUpdate
QUERY_AWARENESS = 3  # Hocuspocus MessageType.QueryAwareness
PROBE_SECONDS = float(os.environ.get("HALO_LIVE_PROBE_SECONDS", "0.5"))


class LiveUnavailable(Exception):
    pass


class LiveTooLarge(LiveUnavailable):
    """Live refused the HTML as too large (its request limit is express.json's default, 100 kB)."""


def convertible(html: str) -> str:
    """HTML that live's converter takes: it refuses empty text and text without a tag (DocumentController's
    schema), which Plane's sanitizer and older pages allow; such text becomes one paragraph."""
    if not html or not html.strip():
        return "<p></p>"
    return html if "<" in html and ">" in html else f"<p>{html}</p>"


def convert_html(html: str) -> tuple[bytes, dict]:
    """Live's conversion of HTML into the page editor's Yjs document and ProseMirror JSON."""
    try:
        reply = requests.post(
            urljoin(LIVE_URL, "convert-document/"),
            json={"description_html": convertible(html), "variant": "document"},
            timeout=10,
        )
    except requests.RequestException as error:
        raise LiveUnavailable(str(error)) from error
    if reply.status_code == 413:
        raise LiveTooLarge("the page is larger than live's converter accepts (100 kB of HTML)")
    if reply.status_code != 200:
        raise LiveUnavailable(f"live converter answered HTTP {reply.status_code}")
    data = reply.json()
    return base64.b64decode(data["description_binary"]), data["description_json"]


def channel(page_id) -> str:
    return f"{CHANNEL_PREFIX}:{page_id}"


def is_open(page_id) -> bool:
    """True while a live server holds the page, that is while someone has it open in Halo.

    Live stays subscribed to a page's channel after the page closes (the extension unsubscribes only when the
    page is still in memory a second after the last editor leaves), so a subscriber count only says "never
    opened since live started". A server that holds the page answers an awareness query; one that does not
    stays silent, so silence for PROBE_SECONDS means closed.
    """
    client = redis_instance()
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    try:
        if not any(count for _, count in client.pubsub_numsub(channel(page_id))):
            return False
        pubsub.subscribe(channel(page_id))
        pubsub.get_message(timeout=1)  # the subscribe confirmation, before asking
        client.publish(channel(page_id), query_awareness_message(page_id))
        deadline = time.monotonic() + PROBE_SECONDS
        while (left := deadline - time.monotonic()) > 0:
            message = pubsub.get_message(timeout=left)
            if message and message["type"] == "message" and not _from_us(message["data"]):
                return True
        return False
    except redis.RedisError as error:
        raise LiveUnavailable(str(error)) from error
    finally:
        pubsub.close()


def _from_us(data: bytes) -> bool:
    return data[1 : 1 + data[0]] == IDENTIFIER if data else False


def _var_uint(number: int) -> bytes:
    out = bytearray()
    while True:
        byte = number & 0x7F
        number >>= 7
        if number:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _var_bytes(data: bytes) -> bytes:
    return _var_uint(len(data)) + data


def _message(page_id, body: bytes) -> bytes:
    return bytes([len(IDENTIFIER)]) + IDENTIFIER + _var_bytes(str(page_id).encode()) + body


def sync_update_message(page_id, update: bytes) -> bytes:
    return _message(page_id, _var_uint(SYNC) + _var_uint(YJS_UPDATE) + _var_bytes(update))


def query_awareness_message(page_id) -> bytes:
    return _message(page_id, _var_uint(QUERY_AWARENESS))


def publish_update(page_id, update: bytes) -> int:
    """Send a Yjs update to every open editor of the page; returns the number of live servers reached."""
    return redis_instance().publish(channel(page_id), sync_update_message(page_id, update))

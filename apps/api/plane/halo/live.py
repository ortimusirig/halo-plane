# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Halo's live service (Plane's collaborative editor server), reached the ways Plane itself uses.

convert_html calls live's own converter, as Plane's copy_s3_object task does. Live keeps an open page in memory
and listens on the Redis channel hocuspocus:<page_id> while it is open (@hocuspocus/extension-redis 2.15.2,
default prefix). A sync update published there reaches every open editor of the page.
"""

import base64
import os
from urllib.parse import urljoin

import redis
import requests

from plane.settings.redis import redis_instance

LIVE_URL = os.environ.get("HALO_LIVE_URL", "http://live:3000/live/")
CHANNEL_PREFIX = "hocuspocus"
IDENTIFIER = b"halo-api"  # live skips only messages that carry its own identifier
SYNC = 0  # Hocuspocus MessageType.Sync
YJS_UPDATE = 2  # y-protocols messageYjsUpdate


class LiveUnavailable(Exception):
    pass


def convert_html(html: str) -> tuple[bytes, dict]:
    """Live's conversion of HTML into the page editor's Yjs document and ProseMirror JSON."""
    try:
        reply = requests.post(
            urljoin(LIVE_URL, "convert-document/"),
            json={"description_html": html, "variant": "document"},
            timeout=15,
        )
    except requests.RequestException as error:
        raise LiveUnavailable(str(error)) from error
    if reply.status_code != 200:
        raise LiveUnavailable(f"live converter answered HTTP {reply.status_code}")
    data = reply.json()
    return base64.b64decode(data["description_binary"]), data["description_json"]


def channel(page_id) -> str:
    return f"{CHANNEL_PREFIX}:{page_id}"


def is_open(page_id) -> bool:
    """True while a live server holds the page, that is while someone has it open in Halo."""
    try:
        return any(count for _, count in redis_instance().pubsub_numsub(channel(page_id)))
    except redis.RedisError as error:
        raise LiveUnavailable(str(error)) from error


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


def sync_update_message(page_id, update: bytes) -> bytes:
    body = _var_bytes(str(page_id).encode()) + _var_uint(SYNC) + _var_uint(YJS_UPDATE) + _var_bytes(update)
    return bytes([len(IDENTIFIER)]) + IDENTIFIER + body


def publish_update(page_id, update: bytes) -> int:
    """Send a Yjs update to every open editor of the page; returns the number of live servers reached."""
    return redis_instance().publish(channel(page_id), sync_update_message(page_id, update))

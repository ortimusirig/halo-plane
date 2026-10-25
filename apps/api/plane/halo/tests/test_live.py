# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import base64
from unittest.mock import patch

import pytest
import redis
import requests

from plane.halo import live
from plane.halo.tests.conftest import live_channel
from plane.settings.redis import redis_instance

PAGE = "3f2b1c4d-0000-4000-8000-000000000001"


def test_message_is_a_hocuspocus_sync_update_with_our_identifier():
    message = live.sync_update_message(PAGE, b"\x01\x02\x03")
    assert message[:9] == bytes([8]) + b"halo-api"
    name = PAGE.encode()
    # varString(page id), varUint(0 Sync), varUint(2 messageYjsUpdate), varUint8Array(update)
    assert message[9:] == bytes([len(name)]) + name + bytes([0, 2, 3]) + b"\x01\x02\x03"


def test_long_update_length_is_a_var_uint():
    message = live.sync_update_message(PAGE, b"x" * 300)
    tail = message[9 + 1 + len(PAGE) + 2 :]
    assert tail[:2] == bytes([0xAC, 0x02])  # 300 in LEB128
    assert tail[2:] == b"x" * 300


def test_query_message_asks_live_for_the_page_awareness():
    name = PAGE.encode()
    assert live.query_awareness_message(PAGE) == bytes([8]) + b"halo-api" + bytes([len(name)]) + name + bytes([3])


def test_page_without_a_live_subscriber_is_closed_at_once():
    assert live.is_open(PAGE) is False


def test_page_held_by_live_is_open():
    with live_channel(PAGE):
        assert live.is_open(PAGE) is True


def test_page_live_no_longer_holds_is_closed_although_subscribed():
    # live keeps the channel subscribed after the last editor leaves; only an answer counts.
    with live_channel(PAGE, holds_page=False):
        assert redis_instance().pubsub_numsub(live.channel(PAGE))[0][1] >= 1
        assert live.is_open(PAGE) is False


def test_publish_update_reaches_the_channel():
    pubsub = redis_instance().pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(live.channel(PAGE))
    try:
        pubsub.get_message(timeout=1)
        assert live.publish_update(PAGE, b"\x00") == 1
        message = pubsub.get_message(timeout=2)
        assert message["data"] == live.sync_update_message(PAGE, b"\x00")
    finally:
        pubsub.close()


def test_convert_html_posts_to_live_and_decodes():
    class Reply:
        status_code = 200

        def json(self):
            return {"description_binary": base64.b64encode(b"yjs").decode(), "description_json": {"type": "doc"}}

    with patch("plane.halo.live.requests.post", return_value=Reply()) as post:
        assert live.convert_html("<p>x</p>") == (b"yjs", {"type": "doc"})
    assert post.call_args.args[0] == "http://live:3000/live/convert-document/"
    assert post.call_args.kwargs["json"] == {"description_html": "<p>x</p>", "variant": "document"}


@pytest.mark.parametrize("failure", [requests.ConnectionError("down"), None])
def test_convert_html_failure_is_live_unavailable(failure):
    class Reply:
        status_code = 500

    with patch("plane.halo.live.requests.post", side_effect=failure, return_value=Reply()):
        with pytest.raises(live.LiveUnavailable):
            live.convert_html("<p>x</p>")


@pytest.mark.parametrize("html,sent", [("Hello world", "<p>Hello world</p>"), ("", "<p></p>"), ("  ", "<p></p>"),
                                       ("a &gt; b", "<p>a &gt; b</p>"), ("<p>x</p>", "<p>x</p>")])
def test_text_without_a_tag_is_sent_to_live_as_a_paragraph(html, sent):
    """Live's converter refuses text without a tag (its DocumentController schema); Plane's sanitizer allows it."""

    class Reply:
        status_code = 200

        def json(self):
            return {"description_binary": base64.b64encode(b"yjs").decode(), "description_json": {"type": "doc"}}

    with patch("plane.halo.live.requests.post", return_value=Reply()) as post:
        live.convert_html(html)
    assert post.call_args.kwargs["json"]["description_html"] == sent



def test_halo_s_redis_calls_time_out_on_a_redis_that_does_not_answer():
    """Plane's client waits for ever; Halo's coordination may not hold up a page load."""
    import time

    from plane.halo import lease
    from plane.halo.tests.conftest import silent_redis

    assert lease.redis_client().connection_pool.connection_kwargs["socket_timeout"] == lease.SOCKET_SECONDS
    with silent_redis() as port:
        client = lease.bounded(redis.Redis(host="127.0.0.1", port=port))
        started = time.monotonic()
        with pytest.raises(redis.TimeoutError):
            client.exists("halo:page-edit:x")
        assert time.monotonic() - started < 3

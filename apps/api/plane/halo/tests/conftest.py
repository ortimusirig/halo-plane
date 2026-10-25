# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import re
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pycrdt import Doc, XmlElement, XmlFragment, XmlText
from rest_framework.test import APIClient

from plane.db.models import APIToken, Page, Project, ProjectMember, ProjectPage, WorkspaceMember
from plane.halo import ydoc
from plane.settings.redis import redis_instance
from plane.tests.conftest import api_client  # noqa: F401  Plane's own fixture
from plane.tests.factories import UserFactory, WorkspaceFactory

ADMIN, MEMBER, GUEST = 20, 15, 5


def doc_for(blocks):
    """Stand-in for live's converter: a paragraph per text, an imageComponent per ("image", src) (the
    editor's node names, as live's converter writes them)."""
    doc = Doc()
    body = doc.get(ydoc.BODY, type=XmlFragment)
    with doc.transaction():
        for block in blocks:
            if isinstance(block, tuple):
                body.children.append(XmlElement("imageComponent", {"src": block[1]}))
            else:
                body.children.append(XmlElement("paragraph", {})).children.append(XmlText()).insert(0, block)
    return doc.get_update()


def json_for(blocks):
    """Stand-in for live's ProseMirror JSON of the same blocks."""
    return {"type": "doc", "content": [
        {"type": "imageComponent", "attrs": {"src": block[1]}} if isinstance(block, tuple)
        else {"type": "paragraph", "content": [{"type": "text", "text": block}]}
        for block in blocks
    ]}


def paragraphs(html):
    """The blocks of simple test HTML: <p>text</p> and <image-component src="..."></image-component>."""
    blocks = []
    for text, src in re.findall(r'<p>(.*?)</p>|<image-component src="([^"]*)"', html):
        blocks.append(("image", src) if src else text)
    return blocks


@contextmanager
def live_channel(page_id, holds_page=True):
    """A stand-in live server on the page's Redis channel.

    Like live (@hocuspocus/extension-redis 2.15.2) it stays subscribed after a page closes; it answers a
    query from another server only while it holds the page (holds_page=True).
    """
    from plane.halo import live

    identity = b"live-test"
    pubsub = redis_instance().pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(live.channel(page_id))
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            message = pubsub.get_message(timeout=0.05)
            if message and holds_page and message["data"][1 : 1 + message["data"][0]] != identity:
                name = str(page_id).encode()
                reply = bytes([len(identity)]) + identity + bytes([len(name)]) + name + bytes([1, 1, 0])
                redis_instance().publish(live.channel(page_id), reply)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        pubsub.close()


@contextmanager
def silent_redis():
    """A Redis address that accepts connections and never answers, as a stalled server does."""
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    try:
        yield server.getsockname()[1]
    finally:
        server.close()


@pytest.fixture(autouse=True)
def no_celery():
    with patch("celery.app.task.Task.delay") as delay:
        yield delay


@pytest.fixture(autouse=True)
def clean_redis():
    redis_instance().flushdb()
    yield


@pytest.fixture
def fake_live():
    """Replace live's converter; a test sets .down or .too_large to make it fail."""
    from plane.halo.live import LiveTooLarge, LiveUnavailable

    state = SimpleNamespace(down=False, too_large=False, calls=[])

    def convert(html, timeout=None):
        state.calls.append(html)
        if state.down:
            raise LiveUnavailable("live is down")
        if state.too_large:
            raise LiveTooLarge("over 100 kB")
        return doc_for(paragraphs(html)), json_for(paragraphs(html))

    with patch("plane.halo.live.convert_html", side_effect=convert):
        yield state


def _person(email):
    user = UserFactory(email=email, username=email.split("@")[0])
    token = APIToken.objects.create(user=user, label="AURA (managed)", token=f"plane_api_{user.id.hex}")
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=token.token)
    return SimpleNamespace(user=user, client=client)


@pytest.fixture
def world(db):
    owner, member, admin, guest, outsider = (
        _person(f"{name}@applexus.com") for name in ("owner", "member", "admin", "guest", "outsider")
    )
    workspace = WorkspaceFactory(slug="halo-test", owner=admin.user)
    for person, role in ((owner, MEMBER), (member, MEMBER), (admin, ADMIN), (guest, GUEST), (outsider, MEMBER)):
        WorkspaceMember.objects.create(workspace=workspace, member=person.user, role=role)
    project = Project.objects.create(name="Pages", identifier="PG", workspace=workspace, guest_view_all_features=True)
    for person, role in ((owner, MEMBER), (member, MEMBER), (admin, ADMIN), (guest, GUEST)):
        ProjectMember.objects.create(project=project, workspace=workspace, member=person.user, role=role)

    def make_page(by, name, html, access=0, binary=True):
        page = Page.objects.create(
            workspace=workspace,
            name=name,
            owned_by=by.user,
            access=access,
            description_html=html,
            description_binary=doc_for(paragraphs(html)) if binary else None,
        )
        ProjectPage.objects.create(workspace=workspace, project=project, page=page)
        return page

    public = make_page(owner, "Plan", "<p>Alpha</p><p>Beta</p><p>Gamma</p>")
    private = make_page(admin, "Secret", "<p>Admin only</p>", access=1)
    return SimpleNamespace(
        owner=owner,
        member=member,
        admin=admin,
        guest=guest,
        outsider=outsider,
        workspace=workspace,
        project=project,
        public=public,
        private=private,
        make_page=make_page,
        base=f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/",
    )

# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from pycrdt import Doc, XmlElement, XmlFragment, XmlText

from plane.halo import ydoc


def page(*blocks, title=None):
    """A Yjs page as y-prosemirror writes it: blocks are (tag, attrs, [(text, attrs or None)])."""
    doc = Doc()
    body = doc.get(ydoc.BODY, type=XmlFragment)
    with doc.transaction():
        for tag, attrs, runs in blocks:
            element = body.children.append(XmlElement(tag, attrs))
            text = element.children.append(XmlText())
            for chunk, chunk_attrs in runs:
                text.insert(len(text), chunk, chunk_attrs)
        if title is not None:
            heading = doc.get(ydoc.TITLE, type=XmlFragment).children.append(XmlElement("heading", {"level": 1.0}))
            heading.children.append(XmlText()).insert(0, title)
    return doc.get_update()


def p(text, attrs=None):
    return ("paragraph", {}, [(text, attrs)])


def text_of(binary, fragment=ydoc.BODY):
    doc = Doc()
    doc.apply_update(binary)
    return str(doc.get(fragment, type=XmlFragment))


def test_changed_block_is_replaced_and_others_keep_their_identity():
    old = page(p("Alpha"), p("Beta"), p("Gamma"))
    new = page(p("Alpha"), p("Beta, edited"), p("Gamma"))
    edit = ydoc.edit_document(old, new_body=new)
    assert edit.changed
    assert text_of(edit.state) == text_of(new)
    # A copy that typed into Alpha and Gamma offline keeps that typing after merging the edit.
    client = Doc()
    client.apply_update(old)
    client_before = client.get_state()
    body = client.get(ydoc.BODY, type=XmlFragment)
    alpha, gamma = body.children[0].children[0], body.children[2].children[0]
    alpha.insert(len(alpha), " typed")
    gamma.insert(len(gamma), " offline")
    offline = client.get_update(client_before)
    client.apply_update(edit.update)
    server = Doc()
    server.apply_update(edit.state)
    server.apply_update(offline)
    assert str(client.get(ydoc.BODY, type=XmlFragment)) == str(server.get(ydoc.BODY, type=XmlFragment))
    assert str(server.get(ydoc.BODY, type=XmlFragment)) == (
        "<paragraph>Alpha typed</paragraph><paragraph>Beta, edited</paragraph><paragraph>Gamma offline</paragraph>"
    )


def test_formatting_numeric_attributes_and_unicode_are_copied_exactly():
    old = page(p("x"))
    new = page(
        ("heading", {"level": 2.0}, [("Plan ", None), ("now", {"bold": {}})]),
        p("cafe é \U0001f389 हिंदी end", {"link": {"href": "https://x.test"}}),
    )
    edit = ydoc.edit_document(old, new_body=new)
    assert ydoc.body_signature(edit.state) == ydoc.body_signature(new)
    doc = Doc()
    doc.apply_update(edit.state)
    assert doc.get(ydoc.BODY, type=XmlFragment).children[0].attributes["level"] == 2.0


def test_nested_and_inline_elements_are_copied():
    doc = Doc()
    body = doc.get(ydoc.BODY, type=XmlFragment)
    with doc.transaction():
        bullet = body.children.append(XmlElement("bulletList", {}))
        item = bullet.children.append(XmlElement("listItem", {}))
        para = item.children.append(XmlElement("paragraph", {}))
        para.children.append(XmlText()).insert(0, "Hi ")
        para.children.append(
            XmlElement("mention-component", {"id": "m1", "entity_name": "user_mention", "entity_identifier": "u1"})
        )
        para.children.append(XmlText()).insert(0, " there")
    new = doc.get_update()
    edit = ydoc.edit_document(page(p("old")), new_body=new)
    assert ydoc.body_signature(edit.state) == ydoc.body_signature(new)


def test_same_body_is_no_change():
    old = page(p("Alpha"), p("Beta"))
    edit = ydoc.edit_document(old, new_body=page(p("Alpha"), p("Beta")))
    assert not edit.changed
    assert edit.update == b""


def test_title_text_is_replaced_when_the_page_has_one():
    old = page(p("Alpha"), title="Old name")
    edit = ydoc.edit_document(old, name="New name")
    assert edit.changed
    assert "New name" in text_of(edit.state, ydoc.TITLE)
    assert "Old name" not in text_of(edit.state, ydoc.TITLE)
    assert text_of(edit.state) == text_of(old)


def test_page_without_title_fragment_keeps_it_empty():
    edit = ydoc.edit_document(page(p("Alpha")), name="New name")
    assert not edit.changed
    assert text_of(edit.state, ydoc.TITLE) == ""

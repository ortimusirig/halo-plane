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


def test_separate_edits_keep_the_unchanged_block_between_them():
    old = page(p("Alpha"), p("Beta"), p("Gamma"))
    new = page(p("Alpha 2"), p("Beta"), p("Gamma 2"))
    client = Doc()
    client.apply_update(old)
    before = client.get_state()
    beta = client.get(ydoc.BODY, type=XmlFragment).children[1].children[0]
    beta.insert(len(beta), " offline")
    offline = client.get_update(before)
    edit = ydoc.edit_document(old, new_body=new)
    server = Doc()
    server.apply_update(edit.state)
    server.apply_update(offline)
    assert str(server.get(ydoc.BODY, type=XmlFragment)) == (
        "<paragraph>Alpha 2</paragraph><paragraph>Beta offline</paragraph><paragraph>Gamma 2</paragraph>"
    )


def test_block_ids_from_the_browser_editor_do_not_make_blocks_differ():
    # The browser's UniqueID extension stamps an id on every block; live's converter does not.
    old = page(("paragraph", {"id": "b1"}, [("Alpha", None)]), ("paragraph", {"id": "b2"}, [("Beta", None)]))
    edit = ydoc.edit_document(old, new_body=page(p("Alpha"), p("Beta, edited")))
    doc = Doc()
    doc.apply_update(edit.state)
    blocks = doc.get(ydoc.BODY, type=XmlFragment).children
    assert blocks[0].attributes["id"] == "b1"  # kept, not replaced
    assert blocks[1].attributes["id"] == "b2"  # edited in place, so it keeps its id too
    assert "".join(chunk for chunk, _ in blocks[1].children[0].diff()) == "Beta, edited"


def test_offline_typing_in_the_edited_paragraph_survives():
    old = page(p("Alpha"), p("The plan is short"))
    client = Doc()
    client.apply_update(old)
    before = client.get_state()
    text = client.get(ydoc.BODY, type=XmlFragment).children[1].children[0]
    text.insert(len(text), " and clear")  # typed offline at the end of the paragraph the API edits
    offline = client.get_update(before)
    edit = ydoc.edit_document(old, new_body=page(p("Alpha"), p("The new plan is short")))
    server = Doc()
    server.apply_update(edit.state)
    server.apply_update(offline)
    client.apply_update(edit.update)
    assert str(server.get(ydoc.BODY, type=XmlFragment)) == str(client.get(ydoc.BODY, type=XmlFragment))
    assert str(server.get(ydoc.BODY, type=XmlFragment)) == (
        "<paragraph>Alpha</paragraph><paragraph>The new plan is short and clear</paragraph>"
    )


def test_attribute_and_formatting_changes_keep_the_element():
    old = page(("heading", {"level": 2.0, "id": "h1"}, [("Plan", None)]), p("plain words"))
    new = page(("heading", {"level": 3.0}, [("Plan", None)]), p("plain words", {"bold": {}}))
    edit = ydoc.edit_document(old, new_body=new)
    assert ydoc.body_signature(edit.state) == ydoc.body_signature(new)
    doc = Doc()
    doc.apply_update(edit.state)
    heading = doc.get(ydoc.BODY, type=XmlFragment).children[0]
    assert (heading.attributes["level"], heading.attributes["id"]) == (3.0, "h1")


def test_unicode_text_edits_land_at_the_right_place():
    old = page(p("café \U0001f389 हिं end"))
    new = page(p("café \U0001f389 X हिं end!"))
    edit = ydoc.edit_document(old, new_body=new)
    assert ydoc.body_signature(edit.state) == ydoc.body_signature(new)


def test_append_adds_blocks_and_deletes_nothing():
    old = page(p("Alpha"), p("Beta"))
    edit = ydoc.edit_document(old, new_body=page(p("Next")), append=True)
    assert text_of(edit.state) == "<paragraph>Alpha</paragraph><paragraph>Beta</paragraph><paragraph>Next</paragraph>"


def image(src):
    return ("image-component", {"src": src}, [])


def test_removed_parts_names_dropped_or_swapped_images():
    old = page(p("Alpha"), image("a1"), image("a2"))
    assert ydoc.removed_parts(old, page(p("Alpha"), image("a1"), image("a2"), p("More"))) == []
    assert ydoc.removed_parts(old, page(p("Alpha"), image("a1"), image("zz"))) == ["image-component a2"]
    assert ydoc.removed_parts(old, page(p("Alpha"))) == ["image-component a1", "image-component a2"]


def test_missing_from_is_none_for_a_save_that_knows_everything():
    stored = page(p("Alpha"))
    editor = Doc()
    editor.apply_update(stored)
    text = editor.get(ydoc.BODY, type=XmlFragment).children[0].children[0]
    text.insert(len(text), " typed")
    assert ydoc.missing_from(editor.get_update(), stored) is None


def test_missing_from_merges_an_api_edit_an_editor_never_received():
    base = page(p("Alpha"), p("Beta"))
    editor = Doc()
    editor.apply_update(base)
    text = editor.get(ydoc.BODY, type=XmlFragment).children[0].children[0]
    text.insert(len(text), " typed")  # the editor's save, made without the API edit below
    stale = editor.get_update()
    stored = ydoc.edit_document(base, new_body=page(p("Alpha"), p("Beta by AURA"))).state
    union, missing = ydoc.missing_from(stale, stored)
    assert text_of(union) == "<paragraph>Alpha typed</paragraph><paragraph>Beta by AURA</paragraph>"
    editor.apply_update(missing)
    assert str(editor.get(ydoc.BODY, type=XmlFragment)) == text_of(union)


def test_missing_from_catches_a_pure_deletion():
    base = page(p("Alpha"), p("Beta"))
    stored = ydoc.edit_document(base, new_body=page(p("Alpha"))).state
    union, _ = ydoc.missing_from(base, stored)
    assert text_of(union) == "<paragraph>Alpha</paragraph>"

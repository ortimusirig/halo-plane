# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Change a page's Yjs document so that every copy of it can merge the change.

Plane's editor keeps a page as a Yjs document (body in fragment "default", title in "title"), and browsers keep
their own copy. A change must therefore be an update on that same document, never a new document. The new
document is laid over the old one node by node (sequence diffs of children, character diffs of text): nodes
that stay keep their identity, and a changed paragraph is edited in place, so typing in it on another copy
merges. Nodes are compared without the "id" attribute that the browser editor's UniqueID extension adds and
live's converter never produces.
"""

import difflib
import itertools
import json
from collections import Counter
from typing import NamedTuple

from pycrdt import Doc, XmlElement, XmlFragment, XmlText

BODY = "default"
TITLE = "title"
EDITOR_ID = "id"  # UniqueID's block id (packages/editor unique-id extension)
# Page parts an edit must not drop by accident: Yjs elements are named after the editor's node types
# (packages/editor/src/core/constants/extension.ts), with the attributes that say which one each is.
PROTECTED_PARTS = {
    "imageComponent": ("image", ("src",)),
    "image": ("image", ("src",)),
    "mention": ("mention", ("entity_name", "entity_identifier")),
    "issue-embed-component": ("embedded work item", ("entity_identifier",)),
}


class DocumentEdit(NamedTuple):
    state: bytes  # the whole document after the change
    update: bytes  # only the change, for copies that already hold the old document
    changed: bool


def _load(binary: bytes) -> Doc:
    doc = Doc()
    doc.apply_update(bytes(binary))
    return doc


def _signature(node):
    if isinstance(node, XmlText):
        return ("text", json.dumps(node.diff(), sort_keys=True, default=str))
    attributes = {key: value for key, value in dict(node.attributes).items() if key != EDITOR_ID}
    return (
        node.tag,
        json.dumps(attributes, sort_keys=True, default=str),
        tuple(_signature(child) for child in node.children),
    )


def body_signature(binary: bytes) -> tuple:
    return tuple(_signature(block) for block in _load(binary).get(BODY, type=XmlFragment).children)


def _fill(target, source) -> None:
    """Copy source's children into the integrated, empty target."""
    for child in source.children:
        if isinstance(child, XmlText):
            text = target.children.append(XmlText())
            for chunk, attrs in child.diff():
                if isinstance(chunk, str):
                    text.insert(len(text), chunk, attrs)
                else:
                    text.insert_embed(len(text), chunk, attrs)
        else:
            _fill(target.children.append(XmlElement(child.tag, dict(child.attributes))), child)


def _insert_node(parent, index: int, node) -> None:
    if isinstance(node, XmlText):
        text = parent.children.insert(index, XmlText())
        for chunk, attrs in node.diff():
            if isinstance(chunk, str):
                text.insert(len(text), chunk, attrs)
            else:
                text.insert_embed(len(text), chunk, attrs)
    else:
        _fill(parent.children.insert(index, XmlElement(node.tag, dict(node.attributes))), node)


def _units(text: XmlText):
    """The text as (character, formatting) pairs; None when it holds embeds."""
    units = []
    for chunk, attrs in text.diff():
        if not isinstance(chunk, str):
            return None
        key = json.dumps(attrs, sort_keys=True, default=str) if attrs else ""
        units.extend((character, key) for character in chunk)
    return units


def _patch_text(text: XmlText, new: XmlText) -> bool:
    old_units, new_units = _units(text), _units(new)
    if old_units is None or new_units is None:  # embeds (not used by Plane's editor): rewrite the text
        if text.diff() == new.diff():
            return False
        del text[0 : len(text)]
        for chunk, attrs in new.diff():
            if isinstance(chunk, str):
                text.insert(len(text), chunk, attrs)
            else:
                text.insert_embed(len(text), chunk, attrs)
        return True
    if old_units == new_units:
        return False
    old_string = "".join(character for character, _ in old_units)
    matcher = difflib.SequenceMatcher(None, old_units, new_units, autojunk=False)
    # pycrdt counts text positions in UTF-8 bytes; from the end backwards, earlier positions stay valid.
    for operation, old_start, old_end, new_start, new_end in reversed(matcher.get_opcodes()):
        if operation == "equal":
            continue
        start = len(old_string[:old_start].encode("utf-8"))
        end = len(old_string[:old_end].encode("utf-8"))
        if end > start:
            del text[start:end]
        position = start
        for key, run in itertools.groupby(new_units[new_start:new_end], key=lambda unit: unit[1]):
            chunk = "".join(character for character, _ in run)
            text.insert(position, chunk, json.loads(key) if key else None)
            position += len(chunk.encode("utf-8"))
    return True


def _patch_element(element: XmlElement, new: XmlElement) -> bool:
    changed = False
    old_attributes = dict(element.attributes)
    new_attributes = {key: value for key, value in dict(new.attributes).items() if key != EDITOR_ID}
    for key in old_attributes:
        if key != EDITOR_ID and key not in new_attributes:
            del element.attributes[key]
            changed = True
    for key, value in new_attributes.items():
        if old_attributes.get(key) != value:
            element.attributes[key] = value
            changed = True
    return _patch_children(element, new) or changed


def _same_kind(node, new) -> bool:
    if isinstance(node, XmlText) or isinstance(new, XmlText):
        return isinstance(node, XmlText) and isinstance(new, XmlText)
    return node.tag == new.tag


def _patch_children(parent, new_parent) -> bool:
    """Lay new_parent's children over parent's: keep equal nodes, edit paired nodes in place, add the rest."""
    old, new = list(parent.children), list(new_parent.children)
    matcher = difflib.SequenceMatcher(None, [_signature(n) for n in old], [_signature(n) for n in new], autojunk=False)
    changed = False
    # From the end backwards, so the indexes of earlier nodes stay valid.
    for operation, old_start, old_end, new_start, new_end in reversed(matcher.get_opcodes()):
        if operation == "equal":
            continue
        changed = True
        paired = min(old_end - old_start, new_end - new_start)
        for index in range(old_end - 1, old_start + paired - 1, -1):
            del parent.children[index]
        for offset, node in enumerate(new[new_start + paired : new_end]):
            _insert_node(parent, old_start + paired + offset, node)
        for offset in range(paired):
            index, node, replacement = old_start + offset, old[old_start + offset], new[new_start + offset]
            if not _same_kind(node, replacement):
                del parent.children[index]
                _insert_node(parent, index, replacement)
            elif isinstance(node, XmlText):
                _patch_text(node, replacement)
            else:
                _patch_element(node, replacement)
    return changed


def _replace_blocks(body: XmlFragment, new_body: XmlFragment) -> bool:
    return _patch_children(body, new_body)


def _append_blocks(body: XmlFragment, new_body: XmlFragment) -> bool:
    added = list(new_body.children)
    for block in added:
        _insert_node(body, len(body.children), block)
    return bool(added)


def _set_title(title: XmlFragment, name: str) -> bool:
    heading = next((n for n in title.children if isinstance(n, XmlElement)), None)
    if heading is None:
        return False  # live fills an empty title from the page name when it loads the page
    text = next((n for n in heading.children if isinstance(n, XmlText)), None)
    if text is None:
        text = heading.children.append(XmlText())
    if "".join(chunk for chunk, _ in text.diff() if isinstance(chunk, str)) == name:
        return False
    del text[0 : len(text)]
    text.insert(0, name)
    return True


def edit_document(
    current: bytes, new_body: bytes | None = None, name: str | None = None, append: bool = False
) -> DocumentEdit:
    """Apply a new body (a whole document from live's converter), or append its blocks, and/or a new title."""
    doc = _load(current)
    before = doc.get_state()
    changed = False
    with doc.transaction():
        if new_body is not None:
            body, new = doc.get(BODY, type=XmlFragment), _load(new_body).get(BODY, type=XmlFragment)
            changed |= _append_blocks(body, new) if append else _replace_blocks(body, new)
        if name is not None:
            changed |= _set_title(doc.get(TITLE, type=XmlFragment), name)
    return DocumentEdit(doc.get_update(), doc.get_update(before) if changed else b"", changed)


def protected_parts(binary: bytes) -> Counter:
    """Images, mentions and embedded work items in the body, counted by what each one points at."""
    parts = Counter()

    def walk(node):
        for child in node.children:
            if isinstance(child, XmlElement):
                if child.tag in PROTECTED_PARTS:
                    label, keys = PROTECTED_PARTS[child.tag]
                    attributes = dict(child.attributes)
                    parts[(label,) + tuple(str(attributes.get(key)) for key in keys)] += 1
                walk(child)

    if binary:
        walk(_load(binary).get(BODY, type=XmlFragment))
    return parts


def removed_parts(before: bytes, after: bytes) -> list[str]:
    """Plain descriptions of the protected parts in before that after no longer has."""
    lost = protected_parts(before) - protected_parts(after)
    return [f"{part[0]} {' '.join(part[1:])}" + (f" (x{count})" if count > 1 else "") for part, count in lost.items()]


def missing_from(incoming: bytes, stored: bytes) -> tuple[bytes, bytes] | None:
    """For a save from an editor: None when incoming already holds everything in stored; otherwise the union
    of both (nothing is lost) and the part of stored that incoming lacks (to send to that editor)."""
    doc = _load(incoming)
    state, plain = doc.get_state(), doc.get_update()
    doc.apply_update(bytes(stored))
    union = doc.get_update()
    if union == plain:
        return None
    return union, _load(stored).get_update(state)

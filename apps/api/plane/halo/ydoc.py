# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Change a page's Yjs document so that every copy of it can merge the change.

Plane's editor keeps a page as a Yjs document (body in fragment "default", title in "title"), and browsers keep
their own copy. A change must therefore be an update on that same document, never a new document: only the
top-level blocks that differ are replaced (a sequence diff), and the others keep their identity, so typing in
them merges. Blocks are compared without the "id" attribute that the browser editor's UniqueID extension adds
and live's converter never produces.
"""

import difflib
import json
from collections import Counter
from typing import NamedTuple

from pycrdt import Doc, XmlElement, XmlFragment, XmlText

BODY = "default"
TITLE = "title"
EDITOR_ID = "id"  # UniqueID's block id (packages/editor unique-id extension)
# Page parts an edit must not drop by accident, and the attributes that say which one each is.
PROTECTED_PARTS = {
    "image-component": ("src",),
    "mention-component": ("entity_name", "entity_identifier"),
    "issue-embed-component": ("entity_identifier",),
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


def _insert_block(body: XmlFragment, index: int, block) -> None:
    if isinstance(block, XmlText):
        raise ValueError("a page body holds blocks, not loose text")
    _fill(body.children.insert(index, XmlElement(block.tag, dict(block.attributes))), block)


def _replace_blocks(body: XmlFragment, new_body: XmlFragment) -> bool:
    old, new = list(body.children), list(new_body.children)
    matcher = difflib.SequenceMatcher(
        None, [_signature(n) for n in old], [_signature(n) for n in new], autojunk=False
    )
    changed = False
    # From the end backwards, so the indexes of earlier blocks stay valid.
    for operation, old_start, old_end, new_start, new_end in reversed(matcher.get_opcodes()):
        if operation == "equal":
            continue
        changed = True
        for _ in range(old_end - old_start):
            del body.children[old_start]
        for offset, block in enumerate(new[new_start:new_end]):
            _insert_block(body, old_start + offset, block)
    return changed


def _append_blocks(body: XmlFragment, new_body: XmlFragment) -> bool:
    added = list(new_body.children)
    for block in added:
        _insert_block(body, len(body.children), block)
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
                    attributes = dict(child.attributes)
                    parts[(child.tag,) + tuple(str(attributes.get(key)) for key in PROTECTED_PARTS[child.tag])] += 1
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

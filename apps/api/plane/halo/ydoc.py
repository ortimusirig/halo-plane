# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Change a page's Yjs document so that every copy of it can merge the change.

Plane's editor keeps a page as a Yjs document (body in fragment "default", title in "title"), and browsers keep
their own copy. A change must therefore be an update on that same document, never a new document: only the
top-level blocks that differ are replaced, and the others keep their identity, so typing in them merges.
"""

import json
from typing import NamedTuple

from pycrdt import Doc, XmlElement, XmlFragment, XmlText

BODY = "default"
TITLE = "title"


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
    return (
        node.tag,
        json.dumps(dict(node.attributes), sort_keys=True, default=str),
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


def _replace_blocks(body: XmlFragment, new_body: XmlFragment) -> bool:
    old, new = list(body.children), list(new_body.children)
    old_sig, new_sig = [_signature(n) for n in old], [_signature(n) for n in new]
    start = 0
    while start < min(len(old), len(new)) and old_sig[start] == new_sig[start]:
        start += 1
    end = 0
    while end < min(len(old), len(new)) - start and old_sig[-1 - end] == new_sig[-1 - end]:
        end += 1
    removed, added = len(old) - start - end, new[start : len(new) - end]
    if any(isinstance(block, XmlText) for block in added):
        raise ValueError("a page body holds blocks, not loose text")
    for _ in range(removed):
        del body.children[start]
    for offset, block in enumerate(added):
        _fill(body.children.insert(start + offset, XmlElement(block.tag, dict(block.attributes))), block)
    return bool(removed or added)


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


def edit_document(current: bytes, new_body: bytes | None = None, name: str | None = None) -> DocumentEdit:
    """Apply a new body (a whole document from live's converter) and/or a new title to current."""
    doc = _load(current)
    before = doc.get_state()
    changed = False
    with doc.transaction():
        if new_body is not None:
            changed |= _replace_blocks(doc.get(BODY, type=XmlFragment), _load(new_body).get(BODY, type=XmlFragment))
        if name is not None:
            changed |= _set_title(doc.get(TITLE, type=XmlFragment), name)
    return DocumentEdit(doc.get_update(), doc.get_update(before) if changed else b"", changed)

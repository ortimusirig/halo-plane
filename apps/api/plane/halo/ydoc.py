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
import re
from collections import Counter
from typing import NamedTuple

from pycrdt import Doc, XmlElement, XmlFragment, XmlText

BODY = "default"
TITLE = "title"
EDITOR_ID = "id"  # UniqueID's block id (packages/editor unique-id extension)
# The node types UniqueID gives that id (BLOCK_NODE_TYPES, packages/editor/src/core/constants/extension.ts): only
# there is it the browser's own bookkeeping, ignored when comparing; anywhere else (a mention) it is content.
UNIQUE_ID_TYPES = frozenset({
    "paragraph", "heading", "blockquote", "codeBlock", "horizontalRule", "bulletList", "orderedList", "listItem",
    "taskList", "taskItem", "table", "image", "imageComponent", "calloutComponent", "issue-embed-component",
})


def _own_attributes(node) -> dict:
    """A node's attributes without UniqueID's block id."""
    attributes = dict(node.attributes)
    if node.tag in UNIQUE_ID_TYPES:
        attributes.pop(EDITOR_ID, None)
    return attributes
# Page parts an edit must not drop by accident: Yjs elements are named after the editor's node types
# (packages/editor/src/core/constants/extension.ts), with the attributes that say which one each is.
PROTECTED_PARTS = {
    "imageComponent": ("image", ("src",)),
    "image": ("image", ("src",)),
    "mention": ("mention", ("entity_name", "entity_identifier")),
    "issue-embed-component": ("embedded work item", ("entity_identifier",)),
}


DIFF_LIMIT = 250_000  # pairs one diff may compare; past it the changed middle is rewritten as one run


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
    attributes = _own_attributes(node)
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


def _changes(old: list, new: list) -> list[tuple[int, int, int, int]]:
    """Changed ranges (old_start, old_end, new_start, new_end), after trimming the common start and end."""
    start = 0
    while start < min(len(old), len(new)) and old[start] == new[start]:
        start += 1
    end = 0
    while end < min(len(old), len(new)) - start and old[len(old) - 1 - end] == new[len(new) - 1 - end]:
        end += 1
    old_middle, new_middle = old[start : len(old) - end], new[start : len(new) - end]
    if not old_middle and not new_middle:
        return []
    if len(old_middle) * len(new_middle) > DIFF_LIMIT:  # SequenceMatcher is quadratic on repetitive input
        return [(start, len(old) - end, start, len(new) - end)]
    matcher = difflib.SequenceMatcher(None, old_middle, new_middle, autojunk=False)
    return [
        (start + old_start, start + old_end, start + new_start, start + new_end)
        for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes()
        if operation != "equal"
    ]


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
    # pycrdt counts text positions in UTF-8 bytes; from the end backwards, earlier positions stay valid.
    for old_start, old_end, new_start, new_end in reversed(_changes(old_units, new_units)):
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
    new_attributes = _own_attributes(new)
    for key in _own_attributes(element):
        if key not in new_attributes:
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


def _plain(node) -> str:
    if isinstance(node, XmlText):
        return "".join(chunk for chunk, _ in node.diff() if isinstance(chunk, str))
    return " ".join(_plain(child) for child in node.children)


def _align(old: list, new: list) -> list[tuple[int, int]]:
    """Pairs (old index, new index), in order on both sides, of same-type nodes in one changed range: the pairing
    with the most pairs, and among those the most words in common, so that a node is edited in place rather than
    replaced (an editor's unsent typing in it then survives the merge)."""
    old_words = [frozenset(re.findall(r"\w+", _plain(n).lower())) for n in old]
    new_words = [frozenset(re.findall(r"\w+", _plain(n).lower())) for n in new]

    def weight(i, j):
        if not _same_kind(old[i], new[j]):
            return None
        union = old_words[i] | new_words[j]
        return 1.0 + share * (len(old_words[i] & new_words[j]) / len(union) if union else 1.0)

    rows, cols = len(old), len(new)
    # Words in common, summed over every pair, weigh less than one pair, so the most pairs win first.
    share = 1.0 / (min(rows, cols) + 1)
    best = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            score = max(best[i + 1][j], best[i][j + 1])
            w = weight(i, j)
            if w is not None:
                score = max(score, w + best[i + 1][j + 1])
            best[i][j] = score
    pairs, i, j = [], 0, 0
    while i < rows and j < cols:
        w = weight(i, j)
        if w is not None and best[i][j] == w + best[i + 1][j + 1]:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    return pairs


def _patch_pair(node, replacement) -> None:
    if isinstance(node, XmlText):
        _patch_text(node, replacement)
    else:
        _patch_element(node, replacement)


def _patch_range(parent, start: int, old: list, new: list) -> None:
    """Lay `new` over parent.children[start : start + len(old)], which hold `old`."""
    if len(old) * len(new) > DIFF_LIMIT:
        # Too large to compare: pair the nodes by position (HALO.md, Known limits).
        paired = min(len(old), len(new))
        for index in range(start + len(old) - 1, start + paired - 1, -1):
            del parent.children[index]
        for offset, node in enumerate(new[paired:]):
            _insert_node(parent, start + paired + offset, node)
        for offset in range(paired):
            index, node, replacement = start + offset, old[offset], new[offset]
            if not _same_kind(node, replacement):
                del parent.children[index]
                _insert_node(parent, index, replacement)
            else:
                _patch_pair(node, replacement)
        return
    # Same-type nodes are paired and edited in place; the others are removed or added. From the end backwards,
    # so the indexes of earlier nodes stay valid.
    bounds = [(-1, -1), *_align(old, new), (len(old), len(new))]
    for (old_before, new_before), (old_at, new_at) in reversed(list(zip(bounds, bounds[1:]))):
        if old_at < len(old):
            _patch_pair(old[old_at], new[new_at])
        for index in range(start + old_at - 1, start + old_before, -1):
            del parent.children[index]
        for offset, node in enumerate(new[new_before + 1 : new_at]):
            _insert_node(parent, start + old_before + 1 + offset, node)


def _patch_children(parent, new_parent) -> bool:
    """Lay new_parent's children over parent's: keep equal nodes, edit paired nodes in place, add the rest."""
    old, new = list(parent.children), list(new_parent.children)
    changes = _changes([_signature(n) for n in old], [_signature(n) for n in new])
    # From the end backwards, so the indexes of earlier nodes stay valid.
    for old_start, old_end, new_start, new_end in reversed(changes):
        _patch_range(parent, old_start, old[old_start:old_end], new[new_start:new_end])
    return bool(changes)


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


def with_title(binary: bytes, name: str) -> bytes:
    """The whole document with the page's name as its title when its title fragment is empty, the way Plane's
    getBinaryDataFromDocumentEditorHTMLString adds it (one level-1 heading; 1.0, a float, so that JavaScript reads
    a number). A document that has a title is returned as it is."""
    doc = _load(binary)
    title = doc.get(TITLE, type=XmlFragment)
    if len(title.children):
        return binary
    with doc.transaction():
        heading = title.children.append(XmlElement("heading", {"level": 1.0}))
        if name:
            heading.children.append(XmlText()).insert(0, name)
    return doc.get_update()


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


IMAGE_TAGS = ("imageComponent", "image")


def _images(node):
    for child in node.children:
        if isinstance(child, XmlElement):
            if child.tag in IMAGE_TAGS:
                yield child
            yield from _images(child)


def image_sources(binary: bytes) -> list[str]:
    """The src of every image in the body, in order (an uploaded image's src is its asset id)."""
    sources = (dict(image.attributes).get("src") for image in _images(_load(binary).get(BODY, type=XmlFragment)))
    return [str(src) for src in sources if src]


def image_ids(binary: bytes) -> list[tuple[str | None, str | None]]:
    """The (src, id) of every image in the body, in order: the id is the one Plane's mention index keys an image
    by (page_transaction reads it from the HTML), and the document keeps a retained image's own."""
    images = _images(_load(binary).get(BODY, type=XmlFragment))
    return [(_text(dict(image.attributes).get("src")), _text(dict(image.attributes).get(EDITOR_ID))) for image in images]


def _text(value) -> str | None:
    return None if value is None or value == "" else str(value)


def with_image_sources(binary: bytes, sources: dict) -> bytes:
    """The whole document with each image whose src is a key of sources pointed at its value (a duplicated page's
    own image copies); everything else stays as it is."""
    doc = _load(binary)
    with doc.transaction():
        for image in list(_images(doc.get(BODY, type=XmlFragment))):
            src = dict(image.attributes).get("src")
            if src in sources:
                image.attributes["src"] = sources[src]
    return doc.get_update()


def removed_parts(before: bytes, after: bytes) -> list[str]:
    """Plain descriptions of the protected parts in before that after no longer has."""
    lost = protected_parts(before) - protected_parts(after)
    return [f"{part[0]} {' '.join(part[1:])}" + (f" (x{count})" if count > 1 else "") for part, count in lost.items()]


def _var_uint(data: bytes, index: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index
        shift += 7


def _clients(state: bytes) -> set[int]:
    """The client ids in an encoded Yjs state vector (count, then client and clock pairs, as varUints)."""
    count, index = _var_uint(state, 0)
    clients = set()
    for _ in range(count):
        client, index = _var_uint(state, index)
        _, index = _var_uint(state, index)
        clients.add(client)
    return clients


def missing_from(incoming: bytes, stored: bytes) -> tuple[bytes, bytes] | None:
    """For a save from an editor: None when incoming already holds everything in stored, or when the two share
    no history (no client in common: separately converted first documents of one page, which a union would
    duplicate; the save then replaces the stored document, as Plane's does); otherwise the union of both
    (nothing is lost) and the part of stored that incoming lacks (to send to that editor). An editor that
    missed an API edit always shares history with the stored document, since API edits are lineage edits."""
    doc = _load(incoming)
    state, plain = doc.get_state(), doc.get_update()
    if not _clients(state) & _clients(_load(bytes(stored)).get_state()):
        return None
    doc.apply_update(bytes(stored))
    union = doc.get_update()
    if union == plain:
        return None
    return union, _load(stored).get_update(state)

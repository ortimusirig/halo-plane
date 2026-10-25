# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Plane's mention indexing as Halo relies on it, for halo/upgrade-check.sh (standard library only).

    python halo/index_callers.py callers <tree>       every function in <tree>/apps/api/plane that calls the task
    python halo/index_callers.py module-hash <file>   a short hash of the whole task module

The caller list finds direct calls (page_transaction(...)), task calls (.delay, .apply_async, .s, .si), aliased
and relative imports (from ... import page_transaction as x), qualified calls through an imported module
(page_transaction_task.page_transaction.delay) and the task's dotted name as a string (send_task). A call outside
any function is listed as <module>. Tests, migrations and Halo's own app are left out.
"""

import ast
import hashlib
import os
import sys

TASK_MODULE = "plane.bgtasks.page_transaction_task"
TASK = "page_transaction"
TASK_CALLS = ("delay", "apply_async", "s", "si")


def _absolute(node, module) -> str:
    """The module an ImportFrom names, with a relative import resolved against the importing module."""
    if not node.level:
        return node.module or ""
    package = module.split(".")[: -node.level]
    return ".".join(package + ([node.module] if node.module else []))


def _aliases(tree, module) -> tuple[set[str], set[str]]:
    """Names bound to the task, and names bound to its module, in one file."""
    task_names, module_names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = _absolute(node, module)
            for alias in node.names:
                if source == TASK_MODULE and alias.name == TASK:
                    task_names.add(alias.asname or alias.name)
                if f"{source}.{alias.name}" == TASK_MODULE:
                    module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == TASK_MODULE:
                    module_names.add(alias.asname or alias.name)
    return task_names, module_names


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _is_task(node, task_names, module_names) -> bool:
    name = _dotted(node)
    if name is None:
        return False
    if name in task_names:
        return True
    return any(name == f"{module}.{TASK}" for module in module_names)


def _callers_in(path, rel) -> set[str]:
    source = open(path, encoding="utf-8").read().replace("\r\n", "\n")
    tree = ast.parse(source)
    module = "plane." + rel[: -len(".py")].replace("/", ".")  # a package's __init__ keeps its last part
    task_names, module_names = _aliases(tree, module)
    found = set()

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, scope + [child.name])
                continue
            hit = isinstance(child, ast.Constant) and child.value == f"{TASK_MODULE}.{TASK}"
            if isinstance(child, ast.Call):
                target = child.func
                if isinstance(target, ast.Attribute) and target.attr in TASK_CALLS:
                    target = target.value
                hit = _is_task(target, task_names, module_names)
            if hit:
                found.add(f"{rel}:{'.'.join(scope) or '<module>'}")
            visit(child, scope)

    visit(tree, [])
    return found


def callers(tree_root: str) -> list[str]:
    root = os.path.join(tree_root, "apps", "api", "plane")
    found = set()
    for dirpath, _, filenames in os.walk(root):
        parts = os.path.relpath(dirpath, root).replace(os.sep, "/").split("/")
        if {"tests", "halo", "migrations"} & set(parts):
            continue
        for name in filenames:
            if name.endswith(".py"):
                path = os.path.join(dirpath, name)
                found |= _callers_in(path, os.path.relpath(path, root).replace(os.sep, "/"))
    return sorted(found)


def module_hash(path: str) -> str:
    source = open(path, encoding="utf-8").read().replace("\r\n", "\n")
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def main(argv) -> int:
    if len(argv) == 3 and argv[1] == "callers":
        print(" ".join(callers(argv[2])))
        return 0
    if len(argv) == 3 and argv[1] == "module-hash":
        print(module_hash(argv[2]))
        return 0
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))

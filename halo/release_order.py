# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Halo's release rule for Plane tags, used by halo/upgrade-check.sh: never downgrade, and run one release
behind Plane's newest (n-1). A target is allowed when it is newer than the deployed release, is a release (not a
release candidate), and Plane has already published a newer release.

    python halo/release_order.py <deployed tag> <target tag> [published tag ...]

Prints one line. Exit 0: allowed. 1: not newer than the deployed release (a downgrade or no change). 2: breaks
the n-1 rule (a release candidate, or Plane's newest release). 64: a tag that is not a Plane release tag.
"""

import re
import sys

TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?")


def key(tag: str):
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(tag)
    major, minor, patch, rc = match.groups()
    # A release candidate comes before its release: v1.4.3-rc1 < v1.4.3-rc2 < v1.4.3.
    return (int(major), int(minor), int(patch), int(rc) if rc else float("inf"))


def is_release(tag: str) -> bool:
    return key(tag)[3] == float("inf")


def check(deployed: str, target: str, published: list[str]) -> tuple[int, str]:
    if key(target) <= key(deployed):
        return 1, f"{target} is not newer than the deployed {deployed} (Halo never downgrades)"
    if not is_release(target):
        return 2, f"{target} is a release candidate; Halo runs releases only"
    releases = [tag for tag in published if TAG.fullmatch(tag) and is_release(tag)]
    newest = max(releases, key=key, default=None)
    if newest is None or key(target) >= key(newest):
        return 2, (f"{target} is Plane's newest release (newest published: {newest}); Halo runs one release "
                   "behind, so it moves to this one once a newer release is out")
    return 0, f"{target} is newer than the deployed {deployed} and behind Plane's newest release {newest}"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 64
    try:
        code, message = check(argv[1], argv[2], argv[3:])
    except ValueError as error:
        print(f"not a Plane release tag: {error}")
        return 64
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))

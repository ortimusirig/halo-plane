#!/usr/bin/env bash
# Release gate for a newer Plane CE release, before upgrading Halo.
#
#   bash halo/upgrade-check.sh <newer Plane tag, for example v1.4.3>               (the gate)
#   bash halo/upgrade-check.sh <tag> --check-only     (patch and compatibility report only; NOT a gate)
#
# Halo runs one release behind Plane's newest and never downgrades, so a tag that is not newer than the base in
# halo/release.env is refused (semantic order: v1.4.3-rc1 < v1.4.3). The gate applies this branch's patch to
# the new tag, builds the image on the new upstream image, and runs the Halo tests and the end-to-end test with
# the new live image. It passes only when all of that passes and no compatibility item is open. Nothing here
# changes this branch or any deployment.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
# shellcheck source=release.env
. halo/release.env

new=${1:?give the new Plane tag, for example v1.4.3}
mode=${2:-gate}
py=$(command -v python3 || command -v python)
newer=$("$py" -c '
import re, sys
def key(tag):
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?", tag)
    if not match:
        sys.exit("not a Plane release tag: " + tag)
    major, minor, patch, rc = match.groups()
    return (int(major), int(minor), int(patch), int(rc) if rc else float("inf"))
print("yes" if key(sys.argv[2]) > key(sys.argv[1]) else "no")
' "$PLANE_TAG" "$new")
if [ "$newer" != "yes" ]; then
  echo "FAIL: $new is not newer than $PLANE_TAG (Halo never downgrades)"
  exit 1
fi

git fetch -q --depth 1 origin "refs/tags/$new:refs/tags/$new"
mkdir -p halo/patch
work="$(pwd -W 2>/dev/null || pwd)/halo/patch/worktree-$new"
git worktree remove --force "$work" >/dev/null 2>&1 || true
git worktree add -q --detach "$work" "$new"
trap 'git worktree remove --force "$work"' EXIT
git diff --binary "$PLANE_TAG" HEAD -- apps/api > "$work/halo.patch"

failures=0
open_items=0
if (cd "$work" && git apply --check halo.patch); then
  echo "OK   patch applies to $new"
else
  echo "FAIL patch does not apply to $new: rebase halo/pages-api onto $new and resolve"
  failures=$((failures + 1))
fi

task="$work/apps/api/plane/bgtasks/page_version_task.py"
if grep -qE "page\.description([^_a-z]|$)" "$task"; then
  echo "OK   version-history fix still needed: $new still reads page.description"
else
  echo "OPEN version-history fix: $new no longer reads page.description; drop that part of the hunk when rebasing"
  open_items=$((open_items + 1))
fi
if grep -rlqE "pages" "$work/apps/api/plane/api/urls/"; then
  echo "OPEN Plane's own API v1 now has page routes: compare with plane/halo and prefer Plane's"
  open_items=$((open_items + 1))
else
  echo "OK   Plane's API v1 still has no page routes"
fi
if grep -q "class PagesDescriptionViewSet" "$work/apps/api/plane/app/views/page/base.py" \
  && grep -q '"workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/description/"' \
     "$work/apps/api/plane/app/urls/page.py"; then
  echo "OK   Plane's description endpoint (overridden by plane/halo/app_urls.py) keeps its name and path"
else
  echo "FAIL Plane's description endpoint moved or was renamed: update plane/halo/app_urls.py and views.py"
  failures=$((failures + 1))
fi
hocuspocus=$(grep -E '"@hocuspocus/server"' "$work/pnpm-workspace.yaml" | tr -d ' "' || true)
if [ "$hocuspocus" = "@hocuspocus/server:2.15.2" ]; then
  echo "OK   live uses Hocuspocus 2.15.2 (channel, messages and probe as tested)"
else
  echo "OPEN live Hocuspocus changed ($hocuspocus): recheck live.py; the end-to-end test decides"
  open_items=$((open_items + 1))
fi
if grep -q 'Controller("/convert-document")' "$work/apps/live/src/controllers/document.controller.ts"; then
  echo "OK   live still has /convert-document/"
else
  echo "FAIL live has no /convert-document/ any more: body edits cannot convert HTML"
  failures=$((failures + 1))
fi

if [ "$mode" = "--check-only" ]; then
  echo "CHECK ONLY: $failures failed, $open_items open; this is not a release gate (no build, no tests)"
  [ "$failures" = 0 ]
  exit
fi
if [ "$failures" != 0 ]; then
  echo "GATE FAILED: $failures failed before building"
  exit 1
fi

image="plane-backend-halo:${new}-candidate"
cp "$work/halo.patch" halo/patch/halo.patch
docker build -q -f halo/Dockerfile.backend --build-arg PLANE_IMAGE="makeplane/plane-backend:$new" \
  --build-arg HALO_REVISION="$(git rev-parse HEAD)" -t "$image" halo >/dev/null
echo "OK   built $image"
HALO_BACKEND_IMAGE="$image" bash halo/test-backend.sh plane/halo/tests -q
HALO_BACKEND_IMAGE="$image" PLANE_LIVE_IMAGE="makeplane/plane-live:$new" bash halo/e2e/run.sh
if [ "$open_items" != 0 ]; then
  echo "GATE OPEN: tests pass, but $open_items compatibility item(s) above need a decision"
  exit 3
fi
echo "GATE PASSED for $new"

#!/usr/bin/env bash
# Check that Halo's patch carries over to a newer Plane CE release, before upgrading Halo.
#
#   bash halo/upgrade-check.sh <newer Plane tag, for example v1.4.3> [--build]
#
# Halo runs one release behind Plane's newest and never downgrades, so a tag that is not newer than the
# current base is refused. The check applies this branch's patch to the new tag in a temporary worktree and
# reports what needs a decision. With --build it also builds the image on the new upstream image and runs the
# Halo tests and the end-to-end test against it. Nothing here changes this branch or any deployment.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."

current=v1.4.2
new=${1:?give the new Plane tag, for example v1.4.3}
build=${2:-}
if [ "$new" = "$current" ] || [ "$(printf '%s\n%s\n' "$current" "$new" | sort -V | head -1)" != "$current" ]; then
  echo "FAIL: $new is not newer than $current (Halo never downgrades)"
  exit 1
fi

git fetch -q --depth 1 origin "refs/tags/$new:refs/tags/$new"
mkdir -p halo/patch
work="$(pwd -W 2>/dev/null || pwd)/halo/patch/worktree-$new"
git worktree remove --force "$work" >/dev/null 2>&1 || true
git worktree add -q --detach "$work" "$new"
trap 'git worktree remove --force "$work"' EXIT
git diff --binary "$current" HEAD -- apps/api > "$work/halo.patch"

status=0
if (cd "$work" && git apply --check halo.patch); then
  echo "OK   patch applies to $new"
else
  echo "FAIL patch does not apply to $new: rebase halo/pages-api onto $new and resolve"
  status=1
fi

task="$work/apps/api/plane/bgtasks/page_version_task.py"
if grep -qE "page\.description([^_a-z]|$)" "$task"; then
  echo "KEEP version-history fix: $new still reads page.description"
else
  echo "DROP version-history fix: $new no longer reads page.description (remove that hunk when rebasing)"
fi

if grep -rlqE "pages" "$work/apps/api/plane/api/urls/"; then
  echo "LOOK Plane's own API v1 now has page routes: compare with plane/halo and prefer Plane's"
else
  echo "OK   Plane's API v1 still has no page routes"
fi

hocuspocus=$(grep -E '"@hocuspocus/server"' "$work/pnpm-workspace.yaml" | tr -d ' "' || true)
if [ "$hocuspocus" = "@hocuspocus/server:2.15.2" ]; then
  echo "OK   live uses Hocuspocus 2.15.2 (channel and messages as tested)"
else
  echo "LOOK live Hocuspocus changed ($hocuspocus): rerun the end-to-end test before upgrading"
fi
if grep -q 'Controller("/convert-document")' "$work/apps/live/src/controllers/document.controller.ts"; then
  echo "OK   live still has /convert-document/"
else
  echo "FAIL live has no /convert-document/ any more: body edits cannot convert HTML"
  status=1
fi

if [ "$build" = "--build" ] && [ "$status" = 0 ]; then
  image="plane-backend-halo:${new}-pages-candidate"
  cp "$work/halo.patch" halo/patch/halo.patch
  docker build -q -f halo/Dockerfile.backend --build-arg PLANE_IMAGE="makeplane/plane-backend:$new" \
    --build-arg HALO_REVISION="$(git rev-parse HEAD)" -t "$image" halo >/dev/null
  echo "OK   built $image"
  HALO_BACKEND_IMAGE="$image" bash halo/test-backend.sh plane/halo/tests -q
  HALO_BACKEND_IMAGE="$image" PLANE_LIVE_IMAGE="makeplane/plane-live:$new" bash halo/e2e/run.sh
fi
exit "$status"

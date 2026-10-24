#!/usr/bin/env bash
# Release gate for a newer Plane CE release, before upgrading Halo.
#
#   bash halo/upgrade-check.sh <new tag> [--candidate <branch>] [--check-only]
#
# Run it from the deployed branch: its halo/release.env names the release Halo runs (PLANE_TAG). The candidate
# is the branch to ship (default: this one). When the patch does not apply to the new tag, make a candidate:
# a branch from the new tag with the Halo commits rebased onto it and its own halo/release.env set to the new
# tag; then run the gate again with --candidate <that branch>. The candidate's patch is always its diff from its
# own base.
#
# Halo runs one release behind Plane's newest and never downgrades: the new tag must be newer than the deployed
# release (semantic order: v1.4.3-rc1 < v1.4.3). The gate checks the patch against the new tag, builds the image
# on the new upstream image, runs the Halo tests, Plane's own tests (no new failures against the upstream image)
# and the end-to-end test with the new live image. It passes only when all of that passes and no compatibility
# item is open. --check-only gives the report without building or testing; it is not a gate. Nothing here
# changes a branch or a deployment.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
# shellcheck source=release.env
. halo/release.env
# The release Halo runs: this checkout's base, or the deployed branch's when this run is inside a candidate.
deployed=${HALO_DEPLOYED_TAG:-$PLANE_TAG}

new=${1:?give the new Plane tag, for example v1.4.3}
shift
candidate=HEAD
mode=gate
mode_flag=()
while [ $# -gt 0 ]; do
  case "$1" in
    --candidate) candidate=${2:?give the candidate branch}; shift 2 ;;
    --check-only) mode=check; mode_flag=(--check-only); shift ;;
    *) echo "unknown argument: $1"; exit 2 ;;
  esac
done

if [ "$candidate" != HEAD ]; then
  # Everything a release is built and tested with (patch, Dockerfile, pins, fingerprints, tests) comes from the
  # candidate itself: run this gate again from a checkout of it, keeping the deployed release for the
  # no-downgrade check.
  inside="$(pwd -W 2>/dev/null || pwd)/halo/patch/candidate-$$"
  git worktree add -q --detach "$inside" "$candidate"
  trap 'git worktree remove --force "$inside"' EXIT
  set +e
  HALO_DEPLOYED_TAG="$deployed" bash "$inside/halo/upgrade-check.sh" "$new" "${mode_flag[@]}"
  status=$?
  set -e
  exit "$status"
fi

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
' "$deployed" "$new")
if [ "$newer" != "yes" ]; then
  echo "FAIL: $new is not newer than the deployed $deployed (Halo never downgrades)"
  exit 1
fi
candidate_base=$(git show "$candidate:halo/release.env" | sed -n 's/^PLANE_TAG=//p')
if [ "$candidate_base" != "$deployed" ] && [ "$candidate_base" != "$new" ]; then
  echo "FAIL: candidate $candidate is built on $candidate_base, neither the deployed $deployed nor $new"
  exit 1
fi
echo "deployed $deployed, new $new, candidate $candidate (based on $candidate_base)"

git fetch -q --depth 1 origin "refs/tags/$new:refs/tags/$new"
mkdir -p halo/patch
work="$(pwd -W 2>/dev/null || pwd)/halo/patch/worktree-$new"
git worktree remove --force "$work" >/dev/null 2>&1 || true
git worktree add -q --detach "$work" "$new"
trap 'git worktree remove --force "$work"' EXIT
git diff --binary "$candidate_base" "$candidate" -- apps/api > "$work/halo.patch"

failures=0
open_items=0
if (cd "$work" && git apply --check halo.patch); then
  echo "OK   patch applies to $new"
else
  echo "FAIL patch does not apply to $new: make a candidate branch rebased onto $new (see the header)"
  failures=$((failures + 1))
fi

task="$work/apps/api/plane/bgtasks/page_version_task.py"
if grep -qE "page\.description([^_a-z]|$)" "$task"; then
  echo "OK   version-history fix still needed: $new still reads page.description"
else
  echo "OPEN version-history fix: $new no longer reads page.description; keep only Halo's marked-version line"
  open_items=$((open_items + 1))
fi
if grep -rlqE "pages" "$work/apps/api/plane/api/urls/"; then
  echo "OPEN Plane's own API v1 now has page routes: compare with plane/halo and prefer Plane's"
  open_items=$((open_items + 1))
else
  echo "OK   Plane's API v1 still has no page routes"
fi
fingerprint=$("$py" -c '
import ast, hashlib, sys
src = open(sys.argv[1], encoding="utf-8").read()
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.ClassDef) and node.name == "PagesDescriptionViewSet":
        print(" ".join(item.name + "=" + hashlib.sha256(ast.get_source_segment(src, item).encode()).hexdigest()[:16]
                       for item in node.body if isinstance(item, ast.FunctionDef)))
' "$work/apps/api/plane/app/views/page/base.py")
if [ "$fingerprint" = "$UPSTREAM_DESCRIPTION_FINGERPRINT" ] \
  && grep -q '"workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/description/"' \
     "$work/apps/api/plane/app/urls/page.py"; then
  echo "OK   Plane's description endpoint is unchanged (Halo's subclass mirrors it)"
else
  echo "OPEN Plane's description endpoint changed ($fingerprint): port the change to plane/halo/views.py"
  echo "     HaloPagesDescriptionViewSet and update UPSTREAM_DESCRIPTION_FINGERPRINT"
  open_items=$((open_items + 1))
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
  echo "FAIL live has no /convert-document/ any more: text edits cannot convert HTML"
  failures=$((failures + 1))
fi

if [ "$mode" = check ]; then
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
  --build-arg HALO_REVISION="$(git rev-parse "$candidate")" --build-arg HALO_TAG="$HALO_TAG" -t "$image" halo >/dev/null
label=$(docker inspect "$image" --format "{{index .Config.Labels \"org.opencontainers.image.version\"}}")
[ "$label" = "$HALO_TAG" ] || { echo "FAIL image source label is $label, not $HALO_TAG"; exit 1; }
echo "OK   built $image"
HALO_BACKEND_IMAGE="$image" bash halo/test-backend.sh plane/halo/tests -q
# Plane's own tests: the patched image may not add a failure to those of the upstream image. A run counts only
# when pytest ran the tests: exit code 0 (all passed) or 1 (some failed) and a results line; 2 to 5 (interrupted,
# internal error, usage error, nothing collected) or a missing results line stop the gate.
plane_tests() {  # <log file> <test-backend.sh arguments...>
  local log=$1
  shift
  set +e
  bash halo/test-backend.sh "$@" plane/tests -q > "$log" 2>&1
  local code=$?
  set -e
  if [ "$code" -gt 1 ] || ! grep -qE "[0-9]+ (passed|failed)" "$log"; then
    echo "FAIL Plane's tests did not run (pytest exit $code, $log):"
    tail -5 "$log"
    exit 1
  fi
  grep -E "^(FAILED|ERROR) plane/" "$log" | sed 's/ - .*//' | sort > "$log.failures" || true
}
HALO_UPSTREAM_IMAGE="makeplane/plane-backend:$new" plane_tests halo/patch/upstream-tests.log --upstream
HALO_BACKEND_IMAGE="$image" plane_tests halo/patch/halo-tests.log
added=$(comm -13 halo/patch/upstream-tests.log.failures halo/patch/halo-tests.log.failures)
if [ -n "$added" ]; then
  echo "FAIL Plane tests that fail only with Halo's patch:"
  echo "$added"
  exit 1
fi
echo "OK   Plane's own tests: no failure beyond the upstream image's $(wc -l < halo/patch/upstream-tests.log.failures)"
HALO_BACKEND_IMAGE="$image" PLANE_LIVE_IMAGE="makeplane/plane-live:$new" bash halo/e2e/run.sh
if [ "$open_items" != 0 ]; then
  echo "GATE OPEN: tests pass, but $open_items compatibility item(s) above need a decision"
  exit 3
fi
echo "GATE PASSED for $new"

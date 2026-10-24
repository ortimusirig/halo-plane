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
# Only a candidate built on the new tag can pass: the image is the new upstream image with the patch, so its
# published source (the HALO_TAG of the candidate's release.env) must be that same tree. The gate therefore needs
# the candidate's release.env to name the new tag and a new HALO_TAG, and that tag to exist (locally) on the
# candidate's commit; push the tag only after the gate passed. Running the deployed branch against a new tag is
# still useful as a first look: the report then shows the candidate item OPEN.
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

# This run builds from this checkout (Dockerfile, pins, tests), so it must be exactly the candidate commit.
if ! git diff --quiet HEAD -- apps/api halo || [ -n "$(git ls-files --others --exclude-standard apps/api halo)" ]; then
  echo "FAIL: uncommitted changes under apps/api or halo/: commit them, or gate a branch with --candidate"
  exit 1
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
open_items=0
if [ "$candidate_base" = "$new" ]; then
  echo "OK   candidate is built on $new"
else
  echo "OPEN candidate is built on $candidate_base: for a release, make a candidate branch on $new (see the header)"
  open_items=$((open_items + 1))
fi
tag_commit=$(git rev-parse -q --verify "refs/tags/$HALO_TAG^{commit}" || true)
if [ -n "$tag_commit" ] && [ "$tag_commit" = "$(git rev-parse "$candidate^{commit}")" ]; then
  echo "OK   source tag $HALO_TAG is the candidate's commit"
else
  echo "OPEN source tag $HALO_TAG is missing or on another commit: tag the candidate (git tag $HALO_TAG) before the gate"
  open_items=$((open_items + 1))
fi

git fetch -q --depth 1 origin "refs/tags/$new:refs/tags/$new"
mkdir -p halo/patch
work="$(pwd -W 2>/dev/null || pwd)/halo/patch/worktree-$new"
git worktree remove --force "$work" >/dev/null 2>&1 || true
git worktree add -q --detach "$work" "$new"
trap 'git worktree remove --force "$work"' EXIT
git diff --binary "$candidate_base" "$candidate" -- apps/api > "$work/halo.patch"

failures=0
# The patch is the candidate's diff from the tag its release.env names, so that tag must be in its history
# (a relabelled, not rebased branch would diff in reversions of every upstream change), and the diff may touch
# only Halo's own app and the Plane files Halo changes (PATCH_PLANE_FILES in release.env).
if git merge-base --is-ancestor "$candidate_base" "$candidate"; then
  echo "OK   candidate's history contains $candidate_base"
else
  echo "FAIL candidate is not built on $candidate_base: its release.env says so, but the tag is not in its history"
  failures=$((failures + 1))
fi
outside=$(git diff --name-only "$candidate_base" "$candidate" -- apps/api | while read -r path; do
  case " $PATCH_PLANE_FILES " in *" $path "*) continue ;; esac
  case "$path" in apps/api/plane/halo/*) continue ;; esac
  echo "$path"
done)
if [ -z "$outside" ]; then
  echo "OK   patch touches only plane/halo and the Plane files in PATCH_PLANE_FILES"
else
  echo "FAIL patch touches files outside Halo's patch:"
  echo "$outside" | sed 's/^/     /'
  failures=$((failures + 1))
fi
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
fingerprint() {  # <file under apps/api/plane> <class or function>: a short hash of the function or of each method
  "$py" -c '
import ast, hashlib, sys
src = open(sys.argv[1], encoding="utf-8").read().replace(chr(13) + chr(10), chr(10))
def digest(node):
    return node.name + "=" + hashlib.sha256(ast.get_source_segment(src, node).encode()).hexdigest()[:16]
for node in ast.parse(src).body:
    if isinstance(node, ast.ClassDef) and node.name == sys.argv[2]:
        print(" ".join(digest(item) for item in node.body if isinstance(item, ast.FunctionDef)))
    elif isinstance(node, ast.FunctionDef) and node.name == sys.argv[2]:
        print(digest(node))
' "$work/apps/api/plane/$1" "$2"
}
mirrored() {  # <class name> <expected fingerprint> <route suffix> <Halo class>: Halo's subclass copies that code
  local found
  found=$(fingerprint app/views/page/base.py "$1")
  if [ "$found" = "$2" ] \
    && grep -qF "/pages/<uuid:page_id>/$3/" "$work/apps/api/plane/app/urls/page.py"; then
    echo "OK   Plane's $1 is unchanged (Halo's $4 mirrors it)"
  else
    echo "OPEN Plane's $1 changed ($found): port the change to $4 in plane/halo/views.py and update release.env"
    open_items=$((open_items + 1))
  fi
}
mirrored PagesDescriptionViewSet "$UPSTREAM_DESCRIPTION_FINGERPRINT" description HaloPagesDescriptionViewSet
mirrored PageDuplicateEndpoint "$UPSTREAM_DUPLICATE_FINGERPRINT" duplicate HaloPageDuplicateEndpoint
copy_found=$(for name in copy_s3_objects_of_description_and_assets copy_assets extract_asset_ids replace_asset_ids; do
  fingerprint bgtasks/copy_s3_object.py "$name"
done | tr '\n' ' ' | sed 's/ $//')
if [ "$copy_found" = "$UPSTREAM_COPY_FINGERPRINT" ]; then
  echo "OK   Plane's copy task and helpers are unchanged (Halo's finish_page_copy does that task for pages)"
else
  echo "OPEN Plane's copy task changed ($copy_found): compare with finish_page_copy in plane/halo/tasks.py"
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

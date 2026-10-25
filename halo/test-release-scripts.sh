#!/usr/bin/env bash
# Tests of the release scripts' refusals, without Docker or a build: an uncommitted build input stops
# build-backend.sh and the gate; a branch relabelled to a new Plane tag but not rebased onto it, and a patch that
# changes a Plane file outside Halo's list, fail the gate; this branch passes the gate's checks.
#
#   bash halo/test-release-scripts.sh [new tag]      (default v1.4.3-rc2; fetches that tag; run on a clean tree)
#
# Leaves no branch or tag: test commits are bare commit objects, and the scratch checkout goes at the end.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
new=${1:-v1.4.3-rc2}
failures=0

expect() {  # <name> <exit code> <text in the output> <command...>
  local name=$1 code=$2 text=$3 out status
  shift 3
  set +e
  out=$("$@" 2>&1)
  status=$?
  set -e
  if [ "$status" = "$code" ] && grep -qF -- "$text" <<<"$out"; then
    echo "PASS $name"
  else
    echo "FAIL $name (exit $status, wanted $code and \"$text\"):"
    echo "$out" | tail -8 | sed 's/^/     /'
    failures=$((failures + 1))
  fi
}

commit_with() {  # <path> <sed script>: a commit object on HEAD with that one file changed (no branch, no tag)
  local path=$1 script=$2 index blob tree
  index=$(mktemp)
  GIT_INDEX_FILE=$index git read-tree HEAD
  blob=$(git show "HEAD:$path" | sed "$script" | git hash-object -w --stdin)
  GIT_INDEX_FILE=$index git update-index --cacheinfo "100644,$blob,$path"
  tree=$(GIT_INDEX_FILE=$index git write-tree)
  rm -f "$index"
  git commit-tree "$tree" -p HEAD -m "release script test: $path"
}

scratch="$(pwd -W 2>/dev/null || pwd)/halo/patch/release-test-$$"
git worktree add -q --detach "$scratch" HEAD
trap 'git worktree remove --force "$scratch"' EXIT
# A stand-in docker, so a missing refusal shows up as exit 99 instead of starting a build.
mkdir -p "$scratch/fakebin"
printf '#!/bin/sh\necho "docker was called"\nexit 99\n' > "$scratch/fakebin/docker"
chmod +x "$scratch/fakebin/docker"
nodocker=(env "PATH=$scratch/fakebin:$PATH")

echo "# changed" >> "$scratch/halo/Dockerfile.backend"
expect "build refuses an uncommitted Dockerfile change" 1 "uncommitted changes" "${nodocker[@]}" bash "$scratch/halo/build-backend.sh"
expect "gate refuses an uncommitted Dockerfile change" 1 "uncommitted changes" "${nodocker[@]}" bash "$scratch/halo/upgrade-check.sh" "$new"
git -C "$scratch" checkout -q -- halo/Dockerfile.backend
echo "# changed" >> "$scratch/halo/requirements-halo.txt"
expect "build refuses an uncommitted pin change" 1 "uncommitted changes" "${nodocker[@]}" bash "$scratch/halo/build-backend.sh"
git -C "$scratch" checkout -q -- halo/requirements-halo.txt
touch "$scratch/halo/extra-input.txt"
expect "build refuses an untracked file in halo/" 1 "uncommitted changes" "${nodocker[@]}" bash "$scratch/halo/build-backend.sh"

relabelled=$(commit_with halo/release.conf "s/^PLANE_TAG=.*/PLANE_TAG=$new/")
expect "gate fails a branch relabelled to $new but not rebased" 1 "is not built on $new" \
  "${nodocker[@]}" bash halo/upgrade-check.sh "$new" --candidate "$relabelled" --check-only
outside=$(commit_with apps/api/plane/app/views/page/base.py '$a # changed outside Halo')
expect "gate fails a patch outside Halo's files" 1 "apps/api/plane/app/views/page/base.py" \
  "${nodocker[@]}" bash halo/upgrade-check.sh "$new" --candidate "$outside" --check-only
on_load=$(commit_with apps/api/plane/halo/views.py \
  '/^class HaloPagesDescriptionViewSet/a\    def retrieve(self, request, *args, **kwargs):\n        return None')
expect "gate fails Halo code on Plane's page load" 1 "the page load must stay Plane's" \
  "${nodocker[@]}" bash halo/upgrade-check.sh "$new" --candidate "$on_load" --check-only
expect "this branch passes the gate's checks" 0 "CHECK ONLY: 0 failed" \
  "${nodocker[@]}" bash halo/upgrade-check.sh "$new" --check-only
# Plane's code that Halo mirrors or replaces is unchanged in the new tag, so no fingerprint item may be open.
fingerprints=$("${nodocker[@]}" bash halo/upgrade-check.sh "$new" --check-only 2>&1 || true)
if grep -q "^OPEN Plane's" <<<"$fingerprints"; then
  echo "FAIL fingerprints of Plane's mirrored code match:"
  grep "^OPEN Plane's" <<<"$fingerprints" | sed 's/^/     /'
  failures=$((failures + 1))
else
  echo "PASS fingerprints of Plane's mirrored code match"
fi
expect "the gate refuses a release candidate before building" 1 "FAIL release order (n-1)" \
  "${nodocker[@]}" bash halo/upgrade-check.sh "$new"

# The release rule itself, on made-up tag lists (deployed, target, published tags).
py=$(command -v python3 || command -v python)
order() { "$py" halo/release_order.py "$@"; }
expect "n-1: the newest release is refused" 2 "newest release" order v1.4.2 v1.4.4 v1.4.2 v1.4.3 v1.4.4
expect "n-1: a release with a newer one out is allowed" 0 "behind Plane's newest release v1.4.4" \
  order v1.4.2 v1.4.3 v1.4.2 v1.4.3 v1.4.4
expect "n-1: two releases behind is allowed" 0 "behind Plane's newest release v1.5.1" \
  order v1.4.2 v1.5.0 v1.4.3 v1.5.0 v1.5.1 v1.6.0-rc1
expect "n-1: a release candidate is refused" 2 "release candidate" order v1.4.2 v1.4.3-rc2 v1.4.3-rc2 v1.4.4
expect "no downgrade" 1 "never downgrades" order v1.4.2 v1.4.1 v1.4.1 v1.4.2 v1.4.3
expect "no same release" 1 "never downgrades" order v1.4.2 v1.4.2 v1.4.2 v1.4.3
expect "a malformed tag is refused" 64 "not a Plane release tag" order v1.4.2 latest v1.4.3

echo "${failures} failed"
[ "$failures" = 0 ]

#!/usr/bin/env bash
# Build the Halo backend image: the upstream Plane backend image plus this branch's changes under apps/api.
#
#   bash halo/build-backend.sh [image tag]       (default plane-backend-halo:v1.4.2-pages.2)
#
# The patch is the committed diff from the Plane tag, and the Dockerfile and pins come from halo/, so uncommitted
# changes under apps/api or halo/ stop the build: the image is always exactly a commit (its source tag).
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."

. halo/release.conf
image=${1:-plane-backend-halo:v1.4.2-pages.2}

if ! git diff --quiet HEAD -- apps/api halo || [ -n "$(git ls-files --others --exclude-standard apps/api halo)" ]; then
  echo "FAIL: uncommitted changes under apps/api or halo/; commit them first" >&2
  exit 1
fi
mkdir -p halo/patch
git diff --binary "$PLANE_TAG" HEAD -- apps/api > halo/patch/halo.patch
echo "patch: $(grep -c '^diff --git' halo/patch/halo.patch) files, $(wc -l < halo/patch/halo.patch) lines"

docker build -f halo/Dockerfile.backend \
  --build-arg PLANE_IMAGE="makeplane/plane-backend:$PLANE_TAG" \
  --build-arg HALO_REVISION="$(git rev-parse HEAD)" \
  --build-arg HALO_TAG="$HALO_TAG" \
  -t "$image" halo
echo "built $image from $(git rev-parse --short HEAD)"

#!/usr/bin/env bash
# Build the Halo backend image: the upstream Plane backend image plus this branch's changes under apps/api.
#
#   bash halo/build-backend.sh [image tag]       (default plane-backend-halo:v1.4.2-pages.1)
#
# The patch is the committed diff from the Plane tag, so uncommitted changes under apps/api stop the build.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."

PLANE_TAG=v1.4.2
HALO_TAG=halo-v1.4.2-pages.1
image=${1:-plane-backend-halo:v1.4.2-pages.1}

if ! git diff --quiet HEAD -- apps/api || [ -n "$(git ls-files --others --exclude-standard apps/api)" ]; then
  echo "FAIL: uncommitted changes under apps/api; commit them first" >&2
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

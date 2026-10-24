#!/usr/bin/env bash
# Run Halo's backend tests (pytest) in the Halo backend image, with throwaway postgres and valkey.
#
#   bash halo/test-backend.sh [--dev] [pytest arguments]      (default arguments: plane/halo/tests)
#
# Without --dev it tests the built image plane-backend-halo:v1.4.2-pages.1 (halo/build-backend.sh).
# --dev tests the working tree instead: apps/api/plane is mounted over the upstream image's code.
# --upstream tests the unpatched upstream image (a baseline for Plane's own tests).
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
root=$(pwd -W 2>/dev/null || pwd)

base=${HALO_BACKEND_IMAGE:-plane-backend-halo:v1.4.2-pages.1}
mount=()
if [ "${1:-}" = "--dev" ]; then
  shift
  base=${HALO_UPSTREAM_IMAGE:-makeplane/plane-backend:v1.4.2}
  mount=(-v "$root/apps/api/plane:/code/plane:ro")
elif [ "${1:-}" = "--upstream" ]; then
  shift
  base=${HALO_UPSTREAM_IMAGE:-makeplane/plane-backend:v1.4.2}
fi
[ $# -gt 0 ] || set -- plane/halo/tests

tag=halo-backend-test:local
docker build -q -f halo/Dockerfile.backend-test --build-arg BASE_IMAGE="$base" -t "$tag" halo >/dev/null

net="halo-test-$$"
cleanup() {
  docker rm -f "$net-db" "$net-redis" >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create "$net" >/dev/null
docker run -d --name "$net-db" --network "$net" -e POSTGRES_USER=plane -e POSTGRES_PASSWORD=plane \
  -e POSTGRES_DB=plane postgres:15.7-alpine >/dev/null
docker run -d --name "$net-redis" --network "$net" valkey/valkey:7.2.11-alpine >/dev/null
for _ in $(seq 1 30); do
  docker exec "$net-db" pg_isready -U plane >/dev/null 2>&1 && break
  sleep 1
done

docker run --rm --network "$net" "${mount[@]}" \
  -e DATABASE_URL="postgresql://plane:plane@$net-db:5432/plane" \
  -e REDIS_URL="redis://$net-redis:6379/" \
  -e DJANGO_SETTINGS_MODULE=plane.settings.test -e SECRET_KEY=halo-test-secret -e WEB_URL=https://pm.applexus.net -e DEBUG=0 \
  --entrypoint python "$tag" -m pytest -p no:cacheprovider "$@"

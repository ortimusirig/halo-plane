#!/usr/bin/env bash
# Halo page edit end-to-end test: the Halo backend image, Plane's real live service, and a Node editor client.
#
#   bash halo/e2e/run.sh            (needs Docker and Node 20 or newer; HALO_E2E_KEEP=1 leaves the stack up)
#
# Uses the image built by halo/build-backend.sh (override with HALO_BACKEND_IMAGE) and ports 18000 and 13000
# on 127.0.0.1.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")"
compose=(docker compose -p halo-e2e -f compose.yaml)

stop() {
  if [ "${HALO_E2E_KEEP:-}" != "1" ]; then "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; fi
}
trap stop EXIT

"${compose[@]}" up -d
echo "waiting for the API"
for _ in $(seq 1 120); do
  curl -sf -o /dev/null http://127.0.0.1:18000/api/instances/ && break
  sleep 2
done
curl -sf -o /dev/null http://127.0.0.1:18000/api/instances/ || { "${compose[@]}" logs api | tail -40; exit 1; }
for _ in $(seq 1 30); do
  curl -sf -o /dev/null http://127.0.0.1:13000/live/health && break
  sleep 1
done

seed=$("${compose[@]}" exec -T api python manage.py shell < seed.py | grep '^SEED=' | tail -1 | cut -d= -f2-)
[ -n "$seed" ] || { echo "seed failed"; exit 1; }

[ -d node_modules ] || npm install --no-audit --no-fund --silent
SEED="$seed" COMPOSE_ARGS="compose -p halo-e2e -f $(pwd -W 2>/dev/null || pwd)/compose.yaml" node editor.test.mjs

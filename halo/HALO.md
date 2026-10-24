# Halo changes to Plane Community Edition

Halo (https://pm.applexus.net) is Applexus' self-hosted Plane Community Edition. This branch
(`halo/pages-api`) is Plane CE `v1.4.2` (commit `5f7d927`) plus the changes listed here. Plane is
licensed under the GNU Affero General Public License v3.0 only (see `LICENSE.txt`); these changes are
under the same licence. Plane's copyright notices are unchanged; every file Applexus added says so in
its header. No code from Plane's paid editions is used.

Source for Halo users (AGPL section 13): https://github.com/ortimusirig/halo-plane, one tag per Halo
build (this build: `halo-v1.4.2-pages.1`). The Halo image carries the tag and the commit in its
`org.opencontainers.image.version` and `org.opencontainers.image.revision` labels.

## What changes

1. Project page routes for API v1, signed in with a personal access token (`X-Api-Key`), under
   `/api/v1/workspaces/{slug}/projects/{project_id}/`:

   | Route | Methods |
   | --- | --- |
   | `pages/` | GET list (paginated), POST create |
   | `pages/{page_id}/` | GET retrieve, PATCH name and/or body |
   | `pages/{page_id}/archive/` | POST archive, DELETE unarchive |
   | `pages/{page_id}/versions/`, `pages/{page_id}/versions/{version_id}/` | GET |

   The views are Plane's own (`PageViewSet`, `PageVersionEndpoint`, their serializers and
   `ProjectPagePermission`), with Plane's API-key sign-in and rate limit. Private pages stay with their
   owner, project membership is required, guests follow Plane's guest rule, and only a page's owner or
   a project admin archives it. There is no delete route and no workspace page route.

2. Body edits that open and cached editors can merge (`plane/halo/edit.py`). A page's text is a Yjs
   document, and browsers keep a copy of it, so an edit is a Yjs update on that same document: the live
   service converts the new HTML (`/live/convert-document/`, the editor's own schema) and only the
   top-level blocks that differ are replaced (`plane/halo/ydoc.py`, pycrdt). An edit is refused while
   the page is locked, archived, open in Halo (a subscriber on live's Redis channel
   `hocuspocus:<page_id>`) or saved since the caller read it (`expected_updated_at`). After the save the
   update is published on that channel, so an editor that opened the page at that moment receives it.
   Each body edit writes a "before" and an "after" page version (`plane/halo/versions.py`), restorable
   from the page's version history in Halo.

3. Fix of Plane CE v1.4.2's page version history: `bgtasks/page_version_task.py` read
   `page.description`, which does not exist (`description_json` does), so no page version was ever
   written. Two lines changed. Drop this change once Plane fixes it upstream.

Files: `apps/api/plane/halo/` (new), `apps/api/plane/api/urls/__init__.py` (include),
`apps/api/plane/bgtasks/page_version_task.py` (fix), `apps/api/requirements/base.txt`
(`pycrdt==0.14.8`, MIT), and this `halo/` folder (build, tests, upgrade check).

## Build and test

```bash
bash halo/build-backend.sh                 # image plane-backend-halo:v1.4.2-pages.1
bash halo/test-backend.sh                  # Halo tests in that image (postgres and valkey in Docker)
bash halo/test-backend.sh --dev            # the same against the working tree
bash halo/test-backend.sh --upstream plane/tests   # Plane's own tests on the unpatched image (baseline)
bash halo/e2e/run.sh                       # end-to-end proof with Plane's real live service
```

`build-backend.sh` turns the committed diff from `v1.4.2` into `halo/patch/halo.patch`, and
`Dockerfile.backend` applies it with `git apply` to the code inside `makeplane/plane-backend:v1.4.2`;
a patch that no longer applies stops the build. The image replaces the `api`, `worker`, `beat-worker`
and `migrator` images of a Plane deployment; the patch has no database migrations. The live service
stays the upstream image; the API reaches it at `HALO_LIVE_URL` (default `http://live:3000/live/`).

## Upgrade

Halo runs one release behind Plane's newest and never downgrades. For a new Plane tag run
`bash halo/upgrade-check.sh <tag>`: it rebases a copy of this branch onto the tag, checks the patch
against the new image, builds and tests it, and reports whether Plane fixed the version bug, added its
own API page routes, or changed Hocuspocus or the live converter.

## Rollback

Set the four backend services back to `makeplane/plane-backend:v1.4.2`. Page versions written by the
patched build stay in `page_versions`; stock Plane reads them as normal versions.

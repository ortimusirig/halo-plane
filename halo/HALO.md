# Halo changes to Plane Community Edition

Halo (https://pm.applexus.net) is Applexus' self-hosted Plane Community Edition. This branch
(`halo/pages-api`) is Plane CE `v1.4.2` (commit `5f7d927`, see `halo/release.env`) plus the changes listed
here. Plane is licensed under the GNU Affero General Public License v3.0 only (see `LICENSE.txt`); these
changes are under the same licence. Plane's copyright notices are unchanged; every file Applexus added says
so in its header. No code from Plane's paid editions is used.

## Source for Halo users (AGPL section 13)

https://github.com/ortimusirig/halo-plane, one tag per Halo build (this build: `halo-v1.4.2-pages.1`).
The Halo image carries the tag and the commit in its `org.opencontainers.image.version` and
`org.opencontainers.image.revision` labels. Halo shows the link to the deployed tag on every page
(Applexus' halo.js) and in AURA's Halo guide; each Halo build gets a new tag and both links move to it.

The Corresponding Source of a Halo build is that tag plus the dependency sources listed below, all free to
download:

- The tag, as a zip (`https://github.com/ortimusirig/halo-plane/archive/refs/tags/<tag>.zip`): Plane's full
  source at `v1.4.2` with its LICENSE and notices, Applexus' changes, and the scripts that build and install
  the Halo image (`halo/build-backend.sh`, `halo/Dockerfile.backend`, `halo/requirements-halo.txt`). The base
  image `makeplane/plane-backend:v1.4.2` is built by Plane's own `apps/api/Dockerfile.api` from the same tree,
  with the Python packages pinned in `apps/api/requirements/` (Plane's choice, unchanged by Halo).
- pycrdt 0.14.8 (MIT), the one library Halo adds: source distribution `pycrdt-0.14.8.tar.gz`, sha256
  `45867f5ff08006d852d0cbb3e26b581977122b8f77dcd300359a16188b6cc931`, from PyPI and attached to the GitHub
  release of each tag; its Rust crates are pinned by the `Cargo.lock` inside it. Upstream:
  https://github.com/y-crdt/pycrdt.

Release checklist (before the patched image serves anyone): push the branch and tag; attach the pycrdt
source distribution to the tag's release; download the tag zip and the attachment while signed out; check
that the image's revision label is the tag's commit; install the halo.js link to that tag and see it in
Halo. Only then switch the backend services to the image.

## What changes

1. Project page routes for API v1, signed in with a personal access token (`X-Api-Key`), under
   `/api/v1/workspaces/{slug}/projects/{project_id}/`:

   | Route | Methods |
   | --- | --- |
   | `pages/` | GET list (paginated, with page links), POST create (name, description_html, access) |
   | `pages/{page_id}/` | GET retrieve (HTML, plain text, link, embedded work items), PATCH name and/or text (replace, append or restore a version) |
   | `pages/{page_id}/archive/` | POST archive (refused while the page is open), DELETE unarchive; both refused for pages with sub-pages |
   | `pages/{page_id}/versions/`, `pages/{page_id}/versions/{version_id}/` | GET (plain text added) |

   The views are Plane's own (`PageViewSet`, `PageVersionEndpoint`, their serializers and
   `ProjectPagePermission`), with Plane's API-key sign-in and rate limit. Private pages stay with their
   owner, project membership is required, guests follow Plane's guest rule (also on versions), and only a
   page's owner or a project admin archives it. There is no delete route and no workspace page route.

2. Text edits that open and cached editors can merge (`plane/halo/edit.py`). A page's text is a Yjs
   document, and browsers keep a copy of it, so an edit is a Yjs update on that same document: the live
   service converts the new HTML (`/live/convert-document/`, the editor's own schema) and a sequence diff of
   the top-level blocks (`plane/halo/ydoc.py`, pycrdt) replaces only the blocks that differ, ignoring the
   block ids the browser editor adds. `append_html` adds blocks at the end and deletes nothing.
   - Every edit names the `updated_at` it was made on (`expected_updated_at`, required).
   - One edit of a page at a time (a Redis lease, `plane/halo/lease.py`).
   - Refused while the page is locked, archived, open in Halo or saved since it was read. "Open" means a live
     server holds the page: it answers a Hocuspocus QueryAwareness message on live's Redis channel
     `hocuspocus:<page_id>` (live stays subscribed to that channel after a page closes, so the subscriber
     count alone is not used).
   - Refused when it would remove or swap an image, a mention or an embedded work item, unless the caller
     sends `allow_removal`.
   - After the save the update is published on the page's channel, so an editor that opened the page at that
     moment receives it.
   - Each text edit writes a "before" and an "after" page version, marked so that Plane's version task never
     merges a later save into them (`plane/halo/versions.py`), restorable from the page's version history.

3. Plane's page description endpoint, through which the web app and live load and save pages, is replaced by
   a subclass (`HaloPagesDescriptionViewSet`, routed first by `plane/halo/app_urls.py`). Loading waits while an
   API edit of the page runs. A save that lacks part of the stored document (an API edit that editor never
   received) is merged with it instead of overwriting it, and the missing part is sent to that editor through
   live. Such a save's HTML is that editor's older text, so the page is marked in Halo's own table
   (`halo_page_text_behind`, same transaction) until that editor's next, complete save: reads report
   `text_complete: false`, API edits wait, and the merged save makes no page version. A page's first document
   is built here from its HTML (live's converter) when it is first loaded, so live never converts on its own
   while an API edit writes the same page. The save itself mirrors Plane's code, with its background tasks
   started after commit.

4. Fix of Plane CE v1.4.2's page version history: `bgtasks/page_version_task.py` read `page.description`,
   which does not exist (`description_json` does), so no page version was ever written. The same task skips
   Halo's marked versions when it merges saves.

Files: `apps/api/plane/halo/` (new, a Django app labelled `halo` with one table and its migration),
`apps/api/plane/settings/common.py` (the app in `INSTALLED_APPS`), `apps/api/plane/api/urls/__init__.py` and
`apps/api/plane/urls.py` (includes), `apps/api/plane/bgtasks/page_version_task.py` (fix and marked versions),
`apps/api/requirements/base.txt` (`pycrdt==0.14.8`), and this `halo/` folder (build, tests, upgrade gate).

Known limits: typing on an open page can be lost only where an API edit rewrote the same characters while
live missed the half-second open-page probe. Pages over 100 kB of HTML (live's converter limit) cannot be
edited through the API, and their first document is built by live itself, as in stock Plane. A marked page
stays marked until an editor saves it again (any small change).

## Build and test

```bash
bash halo/build-backend.sh                 # image plane-backend-halo:v1.4.2-pages.1
bash halo/test-backend.sh                  # Halo tests in that image (postgres and valkey in Docker)
bash halo/test-backend.sh --dev            # the same against the working tree
bash halo/test-backend.sh --upstream plane/tests   # Plane's own tests on the unpatched image (baseline)
bash halo/e2e/run.sh                       # end-to-end proof with Plane's real live service
```

`build-backend.sh` turns the committed diff from `PLANE_TAG` into `halo/patch/halo.patch`, and
`Dockerfile.backend` applies it with `git apply` to the code inside `makeplane/plane-backend:v1.4.2`;
a patch that no longer applies stops the build. The image replaces the `api`, `worker`, `beat-worker`
and `migrator` images of a Plane deployment; run the migrator once (Halo's table, migration
`halo.0001_initial`; no change to Plane's tables). The live service
stays the upstream image; the API reaches it at `HALO_LIVE_URL` (default `http://live:3000/live/`).

## Upgrade

Halo runs one release behind Plane's newest and never downgrades. For a new Plane tag run
`bash halo/upgrade-check.sh <tag>`, the release gate: it checks the patch against the new tag, builds the
image on the new upstream image, runs the Halo tests and the end-to-end test with the new live image, and
reports what needs a decision (the version bug fixed upstream, Plane's own API page routes, a moved
description endpoint, a changed Hocuspocus or converter). `--check-only` gives the report without building;
it is not a gate.

## Rollback

Set the four backend services back to `makeplane/plane-backend:v1.4.2`. Page versions written by the
patched build stay in `page_versions`; stock Plane reads them as normal versions. The table
`halo_page_text_behind` stays and is unused by stock Plane (drop it only when Halo's patch is retired).

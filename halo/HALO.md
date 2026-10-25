# Halo changes to Plane Community Edition

Halo (https://pm.applexus.net) is Applexus' self-hosted Plane Community Edition. This branch
(`halo/pages-api`) is Plane CE `v1.4.2` (commit `5f7d927`, see `halo/release.conf`) plus the changes listed
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
   | `pages/` | GET list (paginated, with page links), POST create (name, description_html, access; not in archived projects) |
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
   - Refused for a page that has no editor document yet (made in the web app and not opened since):
     `page_not_ready`, "open it once in Halo". Reads say so beforehand (`text_editable`, with a reason and a
     hint). A page created through the API gets its first document at once (live's converter, the page's name
     as title), unless live does not answer or the HTML is too large, which the create answer says.
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

3. Plane's web-app page routes that save a page (description, the page itself, lock, access, duplicate) are
   answered first by subclasses of Plane's own views (`plane/halo/app_urls.py`), with the same paths, methods
   and answers. Existing behaviour stays as in Plane: nothing there is refused or sanitised beyond Plane's own
   checks; Halo only locks rows and marks pages, for the sake of its API.

   - Loading a page is Plane's own code: the description GET runs Plane's `PagesDescriptionViewSet.retrieve`
     (Halo's subclass does not override it; the upgrade gate fails if it defines anything but its save). No
     Halo wait, Redis call or conversion runs on a load; a page without a document is converted by live
     itself, as in stock Plane.
   - Saving mirrors Plane's code, with its background tasks started after commit. A save that lacks part of the
     stored document (an API edit that editor never received) and shares its history is merged with it instead
     of overwriting it, and the missing part is sent to that editor through live. A save that shares no history
     with the stored document (two separately converted first documents of one page, such as live's and the
     browser's offline fallback's) replaces it, as Plane's save does. When the stored HTML and document
     disagree (a merged save, or a save of only one of them) the page is marked in Halo's own table
     (`halo_page_text_behind`, same transaction) until a save of both: Halo's API then reports
     `text_complete: false` and its edits wait, and the merged save makes no page version and leaves Plane's
     mention index alone (its HTML lacks the API edit's parts; the editor's next, complete save updates both).
   - Plane's mention index (`PageLog`): every ordinary save keeps Plane's own `page_transaction` with Plane's
     arguments. Only merged saves differ: they are not indexed and mark the page `merged`, and the complete
     save that clears that mark queues Halo's `tasks.index_page`, which under the page's row lock brings the
     index in line with the stored text (and leaves a page merged-marked again alone).
   - Lock, unlock and access run Plane's own code after locking the page's row, so none of them can write
     old text back over an API edit that committed in between. The properties PATCH is a copy of Plane's
     `PageViewSet.partial_update` (same checks, serializer and answers; fingerprinted by the upgrade gate) under
     the same row lock, with its `page_transaction` task queued after commit, so a broker failure cannot undo
     the save. It writes `description_html` as in Plane (the web app's store sends its whole copy of the page);
     when that changes the HTML, the page is marked as above.
   - When Redis or the task broker fail, Plane's routes behave as in Plane: a saved change is still answered
     as saved, and Halo's coordination steps are logged and skipped. Halo's Redis calls time out after a
     second.
   - Duplicate (`HaloPageDuplicateEndpoint`) repeats Plane's steps, locks the source page and makes no
     conversion: the copy takes the source's document with its own name as title (a source without one gives a
     copy without one, as in Plane), and a source whose HTML lags its document passes that mark on. Plane's
     copy task (it copies the images, then saves the whole page it read, twice) is replaced for pages by Halo's
     `finish_page_copy`, built from Plane's own helpers and fingerprinted by the upgrade gate: it copies the
     uploaded images (asset ids, in `image-component` and `img`; images from the web stay as they are) and
     points the copy's HTML, JSON and document at them in one update, made only if none of them
     changed since it read them, and publishes the document change to live. So it never writes back a name,
     archive, lock or access change, nor overwrites an edit, however late it runs; a copy saved between its read
     and its write keeps the original's images, as stock Plane can end too. The end-to-end test checks that a
     copy with an uploaded image gets the same text and an own copy of the image file, as Plane's own copy task
     gives. Until the task has run the copy is marked in a second table (`halo_page_being_copied`): API edits of
     it are refused (`page_not_ready`); a mark older than two minutes (a stuck or dead worker) is ignored.

4. Fix of Plane CE v1.4.2's page version history: `bgtasks/page_version_task.py` read `page.description`,
   which does not exist (`description_json` does), so no page version was ever written. The same task skips
   Halo's marked versions when it merges saves.

Files: `apps/api/plane/halo/` (new, a Django app labelled `halo` with two tables and their migration),
`apps/api/plane/settings/common.py` (the app in `INSTALLED_APPS`), `apps/api/plane/api/urls/__init__.py` and
`apps/api/plane/urls.py` (includes), `apps/api/plane/bgtasks/page_version_task.py` (fix and marked versions),
`apps/api/requirements/base.txt` (`pycrdt==0.14.8`), and this `halo/` folder (build, tests, upgrade gate).

Known limits: typing on an open page can be lost only where an API edit rewrote the same characters while
live missed the half-second open-page probe. Pages over 100 kB of HTML (live's converter limit) cannot be
edited through the API. A page made in the web app and not opened since cannot be edited through the API until
someone opens it once (live builds its document then). A marked page stays marked until an editor saves it
again (any small change). When two separately converted first documents of a page meet (the browser's offline
fallback and live at once, on a page nobody opened before), the later save replaces the earlier, as in Plane;
an API edit made between them is replaced too and stays restorable from its versions.
Background tasks go through Plane's broker, as in Plane. When it is down, a change made through Halo's routes
is still saved and answered once; its mentions index (`page_transaction`), version task or a copy's image
copies are skipped and logged.

## Build and test

```bash
bash halo/build-backend.sh                 # image plane-backend-halo:v1.4.2-pages.1
bash halo/test-backend.sh                  # Halo tests in that image (postgres and valkey in Docker)
bash halo/test-backend.sh --dev            # the same against the working tree
bash halo/test-backend.sh --upstream plane/tests   # Plane's own tests on the unpatched image (baseline)
bash halo/e2e/run.sh                       # end-to-end proof with Plane's real live service
bash halo/test-release-scripts.sh          # refusals of the build and the gate (no Docker)
```

`build-backend.sh` turns the committed diff from `PLANE_TAG` into `halo/patch/halo.patch`, and
`Dockerfile.backend` applies it with `git apply` to the code inside `makeplane/plane-backend:v1.4.2`;
a patch that no longer applies stops the build, and so does any uncommitted change under `apps/api` or `halo/`
(the image is always exactly one commit, the one its source tag names). The image replaces the `api`, `worker`, `beat-worker`
and `migrator` images of a Plane deployment; run the migrator once (Halo's two tables, migration
`halo.0001_initial`; no change to Plane's tables). The live service
stays the upstream image; the API reaches it at `HALO_LIVE_URL`, else at Plane's own `LIVE_BASE_URL` plus
`LIVE_BASE_PATH`, else at `http://live:3000/live/` (live on Plane's compose network).

## Upgrade

Halo runs one release behind Plane's newest and never downgrades: the gate refuses a release candidate,
Plane's newest release and any tag not newer than the deployed one (`halo/release_order.py`). For a new
Plane tag run `bash halo/upgrade-check.sh <tag>`, the release gate: it checks the patch against the new tag, builds the
image on the new upstream image, runs the Halo tests and the end-to-end test with the new live image, and
reports what needs a decision (the version bug fixed upstream, Plane's own API page routes, a changed
description or duplicate endpoint, a changed copy task, a changed Hocuspocus or converter). It passes only for a
candidate branch built on the new tag (the tag in its history, not only in its `release.conf`) whose `HALO_TAG` is
tagged on the candidate's commit, so the image and its published source are the same tree; and it fails a patch
that changes Plane files beyond `apps/api/plane/halo/` and the list in `release.conf` (`PATCH_PLANE_FILES`).
`--check-only` gives the report without building; it is not a gate.

## Rollback

Set the four backend services back to `makeplane/plane-backend:v1.4.2`. Page versions written by the
patched build stay in `page_versions`; stock Plane reads them as normal versions. The tables
`halo_page_text_behind` and `halo_page_being_copied` stay and are unused by stock Plane (drop them only when
Halo's patch is retired).

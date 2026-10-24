// Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.
//
// Halo page edits against Plane's real live service. A Node Hocuspocus client stands in for the browser
// editor; keeping its Y.Doc across a disconnect stands in for the browser's IndexedDB copy.
// Run by halo/e2e/run.sh with SEED (JSON from seed.py), API and LIVE_WS in the environment.

import { execFileSync } from "node:child_process";
import { HocuspocusProvider } from "@hocuspocus/provider";
import WebSocket from "ws";
import * as Y from "yjs";

const seed = JSON.parse(process.env.SEED);
const API = process.env.API ?? "http://127.0.0.1:18000";
const LIVE_WS = process.env.LIVE_WS ?? "ws://127.0.0.1:13000/live/collaboration";
const COMPOSE = process.env.COMPOSE_ARGS?.split(" ") ?? ["compose", "-p", "halo-e2e"];
const base = `${API}/api/v1/workspaces/${seed.slug}/projects/${seed.project_id}/`;

let failures = 0;
const pass = (name) => console.log(`PASS ${name}`);
const fail = (name, detail) => {
  failures += 1;
  console.log(`FAIL ${name}: ${detail}`);
};
const check = (name, ok, detail) => (ok ? pass(name) : fail(name, detail));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(fn, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const value = await fn();
    if (value) return value;
    await sleep(250);
  }
  return null;
}

async function api(method, path, body) {
  for (let attempt = 1; ; attempt += 1) {
    try {
      const response = await fetch(base + path, {
        method,
        headers: { "X-Api-Key": seed.token, "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      return { status: response.status, data: text ? JSON.parse(text) : null };
    } catch (error) {
      // Node's fetch can reuse an idle keep-alive socket the API (uvicorn, 5 s) has just closed; the request never
      // reaches the API. A read is safe to send again; a write is not.
      if (method !== "GET" || attempt > 1 || error?.cause?.code !== "UND_ERR_SOCKET") throw error;
    }
  }
}

const textOf = (node) =>
  node instanceof Y.XmlText
    ? node.toDelta().map((d) => (typeof d.insert === "string" ? d.insert : "")).join("")
    : node.toArray().map(textOf).join("");
const blocks = (doc) => doc.getXmlFragment("default").toArray().map(textOf);
const textAt = (doc, index) => doc.getXmlFragment("default").get(index).toArray().find((n) => n instanceof Y.XmlText);

function connect(doc, pageId) {
  return new Promise((resolve, reject) => {
    const params = new URLSearchParams({
      documentType: "project_page",
      projectId: seed.project_id,
      workspaceSlug: seed.slug,
    });
    const provider = new HocuspocusProvider({
      url: `${LIVE_WS}?${params}`,
      name: pageId,
      document: doc,
      token: JSON.stringify({ id: seed.user_id, cookie: seed.cookie }),
      WebSocketPolyfill: WebSocket,
      onSynced: () => resolve(provider),
      onAuthenticationFailed: ({ reason }) => reject(new Error(`live refused the editor: ${reason}`)),
    });
    setTimeout(() => reject(new Error("editor did not sync within 20 s")), 20000);
  });
}

function closeEditor(provider) {
  // As Plane's web app does when a page closes: the provider and its websocket both go.
  const socket = provider.configuration.websocketProvider;
  provider.destroy();
  socket.disconnect();
  socket.destroy();
}

function inApi(code) {
  return execFileSync("docker", [...COMPOSE, "exec", "-T", "api", "python", "manage.py", "shell", "-c", code], {
    encoding: "utf8",
  }).trim();
}

const page = async (id) => (await api("GET", `pages/${id}/`)).data;
// Whether live still holds the page, asked the way the API asks before an edit.
const closed = async (id) =>
  inApi(`from plane.halo import live; print(live.is_open("${id}"))`).split(/\r?\n/).pop() === "False";

async function versionTexts(id) {
  const list = (await api("GET", `pages/${id}/versions/`)).data ?? [];
  const texts = [];
  for (const version of list) texts.push((await api("GET", `pages/${id}/versions/${version.id}/`)).data.description_html);
  return texts;
}

// 1. A page created through the API.
const created = await api("POST", "pages/", { name: "E2E page", description_html: "<p>Alpha</p><p>Beta</p><p>Gamma</p>" });
check("1 create page through the API", created.status === 201, JSON.stringify(created));
const pageId = created.data.id;

// 2. Editor A opens it; live builds the document from the HTML.
const docA = new Y.Doc();
let editorA = await connect(docA, pageId);
await waitFor(() => blocks(docA).length === 3);
check("2 editor opens the page", JSON.stringify(blocks(docA)) === '["Alpha","Beta","Gamma"]', JSON.stringify(blocks(docA)));
// The first document came from Halo (built from the HTML when live loaded the page), not from live's own conversion.
const built = inApi(
  `from plane.db.models import Page; p = Page.objects.get(pk="${pageId}"); print(bool(p.description_binary))`,
);
check("2b Halo built the page's first document", built.split(/\r?\n/).pop() === "True", built);

// 3. An API edit while A has it open is refused, and nothing changes.
const refused = await api("PATCH", `pages/${pageId}/`, {
  description_html: "<p>Alpha</p><p>Beta X</p><p>Gamma</p>",
  expected_updated_at: (await page(pageId)).updated_at,
});
check("3a edit refused while open", refused.status === 409 && refused.data.code === "page_open", JSON.stringify(refused));
await sleep(1000);
check("3b editor and stored text unchanged",
  JSON.stringify(blocks(docA)) === '["Alpha","Beta","Gamma"]' && !(await page(pageId)).description_html.includes("Beta X"),
  JSON.stringify(blocks(docA)));

// 4. A types and closes the page; live saves it, and Plane's version history records it (the backported fix).
textAt(docA, 0).insert(textAt(docA, 0).length, " typed live");
await sleep(500);
closeEditor(editorA);
const saved = await waitFor(async () => (await closed(pageId)) && (await page(pageId)).description_html.includes("Alpha typed live"));
check("4a live saved the editor's typing", Boolean(saved), (await page(pageId)).description_html);
const tracked = await waitFor(async () => (await versionTexts(pageId)).some((html) => html.includes("Alpha typed live")), 20000);
check("4b a save in Halo creates a page version", Boolean(tracked), JSON.stringify(await versionTexts(pageId)));

// 5. The browser's cached copy, with offline typing in another block and in the block AURA will edit.
const docA2 = new Y.Doc();
Y.applyUpdate(docA2, Y.encodeStateAsUpdate(docA));
textAt(docA2, 2).insert(textAt(docA2, 2).length, " offline");
textAt(docA2, 1).insert(0, "Draft: ");

// 6. The API edit (AURA) on the closed page. Live is still subscribed to the page's channel (it does not
// unsubscribe after the last editor leaves), so this also proves a closed page is told apart from an open one.
const subscribers = inApi(
  `from plane.settings.redis import redis_instance; print(redis_instance().pubsub_numsub("hocuspocus:${pageId}")[0][1])`,
);
check("6 live still subscribed to the closed page", Number(subscribers.split("\n").pop()) >= 1, subscribers);
const current = await page(pageId);
const editedHtml = current.description_html.replace(">Beta<", ">Beta edited by AURA<");
check("6a page HTML has the Beta block", editedHtml !== current.description_html, current.description_html);
const edited = await api("PATCH", `pages/${pageId}/`, {
  description_html: editedHtml,
  expected_updated_at: current.updated_at,
});
check("6b edit applied with before and after versions",
  edited.status === 200 && edited.data.changed && edited.data.before_version_id && edited.data.after_version_id,
  JSON.stringify(edited.data));

// 7. The cached copy reconnects: everyone's text is there once.
editorA = await connect(docA2, pageId);
await waitFor(() => blocks(docA2)[1] === "Draft: Beta edited by AURA");
check("7 cached copy merges with no duplicates, offline typing in AURA's block kept",
  JSON.stringify(blocks(docA2)) === '["Alpha typed live","Draft: Beta edited by AURA","Gamma offline"]',
  JSON.stringify(blocks(docA2)));

// 8. Saved text has all three changes; the before version has the original Beta.
closeEditor(editorA);
const final = await waitFor(async () => {
  const html = (await page(pageId)).description_html;
  return (await closed(pageId)) && html.includes("Gamma offline") && html.includes("Draft: Beta edited by AURA") && html.includes("Alpha typed live") ? html : null;
});
check("8a saved text has every change", Boolean(final), (await page(pageId)).description_html);
const before = (await api("GET", `pages/${pageId}/versions/${edited.data.before_version_id}/`)).data.description_html;
const after = (await api("GET", `pages/${pageId}/versions/${edited.data.after_version_id}/`)).data.description_html;
check("8b before version keeps the old Beta", before.includes(">Beta<") && !before.includes("AURA"), before);
check("8c after version has AURA's Beta", after.includes("Beta edited by AURA"), after);

// 9. An update published on live's channel reaches an editor that has the page open.
const docB = new Y.Doc();
const editorB = await connect(docB, pageId);
await waitFor(() => blocks(docB).length === 3);
const reached = inApi(
  [
    "from plane.db.models import Page",
    "from plane.halo import live, ydoc",
    `p = Page.objects.get(pk="${pageId}")`,
    'body, _ = live.convert_html(p.description_html.replace("Gamma offline", "Gamma pushed"))',
    "e = ydoc.edit_document(bytes(p.description_binary), body)",
    "print(live.publish_update(p.id, e.update))",
  ].join("\n"),
);
const pushed = await waitFor(() => blocks(docB)[2] === "Gamma pushed", 5000);
check("9 published update reaches the open editor", reached.endsWith("1") && Boolean(pushed),
  `receivers=${reached} blocks=${JSON.stringify(blocks(docB))}`);
const archivedOpen = await api("POST", `pages/${pageId}/archive/`);
check("9b archive refused while the page is open", archivedOpen.status === 409 && archivedOpen.data.code === "page_open",
  JSON.stringify(archivedOpen));
closeEditor(editorB);

// 10. Undo through the API: the before version restored from its own document, the same safe way.
await waitFor(() => closed(pageId));
const beforeEdit = await page(pageId);
const restored = await api("PATCH", `pages/${pageId}/`, {
  restore_version_id: edited.data.before_version_id,
  expected_updated_at: beforeEdit.updated_at,
});
const restoredHtml = (await page(pageId)).description_html;
check("10 restoring the before version brings the old Beta back",
  restored.status === 200 && restoredHtml.includes(">Beta<") && !restoredHtml.includes("AURA"),
  `${restored.status} ${restoredHtml}`);

// 11. Plane's duplicate from the web app: the copy stays marked until the worker ran Plane's copy task
// (through Halo's finish_page_copy, so this also proves the worker finds Halo's tasks); then it opens and the
// API can edit it.
const duplicated = await fetch(`${API}/api/workspaces/${seed.slug}/projects/${seed.project_id}/pages/${pageId}/duplicate/`, {
  method: "POST",
  headers: { Cookie: seed.cookie, "Content-Type": "application/json" },
});
const copy = await duplicated.json();
check("11a duplicate in the web app", duplicated.status === 201 && copy.name === "E2E page (Copy)", JSON.stringify(copy));
const unmarked = await waitFor(async () =>
  inApi(`from plane.halo.models import PageBeingCopied; print(PageBeingCopied.objects.filter(page_id="${copy.id}").exists())`)
    .split(/\r?\n/).pop() === "False", 30000);
check("11b the worker ran the copy task and removed the mark", Boolean(unmarked), "the copy is still marked");
const docC = new Y.Doc();
const editorC = await connect(docC, copy.id);
await waitFor(() => blocks(docC).length === 3);
check("11c the copy opens with the page's text", blocks(docC)[1] === "Beta", JSON.stringify(blocks(docC)));
closeEditor(editorC);
await waitFor(() => closed(copy.id));
const appended = await api("PATCH", `pages/${copy.id}/`, {
  append_html: "<p>Added to the copy</p>",
  expected_updated_at: (await page(copy.id)).updated_at,
});
check("11d the API edits the copy", appended.status === 200 && appended.data.changed, JSON.stringify(appended.data));

console.log(failures ? `${failures} FAILED` : "ALL PASSED");
process.exit(failures ? 1 : 0);

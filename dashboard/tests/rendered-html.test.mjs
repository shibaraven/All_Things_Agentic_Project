import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("https://shiftzero.test/", { headers: { accept: "text/html", host: "shiftzero.test" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the ShiftZero command center", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>ShiftZero — Autonomous Factory Operations<\/title>/i);
  assert.match(html, /SHIFTZERO/);
  assert.match(html, /A factory that plans/);
  assert.match(html, /Live factory map/);
  assert.match(html, /Agent activity/);
  assert.match(html, /Security boundary/);
  assert.match(html, /FORTIFIED AGENT FLEET/);
  assert.match(html, /GOOGLE CLOUD PROOF/);
  assert.match(html, /https:\/\/shiftzero\.test\/og\.png/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("ships the finished social card and removes starter preview assets", async () => {
  const [page, layout, packageJson, socialCard] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    access(new URL("../public/og.png", import.meta.url)),
  ]);

  assert.match(page, /NEXT_PUBLIC_SHIFTZERO_API_URL/);
  assert.match(page, /EventSource/);
  assert.match(page, /SAFE DEMO REPLAY/);
  assert.match(page, /\/api\/agents/);
  assert.match(page, /\/api\/evidence\/status/);
  assert.match(page, /\/api\/incidents\/.*\/trace/);
  assert.match(page, /INCIDENT TRACE/);
  assert.match(page, /AGV07 Battery 21%/);
  assert.match(page, /affected_entities: \[kind === "BLOCK_AGV" \? "AGV03" : "AGV07"\]/);
  assert.match(page, /last verified E2E/);
  assert.match(page, /prompt_attack-/);
  assert.doesNotMatch(page, /NEXT_PUBLIC_SHIFTZERO_DEMO_TOKEN|X-Demo-Token|shiftzero-local-demo/);
  assert.match(layout, /generateMetadata/);
  assert.match(packageJson, /"name": "shiftzero-command-center"/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.equal(socialCard, undefined);
  await assert.rejects(access(new URL("../app/_sites-preview/SkeletonPreview.tsx", import.meta.url)));
});

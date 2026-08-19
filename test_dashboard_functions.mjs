import assert from "node:assert/strict";
import test from "node:test";

import statusHandler from "./netlify/functions/crawl-status.mjs";
import startHandler from "./netlify/functions/start-crawl.mjs";

function configureEnvironment() {
  process.env.GITHUB_OWNER = "alexander";
  process.env.GITHUB_REPO = "find-free-products";
  process.env.GITHUB_BRANCH = "main";
  process.env.GITHUB_DISPATCH_TOKEN = "secret-token";
  process.env.DASHBOARD_ADMIN_KEY = "private-dashboard-key";
}

test("start endpoint rejects a wrong dashboard key without calling GitHub", async () => {
  configureEnvironment();
  let called = false;
  global.fetch = async () => {
    called = true;
    return new Response(null, { status: 204 });
  };
  const response = await startHandler(new Request("https://site.test/api/crawl/start", {
    method: "POST",
    headers: { "content-type": "application/json", "x-admin-key": "wrong-key" },
    body: JSON.stringify({ maxRuntimeMinutes: "300" }),
  }));
  assert.equal(response.status, 401);
  assert.equal(called, false);
});

test("start endpoint dispatches a validated overnight run", async () => {
  configureEnvironment();
  let captured;
  global.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(null, { status: 204 });
  };
  const response = await startHandler(new Request("https://site.test/api/crawl/start", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-admin-key": "private-dashboard-key",
    },
    body: JSON.stringify({ maxRuntimeMinutes: "300" }),
  }));
  assert.equal(response.status, 202);
  assert.match(captured.url, /nightly-crawl\.yml\/dispatches$/);
  assert.equal(captured.init.method, "POST");
  assert.deepEqual(JSON.parse(captured.init.body), {
    ref: "main",
    inputs: { max_runtime_minutes: "300" },
  });
  assert.equal(captured.init.headers.authorization, "Bearer secret-token");
});

test("status endpoint returns only safe workflow fields", async () => {
  configureEnvironment();
  global.fetch = async () => new Response(JSON.stringify({
    workflow_runs: [{
      id: 42,
      status: "in_progress",
      conclusion: null,
      event: "workflow_dispatch",
      created_at: "2026-08-19T20:00:00Z",
      updated_at: "2026-08-19T20:01:00Z",
      html_url: "https://github.com/alexander/find-free-products/actions/runs/42",
      sensitive_extra_field: "must not leak",
    }],
  }), { status: 200, headers: { "content-type": "application/json" } });

  const response = await statusHandler(new Request("https://site.test/api/crawl/status"));
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(payload.runs.length, 1);
  assert.equal(payload.runs[0].id, 42);
  assert.equal("sensitive_extra_field" in payload.runs[0], false);
});

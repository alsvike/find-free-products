import { githubRequest, githubSettings, json, validAdminKey } from "./_github.mjs";

export default async function handler(request) {
  if (request.method !== "POST") return json({ error: "Method not allowed." }, 405);
  if (!validAdminKey(request)) return json({ error: "Wrong dashboard key." }, 401);

  const settings = githubSettings();
  if (!settings) return json({ error: "GitHub runner settings are not configured in Netlify." }, 503);

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "Invalid request." }, 400);
  }
  const duration = String(body.maxRuntimeMinutes || "300");
  if (!["60", "180", "300"].includes(duration)) {
    return json({ error: "Unsupported run length." }, 400);
  }

  const response = await githubRequest(
    settings,
    `/repos/${settings.owner}/${settings.repo}/actions/workflows/nightly-crawl.yml/dispatches`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        ref: settings.branch,
        inputs: { max_runtime_minutes: duration },
      }),
    },
  );
  if (!response.ok) {
    console.error("GitHub dispatch failed", response.status, await response.text());
    return json({ error: "GitHub rejected the start request. Check the Netlify token settings." }, 502);
  }
  return json({ ok: true, message: "Crawler queued." }, 202);
}

export const config = { path: "/api/crawl/start" };


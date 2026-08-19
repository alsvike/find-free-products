import { githubRequest, githubSettings, json } from "./_github.mjs";

export default async function handler(request) {
  if (request.method !== "GET") return json({ error: "Method not allowed." }, 405);
  const settings = githubSettings();
  if (!settings) return json({ error: "GitHub runner settings are not configured in Netlify." }, 503);

  const response = await githubRequest(
    settings,
    `/repos/${settings.owner}/${settings.repo}/actions/workflows/nightly-crawl.yml/runs?per_page=5`,
  );
  if (!response.ok) {
    console.error("GitHub status failed", response.status, await response.text());
    return json({ error: "Could not read GitHub runner status." }, 502);
  }
  const payload = await response.json();
  const runs = (payload.workflow_runs || []).map((run) => ({
    id: run.id,
    status: run.status,
    conclusion: run.conclusion,
    event: run.event,
    created_at: run.created_at,
    updated_at: run.updated_at,
    html_url: run.html_url,
  }));
  return json({ runs });
}

export const config = { path: "/api/crawl/status" };


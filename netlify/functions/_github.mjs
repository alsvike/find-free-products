import { timingSafeEqual } from "node:crypto";

export function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

export function githubSettings() {
  const owner = process.env.GITHUB_OWNER;
  const repo = process.env.GITHUB_REPO;
  const token = process.env.GITHUB_DISPATCH_TOKEN;
  const branch = process.env.GITHUB_BRANCH || "main";
  if (!owner || !repo || !token) return null;
  if (!/^[A-Za-z0-9_.-]+$/.test(owner) || !/^[A-Za-z0-9_.-]+$/.test(repo)) return null;
  return { owner, repo, token, branch };
}

export function validAdminKey(request) {
  const expected = process.env.DASHBOARD_ADMIN_KEY || "";
  const supplied = request.headers.get("x-admin-key") || "";
  const expectedBytes = Buffer.from(expected);
  const suppliedBytes = Buffer.from(supplied);
  if (!expected || expectedBytes.length !== suppliedBytes.length) return false;
  return timingSafeEqual(expectedBytes, suppliedBytes);
}

export async function githubRequest(settings, path, init = {}) {
  return fetch(`https://api.github.com${path}`, {
    ...init,
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${settings.token}`,
      "x-github-api-version": "2022-11-28",
      "user-agent": "gratisjagten-netlify-dashboard",
      ...(init.headers || {}),
    },
  });
}

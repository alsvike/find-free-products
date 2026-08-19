const numberFormatter = new Intl.NumberFormat("da-DK");
const dashboardConfig = window.GRATISJAGTEN_CONFIG || {};

function githubDataUrl(filename) {
  const { githubOwner, githubRepo, githubBranch = "main" } = dashboardConfig;
  const configured = githubOwner && githubRepo && !githubOwner.startsWith("YOUR_");
  if (!configured) return `/data/${filename}`;
  return `https://raw.githubusercontent.com/${encodeURIComponent(githubOwner)}/${encodeURIComponent(githubRepo)}/${encodeURIComponent(githubBranch)}/site/data/${filename}`;
}

function parseCsv(text, delimiter = ";") {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === delimiter) {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += char;
    }
  }

  if (field || row.length) {
    row.push(field.replace(/\r$/, ""));
    rows.push(row);
  }

  const cleanRows = rows.filter((values) => values.some((value) => value.trim()));
  if (!cleanRows.length) return [];
  const headers = cleanRows[0].map((header) => header.replace(/^\uFEFF/, "").trim());
  return cleanRows.slice(1).map((values) =>
    Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]))
  );
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
}

function evidenceLabel(matchType) {
  const labels = {
    structured_price_zero: "Structured price",
    meta_price_zero: "Metadata price",
    visible_product_price_zero: "Visible 0 kr.",
    visible_product_price_free: "Visible free",
    free_product_heading_with_buy_action: "Free title + cart",
  };
  return labels[matchType] || matchType.replaceAll("_", " ");
}

function latestStatuses(rows) {
  const latest = new Map();
  rows.forEach((row) => {
    if (row.domain) latest.set(row.domain, row);
  });
  return [...latest.values()];
}

function renderResults(findings) {
  const grid = document.querySelector("#results-grid");
  const empty = document.querySelector("#empty-state");
  const search = document.querySelector("#search").value.trim().toLowerCase();
  const filter = document.querySelector("#match-filter").value;
  const visible = findings.filter((finding) => {
    const haystack = `${finding.product_name} ${finding.domain}`.toLowerCase();
    return (!search || haystack.includes(search)) &&
      (filter === "all" || finding.match_type === filter);
  });

  grid.innerHTML = visible
    .map((finding) => `
      <article class="result-card">
        <div>
          <div class="result-shop">${escapeHtml(finding.domain)}</div>
          <h3>${escapeHtml(finding.product_name)}</h3>
          <div class="result-meta">
            <span class="badge">${escapeHtml(evidenceLabel(finding.match_type))}</span>
            <span class="badge">Source: ${escapeHtml(finding.source)}</span>
          </div>
        </div>
        <div class="price-block">
          <strong>${escapeHtml(finding.price || "0 kr.")}</strong>
          <a href="${escapeHtml(safeUrl(finding.url))}" target="_blank" rel="noopener noreferrer">
            Open product ↗
          </a>
        </div>
      </article>
    `)
    .join("");
  empty.hidden = visible.length !== 0;
}

async function loadDashboard() {
  try {
    const [findingsResponse, statusesResponse, infoResponse] = await Promise.all([
      fetch(githubDataUrl("gratis_produkter.csv"), { cache: "no-store" }),
      fetch(githubDataUrl("scannede_webshops.csv"), { cache: "no-store" }),
      fetch(githubDataUrl("run-info.json"), { cache: "no-store" }),
    ]);
    if (!findingsResponse.ok || !statusesResponse.ok || !infoResponse.ok) {
      throw new Error("One or more dashboard data files could not be loaded.");
    }

    const findings = parseCsv(await findingsResponse.text());
    const statuses = latestStatuses(parseCsv(await statusesResponse.text()));
    const info = await infoResponse.json();
    const retryStatuses = new Set([
      "rate_limited", "blocked", "unreachable", "error", "no_html_pages",
    ]);
    const retryCount = statuses.filter((row) =>
      retryStatuses.has(row.status) || row.status?.startsWith("http_")
    ).length;
    const completed = Number(info.completed_shops || 0);
    const total = Number(info.total_shops || statuses.length || 0);
    const pages = statuses.reduce((sum, row) => sum + Number(row.pages_checked || 0), 0);
    const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : 0;

    document.querySelector("#metric-findings").textContent = numberFormatter.format(findings.length);
    document.querySelector("#metric-completed").textContent = numberFormatter.format(completed);
    document.querySelector("#metric-total").textContent = `of ${numberFormatter.format(total)} total`;
    document.querySelector("#metric-pages").textContent = numberFormatter.format(pages);
    document.querySelector("#metric-retry").textContent = numberFormatter.format(retryCount);
    document.querySelector("#progress-percent").textContent = `${percent}%`;
    document.querySelector("#progress-detail").textContent =
      `${numberFormatter.format(completed)} of ${numberFormatter.format(total)} shops complete`;
    document.querySelector("#progress-bar").style.width = `${percent}%`;

    const generated = info.generated_at ? new Date(info.generated_at) : null;
    const formattedDate = generated && !Number.isNaN(generated.valueOf())
      ? new Intl.DateTimeFormat("da-DK", { dateStyle: "medium", timeStyle: "short" }).format(generated)
      : "unknown";
    document.querySelector("#last-updated").textContent = `Last updated ${formattedDate}`;
    document.querySelector("#run-label").textContent =
      retryCount ? `${retryCount} shops queued for retry` : "Nightly crawl is up to date";

    const filter = document.querySelector("#match-filter");
    [...new Set(findings.map((row) => row.match_type).filter(Boolean))]
      .sort()
      .forEach((matchType) => {
        const option = document.createElement("option");
        option.value = matchType;
        option.textContent = evidenceLabel(matchType);
        filter.append(option);
      });

    document.querySelector("#search").addEventListener("input", () => renderResults(findings));
    filter.addEventListener("change", () => renderResults(findings));
    document.querySelector("#download-results").href = githubDataUrl("gratis_produkter.csv");
    document.querySelector("#download-status").href = githubDataUrl("scannede_webshops.csv");
    renderResults(findings);
  } catch (error) {
    console.error(error);
    document.querySelector("#run-label").textContent = "Dashboard data unavailable";
    document.querySelector("#empty-state").hidden = false;
    document.querySelector("#empty-state h3").textContent = "Could not load crawl data";
    document.querySelector("#empty-state p").textContent = "Try refreshing the page in a moment.";
  }
}

function renderRuns(runs = []) {
  const list = document.querySelector("#run-list");
  if (!runs.length) {
    list.innerHTML = '<div class="run-item"><strong>No runs yet</strong><span>Start the first crawl above.</span></div>';
    return;
  }
  list.innerHTML = runs.map((run) => {
    const state = run.status === "completed" ? (run.conclusion || "completed") : run.status;
    const date = new Intl.DateTimeFormat("da-DK", { dateStyle: "medium", timeStyle: "short" })
      .format(new Date(run.created_at));
    return `
      <div class="run-item">
        <strong>${escapeHtml(state)}</strong>
        <span>${escapeHtml(date)} · ${escapeHtml(run.event)}</span>
        <a href="${escapeHtml(safeUrl(run.html_url))}" target="_blank" rel="noopener noreferrer">Open log ↗</a>
      </div>`;
  }).join("");
}

async function refreshCrawlStatus() {
  const state = document.querySelector("#runner-state");
  state.textContent = "Refreshing…";
  try {
    const response = await fetch("/api/crawl/status", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load runner status.");
    renderRuns(payload.runs);
    const active = payload.runs?.find((run) => ["queued", "in_progress", "waiting"].includes(run.status));
    state.textContent = active ? "Crawler is running" : "Runner is idle";
  } catch (error) {
    state.textContent = "Available after Netlify setup";
    renderRuns([]);
    console.error(error);
  }
}

async function startCrawl(event) {
  event.preventDefault();
  const button = document.querySelector("#start-crawl");
  const message = document.querySelector("#console-message");
  const adminKey = document.querySelector("#admin-key").value;
  const maxRuntimeMinutes = document.querySelector("#run-duration").value;
  button.disabled = true;
  message.classList.remove("is-error");
  message.textContent = "Sending the job to the free runner…";
  try {
    const response = await fetch("/api/crawl/start", {
      method: "POST",
      headers: { "content-type": "application/json", "x-admin-key": adminKey },
      body: JSON.stringify({ maxRuntimeMinutes }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "The crawl could not be started.");
    message.textContent = "Crawler queued. GitHub normally starts it within a minute.";
    setTimeout(refreshCrawlStatus, 5000);
  } catch (error) {
    message.classList.add("is-error");
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

loadDashboard();
document.querySelector("#crawl-form").addEventListener("submit", startCrawl);
document.querySelector("#refresh-runs").addEventListener("click", refreshCrawlStatus);
refreshCrawlStatus();

"use strict";

// Helpers shared by every logged-in page. Loaded before the page script.

const $ = (id) => document.getElementById(id);
// The UI copy is English, so numbers and dates use English formatting too.
const numberFormat = new Intl.NumberFormat("en", { notation: "compact" });
const dateFormat = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child != null) node.append(child);
  }
  return node;
}

function relativeDate(iso) {
  if (!iso) return "";
  const days = Math.round((new Date(iso) - Date.now()) / 86400000);
  if (Math.abs(days) < 30) return dateFormat.format(days, "day");
  if (Math.abs(days) < 365) return dateFormat.format(Math.round(days / 30), "month");
  return dateFormat.format(Math.round(days / 365), "year");
}

function safeUrl(url) {
  try {
    return new URL(url).protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

function redirectIfLoggedOut(response) {
  if (response.status !== 401) return false;
  window.location.assign("/login");
  return true;
}

/** Fetch JSON from the companion API; throws Error(detail) on failure. */
async function api(path, options = {}) {
  const init = { ...options, headers: { ...(options.headers || {}) } };
  if (init.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  if (redirectIfLoggedOut(response)) return new Promise(() => {});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`);
  }
  return body;
}

/** Render the result of an install/update call into `container`. */
function showOutcome(container, outcome) {
  const lines = [];
  for (const item of outcome.installed || []) {
    lines.push(el("li", { class: "ok" },
      `Installed ${item.title} ${item.version_number}${item.dependency ? " (dependency)" : ""}`));
  }
  for (const item of outcome.updated || []) {
    lines.push(el("li", { class: "ok" },
      `Updated ${item.title} ${item.from_version || ""} → ${item.to_version}`));
  }
  for (const item of outcome.removed || []) {
    lines.push(el("li", { class: "ok" }, `Removed ${item.title} (${item.file_name})`));
  }
  for (const warning of outcome.warnings || []) {
    lines.push(el("li", { class: "warn" }, warning));
  }
  for (const item of outcome.skipped || []) {
    const line = el("li", { class: "warn" }, `Skipped ${item.title}: ${item.reason}`);
    const url = safeUrl(item.url);
    if (url) line.append(" ", el("a", { href: url, target: "_blank", rel: "noopener" }, "Download"));
    lines.push(line);
  }
  for (const item of outcome.failed || []) {
    lines.push(el("li", { class: "error" }, `Could not update ${item.title}: ${item.reason}`));
  }
  if (!lines.length) lines.push(el("li", {}, "Nothing to do."));
  container.replaceChildren(el("ul", {}, ...lines));
  if (outcome.restart_required) {
    container.append(el("p", { class: "restart" },
      "Restart the server (from its page here or in Crafty) to load the changes."));
  }
  container.hidden = false;
}

$("logout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  }).catch(() => {});
  window.location.assign("/login");
});

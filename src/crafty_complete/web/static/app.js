"use strict";

const state = {
  type: "mod",
  text: "",
  loaders: new Set(),
  categories: new Set(),
  gameVersion: "",
  serverCompatible: true,
  sort: "relevance",
  page: 1,
  pageSize: 20,
};

let searchController = null;

function renderCard(project) {
  const icon = el("img", {
    class: "card-icon",
    alt: "",
    loading: "lazy",
    width: "72",
    height: "72",
  });
  const iconUrl = safeUrl(project.icon_url);
  if (iconUrl) icon.src = iconUrl;

  const link = el("a", { href: safeUrl(project.url) || "#", target: "_blank", rel: "noopener" },
    project.title);
  const tags = el("div", { class: "tags" });
  for (const loader of project.loaders) tags.append(el("span", { class: "tag loader" }, loader));
  for (const category of project.categories) tags.append(el("span", { class: "tag" }, category));
  if (project.server_side === "unsupported") {
    tags.append(el("span", { class: "tag warn" }, "Client only"));
  }
  if (project.latest_game_version) {
    tags.append(el("span", { class: "tag" }, project.latest_game_version));
  }

  return el("li", { class: "card" },
    icon,
    el("div", {},
      el("h3", { class: "card-title" }, link, " ",
        el("span", { class: "card-author" }, `by ${project.author}`)),
      el("p", { class: "card-desc" }, project.description),
      tags),
    el("div", { class: "card-stats" },
      el("span", {}, el("strong", {}, numberFormat.format(project.downloads)), " downloads"),
      el("span", {}, `${numberFormat.format(project.follows)} followers`),
      el("span", {}, `Updated ${relativeDate(project.updated_at)}`),
      installButton(project)),
  );
}

function installButton(project) {
  if (project.project_type !== "mod") return null;
  const button = el("button", { class: "primary compact install" }, "Install");
  button.disabled = !$("target-server").value || project.server_side === "unsupported";
  if (project.server_side === "unsupported") button.title = "Client-only mods cannot run on a server";
  button.addEventListener("click", () => installMod(project, button));
  return button;
}

async function installMod(project, button) {
  const serverId = $("target-server").value;
  if (!serverId) return;
  button.disabled = true;
  button.textContent = "Installing…";
  try {
    const outcome = await api(`/api/servers/${encodeURIComponent(serverId)}/mods`, {
      method: "POST", body: { project_id: project.id },
    });
    showOutcome($("notice"), outcome);
    button.textContent = outcome.installed.length ? "Installed" : "Install";
    if (!outcome.installed.length) button.disabled = false;
  } catch (error) {
    $("notice").replaceChildren(el("p", { class: "error" }, `${project.title}: ${error.message}`));
    $("notice").hidden = false;
    button.textContent = "Install";
    button.disabled = false;
  }
}

async function loadTargetServers() {
  try {
    const servers = await api("/api/servers");
    $("target-server").append(...servers.map((s) => el("option", { value: s.server_id }, s.server_name)));
    try {
      const saved = localStorage.getItem("targetServer");
      if (saved && servers.some((s) => s.server_id === saved)) $("target-server").value = saved;
    } catch { /* storage unavailable */ }
  } catch {
    $("target-server").disabled = true;
  }
}

function setStatus(message, isError = false) {
  const status = $("status");
  status.textContent = message;
  status.classList.toggle("is-error", isError);
}

async function search() {
  searchController?.abort();
  searchController = new AbortController();

  const params = new URLSearchParams({
    q: state.text,
    type: state.type,
    sort: state.sort,
    page: String(state.page),
    page_size: String(state.pageSize),
    server_compatible: String(state.serverCompatible),
  });
  if (state.gameVersion) params.set("game_version", state.gameVersion);
  state.loaders.forEach((loader) => params.append("loader", loader));
  state.categories.forEach((category) => params.append("category", category));

  setStatus("Searching…");
  try {
    const response = await fetch(`/api/catalog/search?${params}`, {
      signal: searchController.signal,
    });
    if (redirectIfLoggedOut(response)) return;
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);

    $("results").replaceChildren(...body.items.map(renderCard));
    const pages = Math.max(1, Math.ceil(body.total / body.page_size));
    setStatus(body.total ? `${numberFormat.format(body.total)} results` : "No results found.");
    $("page-info").textContent = `Page ${body.page} of ${pages}`;
    $("prev").disabled = body.page <= 1;
    $("next").disabled = body.page >= pages;
  } catch (error) {
    if (error.name === "AbortError") return;
    $("results").replaceChildren();
    setStatus(`Search failed: ${error.message}`, true);
  }
}

function renderChecks(container, values, selected) {
  container.replaceChildren(...values.map((value) => {
    const input = el("input", { type: "checkbox", value });
    input.checked = selected.has(value);
    input.addEventListener("change", () => {
      if (input.checked) selected.add(value);
      else selected.delete(value);
      state.page = 1;
      search();
    });
    return el("label", { class: "check" }, input, value);
  }));
}

async function loadFilters() {
  try {
    const response = await fetch(`/api/catalog/filters?type=${state.type}`);
    if (redirectIfLoggedOut(response)) return;
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const filters = await response.json();

    renderChecks($("loader-options"), filters.loaders, state.loaders);
    renderChecks($("category-options"), filters.categories, state.categories);
    const versionSelect = $("game-version");
    versionSelect.replaceChildren(
      el("option", { value: "" }, "All versions"),
      ...filters.game_versions.map((version) => el("option", { value: version }, version)),
    );
    versionSelect.value = state.gameVersion;
  } catch (error) {
    setStatus(`Could not load filters: ${error.message}`, true);
  }
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function bindEvents() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      if (tab.dataset.type === state.type) return;
      document.querySelectorAll(".tab").forEach((other) => {
        const active = other === tab;
        other.classList.toggle("is-active", active);
        other.setAttribute("aria-selected", String(active));
      });
      state.type = tab.dataset.type;
      state.categories.clear();
      state.page = 1;
      $("search-input").placeholder = state.type === "mod" ? "Search mods…" : "Search modpacks…";
      loadFilters();
      search();
    });
  });

  $("search-input").addEventListener("input", debounce((event) => {
    state.text = event.target.value.trim();
    state.page = 1;
    search();
  }, 300));

  $("sort").addEventListener("change", (event) => {
    state.sort = event.target.value;
    state.page = 1;
    search();
  });

  $("game-version").addEventListener("change", (event) => {
    state.gameVersion = event.target.value;
    state.page = 1;
    search();
  });

  $("server-compatible").addEventListener("change", (event) => {
    state.serverCompatible = event.target.checked;
    state.page = 1;
    search();
  });

  $("prev").addEventListener("click", () => { state.page -= 1; search(); window.scrollTo(0, 0); });
  $("next").addEventListener("click", () => { state.page += 1; search(); window.scrollTo(0, 0); });

  $("target-server").addEventListener("change", (event) => {
    try { localStorage.setItem("targetServer", event.target.value); } catch { /* ignore */ }
    search();
  });

  $("toggle-filters").addEventListener("click", (event) => {
    const open = $("filters").classList.toggle("is-open");
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
}

bindEvents();
loadFilters();
// Search right away; a slow Crafty must not delay Modrinth results. When a saved target
// server comes back, search again so the Install buttons become active.
search();
loadTargetServers().then(() => { if ($("target-server").value) search(); });

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

const $ = (id) => document.getElementById(id);
// The UI copy is English, so numbers and dates use English formatting too.
const numberFormat = new Intl.NumberFormat("en", { notation: "compact" });
const dateFormat = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
let searchController = null;

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
      el("span", {}, `Updated ${relativeDate(project.updated_at)}`)),
  );
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

  $("toggle-filters").addEventListener("click", (event) => {
    const open = $("filters").classList.toggle("is-open");
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
}

bindEvents();
loadFilters();
search();
